"""退出后仅清理准确旧绑定的自有Hook；不触碰相邻成员配置。"""
import ctypes
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import uuid


def _argv(command):
    if os.name != 'nt':
        return shlex.split(command)
    from ctypes import wintypes
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(wintypes.LPWSTR)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    count = ctypes.c_int()
    memory = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not memory:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [memory[i] for i in range(count.value)]
    finally:
        kernel.LocalFree(memory)


def cleanup_retired_hook(store, source):
    try:
        from .pair_core import FileLock
    except ImportError:
        from pair_core import FileLock
    path = Path(source['codex_home']) / 'hooks.json'
    if path.resolve() != path.absolute():
        return {'status': 'pending', 'reason': 'redirected_hook_path'}
    if not path.exists():
        return {'status': 'absent', 'removed': 0}
    marker = 'codex-pair:'+store.pair_id+':'+source['id']
    with FileLock(path.with_name('hooks.codex-pair.lock'), timeout=30):
        original = path.read_bytes()
        data = json.loads(original.decode('utf-8-sig'))
        if not isinstance(data,dict) or not isinstance(data.get('hooks',{}),dict):
            raise ValueError('Invalid hook structure / Hook配置结构无效，保留原文件')
        groups = data.get('hooks', {}).get('PermissionRequest', [])
        if (not isinstance(groups,list) or any(not isinstance(group,dict) or not isinstance(group.get('hooks',[]),list)
                or any(not isinstance(handler,dict) for handler in group.get('hooks',[])) for group in groups)):
            raise ValueError('Invalid hook handlers / Hook处理器结构无效，保留原文件')
        kept, removed, unmatched = [], 0, 0
        for group in groups:
            handlers = []
            for handler in group.get('hooks', []):
                owned = False
                if handler.get('statusMessage') == marker and isinstance(handler.get('command'), str):
                    args = _argv(handler['command'])
                    if len(args) >= 2 and Path(args[1]).name == 'pair_permission_hook.py' and len(args[2:]) % 2 == 0:
                        flags = dict(zip(args[2::2], args[3::2]))
                        expected = {'--pair','--state-root','--member','--timeout','--session','--binding-version','--package-digest'}
                        owned = (len(args) == 16 and set(flags) == expected and flags['--pair'] == store.pair_id
                                 and Path(flags['--state-root']).resolve() == store.state_root.resolve()
                                 and flags['--member'] == source['id'] and flags['--session'] == source['session_uuid']
                                 and flags['--binding-version'] == str(source['binding_version']) and flags['--timeout'] == '180'
                                 and re.fullmatch('[0-9a-f]{64}', flags['--package-digest']) is not None
                                 and (os.name != 'nt' or subprocess.list2cmdline(args) == handler['command']))
                    if not owned:
                        unmatched += 1
                if owned:
                    removed += 1
                else:
                    handlers.append(handler)
            if handlers or not group.get('hooks'):
                kept.append(dict(group, hooks=handlers))
        if removed:data['hooks']['PermissionRequest'] = kept
        for event in ('PreToolUse','PostToolUse','Interrupt','UserPromptSubmit','Stop'):
            groups=data.get('hooks',{}).get(event,[]);kept=[]
            if not isinstance(groups,list) or any(not isinstance(g,dict) or not isinstance(g.get('hooks'),list) or any(not isinstance(h,dict) for h in g['hooks']) for g in groups):raise ValueError('Invalid control hook handlers / 保留原文件')
            control_marker='codex-pair-control:'+store.pair_id+':'+source['id']+':'+event
            for group in groups:
                handlers=[]
                for handler in group['hooks']:
                    owned=False
                    if handler.get('statusMessage')==control_marker and isinstance(handler.get('command'),str):
                        args=_argv(handler['command'])
                        flags=dict(zip(args[2::2],args[3::2])) if len(args) in (14,16) else {}
                        expected={'--pair','--state-root','--member','--session','--binding-version','--package-digest'}
                        owned=(len(args) in (14,16) and Path(args[1]).name=='pair_control_hook.py'
                            and (len(args)==14 and set(flags)==expected or len(args)==16 and set(flags)==expected|{'--event'} and flags['--event']==event)
                            and flags['--pair']==store.pair_id and Path(flags['--state-root']).resolve()==store.state_root.resolve()
                            and flags['--member']==source['id'] and flags['--session']==source['session_uuid']
                            and flags['--binding-version']==str(source['binding_version'])
                            and re.fullmatch('[0-9a-f]{64}',flags['--package-digest']) is not None
                            and (os.name!='nt' or subprocess.list2cmdline(args)==handler['command']))
                        if not owned:unmatched+=1
                    if owned:removed+=1
                    else:handlers.append(handler)
                if handlers:kept.append(dict(group,hooks=handlers))
            if event in data.get('hooks',{}):data['hooks'][event]=kept
        if removed:
            backup = path.with_name('hooks.codex-pair-backup-'+uuid.uuid4().hex+'.json')
            with backup.open('xb') as file:file.write(original)
            temporary = path.with_name('hooks.codex-pair-'+uuid.uuid4().hex+'.tmp')
            try:
                with temporary.open('x', encoding='utf-8') as file:
                    json.dump(data, file, ensure_ascii=False, indent=2)
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return {'status': 'pending' if unmatched else 'cleaned', 'removed': removed,
                'unmatched_preserved': unmatched, 'other_bindings_preserved': True}
