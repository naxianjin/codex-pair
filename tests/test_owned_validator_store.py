"""临时真实 Store 的公开接线测试；受限 VM 回收仅在外部边界替换。"""
import copy
import hashlib
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import pair_next_commands as commands
from scripts import pair_validator_dispatch as dispatch
from scripts.pair_core import PairError
from scripts.pair_stages import digest
from scripts.pair_validator_receipts import read_validator_host_receipt
from tests import test_validator_dispatch as dispatch_tests


class OwnedValidatorStoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = dispatch_tests.ValidatorDispatchTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store, self.actor = self.fixture.store, self.fixture.actor
        self.ticket, self.run_id = self.fixture.ticket, self.fixture.run_id
        self.calls = 0
        patcher = patch.object(dispatch.host, 'reconcile_admitted_validator_run',
                               side_effect=self.restricted_result)
        self.boundary = patcher.start()
        self.addCleanup(patcher.stop)

    def dispatch_run(self):
        return self.store.dispatch_owned_validator_run(*self.actor, self.ticket['execution_id'],
            self.ticket['lease'], executable=self.fixture.launch['file']['path'],
            codex_home=self.fixture.root / 'empty-home', run_id=self.run_id)

    def reconcile(self, **changes):
        options = {'run_id': self.run_id}
        options.update(changes)
        return self.store.reconcile_owned_validator_run(*self.actor,
            self.ticket['execution_id'], self.ticket['lease'], **options)

    def restricted_result(self, store, member, session, cwd, execution, lease, *, run_id):
        """这里只替换 VM 文件读取；仍调用实际 Store 校验和保存。"""
        self.calls += 1
        with store.transaction() as db:
            admission = store._option(db, 'validator_admission:' + run_id)
        request = admission['snapshot']['request']
        empty = hashlib.sha256(b'').hexdigest()
        native = {'ExitCode': 0, 'ActiveProcesses': 0, 'TimedOut': False,
                  'HostObservation': {'run_id': run_id}}
        artifact = {'binding': request['binding'], 'files': request['files'],
            'formal_checks_verified': False, 'validators_succeeded': True,
            'results': [{'validator': name, 'exit_code': 0, 'stdout_sha256': empty,
                        'stderr_sha256': empty, 'stderr': ''}
                       for name in ('validate-findings.cjs', 'validate-coverage-ledger.cjs')]}
        saved = store.record_validator_run(member, session, cwd, execution, lease, run_id,
            request, native, artifact, {key: value for key, value in admission['snapshot']['tool_identity'].items()
                                      if key != 'host_shell'})
        return dict(saved, admission_snapshot_digest=admission['snapshot_digest'],
                    remaining_checks=['fixture VM boundary; not an actual restricted run'])

    def record(self, prefix=dispatch._KEY):
        with self.store.transaction() as db:
            return self.store._option(db, prefix + self.run_id)

    def write(self, value, prefix=dispatch._KEY):
        with self.store.transaction() as db:
            self.store._put(db, prefix + self.run_id, value)

    def test_public_dispatch_and_same_run_consumption_preserve_parent_and_are_idempotent(self):
        parent = self.fixture.parent()
        self.assertTrue(self.dispatch_run()['command_succeeded'])
        first = self.reconcile()
        self.assertFalse(first['formal'])
        self.assertFalse(first['formal_checks_verified'])
        self.assertFalse(first['consumption_duplicate'])
        record = self.record(dispatch._CONSUMPTION_KEY)
        self.assertEqual(record['host_record_digest'], digest(self.record()))
        self.assertEqual(record['validator_record_digest'], digest(self.record('validator_run:')))
        self.assertEqual(record['binding']['execution_id'], self.ticket['execution_id'])
        self.assertEqual(self.reconcile()['consumption_digest'], first['consumption_digest'])
        self.assertTrue(self.reconcile()['consumption_duplicate'])
        self.assertEqual(self.fixture.parent(), parent)
        self.assertFalse(self.dispatch_run()['dispatched'])
        self.assertEqual(len(self.fixture.sent), 1)

    def test_official_command_overrides_inherited_windows_module_key(self):
        self.dispatch_run()
        environment = self.record()['start']['command']['env']
        self.assertEqual(set(environment), {'PSMODULEPATH'})
        self.assertTrue(environment['PSMODULEPATH'].lower().endswith(
            r'windowspowershell\v1.0\modules'))
        self.assertTrue(self.reconcile()['saved'])

    def test_unknown_rejected_nonzero_and_missing_start_do_not_reach_vm_consumer(self):
        self.dispatch_run()
        original = self.record()
        cases = [None, dict(original, state='unknown'), dict(original, state='rejected'),
                 dict(original, state='dispatching')]
        changed = copy.deepcopy(original)
        changed['final']['response']['result']['exitCode'] = 2
        cases.append(changed)
        for value in cases:
            self.write(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.reconcile()
        self.assertEqual(self.calls, 0)
        self.assertIsNone(self.record(dispatch._CONSUMPTION_KEY))

    def test_wrong_binding_fixed_command_connection_request_and_producer_are_rejected(self):
        self.dispatch_run()
        original = self.record()
        cases = [('start', 'source_kind', 'original_model_tool'),
                 ('start', 'run_id', 'c' * 32), ('start', 'request_id', True),
                 ('start', 'native_child_start_verified', True),
                 ('final', 'connection_id', 'other'),
                 ('final', 'producer_final', None)]
        for section, key, value in cases:
            changed = copy.deepcopy(original)
            changed[section][key] = value
            changed['start_digest'] = digest(changed['start'])
            self.write(changed)
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.reconcile()
        changed = copy.deepcopy(original)
        changed['start']['command']['command'][-1] = 'c' * 32
        changed['start_digest'] = digest(changed['start'])
        self.write(changed)
        with self.assertRaisesRegex(ValueError, 'fixed command'):
            self.reconcile()
        changed = copy.deepcopy(original)
        changed['final']['response']['id'] += 1
        self.write(changed)
        with self.assertRaisesRegex(ValueError, 'exact response'):
            self.reconcile()
        self.assertEqual(self.calls, 0)

    def test_old_unrecorded_terminal_connection_cannot_be_retroactively_consumed(self):
        self.dispatch_run()
        record = self.record()
        del record['final']['connection_id']
        self.write(record)
        with self.assertRaisesRegex(ValueError, 'terminal connection'):
            self.reconcile()
        self.assertEqual(self.calls, 0)

    def test_changed_source_tool_or_actor_rejects_before_consumption(self):
        self.dispatch_run()
        original = self.record()
        with patch.object(dispatch, '_current_tools', side_effect=lambda: (
                self.fixture.launch['file']['path'], {'node': 'f' * 64})):
            with self.assertRaises((PairError, ValueError)):
                self.reconcile()
        with self.assertRaises(PairError):
            self.store.reconcile_owned_validator_run(*self.fixture.case.fixture.actor('B'),
                self.ticket['execution_id'], self.ticket['lease'], self.run_id)
        self.assertEqual(self.record(), original)
        self.assertEqual(self.calls, 0)

    def test_pause_and_control_change_allow_facts_without_promoting_parent(self):
        self.dispatch_run()
        parent = self.fixture.parent()
        with self.store.transaction() as db:
            self.store._put(db, 'control_generation_v14', self.store._control_generation(db) + 1)
        self.store.request_pause(*self.actor, reason='回收夹具暂停', idempotency_key='consume-pause')
        result = self.reconcile()
        self.assertIn('control_generation', result['controls_changed'])
        self.assertTrue(result['reconciliation_only'])
        self.assertFalse(result['formal_checks_verified'])
        self.assertEqual(self.fixture.parent(), parent)

    def test_changed_command_during_vm_recovery_cannot_save_consumption(self):
        self.dispatch_run()
        real = self.restricted_result
        def changed(*args, **kwargs):
            saved = real(*args, **kwargs)
            record = self.record()
            record['final']['response']['result']['stdout'] = 'changed after VM read'
            self.write(record)
            return saved
        self.boundary.side_effect = changed
        with self.assertRaisesRegex(ValueError, 'changed during'):
            self.reconcile()
        self.assertIsNone(self.record(dispatch._CONSUMPTION_KEY))

    def test_conflicting_prior_consumption_and_wrong_vm_run_are_rejected(self):
        self.dispatch_run()
        self.reconcile()
        record = self.record(dispatch._CONSUMPTION_KEY)
        record['validator_record_digest'] = 'f' * 64
        self.write(record, dispatch._CONSUMPTION_KEY)
        with self.assertRaisesRegex(ValueError, 'consumption conflict'):
            self.reconcile()
        self.write(None, dispatch._CONSUMPTION_KEY)
        validator = self.record('validator_run:')
        validator['native']['HostObservation']['run_id'] = 'c' * 32
        self.write(validator, 'validator_run:')
        self.boundary.side_effect = lambda *args, **kwargs: {'remaining_checks': []}
        with self.assertRaisesRegex(ValueError, 'restricted result binding'):
            self.reconcile()

    def test_recovery_accepts_only_run_reference_and_cannot_satisfy_old_session_receipt(self):
        self.dispatch_run()
        self.reconcile()
        with self.assertRaises(TypeError):
            self.reconcile(response={})
        with self.assertRaises(PairError):
            self.reconcile(run_id='../run')
        with self.assertRaisesRegex(ValueError, 'registration unavailable'):
            read_validator_host_receipt(self.store, *self.actor, self.ticket['execution_id'],
                self.ticket['lease'], run_id=self.run_id)

    def test_cli_reuses_exact_executable_and_signature_constraints(self):
        self.assertEqual(commands.COMMANDS['dispatch-owned-validator-run'], 'dispatch_owned_validator_run')
        self.assertEqual(commands.COMMANDS['reconcile-owned-validator-run'], 'reconcile_owned_validator_run')
        payload = self.fixture.root / 'cli-input.json'
        payload.write_text(json.dumps({'execution_id': self.ticket['execution_id'],
            'lease': self.ticket['lease'], 'run_id': self.run_id,
            'codex_home': str(self.fixture.root / 'empty-home')}), encoding='utf-8')
        args = SimpleNamespace(command='dispatch-owned-validator-run', member=self.actor[0],
            session=self.actor[1], worktree=self.actor[2], input_file=str(payload), input_stdin=False,
            codex=self.fixture.launch['file']['path'])
        cli = SimpleNamespace(validate_actor=lambda store, args: None)
        self.assertTrue(commands.execute(self.store, args, cli)['command_succeeded'])
        args.command = 'reconcile-owned-validator-run'
        payload.write_text(json.dumps({'execution_id': self.ticket['execution_id'],
            'lease': self.ticket['lease'], 'run_id': self.run_id}), encoding='utf-8')
        self.assertFalse(commands.execute(self.store, args, cli)['formal_checks_verified'])
        payload.write_text(json.dumps({'execution_id': self.ticket['execution_id'],
            'lease': self.ticket['lease'], 'run_id': self.run_id, 'response': {}}), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Invalid command fields'):
            commands.execute(self.store, args, cli)


if __name__ == '__main__':
    unittest.main()
