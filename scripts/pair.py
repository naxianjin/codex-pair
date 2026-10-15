"""codex-pair 统一命令入口，依赖仅为 Python 标准库与已有 Codex/Git。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

try:
    from .pair_core import Daemon, FileLock, PairError, Store, dumps, effective_home, member_workspace
    from .pair_transport import AppServer, QueueTransport, TransportError, TransportPool, canonical_path, binding_proof
    from . import pair_git
except ImportError:
    from pair_core import Daemon, FileLock, PairError, Store, dumps, effective_home, member_workspace
    from pair_transport import AppServer, QueueTransport, TransportError, TransportPool, canonical_path, binding_proof
    import pair_git


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PairError(message)


def build_parser():
    common=argparse.ArgumentParser(add_help=False)
    common.add_argument('--json',action='store_true',default=argparse.SUPPRESS,help='输出 JSON')
    common.add_argument('--pair',default=argparse.SUPPRESS,help='协作 ID')
    common.add_argument('--codex-home',default=argparse.SUPPRESS,help='显式 Codex 主目录；默认遵循 CODEX_HOME')
    common.add_argument('--state-root',default=argparse.SUPPRESS,help='共享状态根目录，协作目录为该路径/<ID>；与账号目录独立')
    common.add_argument('--member-codex-home',default=argparse.SUPPRESS,help='成员实际账号目录，默认当前 CODEX_HOME')
    common.add_argument('--codex',default=argparse.SUPPRESS,help='Codex 可执行文件路径')
    parser=Parser(description='两个本机 Codex CLI 的持久化协作',parents=[common])
    commands=parser.add_subparsers(dest='command',required=True)
    def command(name,help_text):
        return commands.add_parser(name,help=help_text,parents=[common])
    def body(cmd,required=False):
        group=cmd.add_mutually_exclusive_group(required=required)
        group.add_argument('--body',help='消息或结果正文')
        group.add_argument('--body-file',help='UTF-8 正文文件，保留原有换行')
    def actor(cmd):
        cmd.add_argument('--member',choices=['A','B'],required=True)
        cmd.add_argument('--session',default=os.environ.get('CODEX_THREAD_ID'),help='成员绑定的完整会话 UUID')
        cmd.add_argument('--worktree',default=str(Path.cwd()),help='必须与当前实际工作目录一致')
    cmd=command('start','由当前已打开窗口发起，生成给另一窗口的邀请并启动本地桥')
    cmd.add_argument('--project',help='目标仓库；默认当前目录所在仓库')
    cmd.add_argument('--delivery-mode',choices=['auto','manual'],default='auto')
    group=cmd.add_mutually_exclusive_group(required=True)
    group.add_argument('--goal','--body',dest='body',help='协作目标')
    group.add_argument('--goal-file','--body-file',dest='body_file',help='UTF-8 目标文件')
    cmd=command('accept','当前已打开的第二个窗口接受邀请，自行识别会话与账号')
    cmd.add_argument('--invite',required=True,help='A 输出的本地邀请 JSON 路径')
    cmd=command('bridge','后台启动或停止当前协作的消息桥')
    cmd.add_argument('--action',choices=['start','stop'],default='start')
    cmd=command('init','初始化项目 .codex-pair 下的共享协作状态')
    cmd.add_argument('--project',required=True)
    cmd.add_argument('--base-ref',default='HEAD')
    cmd.add_argument('--worktree-root',help='默认项目 .codex-pair/worktrees，创建或验证 <ID>-a、<ID>-b')
    cmd=command('join','绑定已有 CLI 会话，或创建独占托管会话')
    cmd.add_argument('--member',choices=['A','B'],required=True)
    cmd.add_argument('--worktree',required=True)
    group=cmd.add_mutually_exclusive_group(required=True)
    group.add_argument('--session',help='已有 CLI 的完整会话 UUID')
    group.add_argument('--current-session',action='store_true',help='从当前 Codex 会话 CODEX_THREAD_ID 加入，并校验实际 cwd')
    group.add_argument('--new-session',action='store_true',help='为此协作创建独占 app-server 会话')
    cmd.add_argument('--backend',choices=['queue','shared','managed'],help='已有会话默认 queue，新会话为 managed')
    cmd.add_argument('--delivery-mode',choices=['auto','manual'],default='auto',help='manual 明确为手动交接，不声称自动唤醒')
    cmd.add_argument('--branch',help='已有 worktree 的预期分支；有 init 计划时必须与计划一致')
    cmd=command('assign','分配任务；依赖和范围检查通过后才激活')
    cmd.add_argument('--task',required=True)
    cmd.add_argument('--title',required=True)
    cmd.add_argument('--to',choices=['A','B'],required=True)
    cmd.add_argument('--allow',action='append',required=True,help='仓库相对修改范围，可重复')
    cmd.add_argument('--depends-on',action='append',default=[])
    cmd.add_argument('--base-ref',help='任务提交基准；默认使用协作基准')
    body(cmd)
    cmd=command('send','先持久化，再由 daemon 投递')
    cmd.add_argument('--from',dest='sender',choices=['A','B'],required=True)
    cmd.add_argument('--to',choices=['A','B'],required=True)
    cmd.add_argument('--task')
    cmd.add_argument('--kind',choices=['note','question','reply'],default='note')
    cmd.add_argument('--reply-to')
    cmd.add_argument('--progress-note',help='回复带来实际进展时提供具体证据；空值计为未进展一轮')
    cmd.add_argument('--dedupe-key',help='相同请求重试必须复用去重键')
    body(cmd,True)
    cmd=command('claim','原子领取；claimed=false 时不得执行')
    actor(cmd)
    cmd.add_argument('--message',help='指定消息 ID；省略时领取最早可用消息')
    cmd.add_argument('--compact',action='store_true',help='仅返回领取凭据，避免重复正文使工具输出截断')
    cmd=command('recover-claim','原领取会话显式取回遗失的回执，不重新领取或重派')
    actor(cmd)
    cmd.add_argument('--message',required=True,help='原来已领取的消息 ID')
    cmd=command('read-message','领取后分页读取长消息或任务，避免工具输出截断')
    actor(cmd)
    cmd.add_argument('--message',required=True)
    cmd.add_argument('--field',choices=['body','task'],default='body')
    cmd.add_argument('--offset',type=int,default=0)
    cmd.add_argument('--limit',type=int,default=2000)
    cmd=command('recover-delivery','显式恢复确定在发送前被长度检查拒绝的消息，保留原 ID 与历史')
    cmd.add_argument('--message',required=True)
    cmd=command('complete','使用领取令牌提交实质结果')
    actor(cmd)
    cmd.add_argument('--message',required=True)
    cmd.add_argument('--token',required=True)
    cmd.add_argument('--commit',action='append',default=[],help='交付的完整提交 SHA，可重复')
    body(cmd,True)
    cmd=command('integrate','检查交付范围并按 SHA 整合；自动暂停新投递并保留冲突')
    cmd.add_argument('--task')
    cmd.add_argument('--worktree')
    cmd.add_argument('--commit',action='append',default=[])
    group=cmd.add_mutually_exclusive_group()
    group.add_argument('--continue',dest='continue_integration',action='store_true')
    group.add_argument('--abort',action='store_true')
    cmd=command('status','显示任务、消息各阶段及守护进程证据')
    cmd.add_argument('--probe',type=float,default=0,help='等待指定秒数，验证 daemon 响应随机挑战')
    for name,label in [('daemon','按成员账号路由的中央 dispatcher'),('relay','仅向本账号成员投递的 relay')]:
        cmd=command(name,label)
        cmd.add_argument('--max-attempts',type=int,default=3)
        cmd.add_argument('--poll-interval',type=float,default=.25)
        cmd.add_argument('--rpc-timeout',type=float,default=15,help='投递回执超时，不是任务耗时限制')
        if name=='relay':cmd.add_argument('--member',choices=['A','B'],required=True)
    cmd=command('relay-stop','停止指定成员 relay，保留其他 relay 与状态')
    cmd.add_argument('--member',choices=['A','B'],required=True)
    for name,label in [('pause','暂停新投递，保留已开始的回合'),('resume','恢复调度与投递'),('stop','停止守护程序，保留状态及代码')]:
        command(name,label)
    return parser


def read_body(args):
    if args.body_file:
        with open(args.body_file,'r',encoding='utf-8-sig',newline='') as file:
            return file.read()
    return args.body or ''


def validate_actor(store,args):
    if not args.session:
        raise PairError('Session UUID required / 必须提供会话 UUID 或 CODEX_THREAD_ID')
    if canonical_path(args.worktree)!=canonical_path(Path.cwd()):
        raise PairError('Actual cwd mismatch / 当前实际工作目录与 --worktree 不符')
    member=store.member(args.member)
    actor_home=canonical_path(getattr(args,'member_codex_home',None) or getattr(args,'codex_home',None) or effective_home())
    if actor_home!=canonical_path(member['codex_home']):
        raise PairError('Member account mismatch / 当前账号目录与成员绑定不符，禁止回退另一账号')
    if member['session_uuid']!=args.session.lower() or member['cwd']!=canonical_path(args.worktree):
        raise PairError('Member binding mismatch / 成员的会话或工作目录不符')
    with AppServer(codex_home=member['codex_home'], executable=getattr(args,'codex',None),cwd=member['cwd'],metadata_only=True) as observer:
        observer.validate(member)


def git_run(cwd,*args,input=None):
    result=subprocess.run(['git','-C',str(cwd),*args],input=input,capture_output=True)
    if result.returncode:
        raise PairError(result.stderr.decode('utf-8',errors='replace')+' / Git 检查失败')
    return result.stdout.decode('utf-8').strip()


def git_context(store):
    context=store.git_context()
    if context:return context
    project=store.status()['pair']['project']
    return store.configure_git(pair_git.inspect_repo(project))


def worktree_proof(store,member_id,cwd,branch=None):
    context=git_context(store)
    planned=context['worktrees'].get(member_id.lower())
    if planned and canonical_path(planned['path'])!=canonical_path(cwd):
        raise PairError('Wrong planned worktree / 与初始化计划的工作目录不符')
    if planned and branch and branch!=planned['branch']:
        raise PairError('Wrong planned branch / 与初始化计划的分支不符')
    current=pair_git.inspect_repo(cwd)
    expected=planned['branch'] if planned else branch or current['branch']
    if not expected:raise PairError('Detached worktree rejected / 不接受 detached HEAD')
    proof=pair_git.validate_worktree(cwd,context['repo']['common_dir'],expected)
    if not proof['valid']:raise PairError(dumps(proof))
    base=store.status()['pair']['base_commit']
    git_run(cwd,'merge-base','--is-ancestor',base,'HEAD')
    return proof


def receiver_proof(store,path):
    context=git_context(store)
    with store.transaction() as db:
        members=[dict(r) for r in db.execute('SELECT * FROM members')]
    requested=canonical_path(path)
    receivers=[m for m in members if requested==member_workspace(m)]
    for member in receivers:
        if requested==member_workspace(member):
            proof=json.loads(member['proof']).get('git')
            if not proof:proof=worktree_proof(store,member['id'],path)
            validation=pair_git.validate_worktree(path,context['repo']['common_dir'],proof['branch'])
            if not validation['valid']:raise PairError(dumps(validation))
            with store.transaction() as db:
                ids=[m['id'] for m in receivers]
                slots=','.join('?' for _ in ids)
                active=db.execute("SELECT id FROM tasks WHERE assignee IN ("+slots+") AND status IN ('active','blocked')",ids).fetchall()
                live=db.execute("SELECT * FROM messages WHERE recipient IN ("+slots+") AND (delivery_status IN ('delivering','unknown') OR (delivery_status='accepted' AND (remote_status IS NULL OR remote_status IN ('queued','inProgress'))))",ids).fetchall()
            # A 自己串行执行的工具调用可以整合。它不能豁免其他回合、
            # 正在开发的任务、队列或结果不明的请求。
            coordinator=next((m for m in receivers if m['id']=='A'),None)
            current_coordinator=(coordinator and os.environ.get('CODEX_THREAD_ID')==coordinator['session_uuid']
                                 and canonical_path(Path.cwd())==coordinator['cwd'])
            if current_coordinator and len(live)==1:
                own=live[0]
                if (own['recipient']=='A' and own['delivery_status']=='accepted' and own['remote_status']=='inProgress'
                        and own['process_status'] in ('claimed','completed')
                        and (own['kind']!='task' or own['process_status']=='completed')):
                    live=[]
            if active or live:
                raise PairError('Receiver still has active or unresolved work / 接收方仍有执行中或结果未确定的工作')
            return validation
    if requested!=canonical_path(context['repo']['root']):
        raise PairError('Unknown integration receiver / 整合目标必须是已绑定 worktree 或原项目目录')
    validation=pair_git.validate_worktree(path,context['repo']['common_dir'],context['repo']['branch'])
    if not validation['valid']:raise PairError(dumps(validation))
    return validation


def save_plan(store,plan,**changes):
    plan.update(changes);plan['updated']=time.time()
    keys=list(changes)+['updated']
    with store.transaction() as db:
        db.execute('UPDATE integrations SET '+','.join(key+'=?' for key in keys)+' WHERE id=?',
                   [plan[key] for key in keys]+[plan['id']])
        store.event(db,'integration_state',integration=plan['id'],status=plan['status'],next_index=plan['next_index'])


def same_patch(worktree,first,second):
    def patch_id(commit):
        patch=subprocess.run(['git','-C',worktree,'show','--format=','--binary',commit],capture_output=True,check=True).stdout
        result=subprocess.run(['git','patch-id','--stable'],input=patch,capture_output=True,check=True).stdout
        return result.split()[0] if result.split() else None
    return patch_id(first) is not None and patch_id(first)==patch_id(second)


def apply_plan(store,plan,*,continuing=False):
    worktree=plan['receiver'];commits=json.loads(plan['commits_json'])
    if continuing and plan['status'] in ('conflict','applying','failed'):
        cherry=subprocess.run(['git','-C',worktree,'rev-parse','--verify','--quiet','CHERRY_PICK_HEAD'],capture_output=True)
        if cherry.returncode==0:
            if cherry.stdout.decode().strip()!=commits[plan['next_index']]:
                raise PairError('Different cherry-pick in progress / 当前 cherry-pick 不属于本交付计划')
            result=pair_git.continue_integration(worktree)
            if not result['success']:
                save_plan(store,plan,status='conflict',result_json=dumps(result));return dict(result,integration_id=plan['id'])
            save_plan(store,plan,next_index=plan['next_index']+1,status='running',current_commit=None)
        else:
            head=pair_git.inspect_repo(worktree)['head']
            if head!=plan['before_head']:
                parents=git_run(worktree,'rev-list','--parents','-n','1',head).split()[1:]
                if parents!=[plan['before_head']] or not same_patch(worktree,head,commits[plan['next_index']]):
                    raise PairError('Integration outcome ambiguous / 整合结果不明，HEAD 与计划证据不符，禁止重复应用')
                save_plan(store,plan,next_index=plan['next_index']+1,status='running',current_commit=None)
            elif plan['status']=='conflict':
                raise PairError('Conflict state was changed externally / 冲突状态已被外部改变，请核对记录后显式中止计划')
    while plan['next_index']<len(commits):
        commit=commits[plan['next_index']]
        before=pair_git.inspect_repo(worktree)['head']
        already=subprocess.run(['git','-C',worktree,'merge-base','--is-ancestor',commit,'HEAD'],capture_output=True)
        if already.returncode==0:
            save_plan(store,plan,next_index=plan['next_index']+1,status='running');continue
        if already.returncode!=1:raise PairError('Cannot inspect commit ancestry / 无法核实提交祖先关系')
        save_plan(store,plan,status='applying',before_head=before,current_commit=commit)
        try:result=pair_git.integrate_commits(worktree,[commit])
        except pair_git.GitError as exc:
            save_plan(store,plan,status='failed',result_json=dumps(exc.to_dict()));raise
        if not result['success']:
            result.update(integration_id=plan['id'],remaining_commits=commits[plan['next_index']+1:])
            save_plan(store,plan,status='conflict',result_json=dumps(result));return result
        save_plan(store,plan,next_index=plan['next_index']+1,status='running',current_commit=None)
    result={'success':True,'status':'completed','integration_id':plan['id'],'integrated_commits':commits,
            'head':pair_git.inspect_repo(worktree)['head'],'receiver':worktree,'collaboration_status':'paused'}
    save_plan(store,plan,status='completed',result_json=dumps(result))
    with store.transaction() as db:
        db.execute("UPDATE tasks SET status='integrated',updated=? WHERE id=?",(time.time(),plan['task_id']))
        store.event(db,'task_integrated',task=plan['task_id'],integration=plan['id'],head=result['head'])
    return result


def integrate(store,args):
    with FileLock(store.directory/'integration.lock',timeout=30),FileLock(store.control_lock,timeout=30):
        with store.transaction() as db:
            db.execute("UPDATE collaborations SET status='paused',revision=revision+1")
            store.event(db,'control',state='paused',reason='integration')
        if args.continue_integration or args.abort:
            with store.transaction() as db:
                plans=[dict(r) for r in db.execute("SELECT * FROM integrations WHERE status NOT IN ('completed','aborted') ORDER BY created DESC")]
            if args.task:plans=[p for p in plans if p['task_id']==args.task]
            if args.worktree:plans=[p for p in plans if canonical_path(p['receiver'])==canonical_path(args.worktree)]
            if len(plans)!=1:raise PairError('Specify exactly one unfinished integration / 必须明确选择一个未完成整合计划')
            plan=plans[0];receiver_proof(store,plan['receiver'])
            if args.abort:
                cherry=subprocess.run(['git','-C',plan['receiver'],'rev-parse','--verify','--quiet','CHERRY_PICK_HEAD'],capture_output=True)
                if cherry.returncode==0:
                    if cherry.stdout.decode().strip()!=json.loads(plan['commits_json'])[plan['next_index']]:
                        raise PairError('Different cherry-pick in progress / 当前 cherry-pick 不属于本计划')
                    result=pair_git.abort_integration(plan['receiver'])
                else:
                    result={'success':True,'status':'aborted','head':pair_git.inspect_repo(plan['receiver'])['head']}
                result.update(integration_id=plan['id'],previously_integrated_commits=json.loads(plan['commits_json'])[:plan['next_index']])
                save_plan(store,plan,status='aborted',result_json=dumps(result));return result
            return apply_plan(store,plan,continuing=True)
        if not args.task:raise PairError('--task is required / 必须选择交付任务')
        task=store.task(args.task)
        if task['status'] not in ('delivered','integrated'):
            raise PairError('Task has no accepted commit delivery / 任务尚未交付可验收的提交')
        commits=json.loads(task['commits_json'] or '[]')
        if args.commit and args.commit!=commits:
            raise PairError('Commit list differs from delivery / 指定提交与任务交付列表不同')
        if not commits:raise PairError('No commits supplied / 任务没有提交')
        source=store.member(task['assignee']);worktree_proof(store,source['id'],member_workspace(source))
        proof=pair_git.check_delivery(member_workspace(source),task['base_commit'] or store.status()['pair']['base_commit'],commits,json.loads(task['allowed_paths']))
        if not proof['passed']:raise PairError('Delivery rejected / 交付校验失败: '+dumps(proof))
        target=canonical_path(args.worktree or member_workspace(store.member('A')))
        receiver_proof(store,target)
        with store.transaction() as db:
            existing=db.execute("SELECT * FROM integrations WHERE task_id=? AND receiver=? ORDER BY created DESC LIMIT 1",(task['id'],target)).fetchone()
            if existing and existing['status']=='completed':return json.loads(existing['result_json'])
            if existing and existing['status']!='aborted':raise PairError('Unfinished integration exists / 已有未完成整合，请使用 --continue 或 --abort')
            now=time.time();plan={'id':str(uuid.uuid4()),'task_id':task['id'],'receiver':target,'commits_json':dumps(commits),
                'next_index':0,'status':'running','before_head':pair_git.inspect_repo(target)['head'],'current_commit':None,'result_json':None,'created':now,'updated':now}
            db.execute('INSERT INTO integrations('+','.join(plan)+') VALUES('+','.join('?' for _ in plan)+')',list(plan.values()))
            store.event(db,'integration_planned',integration=plan['id'],task=task['id'],commits=commits,receiver=target)
        return apply_plan(store,plan)


def resolve_state_root(args, home, pair_id):
    explicit=getattr(args,'state_root',None)
    if explicit:return Path(explicit).resolve()
    # 从嵌套 worktree 运行时，取公共仓库登记的原工作目录。
    result=subprocess.run(['git','worktree','list','--porcelain'],capture_output=True,encoding='utf-8')
    if result.returncode==0:
        first=next((line[9:] for line in result.stdout.splitlines() if line.startswith('worktree ')),None)
        if first:
            candidate=Path(first)/'.codex-pair'/'state'
            if (candidate/pair_id/'state.sqlite3').is_file():return candidate
    # 老数据库不移动，仅在当前账号原位置存在时读取。
    legacy=home/'codex-pair'
    if (legacy/pair_id/'state.sqlite3').is_file():return legacy
    raise PairError('State not found / 未发现协作，请显式提供 --state-root；不扫描其他账号')


def execute(args):
    if args.command in ('start','accept','bridge'):
        try:from . import pair_windows
        except ImportError:import pair_windows
        return pair_windows.execute(args,sys.modules[__name__])
    home=Path(getattr(args,'codex_home',None) or effective_home()).resolve()
    pair_id=getattr(args,'pair',None)
    if args.command=='init':
        project=canonical_path(args.project)
        repo=pair_git.inspect_repo(project)
        if canonical_path(repo['root'])!=project:raise PairError('Use repository root / 必须使用仓库根目录')
        base=git_run(project,'rev-parse','--verify','--end-of-options',args.base_ref+'^{commit}')
        state_root=getattr(args,'state_root',None) or str(Path(project)/'.codex-pair'/'state')
        worktree_root=args.worktree_root or str(Path(project)/'.codex-pair'/'worktrees')
        # 两种目录都先验证，避免非法 worktree 参数导致先创建数据库。
        pair_git.prepare_pair_directory(project,worktree_root,'worktrees')
        for kind in ('handoffs','logs','archives'):
            pair_git.prepare_pair_directory(project,str(Path(project)/'.codex-pair'/kind),kind)
        store=Store.initialize(pair_id or str(uuid.uuid4()),project,base,home=home,state_root=state_root)
        store.configure_git(repo)
        if worktree_root:
            root=Path(worktree_root).resolve()
            expected={r:{'path':str(root/(store.pair_id+'-'+r)),'branch':'codex-pair/'+store.pair_id+'/'+r} for r in ('a','b')}
            if all(Path(item['path']).exists() for item in expected.values()):
                for item in expected.values():
                    validation=pair_git.validate_worktree(item['path'],repo['common_dir'],item['branch'])
                    if not validation['valid']:raise PairError(dumps(validation))
                    git_run(item['path'],'merge-base','--is-ancestor',base,'HEAD')
                trees=expected
            else:
                created=pair_git.create_worktrees(project,store.pair_id,base,str(root))
                trees={r:created[r] for r in ('a','b')}
            store.configure_git(repo,trees)
        return dict(store.status(),git=store.git_context())
    if not pair_id:
        raise PairError('--pair is required / 必须指定协作 ID')
    store=Store(pair_id,home=home,state_root=resolve_state_root(args,home,pair_id))
    executable=getattr(args,'codex',None)
    if args.command=='join':
        home=Path(canonical_path(getattr(args,'member_codex_home',None) or home))
        cwd=canonical_path(args.worktree)
        if args.current_session:
            args.session=os.environ.get('CODEX_THREAD_ID')
            if not args.session or canonical_path(Path.cwd())!=cwd:
                raise PairError('Current session context required / --current-session 必须在目标 Codex 会话及其实际 worktree 内运行')
        git_proof=worktree_proof(store,args.member,cwd,args.branch)
        backend=args.backend or ('managed' if args.new_session else 'queue')
        if args.new_session:
            if backend!='managed':
                raise PairError('--new-session requires managed backend / 新会话必须使用 managed 后端')
            try:existing=store.member(args.member)
            except PairError:existing=None
            if existing:
                if existing['cwd']!=cwd or existing['backend']!='managed' or canonical_path(existing['codex_home'])!=canonical_path(home) or existing['delivery_mode']!=args.delivery_mode:
                    raise PairError('Member already bound differently / 成员已有不同绑定')
                with AppServer(codex_home=home,executable=executable,cwd=cwd,metadata_only=True) as observer:observer.validate(existing)
                return existing
            with AppServer(codex_home=home,executable=executable,cwd=cwd) as client:
                thread=client.create_session(cwd)
                member={'session_uuid':thread['id'],'cwd':cwd}
                receipt=client.start(member,{'id':str(uuid.uuid4()),'envelope':'This session is reserved for codex-pair. Reply only PAIR_READY. Do not use tools or change files.'})
                completion=client.wait_turn(thread['id'],receipt['turn_id'])
                if completion['status']!='completed':
                    raise PairError('Session bootstrap failed / 会话初始化回合未完成')
                proof=binding_proof(client.validate(member),require_provider=True)
                proof['git']=git_proof
                return store.join(args.member,thread['id'],cwd,backend=backend,owned=True,proof=proof,member_codex_home=home,delivery_mode=args.delivery_mode)
        if backend=='managed':
            raise PairError('Use --new-session for managed sessions / 独占会话须由 --new-session 创建')
        with AppServer(backend='shared' if backend=='shared' else 'managed',codex_home=home,executable=executable,cwd=cwd,metadata_only=True) as client:
            proof=binding_proof(client.validate({'session_uuid':args.session,'cwd':cwd}),require_provider=True)
            proof['git']=git_proof
            return store.join(args.member,args.session,cwd,backend=backend,owned=False,proof=proof,member_codex_home=home,delivery_mode=args.delivery_mode)
    if args.command=='assign':
        member=store.member(args.to);worktree_proof(store,args.to,member_workspace(member))
        base=git_run(member_workspace(member),'rev-parse','--verify','--end-of-options',args.base_ref+'^{commit}') if args.base_ref else None
        return store.assign(args.task,args.title,args.to,args.allow,body=read_body(args),dependencies=args.depends_on,base_commit=base)
    if args.command=='send':
        return store.send(args.sender,args.to,read_body(args),kind=args.kind,task_id=args.task,
                          dedupe_key=args.dedupe_key,reply_to=args.reply_to,progress_note=args.progress_note)
    if args.command=='recover-delivery':
        return store.recover_delivery(args.message)
    if args.command in ('claim','recover-claim','read-message','complete'):
        if args.command=='recover-claim':
            current_session=os.environ.get('CODEX_THREAD_ID')
            if not current_session or current_session.lower()!=str(args.session).lower():
                raise PairError('Original CLI session required / 恢复回执须在原领取会话内执行')
            if canonical_path(effective_home())!=canonical_path(store.member(args.member)['codex_home']):
                raise PairError('Original CLI home required / 恢复回执须使用原领取会话的配置目录')
        validate_actor(store,args)
        if args.command=='read-message':
            return store.read_message(args.member,args.session,args.worktree,args.message,field=args.field,offset=args.offset,limit=args.limit)
        if args.command=='recover-claim':
            return store.recover_claim(args.member,args.session,args.worktree,args.message)
        if args.command=='claim':
            result=store.claim(args.member,args.session,args.worktree,message_id=args.message)
            if getattr(args,'compact',False) and result.get('claimed'):
                return {key:result[key] for key in ('claimed','id','sender','recipient','kind','task_id','claim_token','process_status')}
            return result
        if args.commit:
            message=store.message(args.message)
            if message['kind']!='task':raise PairError('Only task completion accepts commits / 只有任务完成可交付提交')
            task=store.task(message['task_id'])
            check=pair_git.check_delivery(member_workspace(store.member(args.member)),task['base_commit'] or store.status()['pair']['base_commit'],args.commit,json.loads(task['allowed_paths']))
            if not check['passed']:raise PairError('Delivery rejected / 交付校验失败: '+dumps(check))
        return store.complete(args.member,args.session,args.worktree,args.message,args.token,read_body(args),commits=args.commit)
    if args.command=='integrate':
        return integrate(store,args)
    if args.command=='status':
        if args.probe<0 or args.probe>30:
            raise PairError('--probe must be between 0 and 30 / 存活探测须在 0 至 30 秒之间')
        return store.status(probe_timeout=args.probe)
    if args.command in ('pause','resume','stop'):
        return store.control({'pause':'paused','resume':'running','stop':'stopped'}[args.command])
    if args.command=='relay-stop':
        with store.transaction() as db:
            db.execute('UPDATE bridge_workers SET stop_requested=1 WHERE member=?',(args.member,))
            store.event(db,'relay_stop_requested',member=args.member)
        return {'member':args.member,'stop_requested':True,'records_preserved':True}
    if args.command in ('daemon','relay'):
        if args.rpc_timeout<=0 or args.rpc_timeout>60:
            raise PairError('RPC timeout must be within 0..60 / 回执超时须大于 0 且最多 60 秒')
        member_id=args.member if args.command=='relay' else None
        if member_id:
            member=store.member(member_id)
            relay_home=canonical_path(getattr(args,'member_codex_home',None) or home)
            if relay_home!=canonical_path(member['codex_home']):
                raise PairError('Relay account mismatch / relay 必须使用本成员账号目录')
        pool=TransportPool(executable=executable,timeout=args.rpc_timeout)
        Daemon(store,pool,max_attempts=args.max_attempts,poll_interval=args.poll_interval,member_id=member_id).run()
        return {'daemon':'exited','state_directory':str(store.directory)}
    raise PairError('Unknown command / 未知命令')


def main(argv=None):
    argv=sys.argv[1:] if argv is None else argv
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8')
    try:
        args=build_parser().parse_args(argv)
        result=execute(args)
        if getattr(args,'json',False):
            print(json.dumps({'ok':True,'result':result},ensure_ascii=False))
        else:
            print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except KeyboardInterrupt:
        return 130
    except (PairError,TransportError,pair_git.GitError,OSError,sqlite3.Error,subprocess.SubprocessError,ValueError,KeyError) as exc:
        if '--json' in argv:
            print(json.dumps({'ok':False,'error':str(exc),'type':type(exc).__name__,
                              'details':exc.to_dict() if isinstance(exc,pair_git.GitError) else None},ensure_ascii=False))
        else:
            print(str(exc),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
