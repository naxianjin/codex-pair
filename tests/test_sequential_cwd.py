"""真实审批及结算事务的配置竞态回归；只用临时库和假网络。"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from tests import test_sequential_permissions as sequential


class SequentialCwdTests(unittest.TestCase):
    def fixture(self):
        fixture=sequential.SequentialTests();fixture.setUp()
        self.addCleanup(fixture.tearDown)
        self.addCleanup(fixture.doCleanups)
        return fixture

    def exercise(self,fixture,mutate,*,boundary='read'):
        controller=fixture.controller()
        original=fixture.store.transaction
        armed=False;transactions=0
        def turns(*args,**kwargs):
            nonlocal armed
            if boundary=='read':mutate(original)
            armed=True
            return iter([dict(fixture.turn,status='inProgress')])
        @contextmanager
        def scheduled_transaction():
            nonlocal transactions
            if armed:
                transactions+=1
                if boundary=='settle' and transactions==2:mutate(original)
            with original() as db:yield db
        controller.client.turns=turns
        controller.client.server_requests.put(fixture.second())
        # 只安排竞态时点，所有事务、capture及settle均执行真实实现。
        with patch.object(fixture.store,'transaction',side_effect=scheduled_transaction):
            controller._permissions()
        return fixture.status(),fixture.permissions(),fixture.responses

    def denied(self,result):
        status,rows,responses=result
        self.assertIn(status,('granted','unknown'))
        self.assertEqual(len(rows),1)
        self.assertEqual(responses,[{'id':2,'result':{'decision':'decline'}}])

    def variants(self,fixture):
        return [
            ('reviewer_runtime','cwd',fixture.directories['B']),
            ('reviewer_runtime','codex_home',fixture.directories['B']),
            ('reviewer_runtime','model','gpt-other'),
            ('reviewer_runtime','parent_session',fixture.sessions['B']),
            ('reviewer_runtime','session_uuid',fixture.sessions['B']),
            ('reviewer_runtime','epoch',fixture.ticket['epoch']+1),
            ('reviewer_runtime','execution_id','other-execution'),
            ('reviewer_runtime','owner','other-owner'),
            ('reviewer_runtime','state','unknown'),
            ('members','cwd',fixture.directories['B']),
            ('members','codex_home',fixture.directories['B']),
            ('members','proof',json.dumps(dict(fixture.proof,model='gpt-other'))),
            ('members','binding_version',2),
            ('members','session_uuid',fixture.sessions['B']),
            ('members','lifecycle','suspended'),
            ('members','verified',0),
            ('members','online',0),
        ]

    def config_changes(self,boundary):
        for index in range(17):
            fixture=self.fixture()
            table,column,value=self.variants(fixture)[index]
            with self.subTest(boundary=boundary,table=table,column=column):
                def mutate(transaction):
                    with transaction() as db:
                        db.execute('UPDATE '+table+' SET '+column+'=?'+(" WHERE id='A'" if table=='members' else ''),(value,))
                self.denied(self.exercise(fixture,mutate,boundary=boundary))

    def test_reading_configuration_changes_keep_unresolved(self):
        self.config_changes('read')

    def test_actual_settlement_transaction_rechecks_full_configuration(self):
        self.config_changes('settle')

    def test_same_directory_case_representation_remains_valid(self):
        if os.name!='nt':self.skipTest('Windows大小写目录表示')
        for boundary in ('read','settle'):
            fixture=self.fixture()
            with self.subTest(boundary=boundary):
                def mutate(transaction):
                    with transaction() as db:
                        row=fixture.store._reviewer_row(db)
                        db.execute('UPDATE reviewer_runtime SET cwd=?,codex_home=?',(row['cwd'].swapcase(),row['codex_home'].swapcase()))
                status,rows,responses=self.exercise(fixture,mutate,boundary=boundary)
                self.assertEqual(status,'success')
                self.assertEqual([r['status'] for r in rows],['success','pending'])
                self.assertEqual(responses,[])

    def test_directory_same_spelling_replaced_entity_is_rejected(self):
        for boundary in ('read','settle'):
            fixture=self.fixture()
            with self.subTest(boundary=boundary):
                def mutate(transaction):
                    path=Path(fixture.proof['cwd'])
                    path.rename(path.with_name(path.name+'-original'))
                    path.mkdir()
                self.denied(self.exercise(fixture,mutate,boundary=boundary))

    def test_missing_file_and_redirected_directory_are_rejected(self):
        for kind in ('missing','file','link'):
            fixture=self.fixture()
            target=fixture.root/'alternate'
            if kind=='file':target.write_text('fixture')
            if kind=='link':
                try:target.symlink_to(fixture.proof['cwd'],target_is_directory=True)
                except OSError:
                    result=subprocess.run(['cmd','/c','mklink','/J',str(target),fixture.proof['cwd']],capture_output=True)
                    self.assertEqual(result.returncode,0)
                self.addCleanup(lambda p=target:p.rmdir() if p.exists() and not p.is_symlink() else p.unlink(missing_ok=True))
            with self.subTest(kind=kind):
                def mutate(transaction):
                    with transaction() as db:db.execute('UPDATE reviewer_runtime SET cwd=?',(str(target),))
                self.denied(self.exercise(fixture,mutate))

    def test_original_cwd_does_not_accept_redirect_before_read(self):
        fixture=self.fixture()
        with fixture.store.transaction() as db:db.execute('UPDATE reviewer_runtime SET cwd=?',(fixture.directories['B'],))
        controller=fixture.controller()
        controller.client.server_requests.put(fixture.second());controller._permissions()
        self.denied((fixture.status(),fixture.permissions(),fixture.responses))


if __name__=='__main__':unittest.main()
