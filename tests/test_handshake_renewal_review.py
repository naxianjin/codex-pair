import json
import unittest
from tests import test_core, test_team
from scripts.pair_core import PairError


class Boundaries(unittest.TestCase):
    setUp = test_core.CoreTests.setUp
    tearDown = test_core.CoreTests.tearDown
    actor = test_team.TeamTests.actor
    window = test_team.TeamTests.window
    join = test_team.TeamTests.join

    def prepare(self):
        _, _, self.d = self.join(4, 'D')
        q = self.store.send('D', 'A', 'hello', kind='question', dedupe_key='window-handshake:D:1')
        r = self.store.send('A', 'D', 'reply', kind='reply', reply_to=q['id'])
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET process_status='completed',remote_status='completed' WHERE id=?", (q['id'],))
            db.execute("UPDATE messages SET delivery_status='failed' WHERE id=?", (r['id'],))
        return q, r

    def renew(self, r):
        return self.store.renew_handshake('D', self.d['session_uuid'], self.d['cwd'], r['id'])

    def context(self):
        offset = 0
        while True:
            page = self.store.read_member_context('D', self.d['session_uuid'], self.d['cwd'], offset=offset)
            if page['next_offset'] is None:
                return page['digest']
            offset = page['next_offset']

    def confirm(self, digest):
        return self.store.confirm_member('D', self.d['session_uuid'], self.d['cwd'], proof=self.d['proof'], context_digest=digest)

    def test_d_success_requires_new_terminal_and_context(self):
        q, r = self.prepare()
        new = self.renew(r)
        reply = self.store.send('A', 'D', 'new', kind='reply', reply_to=new['id'])
        digest = self.context()
        with self.assertRaisesRegex(PairError, 'Communication unverified'):
            self.confirm(digest)
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET process_status='completed',remote_status='completed' WHERE id IN (?,?)", (new['id'], reply['id']))
            db.execute('DELETE FROM member_context_reads')
        with self.assertRaisesRegex(PairError, 'Context not read'):
            self.confirm(digest)
        self.assertTrue(self.confirm(self.context())['verified'])
        with self.assertRaisesRegex(PairError, 'Unverified member required'):
            self.renew(r)

    def test_old_success_cannot_confirm_after_renewal(self):
        q, r = self.prepare()
        self.renew(r)
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET process_status='completed',remote_status='completed' WHERE id=?", (r['id'],))
        with self.assertRaisesRegex(PairError, 'Communication unverified'):
            self.confirm(self.context())

    def test_stale_question_or_reply_versions_rejected(self):
        q, r = self.prepare()
        for message in (q, r):
            for column in ('sender_version', 'recipient_version'):
                with self.subTest(message=message['kind'], column=column):
                    with self.store.transaction() as db:
                        db.execute('UPDATE messages SET '+column+'=99 WHERE id=?', (message['id'],))
                    with self.assertRaises(PairError):
                        self.renew(r)
                    with self.store.transaction() as db:
                        db.execute('UPDATE messages SET '+column+'=1 WHERE id=?', (message['id'],))

    def test_wrong_identity_and_paused_rejected(self):
        q, r = self.prepare()
        with self.assertRaises(PairError):
            self.store.renew_handshake('D', self.sessions['B'], self.d['cwd'], r['id'])
        with self.assertRaises(PairError):
            self.store.renew_handshake('D', self.d['session_uuid'], self.directories['B'], r['id'])
        self.store.control('paused')
        with self.assertRaises(PairError):
            self.renew(r)

    def test_unknown_or_delivering_other_message_rejected(self):
        q, r = self.prepare()
        note = self.store.send('A', 'D', 'other', kind='note')
        for status in ('unknown', 'delivering'):
            with self.subTest(status=status):
                with self.store.transaction() as db:
                    db.execute('UPDATE messages SET delivery_status=? WHERE id=?', (status, note['id']))
                with self.assertRaisesRegex(PairError, 'Unresolved work'):
                    self.renew(r)

    def test_completed_business_with_model_inprogress_rejected(self):
        q, r = self.prepare()
        note = self.store.send('A', 'D', 'business result', kind='result')
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='accepted',process_status='completed',remote_status='inProgress' WHERE id=?", (note['id'],))
        with self.assertRaisesRegex(PairError, 'Unresolved work'):
            self.renew(r)

    def test_active_business_task_rejected(self):
        self.store.assign('ongoing', 'active business', 'B', ['src/x.py'])
        q, r = self.prepare()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET assignee='D',status='active' WHERE id='ongoing'")
        with self.assertRaisesRegex(PairError, 'Unresolved work'):
            self.renew(r)

    def test_task_receipt_completed_but_model_inprogress_rejected(self):
        q, r = self.prepare()
        message = self.store.send('A', 'D', 'business task', kind='task')
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='accepted',process_status='completed',remote_status='inProgress' WHERE id=?", (message['id'],))
        with self.assertRaisesRegex(PairError, 'Unresolved work'):
            self.renew(r)

    def test_repeat_no_new_message_or_verification(self):
        q, r = self.prepare()
        first = self.renew(r)
        for _ in range(3):
            self.assertEqual(self.renew(r)['id'], first['id'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM messages WHERE dedupe_key=?", ('renewed-handshake:'+r['id'],)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT verified FROM members WHERE id='D'").fetchone()[0], 0)
            self.assertEqual(dict(db.execute('SELECT * FROM messages WHERE id=?', (r['id'],)).fetchone())['delivery_status'], 'failed')

    def test_unrelated_question_rejected(self):
        q, r = self.prepare()
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET dedupe_key='ordinary-question' WHERE id=?", (q['id'],))
        with self.assertRaisesRegex(PairError, 'Current handshake required'):
            self.renew(r)

    def test_changed_proof_or_digest_rejected(self):
        q, r = self.prepare()
        digest = self.context()
        changed = dict(self.d['proof'], model='different')
        with self.assertRaisesRegex(PairError, 'Configuration changed'):
            self.store.confirm_member('D', self.d['session_uuid'], self.d['cwd'], proof=changed, context_digest=digest)
        with self.assertRaisesRegex(PairError, 'Context changed'):
            self.confirm('0'*64)
