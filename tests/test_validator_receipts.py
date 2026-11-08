"""真实临时 Store 与持久日志；原宿主登记和官方 API 使用明确隔离夹具。"""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import pair_validator_host as host
from scripts import pair_validator_receipts as query
from scripts.pair_core import PairError
from scripts.pair_producer import file_identity, strict_display
from scripts.pair_stages import digest
from scripts.pair_transport import canonical_path
from tests import test_validator_admission as admission_tests


class ValidatorReceiptTests(unittest.TestCase):
    def setUp(self):
        self.f = admission_tests.ValidatorAdmissionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.store = self.f.store
        source = host._PACKAGE_ROOT / 'validation/bytecode-provenance-20261105'
        inventory, _, hashes, _, tools, _ = host._diagnostic_sources(source)
        self.shell, self.f.tools = host._admission_tools(source, inventory, hashes, tools)
        self.f.admit()
        self.f.verify('dispatch')
        admission = self.f.admission()
        self.actor = admission['snapshot']['actor']
        self.command = {'argv': [str(self.shell), '-NoLogo', '-NoProfile', '-NonInteractive',
            '-ExecutionPolicy', 'RemoteSigned', '-File', str(source / 'Invoke-ValidatorRawModuleDiagnostic.ps1'),
            '-InputDirectory', str(host._RUN_ROOT.parent / 'prepared' / self.f.run_id),
            '-SourceInventory', str(source / 'raw-module-source-inventory-loaded-reader.json'),
            '-EvidenceProfile', 'loaded-reader-v2', '-RunId', self.f.run_id],
            'cwd': canonical_path(host._PACKAGE_ROOT)}
        exe = self.f.fixture.root / 'codex.exe'
        exe.write_bytes(b'unit-only producer executable, never started')
        binding = {'pair': self.store.pair_id, 'run': self.f.run_id,
            'execution': self.f.ticket['execution_id'], 'session': self.actor['session'],
            'turn': 'host-turn', 'item': 'host-item', 'home': self.actor['home'],
            'cwd': self.actor['cwd'], 'admission_digest': admission['snapshot_digest'],
            'command_digest': digest(self.command)}
        start = {'binding': binding, 'launch': {'version': '0.167.0-alpha.3.2',
            'file': file_identity(exe), 'process': {'pid': 123, 'created': 456}}, 'finished': False}
        self.record = {'schema_version': 1, 'run_id': self.f.run_id,
            'admission_snapshot_digest': admission['snapshot_digest'], 'command': self.command,
            'turn_id': 'host-turn', 'item_id': 'host-item',
            'start_producer': start, 'final_producer': dict(copy.deepcopy(start), finished=True)}
        self.save_record()
        self.raw = {'type': 'CommandExecution', 'id': 'host-item', 'status': 'completed',
            'exit_code': 0, 'command': self.command['argv'], 'cwd': self.command['cwd'],
            'source': 'unified_exec_startup'}
        self.api = {'type': 'commandExecution', 'id': 'host-item', 'status': 'completed',
            'exitCode': 0, 'command': strict_display(self.command['argv']),
            'cwd': self.command['cwd'], 'source': 'unifiedExecStartup'}
        self.thread = {'id': self.actor['session'], 'cwd': self.actor['cwd'],
            'model': self.actor['model'], 'modelProvider': self.actor['provider']}
        self.turn = {'id': 'host-turn', 'threadId': self.actor['session'], 'status': 'completed',
            'completedAt': 123, 'items': [self.api]}
        self.mutate = None
        self.observer_calls = 0
        self.rollout()

    def save_record(self):
        with self.store.transaction() as db:
            self.store._put(db, 'validator_host_execution:' + self.f.run_id, self.record)

    def rollout(self, *, items=None, terminal=True, turn_id='host-turn', session=None):
        session = self.actor['session'] if session is None else session
        path = Path(self.actor['home']) / 'sessions' / 'validator.jsonl'
        path.parent.mkdir(exist_ok=True)
        self.thread['path'] = str(path)
        rows = [{'type': 'session_meta', 'payload': {'id': session, 'cwd': self.actor['cwd']}},
                {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': turn_id}}]
        rows += [{'type': 'event_msg', 'payload': {'type': 'item_completed',
            'thread_id': session, 'turn_id': turn_id, 'item': item}}
            for item in ([self.raw] if items is None else items)]
        if terminal:
            rows += [{'type': 'event_msg', 'payload': {'type': 'task_complete',
                'turn_id': turn_id, 'completed_at': 123}}]
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')

    def observer(self, **options):
        self.observer_calls += 1
        self.assertEqual(options, {'codex_home': self.actor['home'], 'cwd': self.actor['cwd'],
            'metadata_only': True, 'executable': self.record['final_producer']['launch']['file']['path']})
        case = self
        class Observer:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read_thread(self, session):
                case.assertEqual(session, case.actor['session'])
                return copy.deepcopy(case.thread)
            def turns(self, session, *, items_view):
                case.assertEqual(session, case.actor['session'])
                case.assertEqual(items_view, 'full')
                if case.mutate: case.mutate()
                return iter([copy.deepcopy(case.turn)])
        return Observer()

    def read(self, **changes):
        args = {'run_id': self.f.run_id, 'observer_factory': self.observer}
        args.update(changes)
        return query.read_validator_host_receipt(self.store, *self.f.fixture.actor(),
            self.f.ticket['execution_id'], self.f.ticket['lease'], **args)

    def test_exact_dual_source_query_is_read_only_and_does_not_promote_parent(self):
        with self.store.transaction() as db:
            before_report = dict(db.execute('SELECT * FROM auxiliary_reports').fetchone())
            before_stage = dict(db.execute('SELECT * FROM review_stages').fetchone())
            before_settings = list(db.execute('SELECT key,value FROM pair_settings ORDER BY key'))
        result = self.read()
        self.assertTrue(result['official_cli_receipt_verified'])
        self.assertTrue(result['command_succeeded'])
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['item_id'], 'host-item')
        self.assertFalse(result['formal'])
        self.assertFalse(result['formal_checks_verified'])
        self.assertFalse(result['replayed'])
        self.assertTrue(result['reconciliation_only'])
        self.assertEqual(self.read(), result)
        with self.store.transaction() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM auxiliary_reports').fetchone()), before_report)
            self.assertEqual(dict(db.execute('SELECT * FROM review_stages').fetchone()), before_stage)
            self.assertEqual([tuple(x) for x in db.execute('SELECT key,value FROM pair_settings ORDER BY key')],
                             [tuple(x) for x in before_settings])
        self.assertFalse(self.f.verify('dispatch')['dispatch_allowed'])

    def test_no_preregistered_host_execution_cannot_use_observer_process_as_producer(self):
        with self.store.transaction() as db:
            db.execute('DELETE FROM pair_settings WHERE key=?', ('validator_host_execution:' + self.f.run_id,))
        with self.assertRaisesRegex(ValueError, 'registration unavailable'):
            self.read()
        self.assertEqual(self.observer_calls, 0)
        self.assertFalse(self.f.verify('dispatch')['dispatch_allowed'])

    def test_unclaimed_admission_cannot_receive_a_host_receipt(self):
        admission = self.f.admission()
        admission['state'] = 'admitted'
        with self.store.transaction() as db:
            self.store._put(db, 'validator_admission:' + self.f.run_id, admission)
        with self.assertRaisesRegex(ValueError, 'dispatch not registered'):
            self.read()

    def test_foreign_api_identity_rejected(self):
        original = copy.deepcopy(self.thread)
        for change in ({'id': self.f.fixture.sessions['B']}, {'cwd': str(self.f.fixture.root)},
                       {'model': 'gpt-other'}, {'modelProvider': 'apinebula'}):
            with self.subTest(change=change):
                self.thread = dict(original, **change)
                with self.assertRaisesRegex((ValueError, PairError), 'identity mismatch'):
                    self.read()
        self.thread = original

    def test_wrong_turn_or_unknown_api_terminal_never_replays(self):
        original = copy.deepcopy(self.turn)
        for change in ({'id': 'other'}, {'threadId': self.f.fixture.sessions['B']},
                       {'status': 'inProgress'}, {'status': 'interrupted'}):
            with self.subTest(change=change):
                self.turn = dict(original, **change)
                with self.assertRaisesRegex(ValueError, 'turn unresolved'):
                    self.read()
        self.turn = dict(original, items=[dict(self.api, status='inProgress')])
        with self.assertRaisesRegex(ValueError, 'tool unresolved'):
            self.read()
        self.assertFalse(self.f.verify('dispatch')['dispatch_allowed'])

    def test_raw_source_requires_exact_session_turn_and_durable_terminal(self):
        for changes in ({'session': self.f.fixture.sessions['B']}, {'turn_id': 'other'}, {'terminal': False}):
            with self.subTest(changes=changes):
                self.rollout(**changes)
                with self.assertRaisesRegex(ValueError, 'receipt missing'):
                    self.read()

    def test_single_source_and_wrong_item_cannot_supply_receipt(self):
        for items in ([], [dict(self.raw, id='other')]):
            with self.subTest(items=items):
                self.rollout(items=items)
                with self.assertRaisesRegex(ValueError, 'receipt missing'):
                    self.read()
        self.rollout()
        self.turn['items'] = []
        with self.assertRaisesRegex(ValueError, 'receipt missing'):
            self.read()

    def test_api_raw_conflicts_are_not_resurrected_by_another_success(self):
        for change in ({'exitCode': 1}, {'cwd': str(self.f.fixture.root)},
                       {'command': self.api['command'] + ' '}, {'source': 'agent'},
                       {'status': 'failed'}, {'exitCode': False}):
            with self.subTest(change=change):
                self.turn['items'] = [self.api, dict(self.api, **change), self.api]
                with self.assertRaisesRegex(ValueError, 'receipt missing'):
                    self.read()

    def test_matching_dual_source_other_run_or_argv_is_not_the_fixed_command(self):
        for argv in (self.command['argv'][:-1] + ['2' * 32],
                     self.command['argv'] + ['-Another', 'value']):
            with self.subTest(argv=argv):
                self.raw['command'] = argv
                self.api['command'] = strict_display(argv)
                self.rollout()
                with self.assertRaisesRegex(ValueError, 'receipt missing'):
                    self.read()

    def test_nonzero_exit_is_observed_without_approval(self):
        self.api.update(status='failed', exitCode=7)
        self.raw.update(status='failed', exit_code=7)
        self.rollout()
        result = self.read()
        self.assertEqual(result['exit_code'], 7)
        self.assertFalse(result['command_succeeded'])
        self.assertFalse(result['formal_checks_verified'])

    def test_wrong_registration_run_command_and_admission_digest_rejected(self):
        original = copy.deepcopy(self.record)
        for changes in ({'run_id': '2' * 32}, {'admission_snapshot_digest': 'f' * 64},
                        {'command': dict(self.command, cwd=self.actor['cwd'])},
                        {'schema_version': True}, {'item_id': ''}, {'extra': True}):
            with self.subTest(changes=changes):
                self.record = dict(original, **changes)
                self.save_record()
                with self.assertRaisesRegex(ValueError, 'registration unavailable'):
                    self.read()
        self.record = original
        self.save_record()

    def test_missing_start_end_changed_launch_and_unknown_version_rejected(self):
        original = copy.deepcopy(self.record)
        for key, change in (('start_producer', None), ('final_producer', None),
                            ('final_producer', dict(original['final_producer'], finished=False))):
            with self.subTest(key=key):
                self.record = dict(original, **{key: change})
                self.save_record()
                with self.assertRaisesRegex(ValueError, 'producer lifecycle'):
                    self.read()
        self.record = copy.deepcopy(original)
        self.record['final_producer']['launch']['process']['created'] = 999
        self.save_record()
        with self.assertRaisesRegex(ValueError, 'producer lifecycle'):
            self.read()
        for version in ('0.167.0-alpha.3.3', '0.163.0'):
            self.record = copy.deepcopy(original)
            for field in ('start_producer', 'final_producer'):
                self.record[field]['launch']['version'] = version
            self.save_record()
            with self.subTest(version=version), self.assertRaises(ValueError):
                self.read()

    def test_producer_binding_cannot_be_rebound_to_other_host_turn(self):
        for field in ('session', 'turn', 'run', 'command_digest'):
            original = copy.deepcopy(self.record)
            for producer in ('start_producer', 'final_producer'):
                self.record[producer]['binding'][field] = 'other'
            self.save_record()
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'producer lifecycle'):
                self.read()
            self.record = original

    def test_pause_and_generation_change_only_collect_original_evidence(self):
        self.store.request_pause(*self.f.fixture.actor(), reason='receipt unit pause', idempotency_key='receipt-pause')
        result = self.read()
        self.assertIn('control_generation', result['controls_changed'])
        self.assertTrue(result['reconciliation_only'])
        self.assertFalse(result['formal_checks_verified'])
        with self.assertRaises(PairError):
            self.f.verify('dispatch')

    def test_control_change_during_query_does_not_grant_new_execution(self):
        self.mutate = lambda: self.store.request_pause(*self.f.fixture.actor(),
            reason='during receipt query', idempotency_key='query-pause')
        result = self.read()
        self.assertIn('control_generation', result['controls_changed'])
        self.assertFalse(result['formal'])
        self.assertFalse(result['replayed'])

    def test_registered_host_binding_change_during_read_rejected(self):
        def mutate():
            self.record['item_id'] = 'changed'
            self.save_record()
        self.mutate = mutate
        with self.assertRaisesRegex(ValueError, 'producer lifecycle|binding changed'):
            self.read()

    def test_report_input_tool_or_actor_changes_reject_stale_source(self):
        with self.store.transaction() as db:
            original = db.execute('SELECT raw_report FROM auxiliary_reports').fetchone()[0]
            db.execute("UPDATE auxiliary_reports SET raw_report='{}'")
        with self.assertRaises((PairError, ValueError)):
            self.read()
        with self.store.transaction() as db:
            db.execute('UPDATE auxiliary_reports SET raw_report=?', (original,))
            db.execute("UPDATE members SET binding_version=binding_version+1 WHERE id='A'")
        with self.assertRaises(PairError):
            self.read()

    def test_changed_tool_identity_or_authorization_remains_a_recovery_boundary(self):
        changed = dict(self.f.tools, host_shell='f' * 64)
        with patch.object(host, '_admission_tools', return_value=(self.shell, changed)):
            with self.assertRaisesRegex(PairError, 'admission changed'):
                self.read()
        self.store.delegation_policy(*self.f.fixture.actor(), 'receipt unit authorization', enabled=True)
        result = self.read()
        self.assertIn('authorization_version', result['controls_changed'])
        self.assertFalse(result['formal_checks_verified'])
        self.assertTrue(result['reconciliation_only'])

    def test_correct_dual_source_with_wrong_execution_directory_is_rejected(self):
        directory = str(self.f.fixture.root)
        self.api['cwd'] = directory
        self.raw['cwd'] = directory
        self.rollout()
        with self.assertRaisesRegex(ValueError, 'receipt missing'):
            self.read()

    def test_query_accepts_no_caller_sources_or_passed_boolean(self):
        for change in ({'passed': True}, {'receipts': [self.api, self.raw]}, {'turn_id': 'host-turn'},
                       {'command': self.command}, {'producer': '0.167.0-alpha.3.2'}):
            with self.subTest(change=change), self.assertRaises(TypeError):
                self.read(**change)

    def test_host_registration_is_immutable_and_idempotent(self):
        result = query.register_validator_host_execution(self.store, *self.f.fixture.actor(),
            self.f.ticket['execution_id'], self.f.ticket['lease'], run_id=self.f.run_id,
            turn_id='host-turn', item_id='host-item', command=self.command,
            start_producer=self.record['start_producer'], final_producer=self.record['final_producer'])
        self.assertTrue(result['duplicate'])
        again = query.register_validator_host_execution(self.store, *self.f.fixture.actor(),
            self.f.ticket['execution_id'], self.f.ticket['lease'], run_id=self.f.run_id,
            turn_id='host-turn', item_id='host-item', command=self.command,
            start_producer=self.record['start_producer'], final_producer=self.record['final_producer'])
        self.assertTrue(again['duplicate'])
        with self.assertRaisesRegex(ValueError, 'registration conflict|producer lifecycle'):
            query.register_validator_host_execution(self.store, *self.f.fixture.actor(),
                self.f.ticket['execution_id'], self.f.ticket['lease'], run_id=self.f.run_id,
                turn_id='other-turn', item_id='host-item', command=self.command,
                start_producer=self.record['start_producer'], final_producer=self.record['final_producer'])

    def test_host_registration_requires_dispatch_claim(self):
        admission = self.f.admission()
        admission['state'] = 'admitted'
        with self.store.transaction() as db:
            self.store._put(db, 'validator_admission:' + self.f.run_id, admission)
        with self.assertRaisesRegex(ValueError, 'dispatch not registered'):
            query.register_validator_host_execution(self.store, *self.f.fixture.actor(),
                self.f.ticket['execution_id'], self.f.ticket['lease'], run_id=self.f.run_id,
                turn_id='host-turn', item_id='host-item', command=self.command,
                start_producer=self.record['start_producer'], final_producer=self.record['final_producer'])


if __name__ == '__main__':
    unittest.main()
