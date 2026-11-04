"""隐藏审核者主动交接；原执行结算、A接收、继任者完整读取分别登记。"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import queue
import time
import uuid

try:
    from .pair_reviewer import fail, safe_evidence
    from .pair_recovery import encoded, digest
    from .pair_transport import TransportError, redact_diagnostic
except ImportError:
    from pair_reviewer import fail, safe_evidence
    from pair_recovery import encoded, digest
    from pair_transport import TransportError, redact_diagnostic


HANDOFF_TOOL={'name':'pair_review_handoff','description':'请求主动交接，或分页读取并确认A指定的交接；不会授予业务执行或审批权限。',
    'inputSchema':{'type':'object','properties':{'action':{'enum':['request','read','consume']},
        'progress':{'type':'number'},'body':{'type':'string'},'handoff_id':{'type':'string'},
        'offset':{'type':'integer'},'digest':{'type':'string'},'plan':{'type':'string'}},'required':['action'],'additionalProperties':False}}


def identity(store,db,kind,key):
    row=store._reviewer_row(db) if kind=='reviewer' else db.execute('SELECT * FROM auxiliary_sessions WHERE id=?',(key,)).fetchone()
    if not row or not row['session_uuid']:fail('Hidden identity unavailable')
    return {'kind':kind,'id':'reviewer' if kind=='reviewer' else key,'session':row['session_uuid'],'epoch':row['epoch']},dict(row)


def pending(store,db,subject):
    key=store._option(db,'hidden_handoff_current:'+subject['session'])
    row=store._option(db,'hidden_handoff:'+key) if key else None
    return row if row and row['state'] in ('requested','saved','accepted') else None


def blocked(store,db,subject):
    if pending(store,db,subject):return True
    for row in db.execute("SELECT value FROM pair_settings WHERE key LIKE 'hidden_handoff:%'"):
        value=json.loads(row[0])
        if value.get('successor')==subject and value['state']!='consumed':return True
    return False


def _work(store,db,record):
    task_id=record['business_task']
    return {'requirements_version':store._option(db,'requirements_version',1),
        'stages':[dict(row) for row in db.execute('SELECT id,task_id,digest,status,requirements_version FROM review_stages WHERE task_id=? ORDER BY id',(task_id,))],
        'tasks':[dict(row) for row in db.execute('SELECT id,status,requirements_version,task_kind FROM tasks WHERE id=?',(task_id,))],
        'repair_links':[dict(row) for row in db.execute('SELECT l.* FROM repair_links l JOIN review_issues i ON i.id=l.issue_id WHERE i.root_task=? OR l.task_id=? ORDER BY l.task_id',(task_id,task_id))]}


def task_gate(store,db,task_id):
    for row in db.execute("SELECT value FROM pair_settings WHERE key LIKE 'hidden_handoff:%'"):
        record=json.loads(row[0])
        if record.get('successor_task',record.get('auxiliary_task'))==task_id and record['state'] in ('requested','saved','read_confirmed'):
            return record['id']
        if record.get('successor_task',record.get('auxiliary_task'))==task_id and record['state']=='accepted' and record.get('successor'):
            return record['id']
    return None


def handle_tool(store,subject,arguments,*,execution=None):
    if not isinstance(arguments,dict):fail('Handoff object required')
    action=arguments.get('action')
    with store.transaction() as db:
        current,source=identity(store,db,subject['kind'],subject['id'])
        if current!=subject:fail('Hidden handoff identity changed')
        if action=='request':
            progress=arguments.get('progress');body=arguments.get('body');safe_evidence(body)
            if type(progress) not in (int,float) or not math.isfinite(progress) or not 0<=progress<=100:fail('Exact progress required')
            if subject['kind']=='reviewer':
                owned=source['state']=='running' and source['execution_id']==execution
                stage=db.execute('SELECT task_id FROM review_stages WHERE execution_id=?',(execution,)).fetchone()
                binding={'business_task':stage['task_id'] if stage else None,'auxiliary_task':None,'candidate':None}
            else:
                run=db.execute("SELECT * FROM auxiliary_executions WHERE id=? AND reviewer_id=? AND epoch=? AND session_uuid=? AND state='running'",
                    (execution,subject['id'],subject['epoch'],subject['session'])).fetchone()
                owned=bool(run)
                task=store._auxiliary_task(db,run['task_id']) if run else None
                candidate=store._auxiliary_candidate(db,task['candidate_id']) if task else None
                stage=db.execute('SELECT task_id FROM review_stages WHERE id=?',(candidate['stage_id'],)).fetchone() if candidate else None
                binding={'business_task':stage['task_id'] if stage else None,'auxiliary_task':task['id'] if task else None,'candidate':candidate['id'] if candidate else None}
            if not owned:fail('Owned hidden execution required')
            if not binding['business_task']:fail('Exact hidden work binding required')
            old=pending(store,db,subject)
            if old:return {'id':old['id'],'state':old['state'],'duplicate':True}
            key=uuid.uuid4().hex
            record={'id':key,'source':subject,'execution':execution,'progress':progress,'body':body,
                'mode':'restart' if progress<=50 else 'finish_current','state':'requested','created':time.time(),'reads':{},
                'config_id':source.get('config_id'),'successor':None,**binding}
            store._put(db,'hidden_handoff:'+key,record);store._put(db,'hidden_handoff_current:'+subject['session'],key)
            store._send(db,'A','A','隐藏审核者请求交接；原执行终态确认后接收。handoff='+key,
                kind='note',dedupe_key='hidden-handoff-request:'+key)
            store.event(db,'hidden_handoff_requested',handoff=key,source=subject,execution=execution,progress=progress)
            return {'id':key,'state':'requested','stop_new_work':True,'finish_current':progress>50}
        record=store._option(db,'hidden_handoff:'+str(arguments.get('handoff_id')))
        if not record or record.get('successor')!=subject or record['state'] not in ('accepted','read_confirmed','consumed'):fail('Exact successor required')
        text=_document(store,db,record)
        reader=digest(subject)
        if action=='read':
            offset=arguments.get('offset',0)
            if type(offset) is not int or offset<0 or offset>record['reads'].get(reader,0):fail('Sequential handoff pages required')
            end=min(len(text),offset+2000);record['reads'][reader]=max(end,record['reads'].get(reader,0))
            store._put(db,'hidden_handoff:'+record['id'],record)
            return {'text':text[offset:end],'digest':record['digest'],'next_offset':end if end<len(text) else None}
        if action=='consume':
            safe_evidence(arguments.get('plan'))
            if arguments.get('digest')!=record['digest'] or record['reads'].get(reader,0)<len(text):fail('Complete exact handoff read required')
            if _work(store,db,record)!=record['work']:fail('Handoff work changed / 交接后现场变化，先由A重新核对')
            record.update(state='read_confirmed',read_confirmed_at=time.time(),successor_plan=arguments['plan'])
            store._put(db,'hidden_handoff:'+record['id'],record)
            store.event(db,'hidden_handoff_consumed',handoff=record['id'],successor=subject,digest=record['digest'])
            return {'read_confirmed':True,'original_turn_terminal_required':True,'past_verdicts_unchanged':True,'contributions_retained':True}
        fail('Unsupported handoff action')


def _document(store,db,record):
    path=store._handoff_path(db,'hidden-'+record['id'],record.get('revision',1))
    if path.resolve()!=path.absolute() or not path.is_file():fail('Hidden handoff document missing')
    text=path.read_text(encoding='utf-8')
    if hashlib.sha256(text.encode()).hexdigest()!=record['digest']:fail('Hidden handoff document changed')
    return text


def seal(store,subject,*,receipt):
    """仅由取得原执行终态的控制器调用；不由模型JSON确认结束。"""
    safe_evidence(receipt)
    with store.transaction() as db:
        record=pending(store,db,subject)
        if not record or record['state']!='requested':return
        current,source=identity(store,db,subject['kind'],subject['id'])
        if current!=subject or source['state']!='idle':fail('Original hidden executor not idle')
        if subject['kind']=='reviewer':
            if source['owner'] or db.execute("SELECT 1 FROM managed_permissions WHERE status IN ('pending','allowed','granted','unknown')").fetchone():fail('Original hidden permission unresolved')
        elif db.execute("SELECT 1 FROM auxiliary_executions WHERE reviewer_id=? AND state IN ('reserved','starting','running','unknown')",(subject['id'],)).fetchone():fail('Original auxiliary execution unresolved')
        work=_work(store,db,record)
        text='# 隐藏审核交接\n\n'+record['body']+'\n\n```json\n'+encoded({'source':subject,'execution':record['execution'],'receipt':receipt,'work':work})+'\n```\n'
        path=store._handoff_path(db,'hidden-'+record['id'])
        if path.exists():
            if path.read_text(encoding='utf-8')!=text:fail('Existing hidden handoff differs')
        else:
            import os
            with path.open('x',encoding='utf-8',newline='\n') as stream:stream.write(text);stream.flush();os.fsync(stream.fileno())
        record.update(state='saved',path=str(path),digest=hashlib.sha256(text.encode()).hexdigest(),work=work,reads={},terminal_receipt=receipt)
        store._put(db,'hidden_handoff:'+record['id'],record)
        store._send(db,'A','A','隐藏审核交接已保存，完整读取后确认接收。handoff='+record['id'],kind='note',dedupe_key='hidden-handoff-saved:'+record['id'])
        store.event(db,'hidden_handoff_saved',handoff=record['id'],digest=record['digest'],source=subject)


class HiddenHandoffMixin:
    def read_hidden_handoff(self,member,session,cwd,handoff_id,offset=0):
        with self.transaction() as db:
            actor=self._a_actor(db,member,session,cwd);record=self._option(db,'hidden_handoff:'+handoff_id)
            if not record or record['state']=='requested':fail('Hidden handoff not saved')
            text=_document(self,db,record);reader='A:'+str(actor['binding_version'])
            if type(offset) is not int or offset<0 or offset>record['reads'].get(reader,0):fail('Sequential handoff pages required')
            end=min(len(text),offset+2000);record['reads'][reader]=max(end,record['reads'].get(reader,0))
            self._put(db,'hidden_handoff:'+handoff_id,record)
            return {'text':text[offset:end],'digest':record['digest'],'next_offset':end if end<len(text) else None}

    def accept_hidden_handoff(self,member,session,cwd,handoff_id,expected_digest,plan,successor_task=None):
        safe_evidence(plan)
        with self.transaction() as db:
            actor=self._a_actor(db,member,session,cwd);record=self._option(db,'hidden_handoff:'+handoff_id)
            if not record or record['state']!='saved':fail('Saved hidden handoff required')
            text=_document(self,db,record)
            if expected_digest!=record['digest'] or record['reads'].get('A:'+str(actor['binding_version']),0)<len(text):fail('A must read complete exact handoff')
            source,row=identity(self,db,record['source']['kind'],record['source']['id'])
            if source!=record['source'] and record.get('source_retired') and record['source']['kind']=='reviewer':
                historical=db.execute('SELECT snapshot FROM reviewer_history WHERE epoch=?',(record['source']['epoch'],)).fetchone()
                original=json.loads(historical['snapshot']) if historical else {}
                if original.get('session_uuid')!=record['source']['session']:fail('Original reviewer retirement evidence missing')
                source=record['source'];row=dict(original,state='retired',owner=None)
            if source!=record['source'] or row['state'] not in ('idle','retired') or _work(self,db,record)!=record['work']:fail('Hidden handoff source changed')
            if source['kind']=='reviewer':
                if row['owner'] or db.execute("SELECT 1 FROM managed_permissions WHERE status IN ('pending','allowed','granted','unknown')").fetchone():fail('Original permission unresolved')
                if row['state']!='retired':db.execute('INSERT INTO reviewer_history VALUES(?,?,?,?)',(source['epoch'],encoded(row),plan,time.time()))
                db.execute("UPDATE reviewer_runtime SET state='retired',updated=? WHERE id=1 AND epoch=?",(time.time(),source['epoch']))
                self._put(db,'reviewer_replacement_evidence','已完整接收隐藏交接 '+handoff_id+'；'+plan)
            else:
                if db.execute("SELECT 1 FROM auxiliary_executions WHERE reviewer_id=? AND state IN ('reserved','starting','running','unknown')",(source['id'],)).fetchone():fail('Original auxiliary execution unresolved')
                next_task=self._auxiliary_task(db,successor_task or record['auxiliary_task'])
                if next_task['candidate_id']!=record['candidate'] or next_task['status'] not in ('pending','waiting'):fail('Exact pending successor task required / A须指定同候选的准确待处理接续任务')
                record['successor_task']=next_task['id']
                db.execute("UPDATE auxiliary_sessions SET state='retired',updated=? WHERE id=?",(time.time(),source['id']))
            record.update(state='accepted',a_plan=plan,accepted_at=time.time(),source_retired=True)
            self._put(db,'hidden_handoff:'+handoff_id,record)
            self.event(db,'hidden_handoff_accepted',handoff=handoff_id,source=source,history_retained=True)
            return {'accepted':True,'successor_must_read':True,'source_retired':True,'model_unchanged':True}

    def reconcile_hidden_handoff(self,member,session,cwd,handoff_id,executable):
        try:from .pair_transport import AppServer
        except ImportError:from pair_transport import AppServer
        with self.transaction() as db:
            self._a_actor(db,member,session,cwd);record=self._option(db,'hidden_handoff:'+handoff_id)
            if not record or not record.get('successor'):fail('Hidden successor missing')
            subject,row=identity(self,db,record['successor']['kind'],record['successor']['id'])
            if subject!=record['successor']:fail('Hidden successor changed')
            if subject['kind']=='reviewer':home=row['codex_home']
            else:
                config,source=self._auxiliary_config(db,row['config_id']);home=source['codex_home']
        with AppServer(codex_home=home,cwd=row['cwd'],executable=executable,metadata_only=True) as client:
            client.validate({'session_uuid':subject['session'],'cwd':row['cwd']})
            result=reconcile_consumption(self,client,subject,handoff_id)
        if subject['kind']=='auxiliary' and result['terminal']:
            with self.transaction() as db:
                current=self._option(db,'hidden_handoff:'+handoff_id)
                execution=db.execute('SELECT * FROM auxiliary_executions WHERE id=?',(current.get('preparation_execution'),)).fetchone()
                if (execution and execution['state']=='unknown' and execution['turn_id'] is None
                        and execution['session_uuid']==subject['session'] and execution['epoch']==subject['epoch']):
                    if result['state']=='consumed':
                        proof={'session':subject['session'],'epoch':subject['epoch'],'handoff':handoff_id,'terminal':current['resume_terminal']}
                        self._put(db,'auxiliary_preparation_ready:'+execution['id'],proof)
                        db.execute("UPDATE auxiliary_executions SET state='reserved',updated=? WHERE id=?",(time.time(),execution['id']))
                        db.execute("UPDATE auxiliary_sessions SET state='reserved',updated=? WHERE id=?",(time.time(),subject['id']))
                        db.execute("UPDATE auxiliary_tasks SET status='reserved' WHERE id=? AND last_execution=?",(execution['task_id'],execution['id']))
                    else:
                        self._end_auxiliary(db,execution,'suspended',{'handoff_preparation_terminal':current['resume_terminal'],'business_not_started':True})
                        db.execute("UPDATE auxiliary_tasks SET status='waiting' WHERE id=? AND last_execution=?",(execution['task_id'],execution['id']))
                    result.update(preparation_reconciled=True,execution_id=execution['id'],business_turn_not_started=True,refresh_required=result['state']!='consumed')
        return result

    def refresh_hidden_handoff(self,member,session,cwd,handoff_id,reason):
        safe_evidence(reason)
        with self.transaction() as db:
            self._a_actor(db,member,session,cwd);record=self._option(db,'hidden_handoff:'+handoff_id)
            if not record or record['state'] not in ('saved','accepted') or record.get('resume_attempt') and not record.get('resume_terminal'):fail('Original handoff unresolved')
            _document(self,db,record)
            work=_work(self,db,record);revision=record.get('revision',1)+1
            text='# 隐藏审核交接\n\n'+record['body']+'\n\n重新核对：'+reason+'\n\n```json\n'+encoded({'source':record['source'],'execution':record['execution'],'work':work,'terminal_receipt':record['terminal_receipt']})+'\n```\n'
            path=self._handoff_path(db,'hidden-'+handoff_id,revision)
            with path.open('x',encoding='utf-8',newline='\n') as stream:stream.write(text)
            record.setdefault('previous_revisions',[]).append({'digest':record['digest'],'resume_attempt':record.get('resume_attempt'),'resume_terminal':record.get('resume_terminal')})
            for key in ('resume_attempt','resume_turn','resume_terminal','resume_generation'):record.pop(key,None)
            record.update(state='saved',revision=revision,path=str(path),work=work,reads={},digest=hashlib.sha256(text.encode()).hexdigest(),successor=None)
            self._put(db,'hidden_handoff:'+handoff_id,record);self.event(db,'hidden_handoff_refreshed',handoff=handoff_id,revision=revision,reason=reason)
            return {'state':'saved','revision':revision,'a_must_read_again':True}


def adopt(store,subject,config_id=None,task_id=None,candidate_id=None,execution_id=None):
    with store.transaction() as db:
        current,_=identity(store,db,subject['kind'],subject['id'])
        if current!=subject:fail('Successor identity changed')
        for row in db.execute("SELECT value FROM pair_settings WHERE key LIKE 'hidden_handoff:%' ORDER BY key"):
            value=json.loads(row[0])
            if value['state'] not in ('accepted','read_confirmed') or value['source']['kind']!=subject['kind'] or value['config_id']!=config_id:continue
            if value.get('successor_task',value.get('auxiliary_task'))!=task_id or value.get('candidate')!=candidate_id:continue
            if value.get('successor') and value['successor']!=subject:continue
            if value['source']['session']==subject['session']:fail('New successor context required')
            value['successor']=subject;store._put(db,'hidden_handoff:'+value['id'],value)
            if execution_id:
                value['preparation_execution']=execution_id;store._put(db,'hidden_handoff:'+value['id'],value)
            store.event(db,'hidden_successor_bound',handoff=value['id'],successor=subject)
            return value
    return None


def consume_with_client(store,client,subject,record):
    """仅传入已关闭原生执行工具的受管客户端；不重放未知接续回合。"""
    existing=store.setting('hidden_handoff:'+record['id'])
    if existing.get('resume_attempt'):
        reconcile_consumption(store,client,subject,record['id'])
        if store.setting('hidden_handoff:'+record['id'])['state']=='consumed':return
        fail('Handoff already terminal without consumption / A核对后重新登记接续，不重放原回合')
    try:from .pair_core import FileLock
    except ImportError:from pair_core import FileLock
    with FileLock(store.control_lock,timeout=30):
        with store.transaction() as db:
            current=store._option(db,'hidden_handoff:'+record['id'])
            store._require_recovery_open(db,member='A',task_id=current.get('business_task'),action='auxiliary')
            if current['state']=='consumed':return
            if current.get('resume_attempt'):fail('Handoff consumption unresolved / 先核对原接续回合，不自动重放')
            current['resume_attempt']=uuid.uuid4().hex
            current['resume_generation']=store._control_generation(db)
            store._put(db,'hidden_handoff:'+record['id'],current)
        turn=client.rpc('turn/start',{'threadId':subject['session'],'clientUserMessageId':current['resume_attempt'],
            'input':[{'type':'text','text':'先用pair_review_handoff action=read按next_offset完整读取handoff_id='+record['id']+
                '，再用action=consume提交准确digest和接续计划；保持历史贡献、审核轮次、未决权限和原任务版本。此回合只办理交接。'}]})['turn']
    with store.transaction() as db:
        current=store._option(db,'hidden_handoff:'+record['id']);current['resume_turn']=turn['id'];store._put(db,'hidden_handoff:'+record['id'],current)
        if current.get('preparation_execution'):
            db.execute('UPDATE auxiliary_executions SET charged=1,started=COALESCE(started,?),updated=? WHERE id=? AND session_uuid=? AND turn_id IS NULL',
                (time.time(),time.time(),current['preparation_execution'],subject['session']))
    deadline=time.monotonic()+600
    terminal=False;interrupted=False
    while time.monotonic()<deadline and not terminal:
        with store.transaction() as db:
            gate=store._recovery_gate(db,member='A',task_id=current.get('business_task'),action='auxiliary') or store._control_generation(db)!=current['resume_generation']
        if gate and not interrupted:
            client.rpc('turn/interrupt',{'threadId':subject['session'],'turnId':turn['id']});interrupted=True
        try:request=client.server_requests.get(timeout=.2)
        except queue.Empty:
            if client._dead:raise TransportError('Handoff client lost / 保留原接续回合')
        else:
            params=request.get('params',{})
            try:
                if request.get('method')!='item/tool/call' or params.get('tool')!=HANDOFF_TOOL['name'] or params.get('threadId')!=subject['session'] or params.get('turnId')!=turn['id']:fail('Only exact handoff tool permitted')
                output=handle_tool(store,subject,params.get('arguments'))
                reply={'success':True,'contentItems':[{'type':'inputText','text':encoded(output)}]}
            except Exception as exc:reply={'success':False,'contentItems':[{'type':'inputText','text':redact_diagnostic(str(exc))[:1000]}]}
            client._write({'id':request['id'],'result':reply})
        while True:
            try:event=client.notifications.get_nowait()
            except queue.Empty:break
            params=event.get('params',{})
            if event.get('method')=='turn/completed' and params.get('threadId')==subject['session'] and params.get('turn',{}).get('id')==turn['id']:terminal=True
    reconcile_consumption(store,client,subject,record['id'])
    if store.setting('hidden_handoff:'+record['id'])['state']!='consumed':fail('Handoff not consumed / 继任者尚未完整读取并登记接续计划')


def reconcile_consumption(store,client,subject,handoff_id):
    record=store.setting('hidden_handoff:'+handoff_id)
    if not record or record.get('successor')!=subject or not record.get('resume_turn'):fail('Exact original handoff turn required')
    observed=next((value for value in client.turns(subject['session'],items_view='full') if value['id']==record['resume_turn']),None)
    if not observed or observed.get('status') not in ('completed','failed','interrupted'):fail('Handoff turn unresolved / 不释放接续执行')
    with store.transaction() as db:
        current=store._option(db,'hidden_handoff:'+record['id'])
        if current.get('successor')!=subject or current.get('resume_turn')!=observed['id']:fail('Handoff reconciliation identity changed')
        current['resume_terminal']={'turn':observed['id'],'status':observed['status'],'digest':digest(observed)}
        if current['state']=='read_confirmed':current.update(state='consumed',consumed_at=time.time())
        store._put(db,'hidden_handoff:'+record['id'],current)
        return {'state':current['state'],'terminal':True,'replayed':False}

