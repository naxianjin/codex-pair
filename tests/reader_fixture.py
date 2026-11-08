"""共享真实编译和反射夹具；临时文件在返回前清理，缓存仅保留字节与观察。"""
import base64
import copy
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


SHELL = Path(os.environ.get('SYSTEMROOT', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
SOURCE = Path(__file__).resolve().parents[1] / 'assets/audit/WindowsSafeInput.cs'


@lru_cache(maxsize=1)
def compiled_reader():
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        script = directory / 'compile.ps1'
        script.write_text("""param([string]$Source,[string]$Output)
$ErrorActionPreference='Stop'
$code=[IO.File]::ReadAllText($Source)
Add-Type -TypeDefinition $code -OutputAssembly (Join-Path $Output 'reader.dll')
$bytes=[IO.File]::ReadAllBytes((Join-Path $Output 'reader.dll'))
$assembly=[Reflection.Assembly]::Load($bytes)
$type=$assembly.GetType('PairAuditInput',$true)
$sha=[Security.Cryptography.SHA256]::Create()
try {
 $methods=@($type.GetMethods([Reflection.BindingFlags]'Static,Public,NonPublic,DeclaredOnly') | ForEach-Object {
  $body=$_.GetMethodBody()
  if($body){$il=$body.GetILAsByteArray();@{metadata_token=$_.MetadataToken;name=$_.Name;signature=$_.ToString();il_bytes=$il.Length;il_sha256=[BitConverter]::ToString($sha.ComputeHash($il)).Replace('-','').ToLowerInvariant()}}
 })
 $observation=@{mvid=$type.Module.ModuleVersionId.ToString();methods=$methods}
 [IO.File]::WriteAllText((Join-Path $Output 'reflection.json'),($observation | ConvertTo-Json -Depth 5))
} finally {$sha.Dispose()}
""", encoding='utf-8')
        result = subprocess.run([str(SHELL), '-NoProfile', '-NonInteractive', '-ExecutionPolicy',
                                 'RemoteSigned', '-File', str(script), '-Source', str(SOURCE),
                                 '-Output', str(directory)], capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError('Reader fixture compilation failed: ' + result.stderr.decode(errors='replace'))
        data = (directory / 'reader.dll').read_bytes()
        reflected = json.loads((directory / 'reflection.json').read_text(encoding='utf-8-sig'))
    return data, reflected


def reader_observation(source_sha256, *, pid=123, process_creation_utc='2026-11-06T17:20:00.1234567Z'):
    data, reflected = compiled_reader()
    methods = [{key: value for key, value in method.items() if key != 'name'}
               for method in reflected['methods']]
    return {'schema_version': 1, 'formal': False, 'pid': pid,
            'process_creation_utc': process_creation_utc, 'source_sha256': source_sha256,
            'assembly_input_sha256': hashlib.sha256(data).hexdigest(),
            'assembly_input_base64': base64.b64encode(data).decode('ascii'),
            'mvid': reflected['mvid'], 'methods': copy.deepcopy(methods)}
