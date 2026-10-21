"""协助执行和低额度交接的行为测试；不调用付费模型。"""
import json
from pathlib import Path
import subprocess
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
        self.store.save_quota('A',quota(8,pool='shared'));self.store.save_quota('B',quota(80,pool='shared'))
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
        self.assertFalse(list((self.project/'.codex-pair'/'checkpoints').glob('*')))

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
        self.assertEqual(len(list((self.project/'.codex-pair'/'checkpoints').glob('*.md'))),1)
        self.assertTrue(all(m['process_status']=='completed' for m in self.store.status()['messages'] if m['kind']=='checkpoint' and m['recipient']=='B'))

    def test_takeover_transfers_coordinator_and_keeps_dependency_unfinished(self):
        self.prepare_checkpoints();self.separate_pools()
        self.store.assign('first','统筹剩余工作','A',['result.txt'])
        self.store.assign('second','依赖','A',['next.txt'],dependencies=['first'])
        first=next(m for m in self.store.status()['messages'] if m['task_id']=='first')
        self.store.claim(*self.binding('A'),message_id=first['id'])
        self.store.save_quota('A',quota(8,pool='a'));self.store.save_quota('B',quota(80,pool='b'))
        self.store.save_checkpoint(*self.binding('A'),'已做调查，后续写文件及验证')
        self.store.schedule()
        fingerprint=self.read_all('B','A')
        self.store.consume_checkpoint(*self.binding('B'),'A',fingerprint,'只做剩余写入与验证')
        self.assertEqual(self.store.status()['assistance']['coordinator'],'B')
        self.assertEqual(self.store.task('second')['status'],'pending')
        self.assertEqual(self.store.task('second')['assignee'],'B')
        self.assertEqual(self.store.assign('third','后续任务','B',['third.txt'])['report_to'],'B')

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
            db.execute('PRAGMA user_version=5')
        upgraded=Store('test',home=self.home)
        self.assertEqual(upgraded.message(message['id'])['claim_token'],claim['claim_token'])
        with upgraded.transaction() as db:
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],8)
            upgraded._require_ack(db,'A','delivery:work')
        self.assertTrue(list(upgraded.directory.glob('schema-v5-*.backup.sqlite3')))

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
        before=quota(9,pool='b')
        self.store.save_quota('B',before);self.store.schedule()
        after=dict(before,remaining=3)
        self.store.save_quota('B',after)
        self.store.save_checkpoint(*self.binding('B'),'保存完整现场')
        self.assertEqual(self.store.status()['assistance']['members']['B']['reserve_percent'],14)

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
