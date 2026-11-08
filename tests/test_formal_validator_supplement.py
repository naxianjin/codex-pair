import hashlib
import unittest

from scripts.pair_auxiliary_runtime import _context, saved_validator_inputs
from scripts.pair_auxiliary import _FORMAL_SUPPLEMENT_AUTHORITY
from scripts.pair_core import PairError
from scripts.pair_stages import digest
from scripts.pair_validator_admission import validator_admission_boundary
from tests import test_validator_admission


class FormalValidatorSupplementTests(unittest.TestCase):
    def setUp(self):
        self.base = test_validator_admission.ValidatorAdmissionTests()
        self.base.setUp()
        self.addCleanup(self.base.tearDown)
        self.store = self.base.store
        self.actor = self.base.fixture.actor()
        self.ticket = self.base.ticket
        self.request = self.base.admit()['request']
        self.base.verify('dispatch')
        empty = hashlib.sha256(b'').hexdigest()
        self.artifact = {'binding': self.request['binding'], 'files': self.request['files'],
            'formal_checks_verified': False, 'validators_succeeded': True,
            'results': [{'validator': name, 'exit_code': 0, 'stdout_sha256': empty,
                         'stderr_sha256': empty, 'stderr': ''}
                        for name in ('validate-findings.cjs', 'validate-coverage-ledger.cjs')]}
        self.native = {'ExitCode': 0, 'ActiveProcesses': 0, 'TimedOut': False}
        with validator_admission_boundary(self.store, *self.actor,
                self.ticket['execution_id'], self.ticket['lease'], run_id=self.base.run_id,
                tool_identity=self.base.tools, purpose='save'):
            self.store.record_validator_run(*self.actor, self.ticket['execution_id'],
                self.ticket['lease'], self.base.run_id, self.request, self.native,
                self.artifact, self.base.tools)

    def binding(self):
        prepared = saved_validator_inputs(self.store, *self.actor,
                                          self.ticket['execution_id'], self.ticket['lease'])
        with self.store.transaction() as db:
            execution = dict(self.store._auxiliary_execution(db, self.ticket['execution_id'], self.ticket['lease']))
            task = dict(self.store._auxiliary_task(db, execution['task_id']))
            candidate = dict(self.store._auxiliary_candidate(db, task['candidate_id']))
            report = db.execute('SELECT * FROM auxiliary_reports WHERE execution_id=?',
                                (self.ticket['execution_id'],)).fetchone()
            admission = self.store._option(db, 'validator_admission:' + self.base.run_id)
            return {
                'execution_id': self.ticket['execution_id'],
                'candidate_digest': candidate['candidate_digest'],
                'contract_digest': candidate['contract_digest'],
                'report_digest': report['report_digest'],
                'stored_report_digest': prepared['stored_report_digest'],
                'adapted_report_digest': prepared['adapted_report_digest'],
                'context_digest': digest(prepared['context']),
                'session_uuid': execution['session_uuid'],
                'execution_epoch': execution['epoch'],
                'control_generation': admission['snapshot']['control_generation'],
                'authorization_version': admission['snapshot']['authorization_version'],
                'admission_snapshot_digest': admission['snapshot_digest'],
            }

    def supplement(self, authority=None, **changes):
        binding = self.binding()
        receipt = {'source': 'official_host_store', 'run_id': self.base.run_id,
                   'host_identity_verified': True, 'native_start_verified': True,
                   'official_receipt_verified': True, 'binding': binding}
        content = {'source': 'official_content_store',
                   'system_image_equivalence_verified': True,
                   'dynamic_content_verified': True, 'jit_content_verified': True,
                   'binding': binding}
        receipt.update(changes.pop('receipt', {}))
        content.update(changes.pop('content', {}))
        return self.store.record_formal_validator_supplement(*self.actor,
            self.ticket['execution_id'], self.ticket['lease'], run_id=self.base.run_id,
            receipt=receipt, content=content, tool_identity=self.base.tools,
            authority=authority)

    def test_missing_formal_source_stays_blocked_and_is_idempotent(self):
        result = self.supplement(content={'jit_content_verified': False})
        self.assertFalse(result['formal_checks_verified'])
        self.assertEqual(result['status'], 'needs_validation')
        self.assertTrue(self.supplement(content={'jit_content_verified': False})['duplicate'])

    def test_json_shaped_all_true_claim_without_authority_stays_blocked(self):
        result = self.supplement()
        self.assertFalse(result['formal_checks_verified'])
        self.assertEqual(result['status'], 'needs_validation')

    def test_complete_formal_record_is_saved_without_mutating_report(self):
        before = self.store.settle_audit(*self.actor, self.base.candidate['id'])
        self.assertEqual(before['verdict'], 'needs_validation')
        result = self.supplement(authority=_FORMAL_SUPPLEMENT_AUTHORITY)
        self.assertTrue(result['formal_checks_verified'])
        with self.store.transaction() as db:
            row = db.execute('SELECT raw_report, evidence FROM auxiliary_reports WHERE execution_id=?',
                             (self.ticket['execution_id'],)).fetchone()
            saved = self.store._option(db, 'validator_supplement:' + self.base.run_id)
        self.assertIsNotNone(saved)
        self.assertIsNotNone(row['raw_report'])
        self.assertEqual(self.store.settle_audit(*self.actor, self.base.candidate['id'])['verdict'],
                         'needs_validation')

    def test_binding_change_conflicts_and_does_not_overwrite(self):
        self.supplement()
        with self.assertRaisesRegex(PairError, 'conflict'):
            self.supplement(content={'jit_content_verified': False})


if __name__ == '__main__':
    unittest.main()
