"""在实际探针和进程创建边界核对环境；外部 CLI 不启动。"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts import pair_producer as producer
from scripts import pair_transport as transport
from scripts import pair_validator_dispatch as dispatch


class ProducerEnvironmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.exe = self.root / 'codex.exe'
        self.exe.write_bytes(b'never executed: isolated environment boundary fixture')
        self.home = self.root / 'empty-home'
        self.fake = {'PATH': os.environ.get('PATH', ''), 'CODEX_HOME': 'parent-home',
            'CODEX_THREAD_ID': 'parent-thread', 'OPENAI_API_KEY': 'fake-key',
            'CUSTOM_ACCESS_TOKEN': 'fake-token', 'OPENAI_BASE_URL': 'https://example.invalid',
            'CODEX_AUTH_JSON': 'fake-auth', 'CODEX_EXEC_SERVER_URL': 'fake-executor'}

    def boundary(self, server=transport.AppServer, **options):
        seen = {}

        def version(argv, **kwargs):
            seen['version'] = dict(os.environ if kwargs.get('env') is None else kwargs['env'])
            self.assertEqual(argv, [producer.file_identity(self.exe)['path'], '--version'])
            # 两次创建之间父环境改变，不能改变已选定的子环境。
            os.environ['CODEX_AUTH_JSON'] = 'changed-parent-auth'
            return Mock(returncode=0, stdout='codex-cli 0.167.0-alpha.3.2\n')

        def refuse(argv, **kwargs):
            seen['process'] = kwargs['env'].copy()
            raise OSError('isolated process boundary; no executable started')

        with patch.dict(os.environ, self.fake, clear=True), \
             patch.object(producer.subprocess, 'run', side_effect=version), \
             patch.object(dispatch.subprocess, 'Popen', side_effect=refuse), \
             self.assertRaisesRegex(transport.TransportError, 'isolated process boundary'):
            if server is transport.AppServer:
                options['process_factory'] = refuse
            server(executable=str(self.exe), codex_home=str(self.home), cwd=str(self.root),
                   track_producer=True, **options)
        self.assertEqual(seen['version'], seen['process'])
        self.assertEqual(seen['version']['CODEX_HOME'], str(self.home))
        return seen['version']

    def test_owned_command_probe_and_process_share_filtered_environment(self):
        env = self.boundary(dispatch._CommandServer)
        self.assertEqual(env, {'PATH': self.fake['PATH'], 'CODEX_HOME': str(self.home)})

    def test_metadata_probe_uses_existing_metadata_environment(self):
        env = self.boundary(metadata_only=True)
        self.assertNotIn('CODEX_THREAD_ID', env)
        self.assertNotIn('OPENAI_API_KEY', env)
        self.assertEqual(env['CODEX_AUTH_JSON'], self.fake['CODEX_AUTH_JSON'])

    def test_model_process_keeps_original_environment(self):
        env = self.boundary()
        for key, value in self.fake.items():
            if key != 'CODEX_HOME':
                self.assertEqual(env[key], value)

    def test_owned_filter_does_not_mutate_parent(self):
        with patch.dict(os.environ, self.fake, clear=True):
            parent = dict(os.environ)
            env = dispatch._official_environment(str(self.home))
            self.assertEqual(dict(os.environ), parent)
        self.assertEqual(env, {'PATH': self.fake['PATH'], 'CODEX_HOME': str(self.home)})

    def test_direct_prepare_launch_preserves_optional_environment(self):
        supplied = {'PATH': 'selected-path'}
        result = Mock(returncode=0, stdout='codex-cli 0.167.0-alpha.3.2\n')
        with patch.object(producer.subprocess, 'run', return_value=result) as run:
            producer.prepare_launch([str(self.exe)], env=supplied)
            self.assertIs(run.call_args.kwargs['env'], supplied)
            producer.prepare_launch([str(self.exe)])
            self.assertIsNone(run.call_args.kwargs['env'])


if __name__ == '__main__':
    unittest.main()
