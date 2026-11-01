"""结果通知按审核执行隔离；只使用临时Store和合成官方回执。"""
import unittest
from scripts.pair_core import PairError
from tests import test_final_receipt_settlement as fixtures


class ExecutionResultTests(unittest.TestCase):
    setUp = fixtures.FinalSettlementTests.setUp
    tearDown = fixtures.FinalSettlementTests.tearDown
    allow = fixtures.FinalSettlementTests.allow
    actor = fixtures.FinalSettlementTests.actor
    observer = fixtures.FinalSettlementTests.observer
    finish = fixtures.FinalSettlementTests.finish
    events = fixtures.FinalSettlementTests.events
    permission_status = fixtures.FinalSettlementTests.permission_status

    def seed_old_result(self, suffix=''):
        with self.store.transaction() as db:
            return self.store._send(db, 'B', 'A', 'prior needs_validation', kind='note',
                                    dedupe_key='review-result:' + self.stage['id'] + suffix)

    def result_rows(self):
        with self.store.transaction() as db:
            return [dict(r) for r in db.execute("SELECT * FROM messages WHERE dedupe_key LIKE 'review-result:%'")]

    def test_legacy_stage_result_does_not_rollback_new_pass(self):
        self.seed_old_result()
        self.assertEqual(self.finish()['verdict'], 'passed')
        rows = self.result_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['body'], 'prior needs_validation')
        self.assertEqual(rows[1]['dedupe_key'], 'review-result:' + self.stage['id'] + ':' + self.lease['execution_id'])
        self.assertEqual(self.events('review_effective_verdict')[-1]['effective_verdict'], 'passed')

    def test_other_execution_result_is_preserved(self):
        self.seed_old_result(':older-execution')
        self.assertEqual(self.finish()['verdict'], 'passed')
        self.assertEqual(len(self.result_rows()), 2)

    def test_same_execution_collision_still_rejects(self):
        self.seed_old_result(':' + self.lease['execution_id'])
        with self.assertRaisesRegex(PairError, 'Dedupe key reused'):
            self.finish()
        self.assertFalse(self.events('review_recorded'))

    def test_duplicate_finish_does_not_send_twice(self):
        self.assertEqual(self.finish()['verdict'], 'passed')
        with self.assertRaises(PairError):
            self.finish()
        self.assertEqual(len(self.result_rows()), 1)


if __name__ == '__main__':
    unittest.main()
