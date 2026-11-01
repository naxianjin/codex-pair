"""审核终态消息选择；合成消息保留实测phase结构，不执行真实工具。"""
import copy
import json
from pathlib import Path
import unittest

from scripts.pair_review_runtime import ReviewerController
from scripts.pair_core import PairError
from tests import test_final_receipt_settlement


class ReportSelectionTests(unittest.TestCase):
    def setUp(self):
        self.cwd=str(Path(__file__).resolve().parent)
        self.controller=ReviewerController(None);self.controller.state={'cwd':self.cwd}
        self.stage={'digest':'a'*64,'payload':json.dumps({'coordinator_required_checks':[{'name':'unit','command':'fixture check'}]})}
        self.report={'candidate_digest':'a'*64,'verdict':'passed','findings':[],
                     'checks':[{'name':'unit','status':'passed','required':True,'receipt_id':'receipt-1'}]}
        self.receipt={'type':'commandExecution','id':'receipt-1','command':'fixture check','cwd':self.cwd,'status':'completed','exitCode':0}

    def message(self,phase='final_answer',text=None):
        return {'type':'agentMessage','phase':phase,'text':json.dumps(self.report) if text is None else text}

    def parse(self,messages,**turn):
        return self.controller._report(dict({'status':'completed','items':[self.receipt,*messages]},**turn),self.stage)

    def test_real_five_message_phase_shape_selects_final(self):
        messages=[self.message('commentary','sanitized progress '+str(i)) for i in range(4)]
        messages.append(self.message())
        self.assertEqual(self.parse(messages)['verdict'],'passed')

    def test_known_partial_before_final_is_not_the_report(self):
        self.assertEqual(self.parse([self.message('partial_answer','work in progress'),self.message()])['verdict'],'passed')

    def test_commentary_json_cannot_override_final(self):
        final=dict(self.report,verdict='needs_fix')
        result=self.parse([self.message('commentary'),self.message(text=json.dumps(final))])
        self.assertEqual(result['verdict'],'needs_fix')

    def test_supported_fences_are_whole_final_message(self):
        for fence in ('```json','```'):
            with self.subTest(fence=fence):
                self.assertEqual(self.parse([self.message(text=fence+'\n'+json.dumps(self.report)+'\n```')])['verdict'],'passed')

    def test_missing_or_nonterminal_phase_never_becomes_final(self):
        for messages in ([],[self.message('commentary')],[self.message('partial_answer')],
                         [self.message('unknown')],[self.message('final')]):
            with self.subTest(messages=messages),self.assertRaises(ValueError):self.parse(messages)

    def test_multiple_final_even_identical_is_ambiguous(self):
        for second in (self.message(),self.message(text=json.dumps(dict(self.report,verdict='needs_fix')))):
            with self.subTest(second=second),self.assertRaisesRegex(ValueError,'ambiguous'):
                self.parse([self.message(),second])

    def test_output_after_final_and_mixed_unknown_phase_refused(self):
        for messages in ([self.message(),self.message('commentary','late')],
                         [self.message(None,'unknown'),self.message()],
                         [self.message('commentary','progress'),self.message(None)]):
            with self.subTest(messages=messages),self.assertRaises(ValueError):self.parse(messages)

    def test_legacy_single_unknown_phase_compatibility(self):
        for message in (self.message(None),{'type':'agentMessage','text':json.dumps(self.report)}):
            with self.subTest(message=message):self.assertEqual(self.parse([message])['verdict'],'passed')
        with self.assertRaises(ValueError):self.parse([self.message(None),self.message(None)])

    def test_malformed_empty_or_embedded_json_refused(self):
        for text in ('','   ','{','prefix '+json.dumps(self.report),json.dumps(self.report)+' trailing',
                     json.dumps(self.report)+json.dumps(self.report),'```json\n{}',
                     '```python\n'+json.dumps(self.report)+'\n```','```json\n'+json.dumps(self.report)+'\n```\ntrailing'):
            with self.subTest(text=text),self.assertRaises(ValueError):self.parse([self.message(text=text)])

    def test_duplicate_json_fields_and_invalid_structure_refused(self):
        text=json.dumps(self.report)
        for bad in ('[]','null','1',text[:-1]+',"verdict":"needs_fix"}',
                    json.dumps(dict(self.report,checks={})),json.dumps(dict(self.report,findings=[None])),
                    json.dumps(dict(self.report,verdict='unknown'))):
            with self.subTest(bad=bad),self.assertRaises(ValueError):self.parse([self.message(text=bad)])

    def test_async_or_nontext_final_refused(self):
        for change in ({'delivery':'async'},{'delivery':'unknown'},{'text':None},{'text':{}}):
            with self.subTest(change=change),self.assertRaises(ValueError):self.parse([dict(self.message(),**change)])

    def test_nonterminal_turn_with_final_never_passes(self):
        for status in ('inProgress','failed','interrupted',None):
            with self.subTest(status=status):self.assertEqual(self.parse([self.message()],status=status)['verdict'],'needs_validation')

    def test_exact_receipt_gates_still_apply(self):
        original=copy.deepcopy(self.receipt)
        for change in ({'id':'wrong'},{'command':'other'},{'cwd':str(Path(self.cwd).parent)},
                       {'exitCode':1},{'exitCode':False},{'status':'inProgress'}):
            with self.subTest(change=change):
                self.receipt=dict(original,**change)
                self.assertEqual(self.parse([self.message()])['verdict'],'needs_validation')
        self.receipt=original
        result=self.parse([self.message()],items=[original,dict(original,exitCode=1),self.message()])
        self.assertEqual(result['verdict'],'needs_validation')


class ReportSelectionFinalizationTests(unittest.TestCase):
    setUp=test_final_receipt_settlement.FinalSettlementTests.setUp
    tearDown=test_final_receipt_settlement.FinalSettlementTests.tearDown
    allow=test_final_receipt_settlement.FinalSettlementTests.allow
    actor=test_final_receipt_settlement.FinalSettlementTests.actor
    observer=test_final_receipt_settlement.FinalSettlementTests.observer
    finish=test_final_receipt_settlement.FinalSettlementTests.finish
    events=test_final_receipt_settlement.FinalSettlementTests.events
    permission_status=test_final_receipt_settlement.FinalSettlementTests.permission_status

    def phased_messages(self):
        return [{'type':'agentMessage','phase':'commentary','text':'isolated progress'},
                dict(self.message,phase='final_answer')]

    def test_verified_full_messages_and_receipts_finish(self):
        self.full['items']=[self.item,*self.phased_messages()]
        self.assertEqual(self.finish()['verdict'],'passed')
        self.assertEqual(self.permission_status(),'success')
        self.assertEqual(len(self.events('review_model_report')[0]['raw_messages']),2)

    def test_notification_fallback_preserves_phase_metadata(self):
        self.notification['items']=self.phased_messages();self.full['items']=[self.item]
        self.assertEqual(self.finish()['verdict'],'passed')

    def test_notification_commentary_json_cannot_be_promoted_by_fallback(self):
        self.notification['items']=[dict(self.message,phase='commentary')];self.full['items']=[self.item]
        with self.assertRaises(ValueError):self.finish()
        self.assertFalse(self.events('review_recorded'))

    def test_full_malformed_final_does_not_fall_back_to_valid_notification(self):
        self.full['items']=[self.item,dict(self.message,phase='final_answer',text='{')]
        with self.assertRaises(ValueError):self.finish()
        self.assertFalse(self.events('review_recorded'))

    def test_wrong_candidate_digest_still_rejected(self):
        report=json.loads(self.raw);report['candidate_digest']='wrong'
        self.full['items']=[self.item,dict(self.message,phase='final_answer',text=json.dumps(report))]
        with self.assertRaisesRegex(PairError,'Candidate changed'):self.finish()
        self.assertFalse(self.events('review_recorded'))

    def test_missing_required_report_fields_not_fabricated(self):
        report=json.loads(self.raw);report.pop('findings')
        self.full['items']=[self.item,dict(self.message,phase='final_answer',text=json.dumps(report))]
        with self.assertRaisesRegex(PairError,'Review evidence required'):self.finish()
        self.assertFalse(self.events('review_recorded'))

    def test_binding_change_during_read_still_rejected(self):
        self.full['items']=[self.item,*self.phased_messages()]
        def mutate():
            with self.store.transaction() as db:db.execute('UPDATE reviewer_runtime SET epoch=epoch+1')
        self.mutate=mutate
        with self.assertRaises(ValueError):self.finish()
        self.assertFalse(self.events('review_recorded'))




class StrictReportSelectionTests(unittest.TestCase):
    def setUp(self):
        from tests.test_strict_producer import ProducerLifecycleTests
        ProducerLifecycleTests.setUp(self)
        self.messages=[{'type':'agentMessage','phase':'commentary','text':'sanitized progress '+str(i)} for i in range(3)]
        self.messages.append(dict(self.f.message,phase='final_answer'))
        self.f.full['items']=self.f.full['items'][:1]+self.messages

    def test_runtime92_three_commentaries_and_exact_pair_pass(self):
        self.assertEqual(self.f.finish()['verdict'],'passed')
        self.assertEqual(self.f.permission_status(),'success')

    def test_real_shape_needs_validation_report_preserved(self):
        report=json.loads(self.f.raw);report['verdict']='needs_validation'
        self.f.full['items'][-1]['text']=json.dumps(report)
        self.assertEqual(self.f.finish()['verdict'],'needs_validation')

    def test_selected_report_cannot_bypass_missing_producer_without_permissions(self):
        from scripts.pair_producer import KEY
        self.f.controller.client=None
        with self.f.store.transaction() as db:
            db.execute('DELETE FROM pair_settings WHERE key=?',(KEY+self.f.lease['execution_id'],))
            db.execute('DELETE FROM managed_permissions')
        self.assertEqual(self.f.finish()['verdict'],'needs_validation')

    def test_selected_report_cannot_bypass_raw_only_receipts(self):
        self.f.full['items']=self.messages
        self.assertEqual(self.f.finish()['verdict'],'needs_validation')
        self.assertEqual(self.f.permission_status(),'unknown')

    def test_selected_report_cannot_bypass_api_only_receipts(self):
        self.f.rollout([])
        self.assertEqual(self.f.finish()['verdict'],'needs_validation')
        self.assertEqual(self.f.permission_status(),'unknown')

    def test_ambiguous_final_with_exact_pair_never_records_acceptance(self):
        self.f.full['items'].append(dict(self.messages[-1]))
        with self.assertRaisesRegex(ValueError,'ambiguous'):self.f.finish()
        self.assertFalse(self.f.events('review_recorded'))

    def test_strict_notification_fallback_keeps_commentary_phase(self):
        self.f.full['items']=self.f.full['items'][:1]
        self.f.notification['items']=[dict(self.f.message,phase='commentary')]
        with self.assertRaisesRegex(ValueError,'ambiguous'):self.f.finish()
        self.assertFalse(self.f.events('review_recorded'))

    def test_strict_wrong_digest_is_rejected(self):
        report=json.loads(self.f.raw);report['candidate_digest']='wrong'
        self.f.full['items'][-1]['text']=json.dumps(report)
        with self.assertRaisesRegex(PairError,'Candidate changed'):self.f.finish()
        self.assertFalse(self.f.events('review_recorded'))

if __name__=='__main__':unittest.main()
