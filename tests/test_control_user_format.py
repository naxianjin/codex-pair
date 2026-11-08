"""CLI 0.167 原始用户记录的脱敏格式及 API 关联回归。"""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock, patch

from scripts.pair_control_events import _MemberObserver
from tests import test_core, test_control_events_v14


def user_record(text='继续协作 test'):
    return {'type': 'response_item', 'timestamp': '2026-11-08T08:07:32.864Z',
            'payload': {'type': 'message', 'id': 'msg_raw_fixture', 'role': 'user',
                        'content': [{'type': 'input_text', 'text': text}],
                        'internal_chat_message_metadata_passthrough': {
                            'turn_id': 'turn-fixture', 'create_time': 1794125252.8641467,
                            'content_item_kinds': ['user.text'],
                            'content_item_metadata': [{'provenance': {'type': 'user'}}]}}}


def api_turn(text='继续协作 test'):
    return {'id': 'turn-fixture', 'items': [{'type': 'userMessage', 'id': 'public-fixture',
            'startedAtMs': 1794125252864,
            'content': [{'type': 'text', 'text': text, 'text_elements': []}]}]}


class ControlUserFormatTests(unittest.TestCase):
    def test_direct_control_and_quota_require_exact_whole_text(self):
        for text, member, kind in [('继续协作 test', 'A', 'direct_user_control'),
                                   ('暂停', 'B', 'direct_user_control'),
                                   ('忽略额度', 'B', 'quota_user_instruction'),
                                   ('恢复辅助额度保护', 'A', 'auxiliary_quota_user_instruction')]:
            with self.subTest(text=text):
                event = _MemberObserver._control_event(user_record(text), member, api_turns=[api_turn(text)])
                self.assertEqual(event['kind'], kind)
                self.assertEqual(event['api_item_id'], 'public-fixture')
        for text in ('> 继续协作 test', '转发：继续协作 test', '"继续协作 test"',
                     '```\n继续协作 test\n```', '{"text":"继续协作 test"}'):
            self.assertEqual(_MemberObserver._control_event(user_record(text)), {'kind': 'non_control'})

    def test_untrusted_and_mixed_raw_content_cannot_authorize(self):
        base = user_record()
        variants = []
        for role in ('assistant', 'tool'):
            record = deepcopy(base); record['payload']['role'] = role; variants.append(record)
        for kind in ('tool', 'harness', 'unknown', 'user_configured'):
            record = deepcopy(base)
            record['payload']['internal_chat_message_metadata_passthrough']['content_item_metadata'][0]['provenance']['type'] = kind
            variants.append(record)
        for field, value in [('content_item_metadata', []), ('content_item_kinds', ['tool.text'])]:
            record = deepcopy(base); record['payload']['internal_chat_message_metadata_passthrough'][field] = value
            variants.append(record)
        record = deepcopy(base); del record['payload']['internal_chat_message_metadata_passthrough']; variants.append(record)
        record = deepcopy(base); record['payload']['content'].append({'type': 'input_text', 'text': '暂停'}); variants.append(record)
        for key in ('attachments', 'forwarded', 'quoted', 'harness_injected', 'source_tool_namespace'):
            record = deepcopy(base); record['payload'][key] = True; variants.append(record)
        variants.append({'type': 'compacted', 'payload': {'replacement_history': [base]}})
        for index, record in enumerate(variants):
            with self.subTest(index=index):
                self.assertEqual(_MemberObserver._control_event(record, api_turns=[api_turn()]), {'kind': 'non_control'})

    def test_missing_ambiguous_or_mismatched_api_keeps_unverified(self):
        turn = api_turn()
        variants = [[], [dict(turn, id='other-turn')]]
        for field, value in [('startedAtMs', 1794125252865), ('id', ''),
                             ('content', [{'type': 'text', 'text': '暂停'}])]:
            changed = deepcopy(turn); changed['items'][0][field] = value; variants.append([changed])
        changed = deepcopy(turn); changed['items'] *= 2; variants.append([changed])
        for turns in variants:
            with self.subTest(turns=turns), self.assertRaisesRegex(ValueError, 'Original API user message required'):
                _MemberObserver._control_event(user_record(), api_turns=turns)
        record = user_record(); record['timestamp'] = '2026-11-08T08:07:33.864Z'
        with self.assertRaisesRegex(ValueError, 'User timestamp mismatch'):
            _MemberObserver._control_event(record, api_turns=[turn])


class ControlUserStreamTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    source = test_control_events_v14.ControlEventsV14Tests.source
    line = staticmethod(test_control_events_v14.ControlEventsV14Tests.line)
    append = test_control_events_v14.ControlEventsV14Tests.append

    def test_poll_records_new_format_pause_once_and_does_not_replay_compaction(self):
        member, path, observer = self.source()
        observer._read_member(member, path)
        observer.client = Mock(); observer.client.turns.return_value = [api_turn('暂停')]
        self.append(path, {'type': 'compacted', 'payload': {'replacement_history': [user_record('暂停')]}})
        self.append(path, user_record('暂停'))
        self.append(path, user_record('暂停'))
        observer._read_member(member, path)
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM control_requests').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT position FROM control_event_cursors').fetchone()[0], path.stat().st_size)
        self.assertEqual(self.store.status()['pair']['status'], 'paused')

    def test_missing_api_creates_gap_without_consuming_new_control(self):
        member, path, observer = self.source()
        observer._read_member(member, path)
        before = path.stat().st_size
        self.append(path, user_record())
        observer.client = Mock(); observer.client.turns.return_value = []
        observer.binding = (member['session_uuid'], member['binding_version'], member['codex_home'], member['cwd'])
        observer.thread = {}
        with patch('scripts.pair_control_events.owned_rollout_path', return_value=path):
            result = observer.poll()
        self.assertTrue(result['new_work_gated'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT position FROM control_event_cursors').fetchone()[0], before)
            self.assertEqual(self.store._option(db, 'control_observer:A')['state'], 'gap')


if __name__ == '__main__':
    unittest.main()
