"""原窗口历史终态不完整时的双向检测；不启动模型或接触业务文件。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.pair_core import Daemon
from scripts.pair_transport import AppServer
from tests import test_core


def event(kind, turn, **extra):
    return {'type':'event_msg','payload':{'type':kind,'turn_id':turn,**extra}}


class RolloutTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pair-rollout-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.member={'session_uuid':'00000000-0000-0000-0000-000000000001',
            'cwd':str(self.root),'codex_home':str(self.root)}
        (self.root/'sessions').mkdir()
        self.path=self.root/'sessions'/'rollout.jsonl'
        self.client=AppServer.__new__(AppServer);self.client.metadata_only=True
        self.write({'type':'session_meta','payload':{'id':self.member['session_uuid'],'cwd':str(self.root)}})

    def write(self, *records):
        with self.path.open('a',encoding='utf-8') as file:
            for record in records:file.write(json.dumps(record)+'\n')

    def observe(self, *, processed=True, thread_status='notLoaded',**context):
        with patch.object(self.client,'validate',return_value={'path':str(self.path),'status':{'type':thread_status}}), \
                patch.object(self.client,'turns',return_value=iter([
                    {'id':'old','status':'interrupted','completedAt':None,'items':[]}])):
            return self.client.observe(self.member,{'turn_id':'old','process_status':'completed' if processed else 'claimed',**context})

    def test_exact_persisted_completion_works_with_notloaded_observer(self):
        self.write(event('task_started','old'),event('task_complete','old',completed_at=123))
        result=self.observe()
        self.assertEqual(result['remote_status'],'completed')
        self.assertEqual(result['completion_source'],'rollout_task_complete')
        self.assertEqual(result['completed_at'],123)

    def test_idle_status_without_persisted_completion_is_insufficient(self):
        self.write(event('task_started','old'))
        self.assertEqual(self.observe(thread_status='idle')['remote_status'],'inProgress')

    def test_newer_active_turn_does_not_release_shared_workspace(self):
        self.write(event('task_started','old'),event('task_complete','old',completed_at=123),event('task_started','new'))
        self.assertEqual(self.observe()['remote_status'],'inProgress')
        self.write(event('task_complete','new',completed_at=124))
        self.assertEqual(self.observe()['remote_status'],'completed')

    def test_superseded_turn_requires_processed_message_and_later_completion(self):
        self.write(event('task_started','old'),event('task_started','new'),event('task_complete','new',completed_at=124))
        self.assertEqual(self.observe(processed=False)['remote_status'],'inProgress')
        result=self.observe()
        self.assertEqual(result['remote_status'],'interrupted')
        self.assertEqual(result['evidence_turn_id'],'new')

    def test_timestamped_interruption_can_recover_only_after_later_completion(self):
        self.write(event('task_started','old'),event('task_started','new'))
        def observe(processed=True):
            with patch.object(self.client,'validate',return_value={'path':str(self.path)}), \
                    patch.object(self.client,'turns',return_value=iter([
                        {'id':'old','status':'interrupted','completedAt':120,'items':[]}])):
                return self.client.observe(self.member,{'turn_id':'old','process_status':'completed' if processed else 'unclaimed'})
        self.assertNotIn('completion_source',observe())
        self.write(event('task_complete','new',completed_at=124))
        self.assertNotIn('completion_source',observe(False))
        self.assertEqual(observe()['completion_source'],'rollout_superseded')

    def test_protocol_yield_requires_latest_persisted_completion(self):
        self.write(event('task_started','old'),event('task_started','new'))
        self.assertEqual(self.observe(processed=False,protocol_yield=True)['remote_status'],'inProgress')
        self.write(event('task_complete','new',completed_at=124))
        self.assertEqual(self.observe(processed=False,protocol_yield=True)['remote_status'],'interrupted')
        self.assertEqual(self.observe(processed=False)['remote_status'],'inProgress')

    def test_resumed_coordination_turn_must_match_completed_evidence(self):
        self.write(event('task_started','old'),event('task_started','new'),event('task_complete','new',completed_at=124))
        self.assertEqual(self.observe(processed=False,protocol_yield_turns=['unrelated'])['remote_status'],'inProgress')
        self.assertEqual(self.observe(processed=False,protocol_yield_turns=['new'])['remote_status'],'interrupted')

    def test_partial_line_waits_then_recovers_and_survives_observer_restart(self):
        self.write(event('task_started','old'))
        data=json.dumps(event('task_complete','old',completed_at=123)).encode()
        with self.path.open('ab') as file:file.write(data[:20])
        self.assertEqual(self.observe()['remote_status'],'inProgress')
        with self.path.open('ab') as file:file.write(data[20:]+b'\n')
        self.assertEqual(self.observe()['remote_status'],'completed')
        self.client=AppServer.__new__(AppServer);self.client.metadata_only=True
        self.assertEqual(self.observe()['remote_status'],'completed')

    def test_unrelated_turn_or_missing_timestamp_is_insufficient(self):
        self.write(event('task_started','other'),event('task_complete','other',completed_at=123))
        self.assertEqual(self.observe()['remote_status'],'inProgress')
        self.write(event('task_started','old'),event('task_complete','old'))
        self.assertEqual(self.observe()['remote_status'],'inProgress')

    def test_outside_account_directory_is_rejected(self):
        self.write(event('task_started','old'),event('task_complete','old',completed_at=123))
        outside=self.root/'outside.jsonl';outside.write_bytes(self.path.read_bytes());self.path=outside
        self.assertEqual(self.observe()['remote_status'],'inProgress')

    @unittest.skipUnless(os.name=='nt','Windows 扩展路径前缀')
    def test_windows_extended_path_keeps_account_boundary(self):
        self.write(event('task_started','old'),event('task_complete','old',completed_at=123))
        self.path=Path('\\\\?\\'+str(self.path))
        self.assertEqual(self.observe()['remote_status'],'completed')

    def test_wrong_session_or_cwd_is_rejected(self):
        for change in ({'id':'other'},{'cwd':str(self.root/'other')}):
            with self.subTest(change=change):
                self.path.unlink()
                self.write({'type':'session_meta','payload':{'id':self.member['session_uuid'],'cwd':str(self.root),**change}},
                    event('task_started','old'),event('task_complete','old',completed_at=123))
                self.client=AppServer.__new__(AppServer);self.client.metadata_only=True
                self.assertEqual(self.observe()['remote_status'],'inProgress')

    def test_corrupt_record_fails_closed(self):
        self.write(event('task_started','old'),[],event('task_complete','old',completed_at=123))
        self.assertEqual(self.observe()['remote_status'],'inProgress')


class BidirectionalCompletionTests(unittest.TestCase):
    setUp=test_core.CoreTests.setUp
    tearDown=test_core.CoreTests.tearDown

    def check_direction(self, first, second):
        with self.store.transaction() as db:
            for member in ('A','B'):
                proof={'git':{'path':str(self.project)}}
                db.execute('UPDATE members SET proof=? WHERE id=?',(json.dumps(proof),member))
        old=self.store.send(second,first,'阶段已收尾通知',kind='note')
        fake=test_core.FakeTransport(self.store);daemon=Daemon(self.store,fake)
        daemon.dispatch_one()
        claim=self.store.claim(first,self.sessions[first],self.directories[first],message_id=old['id'])
        self.store.complete(first,self.sessions[first],self.directories[first],old['id'],claim['claim_token'],'已读并收尾')
        following=self.store.assign('next','明确的后续任务',second,['next.py'])
        self.assertEqual(following['status'],'active')
        self.assertFalse(daemon.dispatch_one())
        directory=self.home/'sessions';directory.mkdir()
        path=directory/'rollout.jsonl'
        turn='turn-'+old['id']
        records=[{'type':'session_meta','payload':{'id':self.sessions[first],'cwd':self.directories[first]}},
            event('task_started',turn),event('task_complete',turn,completed_at=123)]
        path.write_text(''.join(json.dumps(r)+'\n' for r in records),encoding='utf-8')
        client=AppServer.__new__(AppServer);client.metadata_only=True
        with patch.object(client,'validate',return_value={'path':str(path),'status':{'type':'notLoaded'}}), \
                patch.object(client,'turns',side_effect=lambda _:iter([{'id':turn,'status':'interrupted','completedAt':None,'items':[]}])):
            fake.observe=client.observe
            daemon.reconcile()
            # 桥重启使用相同数据库，不重新派发已处理的通知。
            daemon=Daemon(self.store,fake)
            self.assertTrue(daemon.dispatch_one())
            self.assertEqual(fake.calls[-1][0]['id'],second)
            self.assertEqual(fake.calls[-1][1]['task_id'],'next')
            self.assertFalse(daemon.dispatch_one())
        self.assertEqual(len(fake.calls),2)

    def test_a_completion_dispatches_b_task(self):
        self.check_direction('A','B')

    def test_b_completion_dispatches_a_task(self):
        self.check_direction('B','A')
