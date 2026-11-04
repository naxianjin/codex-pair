"""恢复记录的隔离行为用例；本轮未执行，不构成真实窗口或 Hyper-V 验收。"""
import hashlib
import json
import time
import unittest

from scripts.pair_core import PairError, Store
from scripts.pair_diagnostics import snapshot_from_db
from scripts.pair_recovery import DOMAINS, VerifiedRecoveryEvidence, candidate_manifest, digest
from tests import test_core


class EvidenceFixture:
    """隔离测试中的宿主/官方回执边界；不模拟恢复状态机。"""
    def __init__(self, **facts):
        self.facts = facts

    def verify(self, kind, context):
        return VerifiedRecoveryEvidence(kind, digest(context), ('fixture:external-receipt',), self.facts)


class RecoveryV14Tests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown

    def actor(self, member='A'):
        return member, self.sessions[member], self.directories[member]

    def enable(self, domain):
        with self.store.transaction() as db:
            version = db.execute('SELECT version FROM recovery_policies WHERE domain=?', (domain,)).fetchone()[0]
        return self.store.recovery_policy(*self.actor(), domain, enabled=True,
                                          basis='本临时协作的明确测试授权', expected_version=version)

    def fixture(self):
        self.enable('review_recovery')
        for key in ('old-one', 'old-two', 'repair'):
            self.store.assign(key, key, 'B', [key + '.py'], task_kind='repair' if key == 'repair' else 'implementation')
        with self.store.transaction() as db:
            for member in ('A', 'B'):
                proof = {'id': self.sessions[member], 'cwd': self.directories[member], 'modelProvider': 'openai', 'model': 'gpt-test'}
                db.execute('UPDATE members SET proof=?,verified=1 WHERE id=?', (json.dumps(proof), member))
            db.execute("UPDATE messages SET process_status='completed',delivery_status='accepted',remote_status='completed'")
            db.execute("UPDATE tasks SET status='needs_validation' WHERE id IN ('old-one','old-two')")
            db.execute("UPDATE tasks SET status='pending' WHERE id='repair'")
            for index, key in enumerate(('old-one', 'old-two')):
                db.execute('''INSERT INTO review_stages(id,task_id,version,requirements_version,artifact,digest,contributors,
                              checks,payload,status,created,updated) VALUES(?,?,1,1,?,?,?,'[]','{}','needs_validation',?,?)''',
                           ('stage-' + key, key, '{"kind":"snapshot","manifest":{}}', str(index + 1) * 64,
                            '[{"member":"B","provider":"openai","model":"gpt-test"}]', time.time(), time.time()))
            db.execute('''INSERT INTO review_issues(id,root_task,author,body,status,rounds,stage_id,repair_task,updated)
                          VALUES('issue-one','old-one','B','契约缺少恢复入口','open',1,'stage-old-one','repair',?)''', (time.time(),))
            db.execute("INSERT INTO repair_links VALUES('repair','issue-one',1,'stage-old-one')")
        directory = self.project / 'candidate'; directory.mkdir()
        path = directory / 'patch.py'; path.write_text('answer = 42\n', encoding='utf-8')
        manifest = [{'path': 'patch.py', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]
        with self.store.transaction() as db:
            snapshot = snapshot_from_db(db, self.store.pair_id, task_id='repair')
        proposal = {'source_task': 'repair', 'stable_blocker': 'issues:' + digest(['issue-one'])[:24],
                    'blocker_snapshot_digest': snapshot['state_digest'], 'candidate_root': str(directory),
                    'manifest': manifest, 'allowed_paths': ['patch.py'], 'source_manifest_digest': 'a' * 64,
                    'purpose': '复核准确恢复候选', 'check_contract_digest': 'b' * 64,
                    'reviewer': 'A', 'required_deliveries': [], 'remediates': ['issue-one']}
        return proposal

    def queued(self):
        proposal = self.fixture()
        request = self.store.propose_review_recovery(*self.actor(), proposal, idempotency_key='request-one')
        self.store.authorize_review_recovery(*self.actor(), request['id'], request['digest'], basis='仅允许本次隔离恢复审核')
        offset = 0
        while True:
            page = self.store.read_review_recovery(*self.actor(), request['id'], offset=offset)
            if page['next_offset'] is None:
                break
            offset = page['next_offset']
        return request, proposal

    def running(self):
        request, proposal = self.queued()
        claimed = self.store.claim_review_recovery(*self.actor(), request['id'])
        self.store._recovery_evidence_adapter = EvidenceFixture(explicit_admission_cycle=True, isolation_verified=True,
            executor_gates_verified=True, resources_available=True, identity_current=True)
        running = self.store.begin_review_recovery(*self.actor(), request['id'], claimed['token'])
        return request, proposal, claimed, running

    def test_four_domains_default_off_and_independent(self):
        with self.store.transaction() as db:
            before = [dict(row) for row in db.execute('SELECT * FROM recovery_policies ORDER BY domain')]
        self.assertEqual({row['domain'] for row in before}, set(DOMAINS))
        self.assertFalse(any(row['enabled'] for row in before))
        self.enable('transport')
        with self.store.transaction() as db:
            after = [dict(row) for row in db.execute('SELECT * FROM recovery_policies ORDER BY domain')]
        self.assertEqual([row for row in before if row['domain'] != 'transport'], [row for row in after if row['domain'] != 'transport'])

    def test_policy_version_competition_preserves_first_result(self):
        self.enable('transport')
        with self.assertRaisesRegex(PairError, 'Policy version changed'):
            self.store.recovery_policy(*self.actor(), 'transport', enabled=False, basis='旧版本操作', expected_version=1)
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT enabled,version FROM recovery_policies WHERE domain='transport'").fetchone()[:], (1, 2))

    def test_pause_preserves_member_task_and_resources(self):
        self.store.assign('work', 'work', 'B', ['src'], resources=['shared-database'])
        result = self.store.request_pause(*self.actor('B'), reason='用户暂停', idempotency_key='pause-one')
        self.assertEqual(result['state'], 'pausing')
        self.assertFalse(result['all_executors_stopped'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT status FROM tasks WHERE id='work'").fetchone()[0], 'active')
            self.assertEqual(db.execute('SELECT count(*) FROM resource_locks').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT lifecycle FROM members WHERE id='B'").fetchone()[0], 'active')
            self.assertTrue(self.store._recovery_gate(db, member='B', task_id='work'))
            self.assertEqual(self.store._recovery_gate(db, action='reconcile'), [])

    def test_resume_from_running_persists_gate_and_is_cross_member_idempotent(self):
        first = self.store.request_resume(*self.actor('B'), instruction='继续本协作', idempotency_key='resume-one')
        second = self.store.request_resume(*self.actor(), instruction='继续本协作', idempotency_key='resume-two')
        self.assertEqual(first['id'], second['id'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT status FROM collaborations').fetchone()[0], 'paused')
            self.assertEqual(db.execute("SELECT count(*) FROM control_requests WHERE kind='resume'").fetchone()[0], 1)
            self.assertTrue(self.store._recovery_gate(db))

    def test_new_pause_prevents_old_resume_reconciliation(self):
        self.store.assign('work', 'work', 'B', ['src'])
        resume = self.store.request_resume(*self.actor(), instruction='继续', idempotency_key='resume')
        self.store.request_pause(*self.actor('B'), reason='再次暂停', idempotency_key='new-pause')
        with self.assertRaisesRegex(PairError, 'New control event'):
            self.store.reconcile_resume(*self.actor(), resume['id'], tasks=['work'])

    def test_stopped_and_closed_scope_do_not_resume(self):
        self.store.control('stopped')
        with self.assertRaisesRegex(PairError, 'Explicit new scope'):
            self.store.request_resume(*self.actor(), instruction='继续', idempotency_key='stopped-resume')
        with self.store.transaction() as db:
            db.execute("UPDATE collaborations SET status='running'")
            self.store._put(db, 'closed_scope', {'scope': 'delivered'})
        with self.assertRaisesRegex(PairError, 'Explicit new scope'):
            self.store.request_resume(*self.actor(), instruction='继续', idempotency_key='closed-resume')

    def test_resume_without_verified_executor_does_not_open_work(self):
        self.store.assign('work', 'work', 'B', ['src'])
        resume = self.store.request_resume(*self.actor(), instruction='继续', idempotency_key='resume')
        with self.assertRaisesRegex(PairError, 'trusted recovery adapter unavailable'):
            self.store.reconcile_resume(*self.actor(), resume['id'], tasks=['work'])
        with self.store.transaction() as db:
            self.assertTrue(self.store._recovery_gate(db, member='B', task_id='work'))

    def test_partial_resume_does_not_open_unchecked_task(self):
        for key in ('safe', 'unknown'):
            self.store.assign(key, key, 'B', [key])
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='completed',process_status='completed' WHERE task_id='safe'")
        resume = self.store.request_resume(*self.actor(), instruction='继续', idempotency_key='resume')
        self.store._recovery_evidence_adapter = EvidenceFixture(source_is_current_user_instruction=True, all_required_gates_verified=True)
        self.store.reconcile_resume(*self.actor(), resume['id'], tasks=['safe'])
        with self.store.transaction() as db:
            self.assertFalse(self.store._recovery_gate(db, member='B', task_id='safe'))
            self.assertTrue(self.store._recovery_gate(db, member='B', task_id='unknown'))

    def test_empty_pair_can_finish_full_resume_without_permanent_global_gate(self):
        self.store.request_pause(*self.actor(), reason='暂停', idempotency_key='pause')
        resume = self.store.request_resume(*self.actor(), instruction='明确继续本协作', idempotency_key='resume')
        self.store._recovery_evidence_adapter = EvidenceFixture(source_is_current_user_instruction=True,
            all_required_gates_verified=True, full_scope_reconciled=True)
        result = self.store.reconcile_resume(*self.actor(), resume['id'], tasks=[])
        self.assertEqual(result['state'], 'reconciled')
        with self.store.transaction() as db:
            self.assertFalse(self.store._recovery_gate(db, member='A', action='auxiliary'))
            self.assertFalse(self.store._recovery_gate(db))

    def test_empty_resume_without_full_scope_proof_does_not_open_gate(self):
        resume = self.store.request_resume(*self.actor(), instruction='继续', idempotency_key='resume')
        self.store._recovery_evidence_adapter = EvidenceFixture(source_is_current_user_instruction=True, all_required_gates_verified=True)
        with self.assertRaisesRegex(PairError, 'Full scope evidence required'):
            self.store.reconcile_resume(*self.actor(), resume['id'], tasks=[])

    def test_count_limits_survive_store_reopen_and_do_not_refund_unknown(self):
        for index in range(3):
            with self.store.transaction() as db:
                self.store._start_recovery_attempt(db, 'coordination', 'root-issue', str(index), {'plan': index})
                self.store._finish_recovery_attempt(db, 'coordination', 'root-issue', str(index), 'unknown', {'lookup': 'pending'})
        reopened = Store(self.store.pair_id, home=self.home)
        with self.assertRaisesRegex(PairError, 'Attempt budget exhausted'):
            with reopened.transaction() as db:
                reopened._start_recovery_attempt(db, 'coordination', 'root-issue', 'fourth', {'plan': 'new name'})
        with reopened.transaction() as db:
            duplicate = reopened._start_recovery_attempt(db, 'coordination', 'root-issue', '0', {'retry': True})
        self.assertFalse(duplicate['started'])

    def test_restart_budget_uses_persistent_window_and_rejects_backward_clock(self):
        for index, clock in enumerate((1000, 1005, 1020)):
            with self.store.transaction() as db:
                self.store._start_recovery_attempt(db, 'bridge_restart', 'bridge', str(index), {}, now=clock)
        with self.assertRaisesRegex(PairError, 'Attempt budget exhausted'):
            with self.store.transaction() as db:
                self.store._start_recovery_attempt(db, 'bridge_restart', 'bridge', 'again', {}, now=1030)
        with self.assertRaisesRegex(PairError, 'Clock moved backwards'):
            with self.store.transaction() as db:
                self.store._start_recovery_attempt(db, 'bridge_restart', 'bridge', 'backwards', {}, now=999)
        with self.store.transaction() as db:
            result = self.store._start_recovery_attempt(db, 'bridge_restart', 'bridge', 'later', {}, now=1621)
        self.assertTrue(result['started'])

    def test_counters_are_independent_domains(self):
        with self.store.transaction() as db:
            self.store._start_recovery_attempt(db, 'product_repair', 'issue', '1', {})
            self.store._start_recovery_attempt(db, 'approval_failure', 'issue', '1', {})
            self.store._start_recovery_attempt(db, 'coordination', 'issue', '1', {})
            self.assertEqual(db.execute('SELECT count(*) FROM recovery_attempts').fetchone()[0], 3)

    def test_missing_delivery_reminder_is_once_and_does_not_complete_task(self):
        self.store.assign('work', 'work', 'B', ['src'])
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET remote_status='completed',delivery_status='accepted',process_status='claimed' WHERE task_id='work'")
            self.store._recovery_tick(db)
            self.store._recovery_tick(db)
            self.assertEqual(db.execute("SELECT count(*) FROM recovery_outbox WHERE id LIKE 'missing-delivery:%'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT status FROM tasks WHERE id='work'").fetchone()[0], 'active')

    def test_paused_outbox_is_persisted_without_dispatch(self):
        self.store.request_pause(*self.actor(), reason='暂停', idempotency_key='pause')
        with self.store.transaction() as db:
            intent = self.store._record_continuation(db, logical_goal='goal', action='read_result', payload={'scope': 'current'},
                idempotency_key='next-action', recipient='A')
            self.store._recovery_tick(db)
            self.assertEqual(db.execute('SELECT state FROM recovery_outbox WHERE intent_id=?', (intent['id'],)).fetchone()[0], 'pending')
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0], 0)

    def test_continuation_transaction_rolls_back_with_business_failure(self):
        with self.assertRaisesRegex(RuntimeError, 'injected'):
            with self.store.transaction() as db:
                self.store._record_continuation(db, logical_goal='goal', action='read_result', payload={}, idempotency_key='atomic', recipient='A')
                raise RuntimeError('injected')
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM continuation_intents').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM recovery_outbox').fetchone()[0], 0)

    def test_manifest_change_and_parent_traversal_are_rejected(self):
        candidate = self.root / 'candidate'; candidate.mkdir()
        path = candidate / 'item.py'; path.write_text('before', encoding='utf-8')
        manifest = [{'path': 'item.py', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]
        candidate_manifest(str(candidate), manifest, ['item.py'])
        path.write_text('after', encoding='utf-8')
        with self.assertRaisesRegex(PairError, 'Candidate changed'):
            candidate_manifest(str(candidate), manifest, ['item.py'])
        with self.assertRaisesRegex(PairError, 'Invalid manifest entry'):
            candidate_manifest(str(candidate), [{'path': '../item.py', 'sha256': 'a' * 64}], ['.'])

    def test_proposal_cannot_claim_unverified_snapshot(self):
        proposal = self.fixture(); proposal['blocker_snapshot_digest'] = '0' * 64
        with self.assertRaisesRegex(PairError, 'Blocker snapshot changed'):
            self.store.propose_review_recovery(*self.actor(), proposal, idempotency_key='unverified')
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM recovery_requests').fetchone()[0], 0)

    def test_proposal_cannot_use_author_as_independent_reviewer(self):
        proposal = self.fixture(); proposal['reviewer'] = 'B'
        with self.assertRaisesRegex(PairError, 'Independent recovery reviewer'):
            self.store.propose_review_recovery(*self.actor(), proposal, idempotency_key='self-review')

    def test_arbitrary_relation_or_new_blocker_name_cannot_reset_issue(self):
        proposal = self.fixture(); proposal['stable_blocker'] = 'new-name'
        with self.assertRaisesRegex(PairError, 'Stable blocker mismatch'):
            self.store.propose_review_recovery(*self.actor(), proposal, idempotency_key='renamed')

    def test_claim_is_unique_and_original_receipt_can_be_recovered(self):
        request, _ = self.queued()
        first = self.store.claim_review_recovery(*self.actor(), request['id'])
        second = self.store.claim_review_recovery(*self.actor(), request['id'])
        self.assertTrue(first['claimed']); self.assertFalse(second['claimed'])
        recovered = self.store.recover_review_claim(*self.actor(), request['id'])
        self.assertEqual(recovered['token'], first['token'])
        with self.assertRaisesRegex(PairError, 'Recovery binding changed'):
            self.store.recover_review_claim(*self.actor('B'), request['id'])

    def test_begin_without_isolation_adapter_preserves_claim_and_does_not_count(self):
        request, _ = self.queued()
        claimed = self.store.claim_review_recovery(*self.actor(), request['id'])
        with self.assertRaisesRegex(PairError, 'trusted recovery adapter unavailable'):
            self.store.begin_review_recovery(*self.actor(), request['id'], claimed['token'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT status FROM recovery_requests').fetchone()[0], 'claimed')
            self.assertEqual(db.execute('SELECT count(*) FROM recovery_execution_attempts').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM recovery_attempts').fetchone()[0], 0)

    def test_begin_rechecks_candidate_after_claim(self):
        request, proposal = self.queued()
        claimed = self.store.claim_review_recovery(*self.actor(), request['id'])
        (self.project / 'candidate' / 'patch.py').write_text('changed', encoding='utf-8')
        with self.assertRaisesRegex(PairError, 'Candidate changed'):
            self.store.begin_review_recovery(*self.actor(), request['id'], claimed['token'])

    def test_old_epoch_cannot_finish_and_unproven_pass_is_not_accepted(self):
        request, _, claimed, running = self.running()
        with self.assertRaisesRegex(PairError, 'Stale recovery execution'):
            self.store.finish_review_recovery(*self.actor(), request['id'], claimed['token'], epoch=running['epoch'] - 1, report={'verdict': 'passed'})
        self.store._recovery_evidence_adapter = EvidenceFixture(execution_terminal=True)
        result = self.store.finish_review_recovery(*self.actor(), request['id'], claimed['token'], epoch=running['epoch'], report={'verdict': 'passed'})
        self.assertEqual(result['verdict'], 'needs_validation')
        self.assertEqual(self.store.task('repair')['status'], 'pending')

    def test_unknown_retains_single_channel_and_original_attempt(self):
        request, _, _, running = self.running()
        self.store._recovery_evidence_adapter = EvidenceFixture(execution_terminal=False)
        result = self.store.reconcile_review_recovery(*self.actor(), request['id'])
        self.assertEqual(result['status'], 'unknown'); self.assertTrue(result['slot_retained'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM recovery_execution_attempts').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM recovery_attempts').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT execution_epoch FROM recovery_requests').fetchone()[0], running['epoch'])
        with self.assertRaisesRegex(PairError, 'execution unresolved'):
            self.store.revoke_review_recovery(*self.actor(), request['id'], reason='不能将unknown当未开始')

    def test_read_gap_is_rejected_without_advancing_cursor(self):
        proposal = self.fixture()
        request = self.store.propose_review_recovery(*self.actor(), proposal, idempotency_key='request')
        with self.assertRaisesRegex(PairError, 'Recovery read gap'):
            self.store.read_review_recovery(*self.actor(), request['id'], offset=20)
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM recovery_reads').fetchone()[0], 0)

    def test_application_rejects_raw_pass_without_formal_receipts(self):
        request, _ = self.queued()
        self.enable('patch_application')
        with self.store.transaction() as db:
            db.execute("UPDATE recovery_requests SET status='review_passed' WHERE id=?", (request['id'],))
            db.execute("UPDATE review_stages SET status='passed',evidence='{}' WHERE id='stage-old-one'")
        plan = {'old_runtime_digest': 'a' * 64, 'new_runtime_digest': 'b' * 64, 'state_schema': 14,
                'backup_ref': 'fixture:state-backup', 'verification_contract': 'c' * 64, 'allowed_paths': ['runtime']}
        with self.assertRaisesRegex(PairError, 'formal passed stage'):
            self.store.prepare_recovery_application(*self.actor(), request['id'], 'stage-old-one', plan, idempotency_key='apply')
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM recovery_applications').fetchone()[0], 0)

    def remaining_fixture(self, *, enabled=True, pinned=False):
        if enabled:
            self.enable('continuation')
        self.store.assign('saved', '原业务', 'B', ['patch.py'], acceptance=['输出正确'],
                         required_checks=[{'name': 'unit', 'command': 'python unit.py', 'required': True}], pin_member=pinned)
        path = self.root / 'B' / 'patch.py'; path.write_text('saved = 1\n', encoding='utf-8')
        with self.store.transaction() as db:
            proof = {'id': self.sessions['B'], 'cwd': self.directories['B'], 'modelProvider': 'deepseek', 'model': 'deepseek-chat'}
            db.execute("UPDATE members SET proof=? WHERE id='B'", (json.dumps(proof),))
            db.execute("UPDATE tasks SET status='handed_back',result='已保存第一步；第二步未执行' WHERE id='saved'")
            db.execute("UPDATE messages SET process_status='claimed',claim_token='original-token',delivery_status='accepted',remote_status='completed' WHERE task_id='saved'")
            message = db.execute("SELECT id FROM messages WHERE task_id='saved'").fetchone()[0]
        source = self.store.inspect_remaining_work(*self.actor(), 'saved')
        kwargs = {'remaining_steps': [{'id': 'second', 'description': '执行尚未开始的第二步'}],
                  'completed_effects': [{'id': 'first', 'evidence': 'delivery:saved 第一项已保存'}],
                  'candidate': {'root': self.directories['B'], 'manifest': [{'path': 'patch.py', 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]},
                  'evidence': '只办理原目标的剩余步骤', 'idempotency_key': 'continue-saved'}
        first = self.store.continue_remaining_work(*self.actor(), 'saved', 'remaining', source['source_digest'], **kwargs)
        self.assertEqual(first['state'], 'requires_source_read')
        page = self.store.read_delivery(*self.actor(), first['delivery'])
        self.assertIsNone(page['next_offset'])
        self.store.ack_delivery(*self.actor(), first['delivery'], page['digest'])
        return source, kwargs, message

    def test_remaining_work_has_new_claim_and_preserves_old_token_guard(self):
        source, kwargs, message = self.remaining_fixture()
        result = self.store.continue_remaining_work(*self.actor(), 'saved', 'remaining', source['source_digest'], **kwargs)
        self.assertEqual(result['state'], 'registered')
        self.assertEqual(self.store.task('saved')['status'], 'handed_back')
        with self.assertRaises(PairError):
            self.store.complete(*self.actor('B'), message, 'original-token', '旧领取不能重新完成')
        with self.store.transaction() as db:
            current = db.execute("SELECT * FROM messages WHERE task_id='remaining' AND kind='task'").fetchone()
            self.assertNotEqual(current['id'], message)
            self.assertIsNone(current['claim_token'])
            self.assertEqual(self.store._continuation_required_deliveries(db, 'remaining'), ['delivery:saved'])
            self.assertEqual(self.store._continuation_contributors(db, 'remaining')[0]['provider'], 'deepseek')
        repeated = self.store.continue_remaining_work(*self.actor(), 'saved', 'remaining', source['source_digest'], **kwargs)
        self.assertTrue(repeated['duplicate'])

    def test_safe_recovery_cannot_enable_policy_or_duplicate_remaining_work(self):
        source, kwargs, _ = self.remaining_fixture(enabled=False)
        waiting = self.store.continue_remaining_work(*self.actor(), 'saved', 'remaining', source['source_digest'], **kwargs)
        self.assertEqual(waiting['reason'], 'continuation_policy_disabled')
        first = self.store.recover_safe(*self.actor())
        self.assertTrue(first['policies_unchanged'])
        with self.store.transaction() as db:
            self.assertFalse(db.execute("SELECT 1 FROM tasks WHERE id='remaining'").fetchone())
        self.enable('continuation')
        resumed = self.store.recover_safe(*self.actor())
        self.assertEqual(resumed['decision'], 'execute_next')
        self.assertFalse(resumed['models_started'])
        self.store.recover_safe(*self.actor())
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM tasks WHERE id='remaining'").fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM task_continuations').fetchone()[0], 1)

    def test_remaining_plan_waits_for_unknown_original_execution(self):
        source, kwargs, message = self.remaining_fixture()
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='unknown' WHERE id=?", (message,))
        result = self.store.continue_remaining_work(*self.actor(), 'saved', 'remaining', source['source_digest'], **kwargs)
        self.assertEqual(result['reason'], 'EXECUTION_UNRESOLVED')
        with self.store.transaction() as db:
            self.assertFalse(db.execute("SELECT 1 FROM tasks WHERE id='remaining'").fetchone())

    def test_remaining_plan_rechecks_files_before_safe_execution(self):
        source, kwargs, _ = self.remaining_fixture(enabled=False)
        self.store.continue_remaining_work(*self.actor(), 'saved', 'remaining', source['source_digest'], **kwargs)
        (self.root / 'B' / 'patch.py').write_text('user changed it\n', encoding='utf-8')
        self.enable('continuation')
        result = self.store.recover_safe(*self.actor())
        self.assertEqual(result['intents'][0]['decision'], 'blocked_no_safe_action')
        with self.store.transaction() as db:
            self.assertFalse(db.execute("SELECT 1 FROM tasks WHERE id='remaining'").fetchone())

    def test_remaining_plan_does_not_repeat_known_effect_id(self):
        source, kwargs, _ = self.remaining_fixture()
        kwargs['remaining_steps'][0]['id'] = 'first'
        with self.assertRaisesRegex(PairError, 'Distinct remaining effects'):
            self.store.continue_remaining_work(*self.actor(), 'saved', 'remaining', source['source_digest'], **kwargs)

    def test_remaining_plan_preserves_pinned_member(self):
        source, kwargs, _ = self.remaining_fixture(pinned=True)
        (self.root / 'A' / 'patch.py').write_bytes((self.root / 'B' / 'patch.py').read_bytes())
        kwargs['target'] = 'A'
        with self.assertRaisesRegex(PairError, 'Original execution constraint'):
            self.store.continue_remaining_work(*self.actor(), 'saved', 'remaining', source['source_digest'], **kwargs)

    def test_remaining_first_claim_rechecks_candidate_after_queueing(self):
        source, kwargs, _ = self.remaining_fixture()
        self.store.continue_remaining_work(*self.actor(), 'saved', 'remaining', source['source_digest'], **kwargs)
        with self.store.transaction() as db:
            message = db.execute("SELECT id FROM messages WHERE task_id='remaining' AND kind='task'").fetchone()[0]
        (self.root / 'B' / 'patch.py').write_text('changed after registration\n', encoding='utf-8')
        with self.assertRaisesRegex(PairError, 'Candidate changed'):
            self.store.claim(*self.actor('B'), message_id=message)
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT process_status FROM messages WHERE id=?', (message,)).fetchone()[0], 'unclaimed')

    def test_new_continuation_turn_does_not_revive_old_terminal_workspace_lock(self):
        source, kwargs, old_message = self.remaining_fixture()
        self.store.continue_remaining_work(*self.actor(), 'saved', 'remaining', source['source_digest'], **kwargs)
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET remote_result=? WHERE id=?", (json.dumps({'workspace_busy': True}), old_message))
            current = db.execute("SELECT id FROM messages WHERE task_id='remaining' AND kind='task'").fetchone()[0]
        claimed = self.store.claim(*self.actor('B'), message_id=current)
        self.assertTrue(claimed['claimed'])

    def supersession_fixture(self, *, whole_task=False):
        check = {'name': 'unit', 'command': 'python unit.py', 'required': True}
        for key, member in (('old', 'B'), ('new', 'A')):
            self.store.assign(key, key, member, [key + '.py'], acceptance=['输出正确'], required_checks=[check])
        old_material = {'coordinator_acceptance': ['输出正确'], 'coordinator_required_checks': [check]}
        now = time.time()
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET process_status='completed',delivery_status='accepted',remote_status='completed'")
            db.execute("UPDATE tasks SET status='needs_validation' WHERE id='old'")
            db.execute("UPDATE tasks SET status='completed' WHERE id='new'")
            db.execute('''INSERT INTO review_stages(id,task_id,version,requirements_version,artifact,digest,contributors,checks,payload,status,verdict,evidence,created,updated)
                          VALUES('old-stage','old',1,1,'{}',?,'[]','[]',?,'needs_validation','needs_validation','{}',?,?)''', ('a' * 64, json.dumps(old_material), now, now))
            expected = self.store._remaining_source(db, 'old')[1] if whole_task else 'a' * 64
        obligations = list(self.store._obligations(old_material))
        coverage = [{'old': key, 'new': key} for key in obligations]
        source_obligations = self.store._obligations(old_material)
        if whole_task:
            coverage += [{'old': 'stage:old-stage:' + key, 'new': key} for key in obligations]
            source_obligations.update({'stage:old-stage:' + key: value for key, value in self.store._obligations(old_material).items()})
        declaration = {'source_task': 'old', 'source_stage': None if whole_task else 'old-stage',
                       'source_digest': expected, 'requirements_version': 1, 'source_obligations': source_obligations, 'coverage': coverage}
        new_material = dict(old_material, supersedes=[declaration])
        report = {'verdict': 'passed', 'checks': [{'name': 'unit', 'status': 'passed', 'evidence': ['fixture:commandExecution']}], 'findings': []}
        reviewer = '00000000-0000-0000-0000-000000000099'
        with self.store.transaction() as db:
            db.execute('''INSERT INTO review_stages(id,task_id,version,requirements_version,artifact,digest,contributors,checks,payload,status,
                          reviewer_epoch,reviewer_session,execution_id,turn_id,verdict,evidence,created,updated)
                          VALUES('new-stage','new',1,1,'{}',?,'[]','[]',?,'passed',4,?,'formal-execution','formal-turn','passed',?,?,?)''',
                       ('b' * 64, json.dumps(new_material), reviewer, json.dumps(report), now, now))
            self.store.event(db, 'review_effective_verdict', stage='new-stage', digest='b' * 64, epoch=4,
                             session=reviewer, execution='formal-execution', turn='formal-turn', effective_verdict='passed')
            binding = {'pair': self.store.pair_id, 'stage': 'new-stage', 'digest': 'b' * 64, 'epoch': 4,
                       'session': reviewer, 'execution': 'formal-execution', 'turn': 'formal-turn',
                       'cwd': str(self.root / 'review'), 'home': str(self.home)}
            self.store._put(db, 'reviewer_producer:formal-execution', {'binding': binding, 'launch': {'version': '0.163.0'}, 'finished': True})
        return expected, coverage

    def test_stage_replacement_does_not_release_uncovered_task_or_rewrite_old_report(self):
        expected, coverage = self.supersession_fixture()
        result = self.store.supersede_obligations(*self.actor(), 'old', 'new-stage', expected,
            coverage=coverage, source_stage='old-stage', idempotency_key='stage-replacement')
        self.assertTrue(result['effective']); self.assertFalse(result['task_obligations_satisfied'])
        with self.store.transaction() as db:
            self.assertEqual(self.store._stage_backlog_ids(db, 'B'), [])
            self.assertEqual(self.store._task_backlog_ids(db, 'B'), ['old'])
            old = db.execute("SELECT status,verdict,evidence FROM review_stages WHERE id='old-stage'").fetchone()
            self.assertEqual(tuple(old), ('needs_validation', 'needs_validation', '{}'))

    def test_full_replacement_releases_each_count_once_and_preserves_old_task(self):
        expected, coverage = self.supersession_fixture(whole_task=True)
        kwargs = {'coverage': coverage, 'idempotency_key': 'task-replacement'}
        first = self.store.supersede_obligations(*self.actor(), 'old', 'new-stage', expected, **kwargs)
        second = self.store.supersede_obligations(*self.actor(), 'old', 'new-stage', expected, **kwargs)
        self.assertTrue(first['task_obligations_satisfied']); self.assertTrue(second['duplicate'])
        with self.store.transaction() as db:
            self.assertEqual(self.store._stage_backlog_ids(db, 'B'), [])
            self.assertEqual(self.store._task_backlog_ids(db, 'B'), [])
            self.assertEqual(self.store._effective_obligation_task(db, 'old'), 'new')
            self.assertEqual(db.execute("SELECT status FROM tasks WHERE id='old'").fetchone()[0], 'needs_validation')
            self.assertEqual(db.execute('SELECT count(*) FROM obligation_supersessions').fetchone()[0], 1)

    def test_unreviewed_coverage_and_missing_formal_receipt_are_rejected(self):
        expected, coverage = self.supersession_fixture()
        with self.assertRaisesRegex(PairError, 'Incomplete obligation coverage'):
            self.store.supersede_obligations(*self.actor(), 'old', 'new-stage', expected,
                coverage=coverage[:1], source_stage='old-stage', idempotency_key='partial')
        with self.store.transaction() as db:
            db.execute("DELETE FROM events WHERE kind='review_effective_verdict'")
        with self.assertRaisesRegex(PairError, 'Formal supersession receipt missing'):
            self.store.supersede_obligations(*self.actor(), 'old', 'new-stage', expected,
                coverage=coverage, source_stage='old-stage', idempotency_key='no-receipt')

    def test_supersession_loses_effect_when_exact_replacement_execution_changes(self):
        expected, coverage = self.supersession_fixture(whole_task=True)
        self.store.supersede_obligations(*self.actor(), 'old', 'new-stage', expected,
            coverage=coverage, idempotency_key='task-replacement')
        with self.store.transaction() as db:
            db.execute("UPDATE review_stages SET execution_id='another-execution' WHERE id='new-stage'")
            self.assertFalse(self.store._task_obligation_satisfied(db, 'old'))
            self.assertEqual(self.store._task_backlog_ids(db, 'B'), ['old'])
            self.assertEqual(self.store._stage_backlog_ids(db, 'B'), ['old-stage'])
            self.assertEqual(db.execute('SELECT count(*) FROM obligation_supersessions').fetchone()[0], 1)

    def test_supersession_during_pause_records_fact_without_dispatch(self):
        expected, coverage = self.supersession_fixture(whole_task=True)
        self.store.request_pause(*self.actor(), reason='暂停', idempotency_key='pause')
        result = self.store.supersede_obligations(*self.actor(), 'old', 'new-stage', expected,
            coverage=coverage, idempotency_key='task-replacement')
        self.assertFalse(result['automatic_dispatch'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT status FROM collaborations').fetchone()[0], 'paused')


if __name__ == '__main__':
    unittest.main()
