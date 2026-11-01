"""低额度活动回合止损：只用临时库和假RPC，不中断真实会话。"""
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from scripts.pair_core import Daemon
from scripts.pair_transport import TransportPool,QueueTransport,TransportError
from tests import test_core
from tests.test_assistance import quota


class QuotaStopTests(unittest.TestCase):
    setUp=test_core.CoreTests.setUp
    tearDown=test_core.CoreTests.tearDown

    def daemon(self,result=None,error=None,*,shared=False):
        if not shared:
            # 原通用夹具共用home；独立账号必须显式分开，不能靠周期时间差避开共享池。
            home=self.root/'codex-home-b';home.mkdir()
            with self.store.transaction() as db:
                db.execute("UPDATE members SET codex_home=? WHERE id='B'",(str(home.resolve()),))
        self.quota_now=time.time()
        self.store.setting('quota_checkpoints',True)
        self.store.save_quota('A',quota(80,pool='a',now=self.quota_now))
        self.store.save_quota('B',quota(6,pool='b',now=self.quota_now))
        self.store.schedule()
        d=object.__new__(Daemon);d.store=self.store;d.member_id=None
        d.transport=SimpleNamespace(stop_for_quota=Mock(return_value=result,side_effect=error))
        return d

    def test_stop_requested_once_preserves_business_and_pauses(self):
        d=self.daemon({'state':'interrupt_requested','turn_id':'exact'})
        quotas=self.store.quota()
        self.assertNotEqual(self.store.member('A')['codex_home'],self.store.member('B')['codex_home'])
        self.assertEqual([quotas[m]['pool'] for m in ('A','B')],['a','b'])
        self.assertEqual([quotas[m]['remaining'] for m in ('A','B')],[80,6])
        self.assertEqual(quotas['A']['windows'][0]['resets_at'],quotas['B']['windows'][0]['resets_at'])
        d.quota_stop_once();d.quota_stop_once()
        self.assertEqual(d.transport.stop_for_quota.call_count,1)
        self.assertEqual([c.args[0]['id'] for c in d.transport.stop_for_quota.call_args_list],['B'])
        self.assertEqual(self.store.status()['pair']['status'],'paused')
        self.assertFalse(self.store.status()['assistance']['checkpoints'])

    def test_failure_pauses_without_retry_or_false_completion(self):
        d=self.daemon(error=TransportError('RPC result unknown'))
        d.quota_stop_once();d.quota_stop_once()
        self.assertEqual(d.transport.stop_for_quota.call_count,1)
        self.assertEqual([c.args[0]['id'] for c in d.transport.stop_for_quota.call_args_list],['B'])
        self.assertEqual(self.store.status()['pair']['status'],'paused')

    def test_explicit_pause_never_interrupts(self):
        d=self.daemon({'state':'idle'})
        self.store.control('paused');d.quota_stop_once()
        d.transport.stop_for_quota.assert_not_called()

    def test_explicit_stop_never_interrupts(self):
        d=self.daemon({'state':'idle'})
        self.store.control('stopped');d.quota_stop_once()
        d.transport.stop_for_quota.assert_not_called()

    def test_shared_pool_low_water_selects_each_member_once_before_pause(self):
        d=self.daemon({'state':'interrupt_requested','turn_id':'exact'},shared=True)
        now=self.quota_now
        self.store.save_quota('A',quota(6,pool='shared',now=now))
        self.store.save_quota('B',quota(80,pool='shared',now=now))
        quotas=self.store.quota()
        self.assertEqual(self.store.member('A')['codex_home'],self.store.member('B')['codex_home'])
        self.assertEqual([quotas[m]['pool'] for m in ('A','B')],['shared','shared'])
        self.assertEqual([quotas[m]['remaining'] for m in ('A','B')],[6,6])
        self.assertEqual([(w['duration_mins'],w['resets_at']) for w in quotas['A']['windows']],
                         [(w['duration_mins'],w['resets_at']) for w in quotas['B']['windows']])
        self.store.schedule();d.quota_stop_once();d.quota_stop_once()
        self.assertEqual({c.args[0]['id'] for c in d.transport.stop_for_quota.call_args_list},{'A','B'})
        self.assertEqual(d.transport.stop_for_quota.call_count,2)
        self.assertEqual(self.store.status()['pair']['status'],'paused')

    def test_idle_only_once_and_does_not_resume(self):
        d=self.daemon({'state':'idle'})
        d.quota_stop_once();d.quota_stop_once()
        self.assertEqual(d.transport.stop_for_quota.call_count,1)
        self.assertEqual([c.args[0]['id'] for c in d.transport.stop_for_quota.call_args_list],['B'])
        self.assertEqual(self.store.status()['pair']['status'],'running')

    def test_exact_active_turn_rpc_and_ambiguous_turn_rejection(self):
        from tests.test_quota_interrupt_rpc import QuotaInterruptRpcTests
        helper=QuotaInterruptRpcTests();helper.__dict__.update(self.__dict__)
        pool,observer,member,sent,thread,turns=helper.fixture()
        self.assertEqual(pool.stop_for_quota(member)['turn_id'],'exact-turn')
        self.assertEqual([r['params'] for r in sent if r['method']=='turn/interrupt'],
                         [{'threadId':member['session_uuid'],'turnId':'exact-turn'}])
        sent.clear();turns[:]=[{'id':'a','status':'inProgress'},{'id':'b','status':'inProgress'}]
        with self.assertRaises(TransportError):pool.stop_for_quota(member)
        self.assertFalse(any(r['method']=='turn/interrupt' for r in sent))

    def test_all_low_members_selected_before_pause(self):
        d=self.daemon({'state':'interrupt_requested','turn_id':'exact'})
        self.store.save_quota('A',quota(6,pool='a',now=self.quota_now));self.store.schedule()
        d.quota_stop_once()
        self.assertEqual(d.transport.stop_for_quota.call_count,2)

    def test_managed_backend_never_creates_or_resumes_client(self):
        pool=object.__new__(TransportPool);pool.client=Mock()
        with self.assertRaises(TransportError):pool.stop_for_quota({'backend':'managed'})
        pool.client.assert_not_called()
