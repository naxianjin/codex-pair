"""真实临时Store中的历史空锚点、结算及返修门禁回归。"""
import unittest
from tests import test_stages as stages, test_managed_review as managed


class HistoricalAnchorTests(unittest.TestCase):
    setUp=stages.StageTests.setUp
    tearDown=stages.StageTests.tearDown
    actor=stages.StageTests.actor
    delivered=stages.StageTests.delivered
    submit=stages.StageTests.submit
    issue=stages.StageTests.issue
    finding=stages.StageTests.finding

    def legacy(self,**extra):
        self.delivered('legacy');self.submit('legacy')
        self.issue('legacy',dict({'id':'historical','body':'历史缺少复现位置','blocking':True},**extra))

    def check_new_root(self):
        self.delivered();self.submit();self.issue('api',self.finding())
        with self.store.transaction() as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM review_issues')]
            old=next(r for r in rows if r['root_task']=='legacy')
            new=next(r for r in rows if r['root_task']=='api')
            self.assertEqual((old['status'],old['rounds']),('needs_coordination',0))
            self.assertEqual((new['status'],new['rounds']),('open',1))
            self.assertEqual(db.execute('SELECT count(*) FROM repair_links').fetchone()[0],1)

    def test_null_historical_anchor_does_not_crash_new_root(self):
        self.legacy(anchor=None);self.check_new_root()

    def test_missing_historical_anchor_does_not_crash_new_root(self):
        self.legacy();self.check_new_root()

    def test_other_unreliable_anchor_shapes_stay_coordination(self):
        self.delivered('legacy');self.submit('legacy')
        for index,anchor in enumerate(('',[],['not-an-anchor'],'not-an-anchor',False,1,{})):
            self.issue('legacy',{'id':'bad-'+str(index),'body':'无可靠锚点','blocking':True,'anchor':anchor})
        self.check_new_root()

    def test_new_null_or_missing_anchor_never_auto_repairs(self):
        self.delivered();self.submit()
        self.issue('api',{'id':'missing','body':'缺锚点','blocking':True})
        self.issue('api',self.finding(id='null',anchor=None))
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM repair_links').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM review_issues WHERE status='needs_coordination'").fetchone()[0],2)

    def test_same_root_null_history_still_prevents_new_round(self):
        self.delivered();self.submit();self.issue('api',self.finding(anchor=None))
        self.issue('api',self.finding())
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM review_issues').fetchone()
            self.assertEqual((row['status'],row['rounds']),('needs_coordination',0))
            self.assertEqual(db.execute('SELECT count(*) FROM review_issues').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM repair_links').fetchone()[0],0)

    def test_same_path_changed_label_with_null_history_cannot_reset_rounds(self):
        self.legacy(anchor=None)
        self.delivered();self.submit();self.issue('api',self.finding())
        with self.store.transaction() as db:
            original=db.execute("SELECT id FROM review_issues WHERE root_task='api'").fetchone()[0]
            db.execute("UPDATE review_stages SET status='needs_fix' WHERE task_id='legacy'")
        self.delivered('renamed');self.submit('renamed','c'*40)
        self.issue('renamed',self.finding(id='different',anchor={'path':'api.py','reproduction':'changed'}))
        with self.store.transaction() as db:
            row=db.execute('SELECT * FROM review_issues WHERE id=?',(original,)).fetchone()
            self.assertEqual((row['root_task'],row['rounds'],row['status']),('api',1,'needs_coordination'))
            self.assertEqual(len(self.store._option(db,'issue_sources:'+original)),2)

    def test_two_round_cap_survives_unrelated_null_anchor(self):
        self.legacy(anchor=None)
        self.delivered();self.submit();self.issue('api',self.finding())
        for expected in (2,2):
            with self.store.transaction() as db:
                row=db.execute("SELECT * FROM review_issues WHERE root_task='api'").fetchone()
                db.execute("UPDATE tasks SET status='completed' WHERE id=?",(row['repair_task'],))
            self.issue('api',self.finding(id='another-id'))
            with self.store.transaction() as db:
                row=db.execute("SELECT * FROM review_issues WHERE root_task='api'").fetchone()
                self.assertEqual(row['rounds'],expected)
        self.assertEqual(row['status'],'escalated')

    def test_invalid_new_path_and_reproduction_do_not_auto_repair(self):
        self.delivered();self.submit()
        for index,anchor in enumerate(({'path':'../api.py','reproduction':'x'}, {'path':'api.py','reproduction':''},
                                       {'path':'outside.py','reproduction':'x'}, {'path':'api.py'})):
            self.issue('api',self.finding(id=str(index),anchor=anchor))
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM repair_links').fetchone()[0],0)


class PublicSettlementTests(unittest.TestCase):
    setUp=managed.ManagedReviewTests.setUp
    tearDown=managed.ManagedReviewTests.tearDown
    actor=managed.ManagedReviewTests.actor
    stage_helper=managed.ManagedReviewTests.stage_helper
    review=managed.ManagedReviewTests.review

    def test_finish_review_with_historical_null_preserves_needs_fix(self):
        helper=self.stage_helper();helper.delivered('legacy');helper.submit('legacy')
        helper.issue('legacy',helper.finding(anchor=None))
        helper.delivered();stage=helper.submit()
        result=self.review(stage,'needs_fix',[helper.finding()])
        self.assertEqual(result['verdict'],'needs_fix')
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT status FROM review_stages WHERE id=?',(stage['id'],)).fetchone()[0],'needs_fix')
            self.assertEqual(db.execute('SELECT count(*) FROM repair_links').fetchone()[0],1)
