"""唯一受管审核进程；空闲只等待本地队列，不启动模型轮询回合。"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
import sys

try:
    from .pair_producer import KEY, record_producer, stored_context, strict_version, final_context
except ImportError:
    from pair_producer import KEY, record_producer, stored_context, strict_version, final_context

try:
    from .pair_transport import AppServer,DeliveryUnknown,TransportError,binding_proof,redact_diagnostic,canonical_path,command_receipt,command_matches,receipt_cwd,rollout_command_items,merge_command_receipts,same_review_directory
    from .pair_reviewer import reviewer_identity,verify_review_service,fail,safe_evidence
    from .pair_managed import permission_response
    from .pair_stages import encoded,digest
    from . import pair_hidden_handoff as hidden_handoff
except ImportError:
    from pair_transport import AppServer,DeliveryUnknown,TransportError,binding_proof,redact_diagnostic,canonical_path,command_receipt,command_matches,receipt_cwd,rollout_command_items,merge_command_receipts,same_review_directory
    from pair_reviewer import reviewer_identity,verify_review_service,fail,safe_evidence
    from pair_managed import permission_response
    from pair_stages import encoded,digest
    import pair_hidden_handoff as hidden_handoff


def git(root,*args):
    # 审核不能隐式执行仓库hook、fsmonitor、external diff或textconv。
    options=list(args)
    if options and options[0] in ('diff','show'):
        options[1:1]=['--no-ext-diff','--no-textconv']
    result=subprocess.run(['git','-c','core.hooksPath='+str(Path(__file__).resolve()),
        '-c','core.fsmonitor=false','-c','diff.external=','-C',str(root),*options],
        capture_output=True,encoding='utf-8',errors='replace')
    if result.returncode:raise RuntimeError(redact_diagnostic(result.stderr.strip()))
    return result.stdout.strip()


def review_git(root,*args):
    """审核检出只覆盖本次命令，不修改用户或仓库配置。"""
    return git(root,'-c','core.autocrlf=false','-c','core.eol=lf',*args)


def verify_review_bytes(root,*,snapshot=None):
    """直接比较Git blob对象，不使用换行规范化或clean过滤器。"""
    root=Path(root)
    algorithm=git(root,'rev-parse','--show-object-format')
    if algorithm not in ('sha1','sha256'):fail('Unknown Git object format / 不猜测Git对象摘要格式')
    snapshot=snapshot or {}
    for relative,expected in snapshot.items():
        path=root/relative
        if (path.resolve()!=path.absolute() or not path.resolve().is_relative_to(root.resolve())
                or path.is_symlink() or path.exists() and not same_review_directory(path.parent,path.parent)):
            fail('Snapshot path redirected / 不清理重定向的快照路径')
        if path.is_file() and path.stat().st_nlink!=1:fail('Snapshot hard link rejected / 审核快照不接受硬链接')
        actual=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        if expected is None and path.exists():fail('Snapshot deletion changed / 已删除快照路径出现新内容，保留现场')
        if actual!=expected:fail('Snapshot bytes changed / 快照字节已变化，不恢复或覆盖')
    for entry in git(root,'ls-tree','-r','-z','HEAD').split('\0'):
        if not entry:continue
        metadata,relative=entry.split('\t',1)
        mode,kind,expected=metadata.split()
        if relative in snapshot:continue
        path=root/relative
        if (kind!='blob' or mode not in ('100644','100755') or path.is_symlink() or not path.is_file()
                or path.stat().st_nlink!=1
                or not same_review_directory(path.parent,path.parent)):
            fail('Unsupported reviewer checkout / 审核树含缺失、重定向或不支持的跟踪项')
        data=path.read_bytes()
        actual=hashlib.new(algorithm,b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
        if actual!=expected:fail('Reviewer checkout bytes differ / 审核树原始字节与HEAD不符；保留旧树，先核实再重建')


def tree_stamp(root):
    """测试前后固定业务树及未跟踪项；日志不作为业务版本。"""
    head=git(root,'rev-parse','HEAD')
    changes=git(root,'status','--porcelain=v1','--untracked-files=all')
    difference=git(root,'diff','--binary','HEAD')
    unknown=[]
    for relative in git(root,'ls-files','--others','--exclude-standard','-z').split('\0'):
        if not relative:continue
        path=Path(root)/relative
        if path.is_symlink():unknown.append((relative,'symlink'))
        elif path.is_file():unknown.append((relative,hashlib.sha256(path.read_bytes()).hexdigest()))
    stamp=encoded({'diff_digest':hashlib.sha256(difference.encode()).hexdigest(),'untracked':unknown})
    return {'head':head,'changes':changes,'diff':hashlib.sha256(stamp.encode()).hexdigest() if difference or unknown else ''}


def permission_binding_snapshot(runtime,parent,turn_id):
    """固定审核配置及目录实体；进度时间等可变字段不属于身份。"""
    paths={}
    for key,value in (('cwd',runtime['cwd']),('home',runtime['codex_home']),
                      ('parent_cwd',parent['cwd']),('parent_home',parent['codex_home'])):
        if not same_review_directory(value,value):fail('Reviewer directory changed / 审批目录不存在或发生重定向')
        info=Path(value).stat()
        paths[key]=(canonical_path(value),info.st_dev,info.st_ino)
    return {'turn':turn_id,'paths':paths,
        'reviewer':{k:runtime[k] for k in ('state','epoch','session_uuid','execution_id','owner','model','parent_session')},
        'parent':{k:parent[k] for k in ('session_uuid','binding_version','proof','lifecycle','verified','online')}}


def final_review_snapshot(store,stage,state):
    with store.transaction() as db:
        row=db.execute('SELECT * FROM review_stages WHERE id=?',(stage['id'],)).fetchone()
        runtime=store._reviewer_row(db);parent=store._member(db,'A')
        if (not row or row['status']!='running' or row['digest']!=stage['digest']
                or row['reviewer_epoch']!=state['epoch'] or row['reviewer_session']!=state['session']
                or row['execution_id']!=state['execution_id'] or row['turn_id']!=state['turn_id']
                or runtime['state']!='running' or runtime['owner']!=state['owner']
                or runtime['epoch']!=state['epoch'] or runtime['execution_id']!=state['execution_id']
                or runtime['session_uuid']!=state['session']):
            fail('Stale final review / 最终回执读取前阶段或执行已变化')
        fields=('id','digest','status','requirements_version','reviewer_epoch','reviewer_session','execution_id','turn_id','payload','artifact')
        snapshot={'stage':{k:row[k] for k in fields},'binding':permission_binding_snapshot(runtime,parent,state['turn_id']),
                  'producer':store._option(db,KEY+state['execution_id'])}
        validate_final_snapshot(store,db,snapshot)
        return snapshot


def validate_final_snapshot(store,db,snapshot):
    original=snapshot['stage']
    stage=db.execute('SELECT * FROM review_stages WHERE id=?',(original['id'],)).fetchone()
    runtime=store._reviewer_row(db);parent=store._member(db,'A')
    task=db.execute('SELECT requirements_version FROM tasks WHERE id=?',(stage['task_id'],)).fetchone() if stage else None
    if (store._option(db,KEY+original['execution_id'])!=snapshot.get('producer')
            or not stage or any(stage[k]!=v for k,v in original.items()) or not task
            or task[0]!=original['requirements_version']
            or permission_binding_snapshot(runtime,parent,original['turn_id'])!=snapshot['binding']):
        fail('Stale final receipt binding / 最终回执读写期间阶段或身份变化')
    return final_context(store,db,runtime,stage)


def prepare_review_tree(store,head,*,reviewer_id=None):
    with store.transaction() as db:
        project=db.execute('SELECT project FROM collaborations').fetchone()[0]
    if reviewer_id is not None and not re.fullmatch(r'[0-9a-f]{32}',reviewer_id):fail('Invalid auxiliary reviewer / 辅助审核目录必须使用准确内部ID')
    suffix='-aux-'+reviewer_id if reviewer_id else '-reviewer'
    snapshot_key='reviewer_snapshot_applied'+(':'+reviewer_id if reviewer_id else '')
    root=Path(project)/'.codex-pair'/'worktrees'/(store.pair_id+suffix)
    try:
        from .pair_git import prepare_pair_directory
    except ImportError:
        from pair_git import prepare_pair_directory
    prepare_pair_directory(project,str(root.parent),'worktrees')
    if root.exists():
        if not same_review_directory(root,root):fail('Review tree redirected / 审核工作树存在链接或无效目录')
        if git(root,'rev-parse','--git-common-dir')=='.git':fail('Unexpected reviewer repository / 审核路径不是受管工作树')
        if tree_stamp(root)['changes']:
            applied=store.setting(snapshot_key)
            if not applied or applied.get('stamp')!=tree_stamp(root):fail('Review tree changed / 审核工作树含未确认内容，不覆盖或清理')
            verify_review_bytes(root,snapshot=applied['manifest'])
            for relative in applied['manifest']:
                tracked=git(root,'ls-files','--',relative)
                if tracked:review_git(root,'restore','--source=HEAD','--staged','--worktree','--',relative)
                else:
                    path=root/relative
                    expected=applied['manifest'][relative]
                    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==expected:path.unlink()
                    elif path.exists():fail('Snapshot cleanup changed / 不清理未核实内容')
            store.setting(snapshot_key,{})
            if tree_stamp(root)['changes']:fail('Review tree still changed / 保留额外测试产物，不能覆盖')
        verify_review_bytes(root)
    else:
        review_git(project,'worktree','add','--detach',str(root),head)
        verify_review_bytes(root)
    if git(root,'rev-parse','HEAD')!=head:
        review_git(root,'switch','--detach',head)
        verify_review_bytes(root)
    store.setting('managed_tree:'+str(root.resolve()),{'member':'A','role':'reviewer','state':'active','base':head})
    return str(root.resolve())


def materialize_snapshot(store,root,artifact,*,reviewer_id=None):
    if reviewer_id is not None and not re.fullmatch(r'[0-9a-f]{32}',reviewer_id):fail('Invalid auxiliary reviewer / 辅助审核目录必须使用准确内部ID')
    try:from .pair_git import filesystem_path
    except ImportError:from pair_git import filesystem_path
    frozen=filesystem_path(artifact['frozen_root']).resolve(strict=True)
    with store.transaction() as db:project=Path(db.execute('SELECT project FROM collaborations').fetchone()[0])
    if not frozen.is_relative_to(filesystem_path(project/'.codex-pair'/'archives').resolve()):fail('Snapshot outside archive / 只读取本项目冻结快照')
    root=Path(root)
    for relative,expected in artifact['manifest'].items():
        target=filesystem_path(root/relative)
        if target.resolve()!=target.absolute() or not target.resolve().is_relative_to(filesystem_path(root).resolve()):fail('Snapshot target escaped / 快照目标越界')
        if expected is None:
            if target.is_file():target.unlink()
        else:
            source=frozen/relative
            data=filesystem_path(source).read_bytes()
            if hashlib.sha256(data).hexdigest()!=expected:fail('Frozen snapshot changed / 冻结快照摘要变化')
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(data)
    store.setting('reviewer_snapshot_applied'+(':'+reviewer_id if reviewer_id else ''),{'manifest':artifact['manifest'],'stamp':tree_stamp(root)})


def _unique_report_fields(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('Duplicate review report field')
        result[key]=value
    return result


def _terminal_review_report(turn):
    messages=[i for i in turn.get('items',[]) if i.get('type')=='agentMessage']
    if turn.get('status')!='completed':
        return {'verdict':'needs_validation','findings':[],'checks':[],
                'validation_reasons':['turn_not_completed']}
    phases=[i.get('phase') for i in messages]
    # 官方phase为空表示未知；旧格式只兼容已完成回合中的唯一消息。
    if len(messages)==1 and phases==[None]:selected=messages[0]
    elif (phases and all(p in ('commentary','partial_answer','final_answer') for p in phases)
            and phases.count('final_answer')==1 and phases[-1]=='final_answer'):
        selected=messages[-1]
    else:raise ValueError('Missing or ambiguous terminal review report')
    if selected.get('delivery') is not None:
        raise ValueError('Asynchronous review message is not a terminal report')
    text=selected.get('text')
    if not isinstance(text,str) or not text.strip():raise ValueError('Empty terminal review report')
    text=text.strip()
    if text.startswith('```'):
        lines=text.splitlines()
        if len(lines)<3 or lines[0] not in ('```','```json') or lines[-1]!='```':
            raise ValueError('Invalid terminal review report fence')
        text='\n'.join(lines[1:-1]).strip()
    report=json.loads(text,object_pairs_hook=_unique_report_fields)
    if (not isinstance(report,dict) or report.get('verdict') not in ('passed','needs_fix','needs_validation')
            or any(k in report and (not isinstance(report[k],list) or any(not isinstance(v,dict) for v in report[k]))
                   for k in ('findings','checks'))):
        raise ValueError('Invalid terminal review report structure')
    return report


class ReviewerController:
    def __init__(self,store,*,executable=None):
        self.store=store;self.executable=executable
        self.stop_event=threading.Event();self.wake=threading.Event()
        self.thread=threading.Thread(target=self.run,name='codex-pair-reviewer',daemon=True)
        self.client=None;self.state=None;self.pending_permissions={}

    def start(self):self.thread.start()
    def close(self):
        self.stop_event.set();self.wake.set()
        # 后台桥保持存活直到原生回合给出终态；不因等待时长杀在途工具。
        while self.thread.is_alive():self.thread.join(timeout=1)

    def _parent(self):
        a=self.store.member('A')
        with AppServer(codex_home=a['codex_home'],cwd=a['cwd'],executable=self.executable,metadata_only=True,account_metadata=True) as observer:
            actual=observer.validate(a)
            verify_review_service(observer, actual, a['codex_home'])
        return a,actual

    def _session(self,head):
        public=self.store.reviewer_status()['reviewer']
        if public and public['state'] in ('unknown','starting','running'):
            fail('Reviewer reconciliation required / 原审核创建或执行不明，须显式核实后恢复或替换')
        a,actual=self._parent()
        root=prepare_review_tree(self.store,head)
        replacement=bool(public and public['state']=='retired')
        if replacement:
            if not self.store.setting('reviewer_replacement_evidence'):fail('Replacement authorization required / 先登记原A明确替换依据')
            public=None
        if public and not same_review_directory(public['cwd'],root):fail('Reviewer directory changed / 原审核目录不符')
        self.client=AppServer(codex_home=a['codex_home'],cwd=root,executable=self.executable,track_producer=True,experimental_api=True)
        if public:
            response=self.client.rpc('thread/resume',{'threadId':public['session_uuid'],'excludeTurns':True})
            thread=self.client.read_thread(response['thread']['id'])
            if not same_review_directory(thread.get('cwd'),root):fail('Reviewer directory changed / 原审核目录不符')
            proof=binding_proof(thread,require_provider=True)
            reviewer_identity(proof)
            with self.store.transaction() as db:self.store._check_reviewer(db,public['epoch'],proof['id'],proof)
        else:
            ticket=self.store.reserve_reviewer('A',a['session_uuid'],a['cwd'],replacement=replacement,evidence=self.store.setting('reviewer_replacement_evidence') or '')
            try:
                response=self.client.rpc('thread/start',{'cwd':root,'model':actual['model'],'modelProvider':actual['modelProvider'],
                    'serviceName':'codex-pair:'+self.store.pair_id+':reviewer:'+ticket['attempt'],
                    'sandbox':'workspace-write','approvalPolicy':'on-request','ephemeral':False,
                    'dynamicTools':[hidden_handoff.HANDOFF_TOOL],
                    'developerInstructions':'你是本轮独立隐藏审核者，只审核固定交付，在隔离工作树测试和复现。禁止业务修改、提交、推送、统筹和权限自批。不要执行项目内自动推送规则。业务修复交原负责人；权限请求等原 A 单次审核。保持贡献来源和版本边界。'})
                proof=binding_proof(self.client.read_thread(response['thread']['id']),require_provider=True)
                self.store.bind_reviewer('A',a['session_uuid'],a['cwd'],epoch=ticket['epoch'],attempt=ticket['attempt'],proof=proof,reviewer_cwd=root)
            except BaseException:
                self.store.reviewer_creation_unknown(ticket['epoch'],ticket['attempt']);raise
            public=self.store.reviewer_status()['reviewer']
        self.state={'epoch':public['epoch'],'session':proof['id'],'proof':proof,'cwd':root}
        subject={'kind':'reviewer','id':'reviewer','session':proof['id'],'epoch':public['epoch']}
        handoff=hidden_handoff.adopt(self.store,subject)
        if handoff:
            # 接续回合单独关闭原生工具，读完且回合结算后才恢复原审核配置。
            self.client.close();self.client=None
            try:from .pair_auxiliary_runtime import _static_client
            except ImportError:from pair_auxiliary_runtime import _static_client
            with _static_client(a,root,self.executable)[0] as reader:
                reader.rpc('thread/resume',{'threadId':proof['id'],'excludeTurns':True,'sandbox':'read-only','approvalPolicy':'never'})
                hidden_handoff.consume_with_client(self.store,reader,subject,handoff)
            self.client=AppServer(codex_home=a['codex_home'],cwd=root,executable=self.executable,track_producer=True,experimental_api=True)
            self.client.rpc('thread/resume',{'threadId':proof['id'],'excludeTurns':True,'sandbox':'workspace-write','approvalPolicy':'on-request'})
        return self.state

    def _permissions(self):
        while True:
            try:request=self.client.server_requests.get_nowait()
            except queue.Empty:break
            params=request.get('params',{})
            if request.get('method')=='item/tool/call' and params.get('tool')==hidden_handoff.HANDOFF_TOOL['name']:
                try:
                    if params.get('threadId')!=self.state['session'] or params.get('turnId')!=self.state.get('turn_id'):fail('Exact hidden turn required')
                    result=hidden_handoff.handle_tool(self.store,{'kind':'reviewer','id':'reviewer','session':self.state['session'],'epoch':self.state['epoch']},params.get('arguments'),execution=self.state['execution_id'])
                    response={'success':True,'contentItems':[{'type':'inputText','text':encoded(result)}]}
                except Exception as exc:response={'success':False,'contentItems':[{'type':'inputText','text':redact_diagnostic(str(exc))[:1000]}]}
                self.client._write({'id':request['id'],'result':response});continue
            key=(self.state['execution_id'],type(request.get('id')).__name__,str(request.get('id')))
            seen=getattr(self,'_permission_requests',set())
            if key in seen:continue
            seen.add(key);self._permission_requests=seen
            response=permission_response(request,False)
            if response is None:
                self._permission_diagnostic(request,'unsupported')
                self.client._write({'id':request['id'],'error':{'code':-32601,'message':'Unsupported reviewer request'}})
                continue
            try:
                self._settle_before_permission(request)
                permission=self.store.capture_managed_approval(self.state['epoch'],self.state['session'],self.state['execution_id'],request)
                self.pending_permissions[permission['id']]=request
            except Exception as exc:
                self._permission_diagnostic(request,'capture_failed',exc)
                self.client._write({'id':request['id'],'result':response})
        for key,request in list(self.pending_permissions.items()):
            result=self.store.managed_approval_result(key)
            if result['status']=='pending':continue
            self.client._write({'id':request['id'],'result':permission_response(request,result['status']=='allowed')})
            self.pending_permissions.pop(key)

    def _permission_diagnostic(self,request,reason,exc=None):
        # 诊断只留标识和异常类别，失败不能改变权限响应。
        try:
            def bounded(value):
                return redact_diagnostic(str(value))[:160] if isinstance(value,(str,int)) and not isinstance(value,bool) else None
            params=request.get('params',{})
            fields={k:bounded(v) for k,v in {'method':request.get('method'),'request_id':request.get('id'),
                'epoch':self.state['epoch'],'session':self.state['session'],'execution':self.state['execution_id'],
                'turn':params.get('turnId'),'item':params.get('itemId')}.items()}
            with self.store.transaction() as db:
                self.store.event(db,'managed_permission_diagnostic',reason=reason,
                    exception=type(exc).__name__[:80] if exc else None,**fields)
        except Exception:pass

    def _settle_before_permission(self,request):
        state=self.state;params=request.get('params',{});turn_id=state.get('turn_id')
        if not turn_id or params.get('threadId')!=state['session'] or params.get('turnId')!=turn_id:
            fail('Stale permission turn / 新审批不属于当前准确回合')
        with self.store.transaction() as db:
            before=dict(self.store._reviewer_row(db));parent=dict(self.store._member(db,'A'))
            if (before['state']!='running' or before['epoch']!=state['epoch']
                    or before['session_uuid']!=state['session'] or before['execution_id']!=state['execution_id']):
                fail('Stale permission execution / 仅核实本次审核执行')
            if not same_review_directory(before['cwd'],state['cwd']):
                fail('Reviewer directory changed / 当前审批目录与审核配置不符')
            snapshot=permission_binding_snapshot(before,parent,turn_id)
            pending=db.execute("SELECT 1 FROM managed_permissions WHERE epoch=? AND execution_id=? AND session=? AND status IN ('granted','unknown')",
                (state['epoch'],state['execution_id'],state['session'])).fetchone()
            stage=db.execute('SELECT * FROM review_stages WHERE execution_id=? AND turn_id=?',(state['execution_id'],turn_id)).fetchone()
            version=stored_context(self.store,db,before,stage) if stage else None
        if not pending:return
        thread=self.client.read_thread(state['session']);reviewer_identity(thread)
        if (thread.get('modelProvider')!=json.loads(parent['proof']).get('modelProvider')
                or thread.get('id')!=state['session'] or thread.get('model')!=before['model']
                or receipt_cwd(thread.get('cwd'))!=receipt_cwd(before['cwd'])):
            fail('Reviewer identity mismatch / 核实前项时审核身份不符')
        turns=[t for t in self.client.turns(state['session'],items_view='full') if t.get('id')==turn_id]
        if len(turns)!=1:fail('Ambiguous permission turn / 缺少唯一准确回合')
        if turns[0].get('threadId',state['session'])!=state['session']:
            fail('Reviewer identity mismatch / 前项回执不属于原会话')
        turn=dict(turns[0],threadId=state['session'])
        binding={'codex_home':before['codex_home'],'cwd':before['cwd'],'session_uuid':before['session_uuid']}
        fallback=rollout_command_items(binding,thread,turn_id,
            producer_version=version,require_terminal=False)
        turn['rollout_items']=fallback or []
        with self.store.transaction() as db:
            current=dict(self.store._reviewer_row(db));fresh=dict(self.store._member(db,'A'))
            if permission_binding_snapshot(current,fresh,turn_id)!=snapshot:
                fail('Stale execution / 读取前项回执期间绑定变化')
        settle_permissions(self.store,state['epoch'],state['execution_id'],turn,active_turn=turn_id,binding_snapshot=snapshot)

    def _prompt(self,stage):
        material={k:json.loads(stage[k]) for k in ('artifact','contributors','checks','payload')}
        material.update(stage_id=stage['id'],candidate_digest=stage['digest'],requirements_version=stage['requirements_version'])
        material['recovery_context']=self.store.setting('reviewer_recovery_context')
        binding=self._receipt_binding(stage)
        lookup=[sys.executable,'-B',str(Path(__file__).with_name('pair_review_receipts.py')),
            '--state-directory',str(self.store.directory),'--binding',json.dumps(binding,separators=(',',':'))]
        material['receipt_lookup']={'argv':lookup,'powershell':'& '+' '.join("'"+arg.replace("'","''")+"'" for arg in lookup)}
        return ('审核此固定阶段，不先整合。核对需求、允许范围、贡献来源、代码、安全及必要验证。先核对Git候选和干净状态，在当前隔离工作树实际运行必要检查，测试前后核对业务文件。'
            '同一问题沿用稳定ID，原范围阻塞问题交原负责人；不修业务、不提交推送、不担任统筹，不审核审核回执。'
            '结论仅返回一个JSON对象：candidate_digest,verdict(passed/needs_fix/needs_validation),findings数组(id/body/blocking/status/anchor/paths),checks数组(name/required/status/evidence/receipt_id)。'
            'anchor为实际复现证据对应的path和稳定reproduction标识，paths列出所需修改路径；同一问题沿用原锚点，不随任务名或负责人变化。无法可靠定位时不编造锚点，交A协调。'
            '必要检查必须引用本回合实际commandExecution工具项ID及证据，失败、缺失、波动未查明不能通过。非阻塞建议只记录。'
            '完成检查后单独运行receipt_lookup只读查询，取当前绑定和turn下的实际工具项id填receipt_id，不能用chunk_id。查询不可用时记录needs_validation，不猜ID或重跑检查。'
            'coordinator_required_checks.command固定的是实际工具命令形式；必须单独执行，不附加环境赋值、摘要脚本或其他命令。预检后检另开工具项。'
            '若A登记完整shell argv字符串，必须使用其准确shell路径、选项及-Command输入；不能再套一层shell。裸命令被CLI包装而与登记不符时记录needs_validation，请A正规登记准确形式，不自动改写或模糊匹配。'
            '\n材料：'+encoded(material))

    def _receipt_binding(self,stage):
        return {'pair':self.store.pair_id,'stage':stage['id'],'digest':stage['digest'],
            'epoch':self.state['epoch'],'session':self.state['session'],'execution':self.state['execution_id']}

    def _report(self,turn,stage):
        report=_terminal_review_report(turn)
        reasons=[]
        if turn.get('status')!='completed':report['verdict']='needs_validation';reasons.append('turn_not_completed')
        try:from .pair_review_receipts import receipt_items
        except ImportError:from pair_review_receipts import receipt_items
        receipts={};conflicts=set()
        version=(self.state or {}).get('producer_version')
        values=(receipt_items(turn.get('items',[]),version,rollout_items=turn.get('rollout_items',[]))
                if version is not None else turn.get('items',[]))
        for item in values:
            if item.get('type') not in ('commandExecution','CommandExecution'):continue
            key=item.get('id');normalized=command_receipt(item)
            if not normalized or key in receipts and receipts[key]!=normalized:conflicts.add(key)
            else:receipts[key]=normalized
        for key in conflicts:receipts.pop(key,None)
        expected=json.loads(stage['payload']).get('coordinator_required_checks',[])
        provided={c.get('name'):c for c in report.get('checks',[])}
        def in_review_tree(receipt):
            cwd=receipt.get('cwd') if receipt else None
            root=(self.state or {}).get('cwd')
            try:
                return bool(isinstance(cwd,str) and cwd and Path(cwd).is_absolute() and root
                            and canonical_path(cwd)==canonical_path(root))
            except (OSError,ValueError,RuntimeError):return False
        if not expected:report['verdict']='needs_validation';reasons.append('required_checks_not_registered')
        for requirement in expected:
            check=provided.get(requirement['name'],{})
            receipt=receipts.get(str(check.get('receipt_id')))
            if (not in_review_tree(receipt) or check.get('status')!='passed' or not command_matches(requirement['command'],receipt,producer_version=(self.state or {}).get('producer_version'))
                    or receipt.get('status')!='completed' or receipt.get('exitCode')!=0):
                report['verdict']='needs_validation';reasons.append('required_receipt_mismatch:'+requirement['name'])
        for check in report.get('checks',[]):
            if not check.get('required',True):continue
            receipt=receipts.get(str(check.get('receipt_id')))
            if not in_review_tree(receipt) or receipt.get('exitCode')!=0 or receipt.get('status')!='completed':
                report['verdict']='needs_validation';reasons.append('required_receipt_invalid:'+str(check.get('name')))
        if not receipts or any(c.get('required',True) and c.get('status')!='passed' for c in report.get('checks',[])):
            report['verdict']='needs_validation';reasons.append('checks_incomplete')
        if reasons:report['validation_reasons']=reasons
        return report

    def _settle_permissions(self,turn):
        turn=dict(turn,threadId=self.state.get('session'))
        settle_permissions(self.store,self.state['epoch'],self.state['execution_id'],turn)

    def _finish_final_turn(self,stage,turn,proof,*,files_unchanged,observer_factory=AppServer):
        try:from .pair_review_receipts import verified_turn
        except ImportError:from pair_review_receipts import verified_turn
        state=self.state
        producer_failed=False
        if getattr(self.client,'producer_launch',None):
            try:record_producer(self.store,stage['id'],state,self.client.producer_launch,finished=True)
            except (ValueError,KeyError,TypeError,AttributeError,OSError,RuntimeError):producer_failed=True
        snapshot=final_review_snapshot(self.store,stage,state)
        with self.store.transaction() as db:
            producer=validate_final_snapshot(self.store,db,snapshot)
        if producer_failed or producer is None:
            report={'candidate_digest':stage['digest'],'verdict':'needs_validation','findings':[],'checks':[],
                    'validation_reasons':['final_producer_evidence_unverified']}
            return self.store.finish_review(stage['id'],state['epoch'],state['session'],proof,state['owner'],
                state['execution_id'],report,files_unchanged=files_unchanged,receipt_snapshot=snapshot)
        context={'stage':stage['id'],'digest':stage['digest'],'epoch':state['epoch'],
            'session':state['session'],'execution':state['execution_id'],'turn':state['turn_id']}
        notification_messages=[i for i in turn.get('items',[]) if i.get('type')=='agentMessage']
        raw=[i.get('text','') for i in notification_messages]
        safe_evidence(encoded(raw))
        with self.store.transaction() as db:self.store.event(db,'review_model_notification',**context,raw_messages=raw)
        official,version=verified_turn(self.store.directory,self._receipt_binding(stage),observer_factory=observer_factory)
        if (turn.get('id')!=state['turn_id'] or official['id']!=state['turn_id']
                or turn.get('status')!='completed' or official.get('status')!='completed'):
            fail('Final turn unverified / 缺少准确官方完整终态，不结算放行')
        state['producer_version']=version
        messages=[i for i in official['items'] if i.get('type')=='agentMessage']
        if not messages:messages=notification_messages
        raw=[i.get('text','') for i in messages]
        safe_evidence(encoded(raw))
        with self.store.transaction() as db:
            validate_final_snapshot(self.store,db,snapshot)
            self.store.event(db,'review_model_report',**context,raw_messages=raw)
        final=dict(official,items=[i for i in official['items'] if i.get('type')!='agentMessage']+messages)
        settle_permissions(self.store,state['epoch'],state['execution_id'],final,active_turn=state['turn_id'],
            binding_snapshot=snapshot['binding'],final_snapshot=snapshot)
        report=self._report(final,stage)
        safe_evidence(encoded(report))
        with self.store.transaction() as db:
            validate_final_snapshot(self.store,db,snapshot)
            self.store.event(db,'review_controller_report',**context,report=report)
        return self.store.finish_review(stage['id'],state['epoch'],state['session'],proof,state['owner'],
            state['execution_id'],report,files_unchanged=files_unchanged,receipt_snapshot=snapshot)

    def _pin_cli_execution(self,stage,turn_id=None):
        with self.store.transaction() as db:
            required=(self.store._option(db,'member_context_required',False)
                      or self.store._option(db,'closed_scope')
                      or self.store._option(db,'cli_compatibility:A') is not None)
            if not required:return
            current=db.execute('SELECT * FROM review_stages WHERE id=? AND execution_id=? AND reviewer_epoch=? AND reviewer_session=?',
                (stage['id'],self.state['execution_id'],self.state['epoch'],self.state['session'])).fetchone()
            if not current or current['digest']!=stage['digest']:fail('Exact review execution required / CLI绑定必须来自当前持久阶段')
            task_id=current['task_id']
        actor=self.store.member('A')
        pin=self.store.bind_cli_execution('A',actor['session_uuid'],actor['cwd'],self.state['execution_id'],stage['digest'],
            operation='review_settlement',task_id=task_id,turn_id=turn_id)
        launch=self.client.producer_launch
        if pin['executable']!=launch['file'] or pin['version']!=launch['version']:
            fail('Review CLI differs from preflight / 审核实际原生进程与准确CLI预检不符')
        return pin

    def _execute(self,stage):
        state=self.state
        self._parent()
        fresh=binding_proof(self.client.read_thread(state['session']),require_provider=True)
        reviewer_identity(fresh)
        lease=self.store.acquire_reviewer(state['epoch'],state['session'],fresh)
        state.update(owner=lease['owner'],execution_id=lease['execution_id'])
        state.pop('turn_id',None)
        self.store.begin_review(stage['id'],state['epoch'],state['session'],fresh,lease['owner'],lease['execution_id'])
        before=tree_stamp(state['cwd'])
        if before['changes'] and json.loads(stage['artifact'])['kind']!='snapshot':fail('Dirty reviewer tree / 提交候选测试前必须干净')
        known=False;turn_id=None
        try:
            record_producer(self.store,stage['id'],state,self.client.producer_launch)
            self._pin_cli_execution(stage)
            response=self.client.rpc('turn/start',{'threadId':state['session'],'clientUserMessageId':lease['execution_id'],'input':[{'type':'text','text':self._prompt(stage)}]})
            turn_id=response['turn']['id']
            state['turn_id']=turn_id
            self.store.record_review_turn(stage['id'],state['epoch'],lease['execution_id'],turn_id)
            self._pin_cli_execution(stage,turn_id)
            record_producer(self.store,stage['id'],state,self.client.producer_launch)
            interrupt_requested=False
            while True:
                self._permissions()
                while True:
                    try:progress=self.client.progress_events.get_nowait()
                    except queue.Empty:break
                    if progress['thread']==state['session'] and progress['turn']==turn_id:
                        self.store.reviewer_progress(state['epoch'],state['session'],lease['owner'],encoded(progress))
                with self.store.transaction() as db:
                    running=db.execute('SELECT status FROM collaborations').fetchone()[0]=='running'
                    handoff=self.store._role_handoff_pending(db,'reviewer')
                    handoff=handoff or hidden_handoff.pending(self.store,db,{'kind':'reviewer','id':'reviewer','session':state['session'],'epoch':state['epoch']})
                    yield_now=bool(handoff and (handoff['mode']!='finish_current' or self.store._member_suspended(db,'A')))
                if (self.stop_event.is_set() or not running or yield_now) and not interrupt_requested:
                    # 请求取消不是结束证据；保留unknown，后续仅对账不重放。
                    self.client.rpc('turn/interrupt',{'threadId':state['session'],'turnId':turn_id})
                    interrupt_requested=True
                    with self.store.transaction() as db:self.store.event(db,'reviewer_cancel_requested',turn=turn_id,terminal_confirmed=False)
                if self.client._dead:break
                try:
                    turn=self.client.wait_turn(state['session'],turn_id,timeout=1)
                    known=True;break
                except DeliveryUnknown:
                    self.store.reviewer_reminder()
            if not known:return
            after=tree_stamp(state['cwd'])
            actual=binding_proof(self.client.read_thread(state['session']),require_provider=True)
            result=self._finish_final_turn(stage,turn,actual,files_unchanged=before==after)
            self.store.release_reviewer(state['epoch'],state['session'],lease['owner'],outcome='completed',evidence=encoded({'stage':stage['id'],'turn':turn_id,'files_unchanged':before==after,'verdict':result['verdict']}))
        except Exception as exc:
            with self.store.transaction() as db:
                self.store.event(db,'review_finalization_failed',stage=stage['id'],digest=stage['digest'],
                    epoch=state['epoch'],session=state['session'],execution=state['execution_id'],turn=turn_id,
                    effective_verdict='needs_validation',reason=redact_diagnostic(str(exc))[:2000])
            raise
        finally:
            if not known:
                self.store.release_reviewer(state['epoch'],state['session'],lease['owner'],outcome='unknown',evidence=encoded({'stage':stage['id'],'turn':turn_id,'reason':'受管执行回执尚未确认；不重放'}))
            elif self.store.reviewer_status()['reviewer']['state']=='running':
                # 解析失败也保留待验证阶段，不能因模型结束而放行。
                with self.store.transaction() as db:db.execute("UPDATE review_stages SET status='needs_validation' WHERE id=? AND status='running'",(stage['id'],))
                self.store.release_reviewer(state['epoch'],state['session'],lease['owner'],outcome='failed',evidence='审核回合结束但有效结论或文件核验不足；阶段待验证')
            with self.store.transaction() as db:collaboration_running=db.execute('SELECT status FROM collaborations').fetchone()[0]=='running'
            if not known or self.stop_event.is_set() or not collaboration_running or self.store._reviewer_quota_paused():
                self.store.save_reviewer_checkpoint('受管审核在安全边界保存：终态已知='+str(known)+'，保留原回合和队列；恢复前完整读取及核实，不重放未知工具')
            if known:
                verified=next((item for item in self.client.turns(state['session'],items_view='full') if item['id']==turn_id),None)
                if verified and verified.get('status') in ('completed','failed','interrupted'):
                    bound={'session_uuid':state['session'],'codex_home':self.store.member('A')['codex_home'],'cwd':state['cwd']}
                    verified=dict(verified,threadId=state['session'])
                    raw=rollout_command_items(bound,self.client.read_thread(state['session']),turn_id,producer_version=self.client.producer_launch['version'],require_terminal=True,terminal_turn=verified)
                    if raw is not None:hidden_handoff.seal(self.store,{'kind':'reviewer','id':'reviewer','session':state['session'],'epoch':state['epoch']},receipt=encoded({'turn':turn_id,'digest':digest(verified),'raw_digest':digest(raw),'source':'official_and_rollout'}))

    def run(self):
        try:
            from .pair_core import FileLock
        except ImportError:
            from pair_core import FileLock
        try:
            with FileLock(self.store.directory/'reviewer-process.lock',timeout=0):
                while not self.stop_event.is_set():
                    self.store.reviewer_reminder()
                    with self.store.transaction() as db:
                        active=db.execute('SELECT status FROM collaborations').fetchone()[0]=='running'
                        enabled=self.store._option(db,'reviewer_auto',False)
                        current=self.store._reviewer_row(db)
                        if current and current['session_uuid']:
                            waiting=hidden_handoff.pending(self.store,db,{'kind':'reviewer','id':'reviewer','session':current['session_uuid'],'epoch':current['epoch']})
                            if waiting and waiting['state'] in ('requested','saved'):enabled=False
                        stage=next((candidate for candidate in db.execute("SELECT * FROM review_stages WHERE status='pending' ORDER BY priority DESC,created,id")
                                    if not self.store._recovery_gate(db,member='A',task_id=candidate['task_id'],action='review')),None)
                    if active and enabled and stage and not self.store._reviewer_quota_paused():
                        artifact=json.loads(stage['artifact'])
                        try:
                            current=self.store.reviewer_status()['reviewer']
                            if self.client and current and (current['state']=='retired' or current['epoch']!=self.state['epoch']):
                                self.client.close();self.client=None;self.state=None
                            head=artifact.get('head') or artifact['baseline']
                            if not self.client:self._session(head)
                            selected=self.store.next_review(self.state['epoch'],self.state['session'],self.state['proof'])
                            if selected:
                                target=json.loads(selected['artifact'])
                                root=prepare_review_tree(self.store,target.get('head') or target['baseline'])
                                if target['kind']=='snapshot':materialize_snapshot(self.store,root,target)
                                self._execute(selected)
                        except Exception as exc:
                            if any(term in str(exc).lower() for term in ('rate limit','quota exceeded','usage limit')):
                                self.store.quota_error('A','官方审核调用返回限流或额度错误；停止同池新派发')
                            with self.store.transaction() as db:
                                self.store.event(db,'reviewer_blocked',reason=redact_diagnostic(str(exc)),automatic_replay=False)
                                self.store._put(db,'reviewer_auto',False)
                    self.wake.wait(1);self.wake.clear()
        finally:
            if self.client:self.client.close()


def settle_permissions(store,epoch,execution_id,turn,*,active_turn=None,binding_snapshot=None,final_snapshot=None):
    with store.transaction() as db:
        if final_snapshot is not None:validate_final_snapshot(store,db,final_snapshot)
        runtime=store._reviewer_row(db)
        if not runtime or runtime['epoch']!=epoch or runtime['execution_id']!=execution_id:return
        parent=store._member(db,'A')
        if active_turn is not None:
            if (not binding_snapshot or active_turn!=turn.get('id')
                    or permission_binding_snapshot(runtime,parent,active_turn)!=binding_snapshot):
                fail('Stale execution / 实际结算事务中的审核配置已变化')
        proof=json.loads(parent['proof'])
        stage=db.execute('SELECT * FROM review_stages WHERE execution_id=? AND turn_id=?',(execution_id,turn.get('id'))).fetchone()
        record=store._option(db,KEY+execution_id)
        if record is not None and not stage:raise ValueError('Unbound producer settlement')
        version=stored_context(store,db,runtime,stage,live=runtime['state']=='running')
        if strict_version(version) and (turn.get('threadId')!=version.binding['session'] or turn.get('id')!=version.binding['turn']):
            raise ValueError('Producer turn mismatch')
        try:from .pair_review_receipts import receipt_items
        except ImportError:from pair_review_receipts import receipt_items
        items={item['id']:item for item in receipt_items(turn.get('items',[]),version,
            rollout_items=turn.get('rollout_items',[]))}
        for row in db.execute("SELECT * FROM managed_permissions WHERE epoch=? AND execution_id=? AND status IN ('granted','unknown')",(epoch,execution_id)).fetchall():
            request=json.loads(row['request']);params=request.get('params',{})
            if turn.get('id')!=params.get('turnId'):continue
            if active_turn is not None and request.get('method')!='item/commandExecution/requestApproval':continue
            item=items.get(params.get('itemId'));outcome='unknown'
            binding=store._option(db,'managed_binding:'+row['id'])
            current={'session':parent['session_uuid'],'version':parent['binding_version'],
                     'home':parent['codex_home'],'proof':parent['proof']}
            # 旧审批缺少快照时只接受创建后无换绑历史的原绑定，不猜测旧身份。
            binding_ok=(binding==current if binding is not None else (
                parent['joined']<=row['created'] and not db.execute(
                    "SELECT 1 FROM member_history WHERE member='A' AND created>=?",(row['created'],)).fetchone()))
            if active_turn is not None and isinstance(binding,dict):
                # live快照已复核目录实体，仅统一home表示；其他身份字段仍逐字一致。
                binding_ok=(dict(binding,home=current['home'])==current
                    and same_review_directory(binding.get('home'),current['home']))
            if (version is not None and binding_ok and parent['session_uuid']==runtime['parent_session']
                    and canonical_path(parent['codex_home'])==canonical_path(runtime['codex_home'])
                    and proof.get('modelProvider')==store._option(db,'reviewer_provider','openai') and proof.get('model')==runtime['model']
                    and (runtime['state'] in ('running','unknown') or runtime['state']=='idle' and not runtime['owner'] and row['status'] in ('unknown','granted'))
                    and row['session']==runtime['session_uuid']==params.get('threadId')==turn.get('threadId')
                    and (turn.get('status') in ('completed','failed','interrupted') or
                         active_turn==turn.get('id') and active_turn is not None and runtime['state']=='running'
                         and turn.get('status')=='inProgress' and request.get('method')=='item/commandExecution/requestApproval')
                    and item
                    and isinstance(params.get('command'),str) and command_matches(params['command'],item,producer_version=version)
                    and receipt_cwd(params.get('cwd'))==item['cwd']):
                outcome='success' if item['exitCode']==0 else 'failed'
            summary={k:item.get(k) for k in ('id','type','status','exitCode')} if item else None
            source=turn.get('receipt_sources',{}).get(params.get('itemId'),'official_turn_receipt')
            db.execute('UPDATE managed_permissions SET status=?,reason=? WHERE id=?',(outcome,encoded({'item':summary,'source':source,
                'thread':runtime['session_uuid'],'turn':turn['id'],'epoch':epoch,'execution_id':execution_id}),row['id']))
            store.event(db,'managed_permission_execution_result',approval=row['id'],outcome=outcome)


def reconcile_review(store,member,session,cwd,stage_id,evidence):
    """只读官方持久回合对账；未知不重放，不直接放行代码。"""
    safe_evidence(evidence)
    with store.transaction() as db:
        store._a_actor(db,member,session,cwd)
        stage=db.execute('SELECT * FROM review_stages WHERE id=?',(stage_id,)).fetchone()
        runtime=store._reviewer_row(db)
        if not stage or not runtime or not stage['turn_id']:fail('Execution unknown / 缺少准确回合ID，需人工核实，不自动匹配历史')
        parent=store._member(db,'A')
        idle_unsettled=(runtime['state']=='idle' and not runtime['owner'] and db.execute(
            "SELECT 1 FROM managed_permissions WHERE epoch=? AND execution_id=? AND session=? AND status IN ('unknown','granted')",
            (runtime['epoch'],runtime['execution_id'],runtime['session_uuid'])).fetchone())
        if (runtime['state'] not in ('running','unknown') and not idle_unsettled) or stage['status'] not in ('running','needs_validation'):fail('Execution not reconcilable / 仅核实原准确未知执行或工具回执')
        if stage['reviewer_epoch']!=runtime['epoch'] or stage['reviewer_session']!=runtime['session_uuid'] or stage['execution_id']!=runtime['execution_id']:fail('Stale execution / 仅对账原准确执行')
        version=stored_context(store,db,runtime,stage,live=False)
        producer_record=store._option(db,KEY+runtime['execution_id'])
        # 将读取前的身份和阶段快照带入实际结算事务，避免两次事务之间换绑。
        snapshot={'stage':{k:stage[k] for k in ('id','digest','status','requirements_version','reviewer_epoch','reviewer_session','execution_id','turn_id','payload','artifact')},
                  'binding':permission_binding_snapshot(runtime,parent,stage['turn_id']),'producer':producer_record}
    observer_options=dict(codex_home=runtime['codex_home'],cwd=runtime['cwd'],metadata_only=True)
    if producer_record:observer_options['executable']=producer_record['launch']['file']['path']
    with AppServer(**observer_options) as observer:
        thread=observer.read_thread(runtime['session_uuid']);reviewer_identity(thread)
        if thread.get('modelProvider')!=json.loads(parent['proof']).get('modelProvider') or thread.get('id')!=runtime['session_uuid'] or thread.get('model')!=runtime['model'] or receipt_cwd(thread.get('cwd'))!=receipt_cwd(runtime['cwd']):fail('Reviewer identity mismatch / 原审核会话、模型或目录不符')
        # 惰性分页必须在连接仍打开时消费；完整视图仅用于原审核对账。
        turns=observer.turns(runtime['session_uuid'],items_view='full')
        matches=[t for t in turns if t.get('id')==stage['turn_id']]
        if len(matches)>1:fail('Ambiguous reconciliation turn / 原准确回合重复，保留状态等待核实')
        turn=matches[0] if matches else None
        if turn:
            if 'threadId' in turn and turn['threadId']!=runtime['session_uuid']:
                fail('Reviewer turn identity mismatch / 官方回合所属会话矛盾，拒绝覆盖')
            turn=dict(turn,threadId=runtime['session_uuid'])
            member_binding={'codex_home':runtime['codex_home'],'cwd':runtime['cwd'],'session_uuid':runtime['session_uuid']}
            fallback=rollout_command_items(member_binding,thread,stage['turn_id'],producer_version=version,terminal_turn=turn)
            turn['rollout_items']=fallback or []
            if fallback is not None:
                turn['receipt_sources']={item['id']:'official_rollout_item_completed' for item in fallback}
            if not turn.get('completedAt'):
                proof=observer.rollout_completion(member_binding,thread,{},stage['turn_id'])
                if not proof or proof.get('completion_source')!='rollout_task_complete':fail('Turn unresolved / 缺少原回合持久终态')
                turn.update(status='completed',completedAt=proof['completed_at'])
    if not turn or turn.get('status') not in ('completed','failed','interrupted'):fail('Turn unresolved / 未核实结束，不撤销执行占用')
    with store.transaction() as db:
        current=store._reviewer_row(db)
        current_parent=store._member(db,'A')
        original=db.execute('SELECT * FROM review_stages WHERE id=?',(stage_id,)).fetchone()
        if (not current or any(current[k]!=runtime[k] for k in ('epoch','session_uuid','execution_id','state','owner'))
                or any(current_parent[k]!=parent[k] for k in ('session_uuid','binding_version','codex_home','proof'))
                or not original or any(original[k]!=stage[k] for k in ('reviewer_epoch','reviewer_session','execution_id','turn_id','requirements_version','status'))):
            fail('Stale execution / 对账期间绑定或阶段已变化，保留原状态')
    settle_permissions(store,runtime['epoch'],runtime['execution_id'],turn,final_snapshot=snapshot)
    if idle_unsettled:
        # 原租约已经结束；只核实遗留工具回执，不再次释放或创建执行。
        with store.transaction() as db:
            validate_final_snapshot(store,db,snapshot)
            current=store._reviewer_row(db)
            if any(current[k]!=runtime[k] for k in ('epoch','session_uuid','execution_id','state','owner')):fail('Stale execution / 原执行已变化')
            store.event(db,'idle_managed_receipt_reconciled',epoch=runtime['epoch'],execution_id=runtime['execution_id'],turn=turn['id'],evidence=evidence)
        result={'state':'idle','replayed':False,'work_approved':False}
    else:
        result=store.reconcile_reviewer_execution(member,session,cwd,epoch=runtime['epoch'],reviewer_session=runtime['session_uuid'],execution_id=runtime['execution_id'],outcome='completed' if turn['status']=='completed' else 'failed',evidence=evidence)
    with store.transaction() as db:
        if idle_unsettled:validate_final_snapshot(store,db,snapshot)
        db.execute("UPDATE review_stages SET status='needs_validation',updated=? WHERE id=?",(time.time(),stage_id))
        store.event(db,'review_reconciled',stage=stage_id,turn=turn['id'],status=turn['status'],reconciliation=evidence,work_approved=False)
    return result
