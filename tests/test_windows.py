"""当前两个窗口接入，不依赖账号名称或另建 CLI。"""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts import pair, pair_git, pair_windows
from scripts.pair_core import Daemon, MIGRATION_2, SCHEMA, PairError, Store, member_workspace
from scripts.pair_transport import canonical_path
from tests.test_core import FakeTransport
from tests.test_integration import seed
from tests.compatibility_fixture import register_cli_fixture


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='pair-windows-')
        self.root=Path(self.temp.name);self.project=self.root/'project';seed(self.project)
        self.windows={}
        for index,member in enumerate(('A','B'),1):
            cwd=self.root/('opened-terminal-'+member);cwd.mkdir()
            home=self.root/('arbitrary-account-'+str(index));home.mkdir()
            sid='00000000-0000-0000-0000-'+str(index).zfill(12)
            self.windows[member]={'session_uuid':sid,'cwd':canonical_path(cwd),'codex_home':canonical_path(home),
                'proof':{'id':sid,'cwd':canonical_path(cwd),'modelProvider':'openai','model':'test-model'}}
        self.bridge_patch=patch('scripts.pair_windows.bridge',return_value={'running':True,'pid':123,'reused':False})
        self.fake_bridge=self.bridge_patch.start()
        self.addCleanup(self.bridge_patch.stop)

    def tearDown(self):self.temp.cleanup()

    def run_cli(self,member,*argv):
        with patch('scripts.pair_windows.current_window',return_value=self.windows[member]),patch('scripts.pair_windows.AppServer') as observer:
            observer.return_value.__enter__.return_value.validate.return_value=self.windows[member]['proof']
            return pair.execute(pair.build_parser().parse_args(list(argv)))

    def start(self):
        return self.run_cli('A','start','--project',str(self.project),'--goal','修复目标项目',
                            '--workspace-mode','shared','--workspace-reason','旧共享目录回归夹具')

    def joined(self):
        started=self.start()
        accepted=self.run_cli('B','accept','--invite',started['invite_path'])
        return started,accepted,Store(started['pair'],state_root=started['state_root'])

    def confirm_context(self,store,member='B'):
        register_cli_fixture(store, tuple(row['id'] for row in store.visible_members()))
        bound=store.member(member);offset=0
        while True:
            page=store.read_member_context(member,bound['session_uuid'],bound['cwd'],offset=offset,limit=128)
            if page['next_offset'] is None:break
            offset=page['next_offset']
        return store.confirm_member(member,bound['session_uuid'],bound['cwd'],proof=json.loads(bound['proof']),context_digest=page['digest'])

    def finish_handshake(self,store,member='B'):
        question=next(m for m in store.status()['messages'] if m['kind']=='question' and m['sender']==member)
        a=store.member('A');b=store.member(member)
        claim=store.claim('A',a['session_uuid'],a['cwd'],message_id=question['id'])
        reply=store.send('A',member,'确认原窗口',kind='reply',reply_to=question['id'])
        store.complete('A',a['session_uuid'],a['cwd'],question['id'],claim['claim_token'],'已回复')
        claim=store.claim(member,b['session_uuid'],b['cwd'],message_id=reply['id'])
        store.complete(member,b['session_uuid'],b['cwd'],reply['id'],claim['claim_token'],'已收到')
        with store.transaction() as db:db.execute("UPDATE messages SET remote_status='completed',delivery_status='accepted'")
        self.confirm_context(store,member)

    def test_current_window_requires_its_own_environment_and_validates_no_create(self):
        with patch.dict(os.environ,{},clear=True),patch('scripts.pair_windows.AppServer') as observer:
            with self.assertRaisesRegex(PairError,'Current CLI required'):pair_windows.current_window()
            observer.assert_not_called()
        window=self.windows['A']
        with patch.dict(os.environ,{'CODEX_THREAD_ID':window['session_uuid'],'CODEX_HOME':window['codex_home']}),patch('scripts.pair_windows.Path.cwd',return_value=self.project),patch('scripts.pair_windows.AppServer') as observer:
            observer.return_value.__enter__.return_value.read_thread.return_value=window['proof']
            observer.return_value.__enter__.return_value.validate.return_value=window['proof']
            actual=pair_windows.current_window()
            self.assertEqual(actual['session_uuid'],window['session_uuid'])
            self.assertEqual(actual['cwd'],window['cwd'],'不能把工具临时 workdir 当成会话根目录')
            self.assertEqual(observer.call_args.kwargs['codex_home'],window['codex_home'])
            observer.return_value.__enter__.return_value.create_session.assert_not_called()
            observer.return_value.__enter__.return_value.resume.assert_not_called()

    def test_explicit_shared_mode_uses_current_windows_and_preserves_serial_workspace(self):
        started,accepted,store=self.joined()
        self.assertEqual(accepted['role'],'B')
        self.assertEqual(store.member('A')['session_uuid'],self.windows['A']['session_uuid'])
        self.assertEqual(store.member('B')['codex_home'],self.windows['B']['codex_home'])
        self.assertEqual(store.git_context()['worktrees'],{})
        self.assertFalse((self.project/'.codex-pair'/'worktrees').exists())
        self.assertEqual(member_workspace(store.member('B')),canonical_path(self.project))
        self.assertEqual(store.member('B')['cwd'],self.windows['B']['cwd'])
        self.assertNotIn('arbitrary-account',Path(started['invite_path']).read_text(encoding='utf-8'))
        self.assertIn('CODEX_PAIR_JOIN_V1',started['join_instruction'])
        self.assertIn('accept',started['join_argv'])
        self.assertFalse(started['automatic_peer_discovery'])
        self.assertEqual(store.status()['window_flow']['communication'],'awaiting_handshake')

    def test_default_start_accept_create_independent_trees_and_allow_parallel_dispatch(self):
        started=self.run_cli('A','start','--project',str(self.project),'--goal','默认隔离')
        accepted=self.run_cli('B','accept','--invite',started['invite_path'])
        store=Store(started['pair'],state_root=started['state_root'])
        a,b=store.member('A'),store.member('B')
        for member,returned in ((a,started),(b,accepted)):
            workspace=member_workspace(member)
            self.assertEqual(returned['workspace'],workspace)
            self.assertTrue(Path(workspace).is_relative_to(self.project/'.codex-pair/worktrees'))
            self.assertEqual(member['cwd'],self.windows[member['id']]['cwd'])
            self.assertEqual(pair_git.inspect_repo(workspace)['head'],store.status()['pair']['base_commit'])
        self.assertNotEqual(member_workspace(a),member_workspace(b))
        self.finish_handshake(store);store.setting('review_required',False)
        with store.transaction() as db:db.execute('DELETE FROM messages')
        store.assign('first','A开发','A',['same.py']);store.assign('second','B开发','B',['same.py'])
        self.assertEqual([store.task(t)['status'] for t in ('first','second')],['active','active'])
        fake=FakeTransport(store);daemon=Daemon(store,fake)
        self.assertTrue(daemon.dispatch_one());self.assertTrue(daemon.dispatch_one())
        self.assertEqual({call[0]['id'] for call in fake.calls},{'A','B'})

    def test_default_additional_seats_get_separate_trees_without_replacing_existing_plans(self):
        started=self.run_cli('A','start','--project',str(self.project),'--goal','四席位')
        self.run_cli('B','accept','--invite',started['invite_path'])
        store=Store(started['pair'],state_root=started['state_root']);self.finish_handshake(store)
        previous=dict(store.git_context()['worktrees'])
        for index,member in enumerate(('C','D'),3):
            window=dict(self.windows['B'],session_uuid='00000000-0000-0000-0000-'+str(index).zfill(12))
            window['proof']=dict(window['proof'],id=window['session_uuid']);self.windows[member]=window
            a=store.member('A');payload=store.create_invite('A',a['session_uuid'],a['cwd'])
            path=self.root/(member+'-invite.json');path.write_text(json.dumps(payload),encoding='utf-8')
            result=self.run_cli(member,'accept','--invite',str(path))
            self.assertEqual(result['role'],member)
        plans=store.git_context()['worktrees']
        self.assertEqual({key:plans[key] for key in previous},previous)
        self.assertEqual(len({member_workspace(store.member(member)) for member in 'ABCD'}),4)

    def test_shared_mode_requires_reason_before_creating_state(self):
        with self.assertRaisesRegex(PairError,'Shared workspace reason required'):
            self.run_cli('A','start','--project',str(self.project),'--goal','共享','--workspace-mode','shared')
        self.assertFalse((self.project/'.codex-pair/state').exists())

    def test_default_keeps_existing_project_local_tree_and_original_actor_cwd(self):
        trees=pair_git.create_worktrees(str(self.project),'existing-default','HEAD')
        path=canonical_path(trees['a']['path'])
        self.windows['A']['cwd']=path;self.windows['A']['proof']['cwd']=path
        started=self.run_cli('A','start','--project',str(self.project),'--goal','沿用原工作树')
        self.assertEqual(started['workspace'],path)
        self.assertEqual(started['current_window']['cwd'],path)
        self.assertEqual(pair_git.inspect_repo(path)['branch'],trees['a']['branch'])

    def test_worktree_creation_failure_keeps_intent_and_does_not_start_bridge(self):
        with patch('scripts.pair_windows.pair_git.create_worktrees',side_effect=pair_git.GitError('fixture creation failure')):
            with self.assertRaisesRegex(pair_git.GitError,'fixture creation failure'):
                self.run_cli('A','start','--project',str(self.project),'--goal','隔离失败')
        self.fake_bridge.assert_not_called()
        path=next((self.project/'.codex-pair/state').glob('*/state.sqlite3'))
        store=Store(path.parent.name,state_root=path.parent.parent)
        self.assertEqual(store.visible_members(),[])
        with store.transaction() as db:
            intent=json.loads(db.execute("SELECT value FROM pair_settings WHERE key LIKE 'managed_tree:%'").fetchone()[0])
            self.assertEqual(intent['state'],'creating')

    def test_isolated_start_does_not_silently_omit_requested_dirty_baseline(self):
        from scripts.pair_review_runtime import tree_stamp
        dirty=self.project/'user-change.txt';dirty.write_text('保留用户内容',encoding='utf-8')
        plan=self.root/'baseline.json'
        plan.write_text(json.dumps({'stamp':tree_stamp(self.project),'include':['user-change.txt'],
            'exclude':[],'user_instruction':'本轮需要该文件'}),encoding='utf-8')
        with self.assertRaisesRegex(PairError,'Committed baseline required'):
            self.run_cli('A','start','--project',str(self.project),'--goal','保留基准','--baseline-plan-file',str(plan))
        self.assertEqual(dirty.read_text(encoding='utf-8'),'保留用户内容')
        self.fake_bridge.assert_not_called()
        self.assertFalse((self.project/'.codex-pair/worktrees').exists())

    def test_unaccepted_invite_cannot_silently_replace_new_goal(self):
        started=self.start();original=Path(started['invite_path']).read_bytes()
        for member in ('A','B'):
            with self.subTest(member=member),self.assertRaisesRegex(PairError,'Existing goal differs'):
                self.run_cli(member,'start','--project',str(self.project),'--goal','另一个目标')
        self.assertEqual(Path(started['invite_path']).read_bytes(),original)
        self.assertEqual(len(list((self.project/'.codex-pair/state').glob('*/state.sqlite3'))),1)

    def test_repeated_same_goal_before_accept_keeps_invite_and_a(self):
        started=self.start();again=self.start()
        self.assertEqual((again['pair'],again['role'],again['invite_path']),(started['pair'],'A',started['invite_path']))

    def test_same_home_and_same_cwd_are_valid_but_same_session_is_not(self):
        self.windows['B']['codex_home']=self.windows['A']['codex_home']
        self.windows['B']['cwd']=self.windows['A']['cwd']
        self.windows['B']['proof']['cwd']=self.windows['A']['cwd']
        started,_,store=self.joined()
        self.assertEqual(store.member('A')['cwd'],store.member('B')['cwd'])
        with self.assertRaisesRegex(PairError,'Second window required'):
            self.run_cli('A','accept','--invite',started['invite_path'])

    def test_repeated_invite_and_accept_reuse_binding_and_single_handshake(self):
        started,_,store=self.joined()
        second=self.run_cli('A','start','--project',str(self.project),'--goal','修复目标项目','--resume-pair',started['pair'])
        self.assertEqual(second['pair'],started['pair'])
        self.assertEqual(second['invite_path'],started['invite_path'])
        self.run_cli('B','accept','--invite',started['invite_path'])
        self.assertEqual(len(store.status()['messages']),1)
        reused_b=self.run_cli('B','start','--project',str(self.project),'--goal','修复目标项目','--resume-pair',started['pair'])
        self.assertEqual(reused_b['role'],'B')
        self.assertNotIn('join_instruction',reused_b)

    def test_previous_b_starts_new_round_as_a_and_old_invite_stops(self):
        started,_,store=self.joined()
        with store.transaction() as db:
            db.execute("UPDATE messages SET process_status='completed',remote_status='completed'")
        new=self.run_cli('B','start','--project',str(self.project),'--goal','下一轮目标')
        self.assertNotEqual(new['pair'],started['pair'])
        self.assertEqual(new['role'],'A')
        self.assertEqual(new['current_window']['session_uuid'],self.windows['B']['session_uuid'])
        self.assertEqual(store.status()['pair']['status'],'stopped')
        with self.assertRaisesRegex(PairError,'Invitation stopped'):
            self.run_cli('B','accept','--invite',started['invite_path'])

    def test_start_cannot_replace_unresolved_work(self):
        started,_,store=self.joined()
        with self.assertRaisesRegex(PairError,'Previous work unresolved'):
            self.run_cli('B','start','--project',str(self.project),'--goal','新目标')
        self.assertEqual(store.member('B')['session_uuid'],self.windows['B']['session_uuid'])

    def test_concurrent_start_returns_one_invitation_and_one_a(self):
        import threading
        import concurrent.futures
        identity=threading.local()
        barrier=threading.Barrier(2)
        def call(member):
            identity.member=member
            args=pair.build_parser().parse_args(['start','--project',str(self.project),'--goal','并发目标'])
            barrier.wait(timeout=10)
            return pair.execute(args)
        with patch('scripts.pair_windows.current_window',side_effect=lambda *args:self.windows[identity.member]),concurrent.futures.ThreadPoolExecutor(2) as pool:
            results=list(pool.map(call,['A','B']))
        self.assertEqual(results[0]['pair'],results[1]['pair'])
        self.assertEqual(sum(r['role']=='A' for r in results),1)
        self.assertEqual(sum(r.get('requires_accept',False) for r in results),1)
        self.assertEqual(len(list((self.project/'.codex-pair/state').glob('*/state.sqlite3'))),1)

    def test_changed_invitation_is_rejected_before_contacting_window(self):
        started=self.start();path=Path(started['invite_path'])
        payload=json.loads(path.read_text(encoding='utf-8'));payload['goal']='另一个目标'
        path.write_text(json.dumps(payload),encoding='utf-8')
        with patch('scripts.pair_windows.current_window') as current:
            with self.assertRaisesRegex(PairError,'Invitation proof mismatch'):
                pair.execute(pair.build_parser().parse_args(['accept','--invite',str(path)]))
            current.assert_not_called()

    def test_repeated_accept_rejects_goal_completion_before_contact(self):
        started,_,store=self.joined()
        store.setting('goal_completed',True)
        with patch('scripts.pair_windows.current_window') as current:
            with self.assertRaisesRegex(PairError,'Goal completed'):
                pair.execute(pair.build_parser().parse_args(['accept','--invite',started['invite_path']]))
            current.assert_not_called()

    def test_repeated_accept_rejects_changed_requirements_before_contact(self):
        started,_,store=self.joined()
        store.setting('requirements_version',2)
        with patch('scripts.pair_windows.current_window') as current:
            with self.assertRaisesRegex(PairError,'Requirements changed'):
                pair.execute(pair.build_parser().parse_args(['accept','--invite',started['invite_path']]))
            current.assert_not_called()

    def test_repeated_accept_rejects_changed_inviter_before_contact(self):
        started,_,store=self.joined()
        with store.transaction() as db:db.execute("UPDATE members SET binding_version=binding_version+1 WHERE id='A'")
        with patch('scripts.pair_windows.current_window') as current:
            with self.assertRaisesRegex(PairError,'Inviter mismatch'):
                pair.execute(pair.build_parser().parse_args(['accept','--invite',started['invite_path']]))
            current.assert_not_called()

    def test_stopped_invitation_is_rejected_before_contact(self):
        started,_,store=self.joined();store.control('stopped')
        with patch('scripts.pair_windows.current_window') as current:
            with self.assertRaisesRegex(PairError,'Invitation stopped'):
                pair.execute(pair.build_parser().parse_args(['accept','--invite',started['invite_path']]))
            current.assert_not_called()

    def test_copied_initial_invitation_is_rejected_before_contact(self):
        started=self.start();path=Path(started['invite_path']);copy=path.with_name('copy.json')
        copy.write_bytes(path.read_bytes())
        with patch('scripts.pair_windows.current_window') as current:
            with self.assertRaisesRegex(PairError,'Invitation proof mismatch'):
                pair.execute(pair.build_parser().parse_args(['accept','--invite',str(copy)]))
            current.assert_not_called()

    def test_repeated_accept_rejects_different_home(self):
        started,_,store=self.joined()
        self.windows['B']['codex_home']=self.windows['A']['codex_home']
        with self.assertRaisesRegex(PairError,'Invitation consumed'):
            self.run_cli('B','accept','--invite',started['invite_path'])
        self.assertNotEqual(store.member('B')['codex_home'],self.windows['B']['codex_home'])

    def test_context_confirmation_requires_complete_page_coverage(self):
        _,_,store=self.joined();self.finish_handshake(store)
        b=store.member('B')
        with store.transaction() as db:db.execute("DELETE FROM member_context_reads WHERE member='B'")
        page=store.read_member_context('B',b['session_uuid'],b['cwd'],limit=1)
        with self.assertRaisesRegex(PairError,'Context not read'):
            store.confirm_member('B',b['session_uuid'],b['cwd'],proof=json.loads(b['proof']),context_digest=page['digest'])

    def test_additional_member_confirmation_uses_distinct_ready_notification(self):
        _,_,store=self.joined();self.finish_handshake(store)
        a=store.member('A')
        payload=store.create_invite('A',a['session_uuid'],a['cwd'],target='C')
        path=self.project/'.codex-pair'/'handoffs'/'extra-invite.json'
        path.write_text(json.dumps(payload),encoding='utf-8')
        window=dict(self.windows['B']);window['session_uuid']='00000000-0000-0000-0000-000000000003'
        window['proof']=dict(window['proof'],id=window['session_uuid']);self.windows['C']=window
        self.run_cli('C','accept','--invite',str(path))
        self.finish_handshake(store,'C')
        self.assertTrue(store.member('C')['verified'])
        ready=[m for m in store.status()['messages'] if m['kind']=='note']
        self.assertEqual({m['sender'] for m in ready},{'B','C'})

    def test_queued_handshake_cannot_confirm_context(self):
        _,_,store=self.joined()
        question=store.status()['messages'][0];a=store.member('A');b=store.member('B')
        owned=store.claim('A',a['session_uuid'],a['cwd'],message_id=question['id'])
        reply=store.send('A','B','已收到',kind='reply',reply_to=question['id'])
        store.complete('A',a['session_uuid'],a['cwd'],question['id'],owned['claim_token'],'已答复')
        owned=store.claim('B',b['session_uuid'],b['cwd'],message_id=reply['id'])
        store.complete('B',b['session_uuid'],b['cwd'],reply['id'],owned['claim_token'],'已阅读')
        with store.transaction() as db:db.execute("UPDATE messages SET delivery_status='accepted',remote_status='queued'")
        with self.assertRaisesRegex(PairError,'Communication unverified'):self.confirm_context(store)
        self.assertFalse(store.member('B')['verified'])

    def test_shared_workspace_serializes_tasks_and_remote_turns(self):
        _,_,store=self.joined()
        self.finish_handshake(store)
        store.setting('review_required',False)
        with store.transaction() as db:db.execute('DELETE FROM messages')
        store.assign('one','A 修改','A',['one.py'])
        store.assign('two','B 修改','B',['two.py'])
        self.assertEqual(store.task('two')['status'],'pending')
        fake=FakeTransport(store);daemon=Daemon(store,fake)
        self.assertTrue(daemon.dispatch_one())
        message=store.status()['messages'][0];a=store.member('A')
        claim=store.claim('A',a['session_uuid'],a['cwd'],message_id=message['id'])
        store.complete('A',a['session_uuid'],a['cwd'],message['id'],claim['claim_token'],'A 已完成')
        self.assertEqual(store.task('two')['status'],'active')
        self.assertFalse(daemon.dispatch_one(),'A 的远程回合结束前不启动共享目录 B')
        fake.observation={'turn_id':'turn-one','remote_status':'completed'}
        daemon.reconcile()
        self.assertTrue(daemon.dispatch_one())

    def test_shared_workspace_receiver_checks_both_windows(self):
        _,_,store=self.joined()
        with store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='inProgress'")
        with self.assertRaisesRegex(PairError,'active or unresolved'):
            pair.receiver_proof(store,str(self.project))

    def test_communication_requires_both_model_and_business_receipts(self):
        _,_,store=self.joined()
        question=store.status()['messages'][0];a=store.member('A');b=store.member('B')
        claim=store.claim('A',a['session_uuid'],a['cwd'],message_id=question['id'])
        reply=store.send('A','B','已确认窗口连接',kind='reply',reply_to=question['id'])
        store.complete('A',a['session_uuid'],a['cwd'],question['id'],claim['claim_token'],'已答复连接验证')
        claim=store.claim('B',b['session_uuid'],b['cwd'],message_id=reply['id'])
        store.complete('B',b['session_uuid'],b['cwd'],reply['id'],claim['claim_token'],'已收到 A 回复')
        self.assertEqual(store.status()['window_flow']['communication'],'awaiting_handshake')
        with store.transaction() as db:db.execute("UPDATE messages SET remote_status='completed',delivery_status='accepted'")
        self.assertEqual(store.status()['window_flow']['communication'],'awaiting_handshake')
        self.confirm_context(store)
        self.assertEqual(store.status()['window_flow']['communication'],'verified')

    def test_envelope_separates_actor_directory_and_project(self):
        _,_,store=self.joined();a=store.member('A')
        envelope=json.loads(store.envelope(a,store.message(store.status()['messages'][0]['id'])))
        self.assertEqual(envelope['actor_cwd'],self.windows['A']['cwd'])
        self.assertEqual(envelope['workspace'],canonical_path(self.project))
        argv=envelope['claim_argv']
        self.assertEqual(argv[argv.index('--worktree')+1],self.windows['A']['cwd'])

    def test_completed_handshake_recovers_after_original_turn_interrupted(self):
        _,_,store=self.joined();a=store.member('A');b=store.member('B')
        question=store.status()['messages'][0]
        owned=store.claim('A',a['session_uuid'],a['cwd'],message_id=question['id'])
        reply=store.send('A','B','确认连接',kind='reply',reply_to=question['id'])
        store.complete('A',a['session_uuid'],a['cwd'],question['id'],owned['claim_token'],'已回复')
        owned=store.claim('B',b['session_uuid'],b['cwd'],message_id=reply['id'])
        store.complete('B',b['session_uuid'],b['cwd'],reply['id'],owned['claim_token'],'后续回合完成回复')
        with store.transaction() as db:
            db.execute("UPDATE messages SET remote_status='completed',delivery_status='accepted' WHERE id=?",(question['id'],))
            db.execute("UPDATE messages SET remote_status='interrupted',delivery_status='accepted',turn_id='old' WHERE id=?",(reply['id'],))
        self.assertEqual(store.status()['window_flow']['communication'],'awaiting_handshake')
        fake=FakeTransport(store)
        fake.observation={'turn_id':'old','remote_status':'interrupted','completion_source':'rollout_superseded','evidence_turn_id':'resumed'}
        Daemon(store,fake).reconcile()
        self.confirm_context(store)
        self.assertEqual(store.status()['window_flow']['communication'],'verified')
        self.assertEqual(store.message(reply['id'])['attempts'],0)

    def test_ready_notification_waits_for_real_handshake_terminal_states(self):
        _,_,store=self.joined();a=store.member('A');b=store.member('B')
        question=store.status()['messages'][0]
        claim=store.claim('A',a['session_uuid'],a['cwd'],message_id=question['id'])
        reply=store.send('A','B','确认连接',kind='reply',reply_to=question['id'])
        store.complete('A',a['session_uuid'],a['cwd'],question['id'],claim['claim_token'],'已回复')
        claim=store.claim('B',b['session_uuid'],b['cwd'],message_id=reply['id'])
        store.complete('B',b['session_uuid'],b['cwd'],reply['id'],claim['claim_token'],'已确认')
        with store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='inProgress' WHERE kind IN ('question','reply')")
        daemon=Daemon(store,FakeTransport(store))
        with self.assertRaisesRegex(PairError,'Communication unverified'):self.confirm_context(store)
        self.assertFalse(daemon.dispatch_one())
        with store.transaction() as db:
            db.execute("UPDATE messages SET remote_status='completed' WHERE kind IN ('question','reply')")
        self.confirm_context(store)
        self.assertTrue(daemon.dispatch_one())
        ready=next(m for m in store.status()['messages'] if m['kind']=='note')
        self.assertEqual(ready['recipient'],'A')

    def test_manual_receipts_never_mark_automatic_window_communication_verified(self):
        started=self.run_cli('A','start','--project',str(self.project),'--goal','人工协作','--delivery-mode','manual')
        accepted=self.run_cli('B','accept','--invite',started['invite_path'])
        store=Store(started['pair'],state_root=started['state_root'])
        self.assertIn('手动交接',accepted['after_join'])
        question=store.status()['messages'][0];a=store.member('A');b=store.member('B')
        claim=store.claim('A',a['session_uuid'],a['cwd'],message_id=question['id'])
        reply=store.send('A','B','人工确认',kind='reply',reply_to=question['id'])
        store.complete('A',a['session_uuid'],a['cwd'],question['id'],claim['claim_token'],'人工答复')
        claim=store.claim('B',b['session_uuid'],b['cwd'],message_id=reply['id'])
        store.complete('B',b['session_uuid'],b['cwd'],reply['id'],claim['claim_token'],'人工完成')
        self.confirm_context(store)
        self.assertEqual(store.status()['window_flow']['communication'],'manual')
        self.assertEqual(len(store.status()['messages']),2)

    def test_existing_distinct_worktrees_are_reused_and_can_have_parallel_tasks(self):
        trees=pair_git.create_worktrees(str(self.project),'existing','HEAD')
        for m in ('A','B'):
            self.windows[m]['cwd']=canonical_path(trees[m.lower()]['path'])
            self.windows[m]['proof']['cwd']=self.windows[m]['cwd']
        _,_,store=self.joined()
        self.assertNotEqual(member_workspace(store.member('A')),member_workspace(store.member('B')))
        self.finish_handshake(store)
        store.assign('a','独立任务 A','A',['a.py'],required_checks=[{'name':'business','command':'python -B -m unittest'}])
        store.assign('b','独立任务 B','B',['b.py'],required_checks=[{'name':'business','command':'python -B -m unittest'}])
        self.assertEqual([t['status'] for t in store.status()['tasks']],['active','active'])

    def test_schema_three_upgrade_keeps_history_and_allows_same_directory(self):
        home=Path(self.windows['A']['codex_home'])
        directory=home/'codex-pair'/'version-three';directory.mkdir(parents=True)
        stub=Store('version-three',home=home,create=True)
        with closing(sqlite3.connect(directory/'state.sqlite3')) as db:
            db.executescript(SCHEMA);db.executescript(MIGRATION_2);stub._upgrade_accounts(db)
            db.execute('INSERT INTO collaborations(id,project,base_commit,status,created) VALUES(?,?,?,?,?)',
                ('version-three',canonical_path(self.project),'a'*40,'paused',0))
            a=self.windows['A']
            db.execute('INSERT INTO members VALUES(?,?,?,?,?,?,?,?,?)',
                ('A',a['session_uuid'],a['cwd'],'queue',0,json.dumps(a['proof']),0,a['codex_home'],'auto'))
            db.execute("INSERT INTO events(kind,data,created) VALUES('old_history','{}',0)");db.commit()
        store=Store('version-three',home=home)
        b=self.windows['B'];proof={'id':b['session_uuid'],'cwd':a['cwd']}
        store.join('B',b['session_uuid'],a['cwd'],backend='queue',owned=False,proof=proof,member_codex_home=home)
        with store.transaction() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],14)
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='old_history'").fetchone()[0],1)
        self.assertEqual(len(list(directory.glob('schema-v3-*.backup.sqlite3'))),1)

    def test_bridge_survives_launcher_exit_and_is_idempotent(self):
        started=self.start();script=str(Path(pair.__file__).resolve())
        common=[sys.executable,script,'--pair',started['pair'],'--state-root',started['state_root'],'--json']
        def invoke(*args):
            result=subprocess.run(common+list(args),capture_output=True,encoding='utf-8',timeout=15)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            return json.loads(result.stdout)['result']
        store=Store(started['pair'],state_root=started['state_root'])
        process_handle=None
        try:
            first=invoke('bridge')
            if os.name=='nt':
                import ctypes
                kernel=ctypes.WinDLL('kernel32',use_last_error=True)
                kernel.OpenProcess.restype=ctypes.c_void_p
                kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
                kernel.CloseHandle.argtypes=[ctypes.c_void_p]
                process_handle=kernel.OpenProcess(0x00100000,False,first['pid'])
                self.assertTrue(process_handle,ctypes.get_last_error())
            second=invoke('bridge')
            self.assertEqual(first['pid'],second['pid']);self.assertTrue(second['reused'])
            self.assertTrue(store.status(probe_timeout=1)['runtime']['probe_confirmed'])
        finally:
            invoke('bridge','--action','stop')
            deadline=time.monotonic()+10
            while store.status()['runtime']['lock_held'] and time.monotonic()<deadline:time.sleep(.05)
            self.assertFalse(store.status()['runtime']['lock_held'])
            if process_handle:
                try:
                    # 锁释放早于进程退出；等日志句柄关闭后才能删除测试目录。
                    self.assertEqual(kernel.WaitForSingleObject(process_handle,10000),0)
                finally:kernel.CloseHandle(process_handle)


if __name__=='__main__':unittest.main()
