"""审核控制器的原生子进程证据；不是会话创建版本或旁路进程版本。"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess

ALPHA = '0.163.0-alpha.11'
# 精确版本候选；新增构建仍须独立实录审核后才能部署。
STRICT_VERSIONS = (ALPHA, '0.166.0-alpha.5.1', '0.166.0-alpha.6', '0.167.0-alpha.3.2')
SUPPORTED_VERSIONS = ('0.163.0', *STRICT_VERSIONS)
KEY = 'reviewer_producer:'


@dataclass(frozen=True)
class VerifiedProducer:
    version: str
    binding: dict
    evidence_digest: str


def strict_version(value):
    return isinstance(value, VerifiedProducer) and value.version in STRICT_VERSIONS


def rust_shlex_join(argv):
    """按 shlex 1.3.0 Quoter 默认策略单向编码；授权见 references/shlex-LICENSE-MIT.txt。"""
    if not isinstance(argv, list) or not argv or any(not isinstance(s, str) or '\0' in s for s in argv):
        return None
    safe = set('+-./:@]_0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz')
    words = []
    for word in argv:
        if not word:
            words.append("''")
            continue
        # surrogate 不是官方 UTF-8 字符串。
        try: word.encode('utf-8')
        except UnicodeError: return None
        chunks = []
        start = 0
        while start < len(word):
            flags, end = (2, start + 1) if word[start] == '^' else (7, start)
            while end < len(word):
                c = word[end]
                allowed = flags
                if c not in safe: allowed &= ~1
                if c in "'\\^": allowed &= ~2
                if c in '`$!^': allowed &= ~4
                if not allowed: break
                flags, end = allowed, end + 1
            chunk = word[start:end]
            if flags & 1: chunks.append(chunk)
            elif flags & 2: chunks.append("'" + chunk + "'")
            else: chunks.append('"' + ''.join('\\' + c if c in '$`"\\' else c for c in chunk) + '"')
            start = end
        words.append(''.join(chunks))
    return ' '.join(words)


def strict_display(argv):
    rendered = rust_shlex_join(argv)
    # 不对原 argv 脱敏后比较：脱敏是多对一映射。标记本身也不接受。
    if rendered is None or re.search(r'(?i)\[redacted(?:_secret)?\]|<redacted>|<command included NUL byte>', rendered):
        return None
    return rendered


def file_identity(filename):
    path = Path(filename)
    if not path.is_absolute() or not path.is_file(): raise ValueError('Native producer executable required')
    for entry in (path, *path.parents):
        info = entry.lstat()
        if entry.is_symlink() or getattr(info, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
            raise ValueError('Redirected producer executable')
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        after = os.fstat(stream.fileno())
    def identity(s): return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    if identity(before) != identity(after) or identity(after) != identity(path.stat()):
        raise ValueError('Producer executable changed during hash')
    return dict(path=os.path.normcase(str(path.resolve())), device=after.st_dev, inode=after.st_ino,
                size=after.st_size, mtime_ns=after.st_mtime_ns, sha256=digest)


def process_identity(pid):
    # shortcut: alpha 证据仅支持 Windows 原生进程；其他平台需要独立进程身份适配。
    if os.name != 'nt' or type(pid) is not int or pid <= 0: raise ValueError('Unsupported producer process')
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE,ctypes.POINTER(wintypes.DWORD)]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle: raise ValueError('Producer process unavailable')
    try:
        name = ctypes.create_unicode_buffer(32768); size = wintypes.DWORD(len(name))
        times = [wintypes.FILETIME() for _ in range(4)]; code = wintypes.DWORD()
        if (not kernel.QueryFullProcessImageNameW(handle,0,name,ctypes.byref(size))
                or not kernel.GetProcessTimes(handle,*(ctypes.byref(t) for t in times))
                or not kernel.GetExitCodeProcess(handle,ctypes.byref(code)) or code.value != 259):
            raise ValueError('Producer process identity unavailable')
        return {'pid':pid,'created':(times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
                'path':os.path.normcase(str(Path(name.value).resolve()))}
    finally: kernel.CloseHandle(handle)


def prepare_launch(argv):
    if len(argv) != 1 or Path(argv[0]).suffix.lower() != '.exe':
        raise ValueError('Producer tracking requires a native executable')
    before = file_identity(argv[0])
    result = subprocess.run([before['path'],'--version'],capture_output=True,text=True,timeout=15,
                            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    match = re.fullmatch(r'codex-cli ([^\s]+)\s*',result.stdout)
    if result.returncode or not match or match[1] not in SUPPORTED_VERSIONS or file_identity(before['path']) != before:
        raise ValueError('Unsupported or changed producer build')
    return {'file':before,'version':match[1]}


def attach_process(launch, process):
    current = process_identity(process.pid)
    if process.poll() is not None or current['path'] != launch['file']['path'] or file_identity(current['path']) != launch['file']:
        raise ValueError('Producer launch identity mismatch')
    return dict(launch, process=current)


def verify_launch(launch):
    if (process_identity(launch['process']['pid']) != launch['process']
            or file_identity(launch['file']['path']) != launch['file']):
        raise ValueError('Producer process or executable changed')


def execution_binding(store, runtime, stage):
    return {'pair':store.pair_id,'stage':stage['id'],'digest':stage['digest'],'epoch':runtime['epoch'],
            'session':runtime['session_uuid'],'execution':runtime['execution_id'],'turn':stage['turn_id'],
            'cwd':os.path.normcase(str(Path(runtime['cwd']).resolve())),
            'home':os.path.normcase(str(Path(runtime['codex_home']).resolve()))}


def record_producer(store, stage_id, state, launch, *, finished=False):
    verify_launch(launch)
    with store.transaction() as db:
        runtime = store._reviewer_row(db)
        stage = db.execute('SELECT * FROM review_stages WHERE id=?',(stage_id,)).fetchone()
        if (not stage or runtime['state'] != 'running' or runtime['epoch'] != state['epoch']
                or runtime['session_uuid'] != state['session'] or runtime['execution_id'] != state['execution_id']
                or stage['execution_id'] != state['execution_id'] or stage['reviewer_epoch'] != state['epoch']
                or stage['reviewer_session'] != state['session'] or stage['status'] != 'running'):
            raise ValueError('Producer execution changed')
        binding = execution_binding(store,runtime,stage)
        key = KEY + state['execution_id']
        previous = store._option(db,key)
        if finished:
            # 结束复核只能关闭已准确绑定的执行记录，不能补写丢失的起始证据。
            producer_context(previous,binding,live=True)
        if previous is None and not state.get('turn_id'):binding['turn']=None
        record = {'binding':binding,'launch':launch,'finished':finished}
        if previous:
            old = previous['binding']
            if (previous['launch'] != launch or {k:v for k,v in old.items() if k!='turn'} != {k:v for k,v in binding.items() if k!='turn'}
                    or old['turn'] and old['turn'] != binding['turn'] or previous['finished'] and not finished):
                raise ValueError('Producer evidence cannot be rebound')
        if finished and not binding['turn']: raise ValueError('Missing producer turn')
        store._put(db,key,record)
        return record


def producer_context(record, expected, *, live=True):
    if not isinstance(record,dict) or set(record) != {'binding','launch','finished'} or record['binding'] != expected:
        raise ValueError('Missing or mismatched producer evidence')
    if (any(type(record['binding'][k]) is not type(v) for k,v in expected.items())
            or not expected.get('turn') or type(record['finished']) is not bool): raise ValueError('Unbound producer turn')
    launch = record['launch']
    if launch.get('version') not in SUPPORTED_VERSIONS: raise ValueError('Unknown producer version')
    if live: verify_launch(launch)
    elif not record['finished']: raise ValueError('Producer has no final verification')
    digest = hashlib.sha256(json.dumps(record,sort_keys=True).encode()).hexdigest()
    return VerifiedProducer(launch['version'],expected,digest) if launch['version'] in STRICT_VERSIONS else '0.163.0'


def stored_context(store, db, runtime, stage, *, live=True):
    record = store._option(db,KEY+runtime['execution_id'])
    if record is not None:
        return producer_context(record,execution_binding(store,runtime,stage),live=live)
    return None


def final_context(store, db, runtime, stage):
    """最终阶段的独立门禁；缺证据返回None，不能由权限表是否为空决定。"""
    try:
        record=store._option(db,KEY+runtime['execution_id'])
        if (not isinstance(record,dict) or record.get('finished') is not True
                or not stage or runtime['state']!='running' or stage['status']!='running'
                or stage['reviewer_epoch']!=runtime['epoch']
                or stage['reviewer_session']!=runtime['session_uuid']
                or stage['execution_id']!=runtime['execution_id']):return None
        return producer_context(record,execution_binding(store,runtime,stage),live=True)
    except (ValueError,KeyError,TypeError,AttributeError,OSError,RuntimeError):
        return None
