"""明确发行清单；这些测试只能在已验收的隔离执行器中运行。"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts.install_skill import install_skill, package_manifest


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.contents = {'SKILL.md': b'skill', 'assets/rule.json': b'{"rule":1}',
                         'assets/validate.cjs': b'// fixed validation asset',
                         'licenses/license.txt': b'license', 'README.md': b'readme',
                         'POST_RELEASE_TESTS.md': b'unverified', 'CHANGELOG.md': b'changes'}
        for name, content in self.contents.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        self.files = {name: hashlib.sha256(content).hexdigest() for name, content in self.contents.items()}
        self.write_manifest()

    def write_manifest(self):
        (self.source / 'package-manifest.json').write_text(json.dumps({'schema_version': 1,
            'files': self.files}), encoding='utf-8')

    def test_only_explicit_files_and_self_manifest_are_selected(self):
        for name in ('tests/test_private.py', 'scripts/auth.json', '.codex-pair/state.sqlite3', 'assets/unlisted.bin'):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'private sentinel')
        manifest = package_manifest(self.source)
        self.assertEqual(set(manifest), set(self.files) | {'package-manifest.json'})
        self.assertEqual({name: manifest[name] for name in self.files}, self.files)

    def test_no_manifest_never_falls_back_to_extension_scan(self):
        (self.source / 'package-manifest.json').unlink()
        with self.assertRaises(FileNotFoundError):
            package_manifest(self.source)

    def test_modified_missing_or_invalid_digest_fails(self):
        (self.source / 'assets/rule.json').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '摘要不一致'):
            package_manifest(self.source)
        (self.source / 'assets/rule.json').unlink()
        with self.assertRaises(FileNotFoundError):
            package_manifest(self.source)
        self.files['assets/rule.json'] = 'not-a-digest'
        self.write_manifest()
        with self.assertRaisesRegex(ValueError, '摘要无效'):
            package_manifest(self.source)

    def test_private_escape_alias_and_database_entries_rejected(self):
        original = self.files.copy()
        for name in ('../auth.json', 'scripts/auth.json', 'tests/test_a.py', '/SKILL.md',
                     'assets/a:stream', 'assets/../a.json', 'assets//a.json', 'assets/nul.json',
                     'assets/a.json.', 'assets/state.sqlite3', 'assets/events.jsonl'):
            with self.subTest(name=name):
                self.files = {**original, name: 'a' * 64}
                self.write_manifest()
                with self.assertRaisesRegex(ValueError, '不允许的路径'):
                    package_manifest(self.source)

    def test_duplicate_json_fields_and_windows_alias_rejected(self):
        path = self.source / 'package-manifest.json'
        path.write_text('{"schema_version":1,"files":{},"files":{}}', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '重复字段'):
            package_manifest(self.source)
        self.files['readme.md'] = self.files['README.md']
        self.write_manifest()
        with self.assertRaises(ValueError):
            package_manifest(self.source)

    def test_install_keeps_runtime_assets_docs_and_credentials(self):
        home = self.root / 'home'
        home.mkdir()
        (home / 'auth.json').write_bytes(b'credential sentinel')
        old = home / 'skills/codex-pair'
        old.mkdir(parents=True)
        (old / 'notes.txt').write_bytes(b'user note')
        result = install_skill(str(self.source), [str(home)])
        self.assertEqual(package_manifest(old), package_manifest(self.source))
        self.assertEqual((home / 'auth.json').read_bytes(), b'credential sentinel')
        self.assertEqual((Path(result['installations'][0]['backup']) / 'notes.txt').read_bytes(), b'user note')

    def test_registered_hardlink_rejected(self):
        target = self.source / 'assets/rule.json'
        try:
            os.link(target, self.root / 'shared.json')
        except OSError as error:
            self.skipTest('Hardlink fixture unavailable: ' + str(error))
        with self.assertRaisesRegex(ValueError, '硬链接'):
            package_manifest(self.source)


if __name__ == '__main__':
    unittest.main()
