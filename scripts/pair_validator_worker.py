"""可信隔离执行器调用的固定 CJS 工作入口；自身不提供隔离或正式放行证明。"""
import base64
import hashlib
import ctypes
import json
import os
import re
from pathlib import Path
import stat
import subprocess

if __package__:
    from .pair_reader_assembly import parse_assembly
else:
    # -I 不搜索脚本目录；执行器固定并核对同目录依赖的字节。
    import importlib.util
    spec = importlib.util.spec_from_file_location('pair_reader_assembly', Path(__file__).with_name('pair_reader_assembly.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parse_assembly = module.parse_assembly


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


def verify_validator_result(request, native, artifact):
    """宿主验收同次隔离结果；不替代控制链身份和官方工具回执核验。"""
    if (type(native.get('ExitCode')) is not int or type(native.get('ActiveProcesses')) is not int
            or native['ExitCode'] != 0 or native['ActiveProcesses'] != 0
            or native.get('TimedOut') is not False):
        raise ValueError('Validator execution terminal state unverified')
    if (json.dumps(artifact.get('binding'), sort_keys=True, separators=(',', ':'), allow_nan=False)
            != json.dumps(request['binding'], sort_keys=True, separators=(',', ':'), allow_nan=False)
            or artifact.get('files') != request['files']
            or artifact.get('formal_checks_verified') is not False):
        raise ValueError('Validator result binding mismatch')
    results = artifact.get('results')
    names = ['validate-findings.cjs', 'validate-coverage-ledger.cjs']
    if (not isinstance(results, list) or len(results) != 2 or any(not isinstance(item, dict) for item in results)
            or [item.get('validator') for item in results] != names):
        raise ValueError('Validator results incomplete')
    for item in results:
        if type(item.get('exit_code')) is not int:
            raise ValueError('Invalid validator exit code')
        for field in ('stdout_sha256', 'stderr_sha256'):
            value = item.get(field)
            if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise ValueError('Invalid validator output digest')
        stderr = item.get('stderr')
        if (not isinstance(stderr, str) or len(stderr.encode('utf-8')) > 1048576
                or hashlib.sha256(stderr.encode('utf-8')).hexdigest() != item['stderr_sha256']):
            raise ValueError('Validator diagnostic digest mismatch')
    observed = {item['validator']: reader_observations(item['stderr'], request['files']['WindowsSafeInput.cs'])
                for item in results}
    if 'reader_observations' in artifact or any(observed.values()):
        if json.dumps(observed, sort_keys=True) != json.dumps(artifact.get('reader_observations'), sort_keys=True):
            raise ValueError('Reader observation receipt mismatch')
    passed = all(item['exit_code'] == 0 for item in results)
    if artifact.get('validators_succeeded') is not passed:
        raise ValueError('Validator outcome inconsistent')
    return {'execution_completed': True, 'validators_succeeded': passed,
            'formal_checks_verified': False}



_READER_PREFIX = 'PAIR_READER_OBSERVATION:'


def reader_observations(stderr, source_sha256):
    """解析实际读取器的诊断行；内容仍是观察，不能授予正式认证。"""
    observations = []
    for line in stderr.splitlines():
        if not line.startswith(_READER_PREFIX):
            continue
        if len(line.encode('utf-8')) > 65536:
            raise ValueError('Reader observation exceeded limit')
        value = json.loads(line[len(_READER_PREFIX):])
        fields = {'schema_version', 'formal', 'pid', 'process_creation_utc',
                  'source_sha256', 'assembly_input_sha256', 'assembly_input_base64', 'mvid', 'methods'}
        if (not isinstance(value, dict) or set(value) != fields
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['formal'] is not False or type(value['pid']) is not int or value['pid'] <= 0
                or not isinstance(value['process_creation_utc'], str)
                or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}Z', value['process_creation_utc'])
                or value['source_sha256'] != source_sha256
                or not isinstance(value['assembly_input_sha256'], str)
                or not re.fullmatch(r'[0-9a-f]{64}', value['assembly_input_sha256'])
                or not isinstance(value['assembly_input_base64'], str)
                or not value['assembly_input_base64']
                or not isinstance(value['mvid'], str)
                or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', value['mvid'])
                or not isinstance(value['methods'], list) or len(value['methods']) != 3):
            raise ValueError('Reader observation binding mismatch')
        methods = value['methods']
        for method in methods:
            if (not isinstance(method, dict) or set(method) != {'metadata_token', 'signature', 'il_sha256', 'il_bytes'}
                    or type(method['metadata_token']) is not int or method['metadata_token'] >> 24 != 6
                    or type(method['il_bytes']) is not int or not 1 <= method['il_bytes'] <= 1048576
                    or not isinstance(method['signature'], str)
                    or not isinstance(method['il_sha256'], str)
                    or not re.fullmatch(r'[0-9a-f]{64}', method['il_sha256'])):
                raise ValueError('Reader method observation invalid')
        if (len({m['metadata_token'] for m in methods}) != 3
                or {m['signature'] for m in methods} != {'IntPtr OpenRoot(System.String)',
                    'Byte[] Read(System.String)', 'Byte[] ReadBytes(IntPtr, System.String)'}):
            raise ValueError('Reader method set mismatch')
        try:
            assembly = base64.b64decode(value['assembly_input_base64'], validate=True)
        except ValueError as error:
            raise ValueError('Reader assembly encoding invalid') from error
        if base64.b64encode(assembly).decode('ascii') != value['assembly_input_base64']:
            raise ValueError('Reader assembly encoding is not canonical')
        parsed = parse_assembly(assembly, value['assembly_input_sha256'])
        expected_methods = [{key: item[key] for key in ('metadata_token', 'signature', 'il_sha256', 'il_bytes')}
                            for item in parsed['methods']]
        if (parsed['mvid'] != value['mvid']
                or sorted(expected_methods, key=lambda item: item['metadata_token'])
                   != sorted(methods, key=lambda item: item['metadata_token'])):
            raise ValueError('Reader assembly content observation mismatch')
        observations.append(value)
    if len(observations) > 2 or len({(v['pid'], v['process_creation_utc']) for v in observations}) != len(observations):
        raise ValueError('Reader process observations duplicated or excessive')
    return observations


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
                   if key.upper() in ('TEMP', 'TMP')}
    if os.name == 'nt':
        directory = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetWindowsDirectoryW(directory, len(directory))
        if not length or length >= len(directory):
            raise OSError('Windows system directory unavailable')
        environment.update(SYSTEMROOT=directory.value, WINDIR=directory.value)
    for name, field in zip(names, ('findings', 'coverage_ledger')):
        process = subprocess.run([str(node), str(root / name), str(root / (field + '.json'))],
                                 cwd=root, env=environment, capture_output=True, timeout=30)
        if len(process.stdout) + len(process.stderr) > 1024 * 1024:
            raise ValueError('Validator output exceeded limit')
        results.append({'validator': name, 'exit_code': process.returncode,
                        'stdout_sha256': hashlib.sha256(process.stdout).hexdigest(),
                        'stderr_sha256': hashlib.sha256(process.stderr).hexdigest(),
                        'stderr': process.stderr.decode('utf-8', errors='strict')})
    for name in expected:
        if hashlib.sha256((root / name).read_bytes()).hexdigest() != files[name]:
            raise ValueError('Validator input changed during execution')
    if hashlib.sha256(Path(node).read_bytes()).hexdigest() != request['node_sha256']:
        raise ValueError('Validator Node changed during execution')
    observed = {item['validator']: reader_observations(item['stderr'], files['WindowsSafeInput.cs'])
                for item in results}
    if os.name == 'nt' and any(item['exit_code'] == 0 and not observed[item['validator']] for item in results):
        raise ValueError('Successful Windows validator omitted reader observation')
    return {'binding': request['binding'], 'files': files, 'results': results,
            'reader_observations': observed,
            'validators_succeeded': all(item['exit_code'] == 0 for item in results),
            'formal_checks_verified': False}


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    parser.add_argument('--request', required=True)
    parser.add_argument('--node', required=True)
    args = parser.parse_args()
    request_path = Path(args.request)
    with request_path.open('rb') as stream:
        data = stream.read(65537)
    if len(data) > 65536:
        raise ValueError('Validator request exceeded limit')
    request = json.loads(data.decode('utf-8'))
    print(json.dumps(run_validators(args.root, request, node=args.node), ensure_ascii=True, sort_keys=True))
