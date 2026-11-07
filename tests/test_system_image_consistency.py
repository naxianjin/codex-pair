"""系统可执行节一致性检查的正例和拒绝路径。"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'validation/bytecode-provenance-20261105/check-system-image-consistency.py'
SPEC = importlib.util.spec_from_file_location('system_image_consistency', SCRIPT)
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)
COLLECTOR = '1' * 64
CONTRACT_HASH = '2' * 64


class SystemImageConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.contract = {
            'schema_version': 1, 'formal': False, 'clr_sha256': '3' * 64,
            'pdb_sha256': '4' * 64, 'pdb_guid': '12345678-1234-1234-1234-123456789abc',
            'pdb_age': 3, 'symbols': [], 'images': {},
        }
        self.capture = {
            'SchemaVersion': 1, 'Formal': False, 'CaptureId': 'a' * 32, 'PID': 42,
            'ProcessCreationUTC': '2026-11-06T17:33:02.4447742Z',
            'CollectorSHA256': COLLECTOR, 'RuntimeSymbolsSHA256': CONTRACT_HASH, 'Images': [],
        }
        self.state = {key: self.capture[key] for key in (
            'SchemaVersion', 'Formal', 'CaptureId', 'PID', 'ProcessCreationUTC', 'RuntimeSymbolsSHA256')}
        self.state.update(CLRSHA256=self.contract['clr_sha256'], Symbols=[])
        globals_ = {'gThreadTLSIndex': bytes.fromhex('07000000'),
                    'gAppDomainTLSIndex': bytes.fromhex('08000000'),
                    'CExecutionEngine::TlsIndex': bytes.fromhex('09000000'),
                    'g_card_table': bytes.fromhex('0010203040506070'),
                    'g_ephemeral_low': bytes.fromhex('1020304050607080')}
        special = {'JIT_WriteBarrier': 0x3000, 'JIT_WriteBarrier_PreGrow64': 0x4000,
                   'JIT_WriteBarrier_PreGrow64_End': 0x4040,
                   'JIT_WriteBarrier_PreGrow64_Patch_Label_CardTable': 0x4008,
                   'JIT_WriteBarrier_PreGrow64_Patch_Label_Lower': 0x4020}
        self.addresses = dict(special)
        for index, name in enumerate(sorted(CHECKER.SYMBOLS - set(special) - set(globals_))):
            self.addresses[name] = 0x1100 + 0x100 * index
        for index, name in enumerate(globals_):
            self.addresses[name] = 0x6000 + 0x100 * index
        expected = bytearray(b'\x90' * 0x4000)
        actual = bytearray(expected)
        for name, rva in self.addresses.items():
            if 'PatchTLS' in name or 'GetThread_PatchLabel' in name:
                offset = rva - 0x1000
                expected[offset:offset + 9] = bytes.fromhex('654c8b1c25f0f0f0f0')
                actual[offset:offset + 9] = bytes.fromhex(
                    '654c8b1c25c0140000' if 'Shared' in name else '654c8b1c25b8140000')
        for name, instruction in [('GetThread', '65488b0425b8140000c3'),
                                  ('GetAppDomain', '65488b0425c0140000c3'),
                                  ('ClrFlsGetBlock', '65488b0425c8140000c3')]:
            offset = self.addresses[name] - 0x1000
            actual[offset:offset + 10] = bytes.fromhex(instruction)
        template = bytearray(b'\x90' * 64)
        template[8:18] = bytes.fromhex('48b8f0f0f0f0f0f0f0f0')
        template[32:42] = bytes.fromhex('48b8f0f0f0f0f0f0f0f0')
        expected[0x3000:0x3040] = template
        actual[0x3000:0x3040] = template
        template[10:18] = globals_['g_card_table']
        template[34:42] = globals_['g_ephemeral_low']
        actual[0x2000:0x2040] = template
        for name, rva in sorted(self.addresses.items()):
            self.contract['symbols'].append({'name': name, 'rva': rva, 'resolved': True})
            memory = (globals_[name] + b'\0' * 128)[:128] if name in globals_ else actual[rva - 0x1000:rva - 0x1000 + 128]
            self.state['Symbols'].append({'Name': name, 'RVA': rva, 'MemoryHex': memory.hex()})
        self._image('ntdll.dll', [('.text', 0x1000, b'N' * 512, b'N' * 512),
                                  ('PAGE', 0x2000, b'P' * 256, b'P' * 256),
                                  ('RT', 0x3000, b'R' * 128, b'R' * 128)])
        self._image('kernel32.dll', [('.text', 0x1000, b'K' * 512, b'K' * 512)])
        self._image('clr.dll', [('.text', 0x1000, expected, actual)])
        self._state()

    def _image(self, name, sections):
        digest = self.contract['clr_sha256'] if name == 'clr.dll' else hashlib.sha256(name.encode()).hexdigest()
        fixed = {'DiskSHA256': digest, 'ImageSize': 0x10000, 'Sections': []}
        image = {**fixed, 'Name': name, 'BaseAddress': '140000000000000',
                 'ExecutableSectionsTotal': len(sections), 'WritableExecutableSections': 0, 'Sections': []}
        for section_name, rva, expected, actual in sections:
            metadata = {'Section': section_name, 'RVA': rva, 'VirtualSize': len(expected),
                        'RawSize': len(expected), 'Flags': 0x60000020}
            fixed['Sections'].append(metadata)
            image['Sections'].append({**metadata, 'ComparedBytes': len(expected),
                                      'DifferentBytes': sum(a != b for a, b in zip(expected, actual)),
                                      'DiskSHA256': hashlib.sha256(expected).hexdigest(),
                                      'MappedSHA256': hashlib.sha256(actual).hexdigest()})
            for kind, value in [('expected', expected), ('actual', actual)]:
                (self.directory / f'{name}.{section_name}.{kind}.bin').write_bytes(value)
        self.contract['images'][name] = fixed
        self.capture['Images'].append(image)

    def _state(self):
        (self.directory / 'runtime-state.json').write_text(json.dumps(self.state), encoding='utf-8')

    def _check(self):
        return CHECKER.check(self.capture, self.directory, self.contract,
                             collector_sha256=COLLECTOR, contract_sha256=CONTRACT_HASH)

    def _section(self, name='ntdll.dll'):
        return next(image for image in self.capture['Images'] if image['Name'] == name)['Sections'][0]

    def _change_bin(self, name, offset, value, *, update_metadata=False):
        path = self.directory / f'{name}..text.actual.bin'
        content = bytearray(path.read_bytes())
        content[offset] = value
        path.write_bytes(content)
        if update_metadata:
            section = self._section(name)
            section['MappedSHA256'] = hashlib.sha256(content).hexdigest()
            expected = (self.directory / f'{name}..text.expected.bin').read_bytes()
            section['DifferentBytes'] = sum(a != b for a, b in zip(expected, content))

    def test_complete_snapshot_is_consistent_but_not_formal(self):
        result = self._check()
        self.assertIs(result['formal'], False)
        self.assertEqual(result['conclusion'], 'needs_validation')
        self.assertIs(result['consistency_verified'], True)
        self.assertEqual((result['images'], len(result['sections']), result['compared_bytes']), (3, 5, 17792))
        self.assertEqual(result['remaining_difference_count'], 0)
        self.assertGreater(sum(section['raw_difference_count'] for section in result['sections']), 0)

    def test_missing_and_duplicate_module_are_rejected(self):
        self.capture['Images'].pop()
        with self.assertRaisesRegex(ValueError, 'Missing or unexpected'):
            self._check()
        self.capture['Images'].append(self.capture['Images'][0])
        with self.assertRaisesRegex(ValueError, 'Duplicate record identity'):
            self._check()

    def test_section_coverage_and_duplicate_identity_are_rejected(self):
        image = self.capture['Images'][0]
        original = copy.deepcopy(image['Sections'])
        image['Sections'].pop()
        with self.assertRaisesRegex(ValueError, 'coverage incomplete'):
            self._check()
        image['Sections'] = original + [original[0]]
        with self.assertRaisesRegex(ValueError, 'Duplicate record'):
            self._check()
        image['Sections'] = original
        image['ExecutableSectionsTotal'] = 2
        with self.assertRaisesRegex(ValueError, 'coverage incomplete'):
            self._check()

    def test_capture_and_runtime_state_binding_cannot_be_substituted(self):
        for key, value in [('CaptureId', 'b' * 32), ('PID', 43),
                           ('ProcessCreationUTC', '2026-11-06T17:33:03.4447742Z'),
                           ('RuntimeSymbolsSHA256', 'b' * 64)]:
            with self.subTest(key=key):
                original = self.state[key]
                self.state[key] = value
                self._state()
                with self.assertRaisesRegex(ValueError, 'another capture'):
                    self._check()
                self.state[key] = original
        self._state()

    def test_collector_and_contract_digest_binding(self):
        for key in ('CollectorSHA256', 'RuntimeSymbolsSHA256'):
            with self.subTest(key=key):
                original = self.capture[key]
                self.capture[key] = 'b' * 64
                with self.assertRaisesRegex(ValueError, 'source binding mismatch'):
                    self._check()
                self.capture[key] = original

    def test_formal_and_schema_flags_require_exact_types(self):
        for target, formal_key, schema_key in [(self.capture, 'Formal', 'SchemaVersion'),
                                               (self.contract, 'formal', 'schema_version'),
                                               (self.state, 'Formal', 'SchemaVersion')]:
            for key, bad_values in [(formal_key, [True, 0, None]), (schema_key, [True, 1.0, '1'])]:
                for value in bad_values:
                    with self.subTest(key=key, value=value):
                        original = target[key]
                        target[key] = value
                        self._state()
                        with self.assertRaisesRegex(ValueError, 'schema or formal flag'):
                            self._check()
                        target[key] = original
        self._state()

    def test_binary_hash_and_length_tampering(self):
        self._change_bin('ntdll.dll', 0, 0)
        with self.assertRaisesRegex(ValueError, 'digest mismatch'):
            self._check()
        path = self.directory / 'ntdll.dll..text.actual.bin'
        path.write_bytes(path.read_bytes()[:-1])
        with self.assertRaisesRegex(ValueError, 'file size or type mismatch'):
            self._check()

    def test_self_consistent_metadata_cannot_hide_unexplained_bytes(self):
        self._change_bin('clr.dll', 0, 0, update_metadata=True)
        with self.assertRaisesRegex(ValueError, 'Unexplained executable bytes'):
            self._check()

    def test_difference_count_is_recomputed(self):
        self._section('clr.dll')['DifferentBytes'] += 1
        with self.assertRaisesRegex(ValueError, 'difference count mismatch'):
            self._check()

    def test_runtime_code_observation_must_match_section(self):
        observation = next(item for item in self.state['Symbols'] if item['Name'] == 'GetThread')
        observation['MemoryHex'] = '00' + observation['MemoryHex'][2:]
        self._state()
        with self.assertRaisesRegex(ValueError, 'code state differs'):
            self._check()

    def test_out_of_line_tls_is_not_accepted_as_inline(self):
        observation = next(item for item in self.state['Symbols'] if item['Name'] == 'gThreadTLSIndex')
        observation['MemoryHex'] = '40000000' + observation['MemoryHex'][8:]
        self._state()
        with self.assertRaisesRegex(ValueError, 'Only inline TLS slots supported'):
            self._check()

    def test_symbol_set_and_address_substitution(self):
        symbol = self.contract['symbols'].pop()
        with self.assertRaisesRegex(ValueError, 'Exact runtime symbol set'):
            self._check()
        self.contract['symbols'].append(symbol)
        symbol['rva'] += 1
        with self.assertRaisesRegex(ValueError, 'symbol address mismatch'):
            self._check()

    def test_unresolved_and_duplicate_symbol_addresses(self):
        symbol = self.contract['symbols'][0]
        symbol['resolved'] = False
        with self.assertRaisesRegex(ValueError, 'Unresolved runtime symbol'):
            self._check()
        symbol['resolved'] = True
        symbol['rva'] = self.contract['symbols'][1]['rva']
        with self.assertRaisesRegex(ValueError, 'Duplicate runtime symbol addresses'):
            self._check()

    def test_image_contract_and_executable_permissions(self):
        image = self.capture['Images'][0]
        image['DiskSHA256'] = 'b' * 64
        with self.assertRaisesRegex(ValueError, 'image identity mismatch'):
            self._check()
        image['DiskSHA256'] = self.contract['images']['ntdll.dll']['DiskSHA256']
        image['WritableExecutableSections'] = 1
        with self.assertRaisesRegex(ValueError, 'image identity mismatch'):
            self._check()
        image['WritableExecutableSections'] = 0
        self._section()['Flags'] |= 0x80000000
        self.contract['images']['ntdll.dll']['Sections'][0]['Flags'] |= 0x80000000
        with self.assertRaisesRegex(ValueError, 'not fully covered'):
            self._check()

    def test_section_overlap_is_not_complete_coverage(self):
        self.capture['Images'][0]['Sections'][1]['RVA'] = 0x1000
        self.contract['images']['ntdll.dll']['Sections'][1]['RVA'] = 0x1000
        with self.assertRaisesRegex(ValueError, 'Overlapping executable sections'):
            self._check()

    def test_write_barrier_requires_exact_template_placeholder(self):
        path = self.directory / 'clr.dll..text.expected.bin'
        expected = bytearray(path.read_bytes())
        expected[0x300a] = 0
        path.write_bytes(expected)
        self._section('clr.dll')['DiskSHA256'] = hashlib.sha256(expected).hexdigest()
        actual = (self.directory / 'clr.dll..text.actual.bin').read_bytes()
        self._section('clr.dll')['DifferentBytes'] = sum(a != b for a, b in zip(expected, actual))
        with self.assertRaisesRegex(ValueError, 'template placeholder mismatch'):
            self._check()

    def test_runtime_global_values_do_not_silently_allow_other_code(self):
        observation = next(item for item in self.state['Symbols'] if item['Name'] == 'g_card_table')
        observation['MemoryHex'] = '01' + observation['MemoryHex'][2:]
        self._state()
        with self.assertRaisesRegex(ValueError, 'Unexplained executable bytes'):
            self._check()

    def test_duplicate_json_keys_and_oversized_document(self):
        path = self.directory / 'runtime-state.json'
        path.write_text('{"Formal":false,"Formal":true}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Duplicate JSON key'):
            self._check()
        path.write_bytes(b' ' * (1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, 'Evidence JSON exceeded limit'):
            self._check()

    def test_powershell_utf16_json_is_read_without_changing_evidence(self):
        path = self.directory / 'runtime-state.json'
        path.write_text(json.dumps(self.state), encoding='utf-16')
        self.assertTrue(self._check()['consistency_verified'])

    def test_cli_recomputes_contract_digest_and_rejects_tampering(self):
        contract_path = self.directory / 'contract.json'
        contract_bytes = json.dumps(self.contract).encode()
        contract_path.write_bytes(contract_bytes)
        digest = hashlib.sha256(contract_bytes).hexdigest()
        self.capture['RuntimeSymbolsSHA256'] = self.state['RuntimeSymbolsSHA256'] = digest
        self._state()
        capture_path = self.directory / 'capture.json'
        capture_path.write_text(json.dumps(self.capture), encoding='utf-8')
        command = [sys.executable, '-O', str(SCRIPT), '--capture', str(capture_path),
                   '--bytes-dir', str(self.directory), '--contract', str(contract_path),
                   '--collector-sha256', COLLECTOR]
        success = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertFalse(json.loads(success.stdout)['formal'])
        self._change_bin('ntdll.dll', 0, 0)
        failure = subprocess.run(command, capture_output=True, text=True, check=False)
        self.assertNotEqual(failure.returncode, 0)
        self.assertIn('Captured section digest mismatch', failure.stderr)


if __name__ == '__main__':
    unittest.main()
