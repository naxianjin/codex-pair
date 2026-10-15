"""Codex 官方 app-server 的标准库客户端；不读写 Codex 内部数据库。"""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid


class TransportError(RuntimeError):
    """确定尚未投递的错误；只有 retryable=True 时允许有限重试。"""

    def __init__(self, message, *, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class DeliveryUnknown(TransportError):
    """请求可能已经到达，必须对账，禁止自动重发。"""


class RpcError(TransportError):
    def __init__(self, error):
        self.error = error
        super().__init__(json.dumps(error, ensure_ascii=False))


def canonical_path(path):
    return os.path.normcase(str(Path(path).expanduser().resolve(strict=True)))


def session_uuid(value):
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise TransportError("Invalid session UUID / 无效会话 UUID") from exc
    if parsed != value.lower():
        raise TransportError("Use a full session UUID / 必须使用完整会话 UUID")
    return parsed


def codex_command(executable=None):
    """绕过 Windows .cmd 对正文的二次 shell 解析。"""
    candidate = Path(executable) if executable else Path(shutil.which('codex.exe') or shutil.which('codex') or '')
    if not str(candidate) or not candidate.is_file():
        raise TransportError('Codex executable not found / 找不到 Codex 可执行文件')
    if candidate.suffix.lower() == '.exe':
        return [str(candidate.resolve())]
    if os.name=='nt':
        # 优先直接运行原生程序，保证拥有并能清理实际 app-server 进程。
        for package, target in [('codex-win32-x64','x86_64-pc-windows-msvc'),
                                ('codex-win32-arm64','aarch64-pc-windows-msvc')]:
            native=candidate.parent/'node_modules/@openai/codex/node_modules/@openai'/package/'vendor'/target/'bin/codex.exe'
            if native.is_file():
                return [str(native.resolve())]
    # npm 安装保留官方 JS 启动器；node 使用参数数组启动原生 Codex。
    launcher = candidate.parent / 'node_modules' / '@openai' / 'codex' / 'bin' / 'codex.js'
    node = shutil.which('node.exe') or shutil.which('node')
    if launcher.is_file() and node:
        return [node, str(launcher)]
    if os.name != 'nt' and os.access(candidate, os.X_OK):
        return [str(candidate)]
    raise TransportError('Cannot safely launch Codex shim / 无法安全启动 Codex 包装脚本，请指定 codex.exe')


class ProcessJob:
    """Windows Job：父进程崩溃时终止其专属 CLI，避免幽灵进程继续写代码。"""

    def __init__(self,process):
        self.handle=None
        if os.name!='nt':return
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_=[('process_time',ctypes.c_int64),('job_time',ctypes.c_int64),
                      ('flags',wintypes.DWORD),('min_working_set',ctypes.c_size_t),
                      ('max_working_set',ctypes.c_size_t),('active_limit',wintypes.DWORD),
                      ('affinity',ctypes.c_size_t),('priority',wintypes.DWORD),('scheduling',wintypes.DWORD)]
        class Io(ctypes.Structure):
            _fields_=[(name,ctypes.c_uint64) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
        class Extended(ctypes.Structure):
            _fields_=[('basic',Basic),('io',Io),('process_memory',ctypes.c_size_t),
                      ('job_memory',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        self.kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p]
        self.kernel.CreateJobObjectW.restype=ctypes.c_void_p
        self.kernel.SetInformationJobObject.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes=[ctypes.c_void_p,ctypes.c_void_p]
        self.kernel.CloseHandle.argtypes=[ctypes.c_void_p]
        handle=self.kernel.CreateJobObjectW(None,None)
        information=Extended();information.basic.flags=0x2000
        if not handle:
            raise TransportError('CreateJobObject failed / 无法建立子进程崩溃保护: '+str(ctypes.get_last_error()))
        if not self.kernel.SetInformationJobObject(handle,9,ctypes.byref(information),ctypes.sizeof(information)) or not self.kernel.AssignProcessToJobObject(handle,int(process._handle)):
            error=ctypes.get_last_error();self.kernel.CloseHandle(handle)
            raise TransportError('AssignProcessToJobObject failed / 无法绑定子进程崩溃保护: '+str(error))
        self.handle=handle

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle);self.handle=None


class AppServer:
    """一条长连接。managed 拥有独立进程；shared 仅代理现有守护进程。"""

    def __init__(self, *, backend='managed', executable=None, codex_home=None,
                 cwd=None, timeout=15, evidence=None, process_factory=subprocess.Popen):
        if backend not in ('managed', 'shared'):
            raise TransportError('Invalid backend / 无效传输后端')
        self.backend, self.timeout = backend, timeout
        self._next_id = 0
        self._pending = {}
        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._dead = False
        self.notifications = queue.Queue(maxsize=2048)
        self.server_requests = queue.Queue()
        self._evidence = evidence
        argv = codex_command(executable) + ['app-server']
        argv += ['proxy'] if backend == 'shared' else ['--listen', 'stdio://']
        env = os.environ.copy()
        if codex_home is not None:
            env['CODEX_HOME'] = str(codex_home)
        self._stderr = []
        try:
            self.process = process_factory(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, cwd=cwd, env=env,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as exc:
            raise TransportError(str(exc), retryable=True) from exc
        try:
            self._job=ProcessJob(self.process)
        except BaseException:
            self.process.kill();self.process.wait(timeout=5)
            for stream in (self.process.stdin,self.process.stdout,self.process.stderr):stream.close()
            raise
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._errors = threading.Thread(target=self._read_errors, daemon=True)
        self._reader.start()
        self._errors.start()
        try:
            self.identity = self.rpc('initialize', {'clientInfo': {
                'name': 'codex_pair', 'title': 'codex-pair', 'version': '0.1.0'}})
            self.notify('initialized', {})
            if codex_home and canonical_path(self.identity['codexHome']) != canonical_path(codex_home):
                raise TransportError('CODEX_HOME mismatch / app-server 使用了不同的 Codex 目录')
        except BaseException:
            self.close()
            raise

    def _record(self, direction, value):
        if self._evidence:
            self._evidence({'time': time.time(), 'direction': direction, 'value': value})

    def _read(self):
        try:
            for line in self.process.stdout:
                obj = json.loads(line.decode('utf-8'))
                self._record('receive', obj)
                with self._condition:
                    if 'id' in obj and 'method' not in obj:
                        self._pending[obj['id']] = obj
                        self._condition.notify_all()
                    elif 'id' in obj:
                        # 维持原权限配置，不擅自批准服务端请求。
                        self.server_requests.put(obj)
                    else:
                        try:
                            self.notifications.put_nowait(obj)
                        except queue.Full:
                            try:self.notifications.get_nowait()
                            except queue.Empty:pass
                            self.notifications.put_nowait(obj)
        except (OSError, ValueError) as exc:
            self._stderr.append(str(exc))
        finally:
            with self._condition:
                self._dead = True
                self._condition.notify_all()

    def _read_errors(self):
        for line in self.process.stderr:
            self._stderr.append(line.decode('utf-8', errors='replace').rstrip())
            self._stderr[:] = self._stderr[-100:]

    def _write(self, obj):
        self._record('send', obj)
        # ASCII JSON 转义能避开本机 console code page 的中文损坏问题。
        data = (json.dumps(obj, ensure_ascii=True, separators=(',', ':')) + '\n').encode('ascii')
        with self._write_lock:
            self.process.stdin.write(data)
            self.process.stdin.flush()

    def notify(self, method, params):
        self._write({'method': method, 'params': params})

    def rpc(self, method, params, *, timeout=None):
        with self._condition:
            if self._dead:
                raise TransportError('App-server disconnected / app-server 已断开: ' + '\n'.join(self._stderr), retryable=True)
            self._next_id += 1
            request_id = self._next_id
        try:
            self._write({'id': request_id, 'method': method, 'params': params})
        except (OSError, ValueError) as exc:
            raise DeliveryUnknown('App-server write outcome unknown / 写入结果不明: ' + str(exc)) from exc
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        with self._condition:
            while request_id not in self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._dead:
                    raise DeliveryUnknown('RPC result unknown / RPC 结果不明: ' + method + ' ' + '\n'.join(self._stderr))
                self._condition.wait(remaining)
            obj = self._pending.pop(request_id)
        if 'error' in obj:
            raise RpcError(obj['error'])
        return obj['result']

    def read_thread(self, thread_id):
        return self.rpc('thread/read', {'threadId': session_uuid(thread_id)})['thread']

    def validate(self, member):
        thread = self.read_thread(member['session_uuid'])
        if session_uuid(thread['id']) != session_uuid(member['session_uuid']):
            raise TransportError('Session identity mismatch / 会话标识不符')
        if canonical_path(thread['cwd']) != canonical_path(member['cwd']):
            raise TransportError('Session working directory mismatch / 会话实际工作目录不符')
        if thread.get('ephemeral'):
            raise TransportError('Ephemeral session cannot recover / 临时会话无法崩溃恢复')
        if self.backend == 'shared' and thread.get('status', {}).get('type') == 'notLoaded':
            raise TransportError('Session is not loaded by this server / 目标会话未由当前服务器加载')
        proof=member.get('proof')
        proof=json.loads(proof) if isinstance(proof,str) else proof
        if proof and proof.get('git'):
            try:from .pair_git import validate_worktree
            except ImportError:from pair_git import validate_worktree
            expected=proof['git']
            validation=validate_worktree(member['cwd'],expected['common_dir'],expected['branch'])
            if not validation['valid']:
                raise TransportError('Git worktree identity mismatch / Git worktree 身份不符: '+json.dumps(validation['reasons'],ensure_ascii=False))
        return thread

    def resume(self, member):
        self.validate(member)
        # 不覆盖 cwd、模型、权限或登录配置。
        return self.rpc('thread/resume', {'threadId': member['session_uuid'], 'excludeTurns': True})

    def create_session(self, cwd):
        if self.backend != 'managed':
            raise TransportError('New sessions require managed backend / 新建会话需要 managed 后端')
        thread = self.rpc('thread/start', {'cwd': str(Path(cwd).resolve(strict=True))})['thread']
        return thread

    def start(self, member, message):
        thread = self.validate(member)
        status = thread.get('status', {}).get('type')
        if status != 'idle':
            raise TransportError('Session is busy or unavailable / 会话繁忙或不可用: ' + str(status), retryable=True)
        result = self.rpc('turn/start', {'threadId': member['session_uuid'],
            'clientUserMessageId': message['id'],
            'input': [{'type': 'text', 'text': message['envelope']}]})
        return {'turn_id': result['turn']['id'], 'remote_status': result['turn']['status']}

    def turns(self, thread_id):
        cursor = None
        while True:
            params = {'threadId': thread_id, 'limit': 100, 'itemsView': 'summary'}
            if cursor:
                params['cursor'] = cursor
            page = self.rpc('thread/turns/list', params)
            yield from page['data']
            cursor = page.get('nextCursor')
            if not cursor:
                return

    def observe(self, member, message):
        self.validate(member)
        for turn in self.turns(member['session_uuid']):
            matched = turn['id'] == message.get('turn_id') if message.get('turn_id') else any(
                item.get('type') == 'userMessage' and (item.get('clientId') == message['id'] or any(
                    entry.get('type')=='text' and entry.get('text')==message.get('envelope')
                    for entry in item.get('content',[])))
                for item in turn.get('items', []))
            if matched:
                # 非拥有者读取尚在运行的历史时，0.162.1 会合成 interrupted，
                # 但 completedAt 仍为空。只有持久化完成时间才能证明回合已结束。
                state=turn['status']
                if state in ('completed','failed','interrupted') and turn.get('completedAt') is None:
                    state='inProgress'
                return {'turn_id': turn['id'], 'remote_status': state,
                        'reported_status':turn['status'],'completed_at':turn.get('completedAt'),
                        'result': [item.get('text', '') for item in turn.get('items', [])
                                   if item.get('type') == 'agentMessage'], 'error': turn.get('error')}
        return None  # 查不到不是“未投递”的证明。

    def wait_turn(self, thread_id, turn_id, *, timeout=120):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                obj = self.notifications.get(timeout=min(1, max(.01, deadline-time.monotonic())))
            except queue.Empty:
                continue
            params = obj.get('params', {})
            if obj.get('method') == 'turn/completed' and params.get('threadId') == thread_id and params['turn']['id'] == turn_id:
                return params['turn']
        raise DeliveryUnknown('Turn still unresolved / 回合结果仍未确定，不能据此判断失联')

    def close(self):
        if getattr(self, 'process', None):
            if getattr(self,'_job',None):self._job.close()
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
                stream.close()
            for reader in (getattr(self, '_reader', None), getattr(self, '_errors', None)):
                if reader:
                    reader.join(timeout=2)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class TransportPool:
    """每名 managed 成员独占一个 CLI 进程；只恢复本协作创建的会话。"""

    def __init__(self, *, codex_home, executable=None, timeout=15):
        self.options = dict(codex_home=codex_home, executable=executable, timeout=timeout)
        self.clients = {}

    def client(self, member):
        if member['backend'] == 'managed' and not member['owned']:
            raise TransportError('Unowned managed session / 不允许恢复来源不明的独立会话')
        key = member['id']
        if key in self.clients and self.clients[key]._dead:
            self.clients.pop(key).close()
        if key not in self.clients:
            client = (QueueTransport(cwd=member['cwd'], **self.options) if member['backend']=='queue'
                      else AppServer(backend=member['backend'], cwd=member['cwd'], **self.options))
            try:
                if member['backend']=='queue':
                    client.validate(member)
                else:
                    client.resume(member)
            except BaseException:
                client.close()
                raise
            self.clients[key] = client
        return self.clients[key]

    def start(self, member, message):
        return self.client(member).start(member, message)

    def observe(self, member, message):
        return self.client(member).observe(member, message)

    def close(self):
        for client in self.clients.values():
            client.close()
        self.clients.clear()

    def pending_requests(self):
        result=[]
        for member_id,client in self.clients.items():
            target=client.observer if isinstance(client,QueueTransport) else client
            while True:
                try:request=target.server_requests.get_nowait()
                except queue.Empty:break
                result.append({'member':member_id,'request':request})
        return result


class QueueTransport:
    """已有 CLI 的持久化队列；观察进程绝不 resume 目标会话。"""

    def __init__(self, *, codex_home=None, executable=None, cwd=None, timeout=15):
        self.command=codex_command(executable)
        self.timeout=timeout
        self.cwd=cwd
        self.environment=os.environ.copy()
        if codex_home is not None:
            self.environment['CODEX_HOME']=str(codex_home)
        self.observer=AppServer(codex_home=codex_home, executable=executable,cwd=cwd,timeout=timeout)

    @property
    def _dead(self):
        return self.observer._dead

    def validate(self,member):
        return self.observer.validate(member)

    def start(self,member,message):
        self.validate(member)
        # 即使直接调用本 API，也只传 ASCII JSON，避免 Windows CLI 代码页破坏正文。
        envelope=json.dumps(json.loads(message['envelope']),ensure_ascii=True)
        if envelope!=message['envelope']:
            raise TransportError('Envelope must be canonical ASCII JSON / 消息必须是规范 ASCII JSON')
        argv=self.command+['queue','--thread',member['session_uuid'],'--message',envelope]
        if os.name=='nt' and len(subprocess.list2cmdline(argv))>30000:
            raise TransportError('Queue message exceeds Windows command-line limit / 消息超过 Windows 命令行长度限制，请使用 managed 后端')
        try:
            result=subprocess.run(argv,
                                  cwd=self.cwd,env=self.environment,capture_output=True,timeout=self.timeout,
                                  creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        except subprocess.TimeoutExpired as exc:
            raise DeliveryUnknown('Queue result unknown / 队列请求超时，结果不明') from exc
        except OSError as exc:
            raise TransportError(str(exc),retryable=True) from exc
        stdout=result.stdout.decode('utf-8',errors='replace')
        stderr=result.stderr.decode('utf-8',errors='replace')
        if result.returncode!=0:
            # queue 退出失败也可能发生在持久化之后，不推断“可以安全重发”。
            raise DeliveryUnknown('Queue exit outcome unknown / 队列命令失败，投递结果不明: '+stderr)
        match=re.search(r'Queued message ([0-9a-f-]{36}) for thread ([0-9a-f-]{36})',stdout)
        if not match or match.group(2)!=member['session_uuid']:
            raise DeliveryUnknown('Unrecognized queue acknowledgement / 无法确认队列回执: '+stdout)
        return {'turn_id':None,'remote_status':'queued','queue_id':match.group(1)}

    def observe(self,member,message):
        return self.observer.observe(member,message)

    def close(self):
        self.observer.close()
