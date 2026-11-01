"""发送前只读核验超时与真正发送不明必须分开，不能靠完成消息清空unknown。"""
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from scripts import pair
from scripts.pair_core import Daemon, PairError
from scripts.pair_transport import DeliveryUnknown, QueueTransport, TransportError, TransportPool
from tests import test_core


LEGACY = 'RPC result unknown / RPC 结果不明: account/read ' + json.dumps({
    'stage': 'process_start', 'exit_code': None, 'last_success_rpc': 'thread/read',
    'stderr': 'codex_models_manager::manager: failed to refresh available models: request timed out'})


class PreflightDeliveryTests(unittest.TestCase):
    def setUp(self):
        test_core.CoreTests.setUp(self)

    def tearDown(self):
        test_core.CoreTests.tearDown(self)

    def queue(self):
        q = QueueTransport.__new__(QueueTransport)
        q.command = ['codex.exe']; q.cwd = None; q.environment = {}; q.timeout = 1
        return q

    def test_validation_timeout_is_retryable_without_queue_submission(self):
        q = self.queue()
        with patch.object(q, 'validate', side_effect=DeliveryUnknown(LEGACY)), patch('scripts.pair_transport.subprocess.run') as run:
            with self.assertRaises(TransportError) as caught:
                q.start({}, {'envelope': '{}'})
        self.assertNotIsInstance(caught.exception, DeliveryUnknown)
        self.assertTrue(caught.exception.retryable)
        run.assert_not_called()

    def test_client_acquisition_timeout_is_also_before_submission(self):
        pool = TransportPool()
        with patch.object(pool, 'client', side_effect=DeliveryUnknown(LEGACY)):
            with self.assertRaises(TransportError) as caught:
                pool.start({'backend': 'queue'}, {})
        self.assertNotIsInstance(caught.exception, DeliveryUnknown)
        self.assertTrue(caught.exception.retryable)
        with patch.object(pool, 'client', side_effect=DeliveryUnknown(LEGACY)):
            with self.assertRaises(DeliveryUnknown):
                pool.start({'backend': 'managed'}, {})

    def test_queue_timeout_nonzero_and_bad_ack_stay_unknown(self):
        q = self.queue(); member = {'session_uuid': '00000000-0000-0000-0000-000000000001'}
        outcomes = [subprocess.TimeoutExpired(['codex'], 1),
                    subprocess.CompletedProcess([], 1, b'', LEGACY.encode()),
                    subprocess.CompletedProcess([], 0, b'bad acknowledgement', b'')]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__), patch.object(q, 'validate'), patch('scripts.pair_transport.subprocess.run') as run:
                if isinstance(outcome, Exception): run.side_effect = outcome
                else: run.return_value = outcome
                with self.assertRaises(DeliveryUnknown): q.start(member, {'envelope': '{}'})
                run.assert_called_once()

    def test_daemon_retries_preflight_only_with_finite_attempt_limit(self):
        message = self.store.send('A', 'B', '测试有限重试')
        q = self.queue()
        daemon = Daemon(self.store, q, max_attempts=3)
        with patch.object(q, 'validate', side_effect=DeliveryUnknown(LEGACY)), patch('scripts.pair_transport.subprocess.run') as run:
            for expected in ['retry', 'retry', 'failed']:
                self.assertTrue(daemon.dispatch_one())
                self.assertEqual(self.store.message(message['id'])['delivery_status'], expected)
                with self.store.transaction() as db:
                    db.execute('UPDATE messages SET next_attempt=0 WHERE id=?', (message['id'],))
            self.assertFalse(daemon.dispatch_one())
            run.assert_not_called()
        self.assertEqual(self.store.message(message['id'])['attempts'], 3)

    def test_pool_cold_client_validation_and_identity_mismatch(self):
        member = self.store.member('B')
        client = Mock(); client.validate.side_effect = DeliveryUnknown(LEGACY)
        pool = TransportPool()
        with patch('scripts.pair_transport.QueueTransport', return_value=client):
            with self.assertRaises(TransportError) as caught: pool.start(member, {})
        self.assertNotIsInstance(caught.exception, DeliveryUnknown)
        client.start.assert_not_called(); client.close.assert_called_once()
        self.assertFalse(pool.clients)
        q = self.queue()
        with patch.object(q, 'validate', side_effect=TransportError('Account changed / 原账号已变化')), patch('scripts.pair_transport.subprocess.run') as run:
            with self.assertRaises(TransportError) as caught: q.start(member, {})
            self.assertFalse(caught.exception.retryable)
            run.assert_not_called()

    def legacy_result(self, error=LEGACY):
        message = self.store.send('B', 'A', '准备交付', kind='result')
        Daemon(self.store, test_core.FakeTransport(error=DeliveryUnknown(error))).dispatch_one()
        claimed = self.store.claim('A', self.sessions['A'], self.directories['A'], message_id=message['id'])
        self.store.complete('A', self.sessions['A'], self.directories['A'], message['id'], claimed['claim_token'], '已完整阅读并收尾')
        return message

    def recover(self, message, member='A'):
        return self.store.recover_unsent_delivery(member, self.sessions[member], self.directories[member], message['id'], '原通知已读；保留未提交证据，不重发')

    def test_completed_legacy_result_releases_workspace_without_resending(self):
        message = self.legacy_result()
        with self.store.transaction() as db:
            before = [dict(r) for r in db.execute('SELECT * FROM attempts WHERE message_id=?', (message['id'],))]
            self.assertTrue(self.store._running(db))
        result = self.recover(message)
        self.assertTrue(result['recovered']); self.assertFalse(result['automatic_replay'])
        after = self.store.message(message['id'])
        self.assertEqual(after['delivery_status'], 'not_submitted')
        self.assertEqual(after['process_status'], 'completed')
        self.assertIsNone(after['remote_status'])
        with self.store.transaction() as db:
            self.assertEqual(before, [dict(r) for r in db.execute('SELECT * FROM attempts WHERE message_id=?', (message['id'],))])
            self.assertFalse(self.store._running(db))
        self.assertFalse(self.recover(message)['recovered'])
        next_message = self.store.send('A', 'B', '后续任务')
        transport = test_core.FakeTransport(self.store)
        self.assertTrue(Daemon(self.store, transport).dispatch_one())
        self.assertEqual([m['id'] for _, m in transport.calls], [next_message['id']])

    def test_completed_true_unknown_and_forged_suffix_do_not_release(self):
        for error in ['Queue result unknown / 队列请求超时，结果不明',
                      'Queue exit outcome unknown / 队列命令失败，投递结果不明: ' + LEGACY,
                      LEGACY.replace('account/read', 'turn/start'), LEGACY + ' extra']:
            with self.subTest(error=error):
                message = self.legacy_result(error)
                before = self.store.message(message['id'])
                self.assertFalse(self.recover(message)['recovered'])
                self.assertEqual(before, self.store.message(message['id']))
                # 隔离场景独立，不让前一条未知消息阻止生成下一条证据。
                with self.store.transaction() as db:
                    db.execute("UPDATE messages SET delivery_status='failed' WHERE id=?", (message['id'],))

    def test_recovery_checks_authority_receipt_and_full_attempt_history(self):
        message = self.legacy_result()
        with self.assertRaises(PairError): self.recover(message, member='B')
        for field, value in [('queue_id', 'some-queue'), ('turn_id', 'some-turn'), ('remote_status', 'queued'),
                             ('process_status', 'claimed'), ('kind', 'task')]:
            with self.subTest(field=field), self.store.transaction() as db:
                original = db.execute('SELECT ' + field + ' FROM messages WHERE id=?', (message['id'],)).fetchone()[0]
                db.execute('UPDATE messages SET ' + field + '=? WHERE id=?', (value, message['id']))
            self.assertFalse(self.recover(message)['recovered'])
            with self.store.transaction() as db:
                db.execute('UPDATE messages SET ' + field + '=? WHERE id=?', (original, message['id']))
        with self.store.transaction() as db:
            db.execute("UPDATE attempts SET outcome='accepted' WHERE message_id=?", (message['id'],))
        self.assertFalse(self.recover(message)['recovered'])

    def test_cli_requires_real_actor_validation_before_recovery(self):
        message = self.legacy_result()
        argv = ['--pair', 'test', '--codex-home', str(self.home), 'recover-unsent-delivery',
                '--member', 'A', '--session', self.sessions['A'], '--worktree', self.directories['A'],
                '--message', message['id'], '--body', '只结算已读的发送前失败，不重发']
        args = pair.build_parser().parse_args(argv)
        with patch('scripts.pair.validate_actor', side_effect=PairError('identity not verified')):
            with self.assertRaises(PairError): pair.execute(args)
        self.assertEqual(self.store.message(message['id'])['delivery_status'], 'unknown')
        with patch('scripts.pair.validate_actor') as validate:
            self.assertTrue(pair.execute(args)['recovered'])
            validate.assert_called_once()


if __name__ == '__main__': unittest.main()
