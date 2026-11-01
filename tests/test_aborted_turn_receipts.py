"""准确中断回执的隔离验证；只用临时日志、Store和模拟官方接口。"""
import copy
import json
import unittest
from unittest.mock import patch

from scripts import pair_transport as transport, pair_review_runtime as runtime, pair_producer as producer
from scripts.pair_core import PairError
from tests import test_exact_managed_receipts as logs, test_strict_producer as strict


class AbortedLogTests(unittest.TestCase):
    def setUp(self):
        logs.RolloutReceiptTests.setUp(self)
        self.rows[-1]['payload'] = {'type': 'turn_aborted', 'turn_id': 'turn',
                                    'completed_at': 123, 'reason': 'interrupted'}
        self.turn = {'id': 'turn', 'threadId': 'session', 'status': 'interrupted', 'completedAt': 123}

    def read(self, rows=None, turn=None, **kwargs):
        data = self.rows if rows is None else rows
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in data), encoding='utf-8')
        return transport.rollout_command_items(self.member, self.thread, 'turn',
                                               terminal_turn=self.turn if turn is None else turn, **kwargs)

    def test_correlated_abort_reads_completed_item(self):
        self.assertEqual(self.read()[0]['exitCode'], 0)

    def test_default_reader_does_not_accept_abort(self):
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in self.rows), encoding='utf-8')
        self.assertIsNone(transport.rollout_command_items(self.member, self.thread, 'turn'))

    def test_api_binding_and_status_must_match(self):
        for changes in ({'id': 'other'}, {'threadId': 'other'}, {'status': 'inProgress'},
                        {'status': 'completed'}, {'status': 'failed'}, {'completedAt': 124}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.read(turn=dict(self.turn, **changes)))

    def test_invalid_timestamps_rejected(self):
        for stamp in (None, True, False, 0, -1, '123', float('inf'), float('nan'), 10 ** 400):
            with self.subTest(stamp=stamp):
                rows = copy.deepcopy(self.rows)
                rows[-1]['payload']['completed_at'] = stamp
                self.assertIsNone(self.read(rows, dict(self.turn, completedAt=stamp)))

    def test_missing_or_unrelated_abort_rejected(self):
        self.assertIsNone(self.read(self.rows[:-1]))
        for changes in ({'turn_id': 'other'}, {'reason': 'unknown'}, {'completed_at': 124}):
            rows = copy.deepcopy(self.rows)
            rows[-1]['payload'].update(changes)
            with self.subTest(changes=changes):
                self.assertIsNone(self.read(rows))

    def test_missing_start_and_wrong_owner_rejected(self):
        self.assertIsNone(self.read([self.rows[0], *self.rows[2:]]))
        rows = copy.deepcopy(self.rows)
        rows[0]['payload']['id'] = 'other'
        self.assertIsNone(self.read(rows))

    def test_duplicate_or_conflicting_terminal_rejected(self):
        complete = {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn', 'completed_at': 123}}
        for rows in (self.rows + [self.rows[-1]], self.rows + [complete], self.rows[:-1] + [complete, self.rows[-1]]):
            with self.subTest(rows=rows):
                self.assertIsNone(self.read(rows))

    def test_items_or_restart_after_abort_rejected(self):
        for event in (self.rows[2], self.rows[1]):
            self.assertIsNone(self.read(self.rows + [event]))

    def test_ordinary_complete_still_works(self):
        rows = copy.deepcopy(self.rows)
        rows[-1]['payload'] = {'type': 'task_complete', 'turn_id': 'turn', 'completed_at': 123}
        self.assertEqual(self.read(rows, dict(self.turn, status='completed'))[0]['exitCode'], 0)

    def test_correlated_abort_cannot_claim_running_reader_mode(self):
        self.assertIsNone(self.read(require_terminal=False))


class AbortedReconcileTests(unittest.TestCase):
    def setUp(self):
        self.helper = strict.ProducerLifecycleTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.f = self.helper.f
        producer.record_producer(self.f.store, self.f.stage['id'], self.f.controller.state, self.helper.launch, finished=True)
        self.f.full['status'] = 'interrupted'
        self.path = self.f.home / 'sessions/final.jsonl'
        rows = [json.loads(line) for line in self.path.read_text(encoding='utf-8').splitlines()]
        rows[-1]['payload'] = {'type': 'turn_aborted', 'turn_id': 'turn-one', 'completed_at': 123, 'reason': 'interrupted'}
        self.path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')
        with self.f.store.transaction() as db:
            db.execute("UPDATE review_stages SET status='needs_validation' WHERE id=?", (self.f.stage['id'],))
        self.f.store.release_reviewer(self.f.ticket['epoch'], self.f.proof['id'], self.f.lease['owner'],
                                      outcome='failed', evidence='isolated interrupted original turn')

    def reconcile(self):
        with patch.object(runtime, 'AppServer', self.f.observer):
            return runtime.reconcile_review(self.f.store, *self.f.actor(), self.f.stage['id'], 'isolated dual source abort')

    def test_exact_alpha_dual_source_settles_without_approving(self):
        result = self.reconcile()
        self.assertEqual(self.f.permission_status(), 'success')
        self.assertFalse(result['replayed'])
        self.assertFalse(result['work_approved'])
        with self.f.store.transaction() as db:
            self.assertEqual(db.execute('SELECT status FROM review_stages WHERE id=?', (self.f.stage['id'],)).fetchone()[0], 'needs_validation')

    def test_missing_raw_terminal_never_settles_success(self):
        lines = self.path.read_text(encoding='utf-8').splitlines()
        self.path.write_text('\n'.join(lines[:-1]) + '\n', encoding='utf-8')
        self.reconcile()
        self.assertEqual(self.f.permission_status(), 'unknown')

    def test_wrong_api_timestamp_never_settles_success(self):
        self.f.full['completedAt'] = 124
        self.reconcile()
        self.assertEqual(self.f.permission_status(), 'unknown')

    def test_nonterminal_tool_never_settles_success(self):
        self.f.full['items'][0]['status'] = 'inProgress'
        self.reconcile()
        self.assertEqual(self.f.permission_status(), 'unknown')

    def test_wrong_command_keeps_dual_source_rejection(self):
        self.f.full['items'][0]['command'] = 'different-command'
        self.reconcile()
        self.assertEqual(self.f.permission_status(), 'unknown')

    def test_missing_api_receipt_cannot_use_raw_alone(self):
        self.f.full['items'] = []
        self.reconcile()
        self.assertEqual(self.f.permission_status(), 'unknown')

    def test_explicit_wrong_api_thread_is_rejected_before_settlement(self):
        for value in ('wrong-official-thread', '', None, False):
            with self.subTest(thread_id=value):
                self.f.full['threadId'] = value
                with self.assertRaisesRegex(PairError, 'Reviewer turn identity mismatch'):
                    self.reconcile()
                self.assertEqual(self.f.permission_status(), 'granted')
                self.assertEqual(self.f.events('managed_permission_execution_result'), [])

    def test_omitted_api_thread_uses_verified_thread_context(self):
        self.f.full.pop('threadId')
        self.reconcile()
        self.assertEqual(self.f.permission_status(), 'success')

    def reconcile_turns(self, turns):
        def factory(**kwargs):
            observer = self.f.observer(**kwargs)
            observer.turns = lambda *args, **options: iter(turns)
            return observer
        with patch.object(runtime, 'AppServer', factory):
            return runtime.reconcile_review(self.f.store, *self.f.actor(), self.f.stage['id'], 'isolated exact turn uniqueness')

    def test_duplicate_api_turns_rejected_without_database_changes(self):
        good = copy.deepcopy(self.f.full)
        for changes in ({}, {'completedAt': 124}, {'threadId': 'wrong'}, {'status': 'completed'}):
            for reverse in (False, True):
                records = [good, dict(good, **changes)]
                if reverse:
                    records.reverse()
                with self.subTest(changes=changes, reverse=reverse):
                    before = self.f.store.path.read_bytes()
                    with self.assertRaisesRegex(PairError, 'Ambiguous reconciliation turn'):
                        self.reconcile_turns(records)
                    self.assertEqual(before, self.f.store.path.read_bytes())
                    self.assertEqual(self.f.permission_status(), 'granted')

    def test_duplicate_later_in_lazy_pages_rejected(self):
        good = copy.deepcopy(self.f.full)
        def pages():
            yield good
            yield dict(good, id='unrelated-turn')
            yield dict(good, completedAt=124)
        before = self.f.store.path.read_bytes()
        with self.assertRaisesRegex(PairError, 'Ambiguous reconciliation turn'):
            self.reconcile_turns(pages())
        self.assertEqual(before, self.f.store.path.read_bytes())

    def test_page_failure_after_first_match_preserves_database(self):
        def pages():
            yield self.f.full
            raise RuntimeError('isolated page failed')
        before = self.f.store.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'isolated page failed'):
            self.reconcile_turns(pages())
        self.assertEqual(before, self.f.store.path.read_bytes())

    def test_unique_turn_among_unrelated_records_still_settles(self):
        result = self.reconcile_turns([dict(self.f.full, id='other-before'), self.f.full,
                                       dict(self.f.full, id='other-after')])
        self.assertEqual(self.f.permission_status(), 'success')
        self.assertFalse(result['work_approved'])
        self.assertFalse(result['replayed'])

    def test_missing_exact_turn_preserves_database(self):
        before = self.f.store.path.read_bytes()
        with self.assertRaisesRegex(PairError, 'Turn unresolved'):
            self.reconcile_turns([dict(self.f.full, id='other')])
        self.assertEqual(before, self.f.store.path.read_bytes())


if __name__ == '__main__':
    unittest.main()
