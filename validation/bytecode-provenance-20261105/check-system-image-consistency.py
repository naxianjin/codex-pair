"""复算准确构建的可执行节一致性；结果不是可信运行时认证。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import struct


MODULES = {'ntdll.dll', 'kernel32.dll', 'clr.dll'}
SYMBOLS = {
    'AllocateStringFastMP_InlineGetThread__PatchTLSOffset', 'CExecutionEngine::TlsIndex',
    'ClrFlsGetBlock', 'GetAppDomain', 'GetThread', 'JIT_BoxFastMPIGT__PatchTLSLabel',
    'JIT_GetSharedGCStaticBaseNoCtor__PatchTLSLabel', 'JIT_GetSharedGCStaticBase__PatchTLSLabel',
    'JIT_GetSharedNonGCStaticBaseNoCtor__PatchTLSLabel', 'JIT_GetSharedNonGCStaticBase__PatchTLSLabel',
    'JIT_MonEnterStaticWorker_InlineGetThread_GetThread_PatchLabel_1',
    'JIT_MonEnterStaticWorker_InlineGetThread_GetThread_PatchLabel_2',
    'JIT_MonEnterWorker_InlineGetThread_GetThread_PatchLabel',
    'JIT_MonExitStaticWorker_InlineGetThread_GetThread_PatchLabel',
    'JIT_MonExitWorker_InlineGetThread_GetThread_PatchLabel', 'JIT_MonTryEnter_GetThread_PatchLabel',
    'JIT_NewArr1OBJ_MP_InlineGetThread__PatchTLSOffset', 'JIT_NewArr1VC_MP_InlineGetThread__PatchTLSOffset',
    'JIT_TrialAllocSFastMP_InlineGetThread__PatchTLSOffset', 'JIT_WriteBarrier',
    'JIT_WriteBarrier_PreGrow64', 'JIT_WriteBarrier_PreGrow64_End',
    'JIT_WriteBarrier_PreGrow64_Patch_Label_CardTable', 'JIT_WriteBarrier_PreGrow64_Patch_Label_Lower',
    'gAppDomainTLSIndex', 'gThreadTLSIndex', 'g_card_table', 'g_ephemeral_low',
}
MAX_IMAGE_BYTES = 64 * 1024 * 1024


def check(capture, bytes_dir, contract, *, collector_sha256, contract_sha256):
    """核对调用方固定的采集器/契约摘要与全部采集节，始终保留 formal=false。"""
    _hash(collector_sha256)
    _hash(contract_sha256)
    _document(capture, 'SchemaVersion', 'Formal')
    _document(contract, 'schema_version', 'formal')
    if (not isinstance(capture.get('CaptureId'), str)
            or not re.fullmatch(r'[0-9a-f]{32}', capture['CaptureId'])
            or type(capture.get('PID')) is not int or capture['PID'] <= 0
            or not isinstance(capture.get('ProcessCreationUTC'), str)
            or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z', capture['ProcessCreationUTC'])
            or capture.get('CollectorSHA256') != collector_sha256
            or capture.get('RuntimeSymbolsSHA256') != contract_sha256):
        raise ValueError('Capture identity or source binding mismatch')
    _hash(contract.get('clr_sha256'))
    _hash(contract.get('pdb_sha256'))
    if (not isinstance(contract.get('pdb_guid'), str)
            or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', contract['pdb_guid'])
            or type(contract.get('pdb_age')) is not int or contract['pdb_age'] <= 0):
        raise ValueError('PDB identity unavailable in runtime contract')
    if not isinstance(contract.get('images'), dict) or set(contract['images']) != MODULES:
        raise ValueError('Exact three-image contract required')
    images = _unique(capture.get('Images'), 'Name', lowercase=True)
    if set(images) != MODULES:
        raise ValueError('Missing or unexpected captured image')
    symbols = _unique(contract.get('symbols'), 'name')
    if set(symbols) != SYMBOLS:
        raise ValueError('Exact runtime symbol set required')
    clr_size = contract['images']['clr.dll'].get('ImageSize')
    _integer(clr_size, 1, MAX_IMAGE_BYTES)
    for symbol in symbols.values():
        if symbol.get('resolved') is not True:
            raise ValueError('Unresolved runtime symbol')
        _integer(symbol.get('rva'), 0, clr_size - 128)
    if len({item['rva'] for item in symbols.values()}) != len(symbols):
        raise ValueError('Duplicate runtime symbol addresses')
    if contract['images']['clr.dll'].get('DiskSHA256') != contract['clr_sha256']:
        raise ValueError('CLR build contract inconsistent')

    bytes_dir = Path(bytes_dir)
    state = _read_json(bytes_dir / 'runtime-state.json')
    _document(state, 'SchemaVersion', 'Formal')
    for key in ('CaptureId', 'PID', 'ProcessCreationUTC', 'RuntimeSymbolsSHA256'):
        if type(state.get(key)) is not type(capture[key]) or state[key] != capture[key]:
            raise ValueError('Runtime state belongs to another capture')
    if state.get('CLRSHA256') != contract['clr_sha256']:
        raise ValueError('Runtime state CLR build mismatch')
    observations = _unique(state.get('Symbols'), 'Name')
    if set(observations) != SYMBOLS:
        raise ValueError('Runtime state symbol set mismatch')
    memory = {}
    for name, observation in observations.items():
        if type(observation.get('RVA')) is not int or observation['RVA'] != symbols[name]['rva']:
            raise ValueError('Runtime state symbol address mismatch')
        value = observation.get('MemoryHex')
        if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{256}', value):
            raise ValueError('Runtime state symbol bytes invalid')
        memory[name] = bytes.fromhex(value)

    checked = []
    for module_name in sorted(MODULES):
        image, fixed = images[module_name], contract['images'][module_name]
        _hash(fixed.get('DiskSHA256'))
        _integer(fixed.get('ImageSize'), 1, MAX_IMAGE_BYTES)
        if (image.get('DiskSHA256') != fixed['DiskSHA256']
                or type(image.get('ImageSize')) is not int or image['ImageSize'] != fixed['ImageSize']
                or type(image.get('WritableExecutableSections')) is not int
                or image['WritableExecutableSections'] != 0
                or not isinstance(image.get('BaseAddress'), str)
                or not re.fullmatch('[1-9][0-9]{0,19}', image['BaseAddress'])):
            raise ValueError('Captured image identity mismatch')
        sections = _unique(image.get('Sections'), 'Section')
        fixed_sections = _unique(fixed.get('Sections'), 'Section')
        if (not sections or set(sections) != set(fixed_sections)
                or type(image.get('ExecutableSectionsTotal')) is not int
                or image['ExecutableSectionsTotal'] != len(sections)):
            raise ValueError('Executable section coverage incomplete')
        ranges = []
        for name, section in sections.items():
            reference = fixed_sections[name]
            if not re.fullmatch('[.A-Za-z0-9_]{1,8}', name):
                raise ValueError('Unsafe section name')
            for key in ('RVA', 'VirtualSize', 'RawSize', 'Flags'):
                _integer(reference.get(key), 0, 0xffffffff)
                if type(section.get(key)) is not int or section[key] != reference[key]:
                    raise ValueError('Executable section contract mismatch')
            rva, length = reference['RVA'], reference['VirtualSize']
            if (not length or rva + length > fixed['ImageSize']
                    or not reference['Flags'] & 0x20000000 or reference['Flags'] & 0x80000000
                    or type(section.get('ComparedBytes')) is not int or section['ComparedBytes'] != length):
                raise ValueError('Executable virtual section not fully covered')
            ranges.append((rva, rva + length))
            expected = _read_bytes(bytes_dir / (image['Name'] + '.' + name + '.expected.bin'), length)
            actual = _read_bytes(bytes_dir / (image['Name'] + '.' + name + '.actual.bin'), length)
            if (_sha(expected) != section.get('DiskSHA256') or _sha(actual) != section.get('MappedSHA256')):
                raise ValueError('Captured section digest mismatch')
            different = sum(a != b for a, b in zip(expected, actual))
            if type(section.get('DifferentBytes')) is not int or section['DifferentBytes'] != different:
                raise ValueError('Captured difference count mismatch')
            normalized = expected
            if module_name == 'clr.dll' and name == '.text':
                normalized = _clr_transform(expected, rva, symbols, memory)
                for symbol_name, observation in observations.items():
                    offset = observation['RVA'] - rva
                    if 0 <= offset <= length - 128 and memory[symbol_name] != actual[offset:offset + 128]:
                        raise ValueError('Runtime code state differs from captured section')
            if normalized != actual:
                raise ValueError('Unexplained executable bytes remain: ' + module_name + '/' + name)
            checked.append({'image': module_name, 'section': name, 'compared_bytes': length,
                            'raw_difference_count': different, 'remaining_difference_count': 0,
                            'normalized_sha256': _sha(normalized), 'actual_sha256': _sha(actual)})
        for previous, current in zip(sorted(ranges), sorted(ranges)[1:]):
            if previous[1] > current[0]:
                raise ValueError('Overlapping executable sections')
        if module_name == 'clr.dll' and set(sections) != {'.text'}:
            raise ValueError('Unsupported CLR executable section set')
    return {'formal': False, 'conclusion': 'needs_validation', 'consistency_verified': True,
            'capture_id': capture['CaptureId'], 'pid': capture['PID'],
            'process_creation_utc': capture['ProcessCreationUTC'], 'collector_sha256': collector_sha256,
            'contract_sha256': contract_sha256, 'images': len(images), 'sections': checked,
            'compared_bytes': sum(item['compared_bytes'] for item in checked),
            'remaining_difference_count': 0,
            'limit': 'Executable-section and mutable-runtime-state consistency only; no independent loaded-byte, JIT, whole-execution or Host-to-Store authentication.'}


def _clr_transform(expected, section_rva, symbols, memory):
    def position(name, size):
        offset = symbols[name]['rva'] - section_rva
        if not 0 <= offset <= len(expected) - size:
            raise ValueError('CLR patch outside executable section')
        return offset

    result = bytearray(expected)
    template_name = 'JIT_WriteBarrier_PreGrow64'
    start = position(template_name, 1)
    end = position(template_name + '_End', 0)
    if not 1 <= end - start <= 128:
        raise ValueError('Unsupported write barrier template length')
    template = bytearray(expected[start:end])
    for suffix, global_name in [('CardTable', 'g_card_table'), ('Lower', 'g_ephemeral_low')]:
        offset = position(template_name + '_Patch_Label_' + suffix, 10) - start
        if (not 0 <= offset <= len(template) - 10 or template[offset:offset + 2] != b'\x48\xb8'
                or template[offset + 2:offset + 10] != b'\xf0' * 8):
            raise ValueError('Write barrier template placeholder mismatch')
        template[offset + 2:offset + 10] = memory[global_name][:8]
    offset = position('JIT_WriteBarrier', len(template))
    result[offset:offset + len(template)] = template
    tls = {name: struct.unpack_from('<I', memory[name])[0]
           for name in ('gThreadTLSIndex', 'gAppDomainTLSIndex', 'CExecutionEngine::TlsIndex')}
    if any(index >= 64 for index in tls.values()):
        raise ValueError('Only inline TLS slots supported')
    for name, global_name in [('GetThread', 'gThreadTLSIndex'), ('GetAppDomain', 'gAppDomainTLSIndex'),
                              ('ClrFlsGetBlock', 'CExecutionEngine::TlsIndex')]:
        offset = position(name, 10)
        result[offset:offset + 10] = b'\x65\x48\x8b\x04\x25' + struct.pack('<I', 0x1480 + tls[global_name] * 8) + b'\xc3'
    for name in SYMBOLS:
        if 'PatchTLS' not in name and 'GetThread_PatchLabel' not in name:
            continue
        offset = position(name, 9)
        if expected[offset:offset + 5] not in (b'\x65\x4c\x8b\x1c\x25', b'\x65\x48\x8b\x04\x25'):
            raise ValueError('TLS patch instruction template mismatch')
        index = tls['gAppDomainTLSIndex' if 'Shared' in name else 'gThreadTLSIndex']
        result[offset + 5:offset + 9] = struct.pack('<I', 0x1480 + 8 * index)
    return bytes(result)


def _document(value, schema_key, formal_key):
    if (not isinstance(value, dict) or type(value.get(schema_key)) is not int
            or value[schema_key] != 1 or value.get(formal_key) is not False):
        raise ValueError('Invalid diagnostic schema or formal flag')


def _unique(values, key, *, lowercase=False):
    if not isinstance(values, list) or not 1 <= len(values) <= 96:
        raise ValueError('Nonempty bounded records required')
    result = {}
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get(key), str):
            raise ValueError('Invalid record identity')
        name = value[key].lower() if lowercase else value[key]
        if name in result:
            raise ValueError('Duplicate record identity')
        result[name] = value
    return result


def _integer(value, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Integer outside supported bounds')


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('Invalid SHA256')


def _sha(value):
    return hashlib.sha256(value).hexdigest()


def _read_bytes(path, length):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
            or getattr(info, 'st_file_attributes', 0) & 0x400 or info.st_size != length):
        raise ValueError('Captured file size or type mismatch')
    with path.open('rb') as stream:
        value = stream.read(length + 1)
    if len(value) != length:
        raise ValueError('Captured file changed during read')
    return value


def _read_json(path):
    info = path.lstat()
    if info.st_size > 1024 * 1024:
        raise ValueError('Evidence JSON exceeded limit')
    return _json_bytes(_read_bytes(path, info.st_size))


def _json_bytes(value):
    encoding = 'utf-16' if value.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    return json.loads(value.decode(encoding), object_pairs_hook=_json_object)


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
    parser.add_argument('--contract', required=True, type=Path)
    parser.add_argument('--collector-sha256', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    contract_size = args.contract.lstat().st_size
    if contract_size > 1024 * 1024:
        raise ValueError('Runtime contract exceeded limit')
    contract_bytes = _read_bytes(args.contract, contract_size)
    contract = _json_bytes(contract_bytes)
    result = check(_read_json(args.capture), args.bytes_dir, contract,
                   collector_sha256=args.collector_sha256, contract_sha256=_sha(contract_bytes))
    text = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        args.output.write_text(text, encoding='utf-8')
    print(text, end='')


if __name__ == '__main__':
    main()
