"""可信隔离执行器调用的固定 CJS 工作入口；自身不提供隔离或正式放行证明。"""
import hashlib
import os
from pathlib import Path
import stat
import subprocess


def validator_request(prepared, *, node_sha256):
    """把 Store 固定输入转为客体契约；执行器另行固定目录与工具链。"""
    files = {}
    expected = {'validate-findings.cjs': 'findings', 'validate-coverage-ledger.cjs': 'coverage_ledger'}
    if len(prepared['inputs']) != 2:
        raise ValueError('Both fixed validators required')
    for item in prepared['inputs']:
        entry = item['contract']
        name = entry['name']
        if name not in expected or entry['input_field'] != expected.pop(name):
            raise ValueError('Unexpected validator input')
        if hashlib.sha256(item['input_bytes']).hexdigest() != entry['input_sha256']:
            raise ValueError('Prepared validator bytes changed')
        dependencies = {entry['validator_path']: entry['validator_sha256'], **entry['reader_assets']}
        if entry['schema_path']:
            dependencies[entry['schema_path']] = entry['schema_sha256']
        for path, value in dependencies.items():
            if not path.startswith('assets/audit/') or len(Path(path).parts) != 3:
                raise ValueError('Unexpected validator asset path')
            key = Path(path).name
            if key in files and files[key] != value:
                raise ValueError('Conflicting validator dependency')
            files[key] = value
        files[entry['input_field'] + '.json'] = entry['input_sha256']
    return {'files': files, 'binding': {key: prepared[key] for key in
            ('context', 'original_report_digest', 'stored_report_digest', 'adapted_report_digest')},
            'node_sha256': node_sha256}


def run_validators(root, request, *, node):
    """输入目录须由外层执行器固定为只读；不接受任意命令或额外校验器。"""
    root = Path(root).absolute()
    node = Path(node).absolute()
    names = ('validate-findings.cjs', 'validate-coverage-ledger.cjs')
    readers = ('read-windows-input.cjs', 'read-windows-input.ps1', 'WindowsSafeInput.cs')
    files = request['files']
    expected = set(names + readers + ('report-schema.json', 'findings.json', 'coverage_ledger.json'))
    if set(files) != expected or set(request) != {'files', 'binding', 'node_sha256'}:
        raise ValueError('Invalid fixed validator request')
    if hashlib.sha256(Path(node).read_bytes()).hexdigest() != request['node_sha256']:
        raise ValueError('Validator Node identity mismatch')
    for name in expected:
        path = root / name
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                or info.st_nlink != 1 or info.st_size > 5 * 1024 * 1024):
            raise ValueError('Validator input is not a bounded independent file')
        if hashlib.sha256(path.read_bytes()).hexdigest() != files[name]:
            raise ValueError('Validator input digest mismatch')
    results = []
    environment = {key: value for key, value in os.environ.items()
                   if key.upper() in ('SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP')}
    for name, field in zip(names, ('findings', 'coverage_ledger')):
        process = subprocess.run([str(node), str(root / name), str(root / (field + '.json'))],
                                 cwd=root, env=environment, capture_output=True, timeout=30)
        if len(process.stdout) + len(process.stderr) > 1024 * 1024:
            raise ValueError('Validator output exceeded limit')
        results.append({'validator': name, 'exit_code': process.returncode,
                        'stdout_sha256': hashlib.sha256(process.stdout).hexdigest(),
                        'stderr_sha256': hashlib.sha256(process.stderr).hexdigest()})
    for name in expected:
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != files[name]:
            raise ValueError('Validator input changed during execution')
    if hashlib.sha256(Path(node).read_bytes()).hexdigest() != request['node_sha256']:
        raise ValueError('Validator Node changed during execution')
    return {'binding': request['binding'], 'files': files, 'results': results,
            'validators_succeeded': all(item['exit_code'] == 0 for item in results),
            'formal_checks_verified': False}
