"""六项静态发现的行为规格；须在通过验收的隔离环境执行。"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from scripts import pair_balance
from scripts.pair_control_runtime import management_command
from scripts.pair_windows import ps_command
from tests.test_core import CoreTests
from tests.test_handoff_balance import quota


class ScopedControlTests(unittest.TestCase):
    setUp=CoreTests.setUp
    tearDown=CoreTests.tearDown

    def task_turn(self,member='B',task='work'):
        self.store.assign(task,task,member,[task+'.py'])
        message=next(m for m in self.store.status()['messages'] if m['kind']=='task' and m['task_id']==task)
        self.assertTrue(self.store.claim(member,self.sessions[member],self.directories[member],message_id=message['id'])['claimed'])
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET turn_id=?,delivery_status='accepted',remote_status='inProgress' WHERE id=?",('turn-'+task,message['id']))
        return {'session_id':self.sessions[member],'turn_id':'turn-'+task,'tool_use_id':'tool-'+task,
                'hook_event_name':'PreToolUse','tool_name':'Bash','tool_input':{'command':'git status'}}

    def pause_task(self):
        self.store.request_pause('A',self.sessions['A'],self.directories['A'],reason='只暂停work',
                                 idempotency_key='scoped-pause',scope={'kind':'task','id':'work'})

    def test_task_pause_denies_next_tool_and_stop_without_stopping_unrelated_member(self):
        event=self.task_turn();other=self.task_turn('A','other');self.pause_task()
        with self.store.transaction() as db:
            denied=self.store._control_tool_event(db,'B',event)
            self.assertEqual(denied['hookSpecificOutput']['permissionDecision'],'deny')
            self.assertFalse(self.store._control_tool_event(db,'B',dict(event,hook_event_name='Stop'))['continue'])
            self.assertEqual(self.store._control_tool_event(db,'A',other),{})
            self.assertEqual(db.execute('SELECT status FROM collaborations').fetchone()[0],'running')

    def test_unknown_turn_keeps_task_pause_and_records_binding_gap(self):
        event=self.task_turn();self.pause_task();event['turn_id']='unmatched'
        with self.store.transaction() as db:
            _,reasons=self.store._turn_control_gate(db,'B','unmatched')
            self.assertIn('TASK_TURN_BINDING_UNVERIFIED',[r['code'] for r in reasons])
            self.assertEqual(self.store._control_tool_event(db,'B',event)['hookSpecificOutput']['permissionDecision'],'deny')
            self.assertEqual(db.execute('SELECT state FROM control_requests').fetchone()[0],'pausing')

    def test_stale_message_binding_cannot_hide_task_pause(self):
        event=self.task_turn();self.pause_task()
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET recipient_version=99 WHERE task_id='work'")
            self.assertEqual(self.store._control_tool_event(db,'B',event)['hookSpecificOutput']['permissionDecision'],'deny')

    def test_task_pause_post_tool_keeps_output_and_pending_confirmation(self):
        event=self.task_turn()
        with self.store.transaction() as db:self.store._control_tool_event(db,'B',event)
        self.pause_task()
        with self.store.transaction() as db:
            result=self.store._control_tool_event(db,'B',dict(event,hook_event_name='PostToolUse'))
            self.assertIn('systemMessage',result);self.assertNotIn('decision',result)
            self.assertEqual(db.execute('SELECT state FROM control_requests').fetchone()[0],'pausing')


class ManagementArgumentTests(unittest.TestCase):
    setUp=CoreTests.setUp
    tearDown=CoreTests.tearDown

    def command_event(self,command):
        return {'tool_name':'Bash','tool_input':{'command':command}}

    def test_plain_and_quoted_windows_arguments_reach_same_management_gate(self):
        if sys.platform!='win32':self.skipTest('Windows命令路径规格')
        executable=r'C:\Python\python.exe';script=Path(r'D:\候选\scripts\pair_control_runtime.py')
        with patch('scripts.pair_control_runtime.sys.executable',executable),patch('scripts.pair_control_runtime.__file__',str(script)):
            argv=[executable,str(script.with_name('pair.py')),'status','--pair',self.store.pair_id,'--state-root',str(self.store.state_root)]
            plain=' '.join(argv[:2])+' '+ps_command(argv[2:])[2:]
            for command in (plain,ps_command(argv),' '.join('"'+arg+'"' for arg in argv)):
                with self.subTest(command=command):
                    self.assertTrue(management_command(self.store,self.store.member('A'),self.command_event(command)))

    def test_chinese_spaces_and_literal_apostrophe_keep_exact_paths(self):
        script=self.root/"中文 候选's"/'scripts'/'pair_control_runtime.py'
        with patch('scripts.pair_control_runtime.__file__',str(script)):
            argv=[sys.executable,str(script.with_name('pair.py')),'status','--pair',self.store.pair_id,'--state-root',str(self.store.state_root)]
            self.assertTrue(management_command(self.store,self.store.member('A'),self.command_event(ps_command(argv))))

    def test_composition_malformed_quotes_and_other_pair_are_not_management(self):
        script=Path(__file__).resolve().parents[1]/'scripts/pair.py'
        argv=[sys.executable,str(script),'status','--pair',self.store.pair_id,'--state-root',str(self.store.state_root)]
        command=ps_command(argv)
        for text in (command+'; git push',command+' | other',command+' & other',command+" '",command.replace("'test'","'another-pair'")):
            with self.subTest(command=text):
                self.assertFalse(management_command(self.store,self.store.member('A'),self.command_event(text)))


class MultiMemberBalanceTests(unittest.TestCase):
    def test_unknown_third_member_does_not_hide_comparable_high_quota(self):
        values={'A':quota(80,pool='a'),'B':quota(30,pool='b'),'C':{'reliability':'missing','remaining':None}}
        self.assertEqual(pair_balance.choose(['A','B','C'],8,values,{'A':10,'B':0,'C':10}),('A','quota_gap_at_least_20pp'))

    def test_four_members_with_other_bucket_and_shared_pool_keep_valid_pair(self):
        values={'A':quota(80,pool='a'),'B':quota(30,pool='b'),'C':quota(100,pool='c',duration=60),'D':quota(80,pool='a')}
        self.assertIn(pair_balance.choose(['A','B','C','D'],8,values,{'A':10,'B':0,'C':10,'D':10})[0],('A','D'))

    def test_small_gap_and_light_work_use_burden_even_with_unknown_member(self):
        values={'A':quota(80,pool='a'),'B':quota(61,pool='b'),'C':{'reliability':'missing','remaining':None}}
        self.assertEqual(pair_balance.choose(['A','B','C'],8,values,{'A':10,'B':0,'C':10})[0],'B')
        values['B']=quota(30,pool='b')
        self.assertEqual(pair_balance.choose(['A','B','C'],3,values,{'A':10,'B':0,'C':10})[0],'B')
