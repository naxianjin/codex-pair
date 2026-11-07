"""直接测试 fothk 检查器的整页重建及拒绝路径。"""
from __future__ import annotations

import hashlib
import importlib.util
import itertools
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / 'validation/bytecode-provenance-20261105/check-fothk-consistency.py'
SPEC = importlib.util.spec_from_file_location('fothk_consistency', SCRIPT)
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class FothkConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.identity = {'CaptureId': 'a' * 32, 'PID': 42,
                         'ProcessCreationUTC': '2026-11-07T02:05:13.7101518Z'}
        self.capture = {'SchemaVersion': 1, 'Formal': False, **self.identity,
                        'ControlFlowGuardPolicyRead': True, 'ControlFlowGuardPolicy': 1,
                        'Images': [], 'Thunks': [], 'Ranges': []}
        self.bases = {'ntdll.dll': 0x7ffd00000000, 'kernel32.dll': 0x7ffcff000000}
        self.images = {name: self._image(name) for name in self.bases}
        builds = {name: hashlib.sha256(data).hexdigest() for name, data in self.images.items()}
        self.patch = patch.dict(CHECKER.BUILDS, builds)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        for name, data in self.images.items():
            (self.directory / name).write_bytes(data)
            self.capture['Images'].append({'Name': name, 'SHA256': builds[name],
                                            'BaseAddress': str(self.bases[name])})
        self.capture['NtdllSHA256'] = builds['ntdll.dll']
        self.capture['Kernel32SHA256'] = builds['kernel32.dll']
        ntdll = self.bases['ntdll.dll']
        self.page = ntdll + 0x263000
        self.bitmap = 0x7df500000000
        init = bytearray(0x128)
        struct.pack_into('<I', init, 0, 0x128)
        struct.pack_into('<QQ', init, 0xb8, self.bitmap, 0x20000000000)
        struct.pack_into('<4Q', init, 0xf0, self.page + 0x140, self.page + 0x1c0,
                         self.page + 0x40, self.page + 0xc0)
        self._range('LdrSystemDllInitBlock.bin', 'kernel_initialization_block',
                    'ntdll.dll', ntdll + CHECKER.INIT_RVA, init)
        self._range('RtlpXcfgEnabledVolatileFeatures.bin', 'volatile_feature_pointer',
                    'ntdll.dll', ntdll + 0x195060, struct.pack('<Q', 0x7ffe03e0))
        self._range('KUSER_SHARED_DATA.bin', 'shared_data', 'KUSER_SHARED_DATA',
                    0x7ffe0000, b'\0' * 4096)
        for i in range(4):
            rva = 0x16a000 + i * 4096
            self._range(f'scp-template-{i}.bin', 'scp_template', 'ntdll.dll',
                        ntdll + rva, self.images['ntdll.dll'][rva:rva + 0x2b0], executable=True)
        page = bytearray(self.images['ntdll.dll'][0x16a000:0x16a2b0] + b'\xcc' * (4096 - 0x2b0))
        for i, offset in enumerate((0x42, 0xc2, 0x142, 0x1c2, 0x242)):
            struct.pack_into('<Q', page, offset, self.bitmap if i < 4 else ntdll + 0x123c10)
        self._range(f'page-{self.page:x}.bin', 'destination_page', 'ntdll.dll',
                    self.page, page, executable=True)
        target_page = ntdll + 0x165000
        self._range(f'page-{target_page:x}.bin', 'destination_page', 'ntdll.dll', target_page,
                    self.images['ntdll.dll'][0x165000:0x166000], executable=True)
        for name in self.bases:
            rva = 0x170000 if name == 'ntdll.dll' else 0x85000
            actual = bytearray(self.images[name][rva:rva + 4096])
            locations = [(0x11, self.page + 0x1c0), (0x21, self.page + 0xc0),
                         (0x31, ntdll + 0x165340)] if name == 'ntdll.dll' else [(0x11, self.page + 0xc0)]
            for offset, target in locations:
                source = self.bases[name] + rva + offset
                struct.pack_into('<i', actual, offset, target - source - 4)
                self.capture['Thunks'].append({'Image': name, 'Rel32RVA': rva + offset,
                                               'Source': str(source), 'Target': str(target),
                                               'Page': str(target & ~4095)})
            self._range(name + '.fothk.bin', 'fothk', name, self.bases[name] + rva,
                        actual, executable=True)

    def _image(self, name):
        ntdll = name == 'ntdll.dll'
        size = 0x263000 if ntdll else 0xc7000
        data = bytearray(size)
        data[:2] = b'MZ'
        struct.pack_into('<I', data, 0x3c, 0x80)
        data[0x80:0x84] = b'PE\0\0'
        sections = [('.text', 0x1000, 0x169000 if ntdll else 0x84000)]
        if ntdll:
            sections += [(name, 0x16a000 + 4096 * i, 0x2b0) for i, name in
                         enumerate(('SCPCFG', 'SCPCFGFP', 'SCPCFGNP', 'SCPCFGES'))]
        sections += [('fothk', 0x170000 if ntdll else 0x85000, 4096),
                     ('.rdata', 0x171000 if ntdll else 0x86000, 0x3000),
                     ('.config', 0x180000 if ntdll else 0x90000, 4096),
                     ('.reloc', 0x262000 if ntdll else 0xc6000, 4096)]
        struct.pack_into('<HHIIIHH', data, 0x84, 0x8664, len(sections), 0, 0, 0, 240, 0)
        optional = 0x98
        struct.pack_into('<H', data, optional, 0x20b)
        struct.pack_into('<Q', data, optional + 24, 0x180000000)
        struct.pack_into('<I', data, optional + 56, size)
        config = 0x180000 if ntdll else 0x90000
        struct.pack_into('<II', data, optional + 112 + 80, config, 240)
        for i, (section_name, rva, length) in enumerate(sections):
            raw_size = max(4096, length)
            struct.pack_into('<8sIIIIIIHHI', data, optional + 240 + 40 * i,
                             section_name.encode(), length, rva, raw_size, rva, 0, 0, 0, 0,
                             0x60000020 if section_name not in ('.config', '.reloc', '.rdata') else 0x40000040)
        struct.pack_into('<IH', data, config + 224, 0, len(sections))
        records, bdds = bytearray(), bytearray()
        rows = [(0x12ae64, 0x170011, 321, [0x12ae64]),
                (0x1643a0, 0x170021, 322, [0x1643a0]),
                (0x164940, 0x170031, 319, [0x165340, 0x165540])]
        if not ntdll:
            rows = [(0x839a0, 0x85011, 322, [0x839a0])]
        fothk = 0x170000 if ntdll else 0x85000
        data[fothk:fothk + 4096] = b'\xcc' * 4096
        for original, location, feature, candidates in rows:
            relocation = struct.pack('<IIHH', location & ~4095, 12, 0x1000 | (location & 4095), 0)
            records += struct.pack('<IIII', original, len(bdds), len(candidates) * 4, len(relocation))
            records += struct.pack('<' + 'I' * len(candidates), *candidates) + relocation
            bdds += struct.pack('<II', 1, 24) + struct.pack('<HHIHHIHHI', 2, 1, feature, 1, 1, 0, 0, 0, 0)
            data[location - 1] = 0xe9
            struct.pack_into('<i', data, location, original - location - 4)
        payload = struct.pack('<I', len(records)) + records + bdds
        dvrt = struct.pack('<IIQI', 1, len(payload) + 12, 7, len(payload)) + payload
        where = 0x262000 if ntdll else 0xc6000
        data[where:where + len(dvrt)] = dvrt
        if ntdll:
            for i in range(4):
                template = bytearray(b'\x90' * 0x2b0)
                struct.pack_into('<6I', template, 0, 0x40, 0xc0, 0x140, 0x1c0, 0x240, 0x2a4)
                for offset in (0x42, 0xc2, 0x142, 0x1c2, 0x242):
                    template[offset - 2:offset] = b'\x49\xbb'
                    struct.pack_into('<Q', template, offset, CHECKER.PLACEHOLDER)
                start = 0x16a000 + 4096 * i
                data[start:start + len(template)] = template
            exports = [0x16c000, 0x16c2b0, 0x16a000, 0x16a2b0, 0x16d000, 0x16d2b0,
                       0x16b000, 0x16b2b0, 0x1e7430, 0x1e8000, 0x1e7438, 0x1e4218, 0x123c10]
            for i, rva in enumerate(exports):
                struct.pack_into('<Q', data, CHECKER.SCP_EXPORTS_RVA + i * 8, 0x180000000 + rva)
        return bytes(data)

    def _range(self, filename, kind, image, address, data, *, executable=False):
        (self.directory / filename).write_bytes(data)
        self.capture['Ranges'].append({'File': filename, 'Kind': kind, 'Image': image,
                                       'Address': str(address), 'Bytes': len(data),
                                       'SHA256': hashlib.sha256(data).hexdigest(),
                                       'AllocationBase': str(self.bases.get(image, address)),
                                       'RegionBase': str(address), 'RegionSize': str(max(4096, len(data))),
                                       'State': 0x1000, 'Protect': 0x20 if executable else 2,
                                       'Type': 0x1000000 if image in self.bases else 0x20000})

    def _check(self):
        return CHECKER.check(self.capture, self.directory, self.directory, expected_identity=self.identity)

    def _change(self, filename, offset, value, *, rehash=True):
        path = self.directory / filename
        data = bytearray(path.read_bytes())
        data[offset:offset + len(value)] = value
        path.write_bytes(data)
        if rehash:
            next(x for x in self.capture['Ranges'] if x['File'] == filename)['SHA256'] = hashlib.sha256(data).hexdigest()

    def test_complete_page_reconstruction_retains_unverified_selection(self):
        result = self._check()
        self.assertTrue(result['scp_page_reconstruction_verified'])
        self.assertEqual(result['scp_compared_bytes'], 4096)
        self.assertEqual(len(result['fixups']), 4)
        self.assertFalse(result['formal'])
        self.assertFalse(result['fothk_consistency_verified'])
        self.assertFalse(result['bdd_selection_verified'])
        self.assertTrue(result['bdd_semantics_implemented'])
        self.assertEqual(result['conclusion'], 'needs_validation')

    def test_cross_build_disk_bytes_rejected(self):
        path = self.directory / 'ntdll.dll'
        data = bytearray(path.read_bytes())
        data[0x1000] ^= 1
        path.write_bytes(data)
        with self.assertRaisesRegex(ValueError, 'Exact guest DLL SHA256 mismatch'):
            self._check()

    def test_process_identity_mismatch_rejected(self):
        self.capture['PID'] += 1
        with self.assertRaisesRegex(ValueError, 'process identity mismatch'):
            self._check()

    def test_boolean_pid_and_schema_rejected(self):
        self.capture['PID'] = True
        with self.assertRaises(ValueError):
            self._check()
        self.capture['PID'] = self.identity['PID']
        self.capture['SchemaVersion'] = True
        with self.assertRaises(ValueError):
            self._check()

    def test_formal_true_rejected(self):
        self.capture['Formal'] = True
        with self.assertRaisesRegex(ValueError, 'formal flag'):
            self._check()

    def test_unavailable_cfg_policy_rejected(self):
        self.capture['ControlFlowGuardPolicyRead'] = False
        with self.assertRaisesRegex(ValueError, 'CFG policy'):
            self._check()

    def test_template_mutation_rejected_even_with_matching_capture_hash(self):
        self._change('scp-template-0.bin', 0x80, b'\xff')
        with self.assertRaisesRegex(ValueError, 'template differs'):
            self._check()

    def test_scp_instruction_mutation_rejected(self):
        self._change(f'page-{self.page:x}.bin', 0x80, b'\xff')
        with self.assertRaisesRegex(ValueError, 'Unexplained SCP'):
            self._check()

    def test_scp_padding_mutation_rejected(self):
        self._change(f'page-{self.page:x}.bin', 4095, b'\x90')
        with self.assertRaisesRegex(ValueError, 'Unexplained SCP'):
            self._check()

    def test_bitmap_patch_mutation_rejected(self):
        self._change(f'page-{self.page:x}.bin', 0x42, struct.pack('<Q', self.bitmap + 0x10000))
        with self.assertRaisesRegex(ValueError, 'Unexplained SCP'):
            self._check()

    def test_handler_target_mutation_rejected(self):
        self._change(f'page-{self.page:x}.bin', 0x242, struct.pack('<Q', self.bases['ntdll.dll'] + 0x123c11))
        with self.assertRaisesRegex(ValueError, 'Unexplained SCP'):
            self._check()

    def test_wrong_initialization_pointer_rejected(self):
        self._change('LdrSystemDllInitBlock.bin', 0xf8, struct.pack('<Q', self.page + 0x140))
        with self.assertRaisesRegex(ValueError, 'SCP pointers'):
            self._check()

    def test_outside_declared_rel32_mutation_rejected(self):
        self._change('ntdll.dll.fothk.bin', 0x80, b'\x90')
        with self.assertRaisesRegex(ValueError, 'outside declared'):
            self._check()

    def test_rel32_wrong_target_rejected_even_when_thunk_metadata_updated(self):
        source = self.bases['ntdll.dll'] + 0x170011
        target = self.page + 0x140
        self._change('ntdll.dll.fothk.bin', 0x11, struct.pack('<i', target - source - 4))
        self.capture['Thunks'][0]['Target'] = str(target)
        with self.assertRaisesRegex(ValueError, 'CFG thunk target'):
            self._check()

    def test_memset_destination_mutation_rejected(self):
        self._change(f'page-{self.bases["ntdll.dll"] + 0x165000:x}.bin', 0x340, b'\xff')
        with self.assertRaisesRegex(ValueError, 'destination page differs'):
            self._check()

    def test_stale_range_hash_rejected(self):
        self._change('ntdll.dll.fothk.bin', 0x80, b'\x90', rehash=False)
        with self.assertRaisesRegex(ValueError, 'range SHA256'):
            self._check()

    def test_writable_executable_page_rejected(self):
        next(x for x in self.capture['Ranges'] if x['File'] == f'page-{self.page:x}.bin')['Protect'] = 0x40
        with self.assertRaisesRegex(ValueError, 'protection mismatch'):
            self._check()

    def test_missing_range_rejected(self):
        self.capture['Ranges'].pop()
        with self.assertRaisesRegex(ValueError, 'eleven captured ranges'):
            self._check()

    def test_duplicate_thunk_rejected(self):
        self.capture['Thunks'][-1] = dict(self.capture['Thunks'][0])
        with self.assertRaisesRegex(ValueError, 'Duplicate captured thunk'):
            self._check()

    def test_duplicate_json_key_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate JSON key'):
            json.loads('{"PID":42,"PID":43}', object_pairs_hook=CHECKER._json_object)

    def test_unsafe_filename_rejected(self):
        self.capture['Ranges'][0]['File'] = '../outside.bin'
        with self.assertRaisesRegex(ValueError, 'Unsafe range filename'):
            self._check()

    def test_xstate_pointer_cannot_supply_bdd_features(self):
        self._change('RtlpXcfgEnabledVolatileFeatures.bin', 0, struct.pack('<Q', 0x7ffe03e8))
        with self.assertRaisesRegex(ValueError, 'XState feature pointer'):
            self._check()

    def test_malformed_pe_rejected(self):
        with self.assertRaisesRegex(ValueError, 'DOS signature'):
            CHECKER.PE(b'bad file')

    def test_out_of_bounds_dvrt_rejected(self):
        data = bytearray(self.images['ntdll.dll'])
        struct.pack_into('<H', data, 0x180000 + 228, 0xffff)
        with self.assertRaisesRegex(ValueError, 'Unsupported DVRT location'):
            CHECKER.PE(bytes(data)).overrides()

    def test_unavailable_bdd_feature_selection_never_becomes_formal(self):
        for value in (0, 1, (1 << 64) - 1):
            with self.subTest(xstate=value):
                self._change('KUSER_SHARED_DATA.bin', 0x3e0, struct.pack('<Q', value))
                result = self._check()
                self.assertFalse(result['bdd_selection_verified'])
                self.assertFalse(result['fothk_consistency_verified'])
                self.assertEqual(result['conclusion'], 'needs_validation')


class ActualGuestFothkTests(unittest.TestCase):
    def test_exact_guest_capture_remains_needs_validation(self):
        root = Path(r'C:\Users\XOS\Downloads\system-image-review')
        capture = root / 'fothk-investigation/guest-state-r1/capture/fothk-state.json'
        if not capture.is_file():
            self.skipTest('Exact local guest capture unavailable')
        result = subprocess.run([sys.executable, '-X', 'utf8', *(['-O'] if sys.flags.optimize else []), str(SCRIPT),
                                 '--capture', str(capture), '--bytes-dir', str(capture.parent),
                                 '--images-dir', str(root / 'guest-build'),
                                 '--capture-id', 'e8c0e258f6bf4554b54476c911e16a13', '--pid', '5060',
                                 '--process-creation-utc', '2026-11-07T02:05:13.7101518Z'],
                                capture_output=True, text=True, encoding='utf-8', check=False)
        self.assertNotEqual(result.returncode, 0)
        parsed = json.loads(result.stdout)
        self.assertEqual(parsed['raw_difference_count'], 13)
        self.assertTrue(parsed['scp_page_reconstruction_verified'])
        self.assertFalse(parsed['fothk_consistency_verified'])
        self.assertEqual(parsed['conclusion'], 'needs_validation')


class BddEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.entry = {'original_rva': 0x164940,
                      'candidate_rvas': [0x165340, 0x165540, 0x1656c0, 0x165880, 0x1659c0],
                      'nodes': [(4, 1, 319), (4, 2, 316), (4, 3, 0), (3, 3, 0),
                                (7, 5, 319), (7, 6, 316), (6, 6, 1), (10, 8, 319),
                                (10, 9, 0), (9, 9, 2), (12, 11, 319), (11, 11, 3),
                                (14, 13, 315), (13, 13, 4), (0, 0, 0)]}

    def test_all_memset_feature_combinations(self):
        CHECKER._validate_bdd(self.entry['nodes'], 5)
        for erms, alternate, avx, speculative in itertools.product((False, True), repeat=4):
            capabilities = frozenset(feature for feature, enabled in
                                     zip((0, 315, 316, 319), (erms, alternate, avx, speculative)) if enabled)
            with self.subTest(capabilities=sorted(capabilities)):
                result = CHECKER.evaluate_bdd(self.entry, capabilities)
                if speculative:
                    expected_index = (0 if erms else 1) if avx else (2 if erms else 3)
                    self.assertEqual(result['kind'], 'candidate')
                    self.assertEqual(result['candidate_index'], expected_index)
                    self.assertEqual(result['rva'], self.entry['candidate_rvas'][expected_index])
                elif alternate:
                    self.assertEqual(result['candidate_index'], 4)
                    self.assertEqual(result['rva'], 0x1659c0)
                else:
                    self.assertEqual(result['kind'], 'original')
                    self.assertEqual(result['rva'], 0x164940)

    def test_root_system_override_precedes_candidate(self):
        entry = {'original_rva': 0x12ae64, 'candidate_rvas': [0x12ae64],
                 'nodes': [(2, 1, 321), (1, 1, 0), (0, 0, 0)]}
        result = CHECKER.evaluate_bdd(entry, frozenset({321}), system_overrides={321: 0x2631c0})
        self.assertEqual((result['kind'], result['rva']), ('system', 0x2631c0))
        self.assertEqual(CHECKER.evaluate_bdd(entry, frozenset({321}))['kind'], 'candidate')
        result = CHECKER.evaluate_bdd(entry, frozenset(), system_overrides={321: 0x2631c0})
        self.assertEqual((result['kind'], result['rva']), ('original', 0x12ae64))

    def test_non_root_system_feature_does_not_replace_candidate(self):
        result = CHECKER.evaluate_bdd(self.entry, frozenset({315}), system_overrides={315: 0x99})
        self.assertEqual(result['kind'], 'candidate')
        self.assertEqual(result['rva'], 0x1659c0)

    def test_equal_forward_edges_are_not_candidate_leaves(self):
        entry = {'original_rva': 0x12, 'candidate_rvas': [0x34],
                 'nodes': [(1, 1, 319), (1, 1, 0)]}
        CHECKER._validate_bdd(entry['nodes'], 1)
        result = CHECKER.evaluate_bdd(entry, frozenset({319}))
        self.assertEqual(result['kind'], 'candidate')
        self.assertEqual(result['rva'], 0x34)
        self.assertEqual(result['trace'], [{'node': 0, 'feature': 319, 'enabled': True}])

    def test_effective_capabilities_are_explicit_and_bounded(self):
        for value in (None, {319}, frozenset({True}), frozenset({-1}), frozenset({0x148})):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'effective capability'):
                CHECKER.evaluate_bdd(self.entry, value)

    def test_back_edges_and_invalid_leaf_are_rejected(self):
        for nodes in ([(0, 0, 0)], [(1, 2, 0), (1, 0, 0), (0, 0, 0)],
                      [(1, 2, 0), (1, 1, 5), (0, 0, 0)],
                      [(1, 3, 0), (1, 1, 0), (0, 0, 0)]):
            with self.subTest(nodes=nodes), self.assertRaisesRegex(ValueError, 'BDD'):
                CHECKER._validate_bdd(nodes, 5)


if __name__ == '__main__':
    unittest.main()
