"""多人协议的隔离回归草稿；本轮按用户要求未运行。"""
import concurrent.futures
import json
import subprocess
import unittest

from scripts.pair_core import PairError
from tests import test_core


class TeamTests(unittest.TestCase):
    setUp=test_core.CoreTests.setUp
    tearDown=test_core.CoreTests.tearDown

    def actor(self):return ('A',self.sessions['A'],self.directories['A'])

    def test_reverification_allows_only_paused_unstarted_checkpoint(self):
        self.store.control('paused')
        notice=self.store.send('B','B','保存现场',kind='checkpoint')
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='queued' WHERE id=?",(notice['id'],))
            self.assertFalse(self.store._idle(db,['B']))
            self.assertTrue(self.store._reverification_idle(db,'B'))
            for column,value in [('remote_status','inProgress'),('delivery_status','unknown'),('process_status','claimed'),('turn_id','real-turn'),('kind','task'),('recipient_version',99)]:
                old=db.execute('SELECT '+column+' FROM messages WHERE id=?',(notice['id'],)).fetchone()[0]
                db.execute('UPDATE messages SET '+column+'=? WHERE id=?',(value,notice['id']))
                self.assertFalse(self.store._reverification_idle(db,'B'),column)
                db.execute('UPDATE messages SET '+column+'=? WHERE id=?',(old,notice['id']))
            db.execute("UPDATE collaborations SET status='running'")
            self.assertFalse(self.store._reverification_idle(db,'B'))

    def test_reverification_allows_saved_checkpoint_with_stale_queue_receipt(self):
        subprocess.run(['git','init','-q',str(self.project)],check=True,capture_output=True)
        self.store.control('paused')
        notice=self.store.send('B','B','保存现场',kind='checkpoint')
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='queued',process_status='completed' WHERE id=?",(notice['id'],))
            self.assertFalse(self.store._reverification_idle(db,'B'))
            db.execute("UPDATE messages SET remote_status='completed' WHERE id=?",(notice['id'],))
        self.store.save_checkpoint('B',self.sessions['B'],self.directories['B'],'工具已结束，保存现场')
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET remote_status='queued' WHERE id=?",(notice['id'],))
            self.assertTrue(self.store._idle(db,['B']))
            self.assertTrue(self.store._reverification_idle(db,'B'))
            db.execute("UPDATE messages SET remote_status='inProgress' WHERE id=?",(notice['id'],))
            self.assertFalse(self.store._reverification_idle(db,'B'))

    def test_retired_checkpoint_receipt_does_not_block_replacement(self):
        subprocess.run(['git','init','-q',str(self.project)],check=True,capture_output=True)
        self.store.control('paused')
        notice=self.store.send('B','B','保存现场',kind='checkpoint')
        self.store.save_checkpoint('B',self.sessions['B'],self.directories['B'],'原工具结束')
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='queued' WHERE id=?",(notice['id'],))
            db.execute("INSERT INTO member_history SELECT id,binding_version,'{}','原绑定已安全退出',0 FROM members WHERE id='B'")
            db.execute("UPDATE members SET binding_version=binding_version+1 WHERE id='B'")
            self.assertTrue(self.store._idle(db,['A','B']))
            for column,value in [('remote_status','inProgress'),('delivery_status','unknown'),('process_status','claimed'),('turn_id','real-turn'),('recipient_version',99)]:
                old=db.execute('SELECT '+column+' FROM messages WHERE id=?',(notice['id'],)).fetchone()[0]
                db.execute('UPDATE messages SET '+column+'=? WHERE id=?',(value,notice['id']))
                self.assertFalse(self.store._idle(db,['B']),column)
                db.execute('UPDATE messages SET '+column+'=? WHERE id=?',(old,notice['id']))

    def test_retirement_accepts_cancelled_history_but_blocks_unsettled_work(self):
        self.store.control('paused')
        self.store.assign('history','历史任务','B',['history.txt'])
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='cancelled' WHERE id='history'")
        self.store.assign('pending','待办任务','B',['pending.txt'])
        for status in ('active','delivered','waiting_execution'):
            with self.store.transaction() as db:
                db.execute("UPDATE tasks SET status=? WHERE id='pending'",(status,))
            with self.assertRaisesRegex(PairError,'Work unresolved'):
                self.store.retire_member(*self.actor(),'B',execution_resolved=True,evidence='核对历史及未结工作')
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='pending' WHERE id='pending'")
        result=self.store.retire_member('B',self.sessions['B'],self.directories['B'],'B',
            execution_resolved=True,evidence='已取消记录保留，待办交新统筹，原执行已结束')
        self.assertEqual(result['retired'],'B')
        self.assertEqual(self.store.task('history')['status'],'cancelled')
        self.assertEqual(self.store.task('pending')['status'],'pending')

    def test_handshake_terminal_wakes_context_confirmation_once_for_both_members(self):
        self.store.setting('member_context_required',True)
        with self.store.transaction() as db:db.execute('UPDATE members SET verified=0')
        question=self.store.send('B','A','handshake',kind='question',dedupe_key='window-handshake:B:1')
        reply=self.store.send('A','B','reply',kind='reply',reply_to=question['id'])
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET process_status='completed',remote_status='inProgress' WHERE id IN (?,?)",(question['id'],reply['id']))
        self.store.schedule()
        with self.store.transaction() as db:
            self.assertFalse(db.execute("SELECT 1 FROM messages WHERE dedupe_key LIKE 'member-context:%'").fetchone())
            db.execute("UPDATE messages SET remote_status='completed' WHERE id IN (?,?)",(question['id'],reply['id']))
        self.store.schedule();self.store.schedule()
        with self.store.transaction() as db:
            rows=db.execute("SELECT recipient FROM messages WHERE dedupe_key LIKE 'member-context:%' ORDER BY recipient").fetchall()
            self.assertEqual([r[0] for r in rows],['A','B'])
            self.assertFalse(self.store.window_verified(db))
            db.execute("UPDATE members SET verified=1 WHERE id='B'")
        self.store.schedule()
        with self.store.transaction() as db:
            self.assertFalse(db.execute("SELECT 1 FROM messages WHERE dedupe_key LIKE 'members-ready:%'").fetchone())
            db.execute("UPDATE members SET verified=1 WHERE id='A'")
        self.store.schedule();self.store.schedule()
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE dedupe_key LIKE 'members-ready:%'").fetchone()[0],1)

    def window(self,number):
        directory=self.root/('member-'+str(number));directory.mkdir(exist_ok=True)
        sid='00000000-0000-0000-0000-'+str(number).zfill(12)
        proof={'id':sid,'cwd':str(directory.resolve()),'modelProvider':'openai','model':'gpt-6.1-sol'}
        return {'session_uuid':sid,'cwd':proof['cwd'],'codex_home':str(self.home.resolve()),'proof':proof}

    def join(self,number,target=None):
        invite=self.store.create_invite(*self.actor(),target=target)
        window=self.window(number)
        return self.store.accept_invite(invite,window,{},mode='auto'),invite,window

    def test_maximum_four_and_idempotent_acceptance(self):
        c,invite,window=self.join(3,'C')
        self.assertEqual(c['id'],'C')
        self.assertFalse(c['verified'])
        self.assertEqual(self.store.accept_invite(invite,window,{})['session_uuid'],c['session_uuid'])
        with self.assertRaises(PairError):self.store.accept_invite(invite,self.window(30),{})
        d,_,_=self.join(4,'D')
        self.assertEqual(d['id'],'D')
        invitation=self.store.create_invite(*self.actor())
        with self.assertRaisesRegex(PairError,'maximum'):self.store.accept_invite(invitation,self.window(5),{})
        self.assertEqual(len(self.store.visible_members()),4)

    def test_last_seat_competition_has_single_winner(self):
        self.join(3,'C')
        invitations=[self.store.create_invite(*self.actor()) for _ in range(2)]
        windows=[self.window(4),self.window(5)]
        def accept(index):
            try:self.store.accept_invite(invitations[index],windows[index],{});return 1
            except PairError:return 0
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:self.assertEqual(sum(pool.map(accept,range(2))),1)

    def test_stop_invalidates_invite_without_rebinding(self):
        invitation=self.store.create_invite(*self.actor(),target='C')
        self.store.control('stopped')
        with self.assertRaises(PairError):self.store.accept_invite(invitation,self.window(3),{})
        self.assertEqual(len(self.store.visible_members()),2)

    def test_offline_is_not_retired_and_minimum_two_gate(self):
        with self.store.transaction() as db:db.execute("UPDATE members SET online=0 WHERE id='B'")
        self.store.assign('independent','A独立任务','A',['a.py'])
        self.assertEqual(self.store.task('independent')['status'],'active')
        with self.store.transaction() as db:db.execute("UPDATE members SET lifecycle='retired' WHERE id='B'")
        self.store.assign('later','不足两人不开始','A',['b.py'])
        self.assertEqual(self.store.task('later')['status'],'pending')

    def test_ordinary_question_must_route_via_a(self):
        self.join(3,'C')
        with self.assertRaisesRegex(PairError,'via A'):self.store.send('B','C','普通技术问题',kind='question')

    def test_shared_resource_serializes_separate_workspaces(self):
        self.store.setting('member_context_required',True)
        self.store.assign('schema-a','结构A','A',['a.py'],resources=['database:schema'])
        self.store.assign('schema-b','结构B','B',['b.py'],resources=['database:schema'])
        self.assertEqual(self.store.task('schema-a')['status'],'active')
        self.assertEqual(self.store.task('schema-b')['status'],'pending')

    def test_legacy_scope_lock_spans_separate_workspaces(self):
        self.store.setting('scheduling_policy','legacy')
        self.store.assign('first','旧流程范围锁','A',['Src/Shared/'])
        self.assertEqual(self.store.assign('overlap','重叠','B',['src/shared/file.py'])['status'],'pending')
        self.assertEqual(self.store.assign('separate','不同路径','B',['src/other.py'])['status'],'active')

    def test_new_mode_separate_workspaces_can_edit_same_relative_path(self):
        self.store.setting('member_context_required',True)
        self.store.assign('first','独立树A','A',['src/shared.py'])
        self.assertEqual(self.store.assign('second','独立树B','B',['src/shared.py'])['status'],'active')

    def test_new_mode_same_workspace_serializes_disjoint_paths(self):
        self.store.setting('member_context_required',True)
        with self.store.transaction() as db:
            for member in ('A','B'):
                proof={'git':{'path':str(self.project)}}
                db.execute('UPDATE members SET proof=? WHERE id=?',(json.dumps(proof),member))
        self.store.assign('first','同树A','A',['src/a.py'])
        self.assertEqual(self.store.assign('second','同树B','B',['src/b.py'])['status'],'pending')


if __name__=='__main__':unittest.main()
