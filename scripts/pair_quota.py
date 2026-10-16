"""在独立本地线程读取成员额度；查询失败不占用桥的投递线程或数据库事务。"""
from __future__ import annotations

import json
import queue
import threading
import time

try:
    from .pair_transport import AppServer, TransportError, canonical_path
    from . import pair_balance
except ImportError:
    from pair_transport import AppServer, TransportError, canonical_path
    import pair_balance


def read_member(store, member, client):
    thread=client.validate(member)
    if thread.get('modelProvider')!='openai':
        data={'observed_at':time.time(),'reliability':'unsupported','remaining':None,'windows':[],
              'pool':pair_balance.pool_key({},canonical_path(member['codex_home']))}
    else:
        identity=client.rpc('account/read',{'refreshToken':False},timeout=3)
        raw=client.rpc('account/rateLimits/read',{},timeout=3)
        data=pair_balance.snapshot(raw,model=thread.get('model'),limit_id=thread.get('rateLimitId'),
            account=pair_balance.pool_key(identity,canonical_path(member['codex_home'])))
    store.save_quota(member['id'],data)
    return data


def refresh(store, *, executable=None, max_age=60):
    quotas=store.quota()
    with store.transaction() as db:members=[dict(r) for r in db.execute('SELECT * FROM members')]
    for member in members:
        previous=quotas.get(member['id'],{})
        if previous.get('age_seconds',121)<max_age and previous.get('reliability')=='fresh':continue
        try:
            with AppServer(codex_home=member['codex_home'],cwd=member['cwd'],executable=executable,
                           timeout=3,metadata_only=True,account_metadata=True) as client:
                read_member(store,member,client)
        except (TransportError,OSError,ValueError,KeyError):
            store.save_quota(member['id'],dict(previous,
                reliability=previous.get('reliability') if previous.get('reliability')=='fresh' else 'unavailable',
                refresh_status='unavailable',attempted_at=time.time()))


class QuotaMonitor:
    def __init__(self, store, *, executable=None, member_id=None):
        self.store,self.executable,self.member_id=store,executable,member_id
        self.stop_event=threading.Event()
        self.refresh_event=threading.Event()
        self.thread=threading.Thread(target=self.run,name='codex-pair-quota',daemon=True)
        self.clients={}

    def start(self):self.thread.start()

    def close(self):
        self.stop_event.set()
        self.thread.join(timeout=20)

    def run(self):
        due={}
        try:
            while not self.stop_event.is_set():
                with self.store.transaction() as db:
                    members=[dict(r) for r in db.execute('SELECT * FROM members WHERE ? IS NULL OR id=?',(self.member_id,self.member_id))]
                    complex_wait=db.execute("SELECT 1 FROM tasks WHERE status='pending' AND assignment_mode='auto' AND effort>=5").fetchone()
                for member in members:
                    if self.stop_event.is_set():break
                    key=member['id'];client=self.clients.get(key)
                    force=self.refresh_event.is_set()
                    if client:
                        while True:
                            try:event=client.signal_events.get_nowait()
                            except queue.Empty:break
                            params=event.get('params',{})
                            if event.get('method')=='thread/tokenUsage/updated':self.store.record_usage(key,params)
                            else:force=True
                    saved=self.store.quota().get(key,{})
                    interval=30 if complex_wait else 60
                    if complex_wait and time.time()-saved.get('attempted_at',saved.get('observed_at',0))>30:force=True
                    checked=max(saved.get('observed_at',0),saved.get('attempted_at',0))
                    reset_due=any(checked<w['resets_at']<=time.time() for w in saved.get('windows',[]))
                    if not force and not reset_due and time.monotonic()<due.get(key,0):continue
                    due[key]=time.monotonic()+interval
                    try:
                        if not client or client._dead:
                            if client:client.close()
                            client=AppServer(codex_home=member['codex_home'],cwd=member['cwd'],executable=self.executable,
                                timeout=3,metadata_only=True,account_metadata=True)
                            self.clients[key]=client
                        read_member(self.store,member,client)
                    except (TransportError,OSError,ValueError,KeyError):
                        self.store.save_quota(key,dict(saved,
                            reliability=saved.get('reliability') if saved.get('reliability')=='fresh' else 'unavailable',
                            refresh_status='unavailable',attempted_at=time.time()))
                self.refresh_event.clear()
                self.stop_event.wait(.25)
        finally:
            for client in self.clients.values():client.close()
