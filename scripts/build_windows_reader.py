"""固定源码与 csc 参数的本地构建记录；文件事实不构成独立来源认证。"""
import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import subprocess

try:
    from .pair_producer import file_identity
    from .pair_reader_assembly import parse_assembly
except ImportError:
    from pair_producer import file_identity
    from pair_reader_assembly import parse_assembly


SOURCE = Path(__file__).resolve().parents[1] / 'assets/audit/WindowsSafeInput.cs'
SOURCE_SHA256 = '6f1e2e716c5e7922b59a33b2a4e5570018b213469bf5a90ec48256b813d7003b'
OPTIONS = ('/noconfig', '/nostdlib+', '/nologo', '/target:library', '/optimize+',
           '/debug-', '/platform:anycpu', '/langversion:5', '/checked-', '/unsafe-',
           '/warn:4', '/errorreport:none')
REFERENCES = ('mscorlib.dll', 'System.dll', 'System.Core.dll')
TOOL_FILES = ('csc.exe', 'csc.exe.config', 'clr.dll', *REFERENCES)


def build_contract():
    """只冻结本机文件事实；审核者仍须独立核验工具与构建来源。"""
    if os.name != 'nt':
        raise ValueError('Windows .NET Framework compiler required')
    directory = ctypes.create_unicode_buffer(32768)
    size = ctypes.windll.kernel32.GetWindowsDirectoryW(directory, len(directory))
    if not size or size >= len(directory):
        raise OSError('Windows directory unavailable')
    framework = Path(directory.value) / 'Microsoft.NET/Framework64/v4.0.30319'
    source = file_identity(SOURCE)
    if source['sha256'] != SOURCE_SHA256:
        raise ValueError('Fixed reader source SHA256 mismatch')
    return {'schema_version': 1, 'formal': False, 'source': source,
            'options': list(OPTIONS), 'references': list(REFERENCES),
            'tools': {name: file_identity(framework / name) for name in TOOL_FILES}}


def build_reader(output, contract):
    """新建独占输出目录，保留真实编译终态；不覆盖既有产物。"""
    current = build_contract()
    if contract != current:
        raise ValueError('Reader build contract changed or has unsupported options')
    source_bytes = SOURCE.read_bytes()
    if hashlib.sha256(source_bytes).hexdigest() != SOURCE_SHA256:
        raise ValueError('Fixed reader source changed before snapshot')
    output = Path(output).absolute()
    output.mkdir(parents=True, exist_ok=False)
    snapshot, assembly = output / 'WindowsSafeInput.cs', output / 'reader.dll'
    snapshot.write_bytes(source_bytes)
    framework = Path(current['tools']['csc.exe']['path']).parent
    argv = [str(framework / 'csc.exe'), *OPTIONS, '/out:' + str(assembly)]
    argv += ['/reference:' + str(framework / name) for name in REFERENCES]
    argv.append(str(snapshot))
    windows = framework.parents[2]
    environment = {'SYSTEMROOT': str(windows), 'WINDIR': str(windows),
                   'TEMP': str(output), 'TMP': str(output)}
    record = {'schema_version': 1, 'formal': False, 'conclusion': 'needs_validation',
              'contract': current, 'argv': argv, 'cwd': str(output),
              'environment': environment, 'source_snapshot_sha256': SOURCE_SHA256,
              'limits': ['Local file/process facts are not independent compiler provenance.',
                         'Compiler/runtime loaded bytes, guest compatibility and JIT are unverified.',
                         'No manifest, reader runtime, VM or Store integration.']}
    process = subprocess.Popen(argv, cwd=output, env=environment,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        record['process_start'] = _process_record(process, include_path=True)
        try:
            stdout, stderr = process.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate()
            record['timed_out'] = True
        record['process_final'] = _process_record(process)
        record['exit_code'] = process.returncode
        (output / 'stdout.log').write_bytes(stdout)
        (output / 'stderr.log').write_bytes(stderr)
        record['stdout_sha256'] = hashlib.sha256(stdout).hexdigest()
        record['stderr_sha256'] = hashlib.sha256(stderr).hexdigest()
        initial, final = record['process_start'], record['process_final']
        if any(initial[key] != final[key] for key in ('pid', 'created')):
            raise ValueError('Compiler process identity changed')
        if initial['path'] != current['tools']['csc.exe']['path']:
            raise ValueError('Compiler process path mismatch')
        if record.get('timed_out') or process.returncode != 0 or final['exit_code'] != 0:
            raise ValueError('Reader compiler failed or timed out')
        if build_contract() != current or snapshot.read_bytes() != source_bytes:
            raise ValueError('Reader build input or tool changed during compilation')
        data = assembly.read_bytes()
        sha256 = hashlib.sha256(data).hexdigest()
        record['assembly'] = {'file': 'reader.dll', 'bytes': len(data), 'sha256': sha256,
                              'parsed': parse_assembly(data, sha256)}
        record['build_completed'] = True
    except BaseException as error:
        if process.poll() is None:
            process.kill()
            process.communicate()
        record['build_completed'] = False
        record['error_type'] = type(error).__name__
        record['error'] = str(error)
        if assembly.exists():
            assembly.unlink()
        _write_json(output / 'provenance.json', record)
        raise
    _write_json(output / 'provenance.json', record)
    return record


def _process_record(process, *, include_path=False):
    """结束时保留原句柄的创建时间；映像路径仅在启动后观察一次。"""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    name = ctypes.create_unicode_buffer(32768)
    size, code = wintypes.DWORD(len(name)), wintypes.DWORD()
    times = [wintypes.FILETIME() for _ in range(4)]
    handle = int(process._handle)
    if (not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times))
            or not kernel.GetExitCodeProcess(handle, ctypes.byref(code))):
        raise ctypes.WinError(ctypes.get_last_error())
    record = {'pid': process.pid,
              'created': (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime,
              'exit_code': code.value}
    if include_path:
        if not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        record['path'] = os.path.normcase(str(Path(name.value).resolve()))
    return record


def _write_json(path, value):
    with path.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--write-contract', type=Path)
    group.add_argument('--contract', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.write_contract:
        _write_json(args.write_contract, build_contract())
    else:
        if args.output is None:
            parser.error('--output is required with --contract')
        result = build_reader(args.output, json.loads(args.contract.read_bytes()))
        print(json.dumps({'build_completed': result['build_completed'], 'formal': False,
                          'assembly': result['assembly']}, ensure_ascii=True))


if __name__ == '__main__':
    main()
