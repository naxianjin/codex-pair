"""G13新隔离fixture：实际长路径、Unicode发布、失败保全与安装回滚。"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from scripts import install_skill as install, package_skill as package


class PackagePathTests(unittest.TestCase):
    def setUp(self):
        # 现场留在执行器提供的临时盘；不递归删除Windows长路径或junction。
        fixtures=Path(tempfile.gettempdir())/'codex-pair-fixtures'
        fixtures.mkdir(parents=True,exist_ok=True)
        self.root=install.native_path(tempfile.mkdtemp(prefix='g13-',dir=fixtures))
        self.source=self.root/'source';self.source.mkdir()
        for name in package.REQUIRED | {package.HANDOFF_RUNTIME}:
            path=self.source/name;path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text('synthetic '+name,encoding='utf-8')
        (self.source/'scripts'/'中文😀.py').write_text('中文内容😀',encoding='utf-8')
        names=package.REQUIRED | {package.HANDOFF_RUNTIME,'scripts/中文😀.py'}
        (self.source/'package-manifest.json').write_text(json.dumps({'schema_version':1,
            'files':{name:hashlib.sha256((self.source/name).read_bytes()).hexdigest()
                     for name in names}},ensure_ascii=False),encoding='utf-8')
        self.expected=install.package_manifest(self.source)

    def home(self,name='home'):
        home=self.root/name;home.mkdir()
        for path in ('auth.json','config.toml','hooks/hooks.json'):
            file=home/path;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(b'synthetic sentinel')
        old=home/'skills/codex-pair';old.mkdir(parents=True)
        (old/'SKILL.md').write_bytes(b'old skill')
        (old/'notes.txt').write_bytes(b'original user notes')
        return home,old

    def sentinels(self,home):
        for path in ('auth.json','config.toml','hooks/hooks.json'):
            self.assertEqual((home/path).read_bytes(),b'synthetic sentinel')

    def deep(self,base):
        while len(str(base))<300:base=base/'中文abcdefghij'
        base.mkdir(parents=True)
        return base

    def test_ascii_chinese_nonbmp_checksum_and_deterministic_payload(self):
        hashes=[]
        for i,name in enumerate(('ascii.zip','中文 包.zip','非BMP😀.zip')):
            output=self.root/str(i)/name
            result=package.build_package(self.source,output)
            hashes.append(result['sha256'])
            self.assertEqual(install.native_path(result['checksum_file']).read_bytes(),(result['sha256']+'  '+name+'\n').encode('utf-8'))
            with zipfile.ZipFile(install.native_path(result['archive'])) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(set(archive.namelist()),{'codex-pair/'+name for name in self.expected})
                for relative,digest in self.expected.items():
                    self.assertEqual(hashlib.sha256(archive.read('codex-pair/'+relative)).hexdigest(),digest)
        self.assertEqual(len(set(hashes)),1)

    def test_checksum_write_failure_keeps_recoverable_stage_without_final_pair(self):
        output=self.root/'checksum-failure'/'中文😀.zip'
        real=Path.open
        def fail(path,*args,**kwargs):
            if path.name=='SHA256SUMS.txt':raise OSError('injected checksum write failure')
            return real(path,*args,**kwargs)
        with patch.object(Path,'open',fail),self.assertRaisesRegex(OSError,'checksum write'):
            package.build_package(self.source,output)
        self.assertFalse(output.exists());self.assertFalse(output.with_name('SHA256SUMS.txt').exists())
        recovery=next(output.parent.glob('.codex-pair-package-*'))
        with zipfile.ZipFile(recovery/output.name) as archive:self.assertIsNone(archive.testzip())

    def test_existing_archive_or_checksum_preserved(self):
        for existing in ('old.zip','SHA256SUMS.txt'):
            parent=self.root/existing.replace('.','-');parent.mkdir()
            (parent/existing).write_bytes(b'existing bytes')
            with self.assertRaises(FileExistsError):package.build_package(self.source,parent/'old.zip')
            self.assertEqual((parent/existing).read_bytes(),b'existing bytes')

    def test_output_in_source_and_ambiguous_checksum_names_rejected(self):
        with self.assertRaises(ValueError):package.build_package(self.source,self.source/'bad.zip')
        if os.name!='nt':
            with self.assertRaises(ValueError):package.build_package(self.source,self.root/'bad\nname.zip')

    def test_published_pair_stays_valid_if_staging_cleanup_fails(self):
        real=Path.unlink
        def fail(path,*args,**kwargs):
            if path.name=='complete.zip':raise OSError('injected staging cleanup failure')
            return real(path,*args,**kwargs)
        with patch.object(Path,'unlink',fail):result=package.build_package(self.source,self.root/'cleanup'/'complete.zip')
        self.assertTrue(Path(result['staging_directory']).is_dir())
        self.assertEqual(hashlib.sha256(Path(result['archive']).read_bytes()).hexdigest(),result['sha256'])
        self.assertTrue(Path(result['checksum_file']).read_bytes().startswith(result['sha256'].encode('ascii')))

    def test_concurrent_checksum_creation_rolls_back_only_owned_archive(self):
        output=self.root/'race'/'中文.zip';real=os.link
        def race(source,target,*args,**kwargs):
            if Path(target).name=='SHA256SUMS.txt':Path(target).write_bytes(b'concurrent user checksum')
            return real(source,target,*args,**kwargs)
        with patch.object(package.os,'link',race),self.assertRaises(FileExistsError):package.build_package(self.source,output)
        self.assertFalse(output.exists())
        self.assertEqual(output.with_name('SHA256SUMS.txt').read_bytes(),b'concurrent user checksum')
        recovery=next(output.parent.glob('.codex-pair-package-*'))
        self.assertTrue((recovery/output.name).is_file())
        self.assertTrue((recovery/('published-'+output.name)).is_file())

    def test_concurrent_archive_is_never_overwritten(self):
        output=self.root/'archive-race'/'a.zip';real=os.link
        def race(source,target,*args,**kwargs):
            if Path(target).name=='a.zip':Path(target).write_bytes(b'concurrent archive')
            return real(source,target,*args,**kwargs)
        with patch.object(package.os,'link',race),self.assertRaises(FileExistsError):package.build_package(self.source,output)
        self.assertEqual(output.read_bytes(),b'concurrent archive')
        self.assertFalse(output.with_name('SHA256SUMS.txt').exists())

    def test_interruption_between_publications_leaves_explicit_pending_and_refuses_retry(self):
        output=self.root/'interruption'/'中文😀.zip';real=os.link
        def interrupt(source,target,*args,**kwargs):
            if Path(target).name=='SHA256SUMS.txt':raise KeyboardInterrupt('injected publication interruption')
            return real(source,target,*args,**kwargs)
        with patch.object(package.os,'link',interrupt),self.assertRaises(KeyboardInterrupt):
            package.build_package(self.source,output)
        pending=output.with_name('.'+output.name+'.pending.json')
        self.assertTrue(pending.is_file());self.assertTrue(output.is_file())
        self.assertIn('incomplete',pending.read_text(encoding='utf-8'))
        before=output.read_bytes()
        with self.assertRaises(FileExistsError):package.build_package(self.source,output)
        self.assertEqual(before,output.read_bytes())

    def test_manifest_stat_error_is_not_silently_omitted(self):
        real=Path.stat
        def fail(path,*args,**kwargs):
            if path.name=='中文😀.py':raise PermissionError('injected stat failure')
            return real(path,*args,**kwargs)
        with patch.object(Path,'stat',fail),self.assertRaisesRegex(PermissionError,'stat failure'):
            install.package_manifest(self.source)

    def test_long_source_stage_target_backup_and_failed_are_complete(self):
        source=self.deep(self.root/'long-source')/'source'
        shutil.copytree(self.source,source)
        self.assertEqual(install.package_manifest(source),self.expected)
        home=self.deep(self.root/'long-home')/'home';home.mkdir()
        old=home/'skills/codex-pair';old.mkdir(parents=True)
        shutil.copytree(source,old,dirs_exist_ok=True)
        for path in ('auth.json','config.toml','hooks/hooks.json'):
            file=home/path;file.parent.mkdir(parents=True,exist_ok=True);file.write_bytes(b'synthetic sentinel')
        stages=[];real=install.package_manifest
        def capture(path):
            result=real(path)
            if Path(path).name.startswith('.codex-pair-install-'):stages.append(result)
            return result
        with patch.object(install,'package_manifest',capture):result=install.install_skill(str(source),[str(home),str(home)])
        self.assertEqual(len(result['installations']),1)
        self.assertEqual(stages,[self.expected])
        self.assertEqual(real(old),self.expected)
        self.assertEqual(real(Path(result['installations'][0]['backup'])),self.expected)
        count=0
        def post_failure(path):
            nonlocal count
            value=real(path)
            if Path(path)==old:
                count+=1
                if count==1:return {}
            return value
        with patch.object(install,'package_manifest',post_failure),self.assertRaisesRegex(ValueError,'安装后哈希'):
            install.install_skill(str(source),[str(home)])
        failed=list(old.parent.glob('.codex-pair-failed-*'))
        self.assertEqual(len(failed),1)
        self.assertEqual(real(failed[0]),self.expected)
        self.assertEqual(real(old),self.expected)
        self.sentinels(home)
        result=package.build_package(source,self.deep(self.root/'long-output')/'中文😀.zip')
        self.assertEqual(result['files'],self.expected)

    def test_partial_copy_preserves_old_target_and_failed_stage(self):
        home,old=self.home();real=shutil.copy2;count=0
        def fail(source,destination,*args,**kwargs):
            nonlocal count
            count+=1
            if count==2:raise OSError('injected copy failure')
            return real(source,destination,*args,**kwargs)
        with patch.object(install.shutil,'copy2',fail),self.assertRaisesRegex(OSError,'copy failure'):
            install.install_skill(str(self.source),[str(home)])
        self.assertEqual((old/'notes.txt').read_bytes(),b'original user notes')
        self.assertTrue(list(old.parent.glob('.codex-pair-install-*')))
        self.assertFalse((home/'codex-pair-skill-backups').exists())
        self.sentinels(home)

    def test_all_homes_preflight_and_source_target_overlap(self):
        home,old=self.home()
        with self.assertRaises(FileNotFoundError):install.install_skill(str(self.source),[str(home),str(self.root/'missing')])
        self.assertEqual((old/'notes.txt').read_bytes(),b'original user notes')
        self.assertFalse(list(old.parent.glob('.codex-pair-install-*')))
        with self.assertRaises(ValueError):install.install_skill(str(old),[str(home)])
        self.sentinels(home)

    def test_install_lock_and_target_created_during_copy_are_not_overwritten(self):
        home,old=self.home()
        lock=old.parent/'.codex-pair-install-lock';lock.mkdir()
        with self.assertRaises(FileExistsError):install.install_skill(str(self.source),[str(home)])
        lock.rmdir()
        other=self.root/'empty-home';other.mkdir();target=other/'skills/codex-pair'
        real=shutil.copy2;created=False
        def race(source,destination,*args,**kwargs):
            nonlocal created
            if not created:
                created=True;target.mkdir();(target/'user.md').write_bytes(b'concurrent user target')
            return real(source,destination,*args,**kwargs)
        with patch.object(install.shutil,'copy2',race),self.assertRaisesRegex(ValueError,'发生变化'):
            install.install_skill(str(self.source),[str(other)])
        self.assertEqual((target/'user.md').read_bytes(),b'concurrent user target')

    @unittest.skipUnless(os.name=='nt','Windows junction behavior')
    def test_real_junction_source_target_backup_and_output_rejected(self):
        real=self.root/'real';real.mkdir();(real/'marker.md').write_bytes(b'untouched')
        def junction(link):
            # 本测试所有路径均由新fixture产生，cmd仅用于创建junction，不删除或移动。
            plain=lambda p:str(p)[4:]
            result=subprocess.run(['cmd','/d','/c','mklink','/J',plain(link),plain(real)],capture_output=True)
            self.assertEqual(result.returncode,0,result.stderr)
        source=self.root/'redirect-source';source.mkdir();(source/'SKILL.md').write_bytes(b'skill')
        junction(source/'references')
        (source/'package-manifest.json').write_text(json.dumps({'schema_version':1,
            'files':{'SKILL.md':hashlib.sha256(b'skill').hexdigest(),
                     'references/marker.md':hashlib.sha256(b'untouched').hexdigest()}}),encoding='utf-8')
        with self.assertRaises(ValueError):install.package_manifest(source)
        root_link=self.root/'source-root-link';junction(root_link)
        with self.assertRaises(ValueError):install.package_manifest(root_link)
        home=self.root/'redirect-home';(home/'skills').mkdir(parents=True)
        junction(home/'skills/codex-pair')
        with self.assertRaises(ValueError):install.install_skill(str(self.source),[str(home)])
        backup_home=self.root/'backup-home';backup_home.mkdir()
        junction(backup_home/'codex-pair-skill-backups')
        with self.assertRaises(ValueError):install.install_skill(str(self.source),[str(backup_home)])
        output=self.root/'redirect-output';junction(output)
        with self.assertRaises(ValueError):package.build_package(self.source,output/'包.zip')
        self.assertEqual((real/'marker.md').read_bytes(),b'untouched')


if __name__=='__main__':unittest.main()
