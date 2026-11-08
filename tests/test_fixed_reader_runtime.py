"""准确本地产物的同次加载和句柄释放；不认证构建来源或 JIT。"""
import base64
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.pair_reader_assembly import parse_assembly


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'validation/bytecode-provenance-20261105/Invoke-FixedReaderPrototype.ps1'
ASSEMBLY = Path.home() / 'Downloads/system-image-review/fixed-reader-r4/build/reader.dll'
SHA256 = '43ce0c2ae473bb129c60d3b98b9a36722846e2c2a6d2754afab76151973d27a2'
SHELL = Path(r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe')
MARKER = b'PAIR_FIXED_READER_OBSERVATION:'
EVIDENCE = Path.home() / 'Downloads/system-image-review/fixed-reader-runtime-r5'


@unittest.skipUnless(os.name == 'nt' and SHELL.is_file() and ASSEMBLY.is_file(),
                     'Exact fixed reader local artifact required')
class FixedReaderRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertEqual(hashlib.sha256(ASSEMBLY.read_bytes()).hexdigest(), SHA256)
        EVIDENCE.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=EVIDENCE)
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.dll = self.directory / 'reader.dll'
        shutil.copyfile(ASSEMBLY, self.dll)
        self.input = self.directory / 'input.json'
        self.content = b'["fixed input", "\xe4\xb8\xad\xe6\x96\x87"]'
        self.input.write_bytes(self.content)

    def argv(self, *, dll=None, digest=SHA256, target=None):
        return [str(SHELL), '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
                'RemoteSigned', '-File', str(SCRIPT), '-AssemblyPath', str(dll or self.dll),
                '-ExpectedSHA256', digest, '-LiteralPath', str(target or self.input)]

    def run_reader(self, **changes):
        return subprocess.run(self.argv(**changes), capture_output=True, timeout=30)

    def observation(self, stderr):
        lines = stderr.splitlines()
        self.assertEqual(len(lines), 1, stderr.decode(errors='replace'))
        self.assertTrue(lines[0].startswith(MARKER))
        return json.loads(lines[0][len(MARKER):])

    def test_real_fixed_artifact_loads_exact_bytes_and_reads_input(self):
        result = self.run_reader()
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertEqual(result.stdout, self.content)
        observed = self.observation(result.stderr)
        self.assertIs(observed['formal'], False)
        self.assertEqual(observed['schema_version'], 1)
        self.assertGreater(observed['pid'], 0)
        self.assertTrue(observed['process_creation_utc'].endswith('Z'))
        data = base64.b64decode(observed['assembly_input_base64'], validate=True)
        self.assertEqual(data, ASSEMBLY.read_bytes())
        parsed = parse_assembly(data, SHA256)
        self.assertEqual(observed['assembly_input_sha256'], SHA256)
        self.assertEqual(observed['assembly_location'], '')
        self.assertEqual(observed['mvid'], parsed['mvid'])
        self.assertEqual(sorted(observed['methods'], key=lambda x: x['metadata_token']), parsed['methods'])

    def test_digest_mismatch_rejects_before_read_and_releases_dll(self):
        result = self.run_reader(digest='0' * 64, target=self.directory / 'absent')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertIn(b'Fixed reader assembly SHA256 mismatch', result.stderr)
        self.assertNotIn(MARKER, result.stderr)
        self._open_write()

    def test_invalid_assembly_with_matching_digest_is_rejected(self):
        data = b'not a managed assembly'
        self.dll.write_bytes(data)
        result = self.run_reader(digest=hashlib.sha256(data).hexdigest())
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertNotIn(MARKER, result.stderr)
        self._open_write()

    def test_input_failure_releases_dll_without_success_observation(self):
        result = self.run_reader(target=self.directory / 'absent')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b'')
        self.assertNotIn(MARKER, result.stderr)
        self._open_write()

    def test_external_path_and_digest_boundary(self):
        for changes in ({'dll': Path('reader.dll')}, {'digest': SHA256.upper()}, {'digest': 'bad'}):
            with self.subTest(changes=changes):
                result = self.run_reader(**changes)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, b'')
                self.assertIn(b'Absolute assembly path and lowercase SHA256 required', result.stderr)

    def test_dll_write_sharing_is_denied_until_output_finishes(self):
        self.input.write_bytes(b'x' * (1024 * 1024))
        process = subprocess.Popen(self.argv(), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            line = process.stderr.readline()
            self.observation(line)
            self.assertIsNone(process.poll())
            self.assertEqual(self._open_write(expect_failure=True), 32)
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(process.returncode, 0, stderr.decode(errors='replace'))
            self.assertEqual(stdout, self.input.read_bytes())
            self.assertEqual(stderr, b'')
            self._open_write()
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()
            process.stdout.close()
            process.stderr.close()

    def _open_write(self, *, expect_failure=False):
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(self.dll), 0x40000000, 3, None, 3, 0, None)
        if handle == wintypes.HANDLE(-1).value:
            error = ctypes.get_last_error()
            if not expect_failure:
                raise ctypes.WinError(error)
            return error
        kernel.CloseHandle(handle)
        if expect_failure:
            self.fail('DLL unexpectedly allowed write access while held for reading')


if __name__ == '__main__':
    unittest.main()
