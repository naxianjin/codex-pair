"""候选诊断行为用例；须在已验收 Windows 隔离环境内执行。"""
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from scripts.pair_core import Store
from scripts.pair_diagnostics import diagnose
from scripts.pair_next_commands import validate_checks
from scripts.pair_producer import strict_display
from tests.test_core import CoreTests


class DiagnosticTests(unittest.TestCase):
    setUp = CoreTests.setUp
    tearDown = CoreTests.tearDown

    def snapshot(self, **kwargs):
        return diagnose(self.store.state_root, self.store.pair_id, **kwargs)

    def test_missing_database_does_not_create_directory(self):
        root = self.root / 'missing'
        with self.assertRaisesRegex(ValueError, 'State missing'):
            diagnose(root, 'missing')
        self.assertFalse(root.exists())

    def test_diagnosis_never_initializes_or_upgrades_store(self):
        before = hashlib.sha256(self.store.path.read_bytes()).hexdigest()
        with patch.object(Store, '__init__', side_effect=AssertionError('Store initialized')):
            result = self.snapshot()
        self.assertTrue(result['read_only'])
        self.assertEqual(before, hashlib.sha256(self.store.path.read_bytes()).hexdigest())
        self.assertFalse(result['runtime_observed'])

    def test_multiple_reasons_and_scheduler_share_rules(self):
        self.store.control('paused')
        self.store.assign('parent', '前置', 'A', ['parent.txt'])
        self.store.assign('child', '后续', 'B', ['child.txt'], dependencies=['parent'])
        result = self.snapshot(task_id='child')
        task = next(t for t in result['tasks'] if t['id'] == 'child')
        with self.store.transaction() as db:
            expected = self.store._admission_reasons(db, dict(db.execute("SELECT * FROM tasks WHERE id='child'").fetchone()), 'B')
        self.assertEqual(task['admission']['B'], expected)
        codes = {r['code'] for r in expected}
        self.assertIn('USER_PAUSED_OR_STOPPED', codes)
        self.assertIn('DEPENDENCY_UNSATISFIED', codes)

    def test_unknown_schema_not_upgraded(self):
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.execute('PRAGMA user_version=99')
        with self.assertRaisesRegex(ValueError, 'Unsupported diagnostic schema'):
            self.snapshot()
        with closing(sqlite3.connect(self.store.path)) as db, db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 99)

    def test_pause_blocks_already_queued_business_claim(self):
        self.store.assign('work', '业务', 'B', ['work.txt'])
        with self.store.transaction() as db:
            row = db.execute("SELECT id FROM messages WHERE task_id='work'").fetchone()
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='queued' WHERE id=?", (row[0],))
        self.store.control('paused')
        result = self.store.claim('B', self.sessions['B'], self.directories['B'], message_id=row[0])
        self.assertFalse(result['claimed'])

    def test_resume_does_not_silently_reopen_stopped_collaboration(self):
        self.store.control('stopped')
        with self.assertRaisesRegex(RuntimeError, 'Resume reconciliation required'):
            self.store.control('running')


class ExactCheckTests(unittest.TestCase):
    def test_newlines_and_quotes_remain_exact(self):
        argv = ['powershell', '-Command', '"中文"\nC:\\path with spaces']
        check = {'name': 'evidence', 'argv': argv, 'command': strict_display(argv), 'required': True}
        validate_checks([check])
        with self.assertRaisesRegex(ValueError, 'Invalid exact'):
            validate_checks([dict(check, command=check['command']+'\n')])
        with self.assertRaisesRegex(ValueError, 'Invalid exact'):
            validate_checks([check, check])
