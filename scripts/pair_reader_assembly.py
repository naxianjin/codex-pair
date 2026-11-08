"""从同次加载的 PE/CLI 原始字节复算读取器方法；不执行程序集或认证 JIT。"""
import hashlib
import re
import struct
import uuid


def parse_assembly(data, expected_sha256):
    """只支持 WindowsSafeInput 的小型、优化元数据布局；摘要由受控提取方固定。"""
    if (type(data) is not bytes or not 1 <= len(data) <= 1048576
            or not isinstance(expected_sha256, str)
            or not re.fullmatch('[0-9a-f]{64}', expected_sha256)
            or hashlib.sha256(data).hexdigest() != expected_sha256):
        raise ValueError('Reader assembly bytes or SHA256 mismatch')
    image = _Image(data)
    module = image.rows(0)
    types = image.rows(2)
    methods = image.rows(6)
    if len(module) != 1 or module[0][0] != 0 or module[0][3:] != (0, 0):
        raise ValueError('Unsupported reader module identity')
    mvid_index = module[0][2]
    if not mvid_index or mvid_index * 16 > len(image.streams['#GUID']):
        raise ValueError('Reader MVID outside GUID heap')
    mvid = str(uuid.UUID(bytes_le=image.streams['#GUID'][(mvid_index - 1) * 16:mvid_index * 16]))
    owner = [i for i, row in enumerate(types)
             if image.string(row[1]) == 'PairAuditInput' and image.string(row[2]) == '']
    if len(owner) != 1:
        raise ValueError('Exact reader type required')
    start = types[owner[0]][5]
    end = types[owner[0] + 1][5] if owner[0] + 1 < len(types) else len(methods) + 1
    if not 1 <= start <= end <= len(methods) + 1:
        raise ValueError('Reader method ownership invalid')
    observed = []
    for number, row in enumerate(methods, 1):
        rva, implementation, flags, name_index, signature_index, _ = row
        if not rva:
            if not flags & 0x2000:
                raise ValueError('Reader method has no IL or PInvoke declaration')
            continue
        if not start <= number < end or implementation & 3 or flags & 0x2000 or not flags & 0x10:
            raise ValueError('Unexpected managed reader method')
        name = image.string(name_index)
        signature = _signature(image.blob(signature_index), name)
        offset = image.raw(rva, 1)
        first = data[offset]
        if first & 3 == 2:
            size, header = first >> 2, 1
        elif first & 3 == 3:
            bits, _, size, _ = image.unpack('<HHII', offset)
            header = (bits >> 12) * 4
            if header != 12 or bits & 0x0fe4:
                raise ValueError('Unsupported reader IL header')
        else:
            raise ValueError('Invalid reader IL header')
        if not 1 <= size <= 1048576:
            raise ValueError('Invalid reader IL size')
        body_offset = image.raw(rva, header + size) + header
        il = data[body_offset:body_offset + size]
        observed.append({'metadata_token': 0x06000000 | number, 'name': name,
                         'signature': signature, 'il_bytes': size,
                         'il_sha256': hashlib.sha256(il).hexdigest()})
    expected_signatures = {'IntPtr OpenRoot(System.String)', 'Byte[] Read(System.String)',
                           'Byte[] ReadBytes(IntPtr, System.String)'}
    if len(observed) != 3 or {item['signature'] for item in observed} != expected_signatures:
        raise ValueError('Exact three reader IL methods required')
    return {'formal': False, 'assembly_input_sha256': expected_sha256, 'mvid': mvid,
            'methods': observed}


class _Image:
    def __init__(self, data):
        self.data = data
        if self.take(0, 2) != b'MZ':
            raise ValueError('Invalid reader DOS signature')
        pe = self.unpack('<I', 60)[0]
        if self.take(pe, 4) != b'PE\0\0':
            raise ValueError('Invalid reader PE signature')
        machine, count, _, _, _, optional_size, _ = self.unpack('<HHIIIHH', pe + 4)
        optional = pe + 24
        magic = self.unpack('<H', optional)[0]
        if (machine, magic) not in ((0x14c, 0x10b), (0x8664, 0x20b)) or not 1 <= count <= 16:
            raise ValueError('Unsupported reader PE architecture')
        directory = optional + (96 if magic == 0x10b else 112)
        if optional_size < directory - optional + 15 * 8:
            raise ValueError('Reader CLI directory absent')
        self.take(optional, optional_size)
        self.sections = []
        for i in range(count):
            _, virtual_size, rva, size, raw = self.unpack('<8sIIII', optional + optional_size + 40 * i)
            self.take(raw, size)
            self.sections.append((rva, size, raw))
        ranges = sorted((rva, rva + size) for rva, size, _ in self.sections)
        if any(a[1] > b[0] for a, b in zip(ranges, ranges[1:])):
            raise ValueError('Overlapping reader PE sections')
        cli_rva, cli_size = self.unpack('<II', directory + 14 * 8)
        if cli_size < 72:
            raise ValueError('Reader CLI header absent')
        cli = self.raw(cli_rva, 72)
        metadata_rva, metadata_size, flags = self.unpack('<III', cli + 8)
        if flags != 1:
            raise ValueError('Only IL-only reader assemblies supported')
        metadata = self.take(self.raw(metadata_rva, metadata_size), metadata_size)
        if metadata[:4] != b'BSJB':
            raise ValueError('Invalid reader metadata signature')
        version_size = self._unpack(metadata, '<I', 12)[0]
        cursor = (16 + version_size + 3) & ~3
        _, stream_count = self._unpack(metadata, '<HH', cursor)
        cursor += 4
        if not 1 <= stream_count <= 8:
            raise ValueError('Invalid reader metadata stream count')
        self.streams = {}
        stream_ranges = []
        for _ in range(stream_count):
            offset, size = self._unpack(metadata, '<II', cursor)
            cursor += 8
            end = metadata.find(b'\0', cursor, cursor + 32)
            if end < 0:
                raise ValueError('Invalid reader metadata stream name')
            name = metadata[cursor:end].decode('ascii')
            cursor = (end + 4) & ~3
            if name in self.streams:
                raise ValueError('Duplicate reader metadata stream')
            self.streams[name] = self._take(metadata, offset, size)
            stream_ranges.append((offset, offset + size))
        if set(self.streams) != {'#~', '#Strings', '#US', '#GUID', '#Blob'}:
            raise ValueError('Unsupported reader metadata streams')
        ordered = sorted(stream_ranges)
        if ordered[0][0] < cursor or any(a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
            raise ValueError('Overlapping reader metadata streams')
        tables = self.streams['#~']
        if self._unpack(tables, '<I', 0)[0] != 0 or self._take(tables, 4, 4) != b'\x02\x00\x00\x01':
            raise ValueError('Unsupported reader table encoding')
        valid = self._unpack(tables, '<Q', 8)[0]
        formats = {0: '<HHHHH', 1: '<HHH', 2: '<IHHHHH', 4: '<HHH', 6: '<IHHHHH',
                   8: '<HHH', 10: '<HHH', 12: '<HHH', 17: '<H', 26: '<H', 27: '<H',
                   28: '<HHHH', 32: '<IHHHHIHHH', 35: '<HHHHIHHHH', 41: '<HH', 43: '<HH'}
        if valid & ~sum(1 << table for table in formats):
            raise ValueError('Unsupported reader metadata table')
        counts, cursor = {}, 24
        for table in formats:
            if valid & (1 << table):
                counts[table] = self._unpack(tables, '<I', cursor)[0]
                cursor += 4
        # shortcut: 当前 reader 只使用两字节索引；更大的元数据必须新增明确支持。
        if not counts or any(value >= 2048 for value in counts.values()):
            raise ValueError('Reader metadata indices exceed supported size')
        self.tables = {}
        for table, count in counts.items():
            fmt = formats[table]
            size = struct.calcsize(fmt)
            self._take(tables, cursor, count * size)
            self.tables[table] = [self._unpack(tables, fmt, cursor + i * size) for i in range(count)]
            cursor += count * size
        if any(tables[cursor:]):
            raise ValueError('Unexpected reader metadata table trailing bytes')

    def rows(self, table):
        return self.tables.get(table, [])

    def string(self, index):
        heap = self.streams['#Strings']
        if not 0 <= index < len(heap):
            raise ValueError('Reader string index outside heap')
        end = heap.find(b'\0', index)
        if end < 0:
            raise ValueError('Unterminated reader metadata string')
        return heap[index:end].decode('utf-8')

    def blob(self, index):
        heap = self.streams['#Blob']
        if not 0 < index < len(heap):
            raise ValueError('Reader blob index outside heap')
        first = heap[index]
        if first < 128:
            length, header = first, 1
        elif first < 192:
            length, header = ((first & 63) << 8) | self._take(heap, index + 1, 1)[0], 2
        elif first < 224:
            tail = self._take(heap, index + 1, 3)
            length, header = ((first & 31) << 24) | int.from_bytes(tail, 'big'), 4
        else:
            raise ValueError('Invalid reader blob length')
        return self._take(heap, index + header, length)

    def raw(self, rva, size):
        matches = [raw + rva - start for start, length, raw in self.sections
                   if start <= rva and size <= length - (rva - start)]
        if len(matches) != 1:
            raise ValueError('Reader RVA is not fully file backed')
        return matches[0]

    def take(self, offset, size):
        return self._take(self.data, offset, size)

    def unpack(self, fmt, offset):
        return self._unpack(self.data, fmt, offset)

    @staticmethod
    def _take(data, offset, size):
        if offset < 0 or size < 0 or offset + size > len(data):
            raise ValueError('Reader assembly range outside bytes')
        return data[offset:offset + size]

    @classmethod
    def _unpack(cls, data, fmt, offset):
        return struct.unpack(fmt, cls._take(data, offset, struct.calcsize(fmt)))


def _signature(blob, name):
    if len(blob) < 3 or blob[0] != 0:
        raise ValueError('Unsupported reader method calling convention')
    cursor = 2
    names = {0x18: 'IntPtr', 0x0e: 'System.String', 0x05: 'Byte'}

    def element():
        nonlocal cursor
        if cursor >= len(blob):
            raise ValueError('Truncated reader method signature')
        tag = blob[cursor]
        cursor += 1
        if tag == 0x1d:
            return element() + '[]'
        if tag not in names:
            raise ValueError('Unsupported reader signature element')
        return names[tag]

    returned = element()
    arguments = [element() for _ in range(blob[1])]
    if cursor != len(blob):
        raise ValueError('Unexpected reader method signature bytes')
    return returned + ' ' + name + '(' + ', '.join(arguments) + ')'
