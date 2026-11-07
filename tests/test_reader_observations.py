"""实际读取器观察的边界校验，不把观察升级为认证。"""
import copy
import json
import hashlib
import unittest

from scripts.pair_validator_worker import reader_observations, verify_validator_result


class ReaderObservationTests(unittest.TestCase):
    def setUp(self):
        self.observation = {
            'schema_version': 1, 'formal': False, 'pid': 123,
            'process_creation_utc': '2026-11-06T17:20:00.1234567Z',
            'source_sha256': 'a' * 64, 'assembly_input_sha256': 'b' * 64,
            'mvid': '00000000-0000-0000-0000-000000000001',
            'methods': [{'metadata_token': 0x06000001 + i, 'signature': signature,
                         'il_sha256': 'c' * 64, 'il_bytes': 20}
                        for i, signature in enumerate(('IntPtr OpenRoot(System.String)',
                            'Byte[] Read(System.String)', 'Byte[] ReadBytes(IntPtr, System.String)'))]}

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

    def test_ordinary_diagnostics_do_not_become_observations(self):
        self.assertEqual(reader_observations('invalid array\n', 'a' * 64), [])
        with self.assertRaises(ValueError):
            reader_observations('PAIR_READER_OBSERVATION:invalid\n', 'a' * 64)
        with self.assertRaisesRegex(ValueError, 'exceeded'):
            reader_observations('PAIR_READER_OBSERVATION:' + 'x' * 8193, 'a' * 64)


if __name__ == '__main__':
    unittest.main()
