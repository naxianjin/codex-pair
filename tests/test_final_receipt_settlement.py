"""真实Store/解析/结算/完成门禁；仅官方元数据传输使用隔离observer。"""
from contextlib import closing
import copy
import json
from pathlib import Path
import shlex
import sqlite3
import unittest
from unittest.mock import patch

from scripts import pair_review_runtime as runtime
from scripts.pair_core import PairError, Store
from tests import test_managed_review, test_stages, test_integration


class FinalSettlementTests(unittest.TestCase):
    tearDown = test_managed_review.ManagedReviewTests.tearDown
    allow = test_managed_review.ManagedReviewTests.allow
    actor = test_managed_review.ManagedReviewTests.actor

    def setUp(self):
        test_managed_review.ManagedReviewTests.setUp(self)
        self.project.rmdir()
        test_integration.seed(self.project)
        directory = self.project / '.codex-pair/state' / self.store.pair_id
        directory.mkdir(parents=True)
        with closing(sqlite3.connect(self.store.path)) as source, closing(sqlite3.connect(directory / 'state.sqlite3')) as target:
            source.backup(target)
        self.store = Store(self.store.pair_id, home=self.home, state_root=directory.parent)
        with self.store.transaction() as db:
            parent = json.loads(self.store._member(db, 'A')['proof'])
            parent['cliVersion'] = '0.163.0'
            db.execute("UPDATE members SET proof=? WHERE id='A'", (json.dumps(parent),))
        self.argv = ['C:/Program Files/PowerShell/7/pwsh.exe', '-NoProfile', '-Command', 'python -m unittest']
        self.command = shlex.join(self.argv)
        helper = test_stages.StageTests()
        helper.__dict__.update(self.__dict__)
        helper.delivered()
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET required_checks=? WHERE id=?', (json.dumps([{'name': 'unit', 'command': self.command}]), 'api'))
        self.stage = helper.submit()
        self.store.begin_review(self.stage['id'], self.ticket['epoch'], self.proof['id'], self.proof, self.lease['owner'], self.lease['execution_id'])
        self.store.record_review_turn(self.stage['id'], self.ticket['epoch'], self.lease['execution_id'], 'turn-one')
        self.permission = self.store.capture_managed_approval(self.ticket['epoch'], self.proof['id'], self.lease['execution_id'],
            {'id': 1, 'method': 'item/commandExecution/requestApproval', 'params': {'threadId': self.proof['id'],
             'turnId': 'turn-one', 'itemId': 'last-item', 'command': self.command, 'cwd': self.proof['cwd']}})
        self.allow(self.permission)
        self.store.managed_approval_result(self.permission['id'])
        self.item = {'id': 'last-item', 'type': 'CommandExecution', 'status': 'completed', 'exit_code': 0,
                     'command': self.argv, 'cwd': self.proof['cwd']}
        report = {'candidate_digest': self.stage['digest'], 'verdict': 'passed', 'findings': [],
                  'checks': [{'name': 'unit', 'required': True, 'status': 'passed', 'evidence': 'actual isolated exact receipt', 'receipt_id': 'last-item'}]}
        self.raw = '\n' + json.dumps(report, indent=2) + '\n'
        self.message = {'type': 'agentMessage', 'text': self.raw}
        self.notification = {'id': 'turn-one', 'threadId': self.proof['id'], 'status': 'completed', 'completedAt': 123, 'items': [self.message]}
        self.full = dict(self.notification, items=[self.item, self.message])
        self.controller = runtime.ReviewerController(self.store)
        self.controller.state = {'epoch': self.ticket['epoch'], 'session': self.proof['id'], 'execution_id': self.lease['execution_id'],
                                 'owner': self.lease['owner'], 'turn_id': 'turn-one', 'cwd': self.proof['cwd']}
        self.mutate = None
        from tests.producer_fixture import install
        install(self,self.store.path)

    def observer(self, **kwargs):
        fixture = self
        class Observer:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read_thread(self, session): return fixture.proof
            def turns(self, session, *, items_view):
                fixture.assertEqual(items_view, 'full')
                if fixture.mutate: fixture.mutate()
                return iter([copy.deepcopy(fixture.full)])
        return Observer()

    def finish(self, unchanged=True):
        return self.controller._finish_final_turn(self.stage, self.notification, self.proof,
            files_unchanged=unchanged, observer_factory=self.observer)

    def permission_status(self):
        with self.store.transaction() as db:
            return db.execute('SELECT status FROM managed_permissions WHERE id=?', (self.permission['id'],)).fetchone()[0]

    def events(self, kind):
        with self.store.transaction() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT data FROM events WHERE kind=? ORDER BY seq', (kind,))]

    def rollout(self, items):
        sessions = self.home / 'sessions'
        sessions.mkdir(exist_ok=True)
        path = sessions / 'final.jsonl'
        self.proof['path'] = str(path)
        rows = [{'type': 'session_meta', 'payload': {'id': self.proof['id'], 'cwd': self.proof['cwd']}},
                {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn-one'}}]
        rows += [{'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': self.proof['id'], 'turn_id': 'turn-one', 'item': item}} for item in items]
        rows.append({'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'turn-one', 'completed_at': 123}})
        path.write_text(''.join(json.dumps(row) + '\n' for row in rows), encoding='utf-8')

    def test_missing_notification_item_full_rollout_settles_before_verdict(self):
        runtime.settle_permissions(self.store, self.ticket['epoch'], self.lease['execution_id'], self.notification)
        self.assertEqual(self.permission_status(), 'unknown')
        self.full['items'] = [self.message]
        self.rollout([self.item])
        self.assertEqual(self.finish()['verdict'], 'passed')
        self.assertEqual(self.permission_status(), 'success')
        self.assertEqual(self.events('review_model_report')[0]['raw_messages'], [self.raw])
        self.assertEqual(self.events('review_controller_report')[0]['report']['verdict'], 'passed')
        self.assertEqual(self.events('review_effective_verdict')[0]['effective_verdict'], 'passed')

    def test_full_notification_settlement_is_idempotent(self):
        self.notification['items'] = [self.item, self.message]
        runtime.settle_permissions(self.store, self.ticket['epoch'], self.lease['execution_id'], self.notification)
        count = len(self.events('managed_permission_execution_result'))
        self.assertEqual(self.finish()['verdict'], 'passed')
        self.assertEqual(len(self.events('managed_permission_execution_result')), count)

    def test_missing_final_item_stays_unknown_and_preserves_downgrade_reports(self):
        self.full['items'] = [self.message]
        self.assertEqual(self.finish()['verdict'], 'needs_validation')
        self.assertEqual(self.permission_status(), 'unknown')
        self.assertEqual(self.events('review_model_report')[0]['raw_messages'], [self.raw])
        self.assertIn('permission_execution_unresolved', self.events('review_effective_verdict')[0]['reasons'])
        self.assertIn('required_receipt_mismatch:unit', self.events('review_controller_report')[0]['report']['validation_reasons'])

    def test_conflicting_final_rollout_stays_unknown(self):
        self.rollout([self.item, dict(self.item, exit_code=1)])
        self.assertEqual(self.finish()['verdict'], 'needs_validation')
        self.assertEqual(self.permission_status(), 'unknown')

    def test_nonzero_final_receipt_cannot_pass(self):
        self.full['items'] = [dict(self.item, exit_code=1), self.message]
        self.assertEqual(self.finish()['verdict'], 'needs_validation')
        self.assertEqual(self.permission_status(), 'failed')

    def test_extra_shell_argument_keeps_permission_unknown(self):
        self.full['items'] = [dict(self.item, command=self.argv[:1] + ['-NoLogo'] + self.argv[1:]), self.message]
        self.assertEqual(self.finish()['verdict'], 'needs_validation')
        self.assertEqual(self.permission_status(), 'unknown')

    def test_nested_shell_wrapper_keeps_permission_unknown(self):
        wrapped = self.argv[:-1] + ['pwsh -Command "python -m unittest"']
        self.full['items'] = [dict(self.item, command=wrapped), self.message]
        self.assertEqual(self.finish()['verdict'], 'needs_validation')
        self.assertEqual(self.permission_status(), 'unknown')

    def test_unfinished_full_turn_never_settles_or_passes(self):
        self.full['status'] = 'inProgress'
        with self.assertRaisesRegex(PairError, 'Final turn unverified'): self.finish()
        self.assertEqual(self.permission_status(), 'granted')

    def test_changed_files_reason_is_separate_from_normalized_passed_report(self):
        self.assertEqual(self.finish(False)['verdict'], 'needs_validation')
        outcome = self.events('review_effective_verdict')[0]
        self.assertEqual(outcome['normalized_verdict'], 'passed')
        self.assertIn('candidate_files_changed', outcome['reasons'])

    def test_binding_stage_epoch_execution_or_owner_changes_during_read_reject(self):
        statements = ["UPDATE members SET binding_version=binding_version+1 WHERE id='A'",
                      "UPDATE review_stages SET digest='changed'", "UPDATE reviewer_runtime SET epoch=epoch+1",
                      "UPDATE reviewer_runtime SET execution_id='changed'", "UPDATE reviewer_runtime SET owner='changed'",
                      "UPDATE review_stages SET turn_id='changed'", "UPDATE review_stages SET id='changed'",
                      "UPDATE review_stages SET requirements_version=requirements_version+1"]
        for statement in statements:
            fixture = type(self)()
            fixture.setUp()
            try:
                def mutate():
                    with fixture.store.transaction() as db: db.execute(statement)
                fixture.mutate = mutate
                with self.subTest(statement=statement), self.assertRaises((PairError, ValueError)):
                    fixture.finish()
                self.assertEqual(fixture.permission_status(), 'granted')
                self.assertEqual(fixture.events('review_effective_verdict'), [])
            finally:
                fixture.doCleanups()
                fixture.tearDown()

    def test_finish_write_transaction_rechecks_snapshot(self):
        snapshot = runtime.final_review_snapshot(self.store, self.stage, self.controller.state)
        with self.store.transaction() as db: db.execute("UPDATE reviewer_runtime SET owner='changed'")
        report = json.loads(self.raw)
        with self.assertRaisesRegex(PairError, 'Stale final receipt binding'):
            self.store.finish_review(self.stage['id'], self.ticket['epoch'], self.proof['id'], self.proof,
                self.lease['owner'], self.lease['execution_id'], report, files_unchanged=True, receipt_snapshot=snapshot)

    def test_reconcile_appends_without_erasing_reports_or_approving_work(self):
        self.full['items'] = [self.message]
        self.assertEqual(self.finish()['verdict'], 'needs_validation')
        with self.store.transaction() as db:
            previous = db.execute('SELECT evidence FROM review_stages WHERE id=?', (self.stage['id'],)).fetchone()[0]
        self.store.release_reviewer(self.ticket['epoch'], self.proof['id'], self.lease['owner'], outcome='completed', evidence='isolated terminal proof')
        self.full['items'] = [self.item, self.message]
        with patch.object(runtime, 'AppServer', self.observer):
            result = runtime.reconcile_review(self.store, 'A', self.sessions['A'], self.directories['A'], self.stage['id'], 'exact isolated terminal receipt')
        self.assertFalse(result['work_approved'])
        with self.store.transaction() as db:
            current = db.execute('SELECT status,evidence FROM review_stages WHERE id=?', (self.stage['id'],)).fetchone()
        self.assertEqual(tuple(current), ('needs_validation', previous))
        self.assertTrue(self.events('review_reconciled'))
        self.assertTrue(self.events('review_model_report'))
