"""固定审查资产与报告适配；不启动模型、OCR、Node、被审代码或子进程。"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

try:
    from .install_skill import checked_path, package_manifest
except ImportError:
    from install_skill import checked_path, package_manifest


ADAPTER_VERSION = 'codex-pair-audit/1'
UPSTREAM_COMMITS = {
    'alibaba/open-code-review': '216dcf513593055a70165f793e96b52589d71b1b',
    'cloudflare/security-audit-skill': 'c1c8a8c1471069fb0e188eeaff69b8e8db6564a8',
}
CLOUDFLARE_ASSETS = ('WORKFLOW.md', 'RECONNAISSANCE.md', 'ATTACK-CLASSES.md', 'HUNTING.md',
    'VALIDATION-AND-REPORTING.md', 'AI-AND-LLM.md', 'CLIENT-SIDE.md', 'CLOUD-AND-DEPLOYMENT.md',
    'DATA-ISOLATION-AND-LIFECYCLE.md', 'DESKTOP-MOBILE-AND-LOCAL-IPC.md',
    'MEMORY-SAFETY-AND-BINARY.md', 'PROTOCOLS-RPC-AND-MESSAGING.md',
    'RESOURCE-EXHAUSTION-AND-AVAILABILITY.md', 'SUPPLY-CHAIN-AND-RELEASE.md',
    'WEB-PROTOCOL-AND-AUTH.md', 'report-schema.json', 'validate-findings.cjs',
    'validate-coverage-ledger.cjs')
CONTEXT_FIELDS = ('pair_id', 'audit_id', 'stage_id', 'auxiliary_task_id', 'session',
                  'execution_id', 'epoch', 'candidate_digest', 'contract_digest',
                  'requirements_version', 'track')
PATCH_FIELDS = {'patch', 'diff', 'fixed_code', 'code_changes', 'fix_code', 'implementation_steps'}
MAX_REPORT_BYTES = 5 * 1024 * 1024
WINDOWS_INPUT_ASSETS = ('assets/audit/read-windows-input.cjs',
    'assets/audit/read-windows-input.ps1', 'assets/audit/WindowsSafeInput.cs')


class AuditAdapterError(ValueError):
    pass


def load_assets(root=None):
    """校验发行清单及原始上游摘要，不以文件存在证明工具已运行。"""
    root = Path(root) if root is not None else Path(__file__).resolve().parents[1]
    manifest = package_manifest(root)
    if not set(WINDOWS_INPUT_ASSETS) <= manifest.keys():
        raise AuditAdapterError('Windows input reader assets missing / Windows安全读取资产不完整')
    lock = json.loads((root / 'assets/upstream-lock.json').read_text(encoding='utf-8'))
    if lock.get('schema_version') != 1 or lock.get('adapter_version') != ADAPTER_VERSION:
        raise AuditAdapterError('Audit asset version mismatch / 审查资产版本不符')
    seen = set()
    for item in lock.get('files', []):
        if (item.get('source') not in UPSTREAM_COMMITS
                or item.get('commit') != UPSTREAM_COMMITS[item['source']]
                or item.get('path') in seen
                or manifest.get(item.get('path')) != item.get('sha256')):
            raise AuditAdapterError('Upstream asset digest mismatch / 上游资产来源或摘要不符')
        seen.add(item['path'])
    required = {'assets/upstream/ocr/WORKFLOW.md', 'assets/upstream/ocr/system_rules.json',
                'licenses/open-code-review-Apache-2.0.txt', 'licenses/cloudflare-security-audit-MIT.txt'}
    required.update('assets/upstream/cloudflare/' + name for name in CLOUDFLARE_ASSETS)
    if not required <= seen:
        raise AuditAdapterError('Upstream assets incomplete / 必需上游资产未列入锁定清单')
    return {'root': str(root), 'manifest': manifest, 'upstreams': lock,
            'dependency': json.loads((root / 'assets/ocr-dependency.json').read_text(encoding='utf-8')),
            'adapter_version': ADAPTER_VERSION, 'executed': False}


def dependency_status(ocr_executable=None, *, platform_name='windows-amd64', root=None):
    assets = load_assets(root)
    dependency = assets['dependency']
    expected = dependency['platforms'].get(platform_name)
    result = {'dependency': 'ocr', 'version': dependency['version'],
              'source_commit': dependency['source_commit'], 'platform': platform_name,
              'installed': False, 'bytes_verified': False, 'executed': False,
              'capability_verified': False, 'needs_validation': ['isolated_delegate_probe_required']}
    if expected is None:
        return {**result, 'reason': 'unsupported_platform'}
    if ocr_executable is None:
        return {**result, 'reason': 'ocr_dependency_missing'}
    path = checked_path(ocr_executable)
    if not path.is_file():
        return {**result, 'reason': 'ocr_dependency_missing', 'path': str(path)}
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {**result, 'installed': True, 'path': str(path), 'sha256': digest,
            'bytes_verified': digest == expected['sha256'],
            'reason': 'isolated_probe_required' if digest == expected['sha256'] else 'ocr_digest_mismatch'}


def ocr_command_plan(executable, repository, *, candidate_files, from_ref=None, to_ref=None,
                     commit=None, platform_name='windows-amd64', root=None):
    """只生成参数数组；依赖不符时不降级、不自动下载或改OCR配置。"""
    dependency = dependency_status(executable, platform_name=platform_name, root=root)
    if not dependency['bytes_verified']:
        return {'ready': False, 'dependency': dependency, 'commands': []}
    if not Path(repository).is_absolute():
        raise AuditAdapterError('Absolute repository required / 项目必须使用绝对路径')
    if bool(from_ref) != bool(to_ref) or commit and (from_ref or to_ref):
        raise AuditAdapterError('Invalid OCR refs / 范围与提交模式不能混用')
    refs = []
    for option, value in (('--from', from_ref), ('--to', to_ref), ('--commit', commit)):
        if value is not None:
            if not isinstance(value, str) or not value or value.startswith('-') or any(ord(c) < 32 for c in value):
                raise AuditAdapterError('Invalid OCR ref / 无效Git引用')
            refs.extend((option, value))
    paths = list(dict.fromkeys(_source_path(item['path']) for item in candidate_files))
    common = ['--format', 'json', '--repo', str(repository)]
    commands = [[dependency['path'], 'delegate', 'preview', *common, *refs]]
    if paths:
        commands.append([dependency['path'], 'delegate', 'rule', *common, *refs, '--', *paths])
    return {'ready': True, 'dependency': dependency, 'commands': commands,
            'requires_isolated_executor': True, 'executed': False,
            'rule_content_is_untrusted_data': True, 'llm_configuration_required': False}


def adapt_ocr_preview(raw, *, candidate_files):
    """清单身份为(path,status)，保留同路径删除后新建的两个条目。"""
    _json_digest(raw)
    if not isinstance(raw, dict) or raw.get('schema_version') != '1' or raw.get('mode') not in ('workspace', 'range', 'commit'):
        raise AuditAdapterError('Unsupported OCR preview / 要求固定JSON协议schema_version=1')
    if raw['mode'] == 'range' and not all(raw.get(key) for key in ('from', 'to', 'merge_base')):
        raise AuditAdapterError('Missing OCR range refs / 范围引用不完整')
    if raw['mode'] == 'commit' and not raw.get('commit'):
        raise AuditAdapterError('Missing OCR commit / 提交引用缺失')
    expected = {_file_identity(item): item for item in candidate_files}
    if len(expected) != len(candidate_files):
        raise AuditAdapterError('Duplicate candidate identity / 固定候选文件身份重复')
    entries = []
    seen = set()
    for key, disposition in (('reviewable_files', 'pending'), ('excluded_files', 'skipped')):
        if not isinstance(raw.get(key), list):
            raise AuditAdapterError('Invalid OCR files / 文件列表缺失')
        for item in raw[key]:
            identity = _file_identity(item)
            if identity in seen or identity not in expected:
                raise AuditAdapterError('OCR candidate mismatch / 预览包含重复或非固定候选文件')
            if disposition == 'skipped' and not _text(item.get('exclude_reason')):
                raise AuditAdapterError('OCR exclusion reason missing / 排除文件必须有理由')
            seen.add(identity)
            entries.append({**copy.deepcopy(item), 'review_status': disposition,
                            'candidate_sha256': expected[identity].get('sha256')})
    if seen != expected.keys():
        raise AuditAdapterError('OCR omitted candidate files / 固定候选文件未完整列入预览')
    counts = {'total_files': len(entries), 'reviewable_count': len(raw['reviewable_files']),
              'excluded_count': len(raw['excluded_files'])}
    if any(type(raw.get(key)) is not int or raw[key] != value for key, value in counts.items()):
        raise AuditAdapterError('OCR count mismatch / 预览数量与条目不一致')
    return {'raw_preview': copy.deepcopy(raw), 'files': entries, 'counts': counts,
            'review_complete': False, 'adapter_version': ADAPTER_VERSION}


def adapt_ocr_rules(raw, *, files):
    _json_digest(raw)
    if not isinstance(raw, dict) or raw.get('schema_version') != '1' or not isinstance(raw.get('groups'), list):
        raise AuditAdapterError('Unsupported OCR rules / 要求固定JSON规则协议')
    expected = {_source_path(path) for path in files}
    seen, groups = set(), set()
    for group in raw['groups']:
        if (not isinstance(group, dict) or type(group.get('group_id')) is not int
                or group['group_id'] in groups or not isinstance(group.get('files'), list)
                or not _text(group.get('rule'))):
            raise AuditAdapterError('Invalid OCR rule group / 规则分组无效')
        groups.add(group['group_id'])
        for path in group['files']:
            path = _source_path(path)
            if path in seen or path not in expected:
                raise AuditAdapterError('OCR rule scope mismatch / 规则文件重复或超范围')
            seen.add(path)
    if seen != expected:
        raise AuditAdapterError('OCR rules incomplete / 有文件未取得规则')
    return {'raw_rules': copy.deepcopy(raw), 'groups': copy.deepcopy(raw['groups']),
            'trusted_instructions': False, 'adapter_version': ADAPTER_VERSION}


def adapt_report(raw, *, context, expected_units, root=None):
    """接收报告数据；完整CJS验证和实际回执由受信结算入口负责。"""
    raw_digest = _json_digest(raw)
    if not isinstance(raw, dict):
        raise AuditAdapterError('Report object required / 报告必须为对象')
    if (not isinstance(context, dict) or any(key not in context for key in CONTEXT_FIELDS)
            or context.get('track') not in ('ocr', 'cloudflare')):
        raise AuditAdapterError('Incomplete audit context / 缺少固定审核身份和版本')
    assets = load_assets(root)
    report = copy.deepcopy(raw)
    errors, stripped, gaps, validators = [], [], [], []
    if _json_digest(report.get('context')) != _json_digest(context):
        errors.append('report_context_mismatch')
    if report.get('verdict') not in ('passed', 'needs_fix', 'needs_validation'):
        errors.append('invalid_report_verdict')
    units = {}
    for item in expected_units:
        if not isinstance(item, dict) or not _text(item.get('id')) or item['id'] in units or type(item.get('required', True)) is not bool:
            raise AuditAdapterError('Invalid registered coverage units / 登记覆盖单元无效')
        units[item['id']] = item
    coverage = report.get('coverage')
    seen = set()
    if not isinstance(coverage, list):
        coverage = []
        errors.append('coverage_missing')
    for item in coverage:
        if not isinstance(item, dict) or item.get('unit_id') not in units or item.get('unit_id') in seen:
            errors.append('coverage_scope_mismatch')
            continue
        seen.add(item['unit_id'])
        status = item.get('status')
        if status not in ('reviewed', 'covered', 'skipped', 'out_of_scope', 'needs_validation'):
            errors.append('invalid_coverage_status:' + item['unit_id'])
        if status in ('reviewed', 'covered'):
            if not _strings(item.get('evidence_refs')):
                gaps.append('coverage_evidence_missing:' + item['unit_id'])
        else:
            if not _text(item.get('reason')):
                errors.append('coverage_reason_missing:' + item['unit_id'])
            if units[item['unit_id']].get('required', True):
                gaps.append('required_coverage_incomplete:' + item['unit_id'])
    for key in units.keys() - seen:
        errors.append('coverage_unit_omitted:' + key)
    findings = report.get('findings')
    if not isinstance(findings, list):
        findings = []
        errors.append('findings_missing')
    fingerprints = set()
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            errors.append('invalid_finding:' + str(index))
            continue
        _strip_advice(finding, index, stripped)
        fingerprint = finding.get('fingerprint')
        verdict = finding.get('verdict')
        if not _text(fingerprint) or fingerprint in fingerprints:
            errors.append('invalid_or_duplicate_fingerprint:' + str(index))
        else:
            fingerprints.add(fingerprint)
        if verdict not in ('confirmed', 'needs_validation', 'rejected'):
            errors.append('invalid_finding_verdict:' + str(index))
        if verdict == 'needs_validation':
            gaps.append('finding_needs_validation:' + str(fingerprint))
            if 'severity' in finding:
                errors.append('unverified_severity:' + str(fingerprint))
        if context['track'] == 'ocr':
            required = ('title', 'description', 'requirement', 'trigger', 'impact', 'confidence')
            if any(not _text(finding.get(key)) for key in required) or not _strings(finding.get('evidence_refs')):
                errors.append('ocr_finding_evidence_incomplete:' + str(fingerprint))
            try:
                _source_path(finding.get('path'))
            except AuditAdapterError:
                errors.append('ocr_finding_path_invalid:' + str(fingerprint))
            if type(finding.get('line')) is not int or finding['line'] < 1:
                errors.append('ocr_finding_line_invalid:' + str(fingerprint))
    candidates = report.get('unverified_candidates', [])
    if not isinstance(candidates, list):
        errors.append('invalid_unverified_candidates')
    elif candidates:
        gaps.append('unverified_candidates_pending')
        for item in candidates:
            if not isinstance(item, dict) or item.get('state') != 'unverified_candidate' or 'severity' in item:
                errors.append('unverified_candidate_mislabeled')
    records = report.get('validation_records', [])
    if not isinstance(records, list):
        records = []
        errors.append('invalid_validation_records')
    validated = set()
    for item in records:
        if (not isinstance(item, dict) or item.get('fingerprint') not in fingerprints
                or item.get('fingerprint') in validated or not _strings(item.get('evidence_refs'))
                or not _text(item.get('validator_session')) or not _text(item.get('discoverer_session'))
                or item['validator_session'] == item['discoverer_session']):
            errors.append('invalid_independent_validation_record')
        else:
            validated.add(item['fingerprint'])
    if fingerprints - validated:
        gaps.append('independent_finding_validation_missing')
    checks = report.get('checks')
    if not isinstance(checks, list):
        errors.append('checks_missing')
    else:
        for check in checks:
            if not isinstance(check, dict) or not _text(check.get('name')) or check.get('status') not in ('passed', 'failed', 'needs_validation'):
                errors.append('invalid_check')
            elif not _strings(check.get('evidence_refs')) or check['status'] == 'needs_validation':
                gaps.append('check_evidence_pending:' + check['name'])
    if context['track'] == 'cloudflare':
        ledger = report.get('coverage_ledger')
        if not isinstance(ledger, list):
            errors.append('cloudflare_coverage_ledger_missing')
        else:
            by_id = {item.get('coverage_id'): item for item in ledger if isinstance(item, dict)}
            if len(by_id) != len(ledger) or by_id.keys() != units.keys():
                errors.append('cloudflare_coverage_scope_mismatch')
            for key, item in by_id.items():
                if key in units and units[key].get('required', True) and item.get('status') != 'covered':
                    gaps.append('cloudflare_required_unit_incomplete:' + str(key))
        validators = _validator_contracts(report, assets)
    confirmed = any(isinstance(item, dict) and item.get('verdict') == 'confirmed' for item in findings)
    failed = any(isinstance(item, dict) and item.get('status') == 'failed'
                 for item in (checks if isinstance(checks, list) else []))
    if report.get('verdict') == 'passed' and (confirmed or failed or errors or gaps):
        errors.append('unsupported_pass_claim')
    # 此函数不查询官方回执，不把自报身份、JSON合法或零发现当作通过。
    return {'raw_report': copy.deepcopy(raw), 'raw_sha256': raw_digest,
            'digest_format': 'json-utf8-sorted-compact-v1',
            'adapted_report': report, 'adapted_sha256': _json_digest(report),
            'validation': {'schema_version': ADAPTER_VERSION, 'errors': errors,
                'stripped_fields': stripped, 'coverage_complete': not errors and not gaps,
                'eligible_for_settlement': False, 'required_validations': validators,
                'needs_validation': gaps + ['official_receipt_and_independence_verification_required']
                    + (['cloudflare_validators_not_executed'] if validators else []),
                'upstream_unmodified_schema_passed': False}}


def validator_inputs(adapted, *, root=None):
    """从固定适配报告生成校验输入；仅准备字节，不代表执行或正式通过。"""
    assets = load_assets(root)
    report = adapted['adapted_report']
    if _json_digest(report) != adapted['adapted_sha256']:
        raise AuditAdapterError('Validator report changed / 固定报告已变化')
    expected = _validator_contracts(report, assets)
    if adapted['validation']['required_validations'] != expected:
        raise AuditAdapterError('Validator contract changed / 校验契约与当前资产或报告不符')
    return [{'contract': entry, 'input_bytes': json.dumps(report[entry['input_field']],
                ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8'),
             'digest_format': 'json-utf8-sorted-compact-v1'} for entry in expected]


def _validator_contracts(report, assets):
    expected = []
    if report['context']['track'] == 'cloudflare':
        for field, name in (('findings', 'validate-findings.cjs'),
                            ('coverage_ledger', 'validate-coverage-ledger.cjs')):
            path = 'assets/audit/' + name
            expected.append({'name': name, 'validator_path': path,
                'validator_sha256': assets['manifest'][path], 'input_field': field,
                'reader_assets': {p: assets['manifest'][p] for p in WINDOWS_INPUT_ASSETS},
                'input_sha256': _json_digest(report.get(field)),
                'schema_path': 'assets/audit/report-schema.json' if field == 'findings' else None,
                'schema_sha256': assets['manifest']['assets/audit/report-schema.json'] if field == 'findings' else None})
    return expected


def _json_digest(value):
    try:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, RecursionError, UnicodeError) as error:
        raise AuditAdapterError('Invalid report JSON / 报告不是有效UTF-8 JSON数据') from error
    if len(data) > MAX_REPORT_BYTES:
        raise AuditAdapterError('Audit report too large / 报告超出5MiB边界')
    return hashlib.sha256(data).hexdigest()


def _text(value):
    return isinstance(value, str) and bool(value.strip())


def _strings(value):
    return isinstance(value, list) and bool(value) and all(_text(item) for item in value)


def _source_path(value):
    if (not isinstance(value, str) or not value or PurePosixPath(value).as_posix() != value
            or PurePosixPath(value).is_absolute() or any(part in ('', '.', '..') for part in value.split('/'))
            or any(ord(char) < 32 or char in '\\:*?"<>|' for char in value)):
        raise AuditAdapterError('Invalid source path / 文件必须为规范的仓库相对路径')
    return value


def _file_identity(item):
    if not isinstance(item, dict) or not _text(item.get('status')):
        raise AuditAdapterError('Invalid candidate file identity / 文件身份缺少status')
    return _source_path(item.get('path')), item['status']


def _strip_advice(finding, index, stripped):
    for field in PATCH_FIELDS & finding.keys():
        finding.pop(field)
        stripped.append({'finding': index, 'field': field, 'reason': 'repair_code_forbidden'})
    remediation = finding.get('remediation')
    if 'remediation' not in finding:
        return
    if isinstance(remediation, dict):
        for field in list(remediation):
            if field != 'strategy':
                remediation.pop(field)
                stripped.append({'finding': index, 'field': 'remediation.' + field,
                                 'reason': 'only_optional_direction_allowed'})
    direction = remediation.get('strategy') if isinstance(remediation, dict) else None
    if (finding.get('verdict') != 'confirmed' or not _text(direction) or len(direction) > 400
            or re.search(r'[\r\n`{};]|[。！？!?]\s*\S|\.\s+\S', direction)):
        finding.pop('remediation')
        stripped.append({'finding': index, 'field': 'remediation', 'reason': 'invalid_optional_direction'})
