"""DeepSeek 专项离线验证；不读取真实凭据，不启动 CLI 或付费 API。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import pair_windows
from scripts.pair_core import Daemon, Store
from scripts.pair_transport import (AppServer, QueueTransport, TransportPool,
    TransportError, RpcError, canonical_path, control_environment)


class DeepSeekTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='pair-deepseek-unit-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / 'project'
        self.project.mkdir()
        self.homes = {}
        self.members = {}
        for index, name in enumerate(('A', 'B'), 1):
            home = self.root / ('home-' + name)
            home.mkdir()
            cwd = self.root / ('cwd-' + name)
            cwd.mkdir()
            self.homes[name] = canonical_path(home)
            self.members[name] = dict(id=name, backend='queue', owned=False,
                session_uuid='00000000-0000-0000-0000-' + str(index).zfill(12),
                cwd=canonical_path(cwd), codex_home=self.homes[name])
        self.store = Store.initialize('deepseek-tests', str(self.project), 'a'*40,
                                      home=self.homes['A'])

    def thread(self, name='A', provider='deepseek'):
        member = self.members[name]
        thread = dict(id=member['session_uuid'], cwd=member['cwd'],
                      model='deepseek-flash', ephemeral=False)
        if provider is not None:
            thread['modelProvider'] = provider
        return thread

    def join(self, name, provider='deepseek', **extra):
        member = self.members[name]
        proof = self.thread(name, provider)
        proof.update(extra)
        return self.store.join(name, member['session_uuid'], member['cwd'],
            backend='queue', owned=False, proof=proof,
            member_codex_home=member['codex_home'])

    def observer(self, name='A', provider='deepseek'):
        client = AppServer.__new__(AppServer)
        client.backend = 'managed'
        client.metadata_only = True
        client.read_thread = Mock(return_value=self.thread(name, provider))
        return client

    def test_current_window_uses_provider_metadata_without_account_rpc(self):
        for provider in ('openai', 'deepseek'):
            with self.subTest(provider=provider):
                client = self.observer(provider=provider)
                client.rpc = Mock(side_effect=AssertionError('account RPC forbidden'))
                with patch.dict(os.environ, {'CODEX_THREAD_ID': self.members['A']['session_uuid'],
                                            'CODEX_HOME': self.homes['A']}), \
                     patch('scripts.pair_windows.AppServer', return_value=client) as constructor:
                    result = pair_windows.current_window()
                self.assertEqual(result['proof']['modelProvider'], provider)
                self.assertEqual(result['session_uuid'], self.members['A']['session_uuid'])
                constructor.assert_called_once()
                self.assertTrue(constructor.call_args.kwargs['metadata_only'])
                client.rpc.assert_not_called()

    def test_current_window_rejects_unknown_or_missing_provider_despite_model(self):
        for provider in (None, 'compatible', 'DeepSeek'):
            with self.subTest(provider=provider):
                client = self.observer(provider=provider)
                with patch.dict(os.environ, {'CODEX_THREAD_ID': self.members['A']['session_uuid'],
                                            'CODEX_HOME': self.homes['A']}), \
                     patch('scripts.pair_windows.AppServer', return_value=client), \
                     self.assertRaises(TransportError):
                    pair_windows.current_window()

    def test_store_persists_only_minimal_proof_including_nested_git(self):
        marker = 'FAKE_PRIVATE_VALUE'
        self.join('A', preview=marker, extra={'password': marker}, turns=[marker],
                  authorization=marker, arbitrary={'nested': marker},
                  git={'path': self.members['A']['cwd'], 'common_dir': str(self.project),
                       'branch': 'main', 'valid': True, 'extra': marker, 'api_key': marker})
        raw = self.store.member('A')['proof']
        proof = json.loads(raw)
        self.assertNotIn(marker, raw)
        self.assertEqual(set(proof), {'id', 'cwd', 'modelProvider', 'model', 'ephemeral', 'git'})
        self.assertEqual(set(proof['git']), {'path', 'common_dir', 'branch', 'valid'})
        self.assertEqual(proof['modelProvider'], 'deepseek')

    def test_legacy_proof_remains_unchanged_and_metadata_is_missing(self):
        self.join('A')
        legacy = json.dumps(dict(id=self.members['A']['session_uuid'],
                                cwd=self.members['A']['cwd'], legacy_extra='historical'))
        with self.store.transaction() as db:
            db.execute("UPDATE members SET proof=? WHERE id='A'", (legacy,))
        member = self.store.status()['members'][0]
        self.assertEqual(member['connection']['metadata_status'], 'missing')
        self.assertIsNone(member['connection']['provider'])
        self.assertEqual(member['capabilities']['automatic_communication'], 'unverified')
        self.assertEqual(self.store.member('A')['proof'], legacy)

    def test_provider_change_prevents_queue_submission(self):
        member = dict(self.members['A'], proof=self.thread(provider='deepseek'))
        transport = QueueTransport.__new__(QueueTransport)
        transport.observer = self.observer(provider='openai')
        with patch('scripts.pair_transport.subprocess.run') as run, \
             self.assertRaisesRegex(TransportError, 'provider changed'):
            transport.start(member, {'envelope': '{}'})
        run.assert_not_called()

    def test_control_environment_filters_credentials_without_mutating_parent(self):
        fake = dict(OPENAI_API_KEY='fake-openai', DEEPSEEK_API_KEY='fake-deepseek',
                    CUSTOM_API_KEY='fake-custom', service_auth_token='fake-auth',
                    OPENAI_BASE_URL='https://example.invalid', CODEX_THREAD_ID='parent-thread',
                    CODEX_HOME=self.homes['A'], PATH='keep-path')
        with patch.dict(os.environ, fake, clear=True):
            parent = dict(os.environ)
            child = control_environment(self.homes['B'])
            self.assertEqual(dict(os.environ), parent)
        self.assertEqual(child, {'CODEX_HOME': self.homes['B'], 'PATH': 'keep-path'})

    def test_metadata_process_creation_receives_filtered_environment(self):
        captured = {}
        def refuse_process(*args, **kwargs):
            captured.update(kwargs)
            raise OSError('fake spawn blocked')
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fake-parent',
                                    'DEEPSEEK_API_KEY': 'fake-ds',
                                    'CUSTOM_API_KEY': 'fake-custom', 'CODEX_THREAD_ID': 'parent'}), \
             patch('scripts.pair_transport.codex_command', return_value=['fake-codex']), \
             self.assertRaisesRegex(TransportError, 'fake spawn blocked'):
            AppServer(codex_home=self.homes['B'], metadata_only=True,
                      process_factory=refuse_process)
        for key in ('OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'CUSTOM_API_KEY', 'CODEX_THREAD_ID'):
            self.assertNotIn(key, captured['env'])
        self.assertEqual(captured['env']['CODEX_HOME'], self.homes['B'])

    def test_queue_subprocess_does_not_receive_parent_credentials(self):
        member = self.members['B']
        response = subprocess.CompletedProcess([], 0,
            ('Queued message 00000000-0000-0000-0000-000000000009 for thread '
             + member['session_uuid']).encode(), b'')
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fake-openai',
                                    'DEEPSEEK_API_KEY': 'fake-ds',
                                    'CUSTOM_API_KEY': 'fake-custom', 'CODEX_THREAD_ID': 'parent'}), \
             patch('scripts.pair_transport.codex_command', return_value=['fake-codex']), \
             patch('scripts.pair_transport.AppServer') as observer, \
             patch('scripts.pair_transport.subprocess.run', return_value=response) as run:
            transport = QueueTransport(codex_home=self.homes['B'])
            receipt = transport.start(member, {'envelope': '{}'})
            self.assertTrue(observer.call_args.kwargs['metadata_only'])
            for key in ('OPENAI_API_KEY', 'DEEPSEEK_API_KEY', 'CUSTOM_API_KEY', 'CODEX_THREAD_ID'):
                self.assertNotIn(key, run.call_args.kwargs['env'])
                self.assertIn(key, os.environ)
            self.assertEqual(receipt['remote_status'], 'queued')
            self.assertIsNone(receipt['turn_id'])

    def test_metadata_rpc_rejects_inference_and_mutation_before_write(self):
        client = self.observer()
        client._write = Mock()
        for method in ('turn/start', 'thread/start', 'thread/resume', 'config/write'):
            with self.subTest(method=method), self.assertRaises(TransportError):
                client.rpc(method, {})
        client._write.assert_not_called()

    def test_metadata_high_level_methods_cannot_bypass_rpc_guard(self):
        client = self.observer()
        client.rpc = Mock()
        client.validate = Mock()
        calls = [lambda: client.start(self.members['A'], {'envelope': '{}'}),
                 lambda: client.resume(self.members['A']),
                 lambda: client.create_session(str(self.project))]
        for call in calls:
            with self.assertRaises(TransportError):
                call()
        client.rpc.assert_not_called()
        client.validate.assert_not_called()

    def test_status_keeps_queue_turn_business_and_handshake_separate(self):
        self.join('A', 'openai')
        self.join('B')
        message = self.store.send('A', 'B', 'offline evidence')
        def capabilities():
            return next(m for m in self.store.status()['members'] if m['id']=='B')['capabilities']
        self.assertEqual(capabilities()['queue_delivery'], 'unverified')
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='queued' WHERE id=?", (message['id'],))
        self.assertEqual(capabilities()['queue_delivery'], 'accepted')
        self.assertEqual(capabilities()['turn_completion'], 'unverified')
        self.assertEqual(capabilities()['business_processing'], 'unverified')
        claim = self.store.claim('B', self.members['B']['session_uuid'], self.members['B']['cwd'], message_id=message['id'])
        self.store.complete('B', self.members['B']['session_uuid'], self.members['B']['cwd'],
                            message['id'], claim['claim_token'], 'offline completed')
        self.assertEqual(capabilities()['business_processing'], 'verified')
        self.assertEqual(capabilities()['turn_completion'], 'unverified')
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET remote_status='completed' WHERE id=?", (message['id'],))
        self.assertEqual(capabilities()['turn_completion'], 'verified')
        self.assertEqual(capabilities()['automatic_communication'], 'unverified')
        self.assertEqual(capabilities()['task_tools'], 'unverified')
        connection = self.store.status()['members'][0]['connection']
        self.assertEqual(connection['source'], 'binding_snapshot')

    def test_automatic_handshake_needs_both_business_and_model_completion(self):
        self.join('A', 'openai')
        self.join('B')
        question = self.store.send('B', 'A', 'offline handshake', kind='question',
                                   dedupe_key='window-handshake')
        reply = self.store.send('A', 'B', 'offline reply', kind='reply',
                               reply_to=question['id'])
        def capability():
            return self.store.status()['members'][0]['capabilities']
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='completed'")
        self.assertEqual(capability()['automatic_communication'], 'unverified')
        for name, message in (('A', question), ('B', reply)):
            member = self.members[name]
            claim = self.store.claim(name, member['session_uuid'], member['cwd'], message_id=message['id'])
            self.store.complete(name, member['session_uuid'], member['cwd'], message['id'],
                                claim['claim_token'], 'offline business receipt')
            expected = 'verified' if name == 'B' else 'unverified'
            self.assertEqual(capability()['automatic_communication'], expected)
        self.assertEqual(capability()['task_tools'], 'unverified')

    def test_observation_requires_persisted_completion_and_redacts_error(self):
        client = self.observer()
        client.validate = Mock()
        turn = dict(id='turn', status='completed', completedAt=None, items=[],
                    error={'authorization': 'fake-secret', 'detail': 'Bearer fake-bearer'})
        client.turns = Mock(side_effect=lambda _: iter([turn]))
        result = client.observe(self.members['A'], {'turn_id': 'turn'})
        self.assertEqual(result['remote_status'], 'inProgress')
        turn['completedAt'] = 123
        result = client.observe(self.members['A'], {'turn_id': 'turn'})
        self.assertEqual(result['remote_status'], 'completed')
        self.assertNotIn('fake-secret', json.dumps(result))
        self.assertNotIn('fake-bearer', json.dumps(result))

    def test_errors_evidence_and_observation_failures_redact_fake_secrets(self):
        secrets = ['sk-fakeKey123', 'fake-bearer-123', 'fake-dictionary-123']
        payload = dict(api_key=secrets[2], nested=[{'authorization': secrets[2]}],
                       detail='sk-fakeKey123 Bearer fake-bearer-123')
        values = [str(TransportError(payload['detail'])), str(RpcError(payload))]
        recorded = []
        client = self.observer()
        client._evidence = recorded.append
        client._record('receive', payload)
        values.append(json.dumps(recorded))
        self.join('A', 'openai')
        self.join('B')
        message = self.store.send('A', 'B', 'offline')
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='queued' WHERE id=?", (message['id'],))
        fake = Mock()
        fake.observe.side_effect = TransportError(payload['detail'])
        Daemon(self.store, fake).reconcile()
        values.append(json.dumps(self.store.status()))
        for value in values:
            for secret in secrets:
                self.assertNotIn(secret, value)

    def test_mixed_and_deepseek_pair_routing_never_falls_back_on_identity_error(self):
        for providers in (('openai','deepseek'), ('deepseek','openai'), ('deepseek','deepseek')):
            with self.subTest(providers=providers):
                pool = TransportPool()
                clients = []
                def factory(**kwargs):
                    client = Mock()
                    client._dead = False
                    client.validate.side_effect = TransportError('Session identity mismatch')
                    clients.append((kwargs, client))
                    return client
                with patch('scripts.pair_transport.QueueTransport', side_effect=factory):
                    for name, provider in zip(('A','B'), providers):
                        member = dict(self.members[name], proof=self.thread(name, provider))
                        with self.assertRaisesRegex(TransportError, 'identity mismatch'):
                            pool.start(member, {'envelope': '{}'})
                self.assertEqual([args['codex_home'] for args, _ in clients],
                                 [self.homes['A'], self.homes['B']])
                self.assertEqual(pool.clients, {})
                for _, client in clients:
                    client.start.assert_not_called()
                    client.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
