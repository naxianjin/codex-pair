"""真实调度/完成产生的规划交付依赖；仅使用临时SQLite与标记文件。"""
import json
import unittest

from scripts.pair_core import PairError
from tests import test_core, test_handoff_balance


class PlanningDeliveryTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    finish = test_handoff_balance.HandoffTests.finish
    acknowledge = test_handoff_balance.HandoffTests.acknowledge
    close_result = test_handoff_balance.HandoffTests.close_result

    def completed_plan(self):
        self.store.setting('window_flow', {'goal': '继续原目标'})
        self.store.assign('one', '前置真实业务', 'A', ['one.py'])
        self.finish('one')
        self.close_result('one')
        plan = next(t for t in self.store.status()['tasks'] if t['task_kind'] == 'planning')
        message = next(m for m in self.store.status()['messages'] if m['task_id'] == plan['id'])
        claim = self.store.claim('A', self.sessions['A'], self.directories['A'], message_id=message['id'])
        self.store.assign('after-plan', '规划后的合法业务', 'B', ['after.py'], dependencies=[plan['id']])
        result = '已核对原目标并登记after-plan。' * 400
        self.store.complete('A', self.sessions['A'], self.directories['A'], message['id'], claim['claim_token'], result)
        self.plan, self.plan_message, self.plan_claim, self.plan_result = plan, message, claim, result
        self.record = 'delivery:' + plan['id']
        self.followup = next(m for m in self.store.status()['messages'] if m['task_id'] == 'after-plan')
        return self.record

    def envelope(self):
        return json.loads(self.store.envelope(self.store.member('B'), self.store.message(self.followup['id'])))

    def test_actual_completed_plan_dependency_has_readable_real_delivery(self):
        delivery = self.completed_plan()
        self.assertEqual(self.store.task('after-plan')['status'], 'active')
        self.assertIn(delivery, self.envelope()['required_deliveries'])
        page = self.store.read_delivery('B', self.sessions['B'], self.directories['B'], delivery)
        self.assertEqual(page['text'], self.plan_result[:2000])

    def test_full_pagination_digest_and_once_only_followup(self):
        delivery = self.completed_plan()
        claim = self.store.claim('B', self.sessions['B'], self.directories['B'], message_id=self.followup['id'])
        end = len(self.plan_result) - 1
        last = self.store.read_delivery('B', self.sessions['B'], self.directories['B'], delivery, offset=end)
        with self.assertRaisesRegex(PairError, 'fully read'):
            self.store.ack_delivery('B', self.sessions['B'], self.directories['B'], delivery, last['digest'])
        with self.assertRaisesRegex(PairError, 'acknowledgement'):
            self.store.complete('B', self.sessions['B'], self.directories['B'], self.followup['id'], claim['claim_token'], '尚未确认')
        offset = 0
        text = ''
        while True:
            page = self.store.read_delivery('B', self.sessions['B'], self.directories['B'], delivery, offset=offset)
            text += page['text']
            offset = page['next_offset']
            if offset is None: break
        self.assertEqual(text, self.plan_result)
        with self.assertRaisesRegex(PairError, 'version'):
            self.store.ack_delivery('B', self.sessions['B'], self.directories['B'], delivery, 'wrong')
        self.store.ack_delivery('B', self.sessions['B'], self.directories['B'], delivery, page['digest'])
        marker = self.root / 'side-effect.txt'
        marker.write_text('once', encoding='utf-8')
        self.store.complete('B', self.sessions['B'], self.directories['B'], self.followup['id'], claim['claim_token'], '业务只执行一次')
        self.assertFalse(self.store.claim('B', self.sessions['B'], self.directories['B'], message_id=self.followup['id'])['claimed'])
        self.assertTrue(self.store.complete('B', self.sessions['B'], self.directories['B'], self.followup['id'], claim['claim_token'], '重复回执')['duplicate'])
        self.assertEqual(marker.read_text(encoding='utf-8'), 'once')

    def test_planning_duplicate_completion_has_no_result_or_recursive_plan(self):
        self.completed_plan()
        before = self.store.status()
        self.assertTrue(self.store.complete('A', self.sessions['A'], self.directories['A'], self.plan_message['id'], self.plan_claim['claim_token'], '重复规划完成')['duplicate'])
        self.store.schedule()
        after = self.store.status()
        self.assertEqual(len(before['messages']), len(after['messages']))
        self.assertEqual(len(before['tasks']), len(after['tasks']))
        self.assertFalse(any(m['kind'] == 'result' and m['task_id'] == self.plan['id'] for m in after['messages']))
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM deliveries WHERE task_id=?', (self.plan['id'],)).fetchone()[0], 1)

    def remove_legacy_delivery(self):
        # 仅临时fixture模拟旧版已完成规划：真实result保留，不触碰D或运行库。
        with self.store.transaction() as db:
            db.execute('DELETE FROM deliveries WHERE id=?', (self.record,))

    def test_legacy_completed_plan_reuses_real_result_without_execution_or_notification(self):
        self.completed_plan()
        self.remove_legacy_delivery()
        before = self.store.status()
        self.assertIn(self.record, self.envelope()['required_deliveries'])
        self.assertIn(self.record, self.envelope()['required_deliveries'])
        self.acknowledge('B', self.record)
        with self.store.transaction() as db:
            record = dict(db.execute('SELECT * FROM deliveries WHERE id=?', (self.record,)).fetchone())
            self.assertEqual(record['body'], self.plan_result)
            self.assertEqual(json.loads(record['commits_json']), [])
        self.assertEqual(len(self.store.status()['messages']), len(before['messages']))
        self.assertEqual(len(self.store.status()['tasks']), len(before['tasks']))

    def test_legacy_missing_real_result_fails_envelope_and_completion_closed(self):
        self.completed_plan()
        self.remove_legacy_delivery()
        claim = self.store.claim('B', self.sessions['B'], self.directories['B'], message_id=self.followup['id'])
        for value in (None, '', '   '):
            with self.store.transaction() as db:
                db.execute('UPDATE tasks SET result=? WHERE id=?', (value, self.plan['id']))
            with self.assertRaisesRegex(PairError, 'Planning result unavailable'): self.envelope()
            with self.assertRaisesRegex(PairError, 'Planning result unavailable'):
                self.store.complete('B', self.sessions['B'], self.directories['B'], self.followup['id'], claim['claim_token'], '不得跳过不存在的前置交付')
            with self.store.transaction() as db:
                self.assertIsNone(db.execute('SELECT * FROM deliveries WHERE id=?', (self.record,)).fetchone())


if __name__ == '__main__':
    unittest.main()
