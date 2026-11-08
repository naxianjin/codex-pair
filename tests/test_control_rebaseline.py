"""恢复来源按 API 回合顺序和原日志中的最新控制指令核对。"""
from contextlib import contextmanager
import json
from unittest.mock import Mock, patch
import unittest

from scripts.pair_core import PairError
from tests import test_core
from tests.test_control_user_format import user_record, api_turn
from scripts.pair_control_events import _MemberObserver, MAX_RECORD_BYTES


class ControlRebaselineTests(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown

    def prepare(self, texts):
        self.instruction = '继续协作 test'
        self.store.request_pause('A', self.sessions['A'], self.directories['A'],
                                 reason='用户暂停', idempotency_key='rebaseline-pause')
        self.prior = {'state': 'gap', 'observed_at': 1}
        self.store.setting('control_observer:A', self.prior)
        self.store.setting('cli_trusted_executable:A', {'file': {'sha256': 'fixture'}})
        self.path = self.root / 'source.jsonl'
        records = [{'type': 'session_meta', 'payload': {'id': self.sessions['A'], 'cwd': self.directories['A']}}]
        records += [{'type': 'event_msg', 'timestamp': '2026-11-08T12:00:00Z',
                     'payload': {'type': 'user_message', 'message': text}} for text in texts]
        self.path.write_bytes(b''.join((json.dumps(r) + '\n').encode() for r in records))

    @staticmethod
    def turn(*texts):
        return {'items': [{'type': 'userMessage', 'content': [{'type': 'text', 'text': text}]}
                          for text in texts]}

    @contextmanager
    def api(self, *turns):
        client = Mock()
        client.turns.return_value = turns
        with patch('scripts.pair_control_runtime.AppServer') as server, \
                patch('scripts.pair_producer.file_identity', return_value={'sha256': 'fixture'}), \
                patch('scripts.pair_transport.owned_rollout_path', return_value=self.path):
            server.return_value.__enter__.return_value = client
            yield

    def rebaseline(self):
        return self.store.rebaseline_control_source('A', self.sessions['A'], self.directories['A'],
                                                    'fixture.exe', instruction=self.instruction)

    def test_newest_control_survives_unrelated_steering_and_keeps_pause(self):
        self.prepare(['暂停', '继续协作 test', '原审核窗口为 C'])
        with self.api(self.turn(self.instruction, '原审核窗口为 C'), self.turn('暂停')):
            result = self.rebaseline()
        self.assertTrue(result['rebaselined'])
        self.assertFalse(result['resumed'])
        self.assertEqual(self.store.status()['pair']['status'], 'paused')
        self.assertEqual(self.store.setting('user_control_latest:A')['instruction'], self.instruction)

    def test_latest_pause_in_newest_api_turn_blocks_old_resume(self):
        self.prepare(['继续协作 test', '暂停'])
        with self.api(self.turn('暂停'), self.turn(self.instruction)), \
                self.assertRaisesRegex(PairError, 'Original user message unavailable'):
            self.rebaseline()
        self.assertEqual(self.store.setting('control_observer:A'), self.prior)

    def test_later_pause_in_same_api_turn_blocks_resume(self):
        self.prepare(['继续协作 test', '暂停'])
        with self.api(self.turn(self.instruction, '暂停', '说明')), \
                self.assertRaisesRegex(PairError, 'Original user message unavailable'):
            self.rebaseline()

    def test_later_original_log_pause_blocks_stale_api_resume(self):
        self.prepare(['继续协作 test', '暂停'])
        with self.api(self.turn(self.instruction)), \
                self.assertRaisesRegex(PairError, 'New original user event required'):
            self.rebaseline()
        self.assertEqual(self.store.setting('control_observer:A'), self.prior)

    def test_api_only_or_pre_gap_resume_never_creates_source(self):
        for texts, observed in [(['无控制指令'], 1), (['继续协作 test'], 9999999999)]:
            with self.subTest(texts=texts, observed=observed):
                self.prepare(texts)
                self.prior['observed_at'] = observed
                self.store.setting('control_observer:A', self.prior)
                with self.api(self.turn(self.instruction)), \
                        self.assertRaisesRegex(PairError, 'New original user event required'):
                    self.rebaseline()
                self.assertEqual(self.store.setting('control_observer:A'), self.prior)

    def test_new_format_before_last_four_mb_rebaselines_without_resuming(self):
        self.prepare([])
        with self.path.open('ab') as stream:
            stream.write((json.dumps(user_record()) + '\n').encode())
            for _ in range(6):
                stream.write((json.dumps({'type': 'response_item', 'payload': {'type': 'message',
                    'role': 'assistant', 'content': 'x' * (MAX_RECORD_BYTES // 4)}}) + '\n').encode())
        with self.api(api_turn()):
            result = self.rebaseline()
        self.assertTrue(result['rebaselined'])
        self.assertFalse(result['resumed'])
        self.assertEqual(self.store.status()['pair']['status'], 'paused')

    def test_new_format_mismatched_api_keeps_gap(self):
        self.prepare([])
        with self.path.open('ab') as stream:
            stream.write((json.dumps(user_record()) + '\n').encode())
        turn = api_turn(); turn['items'][0]['startedAtMs'] += 1
        with self.api(turn), self.assertRaisesRegex(ValueError, 'Original API user message required'):
            self.rebaseline()
        self.assertEqual(self.store.setting('control_observer:A'), self.prior)

    def test_same_source_anchor_and_complete_suffix_are_required(self):
        self.prepare([])
        from scripts.pair_recovery import digest
        from scripts.pair_transport import canonical_path
        stat = self.path.stat()
        self.prior.update(source_identity=digest({'path': canonical_path(self.path),
            'device': stat.st_dev, 'inode': stat.st_ino}), position=stat.st_size, anchor='wrong')
        self.store.setting('control_observer:A', self.prior)
        with self.api(api_turn()), self.assertRaisesRegex(PairError, 'Original control boundary changed'):
            self.rebaseline()
        with self.path.open('rb') as stream:
            self.prior['anchor'] = _MemberObserver._anchor(stream, stat.st_size)
        self.store.setting('control_observer:A', self.prior)
        with self.path.open('ab') as stream:
            stream.write((json.dumps(user_record()) + '\n').encode())
            stream.write(b'{"type":')
        with self.api(api_turn()), self.assertRaisesRegex(PairError, 'Incomplete original control record'):
            self.rebaseline()
        self.assertEqual(self.store.setting('control_observer:A'), self.prior)


if __name__ == '__main__':
    unittest.main()
