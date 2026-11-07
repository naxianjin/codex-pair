"""复算准确客体的 fothk/SCP 字节；未证明 BDD 选择时始终拒绝完整通过。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import struct


BUILDS = {
    'ntdll.dll': '4dfbbd3d58225196603c6e61a9a61a7f71b9712ee63c79431998e51b393b63e6',
    'kernel32.dll': 'f694ab9ccf9d8c0b24003d14788b864a97c0f1ffe697ee043174626c30b96e2b',
}
MAX_FILE = 32 * 1024 * 1024
INIT_RVA = 0x1e7440
SCP_EXPORTS_RVA = 0x172710
SCP_TEMPLATE_RVA = 0x16a000
SCP_LENGTH = 0x2b0
PLACEHOLDER = 0x0123456789abcdef


def check(capture, bytes_dir, images_dir, *, expected_identity):
    """只核对同次补采；调用者须独立固定 CaptureId、PID 与创建时间。"""
    if (not isinstance(capture, dict) or type(capture.get('SchemaVersion')) is not int
            or capture['SchemaVersion'] != 1 or capture.get('Formal') is not False):
        raise ValueError('Invalid diagnostic schema or formal flag')
    identity = {key: capture.get(key) for key in ('CaptureId', 'PID', 'ProcessCreationUTC')}
    if (not isinstance(identity['CaptureId'], str)
            or not re.fullmatch('[0-9a-f]{32}', identity['CaptureId'])
            or type(identity['PID']) is not int or identity['PID'] <= 0
            or not isinstance(identity['ProcessCreationUTC'], str)
            or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z', identity['ProcessCreationUTC'])
            or identity != expected_identity
            or any(type(identity[k]) is not type(expected_identity[k]) for k in identity)):
        raise ValueError('Capture process identity mismatch')
    if (capture.get('ControlFlowGuardPolicyRead') is not True
            or type(capture.get('ControlFlowGuardPolicy')) is not int
            or capture['ControlFlowGuardPolicy'] != 1):
        raise ValueError('Unsupported or unavailable CFG policy')
    images = _unique(capture.get('Images'), 'Name')
    if set(images) != set(BUILDS):
        raise ValueError('Exact two-image capture required')
    ranges = _unique(capture.get('Ranges'), 'File')
    if len(ranges) != 11:
        raise ValueError('Exact eleven captured ranges required')
    binaries, bases, memory = {}, {}, {}
    for name, digest in BUILDS.items():
        data = _read_file(Path(images_dir) / name)
        if (_sha(data) != digest or images[name].get('SHA256') != digest
                or capture.get('NtdllSHA256' if name == 'ntdll.dll' else 'Kernel32SHA256') != digest):
            raise ValueError('Exact guest DLL SHA256 mismatch: ' + name)
        binaries[name] = PE(data)
        bases[name] = _address(images[name].get('BaseAddress'))
    for filename, record in ranges.items():
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,96}', filename):
            raise ValueError('Unsafe range filename')
        size = record.get('Bytes')
        if type(size) is not int or not 1 <= size <= 4096:
            raise ValueError('Invalid captured range size')
        data = _read_file(Path(bytes_dir) / filename, size)
        if _sha(data) != record.get('SHA256'):
            raise ValueError('Captured range SHA256 mismatch')
        address = _address(record.get('Address'))
        region = _address(record.get('RegionBase'))
        region_size = _address(record.get('RegionSize'))
        if (type(record.get('State')) is not int or record['State'] != 0x1000
                or address < region or address + size > region + region_size):
            raise ValueError('Captured memory range not fully committed')
        memory[filename] = data

    used = set()

    def observed(filename, kind, image, address, size, *, executable=False):
        record = ranges.get(filename)
        if (record is None or record.get('Kind') != kind or record.get('Image') != image
                or record.get('Address') != str(address) or record.get('Bytes') != size):
            raise ValueError('Captured range identity mismatch: ' + filename)
        if image in bases:
            if (record.get('AllocationBase') != str(bases[image])
                    or type(record.get('Type')) is not int or record['Type'] != 0x1000000
                    or type(record.get('Protect')) is not int
                    or record['Protect'] != (0x20 if executable else 0x02)):
                raise ValueError('Captured image memory protection mismatch')
        elif (record.get('Type') != 0x20000 or record.get('Protect') != 0x02):
            raise ValueError('Shared data memory protection mismatch')
        used.add(filename)
        return memory[filename]

    ntdll, base = binaries['ntdll.dll'], bases['ntdll.dll']
    init = observed('LdrSystemDllInitBlock.bin', 'kernel_initialization_block',
                    'ntdll.dll', base + INIT_RVA, 0x128)
    if struct.unpack_from('<I', init)[0] != 0x128:
        raise ValueError('Unsupported system DLL initialization block')
    bitmap = struct.unpack_from('<Q', init, 0xb8)[0]
    bitmap_size = struct.unpack_from('<Q', init, 0xc0)[0]
    if not bitmap or bitmap % 0x10000 or bitmap_size != 0x20000000000:
        raise ValueError('Unsupported CFG bitmap state')
    exports = [ntdll.unpack('<Q', ntdll.raw(SCP_EXPORTS_RVA + i * 8, 8))[0] - ntdll.base
               for i in range(13)]
    if exports[:8] != [0x16c000, 0x16c2b0, 0x16a000, 0x16a2b0,
                       0x16d000, 0x16d2b0, 0x16b000, 0x16b2b0]:
        raise ValueError('Unsupported SCP export table')
    templates = []
    for i in range(4):
        rva = SCP_TEMPLATE_RVA + i * 4096
        data = observed(f'scp-template-{i}.bin', 'scp_template', 'ntdll.dll',
                        base + rva, SCP_LENGTH, executable=True)
        if data != ntdll.take(ntdll.raw(rva, SCP_LENGTH), SCP_LENGTH):
            raise ValueError('Captured SCP template differs from exact disk image')
        templates.append(data)
    offsets = struct.unpack_from('<6I', templates[0])
    if offsets != (0x40, 0xc0, 0x140, 0x1c0, 0x240, 0x2a4):
        raise ValueError('Unsupported SCP template header')
    check_ptr, check_es, dispatch_ptr, dispatch_es = struct.unpack_from('<4Q', init, 0xf0)
    page = dispatch_ptr - offsets[0]
    if (page % 4096 or page != base + ntdll.image_size
            or (check_ptr, check_es, dispatch_es) !=
            (page + offsets[2], page + offsets[3], page + offsets[1])):
        raise ValueError('SCP pointers disagree with initialization block or template')
    reconstructed = bytearray(templates[0] + b'\xcc' * (4096 - SCP_LENGTH))
    for index, offset in enumerate(offsets[:5]):
        if struct.unpack_from('<Q', reconstructed, offset + 2)[0] != PLACEHOLDER:
            raise ValueError('SCP template placeholder mismatch')
        struct.pack_into('<Q', reconstructed, offset + 2,
                         bitmap if index < 4 else base + exports[12])
    filename = f'page-{page:x}.bin'
    actual_scp = observed(filename, 'destination_page', 'ntdll.dll', page, 4096, executable=True)
    if bytes(reconstructed) != actual_scp:
        raise ValueError('Unexplained SCP destination page bytes remain')

    pointer = observed('RtlpXcfgEnabledVolatileFeatures.bin', 'volatile_feature_pointer',
                       'ntdll.dll', base + 0x195060, 8)
    shared = observed('KUSER_SHARED_DATA.bin', 'shared_data', 'KUSER_SHARED_DATA',
                      0x7ffe0000, 4096)
    if pointer != struct.pack('<Q', 0x7ffe03e0):
        raise ValueError('Unsupported volatile XState feature pointer')
    # 此值用于 XRSTOR；不把 XState mask 当作尚未证明的函数覆盖特性位图。
    xstate = struct.unpack_from('<Q', shared, 0x3e0)[0]

    thunks = {}
    values = capture.get('Thunks')
    if not isinstance(values, list) or len(values) != 4:
        raise ValueError('Exact four captured thunks required')
    for value in values:
        if not isinstance(value, dict) or type(value.get('Rel32RVA')) is not int:
            raise ValueError('Invalid captured thunk')
        key = (value.get('Image'), value['Rel32RVA'])
        if key in thunks:
            raise ValueError('Duplicate captured thunk')
        thunks[key] = value
    fixups, raw_differences = [], 0
    for name, pe in binaries.items():
        section = next((s for s in pe.sections if s['name'] == 'fothk'), None)
        if section is None or section['virtual_size'] != 4096:
            raise ValueError('Unsupported fothk section')
        rva = section['rva']
        expected = pe.take(pe.raw(rva, 4096), 4096)
        actual = observed(name + '.fothk.bin', 'fothk', name, bases[name] + rva,
                          4096, executable=True)
        normalized = bytearray(expected)
        entries = pe.overrides()
        for entry in entries:
            for relocation in entry['relocations']:
                offset = relocation - rva
                if (not 1 <= offset <= 4092 or expected[offset - 1] != 0xe9
                        or actual[offset - 1] != 0xe9):
                    raise ValueError('Override is not an E9 REL32 thunk')
                original = relocation + 4 + struct.unpack_from('<i', expected, offset)[0]
                if original != entry['original_rva']:
                    raise ValueError('Disk thunk disagrees with DVRT original RVA')
                target = bases[name] + relocation + 4 + struct.unpack_from('<i', actual, offset)[0]
                observation = thunks.pop((name, relocation), None)
                if (observation is None or observation.get('Source') != str(bases[name] + relocation)
                        or observation.get('Target') != str(target)
                        or observation.get('Page') != str(target & ~4095)):
                    raise ValueError('Captured thunk disagrees with actual REL32 bytes')
                is_cfg = entry['features'] in ({321}, {322})
                if is_cfg:
                    destination = check_es if entry['features'] == {321} else dispatch_es
                    if target != destination:
                        raise ValueError('CFG thunk target disagrees with initialization block')
                else:
                    if target - bases[name] not in entry['candidate_rvas']:
                        raise ValueError('Function target is absent from disk DVRT candidates')
                    target_page = target & ~4095
                    target_name = f'page-{target_page:x}.bin'
                    destination = observed(target_name, 'destination_page', name, target_page,
                                           4096, executable=True)
                    if destination != pe.take(pe.raw(target_page - bases[name], 4096), 4096):
                        raise ValueError('Function destination page differs from exact disk image')
                normalized[offset:offset + 4] = actual[offset:offset + 4]
                fixups.append({'image': name, 'rel32_rva': relocation, 'target': str(target),
                               'features': sorted(entry['features']), 'bdd_selection_verified': False,
                               'destination_page_bytes_verified': True})
        if bytes(normalized) != actual:
            raise ValueError('Difference outside declared function-override REL32 fields')
        raw_differences += sum(a != b for a, b in zip(expected, actual))
    if thunks or len(fixups) != 4 or used != set(ranges):
        raise ValueError('Missing or unconsumed capture evidence')
    return {'schema_version': 1, 'formal': False, 'conclusion': 'needs_validation',
            'capture_identity': identity, 'raw_difference_count': raw_differences,
            'scp_page_reconstruction_verified': True, 'scp_compared_bytes': 4096,
            'scp_template_selection_verified': False, 'bdd_selection_verified': False,
            'fothk_consistency_verified': False, 'fixups': fixups, 'xstate_mask': xstate,
            'bdd_semantics_implemented': True,
            'blockers': ['Effective BDD capabilities after boot masking or VSL initialization are unverified.',
                         'Kernel choice of the ordinary SCP template is unverified.'],
            'limit': 'Exact-build diagnostic consistency only; no independent authentication, JIT, formal receipt or Host-to-Store admission.'}


class PE:
    def __init__(self, data):
        self.data = data
        if self.take(0, 2) != b'MZ':
            raise ValueError('DOS signature mismatch')
        offset = self.unpack('<I', 0x3c)[0]
        if self.take(offset, 4) != b'PE\0\0':
            raise ValueError('PE signature mismatch')
        machine, count, _, _, _, optional_size, _ = self.unpack('<HHIIIHH', offset + 4)
        optional = offset + 24
        if machine != 0x8664 or self.unpack('<H', optional)[0] != 0x20b or optional_size < 240:
            raise ValueError('Only AMD64 PE32+ supported')
        if not 1 <= count <= 96:
            raise ValueError('Invalid PE section count')
        self.base = self.unpack('<Q', optional + 24)[0]
        self.image_size = self.unpack('<I', optional + 56)[0]
        self.directories = [self.unpack('<II', optional + 112 + 8 * i) for i in range(16)]
        self.sections = []
        for i in range(count):
            name, size, rva, raw_size, raw, _, _, _, _, flags = self.unpack(
                '<8sIIIIIIHHI', optional + optional_size + 40 * i)
            self.take(raw, raw_size)
            if rva + size > self.image_size:
                raise ValueError('Section outside image')
            self.sections.append(dict(name=name.rstrip(b'\0').decode('ascii'),
                                      rva=rva, virtual_size=size, raw_size=raw_size,
                                      raw_offset=raw, flags=flags))

    def take(self, offset, size):
        if offset < 0 or size < 0 or offset + size > len(self.data):
            raise ValueError('PE range outside file')
        return self.data[offset:offset + size]

    def unpack(self, fmt, offset):
        return struct.unpack(fmt, self.take(offset, struct.calcsize(fmt)))

    def raw(self, rva, size=1):
        for section in self.sections:
            delta = rva - section['rva']
            if 0 <= delta and delta + size <= section['raw_size']:
                return section['raw_offset'] + delta
        raise ValueError('RVA has no file-backed section')

    def overrides(self):
        rva, size = self.directories[10]
        config = self.raw(rva, size)
        if size < 230:
            raise ValueError('Load config has no DVRT offset')
        relative, index = self.unpack('<IH', config + 224)
        if self.unpack('<Q', config + 192)[0] or not 1 <= index <= len(self.sections):
            raise ValueError('Unsupported DVRT location')
        section = self.sections[index - 1]
        start = section['raw_offset'] + relative
        version, size = self.unpack('<II', start)
        if version != 1 or relative + 8 + size > section['raw_size']:
            raise ValueError('Unsupported or out-of-bounds DVRT')
        position, end, entries = start + 8, start + 8 + size, []
        while position < end:
            if position + 12 > end:
                raise ValueError('Truncated DVRT record')
            symbol, size = self.unpack('<QI', position)
            payload, entry_end = position + 12, position + 12 + size
            if symbol != 7 or size < 4 or entry_end > end:
                raise ValueError('Unsupported DVRT record')
            override_size = self.unpack('<I', payload)[0]
            record, bdd_start = payload + 4, payload + 4 + override_size
            if bdd_start > entry_end:
                raise ValueError('Override table outside DVRT record')
            while record < bdd_start:
                if record + 16 > bdd_start:
                    raise ValueError('Truncated function override record')
                original, bdd_offset, rva_size, reloc_size = self.unpack('<IIII', record)
                if not rva_size or rva_size % 4 or record + 16 + rva_size + reloc_size > bdd_start:
                    raise ValueError('Invalid function override record')
                candidates = [self.unpack('<I', record + 16 + i)[0] for i in range(0, rva_size, 4)]
                cursor, relocs = record + 16 + rva_size, []
                reloc_end = cursor + reloc_size
                while cursor < reloc_end:
                    if cursor + 8 > reloc_end:
                        raise ValueError('Truncated override relocation block')
                    page, block_size = self.unpack('<II', cursor)
                    if block_size < 8 or block_size % 4 or cursor + block_size > reloc_end:
                        raise ValueError('Invalid override relocation block')
                    for i in range(8, block_size, 2):
                        encoded = self.unpack('<H', cursor + i)[0]
                        if encoded >> 12:
                            if encoded >> 12 != 1:
                                raise ValueError('Only X64 REL32 overrides supported')
                            relocs.append(page + (encoded & 0xfff))
                    cursor += block_size
                bdd = bdd_start + bdd_offset
                if bdd + 8 > entry_end:
                    raise ValueError('Invalid BDD range')
                bdd_version, bdd_size = self.unpack('<II', bdd)
                if bdd_version != 1 or not bdd_size or bdd_size % 8 or bdd + 8 + bdd_size > entry_end:
                    raise ValueError('Invalid BDD range')
                nodes = [self.unpack('<HHI', bdd + 8 + i) for i in range(0, bdd_size, 8)]
                _validate_bdd(nodes, len(candidates))
                features = {value for index, (left, right, value) in enumerate(nodes)
                            if (left, right) not in ((0, 0), (index, index))}
                entries.append(dict(original_rva=original, candidate_rvas=candidates,
                                    relocations=relocs, features=features, nodes=nodes))
                record += 16 + rva_size + reloc_size
            position = entry_end
        return entries


def evaluate_bdd(entry, capabilities, *, system_overrides=None):
    """复算已解析的 BDD；有效特性位必须由调用者独立取得，不能从目标倒推。"""
    if (not isinstance(capabilities, frozenset)
            or any(type(value) is not int or not 0 <= value < 0x148 for value in capabilities)):
        raise ValueError('Explicit effective capability set required')
    system_overrides = {} if system_overrides is None else system_overrides
    index, trace = 0, []
    while True:
        left, right, value = entry['nodes'][index]
        if left == right == 0:
            return {'kind': 'original', 'rva': entry['original_rva'], 'trace': trace}
        if left == right == index:
            return {'kind': 'candidate', 'rva': entry['candidate_rvas'][value],
                    'candidate_index': value, 'trace': trace}
        enabled = value in capabilities
        trace.append({'node': index, 'feature': value, 'enabled': enabled})
        # 准确内核仅在根节点尝试系统覆盖，成功后不再进入候选叶。
        if index == 0 and enabled and value in system_overrides:
            return {'kind': 'system', 'rva': system_overrides[value], 'trace': trace}
        index = right if enabled else left


def _validate_bdd(nodes, candidate_count):
    for index, (left, right, value) in enumerate(nodes):
        if left == right == 0 and index:
            continue
        if left == right == index and index:
            if value >= candidate_count:
                raise ValueError('BDD leaf outside candidate array')
        elif not index < left < len(nodes) or not index < right < len(nodes):
            raise ValueError('BDD requires forward edges, indexed leaves and non-root sentinel')


def _read_file(path, expected_size=None):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, 'st_file_attributes', 0) & 0x400
            or not 0 < info.st_size <= MAX_FILE
            or expected_size is not None and info.st_size != expected_size):
        raise ValueError('Evidence file type or size mismatch')
    with path.open('rb') as stream:
        data = stream.read(info.st_size + 1)
    if len(data) != info.st_size:
        raise ValueError('Evidence file changed during read')
    return data


def _unique(values, key):
    if not isinstance(values, list) or not 1 <= len(values) <= 32:
        raise ValueError('Bounded evidence records required')
    result = {}
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get(key), str) or value[key] in result:
            raise ValueError('Invalid or duplicate evidence identity')
        result[value[key]] = value
    return result


def _address(value):
    if not isinstance(value, str) or not re.fullmatch('[1-9][0-9]{0,19}', value):
        raise ValueError('Invalid decimal memory address')
    result = int(value)
    if result >= 1 << 64:
        raise ValueError('Memory address exceeds uint64')
    return result


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', required=True, type=Path)
    parser.add_argument('--bytes-dir', required=True, type=Path)
    parser.add_argument('--images-dir', required=True, type=Path)
    parser.add_argument('--capture-id', required=True)
    parser.add_argument('--pid', required=True, type=int)
    parser.add_argument('--process-creation-utc', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    capture = json.loads(_read_file(args.capture).decode('utf-8-sig'), object_pairs_hook=_json_object)
    result = check(capture, args.bytes_dir, args.images_dir,
                   expected_identity={'CaptureId': args.capture_id, 'PID': args.pid,
                                      'ProcessCreationUTC': args.process_creation_utc})
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(text, encoding='utf-8')
    print(text, end='')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
