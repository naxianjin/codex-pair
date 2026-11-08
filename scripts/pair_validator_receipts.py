"""查询预登记宿主的准确工具回执；不启动执行器或授予正式结算。"""
try:
    from . import pair_validator_host as host
    from .pair_producer import producer_context, strict_version
    from .pair_review_receipts import receipt_items
    from .pair_reviewer import reviewer_identity
    from .pair_stages import digest
    from .pair_transport import AppServer, canonical_path, rollout_command_items
    from .pair_validator_admission import _record, _run_id, validator_admission_boundary
except ImportError:
    import pair_validator_host as host
    from pair_producer import producer_context, strict_version
    from pair_review_receipts import receipt_items
    from pair_reviewer import reviewer_identity
    from pair_stages import digest
    from pair_transport import AppServer, canonical_path, rollout_command_items
    from pair_validator_admission import _record, _run_id, validator_admission_boundary


_KEY = 'validator_host_execution:'


def register_validator_host_execution(store, member, session, cwd, execution_id, lease, *,
                                      run_id, turn_id, item_id, command,
                                      start_producer, final_producer):
    """在宿主执行入口登记一次不可变的 API/rollout 回执绑定。

    该入口只登记来源和生命周期，不启动命令，也不把结果标为正式通过；后续查询仍须
    从准确宿主 API 与 rollout 读取同一 turn/item 并复核终态。
    """
    _run_id(run_id)
    if (not isinstance(command, dict) or set(command) != {'argv', 'cwd'}
            or not isinstance(command['argv'], list) or not command['argv']
            or not isinstance(command['cwd'], str)):
        raise ValueError('Validator host command binding required')
    if (not isinstance(turn_id, str) or not 1 <= len(turn_id) <= 128
            or not isinstance(item_id, str) or not 1 <= len(item_id) <= 128):
        raise ValueError('Validator host turn and item binding required')
    before, permission, _ = _current_without_record(store, member, session, cwd,
                                                     execution_id, lease, run_id)
    record = {'schema_version': 1, 'run_id': run_id,
              'admission_snapshot_digest': before['admission']['snapshot_digest'],
              'command': command, 'turn_id': turn_id, 'item_id': item_id,
              'start_producer': start_producer, 'final_producer': final_producer}
    _host_record(store.pair_id, before['admission'], record, command)
    with store.transaction() as db:
        admission = store._option(db, 'validator_admission:' + run_id)
        _record(admission, run_id)
        old = store._option(db, _KEY + run_id)
        if old is not None and old != record:
            raise ValueError('Validator host execution registration conflict')
        if old is None:
            store._put(db, _KEY + run_id, record)
            store.event(db, 'validator_host_execution_registered', run=run_id,
                        execution=execution_id, turn=turn_id, item=item_id,
                        record_digest=digest(record), formal=False)
    return {'run_id': run_id, 'record_digest': digest(record),
            'duplicate': old is not None, 'formal': False,
            'formal_checks_verified': False, 'status': 'needs_validation',
            'controls_changed': permission['controls_changed']}


def _current_without_record(store, member, session, cwd, execution_id, lease, run_id):
    """复用准入边界读取当前 admission；登记前禁止读取旧宿主记录。"""
    source = host._PACKAGE_ROOT / 'validation/bytecode-provenance-20261105'
    inventory, _, hashes, _, tools, _ = host._diagnostic_sources(source)
    _, identity = host._admission_tools(source, inventory, hashes, tools)
    with validator_admission_boundary(store, member, session, cwd, execution_id, lease,
            run_id=run_id, tool_identity=identity, purpose='reconcile') as permission:
        with store.transaction() as db:
            admission = store._option(db, 'validator_admission:' + run_id)
            _record(admission, run_id)
            if admission['state'] != 'dispatching':
                raise ValueError('Validator host dispatch not registered')
        return {'admission': admission}, permission, identity


def read_validator_host_receipt(store, member, session, cwd, execution_id, lease, *,
                                run_id, observer_factory=AppServer):
    """只收原运行引用；来源、命令及生产者必须已有受信宿主登记。"""
    _run_id(run_id)
    before, permission, version = _current(store, member, session, cwd, execution_id, lease, run_id)
    actor, record = before['actor'], before['record']
    with observer_factory(codex_home=actor['home'], cwd=actor['cwd'], metadata_only=True,
            executable=record['final_producer']['launch']['file']['path']) as observer:
        thread = observer.read_thread(actor['session'])
        identity = reviewer_identity(thread)
        if (identity['id'] != actor['session'] or identity['cwd'] != actor['cwd']
                or identity['model'] != actor['model'] or identity['modelProvider'] != actor['provider']):
            raise ValueError('Validator host receipt identity mismatch')
        matches = [turn for turn in observer.turns(actor['session'], items_view='full')
                   if turn.get('id') == record['turn_id']]
        if (len(matches) != 1 or matches[0].get('threadId', actor['session']) != actor['session']
                or matches[0].get('status') != 'completed'):
            raise ValueError('Validator host turn unresolved; reconcile original run, never replay')
        turn = matches[0]
        if any(item.get('status') in ('inProgress', 'running') for item in turn.get('items', [])):
            raise ValueError('Validator host tool unresolved; reconcile original run, never replay')
        raw = rollout_command_items({'codex_home': actor['home'], 'cwd': actor['cwd'],
            'session_uuid': actor['session']}, thread, record['turn_id'], producer_version=version)
        receipts = receipt_items(turn.get('items', []), version, rollout_items=raw or [])
        matches = [item for item in receipts if item['id'] == record['item_id']]
        if (len(matches) != 1 or matches[0].get('command_argv') != before['command']['argv']
                or matches[0]['cwd'] != before['command']['cwd']):
            raise ValueError('Validator host exact command receipt missing or conflicting')
        receipt = matches[0]
    after, final_permission, _ = _current(store, member, session, cwd, execution_id, lease, run_id)
    if after != before:
        raise ValueError('Validator host binding changed during receipt query')
    changed = sorted(set(permission['controls_changed']) | set(final_permission['controls_changed']))
    return {'run_id': run_id, 'source': 'registered_host_official_api_and_rollout',
            'session': actor['session'], 'model_provider': actor['provider'],
            'turn_id': record['turn_id'], 'item_id': record['item_id'],
            'admission_snapshot_digest': record['admission_snapshot_digest'],
            'host_record_digest': digest(record), 'command_digest': digest(before['command']),
            'producer_version': version.version, 'producer_evidence_digest': version.evidence_digest,
            'receipt_status': receipt['status'], 'exit_code': receipt['exitCode'],
            'command_succeeded': receipt['status'] == 'completed' and receipt['exitCode'] == 0,
            'controls_changed': changed, 'reconciliation_only': True, 'replayed': False,
            'official_cli_receipt_verified': True, 'formal': False,
            'formal_checks_verified': False, 'status': 'needs_validation'}


def _current(store, member, session, cwd, execution_id, lease, run_id):
    source = host._PACKAGE_ROOT / 'validation/bytecode-provenance-20261105'
    inventory, _, hashes, _, tools, _ = host._diagnostic_sources(source)
    shell, identity = host._admission_tools(source, inventory, hashes, tools)
    with validator_admission_boundary(store, member, session, cwd, execution_id, lease,
            run_id=run_id, tool_identity=identity, purpose='reconcile') as permission:
        with store.transaction() as db:
            admission = store._option(db, 'validator_admission:' + run_id)
            _record(admission, run_id)
            if admission['state'] != 'dispatching':
                raise ValueError('Validator host dispatch not registered')
            record = store._option(db, _KEY + run_id)
        actor = admission['snapshot']['actor']
        command = {'argv': [str(shell), '-NoLogo', '-NoProfile', '-NonInteractive',
            '-ExecutionPolicy', 'RemoteSigned', '-File', str(source / 'Invoke-ValidatorRawModuleDiagnostic.ps1'),
            '-InputDirectory', str(host._RUN_ROOT.parent / 'prepared' / run_id),
            '-SourceInventory', str(source / 'raw-module-source-inventory-loaded-reader.json'),
            '-EvidenceProfile', 'loaded-reader-v2', '-RunId', run_id],
            'cwd': canonical_path(host._PACKAGE_ROOT)}
        version = _host_record(store.pair_id, admission, record, command)
        return {'actor': actor, 'record': record, 'command': command}, permission, version


def _host_record(pair_id, admission, record, command):
    # shortcut: 本模块只消费可信执行入口预登记；现有 Popen 诊断没有该入口，禁止事后自报补证。
    fields = {'schema_version', 'run_id', 'admission_snapshot_digest', 'command', 'turn_id',
              'item_id', 'start_producer', 'final_producer'}
    if (not isinstance(record, dict) or set(record) != fields
            or type(record['schema_version']) is not int or record['schema_version'] != 1
            or record['run_id'] != admission['run_id']
            or record['admission_snapshot_digest'] != admission['snapshot_digest']
            or record['command'] != command
            or any(not isinstance(record[key], str) or not 1 <= len(record[key]) <= 128
                   for key in ('turn_id', 'item_id'))):
        raise ValueError('Validator host execution registration unavailable or mismatched')
    actor = admission['snapshot']['actor']
    binding = {'pair': pair_id, 'run': admission['run_id'],
               'execution': admission['snapshot']['execution']['id'],
               'session': actor['session'], 'turn': record['turn_id'], 'item': record['item_id'],
               'home': actor['home'], 'cwd': actor['cwd'],
               'admission_digest': admission['snapshot_digest'], 'command_digest': digest(command)}
    start, final = record['start_producer'], record['final_producer']
    if (not isinstance(start, dict) or set(start) != {'binding', 'launch', 'finished'}
            or start['finished'] is not False or start['binding'] != binding
            or not isinstance(final, dict) or final != dict(start, finished=True)):
        raise ValueError('Validator host original producer lifecycle unavailable')
    version = producer_context(final, binding, live=False)
    if not strict_version(version):
        raise ValueError('Validator host strict producer required')
    return version
