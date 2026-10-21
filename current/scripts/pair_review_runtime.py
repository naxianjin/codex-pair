"""唯一受管审核进程；空闲只等待本地队列，不启动模型轮询回合。"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import queue
import subprocess
import threading
import time

try:
    from .pair_transport import AppServer,DeliveryUnknown,TransportError,binding_proof,redact_diagnostic,canonical_path
    from .pair_reviewer import official_identity,fail
    from .pair_managed import permission_response
    from .pair_stages import encoded,digest
except ImportError:
    from pair_transport import AppServer,DeliveryUnknown,TransportError,binding_proof,redact_diagnostic,canonical_path
    from pair_reviewer import official_identity,fail
    from pair_managed import permission_response
    from pair_stages import encoded,digest


def git(root,*args):
    result=subprocess.run(['git','-C',str(root),*args],capture_output=True,encoding='utf-8',errors='replace')
    if result.returncode:raise RuntimeError(redact_diagnostic(result.stderr.strip()))
    return result.stdout.strip()


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


def prepare_review_tree(store,head):
    with store.transaction() as db:
        project=db.execute('SELECT project FROM collaborations').fetchone()[0]
    root=Path(project)/'.codex-pair'/'worktrees'/(store.pair_id+'-reviewer')
    try:
        from .pair_git import prepare_pair_directory
    except ImportError:
        from pair_git import prepare_pair_directory
    prepare_pair_directory(project,str(root.parent),'worktrees')
    if root.exists():
        if root.is_symlink():fail('Review tree redirected / 审核工作树存在链接')
        if tree_stamp(root)['changes']:
            applied=store.setting('reviewer_snapshot_applied')
            if not applied or applied.get('stamp')!=tree_stamp(root):fail('Review tree changed / 审核工作树含未确认内容，不覆盖或清理')
            for relative in applied['manifest']:
                tracked=git(root,'ls-files','--',relative)
                if tracked:git(root,'restore','--source=HEAD','--staged','--worktree','--',relative)
                else:
                    path=root/relative
                    expected=applied['manifest'][relative]
                    if path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()==expected:path.unlink()
                    elif path.exists():fail('Snapshot cleanup changed / 不清理未核实内容')
            store.setting('reviewer_snapshot_applied',{})
            if tree_stamp(root)['changes']:fail('Review tree still changed / 保留额外测试产物，不能覆盖')
        if git(root,'rev-parse','--git-common-dir')=='.git':fail('Unexpected reviewer repository / 审核路径不是受管工作树')
    else:
        git(project,'worktree','add','--detach',str(root),head)
    if git(root,'rev-parse','HEAD')!=head:git(root,'switch','--detach',head)
    store.setting('managed_tree:'+str(root.resolve()),{'member':'A','role':'reviewer','state':'active','base':head})
    return str(root.resolve())


def materialize_snapshot(store,root,artifact):
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
    store.setting('reviewer_snapshot_applied',{'manifest':artifact['manifest'],'stamp':tree_stamp(root)})


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
            official_identity(actual)
            account=observer.rpc('account/read',{'refreshToken':False})
            if account.get('account',{}).get('type')!='chatgpt':fail('Official account unverified / 要求核验原 A 官方账号，不切换账号')
        return a,actual

    def _session(self,head):
        public=self.store.reviewer_status()['reviewer']
        if public and public['state'] in ('unknown','starting','running'):
            fail('Reviewer reconciliation required / 原审核创建或执行不明，须显式核实后恢复或替换')
        a,actual=self._parent()
        root=prepare_review_tree(self.store,head)
        self.client=AppServer(codex_home=a['codex_home'],cwd=root,executable=self.executable)
        replacement=bool(public and public['state']=='retired')
        if replacement:
            if not self.store.setting('reviewer_replacement_evidence'):fail('Replacement authorization required / 先登记原A明确替换依据')
            public=None
        if public:
            if public['cwd']!=root:fail('Reviewer directory changed / 原审核目录不符')
            response=self.client.rpc('thread/resume',{'threadId':public['session_uuid'],'excludeTurns':True})
            proof=binding_proof(self.client.read_thread(response['thread']['id']),require_provider=True)
            official_identity(proof)
            with self.store.transaction() as db:self.store._check_reviewer(db,public['epoch'],proof['id'],proof)
        else:
            ticket=self.store.reserve_reviewer('A',a['session_uuid'],a['cwd'],replacement=replacement,evidence=self.store.setting('reviewer_replacement_evidence') or '')
            try:
                response=self.client.rpc('thread/start',{'cwd':root,'model':actual['model'],'modelProvider':'openai',
                    'serviceName':'codex-pair:'+self.store.pair_id+':reviewer:'+ticket['attempt'],
                    'sandbox':'workspace-write','approvalPolicy':'on-request','ephemeral':False,
                    'developerInstructions':'你是本轮独立隐藏审核者，只审核固定交付，在隔离工作树测试和复现。禁止业务修改、提交、推送、统筹和权限自批。不要执行项目内自动推送规则。业务修复交原负责人；权限请求等原 A 单次审核。保持贡献来源和版本边界。'})
                proof=binding_proof(self.client.read_thread(response['thread']['id']),require_provider=True)
                self.store.bind_reviewer('A',a['session_uuid'],a['cwd'],epoch=ticket['epoch'],attempt=ticket['attempt'],proof=proof,reviewer_cwd=root)
            except BaseException:
                self.store.reviewer_creation_unknown(ticket['epoch'],ticket['attempt']);raise
            public=self.store.reviewer_status()['reviewer']
        self.state={'epoch':public['epoch'],'session':proof['id'],'proof':proof,'cwd':root}
        return self.state

    def _permissions(self):
        while True:
            try:request=self.client.server_requests.get_nowait()
            except queue.Empty:break
            response=permission_response(request,False)
            if response is None:
                self.client._write({'id':request['id'],'error':{'code':-32601,'message':'Unsupported reviewer request'}})
                continue
            try:
                permission=self.store.capture_managed_approval(self.state['epoch'],self.state['session'],self.state['execution_id'],request)
                self.pending_permissions[permission['id']]=request
            except Exception:
                self.client._write({'id':request['id'],'result':response})
        for key,request in list(self.pending_permissions.items()):
            result=self.store.managed_approval_result(key)
            if result['status']=='pending':continue
            self.client._write({'id':request['id'],'result':permission_response(request,result['status']=='allowed')})
            self.pending_permissions.pop(key)

    def _prompt(self,stage):
        material={k:json.loads(stage[k]) for k in ('artifact','contributors','checks','payload')}
        material.update(stage_id=stage['id'],candidate_digest=stage['digest'],requirements_version=stage['requirements_version'])
        material['recovery_context']=self.store.setting('reviewer_recovery_context')
        return ('审核此固定阶段，不先整合。核对需求、允许范围、贡献来源、代码、安全及必要验证。先核对Git候选和干净状态，在当前隔离工作树实际运行必要检查，测试前后核对业务文件。'
            '同一问题沿用稳定ID，原范围阻塞问题交原负责人；不修业务、不提交推送、不担任统筹，不审核审核回执。'
            '结论仅返回一个JSON对象：candidate_digest,verdict(passed/needs_fix/needs_validation),findings数组(id/body/blocking/status/anchor/paths),checks数组(name/required/status/evidence/receipt_id)。'
            'anchor为实际复现证据对应的path和稳定reproduction标识，paths列出所需修改路径；同一问题沿用原锚点，不随任务名或负责人变化。无法可靠定位时不编造锚点，交A协调。'
            '必要检查必须引用本回合实际commandExecution工具项ID及证据，失败、缺失、波动未查明不能通过。非阻塞建议只记录。'
            '\n材料：'+encoded(material))

    def _report(self,turn,stage):
        text='\n'.join(i.get('text','') for i in turn.get('items',[]) if i.get('type')=='agentMessage').strip()
        if text.startswith('```'):
            text=text.split('\n',1)[1].rsplit('```',1)[0].strip()
        if turn.get('status')!='completed' and not text:
            report={'verdict':'needs_validation','findings':[],'checks':[]}
        else:
            report=json.loads(text)
        if turn.get('status')!='completed':report['verdict']='needs_validation'
        receipts={str(i.get('id')):i for i in turn.get('items',[]) if i.get('type')=='commandExecution'}
        expected=json.loads(stage['payload']).get('coordinator_required_checks',[])
        provided={c.get('name'):c for c in report.get('checks',[])}
        def in_review_tree(receipt):
            cwd=receipt.get('cwd') if receipt else None
            root=(self.state or {}).get('cwd')
            try:
                return bool(isinstance(cwd,str) and cwd and Path(cwd).is_absolute() and root
                            and canonical_path(cwd)==canonical_path(root))
            except (OSError,ValueError,RuntimeError):return False
        if not expected:report['verdict']='needs_validation'
        for requirement in expected:
            check=provided.get(requirement['name'],{})
            receipt=receipts.get(str(check.get('receipt_id')))
            if (not in_review_tree(receipt) or check.get('status')!='passed' or receipt.get('command')!=requirement['command']
                    or receipt.get('status')!='completed' or receipt.get('exitCode')!=0):report['verdict']='needs_validation'
        for check in report.get('checks',[]):
            if not check.get('required',True):continue
            receipt=receipts.get(str(check.get('receipt_id')))
            if not in_review_tree(receipt) or receipt.get('exitCode')!=0 or receipt.get('status')!='completed':report['verdict']='needs_validation'
        if not receipts or any(c.get('required',True) and c.get('status')!='passed' for c in report.get('checks',[])):report['verdict']='needs_validation'
        return report

    def _settle_permissions(self,turn):
        settle_permissions(self.store,self.state['epoch'],self.state['execution_id'],turn)

    def _execute(self,stage):
        state=self.state
        self._parent()
        fresh=binding_proof(self.client.read_thread(state['session']),require_provider=True)
        official_identity(fresh)
        lease=self.store.acquire_reviewer(state['epoch'],state['session'],fresh)
        state.update(owner=lease['owner'],execution_id=lease['execution_id'])
        self.store.begin_review(stage['id'],state['epoch'],state['session'],fresh,lease['owner'],lease['execution_id'])
        before=tree_stamp(state['cwd'])
        if before['changes'] and json.loads(stage['artifact'])['kind']!='snapshot':fail('Dirty reviewer tree / 提交候选测试前必须干净')
        known=False;turn_id=None
        try:
            response=self.client.rpc('turn/start',{'threadId':state['session'],'clientUserMessageId':lease['execution_id'],'input':[{'type':'text','text':self._prompt(stage)}]})
            turn_id=response['turn']['id']
            self.store.record_review_turn(stage['id'],state['epoch'],lease['execution_id'],turn_id)
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
            self._settle_permissions(turn)
            after=tree_stamp(state['cwd'])
            actual=binding_proof(self.client.read_thread(state['session']),require_provider=True)
            report=self._report(turn,stage)
            result=self.store.finish_review(stage['id'],state['epoch'],state['session'],actual,lease['owner'],lease['execution_id'],report,files_unchanged=before==after)
            report['verdict']=result['verdict']
            self.store.release_reviewer(state['epoch'],state['session'],lease['owner'],outcome='completed',evidence=encoded({'stage':stage['id'],'turn':turn_id,'files_unchanged':before==after,'verdict':report['verdict']}))
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
                        stage=db.execute("SELECT * FROM review_stages WHERE status='pending' ORDER BY priority DESC,created,id LIMIT 1").fetchone()
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


def settle_permissions(store,epoch,execution_id,turn):
    items={str(i.get('id')):i for i in turn.get('items',[])}
    with store.transaction() as db:
        for row in db.execute("SELECT * FROM managed_permissions WHERE epoch=? AND execution_id=? AND status IN ('granted','unknown')",(epoch,execution_id)).fetchall():
            request=json.loads(row['request']);params=request.get('params',{})
            if turn.get('id')!=params.get('turnId'):continue
            item=items.get(str(params.get('itemId')));outcome='unknown'
            if item and item.get('status')=='completed':
                if item.get('type')=='commandExecution' and isinstance(item.get('exitCode'),int):outcome='success' if item['exitCode']==0 else 'failed'
                elif item.get('type')=='fileChange':outcome='success'
            elif item and item.get('status')=='failed':outcome='failed'
            summary={k:item.get(k) for k in ('id','type','status','exitCode')} if item else None
            db.execute('UPDATE managed_permissions SET status=?,reason=? WHERE id=?',(outcome,encoded({'item':summary,'source':'official_turn_receipt'}),row['id']))
            store.event(db,'managed_permission_execution_result',approval=row['id'],outcome=outcome)


def reconcile_review(store,member,session,cwd,stage_id,evidence):
    """只读官方持久回合对账；未知不重放，不直接放行代码。"""
    with store.transaction() as db:
        store._a_actor(db,member,session,cwd)
        stage=db.execute('SELECT * FROM review_stages WHERE id=?',(stage_id,)).fetchone()
        runtime=store._reviewer_row(db)
        if not stage or not runtime or not stage['turn_id']:fail('Execution unknown / 缺少准确回合ID，需人工核实，不自动匹配历史')
        if stage['reviewer_epoch']!=runtime['epoch'] or stage['execution_id']!=runtime['execution_id']:fail('Stale execution / 仅对账原准确执行')
    with AppServer(codex_home=runtime['codex_home'],cwd=runtime['cwd'],metadata_only=True) as observer:
        thread=observer.read_thread(runtime['session_uuid']);official_identity(thread)
        turns=observer.turns(runtime['session_uuid'])
    turn=next((t for t in turns if t.get('id')==stage['turn_id']),None)
    if not turn or turn.get('status') not in ('completed','failed','interrupted'):fail('Turn unresolved / 未核实结束，不撤销执行占用')
    settle_permissions(store,runtime['epoch'],runtime['execution_id'],turn)
    result=store.reconcile_reviewer_execution(member,session,cwd,epoch=runtime['epoch'],reviewer_session=runtime['session_uuid'],execution_id=runtime['execution_id'],outcome='completed' if turn['status']=='completed' else 'failed',evidence=evidence)
    with store.transaction() as db:
        db.execute("UPDATE review_stages SET status='needs_validation',evidence=? WHERE id=?",(encoded({'turn':turn['id'],'status':turn['status'],'reconciliation':evidence}),stage_id))
    return result
