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


def binding_current(db,member):
    current=db.execute("SELECT * FROM members WHERE id=? AND lifecycle<>'retired'",(member['id'],)).fetchone()
    return bool(current and all(current[key]==member.get(key) for key in
        ('session_uuid','codex_home','cwd','proof','binding_version')))


def save_current_quota(store,member,data):
    # 查询发生在事务外；落盘时再次核对绑定，不能污染替换后的成员。
    with store.transaction() as db:
        if not binding_current(db,member):return False
        db.execute('INSERT INTO quota_snapshots VALUES(?,?) ON CONFLICT(member) DO UPDATE SET data=excluded.data',
            (member['id'],json.dumps(data,ensure_ascii=False)))
        return True


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
    save_current_quota(store,member,data)
    return data


def suspend_identity(store, member, reason, *, account=False):
    with store.transaction() as db:
        if not binding_current(db,member):return
        bound=db.execute("SELECT * FROM members WHERE id=? AND lifecycle<>'retired'",(member['id'],)).fetchone()
        if not bound:return
        if account:
            db.execute("UPDATE members SET lifecycle='suspended',verified=0 WHERE codex_home=? AND lifecycle<>'retired'",(bound['codex_home'],))
        else:
            db.execute("UPDATE members SET lifecycle='suspended',verified=0 WHERE id=?",(member['id'],))
        store._invalidate_approvals(db,reason)
        store.event(db,'member_configuration_suspended',member=member['id'],reason=reason,reverification_required=True)


def query_failed(store, member, previous, error):
    # 只识别校验器的明确错误前缀，普通 RPC 错误仍保留旧额度保护。
    reason=str(error)
    if isinstance(error,TransportError) and reason.partition(' / ')[0] in (
            'Model changed','Model provider changed','Account changed','account mismatch'):
        suspend_identity(store,member,reason,account=reason.startswith(('Account changed','account mismatch')))
    save_current_quota(store,member,dict(previous,
        reliability=previous.get('reliability') if previous.get('reliability')=='fresh' else 'unavailable',
        refresh_status='unavailable',attempted_at=time.time()))


def refresh(store, *, executable=None, max_age=60):
    quotas=store.quota()
    with store.transaction() as db:members=[dict(r) for r in db.execute("SELECT * FROM members WHERE lifecycle<>'retired'")]
    for member in members:
        previous=quotas.get(member['id'],{})
        if previous.get('age_seconds',121)<max_age and previous.get('reliability')=='fresh':continue
        try:
            with AppServer(codex_home=member['codex_home'],cwd=member['cwd'],executable=executable,
                           timeout=3,metadata_only=True,account_metadata=True) as client:
                read_member(store,member,client)
        except (TransportError,OSError,ValueError,KeyError) as exc:
            query_failed(store,member,previous,exc)


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
        due={};bindings={}
        try:
            while not self.stop_event.is_set():
                with self.store.transaction() as db:
                    members=[dict(r) for r in db.execute("SELECT * FROM members WHERE lifecycle<>'retired' AND (? IS NULL OR id=?)",(self.member_id,self.member_id))]
                    complex_wait=db.execute("SELECT 1 FROM tasks WHERE status='pending' AND assignment_mode='auto' AND effort>=5").fetchone()
                for member in members:
                    if self.stop_event.is_set():break
                    key=member['id'];client=self.clients.get(key)
                    identity=tuple(member[k] for k in ('session_uuid','codex_home','cwd','proof','binding_version'))
                    if key in bindings and bindings[key]!=identity:
                        if client:client.close()
                        self.clients.pop(key,None);client=None;due.pop(key,None)
                    bindings[key]=identity
                    force=self.refresh_event.is_set()
                    if client:
                        while True:
                            try:event=client.signal_events.get_nowait()
                            except queue.Empty:break
                            params=event.get('params',{})
                            if event.get('method')=='thread/tokenUsage/updated':self.store.record_usage(key,params)
                            else:
                                force=True
                                # 通知也会在初始化时出现；刷新后由 validate 比较真实身份。
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
                    except (TransportError,OSError,ValueError,KeyError) as exc:
                        query_failed(self.store,member,saved,exc)
                self.refresh_event.clear()
                self.stop_event.wait(.25)
        finally:
            for client in self.clients.values():client.close()
