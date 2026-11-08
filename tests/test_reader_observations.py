"""实际读取器观察的边界校验，不把观察升级为认证。"""
import base64
import copy
import json
import hashlib
import unittest
import os
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile

from scripts.pair_validator_worker import reader_observations, verify_validator_result
from scripts.pair_reader_assembly import _Image
from tests.reader_fixture import SHELL, SOURCE, compiled_reader, reader_observation


@unittest.skipUnless(os.name == 'nt' and SHELL.is_file(), 'Windows .NET compiler required')
class ReaderObservationTests(unittest.TestCase):
    def setUp(self):
        self.observation = reader_observation('a' * 64)

    def line(self, value):
        return 'PAIR_READER_OBSERVATION:' + json.dumps(value) + '\n'

    def test_source_binding_and_process_method_boundaries(self):
        self.assertEqual(reader_observations(self.line(self.observation), 'a' * 64), [self.observation])
        for field, value in [('schema_version', True), ('formal', True), ('pid', True),
                             ('source_sha256', 'b' * 64), ('mvid', 'invalid'),
                             ('process_creation_utc', 'unbound'), ('methods', [])]:
            with self.subTest(field=field):
                changed = dict(self.observation, **{field: value})
                with self.assertRaises(ValueError):
                    reader_observations(self.line(changed), 'a' * 64)
        for field, value in [('metadata_token', True), ('il_bytes', 0),
                             ('signature', 'Byte[] Unknown()'), ('il_sha256', 'bad')]:
            changed = copy.deepcopy(self.observation)
            changed['methods'][0][field] = value
            with self.subTest(method_field=field), self.assertRaises(ValueError):
                reader_observations(self.line(changed), 'a' * 64)
        duplicated = self.line(self.observation) * 2
        with self.assertRaisesRegex(ValueError, 'duplicated'):
            reader_observations(duplicated, 'a' * 64)
        with self.assertRaises(ValueError):
            reader_observations(self.line(dict(self.observation, padding='x')), 'a' * 64)

    def test_observation_cannot_be_removed_or_changed_in_receipt(self):
        stderr = self.line(self.observation)
        empty = hashlib.sha256(b'').hexdigest()
        names = ['validate-findings.cjs', 'validate-coverage-ledger.cjs']
        request = {'binding': {}, 'files': {'WindowsSafeInput.cs': 'a' * 64}}
        artifact = {'binding': {}, 'files': request['files'], 'formal_checks_verified': False,
                    'validators_succeeded': True,
                    'results': [{'validator': name, 'exit_code': 0,
                                 'stdout_sha256': empty, 'stderr_sha256': hashlib.sha256(stderr.encode()).hexdigest(),
                                 'stderr': stderr} for name in names],
                    'reader_observations': {name: [self.observation] for name in names}}
        native = {'ExitCode': 0, 'ActiveProcesses': 0, 'TimedOut': False}
        self.assertTrue(verify_validator_result(request, native, artifact)['validators_succeeded'])
        for changed in (dict(artifact, reader_observations={}),
                        {key: value for key, value in artifact.items() if key != 'reader_observations'}):
            with self.assertRaisesRegex(ValueError, 'observation receipt mismatch'):
                verify_validator_result(request, native, changed)

    def test_loaded_bytes_mvid_and_all_il_fields_are_recomputed(self):
        for field, value in [('assembly_input_base64', '@invalid'),
                             ('assembly_input_base64', self.observation['assembly_input_base64'] + '\n'),
                             ('assembly_input_sha256', '0' * 64),
                             ('mvid', '00000000-0000-0000-0000-000000000001')]:
            changed = dict(self.observation, **{field: value})
            with self.subTest(field=field, value=value[:30]), self.assertRaises(ValueError):
                reader_observations(self.line(changed), 'a' * 64)
        for field, value in [('metadata_token', 0x06000060), ('il_bytes', 1), ('il_sha256', '0' * 64)]:
            changed = copy.deepcopy(self.observation)
            changed['methods'][0][field] = value
            with self.subTest(method_field=field), self.assertRaises(ValueError):
                reader_observations(self.line(changed), 'a' * 64)
        data, _ = compiled_reader()
        image = _Image(data)
        position = image.raw(next(row[0] for row in image.rows(6) if row[0]), 12) + 12
        changed_data = bytearray(data)
        changed_data[position] ^= 1
        changed = dict(self.observation, assembly_input_base64=base64.b64encode(changed_data).decode('ascii'),
                       assembly_input_sha256=hashlib.sha256(changed_data).hexdigest())
        with self.assertRaisesRegex(ValueError, 'content observation mismatch'):
            reader_observations(self.line(changed), 'a' * 64)

    def test_real_reader_emits_exact_loaded_bytes_and_input(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'input.json'
            content = b'["real input"]'
            path.write_bytes(content)
            script = SOURCE.with_name('read-windows-input.ps1')
            result = subprocess.run([str(SHELL), '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
                                     'RemoteSigned', '-File', str(script), '-LiteralPath', str(path)],
                                    capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertEqual(result.stdout, content)
        values = reader_observations(result.stderr.decode('utf-8'), hashlib.sha256(SOURCE.read_bytes()).hexdigest())
        self.assertEqual(len(values), 1)
        self.assertGreater(len(values[0]['assembly_input_base64']), 8192)
        self.assertIs(values[0]['formal'], False)

    @unittest.skipUnless(shutil.which('node'), 'Node required')
    def test_copied_worker_loads_fixed_parser_under_isolated_python(self):
        source = SOURCE.parents[2]
        node = shutil.which('node')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / 'input'
            root.mkdir()
            assets = ('validate-findings.cjs', 'validate-coverage-ledger.cjs', 'report-schema.json',
                      'read-windows-input.cjs', 'read-windows-input.ps1', 'WindowsSafeInput.cs')
            for name in assets:
                shutil.copyfile(SOURCE.parent / name, root / name)
            for name in ('findings.json', 'coverage_ledger.json'):
                (root / name).write_bytes(b'[]')
            request = {'binding': {'execution': 'isolated-copy'},
                       'node_sha256': hashlib.sha256(Path(node).read_bytes()).hexdigest(),
                       'files': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                 for path in root.iterdir()}}
            for name in ('pair_validator_worker.py', 'pair_reader_assembly.py'):
                shutil.copyfile(source / 'scripts' / name, root / name)
            request_path = root / 'request.json'
            request_path.write_text(json.dumps(request), encoding='utf-8')
            result = subprocess.run([sys.executable, '-I', '-B', str(root / 'pair_validator_worker.py'),
                                     '--root', str(root), '--request', str(request_path), '--node', node],
                                    cwd=temporary, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        artifact = json.loads(result.stdout)
        self.assertTrue(artifact['validators_succeeded'])
        self.assertIs(artifact['formal_checks_verified'], False)
        for values in artifact['reader_observations'].values():
            self.assertEqual(len(values), 1)
            self.assertGreater(len(values[0]['assembly_input_base64']), 8192)
        native = {'ExitCode': 0, 'ActiveProcesses': 0, 'TimedOut': False}
        self.assertTrue(verify_validator_result(request, native, artifact)['validators_succeeded'])

    def test_ordinary_diagnostics_do_not_become_observations(self):
        self.assertEqual(reader_observations('invalid array\n', 'a' * 64), [])
        with self.assertRaises(ValueError):
            reader_observations('PAIR_READER_OBSERVATION:invalid\n', 'a' * 64)
        with self.assertRaisesRegex(ValueError, 'exceeded'):
            reader_observations('PAIR_READER_OBSERVATION:' + 'x' * 65537, 'a' * 64)


if __name__ == '__main__':
    unittest.main()
