"""Codex 官方 app-server 的标准库客户端；不读写 Codex 内部数据库。"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import shlex
import stat
import subprocess
import threading
import time
import uuid
from urllib.parse import urlsplit, unquote

try:
    from .pair_producer import strict_version, strict_display, prepare_launch, attach_process
except ImportError:
    from pair_producer import strict_version, strict_display, prepare_launch, attach_process


class TransportError(RuntimeError):
    """确定尚未投递的错误；只有 retryable=True 时允许有限重试。"""

    def __init__(self, message, *, retryable=False):
        super().__init__(redact_diagnostic(str(message)))
        self.retryable = retryable


class DeliveryUnknown(TransportError):
    """请求可能已经到达，必须对账，禁止自动重发。"""


class SessionBusy(TransportError):
    """预检确认仍有回合在执行，尚未提交本消息；等待空闲不消耗投递重试。"""


class RpcError(TransportError):
    def __init__(self, error):
        self.error = redact_diagnostic(error)
        super().__init__(json.dumps(self.error, ensure_ascii=False))


def redact_diagnostic(value):
    """诊断中的 API 令牌和认证字段不写入证据或共享状态。"""
    if isinstance(value,str):
        value=re.sub(r'\bsk-[A-Za-z0-9_-]+','[REDACTED]',value)
        value=re.sub(r'(?i)(\bBearer\s+)[A-Za-z0-9._~+/=\-]+',r'\1[REDACTED]',value)
        return re.sub(r'''(?i)((?:access_token|refresh_token|api_key|authorization|password|secret)["']?\s*[:=]\s*["']?)[^\s,"';}]+''',r'\1[REDACTED]',value)
    if isinstance(value,dict):
        sensitive={'api_key','apikey','authorization','access_token','refresh_token','bearer_token',
                   'experimental_bearer_token','password','secret'}
        return {key:('[REDACTED]' if str(key).lower() in sensitive else redact_diagnostic(item)) for key,item in value.items()}
    if isinstance(value,list):return [redact_diagnostic(item) for item in value]
    return value


def canonical_path(path):
    return os.path.normcase(str(Path(path).expanduser().resolve(strict=True)))


def same_review_directory(left,right):
    """仅比较已有真实目录；拒绝相对路径、上级转义和链接重定向。"""
    try:
        paths=(Path(left),Path(right))
        for path in paths:
            if not path.is_absolute() or '..' in path.parts or not path.is_dir():return False
            for entry in (path,*path.parents):
                if (entry.is_symlink() or getattr(entry.lstat(),'st_file_attributes',0)
                        & getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0)):
                    return False
        return canonical_path(paths[0])==canonical_path(paths[1]) and paths[0].samefile(paths[1])
    except (OSError,ValueError,TypeError,RuntimeError):return False


def receipt_cwd(value):
    """只接受本地绝对目录或无主机/查询的file URI，不猜测远程及相对路径。"""
    if not isinstance(value,str) or not value:return None
    if value.startswith('file:'):
        uri=urlsplit(value)
        if uri.scheme!='file' or uri.netloc or uri.query or uri.fragment:return None
        value=unquote(uri.path)
        if os.name=='nt' and re.match(r'^/[A-Za-z]:/',value):value=value[1:]
    try:
        path=Path(value)
        return canonical_path(path) if path.is_absolute() and path.is_dir() else None
    except (OSError,ValueError,RuntimeError):return None


def command_receipt(item):
    """白名单适配API与持久事件；不使用输出或模型文字推导执行结果。"""
    if not isinstance(item,dict) or item.get('type') not in ('commandExecution','CommandExecution'):return None
    code=item.get('exitCode',item.get('exit_code'))
    if type(code) is not int or ('exitCode' in item and 'exit_code' in item and
            (type(item['exit_code']) is not int or item['exit_code']!=code)):return None
    status=item.get('status')
    if status not in ('completed','failed') or status=='failed' and code==0:return None
    command=item.get('command')
    argv=item.get('command_argv')
    if argv is not None and (not isinstance(argv,list) or not argv or any(not isinstance(x,str) or '\0' in x for x in argv) or not argv[0]):return None
    if isinstance(command,list):
        if not command or any(not isinstance(part,str) or '\0' in part for part in command) or not command[0]:return None
        if argv is not None and argv!=command:return None
        argv=list(command)
        command=subprocess.list2cmdline(command) if os.name=='nt' else (command[0] if len(command)==1 else None)
    if argv is not None and isinstance(item.get('command'),str) and item['command']!=subprocess.list2cmdline(argv):return None
    cwd=receipt_cwd(item.get('cwd'))
    if not isinstance(command,str) or not command or not cwd or not isinstance(item.get('id'),str) or not item['id']:return None
    result={'id':item['id'],'type':'commandExecution','status':status,'exitCode':code,'command':command,'cwd':cwd}
    if argv is not None:result['command_argv']=argv
    if 'source' in item:
        if not isinstance(item['source'],str):return None
        result['source']={'unifiedExecStartup':'unified_exec_startup'}.get(item['source'],item['source'])
    return result


def command_matches(request,item,*,producer_version=None):
    if not isinstance(request,str) or not request or '\0' in request:return False
    if strict_version(producer_version):
        return (item.get('source')=='unified_exec_startup' and item.get('command_argv') is not None
                and strict_display(item['command_argv'])==request)
    if producer_version not in (None,'0.163.0'):return False
    if request==item['command']:return True
    argv=item.get('command_argv')
    # 官方0.163.0审批采用shlex_join；未知格式不猜测，脚本只逐字比较。
    if not argv or producer_version!='0.163.0':return False
    try:return shlex.split(request,posix=True)==argv
    except ValueError:return False


def merge_command_receipts(left,right,*,producer_version=None):
    """不同展示仅凭已核实版本的原argv合并；矛盾项不能被后续成功覆盖。"""
    if strict_version(producer_version):
        if any(left.get(k)!=right.get(k) for k in ('id','type','status','exitCode','cwd','source')):return None
        if left.get('source')!='unified_exec_startup':return None
        if left==right:return left
        raw,display=(left,right) if left.get('command_argv') is not None else (right,left)
        argv=raw.get('command_argv')
        if argv is None or display.get('command_argv') is not None:return None
        return raw if strict_display(argv)==display['command'] else None
    if producer_version not in (None,'0.163.0'):return None
    if left==right:return left
    if any(left[k]!=right[k] for k in ('id','type','status','exitCode','cwd')):return None
    if producer_version!='0.163.0':return None
    raw,display=(left,right) if left.get('command_argv') else (right,left)
    argv=raw.get('command_argv')
    if not argv or display.get('command_argv') or any('\0' in arg for arg in argv) or '\0' in display['command']:return None
    try:
        if shlex.split(display['command'],posix=True)==argv:return raw
    except ValueError:pass
    return None


def owned_rollout_path(member,thread):
    """沿用官方会话目录的实体归属检查，额外拒绝home外sessions重定向。"""
    home=Path(member['codex_home']).resolve(strict=True)
    sessions=(home/'sessions').resolve(strict=True)
    original=Path(thread['path'])
    if not original.is_absolute() or original.is_symlink():raise ValueError('Ambiguous rollout path')
    path=original.resolve(strict=True)
    if not sessions.is_relative_to(home) or path.suffix!='.jsonl' or not (path.is_relative_to(sessions)
            or any(parent.samefile(sessions) for parent in path.parents)):
        raise ValueError('Rollout ownership mismatch')
    return path


def rollout_command_items(member,thread,turn_id,*,producer_version=None,require_terminal=True,terminal_turn=None):
    """有界读取精确官方工具事件；仅显式选项允许回合进行中的工具终态。"""
    try:
        if thread.get('id')!=member['session_uuid'] or receipt_cwd(thread.get('cwd'))!=receipt_cwd(member['cwd']):return None
        if terminal_turn is not None and (not require_terminal or not isinstance(terminal_turn,dict)
                or terminal_turn.get('id')!=turn_id or terminal_turn.get('threadId')!=member['session_uuid']):return None
        path=owned_rollout_path(member,thread);size=path.stat().st_size
        if size>32*1024*1024:return None
        with path.open('rb') as stream:data=stream.read(size)
        if len(data)!=size or not data.endswith(b'\n'):return None
        meta=None;started=False;terminal=False;items={};conflicts=set()
        def unique_object(pairs):
            result={}
            for key,value in pairs:
                if key in result:raise ValueError('Duplicate rollout JSON key')
                result[key]=value
            return result
        for line in data.splitlines():
            # 仅compacted可使用较大行预算；普通工具事件仍限制为1 MiB。
            if len(line)>4*1024*1024:return None
            record=json.loads(line,object_pairs_hook=unique_object)
            if not isinstance(record,dict):return None
            if len(line)>1024*1024 and record.get('type')!='compacted':return None
            payload=record.get('payload')
            if not isinstance(payload,dict):return None
            if record.get('type')=='session_meta':
                if meta is not None and meta!=payload:return None
                meta=payload
            if (not require_terminal and started and not terminal and record.get('type')=='event_msg'
                    and payload.get('type')=='task_started' and payload.get('turn_id')!=turn_id):return None
            if record.get('type')!='event_msg' or payload.get('turn_id')!=turn_id:continue
            kind=payload.get('type')
            if kind=='task_started':
                if terminal:return None
                started=True
            stamp=payload.get('completed_at')
            if kind=='turn_aborted' and terminal_turn is not None:
                # 仅公开对账传入的官方终态与持久日志完全一致时接受中断；不把工具或业务判为成功。
                api_stamp=terminal_turn.get('completedAt')
                if (not started or terminal or payload.get('reason')!='interrupted'
                        or terminal_turn.get('status')!='interrupted'
                        or type(stamp) not in (int,float) or type(api_stamp) not in (int,float)
                        or not math.isfinite(stamp) or not math.isfinite(api_stamp)
                        or stamp<=0 or api_stamp!=stamp):return None
                terminal=True
            if kind=='task_complete' and terminal_turn is not None:
                if terminal or terminal_turn.get('status')=='interrupted':return None
            if kind=='task_complete' and started and type(stamp) in (str,int,float) and stamp:terminal=True
            if kind!='item_completed':continue
            if not started or terminal:return None
            if payload.get('thread_id')!=member['session_uuid']:return None
            item=payload.get('item');key=item.get('id') if isinstance(item,dict) else None
            if not isinstance(key,str):return None
            normalized=command_receipt(item)
            merged=(merge_command_receipts(items[key],normalized,producer_version=producer_version)
                    if normalized and key in items else normalized)
            if not merged:conflicts.add(key)
            else:items[key]=merged
        if not meta or meta.get('id')!=member['session_uuid'] or receipt_cwd(meta.get('cwd'))!=receipt_cwd(member['cwd']):return None
        if not started or require_terminal and not terminal:return None
        return [item for key,item in items.items() if key not in conflicts]+[{'id':key,'type':'conflictingReceipt'} for key in conflicts]
    except (OSError,KeyError,TypeError,ValueError,RuntimeError,OverflowError):return None


def session_uuid(value):
    try:
        parsed = str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise TransportError("Invalid session UUID / 无效会话 UUID") from exc
    if parsed != value.lower():
        raise TransportError("Use a full session UUID / 必须使用完整会话 UUID")
    return parsed


def provider_identity(proof):
    """只使用 thread/read 的提供方，不从模型名、账号目录或登录文件猜测。"""
    provider=proof.get('modelProvider')
    supported=provider in ('openai','deepseek')
    model=proof.get('model')
    if not isinstance(model,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}',model) or model.startswith('sk-'):
        model=None
    return {'provider':provider if supported else None,'model':model,
            'metadata_status':'verified' if supported else ('missing' if provider is None else 'unsupported'),
            'source':'binding_snapshot'}


def binding_proof(thread, *, require_provider=False):
    """持久化白名单，拒绝把线程预览、认证、任意 extra 或历史回合写入绑定。"""
    identity=provider_identity(thread)
    if identity['metadata_status']=='unsupported':
        raise TransportError('Unsupported model provider / 当前仅支持 OpenAI 与 DeepSeek 提供方')
    if require_provider and identity['metadata_status']=='missing':
        raise TransportError('Provider metadata missing / 无法从当前会话确认提供方，不猜测兼容性')
    proof={'id':session_uuid(thread['id']),'cwd':canonical_path(thread['cwd']),
           'ephemeral':bool(thread.get('ephemeral'))}
    if identity['provider'] is not None:proof['modelProvider']=identity['provider']
    if identity['model'] is not None:proof['model']=identity['model']
    account=thread.get('accountFingerprint')
    if isinstance(account,str) and re.fullmatch(r'[0-9a-f]{64}',account):proof['accountFingerprint']=account
    version=thread.get('cliVersion')
    if isinstance(version,str) and re.fullmatch(r'[0-9][A-Za-z0-9.+\-]{0,63}',version):
        proof['cliVersion']=version
    git=thread.get('git')
    if isinstance(git,dict):
        proof['git']={key:git[key] for key in ('path','common_dir','branch') if isinstance(git.get(key),str)}
        if isinstance(git.get('valid'),bool):proof['git']['valid']=git['valid']
    return proof


def control_environment(codex_home=None):
    """旁路控制只读元数据或投递队列，不继承发起者的推理凭据和会话身份。"""
    environment=os.environ.copy()
    for key in list(environment):
        if key.upper() in ('OPENAI_BASE_URL','CODEX_THREAD_ID') or key.upper().endswith(('_API_KEY','_AUTH_TOKEN','_ACCESS_TOKEN','_BEARER_TOKEN')):
            environment.pop(key)
    if codex_home is not None:environment['CODEX_HOME']=str(codex_home)
    return environment


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
                 cwd=None, timeout=15, evidence=None, process_factory=subprocess.Popen, metadata_only=False, account_metadata=False, hook_metadata=False, track_producer=False,
                 config_overrides=None, experimental_api=False, runtime_metadata=False):
        if backend not in ('managed', 'shared'):
            raise TransportError('Invalid backend / 无效传输后端')
        self.backend, self.timeout = backend, timeout
        self.metadata_only=metadata_only
        self.account_metadata=account_metadata
        self.hook_metadata=hook_metadata
        if runtime_metadata and (backend!='shared' or not metadata_only):raise TransportError('Original runtime metadata requires shared read-only connection')
        self.runtime_metadata=runtime_metadata
        self.signal_events=queue.Queue(maxsize=2048)
        self._next_id = 0
        self._pending = {}
        self._condition = threading.Condition()
        self._write_lock = threading.Lock()
        self._dead = False
        self.notifications = queue.Queue(maxsize=2048)
        self.progress_events = queue.Queue(maxsize=2048)
        self.server_requests = queue.Queue()
        self._evidence = evidence
        executable_argv = codex_command(executable)
        if track_producer and backend!='managed':raise TransportError('Producer tracking requires owned process / 仅跟踪本控制器拥有的进程')
        launch = prepare_launch(executable_argv) if track_producer else None
        argv = executable_argv + ['app-server']
        if config_overrides:
            if backend != 'managed' or not isinstance(config_overrides, dict):
                raise TransportError('Owned config override required / 仅独立受管进程可覆盖其配置')
            for key, value in config_overrides.items():
                if (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*', key)
                        or type(value) not in (bool, int, str)):
                    raise TransportError('Invalid config override / 只接受明确配置键及布尔、整数、字符串值')
                argv += ['-c', key + '=' + json.dumps(value, ensure_ascii=False)]
        argv += ['proxy'] if backend == 'shared' else ['--listen', 'stdio://']
        env = control_environment(codex_home) if metadata_only else os.environ.copy()
        if codex_home is not None:
            env['CODEX_HOME'] = str(codex_home)
        self._stderr = []
        self._diagnostic_stage='process_start'
        self._last_success_rpc=None
        try:
            self.process = process_factory(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, cwd=cwd, env=env,
                                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        except OSError as exc:
            self._stderr.append(redact_diagnostic(str(exc))[:512])
            raise TransportError('App-server startup failed / 辅助进程启动失败: '+self._disconnect_diagnostic(), retryable=True) from exc
        try:
            self._job=ProcessJob(self.process)
            self.producer_launch=attach_process(launch,self.process) if launch else None
        except BaseException:
            self.process.kill();self.process.wait(timeout=5)
            for stream in (self.process.stdin,self.process.stdout,self.process.stderr):stream.close()
            raise
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._errors = threading.Thread(target=self._read_errors, daemon=True)
        self._reader.start()
        self._errors.start()
        try:
            initialize = {'clientInfo': {'name': 'codex_pair', 'title': 'codex-pair', 'version': '0.1.0'}}
            if experimental_api:
                initialize['capabilities'] = {'experimentalApi': True}
            self.identity = self.rpc('initialize', initialize)
            self.notify('initialized', {})
            if codex_home and canonical_path(self.identity['codexHome']) != canonical_path(codex_home):
                raise TransportError('CODEX_HOME mismatch / app-server 使用了不同的 Codex 目录')
        except BaseException:
            self.close()
            raise

    def _record(self, direction, value):
        if self._evidence:
            self._evidence({'time': time.time(), 'direction': direction, 'value': redact_diagnostic(value)})

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
                        if obj.get('method')=='item/completed':
                            params=obj.get('params',{});item=params.get('item',{})
                            if item.get('type') in ('commandExecution','fileChange'):
                                try:self.progress_events.put_nowait({'thread':params.get('threadId'),'turn':params.get('turnId'),'item':item.get('id'),'type':item.get('type'),'status':item.get('status')})
                                except queue.Full:pass
                        if obj.get('method') in ('account/rateLimits/updated','account/updated','thread/tokenUsage/updated'):
                            try:self.signal_events.put_nowait(obj)
                            except queue.Full:pass
                        try:
                            self.notifications.put_nowait(obj)
                        except queue.Full:
                            try:self.notifications.get_nowait()
                            except queue.Empty:pass
                            self.notifications.put_nowait(obj)
        except (OSError, ValueError) as exc:
            self._stderr.append(str(exc))
            self._diagnostic_stage='read_parse_error'
        finally:
            if getattr(self,'_diagnostic_stage',None)!='read_parse_error':self._diagnostic_stage='stdout_eof'
            with self._condition:
                self._dead = True
                self._condition.notify_all()

    def _read_errors(self):
        for line in self.process.stderr:
            self._stderr.append(redact_diagnostic(line.decode('utf-8', errors='replace').rstrip())[:512])
            self._stderr[:] = self._stderr[-16:]

    def _disconnect_diagnostic(self):
        process=getattr(self,'process',None)
        try:code=process.poll() if process else None
        except (OSError,AttributeError):code=None
        return json.dumps({'stage':getattr(self,'_diagnostic_stage','unknown'),
            'exit_code':code if type(code) is int else None,'last_success_rpc':getattr(self,'_last_success_rpc',None),
            'stderr':redact_diagnostic('\n'.join(getattr(self,'_stderr',[])[-4:]))[:2048]},ensure_ascii=True)

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
        allowed=('initialize','thread/read','thread/turns/list')
        if getattr(self,'account_metadata',False):allowed+=('account/read','account/rateLimits/read')
        if getattr(self,'hook_metadata',False):allowed+=('hooks/list',)
        if getattr(self,'runtime_metadata',False):allowed+=('thread/backgroundTerminals/list',)
        if getattr(self,'metadata_only',False) and method not in allowed:
            grant=getattr(self,'_quota_interrupt',None)
            if method!='turn/interrupt' or grant is None or params!=grant:
                raise TransportError('Metadata-only observer / 只读观察进程拒绝非元数据 RPC')
            with self._condition:
                if self._quota_interrupt!=grant:
                    raise TransportError('Quota interrupt consumed / 准确中断凭据已消费')
                self._quota_interrupt=None
        with self._condition:
            if self._dead:
                raise TransportError('App-server disconnected / app-server 已断开: ' + self._disconnect_diagnostic(), retryable=True)
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
                    raise DeliveryUnknown('RPC result unknown / RPC 结果不明: ' + method + ' ' + self._disconnect_diagnostic())
                self._condition.wait(remaining)
            obj = self._pending.pop(request_id)
        if 'error' in obj:
            raise RpcError(obj['error'])
        self._last_success_rpc=method
        return obj['result']

    def read_thread(self, thread_id):
        return self.rpc('thread/read', {'threadId': session_uuid(thread_id)})['thread']

    def interrupt_for_quota(self,member):
        """仅原账号已核验的唯一活动回合可获一次中断，不开放通用写RPC。"""
        member=dict(member)
        try:
            proof=member.get('proof')
            proof=json.loads(proof) if isinstance(proof,str) else dict(proof or {})
            member['proof']=proof
            valid=(getattr(self,'metadata_only',False) and getattr(self,'account_metadata',False)
                and member.get('backend')=='queue' and not member.get('owned')
                and type(member.get('binding_version')) is int and member['binding_version']>=1
                and proof.get('id')==session_uuid(member['session_uuid'])
                and canonical_path(proof['cwd'])==canonical_path(member['cwd'])
                and proof.get('modelProvider') in ('openai','deepseek') and bool(proof.get('model'))
                and bool(proof.get('accountFingerprint'))
                and canonical_path(self.identity['codexHome'])==canonical_path(member['codex_home']))
        except (KeyError,TypeError,ValueError,OSError) as exc:
            raise TransportError('Quota binding unverified / 原账号或会话绑定未核验') from exc
        if not valid:raise TransportError('Quota binding unverified / 原账号或会话绑定未核验')
        thread=self.validate(member)
        if thread.get('status',{}).get('type')=='idle':return {'state':'idle'}
        if thread.get('status',{}).get('type')!='active':
            raise TransportError('Session unavailable / 原CLI活动状态未核实，不恢复会话')
        turns=[t for t in self.turns(member['session_uuid']) if t.get('status')=='inProgress']
        if len(turns)!=1 or not isinstance(turns[0].get('id'),str) or not turns[0]['id']:
            raise TransportError('Active turn unresolved / 无法唯一确认原活动回合，不猜测中断')
        turn=turns[0]['id'];params={'threadId':member['session_uuid'],'turnId':turn}
        # 同一原回合不因本地绑定版本改变而再次发送未知中断。
        key=(member['session_uuid'],turn)
        with self._condition:
            attempts=getattr(self,'_quota_interrupt_attempts',set())
            if key in attempts or getattr(self,'_quota_interrupt',None) is not None:
                raise TransportError('Quota interrupt already attempted / 原准确回合已请求中断，不重试')
            attempts.add(key);self._quota_interrupt_attempts=attempts
            self._quota_interrupt=params
        try:
            self.rpc('turn/interrupt',params)
        finally:
            with self._condition:self._quota_interrupt=None
        return {'state':'interrupt_requested','turn_id':turn}

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
        actual=binding_proof(thread)
        if proof and proof.get('accountFingerprint') and getattr(self,'account_metadata',False):
            try:from .pair_balance import pool_key
            except ImportError:from pair_balance import pool_key
            identity=self.rpc('account/read',{'refreshToken':False})
            if pool_key(identity,canonical_path(member['codex_home']))!=proof['accountFingerprint']:
                raise TransportError('Account changed / 原会话实际账号已变化，先暂停并重新核验')
        if proof and proof.get('model') and proof['model']!=actual.get('model'):
                raise TransportError('Model changed / 会话模型已变化，必须暂停并重新核验')
        if proof and proof.get('modelProvider') and proof['modelProvider']!=actual.get('modelProvider'):
            raise TransportError('Model provider changed / 会话提供方与绑定记录不符，不静默换用配置')
        if proof and proof.get('git'):
            try:from .pair_git import validate_worktree
            except ImportError:from pair_git import validate_worktree
            expected=proof['git']
            validation=validate_worktree(expected.get('path') or member['cwd'],expected['common_dir'],expected['branch'])
            if not validation['valid']:
                raise TransportError('Git worktree identity mismatch / Git worktree 身份不符: '+json.dumps(validation['reasons'],ensure_ascii=False))
        return thread

    def resume(self, member):
        if getattr(self,'metadata_only',False):
            raise TransportError('Metadata-only observer / 只读观察进程不能恢复或启动模型回合')
        self.validate(member)
        # 不覆盖 cwd、模型、权限或登录配置。
        return self.rpc('thread/resume', {'threadId': member['session_uuid'], 'excludeTurns': True})

    def create_session(self, cwd):
        if getattr(self,'metadata_only',False):
            raise TransportError('Metadata-only observer / 只读观察进程不能创建模型会话')
        if self.backend != 'managed':
            raise TransportError('New sessions require managed backend / 新建会话需要 managed 后端')
        thread = self.rpc('thread/start', {'cwd': str(Path(cwd).resolve(strict=True))})['thread']
        return thread

    def start(self, member, message):
        if getattr(self,'metadata_only',False):
            raise TransportError('Metadata-only observer / 只读观察进程不能启动模型回合')
        thread = self.validate(member)
        status = thread.get('status', {}).get('type')
        if status != 'idle':
            if status=='active':
                raise SessionBusy('Session is busy / 会话仍有回合执行中')
            raise TransportError('Session is unavailable / 会话不可用: ' + str(status), retryable=True)
        result = self.rpc('turn/start', {'threadId': member['session_uuid'],
            'clientUserMessageId': message['id'],
            'input': [{'type': 'text', 'text': message['envelope']}]})
        return {'turn_id': result['turn']['id'], 'remote_status': result['turn']['status']}

    def turns(self, thread_id, *, items_view='summary'):
        if items_view not in ('summary','full'):raise TransportError('Unsupported item view')
        cursor = None
        seen=set()
        while True:
            params = {'threadId': thread_id, 'limit': 100, 'itemsView': items_view}
            if cursor:
                params['cursor'] = cursor
            page = self.rpc('thread/turns/list', params)
            yield from page['data']
            cursor = page.get('nextCursor')
            if not cursor:
                return
            if cursor in seen:raise TransportError('Repeated turn cursor / 回合分页游标循环')
            seen.add(cursor)

    def rollout_completion(self, member, thread, message, turn_id, *, workspace_only=False, api_turns=()):
        """只认本账号、同一会话的持久化回合事件，增量读取以免阻塞消息桥。"""
        if not getattr(self,'metadata_only',False) or not isinstance(thread,dict):return None
        try:
            path=owned_rollout_path(member,thread)
            # Windows 官方返回的路径可能带 \\?\ 前缀，按目录实体核对归属。
            stat=path.stat();identity=(stat.st_dev,stat.st_ino)
            cache=getattr(self,'_rollouts',{})
            self._rollouts=cache
            key=str(path)
            state=cache.get(key)
            if state is None or state['identity']!=identity or stat.st_size<state['offset']:
                state={'identity':identity,'offset':0,'meta':None,'started':set(),'completed':{},'latest':None,'valid':True}
                cache[key]=state
            with path.open('rb') as file:
                file.seek(state['offset'])
                while file.tell()<stat.st_size:
                    line=file.readline()
                    if not line or not line.endswith(b'\n'):return None
                    state['offset']=file.tell()
                    try:record=json.loads(line)
                    except (ValueError,UnicodeError):
                        state['valid']=False;continue
                    if not isinstance(record,dict) or not isinstance(record.get('payload'),dict):
                        state['valid']=False;continue
                    payload=record['payload']
                    if record.get('type')=='session_meta':state['meta']=payload
                    if record.get('type')!='event_msg':continue
                    event=payload.get('type');current=payload.get('turn_id')
                    if not isinstance(current,str):continue
                    if event=='task_started':
                        state['started'].add(current);state['latest']=current
                        state['completed'].pop(current,None)
                    elif event=='task_complete' and current in state['started'] and payload.get('completed_at') is not None:
                        state['completed'][current]=payload['completed_at']
            meta=state['meta']
            if not state['valid'] or not isinstance(meta,dict) or meta.get('id')!=member['session_uuid']:return None
            if canonical_path(meta['cwd'])!=canonical_path(member['cwd']):return None
            latest=state['latest']
            if workspace_only:
                if latest not in state['completed']:return True
                for turn in api_turns:
                    if self._turn_state(turn) in ('completed','failed','interrupted'):continue
                    current=turn['id']
                    if current in state['completed']:continue
                    # 同会话后续回合的落盘完成可消解已开始的旧回合，不能消解尚未入日志的新回合。
                    if turn['status']!='queued' and current in state['started'] and current!=latest:continue
                    return True
                return False
            # 新回合尚在执行时继续保留共享工作区的阻塞，不被旧完成事件放行。
            if latest not in state['completed']:return None
            if turn_id in state['completed']:
                return {'remote_status':'completed','completed_at':state['completed'][turn_id],
                    'completion_source':'rollout_task_complete','evidence_turn_id':turn_id}
            if turn_id in state['started'] and turn_id!=latest and (message.get('process_status')=='completed' or message.get('protocol_yield') or latest in message.get('protocol_yield_turns',())):
                return {'remote_status':'interrupted','completed_at':None,
                    'completion_source':'rollout_superseded','evidence_turn_id':latest}
        except (OSError,KeyError,TypeError,ValueError):return None
        return None

    def observe(self, member, message):
        thread = self.validate(member)
        turns = list(self.turns(member['session_uuid']))
        for turn in turns:
            matched = turn['id'] == message.get('turn_id') if message.get('turn_id') else any(
                item.get('type') == 'userMessage' and (item.get('clientId') == message['id'] or any(
                    entry.get('type')=='text' and entry.get('text')==message.get('envelope')
                    for entry in item.get('content',[])))
                for item in turn.get('items', []))
            if matched:
                # 非拥有者读取尚在运行的历史时，0.162.1 会合成 interrupted，
                # 但 completedAt 仍为空。只有持久化完成时间才能证明回合已结束。
                state=self._turn_state(turn)
                receipt={'turn_id': turn['id'], 'remote_status': state,
                        'reported_status':turn['status'],'completed_at':turn.get('completedAt'),
                        'result': [item.get('text', '') for item in turn.get('items', [])
                                   if item.get('type') == 'agentMessage'], 'error': redact_diagnostic(turn.get('error'))}
                if state=='inProgress' or state=='interrupted' and message.get('process_status')=='completed':
                    proof=self.rollout_completion(member,thread,message,turn['id'])
                    if proof:receipt.update(proof)
                if getattr(self,'metadata_only',False):
                    receipt['workspace_busy']=self.rollout_completion(
                        member,thread,message,turn['id'],workspace_only=True,api_turns=turns) is not False
                return receipt
        return None  # 查不到不是“未投递”的证明。

    @staticmethod
    def _turn_state(turn):
        state=turn['status']
        if state in ('completed','failed','interrupted') and turn.get('completedAt') is None:
            return 'inProgress'
        return state

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

    def __init__(self, *, codex_home=None, executable=None, timeout=15):
        self.options = dict(executable=executable, timeout=timeout)
        self.clients = {}

    def client(self, member):
        if member['backend'] == 'managed' and not member['owned']:
            raise TransportError('Unowned managed session / 不允许恢复来源不明的独立会话')
        if not member.get('codex_home'):
            raise TransportError('Member account missing / 成员缺少账号目录，拒绝使用默认账号')
        account=canonical_path(member['codex_home'])
        key = (member['id'],account,member['backend'],member['session_uuid'],canonical_path(member['cwd']))
        if key in self.clients and self.clients[key]._dead:
            self.clients.pop(key).close()
        if key not in self.clients:
            client = (QueueTransport(codex_home=account,cwd=member['cwd'], **self.options) if member['backend']=='queue'
                      else AppServer(codex_home=account,backend=member['backend'], cwd=member['cwd'], **self.options))
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
        try:
            client = self.client(member)
        except DeliveryUnknown as exc:
            if member['backend'] != 'queue': raise
            # queue客户端的创建/身份核验尚未提交消息；不把这个异常扩散为发送不明。
            raise TransportError('Queue preflight not submitted / 队列发送前核验失败: ' + str(exc), retryable=True) from exc
        return client.start(member, message)

    def observe(self, member, message):
        return self.client(member).observe(member, message)

    def stop_for_quota(self, member):
        if member.get('backend')!='queue':
            raise TransportError('Quota stop unsupported / 不接续托管模型会话')
        client=self.client(member)
        if not isinstance(client,QueueTransport):
            raise TransportError('Quota stop unsupported / 仅处理已绑定原CLI，不恢复模型')
        return client.observer.interrupt_for_quota(member)

    def close(self):
        for client in self.clients.values():
            client.close()
        self.clients.clear()

    def pending_requests(self):
        result=[]
        for key,client in self.clients.items():
            target=client.observer if isinstance(client,QueueTransport) else client
            while True:
                try:request=target.server_requests.get_nowait()
                except queue.Empty:break
                result.append({'member':key[0],'request':request})
        return result

    def signals(self):
        events=[]
        for key,client in self.clients.items():
            target=client.observer if isinstance(client,QueueTransport) else client
            while True:
                try:events.append((key[0],target.signal_events.get_nowait()))
                except queue.Empty:break
        return events


class QueueTransport:
    """已有 CLI 的持久化队列；观察进程绝不 resume 目标会话。"""

    def __init__(self, *, codex_home=None, executable=None, cwd=None, timeout=15):
        self.command=codex_command(executable)
        self.timeout=timeout
        self.cwd=cwd
        self.environment=control_environment(codex_home)
        self.observer=AppServer(codex_home=codex_home, executable=executable,cwd=cwd,timeout=timeout,metadata_only=True,account_metadata=True)

    @property
    def _dead(self):
        return self.observer._dead

    def validate(self,member):
        return self.observer.validate(member)

    def start(self,member,message):
        try:
            self.validate(member)
        except DeliveryUnknown as exc:
            raise TransportError('Queue preflight not submitted / 队列发送前核验失败: ' + str(exc), retryable=True) from exc
        # 即使直接调用本 API，也只传 ASCII JSON，避免 Windows CLI 代码页破坏正文。
        envelope=json.dumps(json.loads(message['envelope']),ensure_ascii=True)
        if envelope!=message['envelope']:
            raise TransportError('Envelope must be canonical ASCII JSON / 消息必须是规范 ASCII JSON')
        argv=self.command+['queue','--thread',member['session_uuid'],'--message',envelope]
        if os.name=='nt' and len(subprocess.list2cmdline(argv))>30000:
            raise TransportError('Queue message exceeds Windows command-line limit / 消息超过 Windows 命令行长度限制；请更新技能并用 recover-delivery 恢复原消息，不另建会话')
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
