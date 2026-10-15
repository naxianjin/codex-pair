"""codex-pair 统一命令入口，依赖仅为 Python 标准库与已有 Codex/Git。"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid

try:
    from .pair_core import Daemon, PairError, Store, effective_home
    from .pair_transport import AppServer, QueueTransport, TransportError, TransportPool, canonical_path
except ImportError:
    from pair_core import Daemon, PairError, Store, effective_home
    from pair_transport import AppServer, QueueTransport, TransportError, TransportPool, canonical_path


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise PairError(message)


def build_parser():
    common=argparse.ArgumentParser(add_help=False)
    common.add_argument('--json',action='store_true',default=argparse.SUPPRESS,help='输出 JSON')
    common.add_argument('--pair',default=argparse.SUPPRESS,help='协作 ID')
    common.add_argument('--codex-home',default=argparse.SUPPRESS,help='显式 Codex 主目录；默认遵循 CODEX_HOME')
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
    cmd=command('init','初始化仓库外的协作状态')
    cmd.add_argument('--project',required=True)
    cmd.add_argument('--base-ref',default='HEAD')
    cmd=command('join','绑定已有 CLI 会话，或创建独占托管会话')
    cmd.add_argument('--member',choices=['A','B'],required=True)
    cmd.add_argument('--worktree',required=True)
    group=cmd.add_mutually_exclusive_group(required=True)
    group.add_argument('--session',help='已有 CLI 的完整会话 UUID')
    group.add_argument('--new-session',action='store_true',help='为此协作创建独占 app-server 会话')
    cmd.add_argument('--backend',choices=['queue','shared','managed'],help='已有会话默认 queue，新会话为 managed')
    cmd=command('assign','分配任务；依赖和范围检查通过后才激活')
    cmd.add_argument('--task',required=True)
    cmd.add_argument('--title',required=True)
    cmd.add_argument('--to',choices=['A','B'],required=True)
    cmd.add_argument('--allow',action='append',required=True,help='仓库相对修改范围，可重复')
    cmd.add_argument('--depends-on',action='append',default=[])
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
    cmd=command('complete','使用领取令牌提交实质结果')
    actor(cmd)
    cmd.add_argument('--message',required=True)
    cmd.add_argument('--token',required=True)
    cmd.add_argument('--commit',action='append',default=[],help='交付的完整提交 SHA，可重复')
    body(cmd,True)
    cmd=command('integrate','整合明确交付的提交（等待 B 的模块交付后连接）')
    cmd.add_argument('--task')
    cmd.add_argument('--worktree')
    cmd.add_argument('--commit',action='append',default=[])
    group=cmd.add_mutually_exclusive_group()
    group.add_argument('--continue',dest='continue_integration',action='store_true')
    group.add_argument('--abort',action='store_true')
    cmd=command('status','显示任务、消息各阶段及守护进程证据')
    cmd.add_argument('--probe',type=float,default=0,help='等待指定秒数，验证 daemon 响应随机挑战')
    cmd=command('daemon','在当前进程运行单实例守护程序')
    cmd.add_argument('--max-attempts',type=int,default=3)
    cmd.add_argument('--poll-interval',type=float,default=.25)
    cmd.add_argument('--rpc-timeout',type=float,default=15,help='投递回执超时，不是任务耗时限制')
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
    if member['session_uuid']!=args.session.lower() or member['cwd']!=canonical_path(args.worktree):
        raise PairError('Member binding mismatch / 成员的会话或工作目录不符')
    with AppServer(codex_home=store.home, executable=getattr(args,'codex',None),cwd=member['cwd']) as observer:
        observer.validate(member)


def execute(args):
    home=Path(getattr(args,'codex_home',None) or effective_home()).resolve()
    pair_id=getattr(args,'pair',None)
    if args.command=='init':
        project=canonical_path(args.project)
        result=subprocess.run(['git','-C',project,'rev-parse','--verify','--end-of-options',args.base_ref+'^{commit}'],capture_output=True,text=True)
        if result.returncode:
            raise PairError(result.stderr.strip()+' / 无法解析基准提交')
        store=Store.initialize(pair_id or str(uuid.uuid4()),project,result.stdout.strip(),home=home)
        return store.status()
    if not pair_id:
        raise PairError('--pair is required / 必须指定协作 ID')
    store=Store(pair_id,home=home)
    executable=getattr(args,'codex',None)
    if args.command=='join':
        cwd=canonical_path(args.worktree)
        backend=args.backend or ('managed' if args.new_session else 'queue')
        if args.new_session:
            if backend!='managed':
                raise PairError('--new-session requires managed backend / 新会话必须使用 managed 后端')
            try:existing=store.member(args.member)
            except PairError:existing=None
            if existing:
                if existing['cwd']!=cwd or existing['backend']!='managed':
                    raise PairError('Member already bound differently / 成员已有不同绑定')
                return existing
            with AppServer(codex_home=home,executable=executable,cwd=cwd) as client:
                thread=client.create_session(cwd)
                member={'session_uuid':thread['id'],'cwd':cwd}
                receipt=client.start(member,{'id':str(uuid.uuid4()),'envelope':'This session is reserved for codex-pair. Reply only PAIR_READY. Do not use tools or change files.'})
                completion=client.wait_turn(thread['id'],receipt['turn_id'])
                if completion['status']!='completed':
                    raise PairError('Session bootstrap failed / 会话初始化回合未完成')
                proof=client.validate(member)
                return store.join(args.member,thread['id'],cwd,backend=backend,owned=True,proof=proof)
        if backend=='managed':
            raise PairError('Use --new-session for managed sessions / 独占会话须由 --new-session 创建')
        with AppServer(backend='shared' if backend=='shared' else 'managed',codex_home=home,executable=executable,cwd=cwd) as client:
            proof=client.validate({'session_uuid':args.session,'cwd':cwd})
            return store.join(args.member,args.session,cwd,backend=backend,owned=False,proof=proof)
    if args.command=='assign':
        return store.assign(args.task,args.title,args.to,args.allow,body=read_body(args),dependencies=args.depends_on)
    if args.command=='send':
        return store.send(args.sender,args.to,read_body(args),kind=args.kind,task_id=args.task,
                          dedupe_key=args.dedupe_key,reply_to=args.reply_to,progress_note=args.progress_note)
    if args.command in ('claim','complete'):
        validate_actor(store,args)
        if args.command=='claim':
            return store.claim(args.member,args.session,args.worktree,message_id=args.message)
        return store.complete(args.member,args.session,args.worktree,args.message,args.token,read_body(args),commits=args.commit)
    if args.command=='integrate':
        raise PairError('Git integration awaits B delivery / Git 整合接口尚待 B 的明确交付提交；当前不会执行整合')
    if args.command=='status':
        if args.probe<0 or args.probe>30:
            raise PairError('--probe must be between 0 and 30 / 存活探测须在 0 至 30 秒之间')
        return store.status(probe_timeout=args.probe)
    if args.command in ('pause','resume','stop'):
        return store.control({'pause':'paused','resume':'running','stop':'stopped'}[args.command])
    if args.command=='daemon':
        if args.rpc_timeout<=0 or args.rpc_timeout>60:
            raise PairError('RPC timeout must be within 0..60 / 回执超时须大于 0 且最多 60 秒')
        pool=TransportPool(codex_home=home,executable=executable,timeout=args.rpc_timeout)
        Daemon(store,pool,max_attempts=args.max_attempts,poll_interval=args.poll_interval).run()
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
    except (PairError,TransportError,OSError,sqlite3.Error,ValueError,KeyError) as exc:
        if '--json' in argv:
            print(json.dumps({'ok':False,'error':str(exc),'type':type(exc).__name__},ensure_ascii=False))
        else:
            print(str(exc),file=sys.stderr)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
