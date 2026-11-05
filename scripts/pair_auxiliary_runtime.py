"""同一消息桥中的辅助池控制器；静态审阅不执行候选，动态验证保留独立门禁。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import stat
import threading
import time

try:
    from .pair_transport import AppServer, TransportError, canonical_path, command_matches, receipt_cwd, rollout_command_items, redact_diagnostic
    from .pair_producer import verify_launch, producer_context
    from .pair_review_receipts import receipt_items
    from .pair_review_runtime import prepare_review_tree, materialize_snapshot, verify_review_bytes, tree_stamp, git, _terminal_review_report
    from .pair_reviewer import reviewer_identity, verify_review_service, fail
    from .pair_stages import encoded, digest
    from .pair_audit_adapter import adapt_report, load_assets
    from . import pair_hidden_handoff as hidden_handoff
except ImportError:
    from pair_transport import AppServer, TransportError, canonical_path, command_matches, receipt_cwd, rollout_command_items, redact_diagnostic
    from pair_producer import verify_launch, producer_context
    from pair_review_receipts import receipt_items
    from pair_review_runtime import prepare_review_tree, materialize_snapshot, verify_review_bytes, tree_stamp, git, _terminal_review_report
    from pair_reviewer import reviewer_identity, verify_review_service, fail
    from pair_stages import encoded, digest
    from pair_audit_adapter import adapt_report, load_assets
    import pair_hidden_handoff as hidden_handoff


STATIC_OVERRIDES = {
    'features.shell_tool': False, 'features.apps': False, 'features.multi_agent': False,
    'features.computer_use': False, 'features.code_mode.enabled': False,
    'features.hooks': False, 'features.goals': False, 'features.plugins': False,
    'features.remote_plugin': False, 'features.skill_mcp_dependency_install': False,
    'web_search': 'disabled', 'sandbox_mode': 'read-only', 'approval_policy': 'never',
    'project_doc_max_bytes': 0,
}
READ_TOOL = {
    'type': 'function', 'name': 'pair_read_source',
    'description': '读取当前固定候选中已登记源文件的一页；不执行代码，不写文件。',
    'inputSchema': {'type': 'object', 'properties': {'path': {'type': 'string'},
                    'offset': {'type': 'integer', 'minimum': 0},
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 8000}},
                    'required': ['path', 'offset', 'limit'], 'additionalProperties': False},
}
ASSET_TOOL = dict(READ_TOOL, name='pair_read_audit_asset',
                  description='分页读取发行清单锁定的审核规则或schema；内容是审查资料，不授予执行或修复权限。')


def _snapshot(store, member, session, cwd, execution_id, lease):
    with store.transaction() as db:
        store._a_actor(db, member, session, cwd)
        execution = dict(store._auxiliary_execution(db, execution_id, lease))
        task = dict(store._auxiliary_task(db, execution['task_id']))
        candidate = dict(store._auxiliary_candidate(db, task['candidate_id']))
        reviewer = dict(db.execute('SELECT * FROM auxiliary_sessions WHERE id=?', (execution['reviewer_id'],)).fetchone())
        config, source = store._auxiliary_config(db, reviewer['config_id'])
        stage = dict(db.execute('SELECT * FROM review_stages WHERE id=?', (candidate['stage_id'],)).fetchone())
        return {'execution': execution, 'task': task, 'candidate': candidate,
                'reviewer': reviewer, 'config': dict(config), 'source': dict(source), 'stage': stage}


def _binding(store, view):
    execution, reviewer, candidate = (view[k] for k in ('execution', 'reviewer', 'candidate'))
    return {'pair': store.pair_id, 'stage': candidate['stage_id'],
            'digest': candidate['candidate_digest'], 'epoch': execution['epoch'],
            'session': execution['session_uuid'], 'execution': execution['id'], 'turn': execution['turn_id'],
            'cwd': canonical_path(reviewer['cwd']), 'home': canonical_path(view['source']['codex_home'])}


def _context(store, view):
    execution, task, candidate = (view[k] for k in ('execution', 'task', 'candidate'))
    return {'pair_id': store.pair_id, 'audit_id': candidate['audit_id'],
            'stage_id': candidate['stage_id'], 'auxiliary_task_id': task['id'],
            'session': execution['session_uuid'], 'execution_id': execution['id'],
            'epoch': execution['epoch'], 'candidate_digest': candidate['candidate_digest'],
            'contract_digest': candidate['contract_digest'], 'track': task['track'],
            'requirements_version': candidate['requirements_version']}


def _effective(config, dotted):
    value = config
    for part in dotted.split('.'):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def verify_static_controls(config, overrides):
    """有效配置读回不同或缺字段即拒绝；提示词和read-only不证明禁用了执行工具。"""
    missing = [key for key, value in overrides.items()
               if type(_effective(config, key)) is not type(value) or _effective(config, key) != value]
    servers = config.get('mcp_servers', {})
    if not isinstance(servers, dict):
        missing.append('mcp_servers')
    else:
        missing.extend('mcp_servers.' + name + '.enabled' for name, server in servers.items()
                       if not isinstance(server, dict) or server.get('enabled', True))
    if missing:
        fail('Static tools not verified / 静态会话工具限制未被当前CLI确认：' + ','.join(sorted(set(missing))))
    return {'mode': 'static', 'verified_keys': sorted(overrides), 'config_digest': digest(overrides)}


def _static_client(source, cwd, executable):
    # 第一条连接不创建模型会话；读取配置只为了准确禁用该配置已有的MCP入口。
    with AppServer(codex_home=source['codex_home'], cwd=cwd, executable=executable,
                   config_overrides=STATIC_OVERRIDES) as probe:
        config = probe.rpc('config/read', {'cwd': cwd, 'includeLayers': False})['config']
        overrides = dict(STATIC_OVERRIDES)
        for name in config.get('mcp_servers', {}):
            if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
                fail('Unsupported MCP config name / 无法准确禁用此配置中的MCP名称')
            overrides['mcp_servers.' + name + '.enabled'] = False
    client = AppServer(codex_home=source['codex_home'], cwd=cwd, executable=executable,
                       config_overrides=overrides, experimental_api=True, track_producer=True)
    try:
        config = client.rpc('config/read', {'cwd': cwd, 'includeLayers': False})['config']
        controls = verify_static_controls(config, overrides)
        actual = reviewer_identity(client.read_thread(source['session_uuid']))
        expected = reviewer_identity(json.loads(source['proof']))
        if actual != expected:
            fail('Auxiliary source changed / 原允许配置的真实身份已改变')
        account_pool = verify_review_service(client, actual, source['codex_home'], require_account_id=True)
        return client, controls, account_pool
    except BaseException:
        client.close()
        raise


def _source_manifest(root, artifact):
    verify_review_bytes(root, snapshot=artifact.get('manifest') if artifact['kind'] == 'snapshot' else None)
    files = {name for name in git(root, 'ls-files', '-z').split('\0') if name}
    if artifact['kind'] == 'snapshot':
        for name, checksum in artifact['manifest'].items():
            if checksum is None:
                files.discard(name)
            else:
                files.add(name)
    result = {}
    for name in sorted(files):
        path = Path(root) / name
        if path.resolve() != path.absolute() or not path.resolve().is_relative_to(Path(root).resolve()):
            fail('Source redirected / 固定候选包含重定向路径')
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def read_source_page(root, manifest, arguments):
    if not isinstance(arguments, dict) or set(arguments) != {'path', 'offset', 'limit'}:
        raise ValueError('Invalid source page')
    name, offset, limit = (arguments[k] for k in ('path', 'offset', 'limit'))
    if not isinstance(name, str) or name not in manifest or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 8000:
        raise ValueError('Invalid source page')
    root = Path(root).resolve(strict=True)
    path = root / name
    if path.resolve() != path.absolute() or not path.resolve().is_relative_to(root):
        raise ValueError('Source path redirected')
    # 候选从未运行；仍核对同一打开文件的身份和摘要，拒绝用户在审阅中改变该输入。
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > 4 * 1024 * 1024:
            raise ValueError('Unsupported source file')
        data = stream.read(4 * 1024 * 1024 + 1)
        after = os.fstat(stream.fileno())
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or hashlib.sha256(data).hexdigest() != manifest[name]):
        raise ValueError('Candidate source changed')
    content = data.decode('utf-8')
    if offset > len(content):
        raise ValueError('Source page outside document')
    end = min(len(content), offset + limit)
    return {'path': name, 'sha256': manifest[name], 'offset': offset, 'body': redact_diagnostic(content[offset:end]),
            'next_offset': end if end < len(content) else None}


def begin_execution(store, member, session, cwd, execution_id, lease, *, executable=None, stop_event=None):
    """同步拥有一次受管执行；退出前留准确终态，不能把启动RPC成功当交付。"""
    view = _snapshot(store, member, session, cwd, execution_id, lease)
    if view['execution']['state'] != 'reserved':
        fail('Auxiliary execution unavailable / 不重复开始已在途或未知执行')
    with store.transaction() as db:
        store._auxiliary_dispatchable(db,task_id=view['stage']['task_id'])
        config,source=store._auxiliary_config(db,view['reviewer']['config_id'])
        store._auxiliary_quota(db,config,source)
    if view['task']['requires_dynamic']:
        return {'started': False, 'status': 'needs_validation', 'reason': 'hyperv_environment_unverified',
                'execution_id': execution_id, 'reservation_preserved': True}
    assets = load_assets()
    asset_manifest = {name: checksum for name, checksum in assets['manifest'].items()
                      if name.startswith('assets/') and Path(name).suffix in ('.json', '.md')}
    artifact = json.loads(view['stage']['artifact'])
    head = artifact.get('head') or artifact.get('base') or json.loads(view['stage']['payload']).get('baseline')
    if not isinstance(head, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', head):
        fail('Auxiliary baseline unavailable / 缺准确Git基准，不能猜测候选')
    reviewer_id = view['reviewer']['id']
    root = prepare_review_tree(store, head, reviewer_id=reviewer_id)
    if artifact['kind'] == 'snapshot':
        materialize_snapshot(store, root, artifact, reviewer_id=reviewer_id)
    manifest = _source_manifest(root, artifact)
    before = tree_stamp(root)
    with store.transaction() as db:
        store._a_actor(db, member, session, cwd)
        current = store._auxiliary_execution(db, execution_id, lease)
        store._auxiliary_dispatchable(db, task_id=view['stage']['task_id'])
        if current['state'] != 'reserved':
            fail('Auxiliary start changed / 准备后执行预留已改变')
        db.execute('UPDATE auxiliary_sessions SET cwd=? WHERE id=? AND epoch=?', (canonical_path(root), reviewer_id, current['epoch']))
    client = None
    rpc_started = False
    terminal = None
    try:
        client, controls, account_pool = _static_client(view['source'], root, executable)
        verify_launch(client.producer_launch)
        with store.transaction() as db:
            current = store._auxiliary_execution(db, execution_id, lease)
            reviewer = db.execute('SELECT * FROM auxiliary_sessions WHERE id=?', (reviewer_id,)).fetchone()
            store._auxiliary_dispatchable(db, task_id=view['stage']['task_id'])
            if current['state'] != 'reserved' or reviewer['state'] != 'reserved':
                fail('Auxiliary creation changed / 已有创建在途，不重复创建')
            db.execute("UPDATE auxiliary_sessions SET state='starting',updated=? WHERE id=? AND epoch=?", (time.time(), reviewer_id, current['epoch']))
            db.execute('UPDATE auxiliary_executions SET producer=?,updated=? WHERE id=?',
                       (encoded({'launch': client.producer_launch, 'controls': controls}), time.time(), execution_id))
        params = {'cwd': root, 'model': view['reviewer']['model'], 'modelProvider': json.loads(view['config']['binding'])['provider'],
                  'sandbox': 'read-only', 'approvalPolicy': 'never', 'ephemeral': False,
                  'dynamicTools': [READ_TOOL, ASSET_TOOL, hidden_handoff.HANDOFF_TOOL], 'developerInstructions':
                  '你是独立静态审核者。仅用pair_read_source阅读固定候选及给定材料；不执行候选，不修改产品，不提交、合并、安装、发布或委派。'
                  '输出有证据的结构化报告。每个问题最多一句可选修复方向，不给补丁或实现步骤。'
                  '需要动态验证或缺资料时准确记录needs_validation；历史结论只作待核查主张。'}
        rpc_started = True
        if view['reviewer']['session_uuid']:
            params.pop('ephemeral')
            params.pop('dynamicTools')  # resume恢复原已持久化的动态工具，不发送schema未声明字段。
            params['threadId'] = view['reviewer']['session_uuid']
            response = client.rpc('thread/resume', params)
        else:
            params['serviceName'] = 'codex-pair:' + store.pair_id + ':auxiliary:' + view['reviewer']['creation_id']
            response = client.rpc('thread/start', params)
        if response.get('sandbox', {}).get('type') != 'readOnly' or response.get('approvalPolicy') != 'never':
            fail('Static sandbox not confirmed / CLI未确认只读及禁止提权策略')
        proof = client.read_thread(response['thread']['id'])
        identity = reviewer_identity(proof)
        preparation=store.setting('auxiliary_preparation_ready:'+execution_id)
        prepared=bool(preparation and preparation['session']==identity['id'] and preparation['epoch']==view['execution']['epoch'])
        if view['task']['role'] in ('validation', 'final_verification', 'conflict_review') and not prepared and next(client.turns(identity['id']),None) is not None:
            fail('Fresh review context required / 新独立审核上下文已含旧回合')
        with store.transaction() as db:
            store._a_actor(db, member, session, cwd)
            store._begin_auxiliary_verified(db, execution_id, lease, identity, account_pool,
                                           {'launch': client.producer_launch, 'controls': controls,
                                            'manifest': manifest, 'root': root, 'before': before})
        view = _snapshot(store, member, session, cwd, execution_id, lease)
        subject={'kind':'auxiliary','id':reviewer_id,'session':identity['id'],'epoch':view['execution']['epoch']}
        handoff=hidden_handoff.adopt(store,subject,view['reviewer']['config_id'],view['task']['id'],view['candidate']['id'],execution_id)
        if handoff:hidden_handoff.consume_with_client(store,client,subject,handoff)
        context = _context(store, view)
        units = [u for u in json.loads(view['candidate']['coverage_units'])
                 if u['track'] == view['task']['track'] and u['id'] in json.loads(view['task']['unit_ids'])]
        material = {'context': context, 'role': view['task']['role'], 'coverage_units': units,
                    'candidate_files': manifest, 'requirements': json.loads(view['stage']['payload']),
                    'audit_assets': asset_manifest, 'ocr_delegate_executed': False,
                    'upstream_assets_are_reference_material': True,
                    'report_fields': ['context', 'verdict', 'coverage', 'findings', 'checks'],
                    'limits': {'target_execution': False, 'formal_checks': 'not_run', 'patches': False}}
        # 控制事件可能在元数据查询时到达，紧邻真正模型请求再次检查。
        with store.transaction() as db:
            store._auxiliary_dispatchable(db, task_id=view['stage']['task_id'])
            current = store._auxiliary_execution(db, execution_id, lease)
            if current['state'] != 'starting':
                fail('Auxiliary admission changed / 当前执行已不能启动新回合')
        turn = client.rpc('turn/start', {'threadId': identity['id'],
                                        'input': [{'type': 'text', 'text': encoded(material)}]})['turn']
        with store.transaction() as db:
            store._record_auxiliary_turn(db, execution_id, lease, turn['id'])
        interrupted = False
        last_progress = time.monotonic()
        reminded = False
        while terminal is None:
            while True:
                try:
                    request = client.server_requests.get_nowait()
                except queue.Empty:
                    break
                params = request.get('params', {})
                if request.get('method')=='item/tool/call' and params.get('tool')==hidden_handoff.HANDOFF_TOOL['name']:
                    try:
                        if params.get('threadId')!=identity['id'] or params.get('turnId')!=turn['id']:fail('Exact auxiliary turn required')
                        output=hidden_handoff.handle_tool(store,subject,params.get('arguments'),execution=execution_id)
                        reply={'success':True,'contentItems':[{'type':'inputText','text':encoded(output)}]}
                    except Exception as exc:reply={'success':False,'contentItems':[{'type':'inputText','text':redact_diagnostic(str(exc))[:1000]}]}
                    client._write({'id':request['id'],'result':reply});continue
                if (request.get('method') == 'item/tool/call' and params.get('tool') in (READ_TOOL['name'], ASSET_TOOL['name'])
                        and params.get('threadId') == identity['id'] and params.get('turnId') == turn['id']):
                    with store.transaction() as db:
                        gate = store._recovery_gate(db, member='A', task_id=view['stage']['task_id'], action='auxiliary')
                    try:
                        if gate:
                            raise ValueError('Control pause active')
                        source_root, source_manifest = ((root, manifest) if params['tool'] == READ_TOOL['name']
                                                        else (assets['root'], asset_manifest))
                        page = read_source_page(source_root, source_manifest, params.get('arguments'))
                        reply = {'success': True, 'contentItems': [{'type': 'inputText', 'text': encoded(page)}]}
                        last_progress = time.monotonic()
                        reminded = False
                    except (ValueError, OSError, UnicodeError) as error:
                        reply = {'success': False, 'contentItems': [{'type': 'inputText', 'text': redact_diagnostic(str(error))}]}
                    client._write({'id': request['id'], 'result': reply})
                else:
                    # 审核者不具备普通工具权限，此入口不转发成A的业务授权。
                    client._write({'id': request['id'], 'error': {'code': -32601, 'message': 'Unsupported static auxiliary operation'}})
            with store.transaction() as db:
                gate = store._recovery_gate(db, member='A', task_id=view['stage']['task_id'], action='auxiliary')
                running = db.execute('SELECT status FROM collaborations').fetchone()[0] == 'running'
                enabled = store._auxiliary_policy(db)['enabled']
                handoff=hidden_handoff.pending(store,db,subject)
            if (gate or not running or not enabled or handoff and handoff['mode']=='restart' or stop_event is not None and stop_event.is_set()) and not interrupted:
                client.rpc('turn/interrupt', {'threadId': identity['id'], 'turnId': turn['id']})
                interrupted = True
            try:
                event = client.notifications.get(timeout=.25)
            except queue.Empty:
                if client._dead:
                    raise TransportError('Auxiliary transport closed / 原回合终态未知')
                event = {}
            params = event.get('params', {})
            if event.get('method') == 'turn/completed' and params.get('threadId') == identity['id'] and params.get('turn', {}).get('id') == turn['id']:
                terminal = params['turn']
            if not reminded and time.monotonic() - last_progress >= 600:
                with store.transaction() as db:
                    store.event(db, 'auxiliary_no_progress', execution=execution_id, action='investigate_only')
                    store._send(db, 'A', 'A', '辅助审核十分钟无可核验进展，仅核实原执行：' + execution_id,
                                kind='blocked', dedupe_key='auxiliary-stalled:' + execution_id)
                reminded = True
        view = _snapshot(store, member, session, cwd, execution_id, lease)
        observed, version, receipts = _observe_execution(store, view, client=client)
        if observed['status'] != 'completed':
            with store.transaction() as db:
                current = store._auxiliary_execution(db, execution_id, lease)
                store._end_auxiliary(db, current, 'suspended', {'turn_id': turn['id'], 'status': observed['status'], 'source': 'official_turn'})
                db.execute("UPDATE auxiliary_tasks SET status='waiting',waiting=? WHERE id=? AND last_execution=?",
                           (encoded({'reason': 'control_or_model_interruption', 'remaining_steps_only': True}), current['task_id'], execution_id))
            hidden_handoff.seal(store,subject,receipt=encoded({'turn':turn['id'],'receipts':receipts,'status':observed['status']}))
            return {'started': True, 'status': 'waiting', 'execution_id': execution_id, 'replayed': False}
        report = _terminal_review_report(observed)
        if tree_stamp(root) != before:
            fail('Static candidate changed / 审阅期间候选内容变化，保留现场待核实')
        verify_review_bytes(root, snapshot=artifact.get('manifest') if artifact['kind'] == 'snapshot' else None)
        _save_final_producer(store, member, session, cwd, execution_id, lease, client)
        result=finish_execution(store, member, session, cwd, execution_id, lease, report,executable=executable,client=client)
        hidden_handoff.seal(store,subject,receipt=encoded({'turn':turn['id'],'receipts':receipts,'status':observed['status']}))
        return result
    except Exception as error:
        if rpc_started:
            store.auxiliary_unknown(member, session, cwd, execution_id, lease, redact_diagnostic(str(error))[:2000] or type(error).__name__)
        raise
    finally:
        if client is not None:
            # 候选执行工具已禁用；关闭本控制器的连接，不触及其他账号窗口。
            client.close()


def _save_final_producer(store, member, session, cwd, execution_id, lease, client):
    verify_launch(client.producer_launch)
    view = _snapshot(store, member, session, cwd, execution_id, lease)
    record = json.loads(view['execution']['producer'])
    if record['launch'] != client.producer_launch:
        fail('Auxiliary producer changed / 结束回执不是原受管进程')
    record['binding'] = _binding(store, view)
    record['finished'] = True
    record['formal'] = {'binding': record['binding'], 'launch': record['launch'], 'finished': True}
    producer_context(record['formal'], record['binding'], live=True)
    with store.transaction() as db:
        current = store._auxiliary_execution(db, execution_id, lease)
        if current['state'] != 'running' or current['turn_id'] != view['execution']['turn_id']:
            fail('Auxiliary terminal changed / 最终生产者证据绑定已变化')
        db.execute('UPDATE auxiliary_executions SET producer=? WHERE id=?', (encoded(record), execution_id))


def _observe_execution(store, view, *, client=None, executable=None, allow_unverified_producer=False):
    execution = view['execution']
    if not execution['session_uuid'] or not execution['turn_id'] or not execution['producer']:
        fail('Auxiliary receipt unavailable / 启动结果未知且缺准确会话或回合，不能猜测或重放')
    producer = json.loads(execution['producer'])
    expected = _binding(store, view)
    try:
        if 'formal' in producer:
            version = producer_context(producer['formal'], expected, live=False)
        else:
            verify_launch(producer['launch'])
            version = producer_context({'binding': expected, 'launch': producer['launch'], 'finished': False}, expected, live=True)
    except (ValueError, OSError):
        if not allow_unverified_producer:
            raise
        # 崩溃后的官方终态仍能解除准确执行占用；没有最终生产者证据绝不放行阶段。
        version = None
    owned = client is None
    if owned:
        client = AppServer(codex_home=view['source']['codex_home'], cwd=view['reviewer']['cwd'],
                           executable=executable or producer['launch']['file']['path'], metadata_only=True)
    try:
        thread = client.read_thread(execution['session_uuid'])
        identity = reviewer_identity(thread)
        if (identity['modelProvider'] != json.loads(view['config']['binding'])['provider']
                or identity['id'] != execution['session_uuid'] or identity['model'] != view['reviewer']['model']
                or identity['cwd'] != canonical_path(view['reviewer']['cwd'])):
            fail('Auxiliary receipt identity changed / 原准确会话身份不符')
        matches = [t for t in client.turns(execution['session_uuid'], items_view='full') if t.get('id') == execution['turn_id']]
        if len(matches) != 1 or matches[0].get('threadId', execution['session_uuid']) != execution['session_uuid']:
            fail('Auxiliary receipt turn ambiguous / 缺唯一准确回合终态')
        turn = matches[0]
        if turn.get('status') not in ('completed', 'interrupted', 'failed'):
            fail('Auxiliary execution unresolved / 回合仍在运行或终态未知')
        items = turn.get('items', [])
        if any(item.get('status') in ('inProgress', 'running') for item in items):
            fail('Auxiliary tool unresolved / 回合终态不能覆盖仍在运行的工具')
        bound = {'codex_home': view['source']['codex_home'], 'cwd': view['reviewer']['cwd'], 'session_uuid': execution['session_uuid']}
        raw = rollout_command_items(bound, thread, execution['turn_id'], producer_version=version, require_terminal=True)
        receipts = receipt_items(items, version, rollout_items=raw or [])
        return turn, version, receipts
    finally:
        if owned:
            client.close()


def finish_execution(store, member, session, cwd, execution_id, lease, report, *, executable=None, client=None,
                     allow_unverified_producer=False):
    before = _snapshot(store, member, session, cwd, execution_id, lease)
    if before['execution']['state'] == 'completed':
        with store.transaction() as db:
            old = db.execute('SELECT report_digest FROM auxiliary_reports WHERE execution_id=?', (execution_id,)).fetchone()
            if old and old[0] == digest(report):
                return {'saved': True, 'duplicate': True, 'stage_passed': False}
        fail('Auxiliary report already fixed / 不得覆盖原报告')
    turn, version, receipts = _observe_execution(store, before, client=client, executable=executable,
                                                 allow_unverified_producer=allow_unverified_producer)
    if turn['status'] != 'completed' or _terminal_review_report(turn) != report:
        fail('Auxiliary report source mismatch / 报告须等于准确官方终态中的最终报告，不能提交任意JSON')
    task = before['task']
    units = [u for u in json.loads(before['candidate']['coverage_units'])
             if u['track'] == task['track'] and u['id'] in json.loads(task['unit_ids'])]
    sanitized = redact_diagnostic(report)
    adapted = adapt_report(sanitized, context=_context(store, before), expected_units=units)
    required = json.loads(before['stage']['payload']).get('coordinator_required_checks', [])
    verified = bool(required)
    checked = []
    for check in required:
        matches = [item for item in receipts if item['status'] == 'completed' and item['exitCode'] == 0
                   and item['cwd'] == receipt_cwd(before['reviewer']['cwd'])
                   and command_matches(check.get('command', ''), item, producer_version=version)]
        if len(matches) != 1:
            verified = False
        else:
            checked.append({'name': check.get('name'), 'receipt_id': matches[0]['id']})
    # CJS报告/覆盖校验也需准确执行证据；适配器预检不替代完整校验器。
    if adapted['validation'].get('required_validations'):
        verified = False
    producer = json.loads(before['execution']['producer'])
    if not producer.get('finished') or 'formal' not in producer:
        verified = False
    evidence = {'source': 'official_turn_and_rollout', 'turn_id': turn['id'],
                'original_report_digest': digest(report), 'stored_report_redacted': sanitized != report,
                'terminal_status': turn['status'], 'receipt_ids': [r['id'] for r in receipts],
                'checks': checked, 'formal_checks_verified': verified,
                'candidate_digest': before['candidate']['candidate_digest'],
                'contract_digest': before['candidate']['contract_digest'],
                'needs_validation': [] if verified else ['required_checks_or_schema_validator_receipts_missing']}
    with store.transaction() as db:
        store._a_actor(db, member, session, cwd)
        current = store._auxiliary_execution(db, execution_id, lease)
        if any(current[k] != before['execution'][k] for k in ('state', 'turn_id', 'candidate_digest', 'contract_digest', 'session_uuid', 'producer')):
            fail('Auxiliary receipt changed / 查询期间执行或候选改变')
        return store._save_auxiliary_report(db, current, sanitized, adapted['adapted_report'], adapted['validation'], evidence)


def reconcile_execution(store, member, session, cwd, execution_id, lease, *, executable=None):
    view = _snapshot(store, member, session, cwd, execution_id, lease)
    if view['execution']['state'] not in ('starting', 'running', 'unknown'):
        return {'reconciled': False, 'state': view['execution']['state'], 'replayed': False}
    turn, _, _ = _observe_execution(store, view, executable=executable, allow_unverified_producer=True)
    if turn['status'] == 'completed':
        return finish_execution(store, member, session, cwd, execution_id, lease,
                                _terminal_review_report(turn), executable=executable, allow_unverified_producer=True)
    with store.transaction() as db:
        store._a_actor(db, member, session, cwd)
        current = store._auxiliary_execution(db, execution_id, lease)
        if current['turn_id'] != view['execution']['turn_id'] or current['state'] != view['execution']['state']:
            fail('Auxiliary reconciliation changed / 准确执行已发生变化')
        store._end_auxiliary(db, current, 'suspended', {'turn_id': turn['id'], 'status': turn['status'], 'source': 'official_turn'})
        db.execute("UPDATE auxiliary_tasks SET status='waiting',waiting=? WHERE id=? AND last_execution=?",
                   (encoded({'reason': 'reconciled_interruption', 'remaining_steps_only': True}), current['task_id'], execution_id))
        return {'reconciled': True, 'state': 'waiting', 'replayed': False, 'budget_refunded': False}


class AuxiliaryController:
    """桥只能选择这一控制器或旧ReviewerController，不同时启动二者。"""
    def __init__(self, store, *, executable=None):
        self.store, self.executable = store, executable
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.thread = threading.Thread(target=self.run, name='codex-pair-auxiliary', daemon=True)

    def start(self):
        self.thread.start()

    def close(self):
        self.stop_event.set()
        self.wake.set()
        while self.thread.is_alive():
            self.thread.join(timeout=1)

    def run(self):
        try:
            from .pair_core import FileLock
        except ImportError:
            from pair_core import FileLock
        futures = {}
        try:from .pair_autoscale import resource_sample,autoscale_tick
        except ImportError:from pair_autoscale import resource_sample,autoscale_tick
        previous_sample=None;next_sample=0
        with FileLock(self.store.directory / 'reviewer-process.lock', timeout=0), ThreadPoolExecutor(max_workers=4) as workers:
            while not self.stop_event.is_set():
                if time.monotonic()>=next_sample:
                    previous_sample=resource_sample(previous_sample)
                    autoscale_tick(self.store,previous_sample)
                    next_sample=time.monotonic()+30
                for execution, future in list(futures.items()):
                    if future.done():
                        try:
                            future.result()
                        except Exception as error:
                            with self.store.transaction() as db:
                                self.store.event(db, 'auxiliary_controller_blocked', execution=execution,
                                                 reason=redact_diagnostic(str(error))[:2000], automatic_replay=False)
                        del futures[execution]
                with self.store.transaction() as db:
                    policy = self.store._auxiliary_policy(db)
                    actor = dict(self.store._member(db, 'A'))
                    tasks = [dict(row) for row in db.execute("SELECT * FROM auxiliary_tasks WHERE status IN ('pending','waiting') ORDER BY CASE role WHEN 'conflict_review' THEN 0 WHEN 'final_verification' THEN 1 WHEN 'validation' THEN 2 ELSE 3 END,created,id")]
                    configs = [row[0] for row in db.execute('SELECT id FROM auxiliary_configs WHERE enabled=1 ORDER BY created,id')]
                    prepared=[dict(row) for row in db.execute("SELECT e.* FROM auxiliary_executions e JOIN pair_settings p ON p.key='auxiliary_preparation_ready:'||e.id WHERE e.state='reserved'")]
                if policy['enabled']:
                    for execution in prepared:
                        if self.stop_event.is_set() or len(futures)>=policy['concurrency']:break
                        if execution['id'] not in futures:
                            try:
                                with self.store.transaction() as db:
                                    task=self.store._auxiliary_task(db,execution['task_id']);candidate=self.store._auxiliary_candidate(db,task['candidate_id'])
                                    origin=db.execute('SELECT task_id FROM review_stages WHERE id=?',(candidate['stage_id'],)).fetchone()[0]
                                    self.store._auxiliary_dispatchable(db,task_id=origin)
                                    reviewer=db.execute('SELECT config_id FROM auxiliary_sessions WHERE id=?',(execution['reviewer_id'],)).fetchone()
                                    config,source=self.store._auxiliary_config(db,reviewer['config_id']);self.store._auxiliary_quota(db,config,source)
                                    if self.store._auxiliary_active(db)>policy['concurrency']:continue
                                    attempt=self.store._option(db,'auxiliary_preparation_dispatch:'+execution['id'],{})
                                    quota=self.store._quotas(db).get(source['id'],{})
                                    context=digest({'task':dict(task),'candidate':candidate['candidate_digest'],'generation':self.store._control_generation(db),'config':config['binding'],
                                        'quota':{key:quota.get(key) for key in ('reliability','remaining','observed_at','limit_id','pool','windows')}})
                                    if attempt.get('context')==context or attempt.get('count',0)>=3:continue
                                    self.store._put(db,'auxiliary_preparation_dispatch:'+execution['id'],{'context':context,'count':attempt.get('count',0)+1})
                            except Exception as error:
                                with self.store.transaction() as db:
                                    reason=redact_diagnostic(str(error))[:1000];key='auxiliary_preparation_blocker:'+execution['id']
                                    if self.store._option(db,key)!=reason:self.store._put(db,key,reason);self.store.event(db,'auxiliary_preparation_waiting',execution=execution['id'],reason=reason)
                                continue
                            futures[execution['id']]=workers.submit(begin_execution,self.store,'A',actor['session_uuid'],actor['cwd'],execution['id'],execution['lease'],executable=self.executable,stop_event=self.stop_event)
                    for task in tasks:
                        if self.stop_event.is_set() or len(futures) >= policy['concurrency']:
                            break
                        # 动态验证缺环境时不占满静态分析的计算槽；义务仍保留。
                        if task['requires_dynamic']:
                            continue
                        admitted = False
                        for config in configs:
                            try:
                                ticket = self.store.reserve_auxiliary('A', actor['session_uuid'], actor['cwd'], task['id'], config)
                            except Exception as error:
                                with self.store.transaction() as db:
                                    reason = redact_diagnostic(str(error))[:2000]
                                    key = 'auxiliary_blocker:' + task['id'] + ':' + config
                                    if self.store._option(db, key) != reason:
                                        self.store._put(db, key, reason)
                                        self.store.event(db, 'auxiliary_admission_waiting', task=task['id'], config=config, reason=reason)
                                continue
                            futures[ticket['execution_id']] = workers.submit(begin_execution, self.store,
                                'A', actor['session_uuid'], actor['cwd'], ticket['execution_id'], ticket['lease'], executable=self.executable,
                                stop_event=self.stop_event)
                            admitted = True
                            break
                        if not admitted:
                            continue
                self.wake.wait(.5)
                self.wake.clear()
