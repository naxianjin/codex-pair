from tests.schema_fixture import remove_team_extensions
"""协助执行和低额度交接的行为测试；不调用付费模型。"""
import json
import os
import tempfile
from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import patch

from scripts.pair_core import Daemon, PairError, Store
from scripts.pair import build_parser
from tests import test_core
from tests.test_handoff_balance import quota


class AssistanceTests(unittest.TestCase):
    setUp=test_core.CoreTests.setUp
    tearDown=test_core.CoreTests.tearDown

    def binding(self,member):return member,self.sessions[member],self.directories[member]

    def task(self):
        self.store.assign('work','写入授权文件','B',['result.txt'])
        message=next(m for m in self.store.status()['messages'] if m['kind']=='task')
        claim=self.store.claim(*self.binding('B'),message_id=message['id'])
        return message,claim

    def payload(self):
        return {'argv':['python','-c',"print('authorized')"],'cwd':str(Path(self.directories['B']).resolve()),
            'purpose':'完成当前任务','paths':['result.txt'],'expected_effects':'生成结果文件',
            'authorization_basis':'用户已要求创建 result.txt','reason':'capability_unavailable'}

    def request(self):
        self.store.configure_assistance(*self.binding('A'),execution=True)
        message,claim=self.task()
        request=self.store.request_execution(*self.binding('B'),message['id'],claim['claim_token'],'req1',self.payload())
        return message,claim,request

    def acknowledge(self,member,record):
        offset=0
        while True:
            page=self.store.read_delivery(*self.binding(member),record,offset=offset)
            if page['next_offset'] is None:break
            offset=page['next_offset']
        self.store.ack_delivery(*self.binding(member),record,page['digest'])

    def test_execution_is_opt_in_and_cannot_bypass_rejection_or_scope(self):
        message,claim=self.task()
        with self.assertRaisesRegex(PairError,'disabled'):
            self.store.request_execution(*self.binding('B'),message['id'],claim['claim_token'],'req1',self.payload())
        self.store.configure_assistance(*self.binding('A'),execution=True)
        for change in ({'paths':['other.txt']},{'reason':'user_declined'},{'argv':['echo','sk-testsecret']}):
            with self.subTest(change=change),self.assertRaises(PairError):
                self.store.request_execution(*self.binding('B'),message['id'],claim['claim_token'],'req1',dict(self.payload(),**change))

    def test_execution_claim_review_result_and_parent_resume(self):
        message,claim,request=self.request()
        self.assertEqual(self.store.task('work')['status'],'waiting_execution')
        with self.assertRaisesRegex(PairError,'suspended'):
            self.store.complete(*self.binding('B'),message['id'],claim['claim_token'],'提前完成')
        owned=self.store.claim(*self.binding('A'),message_id=request['message_id'])
        with self.assertRaises(PairError):
            self.store.begin_execution(*self.binding('A'),'req1',owned['claim_token'],'wrong','审核')
        start=self.store.begin_execution(*self.binding('A'),'req1',owned['claim_token'],request['digest'],'已核对用户任务和脚本')
        self.assertTrue(start['execute_in_original_window'])
        with self.assertRaisesRegex(PairError,'already started'):
            self.store.begin_execution(*self.binding('A'),'req1',owned['claim_token'],request['digest'],'再次审核')
        result=self.store.finish_execution(*self.binding('A'),'req1',owned['claim_token'],'success',
            {'summary':'命令完成','argv':self.payload()['argv'],'cwd':self.payload()['cwd'],'exit_code':0,'evidence':'结果已回读','stdout':'Bearer secret-token'})
        self.assertNotIn('secret-token',self.store.read_delivery(*self.binding('B'),result['delivery_id'])['text'])
        received=self.store.claim(*self.binding('B'),message_id=result['message_id'])
        with self.assertRaisesRegex(PairError,'acknowledgement'):
            self.store.complete(*self.binding('B'),result['message_id'],received['claim_token'],'确认结果')
        self.acknowledge('B',result['delivery_id'])
        self.store.complete(*self.binding('B'),result['message_id'],received['claim_token'],'已核对现场')
        self.assertEqual(self.store.task('work')['status'],'active')
        self.store.complete(*self.binding('B'),message['id'],claim['claim_token'],'完成剩余工作')
        self.assertEqual(self.store.task('work')['status'],'completed')
        self.assertEqual(len(self.store.status()['assistance']['requests']),1)

    def test_request_deduplication_and_no_high_privilege_subprocess(self):
        message,claim,request=self.request()
        again=self.store.request_execution(*self.binding('B'),message['id'],claim['claim_token'],'req1',self.payload())
        self.assertEqual(request['message_id'],again['message_id'])
        with self.assertRaisesRegex(PairError,'Request changed'):
            self.store.request_execution(*self.binding('B'),message['id'],claim['claim_token'],'req1',dict(self.payload(),argv=['echo','changed']))
        with patch('subprocess.Popen',side_effect=AssertionError('bridge executed command')):
            daemon=Daemon(self.store,test_core.FakeTransport(self.store))
            with self.store.transaction() as db:
                db.execute("UPDATE messages SET delivery_status='accepted',remote_status='completed' WHERE id=?",(message['id'],))
            self.assertTrue(daemon.dispatch_one())

    def test_running_operation_becomes_unknown_and_cannot_restart(self):
        _,_,request=self.request()
        owned=self.store.claim(*self.binding('A'),message_id=request['message_id'])
        self.store.begin_execution(*self.binding('A'),'req1',owned['claim_token'],request['digest'],'核对授权')
        with self.store.transaction() as db:db.execute("UPDATE messages SET remote_status='interrupted' WHERE id=?",(request['message_id'],))
        self.store.schedule()
        self.assertEqual(self.store.status()['assistance']['requests'][0]['status'],'unknown')
        with self.assertRaises(PairError):
            self.store.begin_execution(*self.binding('A'),'req1',owned['claim_token'],request['digest'],'重试')
        self.assertEqual(self.store.task('work')['status'],'waiting_execution')

    def prepare_checkpoints(self):
        subprocess.run(['git','init','-q',str(self.project)],check=True,capture_output=True)
        # 两个窗口使用同一项目工作树，但保持各自 actor cwd 和账号绑定。
        with self.store.transaction() as db:
            for member in ('A','B'):
                proof=json.loads(self.store._member(db,member)['proof']);proof['git']={'path':str(self.project)}
                db.execute('UPDATE members SET proof=? WHERE id=?',(json.dumps(proof),member))
        self.store.configure_assistance(*self.binding('A'),checkpoints=True)

    def test_saved_checkpoint_stale_queue_receipt_allows_resume_and_consume(self):
        self.prepare_checkpoints()
        self.store.control('paused')
        notice=self.store.send('B','B','保存现场',kind='checkpoint')
        self.store.save_checkpoint(*self.binding('B'),'工具结束，保存现场')
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='queued' WHERE id=?",(notice['id'],))
            for column,value in [('remote_status','inProgress'),('delivery_status','unknown'),('process_status','claimed'),('turn_id','real-turn'),('kind','task'),('recipient_version',99)]:
                old=db.execute('SELECT '+column+' FROM messages WHERE id=?',(notice['id'],)).fetchone()[0]
                db.execute('UPDATE messages SET '+column+'=? WHERE id=?',(value,notice['id']))
                self.assertFalse(self.store._idle(db,['B']),column)
                db.execute('UPDATE messages SET '+column+'=? WHERE id=?',(old,notice['id']))
        offered=self.store.resume_checkpoint(*self.binding('B'),'用户要求继续')
        self.assertTrue(offered['offered'])
        result=self.store.consume_checkpoint(*self.binding('B'),'B',self.read_all('B','B'),'核对原操作已结束，继续协调')
        self.assertTrue(result['consumed'])
        self.store.control('running')
        selected=Daemon(self.store,None)._begin()
        self.assertIsNotNone(selected)
        self.assertEqual(selected[1]['id'],offered['message_id'])
        claim=self.store.claim(*self.binding('B'),message_id=offered['message_id'])
        self.assertTrue(claim['claimed'])
        self.store.complete(*self.binding('B'),offered['message_id'],claim['claim_token'],'已消费检查点并完成通知收尾')

    def test_checkpoint_preserves_cancelled_and_handed_back_tasks(self):
        self.prepare_checkpoints()
        for status in ('cancelled','handed_back'):
            self.store.assign(status,'历史任务','B',[status+'.txt'])
        with self.store.transaction() as db:
            for status in ('cancelled','handed_back'):
                db.execute('UPDATE tasks SET status=? WHERE id=?',(status,status))
            db.execute("UPDATE messages SET process_status='completed',delivery_status='accepted',remote_status='completed'")
        saved=self.store.save_checkpoint(*self.binding('B'),'保留历史终态')
        with self.store.transaction() as db:
            manifest=json.loads(db.execute("SELECT manifest FROM checkpoints WHERE member='B'").fetchone()[0])
        self.assertFalse(manifest['tasks'])
        self.store.schedule()
        for status in ('cancelled','handed_back'):
            self.assertEqual(self.store.task(status)['status'],status)
        self.assertTrue(saved['saved'])

    def test_recovered_checkpoint_does_not_revive_terminal_tasks(self):
        self.prepare_checkpoints()
        self.task()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='cancelled' WHERE id='work'")
            db.execute("UPDATE messages SET process_status='completed',delivery_status='accepted',remote_status='completed'")
        event=self.store.event
        def crash(db,kind,**kwargs):
            if kind=='checkpoint_saved':raise RuntimeError('checkpoint transaction interrupted')
            return event(db,kind,**kwargs)
        with patch.object(self.store,'event',side_effect=crash):
            with self.assertRaises(RuntimeError):
                self.store.save_checkpoint(*self.binding('B'),'文件已保存，数据库事务未提交')
        self.assertTrue(self.store.recover_checkpoint(*self.binding('B'))['recovered'])
        self.assertEqual(self.store.task('work')['status'],'cancelled')

    def test_consumption_preserves_task_cancelled_after_save(self):
        self.prepare_checkpoints()
        self.task()
        self.store.save_checkpoint(*self.binding('B'),'任务尚未完成')
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='cancelled' WHERE id='work'")
            db.execute("UPDATE messages SET process_status='completed',delivery_status='accepted',remote_status='completed'")
        self.store.resume_checkpoint(*self.binding('B'),'只恢复窗口')
        self.store.consume_checkpoint(*self.binding('B'),'B',self.read_all('B','B'),'任务已取消，不再续办')
        self.assertEqual(self.store.task('work')['status'],'cancelled')

    def separate_pools(self):
        other=self.root/'other-home';other.mkdir(exist_ok=True)
        with self.store.transaction() as db:db.execute("UPDATE members SET codex_home=? WHERE id='B'",(str(other),))

    def read_all(self,recipient,source):
        offset=0
        while True:
            page=self.store.read_checkpoint(*self.binding(recipient),source,offset=offset)
            if page['next_offset'] is None:return page['digest']
            offset=page['next_offset']

    def test_checkpoint_missing_does_not_read_or_scan(self):
        self.prepare_checkpoints()
        with patch.object(Path,'read_text',side_effect=AssertionError('unexpected read')),patch.object(Path,'rglob',side_effect=AssertionError('unexpected scan')):
            self.assertFalse(self.store.check_checkpoint(*self.binding('B'))['exists'])

    def test_pinned_pending_task_blocks_peer_takeover_and_preserves_binding(self):
        self.prepare_checkpoints();self.separate_pools()
        self.store.control('paused')
        self.store.assign('pinned','原窗口恢复','B',['b.txt'],pin_member=True)
        self.store.save_checkpoint(*self.binding('B'),'等待原B')
        self.store.save_quota('A',quota(80,pool='a'));self.store.save_quota('B',quota(9,pool='b'))
        self.store.control('running');self.store.schedule()
        self.assertFalse(any(m['kind']=='checkpoint_resume' for m in self.store.status()['messages']))
        self.store.save_quota('B',quota(50,pool='b'));self.store.schedule()
        notice=next(m for m in self.store.status()['messages'] if m['kind']=='checkpoint_resume')
        self.assertEqual(notice['recipient'],'B')
        self.assertEqual(self.store.task('pinned')['assignee'],'B')
        with self.store.transaction() as db:
            task=db.execute("SELECT * FROM tasks WHERE id='pinned'").fetchone()
            self.assertTrue(self.store._task_member_allowed(db,task,'B'))
            self.assertFalse(self.store._task_member_allowed(db,task,'A'))
            db.execute("UPDATE members SET binding_version=binding_version+1 WHERE id='B'")
            self.assertFalse(self.store._task_member_allowed(db,task,'B'))

    def test_independent_reviewer_cannot_take_over_implementation(self):
        self.prepare_checkpoints();self.separate_pools()
        self.store.control('paused')
        self.store.assign('impl','实现','B',['b.txt'])
        self.store.assign('verify','独立核验','A',['a.txt'],dependencies=['impl'],independent_of=['impl'])
        self.store.save_checkpoint(*self.binding('B'),'保留独立性')
        self.store.save_quota('A',quota(80,pool='a'));self.store.save_quota('B',quota(9,pool='b'))
        self.store.control('running');self.store.schedule()
        self.assertFalse(any(m['kind']=='checkpoint_resume' for m in self.store.status()['messages']))
        with self.store.transaction() as db:
            task=db.execute("SELECT * FROM tasks WHERE id='verify'").fetchone()
            self.assertFalse(self.store._task_member_allowed(db,task,'B'))
            self.assertTrue(self.store._task_member_allowed(db,task,'A'))
            self.assertFalse(self.store._checkpoint_transfer_allowed(db,'B','A'))

    def test_transfer_rechecks_new_constraint_before_consumption(self):
        saved,fingerprint=self.offered_checkpoint()
        self.store.control('paused')
        self.store.assign('late','新原窗口约束','B',['late.txt'],pin_member=True)
        with self.assertRaisesRegex(PairError,'transfer constrained'):
            self.store.consume_checkpoint(*self.binding('A'),'B',fingerprint,'不能沿用旧offer')
        self.assertTrue(Path(saved['path']).exists())

    def test_recovered_origin_accepts_new_work_without_reclaiming(self):
        saved,fingerprint=self.offered_checkpoint()
        self.store.consume_checkpoint(*self.binding('A'),'B',fingerprint,'接管剩余工作')
        self.store.save_quota('B',quota(10,pool='b'));self.store.schedule()
        self.assertEqual(self.store.status()['assistance']['members']['B']['state'],'paused')
        self.store.save_quota('B',quota(50,pool='b'))
        with self.store.transaction() as db:db.execute("UPDATE members SET verified=0 WHERE id='B'")
        self.store.schedule()
        self.assertEqual(self.store.status()['assistance']['members']['B']['state'],'paused')
        with self.store.transaction() as db:db.execute("UPDATE members SET verified=1 WHERE id='B'")
        self.store.save_quota('B',quota(50,pool='b'));self.store.schedule();self.store.schedule()
        self.assertEqual(self.store.status()['assistance']['members']['B']['state'],'ready')
        self.assertEqual(self.store.task('work')['assignee'],'A')
        self.assertEqual(self.store.status()['assistance']['checkpoints'][0]['status'],'transferred')

    def test_execution_policy_validation_is_atomic_and_cli_exposes_flags(self):
        self.prepare_checkpoints();self.store.control('paused')
        with self.assertRaises(PairError):
            self.store.assign('bad','错误独立约束','B',['b.txt'],independent_of=['missing'])
        with self.store.transaction() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM tasks WHERE id='bad'").fetchone())
        with self.assertRaises(PairError):
            self.store.assign('auto-pin','不明确原窗口','auto',['b.txt'],pin_member=True)
        args=build_parser().parse_args(['assign','--task','x','--title','x','--to','B','--allow','b.txt','--pin-member','--independent-of','base'])
        self.assertTrue(args.pin_member);self.assertEqual(args.independent_of,['base'])

    def test_configure_policy_persists_when_other_rules_are_updated(self):
        self.prepare_checkpoints();self.store.control('paused')
        self.store.assign('work','原窗口限定','B',['b.txt'])
        self.store.configure_task(*self.binding('A'),'work',pin_member=True)
        self.store.configure_task(*self.binding('A'),'work',priority=90)
        with self.store.transaction() as db:
            task=db.execute("SELECT * FROM tasks WHERE id='work'").fetchone()
            self.assertFalse(self.store._task_member_allowed(db,task,'A'))
        with self.assertRaises(PairError):
            self.store.configure_task(*self.binding('A'),'work',pin_member='true')

    def test_independent_constraint_survives_member_and_quota_changes(self):
        self.prepare_checkpoints();self.store.control('paused')
        self.store.assign('impl','实现','B',['b.txt'])
        self.store.assign('verify','核验','A',['a.txt'],dependencies=['impl'],independent_of=['impl'])
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET assignee='A' WHERE id='impl'")
            task=db.execute("SELECT * FROM tasks WHERE id='verify'").fetchone()
            self.assertFalse(self.store._member_eligible(db,'A',task))
            self.assertTrue(self.store._member_eligible(db,'B',task))

    def checkpoint_path(self,member):
        with self.store.transaction() as db:return self.store._checkpoint_path(db,member)

    def test_checkpoint_mixed_newlines_keep_exact_digest_through_cleanup(self):
        from scripts.pair_assistance import digest
        self.prepare_checkpoints()
        saved=self.store.save_checkpoint(*self.binding('B'),'first\r\nsecond\nthird\rfourth')
        path=Path(saved['path']);original=path.read_bytes()
        self.assertEqual(digest(original.decode('utf-8')),saved['digest'])
        self.assertEqual(self.read_all('B','B'),saved['digest'])
        self.assertEqual(path.read_bytes(),original)
        self.store.resume_checkpoint(*self.binding('B'),'continue original window')
        self.assertEqual(self.store.consume_checkpoint(*self.binding('B'),'B',saved['digest'],'verified exact file bytes')['cleanup'],'deleted')

    def test_visible_checkpoint_names_and_same_home_sessions_are_isolated(self):
        self.prepare_checkpoints()
        for number,member in enumerate(('C','D'),3):
            cwd=self.root/member;cwd.mkdir()
            sid='00000000-0000-0000-0000-'+str(number).zfill(12)
            invite=self.store.create_invite(*self.binding('A'),target=member)
            self.store.accept_invite(invite,{'session_uuid':sid,'cwd':str(cwd),'codex_home':str(self.home),
                'proof':{'id':sid,'cwd':str(cwd),'modelProvider':'openai','model':'gpt-6.1-sol'}},{})
        paths=[self.checkpoint_path(m) for m in ('A','B','C','D')]
        self.assertEqual([p.name for p in paths],['交接文档1.md','交接文档2.md','交接文档3.md','交接文档4.md'])
        self.assertEqual(len(set(p.parent for p in paths)),4)
        self.assertTrue(all(p.parent.parent.name==self.store.pair_id for p in paths))

    def test_checkpoint_namespace_changes_with_pair_and_binding_generation(self):
        self.prepare_checkpoints();first=self.checkpoint_path('B')
        with patch.object(self.store,'pair_id','other-pair'):other=self.checkpoint_path('B')
        self.assertNotEqual(first,other)
        with self.store.transaction() as db:db.execute("UPDATE members SET binding_version=binding_version+1 WHERE id='B'")
        self.assertNotEqual(first,self.checkpoint_path('B'))

    def test_hidden_reviewer_keeps_separate_checkpoint_name(self):
        from tests import test_reviewer
        self.prepare_checkpoints()
        helper=test_reviewer.ReviewerTests();helper.__dict__.update(self.__dict__);helper.ready()
        with self.store.transaction() as db:
            hidden=self.store._reviewer_checkpoint_path(db)
            visible=self.store._checkpoint_path(db,'A')
        self.assertTrue(hidden.name.startswith('reviewer-'))
        self.assertNotEqual(hidden,visible)

    def legacy_checkpoint(self):
        from scripts.pair_assistance import digest
        from scripts.pair_transport import canonical_path
        self.prepare_checkpoints()
        saved=self.store.save_checkpoint(*self.binding('B'),'旧格式交接内容'*200)
        bound=self.store.member('B')
        legacy=self.project/'.codex-pair'/'checkpoints'/(digest(canonical_path(bound['codex_home'])+'|'+bound['session_uuid'])[:24]+'.md')
        current=Path(saved['path'])
        if current!=legacy:current.rename(legacy)
        with self.store.transaction() as db:db.execute("UPDATE checkpoints SET path=? WHERE member='B'",(str(legacy),))
        return legacy,saved

    def test_legacy_registered_checkpoint_reads_consumes_then_uses_new_name(self):
        legacy,saved=self.legacy_checkpoint()
        self.assertEqual(Path(self.store.check_checkpoint(*self.binding('B'))['checkpoints'][0]['path']),legacy)
        self.store.resume_checkpoint(*self.binding('B'),'继续原窗口')
        fingerprint=self.read_all('B','B')
        self.assertEqual(self.store.consume_checkpoint(*self.binding('B'),'B',fingerprint,'完整核对旧文档后继续')['cleanup'],'deleted')
        current=self.store.save_checkpoint(*self.binding('B'),'下一次交接')
        self.assertEqual(Path(current['path']).name,'交接文档2.md')
        self.assertNotEqual(current['path'],str(legacy))

    def test_legacy_orphan_recovers_without_moving_or_overwriting(self):
        legacy,saved=self.legacy_checkpoint()
        with self.store.transaction() as db:db.execute("DELETE FROM checkpoints WHERE member='B'")
        before=legacy.read_bytes()
        with self.assertRaisesRegex(PairError,'Unregistered'):self.store.check_checkpoint(*self.binding('B'))
        self.assertTrue(self.store.recover_checkpoint(*self.binding('B'))['recovered'])
        self.assertEqual(legacy.read_bytes(),before)
        self.assertEqual(self.checkpoint_path('B'),legacy)

    def test_registered_checkpoint_outside_directory_is_rejected(self):
        self.prepare_checkpoints();self.store.save_checkpoint(*self.binding('B'),'现场')
        outside=self.root/'private.md';outside.write_text('保留',encoding='utf-8')
        with self.store.transaction() as db:db.execute("UPDATE checkpoints SET path=? WHERE member='B'",(str(outside),))
        with self.assertRaisesRegex(PairError,'checkpoint path'):self.store.check_checkpoint(*self.binding('B'))
        self.assertEqual(outside.read_text(encoding='utf-8'),'保留')

    def test_checkpoint_redirect_is_rejected_before_write(self):
        self.prepare_checkpoints();target=self.checkpoint_path('B');resolve=Path.resolve
        def redirected(path,*args,**kwargs):
            return self.root/'outside.md' if path==target else resolve(path,*args,**kwargs)
        with patch.object(Path,'resolve',autospec=True,side_effect=redirected):
            with self.assertRaisesRegex(PairError,'Redirected checkpoint path'):
                self.store.save_checkpoint(*self.binding('B'),'不得越界')
        self.assertFalse(target.exists())

    def test_old_record_from_other_binding_is_preserved_not_consumed(self):
        self.prepare_checkpoints();saved=self.store.save_checkpoint(*self.binding('B'),'旧代次交接')
        with self.store.transaction() as db:db.execute("UPDATE members SET binding_version=binding_version+1 WHERE id='B'")
        with self.assertRaisesRegex(PairError,'binding mismatch'):self.store.check_checkpoint(*self.binding('B'))
        self.assertTrue(Path(saved['path']).is_file())

    def test_foreign_pair_legacy_orphan_is_not_recovered(self):
        legacy,_=self.legacy_checkpoint()
        with self.store.transaction() as db:db.execute("DELETE FROM checkpoints WHERE member='B'")
        with patch.object(self.store,'pair_id','other-pair'):
            with self.assertRaisesRegex(PairError,'identity mismatch'):self.store.recover_checkpoint(*self.binding('B'))
        self.assertTrue(legacy.is_file())

    def test_second_save_reuses_path_and_crash_recovery_preserves_new_content(self):
        self.prepare_checkpoints();first=self.store.save_checkpoint(*self.binding('B'),'首次')
        self.store.resume_checkpoint(*self.binding('B'),'继续')
        self.store.consume_checkpoint(*self.binding('B'),'B',self.read_all('B','B'),'核对后继续')
        event=self.store.event
        def crash(db,kind,**kwargs):
            if kind=='checkpoint_saved':raise RuntimeError('simulated crash')
            return event(db,kind,**kwargs)
        with patch.object(self.store,'event',side_effect=crash):
            with self.assertRaises(RuntimeError):self.store.save_checkpoint(*self.binding('B'),'第二次新内容')
        self.assertTrue(Path(first['path']).is_file())
        with self.assertRaisesRegex(PairError,'Unregistered'):self.store.check_checkpoint(*self.binding('B'))
        self.assertTrue(self.store.recover_checkpoint(*self.binding('B'))['recovered'])
        self.assertIn('第二次新内容',self.store.read_checkpoint(*self.binding('B'),'B')['text'])

    def test_low_quota_takeover_persists_before_cleanup_and_no_replay(self):
        self.prepare_checkpoints();self.separate_pools()
        message,claim=self.task()
        self.store.save_quota('A',quota(80,pool='a'));self.store.save_quota('B',quota(9,pool='b'))
        self.store.schedule()
        notices=[m for m in self.store.status()['messages'] if m['kind']=='checkpoint']
        self.assertEqual(len(notices),1)
        note=self.store.claim(*self.binding('B'),message_id=notices[0]['id'])
        saved=self.store.save_checkpoint(*self.binding('B'),'已完成调查；未完成写文件；无未知操作。'*200,message_id=note['id'],token=note['claim_token'])
        self.assertEqual(self.store.task('work')['status'],'quota_wait')
        with self.assertRaisesRegex(PairError,'already pending'):self.store.save_checkpoint(*self.binding('B'),'再写一份')
        self.store.schedule()
        resume=next(m for m in self.store.status()['messages'] if m['kind']=='checkpoint_resume')
        self.assertEqual(resume['recipient'],'A')
        received=self.store.claim(*self.binding('A'),message_id=resume['id'])
        last=self.store.read_checkpoint(*self.binding('A'),'B',offset=2000)
        with self.assertRaisesRegex(PairError,'fully read'):
            self.store.consume_checkpoint(*self.binding('A'),'B',last['digest'],'核对并继续')
        fingerprint=self.read_all('A','B')
        cleanup=self.store.consume_checkpoint(*self.binding('A'),'B',fingerprint,'调查已做，接下来仅写文件并验证')
        self.assertEqual(cleanup['cleanup'],'deleted');self.assertFalse(Path(saved['path']).exists())
        self.assertEqual(self.store.task('work')['assignee'],'A')
        self.store.complete(*self.binding('A'),resume['id'],received['claim_token'],'接管完成')
        self.assertTrue(self.store.consume_checkpoint(*self.binding('A'),'B',fingerprint,'重读')['duplicate'])
        self.assertTrue(self.store.complete(*self.binding('B'),message['id'],claim['claim_token'],'原窗口误完成')['duplicate'])
        self.assertEqual(self.store.task('work')['status'],'active')

    def test_both_low_wait_then_restore_only_after_margin(self):
        self.prepare_checkpoints();self.separate_pools()
        self.store.save_quota('A',quota(8,pool='a'));self.store.save_quota('B',quota(7,pool='b'))
        self.store.schedule()
        for member in ('A','B'):
            msg=next(m for m in self.store.status()['messages'] if m['kind']=='checkpoint' and m['recipient']==member)
            claim=self.store.claim(*self.binding(member),message_id=msg['id'])
            self.store.save_checkpoint(*self.binding(member),'已完成部分保留；未完成等待恢复',message_id=msg['id'],token=claim['claim_token'])
        self.store.schedule()
        self.assertFalse(any(m['kind']=='checkpoint_resume' for m in self.store.status()['messages']))
        self.store.save_quota('A',quota(13,pool='a'));self.store.schedule()
        self.assertFalse(any(m['kind']=='checkpoint_resume' for m in self.store.status()['messages']))
        self.store.save_quota('A',quota(80,pool='a'));self.store.schedule()
        self.assertTrue(any(m['kind']=='checkpoint_resume' and m['recipient']=='A' for m in self.store.status()['messages']))

    def test_shared_pool_and_unknown_do_not_offer_false_capacity(self):
        self.prepare_checkpoints()
        now=time.time()
        self.store.save_quota('A',quota(8,pool='shared',now=now));self.store.save_quota('B',quota(80,pool='shared',now=now))
        self.store.schedule()
        self.assertEqual(self.store.status()['assistance']['members']['B']['state'],'saving')
        with self.store.transaction() as db:self.store._put(db,'quota_state:B','ready')
        self.store.save_quota('B',{'reliability':'unsupported','observed_at':0,'remaining':None,'windows':[]})
        self.store.schedule()
        self.assertEqual(self.store.status()['assistance']['members']['B']['state'],'ready')

    def test_cli_exposes_actor_validated_commands(self):
        parser=build_parser()
        args=parser.parse_args(['begin-execution','--member','A','--request','req','--token','token','--digest','digest','--body','review'])
        self.assertEqual(args.command,'begin-execution')
        with self.assertRaises(PairError):parser.parse_args(['save-checkpoint','--body','missing actor'])

    def offered_checkpoint(self):
        self.prepare_checkpoints();self.separate_pools()
        self.task()
        self.store.save_quota('A',quota(80,pool='a'));self.store.save_quota('B',quota(8,pool='b'))
        saved=self.store.save_checkpoint(*self.binding('B'),'已完成检查；剩余写文件；没有未结束的操作')
        self.store.schedule()
        return saved,self.read_all('A','B')

    def test_cleanup_failure_retains_consumed_marker_then_can_retry(self):
        saved,fingerprint=self.offered_checkpoint()
        with patch.object(Path,'unlink',side_effect=PermissionError('denied')),patch('scripts.pair_assistance.recycle',side_effect=OSError('recycle unavailable')):
            result=self.store.consume_checkpoint(*self.binding('A'),'B',fingerprint,'核对后只继续剩余工作')
        self.assertEqual(result['cleanup'],'retained')
        self.assertTrue(Path(saved['path']).exists())
        self.assertEqual(self.store.task('work')['assignee'],'A')
        self.assertFalse(self.store.check_checkpoint(*self.binding('B'))['exists'])
        self.assertEqual(self.store.recover_checkpoint(*self.binding('B'))['cleanup'],'deleted')

    def test_file_change_is_not_deleted_or_consumed(self):
        saved,fingerprint=self.offered_checkpoint()
        Path(saved['path']).write_text('changed',encoding='utf-8')
        with self.assertRaisesRegex(PairError,'changed'):
            self.store.consume_checkpoint(*self.binding('A'),'B',fingerprint,'继续')
        self.assertEqual(self.store.task('work')['assignee'],'B')
        self.assertEqual(Path(saved['path']).read_text(encoding='utf-8'),'changed')

    def test_atomic_write_failure_keeps_task_and_removes_only_temporary(self):
        self.prepare_checkpoints();self.task()
        with patch('scripts.pair_assistance.os.replace',side_effect=OSError('write failed')):
            with self.assertRaises(OSError):self.store.save_checkpoint(*self.binding('B'),'当前现场')
        self.assertEqual(self.store.task('work')['status'],'active')
        self.assertFalse([p for p in (self.project/'.codex-pair'/'checkpoints').rglob('*') if p.is_file()])

    def test_crash_after_file_write_can_be_reconciled_without_reexecution(self):
        self.prepare_checkpoints();self.task()
        self.store.save_quota('B',quota(8));self.store.schedule()
        original_event=self.store.event
        def crash(db,kind,**kwargs):
            if kind=='checkpoint_saved':raise RuntimeError('simulated crash before commit')
            return original_event(db,kind,**kwargs)
        with patch.object(self.store,'event',side_effect=crash):
            with self.assertRaises(RuntimeError):self.store.save_checkpoint(*self.binding('B'),'已完成调查；剩余写文件')
        self.assertEqual(self.store.task('work')['status'],'active')
        with self.assertRaisesRegex(PairError,'Unregistered'):
            self.store.check_checkpoint(*self.binding('B'))
        self.assertTrue(self.store.recover_checkpoint(*self.binding('B'))['recovered'])
        self.assertEqual(self.store.task('work')['status'],'quota_wait')
        self.assertEqual(len(list((self.project/'.codex-pair'/'checkpoints').rglob('*.md'))),1)
        self.assertTrue(all(m['process_status']=='completed' for m in self.store.status()['messages'] if m['kind']=='checkpoint' and m['recipient']=='B'))

    def takeover_preserves_mode(self,new_context):
        self.prepare_checkpoints();self.separate_pools()
        self.store.setting('member_context_required',new_context)
        self.store.delegation_policy(*self.binding('A'),'当前任务内普通问题由A决定',enabled=True)
        question=self.store.send('B','A','后续实现需要决定',kind='question')
        self.store.assign('first','统筹剩余工作','A',['result.txt'])
        self.store.assign('second','依赖','A',['next.txt'],dependencies=['first'])
        first=next(m for m in self.store.status()['messages'] if m['task_id']=='first')
        self.store.claim(*self.binding('A'),message_id=first['id'])
        self.store.save_quota('A',quota(8,pool='a'));self.store.save_quota('B',quota(80,pool='b'))
        self.store.save_checkpoint(*self.binding('A'),'已做调查，后续写文件及验证')
        self.store.schedule()
        fingerprint=self.read_all('B','A')
        self.store.consume_checkpoint(*self.binding('B'),'A',fingerprint,'只做剩余写入与验证')
        coordinator='A' if new_context else 'B'
        self.assertEqual(self.store.status()['assistance']['coordinator'],coordinator)
        self.assertEqual(self.store.task('second')['status'],'pending')
        self.assertEqual(self.store.task('second')['assignee'],'B')
        self.assertEqual(self.store.task('first')['report_to'],coordinator)
        self.assertEqual(self.store.task('second')['report_to'],coordinator)
        decision=next(q for q in self.store.status()['assistance']['questions'] if q['id']==question['id'])
        self.assertEqual(decision['owner'],coordinator)
        self.assertEqual(self.store.assign('third','后续任务','B',['third.txt'])['report_to'],coordinator)
        with self.store.transaction() as db:self.assertEqual(self.store._option(db,'approval_authority','A'),'A')

    def test_takeover_transfers_coordinator_and_keeps_dependency_unfinished(self):
        self.takeover_preserves_mode(False)

    def test_new_mode_takeover_keeps_a_coordinator_and_approval_authority(self):
        self.takeover_preserves_mode(True)

    def test_manual_resume_unknown_quota_and_no_override_of_known_low(self):
        self.prepare_checkpoints()
        self.store.save_checkpoint(*self.binding('B'),'用户要求暂存，剩余工作等待')
        self.store.save_quota('B',quota(5))
        with self.assertRaisesRegex(PairError,'margin'):
            self.store.resume_checkpoint(*self.binding('B'),'用户要求继续')
        self.store.save_quota('B',{'reliability':'unsupported','remaining':None,'windows':[],'observed_at':0})
        self.assertFalse(self.store.resume_checkpoint(*self.binding('B'),'用户确认可以继续')['quota_verified'])
        fingerprint=self.read_all('B','B')
        self.assertTrue(self.store.consume_checkpoint(*self.binding('B'),'B',fingerprint,'核对现场后继续')['consumed'])

    def test_unknown_execution_result_blocks_parent_until_explicit_reconciliation(self):
        _,_,request=self.request()
        owned=self.store.claim(*self.binding('A'),message_id=request['message_id'])
        self.store.begin_execution(*self.binding('A'),'req1',owned['claim_token'],request['digest'],'核对授权')
        receipt=self.store.finish_execution(*self.binding('A'),'req1',owned['claim_token'],'unknown',{'summary':'工具回执丢失，需查文件'})
        received=self.store.claim(*self.binding('B'),message_id=receipt['message_id'])
        self.acknowledge('B',receipt['delivery_id'])
        self.store.complete(*self.binding('B'),receipt['message_id'],received['claim_token'],'已读，先核查不重跑')
        self.assertEqual(self.store.task('work')['status'],'waiting_execution')
        self.store.resolve_execution(*self.binding('A'),'req1','已回读产物，文件生成成功，后续不再执行原命令')
        self.assertEqual(self.store.task('work')['status'],'active')

    def test_execution_reservation_blocks_other_workspace_messages(self):
        _,_,request=self.request()
        owned=self.store.claim(*self.binding('A'),message_id=request['message_id'])
        self.store.begin_execution(*self.binding('A'),'req1',owned['claim_token'],request['digest'],'核对授权')
        self.store.send('A','B','另一个可能触发业务的消息')
        self.assertFalse(Daemon(self.store,test_core.FakeTransport(self.store)).dispatch_one())

    def test_consumption_rechecks_capacity_after_offer(self):
        _,fingerprint=self.offered_checkpoint()
        self.store.save_quota('A',quota(5,pool='a'))
        with self.assertRaisesRegex(PairError,'capacity'):
            self.store.consume_checkpoint(*self.binding('A'),'B',fingerprint,'不能在不足时继续')
        self.assertEqual(self.store.task('work')['assignee'],'B')

    def test_v5_upgrade_preserves_delivery_ack_and_original_claim(self):
        message,claim=self.task()
        self.store.complete(*self.binding('B'),message['id'],claim['claim_token'],'完成测试交付')
        self.acknowledge('A','delivery:work')
        with self.store.transaction() as db:
            db.execute('DROP TABLE execution_requests');db.execute('DROP TABLE checkpoints');db.execute('DROP TABLE approval_requests');db.execute('DROP TABLE delegation_questions')
            remove_team_extensions(db)
            db.execute('DROP TABLE reviewer_runtime');db.execute('DROP TABLE reviewer_history')
            db.execute('PRAGMA user_version=5')
        upgraded=Store('test',home=self.home)
        self.assertEqual(upgraded.message(message['id'])['claim_token'],claim['claim_token'])
        with upgraded.transaction() as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],13)
            upgraded._require_ack(db,'A','delivery:work')
        self.assertTrue(list(upgraded.directory.glob('schema-v5-*.backup.sqlite3')))

    @unittest.skipUnless(os.name=='nt','Windows SQLite 长路径回归')
    def test_v5_backup_long_path_preserves_delivery_and_claim(self):
        original=tempfile.TemporaryDirectory
        # 固定临时根长，使原备份名超过普通 SQLite 辅助文件路径限制。
        prefix_size=182-len(str(self.root))-1-8
        if prefix_size<1:self.skipTest('父测试目录过深，无法构造固定182字符对照根')
        def directory(*args,**kwargs):
            return original(prefix='p'*prefix_size,dir=self.root)
        fixture=AssistanceTests('test_v5_upgrade_preserves_delivery_ack_and_original_claim')
        with patch.object(tempfile,'TemporaryDirectory',side_effect=directory):
            fixture.setUp()
        try:
            self.assertEqual(len(str(fixture.root)),182)
            fixture.test_v5_upgrade_preserves_delivery_ack_and_original_claim()
        finally:fixture.tearDown()

    def test_decline_cannot_be_retried_using_another_request_id(self):
        message,claim,request=self.request()
        owned=self.store.claim(*self.binding('A'),message_id=request['message_id'])
        receipt=self.store.finish_execution(*self.binding('A'),'req1',owned['claim_token'],'declined',{'summary':'用户明确拒绝该操作'})
        received=self.store.claim(*self.binding('B'),message_id=receipt['message_id'])
        self.acknowledge('B',receipt['delivery_id'])
        self.store.complete(*self.binding('B'),receipt['message_id'],received['claim_token'],'已读拒绝，不重试')
        with self.assertRaisesRegex(PairError,'declined'):
            self.store.request_execution(*self.binding('B'),message['id'],claim['claim_token'],'different-id',self.payload())

    def test_observed_handoff_cost_raises_reserve_without_token_conversion(self):
        self.prepare_checkpoints();self.separate_pools()
        # 独立临时库只模拟B的交接消耗，没有同池并行业务。
        self.store.setting('calibration_isolated:B',True)
        before=quota(9,pool='b')
        self.store.save_quota('B',before);self.store.schedule()
        after=dict(before,remaining=3)
        self.store.save_quota('B',after)
        self.store.save_checkpoint(*self.binding('B'),'保存完整现场')
        self.assertEqual(self.store.status()['assistance']['members']['B']['reserve_percent'],14)

    def test_mixed_account_usage_does_not_calibrate_handoff_cost(self):
        self.prepare_checkpoints();self.separate_pools()
        before=quota(9,pool='b')
        self.store.save_quota('B',before);self.store.schedule()
        self.store.save_quota('B',dict(before,remaining=3))
        self.store.save_checkpoint(*self.binding('B'),'期间还有其他业务用量，没有隔离证据')
        self.assertEqual(self.store.status()['assistance']['members']['B']['reserve_percent'],9)

    def test_followup_planning_uses_transferred_coordinator(self):
        self.store.setting('coordinator','B')
        self.store.setting('window_flow',{'goal':'完成目标'})
        self.store.assign('work','实现','B',['result.txt'])
        task=next(m for m in self.store.status()['messages'] if m['kind']=='task')
        owned=self.store.claim(*self.binding('B'),message_id=task['id'])
        self.store.complete(*self.binding('B'),task['id'],owned['claim_token'],'文件完成且已验证')
        result=next(m for m in self.store.status()['messages'] if m['kind']=='result')
        self.assertEqual(result['recipient'],'B')
        owned=self.store.claim(*self.binding('B'),message_id=result['id'])
        self.acknowledge('B','delivery:work')
        self.store.complete(*self.binding('B'),result['id'],owned['claim_token'],'已读完并核对结果')
        planning=next(t for t in self.store.status()['tasks'] if t['id'].startswith('pair-plan-'))
        self.assertEqual(planning['assignee'],'B')

    def test_original_turn_holds_handoff_and_execution_dispatch(self):
        message,_,request=self.request()
        with self.store.transaction() as db:db.execute("UPDATE messages SET delivery_status='accepted',remote_status='inProgress' WHERE id=?",(message['id'],))
        self.assertFalse(self.store.claim(*self.binding('A'),message_id=request['message_id'])['claimed'])
        self.assertFalse(Daemon(self.store,test_core.FakeTransport(self.store)).dispatch_one())

    def test_real_reversible_command_uses_registered_argv_and_readback(self):
        self.store.configure_assistance(*self.binding('A'),execution=True)
        message,claim=self.task()
        import sys
        payload=dict(self.payload(),argv=[sys.executable,'-c',"from pathlib import Path; Path('result.txt').write_text('OK', encoding='utf-8')"])
        request=self.store.request_execution(*self.binding('B'),message['id'],claim['claim_token'],'real',payload)
        owned=self.store.claim(*self.binding('A'),message_id=request['message_id'])
        begin=self.store.begin_execution(*self.binding('A'),'real',owned['claim_token'],request['digest'],'临时目录内单一授权文件，命令无其他副作用')
        operation=begin['payload']
        process=subprocess.run(operation['argv'],cwd=operation['cwd'],capture_output=True,text=True,check=True)
        output=Path(operation['cwd'])/'result.txt'
        self.assertEqual(output.read_text(encoding='utf-8'),'OK')
        self.store.finish_execution(*self.binding('A'),'real',owned['claim_token'],'success',
            {'summary':'临时目录命令执行并回读成功','argv':operation['argv'],'cwd':operation['cwd'],
             'exit_code':process.returncode,'stdout':process.stdout,'stderr':process.stderr,'evidence':str(output)})
        self.assertEqual(self.store.task('work')['status'],'waiting_execution')

    def test_request_stdin_needs_no_project_request_file(self):
        import io
        from scripts import pair
        self.store.configure_assistance(*self.binding('A'),execution=True)
        message,claim=self.task()
        args=build_parser().parse_args(['request-execution','--pair','test','--codex-home',str(self.home),
            '--state-root',str(self.store.state_root),'--member','B','--session',self.sessions['B'],
            '--worktree',self.directories['B'],'--message',message['id'],'--token',claim['claim_token'],
            '--request','stdin-request','--request-stdin'])
        text='\ufeff'+json.dumps(self.payload(),ensure_ascii=False)
        for stream in (io.StringIO(text),io.TextIOWrapper(io.BytesIO(text.encode('utf-8')),encoding='ascii')):
            with patch('scripts.pair.validate_actor'),patch('scripts.pair.sys.stdin',stream):result=pair.execute(args)
            stream.close()
        self.assertEqual(result['status'],'review')
        self.assertEqual(self.store.task('work')['status'],'waiting_execution')
        self.assertFalse((self.project/'.codex-pair'/'handoffs').exists())
        self.assertEqual(json.loads(result['payload'])['purpose'],'完成当前任务')

    def test_invalid_stdin_leaves_parent_active(self):
        import io
        from scripts import pair
        self.store.configure_assistance(*self.binding('A'),execution=True)
        message,claim=self.task()
        args=build_parser().parse_args(['request-execution','--pair','test','--codex-home',str(self.home),
            '--state-root',str(self.store.state_root),'--member','B','--session',self.sessions['B'],
            '--worktree',self.directories['B'],'--message',message['id'],'--token',claim['claim_token'],
            '--request','invalid-stdin','--request-stdin'])
        with patch('scripts.pair.validate_actor'),patch('scripts.pair.sys.stdin',io.StringIO('not JSON')):
            with self.assertRaises(json.JSONDecodeError):pair.execute(args)
        self.assertEqual(self.store.task('work')['status'],'active')
        self.assertEqual(self.store.status()['assistance']['requests'],[])

    def test_request_file_and_stdin_are_exclusive(self):
        binding=['request-execution','--member','B','--message','message','--token','token','--request','request']
        for extra in ([],['--request-file','input.json','--request-stdin']):
            with self.subTest(extra=extra),self.assertRaises(PairError):build_parser().parse_args(binding+extra)


if __name__=='__main__':unittest.main()
