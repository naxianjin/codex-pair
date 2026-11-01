"""临时 Store 与合成生产者回执；不连接真实模型或活动协作库。"""
import unittest
from unittest.mock import patch

from scripts import pair_review_runtime as runtime
from scripts.pair_core import PairError
from tests import test_final_receipt_settlement as fixtures


class IdleGrantedRepairTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.FinalSettlementTests()
        self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.addCleanup(self.f.doCleanups)
        f = self.f
        with f.store.transaction() as db:
            db.execute("UPDATE review_stages SET status='needs_validation' WHERE id=?", (f.stage['id'],))
        f.store.release_reviewer(f.ticket['epoch'], f.proof['id'], f.lease['owner'],
                                 outcome='completed', evidence='isolated exact terminal fixture')
        self.assertEqual(f.permission_status(), 'granted')

    def reconcile(self):
        f = self.f
        with patch.object(runtime, 'AppServer', f.observer), patch.object(
            f.store, 'reconcile_reviewer_execution', side_effect=AssertionError('idle lease released twice')
        ):
            return runtime.reconcile_review(f.store, *f.actor(), f.stage['id'], 'isolated exact terminal evidence')

    def assert_unapproved(self):
        with self.f.store.transaction() as db:
            self.assertEqual(db.execute('SELECT status FROM review_stages WHERE id=?', (self.f.stage['id'],)).fetchone()[0], 'needs_validation')

    def test_exact_success_no_approval_no_duplicate_release(self):
        result = self.reconcile()
        self.assertFalse(result['work_approved'])
        self.assertFalse(result['replayed'])
        self.assertEqual(self.f.permission_status(), 'success')
        self.assert_unapproved()
        with self.assertRaisesRegex(PairError, 'Execution not reconcilable'):
            self.reconcile()
        self.assertEqual(len(self.f.events('managed_permission_execution_result')), 1)

    def test_exact_failed_receipt(self):
        self.f.full['items'][0]['exit_code'] = 7
        self.f.full['status'] = 'failed'
        self.assertFalse(self.reconcile()['work_approved'])
        self.assertEqual(self.f.permission_status(), 'failed')
        self.assert_unapproved()

    def test_unknown_control(self):
        with self.f.store.transaction() as db:
            db.execute("UPDATE managed_permissions SET status='unknown'")
        self.assertFalse(self.reconcile()['work_approved'])
        self.assertEqual(self.f.permission_status(), 'success')
        self.assert_unapproved()

    def test_wrong_command_cannot_settle(self):
        self.f.full['items'][0]['command'] = ['wrong-command']
        self.reconcile()
        self.assertEqual(self.f.permission_status(), 'unknown')
        self.assert_unapproved()

    def test_wrong_item_cannot_settle(self):
        self.f.full['items'][0]['id'] = 'neighbor-item'
        self.reconcile()
        self.assertEqual(self.f.permission_status(), 'unknown')

    def test_wrong_reviewer_identity_rejected(self):
        self.f.proof = dict(self.f.proof, id='11111111-1111-4111-8111-111111111111')
        with self.assertRaises(PairError):
            self.reconcile()
        self.assertEqual(self.f.permission_status(), 'granted')

    def test_nonterminal_turn_rejected(self):
        self.f.full['status'] = 'inProgress'
        with self.assertRaisesRegex(PairError, 'Turn unresolved'):
            self.reconcile()
        self.assertEqual(self.f.permission_status(), 'granted')

    def test_stale_stage_execution_rejected(self):
        with self.f.store.transaction() as db:
            db.execute("UPDATE review_stages SET execution_id='old-execution' WHERE id=?", (self.f.stage['id'],))
        with self.assertRaisesRegex(PairError, 'Stale execution'):
            self.reconcile()
        self.assertEqual(self.f.permission_status(), 'granted')

    def test_nonterminal_item_cannot_settle(self):
        self.f.full['items'][0]['status'] = 'inProgress'
        self.reconcile()
        self.assertEqual(self.f.permission_status(), 'unknown')
        self.assert_unapproved()

    def test_missing_producer_cannot_settle(self):
        with self.f.store.transaction() as db:
            db.execute('DELETE FROM pair_settings WHERE key=?', (runtime.KEY + self.f.lease['execution_id'],))
        self.reconcile()
        self.assertEqual(self.f.permission_status(), 'unknown')
        self.assert_unapproved()

    def test_rebinding_during_observer_read_rejected(self):
        def mutate():
            with self.f.store.transaction() as db:
                db.execute("UPDATE members SET binding_version=binding_version+1 WHERE id='A'")
        self.f.mutate = mutate
        with self.assertRaisesRegex(PairError, 'Stale execution'):
            self.reconcile()
        self.assertEqual(self.f.permission_status(), 'granted')

    def test_change_between_read_and_settlement_rejected(self):
        original = runtime.settle_permissions
        def race(store, epoch, execution, turn, **kwargs):
            with store.transaction() as db:
                db.execute('UPDATE review_stages SET requirements_version=requirements_version+1 WHERE id=?', (self.f.stage['id'],))
            return original(store, epoch, execution, turn, **kwargs)
        with patch.object(runtime, 'settle_permissions', race), self.assertRaisesRegex(PairError, 'Stale final receipt binding'):
            self.reconcile()
        self.assertEqual(self.f.permission_status(), 'granted')
        self.assertEqual(self.f.events('managed_permission_execution_result'), [])

    def test_binding_change_between_read_and_settlement_rejected(self):
        original = runtime.settle_permissions
        def race(store, epoch, execution, turn, **kwargs):
            with store.transaction() as db:
                db.execute("UPDATE members SET binding_version=binding_version+1 WHERE id='A'")
            return original(store, epoch, execution, turn, **kwargs)
        with patch.object(runtime, 'settle_permissions', race), self.assertRaisesRegex(PairError, 'Stale final receipt binding'):
            self.reconcile()
        self.assertEqual(self.f.permission_status(), 'granted')
        self.assertEqual(self.f.events('managed_permission_execution_result'), [])


if __name__ == '__main__':
    unittest.main()
