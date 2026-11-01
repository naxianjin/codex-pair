"""原A路径表示及完整绑定回归；真实入口、真实临时事务、假网络。"""
import json
from contextlib import chdir,nullcontext
import os
from pathlib import Path
import subprocess
import unittest

from scripts import pair_review_runtime as runtime
from tests import test_sequential_permissions as sequential
from tests import test_sequential_cwd as cwd


class ParentHomeCaseTests(unittest.TestCase):
    def fixture(self):
        fixture=sequential.SequentialTests();fixture.setUp();self.addCleanup(fixture.tearDown)
        self.addCleanup(fixture.doCleanups)
        return fixture

    def exercise(self,fixture,mutate,boundary='read'):
        return cwd.SequentialCwdTests.exercise(self,fixture,mutate,boundary=boundary)

    def assert_pending(self,fixture,result):
        status,rows,responses=result
        self.assertEqual(status,'success')
        self.assertEqual([r['status'] for r in rows],['success','pending'])
        self.assertEqual(responses,[])

    def denied(self,result):
        cwd.SequentialCwdTests.denied(self,result)

    def test_parent_cwd_home_and_combined_case_at_both_windows(self):
        if os.name!='nt':self.skipTest('Windows同目录大小写表示')
        for boundary in ('read','settle'):
            for fields in (('cwd',),('codex_home',),('cwd','codex_home')):
                fixture=self.fixture()
                with self.subTest(boundary=boundary,fields=fields):
                    def mutate(transaction):
                        with transaction() as db:
                            row=fixture.store._member(db,'A')
                            db.execute('UPDATE members SET '+','.join(k+'=?' for k in fields)+" WHERE id='A'",
                                       tuple(row[k].swapcase() for k in fields))
                    self.assert_pending(fixture,self.exercise(fixture,mutate,boundary))

    def test_other_identity_and_original_proof_are_not_normalized_away(self):
        for field in ('session','version','proof','extra'):
            fixture=self.fixture()
            with fixture.store.transaction() as db:
                key='managed_binding:'+fixture.request['id']
                binding=fixture.store._option(db,key)
                if field=='proof':
                    proof=json.loads(binding['proof']);proof['accountFingerprint']='different-account'
                    binding['proof']=json.dumps(proof)
                elif field=='extra':binding['extra']='not-original'
                else:binding[field]='different'
                fixture.store._put(db,key,binding)
            with self.subTest(field=field):
                def mutate(transaction):
                    with transaction() as db:
                        row=fixture.store._member(db,'A')
                        db.execute("UPDATE members SET codex_home=? WHERE id='A'",(row['codex_home'].swapcase(),))
                self.denied(self.exercise(fixture,mutate))

    def test_case_second_request_needs_separate_a_and_one_response(self):
        fixture=self.fixture();controller=fixture.controller()
        def turns(*args,**kwargs):
            with fixture.store.transaction() as db:
                row=fixture.store._member(db,'A')
                db.execute("UPDATE members SET cwd=?,codex_home=? WHERE id='A'",(row['cwd'].swapcase(),row['codex_home'].swapcase()))
            return iter([dict(fixture.turn,status='inProgress')])
        controller.client.turns=turns
        controller.client.server_requests.put(fixture.second());controller._permissions()
        self.assert_pending(fixture,(fixture.status(),fixture.permissions(),fixture.responses))
        row=fixture.permissions()[1]
        fixture.allow({'id':row['id'],'digest':row['digest']})
        controller._permissions();controller._permissions()
        self.assertEqual(fixture.responses,[{'id':2,'result':{'decision':'accept'}}])

    def test_binding_fallback_rejects_wrong_directory_uuid_and_non_a(self):
        from scripts.pair_core import PairError
        for kind in ('different','missing','file','link','traversal','relative','uuid','non-a','retired'):
            fixture=self.fixture();member='B' if kind=='non-a' else 'A'
            sender='A' if member=='B' else 'B'
            message=fixture.store.send(sender,member,'binding fixture',kind='note')
            with fixture.store.transaction() as db:
                row=fixture.store._member(db,member)
                db.execute('UPDATE members SET cwd=? WHERE id=?',(row['cwd'].swapcase(),member))
                if kind=='retired':db.execute("UPDATE members SET lifecycle='retired' WHERE id=?",(member,))
            path=Path(fixture.directories[member]);session=fixture.sessions[member]
            if kind=='different':path=Path(fixture.directories['B'])
            elif kind=='missing':path=fixture.root/'missing'
            elif kind=='file':path=fixture.root/'file';path.write_text('fixture')
            elif kind=='traversal':path=path/'..'/path.name
            elif kind=='relative':path=Path(path.name)
            elif kind=='uuid':session=fixture.sessions['B']
            elif kind=='link':
                link=fixture.root/'actor-link'
                try:link.symlink_to(path,target_is_directory=True)
                except OSError:
                    result=subprocess.run(['cmd','/c','mklink','/J',str(link),str(path)],capture_output=True)
                    self.assertEqual(result.returncode,0)
                self.addCleanup(lambda p=link:p.rmdir() if p.exists() and not p.is_symlink() else p.unlink(missing_ok=True))
                path=link
            with self.subTest(kind=kind),chdir(fixture.root) if kind=='relative' else nullcontext(),self.assertRaises((PairError,OSError)):
                fixture.store.claim(member,session,str(path),message_id=message['id'])

    def test_binding_home_different_missing_or_redirected_cannot_match(self):
        for kind in ('different','missing','link'):
            fixture=self.fixture()
            target=fixture.root/('bound-home-'+kind)
            if kind=='different':target.mkdir()
            if kind=='link':
                try:target.symlink_to(fixture.home,target_is_directory=True)
                except OSError:
                    result=subprocess.run(['cmd','/c','mklink','/J',str(target),str(fixture.home)],capture_output=True)
                    self.assertEqual(result.returncode,0)
                self.addCleanup(lambda p=target:p.rmdir() if p.exists() and not p.is_symlink() else p.unlink(missing_ok=True))
            with fixture.store.transaction() as db:
                key='managed_binding:'+fixture.request['id'];binding=fixture.store._option(db,key)
                binding['home']=str(target);fixture.store._put(db,key,binding)
            with self.subTest(kind=kind):
                self.denied(self.exercise(fixture,lambda transaction:None))

    def test_parent_home_entity_replacement_at_both_windows_is_rejected(self):
        for boundary in ('read','settle'):
            fixture=self.fixture()
            home=fixture.root/'configured-home';home.mkdir()
            with fixture.store.transaction() as db:
                db.execute("UPDATE members SET codex_home=? WHERE id='A'",(str(home),))
                db.execute('UPDATE reviewer_runtime SET codex_home=?',(str(home),))
                key='managed_binding:'+fixture.request['id'];binding=fixture.store._option(db,key)
                binding['home']=str(home);fixture.store._put(db,key,binding)
            with self.subTest(boundary=boundary):
                def mutate(transaction):
                    home.rename(home.with_name('old-configured-home'));home.mkdir()
                self.denied(self.exercise(fixture,mutate,boundary))

    def test_nonlive_binding_strictness_is_unchanged(self):
        fixture=self.fixture()
        with fixture.store.transaction() as db:
            row=fixture.store._member(db,'A')
            db.execute("UPDATE members SET codex_home=? WHERE id='A'",(row['codex_home'].swapcase(),))
        runtime.settle_permissions(fixture.store,fixture.ticket['epoch'],fixture.lease['execution_id'],fixture.turn)
        self.assertEqual(fixture.status(),'unknown')

    def test_live_requires_snapshot_and_normal_progress_still_allowed(self):
        fixture=self.fixture()
        from scripts.pair_core import PairError
        with self.assertRaises(PairError):
            runtime.settle_permissions(fixture.store,fixture.ticket['epoch'],fixture.lease['execution_id'],
                                      dict(fixture.turn,status='inProgress'),active_turn='turn-one')
        self.assertEqual(fixture.status(),'granted')
        def mutate(transaction):
            with transaction() as db:
                db.execute('UPDATE reviewer_runtime SET last_progress=last_progress+1,updated=updated+1,reminded=1')
        self.assert_pending(fixture,self.exercise(fixture,mutate))

if __name__=='__main__':unittest.main()
