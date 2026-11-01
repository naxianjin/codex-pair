"""C-COORD-001第二轮：历史协调记录的既有审核阶段不得免审。"""
import unittest

from tests import test_stages


class CoordinationLegacyStageTests(unittest.TestCase):
    setUp=test_stages.StageTests.setUp
    tearDown=test_stages.StageTests.tearDown
    actor=test_stages.StageTests.actor
    delivered=test_stages.StageTests.delivered
    submit=test_stages.StageTests.submit

    def test_exact_c_pending_stage_reproducer(self):
        self.delivered();self.submit()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET task_kind='coordination',review_required=0,commits_json='[]',required_checks='[]' WHERE id='api'")
            self.assertFalse(self.store._stage_passed(db,'api'))

    def test_all_existing_coordination_stages_fail_closed(self):
        self.delivered();stage=self.submit()
        with self.store.transaction() as db:
            for kind in ('coordination','planning'):
                for state in ('pending','running','needs_fix','needs_validation','stale','passed'):
                    for version in (1,2):
                        with self.subTest(kind=kind,state=state,requirements=version):
                            db.execute("UPDATE tasks SET task_kind=?,review_required=0,commits_json='[]',required_checks='[]',requirements_version=1 WHERE id='api'",(kind,))
                            db.execute('UPDATE review_stages SET status=?,requirements_version=? WHERE id=?',(state,version,stage['id']))
                            self.assertFalse(self.store._stage_passed(db,'api'))

    def test_pure_coordination_without_stage_remains_passed(self):
        self.delivered()
        with self.store.transaction() as db:
            for kind in ('coordination','planning'):
                with self.subTest(kind=kind):
                    db.execute("UPDATE tasks SET task_kind=?,review_required=0,commits_json='[]',required_checks='[]' WHERE id='api'",(kind,))
                    self.assertTrue(self.store._stage_passed(db,'api'))

    def test_business_requires_current_passed_stage(self):
        self.delivered();stage=self.submit()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET task_kind='implementation',review_required=1,requirements_version=1 WHERE id='api'")
            for state in ('pending','running','needs_fix','needs_validation','stale','passed'):
                for version in (1,2):
                    with self.subTest(state=state,requirements=version):
                        db.execute('UPDATE review_stages SET status=?,requirements_version=? WHERE id=?',(state,version,stage['id']))
                        self.assertEqual(self.store._stage_passed(db,'api'),state=='passed' and version==1)
