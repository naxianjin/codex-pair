"""让位及退出协议的隔离回归，不操作真实协作或模型。"""
import json
import unittest
from pathlib import Path

from scripts.pair_core import PairError, Store
from tests import test_core, test_reviewer


class RoleHandoffTests(unittest.TestCase):
    def setUp(self):
        test_core.CoreTests.setUp(self)
        import subprocess
        subprocess.run(['git','init','-q',str(self.project)],check=True,capture_output=True)

    tearDown = test_core.CoreTests.tearDown

    def actor(self, member='A'):
        return member, self.sessions[member], self.directories[member]

    def request(self, progress=25, target='B'):
        return self.store.request_role_handoff(*self.actor(), target=target,
            progress=progress, evidence='用户要求让位；按验收步骤及剩余工作判断')

    def save(self, ticket, blocked_reason=''):
        return self.store.save_role_handoff(*self.actor(), ticket['id'],
            body='已完成调查，剩余实现；原工具已结束，文件和历史保留。',
            execution_resolved=True, blocked_reason=blocked_reason)

    def read_all(self, ticket, member='A'):
        offset = 0
        while True:
            page = self.store.read_role_handoff(*self.actor(member), ticket['id'], offset=offset, limit=61)
            offset = page['next_offset']
            if offset is None:
                return page['digest']

    def ack(self, ticket):
        digest = self.read_all(ticket)
        return self.store.ack_role_handoff(*self.actor(), ticket['id'], digest,
            plan='已核对原执行结束、文件归属及未决记录；接收后指定继任者。')

    def task(self, status='active'):
        self.store.control('paused')
        self.store.assign('work', '当前任务', 'B', ['result.txt'])
        with self.store.transaction() as db:
            db.execute('UPDATE tasks SET status=? WHERE id=?', (status, 'work'))

    def test_half_boundary_and_duplicate_request(self):
        self.task()
        ticket = self.request(50)
        self.assertEqual(ticket['mode'], 'restart')
        self.assertEqual(self.request(50)['id'], ticket['id'])
        with self.store.transaction() as db:
            self.assertFalse(self.store._member_eligible(db,'B'))

    def test_reverification_cannot_resume_draining_member(self):
        self.request()
        with self.store.transaction() as db:
            db.execute("UPDATE members SET lifecycle='active',verified=1 WHERE id='B'")
            self.assertFalse(self.store._member_eligible(db,'B'))

    def test_progress_boundaries(self):
        for progress, mode in ((0,'restart'),(49,'restart'),(50,'restart'),(51,'finish_current'),(99,'finish_current')):
            with self.subTest(progress=progress):
                ticket=self.request(progress)
                self.assertEqual(ticket['mode'],mode)
                self.save(ticket)
                self.ack(ticket)
                with self.store.transaction() as db:
                    db.execute("UPDATE members SET lifecycle='active',online=1,verified=1 WHERE id='B'")

    def test_above_half_must_finish_or_record_blocker(self):
        self.task()
        ticket = self.request(51)
        with self.assertRaisesRegex(PairError, 'Finish current task'):
            self.save(ticket)
        saved = self.save(ticket, '必要测试环境不可用，不能完成当前任务')
        self.assertEqual(saved['state'], 'saved')
        self.assertEqual(self.store.task('work')['status'], 'handoff_wait')

    def test_document_full_read_then_ack_retires_without_marking_work_done(self):
        self.task()
        ticket = self.request()
        saved = self.save(ticket)
        self.assertTrue(Path(saved['path']).is_file())
        last = self.store.read_role_handoff(*self.actor(), ticket['id'], offset=10**6)
        with self.assertRaisesRegex(PairError, 'Unread'):
            self.store.ack_role_handoff(*self.actor(), ticket['id'], last['digest'], plan='只读末页')
        self.ack(ticket)
        self.assertEqual(self.store.member('B')['lifecycle'], 'retired')
        self.assertEqual(self.store.task('work')['status'], 'handoff_wait')
        self.assertTrue(Path(saved['path']).is_file())
        self.assertTrue(self.ack(ticket)['duplicate'])

    def test_unknown_execution_blocks_save(self):
        ticket = self.request()
        note = self.store.send('A', 'B', '可能仍在执行')
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='unknown' WHERE id=?", (note['id'],))
        with self.assertRaisesRegex(PairError, 'Execution unresolved'):
            self.save(ticket)

    def test_takeover_requires_document_read_and_does_not_replay(self):
        self.task()
        ticket = self.request()
        self.save(ticket)
        self.ack(ticket)
        self.read_all(ticket, 'A')
        self.store.control('running')
        # 本用例使用同一实际工作树；会话根cwd仍各自独立。
        with self.store.transaction() as db:
            proof = json.loads(self.store._member(db,'A')['proof'])
            proof['git'] = {'path':self.directories['B']}
            db.execute("UPDATE members SET proof=? WHERE id='A'",(json.dumps(proof),))
        result = self.store.take_role_handoff(*self.actor(), ticket['id'],
            target='A', plan='从目标重新检查，仅登记剩余工作，不重复旧外部操作。')
        self.assertTrue(result['restart_from_goal'])
        self.assertEqual(self.store.task('work')['assignee'], 'A')
        self.assertEqual(self.store.task('work')['status'], 'pending')
        self.assertTrue(self.store.take_role_handoff(*self.actor(), ticket['id'],
            target='A', plan='相同接管重试')['duplicate'])

    def test_document_tampering_and_invalid_progress_rejected(self):
        for progress in (-1, 101, True, float('nan'), '50'):
            with self.assertRaises(PairError):
                self.request(progress)
        ticket = self.request(None)
        self.assertEqual(ticket['mode'], 'undetermined')
        saved = self.save(ticket)
        Path(saved['path']).write_text('被改动的交接', encoding='utf-8')
        with self.assertRaisesRegex(PairError, 'changed'):
            self.read_all(ticket)

    def test_a_cannot_yield_and_stop_never_restarts_business(self):
        with self.assertRaisesRegex(PairError, 'A is fixed'):
            self.request(target='A')
        ticket = self.request()
        self.save(ticket)
        self.ack(ticket)
        self.store.control('stopped')
        with self.assertRaisesRegex(PairError, 'not running'):
            self.store.take_role_handoff(*self.actor(), ticket['id'], target='A', plan='核对')

    def test_hidden_reviewer_yield_preserves_epoch_until_ack(self):
        ticket, proof = test_reviewer.ReviewerTests.ready(self)
        handoff = self.request(50, 'reviewer')
        with self.assertRaisesRegex(PairError, 'handoff'):
            self.store.acquire_reviewer(ticket['epoch'], proof['id'], proof)
        self.save(handoff)
        self.assertEqual(self.store.reviewer_status()['reviewer']['state'], 'idle')
        self.ack(handoff)
        self.assertEqual(self.store.reviewer_status()['reviewer']['state'], 'retired')

    parent = test_reviewer.ReviewerTests.parent
    reserve = test_reviewer.ReviewerTests.reserve
