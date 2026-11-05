"""固定工作入口的真实 Node 校验；不作为隔离或正式回执证明。"""
import copy
import hashlib
import os
import json
import subprocess
import sys
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from scripts.pair_validator_worker import run_validators, verify_validator_result


@unittest.skipUnless(shutil.which('node'), 'Node required')
class ValidatorWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        source = Path(__file__).resolve().parents[1] / 'assets/audit'
        for name in ('validate-findings.cjs', 'validate-coverage-ledger.cjs', 'report-schema.json',
                     'read-windows-input.cjs', 'read-windows-input.ps1', 'WindowsSafeInput.cs'):
            shutil.copyfile(source / name, self.root / name)
        for name in ('findings.json', 'coverage_ledger.json'):
            (self.root / name).write_bytes(b'[]')
        self.node = shutil.which('node')
        self.request = {'binding': {'execution': 'fixture', 'report': 'fixed', 'context': {'epoch': 1}},
                        'node_sha256': hashlib.sha256(Path(self.node).read_bytes()).hexdigest(),
                        'files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.root.iterdir()}}

    def test_real_validators_and_invalid_input(self):
        result = run_validators(self.root, self.request, node=self.node)
        self.assertTrue(result['validators_succeeded'])
        self.assertFalse(result['formal_checks_verified'])
        native = {'ExitCode': 0, 'ActiveProcesses': 0, 'TimedOut': False}
        self.assertTrue(verify_validator_result(self.request, native, result)['validators_succeeded'])
        for kind in ('binding', 'epoch_bool', 'epoch_float', 'missing', 'active', 'forged', 'boolean', 'float', 'null', 'files', 'order', 'digest', 'timeout'):
            changed = copy.deepcopy(result)
            terminal = dict(native)
            if kind == 'binding':
                changed['binding'] = {}
            elif kind in ('epoch_bool', 'epoch_float'):
                changed['binding']['context']['epoch'] = True if kind == 'epoch_bool' else 1.0
            elif kind == 'missing':
                changed['results'].pop()
            elif kind == 'active':
                terminal['ActiveProcesses'] = 1
            elif kind == 'boolean':
                terminal['ExitCode'] = False
            elif kind == 'float':
                terminal['ActiveProcesses'] = 0.0
            elif kind == 'null':
                changed['results'] = [None, None]
            elif kind == 'files':
                changed['files'] = {}
            elif kind == 'order':
                changed['results'].reverse()
            elif kind == 'digest':
                changed['results'][0]['stdout_sha256'] = 'invalid'
            elif kind == 'timeout':
                terminal['TimedOut'] = True
            else:
                changed['validators_succeeded'] = False
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                verify_validator_result(self.request, terminal, changed)
        (self.root / 'findings.json').write_bytes(b'{}')
        self.request['files']['findings.json'] = hashlib.sha256(b'{}').hexdigest()
        rejected = run_validators(self.root, self.request, node=self.node)
        self.assertFalse(rejected['validators_succeeded'])
        self.assertIn('array', rejected['results'][0]['stderr'])
        self.assertFalse(verify_validator_result(self.request, native, rejected)['validators_succeeded'])
        (self.root / 'findings.json').write_bytes(b'[]')
        self.request['files']['findings.json'] = hashlib.sha256(b'[]').hexdigest()
        (self.root / 'coverage_ledger.json').write_bytes(b'{}')
        self.request['files']['coverage_ledger.json'] = hashlib.sha256(b'{}').hexdigest()
        ledger = run_validators(self.root, self.request, node=self.node)
        self.assertEqual([r['exit_code'] for r in ledger['results']], [0, 1])
        self.assertIn('expected a top-level array', ledger['results'][1]['stderr'])
        self.assertFalse(verify_validator_result(self.request, native, ledger)['validators_succeeded'])
        ledger['results'][1]['stderr'] = 'altered diagnostic'
        with self.assertRaisesRegex(ValueError, 'diagnostic digest mismatch'):
            verify_validator_result(self.request, native, ledger)

    def test_isolated_python_cli_returns_bound_results(self):
        request_path = self.root / 'request.json'
        request_path.write_text(json.dumps(self.request), encoding='utf-8')
        worker = Path(__file__).resolve().parents[1] / 'scripts/pair_validator_worker.py'
        result = subprocess.run([sys.executable, '-I', '-B', str(worker), '--root', str(self.root),
                                 '--request', str(request_path), '--node', self.node],
                                capture_output=True, text=True, timeout=40)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['binding'], self.request['binding'])
        self.assertTrue(report['validators_succeeded'])
        self.assertFalse(report['formal_checks_verified'])
        request_path.write_bytes(b' ' * 65537)
        oversized = subprocess.run([sys.executable, '-I', '-B', str(worker), '--root', str(self.root),
                                    '--request', str(request_path), '--node', self.node],
                                   capture_output=True, text=True, timeout=10)
        self.assertNotEqual(oversized.returncode, 0)
        self.assertIn('Validator request exceeded limit', oversized.stderr)

    def test_reject_changed_input_tool_and_extra_command(self):
        for kind in ('input', 'node', 'extra'):
            request = copy.deepcopy(self.request)
            if kind == 'input':
                request['files']['findings.json'] = '0' * 64
            elif kind == 'node':
                request['node_sha256'] = '0' * 64
            else:
                request['command'] = 'unexpected'
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                run_validators(self.root, request, node=self.node)

    def test_node_options_cannot_preload_code_and_claim_success(self):
        preload = self.root / 'preload.cjs'
        preload.write_text('process.exit(0)', encoding='utf-8')
        (self.root / 'findings.json').write_bytes(b'{}')
        self.request['files']['findings.json'] = hashlib.sha256(b'{}').hexdigest()
        with patch.dict('os.environ', {'NODE_OPTIONS': '--require=' + str(preload),
                                      'SYSTEMROOT': str(self.root / 'fake-windows'),
                                      'WINDIR': str(self.root / 'fake-windows')}):
            result = run_validators(self.root, self.request, node=self.node)
            (self.root / 'findings.json').write_bytes(b'[]')
            self.request['files']['findings.json'] = hashlib.sha256(b'[]').hexdigest()
            valid = run_validators(self.root, self.request, node=self.node)
        self.assertFalse(result['validators_succeeded'])
        self.assertTrue(valid['validators_succeeded'])
        self.assertEqual([item['exit_code'] for item in valid['results']], [0, 0])

    def test_relative_root_runs_checked_scripts_instead_of_nested_scripts(self):
        nested = self.root / self.root.name
        nested.mkdir()
        for name in ('validate-findings.cjs', 'validate-coverage-ledger.cjs'):
            (nested / name).write_text('process.exit(0)', encoding='utf-8')
        (self.root / 'findings.json').write_bytes(b'{}')
        self.request['files']['findings.json'] = hashlib.sha256(b'{}').hexdigest()
        previous = Path.cwd()
        try:
            os.chdir(self.root.parent)
            self.assertFalse(run_validators(self.root.name, self.request, node=self.node)['validators_succeeded'])
        finally:
            os.chdir(previous)
