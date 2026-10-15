"""长审查结果的短通知、分页读取与确定未发送失败恢复。"""
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import pair
from scripts.pair_core import Daemon, PairError
from scripts.pair_transport import DeliveryUnknown, TransportError, QueueTransport
from tests import test_claim_recovery
from tests.test_core import FakeTransport


LENGTH_ERROR='Queue message exceeds Windows command-line limit / 消息超过 Windows 命令行长度限制，请使用 managed 后端'


class LongMessageTests(unittest.TestCase):
    def setUp(self):
        test_claim_recovery.ClaimRecoveryTests.setUp(self)

    def claim(self,message):
        return test_claim_recovery.ClaimRecoveryTests.claim(self,message)

    def read(self,message,**kwargs):
        m=self.members['B']
        return self.store.read_message('B',m['session_uuid'],m['cwd'],message['id'],**kwargs)

    def test_large_unicode_review_roundtrip_and_compact_persisted_notification(self):
        body=('审查结果：中文🙂引号"路径C:\\测试\r\n'*10000)
        message=self.store.send('A','B',body,dedupe_key='review')
        transport=FakeTransport(self.store)
        self.assertTrue(Daemon(self.store,transport).dispatch_one())
        envelope=transport.calls[0][1]['envelope']
        self.assertLess(len(subprocess.list2cmdline(['codex','queue','--message',envelope])),30000)
        payload=json.loads(envelope)
        self.assertTrue(payload['content_external'])
        self.assertIsNone(payload['body'])
        self.assertIn('read-message',payload['read_argv'])
        with self.assertRaises(PairError):self.read(message)
        claimed=self.claim(message)
        self.assertTrue(claimed['claimed'])
        chunks=[];offset=0
        while offset is not None:
            page=self.read(message,offset=offset)
            self.assertLessEqual(len(page['text']),2000)
            self.assertNotIn('claim_token',page)
            chunks.append(page['text']);offset=page['next_offset']
        self.assertEqual(''.join(chunks),body)
        m=self.members['B']
        self.store.complete('B',m['session_uuid'],m['cwd'],message['id'],claimed['claim_token'],'审查已整合')
        self.assertFalse(self.claim(message)['claimed'])

    def test_legacy_failure_recovers_same_id_history_and_dedupe(self):
        message=self.store.send('A','B','长审查'*20000,dedupe_key='review')
        daemon=Daemon(self.store,FakeTransport(error=TransportError(LENGTH_ERROR)),max_attempts=1)
        self.assertTrue(daemon.dispatch_one())
        self.assertEqual(self.claim(message)['reason'],'delivery_failed')
        with self.store.transaction() as db:
            # 模拟旧版本持久化的过长信封。
            db.execute('UPDATE messages SET envelope=? WHERE id=?',('legacy'*20000,message['id']))
        before=self.store.message(message['id'])
        self.assertTrue(self.store.recover_delivery(message['id'])['recovered'])
        self.assertFalse(self.store.recover_delivery(message['id'])['recovered'])
        transport=FakeTransport(self.store)
        self.assertTrue(Daemon(self.store,transport,max_attempts=1).dispatch_one())
        self.assertEqual(transport.calls[0][1]['id'],message['id'])
        after=self.store.message(message['id'])
        for key in ('id','body','dedupe_key','created'):self.assertEqual(before[key],after[key])
        self.assertEqual(after['attempts'],2)
        with self.store.transaction() as db:
            attempts=db.execute('SELECT outcome,detail FROM attempts WHERE message_id=? ORDER BY number',(message['id'],)).fetchall()
        self.assertEqual(attempts[0]['detail'],LENGTH_ERROR)
        self.assertEqual([a['outcome'] for a in attempts],['failed','accepted'])
        self.assertTrue(self.claim(message)['claimed'])
        self.assertFalse(self.store.recover_delivery(message['id'])['recovered'])

    def test_unknown_and_other_failures_are_not_requeued(self):
        for error in (DeliveryUnknown(LENGTH_ERROR),TransportError('other failure')):
            message=self.store.send('A','B','review')
            Daemon(self.store,FakeTransport(error=error)).dispatch_one()
            before=self.store.message(message['id'])
            self.assertFalse(self.store.recover_delivery(message['id'])['recovered'])
            self.assertEqual(self.store.message(message['id']),before)
            with self.store.transaction() as db:
                db.execute("UPDATE messages SET delivery_status='failed' WHERE id=?",(message['id'],))

    def test_task_metadata_read_and_page_validation(self):
        self.store.assign('long-task','task','B',['scripts/'],body='长需求'*10000)
        message=next(m for m in self.store.status()['messages'] if m['kind']=='task')
        message=self.store.message(message['id'])
        payload=json.loads(self.store.envelope(self.members['B'],message))
        self.assertIsNone(payload['task'])
        self.claim(message)
        chunks=[];offset=0
        while offset is not None:
            page=self.read(message,field='task',offset=offset)
            chunks.append(page['text']);offset=page['next_offset']
        task=json.loads(''.join(chunks))
        self.assertEqual(json.loads(task['allowed_paths']),['scripts/'])
        self.assertEqual(task['body'],'长需求'*10000)
        for kwargs in ({'offset':-1},{'limit':2001},{'field':'claim_token'}):
            with self.assertRaises(PairError):self.read(message,**kwargs)
        m=self.members['A']
        with self.assertRaises(PairError):
            self.store.read_message('A',m['session_uuid'],m['cwd'],message['id'])

    def test_cli_read_after_compact_claim_and_explicit_delivery_recovery(self):
        message=self.store.send('A','B','中文审查'*10000)
        Daemon(self.store,FakeTransport(error=TransportError(LENGTH_ERROR))).dispatch_one()
        args=pair.build_parser().parse_args(['--json','--pair','receipt','--codex-home',str(self.home),
                                           'recover-delivery','--message',message['id']])
        self.assertTrue(pair.execute(args)['recovered'])
        claim_args=test_claim_recovery.ClaimRecoveryTests.argv(self,'claim',message,'--compact')
        read_args=test_claim_recovery.ClaimRecoveryTests.argv(self,'read-message',message,'--offset','2000','--limit','500')
        with patch('scripts.pair.Path.cwd',return_value=Path(self.members['B']['cwd'])),patch('scripts.pair.AppServer'):
            receipt=pair.execute(pair.build_parser().parse_args(claim_args))
            self.assertTrue(receipt['claimed'])
            self.assertNotIn('body',receipt)
            page=pair.execute(pair.build_parser().parse_args(read_args))
        self.assertEqual(page['text'],message['body'][2000:2500])
        self.assertEqual(page['next_offset'],2500)

    def test_queue_rejects_legacy_before_submission_and_accepts_short_envelope(self):
        message=self.store.send('A','B','长审查🙂'*20000)
        transport=QueueTransport.__new__(QueueTransport)
        transport.command=['codex.exe'];transport.cwd=None;transport.environment={};transport.timeout=1
        member=self.members['B']
        legacy=json.dumps({'body':message['body']},ensure_ascii=True)
        response=subprocess.CompletedProcess([],0,('Queued message 00000000-0000-0000-0000-000000000003 for thread '+member['session_uuid']+'.').encode(),b'')
        with patch.object(transport,'validate'),patch('scripts.pair_transport.os.name','nt'),patch('scripts.pair_transport.subprocess.run',return_value=response) as run:
            with self.assertRaisesRegex(TransportError,'Windows command-line limit'):
                transport.start(member,{'envelope':legacy})
            run.assert_not_called()
            short=self.store.envelope(member,message)
            receipt=transport.start(member,{'envelope':short})
            self.assertEqual(receipt['remote_status'],'queued')
            self.assertEqual(run.call_args.args[0][-1],short)


if __name__=='__main__':unittest.main()
