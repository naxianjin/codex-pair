from pathlib import Path
import hashlib
import shutil
import subprocess
import sys
import os
import tempfile
import unittest
import zipfile

from scripts.install_skill import install_skill, package_manifest
from scripts.package_skill import build_package, RUNTIME_ROOTS


SOURCE=Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def test_downloadable_skill_has_single_root_and_no_local_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source=root/'source';source.mkdir()
            for name in package_manifest(SOURCE):
                if Path(name).parts[0] not in RUNTIME_ROOTS:continue
                destination=source/name;destination.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(SOURCE/name,destination)
            for name in ('auth.json','config.toml','.codex-pair/state/private.sqlite3','tests/test_private.py','scripts/__pycache__/private.pyc'):
                path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('PRIVATE',encoding='utf-8')
            report=build_package(source,root/'download'/'codex-pair.zip')
            with zipfile.ZipFile(report['archive']) as bundle:
                names=bundle.namelist()
                self.assertIn('codex-pair/SKILL.md',names)
                self.assertIn('codex-pair/scripts/install_skill.py',names)
                self.assertIn('codex-pair/package-manifest.json',names)
                self.assertIn('codex-pair/assets/audit/report-schema.json',names)
                self.assertIn('codex-pair/README.md',names)
                self.assertIn('codex-pair/POST_RELEASE_TESTS.md',names)
                self.assertTrue(all(name.startswith('codex-pair/') for name in names))
                self.assertFalse(any(part in name for name in names for part in ('tests/','__pycache__','auth.json','config.toml','.codex-pair/')))
                for name in names:self.assertNotIn(b'PRIVATE',bundle.read(name))
                bundle.extractall(root/'unpacked')
            account=root/'account';account.mkdir()
            (account/'auth.json').write_bytes(b'original login')
            old=account/'skills'/'codex-pair';old.mkdir(parents=True)
            (old/'user-note.txt').write_text('keep',encoding='utf-8')
            installed=install_skill(str(root/'unpacked'/'codex-pair'),[str(account)])
            self.assertEqual(package_manifest(root/'unpacked'/'codex-pair'),package_manifest(old))
            self.assertEqual((account/'auth.json').read_bytes(),b'original login')
            self.assertEqual((Path(installed['installations'][0]['backup'])/'user-note.txt').read_text(),'keep')
            self.assertEqual(Path(report['checksum_file']).read_text().split()[0],hashlib.sha256(Path(report['archive']).read_bytes()).hexdigest())

    @unittest.skipUnless(os.name == 'nt' and shutil.which('node'), 'Installed Windows validation requires Node')
    def test_installed_entry_and_validator_run_outside_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            report=build_package(SOURCE,root/'download'/'codex-pair.zip')
            with zipfile.ZipFile(report['archive']) as bundle:
                bundle.extractall(root/'unpacked')
            home=root/'profile';home.mkdir()
            install_skill(str(root/'unpacked'/'codex-pair'),[str(home)])
            installed=home/'skills'/'codex-pair'
            cwd=root/'empty-working-directory';cwd.mkdir()
            help_result=subprocess.run([sys.executable,'-B',str(installed/'scripts/pair.py'),'--help'],
                                       cwd=cwd,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
            self.assertEqual(help_result.returncode,0,help_result.stderr)
            self.assertIn('usage:',help_result.stdout.lower())
            document=cwd/'coverage.json';document.write_text('[]',encoding='utf-8')
            checked=subprocess.run([shutil.which('node'),str(installed/'assets/audit/validate-coverage-ledger.cjs'),str(document)],
                                   cwd=cwd,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
            self.assertEqual(checked.returncode,0,checked.stderr)
            self.assertEqual(package_manifest(installed),package_manifest(SOURCE))

    def test_repeat_build_is_reproducible_and_existing_files_are_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            first=build_package(SOURCE,root/'one'/'codex-pair.zip')
            second=build_package(SOURCE,root/'two'/'codex-pair.zip')
            self.assertEqual(first['sha256'],second['sha256'])
            original=Path(first['archive']).read_bytes()
            with self.assertRaises(FileExistsError):build_package(SOURCE,first['archive'])
            self.assertEqual(original,Path(first['archive']).read_bytes())


if __name__=='__main__':unittest.main()
