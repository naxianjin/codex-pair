"""领取输出被工具截断后恢复原回执，不重新执行或产生第二个领取者。"""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import pair
from scripts.pair_core import PairError, Store


class ClaimRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pair-receipt-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.home=self.root/'home';self.home.mkdir()
        self.project=self.root/'project';self.project.mkdir()
        self.store=Store.initialize('receipt',str(self.project),'a'*40,home=self.home)
        self.members={}
        for index,member_id in enumerate(('A','B'),1):
            cwd=self.root/member_id;cwd.mkdir()
            sid=f'00000000-0000-0000-0000-{index:012d}'
            self.members[member_id]=self.store.join(member_id,sid,str(cwd),backend='queue',
                owned=False,proof={'id':sid,'cwd':str(cwd)})

    def claim(self,message):
        member=self.members['B']
        return self.store.claim('B',member['session_uuid'],member['cwd'],message_id=message['id'])

    def recover(self,message,member_id='B'):
        member=self.members[member_id]
        return self.store.recover_claim(member_id,member['session_uuid'],member['cwd'],message['id'])

    def test_recovery_returns_original_token_without_changing_message_or_events(self):
        message=self.store.send('A','B','原任务')
        first=self.claim(message)
        before=self.store.message(message['id'])
        events=self.store.status()['last_event']
        self.assertFalse(self.claim(message)['claimed'])
        recovered=self.recover(message)
        self.assertTrue(recovered['recovered'])
        self.assertTrue(recovered['resume_only'])
        self.assertEqual(recovered['claim_token'],first['claim_token'])
        self.assertEqual(self.store.message(message['id']),before)
        self.assertEqual(self.store.status()['last_event'],events)
        self.assertNotIn('claimed',recovered)
        self.assertFalse(self.claim(message)['claimed'])

    def test_recovery_rejects_other_owner_bad_binding_and_unclaimed_message(self):
        message=self.store.send('A','B','原任务')
        self.assertFalse(self.recover(message)['recovered'])
        self.claim(message)
        self.assertFalse(self.recover(message,'A')['recovered'])
        with self.assertRaises(PairError):
            self.store.recover_claim('B',self.members['A']['session_uuid'],self.members['B']['cwd'],message['id'])

    def test_completed_and_stopped_claims_cannot_resume(self):
        message=self.store.send('A','B','原任务')
        claimed=self.claim(message);member=self.members['B']
        self.store.complete('B',member['session_uuid'],member['cwd'],message['id'],claimed['claim_token'],'已处理')
        self.assertFalse(self.recover(message)['recovered'])
        other=self.store.send('A','B','另一个任务');self.claim(other)
        self.store.control('stopped')
        self.assertFalse(self.recover(other)['recovered'])

    def test_running_or_unknown_delivery_cannot_be_resumed(self):
        message=self.store.send('A','B','原任务');self.claim(message)
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET remote_status='inProgress' WHERE id=?",(message['id'],))
        self.assertEqual(self.recover(message)['reason'],'previous_turn_unresolved')
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET remote_status='completed',delivery_status='unknown' WHERE id=?",(message['id'],))
        self.assertEqual(self.recover(message)['reason'],'previous_turn_unresolved')

    def argv(self,command,message,*extra):
        member=self.members['B']
        return ['--json','--pair','receipt','--codex-home',str(self.home),command,
                '--member','B','--session',member['session_uuid'],'--worktree',member['cwd'],
                '--message',message['id'],*extra]

    def test_large_payload_has_small_parseable_receipt_and_recovers_after_lost_output(self):
        self.store.assign('large','长任务','B',['docs/'],body='中文需求与操作说明'*10000)
        message=next(m for m in self.store.status()['messages'] if m['kind']=='task')
        with self.store.transaction() as db:
            db.execute('UPDATE messages SET envelope=? WHERE id=?',('重复信封'*10000,message['id']))
        member=self.members['B']
        environment={'CODEX_HOME':str(self.home),'CODEX_THREAD_ID':member['session_uuid']}
        with patch.dict(os.environ,environment),patch('scripts.pair.Path.cwd',return_value=Path(member['cwd'])), \
                patch('scripts.pair.AppServer'):
            output=io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(pair.main(self.argv('claim',message,'--compact')),0)
            receipt=json.loads(output.getvalue())
            self.assertTrue(receipt['result']['claimed'])
            self.assertLess(len(output.getvalue().encode('utf-8')),700)
            self.assertNotIn('body',receipt['result'])
            self.assertNotIn('envelope',receipt['result'])
            self.assertFalse(pair.execute(pair.build_parser().parse_args(self.argv('claim',message)))['claimed'])
            recovered=pair.execute(pair.build_parser().parse_args(self.argv('recover-claim',message)))
            self.assertEqual(recovered['claim_token'],receipt['result']['claim_token'])
            self.store.complete('B',member['session_uuid'],member['cwd'],message['id'],recovered['claim_token'],'原任务完成')
            self.assertEqual(self.store.task('large')['status'],'completed')

    def test_recovery_cli_requires_original_environment_not_supplied_identity(self):
        message=self.store.send('A','B','原任务');self.claim(message)
        arguments=pair.build_parser().parse_args(self.argv('recover-claim',message))
        with patch.dict(os.environ,{'CODEX_THREAD_ID':self.members['A']['session_uuid'],'CODEX_HOME':str(self.home)}):
            with self.assertRaisesRegex(PairError,'Original CLI session required'):
                pair.execute(arguments)
        with patch.dict(os.environ,{'CODEX_THREAD_ID':self.members['B']['session_uuid'],'CODEX_HOME':str(self.project)}):
            with self.assertRaisesRegex(PairError,'Original CLI home required'):
                pair.execute(arguments)


if __name__=='__main__':
    unittest.main()
