"""可见成员注册、邀请与绑定代次；旧会话和未确认执行不能自动换绑。"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

try:
    from .pair_transport import canonical_path, session_uuid, binding_proof
    from .pair_reviewer import fail, safe_evidence
except ImportError:
    from pair_transport import canonical_path, session_uuid, binding_proof
    from pair_reviewer import fail, safe_evidence

VISIBLE = ('A', 'B', 'C', 'D')


class TeamMixin:
    def _upgrade_team(self, db):
        db.execute('PRAGMA foreign_keys=OFF')
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('''CREATE TABLE members_team(id TEXT PRIMARY KEY CHECK(id IN ('A','B','C','D')),
                    session_uuid TEXT NOT NULL UNIQUE,cwd TEXT NOT NULL,backend TEXT NOT NULL,
                    owned INTEGER NOT NULL,proof TEXT NOT NULL,joined REAL NOT NULL,codex_home TEXT,
                    delivery_mode TEXT NOT NULL DEFAULT 'auto',binding_version INTEGER NOT NULL DEFAULT 1,
                    lifecycle TEXT NOT NULL DEFAULT 'active',online INTEGER NOT NULL DEFAULT 1,
                    verified INTEGER NOT NULL DEFAULT 0,capabilities TEXT NOT NULL DEFAULT '[]')''')
                db.execute('''INSERT INTO members_team(id,session_uuid,cwd,backend,owned,proof,joined,codex_home,delivery_mode,verified)
                    SELECT id,session_uuid,cwd,backend,owned,proof,joined,codex_home,delivery_mode,1 FROM members''')
                db.execute('DROP TABLE members')
                db.execute('ALTER TABLE members_team RENAME TO members')
                db.execute('''CREATE TABLE member_history(member TEXT NOT NULL,version INTEGER NOT NULL,
                    snapshot TEXT NOT NULL,evidence TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(member,version))''')
                db.execute('''CREATE TABLE invitations(id TEXT PRIMARY KEY,nonce_hash TEXT NOT NULL,
                    target TEXT,replacement_version INTEGER,requirements_version INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'open',accepted_session TEXT,accepted_member TEXT,
                    created REAL NOT NULL,expires REAL NOT NULL)''')
                db.execute('ALTER TABLE messages ADD COLUMN recipient_version INTEGER NOT NULL DEFAULT 1')
                db.execute('ALTER TABLE messages ADD COLUMN sender_version INTEGER NOT NULL DEFAULT 1')
                db.execute("ALTER TABLE tasks ADD COLUMN task_kind TEXT NOT NULL DEFAULT 'implementation'")
                db.execute('ALTER TABLE tasks ADD COLUMN requirements_version INTEGER NOT NULL DEFAULT 1')
                db.execute('ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 0')
                db.execute("ALTER TABLE tasks ADD COLUMN capabilities TEXT NOT NULL DEFAULT '[]'")
                db.execute("ALTER TABLE tasks ADD COLUMN resources TEXT NOT NULL DEFAULT '[]'")
                db.execute('ALTER TABLE tasks ADD COLUMN review_required INTEGER NOT NULL DEFAULT 0')
                db.execute("ALTER TABLE tasks ADD COLUMN acceptance TEXT NOT NULL DEFAULT '[]'")
                db.execute("ALTER TABLE tasks ADD COLUMN required_checks TEXT NOT NULL DEFAULT '[]'")
                db.execute('''CREATE TABLE requirement_changes(version INTEGER PRIMARY KEY,body TEXT NOT NULL,
                    affected TEXT NOT NULL,created REAL NOT NULL)''')
                db.execute('''CREATE TABLE resource_locks(resource TEXT PRIMARY KEY,task_id TEXT NOT NULL,
                    owner TEXT NOT NULL,created REAL NOT NULL)''')
                if db.execute('PRAGMA foreign_key_check').fetchall(): fail('Team migration foreign key failure / 成员迁移外键校验失败')
                # 老协作不改变其审批、额度和审核策略，新轮次由 start 显式设置。
                db.execute('PRAGMA user_version=12')
        finally:
            db.execute('PRAGMA foreign_keys=ON')

    def visible_members(self, *, active=True):
        with self.transaction() as db:
            return [dict(r) for r in db.execute("SELECT * FROM members WHERE (?=0 OR lifecycle='active') ORDER BY id", (int(active),))]

    def _team_ready(self, db):
        return db.execute("SELECT count(*) FROM members WHERE lifecycle IN ('active','draining') AND verified=1").fetchone()[0] >= 2

    def _member_eligible(self, db, member, task=None):
        row=self._member(db,member)
        if self._role_handoff_pending(db,member): return False
        if row['lifecycle']!='active' or not row['online'] or not row['verified']: return False
        if task and not set(json.loads(task['capabilities'])).issubset(json.loads(row['capabilities'])): return False
        if task and not self._task_member_allowed(db,task,member): return False
        return True

    def _task_member_allowed(self,db,task,member,*,takeover=()):
        policy=self._option(db,'task_execution_policy:'+task['id'],{})
        pinned=policy.get('pinned')
        if pinned and (pinned['member']!=member or pinned['binding_version']!=self._member(db,member)['binding_version']):return False
        for key in policy.get('independent_of',[]):
            source=db.execute('SELECT assignee FROM tasks WHERE id=?',(key,)).fetchone()
            if not source or source['assignee']==member or key in takeover:return False
        return True

    def _set_task_execution_policy(self,db,task_id,assignee,*,pin_member=False,independent_of=()):
        if not isinstance(pin_member,bool) or not isinstance(independent_of,(list,tuple)) or any(not isinstance(k,str) for k in independent_of):
            fail('Invalid execution policy / 执行约束须使用布尔值及任务ID列表')
        for key in independent_of:
            if key==task_id or not db.execute('SELECT 1 FROM tasks WHERE id=?',(key,)).fetchone():
                fail('Invalid independent task / 独立核验须引用其他已登记任务')
        policy={'independent_of':list(dict.fromkeys(independent_of))}
        if pin_member:policy['pinned']={'member':assignee,'binding_version':self._member(db,assignee)['binding_version']}
        self._put(db,'task_execution_policy:'+task_id,policy)

    def _message_current(self, db, message):
        for field in ('sender','recipient'):
            member=self._member(db,message[field])
            if member['lifecycle'] not in ('active','suspended','draining') or member['binding_version']!=message[field+'_version']: return False
        return True

    def _a_actor(self,db,member,session,cwd):
        row=self._binding(db,member,session,cwd)
        if member!=self._option(db,'approval_authority','A'): fail('A required / 仅经用户核验的本轮审批统筹可执行')
        return row

    def _registry(self, home):
        """按实际配置目录登记全项目占用；不扫描其他会话或凭据。"""
        root=Path(home)/'codex-pair'
        root.mkdir(parents=True,exist_ok=True)
        path=root/'session-bindings.sqlite3'
        if path.is_symlink(): fail('Redirected registry / 会话注册表不能是链接')
        db=sqlite3.connect(path,timeout=15,isolation_level=None)
        db.execute('PRAGMA busy_timeout=15000')
        db.execute('CREATE TABLE IF NOT EXISTS bindings(session TEXT PRIMARY KEY,state_path TEXT NOT NULL,member TEXT NOT NULL,created REAL NOT NULL)')
        return db

    def _reserve_session(self,home,session,member):
        with closing(self._registry(home)) as db:
            db.execute('BEGIN IMMEDIATE')
            try:
                old=db.execute('SELECT state_path,member FROM bindings WHERE session=?',(session,)).fetchone()
                if old and old!=(str(self.path),member):
                    path=Path(old[0]);safe=False
                    if path.is_file() and not path.is_symlink():
                        with closing(sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True,timeout=5)) as source:
                            stopped=source.execute('SELECT status FROM collaborations').fetchone()[0]=='stopped'
                            done=source.execute("SELECT 1 FROM pair_settings WHERE key='goal_completed' AND value='true'").fetchone()
                            unresolved=source.execute("SELECT 1 FROM messages WHERE delivery_status IN ('delivering','unknown') OR (delivery_status='accepted' AND (remote_status IS NULL OR remote_status IN ('queued','inProgress'))) LIMIT 1").fetchone()
                            tasks=source.execute("SELECT 1 FROM tasks WHERE status NOT IN ('completed','integrated','cancelled','handed_back') LIMIT 1").fetchone()
                            safe=bool((stopped or done) and not unresolved and not tasks)
                    if not safe:fail('Session in another collaboration / 同一会话已参加其他协作，先核实旧执行并显式收尾')
                    db.execute('DELETE FROM bindings WHERE session=?',(session,))
                db.execute('INSERT OR IGNORE INTO bindings VALUES(?,?,?,?)',(session,str(self.path),member,time.time()))
                db.commit()
            except BaseException: db.rollback();raise

    def _release_session(self,home,session,member):
        with closing(self._registry(home)) as db:
            db.execute('DELETE FROM bindings WHERE session=? AND state_path=? AND member=?',(session,str(self.path),member))

    def create_invite(self,member,session,cwd,*,target=None,replacement=False,evidence='',ttl=86400):
        if target is not None and target not in VISIBLE[1:]: fail('Invalid seat / 邀请仅用于 B/C/D')
        if not 60<=ttl<=604800: fail('Invalid invitation lifetime / 邀请有效期须在60秒到7天内')
        with self.transaction() as db:
            self._a_actor(db,member,session,cwd)
            self._running(db)
            if self._option(db,'goal_completed',False): fail('Goal completed / 已收尾目标不能邀请')
            version=None
            if replacement:
                safe_evidence(evidence)
                old=self._member(db,target)
                if old['lifecycle']!='retired': fail('Retire old binding first / 先核实旧执行、交付和脏改动再显式退出')
                version=old['binding_version']
            elif target and db.execute("SELECT 1 FROM members WHERE id=?",(target,)).fetchone():
                fail('Seat occupied / 已存在席位须使用显式替换邀请')
            nonce=secrets.token_urlsafe(32);key=uuid.uuid4().hex
            requirement=self._option(db,'requirements_version',1)
            db.execute('INSERT INTO invitations(id,nonce_hash,target,replacement_version,requirements_version,created,expires) VALUES(?,?,?,?,?,?,?)',
                (key,hashlib.sha256(nonce.encode()).hexdigest(),target,version,requirement,time.time(),time.time()+ttl))
            self.event(db,'invitation_created',invitation=key,target=target,replacement=version)
            inviter=self._member(db,member)
            payload={'protocol':'codex-pair/windows/2','pair':self.pair_id,'state_root':str(self.state_root),
                'project':db.execute('SELECT project FROM collaborations').fetchone()[0],
                'invitation':key,'nonce':nonce,'target':target,'requirements_version':requirement,
                'inviter_session':inviter['session_uuid'],'inviter_version':inviter['binding_version']}
            self._put(db,'invitation_digest:'+key,hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest())
            return payload

    def _validate_invite(self,db,payload):
        token=hashlib.sha256(str(payload.get('nonce','')).encode()).hexdigest()
        row=db.execute('SELECT * FROM invitations WHERE id=?',(payload.get('invitation'),)).fetchone()
        digest=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest()
        if (not row or not secrets.compare_digest(row['nonce_hash'],token)
                or self._option(db,'invitation_digest:'+row['id'])!=digest):
            fail('Invitation proof mismatch / 邀请内容与发起记录不符')
        if db.execute('SELECT status FROM collaborations').fetchone()[0]=='stopped':
            fail('Invitation stopped / 原协作已停止')
        self._running(db)
        if self._option(db,'goal_completed',False):fail('Goal completed / 已收尾目标不能接受邀请')
        if row['state'] not in ('open','accepted') or row['expires']<=time.time():fail('Invitation expired / 邀请已失效')
        if row['requirements_version']!=self._option(db,'requirements_version',1):fail('Requirements changed / 获取新需求对应邀请')
        inviter=self._member(db,'A')
        if (inviter['lifecycle']!='active' or inviter['session_uuid']!=payload.get('inviter_session')
                or inviter['binding_version']!=payload.get('inviter_version')):fail('Inviter mismatch / 发起窗口绑定已变化')
        return row

    def accept_invite(self,payload,window,workspace_proof,*,mode='auto'):
        sid=session_uuid(window['session_uuid']);home=canonical_path(window['codex_home'])
        with self.transaction() as db:
            row=self._validate_invite(db,payload)
            if sid==self._member(db,'A')['session_uuid']:fail('Second window required / 请使用另一个原窗口')
            if row['state']=='accepted':
                bound=self._member(db,row['accepted_member'])
                proof=binding_proof(dict(window['proof'],git=workspace_proof),require_provider=True)
                if (row['accepted_session']==sid and bound['session_uuid']==sid and bound['lifecycle']=='active'
                        and bound['codex_home']==home and bound['cwd']==canonical_path(window['cwd'])
                        and json.loads(bound['proof'])==proof):return bound
                fail('Invitation consumed / 邀请只能接受一次，旧绑定已失效')
            occupied={r[0] for r in db.execute("SELECT id FROM members WHERE lifecycle<>'retired'")}
            seat=row['target'] or next((m for m in VISIBLE[1:] if m not in occupied and not db.execute('SELECT 1 FROM members WHERE id=?',(m,)).fetchone()),None)
            if not seat or seat in occupied: fail('Four visible members maximum / 最多四个可见成员，席位已满')
            if db.execute("SELECT 1 FROM reviewer_runtime WHERE session_uuid=? AND state<>'retired'",(sid,)).fetchone(): fail('Reviewer cannot join / 隐藏审核者不占可见席位')
            proof=binding_proof(dict(window['proof'],git=workspace_proof),require_provider=True)
            if proof['id']!=sid or proof['cwd']!=canonical_path(window['cwd']): fail('Binding mismatch / 原窗口身份不符')
            old=db.execute('SELECT * FROM members WHERE id=?',(seat,)).fetchone()
            if old and (row['replacement_version']!=old['binding_version'] or old['lifecycle']!='retired'): fail('Replacement generation changed / 替换绑定代次已变化')
            self._reserve_session(home,sid,seat)
            try:
                if old:
                    db.execute("UPDATE members SET session_uuid=?,cwd=?,backend='queue',owned=0,proof=?,codex_home=?,delivery_mode=?,binding_version=binding_version+1,lifecycle='active',online=1,verified=0,joined=? WHERE id=?",
                        (sid,proof['cwd'],json.dumps(proof),home,mode,time.time(),seat))
                else:
                    db.execute("INSERT INTO members(id,session_uuid,cwd,backend,owned,proof,joined,codex_home,delivery_mode) VALUES(?,?,?,'queue',0,?,?,?,?)",
                        (seat,sid,proof['cwd'],json.dumps(proof),time.time(),home,mode))
                db.execute("UPDATE invitations SET state='accepted',accepted_session=?,accepted_member=? WHERE id=?",(sid,seat,row['id']))
                self.event(db,'invitation_accepted',member=seat,session=sid,invitation=row['id'])
                return self._member(db,seat)
            except BaseException:
                self._release_session(home,sid,seat)
                raise

    def confirm_member(self,member,session,cwd,*,proof,context_digest,capabilities=()):
        safe_evidence(context_digest)
        if not isinstance(capabilities,(list,tuple)) or any(not isinstance(c,str) or not c or len(c)>128 for c in capabilities): fail('Invalid capabilities / 能力标签须是有界字符串列表')
        actual=binding_proof(proof,require_provider=True)
        with self.transaction() as db:
            row=self._binding(db,member,session,cwd)
            content=self._member_context(db,member)
            fingerprint=hashlib.sha256(content.encode()).hexdigest()
            if context_digest!=fingerprint:fail('Context changed / 确认摘要必须对应当前需求、授权、基线和工作树上下文')
            covered=0
            for start,end in db.execute('SELECT start,end FROM member_context_reads WHERE member=? AND binding_version=? AND digest=? ORDER BY start,end',(member,row['binding_version'],fingerprint)):
                if start>covered:break
                covered=max(covered,end)
            if covered<len(content):fail('Context not read / 先完整分页读取成员上下文')
            old=json.loads(row['proof'])
            if any(actual.get(k)!=old.get(k) for k in ('id','cwd','modelProvider','model','accountFingerprint')): fail('Configuration changed / 配置变化先登记暂停和重新核验')
            if member!='A':
                answered=db.execute("""SELECT 1 FROM messages q JOIN messages r ON r.reply_to=q.id
                    JOIN members a ON a.id='A'
                    WHERE q.sender=? AND q.sender_version=? AND r.recipient_version=?
                    AND q.recipient='A' AND r.sender='A' AND r.kind='reply' AND q.kind='question'
                    AND q.dedupe_key=?
                    AND q.recipient_version=a.binding_version AND r.sender_version=a.binding_version
                    AND q.process_status='completed' AND r.process_status='completed'
                    AND (q.remote_status='completed' OR (q.remote_status='interrupted'
                        AND json_extract(q.remote_result,'$.completion_source')='rollout_superseded'
                        AND json_extract(q.remote_result,'$.evidence_turn_id') IS NOT NULL))
                    AND (r.remote_status='completed' OR (r.remote_status='interrupted'
                        AND json_extract(r.remote_result,'$.completion_source')='rollout_superseded'
                        AND json_extract(r.remote_result,'$.evidence_turn_id') IS NOT NULL))""",(member,row['binding_version'],row['binding_version'],'window-handshake:'+member+':'+str(row['binding_version']))).fetchone()
                if not answered: fail('Communication unverified / 先完成新成员与 A 的关联通信验证')
            db.execute("UPDATE members SET verified=1,capabilities=?,online=1 WHERE id=?",(json.dumps(list(capabilities)),member))
            self.event(db,'member_verified',member=member,binding_version=row['binding_version'],context_digest=context_digest)
            if member!='A' and row['delivery_mode']=='auto' and self.window_verified(db):
                ready_key='window-ready' if member=='B' and row['binding_version']==1 else 'member-ready:'+member+':'+str(row['binding_version'])
                self._send(db,member,'A','原窗口通信与成员上下文已确认，请核对本轮目标并继续分工。',kind='note',dedupe_key=ready_key)
            return {'member':member,'verified':True,'binding_version':row['binding_version']}

    def _member_context_tick(self,db):
        if not self._option(db,'member_context_required',False) or self._option(db,'goal_completed',False):return
        if db.execute('SELECT status FROM collaborations').fetchone()[0]!='running':return
        rows=db.execute("""SELECT q.*,r.recipient_version AS reply_version,r.sender_version AS reply_sender,
            r.remote_status AS reply_remote,r.remote_result AS reply_result
            FROM messages q JOIN messages r ON r.reply_to=q.id AND r.kind='reply'
            WHERE q.kind='question' AND q.recipient='A' AND r.sender='A'
            AND q.process_status='completed' AND r.process_status='completed'""").fetchall()
        def terminal(status,result):
            if status=='completed':return True
            evidence=json.loads(result or '{}')
            return status=='interrupted' and evidence.get('completion_source')=='rollout_superseded' and bool(evidence.get('evidence_turn_id'))
        for row in rows:
            if row['dedupe_key']!='window-handshake:'+row['sender']+':'+str(row['sender_version']):continue
            peer=self._member(db,row['sender']);a=self._member(db,'A')
            if (peer['binding_version']!=row['sender_version'] or peer['binding_version']!=row['reply_version']
                    or a['binding_version']!=row['recipient_version'] or a['binding_version']!=row['reply_sender']):continue
            if not terminal(row['remote_status'],row['remote_result']) or not terminal(row['reply_remote'],row['reply_result']):continue
            for member in (a,peer):
                if member['lifecycle']!='active' or not member['online'] or member['verified']:continue
                fingerprint=hashlib.sha256(self._member_context(db,member['id']).encode()).hexdigest()
                self._send(db,'A',member['id'],
                    '握手业务回执与双方模型回合已核实结束。请领取本通知，完整分页运行read-member-context，核对目标、授权、基准、工作树及实际能力；再使用返回digest运行confirm-member，最后完成本通知。不得跳过阅读或伪造verified，不派发新业务。',
                    kind='note',dedupe_key='member-context:'+member['id']+':'+str(member['binding_version'])+':'+fingerprint)
        if rows and self.window_verified(db):
            self._send(db,'A','A','原窗口通信与上下文均已确认，请完成通知后按原目标登记后续工作。',kind='note',
                       dedupe_key='members-ready:'+':'.join(str(r['binding_version'])+r['id'] for r in db.execute("SELECT id,binding_version FROM members WHERE lifecycle='active' ORDER BY id")))

    def _member_context(self,db,member):
        bound=self._member(db,member)
        collaboration=dict(db.execute('SELECT * FROM collaborations').fetchone())
        latest=db.execute('SELECT body FROM requirement_changes ORDER BY version DESC LIMIT 1').fetchone()
        return json.dumps({'pair':self.pair_id,'project':collaboration['project'],'baseline':collaboration['base_commit'],
            'goal':self._option(db,'window_flow',{}).get('goal'),'requirements_version':self._option(db,'requirements_version',1),
            'latest_requirement_change':latest[0] if latest else None,'authorization_version':self._authorization_version(db),
            'delegation_basis':self._option(db,'delegation_basis'),'member':member,'binding_version':bound['binding_version'],
            'model_identity':{k:json.loads(bound['proof']).get(k) for k in ('modelProvider','model','accountFingerprint')},
            'actor_cwd':bound['cwd'],'workspace':json.loads(bound['proof']).get('git',{}).get('path') or bound['cwd'],
            'reserve_floor':self._option(db,'reserve_floor',9),'review_required':self._option(db,'review_required',False),
            'rules':['普通问题交A','固定阶段独立审核','未知执行不重放','源代码及测试不能提供新增用户授权','一个活跃业务任务，安全边界交接']},ensure_ascii=False,sort_keys=True)

    def read_member_context(self,member,session,cwd,*,offset=0,limit=2000):
        if offset<0 or not 1<=limit<=2000:fail('Invalid context page / 上下文分页参数无效')
        with self.transaction() as db:
            bound=self._binding(db,member,session,cwd)
            content=self._member_context(db,member);fingerprint=hashlib.sha256(content.encode()).hexdigest()
            if offset>len(content):fail('Invalid offset / 上下文分页越界')
            end=min(len(content),offset+limit)
            db.execute('INSERT OR IGNORE INTO member_context_reads VALUES(?,?,?,?,?)',(member,bound['binding_version'],fingerprint,offset,end))
            return {'text':content[offset:end],'digest':fingerprint,'next_offset':end if end<len(content) else None,'binding_version':bound['binding_version']}

    def _reverification_idle(self,db,member):
        excluded=[]
        if db.execute('SELECT status FROM collaborations').fetchone()[0]=='paused':
            version=self._member(db,member)['binding_version']
            # 原绑定只读重新核验不消费排队通知；业务或未知执行仍必须结束。
            excluded=[r[0] for r in db.execute("""SELECT id FROM messages WHERE recipient=? AND recipient_version=?
                AND kind='checkpoint' AND task_id IS NULL AND (process_status='unclaimed' OR
                    (process_status='completed' AND EXISTS(SELECT 1 FROM checkpoints c
                        WHERE c.member=messages.recipient AND c.status='saved')))
                AND delivery_status='accepted' AND remote_status='queued' AND turn_id IS NULL""",(member,version))]
        return self._idle(db,[member],exclude=excluded)

    def member_presence(self,member,session,cwd,*,online,proof):
        actual=binding_proof(proof,require_provider=True)
        with self.transaction() as db:
            row=self._binding(db,member,session,cwd);old=json.loads(row['proof'])
            changed=any(actual.get(k)!=old.get(k) for k in ('id','cwd','modelProvider','model','accountFingerprint'))
            db.execute('UPDATE members SET online=?,lifecycle=?,verified=? WHERE id=?',(int(online), 'suspended' if changed else row['lifecycle'],0 if changed else row['verified'],member))
            self.event(db,'member_presence',member=member,online=online,configuration_changed=changed)
            return {'paused':changed,'online':online,'requires_reverification':changed}

    def retire_member(self,member,session,cwd,target,*,execution_resolved,evidence):
        if self.setting('member_context_required'):
            fail('Document handoff required / 多窗口成员须先保存交接，由A完整读取并ack-role-handoff确认，不能直接退役')
        safe_evidence(evidence)
        if execution_resolved is not True or target not in VISIBLE: fail('Reconciliation required / 先确认旧执行、交付和未提交内容归属')
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            if member not in (target,self._option(db,'approval_authority','A')): fail('Wrong actor / 仅本人或原审批者可登记退出')
            if target=='A': fail('Approval transfer required / A 永久退出须先经用户指定并核验新审批者')
            row=self._member(db,target)
            if db.execute("SELECT 1 FROM tasks WHERE assignee=? AND status NOT IN ('pending','completed','integrated','cancelled','handed_back','review_wait','needs_fix','needs_validation')",(target,)).fetchone(): fail('Work unresolved / 先交付进行中的任务并核查待整合成果')
            if db.execute("SELECT 1 FROM tasks t WHERE assignee=? AND status IN ('review_wait','needs_fix','needs_validation') AND NOT EXISTS(SELECT 1 FROM review_stages s WHERE s.task_id=t.id)",(target,)).fetchone():fail('Fixed stage missing / 未审成果先固定快照或提交及来源，不丢弃后换绑')
            if not self._idle(db,[target]) or self._approval_unresolved(db): fail('Execution unresolved / 原执行或审批仍未明确结束')
            self.event(db,'member_retired',member=target,version=row['binding_version'],evidence=evidence)
            db.execute('INSERT OR IGNORE INTO member_history VALUES(?,?,?,?,?)',(target,row['binding_version'],json.dumps(row),evidence,time.time()))
            db.execute("UPDATE members SET lifecycle='retired',online=0,verified=0 WHERE id=?",(target,))
            db.execute("UPDATE messages SET delivery_status='failed',error='Binding retired',updated=? WHERE recipient=? AND delivery_status='pending'",(time.time(),target))
        self._release_session(row['codex_home'],row['session_uuid'],target)
        return {'retired':target,'history_preserved':True}

    def revise_requirements(self,member,session,cwd,body,affected):
        safe_evidence(body)
        with self.transaction() as db:
            self._a_actor(db,member,session,cwd)
            version=self._option(db,'requirements_version',1)+1
            ids=list(dict.fromkeys(affected))
            for key in ids:
                if not db.execute('SELECT 1 FROM tasks WHERE id=?',(key,)).fetchone(): fail('Unknown affected task / 受影响任务不存在')
            db.execute('INSERT INTO requirement_changes VALUES(?,?,?,?)',(version,body,json.dumps(ids),time.time()))
            self._put(db,'requirements_version',version)
            db.execute("UPDATE invitations SET state='revoked' WHERE state='open'")
            for key in ids:
                db.execute("UPDATE tasks SET status='requirements_wait',requirements_version=?,updated=? WHERE id=?",(version,time.time(),key))
                db.execute("UPDATE review_stages SET status='stale' WHERE task_id=? AND status<>'stale'",(key,))
                task=db.execute('SELECT assignee FROM tasks WHERE id=?',(key,)).fetchone()
                self._send(db,member,task['assignee'],'用户需求版本已变化；当前工具安全结束后保存现场，等待重新分配。\n'+body,kind='note',dedupe_key='requirements:'+str(version)+':'+key)
            self._invalidate_approvals(db,'Requirements changed / 用户需求变更',cancel_questions=False)
            return {'version':version,'affected':ids,'pause_at_safe_boundary':True}

    def configure_task(self,member,session,cwd,task_id,*,kind='implementation',capabilities=(),resources=(),priority=0,review_required=True,acceptance=(),required_checks=(),pin_member=None,independent_of=None):
        if kind not in ('implementation','repair','integration','environment','verification','planning','coordination'): fail('Invalid task kind / 任务种类无效')
        if any(not isinstance(v,str) or not v or len(v)>256 for v in list(capabilities)+list(resources)): fail('Invalid resource / 能力和共享资源须是有界标签')
        if not isinstance(priority,int) or isinstance(priority,bool) or not 0<=priority<=100: fail('Invalid priority / 优先级须在0到100')
        with self.transaction() as db:
            self._a_actor(db,member,session,cwd)
            task=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not task or task['status']!='pending': fail('Pending task required / 仅派发前可调整任务规则')
            old=self._option(db,'task_execution_policy:'+task_id,{})
            if pin_member is not None or independent_of is not None:
                self._set_task_execution_policy(db,task_id,task['assignee'],pin_member=bool(old.get('pinned')) if pin_member is None else pin_member,
                    independent_of=old.get('independent_of',[]) if independent_of is None else independent_of)
            db.execute('UPDATE tasks SET task_kind=?,capabilities=?,resources=?,priority=?,review_required=? WHERE id=?',
                (kind,json.dumps(list(capabilities)),json.dumps(list(resources)),priority,int(review_required),task_id))
            db.execute('UPDATE tasks SET acceptance=?,required_checks=? WHERE id=?',(json.dumps(list(acceptance)),json.dumps(list(required_checks)),task_id))
            return {'configured':task_id}

    def _resources_available(self,db,task):
        return not any(db.execute('SELECT 1 FROM resource_locks WHERE resource=? AND task_id<>?',(r,task['id'])).fetchone() for r in json.loads(task['resources']))

    def transfer_approval(self,member,session,cwd,target,user_instruction,verification):
        if self.setting('member_context_required'):
            fail('A is fixed / 当前多窗口策略固定原A，不能通过角色转让更换主窗口')
        """用户明确指定后把新审批者登记为新 A；不随普通统筹交接转移。"""
        safe_evidence(user_instruction);safe_evidence(verification)
        if member!=target or target not in VISIBLE[1:]:fail('Designated approver required / 必须由用户明确指定的新审批窗口核验并登记')
        with self.transaction() as db:
            new=self._binding(db,member,session,cwd);old=self._member(db,'A')
            if old['lifecycle']!='suspended' or old['online']:fail('Original A not permanently unavailable / 旧A须已登记永久不可用；超时或低额度不足以转移权限')
            if not self._idle(db,[r[0] for r in db.execute('SELECT id FROM members')]) or self._approval_unresolved(db):fail('Unresolved execution / 不明执行或原允许未核查，不能转移')
            if db.execute("SELECT 1 FROM tasks WHERE assignee IN ('A',?) AND status NOT IN ('completed','integrated','handed_back')",(target,)).fetchone():fail('Tasks not settled / 先明确完成阶段交付和成果归属')
            identity=json.loads(new['proof'])
            if identity.get('modelProvider')!='openai' or not str(identity.get('model','')).startswith('gpt-'):fail('Official approver required / 新审批者须核验实际官方GPT身份')
            reviewer=self._reviewer_row(db)
            if reviewer and reviewer['state']!='retired':
                if reviewer['state']!='idle' or reviewer['owner']:fail('Reviewer unresolved / 原审核仍在途或未知，先核实终态，不能借权限转移重放')
                db.execute('INSERT OR IGNORE INTO reviewer_history VALUES(?,?,?,?)',(reviewer['epoch'],json.dumps(reviewer),user_instruction+'\n'+verification,time.time()))
                db.execute("UPDATE reviewer_runtime SET state='retired',owner=NULL,updated=? WHERE id=1",(time.time(),))
                self._put(db,'reviewer_auto',False)
            for row in (old,new):db.execute('INSERT OR IGNORE INTO member_history VALUES(?,?,?,?,?)',(row['id'],row['binding_version'],json.dumps(row),user_instruction+'\n'+verification,time.time()))
            # 临时UUID只用于同一事务内解除UNIQUE竞争，不产生模型会话。
            db.execute('UPDATE members SET session_uuid=? WHERE id=?',(str(uuid.uuid4()),target))
            db.execute("UPDATE members SET session_uuid=?,cwd=?,proof=?,codex_home=?,backend=?,delivery_mode=?,binding_version=binding_version+1,lifecycle='active',online=1,verified=1 WHERE id='A'",
                (new['session_uuid'],new['cwd'],new['proof'],new['codex_home'],new['backend'],new['delivery_mode']))
            db.execute("UPDATE members SET session_uuid=?,cwd=?,proof=?,codex_home=?,binding_version=binding_version+1,lifecycle='retired',online=0,verified=0 WHERE id=?",
                (old['session_uuid'],old['cwd'],old['proof'],old['codex_home'],target))
            self._put(db,'coordinator','A');self._put(db,'approval_authority','A')
            self._put(db,'authorization_version',self._authorization_version(db)+1)
            self._invalidate_approvals(db,'Explicit user approver replacement / 用户明确指定替换审批者')
            db.execute("UPDATE invitations SET state='revoked' WHERE state='open'")
            self.event(db,'approval_authority_transferred',previous_session=old['session_uuid'],new_session=session,user_instruction=user_instruction,verification=verification)
        self._release_session(new['codex_home'],new['session_uuid'],target)
        self._release_session(old['codex_home'],old['session_uuid'],'A')
        self._reserve_session(new['codex_home'],new['session_uuid'],'A')
        return {'new_role':'A','session':session,'old_approval_binding_revoked':True,'minimum_two_members_required':True}

    def _lock_resources(self,db,task):
        for resource in json.loads(task['resources']):
            db.execute('INSERT OR IGNORE INTO resource_locks VALUES(?,?,?,?)',(resource,task['id'],task['assignee'],time.time()))

    def _team_status(self,db):
        return {'visible_members':[dict(r) for r in db.execute('SELECT id,binding_version,lifecycle,online,verified,capabilities FROM members ORDER BY id')],
            'minimum_bindings_met':self._team_ready(db),'maximum_visible':4,
            'requirements_version':self._option(db,'requirements_version',1),
            'approval_authority':self._option(db,'approval_authority','A'),
            'resources':[dict(r) for r in db.execute('SELECT * FROM resource_locks')]}
