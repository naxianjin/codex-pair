"""schema 14 恢复记录与门禁；没有可信执行证据时不启动或放行恢复。"""
from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import secrets
import time
import uuid

try:
    from .pair_reviewer import fail, reviewer_identity, safe_evidence
except ImportError:
    from pair_reviewer import fail, reviewer_identity, safe_evidence


DOMAINS = ('transport', 'continuation', 'review_recovery', 'patch_application')
ATTEMPT_LIMITS = {'coordination': (3, None), 'product_repair': (2, None),
                  'approval_failure': (2, None), 'bridge_restart': (3, 600),
                  'auxiliary_start': (64, None)}
MANAGEMENT_ACTIONS = frozenset(('read', 'control', 'save', 'reconcile', 'record'))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def upgrade_recovery_v14(db):
    """由 Store 的迁移事务调用；不提交事务，不设置 user_version。"""
    statements = (
        '''CREATE TABLE recovery_policies(domain TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
           version INTEGER NOT NULL DEFAULT 1, authorization_version INTEGER, goal_digest TEXT,
           basis TEXT, actor_session TEXT, binding_version INTEGER, updated REAL NOT NULL DEFAULT 0)''',
        '''CREATE TABLE control_requests(id TEXT PRIMARY KEY, kind TEXT NOT NULL, generation INTEGER NOT NULL,
           scope TEXT NOT NULL, state TEXT NOT NULL, request_digest TEXT NOT NULL,
           idempotency_key TEXT NOT NULL UNIQUE, source TEXT NOT NULL, evidence TEXT NOT NULL,
           parent_id TEXT, created REAL NOT NULL, updated REAL NOT NULL)''',
        '''CREATE TABLE control_releases(control_id TEXT NOT NULL REFERENCES control_requests(id),
           task_id TEXT NOT NULL REFERENCES tasks(id), resume_id TEXT NOT NULL REFERENCES control_requests(id),
           evidence TEXT NOT NULL, PRIMARY KEY(control_id,task_id))''',
        '''CREATE TABLE control_scope_releases(control_id TEXT NOT NULL REFERENCES control_requests(id),
           scope TEXT NOT NULL, resume_id TEXT NOT NULL REFERENCES control_requests(id), evidence TEXT NOT NULL,
           PRIMARY KEY(control_id,scope))''',
        '''CREATE TABLE control_event_cursors(member TEXT NOT NULL, session TEXT NOT NULL,
           binding_version INTEGER NOT NULL, source_identity TEXT NOT NULL, position INTEGER NOT NULL,
           generation INTEGER NOT NULL, PRIMARY KEY(member,session,binding_version,source_identity))''',
        '''CREATE TABLE continuation_intents(id TEXT PRIMARY KEY, task_id TEXT, logical_goal TEXT NOT NULL,
           action TEXT NOT NULL, payload TEXT NOT NULL, request_digest TEXT NOT NULL,
           idempotency_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)''',
        '''CREATE TABLE recovery_outbox(id TEXT PRIMARY KEY, intent_id TEXT REFERENCES continuation_intents(id),
           recipient TEXT NOT NULL, body TEXT NOT NULL, task_id TEXT, state TEXT NOT NULL DEFAULT 'pending',
           message_id TEXT, created REAL NOT NULL)''',
        '''CREATE TABLE recovery_attempts(kind TEXT NOT NULL, stable_id TEXT NOT NULL, logical_id TEXT NOT NULL,
           state TEXT NOT NULL, started REAL NOT NULL, updated REAL NOT NULL, evidence TEXT NOT NULL,
           PRIMARY KEY(kind,stable_id,logical_id))''',
        '''CREATE TABLE recovery_requests(id TEXT PRIMARY KEY, source_task TEXT NOT NULL REFERENCES tasks(id),
           stable_blocker TEXT NOT NULL, proposal TEXT NOT NULL, proposal_digest TEXT NOT NULL,
           idempotency_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL, authorization_version INTEGER,
           policy_version INTEGER, reviewer TEXT NOT NULL, reviewer_session TEXT NOT NULL,
           binding_version INTEGER NOT NULL, expires_at REAL, claim_token TEXT, execution_epoch INTEGER NOT NULL DEFAULT 0,
           execution_id TEXT, control_generation INTEGER, report TEXT, created REAL NOT NULL, updated REAL NOT NULL)''',
        '''CREATE UNIQUE INDEX recovery_single_execution ON recovery_requests((1))
           WHERE status IN ('claimed','running','report_submitted','unknown')''',
        '''CREATE TABLE recovery_execution_attempts(recovery_id TEXT NOT NULL REFERENCES recovery_requests(id),
           epoch INTEGER NOT NULL, execution_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
           evidence TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, PRIMARY KEY(recovery_id,epoch))''',
        '''CREATE TABLE recovery_reads(recovery_id TEXT NOT NULL REFERENCES recovery_requests(id),
           member TEXT NOT NULL, session TEXT NOT NULL, binding_version INTEGER NOT NULL,
           digest TEXT NOT NULL, read_end INTEGER NOT NULL, PRIMARY KEY(recovery_id,member,session,binding_version))''',
        '''CREATE TABLE recovery_delivery_grants(recovery_id TEXT NOT NULL REFERENCES recovery_requests(id),
           delivery_id TEXT NOT NULL REFERENCES deliveries(id), recipient TEXT NOT NULL,
           delivery_digest TEXT NOT NULL, proposal_digest TEXT NOT NULL, expires_at REAL NOT NULL,
           revoked INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(recovery_id,delivery_id,recipient))''',
        '''CREATE TABLE recovery_applications(id TEXT PRIMARY KEY, recovery_id TEXT NOT NULL REFERENCES recovery_requests(id),
           stage_id TEXT NOT NULL REFERENCES review_stages(id), candidate_digest TEXT NOT NULL,
           plan TEXT NOT NULL, request_digest TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
           policy_version INTEGER NOT NULL, authorization_version INTEGER NOT NULL,
           state TEXT NOT NULL, generation INTEGER NOT NULL, evidence TEXT, created REAL NOT NULL, updated REAL NOT NULL)''',
        '''CREATE TABLE task_continuations(source_task TEXT PRIMARY KEY REFERENCES tasks(id),
           successor_task TEXT NOT NULL UNIQUE REFERENCES tasks(id), root_task TEXT NOT NULL REFERENCES tasks(id),
           source_digest TEXT NOT NULL, source_version INTEGER NOT NULL, plan TEXT NOT NULL,
           required_deliveries TEXT NOT NULL, contributors TEXT NOT NULL, intent_id TEXT NOT NULL UNIQUE,
           actor_session TEXT NOT NULL,binding_version INTEGER NOT NULL,created REAL NOT NULL)''',
        '''CREATE TABLE obligation_supersessions(id TEXT PRIMARY KEY,source_task TEXT NOT NULL REFERENCES tasks(id),
           source_stage TEXT NOT NULL DEFAULT '',source_digest TEXT NOT NULL,
           replacement_stage TEXT NOT NULL REFERENCES review_stages(id),replacement_digest TEXT NOT NULL,
           replacement_proof TEXT NOT NULL,coverage TEXT NOT NULL,request_digest TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,
           state TEXT NOT NULL,actor_session TEXT NOT NULL,binding_version INTEGER NOT NULL,
           created REAL NOT NULL,invalidated_reason TEXT)''',
        '''CREATE UNIQUE INDEX active_obligation_supersession ON obligation_supersessions(source_task,source_stage)
           WHERE state='active' ''',
    )
    for statement in statements:
        db.execute(statement)
    db.executemany('INSERT INTO recovery_policies(domain) VALUES(?)', [(domain,) for domain in DOMAINS])


@dataclass(frozen=True)
class VerifiedRecoveryEvidence:
    """仅供宿主可信适配返回；CLI 输入不能构造或登记这种证明。"""
    kind: str
    context_digest: str
    receipts: tuple[str, ...]
    facts: dict


def candidate_manifest(root, manifest, allowed_paths):
    """只读取固定候选；拒绝真实路径重定向及清单外路径。"""
    if not isinstance(root, str) or not Path(root).is_absolute():
        fail('Absolute candidate root required / 候选根目录须为绝对路径')
    base = Path(root)
    if base.resolve() != base.absolute() or not base.is_dir():
        fail('Redirected candidate root / 候选目录不存在或存在重定向')
    if not isinstance(manifest, list) or not 1 <= len(manifest) <= 10000:
        fail('Fixed manifest required / 候选需要非空、有界清单')
    if not isinstance(allowed_paths, list) or not allowed_paths:
        fail('Allowed paths required / 恢复需要准确允许范围')
    paths = []
    for value in allowed_paths:
        if not isinstance(value, str) or not value or '\\' in value:
            fail('Relative paths required / 使用正斜杠相对路径')
        path = Path(value)
        if path.is_absolute() or path.drive or '..' in path.parts or ':' in value:
            fail('Path outside candidate / 允许范围越界')
        paths.append(value.rstrip('/'))
    seen = set()
    for item in manifest:
        if not isinstance(item, dict) or set(item) != {'path', 'sha256'}:
            fail('Manifest fields required / 清单项只包含path与sha256')
        relative = item['path']
        if (not isinstance(relative, str) or not relative or '\\' in relative or ':' in relative
                or Path(relative).is_absolute() or '..' in Path(relative).parts
                or not isinstance(item['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', item['sha256'])):
            fail('Invalid manifest entry / 清单路径或摘要无效')
        key = relative.casefold()
        if key in seen:
            fail('Duplicate manifest path / 清单路径重复')
        seen.add(key)
        if not any(scope == '.' or relative == scope or relative.startswith(scope + '/') for scope in paths):
            fail('Manifest outside allowed scope / 候选文件越出允许范围')
        target = base / relative
        if target.resolve() != target.absolute() or not target.is_file():
            fail('Redirected candidate file / 候选文件不存在或存在链接重定向')
        checksum = hashlib.sha256()
        with target.open('rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                checksum.update(block)
        if checksum.hexdigest() != item['sha256']:
            fail('Candidate changed / 候选内容与固定清单不符')
    return digest(sorted(manifest, key=lambda item: item['path']))


class RecoveryMixin:
    def recovery_policy(self, member, session, cwd, domain, *, enabled, basis, expected_version):
        if domain not in DOMAINS or type(enabled) is not bool:
            fail('Invalid recovery policy / 恢复策略域或开关无效')
        safe_evidence(basis)
        with self._recovery_control_transaction() as db:
            actor = self._a_actor(db, member, session, cwd)
            row = db.execute('SELECT * FROM recovery_policies WHERE domain=?', (domain,)).fetchone()
            if row['version'] != expected_version:
                fail('Policy version changed / 恢复策略版本已变化')
            if enabled and (db.execute('SELECT status FROM collaborations').fetchone()[0] == 'stopped'
                    or self._option(db, 'closed_scope') and not any(not self._task_scope_closed(db, task['id'])
                        for task in db.execute("SELECT id FROM tasks WHERE status NOT IN ('cancelled','completed','integrated')"))):
                fail('Inactive recovery scope / 已停止或已收尾范围不能启用恢复策略')
            version = row['version'] + 1
            db.execute('''UPDATE recovery_policies SET enabled=?,version=?,authorization_version=?,goal_digest=?,
                          basis=?,actor_session=?,binding_version=?,updated=? WHERE domain=?''',
                       (enabled, version, self._authorization_version(db), self._recovery_goal(db), basis,
                        actor['session_uuid'], actor['binding_version'], time.time(), domain))
            self.event(db, 'recovery_policy_changed', domain=domain, enabled=enabled, version=version)
            return {'domain': domain, 'enabled': enabled, 'version': version, 'other_domains_unchanged': True}

    def request_pause(self, member, session, cwd, *, reason, idempotency_key, scope=None, source=None):
        safe_evidence(reason)
        scope = self._control_scope(scope)
        with self._recovery_control_transaction() as db:
            actor = self._binding(db, member, session, cwd)
            request = {'scope': scope, 'reason': reason, 'source': source or {}, 'session': actor['session_uuid']}
            safe_evidence(encoded(request))
            previous = self._control_duplicate(db, idempotency_key, request)
            if previous:
                return previous
            return self._pause_record(db, actor, scope, request, idempotency_key)

    def request_resume(self, member, session, cwd, *, instruction, idempotency_key, scope=None, source=None):
        safe_evidence(instruction)
        scope = self._control_scope(scope)
        with self._recovery_control_transaction() as db:
            actor = self._binding(db, member, session, cwd)
            if (db.execute('SELECT status FROM collaborations').fetchone()[0] == 'stopped'
                    or self._task_scope_closed(db, scope['id'] if scope['kind']=='task' else None)):
                fail('Explicit new scope required / 普通继续不能复活已停止协作或已收尾范围')
            request = {'scope': scope, 'instruction': instruction, 'source': source or {}, 'session': actor['session_uuid']}
            safe_evidence(encoded(request))
            previous = self._control_duplicate(db, idempotency_key, request)
            if previous:
                return previous
            existing = db.execute("SELECT * FROM control_requests WHERE kind='resume' AND scope=? AND generation=? AND state IN ('reconciling','blocked','partial') ORDER BY generation DESC LIMIT 1", (encoded(scope), self._control_generation(db))).fetchone()
            if existing:
                return {'id': existing['id'], 'generation': existing['generation'], 'state': existing['state'], 'duplicate': True}
            generation = self._new_control_generation(db)
            now = time.time(); key = uuid.uuid4().hex
            db.execute('''INSERT INTO control_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                       (key, 'resume', generation, encoded(scope), 'reconciling', digest(request), idempotency_key,
                        encoded(request), '{}', None, now, now))
            if scope['kind'] == 'pair':
                db.execute("UPDATE collaborations SET status='paused',revision=revision+1")
            self.event(db, 'resume_requested', control=key, generation=generation, member=member,
                       source_verification='required', ordinary_work_blocked=True)
            return {'id': key, 'generation': generation, 'state': 'reconciling', 'duplicate': False,
                    'source_verification': 'required', 'authority_transferred': False}

    def reconcile_resume(self, member, session, cwd, control_id, *, tasks):
        if not isinstance(tasks, list) or any(not isinstance(task, str) for task in tasks):
            fail('Exact tasks required / 恢复核查须明确任务')
        with self._recovery_control_transaction() as db:
            self._a_actor(db, member, session, cwd)
            row = db.execute("SELECT * FROM control_requests WHERE id=? AND kind='resume'", (control_id,)).fetchone()
            if not row or row['state'] not in ('reconciling', 'blocked', 'partial'):
                fail('Resume reconciliation unavailable / 当前恢复核查不可用')
            if self._control_generation(db) != row['generation']:
                fail('New control event / 恢复登记后存在新控制事件，不能忽略新暂停')
            scope = json.loads(row['scope'])
            if (db.execute('SELECT status FROM collaborations').fetchone()[0] == 'stopped'
                    or self._task_scope_closed(db, scope['id'] if scope['kind']=='task' else None)):
                fail('Scope no longer active / 协作已停止或范围已收尾')
            expected = {task['id'] for task in db.execute("SELECT id,assignee FROM tasks WHERE status NOT IN ('cancelled','completed','integrated')")
                        if scope['kind'] == 'pair' or scope == {'kind': 'member', 'id': task['assignee']} or scope == {'kind': 'task', 'id': task['id']}}
            if set(tasks) - expected:
                fail('Task outside resume scope / 核查任务不在本次恢复范围')
            controls = [dict(item) for item in db.execute("SELECT * FROM control_requests WHERE state IN ('pausing','paused','reconciling','blocked','partial')")]
            context = {'control': dict(row), 'tasks': sorted(set(tasks)), 'expected_scope_tasks': sorted(expected),
                       'controls': controls, 'state_digest': self._recovery_state_digest(db)}
            proof = self._trusted_recovery_evidence('resume', context)
            if proof.facts.get('source_is_current_user_instruction') is not True or proof.facts.get('all_required_gates_verified') is not True:
                fail('Resume evidence incomplete / 明确继续来源或执行门禁尚未核验')
            full_scope = proof.facts.get('full_scope_reconciled') is True and set(tasks) == expected
            if not tasks and not full_scope:
                fail('Full scope evidence required / 空任务恢复仍须有完整范围及在途执行证明')
            if full_scope and scope['kind'] == 'pair' and self._recovery_unresolved(db):
                fail('Execution unresolved / 全组恢复仍有未决审批或原执行')
            for task_id in sorted(set(tasks)):
                task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
                if not task or task['status'] in ('cancelled', 'completed', 'integrated'):
                    fail('Task not resumable / 任务不可恢复或已完成')
                actor = self._member(db, task['assignee'])
                if not self._member_eligible(db, actor['id'], dict(task)) or self._member_suspended(db, actor['id']) or self._pool_blocked(db, actor['id']):
                    fail('Member unavailable / 当前成员资格或额度不允许接续')
                if self._recovery_unresolved(db, task_id):
                    fail('Execution unresolved / 任务原执行或审批结果未明确')
                for gate in self._matching_controls(db, actor['id'], task_id):
                    db.execute('INSERT OR REPLACE INTO control_releases VALUES(?,?,?,?)',
                               (gate['id'], task_id, control_id, encoded({'receipts': proof.receipts, 'context_digest': proof.context_digest})))
            evidence = encoded({'receipts': proof.receipts, 'context_digest': proof.context_digest})
            if full_scope:
                for control in controls:
                    prior_scope = json.loads(control['scope'])
                    if scope['kind'] == 'pair' or prior_scope == scope:
                        db.execute("UPDATE control_requests SET state='reconciled',evidence=?,updated=? WHERE id=?", (evidence, time.time(), control['id']))
                    elif scope['kind'] == 'member' and prior_scope['kind'] == 'pair':
                        db.execute('INSERT OR REPLACE INTO control_scope_releases VALUES(?,?,?,?)',
                                   (control['id'], encoded(scope), control_id, evidence))
            state = 'reconciled' if full_scope else 'partial'
            db.execute('UPDATE control_requests SET state=?,evidence=?,updated=? WHERE id=?',
                       (state, encoded({'receipts': proof.receipts, 'tasks': sorted(set(tasks))}), time.time(), control_id))
            db.execute("UPDATE collaborations SET status='running',revision=revision+1")
            self.event(db, 'resume_scope_reconciled', control=control_id, tasks=sorted(set(tasks)), proof_digest=proof.context_digest)
            return {'id': control_id, 'state': state, 'resumed_tasks': sorted(set(tasks)),
                    'remaining_gates_preserved': True, 'policies_unchanged': True}

    def confirm_pause(self, member, session, cwd, control_id):
        with self.transaction() as db:
            self._binding(db, member, session, cwd)
            row = db.execute("SELECT * FROM control_requests WHERE id=? AND kind='pause'", (control_id,)).fetchone()
            if not row or row['state'] not in ('pausing', 'paused'):
                fail('Pause record unavailable / 没有待核实暂停记录')
            context = {'control': dict(row), 'state_digest': self._recovery_state_digest(db)}
            proof = self._trusted_recovery_evidence('pause_confirmation', context)
            if proof.facts.get('all_scope_executors_stopped') is not True or proof.facts.get('inflight_operations_reconciled') is not True:
                return {'id': control_id, 'state': 'pausing', 'all_executors_stopped': False}
            db.execute("UPDATE control_requests SET state='paused',evidence=?,updated=? WHERE id=?",
                       (encoded({'receipts': proof.receipts}), time.time(), control_id))
            self.event(db, 'pause_confirmed', control=control_id, evidence=proof.receipts)
            return {'id': control_id, 'state': 'paused', 'all_executors_stopped': True, 'resources_preserved': True}

    def recovery_status(self, member, session, cwd):
        with self.transaction() as db:
            self._binding(db, member, session, cwd)
            return {'policies': [dict(row) for row in db.execute('SELECT * FROM recovery_policies ORDER BY domain')],
                    'control_generation': self._control_generation(db),
                    'controls': [dict(row) for row in db.execute('SELECT id,kind,generation,scope,state,created,updated FROM control_requests ORDER BY created')],
                    'requests': [dict(row) for row in db.execute('SELECT id,source_task,stable_blocker,status,reviewer,execution_epoch,execution_id,created,updated FROM recovery_requests ORDER BY created')],
                    'counts': [dict(row) for row in db.execute('SELECT kind,stable_id,count(*) attempts FROM recovery_attempts GROUP BY kind,stable_id')],
                    'dynamic_adapter_available': getattr(self, '_recovery_evidence_adapter', None) is not None}

    def inspect_remaining_work(self, member, session, cwd, source_task):
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd)
            source, fingerprint = self._remaining_source(db, source_task)
            return {'task': source_task, 'status': source['status'], 'source_digest': fingerprint,
                    'requirements_version': source['requirements_version'], 'assignee': source['assignee'],
                    'allowed_paths': json.loads(source['allowed_paths']), 'acceptance': json.loads(source['acceptance']),
                    'required_checks': json.loads(source['required_checks']),
                    'dependencies': [r[0] for r in db.execute('SELECT depends_on FROM dependencies WHERE task_id=? ORDER BY depends_on', (source_task,))],
                    'execution_unresolved': self._recovery_unresolved(db, source_task),
                    'existing_successor': self._continuation_successor(db, source_task),
                    'old_claim_reusable': False}

    def continue_remaining_work(self, member, session, cwd, source_task, new_task, expected_digest, *,
                                remaining_steps, completed_effects, candidate, evidence, idempotency_key, target=None):
        try:
            from .pair_core import identifier
        except ImportError:
            from pair_core import identifier
        identifier(new_task)
        if new_task.startswith('pair-plan-') or new_task == source_task:
            fail('New business identity required / 剩余工作须使用独立业务任务ID')
        if not isinstance(remaining_steps, list) or not remaining_steps or not isinstance(completed_effects, list):
            fail('Explicit remaining plan required / 须明确剩余步骤及已经发生的副作用')
        all_ids = set()
        for group, fields in ((remaining_steps, {'id', 'description'}), (completed_effects, {'id', 'evidence'})):
            for item in group:
                if (not isinstance(item, dict) or set(item) != fields or any(not isinstance(value, str) or not value.strip() for value in item.values())
                        or item['id'] in all_ids):
                    fail('Distinct remaining effects required / 剩余步骤与已完成副作用须准确且不得重复')
                all_ids.add(item['id'])
        if not isinstance(candidate, dict) or set(candidate) != {'root', 'manifest'}:
            fail('Fixed continuation candidate required / 接续须保存准确来源目录和文件清单')
        if (not isinstance(candidate['root'], str) or not Path(candidate['root']).is_absolute()
                or target is not None and target not in ('A', 'B', 'C', 'D')):
            fail('Invalid continuation target / 接续须使用准确绝对工作树及有效成员')
        safe_evidence(evidence)
        request = {'source_task': source_task, 'new_task': new_task, 'expected_digest': expected_digest,
                   'remaining_steps': remaining_steps, 'completed_effects': completed_effects, 'candidate': candidate,
                   'evidence': evidence, 'target': target}
        safe_evidence(encoded(request))
        with self._recovery_control_transaction() as db:
            actor = self._a_actor(db, member, session, cwd)
            source, fingerprint = self._remaining_source(db, source_task)
            old = db.execute('SELECT * FROM continuation_intents WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if old:
                original = json.loads(old['payload'])['payload']
                if original['plan'] != request:
                    fail('Remaining plan conflict / 同一接续请求不能改变剩余工作或副作用清单')
                if old['state'] == 'completed':
                    return {'intent': old['id'], 'task': new_task, 'state': 'registered', 'duplicate': True, 'old_claim_reactivated': False}
                return self._execute_remaining_intent(db, old)
            if source['status'] != 'handed_back' or fingerprint != expected_digest:
                fail('Saved source changed / 仅接续准确已交回任务，来源状态或版本已变化')
            if self._continuation_successor(db, source_task) or db.execute('SELECT 1 FROM tasks WHERE id=?', (new_task,)).fetchone():
                fail('Continuation already exists / 原任务已有接续者或新任务ID已使用')
            self._validate_remaining_candidate(db, source, candidate, target or source['assignee'])
            delivery = self._delivery(db, dict(source))
            self._put(db, 'remaining_read:' + source_task, {'member': member, 'session': actor['session_uuid'],
                'binding_version': actor['binding_version'], 'delivery_digest': delivery['digest'], 'source_digest': fingerprint})
            if not db.execute('SELECT 1 FROM record_acks WHERE member=? AND delivery_id=? AND digest=?',
                              (member, delivery['id'], delivery['digest'])).fetchone():
                return {'state': 'requires_source_read', 'source_task': source_task, 'delivery': delivery['id'],
                        'digest': delivery['digest'], 'new_task_created': False}
            intent = self._record_continuation(db, logical_goal=self._continuation_root(db, source_task), action='continue_remaining_work',
                payload={'plan': request, 'actor_session': actor['session_uuid'], 'binding_version': actor['binding_version']},
                idempotency_key=idempotency_key, task_id=source_task)
            row = db.execute('SELECT * FROM continuation_intents WHERE id=?', (intent['id'],)).fetchone()
            return self._execute_remaining_intent(db, row)

    def supersede_obligations(self, member, session, cwd, source_task, replacement_stage, expected_source_digest, *,
                             coverage, idempotency_key, source_stage=None):
        with self._recovery_control_transaction() as db:
            actor = self._a_actor(db, member, session, cwd)
            return self._settle_supersession(db, actor, source_task, replacement_stage, expected_source_digest,
                                             coverage, idempotency_key, source_stage or '')

    def recover_safe(self, member, session, cwd, *, intent_ids=None):
        if intent_ids is not None and (not isinstance(intent_ids, list) or any(not isinstance(key, str) for key in intent_ids)):
            fail('Exact intents required / 指定接续意图必须使用ID列表')
        try:
            from .pair_core import PairError
        except ImportError:
            from pair_core import PairError
        with self._recovery_control_transaction() as db:
            self._a_actor(db, member, session, cwd)
            before = [dict(row) for row in db.execute('SELECT * FROM recovery_policies ORDER BY domain')]
            results = []
            for intent in db.execute("SELECT * FROM continuation_intents WHERE state='pending' ORDER BY created,id").fetchall():
                if intent_ids is not None and intent['id'] not in intent_ids:
                    continue
                if intent['action'] == 'continue_remaining_work':
                    db.execute('SAVEPOINT safe_continuation')
                    try:
                        result = self._execute_remaining_intent(db, intent)
                    except PairError as exc:
                        db.execute('ROLLBACK TO safe_continuation')
                        result = {'intent': intent['id'], 'decision': 'blocked_no_safe_action', 'reason': str(exc)}
                    finally:
                        db.execute('RELEASE safe_continuation')
                    results.append(result)
                else:
                    results.append({'intent': intent['id'], 'decision': 'wait_for_event',
                                    'reason': 'requires_existing_coordinator_workflow', 'action': intent['action']})
            self._recovery_tick(db)
            after = [dict(row) for row in db.execute('SELECT * FROM recovery_policies ORDER BY domain')]
            if before != after:
                fail('Recovery policy changed unexpectedly / safe恢复不得修改四域策略')
            return {'decision': 'execute_next' if any(item.get('state') == 'registered' for item in results) else 'wait_for_event',
                    'intents': results, 'policies_unchanged': True, 'unknown_operations_replayed': False,
                    'models_started': False, 'runtime_switched': False}

    def _execute_remaining_intent(self, db, intent):
        request = json.loads(intent['payload']); payload = request['payload']; plan = payload['plan']
        source, fingerprint = self._remaining_source(db, plan['source_task'])
        target = plan['target'] or source['assignee']
        def wait(reason, detail=None):
            return {'intent': intent['id'], 'task': plan['new_task'], 'decision': 'wait_for_event', 'reason': reason, 'detail': detail}
        if not self._recovery_policy_valid(db, 'continuation'):
            return wait('continuation_policy_disabled')
        coordinator = self._member(db, self._coordinator(db))
        if (coordinator['session_uuid'] != payload['actor_session'] or coordinator['binding_version'] != payload['binding_version']
                or request['authorization_version'] != self._authorization_version(db)):
            return wait('continuation_authorization_changed')
        if source['status'] != 'handed_back' or fingerprint != plan['expected_digest']:
            return wait('source_changed_requires_new_plan')
        if self._continuation_successor(db, source['id']):
            return wait('source_already_has_successor')
        reasons = self._recovery_gate(db, member=target, task_id=source['id'])
        if reasons:
            return wait('control_gate', reasons)
        if self._recovery_unresolved(db, source['id'], ignore_terminal_workspace_activity=True):
            return wait('EXECUTION_UNRESOLVED')
        self._require_ack(db, self._coordinator(db), 'delivery:' + source['id'])
        candidate = dict(source, assignee=target, status='pending', assignment_mode='fixed')
        admission = self._admission_reasons(db, candidate, target)
        if admission:
            return wait('admission_wait', admission)
        self._validate_remaining_candidate(db, source, plan['candidate'], target)
        if db.execute('SELECT 1 FROM tasks WHERE id=?', (plan['new_task'],)).fetchone():
            fail('Continuation task collision / 接续任务ID已被其他工作使用')
        contributors = self._continuation_contributors(db, source['id'], include_self=True)
        if not contributors:
            fail('Source provenance unverified / 原工作实际模型来源缺失，先核对而非猜测')
        delivery = db.execute('SELECT id FROM deliveries WHERE id=?', ('delivery:' + source['id'],)).fetchone()
        if not delivery:
            self._delivery(db, dict(source))
        required = ['delivery:' + source['id']] + self._continuation_required_deliveries(db, source['id'])
        required = list(dict.fromkeys(required))
        now = time.time()
        body = encoded({'source_task': source['id'], 'source_digest': fingerprint,
                        'remaining_steps': plan['remaining_steps'], 'already_completed_effects_do_not_repeat': plan['completed_effects'],
                        'fixed_candidate': plan['candidate'], 'required_deliveries': required,
                        'original_acceptance_unchanged': json.loads(source['acceptance']),
                        'instruction': '只执行列明的剩余步骤。先完整读取并确认原交付，核对当前候选；不得复用旧领取、重做已完成副作用或把旧义务当已通过。'})
        successor = dict(source)
        successor.update(id=plan['new_task'], title='接续：' + source['title'], body=body, assignee=target,
                         status='pending', result=None, commits_json=None, created=now, updated=now,
                         assignment_mode='fixed', assignment_reason='explicit_remaining_work')
        columns = list(successor)
        db.execute('INSERT INTO tasks(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')',
                   [successor[column] for column in columns])
        for dependency in db.execute('SELECT depends_on FROM dependencies WHERE task_id=?', (source['id'],)).fetchall():
            db.execute('INSERT INTO dependencies VALUES(?,?)', (successor['id'], dependency[0]))
        policy = self._option(db, 'task_execution_policy:' + source['id'], {})
        self._put(db, 'task_execution_policy:' + successor['id'], policy)
        db.execute('INSERT INTO task_continuations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (source['id'], successor['id'], self._continuation_root(db, source['id']), fingerprint,
                    source['requirements_version'], encoded(plan), encoded(required), encoded(contributors),
                    intent['id'], payload['actor_session'], payload['binding_version'], now))
        db.execute("UPDATE continuation_intents SET state='completed',updated=? WHERE id=?", (now, intent['id']))
        notice = '已登记剩余工作，原任务及旧领取保持交回状态。source=' + source['id'] + '，successor=' + successor['id']
        db.execute('INSERT INTO recovery_outbox(id,intent_id,recipient,body,task_id,created) VALUES(?,?,?,?,?,?)',
                   ('remaining:' + intent['id'], intent['id'], self._coordinator(db), notice, successor['id'], now))
        self.event(db, 'remaining_work_registered', source=source['id'], successor=successor['id'], intent=intent['id'],
                   source_digest=fingerprint, original_claim_reactivated=False)
        self._schedule(db)
        return {'intent': intent['id'], 'task': successor['id'], 'state': 'registered', 'duplicate': False,
                'task_status': db.execute('SELECT status FROM tasks WHERE id=?', (successor['id'],)).fetchone()[0],
                'old_claim_reactivated': False, 'source_obligations_preserved': True}

    def _remaining_source(self, db, task_id):
        source = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not source:
            fail('Continuation source missing / 原任务不存在')
        material = {'task': {key: source[key] for key in source.keys() if key != 'updated'},
                    'execution_policy': self._option(db, 'task_execution_policy:' + task_id, {}),
                    'dependencies': [row[0] for row in db.execute('SELECT depends_on FROM dependencies WHERE task_id=? ORDER BY depends_on', (task_id,))],
                    'stages': [dict(row) for row in db.execute('SELECT id,digest,version,requirements_version,status FROM review_stages WHERE task_id=? ORDER BY version', (task_id,))]}
        return source, digest(material)

    def _validate_remaining_candidate(self, db, source, candidate, target):
        try:
            from .pair_core import member_workspace
            from .pair_transport import canonical_path
            from .pair_git import inspect_repo, _git
        except ImportError:
            from pair_core import member_workspace
            from pair_transport import canonical_path
            from pair_git import inspect_repo, _git
        owner = self._member(db, source['assignee']); receiver = self._member(db, target)
        if canonical_path(candidate['root']) != canonical_path(member_workspace(owner)):
            fail('Continuation source workspace mismatch / 固定候选不属于原任务登记工作树')
        allowed = json.loads(source['allowed_paths'])
        candidate_manifest(candidate['root'], candidate['manifest'], allowed)
        candidate_manifest(member_workspace(receiver), candidate['manifest'], allowed)
        git = db.execute('SELECT data FROM git_context WHERE id=1').fetchone()
        if git:
            context = json.loads(git[0]); expected = context.get('repo', context).get('common_dir')
            if expected:
                actual = inspect_repo(member_workspace(receiver))
                if canonical_path(actual['common_dir']) != canonical_path(expected):
                    fail('Continuation repository mismatch / 接续工作树不属于原项目仓库')
                if (not source['base_commit'] or not re.fullmatch(r'[0-9a-fA-F]{40}|[0-9a-fA-F]{64}', source['base_commit'])
                        or _git(member_workspace(receiver), ['merge-base', '--is-ancestor', source['base_commit'], 'HEAD'], check=False).returncode):
                    fail('Continuation baseline mismatch / 接续工作树未包含原任务准确基准')
        if not self._task_member_allowed(db, dict(source), target):
            fail('Original execution constraint / 接续不能更换被固定的原成员或破坏独立性')

    def _continuation_successor(self, db, source_task):
        row = db.execute('SELECT successor_task FROM task_continuations WHERE source_task=?', (source_task,)).fetchone()
        return row[0] if row else None

    def _continuation_root(self, db, task_id):
        row = db.execute('SELECT root_task FROM task_continuations WHERE successor_task=?', (task_id,)).fetchone()
        return row[0] if row else task_id

    def _continuation_required_deliveries(self, db, task_id):
        row = db.execute('SELECT required_deliveries FROM task_continuations WHERE successor_task=?', (task_id,)).fetchone()
        return json.loads(row[0]) if row else []

    def _require_continuation_claim(self, db, task_id, member):
        link = db.execute('SELECT * FROM task_continuations WHERE successor_task=?', (task_id,)).fetchone()
        if not link:
            return
        self._require_recovery_policy(db, 'continuation')
        plan = json.loads(link['plan']); source, fingerprint = self._remaining_source(db, link['source_task'])
        task = db.execute('SELECT assignee FROM tasks WHERE id=?', (task_id,)).fetchone()
        intent = db.execute('SELECT payload FROM continuation_intents WHERE id=?', (link['intent_id'],)).fetchone()
        request = json.loads(intent['payload'])
        if (source['status'] != 'handed_back' or fingerprint != link['source_digest'] or not task or task['assignee'] != member
                or request['authorization_version'] != self._authorization_version(db)
                or self._task_has_effective_supersession(db, source['id'])):
            fail('Continuation authority changed / 剩余工作的来源、权限或义务已变化，拒绝新领取')
        if self._recovery_unresolved(db, source['id'], ignore_terminal_workspace_activity=True):
            fail('Continuation source unresolved / 原任务执行尚未明确，拒绝开始剩余工作')
        self._validate_remaining_candidate(db, source, plan['candidate'], member)

    def _continuation_contributors(self, db, task_id, *, include_self=False):
        inherited = db.execute('SELECT contributors FROM task_continuations WHERE successor_task=?', (task_id,)).fetchone()
        result = json.loads(inherited[0]) if inherited else []
        if include_self:
            task = db.execute('SELECT assignee FROM tasks WHERE id=?', (task_id,)).fetchone()
            member = self._member(db, task['assignee'])
            original = db.execute("SELECT recipient_version FROM messages WHERE task_id=? AND kind='task' ORDER BY created DESC LIMIT 1", (task_id,)).fetchone()
            version = original['recipient_version'] if original else member['binding_version']
            if version != member['binding_version']:
                historical = db.execute('SELECT snapshot FROM member_history WHERE member=? AND version=?', (member['id'], version)).fetchone()
                if not historical:
                    fail('Original contributor binding unavailable / 原作者绑定历史缺失，不能用新账号覆盖')
                member = json.loads(historical[0])
            proof = json.loads(member['proof']) if isinstance(member['proof'], str) else member['proof']
            if proof.get('modelProvider') not in ('openai', 'deepseek', 'wokey', 'apinebula') or not proof.get('model'):
                fail('Original contributor model unavailable / 原作者实际模型及提供方尚未核验')
            contributor = {'member': task['assignee'], 'provider': proof['modelProvider'], 'model': proof['model'],
                           'session_uuid': member['session_uuid'], 'binding_version': version, 'source_task': task_id}
            if contributor not in result:
                result.append(contributor)
            for stage in db.execute('SELECT contributors FROM review_stages WHERE task_id=?', (task_id,)):
                for item in json.loads(stage[0]):
                    if item not in result:
                        result.append(item)
        return result

    def _continuation_repair_link(self, db, task_id):
        direct = db.execute('SELECT * FROM repair_links WHERE task_id=?', (task_id,)).fetchone()
        if direct:
            return direct
        root = self._continuation_root(db, task_id)
        return db.execute('SELECT * FROM repair_links WHERE task_id=?', (root,)).fetchone() if root != task_id else None

    def _continuation_delivery_allowed(self, db, member, delivery_id):
        actor = self._member(db, member)
        delivery = db.execute('SELECT task_id,digest FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
        grant = self._option(db, 'remaining_read:' + delivery['task_id'], {}) if delivery else {}
        if (grant.get('member') == member and grant.get('session') == actor['session_uuid']
                and grant.get('binding_version') == actor['binding_version'] and grant.get('delivery_digest') == delivery['digest']
                and self._remaining_source(db, delivery['task_id'])[1] == grant.get('source_digest')):
            return True
        return bool(db.execute('''SELECT 1 FROM task_continuations c JOIN tasks t ON t.id=c.successor_task
             JOIN json_each(c.required_deliveries) d WHERE d.value=? AND t.assignee=?
             AND t.status NOT IN ('cancelled','handed_back') AND EXISTS(
                SELECT 1 FROM messages m WHERE m.task_id=t.id AND m.kind='task' AND m.recipient=? AND m.recipient_version=?)''',
             (delivery_id, member, member, actor['binding_version'])).fetchone())

    def _settle_supersession(self, db, actor, source_task, replacement_stage, expected_source_digest,
                            coverage, idempotency_key, source_stage):
        if not isinstance(coverage, list) or not coverage:
            fail('Complete obligation coverage required / 替代关系须逐项覆盖原验收义务')
        seen = set()
        for item in coverage:
            if (not isinstance(item, dict) or set(item) != {'old', 'new'}
                    or any(not isinstance(value, str) or not value for value in item.values()) or item['old'] in seen):
                fail('Invalid obligation coverage / 覆盖映射须准确且原义务不能重复')
            seen.add(item['old'])
        task, task_digest = self._remaining_source(db, source_task)
        if source_stage:
            source = db.execute('SELECT * FROM review_stages WHERE id=? AND task_id=?', (source_stage, source_task)).fetchone()
            if not source or source['digest'] != expected_source_digest or source['status'] == 'running':
                fail('Superseded stage mismatch / 原阶段摘要或安全终态不符')
            original_material = json.loads(source['payload'])
            source_version = source['requirements_version']
        else:
            if task_digest != expected_source_digest or task['status'] in ('active', 'pending', 'cancelled'):
                fail('Superseded task mismatch / 原任务版本或状态不允许结算替代')
            original_material = {'coordinator_acceptance': json.loads(task['acceptance']),
                                 'coordinator_required_checks': json.loads(task['required_checks'])}
            source_version = task['requirements_version']
        replacement, proof_refs = self._formal_passed_stage(db, replacement_stage)
        if replacement['id'] == source_stage:
            fail('Distinct replacement required / 阶段不能替代自己')
        replacement_task = db.execute('SELECT * FROM tasks WHERE id=?', (replacement['task_id'],)).fetchone()
        if not source_stage:
            current = replacement['task_id']; seen_tasks = set()
            while True:
                if current == source_task or current in seen_tasks:
                    fail('Supersession cycle / 验收替代关系不能形成循环或替代自身')
                seen_tasks.add(current)
                following = self._effective_obligation_task(db, current)
                if following == current:
                    break
                current = following
        if source_task == replacement['task_id'] and source_stage and replacement['version'] <= source['version']:
            fail('Newer replacement required / 同任务替代须使用较新的正式阶段')
        if (self._recovery_unresolved(db, source_task, ignore_terminal_workspace_activity=True)
                or self._recovery_unresolved(db, replacement['task_id'], ignore_terminal_workspace_activity=True)):
            fail('Supersession execution unresolved / 原事项或替代事项有未决执行、审批或审核')
        if db.execute('SELECT 1 FROM resource_locks WHERE task_id IN (?,?)', (source_task, replacement['task_id'])).fetchone():
            fail('Supersession resource occupied / 原事项或替代事项仍占有资源')
        old_obligations = self._obligations(original_material)
        if not source_stage:
            for historical in db.execute("SELECT id,payload FROM review_stages WHERE task_id=? AND status IN ('pending','running','needs_validation','needs_fix')", (source_task,)):
                obligations = self._obligations(json.loads(historical['payload']))
                if not obligations:
                    fail('Historical obligations unavailable / 旧待审阶段验收义务不完整，不能释放整个任务名额')
                for key, obligation in obligations.items():
                    old_obligations['stage:' + historical['id'] + ':' + key] = obligation
        new_material = json.loads(replacement['payload'])
        new_obligations = self._obligations(new_material)
        if not old_obligations or seen != set(old_obligations) or any(item['new'] not in new_obligations for item in coverage):
            fail('Incomplete obligation coverage / 旧验收义务未被完整覆盖或新义务不存在')
        declared = {'source_task': source_task, 'source_stage': source_stage or None,
                    'source_digest': expected_source_digest, 'requirements_version': source_version,
                    'source_obligations': old_obligations, 'coverage': coverage}
        if declared not in new_material.get('supersedes', []):
            fail('Reviewed coverage missing / 准确覆盖声明必须已包含在新阶段独立审核材料中')
        replacement_proof = {'epoch': replacement['reviewer_epoch'], 'session': replacement['reviewer_session'],
                             'execution': replacement['execution_id'], 'turn': replacement['turn_id'],
                             'report_digest': digest(json.loads(replacement['evidence']))}
        request = dict(declared, replacement_stage=replacement_stage, replacement_digest=replacement['digest'], replacement_proof=replacement_proof)
        safe_evidence(encoded(request))
        previous = db.execute('SELECT * FROM obligation_supersessions WHERE idempotency_key=?', (idempotency_key,)).fetchone()
        if previous:
            if previous['request_digest'] != digest(request):
                fail('Supersession request conflict / 同一结算请求不能更换义务或候选')
            return {'id': previous['id'], 'duplicate': True, 'effective': self._supersession_current(db, previous),
                    'old_verdict_unchanged': True}
        active = db.execute("SELECT * FROM obligation_supersessions WHERE source_task=? AND source_stage=? AND state='active'",
                            (source_task, source_stage)).fetchone()
        if active:
            if self._supersession_current(db, active):
                fail('Obligations already superseded / 原义务已有有效替代关系')
            db.execute("UPDATE obligation_supersessions SET state='invalidated',invalidated_reason=? WHERE id=?",
                       ('replacement_no_longer_current', active['id']))
        key = uuid.uuid4().hex
        db.execute('INSERT INTO obligation_supersessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                   (key, source_task, source_stage, expected_source_digest, replacement_stage, replacement['digest'],
                    encoded(replacement_proof), encoded(coverage), digest(request), idempotency_key, 'active', actor['session_uuid'],
                    actor['binding_version'], time.time(), None))
        self.event(db, 'obligations_superseded', supersession=key, source_task=source_task, source_stage=source_stage,
                   replacement_stage=replacement_stage, proof_refs=proof_refs, old_verdict_unchanged=True)
        return {'id': key, 'duplicate': False, 'effective': True, 'old_verdict_unchanged': True,
                'task_obligations_satisfied': self._task_obligation_satisfied(db, source_task),
                'automatic_dispatch': False}

    @staticmethod
    def _obligations(material):
        result = {}
        for criterion in material.get('coordinator_acceptance', []):
            if not isinstance(criterion, str) or not criterion.strip():
                fail('Original acceptance unavailable / 旧验收条件不完整')
            key = 'acceptance:' + digest(criterion)
            if key in result:
                fail('Ambiguous acceptance obligation / 原验收条件存在重复项，先版本化整理')
            result[key] = criterion
        for check in material.get('coordinator_required_checks', []):
            if not isinstance(check, dict) or not check.get('name'):
                fail('Original check contract unavailable / 原检查契约不完整')
            if check.get('required', True):
                key = 'check:' + check['name']
                if key in result:
                    fail('Ambiguous check obligation / 原检查名称不唯一')
                result[key] = check
        return result

    def _formal_passed_stage(self, db, stage_id):
        try:
            from .pair_producer import KEY, producer_context
        except ImportError:
            from pair_producer import KEY, producer_context
        stage = db.execute('SELECT * FROM review_stages WHERE id=?', (stage_id,)).fetchone()
        task = db.execute('SELECT * FROM tasks WHERE id=?', (stage['task_id'],)).fetchone() if stage else None
        latest = db.execute('SELECT id FROM review_stages WHERE task_id=? ORDER BY version DESC LIMIT 1', (stage['task_id'],)).fetchone() if stage else None
        if (not stage or not task or stage['status'] != 'passed' or stage['verdict'] != 'passed'
                or not latest or latest[0] != stage_id or task['requirements_version'] != stage['requirements_version']
                or not stage['reviewer_session'] or not stage['execution_id'] or not stage['turn_id'] or not stage['evidence']):
            fail('Current formal passed stage required / 必须使用当前需求下最新准确正式通过阶段')
        if not self._stage_passed(db, stage['task_id'], allow_supersession=False):
            fail('Formal auxiliary review incomplete / 替代阶段仍有未通过的必要辅助审核')
        events = []
        for row in db.execute("SELECT seq,data FROM events WHERE kind='review_effective_verdict'"):
            value = json.loads(row['data'])
            if value.get('stage') == stage_id and value.get('execution') == stage['execution_id']:
                events.append((row['seq'], value))
        if len(events) != 1:
            fail('Formal supersession receipt missing / 替代阶段缺少唯一准确正式结算')
        seq, record = events[0]
        if any(record.get(key) != expected for key, expected in (('digest', stage['digest']), ('session', stage['reviewer_session']),
                ('epoch', stage['reviewer_epoch']), ('turn', stage['turn_id']), ('effective_verdict', 'passed'))):
            fail('Formal supersession receipt differs / 正式结算与替代阶段不一致')
        producer = self._option(db, KEY + stage['execution_id'])
        binding = dict(producer.get('binding', {})) if isinstance(producer, dict) else {}
        for key, value in {'pair': self.pair_id, 'stage': stage_id, 'digest': stage['digest'],
                           'epoch': stage['reviewer_epoch'], 'session': stage['reviewer_session'],
                           'execution': stage['execution_id'], 'turn': stage['turn_id']}.items():
            if binding.get(key) != value:
                fail('Formal supersession producer missing / 替代阶段缺少准确生产者证据')
        try:
            producer_context(producer, binding, live=False)
        except (ValueError, KeyError, TypeError) as exc:
            fail('Formal supersession producer unverified / 替代阶段生产者未核验: ' + str(exc))
        report = json.loads(stage['evidence']); required = json.loads(stage['payload']).get('coordinator_required_checks', [])
        supplied = {check.get('name'): check for check in report.get('checks', []) if isinstance(check, dict)}
        if not required or any(check['name'] not in supplied or supplied[check['name']].get('status') != 'passed'
                               or not supplied[check['name']].get('evidence') for check in required if check.get('required', True)):
            fail('Formal supersession checks missing / 替代阶段必要检查证据不完整')
        if any(item.get('blocking') and item.get('status') not in ('resolved', 'closed') for item in report.get('findings', [])):
            fail('Formal supersession blocker remains / 替代阶段仍有阻塞问题')
        return stage, ['event:' + str(seq), 'producer:' + digest(producer)]

    def _supersession_current(self, db, relation):
        if relation['state'] != 'active':
            return False
        stage = db.execute('SELECT * FROM review_stages WHERE id=?', (relation['replacement_stage'],)).fetchone()
        task = db.execute('SELECT * FROM tasks WHERE id=?', (stage['task_id'],)).fetchone() if stage else None
        latest = db.execute('SELECT id FROM review_stages WHERE task_id=? ORDER BY version DESC LIMIT 1', (stage['task_id'],)).fetchone() if stage else None
        if (not stage or not task or stage['status'] != 'passed' or stage['digest'] != relation['replacement_digest']
                or task['requirements_version'] != stage['requirements_version'] or not latest or latest[0] != stage['id']):
            return False
        if not self._stage_passed(db, stage['task_id'], allow_supersession=False):
            return False
        proof = {'epoch': stage['reviewer_epoch'], 'session': stage['reviewer_session'], 'execution': stage['execution_id'],
                 'turn': stage['turn_id'], 'report_digest': digest(json.loads(stage['evidence'])) if stage['evidence'] else None}
        if proof != json.loads(relation['replacement_proof']):
            return False
        if relation['source_stage']:
            source = db.execute('SELECT digest FROM review_stages WHERE id=? AND task_id=?', (relation['source_stage'], relation['source_task'])).fetchone()
            return bool(source and source[0] == relation['source_digest'])
        source, fingerprint = self._remaining_source(db, relation['source_task'])
        return source['status'] not in ('cancelled', 'active', 'pending') and fingerprint == relation['source_digest']

    def _stage_obligations_superseded(self, db, stage_id):
        stage = db.execute('SELECT task_id FROM review_stages WHERE id=?', (stage_id,)).fetchone()
        if not stage:
            return False
        for row in db.execute("SELECT * FROM obligation_supersessions WHERE (source_stage=? OR (source_task=? AND source_stage='')) AND state='active'", (stage_id, stage['task_id'])):
            if self._supersession_current(db, row):
                if row['source_stage'] or any(item['old'].startswith('stage:' + stage_id + ':') for item in json.loads(row['coverage'])):
                    return True
        return False

    def _task_obligation_satisfied(self, db, task_id):
        task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not task or task['status'] == 'cancelled':
            return False
        if task['status'] in ('completed', 'integrated'):
            return self._stage_passed(db, task_id)
        return self._task_has_effective_supersession(db, task_id)

    def _task_has_effective_supersession(self, db, task_id):
        direct = db.execute("SELECT * FROM obligation_supersessions WHERE source_task=? AND source_stage='' AND state='active'", (task_id,)).fetchone()
        return bool(direct and self._supersession_current(db, direct))

    def _effective_obligation_task(self, db, task_id):
        direct = db.execute("SELECT * FROM obligation_supersessions WHERE source_task=? AND source_stage='' AND state='active'", (task_id,)).fetchone()
        if direct and self._supersession_current(db, direct):
            return db.execute('SELECT task_id FROM review_stages WHERE id=?', (direct['replacement_stage'],)).fetchone()[0]
        return task_id

    def _stage_backlog_ids(self, db, member):
        return [row['id'] for row in db.execute("SELECT s.id FROM review_stages s JOIN tasks t ON t.id=s.task_id WHERE t.assignee=? AND s.status IN ('pending','running','needs_validation') ORDER BY s.id", (member,))
                if not self._stage_obligations_superseded(db, row['id'])]

    def _task_backlog_ids(self, db, member):
        return [row['id'] for row in db.execute("SELECT id FROM tasks WHERE assignee=? AND status IN ('review_wait','needs_validation') ORDER BY id", (member,))
                if not self._task_obligation_satisfied(db, row['id'])]

    def propose_review_recovery(self, member, session, cwd, proposal, *, idempotency_key):
        required = {'source_task', 'stable_blocker', 'blocker_snapshot_digest', 'candidate_root', 'manifest',
                    'allowed_paths', 'source_manifest_digest', 'purpose', 'check_contract_digest', 'reviewer',
                    'required_deliveries', 'remediates'}
        if not isinstance(proposal, dict) or set(proposal) != required:
            fail('Exact recovery proposal required / 恢复提案字段不完整或存在未知字段')
        safe_evidence(encoded(proposal))
        for field in ('blocker_snapshot_digest', 'source_manifest_digest', 'check_contract_digest'):
            if not isinstance(proposal[field], str) or not re.fullmatch('[0-9a-f]{64}', proposal[field]):
                fail('Exact digest required / 提案须绑定准确摘要')
        if not proposal['stable_blocker'] or not proposal['purpose'] or not isinstance(proposal['required_deliveries'], list):
            fail('Recovery purpose required / 需稳定阻塞、复核目的与交付清单')
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd)
            project = Path(db.execute('SELECT project FROM collaborations').fetchone()[0]).resolve()
            if (not isinstance(proposal['candidate_root'], str) or not Path(proposal['candidate_root']).is_absolute()
                    or not Path(proposal['candidate_root']).resolve().is_relative_to(project)):
                fail('Candidate outside project / 恢复候选必须保存在当前项目目录内')
            fixed = dict(proposal, candidate_manifest_digest=candidate_manifest(proposal['candidate_root'], proposal['manifest'], proposal['allowed_paths']),
                         allowed_effects=['isolated_review_evidence'],
                         forbidden_effects=['product_edit', 'active_runtime_write', 'real_state_write', 'hook_change', 'account_change', 'install', 'publish'])
            old = db.execute('SELECT * FROM recovery_requests WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if old:
                if old['proposal_digest'] != digest(fixed):
                    fail('Recovery request conflict / 同一请求不能绑定不同候选')
                return {'id': old['id'], 'digest': old['proposal_digest'], 'status': old['status'], 'duplicate': True}
            task = db.execute('SELECT * FROM tasks WHERE id=?', (proposal['source_task'],)).fetchone()
            if not task or task['status'] == 'cancelled':
                fail('Recovery source missing / 原任务不存在或已取消')
            try:
                from .pair_diagnostics import snapshot_from_db
            except ImportError:
                from pair_diagnostics import snapshot_from_db
            snapshot = snapshot_from_db(db, self.pair_id, task_id=task['id'])
            if snapshot['state_digest'] != proposal['blocker_snapshot_digest']:
                fail('Blocker snapshot changed / 恢复提案的阻塞快照已变化，重新只读核对')
            cycle = self._recovery_admission_cycle(db, task, fixed, snapshot)
            reviewer = self._member(db, proposal['reviewer'])
            self._independent_recovery_actor(db, reviewer, task)
            for delivery in proposal['required_deliveries']:
                if not isinstance(delivery, str) or not db.execute('SELECT 1 FROM deliveries WHERE id=?', (delivery,)).fetchone():
                    fail('Recovery delivery missing / 指定交付不存在')
            duplicate = db.execute("SELECT id,status FROM recovery_requests WHERE stable_blocker=? AND json_extract(proposal,'$.candidate_manifest_digest')=? AND status NOT IN ('closed','revoked','expired','review_failed')",
                                   (proposal['stable_blocker'], fixed['candidate_manifest_digest'])).fetchone()
            if duplicate:
                fail('Recovery already registered / 同一稳定问题与候选已有有效恢复记录')
            now = time.time(); key = uuid.uuid4().hex
            db.execute('''INSERT INTO recovery_requests(id,source_task,stable_blocker,proposal,proposal_digest,idempotency_key,
                       status,reviewer,reviewer_session,binding_version,created,updated) VALUES(?,?,?,?,?,?,'proposed',?,?,?,?,?)''',
                       (key, task['id'], proposal['stable_blocker'], encoded(fixed), digest(fixed), idempotency_key,
                        reviewer['id'], reviewer['session_uuid'], reviewer['binding_version'], now, now))
            self.event(db, 'recovery_proposed', recovery=key, candidate=fixed['candidate_manifest_digest'])
            return {'id': key, 'digest': digest(fixed), 'status': 'proposed', 'duplicate': False,
                    'admission_cycle_verified': True, 'cycle': cycle}

    def read_review_recovery(self, member, session, cwd, recovery_id, *, offset=0, limit=2000):
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 2000:
            fail('Invalid recovery page / 恢复材料分页参数无效')
        with self.transaction() as db:
            actor = self._binding(db, member, session, cwd)
            row = self._recovery_request(db, recovery_id)
            if member not in ('A', row['reviewer']):
                fail('Recovery reader denied / 当前成员不在本次恢复阅读范围')
            if member == row['reviewer']:
                self._recovery_binding(row, actor)
            content = row['proposal']; fingerprint = row['proposal_digest']
            prior = db.execute('SELECT * FROM recovery_reads WHERE recovery_id=? AND member=? AND session=? AND binding_version=?',
                               (recovery_id, member, session, actor['binding_version'])).fetchone()
            end_read = prior['read_end'] if prior and prior['digest'] == fingerprint else 0
            if offset > end_read or offset > len(content):
                fail('Recovery read gap / 须顺序完整读取恢复材料')
            end = min(len(content), offset + limit)
            db.execute('INSERT OR REPLACE INTO recovery_reads VALUES(?,?,?,?,?,?)',
                       (recovery_id, member, session, actor['binding_version'], fingerprint, max(end_read, end)))
            return {'id': recovery_id, 'digest': fingerprint, 'text': content[offset:end], 'next_offset': end if end < len(content) else None}

    def authorize_review_recovery(self, member, session, cwd, recovery_id, expected_digest, *, basis, ttl=600):
        safe_evidence(basis)
        if type(ttl) is not int or not 1 <= ttl <= 86400:
            fail('Invalid permit lifetime / 开始前许可有效期须为1至86400秒')
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd)
            row = self._recovery_request(db, recovery_id)
            self._require_recovery_open(db, task_id=row['source_task'], action='review_recovery')
            policy = self._require_recovery_policy(db, 'review_recovery')
            if row['proposal_digest'] != expected_digest or row['status'] != 'proposed':
                fail('Recovery proposal changed / 提案版本或状态不符')
            context = self._recovery_context(db, row)
            task = db.execute('SELECT * FROM tasks WHERE id=?', (row['source_task'],)).fetchone()
            cycle = self._recovery_admission_cycle(db, task, json.loads(row['proposal']))
            if self._recovery_unresolved(db, task['id']):
                fail('Execution unresolved / 原业务或审批未结算，不能签发恢复许可')
            now = time.time()
            db.execute("UPDATE recovery_requests SET status='queued',authorization_version=?,policy_version=?,expires_at=?,updated=? WHERE id=?",
                       (self._authorization_version(db), policy['version'], now + ttl, now, recovery_id))
            self.event(db, 'recovery_authorized', recovery=recovery_id, basis=basis, cycle=cycle, context_digest=digest(context))
            return {'id': recovery_id, 'status': 'queued', 'expires_at': now + ttl}

    def claim_review_recovery(self, member, session, cwd, recovery_id):
        with self.transaction() as db:
            actor = self._binding(db, member, session, cwd)
            row = self._recovery_request(db, recovery_id)
            self._recovery_binding(row, actor)
            self._require_recovery_open(db, member=member, task_id=row['source_task'], action='review_recovery')
            if row['status'] != 'queued':
                return {'claimed': False, 'reason': 'already_' + row['status']}
            self._validate_recovery_permit(db, row, actor)
            self._require_recovery_read(db, row, actor)
            if db.execute("SELECT 1 FROM recovery_requests WHERE status IN ('claimed','running','report_submitted','unknown')").fetchone():
                return {'claimed': False, 'reason': 'recovery_channel_occupied'}
            token = secrets.token_hex(24)
            db.execute("UPDATE recovery_requests SET status='claimed',claim_token=?,updated=? WHERE id=?", (token, time.time(), recovery_id))
            self.event(db, 'recovery_claimed', recovery=recovery_id, member=member)
            return {'claimed': True, 'id': recovery_id, 'token': token, 'execution_not_started': True}

    def recover_review_claim(self, member, session, cwd, recovery_id):
        with self.transaction() as db:
            actor = self._binding(db, member, session, cwd); row = self._recovery_request(db, recovery_id)
            self._recovery_binding(row, actor)
            if row['status'] != 'claimed' or row['execution_id']:
                fail('Claim recovery unavailable / 只恢复原会话尚未开始的领取回执')
            self._validate_recovery_permit(db, row, actor)
            return {'id': recovery_id, 'token': row['claim_token'], 'resume_only': True, 'execution_not_started': True}

    def begin_review_recovery(self, member, session, cwd, recovery_id, token):
        with self.transaction() as db:
            actor = self._binding(db, member, session, cwd); row = self._recovery_request(db, recovery_id)
            self._recovery_binding(row, actor)
            if row['status'] != 'claimed' or not secrets.compare_digest(row['claim_token'] or '', token):
                fail('Recovery claim required / 必须持有当前恢复领取')
            self._require_recovery_open(db, member=member, task_id=row['source_task'], action='review_recovery')
            self._validate_recovery_permit(db, row, actor)
            self._require_recovery_read(db, row, actor)
            if self._recovery_unresolved(db, row['source_task']):
                fail('Execution unresolved / 原业务或审批存在未决执行')
            proposal = json.loads(row['proposal'])
            for delivery in proposal['required_deliveries']:
                self._require_ack(db, member, delivery)
            context = self._recovery_context(db, row)
            self._recovery_admission_cycle(db, db.execute('SELECT * FROM tasks WHERE id=?', (row['source_task'],)).fetchone(), proposal)
            proof = self._trusted_recovery_evidence('begin', context)
            for fact in ('explicit_admission_cycle', 'isolation_verified', 'executor_gates_verified', 'resources_available', 'identity_current'):
                if proof.facts.get(fact) is not True:
                    fail('Recovery execution unverified / 恢复执行前置尚未核验: ' + fact)
            self._start_recovery_attempt(db, 'coordination', row['stable_blocker'], recovery_id,
                                         {'purpose': proposal['purpose'], 'receipts': proof.receipts})
            epoch = row['execution_epoch'] + 1; execution = uuid.uuid4().hex; now = time.time()
            db.execute("UPDATE recovery_requests SET status='running',execution_epoch=?,execution_id=?,control_generation=?,updated=? WHERE id=?",
                       (epoch, execution, self._control_generation(db), now, recovery_id))
            db.execute('INSERT INTO recovery_execution_attempts VALUES(?,?,?,?,?,?,?)',
                       (recovery_id, epoch, execution, 'running', encoded({'receipts': proof.receipts}), now, now))
            self.event(db, 'recovery_started', recovery=recovery_id, epoch=epoch, execution=execution)
            return {'id': recovery_id, 'status': 'running', 'epoch': epoch, 'execution_id': execution,
                    'control_generation': self._control_generation(db)}

    def finish_review_recovery(self, member, session, cwd, recovery_id, token, *, epoch, report):
        if not isinstance(report, dict) or report.get('verdict') not in ('passed', 'needs_fix', 'needs_validation'):
            fail('Recovery report required / 需要结构化恢复报告')
        safe_evidence(encoded(report))
        with self.transaction() as db:
            actor = self._binding(db, member, session, cwd); row = self._recovery_request(db, recovery_id)
            self._recovery_binding(row, actor)
            if (row['status'] not in ('running', 'unknown') or row['execution_epoch'] != epoch
                    or not secrets.compare_digest(row['claim_token'] or '', token)):
                fail('Stale recovery execution / 旧领取或代次不能结算当前恢复')
            context = dict(self._recovery_context(db, row, verify_candidate=False), report_digest=digest(report))
            proof = self._formal_recovery_evidence(db, row, report, context) if report.get('formal_stage') else self._trusted_recovery_evidence('finish', context)
            if proof.facts.get('execution_terminal') is not True:
                fail('Recovery execution unresolved / 缺准确工具终态，不释放恢复名额')
            verdict = report['verdict']
            if verdict == 'passed' and not all(proof.facts.get(key) is True for key in
                    ('official_dual_receipts', 'contract_satisfied', 'files_unchanged', 'independent_review')):
                verdict = 'needs_validation'
            state = {'passed': 'review_passed', 'needs_fix': 'review_failed', 'needs_validation': 'needs_validation'}[verdict]
            saved = dict(report, effective_verdict=verdict, trusted_receipts=list(proof.receipts), context_digest=proof.context_digest)
            now = time.time()
            db.execute('UPDATE recovery_requests SET status=?,report=?,updated=? WHERE id=?', (state, encoded(saved), now, recovery_id))
            db.execute('UPDATE recovery_execution_attempts SET state=?,evidence=?,updated=? WHERE recovery_id=? AND epoch=?',
                       (state, encoded(saved), now, recovery_id, epoch))
            self._finish_recovery_attempt(db, 'coordination', row['stable_blocker'], recovery_id, state, {'receipts': proof.receipts})
            self.event(db, 'recovery_review_recorded', recovery=recovery_id, epoch=epoch, verdict=verdict)
            return {'id': recovery_id, 'status': state, 'verdict': verdict, 'original_business_unchanged': True}

    def reconcile_review_recovery(self, member, session, cwd, recovery_id):
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd); row = self._recovery_request(db, recovery_id)
            if row['status'] not in ('running', 'unknown'):
                return {'id': recovery_id, 'status': row['status'], 'changed': False}
            proof = self._trusted_recovery_evidence('reconcile', self._recovery_context(db, row, verify_candidate=False))
            if proof.facts.get('execution_terminal') is not True:
                db.execute("UPDATE recovery_requests SET status='unknown',updated=? WHERE id=?", (time.time(), recovery_id))
                db.execute("UPDATE recovery_execution_attempts SET state='unknown',updated=? WHERE recovery_id=? AND epoch=?", (time.time(), recovery_id, row['execution_epoch']))
                return {'id': recovery_id, 'status': 'unknown', 'slot_retained': True}
            # 对账只结算执行事实；没有独立报告时不能借终态制造通过。
            now = time.time(); saved = {'receipts': proof.receipts, 'needs_report': True}
            db.execute("UPDATE recovery_requests SET status='needs_validation',report=?,updated=? WHERE id=?", (encoded(saved), now, recovery_id))
            db.execute("UPDATE recovery_execution_attempts SET state='needs_validation',evidence=?,updated=? WHERE recovery_id=? AND epoch=?", (encoded(saved), now, recovery_id, row['execution_epoch']))
            self._finish_recovery_attempt(db, 'coordination', row['stable_blocker'], recovery_id, 'needs_validation', saved)
            self.event(db, 'recovery_reconciled', recovery=recovery_id, epoch=row['execution_epoch'], evidence=proof.receipts)
            return {'id': recovery_id, 'status': 'needs_validation', 'original_business_unchanged': True}

    def revoke_review_recovery(self, member, session, cwd, recovery_id, *, reason):
        safe_evidence(reason)
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd); row = self._recovery_request(db, recovery_id)
            if row['status'] in ('running', 'report_submitted', 'unknown'):
                fail('Recovery execution unresolved / 已开始或未知的执行须先对账，不能用撤销释放名额')
            if row['status'] not in ('proposed', 'queued', 'claimed'):
                fail('Recovery no longer revocable / 恢复不在开始前可撤销状态')
            db.execute("UPDATE recovery_requests SET status='revoked',updated=? WHERE id=?", (time.time(), recovery_id))
            db.execute('UPDATE recovery_delivery_grants SET revoked=1 WHERE recovery_id=?', (recovery_id,))
            self.event(db, 'recovery_revoked', recovery=recovery_id, reason=reason)
            return {'id': recovery_id, 'status': 'revoked', 'past_effects_unchanged': True}

    def grant_recovery_delivery_access(self, member, session, cwd, recovery_id, delivery_id, expected_digest, *, ttl=600):
        if type(ttl) is not int or not 1 <= ttl <= 86400:
            fail('Invalid grant lifetime / 读取授权有效期须为1至86400秒')
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd); row = self._recovery_request(db, recovery_id)
            self._require_recovery_policy(db, 'review_recovery')
            proposal = json.loads(row['proposal'])
            if row['status'] not in ('queued', 'claimed', 'running') or delivery_id not in proposal['required_deliveries']:
                fail('Recovery delivery outside scope / 交付不在当前恢复许可范围')
            delivery = db.execute('SELECT * FROM deliveries WHERE id=?', (delivery_id,)).fetchone()
            if not delivery or delivery['digest'] != expected_digest:
                fail('Delivery changed / 交付不存在或摘要已变化')
            expires = min(time.time() + ttl, row['expires_at'])
            if expires <= time.time():
                fail('Recovery permit expired / 恢复许可已过期')
            db.execute('INSERT OR REPLACE INTO recovery_delivery_grants VALUES(?,?,?,?,?,?,0)',
                       (recovery_id, delivery_id, row['reviewer'], expected_digest, row['proposal_digest'], expires))
            self.event(db, 'recovery_delivery_granted', recovery=recovery_id, delivery=delivery_id, recipient=row['reviewer'], digest=expected_digest)
            return {'delivery': delivery_id, 'recipient': row['reviewer'], 'expires_at': expires, 'pagination_required': True}

    def prepare_recovery_application(self, member, session, cwd, recovery_id, stage_id, plan, *, idempotency_key):
        if not isinstance(plan, dict) or set(plan) != {'old_runtime_digest', 'new_runtime_digest', 'state_schema', 'backup_ref', 'verification_contract', 'allowed_paths'}:
            fail('Exact application plan required / 应用计划须包含准确旧新版、schema、备份、核验契约与范围')
        safe_evidence(encoded(plan))
        for key in ('old_runtime_digest', 'new_runtime_digest'):
            if not isinstance(plan[key], str) or not re.fullmatch('[0-9a-f]{64}', plan[key]):
                fail('Runtime digest required / 应用须固定运行副本摘要')
        if (type(plan['state_schema']) is not int or plan['state_schema'] != 14
                or not isinstance(plan['backup_ref'], str) or not plan['backup_ref'].strip()
                or not isinstance(plan['verification_contract'], str) or not re.fullmatch('[0-9a-f]{64}', plan['verification_contract'])
                or not isinstance(plan['allowed_paths'], list) or not plan['allowed_paths']
                or any(not isinstance(path, str) or not path.strip() for path in plan['allowed_paths'])):
            fail('Incomplete application boundary / 应用计划缺少准确schema、备份、检查契约或范围')
        with self._recovery_control_transaction() as db:
            self._a_actor(db, member, session, cwd)
            policy = self._require_recovery_policy(db, 'patch_application')
            row = self._recovery_request(db, recovery_id)
            stage = db.execute('SELECT * FROM review_stages WHERE id=?', (stage_id,)).fetchone()
            if row['status'] != 'review_passed' or not stage or stage['status'] != 'passed' or not stage['evidence']:
                fail('Formal independent review required / 恢复复核和准确正式独立审核均需通过')
            self._formal_passed_stage(db,stage_id)
            proposal=json.loads(row['proposal']);report=json.loads(row['report']);payload=json.loads(stage['payload'])
            if (report.get('formal_stage')!=stage_id or payload.get('candidate_manifest_digest')!=proposal['candidate_manifest_digest']
                    or payload.get('runtime_verification_contract')!=plan['verification_contract']
                    or plan['allowed_paths']!=proposal['allowed_paths']):fail('Application review binding mismatch / 应用须绑定本次完整候选、检查契约和正式审核')
            request = {'recovery': recovery_id, 'stage': stage_id, 'stage_digest': stage['digest'], 'plan': plan}
            previous = db.execute('SELECT * FROM recovery_applications WHERE idempotency_key=?', (idempotency_key,)).fetchone()
            if previous:
                if previous['request_digest'] != digest(request):
                    fail('Application request conflict / 同一应用请求不能更换计划')
                return {'id': previous['id'], 'state': previous['state'], 'duplicate': True}
            self._require_recovery_open(db, task_id=row['source_task'], action='patch_application')
            generation = self._new_control_generation(db); now = time.time(); key = uuid.uuid4().hex
            db.execute('INSERT INTO recovery_applications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (key, recovery_id, stage_id, stage['digest'], encoded(plan), digest(request), idempotency_key,
                        policy['version'], self._authorization_version(db), 'waiting_terminal', generation, '{}', now, now))
            self.event(db, 'recovery_application_queued', application=key, generation=generation)
            return {'id': key, 'state': 'waiting_terminal', 'dispatch_blocked': True, 'files_copied': False,
                    'runtime_switch_implemented': True}

    def begin_recovery_application(self, member, session, cwd, application_id):
        with self._recovery_control_transaction() as db:
            self._a_actor(db, member, session, cwd)
            if self._cli_compatibility_gate(db,member,'runtime_switch'):
                fail('CLI compatibility unverified / 运行切换前须核验准确CLI及适配')
            row = db.execute('SELECT * FROM recovery_applications WHERE id=?', (application_id,)).fetchone()
            if not row or row['state'] != 'waiting_terminal':
                fail('Application preparation unavailable / 应用不在等待安全终态状态')
            policy = self._require_recovery_policy(db, 'patch_application')
            if row['policy_version'] != policy['version'] or row['authorization_version'] != self._authorization_version(db):
                fail('Application authorization changed / 应用授权已变化')
            recovery = self._recovery_request(db, row['recovery_id'])
            reasons = [reason for reason in self._recovery_gate(db, task_id=recovery['source_task'], action='patch_application') if reason.get('application') != application_id]
            if reasons:
                fail('Application control gate / 应用期间出现其他控制门禁: ' + encoded(reasons))
            if self._recovery_unresolved(db):
                return {'id': application_id, 'state': 'waiting_terminal', 'dispatch_blocked': True, 'execution_started': False}
            stage = db.execute('SELECT * FROM review_stages WHERE id=?', (row['stage_id'],)).fetchone()
            if not stage or stage['status'] != 'passed' or stage['digest'] != row['candidate_digest']:
                fail('Reviewed application changed / 应用所需准确正式审核已失效')
            self._formal_passed_stage(db,row['stage_id'])
            proof = self._trusted_recovery_evidence('application_prepare', {'application': dict(row), 'state_digest': self._recovery_state_digest(db)})
            if not all(proof.facts.get(key) is True for key in ('exact_reviewed_candidate', 'compatible_migration', 'backup_integrity', 'exclusive_writer_available')):
                fail('Application not verified / 应用版本、备份、兼容性或独占写条件未核验')
            db.execute("UPDATE recovery_applications SET state='prepared',evidence=?,updated=? WHERE id=?",
                       (encoded({'receipts': proof.receipts}), time.time(), application_id))
            self.event(db, 'recovery_application_prepared', application=application_id, evidence=proof.receipts)
            return {'id': application_id, 'state': 'prepared', 'files_copied': bool(self._option(db,'runtime_copy:'+application_id)), 'runtime_switch_implemented': True}

    def reconcile_recovery_application(self, member, session, cwd, application_id):
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd)
            row = db.execute('SELECT * FROM recovery_applications WHERE id=?', (application_id,)).fetchone()
            if not row or row['state'] not in ('prepared', 'unknown'):
                fail('Application unavailable / 没有待核验应用记录')
            policy = self._require_recovery_policy(db, 'patch_application')
            if row['policy_version'] != policy['version'] or row['authorization_version'] != self._authorization_version(db):
                fail('Application authorization changed / 应用授权已变化，保留切换门禁')
            stage = db.execute('SELECT * FROM review_stages WHERE id=?', (row['stage_id'],)).fetchone()
            if not stage or stage['status'] != 'passed' or stage['digest'] != row['candidate_digest']:
                fail('Reviewed application changed / 正式审核已失效，保留切换门禁')
            self._formal_passed_stage(db,row['stage_id'])
            proof = self._trusted_recovery_evidence('application_reconcile', {'application': dict(row), 'state_digest': self._recovery_state_digest(db)})
            if not all(proof.facts.get(key) is True for key in ('actual_loaded_digest_matches', 'schema_matches', 'verification_checks_passed', 'original_execution_terminal')):
                fail('Application still unverified / 实际加载、schema、必要检查或原执行尚未确认')
            now = time.time()
            db.execute("UPDATE recovery_applications SET state='applied_and_verified',evidence=?,updated=? WHERE id=?",
                       (encoded({'receipts': proof.receipts, 'facts': proof.facts}), now, application_id))
            db.execute("UPDATE recovery_requests SET status='applied_and_verified',updated=? WHERE id=?", (now, row['recovery_id']))
            self.event(db, 'recovery_application_verified', application=application_id, evidence=proof.receipts)
            return {'id': application_id, 'state': 'applied_and_verified', 'other_gates_preserved': True,
                    'business_verdicts_unchanged': True}

    def _task_scope_closed(self, db, task_id=None):
        if self._option(db, 'goal_completed', False):
            return True
        scope = self._option(db, 'closed_scope')
        if not scope:
            return False
        task = db.execute('SELECT id,created FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not task or task_id.startswith('pair-plan-'):
            return True
        if 'task_ids' in scope:
            return task_id in scope['task_ids']
        # 旧记录若不能证明任务在收尾后登记，继续阻止执行。
        return scope.get('closed_at') is None or task['created'] <= scope['closed_at']

    def _recovery_gate(self, db, member=None, task_id=None, action='business'):
        if action in MANAGEMENT_ACTIONS:
            return []
        reasons = []
        state = db.execute('SELECT status FROM collaborations').fetchone()[0]
        if state != 'running':
            reasons.append({'code': 'USER_PAUSED_OR_STOPPED', 'state': state})
        if self._task_scope_closed(db, task_id):
            reasons.append({'code': 'SCOPE_CLOSED'})
        for row in self._matching_controls(db, member, task_id):
            reasons.append({'code': 'RESUME_RECONCILIATION_BLOCKED' if row['kind'] == 'resume' else 'PAUSE_NOT_CONFIRMED',
                            'control': row['id'], 'generation': row['generation'], 'state': row['state']})
        for row in db.execute("SELECT id,generation FROM recovery_applications WHERE state IN ('waiting_terminal','prepared','unknown')"):
            reasons.append({'code': 'RUNTIME_SWITCH_PENDING', 'application': row['id'], 'generation': row['generation']})
        for target in {'A', member} - {None}:
            observer = self._option(db, 'control_observer:' + target)
            if observer and observer.get('state') == 'gap':
                reasons.append({'code': 'CONTROL_EVENT_GAP', 'member': target, 'reason': observer['reason'],
                                'source_identity': observer.get('source_identity'), 'state': 'unverified'})
        return reasons

    def _require_recovery_open(self, db, member=None, task_id=None, action='business'):
        reasons = self._recovery_gate(db, member, task_id, action)
        if reasons:
            fail('Recovery control gate / 新动作受控制门禁限制: ' + encoded(reasons))

    def _record_continuation(self, db, *, logical_goal, action, payload, idempotency_key, task_id=None, recipient=None):
        request = {'goal': logical_goal, 'action': action, 'payload': payload, 'task': task_id,
                   'requirements_version': self._option(db, 'requirements_version', 1),
                   'authorization_version': self._authorization_version(db)}
        safe_evidence(encoded(request))
        previous = db.execute('SELECT * FROM continuation_intents WHERE idempotency_key=?', (idempotency_key,)).fetchone()
        if previous:
            if previous['request_digest'] != digest(request):
                fail('Continuation conflict / 同一后续意图的内容或版本不符')
            return {'id': previous['id'], 'state': previous['state'], 'duplicate': True}
        now = time.time(); key = uuid.uuid4().hex
        db.execute('INSERT INTO continuation_intents VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (key, task_id, logical_goal, action, encoded(request), digest(request), idempotency_key, 'pending', now, now))
        if recipient:
            body = '接续事项已登记，须核验控制状态及原义务后再安排；不重做已有副作用。\n' + encoded({'intent': key, 'action': action, 'task': task_id})
            db.execute('INSERT INTO recovery_outbox(id,intent_id,recipient,body,task_id,created) VALUES(?,?,?,?,?,?)',
                       ('intent:' + key, key, recipient, body, task_id, now))
        self.event(db, 'continuation_intent_recorded', intent=key, action=action, task=task_id)
        return {'id': key, 'state': 'pending', 'duplicate': False}

    def _record_boundary_continuation(self, db, task_id, evidence):
        task = db.execute('SELECT * FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not task or task['status'] != 'handed_back':
            fail('Saved boundary required / 只为已安全交回任务登记剩余义务')
        return self._record_continuation(db, logical_goal=task_id, action='reconcile_remaining_work',
            payload={'source_task': task_id, 'original_member': task['assignee'], 'evidence': evidence,
                     'old_claim_not_reactivated': True}, idempotency_key='boundary:' + task_id + ':' + digest(evidence),
            task_id=task_id, recipient=self._coordinator(db))

    def _recovery_tick(self, db):
        # 只登记一次缺交付提示；未接上真实控制入口时不借 tick 自动放行业务。
        for row in db.execute("""SELECT m.id,m.recipient,m.task_id,t.status FROM messages m JOIN tasks t ON t.id=m.task_id
                 WHERE m.kind='task' AND m.remote_status='completed' AND m.process_status<>'completed'
                 AND t.status='active'""").fetchall():
            key = 'missing-delivery:' + row['id']
            if db.execute('SELECT 1 FROM recovery_outbox WHERE id=?', (key,)).fetchone():
                continue
            body = '原模型回合已结束，但任务尚无合法交付。请原负责人核对已完成成果与原领取；不要重派或重做原业务。task=' + row['task_id'] + '，message=' + row['id']
            db.execute('INSERT INTO recovery_outbox(id,recipient,body,task_id,created) VALUES(?,?,?,?,?)',
                       (key, row['recipient'], body, row['task_id'], time.time()))
            self.event(db, 'business_delivery_missing', task=row['task_id'], message=row['id'])
        for item in db.execute("SELECT * FROM recovery_outbox WHERE state='pending' ORDER BY created,id").fetchall():
            if self._recovery_gate(db, member=item['recipient'], task_id=item['task_id']):
                continue
            if item['intent_id'] and not self._recovery_policy_valid(db, 'continuation'):
                continue
            recipient = self._member(db, item['recipient'])
            if recipient['lifecycle'] == 'retired':
                continue
            message = self._send(db, self._coordinator(db), item['recipient'], item['body'], kind='note',
                                 task_id=item['task_id'], dedupe_key='recovery-outbox:' + item['id'])
            db.execute("UPDATE recovery_outbox SET state='queued',message_id=? WHERE id=?", (message['id'], item['id']))

    def _start_recovery_attempt(self, db, kind, stable_id, logical_id, evidence, *, now=None):
        if kind not in ATTEMPT_LIMITS or not stable_id or not logical_id:
            fail('Invalid counted operation / 处理计数域或稳定身份无效')
        old = db.execute('SELECT * FROM recovery_attempts WHERE kind=? AND stable_id=? AND logical_id=?', (kind, stable_id, logical_id)).fetchone()
        if old:
            return {'started': False, 'duplicate': True, 'state': old['state']}
        now = time.time() if now is None else now
        limit, window = ATTEMPT_LIMITS[kind]
        previous = db.execute('SELECT max(started) FROM recovery_attempts WHERE kind=? AND stable_id=?', (kind, stable_id)).fetchone()[0]
        if previous is not None and now < previous:
            fail('Clock moved backwards / 时钟回退，保留历史计数并等待核对')
        clause = ' AND started>?' if window else ''
        parameters = (kind, stable_id, now - window) if window else (kind, stable_id)
        if db.execute('SELECT count(*) FROM recovery_attempts WHERE kind=? AND stable_id=?' + clause, parameters).fetchone()[0] >= limit:
            fail('Attempt budget exhausted / 此稳定事项已达自动处理上限: ' + kind)
        db.execute('INSERT INTO recovery_attempts VALUES(?,?,?,?,?,?,?)', (kind, stable_id, logical_id, 'started', now, now, encoded(evidence)))
        self.event(db, 'recovery_attempt_started', counter=kind, stable_id=stable_id, logical_id=logical_id)
        return {'started': True, 'duplicate': False}

    def _finish_recovery_attempt(self, db, kind, stable_id, logical_id, state, evidence):
        changed = db.execute('UPDATE recovery_attempts SET state=?,updated=?,evidence=? WHERE kind=? AND stable_id=? AND logical_id=?',
                             (state, time.time(), encoded(evidence), kind, stable_id, logical_id)).rowcount
        if not changed:
            fail('Attempt missing / 不能结算未登记的处理轮次')

    def _recovery_delivery_allowed(self, db, member, delivery_id, delivery_digest):
        now = time.time(); actor = self._member(db, member)
        rows = db.execute('''SELECT r.*,g.delivery_digest,g.proposal_digest grant_digest FROM recovery_delivery_grants g
             JOIN recovery_requests r ON r.id=g.recovery_id WHERE g.delivery_id=? AND g.recipient=? AND g.revoked=0
             AND g.expires_at>? AND r.status IN ('queued','claimed','running')''', (delivery_id, member, now))
        return any(row['delivery_digest'] == delivery_digest and row['proposal_digest'] == row['grant_digest']
                   and row['reviewer_session'] == actor['session_uuid'] and row['binding_version'] == actor['binding_version']
                   and row['authorization_version'] == self._authorization_version(db)
                   and self._recovery_policy_valid(db, 'review_recovery', row['policy_version']) for row in rows)

    def _control_generation(self, db):
        return self._option(db, 'control_generation_v14', 0)

    @contextmanager
    def _recovery_control_transaction(self):
        try:
            from .pair_core import FileLock
        except ImportError:
            from pair_core import FileLock
        with FileLock(self.control_lock, timeout=30), self.transaction() as db:
            yield db

    def _ingest_recovery_control(self, db, member, session, binding_version, source_identity,
                                 position, event, *, initial_baseline=False, verified_evidence=None):
        """后台使用准确来源提交游标和控制事件；首次基准不重演历史。"""
        actor = self._member(db, member)
        if actor['session_uuid'] != session or actor['binding_version'] != binding_version or actor['lifecycle'] == 'retired':
            fail('Stale control source / 控制事件不是当前成员绑定')
        if not isinstance(source_identity, str) or not source_identity or type(position) is not int or position < 0:
            fail('Invalid control cursor / 控制事件来源或位置无效')
        old = db.execute('''SELECT * FROM control_event_cursors WHERE member=? AND session=? AND binding_version=? AND source_identity=?''',
                         (member, session, binding_version, source_identity)).fetchone()
        if old and position <= old['position']:
            return {'duplicate': True, 'generation': old['generation']}
        if not old and not initial_baseline:
            fail('Control source baseline required / 新来源、轮转或替换须先核对事件边界，不跳到末尾')
        if initial_baseline:
            if old:
                fail('Control baseline already exists / 重启不能重建已有基准跳过事件')
            generation = self._control_generation(db)
        else:
            context = {'pair': self.pair_id, 'member': member, 'session': session, 'binding_version': binding_version,
                       'source_identity': source_identity, 'from_position': old['position'], 'position': position, 'event': event}
            proof = self._accept_recovery_evidence('control_event', context, verified_evidence) if verified_evidence is not None else self._trusted_recovery_evidence('control_event', context)
            if not proof.facts.get('exact_source_verified') or not proof.facts.get('complete_record'):
                fail('Control event unverified / 日志来源或完整事件未核验')
            if proof.facts.get('confirmed_user_pause') or member == 'A' and proof.facts.get('confirmed_coordinator_interruption'):
                request = {'scope': {'kind': 'pair'}, 'reason': 'user_pause' if proof.facts.get('confirmed_user_pause') else 'coordinator_interrupted',
                           'source': {'identity': source_identity, 'position': position, 'receipts': proof.receipts}, 'session': session}
                result = self._pause_record(db, actor, {'kind': 'pair'}, request, 'control-event:' + digest(context))
                generation = result['generation']
            else:
                generation = self._control_generation(db)
        db.execute('INSERT OR REPLACE INTO control_event_cursors VALUES(?,?,?,?,?,?)',
                   (member, session, binding_version, source_identity, position, generation))
        return {'duplicate': False, 'generation': generation, 'baseline_only': initial_baseline}

    def _new_control_generation(self, db):
        generation = self._control_generation(db) + 1
        self._put(db, 'control_generation_v14', generation)
        return generation

    def _control_scope(self, scope):
        scope = scope or {'kind': 'pair'}
        if not isinstance(scope, dict) or scope.get('kind') not in ('pair', 'member', 'task'):
            fail('Invalid control scope / 暂停或恢复范围无效')
        expected = {'kind'} if scope['kind'] == 'pair' else {'kind', 'id'}
        if set(scope) != expected or ('id' in scope and (not isinstance(scope['id'], str) or not scope['id'])):
            fail('Exact control scope required / 控制范围须指定准确对象')
        return scope

    def _control_duplicate(self, db, key, request):
        if not isinstance(key, str) or not key or len(key) > 256:
            fail('Control idempotency key required / 控制请求须有稳定幂等键')
        old = db.execute('SELECT * FROM control_requests WHERE idempotency_key=?', (key,)).fetchone()
        if not old:
            return None
        if old['request_digest'] != digest(request):
            fail('Control request conflict / 同一控制请求内容不符')
        return {'id': old['id'], 'generation': old['generation'], 'state': old['state'], 'duplicate': True}

    def _pause_record(self, db, actor, scope, request, key):
        generation = self._new_control_generation(db); now = time.time(); record = uuid.uuid4().hex
        db.execute('INSERT INTO control_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (record, 'pause', generation, encoded(scope), 'pausing', digest(request), key, encoded(request), '{}', None, now, now))
        if scope['kind'] == 'pair' and db.execute('SELECT status FROM collaborations').fetchone()[0] != 'stopped':
            db.execute("UPDATE collaborations SET status='paused',revision=revision+1")
        self.event(db, 'pause_requested', control=record, generation=generation, member=actor['id'], confirmation='unverified')
        return {'id': record, 'generation': generation, 'state': 'pausing', 'duplicate': False,
                'all_executors_stopped': False, 'resources_preserved': True}

    def _matching_controls(self, db, member, task_id):
        result = []
        for row in db.execute("SELECT * FROM control_requests WHERE state IN ('pausing','paused','reconciling','blocked','partial')"):
            scope = json.loads(row['scope'])
            matches = scope['kind'] == 'pair' or scope == {'kind': 'member', 'id': member} or scope == {'kind': 'task', 'id': task_id}
            if member and db.execute('SELECT 1 FROM control_scope_releases WHERE control_id=? AND scope=?',
                                     (row['id'], encoded({'kind': 'member', 'id': member}))).fetchone():
                matches = False
            if matches and not (task_id and db.execute('SELECT 1 FROM control_releases WHERE control_id=? AND task_id=?', (row['id'], task_id)).fetchone()):
                result.append(row)
        return result

    def _recovery_goal(self, db):
        flow = self._option(db, 'window_flow', {})
        facts = {'pair': self.pair_id, 'goal': flow.get('goal'), 'project': db.execute('SELECT project FROM collaborations').fetchone()[0]}
        if self._option(db, 'closed_scope'):
            facts['closed_scope'] = self._option(db, 'closed_scope')
        return digest(facts)

    def _recovery_policy_valid(self, db, domain, version=None):
        row = db.execute('SELECT * FROM recovery_policies WHERE domain=?', (domain,)).fetchone()
        return bool(row and row['enabled'] and (version is None or row['version'] == version)
                    and row['authorization_version'] == self._authorization_version(db) and row['goal_digest'] == self._recovery_goal(db))

    def _require_recovery_policy(self, db, domain):
        if not self._recovery_policy_valid(db, domain):
            fail('Recovery authorization missing / 当前协作未启用该准确恢复权限域: ' + domain)
        return db.execute('SELECT * FROM recovery_policies WHERE domain=?', (domain,)).fetchone()

    def _recovery_request(self, db, recovery_id):
        row = db.execute('SELECT * FROM recovery_requests WHERE id=?', (recovery_id,)).fetchone()
        if not row:
            fail('Recovery not found / 恢复记录不存在')
        return row

    def _recovery_binding(self, row, actor):
        if (row['reviewer'], row['reviewer_session'], row['binding_version']) != (actor['id'], actor['session_uuid'], actor['binding_version']):
            fail('Recovery binding changed / 恢复许可不属于当前准确成员绑定')

    def _independent_recovery_actor(self, db, reviewer, task):
        proof = reviewer_identity(json.loads(reviewer['proof']))
        if proof['id'] != reviewer['session_uuid'] or reviewer['lifecycle'] != 'active':
            fail('Recovery reviewer unavailable / 恢复审核者实际审核身份或生命周期无效')
        contributors = {task['assignee']}
        for stage in db.execute('SELECT contributors FROM review_stages WHERE task_id=?', (task['id'],)):
            contributors.update(item['member'] for item in json.loads(stage[0]))
        if reviewer['id'] in contributors:
            fail('Independent recovery reviewer required / 恢复实现者不能审核自己的候选')

    def _validate_recovery_permit(self, db, row, actor):
        policy = self._require_recovery_policy(db, 'review_recovery')
        if (row['authorization_version'] != self._authorization_version(db) or row['policy_version'] != policy['version']
                or not row['expires_at'] or row['expires_at'] <= time.time()):
            fail('Recovery permit expired or changed / 恢复许可已过期或授权已变化')
        task = db.execute('SELECT * FROM tasks WHERE id=?', (row['source_task'],)).fetchone()
        if not task or task['status'] == 'cancelled':
            fail('Recovery source cancelled / 恢复原任务已失效')
        self._independent_recovery_actor(db, actor, task)
        if self._member_suspended(db, actor['id']) or self._pool_blocked(db, actor['id']):
            fail('Recovery quota unavailable / 审核者额度或账号池不可用')
        proposal = json.loads(row['proposal'])
        if candidate_manifest(proposal['candidate_root'], proposal['manifest'], proposal['allowed_paths']) != proposal['candidate_manifest_digest']:
            fail('Recovery candidate changed / 恢复候选已变化')

    def _require_recovery_read(self, db, row, actor):
        read = db.execute('SELECT * FROM recovery_reads WHERE recovery_id=? AND member=? AND session=? AND binding_version=?',
                          (row['id'], actor['id'], actor['session_uuid'], actor['binding_version'])).fetchone()
        if not read or read['digest'] != row['proposal_digest'] or read['read_end'] < len(row['proposal']):
            fail('Recovery material unread / 恢复材料未按准确摘要完整分页读取')

    def _recovery_unresolved(self, db, task_id=None, *, ignore_terminal_workspace_activity=False):
        clause = ' AND task_id=?' if task_id else ''; values = (task_id,) if task_id else ()
        managed_scope = ' AND (s.task_id=? OR s.id IS NULL)' if task_id else ''
        if db.execute("""SELECT 1 FROM managed_permissions p LEFT JOIN review_stages s
                       ON s.execution_id=p.execution_id AND s.reviewer_epoch=p.epoch AND s.reviewer_session=p.session
                       WHERE p.status IN ('pending','allowed','granted','unknown')""" + managed_scope, values).fetchone():
            return True
        if db.execute("SELECT 1 FROM approval_requests WHERE status IN ('pending','allowed','granted','unknown')" + clause, values).fetchone():
            return True
        if db.execute("SELECT 1 FROM execution_requests WHERE status IN ('running','unknown','needs_authorization')" + clause, values).fetchone():
            return True
        if db.execute("SELECT 1 FROM review_stages WHERE status='running'" + clause, values).fetchone():
            return True
        for message in self._running(db):
            if task_id and message['task_id'] != task_id:
                continue
            # 接续者自己的新回合可使旧终态消息显示 workspace_busy，不能把它误作原工具复活。
            if (ignore_terminal_workspace_activity and message['delivery_status'] == 'accepted'
                    and message['remote_status'] in ('completed', 'failed', 'interrupted')):
                continue
            return True
        return False

    def _recovery_admission_cycle(self, db, task, proposal, snapshot=None):
        """仅接受已登记 repair link 证明的循环；相似名称或提案文字不构成边。"""
        general=self._general_recovery_cycle(db,task,proposal)
        if general is not None:return general
        if snapshot is None:
            try:
                from .pair_diagnostics import snapshot_from_db
            except ImportError:
                from pair_diagnostics import snapshot_from_db
            snapshot = snapshot_from_db(db, self.pair_id, task_id=task['id'])
        remediates = proposal['remediates']
        if not isinstance(remediates, list) or not remediates or any(not isinstance(key, str) for key in remediates):
            fail('Explicit repair relation required / 恢复必须指定已有稳定问题关系')
        links = [dict(row) for row in db.execute('''SELECT r.issue_id,r.source_stage,i.root_task,s.status,s.task_id
                    FROM repair_links r JOIN review_issues i ON i.id=r.issue_id
                    JOIN review_stages s ON s.id=r.source_stage WHERE r.task_id=?''', (task['id'],))]
        if set(remediates) != {link['issue_id'] for link in links}:
            fail('Recovery relation unverified / 提案必须匹配原任务已登记的完整 repair link')
        stable = 'issues:' + digest(sorted(remediates))[:24]
        if proposal['stable_blocker'] != stable:
            fail('Stable blocker mismatch / 稳定阻塞ID须从已有问题关系取得: ' + stable)
        occupied = set()
        for limit in snapshot['limits']:
            if limit['member'] == task['assignee'] and limit['count'] >= limit['limit']:
                occupied.update(limit['items'])
        blocked = [link for link in links if link['status'] in ('pending', 'needs_validation', 'needs_fix')
                   and (link['source_stage'] in occupied or link['root_task'] in occupied)]
        if len(blocked) != len(links):
            fail('Recovery admission cycle unverified / 原问题未形成容量准入循环，使用普通审核流程')
        # 原依赖仍可正常运行时优先推进它，不能借恢复许可绕过前置成果。
        for dependency in db.execute('SELECT depends_on FROM dependencies WHERE task_id=?', (task['id'],)):
            parent = db.execute('SELECT * FROM tasks WHERE id=?', (dependency[0],)).fetchone()
            if parent['status'] not in ('completed', 'integrated') and parent['id'] not in {item['root_task'] for item in links}:
                fail('Normal prerequisite unresolved / 仍有正常前置任务，先推进或核对原前置')
        return {'fingerprint': digest(sorted((link['issue_id'], link['source_stage'], link['root_task']) for link in links)),
                'edges': links, 'state_digest': snapshot['state_digest']}

    def _formal_recovery_evidence(self, db, recovery, report, context):
        """引用既有正式结算，不允许报告自行填写 passed 作为证明。"""
        stage = db.execute('SELECT * FROM review_stages WHERE id=?', (report['formal_stage'],)).fetchone()
        if (not stage or stage['status'] not in ('passed', 'needs_fix', 'needs_validation')
                or stage['reviewer_session'] != recovery['reviewer_session']
                or stage['execution_id'] != recovery['execution_id'] or stage['reviewer_epoch'] != recovery['execution_epoch']):
            fail('Exact formal recovery stage required / 正式阶段须匹配恢复审核者、代次和执行')
        task = db.execute('SELECT * FROM tasks WHERE id=?', (stage['task_id'],)).fetchone()
        if task['requirements_version'] != stage['requirements_version']:
            fail('Formal recovery requirements changed / 正式阶段需求版本已变化')
        payload = json.loads(stage['payload']); proposal = json.loads(recovery['proposal'])
        if (payload.get('recovery_id') != recovery['id']
                or payload.get('candidate_manifest_digest') != proposal['candidate_manifest_digest']):
            fail('Formal recovery candidate mismatch / 正式阶段未绑定本次恢复及准确候选')
        matches = []
        for event in db.execute("SELECT seq,data FROM events WHERE kind='review_effective_verdict'"):
            value = json.loads(event['data'])
            if value.get('stage') == stage['id'] and value.get('execution') == stage['execution_id']:
                matches.append((event['seq'], value))
        if len(matches) != 1:
            fail('Formal recovery receipt missing / 当前准确执行缺少唯一正式结算记录')
        seq, effective = matches[0]
        if any(effective.get(key) != expected for key, expected in
               (('digest', stage['digest']), ('session', stage['reviewer_session']), ('epoch', stage['reviewer_epoch']),
                ('turn', stage['turn_id']), ('effective_verdict', stage['status']))):
            fail('Formal recovery receipt mismatch / 正式结算与阶段绑定不符')
        try:
            from .pair_producer import KEY, producer_context
        except ImportError:
            from pair_producer import KEY, producer_context
        producer = self._option(db, KEY + stage['execution_id'])
        expected = dict(producer.get('binding', {})) if isinstance(producer, dict) else {}
        for key, value in {'pair': self.pair_id, 'stage': stage['id'], 'digest': stage['digest'],
                           'epoch': stage['reviewer_epoch'], 'session': stage['reviewer_session'],
                           'execution': stage['execution_id'], 'turn': stage['turn_id']}.items():
            if expected.get(key) != value:
                fail('Formal producer mismatch / 缺少准确正式生产者绑定')
        try:
            producer_context(producer, expected, live=False)
        except (ValueError, KeyError, TypeError) as exc:
            fail('Formal producer unverified / 正式生产者记录未通过核验: ' + str(exc))
        if not stage['evidence']:
            fail('Formal checks missing / 正式检查证据缺失')
        evidence = json.loads(stage['evidence'])
        passed = stage['status'] == 'passed'
        if report['verdict'] != stage['status']:
            fail('Formal verdict differs / 不能改写既有正式结论')
        try:
            from .pair_core import PairError
        except ImportError:
            from pair_core import PairError
        unchanged = True; file_error = None
        try:
            candidate_manifest(proposal['candidate_root'], proposal['manifest'], proposal['allowed_paths'])
        except (PairError, OSError) as exc:
            unchanged = False; file_error = str(exc)
        return VerifiedRecoveryEvidence('finish', digest(context), ('event:' + str(seq),),
            {'execution_terminal': True, 'official_dual_receipts': passed, 'contract_satisfied': passed,
             'files_unchanged': passed and unchanged, 'files_verification_error': file_error,
             'independent_review': passed, 'formal_report_digest': digest(evidence)})

    def _recovery_state_digest(self, db):
        return digest({'pair': dict(db.execute('SELECT * FROM collaborations').fetchone()),
                       'last_event': db.execute('SELECT max(seq) FROM events').fetchone()[0],
                       'generation': self._control_generation(db), 'authorization_version': self._authorization_version(db)})

    def _recovery_context(self, db, row, *, verify_candidate=True):
        proposal = json.loads(row['proposal'])
        actual = candidate_manifest(proposal['candidate_root'], proposal['manifest'], proposal['allowed_paths']) if verify_candidate else proposal['candidate_manifest_digest']
        if verify_candidate and actual != proposal['candidate_manifest_digest']:
            fail('Recovery candidate changed / 当前候选与恢复提案不符')
        return {'pair': self.pair_id, 'recovery': row['id'], 'proposal_digest': row['proposal_digest'],
                'candidate_manifest_digest': actual, 'reviewer': row['reviewer'], 'session': row['reviewer_session'],
                'binding_version': row['binding_version'], 'epoch': row['execution_epoch'], 'execution_id': row['execution_id'],
                'authorization_version': row['authorization_version'], 'state_digest': self._recovery_state_digest(db),
                'candidate_rechecked': verify_candidate}

    def _trusted_recovery_evidence(self, kind, context):
        adapter = getattr(self, '_recovery_evidence_adapter', None)
        if adapter is None:
            fail('Needs validation: trusted recovery adapter unavailable / 缺少已核验的宿主执行与回执适配，保持待验证')
        return self._accept_recovery_evidence(kind, context, adapter.verify(kind, context))

    def _accept_recovery_evidence(self, kind, context, proof):
        if (not isinstance(proof, VerifiedRecoveryEvidence) or proof.kind != kind
                or proof.context_digest != digest(context) or not proof.receipts
                or not isinstance(proof.facts, dict)
                or any(not isinstance(item, str) or not item for item in proof.receipts)):
            fail('Needs validation: recovery evidence mismatch / 可信证据未绑定当前准确恢复对象')
        return proof
