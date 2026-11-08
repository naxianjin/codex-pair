"""真实编译后独立解析原始 CLI 字节，并与运行时反射结果对照。"""
import hashlib
import os
from pathlib import Path
import struct
import unittest

from scripts.pair_reader_assembly import _Image, parse_assembly


from tests.reader_fixture import SHELL, compiled_reader


@unittest.skipUnless(os.name == 'nt' and SHELL.is_file(), 'Windows .NET compiler required')
class ReaderAssemblyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data, cls.reflected = compiled_reader()
        cls.digest = hashlib.sha256(cls.data).hexdigest()

    def parse(self, data=None):
        data = self.data if data is None else bytes(data)
        return parse_assembly(data, hashlib.sha256(data).hexdigest())

    def test_independent_bytes_match_all_reflected_method_bodies(self):
        result = self.parse()
        self.assertIs(result['formal'], False)
        self.assertEqual(result['assembly_input_sha256'], self.digest)
        self.assertEqual(result['mvid'], self.reflected['mvid'])
        self.assertEqual(result['methods'], self.reflected['methods'])
        self.assertEqual([method['name'] for method in result['methods']], ['OpenRoot', 'Read', 'ReadBytes'])
        image = _Image(self.data)
        self.assertEqual(sum(not row[0] and bool(row[2] & 0x2000) for row in image.rows(6)), 6)

    def test_digest_and_input_type_are_required(self):
        for data, digest in [(self.data, '0' * 64), (self.data, self.digest.upper()),
                             (bytearray(self.data), self.digest), (b'', hashlib.sha256(b'').hexdigest())]:
            with self.subTest(digest=digest), self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
                parse_assembly(data, digest)

    def test_truncated_and_invalid_images_are_rejected(self):
        for data in (self.data[:1], self.data[:63], self.data[:512], self.data[:-512], b'?' + self.data[1:]):
            with self.subTest(length=len(data)), self.assertRaises(ValueError):
                self.parse(data)

    def test_metadata_signature_and_stream_layout_are_rejected(self):
        for needle, replacement in ((b'BSJB', b'BSJ?'), (b'#GUID\0', b'#Bogus'),
                                    (b'#Blob\0', b'#GUID\0')):
            data = bytearray(self.data)
            offset = data.index(needle)
            data[offset:offset + len(needle)] = replacement
            with self.subTest(needle=needle), self.assertRaises(ValueError):
                self.parse(data)

    def test_reader_type_and_method_identity_are_required(self):
        for name in (b'PairAuditInput\0', b'OpenRoot\0', b'ReadBytes\0'):
            data = bytearray(self.data)
            data[data.index(name)] = ord('X')
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.parse(data)

    def test_invalid_il_header_and_body_length_are_rejected(self):
        image = _Image(self.data)
        row = next(row for row in image.rows(6) if row[0])
        position = image.raw(row[0], 12)
        data = bytearray(self.data)
        data[position] &= 0xfc
        with self.assertRaisesRegex(ValueError, 'IL header'):
            self.parse(data)
        data = bytearray(self.data)
        self.assertEqual(data[position] & 3, 3)
        struct.pack_into('<I', data, position + 4, 1048575)
        with self.assertRaisesRegex(ValueError, 'file backed'):
            self.parse(data)

    def test_large_or_unsupported_metadata_indices_are_rejected(self):
        image = _Image(self.data)
        tables = image.streams['#~']
        offset = self.data.index(tables)
        for relative, value in ((6, 1), (24, 0xff)):
            data = bytearray(self.data)
            if relative == 24:
                struct.pack_into('<I', data, offset + relative, 2048)
            else:
                data[offset + relative] = value
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                self.parse(data)

    def test_method_byte_changes_are_independently_reflected_in_digest(self):
        image = _Image(self.data)
        row = next(row for row in image.rows(6) if row[0])
        offset = image.raw(row[0], 12)
        data = bytearray(self.data)
        data[offset + 12] ^= 1
        result = self.parse(data)
        self.assertNotEqual(result['methods'][0]['il_sha256'], self.reflected['methods'][0]['il_sha256'])
        self.assertEqual(result['mvid'], self.reflected['mvid'])


if __name__ == '__main__':
    unittest.main()
