"""只使用临时库和合成官方回执，核验安全收尾。"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,os.environ.get('RECOVERY_CANDIDATE',str(Path(__file__).resolve().parents[1])))
from tests import test_core,test_exact_managed_receipts
from scripts import pair_review_runtime as runtime
from scripts.pair_core import PairError

class BoundaryTests(unittest.TestCase):
    setUp=test_core.CoreTests.setUp
    tearDown=test_core.CoreTests.tearDown
    def prepare(self):
        self.store.assign('blocked-work','saved work','B',['file.txt'])
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='blocked' WHERE id='blocked-work'")
        return 'blocked-work'
    def test_original_owner_can_preserve_blocked_work(self):
        key=self.prepare()
        r=self.store.release_task_boundary('B',self.sessions['B'],self.directories['B'],key,'exact tools terminal; files and hashes preserved')
        self.assertTrue(r['saved']);self.assertEqual(self.store.task(key)['status'],'handed_back')
    def test_a_can_preserve_known_terminal_blocked_task(self):
        key=self.prepare()
        self.assertTrue(self.store.release_task_boundary('A',self.sessions['A'],self.directories['A'],key,'owner tool receipts completed; no related approvals')['saved'])
        self.assertEqual(self.store.task(key)['status'],'handed_back')
    def test_inflight_remote_task_still_blocks(self):
        key=self.prepare()
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='inProgress' WHERE task_id=?",(key,))
        with self.assertRaisesRegex(PairError,'Turn still running'):
            self.store.release_task_boundary('A',self.sessions['A'],self.directories['A'],key,'not enough')
    def test_completed_task_not_reopened(self):
        key=self.prepare()
        with self.store.transaction() as db:db.execute("UPDATE tasks SET status='completed' WHERE id=?",(key,))
        with self.assertRaises(PairError):self.store.release_task_boundary('B',self.sessions['B'],self.directories['B'],key,'done')

class IdleReceiptTests(unittest.TestCase):
    setUp=test_exact_managed_receipts.ExactReceiptTests.setUp
    tearDown=test_exact_managed_receipts.ExactReceiptTests.tearDown
    actor=test_exact_managed_receipts.ExactReceiptTests.actor
    allow=test_exact_managed_receipts.ExactReceiptTests.allow
    def test_idle_unknown_receipt_settles_without_releasing_or_approving(self):
        self.store.assign('api','stage','B',['api.py'])
        with self.store.transaction() as db:db.execute("UPDATE tasks SET status='completed' WHERE id='api'")
        stage=self.store.submit_stage('B',self.sessions['B'],self.directories['B'],'api',{'kind':'commit','head':'b'*40},[{'member':'B','provider':'openai','model':'gpt-test'}],[],{'objective':'audit','acceptance':[],'environment':{},'lockfiles':[],'baseline':'a'*40})
        with self.store.transaction() as db:
            db.execute("UPDATE review_stages SET status='needs_validation',reviewer_epoch=?,reviewer_session=?,execution_id=?,turn_id='turn-one' WHERE id=?",(self.ticket['epoch'],self.proof['id'],self.lease['execution_id'],stage['id']))
            db.execute("UPDATE managed_permissions SET status='unknown'")
        self.store.release_reviewer(self.ticket['epoch'],self.proof['id'],self.lease['owner'],outcome='completed',evidence='official original turn already ended')
        thread=dict(self.proof,path=str(self.root/'missing.jsonl'));turn=self.turn
        class Observer:
            def __init__(self,**kw):pass
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read_thread(self,sid):return thread
            def turns(self,sid,**kw):yield turn
        with patch.object(runtime,'AppServer',Observer),patch.object(self.store,'reconcile_reviewer_execution',side_effect=AssertionError('idle lease must not be released again')):
            r=runtime.reconcile_review(self.store,*self.actor(),stage['id'],'exact original terminal and receipt verified')
        self.assertFalse(r['work_approved'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT status FROM managed_permissions').fetchone()[0],'success')
            self.assertEqual(db.execute('SELECT status FROM review_stages').fetchone()[0],'needs_validation')
    def test_idle_wrong_command_remains_unknown(self):
        with self.store.transaction() as db:db.execute("UPDATE managed_permissions SET status='unknown'")
        self.store.release_reviewer(self.ticket['epoch'],self.proof['id'],self.lease['owner'],outcome='completed',evidence='original terminal known')
        self.turn['items'][0]['command']='different command'
        runtime.settle_permissions(self.store,self.ticket['epoch'],self.lease['execution_id'],self.turn)
        with self.store.transaction() as db:self.assertEqual(db.execute('SELECT status FROM managed_permissions').fetchone()[0],'unknown')

if __name__=='__main__':unittest.main()
