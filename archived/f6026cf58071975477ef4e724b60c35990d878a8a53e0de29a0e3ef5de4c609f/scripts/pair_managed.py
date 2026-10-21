"""受管审核的原生RPC审批适配；不把审核结论视为工具权限。"""
from __future__ import annotations

import json
import time
import uuid

try:
    from .pair_reviewer import fail,safe_evidence
    from .pair_stages import digest,encoded
except ImportError:
    from pair_reviewer import fail,safe_evidence
    from pair_stages import digest,encoded


class ManagedApprovalMixin:
    def _managed_approver_available(self,db):
        authority=self._option(db,'approval_authority','A')
        member=db.execute('SELECT lifecycle,verified,online FROM members WHERE id=?',(authority,)).fetchone()
        return bool(member and member['lifecycle']=='active' and member['verified'] and member['online']
                    and not self._member_suspended(db,authority))

    def _invalidate_approvals(self,db,reason,*,cancel_questions=True):
        super()._invalidate_approvals(db,reason,cancel_questions=cancel_questions)
        for row in db.execute("SELECT * FROM managed_permissions WHERE status IN ('pending','allowed')").fetchall():
            self._expire_managed(db,row,'cancelled')
        # 已发出的批准可能已执行，保留granted/unknown等待准确回执核实。
        db.execute("UPDATE managed_permissions SET retry_state='cancelled' WHERE retry_state IN ('waiting','ready')")

    def _approval_unresolved(self,db,*,task_id=None,grants_only=False):
        states="('granted','unknown')" if grants_only else "('pending','allowed','granted','unknown')"
        return bool(db.execute('SELECT 1 FROM managed_permissions WHERE status IN '+states).fetchone()) or super()._approval_unresolved(db,task_id=task_id,grants_only=grants_only)

    def acquire_reviewer(self,epoch,session,proof):
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM managed_permissions WHERE status IN ('pending','allowed','granted','unknown')").fetchone():
                fail('Managed approval unresolved / 原生工具权限或执行结果未核实，不开始下一审核')
        return super().acquire_reviewer(epoch,session,proof)

    def consume_reviewer_checkpoint(self,member,session,cwd,fingerprint,plan):
        with self.transaction() as db:
            if self._approval_unresolved(db,grants_only=True):fail('Managed execution unresolved / 先核实已批准工具结果，再恢复审核')
        return super().consume_reviewer_checkpoint(member,session,cwd,fingerprint,plan)

    def _expire_managed(self,db,row,retry):
        db.execute("UPDATE managed_permissions SET status='expired',retry_state=? WHERE id=?",(retry,row['id']))
        db.execute("UPDATE messages SET process_status='completed' WHERE id=?",(row['review_message'],))
        if retry=='needs_user':
            a=self._option(db,'approval_authority','A')
            self._send(db,a,a,'受管原生审批连续两次180秒超时，停止自动重试，须用户协调。approval='+row['id'],kind='blocked',dedupe_key='managed-timeout-blocked:'+row['id'])
        self.event(db,'managed_approval_expired',approval=row['id'],notify_user=retry=='needs_user',retry_state=retry)

    def capture_managed_approval(self,epoch,session,execution_id,request):
        safe_evidence(encoded(request))
        method=request.get('method');params=request.get('params',{})
        supported=('item/commandExecution/requestApproval','item/fileChange/requestApproval','item/permissions/requestApproval')
        if method not in supported or params.get('threadId')!=session or not params.get('turnId'):fail('Unsupported managed approval / 不支持的原生审批协议或身份不符')
        with self.transaction() as db:
            row=self._reviewer_row(db)
            if not row or row['epoch']!=epoch or row['session_uuid']!=session or row['execution_id']!=execution_id or row['state']!='running':fail('Stale reviewer / 旧审核会话不能请求审批')
            if db.execute('SELECT status FROM collaborations').fetchone()[0]!='running' or not self._option(db,'approval_assistance',False):fail('Approval disabled / 当前不允许转交审批')
            a=self._option(db,'approval_authority','A')
            if not self._managed_approver_available(db):fail('A unavailable / 原审批者不可用')
            if db.execute("SELECT 1 FROM managed_permissions WHERE status IN ('granted','unknown')").fetchone():fail('Managed execution unresolved / 已批准执行结果未核实，不重放其他请求')
            operation=digest({'method':method,'params':{k:v for k,v in params.items() if k not in ('turnId','itemId','reason','approvalId')}})
            old=db.execute('SELECT * FROM managed_permissions WHERE operation=? AND authorization_version=? ORDER BY created DESC LIMIT 1',(operation,self._authorization_version(db))).fetchone()
            if old and old['status'] in ('pending','allowed','granted','unknown','denied'):fail('Managed operation unresolved / 原生请求仍在途、被拒绝或可能执行，不重放')
            if old and old['status']=='expired' and old['retry_state']!='ready':fail('A must prepare retry / 原生超时也须原 A 核实未执行后安排，连续两次转用户')
            key=uuid.uuid4().hex;fingerprint=digest(request);now=time.time()
            message=self._send(db,a,a,'隐藏审核 CLI 原生单次权限请求，只审核既有用户授权及 A 当前实际权限；不执行请求操作。\n'+encoded(request)+'\napproval='+key+'\ndigest='+fingerprint,
                kind='approval_review',dedupe_key='managed-approval:'+key)
            db.execute('''INSERT INTO managed_permissions(id,epoch,session,execution_id,request,digest,operation,authorization_version,status,expires,review_message,attempt,created)
                VALUES(?,?,?,?,?,?,?,?,'pending',?,?,?,?)''',(key,epoch,session,execution_id,encoded(request),fingerprint,operation,self._authorization_version(db),now+180,message['id'],old['attempt']+1 if old else 1,now))
            return {'id':key,'digest':fingerprint}

    def managed_approval_result(self,key):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM managed_permissions WHERE id=?',(key,)).fetchone()
            if not row:fail('Unknown permission / 原生审批不存在')
            current=self._reviewer_row(db)
            valid=bool(current and current['epoch']==row['epoch'] and current['session_uuid']==row['session'] and current['execution_id']==row['execution_id'] and current['state']=='running'
                and row['authorization_version']==self._authorization_version(db) and self._option(db,'approval_assistance',False)
                and self._managed_approver_available(db)
                and db.execute('SELECT status FROM collaborations').fetchone()[0]=='running')
            if row['status'] in ('pending','allowed') and (not valid or row['expires']<=time.time()):
                retry='waiting' if valid and row['attempt']<2 else ('needs_user' if valid else 'cancelled')
                self._expire_managed(db,row,retry)
                return {'status':'expired','reason':'A did not authorize / 原生请求未获当前有效单次批准'}
            if row['status']=='allowed':
                db.execute("UPDATE managed_permissions SET status='granted' WHERE id=?",(key,))
            return {'status':row['status'],'reason':row['reason']}

    def review_approval(self,member,session,cwd,request_id,token,summary,decision,reason,*,within_permissions=False,capability_evidence=None):
        with self.transaction() as db:
            managed=db.execute('SELECT * FROM managed_permissions WHERE id=?',(request_id,)).fetchone()
            if managed:
                self._a_actor(db,member,session,cwd)
                self._owned_message(db,member,managed['review_message'],token)
                safe_evidence(reason)
                if decision not in ('allow','deny') or managed['digest']!=summary or managed['status']!='pending' or managed['expires']<=time.time():fail('Managed approval changed / 原生审批已处理、过期或摘要不符')
                if managed['authorization_version']!=self._authorization_version(db) or not self._option(db,'approval_assistance',False):fail('Authorization changed / 用户授权已失效')
                if db.execute('SELECT status FROM collaborations').fetchone()[0]!='running' or not self._managed_approver_available(db):fail('Approver unavailable / 当前不可批准')
                current=self._reviewer_row(db)
                if not current or current['epoch']!=managed['epoch'] or current['execution_id']!=managed['execution_id'] or current['state']!='running':fail('Execution changed / 原生审批对应执行已变更')
                if decision=='allow':
                    if not within_permissions or not capability_evidence:fail('Capability evidence required / 必须核对 A 实际权限')
                    safe_evidence(capability_evidence)
                db.execute('UPDATE managed_permissions SET status=?,reason=? WHERE id=?',('allowed' if decision=='allow' else 'denied',reason,request_id))
                db.execute("UPDATE messages SET process_status='completed' WHERE id=?",(managed['review_message'],))
                self.event(db,'managed_permission_reviewed',approval=request_id,decision=decision,evidence=reason,capability=capability_evidence)
                return {'reviewed':True,'decision':decision,'execution_verified':False,'scope':'single_request'}
        return super().review_approval(member,session,cwd,request_id,token,summary,decision,reason,within_permissions=within_permissions,capability_evidence=capability_evidence)

    def prepare_approval_retry(self,member,session,cwd,request_id,reason):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM managed_permissions WHERE id=?',(request_id,)).fetchone()
            if row:
                self._a_actor(db,member,session,cwd);safe_evidence(reason)
                if row['status']!='expired' or row['attempt']>=2 or row['retry_state'] not in ('waiting','ready'):fail('Retry unavailable / 原生请求不可自动重试')
                current=self._reviewer_row(db)
                if current and current['state']=='running':fail('Execution unresolved / 核实原审核工具已结束后再准备重发')
                db.execute("UPDATE managed_permissions SET retry_state='ready',reason=? WHERE id=?",(reason,request_id))
                return {'ready':True,'automatic_replay':False}
        return super().prepare_approval_retry(member,session,cwd,request_id,reason)

    def approval_review_ready(self,db,message):
        if message['kind']=='approval_review':
            row=db.execute('SELECT status,expires FROM managed_permissions WHERE review_message=?',(message['id'],)).fetchone()
            if row:return row['status']=='pending' and row['expires']>time.time() and not self._member_suspended(db,message['recipient'])
        return super().approval_review_ready(db,message)

    def approval_tick(self,db):
        super().approval_tick(db)
        for row in db.execute("SELECT * FROM managed_permissions WHERE status IN ('pending','allowed')").fetchall():
            invalid=row['authorization_version']!=self._authorization_version(db) or not self._option(db,'approval_assistance',False)
            if invalid or row['expires']<=time.time():
                retry='cancelled' if invalid else ('waiting' if row['attempt']<2 else 'needs_user')
                self._expire_managed(db,row,retry)


def permission_response(request,allowed):
    method=request.get('method')
    if method=='item/permissions/requestApproval':
        return {'permissions':request['params'].get('permissions',{}) if allowed else {},'scope':'turn','strictAutoReview':True}
    if method in ('item/commandExecution/requestApproval','item/fileChange/requestApproval'):
        return {'decision':'accept' if allowed else 'decline'}
    return None
