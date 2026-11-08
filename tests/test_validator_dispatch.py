"""真实临时 Store 验证派发边界；只替换外部官方执行器，不运行 VM。"""
import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from scripts import pair_validator_dispatch as dispatch
from scripts import pair_validator_host as host
from scripts.pair_core import FileLock, PairError
from scripts.pair_transport import DeliveryUnknown
from tests import test_validator_admission


class ValidatorDispatchTests(unittest.TestCase):
    def setUp(self):
        self.case = test_validator_admission.ValidatorAdmissionTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.store, self.actor = self.case.store, self.case.fixture.actor()
        self.ticket = self.case.ticket
        self.run_id = 'b' * 32
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        patcher = patch.object(host, '_RUN_ROOT', self.root / 'runs')
        patcher.start()
        self.addCleanup(patcher.stop)
        self.servers, self.sent = [], []
        self.before_send = self.during_wait = None
        self.outcome = 'success'
        self.launch = {'version': dispatch._VERSION,
            'file': {'path': str(self.root / 'codex.exe'), 'sha256': dispatch._EXECUTABLE_SHA256},
            'process': {'pid': 1234, 'created': 987654321, 'path': str(self.root / 'codex.exe')}}
        owner = self

        class Server(dispatch._CommandServer):
            def __init__(self, **options):
                owner.assertEqual(list(Path(options['codex_home']).iterdir()), [])
                owner.assertTrue(options['track_producer'])
                owner.assertTrue(options['experimental_api'])
                owner.assertEqual(options['backend'], 'managed')
                self.connection_id = 'fixed-fixture-connection'
                self.producer_launch = copy.deepcopy(owner.launch)
                self._condition = threading.Condition()
                self._next_id, self._pending, self._dead = 1, {}, False
                self.closed = False
                owner.servers.append(self)

            def _write(self, request):
                if owner.before_send:
                    owner.before_send(self, request)
                record = owner.record()
                owner.assertEqual(record['state'], 'dispatching')
                owner.assertEqual(record['start']['request_id'], request['id'])
                owner.assertEqual(record['start']['command'], request['params'])
                owner.assertEqual(record['start']['producer_start'], self.producer_launch)
                owner.assertFalse(record['start']['native_child_start_verified'])
                owner.assertEqual(request['method'], 'command/exec')
                with owner.assertRaises(PairError), FileLock(owner.store.control_lock):
                    owner.fail('Command send outside admission control boundary')
                params = request['params']
                directory = Path(params['command'][params['command'].index('-InputDirectory') + 1])
                with owner.store.transaction() as db:
                    admission = owner.store._option(db, 'validator_admission:' + owner.run_id)
                owner.assertEqual(json.loads((directory / 'request.json').read_bytes()),
                                  admission['snapshot']['request'])
                for name in host._WORKER_SOURCES:
                    owner.assertEqual((directory / name).read_bytes(),
                                      (host._PACKAGE_ROOT / 'scripts' / name).read_bytes())
                owner.sent.append(copy.deepcopy(request))
                if owner.outcome == 'write_unknown':
                    raise OSError('Isolated write outcome unavailable')
                if owner.outcome == 'disconnect':
                    self._dead = True
                    return
                result = {'exitCode': 7 if owner.outcome == 'nonzero' else 0,
                          'stdout': '', 'stderr': ''}
                response = {'id': request['id'], 'result': result}
                if owner.outcome == 'rpc_error':
                    response = {'id': request['id'], 'error': {'code': -32000, 'message': 'Isolated rejection'}}
                if owner.outcome == 'wrong_id':
                    response['id'] += 1
                if owner.outcome == 'bad_exit':
                    response['result']['exitCode'] = False
                self._pending[request['id']] = response

            def wait_command(self, *args, **options):
                with FileLock(owner.store.control_lock):
                    pass
                if owner.during_wait:
                    owner.during_wait(self)
                return super().wait_command(*args, **options)

            def close(self):
                self.closed = True

        self.patchers = [patch.object(dispatch, '_CommandServer', Server),
            patch.object(dispatch, 'file_identity', return_value=self.launch['file']),
            patch.object(dispatch, 'verify_launch')]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_command(self, **options):
        arguments = dict(executable=self.launch['file']['path'], codex_home=self.root / 'empty-home',
                         run_id=self.run_id)
        arguments.update(options)
        return dispatch.dispatch_owned_validator_run(self.store, *self.actor,
            self.ticket['execution_id'], self.ticket['lease'],
            **arguments)

    def record(self):
        with self.store.transaction() as db:
            return self.store._option(db, dispatch._KEY + self.run_id)

    def parent(self):
        with self.store.transaction() as db:
            return (dict(db.execute('SELECT * FROM auxiliary_reports').fetchone()),
                    dict(db.execute('SELECT * FROM review_stages').fetchone()))

    def test_fixed_command_start_precedes_send_and_terminal_preserves_parent(self):
        before = self.parent()
        result = self.run_command()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(result['state'], 'terminal')
        self.assertTrue(result['command_succeeded'])
        self.assertFalse(result['formal'])
        self.assertFalse(result['formal_checks_verified'])
        self.assertTrue(self.servers[0].closed)
        record = self.record()
        self.assertEqual(record['start']['authorizer']['session'], self.actor[1])
        self.assertNotIn('turn_id', record['start'])
        self.assertNotIn('item_id', record['start'])
        self.assertEqual(record['final']['producer_final'], record['start']['producer_start'])
        self.assertEqual(self.parent(), before)
        self.assertFalse(self.run_command()['dispatched'])
        self.assertEqual(len(self.sent), 1)

    def test_disconnect_and_write_unknown_remain_unknown_without_replay(self):
        for outcome in ('disconnect', 'write_unknown'):
            self.outcome = outcome
            self.run_id = ('c' if outcome == 'disconnect' else 'd') * 32
            home = self.root / ('home-' + outcome)
            result = self.run_command(codex_home=home)
            self.assertEqual(result['state'], 'unknown')
            self.assertIsNone(result['controller_exit_code'])
            self.assertFalse(result['command_succeeded'])
            self.assertIsNone(self.record()['final']['producer_final'])
            count = len(self.sent)
            self.assertFalse(self.run_command(codex_home=home)['dispatched'])
            self.assertEqual(len(self.sent), count)

    def test_control_and_authorization_changes_save_only_reconciliation_facts(self):
        def changed(_):
            with self.store.transaction() as db:
                self.store._put(db, 'control_generation_v14', self.store._control_generation(db) + 1)
            self.store.delegation_policy(*self.actor, '隔离授权变化', enabled=True)
        self.during_wait = changed
        result = self.run_command()
        self.assertEqual(set(result['controls_changed']), {'control_generation', 'authorization_version'})
        self.assertTrue(result['reconciliation_only'])
        self.assertFalse(result['formal_checks_verified'])

    def test_pause_during_execution_does_not_block_or_promote(self):
        self.during_wait = lambda _: self.store.request_pause(
            *self.actor, reason='隔离执行中暂停', idempotency_key='owned-pause')
        result = self.run_command()
        self.assertEqual(result['state'], 'terminal')
        self.assertIn('Collaboration not running', self.record()['final']['control_block'])
        self.assertFalse(result['formal'])

    def test_candidate_change_rejects_final_and_preserves_dispatch_claim(self):
        self.during_wait = lambda _: self.case.change(
            'auxiliary_candidates', 'candidate_digest', 'changed', row=self.case.candidate['id'])
        with self.assertRaisesRegex(PairError, 'Stale audit candidate'):
            self.run_command()
        self.assertEqual(self.record()['state'], 'dispatching')
        self.assertIsNone(self.record()['final'])
        self.assertTrue(self.servers[0].closed)

    def test_current_tool_change_before_send_is_rejected(self):
        real = dispatch._current_tools
        calls = 0
        def tools():
            nonlocal calls
            calls += 1
            shell, identity = real()
            return shell, dict(identity, host_shell='f' * 64) if calls == 2 else identity
        with patch.object(dispatch, '_current_tools', side_effect=tools):
            with self.assertRaisesRegex(PairError, 'admission changed'):
                self.run_command()
        self.assertEqual(self.sent, [])
        self.assertIsNone(self.record())

    def test_wrong_connection_producer_request_or_exit_type_cannot_save_terminal(self):
        for outcome in ('wrong_id', 'bad_exit'):
            self.outcome = outcome
            self.run_id = ('e' if outcome == 'wrong_id' else 'f') * 32
            with self.subTest(outcome=outcome), self.assertRaisesRegex(ValueError, 'terminal'):
                self.run_command(codex_home=self.root / outcome)
            self.assertIsNone(self.record()['final'])
        self.outcome = 'success'
        self.run_id = '9' * 32
        self.during_wait = lambda server: setattr(server, 'connection_id', 'changed')
        with self.assertRaisesRegex(ValueError, 'connection changed'):
            self.run_command(codex_home=self.root / 'changed-connection')
        self.assertIsNone(self.record()['final'])

    def test_nonzero_and_rpc_error_are_terminal_facts_without_success(self):
        for outcome in ('nonzero', 'rpc_error'):
            self.outcome = outcome
            self.run_id = ('7' if outcome == 'nonzero' else '8') * 32
            result = self.run_command(codex_home=self.root / outcome)
            self.assertEqual(result['state'], 'rejected' if outcome == 'rpc_error' else 'terminal')
            self.assertFalse(result['command_succeeded'])
            self.assertFalse(result['formal_checks_verified'])

    def test_missing_changed_or_conflicting_start_never_accepts_final(self):
        self.run_command()
        record = self.record()
        start, final = record['start'], record['final']
        permission = {'snapshot_digest': start['binding']['admission_snapshot_digest'],
                      'controls_changed': [], 'control_block': None}
        def save(response=final['response'], producer=final['producer_final']):
            return dispatch._save_final(self.store, start, permission,
                response=response, unknown=None, producer_final=producer,
                connection_id=start['connection_id'])
        changed = copy.deepcopy(final['response'])
        changed['result']['exitCode'] = 3
        with self.assertRaisesRegex(ValueError, 'terminal conflict'):
            save(changed)
        wrong_producer = copy.deepcopy(final['producer_final'])
        wrong_producer['process']['created'] += 1
        with self.assertRaisesRegex(ValueError, 'producer or connection changed'):
            save(producer=wrong_producer)
        forged = copy.deepcopy(record)
        forged['start']['connection_id'] = 'rebound'
        with self.store.transaction() as db:
            self.store._put(db, dispatch._KEY + self.run_id, forged)
        with self.assertRaisesRegex(ValueError, 'original start missing or changed'):
            save()
        with self.store.transaction() as db:
            self.store._put(db, dispatch._KEY + self.run_id, None)
        with self.assertRaisesRegex(ValueError, 'original start missing'):
            save()

    def test_unknown_build_existing_home_and_pause_cannot_send_command(self):
        with patch.object(dispatch, 'file_identity', return_value={'sha256': 'f' * 64}):
            with self.assertRaisesRegex(ValueError, 'Exact official'):
                self.run_command()
        self.assertEqual(self.servers, [])
        (self.root / 'empty-home').mkdir()
        with self.assertRaises(FileExistsError):
            self.run_command()
        self.assertEqual(self.servers, [])
        self.store.request_pause(*self.actor, reason='派发前暂停', idempotency_key='before-owned')
        with self.assertRaisesRegex(PairError, 'not running'):
            self.run_command(run_id='6' * 32)
        self.assertEqual(self.sent, [])

    def test_owned_server_does_not_inherit_credentials_or_external_executor(self):
        inherited = {'CODEX_THREAD_ID': 'original-thread', 'CODEX_EXEC_SERVER_URL': 'external',
                     'CODEX_AUTH_JSON': 'private', 'OPENAI_API_KEY': 'secret',
                     'CUSTOM_ACCESS_TOKEN': 'secret'}
        with patch.dict(os.environ, inherited), patch.object(dispatch.subprocess, 'Popen') as start:
            dispatch._start_official_process(['fixed.exe'], env={'CODEX_HOME': str(self.root)})
        environment = start.call_args.kwargs['env']
        self.assertEqual(environment['CODEX_HOME'], str(self.root))
        self.assertFalse(set(inherited) & set(environment))


if __name__ == '__main__':
    unittest.main()
