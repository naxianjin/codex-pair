"""CLI变化的只读预检与独立登记；不以版本名称、帮助文本或进程存在证明通信通过。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import sysconfig
import time

try:
    from .pair_producer import file_identity, verify_launch, SUPPORTED_VERSIONS, STRICT_VERSIONS
    from .pair_transport import AppServer, canonical_path, session_uuid, command_receipt, redact_diagnostic
    from .pair_reviewer import fail
    from .pair_stages import digest, encoded
except ImportError:
    from pair_producer import file_identity, verify_launch, SUPPORTED_VERSIONS, STRICT_VERSIONS
    from pair_transport import AppServer, canonical_path, session_uuid, command_receipt, redact_diagnostic
    from pair_reviewer import fail
    from pair_stages import digest, encoded


ADAPTER_VERSION = 'codex-pair-cli-compatibility/1'
OPERATIONS = ('new_delivery', 'approval_execution', 'review_settlement', 'runtime_switch')
ADAPTER_FILES = ('pair_compatibility.py', 'pair_producer.py', 'pair_transport.py', 'pair_review_receipts.py')


def safe_preflight(store, executable, *, observers=None, member_id=None):
    """启动/守护层显式登记；无可信基线且无已有原绑定观察器时只报缺口。"""
    observers = {} if observers is None else observers
    with store.transaction() as db:
        members = [dict(row) for row in db.execute("SELECT * FROM members WHERE lifecycle<>'retired' AND (? IS NULL OR id=?)", (member_id, member_id))]
    results = {}
    for actor in members:
        observer = observers.get(actor['id'])
        owned = None
        try:
            trusted = store.setting('cli_trusted_executable:' + actor['id'])
            if trusted is None and observer is not None:
                store._register_original_cli_baseline(actor['id'], actor['session_uuid'], actor['cwd'], observer, executable=executable)
                trusted = store.setting('cli_trusted_executable:' + actor['id'])
            if observer is None and trusted is not None:
                try:
                    current = file_identity(executable)
                except (ValueError, OSError, TypeError):
                    current = None
                # 不先运行未知新exe再检查它；只在与既有来源准确一致时创建元数据连接。
                if _match_trusted_executable(current, trusted)['status'] == 'verified':
                    owned = AppServer(codex_home=actor['codex_home'], cwd=actor['cwd'], executable=current['path'],
                                      metadata_only=True, track_producer=True)
                    observer = owned
            results[actor['id']] = store.refresh_cli_compatibility(actor['id'], actor['session_uuid'], actor['cwd'],
                                                                 executable, observer=observer)
        except (ValueError, OSError, RuntimeError, KeyError, TypeError) as error:
            # 查询不可用时仍登记只读文件事实，防旧ready缓存继续用于新投递。
            result = store.refresh_cli_compatibility(actor['id'], actor['session_uuid'], actor['cwd'], executable)
            results[actor['id']] = dict(result, observer_error_type=type(error).__name__)
        finally:
            if owned is not None:
                owned.close()
    return {'members': results, 'new_work_started': False,
            'first_start_rule': 'missing_baseline_requires_existing_original_binding_observer',
            'all_delivery_preflights_ready': bool(results) and all(
                row['operations']['new_delivery']['status'] == 'preflight_ready' for row in results.values())}


class CompatibilityMonitor:
    """本地变化立即复查；无变化时默认每30秒只读核验当前身份，不启动模型。"""
    def __init__(self, store, executable, *, member_id=None, interval=30):
        if type(interval) not in (int, float) or not 1 <= interval <= 300:
            raise ValueError('Invalid compatibility monitor interval')
        self.store, self.executable, self.member_id = store, executable, member_id
        self.interval, self.next_due, self.last_hint = interval, 0, None

    def poll(self, *, force=False):
        with self.store.transaction() as db:
            identities = [(row['id'], row['session_uuid'], row['binding_version'], row['proof'], row['cwd'], row['codex_home'])
                          for row in db.execute("SELECT * FROM members WHERE lifecycle<>'retired' AND (? IS NULL OR id=?) ORDER BY id", (self.member_id, self.member_id))]
        try:
            info = Path(self.executable).stat()
            executable_hint = (str(self.executable), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        except (ValueError, OSError, TypeError):
            executable_hint = (str(self.executable), 'unavailable')
        hint = digest([executable_hint, identities])
        now = time.monotonic()
        if not force and hint == self.last_hint and now < self.next_due:
            return {'checked': False, 'reason': 'unchanged_until_next_metadata_check'}
        self.last_hint, self.next_due = hint, now + self.interval
        return {'checked': True, **safe_preflight(self.store, self.executable, member_id=self.member_id)}


def compatibility_probe(executable, *, expected_binding=None, trusted_executable=None,
                        observer=None, previous=None, force=False, timeout=5):
    """只读：无可信exe来源时不启动进程；observer须是调用方已有的只读元数据连接。"""
    if type(force) is not bool or type(timeout) not in (int, float) or not 0 < timeout <= 15:
        raise ValueError('Invalid compatibility probe options')
    record = {'adapter_version': ADAPTER_VERSION, 'checked_at': time.time(), 'status': 'not_verified',
              'read_only': True, 'persisted': False, 'model_started': False, 'test_message_sent': False,
              'configuration_changed': False, 'communication': {'status': 'not_verified', 'reason': 'not_tested_by_readonly_preflight'},
              'capabilities': {}, 'observations': [], 'reasons': [], 'operations': {}, 'reused': False}
    capabilities = record['capabilities']
    try:
        binding = _expected_binding(expected_binding)
    except (ValueError, OSError):
        binding = None
        record['reasons'].append('expected_binding_metadata_incomplete')
    record['binding'] = binding
    if os.name == 'nt':
        # platform.uname() 在 Windows 可能启动 cmd /c ver；只读预检不借系统信息查询启动进程。
        version = sys.getwindowsversion()
        environment = {'os': 'Windows', 'release': '.'.join(map(str, version[:3])),
                       'machine': sysconfig.get_platform(), 'machine_source': 'python_platform_tag'}
    else:
        environment = {'os': platform.system(), 'release': platform.release(), 'machine': platform.machine()}
    environment.update(python=list(sys.version_info[:3]), adapter_digest=_adapter_digest(), binding=binding)
    record['environment'] = environment
    identity = None
    try:
        path = Path(executable) if isinstance(executable, (str, os.PathLike)) else None
        if path is None or not path.is_absolute() or path.suffix.lower() != '.exe':
            raise ValueError('Explicit absolute native executable required')
        identity = file_identity(path)
        record['executable'] = identity
        capabilities['executable_file'] = {'status': 'observed', 'source': 'file_identity_sha256'}
    except (ValueError, OSError) as error:
        record['executable'] = {'requested_path': str(executable) if isinstance(executable, (str, os.PathLike)) else None,
                                'available': False}
        capabilities['executable_file'] = {'status': 'not_verified', 'reason': type(error).__name__}
        record['reasons'].append('native_executable_missing_or_unsafe')
    trust = _match_trusted_executable(identity, trusted_executable)
    capabilities['executable_origin'] = trust
    if trust['status'] != 'verified':
        record['reasons'].append('trusted_executable_baseline_missing_or_changed')
    metadata = _metadata_probe(observer, binding, identity) if observer is not None else {
        'identity': {'status': 'not_verified', 'reason': 'live_metadata_observer_unavailable'},
        'readonly_metadata': {'status': 'not_verified', 'reason': 'not_queried'},
        'turn_fields': {'status': 'not_verified', 'reason': 'not_queried'},
        'tool_fields': {'status': 'not_verified', 'reason': 'not_queried'},
        'observer_executable': {'status': 'not_verified', 'reason': 'not_queried'},
    }
    capabilities.update(metadata)
    fingerprint_material = {'executable': record['executable'], 'environment': environment,
                            'trust': trust, 'metadata': metadata}
    record['fingerprint'] = digest(fingerprint_material)
    version = None
    reused = (not force and isinstance(previous, dict) and previous.get('fingerprint') == record['fingerprint']
              and previous.get('adapter_version') == ADAPTER_VERSION
              and all(key in previous.get('capabilities', {}) for key in ('cli_version', 'app_server_cli', 'queue_cli')))
    if reused:
        # 只复用相同指纹的CLI本地查询；当前身份和业务门禁仍重新计算。
        record['reused'] = True
        version = previous.get('version')
        record['observations'] = previous.get('observations', [])
        for key in ('cli_version', 'app_server_cli', 'queue_cli'):
            capabilities[key] = previous['capabilities'][key]
    elif identity is not None and trust['status'] == 'verified':
        version_result = _query_cli(identity, ['--version'], binding, timeout)
        record['observations'].append(_query_summary('version', version_result))
        match = re.fullmatch(r'codex-cli ([0-9][A-Za-z0-9.+-]{0,63})\s*', version_result.get('stdout', ''))
        if version_result['status'] == 'completed' and version_result['exit_code'] == 0 and match:
            version = match[1]
            capabilities['cli_version'] = {'status': 'observed', 'version': version}
        else:
            capabilities['cli_version'] = {'status': 'not_verified', 'reason': 'version_query_failed_or_format_unknown'}
            record['reasons'].append('cli_version_unavailable')
        top = _query_cli(identity, ['--help'], binding, timeout)
        record['observations'].append(_query_summary('help', top))
        commands = _help_commands(top)
        capabilities['queue_cli'] = {'status': 'declared' if 'queue' in commands else 'not_verified',
                                     'reason': 'help_only_not_delivery_proof'}
        if 'app-server' in commands:
            app = _query_cli(identity, ['app-server', '--help'], binding, timeout)
            record['observations'].append(_query_summary('app_server_help', app))
            available = app['status'] == 'completed' and app['exit_code'] == 0 and 'app-server' in app.get('stdout', '')
            capabilities['app_server_cli'] = {'status': 'declared' if available else 'not_verified',
                                              'reason': 'help_only_not_rpc_validation'}
        else:
            capabilities['app_server_cli'] = {'status': 'not_verified', 'reason': 'app_server_not_declared'}
        try:
            if file_identity(identity['path']) != identity:
                raise ValueError('Executable changed during compatibility probe')
        except (ValueError, OSError):
            record['reasons'].append('executable_changed_during_probe')
            capabilities['executable_origin'] = {'status': 'not_verified', 'reason': 'file_changed'}
    else:
        capabilities['cli_version'] = {'status': 'not_verified', 'reason': 'untrusted_executable_not_run'}
        capabilities['app_server_cli'] = {'status': 'not_verified', 'reason': 'untrusted_executable_not_run'}
        capabilities['queue_cli'] = {'status': 'not_verified', 'reason': 'untrusted_executable_not_run'}
    record['version'] = version
    known = version in SUPPORTED_VERSIONS
    capabilities['receipt_adapter'] = {
        'status': 'known_local_adapter' if known else 'not_verified',
        'format': 'strict_display_and_raw' if version in STRICT_VERSIONS else ('legacy_0_163_0' if known else None),
        'actual_receipt_validated': False,
    }
    if not known:
        record['reasons'].append('receipt_adapter_version_not_registered')
    base = []
    if capabilities['executable_origin']['status'] != 'verified':
        base.append('executable_origin_not_verified')
    if not known:
        base.append('cli_adapter_not_verified')
    if capabilities['identity']['status'] != 'verified':
        base.append('current_session_identity_not_verified')
    if capabilities['readonly_metadata']['status'] != 'verified':
        base.append('readonly_metadata_not_verified')
    if capabilities['observer_executable']['status'] != 'verified':
        base.append('metadata_observer_executable_not_verified')
    elif capabilities['observer_executable'].get('version') != version:
        base.append('metadata_observer_version_mismatch')
    if capabilities['app_server_cli']['status'] != 'declared':
        base.append('app_server_cli_not_available')
    for operation in OPERATIONS:
        reasons = list(base)
        if operation in ('approval_execution', 'review_settlement') and capabilities['tool_fields']['status'] != 'observed':
            reasons.append('official_tool_evidence_fields_not_observed')
        if operation == 'new_delivery' and binding and binding.get('backend') == 'queue' and capabilities['queue_cli']['status'] != 'declared':
            reasons.append('queue_cli_not_declared')
        record['operations'][operation] = {'status': 'preflight_ready' if not reasons else 'not_verified',
                                           'reasons': reasons, 'execution_authorized': False}
    record['reasons'] = sorted(set(record['reasons'] + base))
    record['status'] = 'preflight_ready' if not base else 'not_verified'
    record['verification_limits'] = ['help_is_not_rpc_or_delivery_proof', 'no_model_or_message_probe',
        'existing_permissions_and_user_pause_still_apply', 'formal_receipts_need_per_execution_validation']
    return record


class CompatibilityMixin:
    def _register_original_cli_baseline(self, member, session, cwd, observer, *, executable):
        """仅内部首次登记原绑定的受管只读进程；不接受JSON证明，也不覆盖旧exe基线。"""
        if (not isinstance(observer, AppServer) or getattr(observer, 'metadata_only', None) is not True
                or getattr(observer, 'backend', None) != 'managed'
                or not isinstance(getattr(observer, 'producer_launch', None), dict)):
            fail('Owned readonly observer required / 只接受实际受管只读AppServer的原生进程证据')
        launch = observer.producer_launch
        verify_launch(launch)
        if file_identity(executable) != launch['file']:
            fail('Original CLI executable mismatch / 原绑定查询必须使用所指定的同一原生exe')
        with self.transaction() as db:
            actor = dict(self._binding(db, member, session, cwd))
            expected = _expected_binding(_member_binding(actor))
        actual = observer.read_thread(expected['session_uuid'])
        view = {'session_uuid': session_uuid(actual['id']), 'cwd': canonical_path(actual['cwd']),
                'model': actual.get('model'), 'modelProvider': actual.get('modelProvider')}
        home = observer.identity.get('codexHome')
        if (not isinstance(home, str) or canonical_path(home) != expected['codex_home'] or actual.get('ephemeral')
                or any(view[key] != expected[key] for key in view)):
            fail('Original CLI binding not verified / 实际原会话、配置、cwd、模型或提供方不符')
        verify_launch(launch)
        source = 'original-visible-binding:' + session + ':' + str(actor['binding_version'])
        baseline = {'file': launch['file'], 'version': launch['version'], 'source_reference': source,
                    'source_binding': expected, 'process': launch['process'], 'created': time.time(),
                    'digital_signature_or_upstream_attestation': 'not_claimed'}
        with self.transaction() as db:
            current = self._binding(db, member, session, cwd)
            if _expected_binding(_member_binding(current)) != expected:
                fail('Original CLI binding changed / 查询期间原绑定变化，不登记过期来源')
            key = 'cli_trusted_executable:' + member
            old = self._option(db, key)
            if old is not None:
                if old.get('file') != baseline['file']:
                    fail('CLI baseline replacement requires review / 已有exe基线，不以首次入口覆盖升级后的路径或摘要')
                return {'registered': False, 'duplicate': True, 'source_reference': old['source_reference']}
            self._put(db, key, baseline)
            self.event(db, 'cli_original_baseline_registered', member=member, source_reference=source,
                       executable_sha256=baseline['file']['sha256'], communication_verified=False)
            return {'registered': True, 'duplicate': False, 'source_reference': source,
                    'communication_verified': False, 'permissions_granted': False}

    def refresh_cli_compatibility(self, member, session, cwd, executable, *, observer=None, force=False):
        """显式授权写入口；doctor不得调用。可信exe来源只读既存记录，不接受JSON自证。"""
        with self.transaction() as db:
            actor = dict(self._binding(db, member, session, cwd))
            binding = _member_binding(actor)
            previous = self._option(db, 'cli_compatibility:' + member)
            trusted = self._option(db, 'cli_trusted_executable:' + member)
        result = compatibility_probe(executable, expected_binding=binding, trusted_executable=trusted,
                                     observer=observer, previous=previous, force=force)
        with self.transaction() as db:
            current = dict(self._binding(db, member, session, cwd))
            if _member_binding(current) != binding:
                fail('CLI compatibility binding changed / 预检期间成员或配置变化，不登记过期结果')
            latest = self._option(db, 'cli_compatibility:' + member)
            if latest != previous:
                fail('CLI compatibility refresh raced / 已有较新的预检结果，保留并重新读取')
            changed = previous is None or previous.get('fingerprint') != result['fingerprint']
            saved = dict(result, persisted=True)
            self._put(db, 'cli_compatibility:' + member, saved)
            if changed:
                self.event(db, 'cli_compatibility_changed', member=member, fingerprint=result['fingerprint'],
                           status=result['status'], reasons=result['reasons'], existing_executions_unchanged=True)
            return dict(saved, change_event_created=changed)

    def cli_compatibility_status(self, member, session, cwd):
        with self.transaction() as db:
            self._binding(db, member, session, cwd)
            return {'record': self._option(db, 'cli_compatibility:' + member),
                    'trust_baseline_present': self._option(db, 'cli_trusted_executable:' + member) is not None,
                    'communication_verified_by_probe': False}

    def _cli_compatibility_gate(self, db, member, operation):
        if operation not in OPERATIONS:
            fail('Unknown compatibility operation / 兼容性门禁操作不受支持')
        record = self._option(db, 'cli_compatibility:' + member)
        if not record:
            if not self._option(db,'member_context_required',False):return []
            return ['cli_compatibility_not_checked']
        try:
            binding = _expected_binding(_member_binding(self._member(db, member)))
        except (ValueError, OSError):
            return ['cli_binding_metadata_incomplete']
        if record.get('binding') != binding:
            return ['cli_compatibility_binding_changed']
        item = record.get('operations', {}).get(operation, {})
        return [] if item.get('status') == 'preflight_ready' else item.get('reasons') or ['cli_capability_not_verified']

    def bind_cli_execution(self, member, session, cwd, execution_id, contract_digest, *, operation,
                           turn_id=None, task_id=None):
        """新执行固定当时exe/版本/契约；旧记录不因新CLI预检而改写。"""
        _execution_id(execution_id)
        if not isinstance(contract_digest, str) or not re.fullmatch(r'[0-9a-f]{64}', contract_digest):
            fail('CLI contract digest required / 执行须绑定准确检查契约摘要')
        if turn_id is not None and (not isinstance(turn_id, str) or not turn_id or len(turn_id) > 128):
            fail('Invalid CLI turn / 回合ID无效')
        with self.transaction() as db:
            actor = self._binding(db, member, session, cwd)
            if task_id is not None and (not isinstance(task_id, str) or not db.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone()):
                fail('Exact execution task required / CLI执行只能关联已登记的准确任务')
            key = 'cli_execution:' + execution_id
            old = self._option(db, key)
            if old:
                if (old['member'] != member or old['session'] != session or old['binding_version'] != actor['binding_version']
                        or old['contract_digest'] != contract_digest or old['operation'] != operation
                        or old.get('turn_id') not in (None, turn_id) or old.get('task_id') != task_id):
                    fail('CLI execution cannot be rebound / 不得将旧执行改绑新身份、回合或契约')
                if turn_id and old.get('turn_id') is None:
                    old = dict(old, turn_id=turn_id)
                    self._put(db, key, old)
                return dict(old, duplicate=True)
            probe = self._option(db, 'cli_compatibility:' + member)
            reasons = self._cli_compatibility_gate(db, member, operation)
            if probe is None:
                reasons.append('cli_compatibility_not_checked')
            if reasons:
                fail('CLI compatibility not verified / ' + ','.join(reasons))
            self._require_recovery_open(db, member=member, task_id=task_id, action='business')
            try:
                unchanged = file_identity(probe['executable']['path']) == probe['executable']
            except (ValueError, OSError):
                unchanged = False
            if not unchanged:
                fail('CLI changed after preflight / 新执行前exe已经变化或消失，先预检，不借旧结果启动')
            pinned = {'execution_id': execution_id, 'member': member, 'session': session,
                      'binding_version': actor['binding_version'], 'turn_id': turn_id, 'task_id': task_id, 'operation': operation,
                      'contract_digest': contract_digest, 'executable': probe['executable'],
                      'version': probe['version'], 'adapter_version': probe['adapter_version'],
                      'adapter_digest': probe['environment']['adapter_digest'],
                      'preflight_fingerprint': probe['fingerprint'], 'created': time.time()}
            self._put(db, key, pinned)
            self.event(db, 'cli_execution_bound', execution=execution_id, member=member,
                       fingerprint=probe['fingerprint'], operation=operation)
            return dict(pinned, duplicate=False)

    def inspect_cli_execution(self, member, session, cwd, execution_id):
        _execution_id(execution_id)
        with self.transaction() as db:
            self._binding(db, member, session, cwd)
            pinned = self._option(db, 'cli_execution:' + execution_id)
        if not pinned or pinned['member'] != member or pinned['session'] != session:
            fail('Unknown owned CLI execution / 必须查询本人准确执行记录')
        try:
            current = file_identity(pinned['executable']['path'])
            present = current == pinned['executable']
        except (ValueError, OSError):
            present = False
        return {'execution': pinned, 'original_executable_unchanged': present,
                'status': 'original_binding_preserved' if present else 'needs_validation',
                'result': 'unknown', 'replayed': False, 'new_cli_substituted': False,
                'next_action': 'query_original_turn_and_tools' if present else 'reconcile_original_execution_without_replay'}


def _expected_binding(value):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError('Invalid expected CLI binding')
    required = ('session_uuid', 'codex_home', 'cwd', 'model', 'modelProvider')
    if any(not isinstance(value.get(k), str) or not value[k] for k in required):
        raise ValueError('Incomplete expected CLI binding')
    if value['modelProvider'] not in ('openai', 'deepseek'):
        raise ValueError('Unsupported expected model provider')
    return {'session_uuid': session_uuid(value['session_uuid']), 'codex_home': canonical_path(value['codex_home']),
            'cwd': canonical_path(value['cwd']), 'model': value['model'], 'modelProvider': value['modelProvider'],
            'binding_version': value.get('binding_version'), 'backend': value.get('backend')}


def _member_binding(member):
    proof = json.loads(member['proof'])
    return {'session_uuid': member['session_uuid'], 'codex_home': member['codex_home'], 'cwd': member['cwd'],
            'model': proof.get('model'), 'modelProvider': proof.get('modelProvider'),
            'binding_version': member['binding_version'], 'backend': member['backend']}


def _match_trusted_executable(identity, trusted):
    if identity is None or not isinstance(trusted, dict):
        return {'status': 'not_verified', 'reason': 'trusted_baseline_unavailable'}
    pinned = trusted.get('file')
    source = trusted.get('source_reference')
    if (not isinstance(pinned, dict) or not isinstance(source, str) or not 1 <= len(source) <= 256
            or redact_diagnostic(source) != source
            or pinned != identity):
        return {'status': 'not_verified', 'reason': 'trusted_file_identity_mismatch'}
    return {'status': 'verified', 'source_reference': source, 'scope': 'matches_existing_trusted_executable_record'}


def _metadata_probe(observer, binding, executable):
    result = {key: {'status': 'not_verified', 'reason': 'not_queried'}
              for key in ('identity', 'readonly_metadata', 'turn_fields', 'tool_fields', 'observer_executable')}
    launch = getattr(observer, 'producer_launch', None)
    if not binding or executable is None or (getattr(observer, 'metadata_only', None) is not True and not launch):
        result['identity']['reason'] = 'exact_binding_or_readonly_observer_missing'
        return result
    try:
        if isinstance(launch, dict) and launch.get('file') == executable:
            verify_launch(launch)
            result['observer_executable'] = {'status': 'verified', 'source': 'existing_owned_process_binding',
                                             'version': launch.get('version')}
        else:
            result['observer_executable']['reason'] = 'observer_native_process_binding_unavailable'
        home = getattr(observer, 'identity', {}).get('codexHome')
        if not isinstance(home, str) or canonical_path(home) != binding['codex_home']:
            result['identity']['reason'] = 'observer_codex_home_mismatch'
            return result
        actual = observer.read_thread(binding['session_uuid'])
        view = {'session_uuid': session_uuid(actual['id']), 'cwd': canonical_path(actual['cwd']),
                'model': actual.get('model'), 'modelProvider': actual.get('modelProvider')}
        if actual.get('ephemeral') or any(view[k] != binding[k] for k in view):
            result['identity'] = {'status': 'not_verified', 'reason': 'live_session_binding_mismatch'}
            return result
        result['identity'] = {'status': 'verified', 'source': 'existing_readonly_observer', 'fields': sorted(view)}
        page = observer.rpc('thread/turns/list', {'threadId': binding['session_uuid'], 'limit': 1, 'itemsView': 'full'})
        if not isinstance(page, dict) or not isinstance(page.get('data'), list) or len(page['data']) > 1:
            result['readonly_metadata']['reason'] = 'turn_page_shape_unknown'
            return result
        result['readonly_metadata'] = {'status': 'verified', 'methods': ['thread/read', 'thread/turns/list'], 'model_created': False}
        if not page['data']:
            result['turn_fields']['reason'] = 'no_existing_turn_to_observe'
            result['tool_fields']['reason'] = 'no_existing_tool_to_observe'
            return result
        turn = page['data'][0]
        if (not isinstance(turn, dict) or not isinstance(turn.get('id'), str)
                or turn.get('threadId', binding['session_uuid']) != binding['session_uuid']
                or turn.get('status') not in ('completed', 'failed', 'interrupted', 'inProgress', 'queued')
                or not isinstance(turn.get('items'), list)):
            result['turn_fields']['reason'] = 'turn_fields_unknown'
            return result
        result['turn_fields'] = {'status': 'observed', 'fields': ['id', 'status', 'items'], 'terminal_result_claimed': False}
        commands = [item for item in turn['items'] if isinstance(item, dict) and item.get('type') in ('commandExecution', 'CommandExecution')]
        terminal = [item for item in commands if item.get('status') in ('completed', 'failed')]
        if terminal and all(command_receipt(item) is not None for item in terminal):
            result['tool_fields'] = {'status': 'observed', 'fields': ['id', 'type', 'status', 'exitCode', 'command', 'cwd'],
                                     'raw_argv_observed': any('command_argv' in item or isinstance(item.get('command'), list) for item in terminal),
                                     'exact_receipt_verified': False}
        else:
            result['tool_fields']['reason'] = 'terminal_tool_fields_not_observed' if not terminal else 'tool_fields_unknown'
        return result
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, AttributeError) as error:
        result['readonly_metadata'] = {'status': 'not_verified', 'reason': 'metadata_query_failed', 'error_type': type(error).__name__}
        return result


def _query_cli(identity, arguments, binding, timeout):
    # 只传Windows启动所需白名单，不继承API密钥、令牌或整份用户环境。
    environment = {name: os.environ[name] for name in ('SYSTEMROOT', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP') if name in os.environ}
    if binding:
        environment['CODEX_HOME'] = binding['codex_home']
    try:
        if file_identity(identity['path']) != identity:
            return {'status': 'not_verified', 'reason': 'executable_changed_before_query'}
        result = subprocess.run([identity['path'], *arguments], cwd=binding['cwd'] if binding else str(Path(identity['path']).parent),
                                env=environment, capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        return {'status': 'completed', 'exit_code': result.returncode, 'stdout': result.stdout[:65536],
                'stdout_sha256': hashlib.sha256(result.stdout.encode('utf-8')).hexdigest(),
                'stderr_present': bool(result.stderr)}
    except subprocess.TimeoutExpired:
        return {'status': 'not_verified', 'reason': 'metadata_command_timeout'}
    except OSError as error:
        return {'status': 'not_verified', 'reason': 'metadata_command_unavailable', 'error_type': type(error).__name__}


def _query_summary(name, value):
    return {'query': name, **{key: item for key, item in value.items() if key != 'stdout'}}


def _help_commands(result):
    if result['status'] != 'completed' or result['exit_code'] != 0:
        return set()
    return set(re.findall(r'^\s{2,}([a-z][a-z0-9-]*)\s{2,}', result.get('stdout', ''), flags=re.MULTILINE))


def _adapter_digest():
    root = Path(__file__).resolve().parent
    return digest({name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in ADAPTER_FILES})


def _execution_id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        fail('Invalid CLI execution ID / 执行ID无效')
