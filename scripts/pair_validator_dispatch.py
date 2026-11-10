"""固定验证器的 owned command/exec 诊断；授权者与执行器分开，始终非正式。"""
import copy
import hashlib
from pathlib import Path
import subprocess
import time
import uuid

try:
    from . import pair_validator_host as host
    from .pair_producer import file_identity, verify_launch
    from .pair_stages import digest
    from .pair_transport import AppServer, DeliveryUnknown, control_environment
    from .pair_validator_admission import admit_validator_run, validator_admission_boundary, _run_id
except ImportError:
    import pair_validator_host as host
    from pair_producer import file_identity, verify_launch
    from pair_stages import digest
    from pair_transport import AppServer, DeliveryUnknown, control_environment
    from pair_validator_admission import admit_validator_run, validator_admission_boundary, _run_id


_KEY = 'validator_host_command:'
_CONSUMPTION_KEY = 'validator_host_consumption:'
_VERSION = '0.167.0-alpha.3.2'
_EXECUTABLE_SHA256 = '4b3286e35d7fe866c6b0eb3f07d7a4cdb5e96489576170dfd6b3a15aa1c67382'


def dispatch_owned_validator_run(store, member, session, cwd, execution_id, lease, *,
                                 executable, codex_home, run_id=None):
    """仅构造固定命令，不接收调用者 argv、回执或事后执行记录。"""
    run_id = uuid.uuid4().hex if run_id is None else run_id
    _run_id(run_id)
    if file_identity(executable)['sha256'] != _EXECUTABLE_SHA256:
        raise ValueError('Exact official command executor build required')
    shell, identity = _current_tools()
    admitted = admit_validator_run(store, member, session, cwd, execution_id, lease,
                                  run_id=run_id, tool_identity=identity)
    with store.transaction() as db:
        record = store._option(db, 'validator_admission:' + run_id)
        if record['state'] != 'admitted':
            return _summary(run_id, store._option(db, _KEY + run_id), dispatched=False)
    command = host._prepare_restricted_validator_inputs(store, member, session, cwd,
        execution_id, lease, run_id=run_id, admitted=admitted, identity=identity, shell=shell)
    home = Path(codex_home).resolve()
    home.mkdir(parents=True, exist_ok=False)
    with _CommandServer(executable=executable, codex_home=home, cwd=host._PACKAGE_ROOT,
            backend='managed', track_producer=True, experimental_api=True) as client:
        launch = client.producer_launch
        if launch['version'] != _VERSION or launch['file']['sha256'] != _EXECUTABLE_SHA256:
            raise ValueError('Exact official command executor build required')
        verify_launch(launch)
        # 官方环境映射区分键大小写；与继承的 Windows 大写键保持一致。
        params = {'command': command['argv'], 'cwd': command['cwd'],
                  'env': {key.upper(): value for key, value in command['env'].items()},
                  'processId': 'validator-' + run_id, 'timeoutMs': 300000}
        _, current_identity = _current_tools()
        with validator_admission_boundary(store, member, session, cwd, execution_id, lease,
                run_id=run_id, tool_identity=current_identity, purpose='dispatch') as permission:
            if not permission['dispatch_allowed']:
                return _summary(run_id, None, dispatched=False)
            request_id = client.reserve_command()
            start = _save_start(store, run_id, execution_id, lease, permission,
                launch, client.connection_id, request_id, params)
            try:
                client.send_command(request_id, params)
            except DeliveryUnknown as error:
                unknown = str(error)
            else:
                unknown = None
        # 执行期间不持控制锁，暂停仍可登记；未知结果永不重新派发。
        if unknown is None:
            try:
                response = client.wait_command(request_id, timeout=315)
            except DeliveryUnknown as error:
                unknown = str(error)
        _, current_identity = _current_tools()
        with validator_admission_boundary(store, member, session, cwd, execution_id, lease,
                run_id=run_id, tool_identity=current_identity, purpose='reconcile') as permission:
            if unknown is None:
                verify_launch(launch)
            record = _save_final(store, start, permission,
                response=None if unknown else response, unknown=unknown,
                producer_final=None if unknown else launch, connection_id=client.connection_id)
        return _summary(run_id, record, dispatched=True)


def reconcile_owned_validator_run(store, member, session, cwd, execution_id, lease, *, run_id):
    """仅消费预登记命令的成功终态和同次受限结果，不补造执行或正式回执。"""
    _run_id(run_id)
    shell, identity = _current_tools()
    with validator_admission_boundary(store, member, session, cwd, execution_id, lease,
            run_id=run_id, tool_identity=identity, purpose='reconcile') as permission:
        before = _consumable_command(store, run_id, execution_id, lease, permission, shell, identity)
    result = host.reconcile_admitted_validator_run(store, member, session, cwd,
        execution_id, lease, run_id=run_id)
    shell, identity = _current_tools()
    with validator_admission_boundary(store, member, session, cwd, execution_id, lease,
            run_id=run_id, tool_identity=identity, purpose='reconcile') as permission:
        after = _consumable_command(store, run_id, execution_id, lease, permission, shell, identity)
        if after != before:
            raise ValueError('Owned command changed during restricted result reconciliation')
        with store.transaction() as db:
            validator = store._option(db, 'validator_run:' + run_id)
            if (not isinstance(validator, dict) or validator.get('run_id') != run_id
                    or validator.get('execution_id') != execution_id
                    or digest(validator.get('request')) != before['start']['input_digest']
                    or validator.get('tool_identity') != {key: value for key, value in identity.items()
                                                        if key != 'host_shell'}
                    or validator.get('formal_checks_verified') is not False
                    or validator.get('native', {}).get('HostObservation', {}).get('run_id') != run_id):
                raise ValueError('Owned command restricted result binding mismatch')
            consumption = {'schema_version': 1, 'run_id': run_id,
                'source_kind': 'official_command_exec', 'binding': before['start']['binding'],
                'host_record_digest': digest(before), 'validator_record_digest': digest(validator),
                'formal': False, 'formal_checks_verified': False}
            old = store._option(db, _CONSUMPTION_KEY + run_id)
            if old is not None and old != consumption:
                raise ValueError('Owned command consumption conflict; original binding preserved')
            if old is None:
                store._put(db, _CONSUMPTION_KEY + run_id, consumption)
                store.event(db, 'validator_host_command_consumed', run=run_id,
                    execution=execution_id, host_record_digest=consumption['host_record_digest'],
                    validator_record_digest=consumption['validator_record_digest'], formal=False)
    return dict(result, source_kind='official_command_exec', host_record_digest=digest(before),
        consumption_digest=digest(consumption), consumption_duplicate=old is not None,
        controls_changed=permission['controls_changed'], reconciliation_only=True,
        formal=False, formal_checks_verified=False, status='needs_validation')


class _CommandServer(AppServer):
    """仅拆开固定 command/exec 的发送与等待，不开放其他异步写接口。"""
    def __init__(self, **options):
        self.connection_id = uuid.uuid4().hex
        super().__init__(process_factory=subprocess.Popen, **options)

    def _process_environment(self, codex_home):
        return _official_environment(codex_home)

    def reserve_command(self):
        with self._condition:
            if self._dead:
                raise DeliveryUnknown('Owned command connection unavailable; do not replay')
            self._next_id += 1
            return self._next_id

    def send_command(self, request_id, params):
        try:
            self._write({'id': request_id, 'method': 'command/exec', 'params': params})
        except (OSError, ValueError) as error:
            raise DeliveryUnknown('Owned command write outcome unknown; do not replay') from error

    def wait_command(self, request_id, *, timeout):
        deadline = time.monotonic() + timeout
        with self._condition:
            while request_id not in self._pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._dead:
                    raise DeliveryUnknown('Owned command terminal unavailable; reconcile original run, do not replay')
                self._condition.wait(remaining)
            return self._pending.pop(request_id)


def _current_tools():
    source = host._PACKAGE_ROOT / 'validation/bytecode-provenance-20261105'
    inventory, _, hashes, _, tools, _ = host._diagnostic_sources(source)
    return host._admission_tools(source, inventory, hashes, tools)


def _official_environment(codex_home):
    # 空 home 不继承模型凭据、原会话身份或外部执行服务器。
    environment = control_environment(codex_home)
    for key in ('CODEX_EXEC_SERVER_URL', 'CODEX_AUTH_JSON'):
        environment.pop(key, None)
    return environment


def _start_official_process(argv, **options):
    options['env'] = _official_environment(options['env']['CODEX_HOME'])
    return subprocess.Popen(argv, **options)


def _consumable_command(store, run_id, execution_id, lease, permission, shell, identity):
    with store.transaction() as db:
        admission = store._option(db, 'validator_admission:' + run_id)
        record = store._option(db, _KEY + run_id)
    snapshot = admission['snapshot']
    if (admission['state'] != 'dispatching' or not isinstance(record, dict)
            or set(record) != {'start', 'start_digest', 'state', 'final'}
            or record['state'] != 'terminal' or not isinstance(record['start'], dict)
            or record['start_digest'] != digest(record['start'])):
        raise ValueError('Owned command successful preregistered terminal required; never replay')
    start = record['start']
    expected_fields = {'schema_version', 'source_kind', 'run_id', 'binding', 'authorizer',
        'tool_identity', 'input_digest', 'producer_start', 'connection_id', 'request_id',
        'command', 'native_child_start_verified', 'formal', 'formal_checks_verified'}
    binding = {'pair_id': store.pair_id, 'execution_id': execution_id,
        'lease_sha256': hashlib.sha256(lease.encode('utf-8')).hexdigest(),
        'admission_snapshot_digest': permission['snapshot_digest']}
    source = host._PACKAGE_ROOT / 'validation/bytecode-provenance-20261105'
    command = {'command': [str(shell), '-NoLogo', '-NoProfile', '-NonInteractive',
        '-ExecutionPolicy', 'RemoteSigned', '-File', str(source / 'Invoke-ValidatorRawModuleDiagnostic.ps1'),
        '-InputDirectory', str(host._RUN_ROOT.parent / 'prepared' / run_id),
        '-SourceInventory', str(source / 'raw-module-source-inventory-loaded-reader.json'),
        '-EvidenceProfile', 'loaded-reader-v2', '-RunId', run_id],
        'cwd': str(host._PACKAGE_ROOT), 'env': {'PSMODULEPATH': str(shell.parent / 'Modules')},
        'processId': 'validator-' + run_id, 'timeoutMs': 300000}
    if (set(start) != expected_fields or type(start['schema_version']) is not int
            or start['schema_version'] != 1 or start['source_kind'] != 'official_command_exec'
            or start['run_id'] != run_id or start['binding'] != binding
            or start['authorizer'] != snapshot['actor'] or start['tool_identity'] != identity
            or start['input_digest'] != digest(snapshot['request']) or start['command'] != command
            or start['native_child_start_verified'] is not False or start['formal'] is not False
            or start['formal_checks_verified'] is not False or type(start['request_id']) is not int
            or start['request_id'] <= 0 or not isinstance(start['connection_id'], str)
            or not 1 <= len(start['connection_id']) <= 128):
        raise ValueError('Owned command original admission, identity or fixed command mismatch')
    final = record['final']
    if (not isinstance(final, dict) or set(final) != {'response', 'unknown', 'producer_final',
            'connection_id', 'controls_changed', 'control_block', 'reconciliation_only'}
            or final['unknown'] is not None or final['connection_id'] != start['connection_id']
            or final['producer_final'] != start['producer_start']
            or final['reconciliation_only'] is not True):
        raise ValueError('Owned command original terminal connection or producer mismatch')
    response = final['response']
    if (not isinstance(response, dict) or set(response) != {'id', 'result'}
            or type(response['id']) is not int or response['id'] != start['request_id']
            or not isinstance(response['result'], dict)
            or set(response['result']) != {'exitCode', 'stdout', 'stderr'}
            or type(response['result']['exitCode']) is not int or response['result']['exitCode'] != 0
            or any(not isinstance(response['result'][name], str) for name in ('stdout', 'stderr'))):
        raise ValueError('Owned command successful exact response required')
    launch = start['producer_start']
    if (not isinstance(launch, dict) or set(launch) != {'version', 'file', 'process'}
            or launch['version'] != _VERSION or launch['file'].get('sha256') != _EXECUTABLE_SHA256
            or file_identity(launch['file']['path']) != launch['file']
            or launch['process'].get('path') != launch['file']['path']
            or any(type(launch['process'].get(name)) is not int or launch['process'][name] <= 0
                   for name in ('pid', 'created'))):
        raise ValueError('Owned command exact historical producer required')
    return record


def _save_start(store, run_id, execution_id, lease, permission, launch,
                connection_id, request_id, params):
    with store.transaction() as db:
        admission = store._option(db, 'validator_admission:' + run_id)
        snapshot = admission['snapshot']
        lease_sha = hashlib.sha256(lease.encode('utf-8')).hexdigest()
        if (admission['state'] != 'dispatching'
                or admission['snapshot_digest'] != permission['snapshot_digest']
                or snapshot['execution']['id'] != execution_id or snapshot['lease_sha256'] != lease_sha
                or store._option(db, _KEY + run_id) is not None):
            raise ValueError('Owned command start missing admission or already fixed')
        start = {'schema_version': 1, 'source_kind': 'official_command_exec', 'run_id': run_id,
            'binding': {'pair_id': store.pair_id, 'execution_id': execution_id,
                'lease_sha256': lease_sha, 'admission_snapshot_digest': permission['snapshot_digest']},
            'authorizer': snapshot['actor'], 'tool_identity': snapshot['tool_identity'],
            'input_digest': digest(snapshot['request']), 'producer_start': launch,
            'connection_id': connection_id, 'request_id': request_id, 'command': params,
            'native_child_start_verified': False, 'formal': False, 'formal_checks_verified': False}
        start = copy.deepcopy(start)
        record = {'start': start, 'start_digest': digest(start), 'state': 'dispatching', 'final': None}
        store._put(db, _KEY + run_id, record)
        store.event(db, 'validator_host_command_registered', run=run_id, execution=execution_id,
                    start_digest=record['start_digest'], formal=False)
        return start


def _save_final(store, start, permission, *, response, unknown, producer_final, connection_id):
    if (response is None) == (unknown is None):
        raise ValueError('Owned command requires one terminal response or unknown outcome')
    if (connection_id != start['connection_id']
            or response is not None and producer_final != start['producer_start']
            or response is None and producer_final is not None):
        raise ValueError('Owned command terminal producer or connection changed')
    if response is not None:
        if (not isinstance(response, dict) or response.get('id') != start['request_id']
                or set(response) not in ({'id', 'result'}, {'id', 'error'})):
            raise ValueError('Owned command terminal request mismatch')
        if 'result' in response:
            result = response['result']
            if (not isinstance(result, dict) or set(result) != {'exitCode', 'stdout', 'stderr'}
                    or type(result['exitCode']) is not int
                    or not isinstance(result['stdout'], str) or not isinstance(result['stderr'], str)):
                raise ValueError('Owned command terminal result invalid')
    final = {'response': response, 'unknown': unknown,
             'producer_final': producer_final,
             'connection_id': connection_id,
             'controls_changed': permission['controls_changed'],
             'control_block': permission['control_block'], 'reconciliation_only': True}
    with store.transaction() as db:
        record = store._option(db, _KEY + start['run_id'])
        if (not record or record['start'] != start or record['start_digest'] != digest(start)
                or permission['snapshot_digest'] != start['binding']['admission_snapshot_digest']):
            raise ValueError('Owned command original start missing or changed')
        if record['final'] is not None and record['final'] != final:
            raise ValueError('Owned command terminal conflict; original fact preserved')
        state = 'unknown' if unknown is not None else 'rejected' if 'error' in response else 'terminal'
        record.update(state=state, final=final)
        store._put(db, _KEY + start['run_id'], record)
        store.event(db, 'validator_host_command_observed', run=start['run_id'],
                    state=record['state'], formal=False)
        return record


def _summary(run_id, record, *, dispatched):
    final = record['final'] if record else None
    response = final['response'] if final else None
    result = response.get('result') if response else None
    return {'run_id': run_id, 'source_kind': 'official_command_exec', 'dispatched': dispatched,
        'replayed': False, 'state': record['state'] if record else 'reconciliation_required',
        'controller_exit_code': result['exitCode'] if result else None,
        'command_succeeded': result is not None and result['exitCode'] == 0,
        'controls_changed': final['controls_changed'] if final else [],
        'reconciliation_only': True, 'formal': False, 'formal_checks_verified': False,
        'status': 'needs_validation'}
