"""临时 SQLite 与假客户端验证额度身份保护，不调用真实模型。"""
import queue
import time
import unittest
from unittest.mock import patch

from scripts import pair_quota
from scripts.pair_transport import TransportError
from tests import test_core
from tests import test_delegation


class QuotaGuardTests(unittest.TestCase):
    setUp=test_core.CoreTests.setUp
    tearDown=test_core.CoreTests.tearDown

    def members(self):
        with self.store.transaction() as db:
            return {r['id']:dict(r) for r in db.execute('SELECT * FROM members')}

    def run_query(self, mode, error, *, signals=()):
        calls=[]
        monitor=pair_quota.QuotaMonitor(self.store)
        class Client:
            _dead=False
            def __init__(self, **kwargs):
                self.signal_events=queue.Queue()
                for event in signals:self.signal_events.put(event)
            def __enter__(self):return self
            def __exit__(self,*args):self.close()
            def close(self):pass
            def validate(self,member):
                calls.append(member['id'])
                raise error
        with patch.object(pair_quota,'AppServer',Client):
            if mode=='refresh':pair_quota.refresh(self.store,max_age=0)
            else:
                if signals:monitor.clients['A']=Client()
                with patch.object(monitor.stop_event,'wait',side_effect=lambda _:monitor.stop_event.set()):
                    monitor.run()
        return calls

    def test_model_and_provider_suspend_only_exact_member_and_preserve_business(self):
        for mode in ('refresh','monitor'):
            for reason in ('Model changed','Model provider changed'):
                with self.subTest(mode=mode,reason=reason):
                    with self.store.transaction() as db:
                        db.execute("UPDATE members SET lifecycle='active',verified=1")
                    message=self.store.send('A','B','保留在途业务')
                    with self.store.transaction() as db:
                        db.execute("UPDATE messages SET delivery_status='unknown',process_status='claimed',claim_token='keep' WHERE id=?",(message['id'],))
                        before=dict(db.execute('SELECT * FROM messages WHERE id=?',(message['id'],)).fetchone())
                    original=pair_quota.read_member
                    def read(store,member,client):
                        if member['id']=='A':return original(store,member,client)
                    with patch.object(pair_quota,'read_member',side_effect=read),patch.object(self.store,'_invalidate_approvals',wraps=self.store._invalidate_approvals) as revoke:
                        self.run_query(mode,TransportError(reason+' / 身份变化'))
                        revoke.assert_called_once()
                    members=self.members()
                    self.assertEqual((members['A']['lifecycle'],members['A']['verified']),('suspended',0))
                    self.assertEqual((members['B']['lifecycle'],members['B']['verified']),('active',1))
                    with self.store.transaction() as db:
                        self.assertEqual(dict(db.execute('SELECT * FROM messages WHERE id=?',(message['id'],)).fetchone()),before)

    def test_account_change_suspends_same_home_but_not_other_home(self):
        for mode in ('refresh','monitor'):
            for same_home in (True,False):
                with self.subTest(mode=mode,same_home=same_home):
                    with self.store.transaction() as db:
                        db.execute("UPDATE members SET lifecycle='active',verified=1")
                        home=db.execute("SELECT codex_home FROM members WHERE id='A'").fetchone()[0]
                        db.execute('UPDATE members SET codex_home=? WHERE id=?',(home if same_home else str(self.root/'other-home'),'B'))
                    original=pair_quota.read_member
                    def read(store,member,client):
                        if member['id']=='A':return original(store,member,client)
                    with patch.object(pair_quota,'read_member',side_effect=read):
                        self.run_query(mode,TransportError('Account changed / 身份变化'))
                    members=self.members()
                    self.assertEqual(members['A']['lifecycle'],'suspended')
                    self.assertEqual(members['B']['lifecycle'],'suspended' if same_home else 'active')

    def test_network_error_preserves_fresh_zero_quota_and_does_not_suspend(self):
        for mode in ('refresh','monitor'):
            for error in (TransportError('RPC failed: Model changed upstream'),OSError('network unavailable')):
                with self.subTest(mode=mode,error=str(error)):
                    with self.store.transaction() as db:db.execute("UPDATE members SET lifecycle='active',verified=1")
                    for member in ('A','B'):
                        self.store.save_quota(member,{'observed_at':time.time(),'reliability':'fresh','remaining':0,'windows':[]})
                    self.run_query(mode,error)
                    for member in self.members().values():self.assertEqual(member['lifecycle'],'active')
                    for saved in self.store.quota().values():
                        self.assertEqual(saved['remaining'],0)
                        self.assertEqual(saved['reliability'],'fresh')
                        self.assertEqual(saved['refresh_status'],'unavailable')

    def test_retired_binding_is_not_queried_or_resurrected_by_account_change(self):
        for mode in ('refresh','monitor'):
            with self.subTest(mode=mode):
                with self.store.transaction() as db:
                    db.execute("UPDATE members SET lifecycle='active',verified=1 WHERE id='A'")
                    db.execute("UPDATE members SET lifecycle='retired',verified=0 WHERE id='B'")
                calls=self.run_query(mode,TransportError('Account changed / 身份变化'))
                self.assertEqual(calls,['A'])
                self.assertEqual(self.members()['B']['lifecycle'],'retired')

    def test_account_notification_keeps_retired_binding_retired(self):
        with self.store.transaction() as db:db.execute("UPDATE members SET lifecycle='retired' WHERE id='B'")
        self.run_query('monitor',OSError('offline'),signals=({'method':'account/updated'},))
        self.assertEqual(self.members()['A']['lifecycle'],'active')
        self.assertEqual(self.members()['B']['lifecycle'],'retired')

    def test_account_notification_still_suspends_when_validation_detects_change(self):
        self.run_query('monitor',TransportError('Account changed / identity differs'),
                       signals=({'method':'account/updated'},))
        self.assertEqual(self.members()['A']['lifecycle'],'suspended')

    def test_account_notification_forces_validation_with_fresh_snapshot(self):
        self.store.save_quota('A',{'observed_at':time.time(),'reliability':'fresh','remaining':63,'windows':[]})
        calls=self.run_query('monitor',OSError('offline'),signals=({'method':'account/updated'},))
        self.assertIn('A',calls)
        self.assertEqual(self.members()['A']['lifecycle'],'active')
        self.assertEqual(self.store.quota()['A']['remaining'],63)

    def test_old_query_failure_cannot_suspend_or_overwrite_replacement(self):
        old=self.members()['A']
        current={'observed_at':time.time(),'reliability':'fresh','remaining':63,'windows':[]}
        self.store.save_quota('A',current)
        with self.store.transaction() as db:
            db.execute("UPDATE members SET binding_version=binding_version+1 WHERE id='A'")
        pair_quota.query_failed(self.store,old,{'remaining':0},TransportError('Account changed / 旧请求晚到'))
        self.assertEqual(self.members()['A']['lifecycle'],'active')
        self.assertEqual(self.store.quota()['A']['remaining'],63)

    def test_old_success_cannot_overwrite_replacement_quota(self):
        old=self.members()['A']
        self.store.save_quota('A',{'observed_at':time.time(),'reliability':'fresh','remaining':63,'windows':[]})
        class Client:
            def validate(inner,member):
                with self.store.transaction() as db:
                    db.execute("UPDATE members SET binding_version=binding_version+1 WHERE id='A'")
                return {'modelProvider':'deepseek'}
        pair_quota.read_member(self.store,old,Client())
        self.assertEqual(self.store.quota()['A']['remaining'],63)

    def test_monitor_replaces_cached_client_after_binding_changes(self):
        monitor=pair_quota.QuotaMonitor(self.store,member_id='A')
        clients=[];waits=[]
        class Client:
            _dead=False
            def __init__(inner,**kwargs):
                inner.home=kwargs['codex_home'];inner.closed=False
                inner.signal_events=queue.Queue();clients.append(inner)
            def validate(inner,member):raise OSError('network unavailable')
            def close(inner):inner.closed=True
        def boundary(_):
            waits.append(True)
            if len(waits)==1:
                with self.store.transaction() as db:
                    db.execute("UPDATE members SET codex_home=?,binding_version=binding_version+1 WHERE id='A'",(str(self.root/'replacement-home'),))
            else:monitor.stop_event.set()
        with patch.object(pair_quota,'AppServer',Client),patch.object(monitor.stop_event,'wait',side_effect=boundary):
            monitor.run()
        self.assertEqual(len(clients),2)
        self.assertNotEqual(clients[0].home,clients[1].home)
        self.assertTrue(all(c.closed for c in clients))

    def check_approval_revocation(self, mode, allowed):
        helper=test_delegation.DelegationTests()
        helper.store,helper.sessions,helper.directories,helper.root=self.store,self.sessions,self.directories,self.root
        helper.enable()
        request=helper.request()
        if allowed:helper.allow(request)
        self.run_query(mode,TransportError('Model changed / 身份变化'))
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM approval_requests WHERE id=?',(request['id'],)).fetchone()
            self.assertEqual(row['status'],'expired')
            self.assertEqual(row['retry_state'],'cancelled')

    def test_refresh_expires_pending_approval(self):self.check_approval_revocation('refresh',False)

    def test_refresh_expires_unconsumed_allow(self):self.check_approval_revocation('refresh',True)

    def test_monitor_expires_pending_approval(self):self.check_approval_revocation('monitor',False)

    def test_monitor_expires_unconsumed_allow(self):self.check_approval_revocation('monitor',True)


if __name__=='__main__':unittest.main()
