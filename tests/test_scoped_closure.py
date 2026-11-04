"""范围收尾使用隔离状态库；历史审核结论仅作为外部输入，不调用真实模型。"""
import copy
import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from scripts import pair_commands
from scripts.pair_core import Daemon, PairError
from tests import test_core, test_handoff_balance, test_reviewer
from tests.compatibility_fixture import register_cli_fixture
from tests.recovery_fixture import resume_for_test


class ScopedClosureTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    finish = test_handoff_balance.HandoffTests.finish
    close_result = test_handoff_balance.HandoffTests.close_result
    acknowledge = test_handoff_balance.HandoffTests.acknowledge

    def prepare(self):
        self.store.assign('done', '本轮已完成验收', 'B', ['done.md'], task_kind='coordination')
        self.finish('done')
        self.close_result('done')
        for key, status in [('old-cancelled', 'cancelled'), ('old-returned', 'handed_back'), ('old-review', 'review_wait')]:
            self.store.assign(key, '旧矩阵历史', 'B', [key + '/'])
            with self.store.transaction() as db:
                db.execute('UPDATE tasks SET status=? WHERE id=?', (status, key))
                if status == 'review_wait':
                    db.execute('UPDATE tasks SET review_required=1 WHERE id=?', (key,))
                db.execute("UPDATE messages SET process_status='completed',delivery_status='accepted',remote_status='completed' WHERE task_id=?", (key,))
        self.store.setting('window_flow', {'goal': '旧G01–G16完整目标'})
        self.store.setting('review_required', True)
        self.store.setting('publish_enabled', False)
        self.store.schedule()
        self.plan = next(t for t in self.store.status()['tasks'] if t['task_kind'] == 'planning')
        self.message = next(m for m in self.store.status()['messages'] if m['task_id'] == self.plan['id'])
        claim = self.store.claim('A', self.sessions['A'], self.directories['A'], message_id=self.message['id'])
        self.token = claim['claim_token']
        with self.store.transaction() as db:
            delivery = self.store._readable_delivery(db, 'A', 'delivery:done')
        self.completed = {'done': {'delivery': 'delivery:done', 'digest': delivery['digest']}}
        self.excluded = {key: '用户本轮只验收正常使用，旧矩阵保持原状态' for key in ('old-cancelled', 'old-returned', 'old-review')}

    def close(self, **changes):
        args = dict(scope='正常使用与R24/R27已审运行包', user_instruction='用户明确要求先结束本轮正常使用验收，旧矩阵另行保留',
                    completed=self.completed, excluded=self.excluded, message_id=self.message['id'], token=self.token)
        args.update(changes)
        return self.store.close_scope('A', self.sessions['A'], self.directories['A'], **args)

    def snapshot(self):
        with self.store.transaction() as db:
            result = {table: [dict(r) for r in db.execute('SELECT * FROM ' + table)]
                      for table in ('tasks', 'review_stages', 'members', 'collaborations', 'invitations')}
            result['settings'] = [dict(r) for r in db.execute("SELECT * FROM pair_settings WHERE key<>'closed_scope'")]
            return result

    def test_scoped_close_preserves_history_and_strict_goal_semantics(self):
        self.prepare()
        with self.assertRaisesRegex(PairError, 'Work remains'):
            self.store.complete('A', self.sessions['A'], self.directories['A'], self.message['id'], self.token, '全目标不能误报通过', goal_complete=True)
        before = self.snapshot()
        result = self.close()
        self.assertTrue(result['closed'])
        self.assertFalse(result['goal_completed'])
        self.assertEqual(before, self.snapshot())
        self.assertTrue(self.close()['duplicate'])
        self.store.complete('A', self.sessions['A'], self.directories['A'], self.message['id'], self.token, '只结束明确范围，旧矩阵未通过')
        for _ in range(3):
            self.store.schedule()
        flow = self.store.status()['window_flow']
        self.assertFalse(flow['goal_completed'])
        self.assertEqual(flow['closed_scope']['excluded'], self.excluded)
        self.assertEqual(len([t for t in self.store.status()['tasks'] if t['task_kind'] == 'planning']), 1)
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='scope_closed'").fetchone()[0], 1)

    def test_requires_original_a_claim_and_coordination_task(self):
        self.prepare()
        with self.assertRaises(PairError):
            self.store.close_scope('B', self.sessions['B'], self.directories['B'], '本轮', '用户范围', self.completed, self.excluded,
                                   message_id=self.message['id'], token=self.token)
        with self.assertRaises(PairError):
            self.close(token='wrong')
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET task_kind='implementation' WHERE id=?", (self.plan['id'],))
        with self.assertRaisesRegex(PairError, 'Coordination claim'):
            self.close()

    def test_explicit_partition_and_user_basis_required(self):
        self.prepare()
        for changes in ({'user_instruction': ''}, {'excluded': {}}, {'excluded': dict(self.excluded, done='排除重叠')},
                        {'excluded': dict(self.excluded, **{'old-review': ''})}):
            with self.subTest(changes=changes), self.assertRaises(PairError):
                self.close(**changes)
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='pending' WHERE id='old-review'")
        with self.assertRaisesRegex(PairError, 'Work remains'):
            self.close()

    def test_exact_digest_full_read_and_review_required(self):
        self.prepare()
        wrong = copy.deepcopy(self.completed)
        wrong['done']['digest'] = '0' * 64
        with self.assertRaisesRegex(PairError, 'digest mismatch'):
            self.close(completed=wrong)
        with self.store.transaction() as db:
            db.execute("DELETE FROM record_acks WHERE member='A'")
        with self.assertRaisesRegex(PairError, 'acknowledgement'):
            self.close()
        self.acknowledge('A', 'delivery:done')
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET review_required=1,task_kind='verification' WHERE id='done'")
        with self.assertRaisesRegex(PairError, 'Review remains'):
            self.close()

    def test_current_passed_stage_required_and_stale_stage_rejected(self):
        self.prepare()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET task_kind='verification' WHERE id='done'")
        stage = self.store.submit_stage('B', self.sessions['B'], self.directories['B'], 'done', {'kind': 'commit', 'head': 'b' * 40},
                                       [{'member': 'B', 'provider': 'openai', 'model': 'test-fixture'}], [],
                                       {'objective': '测试已审范围', 'acceptance': [], 'environment': {}, 'lockfiles': [], 'baseline': 'a' * 40})
        # 隔离夹具导入外部审核结论；范围收尾本身没有授予审核通过的能力。
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='completed' WHERE id='done'")
        with self.assertRaisesRegex(PairError, 'Review remains'):
            self.close()
        with self.store.transaction() as db:
            db.execute("UPDATE review_stages SET status='running' WHERE id=?", (stage['id'],))
        with self.assertRaisesRegex(PairError, 'Execution unresolved'):
            self.close()
        with self.store.transaction() as db:
            db.execute("UPDATE review_stages SET status='passed' WHERE id=?", (stage['id'],))
        with self.assertRaisesRegex(PairError, 'Stale scope delivery'):
            self.close()
        delivery_id = 'stage:' + stage['id']
        self.acknowledge('A', delivery_id)
        with self.store.transaction() as db:
            digest = self.store._readable_delivery(db, 'A', delivery_id)['digest']
        self.completed = {'done': {'delivery': delivery_id, 'digest': digest}}
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET requirements_version=requirements_version+1 WHERE id='done'")
        with self.assertRaisesRegex(PairError, 'Review remains'):
            self.close()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET requirements_version=requirements_version-1 WHERE id='done'")
        self.assertTrue(self.close()['closed'])

    def test_unresolved_execution_in_excluded_history_still_blocks(self):
        self.prepare()
        for state in ('unknown', 'delivering', 'accepted'):
            with self.subTest(state=state):
                with self.store.transaction() as db:
                    db.execute("UPDATE messages SET delivery_status=?,remote_status='inProgress' WHERE task_id='old-review'", (state,))
                with self.assertRaisesRegex(PairError, 'Execution unresolved'):
                    self.close()

    def test_unfinished_in_scope_receipt_blocks(self):
        self.prepare()
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET process_status='pending' WHERE task_id='done' AND kind='result'")
        with self.assertRaisesRegex(PairError, 'Receipt incomplete'):
            self.close()

    def test_pending_or_granted_approval_still_blocks(self):
        self.prepare()
        self.store.setting('approval_assistance', True)
        request = self.store.capture_approval('B', {'hook_event_name': 'PermissionRequest', 'session_id': self.sessions['B'],
                'turn_id': 'isolated', 'cwd': self.directories['B'], 'tool_name': 'Bash',
                'tool_input': {'command': 'python isolated_check.py'}, 'permission_mode': 'default'})
        for state in ('pending', 'allowed', 'granted', 'unknown'):
            with self.subTest(state=state):
                with self.store.transaction() as db:
                    db.execute('UPDATE approval_requests SET status=? WHERE id=?', (state, request['id']))
                with self.assertRaisesRegex(PairError, 'Execution unresolved'):
                    self.close()

    def test_business_contract_cannot_be_used_as_closure_claim(self):
        self.prepare()
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET required_checks=? WHERE id=?', ('["business"]', self.plan['id']))
        with self.assertRaisesRegex(PairError, 'report-only'):
            self.close()

    def test_public_command_reads_json_and_uses_actor_validation(self):
        self.prepare()
        payload = {'scope': '本轮', 'user_instruction': '用户明确要求只结束本轮验收', 'completed': self.completed,
                   'excluded': self.excluded, 'message': self.message['id'], 'token': self.token}
        path = self.root / 'closure.json'
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
        args = SimpleNamespace(command='close-scope', member='A', session=self.sessions['A'], worktree=self.directories['A'],
                               input_stdin=False, input_file=str(path))
        validated = []
        def validate(store, actor, **kwargs):
            self.assertIs(store, self.store)
            self.assertIs(actor, args)
            validated.append(True)
        self.assertIn('close-scope', pair_commands.COMMANDS)
        self.assertTrue(pair_commands.execute(self.store, args, SimpleNamespace(validate_actor=validate))['closed'])
        self.assertEqual(validated, [True])

    def test_new_task_review_and_cli_binding_keep_closed_history_blocked(self):
        self.prepare()
        self.close()
        helper=test_reviewer.ReviewerTests();helper.__dict__.update(self.__dict__)
        ticket,proof=helper.ready()
        register_cli_fixture(self.store)
        self.store.assign('new-review','用户明确的新业务','B',['new.py'],
                          required_checks=[{'name':'business','command':'python -B -m unittest'}])
        self.finish('new-review')
        stage=self.store.submit_stage('B',self.sessions['B'],self.directories['B'],'new-review',
            {'kind':'commit','head':'b'*40},[{'member':'B','provider':'openai','model':'gpt-test'}],
            [{'name':'business','required':True,'status':'not_run'}],
            {'objective':'新任务审核','acceptance':[],'environment':{},'lockfiles':[],'baseline':'a'*40})
        self.assertEqual(self.store.next_review(ticket['epoch'],proof['id'],proof)['id'],stage['id'])
        lease=self.store.acquire_reviewer(ticket['epoch'],proof['id'],proof)
        self.store.begin_review(stage['id'],ticket['epoch'],proof['id'],proof,lease['owner'],lease['execution_id'])
        actor=('A',self.sessions['A'],self.directories['A'])
        bound=self.store.bind_cli_execution(*actor,lease['execution_id'],stage['digest'],
                                           operation='review_settlement',task_id='new-review')
        self.assertEqual(bound['task_id'],'new-review')
        with self.assertRaisesRegex(PairError,'Recovery control gate'):
            self.store.bind_cli_execution(*actor,'old-execution','c'*64,operation='new_delivery',task_id='old-review')
        with self.assertRaisesRegex(PairError,'cannot be rebound'):
            self.store.bind_cli_execution(*actor,lease['execution_id'],stage['digest'],
                                          operation='review_settlement',task_id='old-review')
        self.store.setting('approval_assistance',True)
        request=self.store.capture_managed_approval(ticket['epoch'],proof['id'],lease['execution_id'],
            {'id':'new-permission','method':'item/commandExecution/requestApproval',
             'params':{'threadId':proof['id'],'turnId':'new-turn','itemId':'new-command',
                       'command':'python -B -m unittest','cwd':proof['cwd']}})
        self.assertEqual(self.store.managed_approval_result(request['id'])['status'],'pending')
        with self.store.transaction() as db:
            db.execute("UPDATE review_stages SET task_id='old-review' WHERE id=?",(stage['id'],))
        self.assertEqual(self.store.managed_approval_result(request['id'])['status'],'expired')

    def test_recovery_application_scope_comes_from_persisted_source_task(self):
        from scripts.pair_runtime_apply import application_gate
        from tests.test_recovery_v14 import EvidenceFixture
        self.prepare()
        self.close()
        actor=('A',self.sessions['A'],self.directories['A'])
        self.store.complete(*actor,self.message['id'],self.token,'原范围已安全收尾')
        register_cli_fixture(self.store)
        self.store.assign('new-runtime','新范围的运行修复','B',['runtime/'],
                          required_checks=[{'name':'business','command':'python -B -m unittest'}])
        self.finish('new-runtime')
        self.close_result('new-runtime')
        stage=self.store.submit_stage('B',self.sessions['B'],self.directories['B'],'new-runtime',
            {'kind':'commit','head':'b'*40},[{'member':'B','provider':'openai','model':'gpt-test'}],[],
            {'objective':'运行修复','acceptance':[],'environment':{},'lockfiles':[],'baseline':'a'*40,
             'candidate_manifest_digest':'b'*64,'runtime_verification_contract':'c'*64})
        policy=self.store.recovery_policy(*actor,'patch_application',enabled=True,
            basis='用户明确授权新任务的已审运行修复',expected_version=1)
        plan={'old_runtime_digest':'a'*64,'new_runtime_digest':'b'*64,'state_schema':14,
              'backup_ref':'fixture:backup','verification_contract':'c'*64,'allowed_paths':['runtime']}
        proposal={'candidate_manifest_digest':'b'*64,'allowed_paths':['runtime']}
        now=time.time()
        # 正式回执和备份完整性仅替代外部证据边界；应用状态机及范围检查使用真实实现。
        with self.store.transaction() as db:
            db.execute("UPDATE review_stages SET status='passed',evidence='{}' WHERE id=?",(stage['id'],))
            db.execute("UPDATE tasks SET status='completed' WHERE id='new-runtime'")
            db.execute('''INSERT INTO recovery_requests(id,source_task,stable_blocker,proposal,proposal_digest,
                idempotency_key,status,reviewer,reviewer_session,binding_version,report,created,updated)
                VALUES(?,?,?,?,?,?,'review_passed','A',?,?,?,?,?)''',
                ('scope-runtime','new-runtime','fixture:blocker',json.dumps(proposal),'a'*64,'fixture:runtime',
                 self.sessions['A'],1,json.dumps({'formal_stage':stage['id']}),now,now))
        self.store._recovery_evidence_adapter=EvidenceFixture(exact_reviewed_candidate=True,compatible_migration=True,
            backup_integrity=True,exclusive_writer_available=True)
        with patch.object(self.store,'_formal_passed_stage',return_value={'external_fixture':True}):
            application=self.store.prepare_recovery_application(*actor,'scope-runtime',stage['id'],plan,idempotency_key='apply-new')
            self.assertEqual(self.store.begin_recovery_application(*actor,application['id'])['state'],'prepared')
            with self.store.transaction() as db:
                application_gate(self.store,db,application['id'])
                db.execute("UPDATE recovery_requests SET source_task='old-review' WHERE id='scope-runtime'")
            with self.store.transaction() as db,self.assertRaisesRegex(PairError,'Runtime application paused'):
                application_gate(self.store,db,application['id'])
            with self.store.transaction() as db:
                db.execute("UPDATE recovery_applications SET state='waiting_terminal' WHERE id=?",(application['id'],))
            with self.assertRaisesRegex(PairError,'SCOPE_CLOSED'):
                self.store.begin_recovery_application(*actor,application['id'])
            with self.assertRaisesRegex(PairError,'SCOPE_CLOSED'):
                self.store.prepare_recovery_application(*actor,'scope-runtime',stage['id'],plan,idempotency_key='apply-old')

    def test_new_task_resumes_only_by_its_exact_scope(self):
        self.prepare()
        self.close()
        closed=self.store.setting('closed_scope')
        self.store.assign('new-work','明确继续的新业务','A',['new/'],
                          required_checks=[{'name':'business','command':'python -B -m unittest'}])
        actor=('A',self.sessions['A'],self.directories['A'])
        self.store.request_pause(*actor,reason='用户暂停新任务',idempotency_key='pause-new',scope={'kind':'task','id':'new-work'})
        with self.store.transaction() as db:
            self.assertTrue(self.store._recovery_gate(db,'A','new-work'))
        for scope in (None,{'kind':'task','id':'old-review'},{'kind':'task','id':'unknown'}):
            with self.subTest(scope=scope),self.assertRaisesRegex(PairError,'Explicit new scope'):
                self.store.request_resume(*actor,instruction='继续',idempotency_key='invalid-resume',scope=scope)
        resumed=resume_for_test(self.store,scope={'kind':'task','id':'new-work'})
        self.assertEqual(resumed['state'],'reconciled')
        with self.store.transaction() as db:
            self.assertFalse(self.store._recovery_gate(db,'A','new-work'))
            self.assertTrue(self.store._recovery_gate(db,'B','old-review'))
        self.assertEqual(self.store.setting('closed_scope'),closed)

    def test_new_scope_does_not_inherit_auxiliary_or_quota_consent(self):
        self.prepare()
        actor=('A',self.sessions['A'],self.directories['A'])
        self.store.auxiliary_policy(*actor,enabled=True,reason='原范围辅助授权')
        quota={'reliability':'fresh','observed_at':time.time(),'remaining':5,'pool':'fixture-a','limit_id':'codex',
               'windows':[{'duration_mins':300,'resets_at':time.time()+600}]}
        self.store.save_quota('A',quota)
        with self.store.transaction() as db:
            self.store._record_quota_user_event(db,'A','忽略额度',event_id='before-close',source_identity='fixture',
                session=self.sessions['A'],binding_version=self.store._member(db,'A')['binding_version'])
        self.close()
        self.store.assign('new-authorized','新增工作','B',['new-authorized/'],
                          required_checks=[{'name':'business','command':'python -B -m unittest'}])
        with self.store.transaction() as db:
            self.assertFalse(self.store._soft_quota_ignored(db,'A'))
            with self.assertRaisesRegex(PairError,'scope authorization'):
                self.store._auxiliary_dispatchable(db,task_id='new-authorized')
        self.store.auxiliary_policy(*actor,enabled=True,reason='用户明确授权新范围辅助工作')
        with self.store.transaction() as db:
            self.store._record_quota_user_event(db,'A','忽略额度',event_id='after-close',source_identity='fixture',
                session=self.sessions['A'],binding_version=self.store._member(db,'A')['binding_version'])
            self.assertTrue(self.store._soft_quota_ignored(db,'A'))
            self.store._auxiliary_dispatchable(db,task_id='new-authorized')
            with self.assertRaisesRegex(PairError,'Scope closed'):
                self.store._auxiliary_dispatchable(db,task_id='old-review')

    def test_legacy_scope_allows_only_tasks_proven_registered_after_close(self):
        self.prepare()
        self.close()
        legacy=dict(self.store.setting('closed_scope'))
        legacy.pop('task_ids')
        self.store.setting('closed_scope',legacy)
        with patch('scripts.pair_core.time.time',return_value=legacy['closed_at']+1):
            self.store.assign('later','明确登记的后续业务','A',['later/'],
                              required_checks=[{'name':'business','command':'python -B -m unittest'}])
        self.assertEqual(self.store.task('later')['status'],'active')
        legacy.pop('closed_at')
        self.store.setting('closed_scope',legacy)
        with self.store.transaction() as db:
            self.assertTrue(any(r['code']=='SCOPE_CLOSED' for r in self.store._recovery_gate(db,'A','later')))

    def test_closed_scope_does_not_dispatch_old_plans_or_release_unreviewed_dependencies(self):
        self.prepare()
        self.close()
        # 已登记的自动规划保持历史状态，不再投递，也不占新显式业务的调度位置。
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET process_status='pending' WHERE id=?", (self.message['id'],))
        fake = test_core.FakeTransport(self.store)
        daemon = Daemon(self.store, fake)
        self.assertFalse(daemon.dispatch_one())
        self.store.assign('explicit', '用户后续明确业务', 'A', ['explicit/'],
                          required_checks=[{'name': 'business', 'command': 'python -B -m unittest'}])
        self.assertEqual(self.store.task('explicit')['status'], 'active')
        with self.store.transaction() as db:
            self.assertFalse(any(r['code']=='SCOPE_CLOSED' for r in self.store._recovery_gate(db,'A','explicit')))
            self.assertTrue(any(r['code']=='SCOPE_CLOSED' for r in self.store._recovery_gate(db,'B','old-review')))
            self.assertTrue(any(r['code']=='SCOPE_CLOSED' for r in self.store._recovery_gate(db,'A',self.plan['id'])))
        self.store.assign('blocked', '依赖未审历史', 'B', ['blocked/'], dependencies=['old-review'],
                          required_checks=[{'name': 'business', 'command': 'python -B -m unittest'}])
        self.assertEqual(self.store.task('blocked')['status'], 'pending')
        self.assertFalse(self.store.status()['window_flow']['goal_completed'])
        self.assertEqual(self.store.task('old-review')['status'], 'review_wait')
        with self.assertRaises(PairError):
            self.store.require_reviewed('old-review')
        self.assertFalse(self.store.setting('publish_enabled'))


if __name__ == '__main__':
    unittest.main()
