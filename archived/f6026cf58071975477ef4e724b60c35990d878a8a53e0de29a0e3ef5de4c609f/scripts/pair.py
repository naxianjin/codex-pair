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
    parser=Parser(description='最多四个可见Codex CLI及一个独立隐藏审核者',parents=[common])
    commands=parser.add_subparsers(dest='command',required=True)
    def command(name,help_text):
        return commands.add_parser(name,help=help_text,parents=[common])
    def body(cmd,required=False):
        group=cmd.add_mutually_exclusive_group(required=required)
        group.add_argument('--body',help='消息或结果正文')
        group.add_argument('--body-file',help='UTF-8 正文文件，保留原有换行')
    def actor(cmd):
        cmd.add_argument('--member',choices=['A','B','C','D'],required=True)
        cmd.add_argument('--session',default=os.environ.get('CODEX_THREAD_ID'),help='成员绑定的完整会话 UUID')
        cmd.add_argument('--worktree',default=str(Path.cwd()),help='必须与当前实际工作目录一致')
    cmd=command('start','由当前已打开窗口发起，生成给另一窗口的邀请并启动本地桥')
    cmd.add_argument('--project',help='目标仓库；默认当前目录所在仓库')
    cmd.add_argument('--delivery-mode',choices=['auto','manual'],default='auto')
    cmd.add_argument('--resume-pair',help='明确恢复旧协作并保留原数据库角色')
    cmd.add_argument('--baseline-plan-file',help='新轮次存在未提交改动时，提供用户确认的基线归属 JSON')
    cmd.add_argument('--execution-assistance',choices=['task-scoped'],help='显式开启 B 请求 A 在当前任务授权内承接执行')
    cmd.add_argument('--quota-checkpoints',action='store_true',help='开启低额度项目内交接与自动恢复')
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
    cmd.add_argument('--member',choices=['A','B','C','D'],required=True)
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
    cmd.add_argument('--to',choices=['A','B','C','D','auto'],required=True)
    cmd.add_argument('--report-to',choices=['A','B','C','D'],help='默认当前统筹方，初始为 A')
    cmd.add_argument('--effort',type=int,choices=[1,2,3,5,8],default=3)
    cmd.add_argument('--task-kind',choices=['implementation','repair','integration','environment','verification','coordination'],default='implementation')
    cmd.add_argument('--capability',action='append',default=[])
    cmd.add_argument('--resource',action='append',default=[])
    cmd.add_argument('--priority',type=int,default=0)
    cmd.add_argument('--acceptance',action='append',default=[])
    cmd.add_argument('--required-check',action='append',default=[],help='A定义的NAME=准确检查命令，可重复')
    cmd.add_argument('--pin-member',action='store_true',help='固定原成员及绑定代次，交接不可转移')
    cmd.add_argument('--independent-of',action='append',default=[],help='不得由被引用任务的执行者核验，可重复')
    cmd.add_argument('--allow',action='append',required=True,help='仓库相对修改范围，可重复')
    cmd.add_argument('--depends-on',action='append',default=[])
    cmd.add_argument('--base-ref',help='任务提交基准；默认使用协作基准')
    body(cmd)
    cmd=command('send','先持久化，再由 daemon 投递')
    cmd.add_argument('--from',dest='sender',choices=['A','B','C','D'],required=True)
    cmd.add_argument('--to',choices=['A','B','C','D'],required=True)
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
    cmd.add_argument('--partial',action='store_true',help='中途交回，任务未完成且不满足依赖')
    cmd.add_argument('--goal-complete',action='store_true',help='仅自动规划任务：确认原目标已全部完成，停止自动接续')
    body(cmd,True)
    cmd=command('handoff','幂等补发已有交付，不重做任务')
    cmd.add_argument('--task',required=True)
    cmd.add_argument('--to',choices=['A','B','C','D'],required=True)
    for name in ('read-delivery','ack-delivery'):
        cmd=command(name,'分页读取交付记录' if name=='read-delivery' else '确认已完整读取指定版本交付')
        actor(cmd)
        cmd.add_argument('--delivery',required=True)
        if name=='read-delivery':
            cmd.add_argument('--offset',type=int,default=0)
            cmd.add_argument('--limit',type=int,default=2000)
        else:cmd.add_argument('--digest',required=True)
    cmd=command('quota','显示额度快照与可靠性；--refresh 使用各成员自身环境读取')
    cmd.add_argument('--refresh',action='store_true')
    cmd=command('assistance-policy','由本轮 A 设置协助执行和额度交接策略')
    actor(cmd)
    cmd.add_argument('--execution',choices=['on','off'])
    cmd.add_argument('--checkpoints',choices=['on','off'])
    cmd=command('install-approval-hook','为已绑定 B 配置官方逐请求审批 Hook，不绕过 Hook 信任')
    actor(cmd)
    cmd.add_argument('--target',choices=['B','C','D'],default='B')
    cmd.add_argument('--timeout',type=int,default=180)
    cmd=command('check-approval-hook','只读核验本轮 B 的 Hook 支持与信任状态')
    actor(cmd)
    cmd.add_argument('--target',choices=['B','C','D'],default='B')
    cmd=command('delegation-policy','登记用户委托开启、撤销或授权范围变更，并使旧批准失效')
    actor(cmd)
    cmd.add_argument('--enabled',choices=['on','off'])
    body(cmd,True)
    cmd=command('ask-question','B 将当前任务问题交给 A，发送后结束回合')
    actor(cmd)
    cmd.add_argument('--task')
    cmd.add_argument('--question-key',required=True,help='相同问题复用该键，避免重复提问')
    cmd.add_argument('--progress-note',help='新证据或实际进展，不能用改写原问题冒充进展')
    body(cmd,True)
    cmd=command('question-state','记录采用回答、转交用户或取消问题')
    actor(cmd)
    cmd.add_argument('--question',required=True)
    cmd.add_argument('--state',choices=['adopted','needs_user','cancelled'],required=True)
    body(cmd,True)
    cmd=command('prepare-approval-retry','A 核实旧操作未执行且准备好审核后，通知 B 重新请求')
    actor(cmd)
    cmd.add_argument('--approval',required=True)
    body(cmd,True)
    cmd=command('report-approval','B 记录获批操作的实际执行结果，不等于独立验证')
    actor(cmd)
    cmd.add_argument('--approval',required=True)
    cmd.add_argument('--outcome',choices=['success','failed','unknown'],required=True)
    body(cmd,True)
    cmd=command('review-approval','A 按原任务授权及自身当前权限审核 B 的一项官方审批')
    actor(cmd)
    cmd.add_argument('--approval',required=True)
    cmd.add_argument('--digest',required=True)
    cmd.add_argument('--token',required=True)
    cmd.add_argument('--decision',choices=['allow','deny'],required=True)
    cmd.add_argument('--within-a-permissions',action='store_true')
    cmd.add_argument('--capability-evidence')
    body(cmd,True)
    cmd=command('request-execution','B 提交当前任务内的协助操作，不由桥执行')
    actor(cmd)
    cmd.add_argument('--message',required=True)
    cmd.add_argument('--token',required=True)
    cmd.add_argument('--request',required=True)
    request_input=cmd.add_mutually_exclusive_group(required=True)
    request_input.add_argument('--request-file',help='UTF-8 JSON 请求，完整字段见 references/assistance.md')
    request_input.add_argument('--request-stdin',action='store_true',help='从标准输入读取 JSON，无需在受限工作树写请求文件')
    cmd=command('begin-execution','A 审核后登记开始；重复登记拒绝执行')
    actor(cmd)
    cmd.add_argument('--request',required=True)
    cmd.add_argument('--token',required=True)
    cmd.add_argument('--digest',required=True)
    body(cmd,True)
    cmd=command('finish-execution','A 交付执行结果并关闭请求消息')
    actor(cmd)
    cmd.add_argument('--request',required=True)
    cmd.add_argument('--token',required=True)
    cmd.add_argument('--outcome',choices=['success','failed','declined','needs_authorization','unknown'],required=True)
    cmd.add_argument('--result-file',required=True,help='UTF-8 JSON 结果及实际执行证据')
    cmd=command('resolve-execution','核实未知操作或补充授权，禁止自动重跑')
    actor(cmd)
    cmd.add_argument('--request',required=True)
    body(cmd,True)
    cmd=command('save-checkpoint','在项目内保存本窗口唯一交接文档，并暂停业务')
    actor(cmd)
    cmd.add_argument('--message',help='可选，已领取的额度交接通知')
    cmd.add_argument('--token')
    body(cmd,True)
    cmd=command('check-checkpoint','仅检查项目内本窗口及指定接管文档是否存在')
    actor(cmd)
    cmd=command('recover-checkpoint','原窗口核实文件落盘后的事务中断，或重试清理已消费文档')
    actor(cmd)
    cmd=command('resume-checkpoint','额度无法读取时，记录用户明确要求恢复本窗口的依据')
    actor(cmd)
    body(cmd,True)
    cmd=command('read-checkpoint','分页完整读取交接文档')
    actor(cmd)
    cmd.add_argument('--source',choices=['A','B','C','D'],required=True)
    cmd.add_argument('--offset',type=int,default=0)
    cmd.add_argument('--limit',type=int,default=2000)
    cmd=command('consume-checkpoint','持久化恢复计划及接管，再清理文档')
    actor(cmd)
    cmd.add_argument('--source',choices=['A','B','C','D'],required=True)
    cmd.add_argument('--digest',required=True)
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
        if name=='relay':cmd.add_argument('--member',choices=['A','B','C','D'],required=True)
    cmd=command('relay-stop','停止指定成员 relay，保留其他 relay 与状态')
    cmd.add_argument('--member',choices=['A','B','C','D'],required=True)
    for name,label in [('pause','暂停新投递，保留已开始的回合'),('resume','恢复调度与投递'),('stop','停止守护程序，保留状态及代码')]:
        command(name,label)
    try:from . import pair_commands
    except ImportError:import pair_commands
    pair_commands.register(command,actor,body)
    return parser


def read_body(args):
    if args.body_file:
        with open(args.body_file,'r',encoding='utf-8-sig',newline='') as file:
            return file.read()
    return args.body or ''


def validate_actor(store,args,*,allow_configuration_change=False):
    if not args.session:
        raise PairError('Session UUID required / 必须提供会话 UUID 或 CODEX_THREAD_ID')
    current_session=os.environ.get('CODEX_THREAD_ID')
    if not current_session or current_session.lower()!=args.session.lower():
        raise PairError('Current original CLI required / 不能填写其他窗口会话借用身份；须从本原CLI执行')
    if canonical_path(args.worktree)!=canonical_path(Path.cwd()):
        raise PairError('Actual cwd mismatch / 当前实际工作目录与 --worktree 不符')
    member=store.member(args.member)
    actor_home=canonical_path(getattr(args,'member_codex_home',None) or getattr(args,'codex_home',None) or effective_home())
    if actor_home!=canonical_path(member['codex_home']):
        raise PairError('Member account mismatch / 当前账号目录与成员绑定不符，禁止回退另一账号')
    if member['session_uuid']!=args.session.lower() or member['cwd']!=canonical_path(args.worktree):
        raise PairError('Member binding mismatch / 成员的会话或工作目录不符')
    with AppServer(codex_home=member['codex_home'], executable=getattr(args,'codex',None),cwd=member['cwd'],metadata_only=True,account_metadata=True) as observer:
        target=dict(member)
        if allow_configuration_change:target.pop('proof',None)
        observer.validate(target)


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
                active=db.execute("SELECT id FROM tasks WHERE assignee IN ("+slots+") AND status IN ('active','blocked','waiting_execution','quota_wait','checkpoint_wait')",ids).fetchall()
                executing=db.execute("SELECT 1 FROM execution_requests WHERE status='running'").fetchone()
                live=db.execute("SELECT * FROM messages WHERE recipient IN ("+slots+") AND (delivery_status IN ('delivering','unknown') OR (delivery_status='accepted' AND (remote_status IS NULL OR remote_status IN ('queued','inProgress'))))",ids).fetchall()
            # 当前统筹方串行执行的工具调用可以整合。不能豁免其他回合、
            # 正在开发的任务、队列或结果不明的请求。
            coordinator_id=store.setting('coordinator') or 'A'
            coordinator=next((m for m in receivers if m['id']==coordinator_id),None)
            current_coordinator=(coordinator and os.environ.get('CODEX_THREAD_ID')==coordinator['session_uuid']
                                 and canonical_path(Path.cwd())==coordinator['cwd'])
            if current_coordinator and len(live)==1:
                own=live[0]
                if (own['recipient']==coordinator_id and own['delivery_status']=='accepted' and own['remote_status']=='inProgress'
                        and own['process_status'] in ('claimed','completed')
                        and (own['kind']!='task' or own['process_status']=='completed')):
                    live=[]
            if active or live or executing:
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
    if store.setting('review_required'):
        try:from .pair_windows import current_window
        except ImportError:from pair_windows import current_window
        window=current_window(getattr(args,'codex',None))
        with store.transaction() as db:store._a_actor(db,'A',window['session_uuid'],window['cwd'])
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
        if store.setting('review_required'):store.require_reviewed(task['id'])
        if task['status'] not in ('delivered','integrated'):
            raise PairError('Task has no accepted commit delivery / 任务尚未交付可验收的提交')
        commits=json.loads(task['commits_json'] or '[]')
        if args.commit and args.commit!=commits:
            raise PairError('Commit list differs from delivery / 指定提交与任务交付列表不同')
        if not commits:raise PairError('No commits supplied / 任务没有提交')
        source=store.member(task['assignee']);worktree_proof(store,source['id'],member_workspace(source))
        proof=pair_git.check_delivery(member_workspace(source),task['base_commit'] or store.status()['pair']['base_commit'],commits,json.loads(task['allowed_paths']))
        if not proof['passed']:raise PairError('Delivery rejected / 交付校验失败: '+dumps(proof))
        target=canonical_path(args.worktree or member_workspace(store.member(store.setting('coordinator') or 'A')))
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
    try:from . import pair_commands
    except ImportError:import pair_commands
    if args.command in pair_commands.COMMANDS:return pair_commands.execute(store,args,sys.modules[__name__])
    if args.command=='join':
        if not os.environ.get('CODEX_THREAD_ID') or not args.current_session and str(args.session).lower()!=os.environ['CODEX_THREAD_ID'].lower():
            raise PairError('Original visible CLI required / 可见成员只能绑定本原窗口')
        home=Path(canonical_path(getattr(args,'member_codex_home',None) or home))
        cwd=canonical_path(args.worktree)
        if args.current_session:
            args.session=os.environ.get('CODEX_THREAD_ID')
            if not args.session or canonical_path(Path.cwd())!=cwd:
                raise PairError('Current session context required / --current-session 必须在目标 Codex 会话及其实际 worktree 内运行')
        git_proof=worktree_proof(store,args.member,cwd,args.branch)
        backend=args.backend or ('managed' if args.new_session else 'queue')
        if args.new_session:
            raise PairError('Visible sessions must already exist / 可见成员只能使用原窗口；隐藏审核会话由独立控制器创建')
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
        if any('=' not in c or not all(c.split('=',1)) for c in args.required_check):raise PairError('Invalid required check / 使用NAME=准确命令定义必要检查')
        if store.setting('review_required'):
            window=__import__('importlib').import_module('scripts.pair_windows' if __package__ else 'pair_windows').current_window(executable)
            with store.transaction() as db:store._a_actor(db,'A',window['session_uuid'],window['cwd'])
        members=store.visible_members() if args.to=='auto' else [store.member(args.to)]
        for member in members:worktree_proof(store,member['id'],member_workspace(member))
        member=members[0]
        base=git_run(member_workspace(member),'rev-parse','--verify','--end-of-options',args.base_ref+'^{commit}') if args.base_ref else None
        if args.to=='auto' and args.effort>=5:
            try:from .pair_quota import refresh
            except ImportError:from pair_quota import refresh
            refresh(store,executable=executable,max_age=30)
        return store.assign(args.task,args.title,args.to,args.allow,body=read_body(args),dependencies=args.depends_on,base_commit=base,report_to=args.report_to,effort=args.effort,
            task_kind=args.task_kind,capabilities=args.capability,resources=args.resource,priority=args.priority,acceptance=args.acceptance,pin_member=args.pin_member,independent_of=args.independent_of,
            required_checks=[{'name':c.split('=',1)[0],'command':c.split('=',1)[1],'required':True} for c in args.required_check if '=' in c])
    if args.command=='handoff':return store.handoff(args.task,args.to)
    if args.command=='quota':
        if args.refresh:
            try:from .pair_quota import refresh
            except ImportError:from pair_quota import refresh
            refresh(store,executable=executable,max_age=0)
        return store.quota()
    if args.command=='send':
        if store.setting('review_required'):
            try:from .pair_windows import current_window
            except ImportError:from pair_windows import current_window
            window=current_window(executable)
            with store.transaction() as db:store._binding(db,args.sender,window['session_uuid'],window['cwd'])
        return store.send(args.sender,args.to,read_body(args),kind=args.kind,task_id=args.task,
                          dedupe_key=args.dedupe_key,reply_to=args.reply_to,progress_note=args.progress_note)
    if args.command=='recover-delivery':
        return store.recover_delivery(args.message)
    if args.command in ('assistance-policy','install-approval-hook','check-approval-hook','delegation-policy','ask-question','question-state','prepare-approval-retry','report-approval','review-approval','request-execution','begin-execution','finish-execution','resolve-execution',
                        'save-checkpoint','check-checkpoint','recover-checkpoint','resume-checkpoint','read-checkpoint','consume-checkpoint'):
        validate_actor(store,args)
        binding=(args.member,args.session,args.worktree)
        if args.command=='delegation-policy':return store.delegation_policy(*binding,read_body(args),enabled=None if args.enabled is None else args.enabled=='on')
        if args.command=='ask-question':
            if args.member=='A':raise PairError('Executor required / 普通执行者向A提问')
            if not store.setting('delegation_enabled'):raise PairError('Delegation disabled / 未开启问题委托')
            if args.task:
                task=store.task(args.task)
                if task['assignee']!=args.member or task['status']!='active':raise PairError('Active owned task required / 提问关联任务必须是 B 当前负责的活动任务')
            return store.send(args.member,'A',read_body(args),kind='question',task_id=args.task,dedupe_key='delegated-question:'+args.question_key,progress_note=args.progress_note)
        if args.command=='question-state':return store.question_state(*binding,args.question,args.state,read_body(args))
        if args.command=='prepare-approval-retry':return store.prepare_approval_retry(*binding,args.approval,read_body(args))
        if args.command=='report-approval':return store.report_approval(*binding,args.approval,args.outcome,read_body(args))
        if args.command=='check-approval-hook':
            b=store.member(args.target)
            with AppServer(codex_home=b['codex_home'],executable=executable,cwd=b['cwd'],metadata_only=True,hook_metadata=True) as observer:
                return store.record_hook_status(observer.rpc('hooks/list',{}),target=args.target)
        if args.command=='install-approval-hook':
            return store.install_approval_hook(*binding,python=sys.executable,script=str(Path(__file__).with_name('pair_permission_hook.py')),timeout=args.timeout,target=args.target)
        if args.command=='review-approval':
            return store.review_approval(*binding,args.approval,args.token,args.digest,args.decision,read_body(args),
                within_permissions=args.within_a_permissions,capability_evidence=args.capability_evidence)
        if args.command=='assistance-policy':
            return store.configure_assistance(*binding,execution=None if args.execution is None else args.execution=='on',checkpoints=None if args.checkpoints is None else args.checkpoints=='on')
        if args.command=='request-execution':
            content=(sys.stdin.buffer.read().decode('utf-8-sig') if hasattr(sys.stdin,'buffer') else sys.stdin.read()) if args.request_stdin else Path(args.request_file).read_text(encoding='utf-8-sig')
            return store.request_execution(*binding,args.message,args.token,args.request,json.loads(content.lstrip('\ufeff')))
        if args.command=='begin-execution':return store.begin_execution(*binding,args.request,args.token,args.digest,read_body(args))
        if args.command=='finish-execution':return store.finish_execution(*binding,args.request,args.token,args.outcome,json.loads(Path(args.result_file).read_text(encoding='utf-8-sig')))
        if args.command=='resolve-execution':return store.resolve_execution(*binding,args.request,read_body(args))
        if args.command=='save-checkpoint':return store.save_checkpoint(*binding,read_body(args),message_id=args.message,token=args.token)
        if args.command=='check-checkpoint':return store.check_checkpoint(*binding)
        if args.command=='recover-checkpoint':return store.recover_checkpoint(*binding)
        if args.command=='resume-checkpoint':return store.resume_checkpoint(*binding,read_body(args))
        if args.command=='read-checkpoint':return store.read_checkpoint(*binding,args.source,offset=args.offset,limit=args.limit)
        if args.command=='consume-checkpoint':return store.consume_checkpoint(*binding,args.source,args.digest,read_body(args))
    if args.command in ('claim','recover-claim','read-message','complete','read-delivery','ack-delivery'):
        if args.command=='recover-claim':
            current_session=os.environ.get('CODEX_THREAD_ID')
            if not current_session or current_session.lower()!=str(args.session).lower():
                raise PairError('Original CLI session required / 恢复回执须在原领取会话内执行')
            if canonical_path(effective_home())!=canonical_path(store.member(args.member)['codex_home']):
                raise PairError('Original CLI home required / 恢复回执须使用原领取会话的配置目录')
        validate_actor(store,args)
        if args.command=='read-delivery':return store.read_delivery(args.member,args.session,args.worktree,args.delivery,offset=args.offset,limit=args.limit)
        if args.command=='ack-delivery':return store.ack_delivery(args.member,args.session,args.worktree,args.delivery,args.digest)
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
        return store.complete(args.member,args.session,args.worktree,args.message,args.token,read_body(args),commits=args.commit,partial=args.partial,goal_complete=args.goal_complete)
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
