"""辅助池的准入、累计预算与固定候选报告；模型文字不能授予运行权或阶段通过。"""
from __future__ import annotations

import json
import re
import time
import uuid

try:
    from .pair_reviewer import fail, reviewer_identity, safe_evidence
    from .pair_stages import digest, encoded
    from .pair_transport import canonical_path, session_uuid
    from . import pair_balance
except ImportError:
    from pair_reviewer import fail, reviewer_identity, safe_evidence
    from pair_stages import digest, encoded
    from pair_transport import canonical_path, session_uuid
    import pair_balance


DEFAULT_POLICY = {'enabled': False, 'concurrency': 1, 'task_capacity': 2, 'stage_capacity': 2,
                  'automatic': True, 'max_concurrency': 4}
ACTIVE_EXECUTIONS = ('reserved', 'starting', 'running', 'unknown')
ROLES = ('analysis', 'discovery', 'validation', 'final_verification', 'conflict_review')
INDEPENDENT_ROLES = ('validation', 'final_verification', 'conflict_review')


def upgrade_auxiliary_v14(db):
    """由主迁移事务调用；不提交事务、不改 user_version、不覆盖旧审核记录。"""
    statements = (
        '''CREATE TABLE IF NOT EXISTS auxiliary_configs(
            id TEXT PRIMARY KEY, member TEXT NOT NULL, binding TEXT NOT NULL,
            enabled INTEGER NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS auxiliary_sessions(
            id TEXT PRIMARY KEY, config_id TEXT NOT NULL REFERENCES auxiliary_configs(id),
            session_uuid TEXT UNIQUE, epoch INTEGER NOT NULL, cwd TEXT NOT NULL,
            model TEXT NOT NULL, pool TEXT, state TEXT NOT NULL, creation_id TEXT NOT NULL UNIQUE,
            proof TEXT, created REAL NOT NULL, updated REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS auxiliary_audits(
            id TEXT PRIMARY KEY, root_task TEXT NOT NULL UNIQUE REFERENCES tasks(id),
            objective TEXT NOT NULL, budget INTEGER NOT NULL DEFAULT 64,
            completion_reserve INTEGER NOT NULL DEFAULT 8, created REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS auxiliary_candidates(
            id TEXT PRIMARY KEY, audit_id TEXT NOT NULL REFERENCES auxiliary_audits(id),
            stage_id TEXT NOT NULL UNIQUE REFERENCES review_stages(id),
            candidate_digest TEXT NOT NULL, contract_digest TEXT NOT NULL,
            requirements_version INTEGER NOT NULL, tracks TEXT NOT NULL,
            coverage_units TEXT NOT NULL, contributors TEXT NOT NULL,
            status TEXT NOT NULL, result TEXT, created REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS auxiliary_tasks(
            id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL REFERENCES auxiliary_candidates(id),
            parent_task TEXT REFERENCES auxiliary_tasks(id), track TEXT NOT NULL, role TEXT NOT NULL,
            unit_ids TEXT NOT NULL, dependencies TEXT NOT NULL, independent_of TEXT NOT NULL,
            resources TEXT NOT NULL, requires_dynamic INTEGER NOT NULL,
            status TEXT NOT NULL, last_execution TEXT, waiting TEXT, created REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS auxiliary_executions(
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES auxiliary_tasks(id),
            audit_id TEXT NOT NULL REFERENCES auxiliary_audits(id),
            reviewer_id TEXT NOT NULL REFERENCES auxiliary_sessions(id), epoch INTEGER NOT NULL,
            session_uuid TEXT, lease TEXT NOT NULL, candidate_digest TEXT NOT NULL,
            contract_digest TEXT NOT NULL, config_binding TEXT NOT NULL,
            state TEXT NOT NULL, charged INTEGER NOT NULL DEFAULT 0, turn_id TEXT,
            producer TEXT, terminal_evidence TEXT, started REAL, created REAL NOT NULL,
            updated REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS auxiliary_reports(
            execution_id TEXT PRIMARY KEY REFERENCES auxiliary_executions(id),
            report_digest TEXT NOT NULL, raw_report TEXT NOT NULL, adapted_report TEXT NOT NULL,
            validation TEXT NOT NULL, evidence TEXT NOT NULL, created REAL NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS auxiliary_contributions(
            audit_id TEXT NOT NULL, context_id TEXT NOT NULL, kind TEXT NOT NULL,
            task_id TEXT NOT NULL, PRIMARY KEY(audit_id,context_id,kind,task_id))''',
        '''CREATE TABLE IF NOT EXISTS auxiliary_resources(
            resource TEXT PRIMARY KEY, execution_id TEXT NOT NULL REFERENCES auxiliary_executions(id))''',
        '''CREATE TABLE IF NOT EXISTS auxiliary_legacy_bindings(
            epoch INTEGER PRIMARY KEY, snapshot TEXT NOT NULL, imported REAL NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS auxiliary_execution_audit ON auxiliary_executions(audit_id,state)',
        'CREATE INDEX IF NOT EXISTS auxiliary_task_candidate ON auxiliary_tasks(candidate_id,status)',
    )
    for statement in statements:
        db.execute(statement)
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviewer_runtime'").fetchone():
        for row in db.execute('SELECT * FROM reviewer_runtime').fetchall():
            value = dict(row)
            db.execute('INSERT OR IGNORE INTO auxiliary_legacy_bindings VALUES(?,?,?)',
                       (value['epoch'], encoded(value), time.time()))


class AuxiliaryMixin:
    def auxiliary_policy(self, member, session, cwd, *, enabled=None, concurrency=None,
                         task_capacity=None, stage_capacity=None, reason=None, automatic=None, max_concurrency=None):
        changes = {k: v for k, v in locals().items()
                   if k in DEFAULT_POLICY and v is not None}
        if 'enabled' in changes and type(enabled) is not bool:
            fail('Invalid auxiliary policy / enabled 必须是布尔值')
        if automatic is not None and type(automatic) is not bool:fail('Invalid automatic policy / automatic须为布尔值')
        if concurrency is not None and automatic is None:changes['automatic']=False
        for key in ('concurrency', 'task_capacity', 'stage_capacity','max_concurrency'):
            if key in changes and (type(changes[key]) is not int
                                   or not (1 if key in ('concurrency','max_concurrency') else 2) <= changes[key] <= 4):
                fail('Invalid auxiliary capacity / 辅助并发为1—4，两套待审容量分别为2—4')
        if changes:
            safe_evidence(reason)
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd)
            previous = self._auxiliary_policy(db)
            current = dict(previous, **changes)
            if current['concurrency']>current['max_concurrency']:current['concurrency']=current['max_concurrency']
            if changes:
                if current['enabled'] and not previous['enabled']:
                    old = self._reviewer_row(db)
                    if old and old['state'] in ('starting', 'running', 'unknown'):
                        fail('Legacy reviewer unresolved / 旧审核执行须先对账，不能并行启动两套控制器')
                    self._put(db, 'reviewer_auto', False)
                if changes.get('enabled') is True:
                    current['scope_digest'] = digest(self._option(db, 'closed_scope'))
                self._put(db, 'auxiliary_policy', current)
                self.event(db, 'auxiliary_policy_changed', previous=previous, current=current, reason=reason)
            return dict(current, active=self._auxiliary_active(db), existing_work_preserved=True)

    def _auxiliary_policy(self, db):
        return dict(DEFAULT_POLICY, **self._option(db, 'auxiliary_policy', {}))

    def _review_capacity(self, db, kind):
        if kind not in ('task', 'stage'):
            fail('Unknown review capacity / 未知待审容量')
        return self._auxiliary_policy(db)[kind + '_capacity']

    def allow_auxiliary_config(self, member, session, cwd, config_id, source_member, reason,
                               *, enabled=True):
        _identifier(config_id)
        safe_evidence(reason)
        if type(enabled) is not bool:
            fail('Invalid config policy / enabled 必须是布尔值')
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd)
            source = self._member(db, source_member)
            reviewer_identity(json.loads(source['proof']))
            if source['lifecycle'] != 'active' or not source['verified']:
                fail('Verified config required / 只登记本轮已核验的GPT审核配置')
            binding = _config_binding(source)
            old = db.execute('SELECT * FROM auxiliary_configs WHERE id=?', (config_id,)).fetchone()
            if old and old['binding'] != encoded(binding):
                if db.execute("SELECT 1 FROM auxiliary_sessions s JOIN auxiliary_executions e ON e.reviewer_id=s.id WHERE s.config_id=? AND e.state IN ('reserved','starting','running','unknown')", (config_id,)).fetchone():
                    fail('Config execution unresolved / 旧配置仍有执行，先核实终态')
                db.execute("UPDATE auxiliary_sessions SET state='retired' WHERE config_id=?", (config_id,))
            db.execute('''INSERT INTO auxiliary_configs VALUES(?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET member=excluded.member,binding=excluded.binding,
                enabled=excluded.enabled,reason=excluded.reason''',
                       (config_id, source_member, encoded(binding), int(enabled), reason, time.time()))
            self.event(db, 'auxiliary_config_allowed', config=config_id, source_member=source_member,
                       enabled=enabled, binding_version=source['binding_version'], reason=reason)
            return {'config_id': config_id, 'enabled': enabled, 'runtime_identity_recheck_required': True}

    def register_audit(self, member, session, cwd, audit_id, stage_id, *, tracks, coverage_units,
                       completion_reserve=8):
        _identifier(audit_id)
        if (not isinstance(tracks, list) or not tracks or len(set(tracks)) != len(tracks)
                or any(t not in ('ocr', 'cloudflare') for t in tracks)):
            fail('Invalid audit tracks / 必须固定 ocr/cloudflare 必需轨道')
        if type(completion_reserve) is not int or not 1 <= completion_reserve <= 32:
            fail('Invalid completion reserve / 必须为验证、最终核验及父接续预留1—32次预算')
        if not isinstance(coverage_units, list) or not coverage_units:
            fail('Coverage required / 必须先固定覆盖单元，不能以零发现替代覆盖')
        units = []
        for unit in coverage_units:
            if not isinstance(unit, dict) or unit.get('track') not in tracks or type(unit.get('required')) is not bool:
                fail('Invalid coverage unit / 覆盖单元须有 id、track、required')
            _identifier(unit.get('id'))
            units.append(dict(unit))
        if len({(u['track'], u['id']) for u in units}) != len(units):
            fail('Duplicate coverage unit / 同轨道覆盖单元不能重复')
        safe_evidence(encoded(units))
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd)
            stage = db.execute('SELECT * FROM review_stages WHERE id=?', (stage_id,)).fetchone()
            if not stage or stage['status'] not in ('pending', 'needs_validation', 'needs_fix'):
                fail('Fixed candidate required / 仅对准确未运行候选建立融合审计')
            task = db.execute('SELECT * FROM tasks WHERE id=?', (stage['task_id'],)).fetchone()
            root = task['id']
            seen = set()
            while root not in seen:
                seen.add(root)
                link = db.execute('SELECT i.root_task FROM repair_links l JOIN review_issues i ON i.id=l.issue_id WHERE l.task_id=?', (root,)).fetchone()
                if not link or link[0] == root:
                    break
                root = link[0]
            existing = db.execute('SELECT * FROM auxiliary_audits WHERE root_task=?', (root,)).fetchone()
            if existing:
                audit_id = existing['id']
            else:
                if db.execute('SELECT 1 FROM auxiliary_audits WHERE id=?', (audit_id,)).fetchone():
                    fail('Audit identity conflict / 同审计ID不能重新绑定另一个目标')
                db.execute('INSERT INTO auxiliary_audits VALUES(?,?,?,?,?,?)',
                           (audit_id, root, task['title'], 64, completion_reserve, time.time()))
            payload = json.loads(stage['payload'])
            contract = digest({'requirements_version': stage['requirements_version'],
                               'acceptance': payload.get('coordinator_acceptance', []),
                               'checks': payload.get('coordinator_required_checks', [])})
            authors = []
            for contributor in json.loads(stage['contributors']):
                context = contributor.get('session_uuid') or contributor.get('session')
                if context:
                    authors.append(session_uuid(context))
                else:
                    fail('Contributor identity unavailable / 贡献者准确会话缺失，不能授予独立审核资格')
            old = db.execute('SELECT * FROM auxiliary_candidates WHERE stage_id=?', (stage_id,)).fetchone()
            material = (encoded(tracks), encoded(units), encoded(sorted(set(authors))))
            if old:
                if (old['tracks'], old['coverage_units'], old['contributors']) != material:
                    fail('Audit scope immutable / 不能删轨道或覆盖义务以改变已有审核结论')
                return dict(old)
            candidate_id = uuid.uuid4().hex
            db.execute('INSERT INTO auxiliary_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (candidate_id, audit_id, stage_id, stage['digest'], contract,
                        stage['requirements_version'], *material, 'pending', None, time.time()))
            for context in set(authors):
                db.execute('INSERT OR IGNORE INTO auxiliary_contributions VALUES(?,?,?,?)',
                           (audit_id, context, 'implementation', task['id']))
            self.event(db, 'auxiliary_audit_registered', audit=audit_id, candidate=candidate_id,
                       stage=stage_id, tracks=tracks, budget_preserved=bool(existing))
            return dict(db.execute('SELECT * FROM auxiliary_candidates WHERE id=?', (candidate_id,)).fetchone())

    def prepare_auxiliary_quota_consent(self, member, session, cwd, config_id):
        with self.transaction() as db:
            actor = self._a_actor(db, member, session, cwd)
            if member != 'A':
                fail('Original A required / 辅助额度授权须由原A向用户展示')
            scope = self._auxiliary_quota_scope(db, config_id)
            proposal_id = uuid.uuid4().hex
            proposal = {'id': proposal_id, 'scope': scope, 'session': session,
                        'binding_version': actor['binding_version'], 'created': time.time()}
            self._put(db, 'auxiliary_quota_proposal:' + proposal_id, proposal)
            return {'proposal_id': proposal_id, 'scope': scope, 'enabled': False,
                    'confirmation_text': '忽略辅助额度 ' + proposal_id,
                    'instruction': '向用户展示本协作、允许配置、账号池、桶及周期后，等待原A的真实用户消息；普通JSON不能授权。'}

    def auxiliary_quota_policy(self, member, session, cwd, config_id, allow_soft_limit, reason, *, source_event=None):
        """只授权本协作当前已核实账号池和周期；工作窗口豁免从不自动继承。"""
        if type(allow_soft_limit) is not bool:
            fail('Invalid auxiliary quota policy / 额度豁免须明确启用或撤销')
        safe_evidence(reason)
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd)
            config, source = self._auxiliary_config(db, config_id)
            quota = pair_balance.current(self._quotas(db).get(source['id'], {}))
            if not allow_soft_limit:
                revoked = []
                for row in db.execute("SELECT key,value FROM pair_settings WHERE key LIKE 'auxiliary_quota_override:%'").fetchall():
                    value = json.loads(row['value'])
                    if config_id in value.get('bindings', {}):
                        value['enabled'] = False
                        self._put(db, row['key'], value)
                        revoked.append(value['pool'])
                self.event(db, 'auxiliary_quota_override_revoked', config=config_id, reason=reason)
                return {'enabled': False, 'pools': revoked}
            event = self._option(db, 'auxiliary_quota_user_event:' + str(source_event), {})
            scope = self._auxiliary_quota_scope(db, config_id)
            grant = self._option(db, 'auxiliary_quota_override:' + scope['pool'], {})
            if (not event.get('enabled') or event.get('scope') != scope or not grant.get('enabled') or grant.get('scope') != scope
                    or grant.get('source_event') != source_event or member != 'A'
                    or event.get('source_session') != session):
                fail('Direct auxiliary consent required / 必须引用原A准确用户消息及已展示的当前池周期；reason不是授权')
            return {'enabled': True, 'pool': scope['pool'], 'cycle': scope['cycle'], 'source_event': source_event,
                    'hard_errors_still_block': True, 'audit_budget_unchanged': True}

    def _auxiliary_quota_scope(self, db, config_id):
        config, source = self._auxiliary_config(db, config_id)
        quota = pair_balance.current(self._quotas(db).get(source['id'], {}))
        if quota.get('reliability') != 'fresh' or not quota.get('pool') or not quota.get('windows'):
            fail('Auxiliary quota cycle unknown / 未核实账号池和当前周期')
        bindings = {}
        for allowed in db.execute('SELECT * FROM auxiliary_configs WHERE enabled=1'):
            current = self._quotas(db).get(allowed['member'], {})
            if current.get('pool') == quota['pool']:
                bindings[allowed['id']] = digest(allowed['binding'])
        scope = {'pair': self.pair_id, 'config_id': config_id, 'pool': quota['pool'],
                 'limit_id': quota.get('limit_id'), 'cycle': _quota_cycle(quota), 'bindings': bindings}
        if self._option(db, 'closed_scope'):
            scope['closed_scope_digest'] = digest(self._option(db, 'closed_scope'))
        return scope

    def _record_auxiliary_quota_user_event(self, db, member, text, *, event_id, source_identity,
                                          session, binding_version):
        if member != 'A':
            return {'recognized': True, 'enabled': False, 'reason': 'original_a_required'}
        actor = self._member(db, member)
        if actor['session_uuid'] != session or actor['binding_version'] != binding_version:
            return {'recognized': True, 'enabled': False, 'reason': 'stale_binding'}
        key = 'auxiliary_quota_user_event:' + event_id
        old = self._option(db, key)
        if old is not None:
            return dict(old, duplicate=True)
        if text.strip() == '恢复辅助额度保护':
            for row in db.execute("SELECT key,value FROM pair_settings WHERE key LIKE 'auxiliary_quota_override:%'").fetchall():
                grant = json.loads(row['value'])
                grant['enabled'] = False
                self._put(db, row['key'], grant)
            result = {'enabled': False, 'source_event': event_id, 'source_session': session}
        else:
            match = re.fullmatch(r'忽略辅助额度 ([0-9a-f]{32})', text.strip())
            if not match:
                return {'recognized': False}
            proposal = self._option(db, 'auxiliary_quota_proposal:' + match[1], {})
            if (proposal.get('session') != session or proposal.get('binding_version') != binding_version
                    or not proposal.get('scope') or proposal.get('consumed_by')):
                return {'recognized': True, 'enabled': False, 'reason': 'proposal_missing_or_stale'}
            try:
                from .pair_core import PairError
            except ImportError:
                from pair_core import PairError
            try:
                scope = self._auxiliary_quota_scope(db, proposal['scope']['config_id'])
            except PairError:
                result = {'recognized': True, 'enabled': False, 'reason': 'quota_scope_unavailable',
                          'source_event': event_id, 'source_session': session}
                self._put(db, key, result)
                self.event(db, 'auxiliary_quota_user_consent_rejected', source_event=event_id, reason=result['reason'])
                return result
            if scope != proposal['scope']:
                return {'recognized': True, 'enabled': False, 'reason': 'displayed_scope_changed'}
            result = {'enabled': True, 'scope': scope, 'source_event': event_id,
                      'source_session': session, 'source_binding_version': binding_version,
                      'source_identity': source_identity, 'created': time.time()}
            self._put(db, 'auxiliary_quota_override:' + scope['pool'], dict(scope, **result))
            self._put(db, 'auxiliary_quota_proposal:' + match[1], dict(proposal, consumed_by=event_id))
        self._put(db, key, result)
        self.event(db, 'auxiliary_quota_user_consent', enabled=result['enabled'], source_event=event_id,
                   hard_limits_preserved=True)
        return result

    def queue_auxiliary(self, member, session, cwd, task_id, candidate_id, track, role, unit_ids,
                        *, dependencies=(), independent_of=(), parent_task=None, resources=(),
                        requires_dynamic=False):
        _identifier(task_id)
        if role not in ROLES or track not in ('ocr', 'cloudflare') or type(requires_dynamic) is not bool:
            fail('Invalid auxiliary task / 辅助任务仅分析、发现、验证和独立复核')
        for items in (unit_ids, dependencies, independent_of, resources):
            if not isinstance(items, (list, tuple)) or len(items) != len(set(items)):
                fail('Invalid auxiliary references / 标识列表不能重复')
            for item in items:
                _identifier(item)
        if not unit_ids:
            fail('Coverage units required / 辅助任务必须归属已登记覆盖单元')
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd)
            candidate = self._auxiliary_candidate(db, candidate_id)
            available = {u['id'] for u in json.loads(candidate['coverage_units']) if u['track'] == track}
            if track not in json.loads(candidate['tracks']) or not set(unit_ids) <= available:
                fail('Auxiliary scope mismatch / 不得扩大轨道或覆盖范围')
            for related in set(dependencies) | set(independent_of) | ({parent_task} if parent_task else set()):
                row = db.execute('SELECT candidate_id FROM auxiliary_tasks WHERE id=?', (related,)).fetchone()
                if not row or row[0] != candidate_id or related == task_id:
                    fail('Invalid auxiliary dependency / 依赖及独立性必须绑定同一准确候选的已有任务')
            value = (candidate_id, parent_task, track, role, encoded(list(unit_ids)),
                     encoded(list(dependencies)), encoded(list(independent_of)), encoded(list(resources)),
                     int(requires_dynamic))
            old = db.execute('SELECT * FROM auxiliary_tasks WHERE id=?', (task_id,)).fetchone()
            if old:
                keys = ('candidate_id', 'parent_task', 'track', 'role', 'unit_ids', 'dependencies',
                        'independent_of', 'resources', 'requires_dynamic')
                if tuple(old[k] for k in keys) != value:
                    fail('Auxiliary task immutable / 同任务ID不能改写候选、依赖或审查范围')
                return dict(old)
            db.execute('INSERT INTO auxiliary_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (task_id, *value, 'pending', None, None, time.time()))
            self.event(db, 'auxiliary_task_queued', task=task_id, candidate=candidate_id, role=role)
            return dict(db.execute('SELECT * FROM auxiliary_tasks WHERE id=?', (task_id,)).fetchone())

    def reserve_auxiliary(self, member, session, cwd, task_id, config_id, *, reviewer_id=None):
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd)
            task = self._auxiliary_task(db, task_id)
            try:from .pair_hidden_handoff import task_gate
            except ImportError:from pair_hidden_handoff import task_gate
            if task_gate(self,db,task_id):fail('Auxiliary handoff awaiting acceptance / 原任务须先完成交接，不换会话重派')
            candidate = self._auxiliary_candidate(db, task['candidate_id'])
            origin = db.execute('SELECT task_id FROM review_stages WHERE id=?', (candidate['stage_id'],)).fetchone()[0]
            self._auxiliary_dispatchable(db, task_id=origin)
            if task['status'] not in ('pending', 'waiting'):
                fail('Auxiliary task unavailable / 任务已领取或须先核实旧执行')
            for dependency in json.loads(task['dependencies']):
                row = self._auxiliary_task(db, dependency)
                if row['status'] != 'completed':
                    fail('Auxiliary dependency waiting / 依赖尚未完成')
            config, source = self._auxiliary_config(db, config_id)
            self._auxiliary_quota(db, config, source)
            active = self._auxiliary_active(db)
            if active >= self._auxiliary_policy(db)['concurrency']:
                fail('Auxiliary pool full / 在途及unknown占用已达辅助上限')
            audit = db.execute('SELECT * FROM auxiliary_audits WHERE id=?', (candidate['audit_id'],)).fetchone()
            counts = self._auxiliary_budget(db, audit['id'])
            balance = audit['budget'] - counts['charged'] - counts['reserved']
            floor = audit['completion_reserve'] if task['role'] in ('analysis', 'discovery') else 0
            if balance <= floor:
                fail('Auxiliary budget reserved / 保留验证及接续预算，不能继续新查找')
            for resource in json.loads(task['resources']):
                if db.execute('SELECT 1 FROM auxiliary_resources WHERE resource=?', (resource,)).fetchone():
                    fail('Auxiliary resource held / 必需资源仍由其他准确执行占用')
            now = time.time()
            if reviewer_id is not None:
                reviewer = db.execute('SELECT * FROM auxiliary_sessions WHERE id=?', (reviewer_id,)).fetchone()
                if not reviewer or reviewer['config_id'] != config_id or reviewer['state'] != 'idle':
                    fail('Auxiliary session unavailable / 必须指定准确空闲辅助会话')
                try:from .pair_hidden_handoff import blocked
                except ImportError:from pair_hidden_handoff import blocked
                if blocked(self,db,{'kind':'auxiliary','id':reviewer_id,'session':reviewer['session_uuid'],'epoch':reviewer['epoch']}):fail('Auxiliary handoff pending')
                self._auxiliary_independent(db, candidate, task, reviewer['session_uuid'])
                if task['role'] in INDEPENDENT_ROLES and db.execute('SELECT 1 FROM auxiliary_executions WHERE session_uuid=? AND charged=1', (reviewer['session_uuid'],)).fetchone():
                    fail('Fresh context required / 独立验证使用未继承旧审核聊天的新上下文')
                reviewer = dict(reviewer)
                reviewer['epoch'] += 1
                db.execute("UPDATE auxiliary_sessions SET epoch=?,state='reserved',updated=? WHERE id=?",
                           (reviewer['epoch'], now, reviewer_id))
            else:
                reviewer_id = uuid.uuid4().hex
                reviewer = {'id': reviewer_id, 'epoch': 1, 'session_uuid': None}
                proof = json.loads(source['proof'])
                root = str(self.directory / 'auxiliary' / reviewer_id)
                db.execute('INSERT INTO auxiliary_sessions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (reviewer_id, config_id, None, 1, root, proof['model'], None, 'reserved',
                            uuid.uuid4().hex, None, now, now))
            execution_id = uuid.uuid4().hex
            lease = uuid.uuid4().hex
            db.execute('''INSERT INTO auxiliary_executions(id,task_id,audit_id,reviewer_id,epoch,
                session_uuid,lease,candidate_digest,contract_digest,config_binding,state,created,updated)
                VALUES(?,?,?,?,?,?,?,?,?,?,'reserved',?,?)''',
                       (execution_id, task_id, audit['id'], reviewer_id, reviewer['epoch'],
                        reviewer['session_uuid'], lease, candidate['candidate_digest'], candidate['contract_digest'],
                        config['binding'], now, now))
            for resource in json.loads(task['resources']):
                db.execute('INSERT INTO auxiliary_resources VALUES(?,?)', (resource, execution_id))
            db.execute("UPDATE auxiliary_tasks SET status='reserved',last_execution=? WHERE id=?", (execution_id, task_id))
            self.event(db, 'auxiliary_reserved', task=task_id, execution=execution_id, reviewer=reviewer_id)
            return {'execution_id': execution_id, 'reviewer_id': reviewer_id, 'epoch': reviewer['epoch'],
                    'lease': lease, 'actual_start_charged': False}

    def begin_auxiliary(self, member, session, cwd, execution_id, lease, *, executable=None):
        """公开入口仅做受信控制器启动前核验；JSON不能提供身份或隔离证明。"""
        try:
            from .pair_auxiliary_runtime import begin_execution
        except ImportError:
            from pair_auxiliary_runtime import begin_execution
        return begin_execution(self, member, session, cwd, execution_id, lease, executable=executable)

    def _begin_auxiliary_verified(self, db, execution_id, lease, identity, pool, producer):
        execution = self._auxiliary_execution(db, execution_id, lease)
        if execution['state'] != 'reserved':
            fail('Auxiliary execution changed / 仅准确预留可开始，不重放未知启动')
        task = self._auxiliary_task(db, execution['task_id'])
        candidate = self._auxiliary_candidate(db, task['candidate_id'])
        origin = db.execute('SELECT task_id FROM review_stages WHERE id=?', (candidate['stage_id'],)).fetchone()[0]
        self._auxiliary_dispatchable(db, task_id=origin)
        reviewer = dict(db.execute('SELECT * FROM auxiliary_sessions WHERE id=?', (execution['reviewer_id'],)).fetchone())
        config, source = self._auxiliary_config(db, reviewer['config_id'])
        self._auxiliary_quota(db, config, source)
        actual = reviewer_identity(identity)
        if (actual['modelProvider'] != json.loads(config['binding'])['provider']
                or actual['model'] != reviewer['model'] or actual['cwd'] != canonical_path(reviewer['cwd'])
                or execution['config_binding'] != config['binding']
                or reviewer['session_uuid'] not in (None, actual['id'])
                or db.execute('SELECT 1 FROM members WHERE session_uuid=?', (actual['id'],)).fetchone()):
            fail('Auxiliary identity mismatch / 真实会话、配置、模型或目录与预留不符')
        self._auxiliary_independent(db, candidate, task, actual['id'])
        preparation=self._option(db,'auxiliary_preparation_ready:'+execution_id)
        prepared=bool(preparation and preparation['session']==actual['id'] and preparation['epoch']==execution['epoch'])
        if task['role'] in INDEPENDENT_ROLES and db.execute('SELECT 1 FROM auxiliary_executions WHERE session_uuid=? AND charged=1 AND (?=0 OR id<>?)', (actual['id'],int(prepared),execution_id)).fetchone():
            fail('Fresh context required / 复核会话已有实质执行历史')
        if self._auxiliary_active(db) > self._auxiliary_policy(db)['concurrency']:
            fail('Auxiliary capacity reduced / 下调后先保留预留，不再新启动')
        now = time.time()
        db.execute("UPDATE auxiliary_sessions SET session_uuid=?,pool=?,state='starting',proof=?,updated=? WHERE id=? AND epoch=?",
                   (actual['id'], pool, encoded(actual), now, reviewer['id'], execution['epoch']))
        db.execute("UPDATE auxiliary_executions SET state='starting',session_uuid=?,producer=?,updated=? WHERE id=?",
                   (actual['id'], encoded(producer), now, execution_id))
        return dict(execution, session_uuid=actual['id'], state='starting')

    def _record_auxiliary_turn(self, db, execution_id, lease, turn_id):
        execution = self._auxiliary_execution(db, execution_id, lease)
        if execution['state'] not in ('starting', 'running') or not isinstance(turn_id, str) or not turn_id:
            fail('Auxiliary start mismatch / 回合必须属于准确待开始执行')
        if execution['turn_id'] and execution['turn_id'] != turn_id:
            fail('Auxiliary turn rebound / 不得给同一执行绑定另一个模型回合')
        now = time.time()
        db.execute("UPDATE auxiliary_executions SET state='running',charged=1,turn_id=?,started=COALESCE(started,?),updated=? WHERE id=?", (turn_id, now, now, execution_id))
        db.execute("UPDATE auxiliary_sessions SET state='running',updated=? WHERE id=? AND epoch=?", (now, execution['reviewer_id'], execution['epoch']))
        db.execute("UPDATE auxiliary_tasks SET status='running' WHERE id=? AND last_execution=?", (execution['task_id'], execution_id))
        self.event(db, 'auxiliary_started', execution=execution_id, turn=turn_id, budget_charged_once=True)

    def auxiliary_unknown(self, member, session, cwd, execution_id, lease, reason):
        safe_evidence(reason)
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd)
            row = self._auxiliary_execution(db, execution_id, lease)
            if row['state'] not in ACTIVE_EXECUTIONS:
                return {'recorded': False, 'state': row['state']}
            db.execute("UPDATE auxiliary_executions SET state='unknown',terminal_evidence=?,updated=? WHERE id=?", (encoded({'reason': reason}), time.time(), execution_id))
            db.execute("UPDATE auxiliary_sessions SET state='unknown' WHERE id=? AND epoch=?", (row['reviewer_id'], row['epoch']))
            db.execute("UPDATE auxiliary_tasks SET status='unknown' WHERE id=? AND last_execution=?", (row['task_id'], execution_id))
            self.event(db, 'auxiliary_unknown', execution=execution_id, reason=reason, reservation_retained=True)
            return {'recorded': True, 'replayed': False, 'reservation_retained': True}

    def cancel_auxiliary_reservation(self, member, session, cwd, execution_id, lease, reason):
        safe_evidence(reason)
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd)
            row = self._auxiliary_execution(db, execution_id, lease)
            if row['state'] == 'not_started':
                return {'released': True, 'duplicate': True}
            reviewer = db.execute('SELECT * FROM auxiliary_sessions WHERE id=?', (row['reviewer_id'],)).fetchone()
            if row['state'] != 'reserved' or row['producer'] or row['turn_id'] or reviewer['state'] != 'reserved':
                fail('Start may have occurred / 创建或执行可能开始，必须可信对账，不能手工退款')
            self._end_auxiliary(db, row, 'not_started', {'reason': reason, 'no_rpc_started': True})
            db.execute("UPDATE auxiliary_tasks SET status='pending' WHERE id=? AND last_execution=?", (row['task_id'], execution_id))
            return {'released': True, 'duplicate': False, 'charged': False}

    def reconcile_auxiliary(self, member, session, cwd, execution_id, lease, *, executable=None):
        try:
            from .pair_auxiliary_runtime import reconcile_execution
        except ImportError:
            from pair_auxiliary_runtime import reconcile_execution
        return reconcile_execution(self, member, session, cwd, execution_id, lease, executable=executable)

    def wait_auxiliary(self, member, session, cwd, task_id, child_tasks, plan):
        safe_evidence(plan)
        if not isinstance(child_tasks, list) or not child_tasks or len(set(child_tasks)) != len(child_tasks):
            fail('Child dependencies required / 等待须明确子任务且不能重复')
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd)
            task = self._auxiliary_task(db, task_id)
            execution = db.execute('SELECT * FROM auxiliary_executions WHERE id=?', (task['last_execution'],)).fetchone()
            if not execution or execution['state'] not in ('completed', 'suspended') or not execution['terminal_evidence']:
                fail('Parent execution unresolved / 父执行安全结束并留证后才能释放槽并等待子任务')
            if db.execute('SELECT 1 FROM auxiliary_resources WHERE execution_id=?', (execution['id'],)).fetchone():
                fail('Parent resource held / 不能让子任务等待父任务仍独占的资源')
            for child_id in child_tasks:
                child = self._auxiliary_task(db, child_id)
                if child['parent_task'] != task_id or child['candidate_id'] != task['candidate_id']:
                    fail('Child task mismatch / 子任务必须属于准确父任务及候选')
                pending = list(json.loads(child['dependencies']))
                seen = set()
                while pending:
                    dependency = pending.pop()
                    if dependency == task_id:
                        fail('Auxiliary dependency cycle / 父子互相等待，须A重排先后次序')
                    if dependency not in seen:
                        seen.add(dependency)
                        pending.extend(json.loads(self._auxiliary_task(db, dependency)['dependencies']))
            db.execute("UPDATE auxiliary_tasks SET status='waiting',dependencies=?,waiting=? WHERE id=?",
                       (encoded(child_tasks), encoded({'plan': plan, 'execution': execution['id']}), task_id))
            self.event(db, 'auxiliary_parent_waiting', task=task_id, children=child_tasks, budget_refunded=False)
            return {'waiting': True, 'compute_slot_released': True, 'resume_requires_new_admission': True}

    def retire_auxiliary(self, member, session, cwd, reviewer_id, epoch, reason):
        safe_evidence(reason)
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd)
            row = db.execute('SELECT * FROM auxiliary_sessions WHERE id=?', (reviewer_id,)).fetchone()
            if not row or row['epoch'] != epoch:
                fail('Stale auxiliary retirement / 只能退役准确会话代次')
            if db.execute("SELECT 1 FROM auxiliary_executions WHERE reviewer_id=? AND state IN ('reserved','starting','running','unknown')", (reviewer_id,)).fetchone():
                fail('Auxiliary execution unresolved / 活动或未知执行未结算，不能退役释放')
            duplicate = row['state'] == 'retired'
            if not duplicate:
                db.execute("UPDATE auxiliary_sessions SET state='retired',updated=? WHERE id=? AND epoch=?", (time.time(), reviewer_id, epoch))
                self.event(db, 'auxiliary_retired', reviewer=reviewer_id, epoch=epoch, reason=reason,
                           contributions_preserved=True)
            return {'retired': True, 'duplicate': duplicate, 'files_deleted': False}

    def finish_auxiliary(self, member, session, cwd, execution_id, lease, report, *, executable=None):
        if not isinstance(report, dict):
            fail('Structured auxiliary report required / 报告必须为对象')
        safe_evidence(encoded(report))
        try:
            from .pair_auxiliary_runtime import finish_execution
        except ImportError:
            from pair_auxiliary_runtime import finish_execution
        return finish_execution(self, member, session, cwd, execution_id, lease, report, executable=executable)

    def _save_auxiliary_report(self, db, execution, raw, adapted, validation, evidence):
        fingerprint = evidence.get('original_report_digest', digest(raw))
        old = db.execute('SELECT * FROM auxiliary_reports WHERE execution_id=?', (execution['id'],)).fetchone()
        if old:
            if old['report_digest'] != fingerprint:
                fail('Report already fixed / 同一执行的原报告不能被重写')
            return {'saved': True, 'duplicate': True, 'stage_passed': False}
        task = self._auxiliary_task(db, execution['task_id'])
        candidate = self._auxiliary_candidate(db, task['candidate_id'])
        if task['last_execution'] != execution['id'] or candidate['candidate_digest'] != execution['candidate_digest']:
            fail('Stale auxiliary report / 迟到报告不能覆盖新执行或候选')
        reviewer=db.execute('SELECT proof FROM auxiliary_sessions WHERE id=?',(execution['reviewer_id'],)).fetchone()
        identity=reviewer_identity(json.loads(reviewer['proof']))
        provider=identity['modelProvider']
        evidence=dict(evidence, reviewer_provider=provider, review_service='third_party' if provider=='apinebula' else 'openai_official',
                      stored_report_digest=digest(raw), adapted_report_digest=digest(adapted))
        db.execute('INSERT INTO auxiliary_reports VALUES(?,?,?,?,?,?,?)',
                   (execution['id'], fingerprint, encoded(raw), encoded(adapted), encoded(validation), encoded(evidence), time.time()))
        self._end_auxiliary(db, execution, 'completed', evidence)
        db.execute("UPDATE auxiliary_tasks SET status='completed' WHERE id=? AND last_execution=?", (task['id'], execution['id']))
        db.execute('INSERT OR IGNORE INTO auxiliary_contributions VALUES(?,?,?,?)',
                   (candidate['audit_id'], execution['session_uuid'], task['role'], task['id']))
        self.event(db, 'auxiliary_report_saved', execution=execution['id'], task=task['id'], report_digest=fingerprint, stage_passed=False)
        return {'saved': True, 'duplicate': False, 'stage_passed': False,
                'validation': validation, 'actual_start_counted': bool(execution['charged'])}

    def record_validator_run(self, member, session, cwd, execution_id, lease, run_id,
                             request, native, artifact, tool_identity):
        """保存可核验的补充证据；诊断执行器不能授予正式通过。"""
        try:
            from .pair_auxiliary_runtime import saved_validator_inputs
            from .pair_validator_worker import validator_request, verify_validator_result
        except ImportError:
            from pair_auxiliary_runtime import saved_validator_inputs
            from pair_validator_worker import validator_request, verify_validator_result
        if any(not isinstance(value, dict) for value in (request, native, artifact)):
            fail('Validator objects required')
        if not isinstance(run_id, str) or not re.fullmatch(r'[0-9a-f]{32}', run_id):
            fail('Invalid validator run ID')
        if not isinstance(tool_identity, dict) or not tool_identity:
            fail('Validator tool identity required')
        for name, value in tool_identity.items():
            if not isinstance(name, str) or not name or not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value):
                fail('Invalid validator tool identity')
        if tool_identity.get('node') != request.get('node_sha256'):
            fail('Validator Node binding mismatch')
        if len(tool_identity) > 32 or len(encoded({'request': request, 'native': native, 'artifact': artifact, 'tools': tool_identity}).encode('utf-8')) > 2097152:
            fail('Validator evidence exceeds limit')
        prepared = saved_validator_inputs(self, member, session, cwd, execution_id, lease)
        expected = validator_request(prepared, node_sha256=request['node_sha256'])
        if digest(expected) != digest(request):
            fail('Validator request changed / 请求不是当前固定报告')
        verified = verify_validator_result(request, native, artifact)
        record = {'execution_id': execution_id, 'run_id': run_id, 'request': request,
                  'native': native, 'artifact': artifact, 'tool_identity': tool_identity,
                  'verification': verified, 'formal_checks_verified': False}
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd)
            execution = self._auxiliary_execution(db, execution_id, lease)
            report = db.execute('SELECT * FROM auxiliary_reports WHERE execution_id=?', (execution_id,)).fetchone()
            if (execution['state'] != 'completed' or not report
                    or report['report_digest'] != prepared['original_report_digest']
                    or digest(json.loads(report['raw_report'])) != prepared['stored_report_digest']
                    or digest(json.loads(report['adapted_report'])) != prepared['adapted_report_digest']):
                fail('Validator source changed / 保存期间报告已变化')
            candidate = self._auxiliary_candidate(db, self._auxiliary_task(db, execution['task_id'])['candidate_id'])
            if any(execution[key] != candidate[key] for key in ('candidate_digest', 'contract_digest')):
                fail('Validator candidate changed')
            key = 'validator_run:' + run_id
            old = self._option(db, key)
            if old is not None and old != record:
                fail('Validator receipt conflict / 不得覆盖既有运行')
            if old is None:
                self._put(db, key, record)
                runs = self._option(db, 'validator_runs:' + execution_id, [])
                self._put(db, 'validator_runs:' + execution_id, runs + [run_id])
                self.event(db, 'validator_run_saved', execution=execution_id, run=run_id,
                           request_digest=digest(request), formal_checks_verified=False)
            return {'saved': True, 'duplicate': old is not None, 'run_id': run_id,
                    'validators_succeeded': verified['validators_succeeded'], 'formal_checks_verified': False}

    def settle_audit(self, member, session, cwd, candidate_id):
        """子报告不写父stage；缺本次准确正式回执时保留 needs_validation。"""
        try:
            from .pair_auxiliary_runtime import _context
        except ImportError:
            from pair_auxiliary_runtime import _context
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd)
            candidate = self._auxiliary_candidate(db, candidate_id)
            tasks = list(db.execute('SELECT * FROM auxiliary_tasks WHERE candidate_id=?', (candidate_id,)))
            reasons = []
            required = {(u['track'], u['id']) for u in json.loads(candidate['coverage_units']) if u['required']}
            covered = set()
            verdicts = set()
            records = []
            validator_runs = []
            for task in tasks:
                report = db.execute('SELECT * FROM auxiliary_reports WHERE execution_id=?', (task['last_execution'],)).fetchone()
                execution = db.execute('SELECT * FROM auxiliary_executions WHERE id=?', (task['last_execution'],)).fetchone()
                if not report or not execution or execution['state'] != 'completed':
                    reasons.append('task_unresolved:' + task['id'])
                    continue
                if any(execution[key] != candidate[key] for key in ('candidate_digest', 'contract_digest')):
                    reasons.append('execution_binding_mismatch:' + task['id'])
                    continue
                result = json.loads(report['adapted_report'])
                validation = json.loads(report['validation'])
                evidence = json.loads(report['evidence'])
                if (evidence.get('stored_report_digest') != digest(json.loads(report['raw_report']))
                        or evidence.get('adapted_report_digest') != digest(result)):
                    reasons.append('report_integrity_mismatch:' + task['id'])
                    continue
                self._auxiliary_independent(db, candidate, task, execution['session_uuid'])
                if validation.get('errors') or not validation.get('coverage_complete'):
                    reasons.append('report_validation_incomplete:' + task['id'])
                for run_id in self._option(db, 'validator_runs:' + execution['id'], []):
                    supplemental = self._option(db, 'validator_run:' + run_id)
                    if (supplemental and supplemental['execution_id'] == execution['id']
                            and digest(supplemental['request']['binding']['context']) == digest(_context(self,
                                {'execution': execution, 'task': task, 'candidate': candidate}))
                            and supplemental['request']['binding']['original_report_digest'] == report['report_digest']
                            and supplemental['request']['binding']['stored_report_digest'] == evidence['stored_report_digest']
                            and supplemental['request']['binding']['adapted_report_digest'] == evidence['adapted_report_digest']):
                        validator_runs.append({'run_id': run_id, 'execution_id': execution['id'],
                                               'formal_checks_verified': False})
                if not evidence.get('formal_checks_verified'):
                    reasons.append('formal_receipts_missing:' + task['id'])
                verdicts.add(result.get('verdict', 'needs_validation'))
                if task['role'] in ('final_verification', 'conflict_review') and not validation.get('errors'):
                    covered.update((task['track'], unit) for unit in json.loads(task['unit_ids']))
                records.append(report['report_digest'])
            if required - covered:
                reasons.append('required_coverage_or_independent_final_verification_missing')
            if self._approval_unresolved(db, grants_only=True):
                reasons.append('permission_execution_unresolved')
            if len(verdicts - {'needs_validation'}) > 1:
                reasons.append('conflicting_conclusions_require_fresh_context')
                if self._queue_conflict_reviews(db, candidate, tasks, records):
                    reasons.append('conflict_review_limit_requires_coordinator')
            if 'needs_validation' in verdicts or not verdicts:
                reasons.append('necessary_validation_missing')
            # 只检查父阶段已经保存的正式结论；不能调用含本辅助门禁的_stage_passed形成循环。
            stage = db.execute('SELECT * FROM review_stages WHERE id=?', (candidate['stage_id'],)).fetchone()
            latest = db.execute('SELECT id FROM review_stages WHERE task_id=? ORDER BY version DESC LIMIT 1', (stage['task_id'],)).fetchone()
            parent_report = json.loads(stage['evidence']) if stage['evidence'] else {}
            if (stage['status'] != 'passed' or stage['verdict'] != 'passed' or latest['id'] != stage['id']
                    or parent_report.get('verdict') != 'passed'
                    or parent_report.get('candidate_digest') != stage['digest']):
                reasons.append('parent_fixed_stage_official_review_required')
            status = 'needs_validation' if reasons else ('needs_fix' if 'needs_fix' in verdicts else 'passed')
            result = {'candidate_id': candidate_id, 'stage_id': candidate['stage_id'], 'verdict': status,
                      'reasons': sorted(set(reasons)), 'report_digests': records,
                      'ordinary_stage_changed': False, 'new_work_started': False, 'validator_runs': validator_runs}
            db.execute('UPDATE auxiliary_candidates SET status=?,result=? WHERE id=?', (status, encoded(result), candidate_id))
            self.event(db, 'auxiliary_audit_settled', candidate=candidate_id, verdict=status, reasons=result['reasons'])
            return result

    def _queue_conflict_reviews(self, db, candidate, tasks, records):
        exhausted = False
        for track in json.loads(candidate['tracks']):
            sources = [task['id'] for task in tasks if task['track'] == track
                       and task['role'] in ('final_verification', 'conflict_review') and task['status'] == 'completed']
            if not sources:
                continue
            key = 'aux-conflict-' + digest([candidate['id'], track, sorted(records)])[:32]
            units = [unit['id'] for unit in json.loads(candidate['coverage_units']) if unit['track'] == track]
            if db.execute('SELECT 1 FROM auxiliary_tasks WHERE id=?', (key,)).fetchone():
                continue
            counter = 'auxiliary_conflict_rounds:' + candidate['audit_id'] + ':' + track
            rounds = self._option(db, counter, 0)
            if rounds >= 3:
                exhausted = True
                continue
            db.execute('INSERT INTO auxiliary_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (key, candidate['id'], None, track, 'conflict_review', encoded(units),
                        encoded(sources), encoded(sources), '[]', 0, 'pending', None, None, time.time()))
            self._put(db, counter, rounds + 1)
            self.event(db, 'auxiliary_conflict_review_queued', task=key, candidate=candidate['id'],
                       fresh_context_required=True, new_work_started=False)
        return exhausted

    def auxiliary_status(self, member, session, cwd):
        with self.transaction() as db:
            self._binding(db, member, session, cwd)
            audits = []
            for row in db.execute('SELECT * FROM auxiliary_audits ORDER BY created'):
                audits.append(dict(row, **self._auxiliary_budget(db, row['id'])))
            sessions = [dict(r) for r in db.execute('SELECT id,config_id,session_uuid,epoch,model,pool,state,created,updated FROM auxiliary_sessions')]
            return {'policy': self._auxiliary_policy(db), 'active_slots': self._auxiliary_active(db),
                    'audits': audits, 'sessions': sessions,
                    'tasks': [dict(r) for r in db.execute('SELECT * FROM auxiliary_tasks ORDER BY created')],
                    'candidates': [dict(r) for r in db.execute('SELECT * FROM auxiliary_candidates ORDER BY created')],
                    'executions': [dict(r) for r in db.execute('SELECT id,task_id,audit_id,reviewer_id,epoch,session_uuid,state,charged,turn_id,started,created,updated FROM auxiliary_executions ORDER BY created')],
                    'legacy_bindings_preserved': db.execute('SELECT count(*) FROM auxiliary_legacy_bindings').fetchone()[0]}

    def _auxiliary_dispatchable(self, db, *, task_id=None):
        if not self._auxiliary_policy(db)['enabled']:
            fail('Auxiliary pool disabled / 辅助池尚未启用')
        if db.execute('SELECT status FROM collaborations').fetchone()[0] != 'running':
            fail('Collaboration not running / 暂停或停止时不开始辅助工作')
        if self._task_scope_closed(db, task_id):
            fail('Scope closed / 已收尾范围不能由辅助事件复活')
        scope = self._option(db, 'closed_scope')
        if scope and self._auxiliary_policy(db).get('scope_digest') != digest(scope):
            fail('Auxiliary scope authorization required / 新范围须由A明确重新授权辅助池')
        if self._member_suspended(db, 'A'):
            fail('Coordinator unavailable / A暂停时不增加辅助执行')
        self._require_recovery_open(db, member='A', task_id=task_id, action='auxiliary')

    def _auxiliary_config(self, db, config_id):
        row = db.execute('SELECT * FROM auxiliary_configs WHERE id=?', (config_id,)).fetchone()
        if not row or not row['enabled']:
            fail('Auxiliary config not allowed / 未登记允许的审核配置')
        source = self._member(db, row['member'])
        if row['binding'] != encoded(_config_binding(source)) or source['lifecycle'] != 'active' or not source['verified']:
            fail('Auxiliary config changed / 账号、模型或绑定变化后必须重新授权核验')
        reviewer_identity(json.loads(source['proof']))
        return row, source

    def _auxiliary_quota(self, db, config, source):
        quota = self._quotas(db).get(source['id'], {})
        if self._pool_blocked(db, source['id']):
            fail('Auxiliary pool backoff / 共享账号池仍在限流或等待可靠恢复读数')
        current = pair_balance.current(quota)
        if current.get('reliability') == 'fresh':
            if current.get('remaining', 0) <= 0:
                fail('Auxiliary quota exhausted / 实际额度耗尽不能由软保护授权绕过')
            if current.get('remaining', 0) <= self._reserve(db, source['id']):
                grant = self._option(db, 'auxiliary_quota_override:' + str(current.get('pool')), {})
                coordinator = self._member(db, 'A')
                allowed = (grant.get('enabled') and grant.get('pair') == self.pair_id
                           and grant.get('pool') == current.get('pool')
                           and grant.get('limit_id') == current.get('limit_id')
                           and grant.get('cycle') == _quota_cycle(current)
                           and grant.get('source_session') == coordinator['session_uuid']
                           and grant.get('source_binding_version') == coordinator['binding_version']
                           and grant.get('bindings', {}).get(config['id']) == digest(config['binding'])
                           and (not self._option(db, 'closed_scope') or grant.get('closed_scope_digest') == digest(self._option(db, 'closed_scope'))))
                if not allowed:
                    fail('Auxiliary quota protected / 工作窗口的忽略额度许可不自动传给辅助池')

    def _auxiliary_independent(self, db, candidate, task, context):
        if not context:
            fail('Auxiliary context unknown / 缺准确上下文身份')
        if context in json.loads(candidate['contributors']):
            fail('Independent reviewer required / 实际实现者不能审核自己的候选')
        for related in json.loads(task['independent_of']):
            if db.execute('SELECT 1 FROM auxiliary_executions WHERE task_id=? AND session_uuid=? AND charged=1', (related, context)).fetchone():
                fail('Contribution independence violated / 发现或原验证上下文不能充当其独立复核者')
        if task['role'] in INDEPENDENT_ROLES and db.execute("SELECT 1 FROM auxiliary_contributions WHERE audit_id=? AND context_id=? AND kind IN ('implementation','discovery')", (candidate['audit_id'], context)).fetchone():
            fail('Contribution independence violated / 角色或账号改名不清除实际贡献链')

    def _auxiliary_candidate(self, db, candidate_id):
        row = db.execute('SELECT * FROM auxiliary_candidates WHERE id=?', (candidate_id,)).fetchone()
        if not row:
            fail('Unknown audit candidate / 未登记审计候选')
        stage = db.execute('SELECT * FROM review_stages WHERE id=?', (row['stage_id'],)).fetchone()
        task = db.execute('SELECT requirements_version FROM tasks WHERE id=?', (stage['task_id'],)).fetchone() if stage else None
        if not stage or stage['digest'] != row['candidate_digest'] or stage['status'] == 'stale' or not task or task[0] != row['requirements_version']:
            fail('Stale audit candidate / 候选或需求已变化，旧报告不能追认新版本')
        payload = json.loads(stage['payload'])
        if row['contract_digest'] != digest({'requirements_version': stage['requirements_version'], 'acceptance': payload.get('coordinator_acceptance', []), 'checks': payload.get('coordinator_required_checks', [])}):
            fail('Audit contract changed / 检查契约变化须登记准确新候选')
        return row

    def _auxiliary_task(self, db, task_id):
        row = db.execute('SELECT * FROM auxiliary_tasks WHERE id=?', (task_id,)).fetchone()
        if not row:
            fail('Unknown auxiliary task / 未登记辅助任务')
        return row

    def _auxiliary_execution(self, db, execution_id, lease):
        row = db.execute('SELECT * FROM auxiliary_executions WHERE id=?', (execution_id,)).fetchone()
        if not row or row['lease'] != lease:
            fail('Auxiliary lease mismatch / 执行ID或领取凭据不符')
        reviewer = db.execute('SELECT * FROM auxiliary_sessions WHERE id=?', (row['reviewer_id'],)).fetchone()
        task = self._auxiliary_task(db, row['task_id'])
        if not reviewer or reviewer['epoch'] != row['epoch'] or task['last_execution'] != execution_id:
            fail('Stale auxiliary execution / 旧代次不能修改新执行或释放其他租约')
        return row

    @staticmethod
    def _auxiliary_budget(db, audit_id):
        charged = db.execute('SELECT count(*) FROM auxiliary_executions WHERE audit_id=? AND charged=1', (audit_id,)).fetchone()[0]
        reserved = db.execute("SELECT count(*) FROM auxiliary_executions WHERE audit_id=? AND charged=0 AND state IN ('reserved','starting','running','unknown')", (audit_id,)).fetchone()[0]
        return {'charged': charged, 'reserved': reserved}

    @staticmethod
    def _auxiliary_active(db):
        total = db.execute("SELECT count(*) FROM auxiliary_executions WHERE state IN ('reserved','starting','running','unknown')").fetchone()[0]
        old = db.execute('SELECT state FROM reviewer_runtime WHERE id=1').fetchone()
        return total + int(bool(old and old[0] in ('starting', 'running', 'unknown')))

    def _end_auxiliary(self, db, execution, state, evidence):
        db.execute('UPDATE auxiliary_executions SET state=?,terminal_evidence=?,updated=? WHERE id=?', (state, encoded(evidence), time.time(), execution['id']))
        db.execute('DELETE FROM auxiliary_resources WHERE execution_id=?', (execution['id'],))
        reviewer = db.execute('SELECT session_uuid FROM auxiliary_sessions WHERE id=?', (execution['reviewer_id'],)).fetchone()
        db.execute('UPDATE auxiliary_sessions SET state=?,updated=? WHERE id=? AND epoch=?',
                   ('idle' if reviewer and reviewer[0] else 'retired', time.time(), execution['reviewer_id'], execution['epoch']))
        self.event(db, 'auxiliary_execution_terminal', execution=execution['id'], state=state, budget_refunded=False)


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        fail('Invalid auxiliary identifier / 标识须为有界字母数字及_.:-')


def _config_binding(member):
    proof = reviewer_identity(json.loads(member['proof']))
    return {'member': member['id'], 'session': member['session_uuid'], 'version': member['binding_version'],
            'home': canonical_path(member['codex_home']), 'cwd': canonical_path(member['cwd']),
            'model': proof['model'], 'provider': proof['modelProvider']}


def _quota_cycle(quota):
    return sorted([[window['duration_mins'], window['resets_at']] for window in quota['windows']])
