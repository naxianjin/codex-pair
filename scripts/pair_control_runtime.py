"""从原会话API、原日志和本轮工具台账取得恢复证据；不接受JSON自报通过。"""
from __future__ import annotations

import json
from pathlib import Path
import re
import shlex
import sys
import time

try:
    from .pair_recovery import VerifiedRecoveryEvidence,digest,encoded
    from .pair_reviewer import fail
    from .pair_transport import AppServer,canonical_path,rollout_command_items
except ImportError:
    from pair_recovery import VerifiedRecoveryEvidence,digest,encoded
    from pair_reviewer import fail
    from pair_transport import AppServer,canonical_path,rollout_command_items


CONTINUE_TEXT=('继续','继续原任务','继续协作')
PAUSE_TEXT=('暂停','停一下','全部暂停','暂停协作','暂停所有工作')

MANAGEMENT_COMMANDS=frozenset(('doctor','diagnose-blockers','status','read-message','read-delivery','ack-delivery',
    'claim','complete','request-pause','request-resume','confirm-pause','reconcile-resume','control-runtime-status',
    'recovery-status','check-checkpoint','read-checkpoint','save-checkpoint','consume-checkpoint','role-handoff-status'))


def management_command(store,actor,event):
    command=(event.get('tool_input') or {}).get('command') if isinstance(event.get('tool_input'),dict) else None
    if event.get('tool_name')!='Bash' or not isinstance(command,str) or any(c in command for c in '\r\n;|<>`$'):return False
    try:
        # 仅接受独立字面量参数；PowerShell路径中的反斜杠不是POSIX转义。
        literal=r"'(?:[^']|'')*'|\"[^\"]*\"|[^\s'\"]+"
        if not re.fullmatch(r'\s*(?:'+literal+r')(?:\s+(?:'+literal+r'))*\s*',command):return False
        arguments=[value[1:-1].replace("''", "'") if value.startswith("'") else
                   value[1:-1] if value.startswith('"') else value for value in re.findall(literal,command)]
        if arguments and arguments[0]=='&':arguments=arguments[1:]
        if len(arguments)<3 or any('&' in value for value in arguments):return False
        if Path(arguments[0]).resolve()!=Path(sys.executable).resolve() or Path(arguments[1]).resolve()!=Path(__file__).with_name('pair.py').resolve():return False
        try:from .pair import build_parser
        except ImportError:from pair import build_parser
        args=build_parser().parse_args(arguments[2:])
        if args.command not in MANAGEMENT_COMMANDS or getattr(args,'pair',None)!=store.pair_id or Path(getattr(args,'state_root','')).resolve()!=store.state_root.resolve():return False
        if hasattr(args,'member') and (args.member!=actor['id'] or args.session!=actor['session_uuid']):return False
        return True
    except (ValueError,RuntimeError,SystemExit):return False


class ControlRuntimeMixin:
    def inspect_control_capabilities(self,member,session,cwd,executable):
        try:
            from .pair_native_capabilities import inspect_original_runtime
            from .pair_producer import file_identity
        except ImportError:
            from pair_native_capabilities import inspect_original_runtime
            from pair_producer import file_identity
        with self.transaction() as db:
            actor=dict(self._binding(db,member,session,cwd));configured=self._option(db,'control_hooks:'+member)
            trusted=self._option(db,'cli_trusted_executable:'+member)
        actual=file_identity(executable)
        if not trusted or actual!=trusted['file']:fail('Original CLI executable required')
        result=inspect_original_runtime(actor,configured,executable)
        return dict(result,member=member,session=session,binding_version=actor['binding_version'],
                    executable_sha256=actual['sha256'],observed_at=time.time(),full_tool_control=False)

    def install_control_hooks(self,member,session,cwd,target):
        import os
        import subprocess
        import uuid
        try:
            from .pair_core import FileLock
            from .pair_approvals import approval_package_digest
        except ImportError:
            from pair_core import FileLock
            from pair_approvals import approval_package_digest
        with self.transaction() as db:
            self._a_actor(db,member,session,cwd);actor=dict(self._member(db,target))
            if actor['lifecycle']!='active':fail('Active control target required')
        path=Path(actor['codex_home'])/'hooks.json'
        if path.resolve()!=path.absolute():fail('Redirected control hook path')
        script=Path(__file__).with_name('pair_control_hook.py').resolve(strict=True)
        arguments=[sys.executable,str(script),'--pair',self.pair_id,'--state-root',str(self.state_root),
            '--member',target,'--session',actor['session_uuid'],'--binding-version',str(actor['binding_version']),
            '--package-digest',approval_package_digest(script.parent)]
        command=subprocess.list2cmdline(arguments) if os.name=='nt' else shlex.join(arguments)
        try:from .pair_native_capabilities import CONTROL_EVENTS
        except ImportError:from pair_native_capabilities import CONTROL_EVENTS
        markers={event:'codex-pair-control:'+self.pair_id+':'+target+':'+event for event in CONTROL_EVENTS}
        commands={event:subprocess.list2cmdline(arguments+['--event',event]) if os.name=='nt' else shlex.join(arguments+['--event',event]) for event in CONTROL_EVENTS}
        with FileLock(path.with_name('hooks.codex-pair.lock'),timeout=30):
            original=path.read_bytes() if path.exists() else None
            data=json.loads(original.decode('utf-8-sig')) if original else {}
            hooks=data.setdefault('hooks',{})
            if not isinstance(hooks,dict):fail('Invalid hook document / 保留原配置')
            for event,marker in markers.items():
                groups=hooks.setdefault(event,[])
                if not isinstance(groups,list) or any(not isinstance(g,dict) or not isinstance(g.get('hooks'),list) or any(not isinstance(h,dict) for h in g['hooks']) for g in groups):fail('Invalid hook handlers / 保留原配置')
                kept=[]
                for group in groups:
                    handlers=[h for h in group['hooks'] if h.get('statusMessage')!=marker]
                    if handlers:kept.append(dict(group,hooks=handlers))
                own={'hooks':[{'type':'command','command':commands[event],'timeout':3 if event=='Interrupt' else 30,'statusMessage':marker}]}
                if event in ('PreToolUse','PostToolUse'):own['matcher']='*'
                hooks[event]=kept+[own]
            if original is None or json.loads(original.decode('utf-8-sig'))!=data:
                if original:
                    with path.with_name('hooks.codex-pair-backup-'+uuid.uuid4().hex+'.json').open('xb') as file:file.write(original)
                temporary=path.with_name('hooks.codex-pair-'+uuid.uuid4().hex+'.tmp')
                try:
                    with temporary.open('x',encoding='utf-8') as file:
                        json.dump(data,file,ensure_ascii=False,indent=2);file.flush();os.fsync(file.fileno())
                    os.replace(temporary,path)
                finally:
                    if temporary.exists():temporary.unlink()
        with self.transaction() as db:
            current=self._member(db,target)
            if any(current[key]!=actor[key] for key in ('session_uuid','binding_version')):fail('Binding changed during control hook install')
            record={'session':actor['session_uuid'],'binding_version':actor['binding_version'],
                'command':command,'commands':commands,'markers':list(markers.values()),'events':markers,'status':'installed_requires_trust'}
            self._put(db,'control_hooks:'+target,record);self.event(db,'control_hooks_installed',member=target,trust_bypassed=False)
        return {'installed':True,'target':target,'trust_required':True,'complete_enforcement_claimed':False,
                'unsupported':['hosted_tools','existing_unified_exec_stdin','specialized_unhooked_tools']}

    def rebaseline_control_source(self,member,session,cwd,executable,*,instruction):
        """缺口后只接收新的准确用户继续指令；不重放、补造缺失历史。"""
        from datetime import datetime
        import hashlib
        import os
        try:
            from .pair_control_events import _MemberObserver, MAX_RECORD_BYTES
            from .pair_transport import owned_rollout_path
            from .pair_core import FileLock
        except ImportError:
            from pair_control_events import _MemberObserver, MAX_RECORD_BYTES
            from pair_transport import owned_rollout_path
            from pair_core import FileLock
        if instruction!='继续协作 '+self.pair_id:fail('Exact new user instruction required / 缺口恢复须由原窗口新输入继续协作及准确ID')
        actor=self.member(member)
        if actor['session_uuid']!=session or canonical_path(actor['cwd'])!=canonical_path(cwd):fail('Original binding required')
        try:from .pair_producer import file_identity
        except ImportError:from pair_producer import file_identity
        trusted=self.setting('cli_trusted_executable:'+member)
        if not trusted or file_identity(executable)!=trusted['file']:fail('Original CLI executable required')
        prior=self.setting('control_observer:'+member)
        if not prior or prior.get('state')!='gap':fail('Control source has no unresolved gap')
        with AppServer(codex_home=actor['codex_home'],cwd=actor['cwd'],executable=executable,metadata_only=True) as client:
            thread=client.validate(actor);turns=list(client.turns(session,items_view='full'))
            users=[item for turn in turns for item in turn.get('items',[]) if item.get('type')=='userMessage']
            if not users or ''.join(part.get('text','') for part in users[-1].get('content',[]) if part.get('type') in ('text','inputText'))!=instruction:
                fail('Original user message unavailable / API尚未确认原窗口的新继续指令')
            path=owned_rollout_path(actor,thread)
            with path.open('rb') as stream:
                stat=os.fstat(stream.fileno());meta=json.loads(stream.readline(MAX_RECORD_BYTES+1));metadata_end=stream.tell()
                if meta.get('type')!='session_meta' or meta.get('payload',{}).get('id')!=session:fail('Original source identity mismatch')
                identity=digest({'path':canonical_path(path),'device':stat.st_dev,'inode':stat.st_ino})
                start=max(metadata_end,stat.st_size-MAX_RECORD_BYTES);stream.seek(start)
                if start>metadata_end:stream.readline()
                candidate=None
                while stream.tell()<stat.st_size:
                    position=stream.tell();line=stream.readline(MAX_RECORD_BYTES+1)
                    if not line.endswith(b'\n') or len(line)>MAX_RECORD_BYTES:break
                    record=json.loads(line);event=_MemberObserver._control_event(record,member)
                    if event.get('kind')=='direct_user_control' and event['text']==instruction:
                        timestamp=datetime.fromisoformat(record['timestamp'].replace('Z','+00:00'))
                        if timestamp.tzinfo and timestamp.timestamp()>prior['observed_at']:
                            candidate={'start':position,'end':stream.tell(),'hash':hashlib.sha256(line).hexdigest()}
                if not candidate:fail('New original user event required / 缺口之后尚无可验证的新用户指令')
                end=candidate['end'];anchor=_MemberObserver._anchor(stream,end)
        with FileLock(self.control_lock,timeout=30),self.transaction() as db:
            self._binding(db,member,session,cwd)
            if self._option(db,'control_observer:'+member)!=prior:fail('Control gap changed / 重新核对来源')
            db.execute('INSERT OR REPLACE INTO control_event_cursors VALUES(?,?,?,?,?,?)',
                (member,session,actor['binding_version'],identity,end,self._control_generation(db)))
            self._put(db,'control_observer:'+member,_MemberObserver._state(actor,identity,end,anchor,'ready'))
            event_id='rollout:'+identity+':'+str(candidate['start'])+':'+candidate['hash']
            self._record_user_control(db,member,instruction,event_id=event_id,source_identity=identity,session=session,binding_version=actor['binding_version'])
            self.event(db,'control_source_rebaselined',member=member,previous=prior,new_source=identity,position=end,history_replayed=False)
            return {'rebaselined':True,'source_event':event_id,'history_gap_preserved':True,'resumed':False}

    def _record_user_control(self,db,member,text,*,event_id,source_identity,session,binding_version):
        actor=self._member(db,member)
        if actor['session_uuid']!=session or actor['binding_version']!=binding_version:return
        instruction=text.strip()
        allowed=CONTINUE_TEXT+PAUSE_TEXT+('继续协作 '+self.pair_id,'暂停协作 '+self.pair_id)
        if instruction not in allowed:return
        key='user_control:'+event_id
        if self._option(db,key):return
        value={'member':member,'session':session,'binding_version':binding_version,'instruction':instruction,
               'event_id':event_id,'source_identity':source_identity,'generation':self._control_generation(db),
               'recorded':time.time(),'kind':'resume' if instruction.startswith('继续') else 'pause'}
        self._put(db,key,value);self._put(db,'user_control_latest:'+member,value)
        self.event(db,'direct_user_control_recorded',member=member,source_event=event_id,kind=value['kind'])
        if value['kind']=='pause':
            scope={'kind':'pair'}
            self._pause_record(db,actor,scope,{'reason':instruction,'source':value},'user:'+event_id)

    def control_runtime_status(self,member,session,cwd):
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            return {'hooks':self._option(db,'control_hooks:'+member),
                    'source':self._option(db,'user_control_latest:'+member),
                    'inflight':[json.loads(row[0]) for row in db.execute("SELECT value FROM pair_settings WHERE key LIKE 'control_tool:%'")
                                if json.loads(row[0]).get('state')=='admitted'],
                    'coverage':{'local_tools':'PreToolUse','hosted_tools':'unsupported','existing_stdin':'original_tool_terminal_required'},
                    'complete_enforcement_claimed':False}

    def _control_tool_event(self,db,member,event):
        actor=self._member(db,member)
        if event.get('session_id')!=actor['session_uuid']:return {}
        kind=event.get('hook_event_name');turn=event.get('turn_id');tool=event.get('tool_use_id')
        if kind not in ('PreToolUse','PostToolUse','Interrupt','UserPromptSubmit','Stop'):fail('Unsupported control hook')
        if not isinstance(turn,str) or not turn:fail('Exact turn required')
        if kind=='UserPromptSubmit':
            prompt=event.get('prompt')
            if not isinstance(prompt,str):fail('Exact prompt required')
            try:envelope=json.loads(prompt)
            except ValueError:return {}
            if not isinstance(envelope,dict) or envelope.get('protocol')!='codex-pair/1' or envelope.get('pair_id')!=self.pair_id:return {}
            message=db.execute('SELECT * FROM messages WHERE id=?',(envelope.get('message_id'),)).fetchone()
            exact=bool(message and message['recipient']==member and message['envelope']==prompt.strip()
                       and self._message_current(db,message) and message['process_status']=='unclaimed')
            reasons=self._recovery_gate(db,member=member,task_id=message['task_id'] if exact else None)
            if not exact or reasons:
                self.event(db,'queued_prompt_blocked',member=member,message=envelope.get('message_id'),turn=turn,identity_verified=exact,generation=self._control_generation(db))
                return {'decision':'block','reason':'codex-pair message paused or stale / 本协作消息暂停或身份不符，保留原任务，核对原回执后接续。'}
            return {}
        task_id,gate=self._turn_control_gate(db,member,turn)
        if kind=='Stop':
            if gate:
                return {'continue':False,'stopReason':'codex-pair paused / 保留现场，不自动开启后续回合。'}
            return {}
        if kind=='Interrupt':
            if member=='A':self._pause_record(db,actor,{'kind':'pair'},
                {'reason':'native_interrupt','turn_id':turn},'native-interrupt:'+actor['session_uuid']+':'+turn)
            return {}
        if not isinstance(tool,str) or not tool or not isinstance(event.get('tool_name'),str):fail('Exact tool required')
        identity={'session':actor['session_uuid'],'version':actor['binding_version'],'turn':turn,'tool':tool}
        key='control_tool:'+digest(identity);old=self._option(db,key)
        fingerprint=digest(event.get('tool_input'))
        if old and old['input_digest']!=fingerprint:fail('Control tool identity conflict')
        if kind=='PostToolUse':
            if old:
                old.update(state='returned',returned=time.time())
                self._put(db,key,old);self.event(db,'control_tool_returned',identity=identity)
            if gate:
                # PostToolUse的continue:false只替换结果，不能停止模型；保留原输出供对账。
                return {'systemMessage':'codex-pair paused / 当前工具已返回，后台进程仍须对账；后续本地工具由PreToolUse门禁阻断。'}
            return {}
        management=management_command(self,actor,event)
        reasons=[] if management else gate
        if old and old['state']=='admitted':
            reasons=reasons+[{'code':'DUPLICATE_TOOL_UNRESOLVED'}]
        if old and old['state']=='returned':reasons=reasons+[{'code':'TOOL_ALREADY_RETURNED'}]
        record={**identity,'member':member,'name':event['tool_name'],'input_digest':fingerprint,
                'generation':self._control_generation(db),'state':'denied' if reasons else 'admitted','created':time.time()}
        record.update(management=management,task_id=task_id)
        if not old:self._put(db,key,record)
        elif old['state']=='denied' and not reasons:self._put(db,key,record)
        self.event(db,'control_tool_gate',identity=identity,allowed=not reasons,generation=record['generation'])
        if reasons:
            return {'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'deny',
                'permissionDecisionReason':'codex-pair paused or unresolved / 协作暂停或原操作未对账；保存现场并办理恢复'}}
        # 不返回allow，避免覆盖其他Hook或CLI原审批。
        return {}

    def _turn_control_gate(self,db,member,turn):
        messages=list(db.execute("SELECT * FROM messages WHERE recipient=? AND turn_id=? AND kind='task'",(member,turn)))
        tasks={message['task_id'] for message in messages if self._message_current(db,message)}
        if len(tasks)==1:
            task_id=next(iter(tasks))
            task=db.execute('SELECT assignee FROM tasks WHERE id=?',(task_id,)).fetchone()
            if task and task['assignee']==member:
                return task_id,self._recovery_gate(db,member=member,task_id=task_id)
        reasons=self._recovery_gate(db,member=member)
        # 已有任务级暂停时，不能把尚未对上原回合的工具当作无关工作放行。
        for row in db.execute("SELECT scope FROM control_requests WHERE state IN ('pausing','paused','reconciling','blocked','partial')"):
            scope=json.loads(row['scope'])
            if scope['kind']!='task':continue
            task=db.execute('SELECT assignee,status FROM tasks WHERE id=?',(scope['id'],)).fetchone()
            if (task and task['assignee']==member and task['status'] not in ('completed','integrated','cancelled')
                    and self._matching_controls(db,member,scope['id'])):
                reasons.append({'code':'TASK_TURN_BINDING_UNVERIFIED','task':scope['id'],'turn':turn})
        return None,reasons

    def renew_review_recovery(self,member,session,cwd,recovery_id,token,*,epoch,seconds=120):
        import secrets
        if type(seconds) is not int or not 30<=seconds<=600:fail('Lease duration must be 30..600 seconds')
        with self.transaction() as db:
            actor=self._binding(db,member,session,cwd);row=self._recovery_request(db,recovery_id)
            self._recovery_binding(row,actor)
            if row['status'] not in ('running','unknown') or row['execution_epoch']!=epoch or not secrets.compare_digest(row['claim_token'] or '',token):fail('Exact recovery lease required')
            proof=self._trusted_recovery_evidence('lease',self._recovery_context(db,row,verify_candidate=False))
            if proof.facts.get('identity_current') is not True or proof.facts.get('execution_observed') is not True:fail('Recovery lease unverified')
            self._put(db,'recovery_lease:'+recovery_id,{'epoch':epoch,'execution':row['execution_id'],'renewed':time.time(),'until':time.time()+seconds,'receipts':proof.receipts})
            return {'renewed':True,'epoch':epoch,'seconds':seconds,'expired_lease_never_releases_execution':True}


class ProductionRecoveryAdapter:
    """查询在写事务外完成；消费证据时比较状态摘要，变化时拒绝沿用。"""
    def __init__(self,store,executable):
        self.store=store;self.executable=executable;self.snapshot=None

    def capture(self,kind):
        if not self.executable:fail('Exact CLI required / 恢复取证须显式指定--codex')
        store=self.store
        # 先补读准确用户控制事件，不从命令正文推断用户授权。
        try:from .pair_control_events import ControlObserver
        except ImportError:from pair_control_events import ControlObserver
        observer=ControlObserver(store,self.executable)
        try:observer.poll()
        finally:observer.close()
        started=time.time()
        with store.transaction() as db:
            state_digest=store._recovery_state_digest(db)
            members=[dict(row) for row in db.execute("SELECT * FROM members WHERE lifecycle<>'retired'")]
            subjects=list(members)
            reviewer=db.execute("SELECT * FROM reviewer_runtime WHERE state<>'retired'").fetchone()
            if reviewer:
                subjects.append({'id':'@reviewer','trust_member':'A','session_uuid':reviewer['session_uuid'],
                    'codex_home':reviewer['codex_home'],'cwd':reviewer['cwd'],'binding_version':reviewer['epoch'],
                    'proof':encoded({'id':reviewer['session_uuid'],'cwd':reviewer['cwd'],'model':reviewer['model'],'modelProvider':'openai'}),
                    'unknown':reviewer['state'] in ('starting','unknown')})
            for row in db.execute("SELECT s.*,c.member AS source_member FROM auxiliary_sessions s JOIN auxiliary_configs c ON c.id=s.config_id WHERE s.state<>'retired'"):
                parent=store._member(db,row['source_member'])
                subjects.append({'id':'@aux:'+row['id'],'trust_member':row['source_member'],'session_uuid':row['session_uuid'],
                    'codex_home':parent['codex_home'],'cwd':row['cwd'],'binding_version':row['epoch'],
                    'proof':row['proof'] or encoded({'id':row['session_uuid'],'cwd':row['cwd'],'model':row['model'],'modelProvider':'openai'}),
                    'unknown':row['state'] in ('starting','unknown')})
            settings={row['key']:json.loads(row['value']) for row in db.execute("SELECT key,value FROM pair_settings WHERE key LIKE 'control_%' OR key LIKE 'user_control%' OR key LIKE 'cli_trusted_executable:%'")}
            messages=[dict(row) for row in db.execute('SELECT id,recipient,turn_id,delivery_status,remote_status,task_id FROM messages')]
            current_generation=store._control_generation(db)
            task_owners={row['id']:row['assignee'] for row in db.execute('SELECT id,assignee FROM tasks')}
            executions=[dict(row) for row in db.execute('SELECT * FROM review_stages WHERE execution_id IS NOT NULL')]
        sessions={};receipts=[];observed_executions={}
        for actor in subjects:
            if not actor['session_uuid'] or actor.get('unknown'):
                sessions[actor['id']]={'verified':False,'reason':'session_creation_or_execution_unknown'};continue
            trust=settings.get('cli_trusted_executable:'+actor.get('trust_member',actor['id']))
            if not trust:sessions[actor['id']]={'verified':False,'reason':'cli_baseline_missing'};continue
            try:
                from .pair_producer import file_identity
            except ImportError:from pair_producer import file_identity
            if file_identity(self.executable)!=trust['file']:
                sessions[actor['id']]={'verified':False,'reason':'cli_changed'};continue
            with AppServer(codex_home=actor['codex_home'],cwd=actor['cwd'],executable=self.executable,metadata_only=True) as client:
                thread=client.validate(actor)
                turns=list(client.turns(actor['session_uuid'],items_view='full'))
                if len({turn.get('id') for turn in turns})!=len(turns):fail('Duplicate original turns / 原线程回合ID重复')
                turn_map={turn['id']:turn for turn in turns}
                ledger=[value for key,value in settings.items() if key.startswith('control_tool:') and value.get('session')==actor['session_uuid'] and value.get('state') in ('admitted','returned')]
                management_ids={item['tool'] for item in ledger if item.get('management') and item['state']=='admitted'}
                management_turns={turn['id'] for turn in turns if any(item.get('id') in management_ids for item in turn.get('items',[]))
                    and all(item.get('id') in management_ids for item in turn.get('items',[]) if item.get('type') in ('commandExecution','mcpToolCall','dynamicToolCall','fileChange') and item.get('status') not in ('completed','failed'))}
                active=[turn['id'] for turn in turns if turn.get('status') not in ('completed','failed','interrupted') and turn['id'] not in management_turns]
                unknown=[]
                required={m['turn_id'] for m in messages if m['recipient']==actor['id'] and m['turn_id']}
                required.update(item['turn'] for item in ledger)
                required.update(row['turn_id'] for row in executions if row['reviewer_session']==actor['session_uuid'] and row['turn_id'])
                terminal_receipts={}
                for turn_id in required:
                    if turn_id in management_turns:continue
                    turn=turn_map.get(turn_id)
                    if not turn or turn.get('status') not in ('completed','failed','interrupted'):unknown.append(turn_id);continue
                    raw=rollout_command_items(actor,thread,turn_id,producer_version=trust.get('version'),terminal_turn=turn)
                    if raw is None:unknown.append(turn_id)
                    else:
                        commands=[item for item in turn.get('items',[]) if item.get('type')=='commandExecution']
                        if any(item.get('status') not in ('completed','failed') or type(item.get('exitCode')) is not int for item in commands):unknown.append(turn_id);continue
                        receipt='official+rollout:'+actor['session_uuid']+':'+turn_id+':'+digest(raw)
                        receipts.append(receipt);terminal_receipts[turn_id]=receipt
                if any(m['recipient']==actor['id'] and m['delivery_status'] in ('delivering','unknown') for m in messages):unknown.append('delivery_unknown')
                for item in ledger:
                    if item.get('management') and item['turn'] in management_turns:continue
                    turn=turn_map.get(item['turn'])
                    if not turn or turn.get('status') not in ('completed','failed','interrupted'):unknown.append(item['tool']);continue
                    matched=[tool for tool in turn.get('items',[]) if tool.get('id')==item['tool']]
                    if len(matched)!=1 or matched[0].get('status') not in ('completed','failed'):unknown.append(item['tool'])
                hooks=settings.get('control_hooks:'+actor['id'])
                trusted=False;original=None
                if not actor['id'].startswith('@'):
                    try:from .pair_native_capabilities import inspect_original_runtime
                    except ImportError:from pair_native_capabilities import inspect_original_runtime
                    original=inspect_original_runtime(actor,hooks,self.executable)
                    trusted=bool(original.get('available') and original['capabilities']['local_tool_definition_trusted'])
                    if not original.get('available'):unknown.append('original_runtime_unavailable')
                    elif original['background_terminals']:unknown.append('original_background_terminals_running')
                    elif (original.get('thread_status') or {}).get('type')!='idle':unknown.append('original_thread_not_idle')
                if actor['id'].startswith('@aux:'):
                    # 辅助池的静态执行器逐工具检查控制代次；仅接受真实存储的关闭原生工具证明。
                    with store.transaction() as db:
                        auxiliary_producers=list(db.execute('SELECT producer FROM auxiliary_executions WHERE reviewer_id=? AND session_uuid=? ORDER BY created DESC LIMIT 1',(actor['id'][5:],actor['session_uuid'])))
                    producer=json.loads(auxiliary_producers[0][0]) if auxiliary_producers and auxiliary_producers[0][0] else {}
                    controls=producer.get('controls',{})
                    try:from .pair_auxiliary_runtime import STATIC_OVERRIDES
                    except ImportError:from pair_auxiliary_runtime import STATIC_OVERRIDES
                    trusted=bool(controls.get('mode')=='static' and set(STATIC_OVERRIDES).issubset(controls.get('verified_keys',[])))
                sessions[actor['id']]={'verified':True,'session':actor['session_uuid'],'version':actor['binding_version'],
                    'idle':not active and not unknown,'active':active,'unresolved':unknown,'local_hook_trust':trusted,
                    'hosted_tool_boundary':'not_claimed','original_runtime':original}
                for row in executions:
                    if row['reviewer_session']!=actor['session_uuid']:continue
                    turn=turn_map.get(row['turn_id'])
                    observed_executions[row['execution_id']]={'session':actor['session_uuid'],'epoch':row['reviewer_epoch'],
                        'turn':row['turn_id'],'observed':turn is not None,'terminal':row['turn_id'] in terminal_receipts,
                        'receipt':terminal_receipts.get(row['turn_id'])}
                receipts.append('thread-snapshot:'+actor['session_uuid']+':'+digest(turns))
        self.snapshot={'state_digest':state_digest,'sessions':sessions,'members':members,'settings':settings,
                       'generation':current_generation,'receipts':receipts,'captured':started,'kind':kind,'task_owners':task_owners,
                       'executions':observed_executions}
        return self.snapshot

    def verify(self,kind,context):
        snapshot=self.snapshot
        if not snapshot or snapshot['kind']!=kind or time.time()-snapshot['captured']>15 or context.get('state_digest')!=snapshot['state_digest']:
            fail('Recovery observation stale / 原状态或查询证据已变化，重新只读取证')
        sessions=snapshot['sessions'];facts={};receipts=list(snapshot['receipts'])
        if kind in ('pause_confirmation','resume'):
            control=context['control'];scope=json.loads(control['scope'])
            targets=set(sessions) if scope['kind']=='pair' else ({scope['id']} if scope['kind']=='member' else {snapshot['task_owners'].get(scope['id'])})
            safe=bool(targets) and all(sessions.get(key,{}).get('verified') and sessions.get(key,{}).get('idle') for key in targets)
            gates=safe and all(sessions[key].get('local_hook_trust') for key in targets)
            complete_boundary=gates and all(key.startswith('@aux:') for key in targets)
            facts.update(all_scope_executors_stopped=safe and complete_boundary,inflight_operations_reconciled=safe and complete_boundary,
                         observed_idle=safe,
                         all_required_gates_verified=complete_boundary,full_scope_reconciled=complete_boundary,
                         coverage={'local_tools':gates,'hosted_tools':'unsupported','cross_tool_atomicity':'requires_client_validation'})
            if kind=='resume':
                request=json.loads(control['source']);actor=next((m for m in snapshot['members'] if m['session_uuid']==request['session']),None)
                source=(snapshot['settings'].get('user_control_latest:'+actor['id']) or {}) if actor else {}
                verified=bool(source.get('kind')=='resume' and source.get('session')==request['session']
                    and source.get('binding_version')==actor['binding_version'] and source.get('instruction')==request['instruction'].strip()
                    and source.get('generation')==control['generation']-1)
                facts['source_is_current_user_instruction']=verified
                if verified:receipts.append(source['event_id'])
        elif kind in ('begin','reconcile','lease'):
            subject=next((value for value in sessions.values() if value.get('session')==context['session']),{})
            execution=snapshot['executions'].get(context.get('execution_id'),{})
            exact=execution.get('session')==context['session'] and execution.get('epoch')==context['epoch']
            facts.update(identity_current=subject.get('verified',False) and subject.get('version')==context['binding_version'],
                         execution_observed=exact and execution.get('observed',False),
                         execution_terminal=exact and execution.get('terminal',False),executor_gates_verified=subject.get('local_hook_trust',False),
                         resources_available=all(v.get('idle') for v in sessions.values()),explicit_admission_cycle=True,
                         isolation_verified=False)
        else:
            fail('Recovery evidence kind unsupported / 此入口须使用对应实际运行或正式审核证据')
        if not receipts:fail('Recovery evidence unavailable / 没有原会话证据')
        return VerifiedRecoveryEvidence(kind,digest(context),tuple(receipts),facts)
