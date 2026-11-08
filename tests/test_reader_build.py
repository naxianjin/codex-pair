"""固定编译器真实构建与契约边界；不认证系统映像或 JIT。"""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import build_windows_reader as builder
from scripts.pair_reader_assembly import parse_assembly


@unittest.skipUnless(os.name == 'nt', 'Windows .NET Framework compiler required')
class ReaderBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.contract = builder.build_contract()
        cls.temporary = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temporary.name) / 'built'
        cls.result = builder.build_reader(cls.output, cls.contract)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def test_real_compiler_and_entire_artifact_are_recorded(self):
        record = self.result
        self.assertTrue(record['build_completed'])
        self.assertFalse(record['formal'])
        self.assertEqual(record['conclusion'], 'needs_validation')
        self.assertEqual(record['exit_code'], 0)
        self.assertEqual(record['process_final']['exit_code'], 0)
        self.assertGreater(record['process_start']['created'], 0)
        self.assertGreater(record['process_start']['pid'], 0)
        for field in ('pid', 'created'):
            self.assertEqual(record['process_start'][field], record['process_final'][field])
        self.assertNotIn('path', record['process_final'])
        self.assertEqual(record['process_start']['path'], self.contract['tools']['csc.exe']['path'])
        data = (self.output / 'reader.dll').read_bytes()
        sha = hashlib.sha256(data).hexdigest()
        self.assertEqual(record['assembly']['sha256'], sha)
        self.assertEqual(record['assembly']['parsed'], parse_assembly(data, sha))
        self.assertEqual(json.loads((self.output / 'provenance.json').read_bytes()), record)
        self.assertEqual((self.output / 'WindowsSafeInput.cs').read_bytes(), builder.SOURCE.read_bytes())

    def test_exact_options_and_minimal_environment(self):
        argv = self.result['argv']
        self.assertEqual(argv[1:1 + len(builder.OPTIONS)], list(builder.OPTIONS))
        self.assertIn('/noconfig', argv)
        self.assertIn('/nostdlib+', argv)
        self.assertEqual(set(self.result['environment']), {'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP'})
        self.assertEqual(Path(self.result['environment']['SYSTEMROOT']), Path(r'C:\Windows'))

    def test_existing_output_is_not_overwritten(self):
        before = (self.output / 'reader.dll').read_bytes()
        with self.assertRaises(FileExistsError):
            builder.build_reader(self.output, self.contract)
        self.assertEqual((self.output / 'reader.dll').read_bytes(), before)

    def test_source_tool_or_options_contract_changes_are_rejected_before_launch(self):
        for field in ('source', 'tool', 'options', 'extra'):
            changed = copy.deepcopy(self.contract)
            if field == 'source':
                changed['source']['sha256'] = '0' * 64
            elif field == 'tool':
                changed['tools']['csc.exe']['sha256'] = '0' * 64
            elif field == 'options':
                changed['options'][4] = '/optimize-'
            else:
                changed['extra'] = True
            with self.subTest(field=field), patch.object(builder.subprocess, 'Popen') as launch:
                with self.assertRaisesRegex(ValueError, 'contract changed'):
                    builder.build_reader(self.output.parent / field, changed)
                launch.assert_not_called()

    def test_actual_changed_source_is_rejected(self):
        source = self.output.parent / 'changed.cs'
        source.write_text('public class Changed {}', encoding='utf-8')
        with patch.object(builder, 'SOURCE', source), self.assertRaisesRegex(ValueError, 'source SHA256'):
            builder.build_contract()

    def test_actual_tool_change_at_final_boundary_removes_only_own_assembly(self):
        changed = copy.deepcopy(self.contract)
        changed['tools']['csc.exe']['sha256'] = '0' * 64
        output = self.output.parent / 'changed-tools'
        with patch.object(builder, 'build_contract', side_effect=[self.contract, changed]):
            with self.assertRaisesRegex(ValueError, 'changed during compilation'):
                builder.build_reader(output, self.contract)
        self.assertFalse((output / 'reader.dll').exists())
        self.assertFalse(json.loads((output / 'provenance.json').read_bytes())['build_completed'])
        self.assertTrue((output / 'WindowsSafeInput.cs').exists())
        self.assertTrue((self.output / 'reader.dll').exists())


if __name__ == '__main__':
    unittest.main()
