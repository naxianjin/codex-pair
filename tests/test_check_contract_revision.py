import copy,io,json,unittest
from types import SimpleNamespace
from unittest.mock import patch
from tests import test_stages
from scripts.pair_core import PairError
from scripts.pair_producer import strict_display
from scripts import pair_commands
class ContractRevisionTests(unittest.TestCase):
    setUp=test_stages.StageTests.setUp
    tearDown=test_stages.StageTests.tearDown
    actor=test_stages.StageTests.actor
    delivered=test_stages.StageTests.delivered
    submit=test_stages.StageTests.submit
    def fixture(self):
        self.delivered();s=self.submit()
        with self.store.transaction() as db:
            db.execute("UPDATE review_stages SET status='needs_validation',verdict='needs_validation',evidence=? WHERE id=?",(json.dumps({'old':'report'}),s['id']))
        self.stage=s
        self.checks=[{'name':'inventory','argv':['C:/Program Files/PowerShell/7/pwsh.exe','-NoProfile','-Command','Get-ChildItem -LiteralPath D:/fixture'],'required':True}]
        self.checks[0]['command']=strict_display(self.checks[0]['argv'])
    def revise(self,**kw):
        p={'task_id':'api','stage_id':self.stage['id'],'expected_digest':self.stage['digest'],'reason':'Fix exact registered validation contract','required_checks':self.checks}
        p.update(kw)
        return self.store.revise_check_contract(*self.actor(),**p)
    def test_inventory_revision_requires_new_submission(self):
        self.fixture()
        with self.store.transaction() as db:before=dict(db.execute('SELECT * FROM review_stages').fetchone())
        result=self.revise();self.assertEqual(result['requirements_version'],2)
        with self.store.transaction() as db:
            self.assertEqual(before,dict(db.execute('SELECT * FROM review_stages').fetchone()))
            self.assertFalse(self.store._stage_passed(db,'api'))
        new=self.submit()
        self.assertEqual(new['requirements_version'],2)
        self.assertNotEqual(new['digest'],self.stage['digest'])
        self.assertEqual(json.loads(new['payload'])['coordinator_required_checks'],self.checks)
        self.assertEqual(new['status'],'pending')
        self.assertEqual(new['contributors'],self.stage['contributors'])
    def test_strict_display_contract(self):
        self.fixture()
        self.checks[0]['argv'][0]=r'C:\Program Files\PowerShell\7\pwsh.exe'
        self.checks[0]['command']=strict_display(self.checks[0]['argv'])
        self.assertTrue(self.revise()['resubmit_required'])
    def test_idempotent_and_conflicting_request(self):
        self.fixture();first=self.revise();again=self.revise()
        self.assertTrue(again['duplicate']);self.assertEqual(first['requirements_version'],again['requirements_version'])
        with self.assertRaisesRegex(PairError,'Revision conflict'):self.revise(reason='different reason')
    def test_wrong_digest_stage_task_actor(self):
        self.fixture()
        for fields in ({'expected_digest':'f'*64},{'stage_id':'wrong'},{'task_id':'wrong'}):
            with self.subTest(fields=fields),self.assertRaises(PairError):self.revise(**fields)
        with self.assertRaises(PairError):self.store.revise_check_contract(*self.actor('B'),'api',self.stage['id'],self.stage['digest'],'reason',self.checks)
        with self.assertRaises(PairError):self.store.revise_check_contract('A',self.sessions['B'],self.directories['A'],'api',self.stage['id'],self.stage['digest'],'reason',self.checks)
    def test_nonterminal_stage_and_business_refused(self):
        self.fixture()
        for state in ('pending','running','passed','stale'):
            with self.store.transaction() as db:db.execute('UPDATE review_stages SET status=?',(state,))
            with self.subTest(state=state),self.assertRaises(PairError):self.revise()
        with self.store.transaction() as db:
            db.execute("UPDATE review_stages SET status='needs_validation'")
            db.execute("UPDATE tasks SET status='active' WHERE id='api'")
        with self.assertRaises(PairError):self.revise()
    def test_unknown_and_running_messages_refused(self):
        self.fixture()
        for delivery,remote in [('unknown',None),('accepted','inProgress'),('delivering',None)]:
            with self.store.transaction() as db:db.execute('UPDATE messages SET delivery_status=?,remote_status=? WHERE task_id=?',(delivery,remote,'api'))
            with self.subTest(delivery=delivery),self.assertRaises(PairError):self.revise()
    def test_managed_permission_states_refused(self):
        self.fixture()
        with self.store.transaction() as db:
            db.execute("INSERT INTO managed_permissions(id,epoch,session,execution_id,request,digest,operation,authorization_version,status,expires,review_message,attempt,created) VALUES('p',1,'s','e','{}','d','op',1,'granted',999,'m',1,0)")
        for state in ('pending','allowed','granted','unknown'):
            with self.store.transaction() as db:db.execute('UPDATE managed_permissions SET status=?',(state,))
            with self.subTest(state=state),self.assertRaises(PairError):self.revise()
    def test_invalid_check_structures(self):
        self.fixture()
        bad=[[],None,{},[{}],[dict(self.checks[0],required=False)],[dict(self.checks[0],command='wrong')],
             [dict(self.checks[0],argv=[])],[dict(self.checks[0],argv=[None])],[dict(self.checks[0],name='')],
             self.checks*2,[dict(self.checks[0],extra=True)],[dict(self.checks[0],argv=['x\0'])]]
        for checks in bad:
            with self.subTest(checks=checks),self.assertRaises(PairError):self.revise(required_checks=checks)
    def test_cannot_remove_old_obligations(self):
        self.fixture()
        with self.store.transaction() as db:db.execute('UPDATE tasks SET required_checks=? WHERE id=?',(json.dumps([{'name':'old','command':'old','required':True}]),'api'))
        with self.assertRaisesRegex(PairError,'obligation'):self.revise()
    def test_public_dispatch_and_actor_validation(self):
        self.fixture()
        args=SimpleNamespace(command='revise-check-contract',member='A',session=self.sessions['A'],worktree=self.directories['A'],input_stdin=True)
        cli=SimpleNamespace(validate_actor=lambda *a,**k:None)
        body={'task':'api','stage':self.stage['id'],'digest':self.stage['digest'],'reason':'public explicit revision','required_checks':self.checks}
        self.assertIn(args.command,pair_commands.COMMANDS)
        with patch('sys.stdin',io.StringIO(json.dumps(body))):
            self.assertTrue(pair_commands.execute(self.store,args,cli)['resubmit_required'])
    def test_existing_capacity_and_author_preserved(self):
        self.fixture();self.delivered('second');self.submit('second','c'*40)
        self.revise();new=self.submit()
        self.assertEqual(new['version'],2)
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM review_stages WHERE status IN ('pending','running','needs_validation')").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT assignee,task_kind FROM tasks WHERE id='api'").fetchone()[:],('B','implementation'))
        with self.assertRaises(PairError):self.store.require_reviewed('api')

    def test_old_contract_cannot_resume(self):
        self.fixture();self.revise()
        args=SimpleNamespace(command='resume-stage',member='A',session=self.sessions['A'],worktree=self.directories['A'],input_stdin=True)
        body={'stage':self.stage['id'],'evidence':'old execution ended','safe_to_repeat_checks':True}
        with patch('sys.stdin',io.StringIO(json.dumps(body))),self.assertRaisesRegex(PairError,'Stale contract'):
            pair_commands.execute(self.store,args,SimpleNamespace(validate_actor=lambda *a,**k:None))
    def test_reviewer_running_unknown_and_lease_refused(self):
        self.fixture()
        with self.store.transaction() as db:
            db.execute("INSERT INTO reviewer_runtime(id,epoch,state,attempt,codex_home,cwd,model,parent_session,last_progress,created,updated) VALUES(1,1,'idle','try','home','cwd','gpt-test','parent',0,0,0)")
        for state,owner in [('starting',None),('running',None),('unknown',None),('idle','lease')]:
            with self.store.transaction() as db:db.execute('UPDATE reviewer_runtime SET state=?,owner=?',(state,owner))
            with self.subTest(state=state,owner=owner),self.assertRaises(PairError):self.revise()
    def test_concurrent_duplicate_revision_changes_version_once(self):
        import concurrent.futures
        self.fixture()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _:self.revise(),range(2)))
        self.assertEqual(sorted(r['duplicate'] for r in results),[False,True])
        self.assertEqual(self.store.task('api')['requirements_version'],2)
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='check_contract_revised'").fetchone()[0],1)
    def test_old_reports_rounds_and_ownership_unchanged(self):
        self.fixture()
        with self.store.transaction() as db:
            db.execute("INSERT INTO review_issues(id,root_task,author,body,status,rounds,stage_id,updated) VALUES('stable','api','B','finding','escalated',2,?,0)",(self.stage['id'],))
            issue=dict(db.execute('SELECT * FROM review_issues').fetchone())
            task=dict(db.execute("SELECT * FROM tasks WHERE id='api'").fetchone())
        self.revise()
        with self.store.transaction() as db:
            self.assertEqual(issue,dict(db.execute('SELECT * FROM review_issues').fetchone()))
            changed=dict(db.execute("SELECT * FROM tasks WHERE id='api'").fetchone())
        for k in task:
            if k not in ('required_checks','requirements_version','review_required','updated'):self.assertEqual(task[k],changed[k],k)


    def linked(self,key='linked-repair',delivery='accepted',remote='completed'):
        self.delivered(key)
        with self.store.transaction() as db:
            exists=db.execute('SELECT 1 FROM messages WHERE task_id=?',(key,)).fetchone()
        if not exists:self.store.send('A','B','isolated linked repair',kind='task',task_id=key)
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET task_kind='repair' WHERE id=?",(key,))
            db.execute('INSERT INTO repair_links VALUES(?,?,?,?)',(key,'stable-'+key,1,self.stage['id']))
            db.execute("UPDATE messages SET delivery_status=?,remote_status=?,process_status='completed' WHERE task_id=?",(delivery,remote,key))
    def contract_snapshot(self):
        with self.store.transaction() as db:
            return {table:[dict(r) for r in db.execute('SELECT * FROM '+table)] for table in ('tasks','review_stages','pair_settings','events','repair_links','messages')}
    def reject_linked(self,delivery,remote):
        self.fixture();self.linked(delivery=delivery,remote=remote)
        with self.store.transaction() as db:
            self.assertTrue(any(m['task_id']=='linked-repair' for m in self.store._running(db)))
        before=self.contract_snapshot()
        with self.assertRaisesRegex(PairError,'Execution unresolved'):self.revise()
        self.assertEqual(self.contract_snapshot(),before)
    def test_linked_delivered_inprogress_rejects_without_mutation(self):
        self.reject_linked('accepted','inProgress')
    def test_linked_delivered_unknown_rejects_without_mutation(self):
        self.reject_linked('unknown',None)
    def test_linked_verified_terminal_allows_revision(self):
        self.fixture();self.linked()
        self.assertEqual(self.revise()['requirements_version'],2)
    def test_all_linked_repairs_checked(self):
        self.fixture();self.linked('first');self.linked('second','unknown',None)
        with self.store.transaction() as db:self.assertTrue(any(m['task_id']=='second' for m in self.store._running(db)))
        before=self.contract_snapshot()
        with self.assertRaisesRegex(PairError,'Execution unresolved'):self.revise()
        self.assertEqual(self.contract_snapshot(),before)
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='completed' WHERE task_id='second'")
        self.assertTrue(self.revise()['resubmit_required'])
    def test_linked_uncompleted_business_message_rejected(self):
        self.fixture();self.linked()
        with self.store.transaction() as db:db.execute("UPDATE messages SET process_status='claimed' WHERE task_id='linked-repair'")
        before=self.contract_snapshot()
        with self.assertRaisesRegex(PairError,'Execution unresolved'):self.revise()
        self.assertEqual(self.contract_snapshot(),before)

if __name__=='__main__':unittest.main()
