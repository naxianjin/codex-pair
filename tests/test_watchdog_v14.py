"""只替代OS/启动边界，使用真实Store和恢复计数；待隔离环境执行。"""
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import pair_watchdog as watchdog
from scripts.pair_core import PairError, Store
from tests import test_core


class WatchdogTests(unittest.TestCase):
    def setUp(self):
        test_core.CoreTests.setUp(self)
        self.runtime = self.project / 'watchdog-runtime'
        (self.runtime / 'scripts').mkdir(parents=True)
        (self.runtime / 'SKILL.md').write_bytes(b'fixed skill')
        self.entry = self.runtime / 'scripts/pair.py'
        self.entry.write_bytes(b'# fixed bridge fixture, never executed')
        (self.runtime / 'package-manifest.json').write_text(json.dumps({'schema_version': 1,
            'files': {name: hashlib.sha256((self.runtime / name).read_bytes()).hexdigest()
                      for name in ('SKILL.md', 'scripts/pair.py')}}), encoding='utf-8')
        self.codex = self.project / 'codex-fixture.exe'
        self.codex.write_bytes(b'fixed external executable fixture, never executed')
        self.cli = SimpleNamespace(__file__=str(self.entry))
        self.original = {'state': 'alive', 'pid': 4321, 'created': 100, 'image': sys.executable}
        with self.store.transaction() as db:
            db.execute("UPDATE runtime SET owner='original',pid=4321")
        self.store.recovery_policy('A', self.sessions['A'], self.directories['A'], 'transport',
            enabled=True, basis='仅隔离测试授权', expected_version=1)

    tearDown = test_core.CoreTests.tearDown

    def status(self, locked=False, probe=False):
        with self.store.transaction() as db:
            runtime = dict(db.execute('SELECT * FROM runtime').fetchone())
        return {'runtime': {**runtime, 'lock_held': locked, 'probe_confirmed': probe}, 'relays': []}

    def configure(self):
        with patch.object(self.store, 'status', return_value=self.status(True, True)), \
                patch.object(watchdog, 'process_identity', return_value=self.original):
            return watchdog.configure_watchdog(self.store, 'A', self.sessions['A'], self.directories['A'],
                cli_path=self.entry, python_path=sys.executable, codex_path=self.codex)

    def tick(self, now, launch=None, observation=None, locked=False, probe=False):
        config = self.store.setting(watchdog.SETTING)
        observed = observation or {'state': 'absent', 'pid': config['bridge']['pid']}
        with patch.object(watchdog, 'process_identity', return_value=observed), \
                patch.object(self.store, 'status', return_value=self.status(locked, probe)), \
                patch('scripts.pair_windows.bridge', side_effect=launch) as bridge:
            result = watchdog.watchdog_tick(self.store, self.cli, now=now)
        return result, bridge.call_count

    def launch_success(self, store, cli, executable, *, before_start):
        with before_start() as nonce:
            self.assertEqual(executable, str(watchdog.checked_path(self.codex)))
        with store.transaction() as db:
            db.execute('UPDATE runtime SET owner=?,pid=?', ('new-' + nonce, os.getpid()))
        identity = {'state': 'alive', 'pid': os.getpid(), 'created': 200 + self.attempts(), 'image': sys.executable}
        with patch.object(watchdog, 'process_identity', return_value=identity), patch.object(sys, 'argv', [str(self.entry)]):
            watchdog.acknowledge_bridge_start(store, nonce)
        return {'running': True, 'reused': False, 'pid': os.getpid()}

    def attempts(self):
        with self.store.transaction() as db:
            return db.execute("SELECT count(*) FROM recovery_attempts WHERE kind='bridge_restart'").fetchone()[0]

    def test_requires_transport_policy_and_verified_existing_bridge(self):
        with self.store.transaction() as db:
            db.execute("UPDATE recovery_policies SET enabled=0 WHERE domain='transport'")
        with self.assertRaisesRegex(PairError, 'Recovery authorization missing'):
            self.configure()
        self.assertEqual(self.attempts(), 0)

    def test_alive_unresponsive_never_killed_or_restarted(self):
        self.configure()
        result, calls = self.tick(1000, observation=self.original, locked=True)
        self.assertEqual(result['reason'], 'bridge_alive_unresponsive_or_locked')
        self.assertEqual(calls, 0)
        self.assertEqual(self.attempts(), 0)
        self.tick(1030, observation=self.original, locked=True)
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='watchdog_blocked'").fetchone()[0], 1)

    def test_unknown_process_exit_does_not_start(self):
        self.configure()
        result, calls = self.tick(1000, observation={'state': 'unknown', 'pid': 4321, 'error': 'Access denied'})
        self.assertEqual(result['reason'], 'bridge_exit_unconfirmed')
        self.assertEqual(calls, 0)

    def test_pid_reuse_proves_old_exit_but_does_not_touch_foreign_process(self):
        self.configure()
        result, calls = self.tick(1000, observation={**self.original, 'created': 999})
        self.assertEqual(result['state'], 'backoff')
        self.assertEqual(result['next_attempt_at'], 1005)
        self.assertEqual(calls, 0)

    def test_three_restarts_backoff_and_durable_window(self):
        self.configure()
        for observed, due in ((1000, 1005), (1010, 1025), (1030, 1060)):
            result, calls = self.tick(observed)
            self.assertEqual(result['next_attempt_at'], due)
            self.assertEqual(calls, 0)
            result, calls = self.tick(due, self.launch_success)
            self.assertEqual(result['state'], 'watching')
            self.assertEqual(calls, 1)
        self.assertEqual(self.attempts(), 3)
        result, calls = self.tick(1070)
        self.assertEqual(result['reason'], 'bridge_restart_budget_exhausted')
        self.assertEqual(result['next_attempt_at'], 1605)
        self.assertEqual(calls, 0)
        reopened = Store(self.store.pair_id, home=self.store.home, state_root=self.store.state_root)
        with reopened.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM recovery_attempts WHERE kind='bridge_restart'").fetchone()[0], 3)
        self.assertEqual(reopened.setting(watchdog.SETTING)['next_attempt_at'], 1605)

    def test_unknown_start_preserves_attempt_and_never_replays(self):
        self.configure()
        self.tick(1000)
        def uncertain(store, cli, executable, *, before_start):
            with before_start():
                raise OSError('startup result unknown')
        with self.assertRaisesRegex(OSError, 'startup result unknown'):
            self.tick(1005, uncertain)
        self.assertEqual(self.attempts(), 1)
        self.assertIsNotNone(self.store.setting(watchdog.SETTING)['pending'])
        result, calls = self.tick(1040)
        self.assertEqual(result['reason'], 'bridge_restart_result_unknown')
        self.assertEqual(calls, 0)
        self.assertEqual(self.attempts(), 1)

    def test_stop_closes_intent_and_pause_is_not_cleared(self):
        self.configure()
        self.store.control('paused')
        self.tick(1000)
        self.tick(1005, self.launch_success)
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT status FROM collaborations').fetchone()[0], 'paused')
        watchdog.disable_watchdog(self.store, reason='用户明确停止')
        self.store.control('stopped')
        result, calls = self.tick(1040)
        self.assertFalse(result['enabled'])
        self.assertEqual(calls, 0)

    def test_runtime_change_is_rejected_before_attempt_counter(self):
        self.configure()
        self.tick(1000)
        self.entry.write_bytes(b'changed runtime')
        with self.assertRaisesRegex(ValueError, '摘要不一致'):
            self.tick(1005, self.launch_success)
        self.assertEqual(self.attempts(), 0)

    def test_clock_backwards_and_stale_bootstrap_rejected(self):
        self.configure()
        self.tick(1000)
        result, calls = self.tick(990)
        self.assertEqual(result['reason'], 'clock_moved_backwards')
        self.assertEqual(calls, 0)
        with patch.object(watchdog, 'process_identity', return_value=self.original):
            with self.assertRaisesRegex(PairError, 'Watchdog bootstrap mismatch'):
                watchdog.acknowledge_bridge_start(self.store, 'old-nonce')


if __name__ == '__main__':
    unittest.main()
