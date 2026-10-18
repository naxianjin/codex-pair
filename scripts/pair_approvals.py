"""通过官方 PermissionRequest Hook 将受限窗口的逐次审批交给 A。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
import uuid

try:
    from .pair_transport import canonical_path,redact_diagnostic
except ImportError:
    from pair_transport import canonical_path,redact_diagnostic


def fail(message):
    try:
        from .pair_core import PairError
    except ImportError:
        from pair_core import PairError
    raise PairError(message)


def dump(value):return json.dumps(value,ensure_ascii=False,sort_keys=True)


def fingerprint(value):return hashlib.sha256(dump(value).encode('utf-8')).hexdigest()


def approval_package_digest(directory):
    # 把代码版本纳入官方信任定义，避免只信任固定脚本路径而静默更换实现。
    return fingerprint({p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(Path(directory).glob('pair*.py'))})


def coordination_lines(command):
    """只拆分引号外的换行；拒绝重定向、管道、变量展开及其他 shell 组合。"""
    lines=[];part=[];quote=None
    for char in command:
        if quote:
            if quote=='"' and char in ('$','`'):return None
            part.append(char)
            if char==quote:quote=None
        elif char in ("'",'"'):
            quote=char;part.append(char)
        elif char in ('\r','\n'):
            if ''.join(part).strip():lines.append(''.join(part).strip())
            part=[]
        elif char in (';','|','&','$','`','<','>','(',')'):
            if char=='&' and not ''.join(part).strip():part.append(char)
            else:return None
        else:part.append(char)
    if quote:return None
    if ''.join(part).strip():lines.append(''.join(part).strip())
    return lines


def hook_response(decision,reason=''):
    result={'behavior':decision}
    if decision=='deny':result['message']=reason or 'A did not authorize this request / A 未批准本次请求'
    return {'hookSpecificOutput':{'hookEventName':'PermissionRequest','decision':result}}


class ApprovalMixin:
    def _coordination_approval(self,db,row):
        """仅识别本脚本的固定协调子命令，避免汇报审批结果又要求汇报自身的循环。"""
        import shlex
        import sys
        try:
            try:from .pair import build_parser
            except ImportError:from pair import build_parser
            event=json.loads(row['event'])
            command=event.get('tool_input',{}).get('command','')
            if not isinstance(command,str) or not command.strip():return False
            member=self._member(db,row['member'])
            safe={'claim','read-message','check-checkpoint','read-delivery','ack-delivery','report-approval','question-state','complete'}
            lines=coordination_lines(command)
            if not lines:return False
            for line in lines:
                line=line.strip()
                if not line:continue
                if line.startswith('& '):line=line[2:]
                words=shlex.split(line,posix=True)
                if len(words)<3 or canonical_path(words[0])!=canonical_path(sys.executable) or canonical_path(words[1])!=canonical_path(Path(__file__).with_name('pair.py')):return False
                args=build_parser().parse_args(words[2:])
                if args.command not in safe or args.member!=row['member'] or args.session!=member['session_uuid']:return False
                if getattr(args,'pair',None)!=self.pair_id or canonical_path(args.state_root)!=canonical_path(self.state_root):return False
                if canonical_path(args.codex_home)!=canonical_path(member['codex_home']) or canonical_path(args.worktree)!=canonical_path(member['cwd']):return False
            return True
        except (Exception,SystemExit):return False

    def _approval_unresolved(self,db,*,task_id=None,grants_only=False):
        states="('granted','unknown')" if grants_only else "('pending','allowed','granted','unknown')"
        rows=db.execute('SELECT * FROM approval_requests WHERE status IN '+states+(' AND task_id=?' if task_id else ''),[task_id] if task_id else []).fetchall()
        return any(r['status']!='granted' or not self._coordination_approval(db,r) for r in rows)

    def _upgrade_delegation(self,db):
        """保留旧审批和令牌；任务关联可空，授权版本独立于调度版本。"""
        with db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE approval_requests_v8(id TEXT PRIMARY KEY,member TEXT NOT NULL REFERENCES members(id),
                task_id TEXT REFERENCES tasks(id),source_message TEXT REFERENCES messages(id),
                review_message TEXT NOT NULL REFERENCES messages(id),event TEXT NOT NULL,digest TEXT NOT NULL,
                status TEXT NOT NULL,expires REAL NOT NULL,reason TEXT,capability_evidence TEXT,
                created REAL NOT NULL,updated REAL NOT NULL,authorization_version INTEGER NOT NULL DEFAULT 1,
                operation_digest TEXT,previous_id TEXT,attempt INTEGER NOT NULL DEFAULT 1,
                retry_state TEXT NOT NULL DEFAULT 'none',retry_message TEXT)''')
            db.execute('''INSERT INTO approval_requests_v8(id,member,task_id,source_message,review_message,event,digest,
                status,expires,reason,capability_evidence,created,updated)
                SELECT id,member,task_id,source_message,review_message,event,digest,status,expires,reason,capability_evidence,created,updated FROM approval_requests''')
            db.execute('DROP TABLE approval_requests')
            db.execute('ALTER TABLE approval_requests_v8 RENAME TO approval_requests')
            for row in db.execute('SELECT id,event,status FROM approval_requests').fetchall():
                event=json.loads(row[1])
                db.execute('UPDATE approval_requests SET operation_digest=? WHERE id=?',
                    (fingerprint({k:event.get(k) for k in ('cwd','tool_name','tool_input')}),row[0]))
                if row[2]=='allowed':db.execute("UPDATE approval_requests SET status='unknown',reason='Legacy allow may have executed / 旧版允许可能已执行，须核实' WHERE id=?",(row[0],))
            db.execute('''CREATE TABLE delegation_questions(id TEXT PRIMARY KEY REFERENCES messages(id),
                state TEXT NOT NULL,authorization_version INTEGER NOT NULL,reason TEXT,updated REAL NOT NULL,
                owner TEXT NOT NULL DEFAULT 'A' REFERENCES members(id))''')
            db.execute('PRAGMA user_version=8')

    def _authorization_version(self,db):return self._option(db,'authorization_version',1)

    def _invalidate_approvals(self,db,reason,*,cancel_questions=True):
        for row in db.execute("SELECT * FROM approval_requests WHERE status IN ('pending','allowed')").fetchall():
            self._expire_approval(db,row,reason,retry=False)
        db.execute("UPDATE approval_requests SET retry_state='cancelled' WHERE retry_state IN ('waiting','ready')")
        if cancel_questions:
            db.execute("UPDATE delegation_questions SET state='cancelled',reason=?,updated=? WHERE state IN ('pending','answered','needs_user')",(reason,time.time()))

    def delegation_policy(self,member,session,cwd,reason,*,enabled=None):
        if not reason.strip() or redact_diagnostic(reason)!=reason:fail('Authorization basis required / 请提供不含凭据的用户授权或变更依据')
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            if member!='A':fail('A required / 仅 A 可登记用户的委托授权变更')
            version=self._authorization_version(db)+1
            self._put(db,'authorization_version',version)
            self._put(db,'delegation_basis',reason)
            self._put(db,'delegated_no_progress',0)
            if enabled is not None:
                self._put(db,'delegation_enabled',enabled)
                self._put(db,'approval_assistance',enabled)
            self._invalidate_approvals(db,'Authorization changed / 用户授权已变更：'+reason)
            self.event(db,'delegation_policy',version=version,enabled=enabled,reason=reason)
            return {'authorization_version':version,'enabled':self._option(db,'delegation_enabled',False),'old_approvals_invalidated':True}

    def _delegation_sent(self,db,message):
        if not self._option(db,'delegation_enabled',False):return
        if message['kind']=='question' and message['sender']!=self._coordinator(db) and not message['dedupe_key'].startswith('window-handshake'):
            db.execute('INSERT INTO delegation_questions(id,state,authorization_version,reason,updated,owner) VALUES(?,?,?,?,?,?)',
                (message['id'],'pending',self._authorization_version(db),None,time.time(),self._coordinator(db)))
        if message['kind']=='reply':
            db.execute("UPDATE delegation_questions SET state='answered',updated=? WHERE id=? AND state='pending'",(time.time(),message['reply_to']))

    def question_state(self,member,session,cwd,question,state,reason):
        if state not in ('adopted','needs_user','cancelled') or not reason.strip():fail('Question outcome required / 请提供问题处理结果及依据')
        if redact_diagnostic(reason)!=reason:fail('Credentials in question / 问题记录不能包含凭据')
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            row=db.execute('SELECT q.*,m.sender,m.recipient FROM delegation_questions q JOIN messages m ON m.id=q.id WHERE q.id=?',(question,)).fetchone()
            if not row:fail('Delegated question required / 找不到委托问题')
            if row['authorization_version']!=self._authorization_version(db) or row['state']=='cancelled':fail('Question cancelled / 问题授权已经失效')
            takeover=member==row['owner']==row['sender'] and row['state']=='pending'
            if state=='adopted' and not takeover and (member!=row['sender'] or row['state'] not in ('answered','adopted')):fail('Answer required / 原提问方确认采用回答；接管者可记录自己接管后的决定')
            if state=='needs_user' and member!=row['owner']:fail('Reviewer required / 仅当前回答方可转交用户')
            db.execute('UPDATE delegation_questions SET state=?,reason=?,updated=? WHERE id=?',(state,reason,time.time(),question))
            self.event(db,'question_outcome',question=question,state=state,reason=reason,notify_user=state=='needs_user')
            return {'question':question,'state':state,'notify_user':state=='needs_user'}

    def _upgrade_approvals(self,db):
        with db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE approval_requests(id TEXT PRIMARY KEY,member TEXT NOT NULL REFERENCES members(id),
                task_id TEXT NOT NULL REFERENCES tasks(id),source_message TEXT NOT NULL REFERENCES messages(id),
                review_message TEXT NOT NULL REFERENCES messages(id),event TEXT NOT NULL,digest TEXT NOT NULL,
                status TEXT NOT NULL,expires REAL NOT NULL,reason TEXT,capability_evidence TEXT,
                created REAL NOT NULL,updated REAL NOT NULL)''')
            db.execute('PRAGMA user_version=7')

    def capture_approval(self,member,event,*,timeout=180):
        if not 1<=timeout<=600:fail('Invalid approval timeout / 审批等待必须在 1～600 秒内')
        if not isinstance(event,dict) or event.get('hook_event_name')!='PermissionRequest':fail('PermissionRequest hook required / 只接收官方权限审批 Hook')
        if not event.get('turn_id') or not isinstance(event.get('tool_name'),str) or 'tool_input' not in event:
            fail('Approval context missing / 缺少回合、工具或完整输入')
        safe={k:event[k] for k in ('session_id','turn_id','cwd','permission_mode','hook_event_name','tool_name','tool_input') if k in event}
        if redact_diagnostic(safe)!=safe:fail('Credentials in approval input / 审批包含认证内容，拒绝复制到协作记录')
        with self.transaction() as db:
            bound=self._member(db,member)
            if member=='A' or event.get('session_id')!=bound['session_uuid']:fail('Approval member mismatch / 审批不属于本轮 B 原会话')
            if not self._option(db,'approval_assistance',False):fail('Approval assistance disabled / 本轮未开启 A 审批')
            if db.execute('SELECT status FROM collaborations').fetchone()[0]!='running':fail('Collaboration not running / 协作已暂停或停止，不批准操作')
            if not isinstance(event.get('cwd'),str) or not Path(event['cwd']).is_absolute():
                fail('Approval cwd missing / 审批必须提供准确的绝对工作目录')
            messages=db.execute("""SELECT m.* FROM messages m JOIN tasks t ON t.id=m.task_id WHERE m.recipient=?
                AND t.assignee=? AND t.status IN ('active','waiting_execution') AND m.kind IN ('task','execution_result')
                AND m.delivery_status='accepted' AND (m.turn_id=? OR m.kind='task' AND m.process_status IN ('unclaimed','claimed'))
                ORDER BY (m.turn_id=?) DESC,m.created DESC""",(member,member,event['turn_id'],event['turn_id'])).fetchall()
            source=messages[0] if messages and len({m['task_id'] for m in messages})==1 else None
            task_id=source['task_id'] if source else None
            operation=fingerprint(dict({k:safe[k] for k in ('cwd','tool_name','tool_input')},member=member))
            version=self._authorization_version(db)
            previous=db.execute('SELECT * FROM approval_requests WHERE operation_digest=? AND authorization_version=? ORDER BY created DESC LIMIT 1',(operation,version)).fetchone()
            if previous and previous['status'] in ('pending','allowed'):fail('Approval already waiting / 相同操作仍在等待，不能重复申请')
            if previous and previous['status'] in ('denied','granted','unknown'):fail('Operation requires reconciliation / 操作已被拒绝或可能执行，先核实并登记用户授权变更')
            if previous and previous['status']=='expired' and previous['retry_state']!='ready':
                fail('A must arrange retry / 原请求已失效，须由 A 核实未执行后安排重新请求')
            continuing=previous and previous['status']=='expired' and previous['retry_state']=='ready'
            request_id=uuid.uuid4().hex;now=time.time();summary=fingerprint(safe)
            body=(member+' 的原窗口在官方 PermissionRequest Hook 中等待本次审批，尚未执行操作。'
                  '你是 A，仅进行只读审核，不执行请求命令，不修改业务文件。核对原用户授权、当前项目、完整命令或补丁、目录、副作用及你当前回合的实际权限。任务关联可为空，不能把无任务记录当作新增授权；项目外但属于已授权任务的操作可审核。'
                  '只有既有授权明确且操作不超过 A 当前权限才允许；权限不清、超范围、明确拒绝或系统禁止必须拒绝。'
                  '使用 review-approval --approval '+request_id+' --digest '+summary+
                  ' --token <本审核消息领取令牌> --decision allow/deny --body-file <审核依据>；允许时还须 --within-a-permissions --capability-evidence <A当前实际权限依据>。'
                  '不能用普通 complete 代替，不新增通配授权，不修改 B 沙箱。\n完整审批输入：\n'+dump(safe))
            body+='\n当前委托依据：'+dump({'project':db.execute('SELECT project FROM collaborations').fetchone()[0],
                'goal':self._option(db,'window_flow',{}).get('goal'),'basis':self._option(db,'delegation_basis',None),'authorization_version':version})
            message=self._send(db,member,'A',body,kind='approval_review',task_id=task_id,dedupe_key='approval:'+request_id)
            db.execute('''INSERT INTO approval_requests(id,member,task_id,source_message,review_message,event,digest,status,expires,created,updated,
                authorization_version,operation_digest,previous_id,attempt) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (request_id,member,task_id,source['id'] if source else None,message['id'],dump(safe),summary,'pending',now+timeout,now,now,
                 version,operation,previous['id'] if continuing else None,previous['attempt']+1 if continuing else 1))
            if continuing:db.execute("UPDATE approval_requests SET retry_state='requested' WHERE id=?",(previous['id'],))
            hook_key='approval_hook' if member=='B' else 'approval_hook:'+member
            hook=self._option(db,hook_key,{})
            hook.update(status='callback_observed',last_callback=now)
            self._put(db,hook_key,hook)
            if self._member_suspended(db,'A'):
                self.event(db,'approval_requires_user',approval=request_id,reason='A quota unavailable / A 额度不足，请用户处理',notify_user=True)
                self._expire_approval(db,db.execute('SELECT * FROM approval_requests WHERE id=?',(request_id,)).fetchone(),
                    'A unavailable / A 额度不足，需要用户处理本次权限请求',retry=False)
            self.event(db,'approval_captured',approval=request_id,member=member,task=task_id,digest=summary)
            return {'id':request_id,'digest':summary,'expires':now+timeout,'message_id':message['id']}

    def approval_outcome(self,member,session,request_id,summary):
        with self.transaction() as db:
            bound=self._member(db,member)
            row=db.execute('SELECT * FROM approval_requests WHERE id=?',(request_id,)).fetchone()
            if not row or row['member']!=member or bound['session_uuid']!=session or row['digest']!=summary:
                fail('Approval identity mismatch / 审批归属或摘要不符')
            invalid=(db.execute('SELECT status FROM collaborations').fetchone()[0]!='running'
                     or not self._option(db,'approval_assistance',False) or row['authorization_version']!=self._authorization_version(db))
            if row['task_id']:
                task=db.execute('SELECT assignee,status FROM tasks WHERE id=?',(row['task_id'],)).fetchone()
                invalid=invalid or not task or task['assignee']!=row['member'] or task['status'] not in ('active','waiting_execution')
            if row['source_message']:
                source=db.execute('SELECT turn_id,remote_status FROM messages WHERE id=?',(row['source_message'],)).fetchone()
                if source and source['turn_id']==json.loads(row['event'])['turn_id'] and source['remote_status'] not in ('queued','inProgress'):invalid=True
            if row['status'] in ('pending','allowed') and (invalid or time.time()>=row['expires']):
                self._expire_approval(db,row,'A review timed out or authorization changed / A 审核超时或授权已变更',retry=not invalid)
                row=db.execute('SELECT * FROM approval_requests WHERE id=?',(request_id,)).fetchone()
            if row['status']=='allowed':
                db.execute("UPDATE approval_requests SET status='granted',updated=? WHERE id=?",(time.time(),row['id']))
                self.event(db,'approval_granted',approval=row['id'],execution_verified=False)
            return {'status':row['status'],'reason':row['reason'],'digest':row['digest']}

    def _expire_approval(self,db,row,reason,*,retry=True):
        # 已允许但尚未返回的请求也可以失效；已返回的 granted 绝不伪称撤回。
        state=('waiting' if row['attempt']<2 else 'needs_user') if retry else 'cancelled'
        db.execute("UPDATE approval_requests SET status='expired',retry_state=?,reason=?,updated=? WHERE id=?",(state,reason,time.time(),row['id']))
        db.execute("UPDATE messages SET process_status='completed',updated=? WHERE id=?",(time.time(),row['review_message']))
        self.event(db,'approval_expired',approval=row['id'],reason=reason,notify_user=state=='needs_user')
        if state in ('waiting','needs_user'):
            body=('B 的审批已超时，未获允许。先核对 B 原回合结束且操作未执行，准备好审核后用 prepare-approval-retry 安排 B 重新发起；不能执行原命令或复用旧允许。'
                  if state=='waiting' else 'B 的同一操作已连续两次审批超时。停止自动重试，保留任务和请求，向用户报告阻塞。')
            self._send(db,row['member'],'A',body+'\napproval='+row['id'],kind='note',dedupe_key='approval-expired:'+row['id'])

    def prepare_approval_retry(self,member,session,cwd,request_id,reason):
        if not reason.strip() or redact_diagnostic(reason)!=reason:fail('Reconciliation required / 必须记录不含凭据的未执行核查证据')
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            row=db.execute('SELECT * FROM approval_requests WHERE id=?',(request_id,)).fetchone()
            if member!='A' or not row or row['status']!='expired' or row['retry_state'] not in ('waiting','ready') or row['attempt']>=2:
                fail('Retry unavailable / 仅 A 可安排尚未连续两次超时的请求')
            if row['authorization_version']!=self._authorization_version(db) or not self._option(db,'approval_assistance',False):fail('Authorization changed / 原请求授权已失效')
            if db.execute('SELECT status FROM collaborations').fetchone()[0]!='running' or self._member_suspended(db,'A'):fail('A not ready / A 尚未恢复审核能力')
            if not self._idle(db,[row['member']]):fail('B turn unresolved / B 原回合未结束，不能安排重试')
            if row['retry_state']=='ready':return {'ready':True,'message_id':row['retry_message'],'duplicate':True}
            message=self._send(db,'A',row['member'],'A 已核实旧请求未执行并准备审核。先核对当前用户授权、现场及任务归属，再由原窗口重新发起操作以产生新审批；禁止复用旧允许或重做已执行操作。\n核查依据：'+reason+'\n原请求：'+row['event'],
                kind='note',task_id=row['task_id'],dedupe_key='approval-retry:'+row['id'])
            db.execute("UPDATE approval_requests SET retry_state='ready',retry_message=?,updated=? WHERE id=?",(message['id'],time.time(),row['id']))
            self.event(db,'approval_retry_ready',approval=row['id'],reason=reason)
            return {'ready':True,'message_id':message['id'],'execution_verified':False}

    def review_approval(self,member,session,cwd,request_id,token,summary,decision,reason,*,within_permissions=False,capability_evidence=None):
        if decision not in ('allow','deny') or not reason.strip():fail('Approval decision required / 必须明确允许或拒绝并记录依据')
        if decision=='allow' and (not within_permissions or not capability_evidence or not capability_evidence.strip()):
            fail('A capability evidence required / 允许前必须核实 A 当前权限并记录依据')
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            row=db.execute('SELECT * FROM approval_requests WHERE id=?',(request_id,)).fetchone()
            if member!='A' or not row:fail('A reviewer required / 只能由本轮 A 审核')
            self._owned_message(db,member,row['review_message'],token)
            if row['digest']!=summary:fail('Approval digest mismatch / 待审核操作摘要已变化')
            if row['status']!='pending' or time.time()>=row['expires']:fail('Approval no longer pending / 请求已处理或已过期，不能补发允许')
            if row['authorization_version']!=self._authorization_version(db) or not self._option(db,'approval_assistance',False):fail('Authorization changed / 审批对应的授权已变更')
            if decision=='allow' and self._member_suspended(db,'A'):fail('A unavailable / A 额度不足，须向用户报告，不批准新操作')
            if db.execute('SELECT status FROM collaborations').fetchone()[0]!='running':fail('Collaboration not running / 协作暂停或停止时不能批准')
            source=db.execute('SELECT * FROM messages WHERE id=?',(row['source_message'],)).fetchone()
            event=json.loads(row['event'])
            task=db.execute('SELECT assignee,status FROM tasks WHERE id=?',(row['task_id'],)).fetchone() if row['task_id'] else None
            if row['task_id'] and (not task or task['assignee']!=row['member'] or task['status'] not in ('active','waiting_execution')):
                fail('Approval task changed / 原任务已结束或已转交，不能批准旧操作')
            if source and source['turn_id']==event['turn_id'] and source['remote_status'] not in ('queued','inProgress'):
                fail('Original turn ended or changed / 原审批回合已经结束或变更，不能批准旧操作')
            status='allowed' if decision=='allow' else 'denied'
            db.execute('UPDATE approval_requests SET status=?,reason=?,capability_evidence=?,updated=? WHERE id=?',
                (status,redact_diagnostic(reason),redact_diagnostic(capability_evidence),time.time(),request_id))
            db.execute("UPDATE messages SET process_status='completed',updated=? WHERE id=?",(time.time(),row['review_message']))
            self.event(db,'approval_reviewed',approval=request_id,decision=decision,reviewer_session=session,
                reason=redact_diagnostic(reason),capability_evidence=redact_diagnostic(capability_evidence))
            return {'reviewed':True,'approval':request_id,'decision':decision,'execution_verified':False,'scope':'single_request'}

    def approval_review_ready(self,db,message):
        if message['kind']!='approval_review':return False
        row=db.execute('SELECT * FROM approval_requests WHERE review_message=?',(message['id'],)).fetchone()
        if not row or row['status']!='pending' or row['expires']<=time.time():return False
        earlier=db.execute("SELECT 1 FROM approval_requests WHERE status='pending' AND (created<? OR created=? AND id<?)",(row['created'],row['created'],row['id'])).fetchone()
        return not earlier and message['recipient']=='A' and not self._member_suspended(db,'A')

    def approval_tick(self,db):
        for row in db.execute("SELECT * FROM approval_requests WHERE status IN ('pending','allowed')").fetchall():
            source=db.execute('SELECT remote_status,turn_id FROM messages WHERE id=?',(row['source_message'],)).fetchone()
            event=json.loads(row['event'])
            task=db.execute('SELECT assignee,status FROM tasks WHERE id=?',(row['task_id'],)).fetchone() if row['task_id'] else None
            invalid=(row['authorization_version']!=self._authorization_version(db) or not self._option(db,'approval_assistance',False)
                     or row['task_id'] and (not task or task['assignee']!=row['member'] or task['status'] not in ('active','waiting_execution')))
            if invalid or time.time()>=row['expires'] or source and source['turn_id']==event['turn_id'] and source['remote_status'] not in ('queued','inProgress'):
                self._expire_approval(db,row,'Approval expired or original turn ended / 审批过期或原回合已结束',retry=not invalid)

    def report_approval(self,member,session,cwd,request_id,outcome,evidence):
        if outcome not in ('success','failed','unknown') or not evidence.strip() or redact_diagnostic(evidence)!=evidence:
            fail('Execution evidence required / 请记录不含凭据的退出码、产物或未知状态证据')
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            row=db.execute('SELECT * FROM approval_requests WHERE id=?',(request_id,)).fetchone()
            if member=='A' or not row or row['member']!=member or row['status']!='granted':fail('Granted request required / 只能由 B 记录已交给原 CLI 的操作结果')
            db.execute('UPDATE approval_requests SET status=?,reason=?,updated=? WHERE id=?',(outcome,evidence,time.time(),request_id))
            self.event(db,'approved_operation_result',approval=request_id,outcome=outcome,evidence=evidence)
            return {'approval':request_id,'outcome':outcome,'evidence_recorded':True,'independently_verified':False}

    def record_hook_status(self,listing,*,target='B'):
        """只保存 hooks/list 白名单字段，不把安装当作信任。"""
        with self.transaction() as db:
            configured=self._option(db,'approval_hook' if target=='B' else 'approval_hook:'+target,{})
            marker='codex-pair:'+self.pair_id+':'+target
            hooks=[h for entry in listing.get('data',[]) for h in entry.get('hooks',[]) if h.get('statusMessage')==marker]
            hooks.extend(h for h in listing.get('hooks',[]) if h.get('statusMessage')==marker)
            trusted=bool(hooks) and all(h.get('enabled') and h.get('eventName')=='permissionRequest' and h.get('trustStatus') in ('trusted','managed') for h in hooks)
            if any(entry.get('errors') for entry in listing.get('data',[])):trusted=False
            status='trusted' if trusted else ('installed_requires_trust' if hooks else 'missing_or_unsupported')
            configured.update(status=status,checked_at=time.time(),hooks=[{k:h.get(k) for k in ('key','enabled','trustStatus','currentHash')} for h in hooks])
            self._put(db,'approval_hook' if target=='B' else 'approval_hook:'+target,configured)
            return {'status':status,'trust_required':bool(hooks) and not trusted,'active_verified':False,
                    'detail':'较低权限的请求窗口请在 Hook 安装后输入 /hooks，审阅并信任 codex-pair 对应项；通信成功不代表自动审批就绪。'}

    def install_approval_hook(self,member,session,cwd,*,python,script,timeout=180,target='B'):
        """只安装已授权的逐请求回调，不修改沙箱、审批策略或信任记录。"""
        import os
        import subprocess
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            if member!='A':fail('A required / 仅 A 可配置本轮审批回调')
            if target not in ('B','C','D'):fail('Invalid hook target / 审批目标必须是 B/C/D')
            b=self._member(db,target);a=self._member(db,'A')
            if any(canonical_path(r[0])==canonical_path(b['codex_home']) for r in db.execute('SELECT codex_home FROM members WHERE id<>?',(target,))):fail('Separate config homes required / 多人审批要求各成员配置目录独立')
            if canonical_path(b['codex_home'])==canonical_path(a['codex_home']):fail('Separate config homes required / 当前实现要求 A/B 使用不同配置目录，防止审核递归')
        if not 1<=timeout<=600:fail('Invalid hook timeout / Hook 等待须为 1～600 秒')
        path=Path(b['codex_home'])/'hooks.json'
        if path.resolve()!=path.absolute():fail('Redirected hook config / Hook 配置存在重定向')
        args=[str(Path(python).resolve(strict=True)),str(Path(script).resolve(strict=True)),
              '--pair',self.pair_id,'--state-root',str(self.state_root),'--member',target,'--timeout',str(timeout),
              '--session',b['session_uuid'],'--package-digest',approval_package_digest(Path(script).parent)]
        command=subprocess.list2cmdline(args) if os.name=='nt' else __import__('shlex').join(args)
        marker='codex-pair:'+self.pair_id+':'+target
        group={'matcher':'*','hooks':[{'type':'command','command':command,'timeout':timeout+60,'statusMessage':marker}]}
        previous=path.read_bytes() if path.exists() else None
        data=json.loads(previous.decode('utf-8-sig')) if previous else {}
        groups=data.setdefault('hooks',{}).setdefault('PermissionRequest',[])
        existing=[g for g in groups if any(h.get('statusMessage')==marker for h in g.get('hooks',[]))]
        changed=existing!=[group]
        if changed:
            if previous:
                backup=path.with_name('hooks.codex-pair-backup-'+uuid.uuid4().hex+'.json')
                with backup.open('xb') as file:file.write(previous)
            # 只更新本轮拥有的定义；命令包含包摘要，更新后由 Codex 重新核验信任。
            kept=[]
            for old in groups:
                handlers=[h for h in old.get('hooks',[]) if not (
                    h.get('statusMessage')==marker or str(h.get('statusMessage','')).startswith('codex-pair:')
                    and str(h.get('statusMessage','')).endswith(':'+target) and 'pair_permission_hook.py' in h.get('command',''))]
                if handlers:kept.append(dict(old,hooks=handlers))
            groups[:]=kept
            groups.append(group)
            temporary=path.with_name('hooks.codex-pair-'+uuid.uuid4().hex+'.tmp')
            try:
                with temporary.open('x',encoding='utf-8') as file:
                    file.write(json.dumps(data,ensure_ascii=False,indent=2)+'\n');file.flush();os.fsync(file.fileno())
                os.replace(temporary,path)
            finally:
                if temporary.exists():temporary.unlink()
        with self.transaction() as db:
            if existing and changed:
                self._put(db,'authorization_version',self._authorization_version(db)+1)
                self._invalidate_approvals(db,'Hook definition updated / 审批 Hook 定义已更新，旧请求失效',cancel_questions=False)
                db.execute("UPDATE delegation_questions SET authorization_version=? WHERE state IN ('pending','answered','needs_user')",(self._authorization_version(db),))
            self._put(db,'approval_assistance',True)
            self._put(db,'approval_hook' if target=='B' else 'approval_hook:'+target,{'path':str(path),'status':'installed_requires_trust','session':b['session_uuid']})
            self.event(db,'approval_hook_installed',member=target,path=str(path),trust_bypassed=False)
        return {'installed':True,'path':str(path),'trust_required':True,'active_verified':False,'sandbox_changed':False,
                'detail':'Official hooks require exact-definition trust; installation alone does not activate it / 需在请求窗口信任这一个 Hook，安装不等于已生效'}
