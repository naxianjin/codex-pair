"""受限子树中的固定系统映像采集入口；输出只作诊断观察。"""
import hashlib
import json
from pathlib import Path
import subprocess


root = Path(__file__).resolve().parent
scratch = Path(r'S:\scratch')
capture = scratch / 'image-sections'
process = subprocess.run([
    r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
    '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'RemoteSigned',
    '-File', str(root / 'collector.ps1'), '-CaptureDirectory', str(capture),
    '-RuntimeSymbolsPath', str(root / 'contract.json'),
], capture_output=True, timeout=150)
if process.returncode:
    raise RuntimeError('System image collector failed: ' + process.stderr.decode('utf-8', errors='replace')[:2000])
if not 0 < len(process.stdout) <= 65536:
    raise ValueError('System image capture summary exceeded limit')
summary = json.loads(process.stdout.decode('utf-8-sig'))
if summary.get('Formal') is not False:
    raise ValueError('System image collector must remain diagnostic')
with (capture / 'capture.json').open('xb') as stream:
    stream.write(process.stdout)
files = {path.name: {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'bytes': path.stat().st_size} for path in capture.iterdir()}
with (scratch / 'validators.json').open('x', encoding='utf-8') as stream:
    json.dump({'formal': False, 'capture_id': summary['CaptureId'],
               'pid': summary['PID'], 'process_creation_utc': summary['ProcessCreationUTC'],
               'files': files}, stream, sort_keys=True)
