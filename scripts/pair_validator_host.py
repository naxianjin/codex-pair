"""开发工作树中的固定受限运行对账；文件自洽不能替代正式认证。"""
import base64
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import xml.etree.ElementTree as ET

try:
    from .pair_auxiliary_runtime import saved_validator_inputs
    from .pair_validator_worker import validator_request, verify_validator_result
except ImportError:
    from pair_auxiliary_runtime import saved_validator_inputs
    from pair_validator_worker import validator_request, verify_validator_result


_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
_RUN_ROOT = Path(
    r'D:\Codex_No_Projects\work\github\codex-pair-real-wokey-20261104'
    r'\.codex-pair\worktrees\windows-f93e3110d73b-a'
    r'\validation\direct-tests-v4\hyperv-validator\runs')
_SOURCES = {'Invoke-ValidatorRawModuleDiagnostic.ps1', 'Test-RootTrace.ps1',
            'PairValidatorIdentityDiagnostic.cs', 'root-identity-baseline.json',
            'run-raw-module-etw.ps1', 'check-combined-evidence.py',
            'check-restricted-raw-modules.py'}
_CONTROLS = {'runner.cs', 'entry.py', 'reader.cs', 'reset.ps1'}
_TOOLS = {'powershell': r'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe',
          'mscorlib': r'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\mscorlib.dll',
          'python': r'C:\PairLab\Tools\python\python.exe',
          'node': r'C:\PairLab\Tools\node\node.exe'}
_LIMITS = {'Processes': 16, 'ProcessMiB': 384, 'JobMiB': 512,
           'CPUSeconds': 120, 'WallSeconds': 180, 'DiskMiB': 64}


def reconcile_restricted_validator_run(store, member, session, cwd, execution_id, lease, *, run_id):
    """仅接受固定运行引用，复算当前报告绑定后保存诊断观察。"""
    if not isinstance(run_id, str) or not re.fullmatch(r'[0-9a-f]{32}', run_id):
        raise ValueError('Invalid restricted validator run ID')
    prepared = saved_validator_inputs(store, member, session, cwd, execution_id, lease)
    source = _PACKAGE_ROOT / 'validation/bytecode-provenance-20261105'
    if not source.is_dir():
        raise ValueError('Restricted diagnostic requires its development validation tree; installed host capability unavailable')
    run = _RUN_ROOT / run_id
    host_bytes = _read(run / 'request.json', 65536)
    receipt_bytes = _read(run / 'receipt.json', 65536)
    native_bytes = _read(run / 'native.json', 8 * 1024 * 1024)
    artifact_bytes = _read(run / 'artifact.json', 1048576)
    host, receipt, native, artifact = map(_json, (host_bytes, receipt_bytes, native_bytes, artifact_bytes))
    inventory_bytes = _read(source / 'raw-module-source-inventory-loaded-reader.json', 65536)
    inventory = _json(inventory_bytes)
    if (set(inventory) != {'formal', 'profile', 'files'} or inventory['formal'] is not False
            or inventory['profile'] != 'loaded-reader-v1'
            or set(_hashes(inventory['files'])) != _SOURCES):
        raise ValueError('Restricted source inventory mismatch')
    sources = {name: _read(source / name, 5 * 1024 * 1024) for name in _SOURCES}
    source_hashes = {name: _sha(data) for name, data in sources.items()}
    if _hashes(inventory['files']) != source_hashes:
        raise ValueError('Restricted source digest mismatch')
    baseline = _json(sources['root-identity-baseline.json'])
    tools, controls = _hashes(baseline.get('tools')), _hashes(baseline.get('controls'))
    baseline_limits = dict(zip(('processes', 'process_memory_mib', 'job_memory_mib',
                               'cpu_seconds', 'wall_seconds', 'disk_quota_mib'), _LIMITS.values()))
    if (set(tools) != set(_TOOLS.values()) or set(controls) != _CONTROLS
            or baseline.get('vm_id') != '3d337311-e8b0-4d7e-a746-f21f72e4cdb6'
            or _canonical(baseline.get('limits')) != _canonical(baseline_limits)):
        raise ValueError('Restricted diagnostic baseline mismatch')
    if (set(host) != {'Run', 'VM', 'SourceInventorySHA256', 'ControllerSHA256', 'Files',
                      'EvidenceProfile', 'Formal', 'Limits'}
            or set(receipt) != {'Run', 'State', 'NativeSHA256', 'SourceInventorySHA256',
                               'ControllerSHA256', 'Formal'}
            or set(native) != {'Run', 'Files', 'Tools', 'Native', 'Trace'}
            or host['Run'] != run_id or receipt['Run'] != run_id or native['Run'] != run_id
            or host['Formal'] is not False or receipt['Formal'] is not False
            or host['EvidenceProfile'] != 'loaded-reader-v1' or receipt['State'] != 'terminal'
            or host['VM'] != baseline['vm_id']
            or _canonical(host['Limits']) != _canonical(_LIMITS)
            or not isinstance(native['Trace'], dict) or native['Trace'].get('Formal') is not False
            or not isinstance(native['Native'], dict)):
        raise ValueError('Restricted run receipt mismatch')
    inventory_sha = _sha(inventory_bytes)
    controller_sha = source_hashes['Invoke-ValidatorRawModuleDiagnostic.ps1']
    for record in (host, receipt):
        if (_hash(record['SourceInventorySHA256']) != inventory_sha
                or _hash(record['ControllerSHA256']) != controller_sha):
            raise ValueError('Restricted run source binding mismatch')
    if _hash(receipt['NativeSHA256']) != _sha(native_bytes) or _hashes(native['Tools']) != tools:
        raise ValueError('Restricted native or tool digest mismatch')
    terminal = native['Native']
    if (set(terminal) != {'ExitCode', 'ActiveProcesses', 'TotalProcesses', 'RootPid',
                          'RootCreationFileTime', 'TimedOut', 'Seconds', 'CPUSeconds', 'ArtifactBase64'}
            or any(type(terminal[key]) is not int or not 1 <= terminal[key] <= maximum
                   for key, maximum in (('TotalProcesses', _LIMITS['Processes']),
                                         ('RootPid', 0xffffffff), ('RootCreationFileTime', 0xffffffffffffffff)))
            or any(type(terminal[key]) not in (int, float) or not 0 <= terminal[key] <= maximum
                   for key, maximum in (('Seconds', _LIMITS['WallSeconds']),
                                         ('CPUSeconds', _LIMITS['CPUSeconds'])))):
        raise ValueError('Restricted native resource observation invalid')
    request = validator_request(prepared, node_sha256=tools[_TOOLS['node']])
    files = _hashes(host['Files'])
    if (set(files) != set(request['files']) | _CONTROLS | {'request.json', 'pair_validator_worker.py'}
            or _hashes(native['Files']) != files):
        raise ValueError('Restricted snapshot file set mismatch')
    inputs = {name: _read(run / 'input' / name, 65536 if name == 'request.json' else 5 * 1024 * 1024)
              for name in files}
    if {name: _sha(data) for name, data in inputs.items()} != files:
        raise ValueError('Restricted snapshot digest mismatch')
    expected_files = dict(request['files'], **controls)
    expected_files['pair_validator_worker.py'] = _sha(_read(
        _PACKAGE_ROOT / 'scripts/pair_validator_worker.py', 5 * 1024 * 1024))
    if any(files[name] != value for name, value in expected_files.items()):
        raise ValueError('Restricted current input or control binding mismatch')
    if _canonical(_json(inputs['request.json'])) != _canonical(request):
        raise ValueError('Restricted request is not the current saved report')
    encoded = native['Native'].get('ArtifactBase64')
    if not isinstance(encoded, str) or len(encoded) > 1398104:
        raise ValueError('Restricted artifact encoding invalid')
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise ValueError('Restricted artifact encoding invalid') from error
    if decoded != artifact_bytes:
        raise ValueError('Restricted artifact receipt mismatch')
    verified = verify_validator_result(request, native['Native'], artifact)
    observations = artifact.get('reader_observations', {})
    if any(result['exit_code'] == 0 and not observations.get(result['validator'])
           for result in artifact['results']):
        raise ValueError('Restricted loaded reader observation missing')
    trace_verified = _verify_trace(native, observations, sources, source)
    # 完整 ETW 与 Base64 留在运行目录，Store 保存可回溯摘要，避免重复放大证据。
    terminal = {key: value for key, value in native['Native'].items() if key != 'ArtifactBase64'}
    terminal['HostObservation'] = {'run_id': run_id, 'evidence_profile': 'loaded-reader-v1',
        'host_request_sha256': _sha(host_bytes), 'receipt_sha256': _sha(receipt_bytes),
        'native_sha256': _sha(native_bytes), 'artifact_sha256': _sha(artifact_bytes),
        'source_inventory_sha256': inventory_sha, 'trace_verification': trace_verified,
        'formal_checks_verified': False}
    identity = {name: tools[path] for name, path in _TOOLS.items()}
    identity.update(controller=controller_sha, source_inventory=inventory_sha,
                    worker=expected_files['pair_validator_worker.py'])
    result = store.record_validator_run(member, session, cwd, execution_id, lease,
                                        run_id, request, terminal, artifact, identity)
    return dict(result, **verified, status='needs_validation',
                remaining_checks=['host_identity_unverified', 'system_image_equivalence_unverified',
                                  'jit_content_unverified', 'formal_receipt_unverified'])


def _verify_trace(native, observations, sources, source):
    """复用已核对的检查器，再绑定进程生命周期；不认证观察内容。"""
    trace = native['Trace']
    if (not isinstance(trace.get('Xml'), str) or not isinstance(trace.get('Summary'), str)
            or not isinstance(trace.get('Images'), list)
            or not isinstance(trace.get('ModuleEvents'), list)
            or not isinstance(trace.get('ModuleTemplates'), list)):
        raise ValueError('Restricted ETW evidence incomplete')
    for item in trace['Images']:
        if not isinstance(item, dict) or not isinstance(item.get('Image'), str):
            raise ValueError('Restricted ETW image observation invalid')
        if 'SHA256' in item:
            _hash(item['SHA256'])
    checked = []
    for name in ('check-combined-evidence.py', 'check-restricted-raw-modules.py'):
        namespace = {'__name__': 'restricted_diagnostic_checker', '__file__': str(source / name)}
        exec(compile(sources[name], str(source / name), 'exec'), namespace)
        checked.append(namespace['check'](native, profile='loaded-reader-v1'))
    lost = re.findall(r'(?m)^Total Events[^\S\r\n]+Lost[^\S\r\n]+(\d+)[ \t\r]*$', trace['Summary'])
    if lost != ['0']:
        raise ValueError('Restricted ETW loss count missing, ambiguous or nonzero')
    ns = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}
    document = ET.fromstring(trace['Xml'])
    if any((item.text or '').strip() != '0' for item in document.findall('.//e:Data[@Name="EventsLost"]', ns)):
        raise ValueError('Restricted XML ETW loss count nonzero')
    rows = []
    for event in document.findall('e:Event', ns):
        provider = event.find('e:System/e:Provider', ns)
        if (provider.get('Name') != 'Microsoft-Windows-Kernel-Process'
                or provider.get('Guid', '').lower() != '{22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716}'):
            continue
        event_id = event.findtext('e:System/e:EventID', namespaces=ns)
        if event_id not in ('1', '2'):
            continue
        fields = {}
        for item in event.findall('e:EventData/e:Data', ns):
            if item.get('Name') in fields:
                raise ValueError('Duplicate restricted ETW process field')
            fields[item.get('Name')] = (item.text or '').strip()
        rows.append((event_id, fields))
    starts = [fields for event_id, fields in rows if event_id == '1']
    roots = [row for row in starts if row['ProcessID'] == str(native['Native']['RootPid'])
             and _filetime(row['CreateTime']) == native['Native']['RootCreationFileTime']]
    if len(roots) != 1:
        raise ValueError('Restricted native root creation identity mismatch')
    tree = {(roots[0]['ProcessID'], roots[0]['ProcessSequenceNumber'])}
    for _ in starts:
        for row in starts:
            if (row['ParentProcessID'], row['ParentProcessSequenceNumber']) in tree:
                tree.add((row['ProcessID'], row['ProcessSequenceNumber']))
    if len(tree) != native['Native']['TotalProcesses']:
        raise ValueError('Restricted ETW process tree incomplete')
    tree_starts = {}
    for pid, sequence in tree:
        begin = [row for row in starts if (row['ProcessID'], row['ProcessSequenceNumber']) == (pid, sequence)]
        end = [row for event_id, row in rows if event_id == '2'
               and (row['ProcessID'], row['ProcessSequenceNumber']) == (pid, sequence)]
        if (len(begin) != 1 or len(end) != 1 or begin[0]['CreateTime'] != end[0]['CreateTime']
                or not re.fullmatch(r'\d{1,10}', end[0]['ExitCode']) or int(end[0]['ExitCode']) > 0xffffffff
                or _filetime(end[0]['ExitTime']) < _filetime(begin[0]['CreateTime'])
                or (pid == str(native['Native']['RootPid']) and int(end[0]['ExitCode']) != native['Native']['ExitCode'])):
            raise ValueError('Restricted ETW child terminal state mismatch')
        tree_starts[(int(pid), _filetime(begin[0]['CreateTime']))] = begin[0]
    readers = set()
    for values in observations.values():
        for value in values:
            identity = value['pid'], _filetime(value['process_creation_utc'])
            process = tree_starts.get(identity)
            if (identity in readers or not process
                    or not process['ImageName'].lower().endswith('\\windows\\system32\\windowspowershell\\v1.0\\powershell.exe')):
                raise ValueError('Restricted reader process is not uniquely bound to ETW')
            readers.add(identity)
    return {'processes': len(tree), 'assembly_pairs': checked[0][1], 'module_pairs': checked[1]['module_pairs'],
            'image_hashes': checked[0][2], 'reader_processes': len(readers), 'events_lost': 0,
            'formal': False}


def _filetime(value):
    match = re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)\.(\d{7})(?:00)?Z', value)
    if not match:
        raise ValueError('Restricted process creation timestamp invalid')
    delta = datetime.datetime.fromisoformat(match[1]) - datetime.datetime(1601, 1, 1)
    return (delta.days * 86400 + delta.seconds) * 10000000 + int(match[2])


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('Duplicate restricted evidence field')
            result[key] = value
        return result
    value = json.loads(data.decode('utf-8-sig'), object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise ValueError('Restricted evidence object required')
    _canonical(value)
    return value


def _hash(value):
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', value):
        raise ValueError('Invalid restricted evidence digest')
    return value.lower()


def _hashes(value):
    if not isinstance(value, dict) or any(not isinstance(name, str) for name in value):
        raise ValueError('Restricted digest map required')
    return {name: _hash(item) for name, item in value.items()}


def _read(path, limit):
    def identity(info):
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns
    for entry in (path, *path.parents):
        info = entry.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Redirected restricted evidence path')
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
        raise ValueError('Restricted evidence is not a bounded independent file')
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        data = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
    if (len(data) > limit or identity(before) != identity(opened)
            or identity(opened) != identity(after) or identity(after) != identity(path.lstat())):
        raise ValueError('Restricted evidence changed during read')
    return data
