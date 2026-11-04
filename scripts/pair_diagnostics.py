"""严格只读阻塞快照；调度器与诊断共用准入判定。"""
from __future__ import annotations

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time


def capacity(db, name):
    row = db.execute("SELECT value FROM pair_settings WHERE key='auxiliary_policy'").fetchone()
    policy = json.loads(row[0]) if row else {}
    return policy.get(name, 2)


class AdmissionMixin:
    def _admission_reasons(self, db, task, member_id):
        try:
            from .pair_core import member_workspace, scopes_overlap
        except ImportError:
            from pair_core import member_workspace, scopes_overlap
        reasons = []
        modern = db.execute('PRAGMA user_version').fetchone()[0] >= 14

        def add(code, rule, objects=()):
            reasons.append({'code': code, 'rule': rule, 'objects': list(objects),
                            'owner': 'A', 'task': task['id'], 'member': member_id})

        state = db.execute('SELECT status FROM collaborations').fetchone()[0]
        if state != 'running':
            add('USER_PAUSED_OR_STOPPED', 'collaboration.status', [state])
        if self._option(db, 'goal_completed', False):
            add('USER_PAUSED_OR_STOPPED', 'goal_completed')
        if task['id'].startswith('pair-plan-') and self._option(db, 'closed_scope'):
            add('USER_PAUSED_OR_STOPPED', 'closed_scope')
        if not self._team_ready(db):
            add('ACTOR_UNAVAILABLE', 'two_verified_visible_members')
        if modern:
            reasons.extend(self._recovery_gate(db, member=member_id, task_id=task['id']))
            if self._member(db,member_id)['delivery_mode']!='manual':
                compatibility=self._cli_compatibility_gate(db,member_id,'new_delivery')
                if compatibility:add('CLI_COMPATIBILITY_UNVERIFIED','cli.current_binding',compatibility)
        parents = list(db.execute('''SELECT p.* FROM dependencies d JOIN tasks p
            ON p.id=d.depends_on WHERE d.task_id=? ORDER BY p.id''', (task['id'],)))
        for parent in parents:
            satisfied = self._task_obligation_satisfied(db, parent['id']) if modern else (
                parent['status'] in ('completed', 'integrated') and self._stage_passed(db, parent['id']))
            if not satisfied:
                add('DEPENDENCY_UNSATISFIED', 'dependency.completed_and_reviewed', [parent['id']])
        for resource in json.loads(task['resources']):
            row = db.execute('SELECT task_id FROM resource_locks WHERE resource=? AND task_id<>?',
                             (resource, task['id'])).fetchone()
            if row:
                add('RESOURCE_OCCUPIED', 'resource_locks', [resource, row[0]])
        workspaces = {r['id']: member_workspace(dict(r)) for r in db.execute('SELECT * FROM members')}
        active = [r for r in db.execute("SELECT * FROM tasks WHERE status IN ('active','blocked',"
                  "'waiting_execution','quota_wait','checkpoint_wait','requirements_wait')")
                  if r['id'] != task['id'] and not (r['id'].startswith('pair-plan-') and self._option(db, 'closed_scope'))]
        delivered = [r for r in db.execute("SELECT * FROM tasks WHERE status='delivered'") if r['id'] != task['id']]
        legacy = self._option(db, 'scheduling_policy') != 'quota-balanced' or not self._option(db, 'member_context_required', False)
        if legacy:
            overlaps = [r['id'] for r in active + delivered
                        if scopes_overlap(json.loads(task['allowed_paths']), json.loads(r['allowed_paths']))]
            if overlaps:
                add('RESOURCE_OCCUPIED', 'legacy.allowed_paths', overlaps)
        if not self._member_eligible(db, member_id, task):
            add('ACTOR_UNAVAILABLE', 'member.binding_capabilities_independence')
        if self._member_suspended(db, member_id) or self._pool_blocked(db, member_id):
            add('QUOTA_UNAVAILABLE', 'quota.saved_state_pool_reserve')
        if self._checkpoint_read_required(db, member_id):
            add('CHECKPOINT_RECOVERY_PENDING', 'checkpoint.read_and_consume')
        backlog = self._task_backlog_ids(db, member_id) if modern else [r[0] for r in db.execute("SELECT id FROM tasks WHERE assignee=? "
                   "AND status IN ('review_wait','needs_validation') ORDER BY id", (member_id,))]
        if task['task_kind'] not in ('coordination', 'planning') and len(backlog) >= capacity(db, 'task_capacity'):
            add('MEMBER_REVIEW_BACKLOG_LIMIT', 'task.review_backlog', backlog)
        occupied = [r['id'] for r in active + delivered if r['assignee'] == member_id
                    or workspaces.get(r['assignee']) == workspaces[member_id]]
        if occupied:
            add('WORKSPACE_OCCUPIED', 'task.workspace_owner', occupied)
        for parent in parents:
            if modern:
                effective = self._effective_obligation_task(db, parent['id'])
                parent = db.execute('SELECT * FROM tasks WHERE id=?', (effective,)).fetchone()
            if (task['allowed_paths'] != '[]' and json.loads(parent['commits_json'] or '[]')
                    and workspaces[parent['assignee']] != workspaces[member_id]
                    and not db.execute("SELECT 1 FROM integrations WHERE task_id=? AND receiver=? AND status='completed'",
                                       (parent['id'], workspaces[member_id])).fetchone()):
                add('DEPENDENCY_UNSATISFIED', 'dependency.integrated_in_receiver', [parent['id']])
        if task['review_required'] and not json.loads(task['required_checks'] or '[]'):
            add('CHECK_CONTRACT_MISSING', 'task.nonempty_required_checks')
        return reasons


def snapshot_from_db(db, pair_id, *, task_id=None, now=None):
    """调用方持有同一个读事务；不取得文件锁，不刷新，不调用普通 Store 初始化。"""
    try:
        from .pair_core import Store
    except ImportError:
        from pair_core import Store

    class ReadView(Store):
        def __init__(self):
            self.pair_id = pair_id

        def transaction(self):
            raise RuntimeError('Read snapshot only / 诊断禁止另开事务')

        @staticmethod
        def _put(*args, **kwargs):
            raise RuntimeError('Read snapshot only / 诊断禁止写策略')

        @staticmethod
        def event(*args, **kwargs):
            raise RuntimeError('Read snapshot only / 诊断禁止写审计')

    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version not in (13, 14):
        raise ValueError('Unsupported diagnostic schema / 诊断仅支持 schema 13、14，不自动升级')
    pair = db.execute('SELECT id,status,revision FROM collaborations').fetchone()
    if not pair or pair['id'] != pair_id:
        raise ValueError('State identity mismatch / 协作数据库身份不符')
    view = ReadView()
    observed = time.time() if now is None else now
    members = [dict(r) for r in db.execute('SELECT id,binding_version,lifecycle,online,verified FROM members ORDER BY id')]
    all_tasks = {r['id']: dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY id')}
    selected = set(all_tasks) if task_id is None else {task_id}
    if task_id is not None and task_id not in all_tasks:
        raise ValueError('Task not found / 任务不存在')
    dependencies = [dict(r) for r in db.execute('SELECT * FROM dependencies ORDER BY task_id,depends_on')]
    if task_id:
        pending = [task_id]
        while pending:
            current = pending.pop()
            if version >= 14:
                effective = view._effective_obligation_task(db, current)
                if effective not in selected:
                    selected.add(effective)
                    pending.append(effective)
            for edge in dependencies:
                if edge['task_id'] == current and edge['depends_on'] not in selected:
                    selected.add(edge['depends_on'])
                    pending.append(edge['depends_on'])
    tasks = []
    reasons = []
    for key in sorted(selected):
        task = all_tasks[key]
        item = {k: task[k] for k in ('id', 'assignee', 'status', 'requirements_version', 'review_required', 'updated')}
        if version >= 14:
            item['obligations_satisfied'] = view._task_obligation_satisfied(db, key)
            item['effective_task'] = view._effective_obligation_task(db, key)
        item['admission'] = {}
        if task['status'] not in ('completed', 'integrated', 'cancelled') and not item.get('obligations_satisfied', False):
            targets = [m['id'] for m in members] if task['assignment_mode'] == 'auto' else [task['assignee']]
            for member in targets:
                found = view._admission_reasons(db, task, member)
                item['admission'][member] = found
                reasons.extend(found)
        tasks.append(item)
    stages = [dict(r) for r in db.execute('SELECT id,task_id,version,requirements_version,digest,status,execution_id,updated '
                                        'FROM review_stages ORDER BY id') if r['task_id'] in selected]
    limits = []
    for member in members:
        task_items = view._task_backlog_ids(db, member['id']) if version >= 14 else [r['id'] for r in all_tasks.values() if r['assignee'] == member['id'] and r['status'] in ('review_wait', 'needs_validation')]
        stage_items = view._stage_backlog_ids(db, member['id']) if version >= 14 else [r[0] for r in db.execute("SELECT s.id FROM review_stages s JOIN tasks t ON t.id=s.task_id "
                       "WHERE t.assignee=? AND s.status IN ('pending','running','needs_validation') ORDER BY s.id", (member['id'],))]
        for name, items in (('task_capacity', task_items), ('stage_capacity', stage_items)):
            limit = capacity(db, name)
            limits.append({'kind': name, 'member': member['id'], 'limit': limit, 'count': len(items), 'items': items})
            if name == 'stage_capacity' and len(items) >= limit:
                reasons.append({'code': 'STAGE_REVIEW_BACKLOG_LIMIT', 'rule': 'stage.review_backlog',
                                'member': member['id'], 'objects': items, 'owner': 'A'})
    messages = [dict(r) for r in db.execute('SELECT id,task_id,recipient,kind,delivery_status,process_status,turn_id,remote_status,updated '
                                          'FROM messages ORDER BY id') if r['task_id'] in selected or r['task_id'] is None]
    unresolved = [m for m in messages if m['delivery_status'] in ('delivering', 'unknown') or
                  m['delivery_status'] == 'accepted' and m['remote_status'] in (None, 'queued', 'inProgress')]
    approvals = [dict(r) for r in db.execute("SELECT id,task_id,member,status FROM approval_requests WHERE status IN ('pending','allowed','granted','unknown') ORDER BY id")]
    checkpoints = [dict(r) for r in db.execute("SELECT member,generation,digest,status,updated FROM checkpoints WHERE status NOT IN ('consumed','transferred') ORDER BY member")]
    resources = [dict(r) for r in db.execute('SELECT resource,task_id,owner,created FROM resource_locks ORDER BY resource')]
    for message in unresolved:
        reasons.append({'code': 'EXECUTION_UNRESOLVED', 'rule': 'message.delivery_and_turn',
                        'objects': [message['id']], 'owner': message['recipient']})
    for approval in approvals:
        reasons.append({'code': 'APPROVAL_UNRESOLVED', 'rule': 'approval.original_operation',
                        'objects': [approval['id']], 'owner': 'A'})
    quotas = {member: {k: q.get(k) for k in ('remaining','reliability','observed_at','age_seconds','limit_id')}
              for member, q in view._quotas(db).items()}
    material = {'pair': dict(pair), 'schema': version, 'tasks': tasks, 'members': members, 'stages': stages,
                'dependencies': [d for d in dependencies if d['task_id'] in selected], 'limits': limits,
                'messages': messages, 'unresolved_executions': unresolved, 'approvals': approvals,
                'resources': resources, 'checkpoints': checkpoints, 'quotas': quotas,
                'last_event': db.execute('SELECT max(seq) FROM events').fetchone()[0], 'reasons': reasons}
    if version >= 14:
        try:from .pair_recovery_graph import graph_snapshot
        except ImportError:from pair_recovery_graph import graph_snapshot
        material['recovery_graph']=graph_snapshot(view,db)
        material['supersessions'] = [dict(row, effective=view._supersession_current(db, row)) for row in db.execute(
            'SELECT * FROM obligation_supersessions ORDER BY created,id') if row['source_task'] in selected]
    # age_seconds 随时钟变化，不是状态变更；快照摘要只绑定已观测事实。
    canonical = dict(material, quotas={m: {k:v for k,v in q.items() if k != 'age_seconds'} for m,q in quotas.items()})
    digest = hashlib.sha256(json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()
    return dict(material, id='blockers-'+digest[:24], version=1, created_at=observed, state_digest=digest,
                read_only=True, freshness='database_snapshot_only', runtime_observed=False,
                safe_actions=['read_existing_delivery', 'reconcile_original_execution'],
                actions_requiring_authorization=['change_policy', 'replace_member', 'apply_reviewed_patch'],
                forbidden_actions=['replay_unknown', 'direct_database_edit', 'bypass_review', 'resume_stopped_scope'],
                excluded_history=[key for key in all_tasks if key not in selected],
                next_action='逐项核对原因及原记录；实际变更前重新读取', wake_on='有效状态事件或明确用户指令')


def diagnose(state_root, pair_id, *, task_id=None):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', pair_id):
        raise ValueError('Invalid pair identifier / 协作ID无效')
    path = Path(state_root).absolute() / pair_id / 'state.sqlite3'
    if path.resolve() != path or not path.is_file():
        raise ValueError('State missing or redirected / 状态库不存在或路径重定向；不会创建或修复')
    with _read_guard(path) as stable_main:
        uri = path.as_uri() + '?mode=ro' + ('&immutable=1' if stable_main else '')
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            try:
                return snapshot_from_db(db, pair_id, task_id=task_id)
            finally:
                db.rollback()


@contextmanager
def _read_guard(path):
    """mode=ro仍可能写WAL侧文件；Windows共享模式禁止写入，拿不到安全快照就报错。"""
    if os.name != 'nt':
        raise ValueError('Read-only VFS unavailable / 本候选仅提供Windows无写入读取保护')
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,
                                  wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    class FileInfo(ctypes.Structure):
        _fields_=[('attributes',wintypes.DWORD),('creation',wintypes.FILETIME),
                  ('access',wintypes.FILETIME),('write',wintypes.FILETIME),
                  ('volume',wintypes.DWORD),('size_high',wintypes.DWORD),('size_low',wintypes.DWORD),
                  ('links',wintypes.DWORD),('index_high',wintypes.DWORD),('index_low',wintypes.DWORD)]
    kernel.GetFileInformationByHandle.argtypes=[wintypes.HANDLE,ctypes.POINTER(FileInfo)]
    kernel.GetFileInformationByHandle.restype=wintypes.BOOL
    kernel.GetFileType.argtypes=[wintypes.HANDLE]
    kernel.GetFileType.restype=wintypes.DWORD
    handles = []
    present = set()
    try:
        # 主库先拒绝新写句柄；已有写者会使本次读取明确失败，不抢其运行权。
        for suffix in ('', '-wal', '-shm', '-journal'):
            source = Path(str(path)+suffix)
            if suffix:
                try:source.lstat()
                except FileNotFoundError:continue
            if source.resolve()!=source.absolute():
                raise ValueError('Redirected database side file / 状态文件路径存在重定向')
            handle = kernel.CreateFileW(str(source),0x80000000,0x1,None,3,0x00200000,None)
            if handle == ctypes.c_void_p(-1).value:
                raise OSError(ctypes.get_last_error(), 'Read-only snapshot unavailable / 当前数据库或侧文件不能无写入读取')
            handles.append(handle)
            info=FileInfo()
            if not kernel.GetFileInformationByHandle(handle,ctypes.byref(info)):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.attributes & (0x400|0x10) or kernel.GetFileType(handle)!=1:
                raise ValueError('Nonregular state file / 状态库及侧文件须为非重定向普通文件')
            present.add(suffix)
        with path.open('rb') as file:
            header = file.read(100)
        if len(header) < 100 or header[:16] != b'SQLite format 3\x00':
            raise ValueError('Invalid database header / 无效SQLite文件')
        if '-wal' in present and '-shm' not in present:
            raise ValueError('Read-only WAL unavailable / WAL侧文件缺失；诊断不会创建它们或忽略WAL')
        if '-journal' in present:
            raise ValueError('Journal reconciliation required / 存在回滚日志，诊断不执行恢复')
        # 只有主库已被内核禁止写入且确认没有WAL时才可声明immutable；活动WAL绝不忽略。
        yield '-wal' not in present
    finally:
        for handle in reversed(handles):
            kernel.CloseHandle(handle)
