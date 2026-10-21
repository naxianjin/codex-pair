"""隐藏审核者的持久身份与运行代次；不把进程存在或文字结论当作成果放行。"""
from __future__ import annotations

import json
import time
import uuid
import hashlib
import os
from pathlib import Path

try:
    from .pair_transport import canonical_path, session_uuid, redact_diagnostic
except ImportError:
    from pair_transport import canonical_path, session_uuid, redact_diagnostic


def fail(message):
    try:
        from .pair_core import PairError
    except ImportError:
        from pair_core import PairError
    raise PairError(message)


def official_identity(proof):
    # 此处校验经官方 API 取得的白名单；不从目录名或模型自述推断资格。
    if not isinstance(proof, dict) or proof.get('modelProvider') != 'openai':
        fail('Official GPT required / 隐藏审核者要求已核验的 OpenAI 官方 GPT')
    model = proof.get('model')
    if not isinstance(model, str) or not model.startswith('gpt-') or proof.get('ephemeral'):
        fail('Persistent GPT required / 要求可恢复的 GPT 会话')
    if not isinstance(proof.get('id'), str) or not isinstance(proof.get('cwd'), str):
        fail('Incomplete identity / 缺少经 API 核验的会话或目录')
    return {'id': session_uuid(proof['id']), 'cwd': canonical_path(proof['cwd']),
            'modelProvider': 'openai', 'model': model}


def safe_evidence(evidence):
    if (not isinstance(evidence, str) or not evidence.strip()
            or len(evidence) > 24000 or redact_diagnostic(evidence) != evidence):
        fail('Safe evidence required / 证据须非空、有界且不含可识别的认证内容')


class ReviewerMixin:
    def _reviewer_checkpoint_path(self,db):
        row=self._reviewer_row(db)
        if not row or not row['session_uuid']:fail('Reviewer identity required / 未绑定审核会话，无固定检查点')
        project=Path(db.execute('SELECT project FROM collaborations').fetchone()[0])
        name=hashlib.sha256((row['codex_home']+'|'+row['session_uuid']).encode()).hexdigest()[:24]
        return project/'.codex-pair'/'checkpoints'/('reviewer-'+name+'.md')

    def save_reviewer_checkpoint(self,reason):
        safe_evidence(reason)
        try:from .pair_core import FileLock
        except ImportError:from pair_core import FileLock
        try:from .pair_git import prepare_pair_directory
        except ImportError:from pair_git import prepare_pair_directory
        with FileLock(self.directory/'reviewer-checkpoint.lock',timeout=15),self.transaction() as db:
            runtime=self._reviewer_status(db)['reviewer'];path=self._reviewer_checkpoint_path(db)
            existing=self._option(db,'reviewer_checkpoint')
            if existing and existing.get('status')=='saved':return existing
            if path.exists():fail('Unregistered reviewer checkpoint / 文件存在但未登记，保留并显式恢复')
            project=db.execute('SELECT project FROM collaborations').fetchone()[0]
            prepare_pair_directory(project,str(path.parent),'checkpoints')
            payload={'pair':self.pair_id,'session':runtime['session_uuid'],'epoch':runtime['epoch'],
                'reason':reason,'runtime':runtime,'queue':[dict(r) for r in db.execute("SELECT id,task_id,digest,status,turn_id,execution_id FROM review_stages WHERE status<>'passed'")],
                'authorization_version':self._authorization_version(db)}
            content='# 隐藏审核会话交接\n\n```json\n'+json.dumps(payload,ensure_ascii=False,sort_keys=True,indent=2)+'\n```\n'
            temporary=path.with_suffix('.tmp')
            if temporary.exists():fail('Checkpoint temporary exists / 遗留临时文档先核实，不覆盖')
            with temporary.open('x',encoding='utf-8') as file:
                file.write(content);file.flush();os.fsync(file.fileno())
            os.replace(temporary,path)
            record={'path':str(path),'digest':hashlib.sha256(content.encode()).hexdigest(),'status':'saved','read_end':0,'epoch':runtime['epoch'],'session':runtime['session_uuid']}
            self._put(db,'reviewer_checkpoint',record)
            self._put(db,'reviewer_auto',False)
            self.event(db,'reviewer_checkpoint_saved',digest=record['digest'],epoch=runtime['epoch'])
            return record

    def read_reviewer_checkpoint(self,member,session,cwd,*,offset=0,limit=2000,recover=False):
        if offset<0 or not 1<=limit<=2000:fail('Invalid page / 审核交接分页无效')
        with self.transaction() as db:
            self._a_actor(db,member,session,cwd)
            path=self._reviewer_checkpoint_path(db)
            if not path.is_file():return {'exists':False}
            if path.resolve()!=path.absolute() or path.stat().st_size>2_000_000:fail('Invalid checkpoint file / 审核交接路径或大小异常')
            content=path.read_text(encoding='utf-8');fingerprint=hashlib.sha256(content.encode()).hexdigest()
            record=self._option(db,'reviewer_checkpoint')
            if not record or record['digest']!=fingerprint:
                if not recover:fail('Unregistered checkpoint / 用显式recover读取并核实，不重写文档')
                payload=json.loads(content.split('```json\n',1)[1].rsplit('\n```',1)[0])
                runtime=self._reviewer_row(db)
                if payload['pair']!=self.pair_id or payload['session']!=runtime['session_uuid'] or payload['epoch']!=runtime['epoch']:fail('Checkpoint identity changed / 文档不属于当前审核绑定')
                record={'path':str(path),'digest':fingerprint,'status':'saved','read_end':0,'epoch':runtime['epoch'],'session':runtime['session_uuid']}
            if offset>len(content) or offset>record['read_end']:fail('Read gap / 必须顺序完整读取审核交接')
            end=min(len(content),offset+limit);record['read_end']=max(record['read_end'],end)
            self._put(db,'reviewer_checkpoint',record)
            return {'exists':True,'text':content[offset:end],'digest':fingerprint,'next_offset':end if end<len(content) else None}

    def consume_reviewer_checkpoint(self,member,session,cwd,fingerprint,plan):
        safe_evidence(plan)
        with self.transaction() as db:
            self._a_actor(db,member,session,cwd)
            record=self._option(db,'reviewer_checkpoint');runtime=self._reviewer_row(db)
            if not record or record['digest']!=fingerprint or record['status']!='saved':fail('Checkpoint mismatch / 审核交接版本不符')
            if runtime['state'] not in ('idle','retired') or runtime['owner']:fail('Reviewer execution unresolved / 先准确核实旧回合已结束，不转交unknown')
            path=Path(record['path']);content=path.read_text(encoding='utf-8')
            if hashlib.sha256(content.encode()).hexdigest()!=fingerprint or record['read_end']<len(content):fail('Checkpoint unread or changed / 审核交接未读完或已变化')
            record.update(status='consumed',plan=plan)
            self._put(db,'reviewer_checkpoint',record)
            payload=json.loads(content.split('```json\n',1)[1].rsplit('\n```',1)[0])
            self._put(db,'reviewer_recovery_context',{'snapshot':payload,'plan':plan,'digest':fingerprint})
            self.event(db,'reviewer_checkpoint_consumed',digest=fingerprint,recovery_plan=plan)
        # 计划先持久化；清理失败保留已消费记录，不重复接管。
        try:path.unlink();cleanup='removed'
        except OSError:cleanup='retained'
        return {'consumed':True,'cleanup':cleanup,'automatic_replay':False}

    def _reviewer_quota_paused(self):
        with self.transaction() as db:
            return self._member_suspended(db,'A') or self._pool_blocked(db,'A')

    def _upgrade_reviewer_execution(self, db):
        with db:
            db.execute('BEGIN IMMEDIATE')
            columns = {r[1] for r in db.execute('PRAGMA table_info(reviewer_runtime)')}
            if 'execution_id' not in columns:
                db.execute('ALTER TABLE reviewer_runtime ADD COLUMN execution_id TEXT')
            # 旧租约仅补对账标识，不解除占用，也不推断旧执行已经结束。
            db.execute("UPDATE reviewer_runtime SET execution_id=? WHERE execution_id IS NULL AND state IN ('running','unknown') AND session_uuid IS NOT NULL", (uuid.uuid4().hex,))
            db.execute('PRAGMA user_version=11')

    def _upgrade_reviewer(self, db):
        with db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE reviewer_runtime(
                id INTEGER PRIMARY KEY CHECK(id=1), epoch INTEGER NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('starting','idle','running','unknown','retired')),
                attempt TEXT NOT NULL UNIQUE, session_uuid TEXT UNIQUE,
                codex_home TEXT NOT NULL, cwd TEXT NOT NULL, model TEXT NOT NULL,
                parent_session TEXT NOT NULL, owner TEXT, execution_id TEXT, turn_id TEXT,
                last_progress REAL NOT NULL, reminded INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL, updated REAL NOT NULL)''')
            db.execute('''CREATE TABLE reviewer_history(
                epoch INTEGER PRIMARY KEY, snapshot TEXT NOT NULL, reason TEXT NOT NULL,
                retired REAL NOT NULL)''')
            db.execute('PRAGMA user_version=10')

    def _reviewer_parent(self, db, member, session, cwd):
        parent = self._binding(db, member, session, cwd)
        if member != 'A':
            fail('Original A required / 仅原 A 可管理隐藏审核者')
        proof = json.loads(parent['proof'])
        identity = official_identity(proof)
        if identity['id'] != parent['session_uuid']:
            fail('Parent identity mismatch / A 的实际身份与绑定不符')
        return parent, identity

    @staticmethod
    def _reviewer_row(db):
        row = db.execute('SELECT * FROM reviewer_runtime WHERE id=1').fetchone()
        return dict(row) if row else None

    def reviewer_status(self):
        with self.transaction() as db:
            return self._reviewer_status(db)

    def _reviewer_status(self, db):
        row = self._reviewer_row(db)
        if row:
            # 运行令牌只交给持有者，不在普通状态查询中暴露。
            row.pop('owner', None)
            row.pop('attempt', None)
        return {'reviewer': row, 'visible_seat': False,
                'approval_authority': False, 'release_gate_implemented': self._option(db,'review_required',False),
                'checkpoint':self._option(db,'reviewer_checkpoint')}

    def reserve_reviewer(self, member, session, cwd, *, replacement=False, evidence=''):
        """RPC 前先保留唯一创建意图；未知创建不能换 ID 再建会话。"""
        if replacement:
            safe_evidence(evidence)
        with self.transaction() as db:
            parent, proof = self._reviewer_parent(db, member, session, cwd)
            if db.execute('SELECT status FROM collaborations').fetchone()[0] != 'running':
                fail('Collaboration not running / 停止或暂停后不创建审核会话')
            if self._option(db, 'goal_completed', False):
                fail('Goal completed / 目标完成后不创建审核会话')
            old = self._reviewer_row(db)
            if old:
                if not replacement:
                    fail('Reviewer already reserved / 已有审核会话或创建意图，请先核实，不重复创建')
                if old['state'] != 'retired' or not evidence.strip():
                    fail('Retirement evidence required / 先核实旧执行结束并撤销旧身份，再创建替代者')
            epoch = old['epoch'] + 1 if old else 1
            if old:
                db.execute('DELETE FROM reviewer_runtime WHERE id=1')
            now = time.time()
            attempt = uuid.uuid4().hex
            db.execute('''INSERT INTO reviewer_runtime(
                id,epoch,state,attempt,codex_home,cwd,model,parent_session,last_progress,created,updated)
                VALUES(1,?,'starting',?,?,?,?,?,?,?,?)''',
                (epoch, attempt, parent['codex_home'], canonical_path(cwd), proof['model'],
                 parent['session_uuid'], now, now, now))
            self.event(db, 'reviewer_reserved', epoch=epoch, replacement=replacement)
            return {'epoch': epoch, 'attempt': attempt, 'codex_home': parent['codex_home'],
                    'model': proof['model'], 'modelProvider': 'openai'}

    def bind_reviewer(self, member, session, cwd, *, epoch, attempt, proof, reviewer_cwd):
        actual = official_identity(proof)
        with self.transaction() as db:
            parent, expected = self._reviewer_parent(db, member, session, cwd)
            row = self._reviewer_row(db)
            if (not row or row['epoch'] != epoch or row['attempt'] != attempt
                    or row['state'] != 'starting'):
                fail('Stale reviewer creation / 审核者创建回执已失效或重复')
            if (db.execute('SELECT status FROM collaborations').fetchone()[0] != 'running'
                    or self._option(db, 'goal_completed', False)):
                fail('Collaboration not running / 保留迟到创建证据，不激活审核者')
            if (actual['id'] == parent['session_uuid'] or actual['model'] != row['model']
                    or expected['model'] != row['model'] or parent['session_uuid'] != row['parent_session']
                    or canonical_path(parent['codex_home']) != canonical_path(row['codex_home'])
                    or actual['cwd'] != canonical_path(reviewer_cwd)):
                fail('Reviewer identity mismatch / 要求独立会话、原 A 配置与准确审核工作树')
            if db.execute('SELECT 1 FROM members WHERE session_uuid=?', (actual['id'],)).fetchone():
                fail('Visible session cannot review / 不能把可见成员冒充隐藏审核者')
            now = time.time()
            db.execute("UPDATE reviewer_runtime SET state='idle',session_uuid=?,cwd=?,last_progress=?,updated=? WHERE id=1",
                       (actual['id'], actual['cwd'], now, now))
            self.event(db, 'reviewer_bound', epoch=epoch, session=actual['id'])
            return {'bound': True, 'epoch': epoch, 'session_uuid': actual['id']}

    def reviewer_creation_unknown(self, epoch, attempt):
        with self.transaction() as db:
            changed = db.execute("UPDATE reviewer_runtime SET state='unknown',updated=? WHERE id=1 AND epoch=? AND attempt=? AND state='starting'",
                                 (time.time(), epoch, attempt)).rowcount
            if changed:
                self.event(db, 'reviewer_creation_unknown', epoch=epoch)
            return {'recorded': bool(changed), 'automatic_retry': False}

    def _check_reviewer(self, db, epoch, session, proof):
        row = self._reviewer_row(db)
        actual = official_identity(proof)
        if (not row or row['epoch'] != epoch or row['session_uuid'] != session_uuid(session)
                or row['state'] in ('starting', 'unknown', 'retired')
                or actual['id'] != row['session_uuid'] or actual['cwd'] != row['cwd']
                or actual['model'] != row['model']):
            fail('Stale reviewer identity / 审核身份、配置或运行代次已失效')
        if db.execute('SELECT 1 FROM members WHERE session_uuid=?', (row['session_uuid'],)).fetchone():
            fail('Hidden reviewer cannot join visible members / 审核者不再具有独立身份')
        parent = self._member(db, 'A')
        current = official_identity(json.loads(parent['proof']))
        if (current['model'] != row['model'] or parent['session_uuid'] != row['parent_session']
                or canonical_path(parent['codex_home']) != canonical_path(row['codex_home'])):
            fail('Parent configuration changed / A 配置变化后必须重新核验审核者')
        return row

    def acquire_reviewer(self, epoch, session, proof):
        with self.transaction() as db:
            row = self._check_reviewer(db, epoch, session, proof)
            if db.execute('SELECT status FROM collaborations').fetchone()[0] != 'running':
                fail('Collaboration not running / 不开始新审核')
            if self._option(db, 'goal_completed', False):
                fail('Goal completed / 目标完成后不开始新审核')
            if row['owner'] or row['state'] != 'idle':
                fail('Reviewer lease held / 已有审核在途，不按时间抢占')
            owner = uuid.uuid4().hex
            execution_id = uuid.uuid4().hex
            now = time.time()
            db.execute("UPDATE reviewer_runtime SET state='running',owner=?,execution_id=?,turn_id=NULL,last_progress=?,reminded=0,updated=? WHERE id=1", (owner,execution_id,now,now))
            return {'owner': owner, 'epoch': epoch, 'execution_id': execution_id}

    def reconcile_reviewer_execution(self, member, session, cwd, *, epoch,
                                     reviewer_session, execution_id, outcome, evidence):
        """原 A 显式核实准确执行；撤销遗失 owner，不重放、不登记业务通过。"""
        safe_evidence(evidence)
        if outcome not in ('not_started', 'completed', 'failed', 'cancelled'):
            fail('Terminal evidence required / 未知或仅超时不能解除审核执行占用')
        with self.transaction() as db:
            self._reviewer_parent(db, member, session, cwd)
            row = self._reviewer_row(db)
            if (not row or row['epoch'] != epoch or row['session_uuid'] != session_uuid(reviewer_session)
                    or not execution_id or row['execution_id'] != execution_id
                    or row['state'] not in ('running','unknown')):
                fail('Reviewer execution mismatch / 对账必须匹配当前会话、代次和准确执行ID')
            self.event(db, 'reviewer_execution_reconciled', epoch=epoch,
                       execution_id=execution_id, session=reviewer_session, outcome=outcome,
                       evidence=evidence, owner_revoked=True)
            db.execute("UPDATE reviewer_runtime SET state='idle',owner=NULL,updated=? WHERE id=1", (time.time(),))
            return {'state': 'idle', 'replayed': False, 'work_approved': False}

    def release_reviewer(self, epoch, session, owner, *, outcome, evidence):
        safe_evidence(evidence)
        if outcome not in ('completed', 'failed', 'unknown') or not evidence.strip():
            fail('Execution evidence required / 审核工具结果必须明确且有证据')
        with self.transaction() as db:
            row = self._reviewer_row(db)
            if (not row or row['epoch'] != epoch or row['session_uuid'] != session_uuid(session)
                    or row['owner'] != owner or row['state'] != 'running'):
                fail('Reviewer lease mismatch / 旧进程不能释放新审核者的运行权')
            state = 'unknown' if outcome == 'unknown' else 'idle'
            db.execute('UPDATE reviewer_runtime SET state=?,owner=NULL,updated=? WHERE id=1', (state,time.time()))
            self.event(db, 'reviewer_execution_ended', epoch=epoch, outcome=outcome, evidence=evidence)
            return {'state': state, 'work_approved': False}

    def retire_reviewer(self, member, session, cwd, *, epoch, execution_resolved, evidence):
        safe_evidence(evidence)
        if execution_resolved is not True or not evidence.strip():
            fail('Reconciliation required / 先核实旧执行和证据，不能按无响应时间替换')
        with self.transaction() as db:
            self._reviewer_parent(db, member, session, cwd)
            row = self._reviewer_row(db)
            if not row or row['epoch'] != epoch or row['owner'] or row['state'] in ('starting','running'):
                fail('Reviewer execution unresolved / 旧创建或执行尚未结束，不替换')
            if row['state'] == 'retired':
                return {'retired': True, 'duplicate': True}
            db.execute('INSERT INTO reviewer_history VALUES(?,?,?,?)',
                       (epoch,json.dumps(row,sort_keys=True),evidence,time.time()))
            db.execute("UPDATE reviewer_runtime SET state='retired',updated=? WHERE id=1", (time.time(),))
            self.event(db,'reviewer_retired',epoch=epoch)
            return {'retired': True, 'epoch': epoch}

    def reviewer_progress(self, epoch, session, owner, evidence):
        safe_evidence(evidence)
        if not evidence.strip():
            fail('Progress evidence required / 不以心跳代替有效进展')
        with self.transaction() as db:
            count = db.execute("UPDATE reviewer_runtime SET last_progress=?,reminded=0,updated=? WHERE id=1 AND epoch=? AND session_uuid=? AND owner=? AND state='running'",
                               (time.time(),time.time(),epoch,session_uuid(session),owner)).rowcount
            if not count:
                fail('Reviewer lease mismatch / 进展不属于当前审核实例')
            self.event(db,'reviewer_progress',epoch=epoch,evidence=evidence)

    def reviewer_reminder(self, *, now=None):
        now = time.time() if now is None else now
        with self.transaction() as db:
            row = self._reviewer_row(db)
            if not row or row['state'] != 'running' or row['reminded'] or now-row['last_progress'] < 600:
                return {'notify': False}
            db.execute('UPDATE reviewer_runtime SET reminded=1 WHERE id=1')
            self.event(db,'reviewer_no_progress',epoch=row['epoch'],notify_user=True)
            self._send(db,'A','A','隐藏审核连续十分钟没有有效进展。只核实原回合、工具和证据，不按时间杀进程、判失败或重放。epoch='+str(row['epoch']),
                kind='blocked',dedupe_key='reviewer-stalled:'+str(row['epoch'])+':'+str(row['execution_id'])+':'+str(row['last_progress']))
            return {'notify': True, 'epoch': row['epoch'], 'action': 'investigate_only'}
