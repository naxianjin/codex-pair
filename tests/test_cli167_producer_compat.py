"""准确 0.167 实录格式回归；夹具不替代生产者生命周期或正式宿主认证。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts import pair_producer as producer
from scripts import pair_review_receipts as receipts
from scripts import pair_transport as transport
from tests import test_strict_producer as lifecycle


VERSION = '0.167.0-alpha.3.2'


class FormatTests(unittest.TestCase):
    def setUp(self):
        sample = json.loads((Path(__file__).parent / 'fixtures/producer-cli167.json').read_bytes())
        self.api, self.raw = sample['api'], sample['raw']
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.api['cwd'] = directory.name
        self.raw['cwd'] = Path(directory.name).as_uri()
        self.context = producer.VerifiedProducer(VERSION, {}, 'format-fixture-only')

    def test_exact_observed_pair_uses_existing_forward_renderer(self):
        self.assertEqual(producer.strict_display(self.raw['command']), self.api['command'])
        merged = receipts.receipt_items([self.api], self.context, rollout_items=[self.raw])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]['command_argv'], self.raw['command'])
        self.assertTrue(transport.command_matches(self.api['command'], merged[0],
                                                 producer_version=self.context))

    def test_version_string_unknown_neighbor_and_single_source_rejected(self):
        for version in (VERSION, producer.VerifiedProducer(VERSION + '.1', {}, 'fixture'),
                        producer.VerifiedProducer('0.167.0', {}, 'fixture')):
            with self.subTest(version=version):
                self.assertFalse(producer.strict_version(version))
                self.assertEqual(receipts.receipt_items([self.api], version,
                                 rollout_items=[self.raw]), [])
        self.assertEqual(receipts.receipt_items([self.api], self.context, rollout_items=[]), [])
        self.assertEqual(receipts.receipt_items([], self.context, rollout_items=[self.raw]), [])

    def test_metadata_display_argv_and_source_conflicts_never_resurrect(self):
        self.assertEqual(receipts.receipt_items([dict(self.api, id='other')], self.context,
                         rollout_items=[self.raw]), [])
        for changes in ({'cwd': 'C:/different'}, {'source': 'agent'},
                        {'exitCode': 1}, {'status': 'failed'}, {'exitCode': False},
                        {'command': self.api['command'] + ' '}, {'command': '[REDACTED]'}):
            with self.subTest(changes=changes):
                changed = dict(self.api, **changes)
                self.assertEqual(receipts.receipt_items([self.api, changed], self.context,
                                 rollout_items=[self.raw]), [])
        changed = copy.deepcopy(self.raw)
        changed['command'][-1] += ' '
        self.assertEqual(receipts.receipt_items([self.api], self.context,
                         rollout_items=[changed]), [])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.case = lifecycle.ProducerLifecycleTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.launch['version'] = VERSION
        fixture = self.case.f
        with fixture.store.transaction() as db:
            db.execute('DELETE FROM pair_settings WHERE key=?',
                       (producer.KEY + fixture.lease['execution_id'],))
        self.case.record = producer.record_producer(fixture.store, fixture.stage['id'],
                                                   fixture.controller.state, self.case.launch)

    def test_bound_lifecycle_can_settle_exact_fixture(self):
        self.case.test_final_query_settlement_and_report_use_bound_producer()

    def test_changed_native_file_and_reused_pid_are_rejected(self):
        self.case.test_changed_file_or_process_rejects_before_settlement()

    def test_missing_start_and_final_evidence_are_rejected(self):
        self.case.test_missing_evidence_does_not_use_old_a_proof_either()

    def test_unfinished_producer_cannot_be_used_after_controller_loss(self):
        self.case.test_unfinished_evidence_cannot_be_used_after_controller_loss()

    def test_other_turn_and_build_cannot_rebind(self):
        self.case.test_record_cannot_rebind_another_turn_or_build()


if __name__ == '__main__':
    unittest.main()
