"""当前执行的只读回执入口及精确shell格式回归。"""
import json
from pathlib import Path
import shlex
import tempfile
import unittest
import sqlite3
from contextlib import closing

from scripts.pair_review_runtime import ReviewerController
from scripts import pair_review_receipts as access


class ReviewReceiptAccessTests(unittest.TestCase):
    def test_explicit_conflict_wins_in_both_orders(self):
        success = {'id': 'same-item', 'type': 'commandExecution', 'status': 'completed',
                   'exitCode': 0, 'command': 'python -m unittest', 'cwd': str(self.root)}
        conflict = {'id': 'same-item', 'type': 'conflictingReceipt'}
        for items in ([success, conflict], [conflict, success], [success, conflict, success], [conflict, success, conflict]):
            with self.subTest(items=items):
                self.assertEqual(access.receipt_items(items, '0.163.0'), [])

    def rollout(self, items):
        sessions = self.root / 'sessions'
        sessions.mkdir(exist_ok=True)
        path = sessions / 'current.jsonl'
        self.proof['path'] = str(path)
        events = [
            {'type': 'session_meta', 'payload': {'id': self.binding['session'], 'cwd': str(self.root)}},
            {'type': 'event_msg', 'payload': {'type': 'task_started', 'turn_id': 'turn-one'}},
        ]
        for item in items:
            events.append({'type': 'event_msg', 'payload': {'type': 'item_completed',
                'thread_id': self.binding['session'], 'turn_id': 'turn-one', 'item': item}})
        path.write_text(''.join(json.dumps(event) + '\n' for event in events), encoding='utf-8')
        return access.rollout_command_items({'codex_home': str(self.root), 'cwd': str(self.root),
            'session_uuid': self.binding['session']}, self.proof, 'turn-one',
            producer_version='0.163.0', require_terminal=False)

    def final_verdict(self):
        official, version = access.verified_turn(self.directory, self.binding, observer_factory=self.observer)
        controller = ReviewerController(None)
        controller.state = {'cwd': str(self.root), 'producer_version': version}
        stage = {'payload': json.dumps({'coordinator_required_checks': [{'name': 'unit', 'command': self.item['command']}]})}
        report = {'verdict': 'passed', 'checks': [{'name': 'unit', 'status': 'passed', 'receipt_id': self.item['id']}]}
        return controller._report(dict(official, status='completed', items=official['items'] + [
            {'type': 'agentMessage', 'text': json.dumps(report)}]), stage)['verdict']

    def test_real_rollout_conflict_overrides_api_success_for_query_and_final_gate(self):
        fallback = self.rollout([self.item, dict(self.item, exitCode=1)])
        self.assertEqual(fallback, [{'id': self.item['id'], 'type': 'conflictingReceipt'}])
        for items in ([self.item] + fallback, fallback + [self.item]):
            self.assertEqual(access.receipt_items(items, '0.163.0'), [])
        self.assertEqual(self.read()['receipts'], [])
        self.assertEqual(self.final_verdict(), 'needs_validation')

    def test_api_conflict_overrides_normal_rollout_for_query_and_final_gate(self):
        self.assertEqual(len(self.rollout([self.item])), 1)
        self.turn['items'] = [{'id': self.item['id'], 'type': 'conflictingReceipt'}]
        self.assertEqual(self.read()['receipts'], [])
        self.assertEqual(self.final_verdict(), 'needs_validation')

    def test_consistent_api_and_real_rollout_still_pass_both_paths(self):
        self.rollout([self.item, dict(self.item)])
        self.assertEqual([item['id'] for item in self.read()['receipts']], [self.item['id']])
        self.assertEqual(self.final_verdict(), 'passed')

    def test_missing_id_repeated_conflict_unfinished_and_nonzero_cannot_pass(self):
        for malformed in (dict(self.item, id=''), dict(self.item, id=None), {'type': 'conflictingReceipt'}):
            self.turn['items'] = [self.item, malformed]
            self.assertEqual(self.read()['receipts'], [])
            self.assertEqual(self.final_verdict(), 'needs_validation')
        for change in ({'status': 'inProgress'}, {'exitCode': 1}, {'command': 'different'}):
            self.turn['items'] = [self.item, dict(self.item, **change)]
            self.assertEqual(self.read()['receipts'], [])
            self.assertEqual(self.final_verdict(), 'needs_validation')
        self.turn['items'] = [dict(self.item, exitCode=1)]
        self.assertEqual(self.final_verdict(), 'needs_validation')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / '.codex-pair/state/pair-test'
        self.directory.mkdir(parents=True)
        self.path = self.directory / 'state.sqlite3'
        self.binding = {'pair': 'pair-test', 'stage': 'stage-one', 'digest': 'a'*64, 'epoch': 1,
                        'session': '00000000-0000-0000-0000-000000000099', 'execution': 'exec-one'}
        self.proof = {'id': self.binding['session'], 'cwd': str(self.root), 'modelProvider': 'openai', 'model': 'gpt-6.1-sol', 'ephemeral': False}
        self.item = {'type': 'commandExecution', 'id': 'item-one', 'command': 'python -m unittest', 'cwd': str(self.root), 'status': 'completed', 'exitCode': 0}
        self.turn = {'id': 'turn-one', 'status': 'inProgress', 'items': [self.item]}
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript('''
                CREATE TABLE pair_settings(key TEXT PRIMARY KEY,value);
                CREATE TABLE collaborations(id,project,status);
                CREATE TABLE tasks(id,status,requirements_version);
                CREATE TABLE review_stages(id,digest,status,turn_id,reviewer_epoch,reviewer_session,execution_id,requirements_version,payload,task_id);
                CREATE TABLE reviewer_runtime(state,epoch,session_uuid,execution_id,turn_id,cwd,codex_home,parent_session,owner,model);
                CREATE TABLE members(id,session_uuid,binding_version,proof,lifecycle,verified,online,cwd,codex_home);
            ''')
            db.execute('INSERT INTO collaborations VALUES(?,?,?)', ('pair-test', str(self.root), 'running'))
            db.execute('INSERT INTO tasks VALUES(?,?,?)', ('task-one', 'review_wait', 1))
            db.execute('INSERT INTO review_stages VALUES(?,?,?,?,?,?,?,?,?,?)', ('stage-one', 'a'*64, 'running', 'turn-one', 1, self.binding['session'], 'exec-one', 1, '{}', 'task-one'))
            db.execute('INSERT INTO reviewer_runtime VALUES(?,?,?,?,?,?,?,?,?,?)', ('running', 1, self.binding['session'], 'exec-one', None, str(self.root), str(self.root), 'parent', 'owner', 'gpt-6.1-sol'))
            parent = {'modelProvider': 'openai', 'model': 'gpt-6.1-sol', 'cliVersion': '0.163.0'}
            db.execute('INSERT INTO members VALUES(?,?,?,?,?,?,?,?,?)', ('A', 'parent', 1, json.dumps(parent), 'active', 1, 1, str(self.root), str(self.root)))
            db.commit()
        from tests.producer_fixture import install
        install(self,self.path)

    def observer(self, **kwargs):
        owner = self
        class Observer:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read_thread(self, session): return owner.proof
            def turns(self, session, *, items_view):
                owner.assertEqual(items_view, 'full')
                return iter([owner.turn])
        return Observer()

    def read(self, **kwargs):
        return access.read_receipts(self.directory, dict(self.binding, **kwargs), observer_factory=self.observer)

    def test_current_completed_receipt_is_visible_and_database_unchanged(self):
        before = self.path.read_bytes()
        result = self.read()
        self.assertEqual(result['turn'], 'turn-one')
        self.assertEqual(result['receipts'][0]['id'], 'item-one')
        self.assertEqual(self.path.read_bytes(), before)

    def test_wrong_identity_execution_digest_epoch_or_inactive_binding_rejected(self):
        for key, value in [('session', 'other'), ('execution', 'old'), ('digest', 'old'), ('epoch', 2), ('pair', 'other')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.read(**{key: value})
        self.update("UPDATE reviewer_runtime SET state='idle'")
        with self.assertRaises(ValueError): self.read()

    def update(self, sql):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute(sql)
            db.commit()

    def test_conflicting_unfinished_missing_and_secret_receipts_not_exposed(self):
        self.turn['items'] = [self.item, dict(self.item, exitCode=1), dict(self.item, id='pending', status='inProgress'), dict(self.item, id='secret', command='echo sk-private-key')]
        self.assertEqual(self.read()['receipts'], [])
        self.turn['items'] = []
        self.assertEqual(self.read()['receipts'], [])

    def test_wrong_official_thread_turn_and_late_result_rejected(self):
        self.proof['id'] = '00000000-0000-0000-0000-000000000098'
        with self.assertRaises(ValueError): self.read()
        self.proof['id'] = self.binding['session']
        self.turn['id'] = 'old-turn'
        with self.assertRaises(ValueError): self.read()

    def test_binding_change_during_official_read_rejected(self):
        original = self.observer
        def observer(**kwargs):
            instance = original(**kwargs)
            turns = instance.turns
            def changed(*args, **options):
                self.update('UPDATE members SET binding_version=2')
                return turns(*args, **options)
            instance.turns = changed
            return instance
        with self.assertRaises(ValueError):
            access.read_receipts(self.directory, self.binding, observer_factory=observer)

    def test_state_file_escape_rejected(self):
        moved = self.root / 'state.sqlite3'
        moved.write_bytes(self.path.read_bytes())
        with self.assertRaises(ValueError):
            access.read_receipts(self.root, self.binding, observer_factory=self.observer)

    def test_registered_exact_shell_argv_passes_without_unwrapping(self):
        with tempfile.TemporaryDirectory() as folder:
            controller = ReviewerController(None)
            controller.state = {'cwd': folder, 'producer_version': '0.163.0'}
            argv = ['C:/Program Files/PowerShell/7/pwsh.exe', '-Command', 'D:/Anaconda3/python.exe -B -m unittest -q']
            stage = {'payload': json.dumps({'coordinator_required_checks': [{'name': 'unit', 'command': shlex.join(argv)}]})}
            item = {'id': 'exec-one', 'type': 'CommandExecution', 'command': argv, 'cwd': folder, 'status': 'completed', 'exit_code': 0}
            report = {'verdict': 'passed', 'checks': [{'name': 'unit', 'status': 'passed', 'receipt_id': 'exec-one'}]}
            turn = {'status': 'completed', 'items': [item, {'type': 'agentMessage', 'text': json.dumps(report)}]}
            self.assertEqual(controller._report(turn, stage)['verdict'], 'passed')
            stage['payload'] = json.dumps({'coordinator_required_checks': [{'name': 'unit', 'command': argv[-1]}]})
            self.assertEqual(controller._report(turn, stage)['verdict'], 'needs_validation')
            stage['payload'] = json.dumps({'coordinator_required_checks': [{'name': 'unit', 'command': shlex.join(argv)}]})
            controller.state['producer_version'] = 'unknown'
            self.assertEqual(controller._report(turn, stage)['verdict'], 'needs_validation')


    def tool(self, arguments=None, **changes):
        params = dict(tool=access.RECEIPTS_TOOL['name'], arguments={} if arguments is None else arguments,
                      threadId=self.binding['session'], turnId='turn-one')
        params.update(changes)
        return access.handle_tool(self.directory, self.binding, params, observer_factory=self.observer)

    def test_dynamic_query_reads_completed_items_while_turn_is_in_progress(self):
        before = self.path.read_bytes()
        # Observer exposes only metadata; no wait_turn or tool response API exists.
        response = self.tool()
        self.assertTrue(response['success'])
        result = json.loads(response['contentItems'][0]['text'])
        self.assertEqual(result['receipts'][0]['id'], 'item-one')
        self.assertEqual(self.turn['status'], 'inProgress')
        self.assertEqual(self.path.read_bytes(), before)

    def test_dynamic_query_rejects_model_selected_sources_and_wrong_turn(self):
        for arguments in ({'session': self.binding['session']}, {'path': str(self.path)}, 'new source', [], False):
            self.assertFalse(self.tool(arguments)['success'])
        for changes in ({'threadId': 'other'}, {'turnId': 'other'}, {'tool': 'other'}):
            self.assertFalse(self.tool(**changes)['success'])
        self.assertFalse(self.tool(arguments='{}')['success'])

    def test_dynamic_query_rejects_total_overflow(self):
        self.turn['items'] = [dict(self.item, id=str(i), command='x'*9000) for i in range(8)]
        self.assertFalse(self.tool()['success'])

    def test_dynamic_query_does_not_leak_observer_errors_or_change_database(self):
        before = self.path.read_bytes()
        def denied(**kwargs):
            raise PermissionError('sk-private-key confidential account response')
        params = dict(tool=access.RECEIPTS_TOOL['name'], arguments={},
                      threadId=self.binding['session'], turnId='turn-one')
        result = access.handle_tool(self.directory, self.binding, params, observer_factory=denied)
        self.assertFalse(result['success'])
        self.assertNotIn('sk-private', json.dumps(result))
        self.assertNotIn('confidential', json.dumps(result))
        self.assertEqual(before, self.path.read_bytes())

    def test_dynamic_query_rejects_changed_binding_and_item_overflow(self):
        original = self.observer
        def observer(**kwargs):
            instance = original(**kwargs)
            read = instance.read_thread
            def changed(*args):
                self.update('UPDATE members SET binding_version=2')
                return read(*args)
            instance.read_thread = changed
            return instance
        params = dict(tool=access.RECEIPTS_TOOL['name'], arguments={},
                      threadId=self.binding['session'], turnId='turn-one')
        self.assertFalse(access.handle_tool(self.directory, self.binding, params, observer_factory=observer)['success'])
        self.update('UPDATE members SET binding_version=1')
        self.turn['items'] = [dict(self.item, command='x'*17000)]
        self.assertFalse(self.tool()['success'])
        self.turn['items'] = [dict(self.item, id=str(i)) for i in range(129)]
        self.assertFalse(self.tool()['success'])

    def test_dynamic_query_conflicts_and_failed_items_do_not_grant_approval(self):
        self.turn['items'] = [self.item, dict(self.item, exitCode=1)]
        self.assertEqual(json.loads(self.tool()['contentItems'][0]['text'])['receipts'], [])
        self.assertEqual(self.final_verdict(), 'needs_validation')

    def test_controller_dispatches_query_without_approval_or_wait_turn(self):
        import queue
        from unittest.mock import patch
        from types import SimpleNamespace
        controller = ReviewerController(None)
        controller.store = SimpleNamespace(directory=self.directory)
        controller.state = dict(session=self.binding['session'], turn_id='turn-one',
                                receipt_binding=dict(self.binding))
        params = dict(tool=access.RECEIPTS_TOOL['name'], arguments={},
                      threadId=self.binding['session'], turnId='turn-one')
        requests = queue.Queue()
        requests.put({'id': 'query-one', 'method': 'item/tool/call', 'params': params})
        writes = []
        controller.client = SimpleNamespace(server_requests=requests, _write=writes.append)
        controller.pending_permissions = {}
        handler = access.handle_tool
        with patch.object(access, 'handle_tool', side_effect=lambda directory, binding, params:
                          handler(directory, binding, params, observer_factory=self.observer)):
            controller._permissions()
        self.assertEqual(len(writes), 1)
        self.assertTrue(writes[0]['result']['success'])
        self.assertEqual(controller.pending_permissions, {})

    def test_prompt_uses_fixed_dynamic_tool_instead_of_script_lookup(self):
        from types import SimpleNamespace
        controller = ReviewerController(SimpleNamespace(pair_id='pair-test', setting=lambda key: None))
        controller.state = dict(epoch=1, session=self.binding['session'], execution_id='exec-one')
        stage = dict(id='stage-one', digest='a'*64, requirements_version=1,
                     artifact='{}', contributors='[]', checks='[]', payload='{}')
        prompt = controller._prompt(stage)
        self.assertIn('pair_review_receipts', prompt)
        self.assertNotIn('pair_review_receipts.py', prompt)
        self.assertNotIn('--state-directory', prompt)


class RealRequirementReceiptTests(unittest.TestCase):
    """使用真实Store schema和正式需求变更入口，不给生产表虚构字段。"""
    def setUp(self):
        from tests.test_final_receipt_settlement import FinalSettlementTests
        self.fixture = FinalSettlementTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        f = self.fixture
        self.binding = dict(pair=f.store.pair_id, stage=f.stage['id'], digest=f.stage['digest'],
                            epoch=f.ticket['epoch'], session=f.proof['id'], execution=f.lease['execution_id'])
        self.params = dict(tool=access.RECEIPTS_TOOL['name'], arguments={},
                           threadId=f.proof['id'], turnId='turn-one')
        f.full['status'] = 'inProgress'
        f.full.pop('completedAt', None)

    def query(self, observer=None):
        f = self.fixture
        return access.handle_tool(f.store.directory, self.binding, self.params,
                                  observer_factory=observer or f.observer)

    def test_affected_revision_rejects_current_query(self):
        f = self.fixture
        self.assertTrue(self.query()['success'])
        change = f.store.revise_requirements(*f.actor(), 'changed API requirement', ['api'])
        self.assertEqual(change['affected'], ['api'])
        self.assertFalse(self.query()['success'])
        self.assertEqual(f.store.task('api')['status'], 'requirements_wait')

    def test_unrelated_revision_allows_original_task_to_continue(self):
        f = self.fixture
        f.store.assign('unrelated', 'other task', 'A', ['other.txt'])
        change = f.store.revise_requirements(*f.actor(), 'only unrelated task changes', ['unrelated'])
        self.assertEqual(change['version'], 2)
        self.assertEqual(f.store.task('api')['requirements_version'], 1)
        before = Path(f.store.path).read_bytes()
        self.assertTrue(self.query()['success'])
        self.assertEqual(before, Path(f.store.path).read_bytes())

    def test_affected_revision_during_observer_read_discards_result(self):
        f = self.fixture
        original = f.observer
        def observer(**kwargs):
            instance = original(**kwargs)
            read = instance.read_thread
            def changed(*args):
                f.store.revise_requirements(*f.actor(), 'requirement changed during lookup', ['api'])
                return read(*args)
            instance.read_thread = changed
            return instance
        self.assertFalse(self.query(observer)['success'])

    def test_real_task_stage_version_mismatch_is_rejected_even_if_stage_running(self):
        f = self.fixture
        with f.store.transaction() as db:
            db.execute("UPDATE tasks SET requirements_version=requirements_version+1 WHERE id='api'")
        self.assertFalse(self.query()['success'])


if __name__ == '__main__':
    unittest.main()
