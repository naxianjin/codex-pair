"""真实_session与临时Git目录；仅网络对象为假，不启动模型。"""
import copy
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from scripts import pair_review_runtime as runtime
from scripts.pair_core import PairError
from scripts.pair_transport import canonical_path
from tests import test_core,test_reviewer,test_integration


class ReviewerCwdTests(unittest.TestCase):
    tearDown=test_core.CoreTests.tearDown

    def setUp(self):
        test_core.CoreTests.setUp(self)
        self.project.rmdir();test_integration.seed(self.project)
        self.head=test_integration.git(self.project,'rev-parse','HEAD')
        helper=test_reviewer.ReviewerTests();helper.__dict__.update(self.__dict__);helper.parent()
        self.ticket=self.store.reserve_reviewer('A',self.sessions['A'],self.directories['A'])
        self.review_root=runtime.prepare_review_tree(self.store,self.head)
        self.proof={'id':'00000000-0000-0000-0000-000000000099','cwd':self.review_root,
                    'modelProvider':'openai','model':'gpt-6.1-sol','ephemeral':False}
        self.store.bind_reviewer('A',self.sessions['A'],self.directories['A'],epoch=self.ticket['epoch'],
            attempt=self.ticket['attempt'],proof=self.proof,reviewer_cwd=self.review_root)
        self.calls=[];self.created=[];self.mutate=None

    def run_session(self):
        fixture=self
        class Client:
            def __init__(self,**kw):fixture.created.append(kw)
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def close(self):pass
            def validate(self,member):return json.loads(member['proof'])
            def rpc(self,method,params):
                fixture.calls.append((method,params))
                if method=='account/read':return {'account':{'type':'chatgpt'}}
                if method=='thread/resume':return {'thread':{'id':fixture.proof['id']}}
                raise AssertionError('Forbidden model action: '+method)
            def read_thread(self,sid):
                if fixture.mutate:fixture.mutate()
                return copy.deepcopy(fixture.proof)
        with patch.object(runtime,'AppServer',Client):
            return runtime.ReviewerController(self.store)._session(self.head)

    def test_same_existing_directory_reuses_original_uuid_without_thread_start(self):
        state=self.run_session()
        self.assertEqual(state['session'],self.proof['id'])
        self.assertEqual([p for m,p in self.calls if m=='thread/resume'],
                         [{'threadId':self.proof['id'],'excludeTurns':True}])
        self.assertFalse(any(m in ('thread/start','turn/start','turn/interrupt') for m,p in self.calls))

    def test_case_spelling_difference_is_same_entity(self):
        if os.name!='nt':self.skipTest('本例验证Windows大小写路径表示')
        with self.store.transaction() as db:
            db.execute('UPDATE reviewer_runtime SET cwd=?',(self.review_root.swapcase(),))
        self.assertTrue(Path(self.review_root.swapcase()).samefile(self.review_root))
        self.assertEqual(self.run_session()['session'],self.proof['id'])

    def test_other_missing_file_or_traversal_directory_never_resumes(self):
        other=self.root/'other';other.mkdir();file=self.root/'file';file.write_text('fixture')
        for path in (str(other),str(self.root/'missing'),str(file),str(Path(self.review_root)/'..'/Path(self.review_root).name),'relative'):
            with self.subTest(path=path):
                self.calls.clear();self.created.clear()
                with self.store.transaction() as db:db.execute('UPDATE reviewer_runtime SET cwd=?',(path,))
                with self.assertRaises(PairError):self.run_session()
                self.assertFalse(any(m=='thread/resume' for m,p in self.calls))
                self.assertFalse(any(not c.get('metadata_only',False) for c in self.created))

    def test_wrong_reviewer_model_or_uuid_and_parent_or_home_are_rejected(self):
        for changes in ({'model':'gpt-other'},{'modelProvider':'deepseek'},{'id':self.sessions['B']},{'cwd':self.directories['B']}):
            with self.subTest(changes=changes):
                original=self.proof;self.proof=dict(original,**changes)
                with self.assertRaises(PairError):self.run_session()
                self.proof=original
        for column,value in [('parent_session',self.sessions['B']),('codex_home',str(self.root))]:
            with self.subTest(column=column):
                with self.store.transaction() as db:
                    original=db.execute('SELECT '+column+' FROM reviewer_runtime').fetchone()[0]
                    db.execute('UPDATE reviewer_runtime SET '+column+'=?',(value,))
                with self.assertRaises(PairError):self.run_session()
                with self.store.transaction() as db:db.execute('UPDATE reviewer_runtime SET '+column+'=?',(original,))

    def test_epoch_change_during_metadata_read_is_rejected(self):
        def change():
            with self.store.transaction() as db:db.execute('UPDATE reviewer_runtime SET epoch=epoch+1')
        self.mutate=change
        with self.assertRaises(PairError):self.run_session()

    def test_symlink_or_junction_alias_is_not_plain_path_spelling(self):
        link=self.root/'redirect'
        try:
            try:link.symlink_to(self.review_root,target_is_directory=True)
            except OSError:
                if os.name!='nt':raise
                result=subprocess.run(['cmd','/c','mklink','/J',str(link),self.review_root],capture_output=True)
                if result.returncode:self.skipTest('系统不允许创建隔离目录链接或junction')
            with self.store.transaction() as db:db.execute('UPDATE reviewer_runtime SET cwd=?',(str(link),))
            with self.assertRaises(PairError):self.run_session()
            self.assertFalse(any(m=='thread/resume' for m,p in self.calls))
            with self.store.transaction() as db:db.execute('UPDATE reviewer_runtime SET cwd=?',(canonical_path(self.review_root),))
            self.proof['cwd']=str(link)
            with self.assertRaises(PairError):self.run_session()
        finally:
            if link.is_symlink():link.unlink()
            elif link.exists():link.rmdir()
