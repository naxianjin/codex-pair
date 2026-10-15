"""只接入用户当前打开的两个会话；账号与窗口标题不参与角色选择。"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

try:
    from .pair_core import FileLock, PairError, Store, dumps, effective_home, member_workspace
    from .pair_transport import AppServer, canonical_path, session_uuid, binding_proof
    from . import pair_git
except ImportError:
    from pair_core import FileLock, PairError, Store, dumps, effective_home, member_workspace
    from pair_transport import AppServer, canonical_path, session_uuid, binding_proof
    import pair_git


_children=[]


def current_window(executable=None):
    """只接受本会话环境和官方 API 的交叉验证，不枚举或猜测其他窗口。"""
    value=os.environ.get('CODEX_THREAD_ID')
    if not value:
        raise PairError('Current CLI required / 请在要参与协作的 Codex 窗口内调用技能，无法从普通终端猜测窗口')
    current={'session_uuid':session_uuid(value),'cwd':canonical_path(Path.cwd()),
             'codex_home':canonical_path(effective_home())}
    with AppServer(codex_home=current['codex_home'],cwd=current['cwd'],executable=executable,metadata_only=True) as observer:
        # 工具可能临时设置了 workdir；会话根目录从当前 UUID 的元数据读取。
        # 这既不挑选其他历史会话，也不把临时工具目录误登记成 CLI 根目录。
        thread=observer.read_thread(current['session_uuid'])
        current['cwd']=canonical_path(thread['cwd'])
        current['proof']=binding_proof(observer.validate(current),require_provider=True)
    return current


def workspace_for(window, project):
    target=pair_git.inspect_repo(project)
    try:local=pair_git.inspect_repo(window['cwd'])
    except pair_git.GitError:local=None
    # 已在同仓库的另一个 worktree 时沿用它；从其他目录打开时使用目标项目。
    return local['root'] if local and canonical_path(local['common_dir'])==canonical_path(target['common_dir']) else target['root']


def find_binding(state_root, window):
    found=[]
    for path in sorted(Path(state_root).glob('*/state.sqlite3')):
        if path.is_symlink() or path.resolve()!=path.absolute():continue
        with closing(sqlite3.connect('file:'+path.as_posix()+'?mode=ro',uri=True)) as db:
            columns={r[1] for r in db.execute('PRAGMA table_info(members)')}
            if 'codex_home' not in columns:continue
            row=db.execute('''SELECT c.id,m.id,m.codex_home,m.cwd FROM collaborations c
                JOIN members m ON m.session_uuid=? WHERE c.status<>'stopped' ''',(window['session_uuid'],)).fetchone()
        if row and canonical_path(row[2])==window['codex_home'] and canonical_path(row[3])==window['cwd']:
            found.append((row[0],row[1]))
    if len(found)>1:
        raise PairError('Multiple current bindings / 当前会话存在多条有效绑定，请用 --pair 明确选择，不能自动换角色')
    return found[0] if found else None


def bind_window(store, member_id, window, workspace, cli, mode):
    proof=dict(window['proof'])
    proof['git']=cli.worktree_proof(store,member_id,workspace)
    return store.join(member_id,window['session_uuid'],window['cwd'],backend='queue',owned=False,
        proof=proof,member_codex_home=window['codex_home'],delivery_mode=mode)


def ps_command(argv):
    return '& '+' '.join("'"+str(arg).replace("'","''")+"'" for arg in argv)


def bridge(store, cli, executable=None):
    with FileLock(store.directory/'bridge-start.lock',timeout=10):
        status=store.status(probe_timeout=.3)
        if status['runtime']['lock_held']:
            if not status['runtime']['probe_confirmed']:
                raise PairError('Bridge not responding / 已有消息桥占锁但未响应探测，不抢占或重复启动')
            return {'running':True,'pid':status['runtime']['pid'],'reused':True}
        if any(r['lock_held'] for r in status['relays']):
            return {'running':False,'mode':'relay','detail':'已有逐成员 relay，保留现有消息桥；用 status --probe 检查各侧'}
        if status['pair']['status']=='stopped':store.control('running')
        project=status['pair']['project']
        logs=Path(pair_git.prepare_pair_directory(project,str(Path(project)/'.codex-pair'/'logs'),'logs'))
        stdout_path=logs/(store.pair_id+'-bridge.stdout.log')
        stderr_path=logs/(store.pair_id+'-bridge.stderr.log')
        for path in (stdout_path,stderr_path):
            if path.resolve()!=path.absolute():raise PairError('Redirected log / 日志路径存在重定向')
        argv=[sys.executable,str(Path(cli.__file__).resolve()),'daemon','--pair',store.pair_id,
            '--state-root',str(store.state_root),'--json']
        if executable:argv+=['--codex',executable]
        with stdout_path.open('ab') as output,stderr_path.open('ab') as errors:
            process=subprocess.Popen(argv,cwd=project,stdin=subprocess.DEVNULL,stdout=output,stderr=errors,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),start_new_session=os.name!='nt')
        _children.append(process)
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            if process.poll() is not None:
                raise PairError('Bridge exited / 消息桥启动后退出，请检查 '+str(stderr_path))
            if store.status(probe_timeout=.2)['runtime']['probe_confirmed']:
                return {'running':True,'pid':process.pid,'reused':False,'stdout':str(stdout_path),'stderr':str(stderr_path)}
        raise PairError('Bridge startup unconfirmed / 消息桥存活尚未确认，保留进程与日志，禁止重复启动')


def invitation(store, cli, goal):
    flow=store.setting('window_flow')
    if flow:
        path=Path(flow['invitation'])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=flow['sha256']:
            raise PairError('Invitation changed / 原邀请已修改或缺失，保留现场，不覆盖用户文件')
    else:
        status=store.status()
        project=status['pair']['project']
        directory=Path(pair_git.prepare_pair_directory(project,str(Path(project)/'.codex-pair'/'handoffs'),'handoffs'))
        path=directory/(store.pair_id+'-invite.json')
        payload={'protocol':'codex-pair/windows/1','pair':store.pair_id,'state_root':str(store.state_root),
            'project':project,'base_commit':status['pair']['base_commit'],'goal':goal,
            'inviter_session':store.member('A')['session_uuid'],'nonce':uuid.uuid4().hex}
        content=json.dumps(payload,ensure_ascii=False,sort_keys=True,indent=2)+'\n'
        with path.open('x',encoding='utf-8',newline='\n') as file:file.write(content)
        flow={'goal':goal,'invitation':str(path),'sha256':hashlib.sha256(content.encode('utf-8')).hexdigest()}
        store.setting('window_flow',flow)
    script=str(Path(cli.__file__).resolve())
    argv=[sys.executable,script,'accept','--invite',str(path),'--json']
    text=('CODEX_PAIR_JOIN_V1\n你是窗口 B，请加入窗口 A 已创建的协作，不新建协作或 CLI。\n'
        +'先读取：'+str(Path(script).parent.parent/'SKILL.md')+'\n'
        +'在你当前窗口自己的工具环境中执行以下命令，自动读取本窗口会话、cwd 和账号；不用修改 CODEX_HOME。\n'
        +ps_command(argv)+'\n'
        +'命令成功后按返回的 after_join 执行。只报告绑定与已验证的通信状态，不把入队当成已回复。')
    return {'invite_path':str(path),'join_argv':argv,'join_instruction':text}


def start(args, cli):
    window=current_window(getattr(args,'codex',None))
    repo=pair_git.inspect_repo(args.project or str(Path.cwd()))
    project=repo['root']
    goal=cli.read_body(args)
    if not goal.strip():raise PairError('Goal required / 请给出协作目标')
    root=Path(getattr(args,'state_root',None) or Path(project)/'.codex-pair'/'state').resolve()
    binding=find_binding(root,window)
    pair_id=getattr(args,'pair',None) or (binding[0] if binding else 'windows-'+uuid.uuid4().hex[:12])
    if binding and pair_id!=binding[0]:
        raise PairError('Current window already paired / 当前窗口已有有效协作，先停止原协作再另建，不替换绑定')
    path=root/pair_id/'state.sqlite3'
    if path.is_file():
        store=Store(pair_id,state_root=root,home=window['codex_home'])
        if canonical_path(store.status()['pair']['project'])!=canonical_path(project):
            raise PairError('Project mismatch / 已有协作不是当前目标项目')
        role=binding[1] if binding else 'A'
        existing=store.member(role)
        if existing['session_uuid']!=window['session_uuid'] or canonical_path(existing['codex_home'])!=window['codex_home']:
            raise PairError('Another window already bound / 该角色已绑定其他窗口，不自动替换')
        with AppServer(codex_home=window['codex_home'],cwd=window['cwd'],executable=getattr(args,'codex',None),metadata_only=True) as observer:
            observer.validate(existing)
    else:
        store=Store.initialize(pair_id,project,repo['head'],home=window['codex_home'],state_root=root)
        store.configure_git(repo)
        role='A'
        bind_window(store,'A',window,workspace_for(window,project),cli,args.delivery_mode)
    flow=store.setting('window_flow')
    if flow and flow['goal']!=goal:
        raise PairError('Existing goal differs / 当前窗口已有不同目标的协作，请明确复用原目标或先停止，不静默改写')
    result={'role':role,'pair':pair_id,'state_root':str(root),'current_window':store.member(role),
        'bridge':bridge(store,cli,getattr(args,'codex',None)),
        'automatic_peer_discovery':False,'detail':'只绑定用户明确选中的当前窗口；其他窗口需接受一次邀请，不按账号目录或窗口标题猜测'}
    if role=='A':result.update(invitation(store,cli,goal))
    result['status']=store.status()
    return result


def accept(args, cli):
    path=Path(args.invite).resolve(strict=True)
    payload=json.loads(path.read_text(encoding='utf-8'))
    if payload.get('protocol')!='codex-pair/windows/1':raise PairError('Invalid invitation / 不支持的窗口邀请格式')
    store=Store(payload['pair'],state_root=payload['state_root'])
    flow=store.setting('window_flow')
    if (not flow or Path(flow['invitation']).resolve()!=path
            or hashlib.sha256(path.read_bytes()).hexdigest()!=flow['sha256']):
        raise PairError('Invitation proof mismatch / 邀请内容或路径与发起记录不符')
    status=store.status()
    if status['pair']['project']!=payload['project'] or status['pair']['base_commit']!=payload['base_commit']:
        raise PairError('Invitation project mismatch / 邀请项目或基准不符')
    if status['pair']['status']=='stopped':raise PairError('Invitation stopped / 原协作已停止，请由 A 明确恢复后加入')
    a=store.member('A')
    if a['session_uuid']!=payload['inviter_session']:raise PairError('Inviter mismatch / 发起窗口绑定已变化')
    window=current_window(getattr(args,'codex',None))
    if window['session_uuid']==a['session_uuid']:raise PairError('Second window required / 请把邀请交给另一个已打开的窗口，不能由 A 自己加入 B')
    binding=find_binding(store.state_root,window)
    if binding and binding!=(store.pair_id,'B'):
        raise PairError('Peer already paired / 当前窗口已参加另一协作，不自动改绑')
    workspace=workspace_for(window,payload['project'])
    member=bind_window(store,'B',window,workspace,cli,a['delivery_mode'])
    service=bridge(store,cli,getattr(args,'codex',None))
    hello=store.send('B','A','窗口连接验证：先按信封领取本问题，再用 send --from A --to B --kind reply --reply-to 本问题ID，正文说明已收到并确认本协作；随后 complete 本问题。B 收到回复后领取并完成回复即可。只验证这两个当前窗口，不分配开发任务、不改项目文件、不发送额外致谢。',
        kind='question',dedupe_key='window-handshake')
    return {'role':'B','pair':store.pair_id,'current_window':member,'workspace':member_workspace(member),
        'goal':flow['goal'],'bridge':service,'handshake_message':hello['id'],
        'status':store.status(),
        'after_join':('本窗口已绑定手动交接模式，需要用户转交 handoffs 中的领取指令；不会自动唤醒，不报告自动通信通过。' if a['delivery_mode']=='manual' else
            '本窗口已经绑定。结束当前回合，让桥投递连接验证；收到 A 的 reply 后领取和完成。只有 status.window_flow.communication=verified 才报告自动通信验证通过；否则仍待确认。')}


def execute(args, cli):
    if args.command=='start':return start(args,cli)
    if args.command=='accept':return accept(args,cli)
    pair_id=getattr(args,'pair',None)
    if not pair_id:raise PairError('--pair is required / 消息桥需要协作 ID')
    home=Path(getattr(args,'codex_home',None) or effective_home()).resolve()
    store=Store(pair_id,home=home,state_root=cli.resolve_state_root(args,home,pair_id))
    if args.action=='stop':return store.control('stopped')
    return bridge(store,cli,getattr(args,'codex',None))
