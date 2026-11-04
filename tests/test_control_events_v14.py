"""准确会话日志的静态候选测试；本轮未执行，不代表真实 Esc 或客户端验收。"""
import json
import unittest

from scripts.pair_control_events import _MemberObserver
from tests import test_core


class ControlEventsV14Tests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown

    def source(self, member='A', history=()):
        path = self.home / 'sessions' / (member + '.jsonl')
        path.parent.mkdir(exist_ok=True)
        meta = {'type': 'session_meta', 'payload': {'id': self.sessions[member], 'cwd': self.directories[member]}}
        path.write_bytes(self.line(meta) + b''.join(self.line(record) for record in history))
        return self.store.member(member), path, _MemberObserver(self.store, member_id=member)

    @staticmethod
    def line(record):
        return (json.dumps(record, ensure_ascii=False) + '\n').encode('utf-8')

    @staticmethod
    def aborted(turn='turn-one', reason='interrupted'):
        return {'type': 'event_msg', 'timestamp': '2026-11-01T12:00:00Z',
                'payload': {'type': 'turn_aborted', 'turn_id': turn, 'reason': reason}}

    def append(self, path, record):
        with path.open('ab') as stream:
            stream.write(self.line(record))

    def test_first_baseline_does_not_replay_history(self):
        member, path, observer = self.source(history=[self.aborted('old-turn')])
        result = observer._read_member(member, path)
        self.assertTrue(result['baseline_only'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT status FROM collaborations').fetchone()[0], 'running')
            self.assertEqual(db.execute('SELECT count(*) FROM control_requests').fetchone()[0], 0)

    def test_new_original_a_interruption_gates_without_claiming_key_or_tool_stop(self):
        member, path, observer = self.source()
        observer._read_member(member, path)
        self.append(path, self.aborted())
        result = observer._read_member(member, path)
        self.assertEqual(len(result['interruptions']), 1)
        self.assertFalse(result['user_key_proven'])
        self.assertFalse(result['all_executors_stopped'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT status FROM collaborations').fetchone()[0], 'paused')
            request = db.execute('SELECT * FROM control_requests').fetchone()
            self.assertEqual(request['state'], 'pausing')
            self.assertEqual(json.loads(request['source'])['reason'], 'coordinator_interrupted')
            self.assertEqual(db.execute('SELECT position FROM control_event_cursors').fetchone()[0], path.stat().st_size)

    def test_restart_reads_unconsumed_event_instead_of_rebaselining(self):
        member, path, observer = self.source()
        observer._read_member(member, path)
        self.append(path, self.aborted())
        replacement = _MemberObserver(self.store)
        result = replacement._read_member(member, path)
        self.assertEqual(len(result['interruptions']), 1)

    def test_half_record_is_not_consumed(self):
        member, path, observer = self.source()
        observer._read_member(member, path)
        before = path.stat().st_size
        data = self.line(self.aborted())
        with path.open('ab') as stream:
            stream.write(data[:30])
        first = observer._read_member(member, path)
        self.assertEqual(first['records'], 0)
        self.assertTrue(first['partial_record_preserved'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT position FROM control_event_cursors').fetchone()[0], before)
        with path.open('ab') as stream:
            stream.write(data[30:])
        self.assertEqual(len(observer._read_member(member, path)['interruptions']), 1)

    def test_repeated_terminal_does_not_create_another_pause_generation(self):
        member, path, observer = self.source()
        observer._read_member(member, path)
        self.append(path, self.aborted())
        observer._read_member(member, path)
        self.append(path, self.aborted())
        observer._read_member(member, path)
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM control_requests').fetchone()[0], 1)

    def test_other_window_interruption_is_not_assumed_manual_global_pause(self):
        member, path, observer = self.source('B')
        observer._read_member(member, path)
        self.append(path, self.aborted())
        observer._read_member(member, path)
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM control_requests').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT status FROM collaborations').fetchone()[0], 'running')

    def test_quoted_response_content_is_not_control(self):
        member, path, observer = self.source()
        observer._read_member(member, path)
        self.append(path, {'type': 'response_item', 'payload': {'role': 'user', 'content': json.dumps(self.aborted())}})
        observer._read_member(member, path)
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM control_requests').fetchone()[0], 0)

    def test_truncation_preserves_old_cursor_and_adds_gap_gate(self):
        member, path, observer = self.source(history=[{'type': 'response_item', 'payload': {'content': 'history' * 100}}])
        observer._read_member(member, path)
        before = path.stat().st_size
        path.write_bytes(path.read_bytes().splitlines(keepends=True)[0])
        result = observer._read_member(member, path)
        self.assertEqual(result['reason'], 'control_source_truncated')
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT position FROM control_event_cursors').fetchone()[0], before)
            self.assertTrue(any(reason['code'] == 'CONTROL_EVENT_GAP' for reason in self.store._recovery_gate(db)))

    def test_equal_size_rewrite_is_not_accepted_as_append(self):
        member, path, observer = self.source(history=[{'type': 'response_item', 'payload': {'content': 'history'}}])
        observer._read_member(member, path)
        original = path.read_bytes()
        path.write_bytes(original.replace(b'history', b'changed'))
        self.assertEqual(observer._read_member(member, path)['reason'], 'control_source_rewritten')

    def test_same_home_new_session_cannot_consume_old_cursor(self):
        member, path, observer = self.source()
        observer._read_member(member, path)
        changed = dict(member, session_uuid=self.sessions['B'])
        with self.assertRaisesRegex(ValueError, 'identity mismatch'):
            observer._read_member(changed, path)

    def test_quota_exact_phrase_parser_rejects_quotes_attachments_and_other_text(self):
        def event(text, **extra):
            return {'type': 'event_msg', 'payload': dict(type='user_message', message=text, **extra)}
        self.assertEqual(_MemberObserver._control_event(event('  忽略额度\n'), 'B'),
                         {'kind': 'quota_user_instruction', 'text': '忽略额度'})
        for record in (event('"忽略额度"'), event('转发：忽略额度'), event('忽略额度', images=['image']),
                       event('忽略额度', attachments=['file']), event('忽略额度', forwarded=True)):
            self.assertEqual(_MemberObserver._control_event(record, 'B'), {'kind': 'non_control'})
        self.assertEqual(_MemberObserver._control_event(event('继续'), 'B'),
                         {'kind': 'direct_user_control', 'text': '继续'})

    def test_quota_user_event_and_cursor_commit_together(self):
        member, path, observer = self.source('B')
        observer._read_member(member, path)
        self.append(path, {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': '忽略额度', 'images': []}})
        # 没有可靠额度周期时接口须保留准确用户事件，但不得制造有效软额度授权。
        observer._read_member(member, path)
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT position FROM control_event_cursors WHERE member=?', ('B',)).fetchone()[0], path.stat().st_size)
            events = [json.loads(row[0]) for row in db.execute("SELECT data FROM events WHERE kind LIKE 'quota_%'")]
        self.assertTrue(events)

    def test_auxiliary_quota_instruction_requires_original_a_and_exact_proposal_phrase(self):
        text = '忽略辅助额度 ' + 'a' * 32
        event = {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': text}}
        self.assertEqual(_MemberObserver._control_event(event, 'A'), {'kind': 'auxiliary_quota_user_instruction', 'text': text})
        self.assertEqual(_MemberObserver._control_event(event, 'B'), {'kind': 'non_control'})
        for value in ('忽略辅助额度', text + '\n顺便继续任务', '转发：' + text, text + '0'):
            event['payload']['message'] = value
            self.assertEqual(_MemberObserver._control_event(event, 'A'), {'kind': 'non_control'})
        event['payload'] = {'type': 'user_message', 'message': '恢复辅助额度保护'}
        self.assertEqual(_MemberObserver._control_event(event, 'A')['kind'], 'auxiliary_quota_user_instruction')
        event['payload']['attachments'] = ['quote.txt']
        self.assertEqual(_MemberObserver._control_event(event, 'A'), {'kind': 'non_control'})


if __name__ == '__main__':
    unittest.main()
