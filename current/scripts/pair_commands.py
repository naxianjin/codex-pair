"""多人开发命令，复用原入口的身份校验，不从业务正文取得新增授权。"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

try:
    from .pair_transport import AppServer,binding_proof
    from .pair_reviewer import fail,official_identity,safe_evidence
    from . import pair_workspace
except ImportError:
    from pair_transport import AppServer,binding_proof
    from pair_reviewer import fail,official_identity,safe_evidence
    import pair_workspace


COMMANDS={'invite-member','confirm-member','member-presence','reverify-member','retire-member','transfer-approval',
    'revise-requirements','configure-task','submit-stage','reviewer-policy','reviewer-reconcile','reviewer-retire',
    'review-issue','task-boundary','propose-reserve','record-quota-error','create-worktree','update-baseline',
    'cleanup-worktree','publish-policy','publish-stage','review-status','resume-stage','submit-integration','reviewer-replace','mark-unavailable','read-member-context','read-reviewer-checkpoint','consume-reviewer-checkpoint','cancel-task'}


def register(command,actor,body):
    for name in sorted(COMMANDS):
        cmd=command(name,'多人阶段协作：'+name)
        actor(cmd)
        if name in ('review-status','role-handoff-status'):continue
        group=cmd.add_mutually_exclusive_group(required=True)
        group.add_argument('--input-file',help='UTF-8 JSON，字段见 references/team-commands.md')
        group.add_argument('--input-stdin',action='store_true')


COMMANDS.update({'settle-coordination','request-role-handoff','save-role-handoff','read-role-handoff',
                 'ack-role-handoff','designate-role-successor','take-role-handoff','role-handoff-status'})


def execute(store,args,cli):
    if args.command not in COMMANDS:return None
    cli.validate_actor(store,args,allow_configuration_change=args.command in ('member-presence','reverify-member'))
    actor=(args.member,args.session,args.worktree)
    if args.command=='role-handoff-status':return store.role_handoff_status(*actor)
    if args.command=='review-status':
        with store.transaction() as db:return {'team':store._team_status(db),'stages':store._stage_status(db),'reviewer':store._reviewer_status(db)}
    payload=json.load(sys.stdin) if args.input_stdin else json.loads(Path(args.input_file).read_text(encoding='utf-8-sig'))
    if not isinstance(payload,dict):fail('Object required / 输入须为JSON对象')
    p=payload;name=args.command
    if name=='request-role-handoff':
        return store.request_role_handoff(*actor,target=p.get('target',args.member),progress=p.get('progress'),evidence=p['evidence'])
    if name=='save-role-handoff':
        return store.save_role_handoff(*actor,p['id'],body=p['body'],execution_resolved=p.get('execution_resolved'),blocked_reason=p.get('blocked_reason',''),refresh=p.get('refresh',False),message_id=p.get('message'),token=p.get('token'))
    if name=='read-role-handoff':
        return store.read_role_handoff(*actor,p['id'],offset=p.get('offset',0),limit=p.get('limit',2000))
    if name=='ack-role-handoff':
        return store.ack_role_handoff(*actor,p['id'],p['digest'],plan=p['plan'],message_id=p.get('message'),token=p.get('token'))
    if name=='designate-role-successor':
        return store.designate_role_successor(*actor,p['id'],target=p['target'])
    if name=='take-role-handoff':
        return store.take_role_handoff(*actor,p['id'],target=args.member,plan=p['plan'])
    if name=='read-member-context':return store.read_member_context(*actor,offset=p.get('offset',0),limit=p.get('limit',2000))
    if name=='read-reviewer-checkpoint':return store.read_reviewer_checkpoint(*actor,offset=p.get('offset',0),limit=p.get('limit',2000),recover=p.get('recover',False))
    if name=='consume-reviewer-checkpoint':return store.consume_reviewer_checkpoint(*actor,p['digest'],p['plan'])
    if name=='settle-coordination':
        safe_evidence(p['evidence'])
        with store.transaction() as db:
            store._a_actor(db,*actor)
            task=db.execute('SELECT * FROM tasks WHERE id=?',(p['task'],)).fetchone()
            if not task or task['task_kind'] not in ('planning','coordination') or task['status'] not in ('completed','review_wait'):
                fail('Delivered coordination required / 仅收尾已交付的协调或规划任务')
            if json.loads(task['commits_json'] or '[]') or json.loads(task['required_checks'] or '[]') or db.execute('SELECT 1 FROM review_stages WHERE task_id=?',(task['id'],)).fetchone():
                fail('Business review required / 含提交、必要检查或审核阶段，不得按协调回执放行')
            if any(m['task_id']==task['id'] for m in store._running(db)) or store._approval_unresolved(db,task_id=task['id']):
                fail('Execution unresolved / 先核实原执行及审批终态')
            if db.execute("SELECT 1 FROM messages WHERE task_id=? AND process_status<>'completed'",(task['id'],)).fetchone():
                fail('Receipt incomplete / 先完整读取并完成原交付回执')
            store._require_ack(db,args.member,'delivery:'+task['id'])
            db.execute("UPDATE tasks SET status='completed',review_required=0 WHERE id=?",(task['id'],))
            store.event(db,'coordination_settled',task=task['id'],evidence=p['evidence'])
            store._schedule(db)
            return {'settled':task['id']}
    if name=='cancel-task':
        safe_evidence(p['evidence'])
        with store.transaction() as db:
            store._a_actor(db,*actor)
            task=db.execute('SELECT * FROM tasks WHERE id=?',(p['task'],)).fetchone()
            if not task or not p.get('execution_resolved'):fail('Cancellation reconciliation required / 明确任务及执行已经结束的核查证据')
            if any(m['task_id']==task['id'] for m in store._running(db)) or store._approval_unresolved(db,task_id=task['id']):fail('Execution unresolved / 未知或在途操作不得冒充取消完成')
            if db.execute("SELECT 1 FROM review_stages WHERE task_id=? AND status='running'",(task['id'],)).fetchone():fail('Reviewer still running / 原审核回合尚未结束')
            db.execute("UPDATE tasks SET status='cancelled',result=?,updated=? WHERE id=?",(p['evidence'],__import__('time').time(),task['id']))
            db.execute("UPDATE review_stages SET status='stale' WHERE task_id=?",(task['id'],))
            db.execute('DELETE FROM resource_locks WHERE task_id=?',(task['id'],))
            db.execute("UPDATE messages SET process_status='completed',delivery_status=CASE WHEN delivery_status IN ('pending','retry') THEN 'failed' ELSE delivery_status END WHERE task_id=?",(task['id'],))
            store.event(db,'task_cancelled',task=task['id'],evidence=p['evidence'],files_preserved=True)
        return {'cancelled':True,'files_and_history_preserved':True,'replayed':False}
    if name=='mark-unavailable':
        safe_evidence(p['user_instruction']);safe_evidence(p['evidence'])
        with store.transaction() as db:
            store._binding(db,*actor)
            if not p.get('permanent'):fail('Explicit permanent declaration required / 临时离线不等于永久不可用')
            target=store._member(db,p['target'])
            db.execute("UPDATE members SET lifecycle='suspended',online=0,verified=0 WHERE id=?",(target['id'],))
            store.event(db,'member_permanently_unavailable',target=target['id'],user_instruction=p['user_instruction'],evidence=p['evidence'])
        return {'new_work_paused':True,'task_ownership_unchanged':True,'approval_not_transferred':True}
    if name=='invite-member':
        value=store.create_invite(*actor,target=p.get('target'),replacement=p.get('replacement',False),evidence=p.get('evidence',''),ttl=p.get('ttl',86400))
        with store.transaction() as db:project=db.execute('SELECT project FROM collaborations').fetchone()[0]
        path=Path(project)/'.codex-pair'/'handoffs'/('invite-'+value['invitation']+'.json')
        path.parent.mkdir(parents=True,exist_ok=True)
        with path.open('x',encoding='utf-8') as file:json.dump(value,file,ensure_ascii=False,indent=2)
        return {'invite_path':str(path),'join_argv':[sys.executable,str(Path(cli.__file__).resolve()),'accept','--invite',str(path),'--json'],'target':p.get('target'),'one_use':True}
    if name in ('confirm-member','member-presence','reverify-member'):
        bound=store.member(args.member)
        with AppServer(codex_home=bound['codex_home'],cwd=bound['cwd'],executable=getattr(args,'codex',None),metadata_only=True,account_metadata=True) as observer:
            proof=binding_proof(observer.read_thread(bound['session_uuid']),require_provider=True)
            if proof.get('modelProvider')=='openai':
                try:from .pair_balance import pool_key
                except ImportError:from pair_balance import pool_key
                account=observer.rpc('account/read',{'refreshToken':False})
                if account.get('account'):proof['accountFingerprint']=pool_key(account,bound['codex_home'])
        if name=='confirm-member':return store.confirm_member(*actor,proof=proof,context_digest=p['context_digest'],capabilities=p.get('capabilities',[]))
        if name=='member-presence':return store.member_presence(*actor,online=bool(p['online']),proof=proof)
        safe_evidence(p['evidence'])
        with store.transaction() as db:
            store._binding(db,*actor)
            if not store._reverification_idle(db,args.member):fail('Work in progress / 先安全结束工具及原回合')
            if proof['id']!=bound['session_uuid'] or proof['cwd']!=bound['cwd']:fail('Identity changed / 会话和cwd不能通过模型核验入口更换')
            old=json.loads(bound['proof']);proof['git']=old.get('git')
            db.execute("UPDATE members SET proof=?,lifecycle='active',online=1,verified=0 WHERE id=?",(json.dumps(proof),args.member))
            store._invalidate_approvals(db,'Member configuration reverified / 成员配置变更重新核验',cancel_questions=False)
            reviewer=store._reviewer_row(db)
            if args.member=='A' and reviewer and reviewer['state']!='retired':
                store._put(db,'reviewer_auto',False)
            store.event(db,'member_configuration_reverified',member=args.member,evidence=p['evidence'])
        return {'identity_reverified':True,'context_confirmation_required':True,'reviewer_revalidation_required':args.member=='A'}
    if name=='retire-member':return store.retire_member(*actor,p['target'],execution_resolved=p.get('execution_resolved'),evidence=p['evidence'])
    if name=='transfer-approval':return store.transfer_approval(*actor,p['target'],p['user_instruction'],p['verification'])
    if name=='revise-requirements':return store.revise_requirements(*actor,p['body'],p['affected'])
    if name=='configure-task':return store.configure_task(*actor,p.pop('task'),**p)
    if name=='submit-stage':
        artifact=p['artifact'];bound=store.member(args.member)
        workspace=json.loads(bound['proof']).get('git',{}).get('path') or bound['cwd']
        if artifact.get('workspace') and Path(artifact['workspace']).resolve()!=Path(workspace).resolve():fail('Wrong artifact workspace / 交付来自本人登记工作树')
        artifact['workspace']=workspace
        if artifact['kind']=='commit':
            if pair_workspace.git(workspace,'rev-parse','--verify',artifact['head']+'^{commit}')!=artifact['head']:fail('Candidate SHA mismatch / 提交身份不符')
            if pair_workspace.tree_stamp(workspace)['head']!=artifact['head'] or pair_workspace.tree_stamp(workspace)['changes']:fail('Uncommitted artifact / 固定候选需HEAD匹配且无未提交业务改动')
            commits=artifact.get('commits') or [artifact['head']]
            if not commits or commits[-1]!=artifact['head']:fail('Commit list mismatch / 交付提交末项必须等于固定候选')
            task=store.task(p['task'])
            result=cli.pair_git.check_delivery(workspace,task['base_commit'],commits,json.loads(task['allowed_paths']))
            if not result['passed']:fail('Review scope rejected / 固定送审版本超出任务范围或基准不符')
        else:
            for relative,expected in artifact.get('manifest',{}).items():
                path=(Path(workspace)/relative).resolve()
                if not path.is_relative_to(Path(workspace).resolve()) or path.is_symlink():fail('Snapshot path escaped / 快照路径越界')
                actual=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
                if actual!=expected:fail('Snapshot changed / 快照摘要不符')
            artifact=pair_workspace.freeze_snapshot(store,workspace,artifact,json.loads(store.task(p['task'])['allowed_paths']))
        return store.submit_stage(*actor,p['task'],artifact,p['contributors'],p['checks'],p['payload'],priority=p.get('priority',0))
    if name=='submit-integration':
        artifact=p['artifact'];workspace=artifact['workspace']
        if pair_workspace.tree_stamp(workspace)!={'head':artifact['head'],'changes':'','diff':''}:fail('Integrated tree changed / 整合候选必须固定且干净')
        return store.integration_candidate(*actor,p['sources'],artifact,p['checks'],p['payload'])
    if name=='reviewer-policy':
        safe_evidence(p['reason'])
        with store.transaction() as db:store._a_actor(db,*actor)
        if p['enabled']:
            checkpoint=store.setting('reviewer_checkpoint')
            if checkpoint and checkpoint.get('status')=='saved':fail('Reviewer checkpoint pending / 先完整读取、核实并持久化恢复计划')
            a=store.member('A')
            with AppServer(codex_home=a['codex_home'],cwd=a['cwd'],executable=getattr(args,'codex',None),metadata_only=True,account_metadata=True) as observer:
                official_identity(observer.validate(a))
                if observer.rpc('account/read',{'refreshToken':False}).get('account',{}).get('type')!='chatgpt':fail('Official account required / 原 A 官方账号未核实')
        store.setting('reviewer_auto',bool(p['enabled']))
        return {'enabled':bool(p['enabled']),'same_verified_configuration':True,'permissions_unchanged':True}
    if name=='reviewer-reconcile':
        try:from .pair_review_runtime import reconcile_review
        except ImportError:from pair_review_runtime import reconcile_review
        return reconcile_review(store,*actor,p['stage'],p['evidence'])
    if name=='reviewer-retire':return store.retire_reviewer(*actor,epoch=p['epoch'],execution_resolved=p.get('execution_resolved'),evidence=p['evidence'])
    if name=='reviewer-replace':
        safe_evidence(p['evidence'])
        with store.transaction() as db:
            store._a_actor(db,*actor)
            current=store._reviewer_row(db)
            if not current or current['state']!='retired':fail('Retirement required / 先核实并撤销旧审核者')
            store._put(db,'reviewer_replacement_evidence',p['evidence'])
            store._put(db,'reviewer_auto',True)
        return {'replacement_authorized':True,'old_epoch_invalid':True,'created':False}
    if name=='resume-stage':
        safe_evidence(p['evidence'])
        with store.transaction() as db:
            store._a_actor(db,*actor)
            stage=db.execute('SELECT * FROM review_stages WHERE id=?',(p['stage'],)).fetchone()
            current=store._reviewer_row(db)
            if not stage or stage['status']!='needs_validation' or current and current['state'] not in ('idle','retired'):fail('Review unresolved / 原执行必须先核实结束')
            if not p.get('safe_to_repeat_checks'):fail('Check side effects unresolved / 核实必要测试可安全重新运行，不重放未知外部动作')
            db.execute("UPDATE review_stages SET status='pending' WHERE id=?",(stage['id'],))
            store.event(db,'stage_explicitly_resumed',stage=stage['id'],evidence=p['evidence'])
        return {'resumed':True,'requires_new_review':True}
    if name=='review-issue':return store.resolve_issue(*actor,p['issue'],p['evidence'],user_decision=p.get('user_decision',False))
    if name=='task-boundary':return store.release_task_boundary(*actor,p['task'],p['evidence'])
    if name=='propose-reserve':return store.propose_reserve(*actor,p['value'],p['reason'],sample=p.get('sample'))
    if name=='record-quota-error':return store.quota_error(args.member,p['reason'],seconds=p.get('seconds',60))
    if name=='create-worktree':return pair_workspace.create_tree(store,*actor,p['target'],p['base'])
    if name=='update-baseline':return pair_workspace.update_baseline(store,*actor,p['target'],p['stage'])
    if name=='cleanup-worktree':return pair_workspace.cleanup_tree(store,*actor,p['path'],processes_stopped=p.get('processes_stopped'),evidence=p['evidence'])
    if name=='publish-policy':
        safe_evidence(p['user_instruction'])
        with store.transaction() as db:
            store._a_actor(db,*actor)
            store._put(db,'publish_enabled',bool(p['enabled']))
            if p['enabled']:
                if not all(isinstance(p.get(k),str) and p[k] for k in ('remote','branch','url')):fail('Publish target required / 先确认用户仓库远端、分支、URL')
                store._put(db,'publish_target',{k:p[k] for k in ('remote','branch','url')})
            store.event(db,'publish_policy',enabled=bool(p['enabled']),basis=p['user_instruction'])
        return {'enabled':bool(p['enabled'])}
    if name=='publish-stage':return pair_workspace.publish_stage(store,*actor,p['stage'],p['remote'],p['branch'],reconcile=p.get('reconcile',False))
    fail('Unknown team command / 多人命令未实现')
