"""隔离SQLite和假受管客户端验证审核/权限门禁，不启动模型。"""
import json
import time
import unittest
from unittest.mock import patch

from scripts.pair_core import PairError
from scripts.pair_review_runtime import ReviewerController
from tests import test_core,test_reviewer,test_stages


class ManagedReviewTests(unittest.TestCase):
    def setUp(self):
        test_core.CoreTests.setUp(self)
        helper=test_reviewer.ReviewerTests();helper.__dict__.update(self.__dict__)
        self.ticket,self.proof=helper.ready()
        self.store.setting('approval_assistance',True)
        self.lease=self.store.acquire_reviewer(self.ticket['epoch'],self.proof['id'],self.proof)

    tearDown=test_core.CoreTests.tearDown

    def actor(self):return ('A',self.sessions['A'],self.directories['A'])

    def test_readonly_coordination_finishes_without_settling_unrelated_permission(self):
        self.store.assign('coord','只读现场摘要','B',['report.md'],task_kind='coordination')
        message=next(m for m in self.store.status()['messages'] if m['task_id']=='coord' and m['kind']=='task')
        claim=self.store.claim('B',self.sessions['B'],self.directories['B'],message_id=message['id'])
        request=self.request();self.allow(request);self.store.managed_approval_result(request['id'])
        result=self.store.complete('B',self.sessions['B'],self.directories['B'],message['id'],claim['claim_token'],'只读协调已保存，工具结果仍未核实')
        self.assertTrue(result['completed'])
        with self.store.transaction() as db:
            self.assertTrue(self.store._approval_unresolved(db))
            self.assertEqual(db.execute('SELECT status FROM managed_permissions WHERE id=?',(request['id'],)).fetchone()[0],'granted')

    def test_coordination_exception_rejects_business_and_required_checks(self):
        request=self.request();self.allow(request);self.store.managed_approval_result(request['id'])
        self.store.assign('business','业务','B',['api.py'])
        self.store.assign('coord','只读协调','A',['report.md'],task_kind='coordination')
        with self.store.transaction() as db:
            self.assertTrue(self.store._approval_unresolved(db,task_id='business'))
            db.execute('UPDATE tasks SET required_checks=? WHERE id=?',(json.dumps([{'name':'unit','command':'python -m unittest'}]),'coord'))
            with self.assertRaisesRegex(PairError,'report-only'):
                self.store._approval_unresolved(db,task_id='coord')

    def test_coordination_cannot_bypass_its_own_visible_approval(self):
        self.store.assign('coord','只读现场摘要','B',['report.md'],task_kind='coordination')
        message=next(m for m in self.store.status()['messages'] if m['task_id']=='coord' and m['kind']=='task')
        with self.store.transaction() as db:
            proof=json.loads(self.store._member(db,'B')['proof']);proof['git']={'path':str(self.project)}
            db.execute("UPDATE members SET proof=? WHERE id='B'",(json.dumps(proof),))
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='inProgress',turn_id='coord-turn' WHERE id=?",(message['id'],))
        event={'hook_event_name':'PermissionRequest','session_id':self.sessions['B'],'turn_id':'coord-turn',
            'cwd':self.directories['B'],'tool_name':'Bash','tool_input':{'command':'python report.py'},'permission_mode':'default'}
        request=self.store.capture_approval('B',event)
        with self.store.transaction() as db:
            self.assertTrue(self.store._approval_unresolved(db,task_id='coord'))
            db.execute("UPDATE approval_requests SET status='unknown' WHERE id=?",(request['id'],))
            self.assertTrue(self.store._approval_unresolved(db,task_id='coord'))

    def request(self,turn='turn-one'):
        return self.store.capture_managed_approval(self.ticket['epoch'],self.proof['id'],self.lease['execution_id'],
            {'id':turn,'method':'item/commandExecution/requestApproval','params':{'threadId':self.proof['id'],'turnId':turn,'itemId':'command-one','command':'python -m unittest','cwd':self.proof['cwd']}})

    def allow(self,request):
        with self.store.transaction() as db:row=db.execute('SELECT * FROM managed_permissions WHERE id=?',(request['id'],)).fetchone()
        claim=self.store.claim(*self.actor(),message_id=row['review_message'])
        self.store.review_approval(*self.actor(),request['id'],claim['claim_token'],request['digest'],'allow','已核对现有任务与权限',within_permissions=True,capability_evidence='临时目录只读测试')

    def release(self):
        self.store.release_reviewer(self.ticket['epoch'],self.proof['id'],self.lease['owner'],outcome='completed',evidence='准确原回合已持久化结束')

    def test_managed_granted_unknown_are_unresolved_and_block_new_review(self):
        request=self.request();self.allow(request)
        self.assertEqual(self.store.managed_approval_result(request['id'])['status'],'allowed')
        self.release()
        for status in ('granted','unknown'):
            with self.subTest(status=status),self.store.transaction() as db:
                db.execute('UPDATE managed_permissions SET status=? WHERE id=?',(status,request['id']))
                self.assertTrue(self.store._approval_unresolved(db,grants_only=True))
            with self.assertRaisesRegex(PairError,'unresolved'):
                self.store.acquire_reviewer(self.ticket['epoch'],self.proof['id'],self.proof)

    def test_two_timeouts_persist_one_blocked_notification(self):
        first=self.request()
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM managed_permissions WHERE id=?',(first['id'],)).fetchone()
            self.assertEqual(row['expires']-row['created'],180)
            db.execute('UPDATE managed_permissions SET expires=? WHERE id=?',(time.time()-1,first['id']))
            self.store.approval_tick(db)
        self.release()
        self.store.prepare_approval_retry(*self.actor(),first['id'],'原回合已结束且没有工具执行')
        self.lease=self.store.acquire_reviewer(self.ticket['epoch'],self.proof['id'],self.proof)
        second=self.request('turn-two')
        with self.store.transaction() as db:
            db.execute('UPDATE managed_permissions SET expires=? WHERE id=?',(time.time()-1,second['id']))
        self.store.managed_approval_result(second['id'])
        with self.store.transaction() as db:
            self.store.approval_tick(db);self.store.approval_tick(db)
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE kind='blocked' AND recipient='A'").fetchone()[0],1)
            self.assertEqual(db.execute('SELECT retry_state FROM managed_permissions WHERE id=?',(second['id'],)).fetchone()[0],'needs_user')

    def test_managed_authorization_revocation_does_not_allow_execution(self):
        request=self.request();self.allow(request)
        self.store.setting('authorization_version',2)
        self.assertEqual(self.store.managed_approval_result(request['id'])['status'],'expired')

    def test_suspended_a_cannot_consume_managed_allow(self):
        request=self.request();self.allow(request)
        with self.store.transaction() as db:
            db.execute("UPDATE members SET lifecycle='suspended',verified=0 WHERE id='A'")
        self.assertEqual(self.store.managed_approval_result(request['id'])['status'],'expired')

    def test_identity_invalidation_expires_unconsumed_managed_allow(self):
        from scripts.pair_quota import suspend_identity
        request=self.request();self.allow(request)
        suspend_identity(self.store,self.store.member('A'),'Account changed / identity mismatch',account=True)
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM managed_permissions WHERE id=?',(request['id'],)).fetchone()
            self.assertEqual((row['status'],row['retry_state']),('expired','cancelled'))

    def test_session_reconciliation_precedes_tree_or_client_operations(self):
        controller=ReviewerController(self.store)
        for state in ('starting','running','unknown'):
            with self.subTest(state=state),patch.object(self.store,'reviewer_status',return_value={'reviewer':{'state':state}}),\
                    patch.object(controller,'_parent') as parent,\
                    patch('scripts.pair_review_runtime.prepare_review_tree') as tree,\
                    patch('scripts.pair_review_runtime.AppServer') as client:
                parent.return_value=({'codex_home':str(self.root)}, {})
                with self.assertRaisesRegex(PairError,'reconciliation'):
                    controller._session('a'*40)
                tree.assert_not_called();client.assert_not_called()

    def test_required_receipt_must_use_fixed_review_directory(self):
        controller=ReviewerController(self.store);controller.state={'cwd':str(self.root)}
        stage={'payload':json.dumps({'coordinator_required_checks':[{'name':'unit','command':'python -m unittest'}]})}
        report={'verdict':'passed','checks':[{'name':'unit','status':'passed','receipt_id':'r'}]}
        for cwd in (str(self.root),str(self.root/'other'),None,''):
            with self.subTest(cwd=cwd):
                receipt={'type':'commandExecution','id':'r','command':'python -m unittest',
                         'status':'completed','exitCode':0,'cwd':cwd}
                result=controller._report({'status':'completed','items':[receipt,{'type':'agentMessage','text':json.dumps(report)}]},stage)
                self.assertEqual(result['verdict'],'passed' if cwd==str(self.root) else 'needs_validation')

    def test_report_rejects_boolean_codes_and_conflicting_duplicate_items(self):
        controller=ReviewerController(self.store);controller.state={'cwd':str(self.root)}
        stage={'payload':json.dumps({'coordinator_required_checks':[{'name':'unit','command':'python -m unittest'}]})}
        report={'verdict':'passed','checks':[{'name':'unit','status':'passed','receipt_id':'r'}]}
        message={'type':'agentMessage','text':json.dumps(report)}
        receipt={'type':'commandExecution','id':'r','command':'python -m unittest',
                 'status':'completed','exitCode':0,'cwd':str(self.root)}
        for items in ([dict(receipt,exitCode=False)],[dict(receipt,exitCode=True)],
                      [receipt,dict(receipt,exitCode=1)],
                      [dict(receipt,exitCode=0,exit_code=2)]):
            with self.subTest(items=items):
                result=controller._report({'status':'completed','items':list(items)+[message]},stage)
                self.assertEqual(result['verdict'],'needs_validation')
        raw=dict(receipt,type='CommandExecution',exit_code=0,cwd=self.root.as_uri());raw.pop('exitCode')
        self.assertEqual(controller._report({'status':'completed','items':[raw,message]},stage)['verdict'],'passed')

    def test_managed_denial_cannot_be_replayed(self):
        request=self.request()
        with self.store.transaction() as db:row=db.execute('SELECT * FROM managed_permissions WHERE id=?',(request['id'],)).fetchone()
        claim=self.store.claim(*self.actor(),message_id=row['review_message'])
        self.store.review_approval(*self.actor(),request['id'],claim['claim_token'],request['digest'],'deny','此操作未获授权')
        self.assertEqual(self.store.managed_approval_result(request['id'])['status'],'denied')
        with self.assertRaisesRegex(PairError,'unresolved'):self.request('another-turn')

    def test_missing_receipt_stays_unknown_and_late_epoch_cannot_authorize(self):
        request=self.request();self.allow(request);self.store.managed_approval_result(request['id'])
        controller=ReviewerController(self.store)
        controller.state={'epoch':self.ticket['epoch'],'execution_id':self.lease['execution_id']}
        controller._settle_permissions({'id':'turn-one','items':[]})
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT status FROM managed_permissions WHERE id=?',(request['id'],)).fetchone()[0],'unknown')
        with self.assertRaises(PairError):
            self.store.capture_managed_approval(self.ticket['epoch']+1,self.proof['id'],self.lease['execution_id'],{'id':2,'method':'item/commandExecution/requestApproval','params':{'threadId':self.proof['id'],'turnId':'late'}})

    def test_unknown_permission_requires_exact_turn_and_item_receipt(self):
        request=self.request();self.allow(request);self.store.managed_approval_result(request['id'])
        controller=ReviewerController(self.store)
        controller.state={'epoch':self.ticket['epoch'],'execution_id':self.lease['execution_id'],'session':self.proof['id']}
        controller._settle_permissions({'id':'turn-one','items':[]})
        item={'id':'command-one','type':'commandExecution','status':'completed','exitCode':0,
              'command':'python -m unittest','cwd':self.proof['cwd']}
        controller._settle_permissions({'id':'wrong-turn','items':[item]})
        with self.store.transaction() as db:self.assertTrue(self.store._approval_unresolved(db,grants_only=True))
        controller._settle_permissions({'id':'turn-one','status':'completed','items':[item]})
        # 没有controller生产者证据时，即使命令字面相同也不能结算。
        with self.store.transaction() as db:self.assertTrue(self.store._approval_unresolved(db,grants_only=True))

    def test_unresolved_managed_permission_blocks_checkpoint_recovery(self):
        from tests.test_integration import seed
        self.project.rmdir();seed(self.project)
        request=self.request();self.allow(request);self.store.managed_approval_result(request['id']);self.release()
        self.store.save_reviewer_checkpoint('已批准工具结果仍需核实')
        offset=0
        while True:
            page=self.store.read_reviewer_checkpoint(*self.actor(),offset=offset)
            if page['next_offset'] is None:break
            offset=page['next_offset']
        with self.assertRaisesRegex(PairError,'unresolved'):
            self.store.consume_reviewer_checkpoint(*self.actor(),page['digest'],'原工具未核实不恢复')

    def review(self,stage,verdict,findings=()):
        args=(stage['id'],self.ticket['epoch'],self.proof['id'],self.proof,self.lease['owner'],self.lease['execution_id'])
        self.store.begin_review(*args)
        self.store.record_review_turn(stage['id'],self.ticket['epoch'],self.lease['execution_id'],'fixture-turn')
        from tests.producer_fixture import install
        install(self,self.store.path,stage_id=stage['id'])
        report={'candidate_digest':stage['digest'],'verdict':verdict,'findings':list(findings),
                'checks':[{'name':'unit','status':'passed','required':True,'evidence':'隔离fake工具回执'}]}
        return self.store.finish_review(*args,report,files_unchanged=True)

    def test_review_repair_recheck_then_dependency_releases(self):
        helper=self.stage_helper();helper.delivered()
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET required_checks=? WHERE id=?',(json.dumps([{'name':'unit','command':'python -m unittest'}]),'api'))
        stage=helper.submit();self.store.assign('dependent','后续依赖','B',['use.py'],dependencies=['api'])
        self.review(stage,'needs_fix',[helper.finding()])
        with self.store.transaction() as db:issue=dict(db.execute('SELECT * FROM review_issues').fetchone())
        repair=issue['repair_task']
        with self.store.transaction() as db:db.execute("UPDATE tasks SET status='completed' WHERE id=?",(repair,))
        repaired=helper.submit(repair,'c'*40);self.review(repaired,'passed')
        self.assertEqual(self.store.task('dependent')['status'],'pending')
        updated=helper.submit('api','d'*40);self.review(updated,'passed')
        with self.store.transaction() as db:db.execute("UPDATE tasks SET status='integrated' WHERE id IN (?,?)",('api',repair))
        self.store.schedule()
        self.assertEqual(self.store.task('dependent')['status'],'active')
        with self.store.transaction() as db:self.assertEqual(db.execute('SELECT status FROM review_issues').fetchone()[0],'resolved')

    def test_old_epoch_verdict_is_rejected_without_changing_stage(self):
        helper=self.stage_helper();helper.delivered();stage=helper.submit()
        args=(stage['id'],self.ticket['epoch'],self.proof['id'],self.proof,self.lease['owner'],self.lease['execution_id'])
        self.store.begin_review(*args)
        with self.assertRaises(PairError):
            self.store.finish_review(stage['id'],self.ticket['epoch']+1,self.proof['id'],self.proof,self.lease['owner'],self.lease['execution_id'],
                {'candidate_digest':stage['digest'],'verdict':'passed','findings':[],'checks':[]},files_unchanged=True)
        with self.store.transaction() as db:self.assertEqual(db.execute('SELECT status FROM review_stages WHERE id=?',(stage['id'],)).fetchone()[0],'running')

    def test_unresolved_grant_cannot_pass_business_stage(self):
        request=self.request();self.allow(request);self.store.managed_approval_result(request['id'])
        helper=self.stage_helper();helper.delivered();stage=helper.submit()
        result=self.review(stage,'passed')
        self.assertEqual(result['verdict'],'needs_validation')

    def stage_helper(self):
        helper=test_stages.StageTests();helper.__dict__.update(self.__dict__);return helper

    def test_ordinary_stage_wait_is_bounded_after_priority_reviews(self):
        helper=self.stage_helper();helper.delivered('ordinary');ordinary=helper.submit('ordinary')
        for index in range(4):
            key='urgent-'+str(index)
            helper.delivered(key);stage=helper.submit(key,str(index+1)*40)
            with self.store.transaction() as db:
                db.execute("UPDATE review_stages SET status='passed' WHERE task_id<>? AND task_id<>'ordinary'",(key,))
                db.execute("UPDATE tasks SET task_kind='repair' WHERE id=?",(key,))
            chosen=self.store.next_review(self.ticket['epoch'],self.proof['id'],self.proof)
            if index==3:self.assertEqual(chosen['id'],ordinary['id']);break
            self.assertEqual(chosen['id'],stage['id'])
            self.store.begin_review(stage['id'],self.ticket['epoch'],self.proof['id'],self.proof,self.lease['owner'],self.lease['execution_id'])
            with self.store.transaction() as db:db.execute("UPDATE review_stages SET status='passed' WHERE id=?",(stage['id'],))

    def test_stage_blocking_dependency_precedes_ordinary_delivery(self):
        helper=self.stage_helper();helper.delivered('ordinary');helper.submit('ordinary')
        helper.delivered('upstream');upstream=helper.submit('upstream','c'*40)
        self.store.assign('downstream','等待审核','A',['use.py'],dependencies=['upstream'])
        self.assertEqual(self.store.next_review(self.ticket['epoch'],self.proof['id'],self.proof)['id'],upstream['id'])


if __name__=='__main__':unittest.main()
