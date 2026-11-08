"""固定校验运行的 Store 准入与复核；只登记诊断权限，不认证执行或正式回执。"""
from contextlib import contextmanager
import hashlib
import json
import re

try:
    from .pair_auxiliary import _config_binding
    from .pair_auxiliary_runtime import _context, saved_validator_inputs
    from .pair_core import FileLock, PairError
    from .pair_reviewer import fail, reviewer_identity
    from .pair_stages import digest
    from .pair_transport import canonical_path
    from .pair_validator_worker import validator_request
except ImportError:
    from pair_auxiliary import _config_binding
    from pair_auxiliary_runtime import _context, saved_validator_inputs
    from pair_core import FileLock, PairError
    from pair_reviewer import fail, reviewer_identity
    from pair_stages import digest
    from pair_transport import canonical_path
    from pair_validator_worker import validator_request


def admit_validator_run(store, member, session, cwd, execution_id, lease, *, run_id, tool_identity):
    """登记当前固定输入；工具摘要由调用它的可信宿主核验，本函数不启动工具。"""
    _run_id(run_id)
    tools = _tools(tool_identity)
    prepared = saved_validator_inputs(store, member, session, cwd, execution_id, lease)
    with store._recovery_control_transaction() as db:
        actor = _actor(store, db, member, session, cwd)
        snapshot, origin = _snapshot(store, db, actor, execution_id, lease, prepared, tools)
        store._auxiliary_dispatchable(db, task_id=origin)
        key = 'validator_admission:' + run_id
        old = store._option(db, key)
        record = {'schema_version': 1, 'run_id': run_id, 'snapshot': snapshot,
                  'snapshot_digest': digest(snapshot), 'state': 'admitted', 'formal': False}
        if old is not None:
            _record(old, run_id)
            if digest(old['snapshot']) != record['snapshot_digest']:
                fail('Validator admission conflict / 已登记运行不能改绑其他输入或权限')
        else:
            if store._option(db, 'validator_run:' + run_id) is not None:
                fail('Validator admission too late / 既有运行不能事后补发准入')
            store._put(db, key, record)
            store.event(db, 'validator_admitted', run=run_id, execution=execution_id,
                        snapshot_digest=record['snapshot_digest'], formal=False)
        return {'run_id': run_id, 'snapshot_digest': record['snapshot_digest'],
                'request': snapshot['request'], 'duplicate': old is not None,
                'execution_started': False, 'formal': False, 'formal_checks_verified': False}


def verify_validator_admission(store, member, session, cwd, execution_id, lease, *,
                               run_id, tool_identity, purpose):
    """单次复核结果不是持续许可；实际派发或保存使用下方持锁边界。"""
    with validator_admission_boundary(store, member, session, cwd, execution_id, lease,
            run_id=run_id, tool_identity=tool_identity, purpose=purpose) as result:
        return result


@contextmanager
def validator_admission_boundary(store, member, session, cwd, execution_id, lease, *,
                                 run_id, tool_identity, purpose):
    """复核提交后持有控制锁至调用方派发或保存结束；未知启动不回滚派发领取。"""
    _run_id(run_id)
    tools = _tools(tool_identity)
    if purpose not in ('dispatch', 'save', 'reconcile'):
        fail('Invalid validator admission purpose / 仅允许派发、保存前复核或原运行对账')
    prepared = saved_validator_inputs(store, member, session, cwd, execution_id, lease)
    with FileLock(store.control_lock, timeout=30):
        result = _verify(store, member, session, cwd, execution_id, lease,
                         run_id, tools, purpose, prepared)
        yield result


def _verify(store, member, session, cwd, execution_id, lease, run_id, tools, purpose, prepared):
    with store.transaction() as db:
        actor = _actor(store, db, member, session, cwd)
        key = 'validator_admission:' + run_id
        record = store._option(db, key)
        _record(record, run_id)
        current, origin = _snapshot(store, db, actor, execution_id, lease, prepared, tools)
        previous = record['snapshot']
        changed = [name for name in current if digest(current[name]) != digest(previous.get(name))]
        if any(name not in ('control_generation', 'authorization_version') for name in changed):
            fail('Validator admission changed / 原身份、配置、执行、报告、输入或工具已改变')
        blocked = None
        try:
            store._auxiliary_dispatchable(db, task_id=origin)
        except PairError as error:
            if purpose != 'reconcile':
                raise
            blocked = str(error)
        if changed and purpose != 'reconcile':
            fail('Validator admission stale / 控制代次或授权版本已改变，不能继续原准入')
        saved = store._option(db, 'validator_run:' + run_id) is not None
        dispatch = purpose == 'dispatch' and record['state'] == 'admitted' and not saved
        if purpose == 'save' and record['state'] != 'dispatching':
            fail('Validator dispatch required / 未领取准确运行不能保存执行结果')
        if dispatch:
            record['state'] = 'dispatching'
            store._put(db, key, record)
            store.event(db, 'validator_dispatch_claimed', run=run_id, execution=execution_id,
                        snapshot_digest=record['snapshot_digest'], formal=False)
        return {'run_id': run_id, 'snapshot_digest': record['snapshot_digest'],
                'request': previous['request'], 'dispatch_allowed': dispatch,
                'save_allowed': purpose == 'save' and not saved,
                'reconciliation_only': purpose == 'reconcile' or purpose == 'dispatch' and not dispatch,
                'controls_changed': changed, 'control_block': blocked, 'already_saved': saved,
                'formal': False, 'formal_checks_verified': False}


def _snapshot(store, db, actor, execution_id, lease, prepared, tools):
    execution = dict(store._auxiliary_execution(db, execution_id, lease))
    task = dict(store._auxiliary_task(db, execution['task_id']))
    candidate = dict(store._auxiliary_candidate(db, task['candidate_id']))
    report = db.execute('SELECT * FROM auxiliary_reports WHERE execution_id=?', (execution_id,)).fetchone()
    context = _context(store, {'execution': execution, 'task': task, 'candidate': candidate})
    if (not report or execution['state'] != 'completed'
            or any(execution[key] != candidate[key] for key in ('candidate_digest', 'contract_digest'))
            or digest(context) != digest(prepared['context'])
            or report['report_digest'] != prepared['original_report_digest']
            or digest(json.loads(report['raw_report'])) != prepared['stored_report_digest']
            or digest(json.loads(report['adapted_report'])) != prepared['adapted_report_digest']
            or digest(json.loads(report['validation'])['required_validations'])
               != digest([item['contract'] for item in prepared['inputs']])):
        fail('Validator source changed / 生成输入后固定报告或执行已改变')
    reviewer = dict(db.execute('SELECT * FROM auxiliary_sessions WHERE id=?', (execution['reviewer_id'],)).fetchone())
    config, source = store._auxiliary_config(db, reviewer['config_id'])
    identity = reviewer_identity(json.loads(reviewer['proof']))
    if (execution['config_binding'] != config['binding']
            or execution['session_uuid'] != reviewer['session_uuid'] or identity['id'] != execution['session_uuid']
            or identity['cwd'] != canonical_path(reviewer['cwd']) or identity['model'] != reviewer['model']
            or identity['modelProvider'] != json.loads(config['binding'])['provider']):
        fail('Validator source identity changed / 原执行身份或获准配置已改变')
    stage = db.execute('SELECT task_id FROM review_stages WHERE id=?', (candidate['stage_id'],)).fetchone()
    return {'pair_id': store.pair_id, 'actor': dict(_config_binding(actor),
                proof_digest=digest(json.loads(actor['proof']))),
            'control_generation': store._control_generation(db),
            'authorization_version': store._authorization_version(db),
            'execution': {name: execution[name] for name in ('id', 'task_id', 'audit_id', 'reviewer_id',
                'epoch', 'session_uuid', 'candidate_digest', 'contract_digest', 'config_binding',
                'state', 'charged', 'turn_id', 'producer')},
            'lease_sha256': hashlib.sha256(lease.encode('utf-8')).hexdigest(),
            'reviewer': {name: reviewer[name] for name in ('id', 'config_id', 'session_uuid',
                'epoch', 'cwd', 'model', 'pool', 'creation_id', 'proof')},
            'config': {'id': config['id'], 'binding': config['binding'],
                       'source': _config_binding(source), 'proof_digest': digest(json.loads(source['proof']))},
            'report_digest': digest(dict(report)),
            'request': validator_request(prepared, node_sha256=tools['node']),
            'tool_identity': tools}, stage['task_id']


def _actor(store, db, member, session, cwd):
    actor = store._a_actor(db, member, session, cwd)
    if member != 'A' or actor['lifecycle'] != 'active' or not actor['verified']:
        fail('Verified original A required / 仅当前已核验的原 A 可登记或核对准入')
    identity = reviewer_identity(json.loads(actor['proof']))
    if identity['id'] != actor['session_uuid'] or identity['cwd'] != canonical_path(actor['cwd']):
        fail('Validator coordinator identity mismatch / A 的会话证明与当前绑定不符')
    return actor


def _run_id(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r'[0-9a-f]{32}', run_id):
        fail('Invalid validator run ID')


def _tools(value):
    if (not isinstance(value, dict) or not 1 <= len(value) <= 32 or 'node' not in value
            or any(not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_.-]{0,63}', name)
                   or not isinstance(checksum, str) or not re.fullmatch(r'[0-9a-f]{64}', checksum)
                   for name, checksum in value.items())):
        fail('Invalid validator tool identity')
    return dict(value)


def _record(record, run_id):
    if (not isinstance(record, dict) or set(record) != {'schema_version', 'run_id', 'snapshot',
            'snapshot_digest', 'state', 'formal'} or type(record['schema_version']) is not int
            or record['schema_version'] != 1 or record['run_id'] != run_id
            or not isinstance(record['snapshot'], dict) or record['formal'] is not False
            or record['state'] not in ('admitted', 'dispatching')
            or record['snapshot_digest'] != digest(record['snapshot'])):
        fail('Validator admission unavailable / 缺原运行的完整 Store 准入记录')
