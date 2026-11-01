"""真实审批入口与临时SQLite；网络仅使用假客户端。"""
import copy
import json
import queue
import shlex
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import pair_review_runtime as runtime
from tests import test_exact_managed_receipts as exact


class SequentialTests(unittest.TestCase):
    setUp = exact.ExactReceiptTests.setUp
    tearDown = exact.ExactReceiptTests.tearDown
    actor = exact.ExactReceiptTests.actor
    allow = exact.ExactReceiptTests.allow
    status = exact.ExactReceiptTests.status

    def controller(self, turn=None):
        controller = runtime.ReviewerController(self.store)
        controller.state = {'epoch': self.ticket['epoch'], 'session': self.proof['id'],
                            'execution_id': self.lease['execution_id'], 'turn_id': 'turn-one',
                            'cwd': self.proof['cwd']}
        self.responses = []
        self.reads = []
        view = copy.deepcopy(turn or dict(self.turn, status='inProgress'))
        def turns(session, **kwargs):
            self.reads.append((session, kwargs))
            return iter([view])
        controller.client = SimpleNamespace(server_requests=queue.Queue(),
            _write=self.responses.append, read_thread=lambda session: dict(self.proof), turns=turns)
        return controller

    def second(self, **changes):
        params = dict(self.params, itemId='command-two', command='git rev-parse HEAD')
        params.update(changes)
        return {'id': 2, 'method': 'item/commandExecution/requestApproval', 'params': params}

    def permissions(self):
        with self.store.transaction() as db:
            return [dict(r) for r in db.execute('SELECT * FROM managed_permissions ORDER BY created')]

    def test_completed_first_gets_independent_second_approval(self):
        controller = self.controller()
        controller.client.server_requests.put(self.second())
        controller._permissions()
        self.assertEqual(self.status(), 'success')
        rows = self.permissions()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['status'], 'pending')
        self.assertEqual(self.responses, [])
        self.allow({'id': rows[1]['id'], 'digest': rows[1]['digest']})
        controller._permissions()
        self.assertEqual(self.responses, [{'id': 2, 'result': {'decision': 'accept'}}])
        controller._permissions()
        self.assertEqual(len(self.responses), 1)
        self.assertEqual(self.reads[0][1], {'items_view': 'full'})

    def test_bad_receipts_keep_gate_and_decline_second(self):
        variants = [dict(self.turn, id='other'), dict(self.turn, items=[])]
        for key, value in [('id', 'other'), ('command', 'different'), ('cwd', self.directories['B']),
                           ('exitCode', False), ('status', 'inProgress')]:
            variants.append(dict(self.turn, items=[dict(self.item, **{key: value})]))
        variants.append(dict(self.turn, items=[self.item, dict(self.item, exitCode=1)]))
        for turn in variants:
            with self.subTest(turn=turn):
                with self.store.transaction() as db:
                    db.execute("UPDATE managed_permissions SET status='granted' WHERE id=?", (self.request['id'],))
                controller = self.controller(dict(turn, status='inProgress'))
                controller.client.server_requests.put(self.second())
                controller._permissions()
                self.assertIn(self.status(), ('granted', 'unknown'))
                self.assertEqual(len(self.permissions()), 1)
                self.assertEqual(self.responses, [{'id': 2, 'result': {'decision': 'decline'}}])

    def test_wrong_next_turn_execution_and_binding_never_release_gate(self):
        for variant in ('turn', 'thread', 'execution', 'binding', 'changed_during_read'):
            with self.subTest(variant=variant):
                with self.store.transaction() as db:
                    original = dict(self.store._member(db, 'A'))
                    db.execute("UPDATE managed_permissions SET status='granted' WHERE id=?", (self.request['id'],))
                controller = self.controller()
                request = self.second()
                if variant == 'turn':request['params']['turnId'] = 'other'
                if variant == 'thread':request['params']['threadId'] = self.sessions['B']
                if variant == 'execution':controller.state['execution_id'] = 'other'
                if variant == 'binding':
                    with self.store.transaction() as db:
                        db.execute("UPDATE members SET binding_version=binding_version+1 WHERE id='A'")
                if variant == 'changed_during_read':
                    def turns(*args, **kwargs):
                        with self.store.transaction() as db:
                            db.execute("UPDATE members SET binding_version=binding_version+1 WHERE id='A'")
                        return iter([dict(self.turn, status='inProgress')])
                    controller.client.turns = turns
                controller.client.server_requests.put(request)
                controller._permissions()
                self.assertIn(self.status(), ('granted', 'unknown'))
                self.assertEqual(len(self.permissions()), 1)
                self.assertEqual(self.responses[0]['result']['decision'], 'decline')
                with self.store.transaction() as db:
                    db.execute("UPDATE members SET binding_version=? WHERE id='A'", (original['binding_version'],))

    def test_other_execution_unresolved_permission_is_not_settled(self):
        with self.store.transaction() as db:
            db.execute("UPDATE managed_permissions SET execution_id='older' WHERE id=?", (self.request['id'],))
        controller = self.controller()
        controller.client.server_requests.put(self.second())
        controller._permissions()
        self.assertEqual(self.status(), 'granted')
        self.assertEqual(len(self.permissions()), 1)
        self.assertEqual(self.reads, [])
        self.assertEqual(self.responses[0]['result']['decision'], 'decline')

    def test_diagnostic_is_bounded_redacted_and_distinguishes_unsupported(self):
        controller = self.controller(dict(self.turn, items=[]))
        request = self.second()
        request['params'].update(reason='Bearer secret-value', output='private output')
        controller.client.server_requests.put(request)
        controller.client.server_requests.put({'id': 3, 'method': 'unknown/request', 'params': {}})
        controller._permissions()
        with self.store.transaction() as db:
            rows = [json.loads(r[0]) for r in db.execute("SELECT data FROM events WHERE kind='managed_permission_diagnostic'")]
        self.assertEqual([r['reason'] for r in rows], ['capture_failed', 'unsupported'])
        self.assertEqual(rows[0]['request_id'], '2')
        self.assertEqual(rows[0]['item'], 'command-two')
        self.assertEqual(rows[0]['exception'], 'PairError')
        self.assertNotIn('secret-value', json.dumps(rows))
        self.assertNotIn('private output', json.dumps(rows))
        self.assertNotIn('command', rows[0])
        self.assertEqual(self.responses[1]['error']['code'], -32601)

    def test_diagnostic_failure_and_duplicate_request_do_not_change_denial(self):
        controller = self.controller(dict(self.turn, items=[]))
        controller.client.server_requests.put(self.second())
        controller.client.server_requests.put(self.second())
        original_event = self.store.event
        def fail_diagnostic(db, kind, **fields):
            if kind == 'managed_permission_diagnostic':raise OSError('log failed')
            return original_event(db, kind, **fields)
        with patch.object(self.store, 'event', side_effect=fail_diagnostic):
            controller._permissions()
        self.assertEqual(self.responses, [{'id': 2, 'result': {'decision': 'decline'}}])
        controller._permissions()
        self.assertEqual(len(self.responses), 1)

    def test_read_exception_is_diagnosed_without_grant(self):
        controller = self.controller()
        def broken(*args, **kwargs):raise OSError('Bearer do-not-persist')
        controller.client.read_thread = broken
        controller.client.server_requests.put(self.second())
        controller._permissions()
        self.assertEqual(self.status(), 'granted')
        self.assertEqual(self.responses[0]['result']['decision'], 'decline')
        with self.store.transaction() as db:
            data = db.execute("SELECT data FROM events WHERE kind='managed_permission_diagnostic' ORDER BY seq DESC").fetchone()[0]
        self.assertIn('OSError', data)
        self.assertNotIn('do-not-persist', data)

    def test_running_raw_rollout_matches_exact_official_argv_and_conflict_blocks(self):
        argv = ['C:\\Program Files\\PowerShell\\7\\pwsh.exe', '-Command', 'Get-Content a']
        with self.store.transaction() as db:
            parent = self.store._member(db, 'A')
            proof = json.loads(parent['proof']);proof['cliVersion'] = '0.163.0'
            value = json.dumps(proof)
            db.execute("UPDATE members SET proof=? WHERE id='A'", (value,))
            binding = self.store._option(db, 'managed_binding:' + self.request['id'])
            binding['proof'] = value;self.store._put(db, 'managed_binding:' + self.request['id'], binding)
            params = dict(self.params, command=shlex.join(argv))
            db.execute('UPDATE managed_permissions SET request=? WHERE id=?',
                (json.dumps({'method': 'item/commandExecution/requestApproval', 'params': params}), self.request['id']))
        path = Path(self.home) / 'sessions/receipt.jsonl';path.parent.mkdir(exist_ok=True)
        raw = dict(self.item, type='CommandExecution', command=argv, exit_code=0)
        raw.pop('exitCode')
        rows = [{'type': 'session_meta', 'payload': {'id': self.proof['id'], 'cwd': self.proof['cwd']}},
                {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn-one'}},
                {'type': 'event_msg', 'payload': {'type': 'item_completed', 'thread_id': self.proof['id'], 'turn_id': 'turn-one', 'item': raw}}]
        conflict = copy.deepcopy(rows[-1]);conflict['payload']['item']['exit_code'] = 1
        path.write_text('\n'.join(json.dumps(r) for r in rows + [conflict]) + '\n', encoding='utf-8')
        controller = self.controller(dict(self.turn, items=[]))
        controller.client.read_thread = lambda session: dict(self.proof, path=str(path))
        controller.client.server_requests.put(self.second())
        controller._permissions()
        self.assertEqual(self.status(), 'unknown')
        self.assertEqual(len(self.permissions()), 1)
        self.assertEqual(self.responses[0]['result']['decision'], 'decline')
        with self.store.transaction() as db:
            db.execute("UPDATE managed_permissions SET status='granted' WHERE id=?", (self.request['id'],))
        path.write_text('\n'.join(json.dumps(r) for r in rows) + '\n', encoding='utf-8')
        controller = self.controller(dict(self.turn, items=[]))
        controller.client.read_thread = lambda session: dict(self.proof, path=str(path))
        controller.client.server_requests.put(self.second())
        controller._permissions()
        self.assertEqual(self.status(), 'success')
        self.assertEqual(len(self.permissions()), 2)
        self.assertEqual(self.responses, [])

    def test_wrong_receipt_thread_duplicate_turn_and_changed_reviewer_are_rejected(self):
        for variant in ('thread', 'duplicates', 'reviewer'):
            controller = self.controller()
            if variant == 'thread':
                controller.client.turns = lambda *a, **k: iter([dict(self.turn, threadId='other', status='inProgress')])
            elif variant == 'duplicates':
                controller.client.turns = lambda *a, **k: iter([self.turn, self.turn])
            else:
                controller.client.read_thread = lambda session: dict(self.proof, model='gpt-other')
            controller.client.server_requests.put(self.second())
            controller._permissions()
            self.assertEqual(self.status(), 'granted')
            self.assertEqual(len(self.permissions()), 1)
            self.assertEqual(self.responses[0]['result']['decision'], 'decline')

    def test_duplicate_allowed_request_is_only_answered_once(self):
        controller = self.controller()
        controller.client.server_requests.put(self.second())
        controller._permissions()
        row = self.permissions()[1]
        self.allow({'id': row['id'], 'digest': row['digest']})
        controller.client.server_requests.put(self.second())
        controller._permissions()
        self.assertEqual(self.responses, [{'id': 2, 'result': {'decision': 'accept'}}])
        controller.client.server_requests.put(self.second())
        controller._permissions()
        self.assertEqual(len(self.responses), 1)

    def test_live_rollout_opt_in_keeps_default_terminal_requirement(self):
        helper = exact.RolloutReceiptTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        helper.read(helper.rows[:-1])
        t = runtime.rollout_command_items
        self.assertIsNone(t(helper.member, helper.thread, 'turn'))
        self.assertEqual(len(t(helper.member, helper.thread, 'turn', require_terminal=False)), 1)
        helper.read(helper.rows[:-1], partial=True)
        self.assertIsNone(t(helper.member, helper.thread, 'turn', require_terminal=False))
        rows = copy.deepcopy(helper.rows[:-1])
        rows.append({'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'other'}})
        helper.read(rows)
        self.assertIsNone(t(helper.member, helper.thread, 'turn', require_terminal=False))

if __name__ == '__main__':
    unittest.main()
