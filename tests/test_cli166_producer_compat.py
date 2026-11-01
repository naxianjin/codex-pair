"""CLI166隔离适配回归；复用164历史格式样本，不作为166实录证明。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from scripts import pair_producer as p
from scripts import pair_transport as t
from scripts import pair_review_receipts as access
from tests import test_strict_producer as original

NEW = '0.166.0-alpha.5.1'

class FormatTests(unittest.TestCase):
    def setUp(self):
        self.context = p.VerifiedProducer(NEW, {}, 'isolated-format-only')
        self.sample = json.loads((Path(__file__).parent/'fixtures/producer-cli164.json').read_text(encoding='utf-8'))

    def test_historical_sample_uses_forward_contract_under_synthetic_context(self):
        a,b=self.sample['api'],self.sample['raw']
        self.assertEqual(p.strict_display(b['command_argv']),a['command'])
        result=access.receipt_items([a],self.context,rollout_items=[b])
        self.assertEqual(len(result),1)
        self.assertTrue(t.command_matches(a['command'],result[0],producer_version=self.context))

    def test_bare_new_or_neighbor_version_never_enables_strict_receipts(self):
        a,b=self.sample['api'],self.sample['raw']
        for version in [NEW,'0.164.0','0.164.0-alpha.11','0.164.0-alpha.11.2','0.165.0-alpha.1.2','0.166.0-alpha.5.2']:
            value=version if version==NEW else p.VerifiedProducer(version,{},'fixture')
            self.assertFalse(p.strict_version(value))
            self.assertEqual(access.receipt_items([a],value,rollout_items=[b]),[])

    def test_single_source_wrong_display_or_argv_rejected(self):
        a,b=self.sample['api'],self.sample['raw']
        self.assertEqual(access.receipt_items([a],self.context,rollout_items=[]),[])
        self.assertEqual(access.receipt_items([],self.context,rollout_items=[b]),[])
        self.assertEqual(access.receipt_items([dict(a,command=a['command']+' ')],self.context,rollout_items=[b]),[])
        bad=copy.deepcopy(b);bad['command_argv'][-1]+=' '
        self.assertEqual(access.receipt_items([a],self.context,rollout_items=[bad]),[])

    def test_metadata_conflict_and_redaction_rejected(self):
        a,b=self.sample['api'],self.sample['raw']
        for change in [{'id':'other'},{'cwd':'C:/different'},{'source':'agent'},{'exitCode':1},{'command':'[REDACTED_SECRET]'}]:
            self.assertEqual(access.receipt_items([dict(a,**change)],self.context,rollout_items=[b]),[])

    def test_exact_launch_versions_and_unknown_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            exe=Path(d)/'codex.exe';exe.write_bytes(b'not executable fixture')
            for version in ['0.163.0',p.ALPHA,NEW]:
                with patch.object(p.subprocess,'run',return_value=Mock(returncode=0,stdout='codex-cli '+version+'\n')):
                    self.assertEqual(p.prepare_launch([str(exe)])['version'],version)
            for version in ['0.164.0','0.164.0-alpha.11','0.164.0-alpha.11.2',NEW+'junk']:
                with patch.object(p.subprocess,'run',return_value=Mock(returncode=0,stdout='codex-cli '+version)),self.assertRaises(ValueError):
                    p.prepare_launch([str(exe)])

    def test_launch_file_change_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            exe=Path(d)/'codex.exe';exe.write_bytes(b'before')
            def changed(*args,**kwargs):
                exe.write_bytes(b'after');return Mock(returncode=0,stdout='codex-cli '+NEW)
            with patch.object(p.subprocess,'run',side_effect=changed),self.assertRaises(ValueError):
                p.prepare_launch([str(exe)])

class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.case=original.ProducerLifecycleTests();self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.f=self.case.f
        self.case.launch['version']=NEW
        with self.f.store.transaction() as db:
            db.execute('DELETE FROM pair_settings WHERE key=?',(p.KEY+self.f.lease['execution_id'],))
        self.case.record=p.record_producer(self.f.store,self.f.stage['id'],self.f.controller.state,self.case.launch)

    def test_actual_final_query_settlement_and_report(self):
        self.case.test_final_query_settlement_and_report_use_bound_producer()

    def test_wrong_binding_all_fields_and_unknown_version(self):
        self.case.test_wrong_binding_fields_fail_closed()

    def test_changed_file_rejected(self):
        self.case.test_changed_file_or_process_rejects_before_settlement()

    def test_pid_reuse_rejected(self):
        self.case.test_pid_reuse_rejected()

    def test_missing_start_evidence_rejected(self):
        self.case.test_missing_evidence_does_not_use_old_a_proof_either()

    def test_missing_end_verification_rejected(self):
        self.case.test_unfinished_evidence_cannot_be_used_after_controller_loss()

    def test_other_turn_cannot_rebind(self):
        self.case.test_record_cannot_rebind_another_turn_or_build()

    def test_nonzero_cannot_pass(self):
        self.case.test_nonzero_exact_receipt_is_failed_not_success()

    def test_reading_changed_binding_rejected(self):
        self.case.test_evidence_change_during_read_rejects()

    def test_old_build_cannot_be_rebound_to_new(self):
        old=copy.deepcopy(self.case.launch);old['version']=p.ALPHA
        with self.assertRaises(ValueError):
            p.record_producer(self.f.store,self.f.stage['id'],self.f.controller.state,old)
