"""临时真实 Store 的准入回归；官方执行边界使用既有隔离夹具。"""
import copy
import concurrent.futures
import hashlib
import json
import unittest

from scripts.pair_audit_adapter import adapt_report
from scripts.pair_auxiliary_runtime import _context
from scripts.pair_core import FileLock, PairError, Store
from scripts.pair_stages import digest
from scripts.pair_validator_admission import (admit_validator_run, verify_validator_admission,
                                               validator_admission_boundary)
from tests import test_auxiliary_v14


class ValidatorAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_auxiliary_v14.AuxiliaryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.store = self.fixture.store
        self.candidate = self.fixture.queue()
        self.ticket = self.fixture.reserve()
        self.fixture.start_fixture(self.ticket)
        with self.store.transaction() as db:
            db.execute("UPDATE auxiliary_tasks SET track='cloudflare' WHERE id='aux'")
            db.execute('UPDATE auxiliary_candidates SET tracks=?,coverage_units=? WHERE id=?',
                (json.dumps(['cloudflare']), json.dumps([
                    {'id': 'source', 'track': 'cloudflare', 'required': True}]), self.candidate['id']))
            execution = dict(self.store._auxiliary_execution(db, self.ticket['execution_id'], self.ticket['lease']))
            task = dict(self.store._auxiliary_task(db, execution['task_id']))
            candidate = dict(self.store._auxiliary_candidate(db, task['candidate_id']))
            context = _context(self.store, {'execution': execution, 'task': task, 'candidate': candidate})
            raw = {'context': context, 'verdict': 'needs_validation', 'findings': [], 'checks': [],
                   'coverage_ledger': [{'coverage_id': 'source', 'status': 'covered'}],
                   'coverage': [{'unit_id': 'source', 'status': 'reviewed', 'evidence_refs': ['fixture']}],
                   'unverified_candidates': []}
            adapted = adapt_report(raw, context=context, expected_units=[{'id': 'source', 'required': True}])
            self.store._save_auxiliary_report(db, execution, raw, adapted['adapted_report'],
                adapted['validation'], {'formal_checks_verified': False})
        self.tools = {'node': 'a' * 64, 'worker': 'b' * 64}
        self.run_id = '1' * 32

    def admit(self, **changes):
        args = dict(run_id=self.run_id, tool_identity=self.tools, **changes)
        return admit_validator_run(self.store, *self.fixture.actor(),
            self.ticket['execution_id'], self.ticket['lease'], **args)

    def verify(self, purpose='save', **changes):
        args = dict(run_id=self.run_id, tool_identity=self.tools, purpose=purpose)
        args.update(changes)
        return verify_validator_admission(self.store, *self.fixture.actor(),
            self.ticket['execution_id'], self.ticket['lease'], **args)

    def admission(self):
        with self.store.transaction() as db:
            return self.store._option(db, 'validator_admission:' + self.run_id)

    def change(self, table, field, value, key='id', row='A'):
        with self.store.transaction() as db:
            old = db.execute(f'SELECT {field} FROM {table} WHERE {key}=?', (row,)).fetchone()[0]
            db.execute(f'UPDATE {table} SET {field}=? WHERE {key}=?', (value, row))
        return old

    def test_persisted_snapshot_binds_current_store_inputs_without_promoting_parent(self):
        with self.store.transaction() as db:
            before_report = dict(db.execute('SELECT * FROM auxiliary_reports').fetchone())
            before_stage = dict(db.execute('SELECT * FROM review_stages').fetchone())
        result = self.admit()
        self.assertFalse(result['formal'])
        self.assertFalse(result['execution_started'])
        self.assertFalse(result['formal_checks_verified'])
        record = self.admission()
        snapshot = record['snapshot']
        self.assertEqual(record['snapshot_digest'], digest(snapshot))
        self.assertEqual(snapshot['actor']['session'], self.fixture.sessions['A'])
        self.assertEqual(snapshot['actor']['version'], 1)
        self.assertEqual(snapshot['execution']['id'], self.ticket['execution_id'])
        self.assertNotIn('lease', snapshot['execution'])
        self.assertEqual(snapshot['tool_identity'], self.tools)
        self.assertEqual(len(snapshot['request']['files']), 8)
        self.assertEqual(set(snapshot['request']['binding']), {'context', 'original_report_digest',
            'stored_report_digest', 'adapted_report_digest'})
        self.assertTrue(self.admit()['duplicate'])
        restored = Store(self.store.pair_id, home=self.fixture.home)
        claimed = verify_validator_admission(restored, *self.fixture.actor(),
            self.ticket['execution_id'], self.ticket['lease'], run_id=self.run_id,
            tool_identity=self.tools, purpose='dispatch')
        self.assertTrue(claimed['dispatch_allowed'])
        self.assertTrue(self.verify()['save_allowed'])
        with self.store.transaction() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM auxiliary_reports').fetchone()), before_report)
            self.assertEqual(dict(db.execute('SELECT * FROM review_stages').fetchone()), before_stage)

    def test_dispatch_claim_is_once_and_cannot_be_reset_by_duplicate_admit(self):
        self.admit()
        self.assertTrue(self.verify('dispatch')['dispatch_allowed'])
        self.assertTrue(self.admit()['duplicate'])
        result = self.verify('dispatch')
        self.assertFalse(result['dispatch_allowed'])
        self.assertTrue(result['reconciliation_only'])
        self.assertTrue(self.verify()['save_allowed'])

    def test_concurrent_dispatchers_claim_exactly_one_start(self):
        self.admit()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.verify('dispatch'), range(2)))
        self.assertEqual(sum(result['dispatch_allowed'] for result in results), 1)

    def test_real_store_save_within_boundary_preserves_diagnostic_scope(self):
        request = self.admit()['request']
        self.verify('dispatch')
        empty = hashlib.sha256(b'').hexdigest()
        artifact = {'binding': request['binding'], 'files': request['files'],
            'formal_checks_verified': False, 'validators_succeeded': True,
            'results': [{'validator': name, 'exit_code': 0, 'stdout_sha256': empty,
                         'stderr_sha256': empty, 'stderr': ''}
                        for name in ('validate-findings.cjs', 'validate-coverage-ledger.cjs')]}
        native = {'ExitCode': 0, 'ActiveProcesses': 0, 'TimedOut': False}
        with validator_admission_boundary(self.store, *self.fixture.actor(),
                self.ticket['execution_id'], self.ticket['lease'], run_id=self.run_id,
                tool_identity=self.tools, purpose='save') as result:
            self.assertTrue(result['save_allowed'])
            saved = self.store.record_validator_run(*self.fixture.actor(), self.ticket['execution_id'],
                self.ticket['lease'], self.run_id, request, native, artifact, self.tools)
        self.assertTrue(saved['saved'])
        self.assertFalse(saved['formal_checks_verified'])
        self.assertFalse(self.verify()['save_allowed'])
        settled = self.store.settle_audit(*self.fixture.actor(), self.candidate['id'])
        self.assertEqual(settled['verdict'], 'needs_validation')

    def test_no_dispatch_or_save_without_original_persisted_admission(self):
        for purpose in ('dispatch', 'save', 'reconcile'):
            with self.subTest(purpose=purpose), self.assertRaisesRegex(PairError, 'admission unavailable'):
                self.verify(purpose)
        self.admit()
        with self.assertRaisesRegex(PairError, 'dispatch required'):
            self.verify()

    def test_existing_run_cannot_be_given_retroactive_admission(self):
        with self.store.transaction() as db:
            self.store._put(db, 'validator_run:' + self.run_id, {'formal_checks_verified': False})
        with self.assertRaisesRegex(PairError, 'admission too late'):
            self.admit()

    def test_foreign_member_session_cwd_and_lease_are_rejected(self):
        actors = [self.fixture.actor('B'),
                  ('A', self.fixture.sessions['B'], self.fixture.directories['A']),
                  ('A', self.fixture.sessions['A'], str(self.fixture.root))]
        for actor in actors:
            with self.subTest(actor=actor), self.assertRaises(PairError):
                admit_validator_run(self.store, *actor, self.ticket['execution_id'],
                    self.ticket['lease'], run_id=self.run_id, tool_identity=self.tools)
        with self.assertRaisesRegex(PairError, 'lease mismatch'):
            admit_validator_run(self.store, *self.fixture.actor(), self.ticket['execution_id'],
                'wrong', run_id=self.run_id, tool_identity=self.tools)

    def test_binding_identity_and_config_changes_revoke_old_admission(self):
        self.admit()
        with self.store.transaction() as db:
            proof = json.loads(self.store._member(db, 'A')['proof'])
        for table, field, changed, key, row in (
                ('members', 'binding_version', 2, 'id', 'A'),
                ('members', 'proof', json.dumps(dict(proof, model='gpt-other')), 'id', 'A'),
                ('members', 'proof', json.dumps(dict(proof, observation='changed')), 'id', 'A'),
                ('members', 'verified', 0, 'id', 'A'),
                ('auxiliary_configs', 'enabled', 0, 'id', 'allowed-a'),
                ('auxiliary_sessions', 'proof', '{}', 'id', self.ticket['reviewer_id'])):
            with self.subTest(field=field):
                old = self.change(table, field, changed, key, row)
                try:
                    with self.assertRaises(PairError):
                        self.verify('dispatch')
                finally:
                    self.change(table, field, old, key, row)

    def test_control_generation_change_revokes_dispatch_and_save(self):
        self.admit()
        self.verify('dispatch')
        with self.store.transaction() as db:
            self.store._put(db, 'control_generation_v14', self.store._control_generation(db) + 1)
        for purpose in ('dispatch', 'save'):
            with self.subTest(purpose=purpose), self.assertRaisesRegex(PairError, 'admission stale'):
                self.verify(purpose)
        result = self.verify('reconcile')
        self.assertEqual(result['controls_changed'], ['control_generation'])
        self.assertFalse(result['dispatch_allowed'])
        self.assertFalse(result['save_allowed'])

    def test_authorization_change_revokes_dispatch_and_save(self):
        self.admit()
        self.verify('dispatch')
        self.store.delegation_policy(*self.fixture.actor(), '隔离夹具中的授权变更', enabled=True)
        for purpose in ('dispatch', 'save'):
            with self.subTest(purpose=purpose), self.assertRaisesRegex(PairError, 'admission stale'):
                self.verify(purpose)
        self.assertEqual(self.verify('reconcile')['controls_changed'], ['authorization_version'])

    def test_pause_allows_only_original_run_reconciliation(self):
        self.admit()
        self.verify('dispatch')
        self.store.request_pause(*self.fixture.actor(), reason='隔离测试暂停', idempotency_key='unit-pause')
        for action in (lambda: self.admit(), lambda: self.verify('dispatch'), lambda: self.verify()):
            with self.assertRaisesRegex(PairError, 'not running'):
                action()
        result = self.verify('reconcile')
        self.assertTrue(result['reconciliation_only'])
        self.assertFalse(result['dispatch_allowed'])
        self.assertFalse(result['save_allowed'])
        self.assertFalse(result['formal'])
        self.assertIn('Collaboration not running', result['control_block'])

    def test_closed_scope_or_task_control_prevents_new_admission(self):
        self.store.request_pause(*self.fixture.actor(), reason='隔离任务控制', idempotency_key='task-pause',
            scope={'kind': 'task', 'id': 'product'})
        with self.assertRaisesRegex(PairError, 'control gate'):
            self.admit()

    def test_candidate_contract_execution_and_report_changes_are_rejected(self):
        self.admit()
        self.verify('dispatch')
        for table, field, changed, row in (
                ('auxiliary_candidates', 'candidate_digest', 'changed', self.candidate['id']),
                ('auxiliary_candidates', 'contract_digest', 'changed', self.candidate['id']),
                ('auxiliary_executions', 'state', 'unknown', self.ticket['execution_id']),
                ('auxiliary_executions', 'turn_id', 'changed', self.ticket['execution_id']),
                ('auxiliary_reports', 'raw_report', '{}', self.ticket['execution_id']),
                ('auxiliary_reports', 'adapted_report', '{}', self.ticket['execution_id']),
                ('auxiliary_reports', 'report_digest', 'changed', self.ticket['execution_id'])):
            key = 'execution_id' if table == 'auxiliary_reports' else 'id'
            with self.subTest(table=table, field=field):
                old = self.change(table, field, changed, key, row)
                try:
                    with self.assertRaises((PairError, ValueError)):
                        self.verify()
                finally:
                    self.change(table, field, old, key, row)

    def test_changed_tool_digest_conflicts_with_existing_run(self):
        self.admit()
        changed = dict(self.tools, node='c' * 64)
        with self.assertRaisesRegex(PairError, 'admission conflict'):
            admit_validator_run(self.store, *self.fixture.actor(), self.ticket['execution_id'],
                self.ticket['lease'], run_id=self.run_id, tool_identity=changed)
        with self.assertRaisesRegex(PairError, 'admission changed'):
            self.verify('dispatch', tool_identity=changed)

    def test_boundary_types_and_caller_passed_boolean_cannot_supply_authentication(self):
        for run_id in (None, True, '../escape', 'A' * 32, 'a' * 31):
            with self.subTest(run_id=run_id), self.assertRaisesRegex(PairError, 'run ID'):
                admit_validator_run(self.store, *self.fixture.actor(), self.ticket['execution_id'],
                    self.ticket['lease'], run_id=run_id, tool_identity=self.tools)
        for tools in ({}, {'worker': 'a' * 64}, {'node': True}, {'node': 'a' * 63},
                      {'node': 'a' * 64, 'passed': True}):
            with self.subTest(tools=tools), self.assertRaisesRegex(PairError, 'tool identity'):
                admit_validator_run(self.store, *self.fixture.actor(), self.ticket['execution_id'],
                    self.ticket['lease'], run_id=self.run_id, tool_identity=tools)
        self.admit()
        with self.assertRaises(TypeError):
            self.verify('dispatch', passed=True)
        with self.assertRaisesRegex(PairError, 'purpose'):
            self.verify('passed')

    def test_tampered_or_unreviewed_snapshot_is_not_a_valid_admission(self):
        self.admit()
        original = self.admission()
        for change in ({'formal': True}, {'state': 'passed'}, {'schema_version': True},
                       {'snapshot_digest': 'f' * 64}, {'passed': True}):
            forged = dict(copy.deepcopy(original), **change)
            with self.store.transaction() as db:
                self.store._put(db, 'validator_admission:' + self.run_id, forged)
            with self.subTest(change=change), self.assertRaisesRegex(PairError, 'admission unavailable'):
                self.verify('dispatch')
        with self.store.transaction() as db:
            self.store._put(db, 'validator_admission:' + self.run_id, original)
        self.assertTrue(self.verify('dispatch')['dispatch_allowed'])

    def test_saved_run_never_replays_or_receives_another_save_permission(self):
        self.admit()
        self.verify('dispatch')
        with self.store.transaction() as db:
            self.store._put(db, 'validator_run:' + self.run_id, {'formal_checks_verified': False})
        result = self.verify()
        self.assertTrue(result['already_saved'])
        self.assertFalse(result['save_allowed'])
        self.assertFalse(self.verify('dispatch')['dispatch_allowed'])

    def test_execution_boundary_holds_control_lock_and_releases_it_after_error(self):
        self.admit()
        with self.assertRaisesRegex(RuntimeError, 'unknown start'):
            with validator_admission_boundary(self.store, *self.fixture.actor(),
                    self.ticket['execution_id'], self.ticket['lease'], run_id=self.run_id,
                    tool_identity=self.tools, purpose='dispatch') as result:
                self.assertTrue(result['dispatch_allowed'])
                with self.assertRaisesRegex(PairError, 'Lock held'):
                    with FileLock(self.store.control_lock):
                        self.fail('控制锁不应提前释放')
                with self.store.transaction() as db:
                    self.assertEqual(self.store._option(db,
                        'validator_admission:' + self.run_id)['state'], 'dispatching')
                raise RuntimeError('unknown start')
        with FileLock(self.store.control_lock):
            pass
        self.assertFalse(self.verify('dispatch')['dispatch_allowed'])

    def test_scope_closed_pool_disabled_and_observer_gap_are_existing_dispatch_gates(self):
        with self.store.transaction() as db:
            policy = self.store._option(db, 'auxiliary_policy')
        for name, value in (('goal_completed', True), ('closed_scope', {'task_ids': ['product']}),
                            ('auxiliary_policy', dict(policy, enabled=False)),
                            ('control_observer:A', {'state': 'gap', 'reason': 'unit gap',
                                                    'source_identity': 'unit-source'})):
            with self.subTest(gate=name):
                with self.store.transaction() as db:
                    old = self.store._option(db, name)
                    self.store._put(db, name, value)
                try:
                    with self.assertRaises(PairError):
                        self.admit()
                finally:
                    with self.store.transaction() as db:
                        if old is None:
                            db.execute('DELETE FROM pair_settings WHERE key=?', (name,))
                        else:
                            self.store._put(db, name, old)


if __name__ == '__main__':
    unittest.main()
