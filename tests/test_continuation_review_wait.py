import unittest

from tests import test_core, test_handoff_balance
from scripts.pair_core import PairError


class ContinuationReviewWaitTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    finish = test_handoff_balance.HandoffTests.finish
    acknowledge = test_handoff_balance.HandoffTests.acknowledge
    close_result = test_handoff_balance.HandoffTests.close_result

    def setup_review(self, partial=False):
        self.store.setting('window_flow', {'goal': '继续测试直到完成'})
        self.store.setting('review_required', True)
        self.store.assign('work', '真实待审交付', 'B', ['work.py'],
                          required_checks=[{'name': 'business', 'command': 'python -B -m unittest'}])
        self.finish('work', partial=partial)

    def plans(self):
        return [t for t in self.store.status()['tasks'] if t['task_kind'] == 'planning']

    def test_review_wait_result_wakes_coordinator_without_accepting_business(self):
        self.setup_review()
        self.assertEqual(self.plans(), [])
        self.close_result('work')
        self.assertEqual(len(self.plans()), 1)
        self.assertEqual(self.plans()[0]['status'], 'active')
        self.assertIn('delivery:work', self.plans()[0]['body'])
        self.assertEqual(self.store.task('work')['status'], 'review_wait')
        self.store.assign('consumer', '业务依赖不能放行', 'B', ['consumer.py'], dependencies=['work'],
                          required_checks=[{'name': 'business', 'command': 'python -B -m unittest'}])
        self.assertEqual(self.store.task('consumer')['status'], 'pending')

    def test_partial_result_also_wakes_coordinator(self):
        self.setup_review(partial=True)
        self.close_result('work')
        self.assertEqual(len(self.plans()), 1)
        self.assertEqual(self.store.task('work')['status'], 'handed_back')

    def test_schedule_does_not_duplicate_same_pending_review(self):
        self.setup_review()
        self.close_result('work')
        for _ in range(3):
            self.store.schedule()
        self.assertEqual(len(self.plans()), 1)

    def test_pause_does_not_create_continuation(self):
        self.setup_review()
        message = next(m for m in self.store.status()['messages'] if m['kind'] == 'result')
        claim = self.store.claim('A', self.sessions['A'], self.directories['A'], message_id=message['id'])
        self.acknowledge('A', 'delivery:work')
        self.store.control('paused')
        self.store.complete('A', self.sessions['A'], self.directories['A'], message['id'], claim['claim_token'], '暂停时仅收尾')
        self.assertEqual(self.plans(), [])

    def test_unknown_result_is_not_treated_as_settled(self):
        self.setup_review()
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='unknown' WHERE task_id='work' AND kind='result'")
        self.close_result('work')
        self.assertEqual(self.plans(), [])

    def test_old_review_backlog_cannot_complete_plan_without_next_work(self):
        self.setup_review()
        self.close_result('work')
        plan = self.plans()[0]
        message = next(m for m in self.store.status()['messages'] if m['task_id'] == plan['id'])
        claim = self.store.claim('A', self.sessions['A'], self.directories['A'], message_id=message['id'])
        with self.assertRaisesRegex(PairError, 'Register next work'):
            self.store.complete('A', self.sessions['A'], self.directories['A'], message['id'], claim['claim_token'], '仅重复旧待审状态')
        self.store.assign('review', '安排独立审核', 'A', ['review.md'], task_kind='coordination')
        self.store.complete('A', self.sessions['A'], self.directories['A'], message['id'], claim['claim_token'], '已登记审核')
        self.assertEqual(self.store.task('review')['status'], 'active')

    def test_completed_plan_does_not_recur_for_unchanged_review_backlog(self):
        self.test_old_review_backlog_cannot_complete_plan_without_next_work()
        self.store.schedule()
        self.assertEqual(len(self.plans()), 1)

    def test_goal_complete_still_rejects_unreviewed_work(self):
        self.setup_review()
        self.close_result('work')
        plan = self.plans()[0]
        message = next(m for m in self.store.status()['messages'] if m['task_id'] == plan['id'])
        claim = self.store.claim('A', self.sessions['A'], self.directories['A'], message_id=message['id'])
        with self.assertRaisesRegex(PairError, 'Work remains'):
            self.store.complete('A', self.sessions['A'], self.directories['A'], message['id'], claim['claim_token'], '不得把未审算完成', goal_complete=True)

    def test_status_explains_review_backlog_instead_of_idle(self):
        self.store.setting('review_required', True)
        for key in ('one', 'two'):
            self.store.assign(key, '产生真实待审交付', 'B', [key + '.py'],
                              required_checks=[{'name': 'business', 'command': 'python -B -m unittest'}])
            self.finish(key)
        self.store.assign('next', '普通测试被门禁拦截', 'B', ['next.py'], task_kind='verification',
                          required_checks=[{'name': 'business', 'command': 'python -B -m unittest'}])
        self.assertEqual(self.store.task('next')['status'], 'pending')
        waiting = next(w for w in self.store.status()['waiting'] if w.get('task') == 'next')
        self.assertIn('待审名额已满', ' '.join(waiting['reasons']))


if __name__ == '__main__':
    unittest.main()
