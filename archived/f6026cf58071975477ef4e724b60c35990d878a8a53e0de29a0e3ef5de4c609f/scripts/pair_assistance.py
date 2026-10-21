"""原窗口协助执行与项目内额度检查点；桥永远不执行请求中的命令。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import time
import uuid

try:
    from .pair_transport import canonical_path, redact_diagnostic
    from .pair_approvals import ApprovalMixin
except ImportError:
    from pair_transport import canonical_path, redact_diagnostic
    from pair_approvals import ApprovalMixin


def fail(text):
    try:
        from .pair_core import PairError
    except ImportError:
        from pair_core import PairError
    raise PairError(text)


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True)


def digest(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def workspace(member):
    proof=member['proof']
    if isinstance(proof,str):proof=json.loads(proof)
    return canonical_path(proof.get('git',{}).get('path') or member['cwd'])


def clean(value):
    return redact_diagnostic(value)


def recycle(path):
    """仅使用系统回收站，失败不提权、不改为递归删除。"""
    if os.name!='nt':raise OSError('System recycle bin unavailable')
    import ctypes
    from ctypes import wintypes
    class Operation(ctypes.Structure):
        _fields_=[('hwnd',wintypes.HWND),('wFunc',wintypes.UINT),('pFrom',wintypes.LPCWSTR),
                  ('pTo',wintypes.LPCWSTR),('flags',wintypes.WORD),('aborted',wintypes.BOOL),
                  ('mapping',ctypes.c_void_p),('title',wintypes.LPCWSTR)]
    operation=Operation(wFunc=3,pFrom=str(path)+'\0\0',flags=0x40|0x10|0x4|0x400)
    if ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation)) or operation.aborted:
        raise OSError('System recycle bin failed')


class AssistanceMixin(ApprovalMixin):
    def _upgrade_assistance(self, db):
        # deliveries 原有 task_id UNIQUE 只允许一次业务交付；辅助操作有独立记录。
        with db:
            db.execute('BEGIN IMMEDIATE')
            for table in ('record_reads','record_acks','deliveries'):
                db.execute('ALTER TABLE '+table+' RENAME TO '+table+'_v5')
            db.execute('''CREATE TABLE deliveries(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),
                body TEXT NOT NULL,commits_json TEXT NOT NULL,digest TEXT NOT NULL,created REAL NOT NULL)''')
            db.execute('''CREATE TABLE record_reads(member TEXT NOT NULL,delivery_id TEXT NOT NULL REFERENCES deliveries(id),
                digest TEXT NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,
                PRIMARY KEY(member,delivery_id,digest,start,end))''')
            db.execute('''CREATE TABLE record_acks(member TEXT NOT NULL,delivery_id TEXT NOT NULL REFERENCES deliveries(id),
                digest TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(member,delivery_id,digest))''')
            for table in ('deliveries','record_reads','record_acks'):
                db.execute('INSERT INTO '+table+' SELECT * FROM '+table+'_v5')
            for table in ('record_reads','record_acks','deliveries'):db.execute('DROP TABLE '+table+'_v5')
            db.execute('''CREATE TABLE execution_requests(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),
                source_message TEXT NOT NULL REFERENCES messages(id),message_id TEXT NOT NULL REFERENCES messages(id),
                payload TEXT NOT NULL,digest TEXT NOT NULL,status TEXT NOT NULL,review TEXT,result_delivery TEXT,
                created REAL NOT NULL,updated REAL NOT NULL)''')
            db.execute('''CREATE TABLE checkpoints(member TEXT PRIMARY KEY REFERENCES members(id),generation TEXT NOT NULL,
                path TEXT NOT NULL,digest TEXT NOT NULL,body TEXT NOT NULL,manifest TEXT NOT NULL,status TEXT NOT NULL,
                reads TEXT NOT NULL,recipient TEXT,message_id TEXT,created REAL NOT NULL,updated REAL NOT NULL)''')
            db.execute('PRAGMA user_version=6')

    @staticmethod
    def _option(db,key,default=None):
        row=db.execute('SELECT value FROM pair_settings WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _put(db,key,value):
        db.execute('INSERT INTO pair_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,encoded(value)))

    def _coordinator(self,db):
        return 'A' if self._option(db,'member_context_required',False) else self._option(db,'coordinator','A')

    def configure_assistance(self,member,session,cwd,*,execution=None,checkpoints=None):
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            if member!='A':fail('Coordinator A required / 仅本轮 A 可以设置协助策略')
            if execution is not None:
                if not execution and db.execute("SELECT 1 FROM execution_requests WHERE status NOT IN ('success','failed','declined','resolved')").fetchone():
                    fail('Execution unresolved / 请先核实未结束的执行请求')
                self._put(db,'execution_assistance',bool(execution))
            if checkpoints is not None:
                if not checkpoints and db.execute("SELECT 1 FROM checkpoints WHERE status NOT IN ('consumed','transferred')").fetchone():
                    fail('Checkpoint unresolved / 请先处理未接管的交接')
                if not checkpoints and any(self._option(db,'quota_state:'+m[0],'ready') in ('saving','recovering') for m in db.execute('SELECT id FROM members')):
                    fail('Checkpoint unresolved / 请先完成正在保存或恢复的交接')
                self._put(db,'quota_checkpoints',bool(checkpoints))
                if not checkpoints:
                    for row in db.execute('SELECT id FROM members').fetchall():self._put(db,'quota_state:'+row[0],'ready')
            self.event(db,'assistance_configured',execution=execution,checkpoints=checkpoints)
            return self._assistance_status(db)

    def _owned_message(self,db,member,message_id,token):
        row=db.execute('SELECT * FROM messages WHERE id=? AND recipient=?',(message_id,member)).fetchone()
        if not row or row['process_status']!='claimed' or not secrets.compare_digest(row['claim_token'] or '',token):
            fail('Invalid claim token or owner / 领取令牌或领取者不符')
        return row

    def _running(self,db):
        return list(db.execute("""SELECT * FROM messages WHERE delivery_status IN ('delivering','unknown') OR
            (delivery_status='accepted' AND (remote_status IS NULL OR remote_status IN ('queued','inProgress')))"""))

    def _settled_checkpoint_receipts(self,db):
        # 保存已结束的检查点可能仍保留未启动的排队回执，不等同于业务执行。
        return {r[0] for r in db.execute("""SELECT m.id FROM messages m
            JOIN members b ON b.id=m.recipient AND b.binding_version=m.recipient_version
            JOIN checkpoints c ON c.member=m.recipient
            WHERE m.kind='checkpoint' AND m.task_id IS NULL AND m.process_status='completed'
            AND m.delivery_status='accepted' AND m.remote_status='queued' AND m.turn_id IS NULL
            AND c.status IN ('saved','offered','consumed','transferred')""")}
    def _idle(self,db,members,exclude=()):
        targets={workspace(self._member(db,m)) for m in members}
        exclude=set(exclude)|self._settled_checkpoint_receipts(db)
        if any(m['id'] not in exclude and workspace(self._member(db,m['recipient'])) in targets for m in self._running(db)):return False
        for row in db.execute("SELECT message_id FROM execution_requests WHERE status='running'"):
            if row[0] not in exclude and targets & {workspace(self._member(db,m)) for m in [r[0] for r in db.execute('SELECT id FROM members')]}:return False
        return True

    def request_execution(self,member,session,cwd,message_id,token,request_id,payload):
        try:
            from .pair_core import identifier,normalize_scopes
        except ImportError:
            from pair_core import identifier,normalize_scopes
        identifier(request_id)
        fields={'argv','cwd','purpose','paths','expected_effects','authorization_basis','reason'}
        if not isinstance(payload,dict) or set(payload)!=fields:fail('Invalid execution request / 执行请求字段不完整或包含未知字段')
        argv=payload['argv']
        if not isinstance(argv,list) or not argv or any(not isinstance(a,str) or '\0' in a for a in argv) or not argv[0]:
            fail('Invalid argv / 命令必须是非空参数数组')
        if any(not isinstance(payload[k],str) or not payload[k].strip() for k in ('purpose','expected_effects','authorization_basis')):
            fail('Execution context required / 必须说明目的、预期影响及用户授权依据')
        if not isinstance(payload['cwd'],str) or not payload['cwd'].strip():fail('Invalid execution cwd / 必须提供实际执行目录')
        if payload['reason']!='capability_unavailable':fail('Delegation forbidden / 用户拒绝或系统禁止的操作不能转交绕过')
        if clean(payload)!=payload:fail('Credential in request / 请求不能包含认证令牌')
        if not isinstance(payload['paths'],list) or any(not isinstance(p,str) for p in payload['paths']):fail('Invalid paths / 影响路径必须是相对路径数组')
        payload=dict(payload,paths=normalize_scopes(payload['paths']),cwd=canonical_path(payload['cwd']))
        text=encoded(payload);fingerprint=digest(text)
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            if member=='A' or not self._option(db,'execution_assistance',False):fail('Execution assistance disabled / 需由 A 显式开启本轮任务内协助执行')
            existing=db.execute('SELECT * FROM execution_requests WHERE id=?',(request_id,)).fetchone()
            if existing:
                if existing['digest']!=fingerprint or existing['source_message']!=message_id:fail('Request changed / 请求 ID 已用于不同内容')
                self._owned_message(db,member,message_id,token)
                return dict(existing)
            source=self._owned_message(db,member,message_id,token)
            task=db.execute('SELECT * FROM tasks WHERE id=?',(source['task_id'],)).fetchone()
            if source['kind']!='task' or task['assignee']!=member or task['status']!='active':fail('Active owned task required / 只能为当前领取的活动任务请求协助')
            root=Path(workspace(self._member(db,member)))
            if not Path(payload['cwd']).is_relative_to(root):fail('Execution cwd outside workspace / 执行目录超出任务工作树')
            allowed=json.loads(task['allowed_paths'])
            for path in payload['paths']:
                if not any(path==a or a.endswith('/') and path.startswith(a) for a in allowed):fail('Execution scope exceeded / 影响路径超出父任务允许范围')
                if not (root/path.rstrip('/')).resolve().is_relative_to(root):fail('Redirected execution path / 影响路径存在越界重定向')
            if db.execute("SELECT 1 FROM execution_requests WHERE task_id=? AND status='declined'",(task['id'],)).fetchone():
                fail('Previous execution declined / 已拒绝操作不能换请求 ID 重试；须由 A 核实新的用户授权')
            if db.execute("SELECT 1 FROM execution_requests WHERE task_id=? AND status NOT IN ('success','failed','declined','resolved')",(task['id'],)).fetchone():
                fail('Execution unresolved / 当前任务已有未核实的执行请求')
            msg=self._send(db,member,'A','协助执行请求 '+request_id+'\n摘要 '+fingerprint+'\n'+text+
                '\n先核对完整脚本及用户授权；使用 begin-execution 登记后在 A 原窗口执行，再 finish-execution。不要直接 complete。',
                kind='execution_request',task_id=task['id'],dedupe_key='execution:'+request_id)
            now=time.time()
            db.execute('INSERT INTO execution_requests VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (request_id,task['id'],message_id,msg['id'],text,fingerprint,'review',None,None,now,now))
            db.execute("UPDATE tasks SET status='waiting_execution',updated=? WHERE id=?",(now,task['id']))
            self.event(db,'execution_requested',request=request_id,task=task['id'])
            return dict(db.execute('SELECT * FROM execution_requests WHERE id=?',(request_id,)).fetchone())

    def begin_execution(self,member,session,cwd,request_id,token,fingerprint,review):
        if not isinstance(review,str) or not review.strip():fail('Review required / 必须记录任务授权及命令范围审核依据')
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            row=db.execute('SELECT * FROM execution_requests WHERE id=?',(request_id,)).fetchone()
            if member!='A' or not row:fail('Execution owner mismatch / 只能由 A 处理已有执行请求')
            self._owned_message(db,member,row['message_id'],token)
            if row['digest']!=fingerprint:fail('Request digest mismatch / 待执行内容摘要不符')
            if row['status']!='review':fail('Execution already started or unresolved / 操作已开始或结果待核实，禁止再次执行')
            if not self._idle(db,[r[0] for r in db.execute("SELECT id FROM members WHERE lifecycle='active'")],exclude=[row['message_id']]):fail('Workspace busy / 原窗口回合或操作尚未结束')
            db.execute("UPDATE execution_requests SET status='running',review=?,updated=? WHERE id=?",(clean(review),time.time(),request_id))
            self.event(db,'execution_started',request=request_id,digest=fingerprint)
            return {'request':request_id,'status':'running','payload':json.loads(row['payload']),'execute_in_original_window':True}

    def finish_execution(self,member,session,cwd,request_id,token,outcome,result):
        if outcome not in ('success','failed','declined','needs_authorization','unknown'):fail('Invalid outcome / 无效执行结果')
        if not isinstance(result,dict) or not isinstance(result.get('summary'),str) or not result['summary'].strip():fail('Execution evidence required / 结果须提供实质说明及证据')
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            row=db.execute('SELECT * FROM execution_requests WHERE id=?',(request_id,)).fetchone()
            if member!='A' or not row:fail('Execution owner mismatch / 只能由 A 交付执行结果')
            self._owned_message(db,member,row['message_id'],token)
            if row['status'] not in ('review','running','unknown','needs_authorization'):fail('Execution already finished / 执行请求已经结束')
            if row['status']=='review' and outcome in ('success','failed'):fail('Begin execution required / 必须先登记开始执行')
            payload=json.loads(row['payload'])
            if outcome in ('success','failed'):
                if result.get('argv')!=payload['argv'] or not isinstance(result.get('cwd'),str) or canonical_path(result['cwd'])!=payload['cwd'] or not isinstance(result.get('exit_code'),int) or isinstance(result['exit_code'],bool) or not result.get('evidence'):
                    fail('Execution evidence required / 必须提供原命令、目录、退出码及产物证据')
                if outcome=='success' and result['exit_code']!=0:fail('Exit code disagrees / 非零退出码不能声明成功')
            if row['result_delivery']:fail('Result already delivered / 请先由 B 确认已有结果；未知操作仅核实，不重跑')
            record_id='execution:'+request_id
            body=encoded({'request':request_id,'outcome':outcome,'result':clean(result)})
            db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?)',(record_id,row['task_id'],body,'[]',digest(encoded([body,'[]'])),time.time()))
            recipient=db.execute('SELECT assignee FROM tasks WHERE id=?',(row['task_id'],)).fetchone()[0]
            msg=self._send(db,'A',recipient,'执行结果 '+request_id+'：'+outcome+'。完整读取并确认交付后核对现场；未知结果禁止重跑。',
                kind='execution_result',task_id=row['task_id'],dedupe_key='execution-result:'+request_id)
            db.execute('UPDATE messages SET delivery_id=? WHERE id=?',(record_id,msg['id']))
            db.execute("UPDATE messages SET process_status='completed',updated=? WHERE id=?",(time.time(),row['message_id']))
            db.execute('UPDATE execution_requests SET status=?,result_delivery=?,updated=? WHERE id=?',(outcome,record_id,time.time(),request_id))
            self.event(db,'execution_finished',request=request_id,outcome=outcome)
            return {'request':request_id,'outcome':outcome,'delivery_id':record_id,'message_id':msg['id']}

    def resolve_execution(self,member,session,cwd,request_id,evidence):
        """未知操作先查现场；只解除阻塞，不重新执行命令。"""
        if not evidence.strip():fail('Reconciliation evidence required / 必须提供现场核实或新增授权依据')
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            row=db.execute('SELECT * FROM execution_requests WHERE id=?',(request_id,)).fetchone()
            if member!='A' or not row or row['status'] not in ('unknown','needs_authorization','declined'):fail('Unresolved request required / 仅 A 可核实未知操作或新增的用户授权')
            if not self._idle(db,[r[0] for r in db.execute("SELECT id FROM members WHERE lifecycle='active'")]):fail('Workspace busy / 请先等待原回合结束')
            if row['result_delivery']:
                self._require_ack(db,db.execute('SELECT assignee FROM tasks WHERE id=?',(row['task_id'],)).fetchone()[0],row['result_delivery'])
                msg=db.execute("SELECT process_status FROM messages WHERE delivery_id=?",(row['result_delivery'],)).fetchone()
                if not msg or msg[0]!='completed':fail('Result acknowledgement required / B 必须先确认并收尾旧结果')
            db.execute("UPDATE execution_requests SET status='resolved',updated=? WHERE id=?",(time.time(),request_id))
            db.execute("UPDATE messages SET process_status='completed',updated=? WHERE id=?",(time.time(),row['message_id']))
            db.execute("UPDATE tasks SET status='active' WHERE id=? AND status='waiting_execution'",(row['task_id'],))
            self._send(db,'A',db.execute('SELECT assignee FROM tasks WHERE id=?',(row['task_id'],)).fetchone()[0],'请求 '+request_id+' 已核实，不自动重跑。核实依据：'+clean(evidence),kind='note',task_id=row['task_id'],dedupe_key='execution-resolved:'+request_id)
            self.event(db,'execution_reconciled',request=request_id,evidence=clean(evidence))
            return {'resolved':True,'rerun':False}

    def _checkpoint_path(self,db,member,*,for_save=False):
        bound=self._member(db,member)
        project=Path(db.execute('SELECT project FROM collaborations').fetchone()[0])
        root=project/'.codex-pair'/'checkpoints'
        if root.resolve()!=root.absolute():fail('Redirected checkpoint path / 交接目录存在重定向')
        legacy=root/(digest(canonical_path(bound['codex_home'])+'|'+bound['session_uuid'])[:24]+'.md')
        binding=digest(encoded({'home':canonical_path(bound['codex_home']),'session':bound['session_uuid'],
                                'version':bound.get('binding_version',1)}))[:24]
        current=root/self.pair_id/binding/('交接文档'+str(('A','B','C','D').index(member)+1)+'.md')
        row=db.execute('SELECT * FROM checkpoints WHERE member=?',(member,)).fetchone()
        def checked(path):
            if not path.is_absolute() or not path.is_relative_to(root) or path.resolve()!=path.absolute():
                fail('Redirected checkpoint path / 交接路径越界或存在重定向')
            return path
        checked(current);checked(legacy)
        previous=checked(Path(row['path'])) if row else legacy
        if for_save:
            if previous.exists() or legacy.exists():fail('Checkpoint file exists / 旧交接尚未清理，不能覆盖或另存一份')
            return current
        if row and (row['status'] not in ('consumed','transferred') or previous.exists()):
            manifest=json.loads(row['manifest'])
            if (manifest.get('pair'),manifest.get('member'),manifest.get('session'))!=(self.pair_id,member,bound['session_uuid']):
                fail('Checkpoint binding mismatch / 旧交接属于不同轮次或窗口，保留并核实')
            if manifest.get('binding_version',bound.get('binding_version',1))!=bound.get('binding_version',1):
                fail('Checkpoint binding mismatch / 旧交接绑定代次不符，保留并核实')
            return previous
        return current if current.exists() or not legacy.exists() else legacy

    def _member_suspended(self,db,member):
        if self._option(db,'quota_state:'+member,'ready')!='ready':return True
        if self._pool_blocked(db,member):return True
        q=self._quotas(db).get(member,{}) if self._option(db,'quota_checkpoints',False) else {}
        return q.get('reliability')=='fresh' and q['remaining']<=self._reserve(db,member)

    def _reserve(self,db,member):
        floor=self._option(db,'reserve_floor',9)
        own=max(floor,min(95,self._option(db,'checkpoint_reserve:'+member,floor)))
        quotas=self._quotas(db);pool=quotas.get(member,{}).get('pool')
        peers=[max(floor,min(95,self._option(db,'checkpoint_reserve:'+m,floor))) for m,q in quotas.items() if pool and q.get('pool')==pool and q.get('limit_id')==quotas.get(member,{}).get('limit_id')]
        return max([own]+peers)

    def _healthy(self,db,member,quotas):
        q=quotas.get(member,{})
        return q.get('reliability')=='fresh' and q.get('remaining',0)>=min(100,self._reserve(db,member)+5)

    def _checkpoint_read_required(self,db,member):
        return any(r['status'] not in ('consumed','transferred') for r in db.execute('SELECT status FROM checkpoints WHERE member=? OR recipient=?',(member,member)))

    @staticmethod
    def _transferable_messages(messages):
        return all(m['kind'] in ('task','checkpoint','question','reply','approval_review') or
            m['kind']=='note' and m.get('dedupe_key','').startswith('approval-expired:') for m in messages)

    def resume_checkpoint(self,member,session,cwd,reason):
        """额度不可读取时由用户显式恢复原窗口，不据此宣称额度已验证。"""
        if not reason.strip():fail('Capacity confirmation required / 必须记录用户要求继续的依据')
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            row=db.execute("SELECT * FROM checkpoints WHERE member=? AND status='saved'",(member,)).fetchone()
            if not row:fail('Saved checkpoint required / 只能恢复本窗口尚未投递的交接')
            q=self._quotas(db).get(member,{})
            if q.get('reliability')=='fresh' and not self._healthy(db,member,self._quotas(db)):
                fail('Quota below recovery margin / 已知额度仍未超过恢复线')
            if not self._idle(db,[member]):fail('Workspace busy / 原操作尚未结束')
            msg=self._send(db,member,member,'用户要求恢复本窗口。先完整读取交接并 consume-checkpoint，核对现场后继续。依据：'+clean(reason),
                kind='checkpoint_resume',dedupe_key='checkpoint-resume:'+row['generation'])
            db.execute("UPDATE checkpoints SET recipient=?,message_id=?,status='offered' WHERE member=?",(member,msg['id'],member))
            self._put(db,'manual_capacity:'+row['generation'],True)
            self._put(db,'quota_state:'+member,'recovering')
            self.event(db,'checkpoint_manual_resume',member=member,reason=clean(reason),quota_verified=False)
            return {'offered':True,'quota_verified':False,'message_id':msg['id']}

    def _assistance_tick(self,db):
        self.approval_tick(db)
        if db.execute('SELECT status FROM collaborations').fetchone()[0]!='running':return
        # 执行回合结束而未交付时不能猜测命令是否执行过。
        for r in db.execute("SELECT r.*,m.remote_status FROM execution_requests r JOIN messages m ON m.id=r.message_id WHERE r.status='running'").fetchall():
            if r['remote_status'] in ('completed','failed','interrupted'):
                db.execute("UPDATE execution_requests SET status='unknown',updated=? WHERE id=?",(time.time(),r['id']))
                self.event(db,'execution_outcome_unknown',request=r['id'])
        if not self._option(db,'quota_checkpoints',False):return
        quotas=self._quotas(db)
        members=[r[0] for r in db.execute('SELECT id FROM members')]
        for member in members:
            q=quotas[member];state=self._option(db,'quota_state:'+member,'ready')
            if state=='ready' and q.get('reliability')=='fresh' and q['remaining']<=self._reserve(db,member):
                self._put(db,'quota_state:'+member,'saving')
                self._put(db,'checkpoint_start:'+member,{'remaining':q['remaining'],'pool':q.get('pool'),'limit_id':q.get('limit_id'),'windows':q.get('windows',[])})
                generation=uuid.uuid4().hex
                self._send(db,member,member,'额度已接近交接保留线。停止新工作，等待工具结束，使用 save-checkpoint 保存目标、授权、已完成、未完成、验证及未知操作，然后结束回合；不要直接 complete。',
                    kind='checkpoint',dedupe_key='checkpoint-save:'+generation)
                self.event(db,'checkpoint_requested',member=member,remaining=q['remaining'])
            if state=='paused' and not self._checkpoint_read_required(db,member):
                # 恢复接新任务的资格，不抢回已转交的工作。
                if self._healthy(db,member,quotas) and self._member_eligible(db,member) and not self._pool_blocked(db,member) and self._idle(db,[member]):
                    self._put(db,'quota_state:'+member,'ready')
                    self.event(db,'checkpoint_origin_ready',member=member,tasks_reclaimed=False)
                continue
            checkpoint=db.execute("SELECT * FROM checkpoints WHERE member=? AND status='saved'",(member,)).fetchone()
            if not checkpoint:continue
            peers=[m for m in members if m!=member and self._member_eligible(db,m) and quotas.get(m,{}).get('pool')!=q.get('pool') and self._healthy(db,m,quotas) and not self._member_suspended(db,m) and self._checkpoint_transfer_allowed(db,member,m)]
            peer=min(peers,key=lambda m:sum(t[0] for t in db.execute("SELECT effort FROM tasks WHERE assignee=? AND status='active'",(m,))),default=None)
            same_pool=False
            manifest=json.loads(checkpoint['manifest'])
            transferable=self._transferable_messages(manifest['messages']) and not db.execute("SELECT 1 FROM execution_requests WHERE status NOT IN ('success','failed','declined','resolved')").fetchone()
            if self._approval_unresolved(db,grants_only=True):transferable=False
            recipient=peer if transferable and peer in members and not same_pool and self._healthy(db,peer,quotas) and not self._member_suspended(db,peer) else None
            if not recipient and self._healthy(db,member,quotas):recipient=member
            if not recipient:continue
            if not self._idle(db,[member,recipient]):continue
            if member!=recipient and workspace(self._member(db,member))!=workspace(self._member(db,recipient)):
                # shortcut: 跨 worktree 的未提交修改不能自动搬运，原窗口恢复后继续。
                continue
            msg=self._send(db,member,recipient,'交接已保存。先 check-checkpoint，再 read-checkpoint 分页读完，核对现场，用 consume-checkpoint 附恢复计划持久化接管并清理；完成本通知后继续剩余工作，不能重做已完成操作。',
                kind='checkpoint_resume',dedupe_key='checkpoint-resume:'+checkpoint['generation'])
            db.execute("UPDATE checkpoints SET recipient=?,message_id=?,status='offered',updated=? WHERE member=?",(recipient,msg['id'],time.time(),member))
            self._put(db,'quota_state:'+recipient,'recovering')
            self.event(db,'checkpoint_offered',member=member,recipient=recipient)

    def _checkpoint_transfer_allowed(self,db,source,target):
        tasks=list(db.execute("SELECT * FROM tasks WHERE assignee=? AND status NOT IN ('completed','integrated','delivered','cancelled','handed_back')",(source,)))
        moved={t['id'] for t in tasks}
        if any(not self._member_eligible(db,target,t) or not self._task_member_allowed(db,t,target,takeover=moved) for t in tasks):return False
        # 接管实现不能同时成为其独立核验者，含尚未派发的后续任务。
        return all(self._task_member_allowed(db,t,target,takeover=moved) for t in
            db.execute("SELECT * FROM tasks WHERE assignee=? AND status NOT IN ('completed','integrated','delivered','cancelled','handed_back')",(target,)))

    def save_checkpoint(self,member,session,cwd,body,*,message_id=None,token=None):
        if not isinstance(body,str) or not body.strip():fail('Checkpoint context required / 交接须说明已完成、未完成、验证及下一步')
        try:
            from .pair_core import FileLock
            from .pair_git import prepare_pair_directory
        except ImportError:
            from pair_core import FileLock
            from pair_git import prepare_pair_directory
        with FileLock(self.control_lock,timeout=30),self.transaction() as db:
            self._binding(db,member,session,cwd)
            old=db.execute('SELECT * FROM checkpoints WHERE member=?',(member,)).fetchone()
            if old and old['status'] not in ('consumed','transferred'):fail('Checkpoint already pending / 已有未接管文档，不能覆盖')
            if not self._idle(db,[member],exclude=[message_id] if message_id else []):fail('Workspace busy / 保存前必须等待工具及其他回合结束')
            if message_id:
                message=self._owned_message(db,member,message_id,token or '')
                if message['kind'] not in ('checkpoint','task'):fail('Checkpoint or task message required / 只能从已领取任务或交接通知保存')
            path=self._checkpoint_path(db,member,for_save=True)
            project=db.execute('SELECT project FROM collaborations').fetchone()[0]
            prepare_pair_directory(project,str(path.parent),'checkpoints')
            path.parent.mkdir(parents=True,exist_ok=True)
            if path.exists():fail('Checkpoint file exists / 已有文件未清理，拒绝覆盖')
            tasks=[dict(r) for r in db.execute("SELECT * FROM tasks WHERE assignee=? AND status NOT IN ('completed','integrated','cancelled','handed_back')",(member,))]
            owned=[dict(r) for r in db.execute("SELECT id,task_id,kind,dedupe_key,process_status,delivery_status,remote_status FROM messages WHERE recipient=? AND process_status<>'completed'",(member,))]
            generation=uuid.uuid4().hex
            manifest={'pair':self.pair_id,'state_root':str(self.state_root),'project':project,'member':member,'session':session,
                      'binding_version':self._member(db,member).get('binding_version',1),
                      'goal':self._option(db,'window_flow',{}).get('goal'),'tasks':tasks,'messages':owned,'coordinator':self._coordinator(db),
                      'completed':[dict(r) for r in db.execute("SELECT id,title,result,commits_json FROM tasks WHERE assignee=? AND status IN ('completed','integrated')",(member,))],
                      'executions':[dict(r) for r in db.execute("SELECT id,task_id,status,digest FROM execution_requests WHERE status NOT IN ('success','failed','declined','resolved')")]}
            manifest['questions']=[dict(r) for r in db.execute("SELECT q.* FROM delegation_questions q JOIN messages m ON m.id=q.id WHERE (m.sender=? OR m.recipient=?) AND q.state NOT IN ('adopted','cancelled')",(member,member))]
            manifest['approvals']=[dict(r) for r in db.execute("SELECT id,task_id,status,digest,authorization_version,retry_state,attempt,reason FROM approval_requests WHERE status IN ('pending','allowed','granted','unknown') OR retry_state IN ('waiting','ready','needs_user')")]
            manifest['authorization_version']=self._authorization_version(db)
            text='# 项目工作交接\n\n'+clean(body)+'\n\n## 持久化任务现场\n\n```json\n'+encoded(clean(manifest))+'\n```\n'
            temporary=path.with_suffix('.tmp')
            if temporary.exists():fail('Checkpoint temporary file exists / 遗留临时文件须先核实，拒绝覆盖')
            try:
                with temporary.open('x',encoding='utf-8',newline='\n') as file:
                    file.write(text);file.flush();os.fsync(file.fileno())
                os.replace(temporary,path)
            finally:
                if temporary.exists():temporary.unlink()
            now=time.time()
            db.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                (member,generation,str(path),digest(text),text,encoded(clean(manifest)),'saved','{}',None,None,now,now))
            # 原消息令牌留在数据库，不把令牌写入交接文档。
            db.execute("UPDATE tasks SET status='quota_wait',updated=? WHERE assignee=? AND status NOT IN ('completed','integrated','delivered','cancelled','handed_back')",(now,member))
            db.execute("UPDATE messages SET process_status='completed',updated=? WHERE recipient=? AND kind='checkpoint'",(now,member))
            self._put(db,'quota_state:'+member,'paused')
            before=self._option(db,'checkpoint_start:'+member)
            after=self._quotas(db).get(member,{})
            if self._option(db,'calibration_isolated:'+member,False) and before and after.get('reliability')=='fresh' and before.get('pool')==after.get('pool') and before.get('limit_id')==after.get('limit_id') and before.get('windows') and [w['resets_at'] for w in before['windows']]==[w['resets_at'] for w in after['windows']]:
                spent=before['remaining']-after['remaining']
                if spent>=0:self._put(db,'checkpoint_reserve:'+member,max(self._reserve(db,member),min(95,spent*2+2)))
            self.event(db,'checkpoint_saved',member=member,path=str(path),digest=digest(text))
            return {'saved':True,'path':str(path),'digest':digest(text),'end_turn':True}

    def check_checkpoint(self,member,session,cwd):
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            records=[]
            sources={member}|{r[0] for r in db.execute("SELECT member FROM checkpoints WHERE recipient=? AND status='offered'",(member,))}
            for source in sorted(sources):
                path=self._checkpoint_path(db,source)
                if not path.is_file():continue
                row=db.execute('SELECT * FROM checkpoints WHERE member=?',(source,)).fetchone()
                if not row:fail('Unregistered checkpoint / 存在未登记的交接文件，请核实写入中断，禁止忽略或覆盖')
                if row['status'] in ('consumed','transferred') and digest(path.read_bytes().decode('utf-8'))!=row['digest']:
                    fail('Unregistered checkpoint / 已消费记录之外出现新文件，先核实写入中断')
                if row['status'] in ('consumed','transferred'):continue
                records.append({'source':source,'path':str(path),'digest':row['digest'],'status':row['status']})
            return {'exists':bool(records),'checkpoints':records,'read_history':False}

    def read_checkpoint(self,member,session,cwd,source,*,offset=0,limit=2000):
        if offset<0 or not 1<=limit<=2000:fail('Invalid page / 分页参数无效')
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            row=db.execute('SELECT * FROM checkpoints WHERE member=?',(source,)).fetchone()
            if not row or member not in (source,row['recipient']):fail('Checkpoint access denied / 当前窗口无权读取此交接')
            path=self._checkpoint_path(db,source)
            text=path.read_bytes().decode('utf-8')
            if digest(text)!=row['digest']:fail('Checkpoint changed / 交接文件已变更，不能确认旧版本')
            if offset>len(text):fail('Invalid offset / 分页越界')
            end=min(offset+limit,len(text));reads=json.loads(row['reads'])
            reads.setdefault(member,[]).append([offset,end])
            db.execute('UPDATE checkpoints SET reads=? WHERE member=?',(encoded(reads),source))
            return {'source':source,'digest':row['digest'],'text':text[offset:end],'next_offset':end if end<len(text) else None}

    def consume_checkpoint(self,member,session,cwd,source,fingerprint,recovery_plan):
        if not recovery_plan.strip():fail('Recovery plan required / 必须先记录核实后的恢复计划')
        try:
            from .pair_core import FileLock
        except ImportError:
            from pair_core import FileLock
        with FileLock(self.control_lock,timeout=30),self.transaction() as db:
            self._binding(db,member,session,cwd)
            row=db.execute('SELECT * FROM checkpoints WHERE member=?',(source,)).fetchone()
            if not row or row['digest']!=fingerprint:fail('Checkpoint version mismatch / 交接版本不符')
            if row['status'] in ('consumed','transferred'):return {'consumed':True,'duplicate':True}
            if row['status']!='offered' or row['recipient']!=member:fail('Checkpoint not offered / 需等待桥确认额度和原回合结束后接管')
            if not self._assistance_claimable(db,db.execute('SELECT * FROM messages WHERE id=?',(row['message_id'],)).fetchone()):
                fail('Recovery capacity unavailable / 恢复额度不再可靠或低于恢复线')
            if not self._idle(db,[source,member],exclude=[row['message_id']]):fail('Workspace busy / 接管前必须确认原操作结束')
            covered=0
            for start,end in sorted(json.loads(row['reads']).get(member,[])):
                if start>covered:break
                covered=max(covered,end)
            if covered<len(row['body']):fail('Checkpoint not fully read / 必须完整阅读交接文档')
            path=self._checkpoint_path(db,source)
            if digest(path.read_bytes().decode('utf-8'))!=fingerprint:fail('Checkpoint changed / 文件已变化，不能接管')
            manifest=json.loads(row['manifest'])
            new_context=self._option(db,'member_context_required',False)
            if member!=source:
                if not self._checkpoint_transfer_allowed(db,source,member):
                    fail('Checkpoint transfer constrained / 原窗口或独立核验约束禁止此次接管')
                if not self._transferable_messages(manifest['messages']):
                    fail('Messages unresolved / 原窗口有未收尾问答或执行，须由原窗口恢复核实后再转交')
                if db.execute("SELECT 1 FROM execution_requests WHERE status NOT IN ('success','failed','declined','resolved')").fetchone():
                    fail('Execution unresolved / 存在未知或待授权操作，不能改变任务负责人')
                if self._approval_unresolved(db,grants_only=True):
                    fail('Approval execution unresolved / 获批操作结果未确定，先核实再接管')
            for task in manifest['tasks']:
                current=db.execute('SELECT * FROM tasks WHERE id=?',(task['id'],)).fetchone()
                if current['status'] in ('completed','integrated','delivered','cancelled','handed_back'):continue
                if current['assignee']!=source or current['status']!='quota_wait':fail('Task ownership changed / 任务归属已变化，先核实现场')
                db.execute('UPDATE tasks SET assignee=?,report_to=CASE WHEN ?=0 AND report_to=? THEN ? ELSE report_to END,status=?,updated=? WHERE id=?',
                    (member,new_context,source,member,task['status'],time.time(),task['id']))
                if member!=source:
                    db.execute("UPDATE messages SET process_status='completed',updated=? WHERE task_id=? AND kind='task' AND recipient=?",(time.time(),task['id'],source))
                    if task['status']=='active':
                        self._send(db,member,member,task['body']+'\n从交接记录继续，禁止重跑已完成操作。恢复依据：'+clean(recovery_plan),kind='task',task_id=task['id'],dedupe_key='takeover:'+row['generation']+':'+task['id'])
            if member!=source:
                self._invalidate_approvals(db,'Task takeover / 工作已交接，审批权不随统筹权转移',cancel_questions=False)
                self._put(db,'authorization_version',self._authorization_version(db)+1)
                db.execute("UPDATE delegation_questions SET owner=CASE WHEN owner=? THEN ? ELSE owner END,authorization_version=?,updated=? WHERE state IN ('pending','answered','needs_user')",
                    (source,'A' if new_context else member,self._authorization_version(db),time.time()))
                for old in manifest['messages']:
                    if old['kind'] in ('question','reply','approval_review') or old['kind']=='note' and old.get('dedupe_key','').startswith('approval-expired:'):
                        db.execute("UPDATE messages SET process_status='completed',updated=? WHERE id=?",(time.time(),old['id']))
                self.event(db,'approval_takeover_boundary',reviewer='A',notify_user=True,
                    reason='工作接管不转移系统权限；A 不可用时，需要其审核的请求交给用户')
            if manifest['coordinator']==source:self._put(db,'coordinator','A' if new_context else member)
            if member!=source:
                db.execute("UPDATE tasks SET assignee=?,report_to=CASE WHEN ?=0 AND report_to=? THEN ? ELSE report_to END WHERE status='pending' AND assignee=?",(member,new_context,source,member,source))
            self._put(db,'quota_state:'+member,'ready')
            if member!=source:self._put(db,'quota_state:'+source,'paused')
            state='consumed' if member==source else 'transferred'
            db.execute('UPDATE checkpoints SET status=?,updated=? WHERE member=?',(state,time.time(),source))
            self._put(db,'recovery:'+row['generation'],{'source':source,'recipient':member,'plan':clean(recovery_plan),'digest':fingerprint,'manifest':manifest})
            self.event(db,'checkpoint_consumed',member=source,recipient=member,digest=fingerprint)
        # 先提交接管，再删文件；崩溃留下已消费标记，不能再次接管。
        return self._cleanup_checkpoint(source,fingerprint)

    def _cleanup_checkpoint(self,source,fingerprint):
        with self.transaction() as db:
            path=self._checkpoint_path(db,source)
            row=db.execute('SELECT status,digest FROM checkpoints WHERE member=?',(source,)).fetchone()
            if not row or row['status'] not in ('consumed','transferred') or row['digest']!=fingerprint:fail('Checkpoint not consumed / 未持久化接管，不能删除')
        cleanup='absent'
        if path.exists():
            if digest(path.read_bytes().decode('utf-8'))!=fingerprint:return {'consumed':True,'cleanup':'changed_file_preserved'}
            try:path.unlink();cleanup='deleted'
            except OSError:
                try:recycle(path);cleanup='recycled'
                except OSError:cleanup='retained'
        with self.transaction() as db:self.event(db,'checkpoint_cleanup',member=source,cleanup=cleanup)
        return {'consumed':True,'cleanup':cleanup,'path':str(path)}

    def recover_checkpoint(self,member,session,cwd):
        """处理文件已落盘但事务未提交，或接管后清理中断；不重跑业务。"""
        try:
            from .pair_core import FileLock
        except ImportError:
            from pair_core import FileLock
        with FileLock(self.control_lock,timeout=30),self.transaction() as db:
            self._binding(db,member,session,cwd)
            path=self._checkpoint_path(db,member)
            row=db.execute('SELECT * FROM checkpoints WHERE member=?',(member,)).fetchone()
            if row and row['status'] in ('consumed','transferred') and path.is_file() and digest(path.read_bytes().decode('utf-8'))!=row['digest']:
                row=None
            if row:
                if row['status'] not in ('consumed','transferred'):return {'recovered':False,'reason':'registered_checkpoint_pending'}
                fingerprint=row['digest']
            else:
                if not path.is_file():return {'recovered':False,'reason':'checkpoint_absent'}
                if not self._idle(db,[member]):fail('Workspace busy / 核实遗留交接前先结束原回合')
                text=path.read_bytes().decode('utf-8')
                try:manifest=json.loads(text.rsplit('```json\n',1)[1].rsplit('\n```',1)[0])
                except (ValueError,IndexError):fail('Invalid checkpoint / 遗留交接格式无效，保留原文件')
                project=db.execute('SELECT project FROM collaborations').fetchone()[0]
                if (manifest.get('pair'),manifest.get('project'),manifest.get('member'),manifest.get('session'))!=(self.pair_id,project,member,session):
                    fail('Checkpoint identity mismatch / 遗留交接不属于本项目及原窗口')
                if manifest.get('binding_version',self._member(db,member).get('binding_version',1))!=self._member(db,member).get('binding_version',1):
                    fail('Checkpoint binding mismatch / 遗留交接属于旧绑定代次')
                for task in manifest.get('tasks',[]):
                    current=db.execute('SELECT * FROM tasks WHERE id=?',(task['id'],)).fetchone()
                    if not current or current['assignee']!=member or clean(dict(current))!=task:
                        fail('Checkpoint task changed / 遗留交接后的任务已有变更，保留文件并人工核实')
                now=time.time();fingerprint=digest(text)
                db.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (member,uuid.uuid4().hex,str(path),fingerprint,text,encoded(clean(manifest)),'saved','{}',None,None,now,now))
                db.execute("UPDATE tasks SET status='quota_wait',updated=? WHERE assignee=? AND status NOT IN ('completed','integrated','delivered','cancelled','handed_back')",(now,member))
                db.execute("UPDATE messages SET process_status='completed',updated=? WHERE recipient=? AND kind='checkpoint'",(now,member))
                self._put(db,'quota_state:'+member,'paused')
                self.event(db,'checkpoint_recovered',member=member,digest=fingerprint)
                return {'recovered':True,'resume_only':True,'digest':fingerprint}
        return self._cleanup_checkpoint(member,fingerprint)

    def _assistance_claimable(self,db,message):
        kind=message['kind'];member=message['recipient']
        if kind=='question':
            q=db.execute('SELECT state FROM delegation_questions WHERE id=?',(message['id'],)).fetchone()
            if q and q[0]!='pending':return False
        if kind=='reply' and message['reply_to']:
            q=db.execute('SELECT state,authorization_version FROM delegation_questions WHERE id=?',(message['reply_to'],)).fetchone()
            if q and (q['state']!='answered' or q['authorization_version']!=self._authorization_version(db)):return False
        if kind=='approval_review':return self.approval_review_ready(db,message)
        if kind=='execution_request':
            r=db.execute('SELECT status FROM execution_requests WHERE message_id=?',(message['id'],)).fetchone()
            return bool(r and r[0]=='review') and self._idle(db,[r[0] for r in db.execute("SELECT id FROM members WHERE lifecycle='active'")],exclude=[message['id']]) and not self._member_suspended(db,member)
        if kind=='checkpoint_resume':
            row=db.execute("SELECT * FROM checkpoints WHERE message_id=? AND status IN ('offered','consumed','transferred') AND recipient=?",(message['id'],member)).fetchone()
            if not row:return False
            q=self._quotas(db).get(member,{})
            return self._healthy(db,member,self._quotas(db)) or q.get('reliability')!='fresh' and self._option(db,'manual_capacity:'+row['generation'],False)
        if kind=='checkpoint':return True
        if self._member_suspended(db,member):return False
        if kind=='task':
            if self._role_handoff_pending(db,member):return False
            if self._checkpoint_read_required(db,member):return False
            if self._checkpoint_path(db,member).is_file():
                row=db.execute('SELECT status FROM checkpoints WHERE member=?',(member,)).fetchone()
                if not row or row['status'] not in ('consumed','transferred'):return False
        return True

    def _assistance_dispatchable(self,db,message):
        if message['kind']=='approval_review':return self.approval_review_ready(db,message)
        if not self._assistance_claimable(db,message):return False
        if not self._idle(db,[message['recipient']],exclude=[message['id']]):return False
        if message['kind']=='execution_request':
            return self._idle(db,[r[0] for r in db.execute("SELECT id FROM members WHERE lifecycle='active'")])
        return True

    def _assistance_complete(self,db,member,message,token,result):
        if message['kind'] in ('execution_request','checkpoint','approval_review'):
            fail('Dedicated completion required / 此消息须使用 finish-execution 或 save-checkpoint 完成')
        if message['kind']=='task':
            task=db.execute('SELECT status,assignee FROM tasks WHERE id=?',(message['task_id'],)).fetchone()
            if task['status']!='active' or task['assignee']!=member:fail('Task suspended or transferred / 任务等待中或已转交，不能声明完成')
            if db.execute("SELECT 1 FROM delegation_questions q JOIN messages m ON m.id=q.id WHERE m.task_id=? AND q.state NOT IN ('adopted','cancelled')",(message['task_id'],)).fetchone():
                fail('Question unresolved / 任务仍有未采用回答或待用户处理的问题')
            if self._approval_unresolved(db,task_id=message['task_id']):
                fail('Approval execution unresolved / 审批或获批操作结果尚未核实')
        if message['kind']=='execution_result':
            r=db.execute('SELECT * FROM execution_requests WHERE result_delivery=?',(message['delivery_id'],)).fetchone()
            if r['status'] not in ('unknown','needs_authorization'):
                db.execute("UPDATE tasks SET status='active' WHERE id=? AND status='waiting_execution'",(r['task_id'],))
        if message['kind']=='checkpoint_resume':
            r=db.execute('SELECT status FROM checkpoints WHERE message_id=?',(message['id'],)).fetchone()
            if not r or r[0] not in ('consumed','transferred'):fail('Checkpoint consumption required / 先完整阅读并持久化接管交接')

    def _assistance_status(self,db):
        approvals=[]
        for row in db.execute('SELECT * FROM approval_requests'):
            approvals.append({k:row[k] for k in ('id','task_id','status','digest','expires','reason','authorization_version','attempt','retry_state')})
            approvals[-1]['coordination_only']=self._coordination_approval(db,row)
        return {'execution_enabled':self._option(db,'execution_assistance',False),
                'delegation_enabled':self._option(db,'delegation_enabled',False),'authorization_version':self._authorization_version(db),
                'questions':[dict(r) for r in db.execute('SELECT * FROM delegation_questions')],
                'approval_enabled':self._option(db,'approval_assistance',False),
                'approval_hook':self._option(db,'approval_hook'),
                'approvals':approvals,
                'checkpoints_enabled':self._option(db,'quota_checkpoints',False),'coordinator':self._coordinator(db),
                'members':{r[0]:{'state':self._option(db,'quota_state:'+r[0],'ready'),'reserve_percent':self._reserve(db,r[0])} for r in db.execute('SELECT id FROM members')},
                'requests':[dict(r) for r in db.execute('SELECT id,task_id,status,digest,result_delivery FROM execution_requests')],
                'checkpoints':[dict(r) for r in db.execute('SELECT member,path,digest,status,recipient FROM checkpoints')]}
