"""固定交付与动态保留线草稿；不包含真实模型、权限或额度验收。"""
import json
import unittest

from scripts.pair_core import PairError
from tests import test_core


class StageTests(unittest.TestCase):
    setUp=test_core.CoreTests.setUp
    tearDown=test_core.CoreTests.tearDown

    def actor(self,member='A'):return (member,self.sessions[member],self.directories[member])

    def delivered(self,key='api',member='B'):
        self.store.assign(key,'阶段接口',member,['api.py'])
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='delivered',commits_json=? WHERE id=?",(json.dumps(['b'*40]),key))
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='completed',process_status='completed' WHERE task_id=?",(key,))
        return key

    def submit(self,key='api',head='b'*40):
        return self.store.submit_stage(*self.actor('B'),key,{'kind':'commit','head':head},
            [{'member':'B','provider':'deepseek','model':'deepseek-chat'}],
            [{'name':'unit','required':True,'status':'not_run'}],
            {'objective':'接口','acceptance':['边界正确'],'environment':{},'lockfiles':[],'baseline':'a'*40})

    def test_submitted_stage_is_version_bound_and_idempotent(self):
        self.delivered()
        first=self.submit();again=self.submit()
        self.assertEqual(first['id'],again['id'])
        self.assertEqual(first['status'],'pending')
        self.assertEqual(self.store.task('api')['status'],'review_wait')
        with self.assertRaises(PairError):self.store.require_reviewed('api')

    def test_two_pending_stages_do_not_drop_saved_delivery(self):
        for index in range(3):
            self.delivered('stage-'+str(index))
            if index<2:self.submit('stage-'+str(index),str(index+1)*40)
        with self.assertRaisesRegex(PairError,'backlog'):self.submit('stage-2','3'*40)
        self.assertEqual(self.store.task('stage-2')['status'],'delivered')

    def test_reserve_is_monotonic_finite_and_persisted(self):
        self.store.setting('reserve_floor',6)
        with self.store.transaction() as db:self.assertEqual(self.store._reserve(db,'A'),6)
        for invalid in (True,float('nan'),float('inf'),5,96):
            with self.assertRaises(PairError):self.store.propose_reserve(*self.actor(),invalid,'不可用建议')
        self.assertEqual(self.store.propose_reserve(*self.actor(),12,'需要更长现场保存')['resume'],17)
        with self.assertRaises(PairError):self.store.propose_reserve(*self.actor(),11,'不能下调')
        self.assertEqual(self.store.setting('checkpoint_reserve:A'),12)

    def test_mixed_consumption_cannot_calibrate(self):
        self.store.setting('reserve_floor',6)
        with self.assertRaisesRegex(PairError,'calibration'):
            self.store.propose_reserve(*self.actor(),8,'混合样本',sample={'comparable':True,'mixed_consumption':True,'consumed_percentage_points':2,'evidence':'其他项目并发'})

    def test_missing_review_evidence_cannot_pass(self):
        self.delivered();stage=self.submit()
        self.assertNotEqual(stage['status'],'passed')
        self.store.assign('downstream','不能绕过','A',['use.py'],dependencies=['api'])
        self.assertEqual(self.store.task('downstream')['status'],'pending')

    def test_backlog_allows_replacing_own_nonrunning_stage(self):
        for key in ('first','second'):self.delivered(key);self.submit(key)
        updated=self.submit('first','c'*40)
        self.assertEqual(updated['version'],2)
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM review_stages WHERE status IN ('pending','running','needs_validation')").fetchone()[0],2)

    def test_running_stage_cannot_be_invalidated_by_resubmission(self):
        self.delivered();stage=self.submit()
        with self.store.transaction() as db:db.execute("UPDATE review_stages SET status='running' WHERE id=?",(stage['id'],))
        with self.assertRaises(PairError):self.submit('api','c'*40)
        with self.store.transaction() as db:self.assertEqual(db.execute('SELECT status FROM review_stages WHERE id=?',(stage['id'],)).fetchone()[0],'running')

    def issue(self,key,item):
        with self.store.transaction() as db:
            stage=dict(db.execute('SELECT * FROM review_stages WHERE task_id=? ORDER BY version DESC LIMIT 1',(key,)).fetchone())
            task=dict(db.execute('SELECT * FROM tasks WHERE id=?',(key,)).fetchone())
            self.store._register_issue(db,stage,task,item)

    def finding(self,**changes):
        return dict({'id':'bug-1','body':'边界失败','blocking':True,'anchor':{'path':'api.py','reproduction':'unit:boundary'}},**changes)

    def test_out_of_scope_and_unanchored_findings_need_coordination(self):
        self.delivered();self.submit()
        self.issue('api',self.finding(anchor={'path':'other.py','reproduction':'unit:boundary'}))
        self.issue('api',{'id':'unanchored','body':'缺复现位置','blocking':True})
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM repair_links').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM review_issues WHERE status='needs_coordination'").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE kind='blocked' AND recipient='A'").fetchone()[0],2)

    def test_same_anchor_across_new_task_and_id_keeps_rounds_and_source(self):
        self.delivered();self.submit();self.issue('api',self.finding())
        with self.store.transaction() as db:
            first=dict(db.execute('SELECT * FROM review_issues').fetchone())
            db.execute("UPDATE tasks SET status='completed' WHERE id=?",(first['repair_task'],))
        self.issue('api',self.finding())
        with self.store.transaction() as db:
            second=db.execute('SELECT * FROM review_issues').fetchone()
            self.assertEqual(second['rounds'],2)
            db.execute("UPDATE tasks SET status='completed' WHERE id=?",(second['repair_task'],))
        self.delivered('renamed');self.submit('renamed','c'*40)
        with self.store.transaction() as db:db.execute("UPDATE tasks SET assignee='A' WHERE id='renamed'")
        self.issue('renamed',self.finding(id='new-reviewer-id'))
        with self.store.transaction() as db:
            issues=[dict(r) for r in db.execute('SELECT * FROM review_issues')]
            self.assertEqual(len(issues),1)
            self.assertEqual((issues[0]['root_task'],issues[0]['author'],issues[0]['rounds'],issues[0]['status']),('api','B',2,'escalated'))

    def test_changed_reproduction_label_requires_coordination_without_new_round(self):
        self.delivered();self.submit();self.issue('api',self.finding())
        self.delivered('renamed');self.submit('renamed','c'*40)
        self.issue('renamed',self.finding(id='changed-id',anchor={'path':'api.py','reproduction':'renamed-test'}))
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM review_issues').fetchone()
            self.assertEqual((row['root_task'],row['rounds'],row['status']),('api',1,'needs_coordination'))
            self.assertEqual(db.execute('SELECT count(*) FROM review_issues').fetchone()[0],1)
            self.assertEqual(len(self.store._option(db,'issue_sources:'+row['id'])),2)


if __name__=='__main__':unittest.main()
