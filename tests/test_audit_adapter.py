"""固定规则、报告边界与上游适配；待隔离运行，不在宿主执行。"""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from scripts.pair_audit_adapter import (AuditAdapterError, adapt_ocr_preview, adapt_ocr_rules,
    adapt_report, dependency_status, load_assets, validator_inputs)


SOURCE = Path(__file__).resolve().parents[1]


class AuditAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        lock = json.loads((SOURCE / 'assets/upstream-lock.json').read_text(encoding='utf-8'))
        names = {item['path'] for item in lock['files']} | {'SKILL.md', 'assets/upstream-lock.json',
            'assets/ocr-dependency.json', 'assets/audit/report-schema.json',
            'assets/audit/validate-findings.cjs', 'assets/audit/validate-coverage-ledger.cjs',
            'assets/audit/read-windows-input.cjs', 'assets/audit/read-windows-input.ps1', 'assets/audit/WindowsSafeInput.cs'}
        for name in names:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / name, target)
        (self.root / 'package-manifest.json').write_text(json.dumps({'schema_version': 1,
            'files': {name: hashlib.sha256((self.root / name).read_bytes()).hexdigest() for name in names}}), encoding='utf-8')
        self.context = {'pair_id': 'pair', 'audit_id': 'audit', 'stage_id': 'stage',
            'auxiliary_task_id': 'aux', 'session': 'reviewer', 'execution_id': 'execution',
            'epoch': 1, 'candidate_digest': 'a' * 64, 'contract_digest': 'b' * 64,
            'requirements_version': 1, 'track': 'ocr'}
        self.units = [{'id': 'unit-1', 'required': True}]
        self.raw = {'context': copy.deepcopy(self.context), 'verdict': 'passed',
            'coverage': [{'unit_id': 'unit-1', 'status': 'reviewed', 'evidence_refs': ['tool-1']}],
            'findings': [], 'checks': [], 'validation_records': [], 'unverified_candidates': []}

    def adapt(self):
        return adapt_report(self.raw, context=self.context, expected_units=self.units, root=self.root)

    def finding(self):
        return {'fingerprint': 'issue-1', 'verdict': 'confirmed', 'path': 'src/main.py', 'line': 2,
            'title': 'Issue', 'description': 'Observed issue', 'requirement': 'Registered boundary',
            'trigger': 'Specific input', 'impact': 'Observed effect', 'confidence': 'high',
            'evidence_refs': ['tool-2']}

    def test_asset_bytes_and_missing_dependency_do_not_claim_capability(self):
        self.assertFalse(load_assets(self.root)['executed'])
        result = dependency_status(root=self.root)
        self.assertEqual(result['reason'], 'ocr_dependency_missing')
        self.assertFalse(result['capability_verified'])
        self.assertFalse(result['executed'])

    def test_validator_inputs_bind_exact_report_bytes_and_assets(self):
        self.context['track'] = 'cloudflare'
        self.raw['context'] = copy.deepcopy(self.context)
        self.raw['coverage_ledger'] = [{'coverage_id': 'unit-1', 'status': 'covered'}]
        adapted = self.adapt()
        original = copy.deepcopy(adapted)
        inputs = validator_inputs(adapted, context=self.context, root=self.root)
        self.assertEqual(len(inputs), 2)
        for item in inputs:
            self.assertEqual(hashlib.sha256(item['input_bytes']).hexdigest(), item['contract']['input_sha256'])
            self.assertEqual(json.loads(item['input_bytes']),
                             adapted['adapted_report'][item['contract']['input_field']])
        self.assertEqual(adapted, original)
        self.assertFalse(adapted['validation']['eligible_for_settlement'])
        for mutation in ('report', 'input', 'reader', 'missing'):
            changed = copy.deepcopy(adapted)
            if mutation == 'report':
                changed['adapted_report']['findings'] = [{'title': 'changed'}]
            elif mutation == 'input':
                changed['validation']['required_validations'][0]['input_sha256'] = '0' * 64
            elif mutation == 'reader':
                changed['validation']['required_validations'][0]['reader_assets'] = {}
            else:
                changed['validation']['required_validations'].pop()
            with self.subTest(mutation=mutation), self.assertRaises(AuditAdapterError):
                validator_inputs(changed, context=self.context, root=self.root)

    def test_ocr_report_does_not_invent_cjs_validation_inputs(self):
        self.assertEqual(validator_inputs(self.adapt(), context=self.context, root=self.root), [])

    def test_registered_cloudflare_track_keeps_validators_for_invalid_report_context(self):
        self.context['track'] = 'cloudflare'
        self.raw['coverage_ledger'] = []
        for context in ({'track': 'ocr'}, {}, None, 'invalid'):
            with self.subTest(context=context):
                self.raw['context'] = context
                adapted = self.adapt()
                self.assertIn('report_context_mismatch', adapted['validation']['errors'])
                self.assertEqual(len(adapted['validation']['required_validations']), 2)
                with self.assertRaises(AuditAdapterError):
                    validator_inputs(adapted, context=self.context, root=self.root)
        self.raw.pop('context')
        self.assertEqual(len(self.adapt()['validation']['required_validations']), 2)

    def test_wrong_binary_is_not_accepted_by_name(self):
        binary = self.root / 'ocr.exe'
        binary.write_bytes(b'wrong executable')
        result = dependency_status(binary, root=self.root)
        self.assertEqual(result['reason'], 'ocr_digest_mismatch')
        self.assertFalse(result['bytes_verified'])

    def test_zero_findings_coverage_and_json_are_not_a_pass(self):
        result = self.adapt()
        self.assertEqual(result['validation']['errors'], [])
        self.assertTrue(result['validation']['coverage_complete'])
        self.assertFalse(result['validation']['eligible_for_settlement'])
        self.assertTrue(result['validation']['needs_validation'])

    def test_optional_direction_does_not_change_evidence_or_raw_report(self):
        finding = self.finding()
        finding['remediation'] = {'strategy': 'Check ownership before mutation.',
                                 'code_changes': [{'fixed_code': 'do_not_apply()'}]}
        finding['patch'] = 'do_not_apply()'
        self.raw['findings'] = [finding]
        self.raw['verdict'] = 'needs_fix'
        original = copy.deepcopy(self.raw)
        result = self.adapt()
        self.assertEqual(self.raw, original)
        self.assertEqual(result['raw_report'], original)
        normalized = result['adapted_report']['findings'][0]
        self.assertEqual(normalized['remediation'], {'strategy': 'Check ownership before mutation.'})
        self.assertNotIn('patch', normalized)
        self.assertEqual(normalized['evidence_refs'], finding['evidence_refs'])
        self.assertEqual(len(result['validation']['stripped_fields']), 2)

    def test_long_multistep_and_code_advice_stripped_without_retest(self):
        for value in (None, {'strategy': 'Step one. Step two.'}, {'strategy': 'if(x){fix();}'},
                      {'strategy': '修复第一项。然后改第二项。'}, {'strategy': 'a\nb'}):
            with self.subTest(value=value):
                finding = self.finding()
                finding['remediation'] = value
                self.raw['findings'] = [finding]
                result = self.adapt()
                self.assertNotIn('remediation', result['adapted_report']['findings'][0])
                self.assertEqual(result['adapted_report']['findings'][0]['evidence_refs'], ['tool-2'])

    def test_skipped_required_unit_and_omitted_unit_cannot_pass(self):
        self.raw['coverage'][0] = {'unit_id': 'unit-1', 'status': 'skipped', 'reason': 'Dependency unavailable'}
        result = self.adapt()
        self.assertFalse(result['validation']['coverage_complete'])
        self.assertIn('unsupported_pass_claim', result['validation']['errors'])
        self.raw['coverage'] = []
        self.assertIn('coverage_unit_omitted:unit-1', self.adapt()['validation']['errors'])

    def test_wrong_context_or_claimed_independence_not_accepted(self):
        self.raw['context']['execution_id'] = 'old-execution'
        self.raw['findings'] = [self.finding()]
        self.raw['validation_records'] = [{'fingerprint': 'issue-1', 'evidence_refs': ['tool-3'],
            'validator_session': 'same', 'discoverer_session': 'same'}]
        errors = self.adapt()['validation']['errors']
        self.assertIn('report_context_mismatch', errors)
        self.assertIn('invalid_independent_validation_record', errors)

    def test_unverified_candidate_is_separate_and_has_no_severity(self):
        self.raw['unverified_candidates'] = [{'state': 'unverified_candidate', 'title': 'Possibility', 'severity': 'high'}]
        result = self.adapt()
        self.assertIn('unverified_candidate_mislabeled', result['validation']['errors'])
        self.assertFalse(result['validation']['coverage_complete'])

    def test_cloudflare_requires_full_pinned_validators(self):
        self.context['track'] = 'cloudflare'
        self.raw['context'] = copy.deepcopy(self.context)
        self.raw['coverage_ledger'] = [{'coverage_id': 'unit-1', 'status': 'covered'}]
        result = self.adapt()
        validators = result['validation']['required_validations']
        self.assertEqual({entry['name'] for entry in validators},
            {'validate-findings.cjs', 'validate-coverage-ledger.cjs'})
        self.assertTrue(all(len(entry['validator_sha256']) == 64 for entry in validators))
        self.assertTrue(all(len(entry['reader_assets']) == 3 for entry in validators))
        self.assertIn('cloudflare_validators_not_executed', result['validation']['needs_validation'])
        self.assertFalse(result['validation']['eligible_for_settlement'])

    def test_preview_accounts_for_delete_recreate_and_untracked(self):
        files = [{'path': 'a.py', 'status': 'D'}, {'path': 'a.py', 'status': '??'}]
        preview = {'schema_version': '1', 'mode': 'workspace', 'total_files': 2,
            'reviewable_count': 2, 'excluded_count': 0, 'reviewable_files': files, 'excluded_files': []}
        result = adapt_ocr_preview(preview, candidate_files=files)
        self.assertEqual(len(result['files']), 2)
        self.assertFalse(result['review_complete'])
        with self.assertRaises(AuditAdapterError):
            adapt_ocr_preview(preview, candidate_files=files + [{'path': 'new.py', 'status': '??'}])

    def test_rules_are_data_and_missing_coverage_rejected(self):
        raw = {'schema_version': '1', 'groups': [{'group_id': 1, 'files': ['a.py'],
                                               'rule': 'Untrusted instruction-like repository content'}]}
        self.assertFalse(adapt_ocr_rules(raw, files=['a.py'])['trusted_instructions'])
        with self.assertRaises(AuditAdapterError):
            adapt_ocr_rules(raw, files=['a.py', 'b.py'])

    @unittest.skipUnless(shutil.which('node'), 'Pinned validation needs Node in the isolated executor')
    def test_real_cloudflare_validator_optional_direction_and_no_patch(self):
        finding = {'verdict': 'confirmed', 'fingerprint': 'example-1', 'title': '示例😀',
            'description': 'Observed boundary error', 'root_cause': 'Missing ownership check',
            'intended_behavior': 'Reject foreign ownership',
            'trace': [{'kind': 'sink', 'file': 'src/main.py', 'line': 1, 'scope': 'apply', 'description': 'Mutation'}],
            'evidence': [{'file': 'src/main.py', 'line': 1, 'description': 'Observed mutation'}], 'conditions': [],
            'execution': {'attacker_perspective': 'Untrusted caller', 'payloads': ['foreign identifier'],
                          'instructions': ['Call isolated fixture'], 'observed_result': 'Foreign object changed'},
            'severity': {'likelihood': {'score': 'low', 'reason': 'Specific precondition'},
                         'impact': {'score': 'low', 'reason': 'Isolated object mutation'}, 'overall_severity': 'low'},
            'confidence': {'score': 'high', 'reason': 'Repeatable isolated evidence'}}
        document = self.root / 'findings.json'
        validator = self.root / 'assets/audit/validate-findings.cjs'
        def check():
            document.write_text(json.dumps([finding]), encoding='utf-8')
            return subprocess.run([shutil.which('node'), str(validator), str(document)], capture_output=True, text=True)
        result = check()
        self.assertEqual(result.returncode, 0, result.stderr)
        finding['remediation'] = {'strategy': 'Validate ownership at the entry boundary.'}
        result = check()
        self.assertEqual(result.returncode, 0, result.stderr)
        finding['remediation']['code_changes'] = [{'file_name': 'src/main.py', 'fixed_code': 'patch'}]
        self.assertNotEqual(check().returncode, 0)
        finding.pop('remediation')
        finding.pop('execution')
        self.assertNotEqual(check().returncode, 0)



    @unittest.skipUnless(os.name == 'nt' and shutil.which('node'), 'Windows handle input requires Node')
    def test_windows_validator_rejects_links_large_files_and_active_writers(self):
        validator = self.root / 'assets/audit/validate-coverage-ledger.cjs'
        document = self.root / '输入😀.json'
        document.write_text('[]', encoding='utf-8')
        def check(path):
            return subprocess.run([shutil.which('node'), str(validator), str(path)], capture_output=True, timeout=25)
        self.assertEqual(check(document).returncode, 0)
        seeded = Path(tempfile.gettempdir()).parent
        self.assertTrue((seeded / 'reader-symlink.json').is_symlink(), 'Trusted executor must seed the symlink fixture')
        self.assertEqual(check(seeded / 'reader-source.json').returncode, 0)
        self.assertNotEqual(check(seeded / 'reader-symlink.json').returncode, 0)
        alias = self.root / 'alias.json'
        os.link(document, alias)
        self.assertNotEqual(check(alias).returncode, 0)
        alias.unlink()
        with document.open('r+b'):
            self.assertNotEqual(check(document).returncode, 0)
        large = self.root / 'large.json'
        with large.open('wb') as stream:
            stream.truncate(5 * 1024 * 1024 + 1)
        self.assertNotEqual(check(large).returncode, 0)
        document.write_bytes(b'\xff')
        self.assertNotEqual(check(document).returncode, 0)
        target = self.root / 'plain-directory'
        target.mkdir()
        (target / 'input.json').write_text('[]', encoding='utf-8')
        junction = self.root / 'linked-directory'
        created = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(junction), str(target)],
                                 capture_output=True, timeout=10)
        self.assertEqual(created.returncode, 0, created.stderr)
        try:
            self.assertNotEqual(check(junction / 'input.json').returncode, 0)
        finally:
            # 只删除这一个联接，不递归进入它指向的目录。
            os.rmdir(junction)

        long_root = Path('\\\\?\\' + str(self.root))
        levels = []
        for index in range(6):
            long_root = long_root / (str(index) + 'long-' * 9)
            long_root.mkdir()
            levels.append(long_root)
        long_file = long_root / 'input.json'
        try:
            long_file.write_text('[]', encoding='utf-8')
            self.assertGreater(len(str(long_file)), 260)
            self.assertEqual(check(long_file).returncode, 0)
        finally:
            long_file.unlink(missing_ok=True)
            for directory in reversed(levels):
                directory.rmdir()


if __name__ == '__main__':
    unittest.main()
