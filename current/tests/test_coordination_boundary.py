"""C-COORD-001第一轮：协调豁免不能携带业务成果。"""
import unittest

from scripts.pair_core import PairError
from tests import test_core


class CoordinationBoundaryTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    claim = test_core.CoreTests.claim

    def message(self, task='work'):
        return next(m for m in self.store.status()['messages'] if m['task_id']==task and m['kind']=='task')

    def finish(self, message, commits=()):
        receipt=self.claim(message['id'])
        return self.store.complete('B',self.sessions['B'],self.directories['B'],message['id'],receipt['claim_token'],'已核对并完成报告',commits=commits)

    def reject_commit(self,kind):
        self.store.assign(kind,'报告','B',[kind+'.py'],task_kind=kind)
        message=self.message(kind)
        with self.assertRaisesRegex(PairError,'Coordination is report-only'):
            self.finish(message,['b'*40])
        task=self.store.task(kind)
        self.assertEqual(task['status'],'active')
        self.assertIsNone(task['commits_json'])

    def test_coordination_rejects_code_commits(self):
        self.reject_commit('coordination')

    def test_ordinary_planning_rejects_code_commits(self):
        self.reject_commit('planning')

    def test_assign_rejects_business_checks_for_report_tasks(self):
        for kind in ('coordination','planning'):
            with self.subTest(kind=kind),self.assertRaisesRegex(PairError,'Coordination is report-only'):
                self.store.assign(kind,'报告','B',['report.md'],task_kind=kind,required_checks=[{'name':'business','command':'test'}])

    def test_pending_reclassification_cannot_drop_existing_checks(self):
        self.store.control('paused')
        self.store.assign('work','业务','B',['work.py'],required_checks=[{'name':'business','command':'test'}])
        with self.assertRaisesRegex(PairError,'Coordination is report-only'):
            self.store.configure_task('A',self.sessions['A'],self.directories['A'],'work',kind='coordination')
        self.assertEqual(self.store.task('work')['task_kind'],'implementation')

    def test_pending_report_reclassification_still_rejects_commits(self):
        self.store.control('paused')
        self.store.assign('work','协调准备','B',['report.md'])
        self.store.configure_task('A',self.sessions['A'],self.directories['A'],'work',kind='coordination')
        self.store.control('running')
        with self.assertRaisesRegex(PairError,'Coordination is report-only'):
            self.finish(self.message(),['b'*40])

    def test_plain_report_completion_has_no_recursive_review(self):
        self.store.setting('review_required',True)
        for kind in ('coordination','planning'):
            self.store.assign(kind,'纯报告','B',[kind+'.md'],task_kind=kind)
            self.finish(self.message(kind))
            self.assertEqual(self.store.task(kind)['status'],'completed')
            with self.store.transaction() as db:
                self.assertTrue(self.store._stage_passed(db,kind))
                self.assertFalse(db.execute('SELECT 1 FROM review_stages WHERE task_id=?',(kind,)).fetchone())

    def test_business_review_cannot_be_disabled_by_configuration(self):
        self.store.control('paused')
        self.store.setting('review_required',True)
        self.store.assign('work','业务','B',['work.py'])
        self.store.configure_task('A',self.sessions['A'],self.directories['A'],'work',review_required=False)
        self.assertTrue(self.store.task('work')['review_required'])

    def test_historical_coordination_business_artifact_is_not_passed(self):
        self.store.control('paused')
        self.store.assign('work','历史协调','B',['report.md'],task_kind='coordination')
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='delivered',commits_json=? WHERE id='work'",('["'+'b'*40+'"]',))
            self.assertFalse(self.store._stage_passed(db,'work'))

    def test_complete_rejects_legacy_coordination_business_checks(self):
        self.store.assign('work','历史协调','B',['report.md'],task_kind='coordination')
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET required_checks=? WHERE id='work'",('[{"name":"business","command":"test"}]',))
        with self.assertRaisesRegex(PairError,'Coordination is report-only'):
            self.finish(self.message())
        self.assertEqual(self.store.task('work')['status'],'active')

    def test_reclassification_rejects_new_business_checks(self):
        self.store.control('paused')
        self.store.assign('work','协调准备','B',['report.md'])
        with self.assertRaisesRegex(PairError,'Coordination is report-only'):
            self.store.configure_task('A',self.sessions['A'],self.directories['A'],'work',kind='planning',
                                      required_checks=[{'name':'business','command':'test'}])

    def test_active_reclassification_remains_rejected(self):
        self.store.assign('work','业务','B',['work.py'])
        with self.assertRaisesRegex(PairError,'Pending task'):
            self.store.configure_task('A',self.sessions['A'],self.directories['A'],'work',kind='coordination')
