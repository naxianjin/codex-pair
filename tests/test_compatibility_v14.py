"""CLI兼容性隔离回归；本轮仅编写，不执行候选或这些测试。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch, Mock

from scripts.pair_compatibility import compatibility_probe, CompatibilityMixin, CompatibilityMonitor, safe_preflight
from scripts.pair_core import PairError
from scripts.pair_producer import file_identity, SUPPORTED_VERSIONS
from scripts.pair_transport import AppServer
from tests import test_core
from tests.compatibility_fixture import MetadataObserver


SESSION = '00000000-0000-0000-0000-000000000001'


class CompatibilityProbeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='pair-compatibility-')
        self.root = Path(self.temporary.name)
        self.exe = self.root / 'codex.exe'
        self.exe.write_bytes(b'fixture native bytes; never executed')
        self.home = self.root / 'home'
        self.cwd = self.root / 'workspace'
        self.home.mkdir()
        self.cwd.mkdir()
        self.binding = {'session_uuid': SESSION, 'codex_home': str(self.home), 'cwd': str(self.cwd),
                        'model': 'gpt-6.1-sol', 'modelProvider': 'openai', 'binding_version': 1, 'backend': 'queue'}
        self.identity = file_identity(self.exe)
        self.trust = {'file': self.identity, 'source_reference': 'unit-fixture-previous-producer'}
        self.observer = MetadataObserver(self.binding, self.identity)

    def tearDown(self):
        self.temporary.cleanup()

    def cli(self, argv, **kwargs):
        if argv[1:] == ['--version']:
            output = 'codex-cli ' + SUPPORTED_VERSIONS[0] + '\n'
        elif argv[1:] == ['--help']:
            output = 'Commands:\n  queue        Queue a message\n  app-server   Run app server\n'
        elif argv[1:] == ['app-server', '--help']:
            output = 'Usage: codex app-server [OPTIONS]'
        else:
            raise AssertionError('只允许固定version/help只读查询')
        self.assertFalse(kwargs.get('shell', False))
        self.assertTrue(Path(kwargs['env']['CODEX_HOME']).samefile(self.home), 'CLI must use the exact account home')
        self.assertNotIn('OPENAI_API_KEY', kwargs['env'])
        self.assertNotIn('ACCESS_TOKEN', kwargs['env'])
        return subprocess.CompletedProcess(argv, 0, output, '')

    def probe(self, **kwargs):
        with patch('scripts.pair_compatibility.subprocess.run', side_effect=self.cli), \
             patch('scripts.pair_compatibility.verify_launch'):
            return compatibility_probe(self.exe, expected_binding=self.binding, trusted_executable=self.trust,
                                       observer=self.observer, **kwargs)

    def test_absent_trust_does_not_execute_even_named_codex_exe(self):
        with patch('scripts.pair_compatibility.subprocess.run', side_effect=AssertionError('不得执行未信任exe')):
            record = compatibility_probe(self.exe, expected_binding=self.binding)
        self.assertEqual(record['status'], 'not_verified')
        self.assertIn('trusted_executable_baseline_missing_or_changed', record['reasons'])
        self.assertFalse(record['model_started'])

    @unittest.skipUnless(os.name == 'nt', 'Windows native environment metadata')
    def test_windows_environment_query_never_starts_a_process(self):
        with patch('scripts.pair_compatibility.subprocess.run', side_effect=AssertionError('No implicit system process')):
            record = compatibility_probe(self.exe, expected_binding=self.binding)
        self.assertEqual(record['environment']['os'], 'Windows')
        self.assertEqual(record['environment']['machine_source'], 'python_platform_tag')
        self.assertFalse(record['model_started'])

    def test_file_hash_and_name_do_not_create_trust(self):
        with patch('scripts.pair_compatibility.subprocess.run', side_effect=AssertionError('摘要不能自证来源')):
            record = compatibility_probe(self.exe, expected_binding=self.binding,
                                         trusted_executable={'file': self.identity})
        self.assertEqual(record['capabilities']['executable_origin']['status'], 'not_verified')

    def test_readonly_preflight_does_not_claim_communication_or_permission(self):
        record = self.probe()
        self.assertEqual(record['status'], 'preflight_ready')
        self.assertEqual(record['communication']['status'], 'not_verified')
        self.assertFalse(record['persisted'])
        self.assertFalse(record['test_message_sent'])
        self.assertTrue(all(not item['execution_authorized'] for item in record['operations'].values()))
        self.assertEqual({call[0] for call in self.observer.calls}, {'thread/read', 'thread/turns/list'})

    def test_wokey_preflight_preserves_provider_without_official_account_query(self):
        self.binding['modelProvider'] = 'wokey'
        self.observer = MetadataObserver(self.binding, self.identity)
        record = self.probe()
        self.assertEqual(record['status'], 'preflight_ready')
        self.assertFalse(record['model_started'])
        self.assertEqual({call[0] for call in self.observer.calls}, {'thread/read', 'thread/turns/list'})

    def test_same_fingerprint_skips_repeated_cli_processes(self):
        first = self.probe()
        with (patch('scripts.pair_compatibility.subprocess.run', side_effect=AssertionError('相同指纹不重跑CLI')),
              patch('scripts.pair_compatibility.verify_launch')):
            second = compatibility_probe(self.exe, expected_binding=self.binding, trusted_executable=self.trust,
                                         observer=self.observer, previous=first)
        self.assertTrue(second['reused'])
        self.assertEqual(first['fingerprint'], second['fingerprint'])

    def test_changed_file_does_not_execute_against_old_trust(self):
        first = self.probe()
        self.exe.write_bytes(b'different fixture bytes')
        with patch('scripts.pair_compatibility.subprocess.run', side_effect=AssertionError('不得用旧信任执行新文件')):
            second = compatibility_probe(self.exe, expected_binding=self.binding, trusted_executable=self.trust, previous=first)
        self.assertFalse(second['reused'])
        self.assertEqual(second['status'], 'not_verified')

    def test_missing_path_does_not_substitute_path_search(self):
        with patch('scripts.pair_compatibility.subprocess.run', side_effect=AssertionError('不搜索替代程序')):
            record = compatibility_probe(self.root / 'missing.exe', expected_binding=self.binding, trusted_executable=self.trust)
        self.assertEqual(record['status'], 'not_verified')
        self.assertFalse(record['executable']['available'])

    def test_relative_or_cmd_path_is_not_native_preflight(self):
        for value in ('codex.exe', self.root / 'codex.cmd'):
            with self.subTest(value=str(value)), patch('scripts.pair_compatibility.subprocess.run') as launch:
                record = compatibility_probe(value)
                self.assertEqual(record['status'], 'not_verified')
                launch.assert_not_called()

    def test_model_change_requires_current_identity_reverification(self):
        self.observer.thread['model'] = 'gpt-6-astra'
        result = self.probe()
        self.assertEqual(result['capabilities']['identity']['status'], 'not_verified')
        self.assertEqual(result['operations']['new_delivery']['status'], 'not_verified')

    def test_an_old_observer_cannot_prove_new_executable_protocol(self):
        self.observer.producer_launch = None
        result = self.probe()
        self.assertEqual(result['capabilities']['identity']['status'], 'verified')
        self.assertEqual(result['capabilities']['observer_executable']['status'], 'not_verified')
        self.assertEqual(result['status'], 'not_verified')

    def test_absent_tool_evidence_is_not_approval_ready(self):
        self.observer.turns[0]['items'] = []
        result = self.probe()
        self.assertEqual(result['operations']['new_delivery']['status'], 'preflight_ready')
        self.assertEqual(result['operations']['review_settlement']['status'], 'not_verified')
        self.assertEqual(result['operations']['approval_execution']['status'], 'not_verified')

    def test_unknown_version_not_treated_as_close_enough(self):
        def newer(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 0, 'codex-cli 999.0.0\n', '') if argv[1:] == ['--version'] else self.cli(argv, **kwargs)
        with patch('scripts.pair_compatibility.subprocess.run', side_effect=newer), patch('scripts.pair_compatibility.verify_launch'):
            record = compatibility_probe(self.exe, expected_binding=self.binding, trusted_executable=self.trust, observer=self.observer)
        self.assertEqual(record['capabilities']['receipt_adapter']['status'], 'not_verified')
        self.assertEqual(record['operations']['new_delivery']['status'], 'not_verified')

    def test_timeout_does_not_return_stderr_or_retry_business(self):
        with patch('scripts.pair_compatibility.subprocess.run', side_effect=subprocess.TimeoutExpired('metadata', 5)):
            record = compatibility_probe(self.exe, expected_binding=self.binding, trusted_executable=self.trust)
        self.assertEqual(record['status'], 'not_verified')
        self.assertTrue(all(item.get('reason') == 'metadata_command_timeout' for item in record['observations']))

    def test_raw_metadata_and_auth_values_are_never_in_record(self):
        self.observer.thread['preview'] = 'private prompt'
        self.observer.thread['access_token'] = 'private token'
        result = json.dumps(self.probe())
        self.assertNotIn('private prompt', result)
        self.assertNotIn('private token', result)
        self.assertNotIn('fixture check', result)


class CompatibilityStoreTests(unittest.TestCase):
    tearDown = test_core.CoreTests.tearDown

    def setUp(self):
        test_core.CoreTests.setUp(self)
        self.assertIsInstance(self.store, CompatibilityMixin)
        with self.store.transaction() as db:
            proof = {'id': self.sessions['A'], 'cwd': self.directories['A'], 'model': 'gpt-6.1-sol', 'modelProvider': 'openai'}
            db.execute("UPDATE members SET proof=? WHERE id='A'", (json.dumps(proof),))

    def actor(self):
        return 'A', self.sessions['A'], self.directories['A']

    def test_refresh_without_trust_only_records_blocker(self):
        candidate = self.root / 'fake.exe'
        candidate.write_bytes(b'never executed')
        with patch('scripts.pair_compatibility.subprocess.run', side_effect=AssertionError('不能执行未信任文件')):
            first = self.store.refresh_cli_compatibility(*self.actor(), str(candidate))
            second = self.store.refresh_cli_compatibility(*self.actor(), str(candidate))
        self.assertTrue(first['persisted'])
        self.assertTrue(first['change_event_created'])
        self.assertFalse(second['change_event_created'])
        with self.store.transaction() as db:
            self.assertTrue(self.store._cli_compatibility_gate(db, 'A', 'new_delivery'))

    def test_unverified_probe_cannot_bind_new_execution(self):
        with self.assertRaisesRegex(PairError, 'compatibility not verified'):
            self.store.bind_cli_execution(*self.actor(), 'execution-one', 'a' * 64, operation='new_delivery')

    def test_bridge_preflight_without_baseline_does_not_bootstrap_unknown_exe(self):
        executable = self.root / 'fixture.exe'
        executable.write_bytes(b'never run')
        with (patch('scripts.pair_compatibility.AppServer', side_effect=AssertionError('不启动未知CLI')),
              patch('scripts.pair_compatibility.subprocess.run', side_effect=AssertionError('不运行未知exe'))):
            result = safe_preflight(self.store, str(executable))
        self.assertFalse(result['all_delivery_preflights_ready'])
        self.assertFalse(result['new_work_started'])
        self.assertIsNone(self.store.setting('cli_trusted_executable:A'))

    def test_monitor_limits_unchanged_checks_and_detects_file_change(self):
        executable = self.root / 'fixture.exe'
        executable.write_bytes(b'first fixture')
        monitor = CompatibilityMonitor(self.store, str(executable), member_id='A')
        with patch('scripts.pair_compatibility.time.monotonic', return_value=100):
            first = monitor.poll()
            second = monitor.poll()
            executable.write_bytes(b'changed larger fixture')
            changed = monitor.poll()
        self.assertTrue(first['checked'])
        self.assertFalse(second['checked'])
        self.assertTrue(changed['checked'])

    def original_observer_fixture(self, executable):
        actor = self.store.member('A')
        observer = Mock(spec=AppServer)
        observer.metadata_only = True
        observer.backend = 'managed'
        observer.producer_launch = {'file': file_identity(executable), 'version': SUPPORTED_VERSIONS[0], 'process': {'pid': 123}}
        observer.identity = {'codexHome': actor['codex_home']}
        observer.read_thread.return_value = json.loads(actor['proof'])
        return observer

    def test_json_true_is_not_original_executable_evidence(self):
        with self.assertRaisesRegex(PairError, 'Owned readonly observer required'):
            self.store._register_original_cli_baseline(*self.actor(), {'verified': True}, executable='fake.exe')

    def test_original_process_baseline_is_idempotent_and_cannot_replace_updated_exe(self):
        executable = self.root / 'trusted-fixture.exe'
        executable.write_bytes(b'fixture old executable, never run')
        observer = self.original_observer_fixture(executable)
        with patch('scripts.pair_compatibility.verify_launch'):
            first = self.store._register_original_cli_baseline(*self.actor(), observer, executable=executable)
            again = self.store._register_original_cli_baseline(*self.actor(), observer, executable=executable)
        self.assertTrue(first['registered'])
        self.assertTrue(again['duplicate'])
        saved = self.store.setting('cli_trusted_executable:A')
        executable.write_bytes(b'fixture new executable, never run')
        observer = self.original_observer_fixture(executable)
        with patch('scripts.pair_compatibility.verify_launch'), self.assertRaisesRegex(PairError, 'replacement requires review'):
            self.store._register_original_cli_baseline(*self.actor(), observer, executable=executable)
        self.assertEqual(self.store.setting('cli_trusted_executable:A'), saved)

    def test_wrong_original_session_does_not_register_baseline(self):
        executable = self.root / 'fixture.exe'
        executable.write_bytes(b'fixture, never run')
        observer = self.original_observer_fixture(executable)
        observer.read_thread.return_value['id'] = self.sessions['B']
        with patch('scripts.pair_compatibility.verify_launch'), self.assertRaisesRegex(PairError, 'binding not verified'):
            self.store._register_original_cli_baseline(*self.actor(), observer, executable=executable)
        self.assertIsNone(self.store.setting('cli_trusted_executable:A'))

    def test_missing_original_executable_preserves_unknown_and_no_replay(self):
        key = 'cli_execution:old-execution'
        original = {'execution_id': 'old-execution', 'member': 'A', 'session': self.sessions['A'],
                    'binding_version': 1, 'version': 'original-version', 'turn_id': 'original-turn',
                    'contract_digest': 'a' * 64, 'operation': 'review_settlement',
                    'executable': {'path': str(self.root / 'removed.exe')}}
        self.store.setting(key, original)
        result = self.store.inspect_cli_execution(*self.actor(), 'old-execution')
        self.assertEqual(result['status'], 'needs_validation')
        self.assertEqual(result['result'], 'unknown')
        self.assertFalse(result['replayed'])
        self.assertFalse(result['new_cli_substituted'])
        self.assertEqual(self.store.setting(key), original)


if __name__ == '__main__':
    unittest.main()
