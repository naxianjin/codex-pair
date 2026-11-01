import unittest
from tests.test_core import CoreTests
from scripts.pair_core import PairError
class RenewalTests(unittest.TestCase):
    setUp=CoreTests.setUp
    tearDown=CoreTests.tearDown
    def prepare(self):
        q=self.store.send('B','A','hello',kind='question',dedupe_key='window-handshake:B:1')
        r=self.store.send('A','B','reply',kind='reply',reply_to=q['id'])
        with self.store.transaction() as db:
            db.execute("UPDATE members SET verified=0 WHERE id='B'")
            db.execute("UPDATE messages SET process_status='completed',remote_status='completed' WHERE id=?",(q['id'],))
            db.execute("UPDATE messages SET delivery_status='failed' WHERE id=?",(r['id'],))
        return q,r
    def renew(self,r):return self.store.renew_handshake('B',self.sessions['B'],self.directories['B'],r['id'])
    def test_preserves_failed_reply_and_idempotent(self):
        q,r=self.prepare();new=self.renew(r)
        self.assertEqual(new['id'],self.renew(r)['id'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT delivery_status FROM messages WHERE id=?',(r['id'],)).fetchone()[0],'failed')
            self.assertEqual(db.execute("SELECT verified FROM members WHERE id='B'").fetchone()[0],0)
    def test_unknown_rejected(self):
        q,r=self.prepare()
        with self.store.transaction() as db:db.execute("UPDATE messages SET delivery_status='unknown' WHERE id=?",(r['id'],))
        with self.assertRaises(PairError):self.renew(r)
    def test_claimed_rejected(self):
        q,r=self.prepare()
        with self.store.transaction() as db:db.execute("UPDATE messages SET process_status='claimed' WHERE id=?",(r['id'],))
        with self.assertRaises(PairError):self.renew(r)
    def test_unfinished_question_rejected(self):
        q,r=self.prepare()
        with self.store.transaction() as db:db.execute("UPDATE messages SET remote_status='inProgress' WHERE id=?",(q['id'],))
        with self.assertRaises(PairError):self.renew(r)
    def test_context_wakeup_requires_new_terminals(self):
        q,r=self.prepare();new=self.renew(r)
        reply=self.store.send('A','B','new reply',kind='reply',reply_to=new['id'])
        self.store.setting('member_context_required',True)
        with self.store.transaction() as db:db.execute("UPDATE messages SET process_status='completed',remote_status='inProgress' WHERE id IN (?,?)",(new['id'],reply['id']))
        self.store.schedule()
        with self.store.transaction() as db:
            self.assertFalse(db.execute("SELECT 1 FROM messages WHERE dedupe_key LIKE 'member-context:B:%'").fetchone())
            db.execute("UPDATE messages SET remote_status='completed' WHERE id IN (?,?)",(new['id'],reply['id']))
        self.store.schedule()
        with self.store.transaction() as db:self.assertTrue(db.execute("SELECT 1 FROM messages WHERE dedupe_key LIKE 'member-context:B:%'").fetchone())
