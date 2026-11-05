"""第三方协作和审核的隔离回归，不代表真实服务验收。"""
import json
from contextlib import closing
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from scripts.pair_core import PairError
from scripts.pair_reviewer import official_identity, reviewer_identity, verify_review_service
from scripts.pair_review_runtime import ReviewerController
from scripts.pair_quota import read_member
from tests import test_reviewer, test_auxiliary_v14, test_final_receipt_settlement, test_review_receipt_access


class ApinebulaReviewerTests(unittest.TestCase):
    setUp = test_reviewer.ReviewerTests.setUp
    tearDown = test_reviewer.ReviewerTests.tearDown
    parent = test_reviewer.ReviewerTests.parent
    reserve = test_reviewer.ReviewerTests.reserve

    def ready(self):
        self.parent(modelProvider='apinebula')
        ticket=self.reserve()
        proof={'id':'00000000-0000-0000-0000-000000000099','cwd':self.directories['A'],
               'modelProvider':'apinebula','model':'gpt-6.1-sol','ephemeral':False}
        self.store.bind_reviewer('A',self.sessions['A'],self.directories['A'],epoch=ticket['epoch'],
                                 attempt=ticket['attempt'],proof=proof,reviewer_cwd=self.directories['A'])
        return ticket,proof

    def test_authorized_third_party_is_never_official_identity(self):
        ticket,proof=self.ready()
        self.assertEqual(ticket['modelProvider'],'apinebula')
        self.assertEqual(reviewer_identity(proof)['modelProvider'],'apinebula')
        with self.assertRaisesRegex(PairError,'Official GPT'):official_identity(proof)
        status=self.store.reviewer_status()['reviewer']
        self.assertEqual(status['modelProvider'],'apinebula')
        self.assertEqual(status['review_service'],'third_party')
        self.store.acquire_reviewer(ticket['epoch'],proof['id'],proof)

    def test_provider_change_invalidates_lease_even_with_same_model(self):
        ticket,proof=self.ready()
        with self.assertRaisesRegex(PairError,'Stale reviewer identity'):
            self.store.acquire_reviewer(ticket['epoch'],proof['id'],dict(proof,modelProvider='openai'))
        self.parent(modelProvider='openai')
        with self.assertRaisesRegex(PairError,'Parent configuration changed'):
            self.store.acquire_reviewer(ticket['epoch'],proof['id'],proof)

    def test_bind_rejects_wrong_provider_and_self_review(self):
        self.parent(modelProvider='apinebula');ticket=self.reserve()
        proof={'id':'00000000-0000-0000-0000-000000000099','cwd':self.directories['A'],
               'modelProvider':'openai','model':'gpt-6.1-sol'}
        for identity in (proof,dict(proof,modelProvider='apinebula',id=self.sessions['A'])):
            with self.subTest(identity=identity),self.assertRaisesRegex(PairError,'identity mismatch'):
                self.store.bind_reviewer('A',self.sessions['A'],self.directories['A'],epoch=ticket['epoch'],
                                         attempt=ticket['attempt'],proof=identity,reviewer_cwd=self.directories['A'])

    def test_parent_and_quota_skip_official_account_rpc(self):
        self.parent(modelProvider='apinebula');member=self.store.member('A')
        proof=json.loads(member['proof']);client=Mock();client.validate.return_value=proof
        client.rpc.side_effect=AssertionError('third party must not query official account')
        with patch('scripts.pair_review_runtime.AppServer') as factory:
            factory.return_value.__enter__.return_value=client
            _,actual=ReviewerController(self.store)._parent()
        self.assertEqual(actual['modelProvider'],'apinebula')
        self.assertTrue(verify_review_service(client,proof,member['codex_home']))
        self.assertIsNone(read_member(self.store,member,client)['remaining'])
        client.rpc.assert_not_called()

    def test_managed_session_requests_actual_provider(self):
        self.parent(modelProvider='apinebula')
        member=self.store.member('A');parent=json.loads(member['proof'])
        client=Mock();client.rpc.return_value={'thread':{'id':'00000000-0000-0000-0000-000000000099'}}
        client.read_thread.return_value=dict(parent,id='00000000-0000-0000-0000-000000000099')
        controller=ReviewerController(self.store)
        with patch.object(controller,'_parent',return_value=(member,parent)), \
             patch('scripts.pair_review_runtime.prepare_review_tree',return_value=parent['cwd']), \
             patch('scripts.pair_review_runtime.AppServer',return_value=client), \
             patch('scripts.pair_review_runtime.hidden_handoff.adopt',return_value=None):
            state=controller._session('a'*40)
        self.assertEqual(client.rpc.call_args.args[0],'thread/start')
        self.assertEqual(client.rpc.call_args.args[1]['modelProvider'],'apinebula')
        self.assertEqual(state['proof']['modelProvider'],'apinebula')

    def test_official_service_still_requires_real_account(self):
        self.parent();proof=json.loads(self.store.member('A')['proof']);client=Mock()
        client.rpc.return_value={'account':None}
        with self.assertRaisesRegex(PairError,'Official account unverified'):
            verify_review_service(client,proof,self.home)
        client.rpc.assert_called_once_with('account/read',{'refreshToken':False})


class ApinebulaAuxiliaryTests(unittest.TestCase):
    setUp = test_auxiliary_v14.AuxiliaryTests.setUp
    tearDown = test_auxiliary_v14.AuxiliaryTests.tearDown
    actor = test_auxiliary_v14.AuxiliaryTests.actor
    candidate = test_auxiliary_v14.AuxiliaryTests.candidate
    queue = test_auxiliary_v14.AuxiliaryTests.queue
    reserve = test_auxiliary_v14.AuxiliaryTests.reserve

    def test_auxiliary_provider_bound_and_report_marked_third_party(self):
        with self.store.transaction() as db:
            proof=json.loads(self.store._member(db,'A')['proof']);proof['modelProvider']='apinebula'
            db.execute("UPDATE members SET proof=? WHERE id='A'",(json.dumps(proof),))
        self.store.allow_auxiliary_config(*self.actor(),'allowed-a','A','user allows third-party review')
        self.queue();ticket=self.reserve()
        with self.store.transaction() as db:
            reviewer=dict(db.execute('SELECT * FROM auxiliary_sessions WHERE id=?',(ticket['reviewer_id'],)).fetchone())
            Path(reviewer['cwd']).mkdir(parents=True,exist_ok=True)
            proof={'id':'00000000-0000-0000-0000-000000000099','cwd':reviewer['cwd'],
                   'model':reviewer['model'],'modelProvider':'openai'}
            with self.assertRaisesRegex(PairError,'Auxiliary identity mismatch'):
                self.store._begin_auxiliary_verified(db,ticket['execution_id'],ticket['lease'],proof,'unit-pool',{})
        with self.store.transaction() as db:
            proof['modelProvider']='apinebula'
            self.store._begin_auxiliary_verified(db,ticket['execution_id'],ticket['lease'],proof,'unit-pool',{})
            self.store._record_auxiliary_turn(db,ticket['execution_id'],ticket['lease'],'unit-turn')
            execution=self.store._auxiliary_execution(db,ticket['execution_id'],ticket['lease'])
            self.store._save_auxiliary_report(db,execution,{'verdict':'needs_validation'},{},{},{'formal_checks_verified':False})
            evidence=json.loads(db.execute('SELECT evidence FROM auxiliary_reports WHERE execution_id=?',(ticket['execution_id'],)).fetchone()[0])
        self.assertEqual(evidence['reviewer_provider'],'apinebula')
        self.assertEqual(evidence['review_service'],'third_party')


class ApinebulaSettlementTests(unittest.TestCase):
    setUp = test_final_receipt_settlement.FinalSettlementTests.setUp
    tearDown = test_final_receipt_settlement.FinalSettlementTests.tearDown
    allow = test_final_receipt_settlement.FinalSettlementTests.allow
    actor = test_final_receipt_settlement.FinalSettlementTests.actor
    observer = test_final_receipt_settlement.FinalSettlementTests.observer
    finish = test_final_receipt_settlement.FinalSettlementTests.finish

    def test_final_receipts_accept_actual_third_party_and_record_source(self):
        with self.store.transaction() as db:
            parent=json.loads(self.store._member(db,'A')['proof']);parent['modelProvider']='apinebula'
            db.execute("UPDATE members SET proof=? WHERE id='A'",(json.dumps(parent),))
            self.store._put(db,'reviewer_provider','apinebula')
            self.store._put(db,'managed_binding:'+self.permission['id'],
                {'session':self.sessions['A'],'version':self.store._member(db,'A')['binding_version'],
                 'home':self.store._member(db,'A')['codex_home'],'proof':json.dumps(parent)})
        self.proof['modelProvider']='apinebula'
        self.assertEqual(self.finish()['verdict'],'passed')
        with self.store.transaction() as db:
            row=db.execute('SELECT evidence FROM review_stages WHERE id=?',(self.stage['id'],)).fetchone()
            evidence=json.loads(row[0])
        self.assertEqual(evidence['reviewer_provider'],'apinebula')
        self.assertEqual(evidence['review_service'],'third_party')


class ApinebulaReceiptAccessTests(unittest.TestCase):
    setUp = test_review_receipt_access.ReviewReceiptAccessTests.setUp
    tearDown = test_review_receipt_access.ReviewReceiptAccessTests.tearDown
    observer = test_review_receipt_access.ReviewReceiptAccessTests.observer
    read = test_review_receipt_access.ReviewReceiptAccessTests.read

    def test_receipt_lookup_preserves_bound_third_party_provider(self):
        import sqlite3
        with closing(sqlite3.connect(self.path)) as db:
            parent=json.loads(db.execute("SELECT proof FROM members WHERE id='A'").fetchone()[0])
            parent['modelProvider']='apinebula'
            db.execute("UPDATE members SET proof=? WHERE id='A'",(json.dumps(parent),))
            db.execute('INSERT INTO pair_settings VALUES(?,?)',('reviewer_provider',json.dumps('apinebula')))
            db.commit()
        self.proof['modelProvider']='apinebula'
        self.assertEqual(len(self.read()['receipts']),1)
        self.proof['modelProvider']='openai'
        with self.assertRaisesRegex(ValueError,'thread mismatch'):self.read()


if __name__=='__main__':unittest.main()
