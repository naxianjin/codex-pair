"""只读查询当前审核执行的官方工具项，不创建模型或修改协作库。"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import stat
from types import SimpleNamespace

try:
    from .pair_producer import KEY, producer_context, execution_binding, strict_version
except ImportError:
    from pair_producer import KEY, producer_context, execution_binding, strict_version

try:
    from .pair_transport import AppServer, command_receipt, merge_command_receipts, receipt_cwd, redact_diagnostic, rollout_command_items, same_review_directory
    from .pair_reviewer import reviewer_identity
except ImportError:
    from pair_transport import AppServer, command_receipt, merge_command_receipts, receipt_cwd, redact_diagnostic, rollout_command_items, same_review_directory
    from pair_reviewer import reviewer_identity


RECEIPTS_TOOL = {
    'name': 'pair_review_receipts',
    'description': 'Read completed command receipts for this exact current review execution; never grants approval.',
    'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False},
}


def handle_tool(directory, expected, params, *, observer_factory=AppServer):
    """固定空输入；请求身份与当前库绑定均须准确，不接受模型指定来源。"""
    try:
        if (not isinstance(params, dict) or params.get('tool') != RECEIPTS_TOOL['name']
                or type(params.get('arguments')) is not dict or params['arguments']):
            raise ValueError('Invalid fixed query input')
        stage, runtime, version, before = current_binding(directory, expected)
        if (params.get('threadId') != expected['session']
                or params.get('turnId') != stage['turn_id']):
            raise ValueError('Wrong dynamic query turn')
        result = read_receipts(directory, expected, observer_factory=observer_factory)
        if (current_binding(directory, expected)[3] != before
                or result['turn'] != params['turnId']):
            raise ValueError('Dynamic query binding changed')
        return {'success': True, 'contentItems': [{'type': 'inputText', 'text': json.dumps(result, ensure_ascii=True)}]}
    except Exception as exc:
        # 不返回原始异常文本、账号响应、路径或模型输入。
        return {'success': False, 'contentItems': [{'type': 'inputText', 'text': json.dumps({
            'ok': False, 'stage': 'receipt_query', 'error': type(exc).__name__,
            'detail': 'Current receipts unavailable; preserve needs_validation; do not guess IDs'})}]}


def current_binding(directory, expected):
    fields = {'pair', 'stage', 'digest', 'epoch', 'session', 'execution'}
    if (not isinstance(expected, dict) or set(expected) != fields or type(expected['epoch']) is not int
            or any(not isinstance(expected[key], str) or not 1 <= len(expected[key]) <= 128
                   for key in fields - {'epoch'})):
        raise ValueError('Invalid receipt binding')
    # 不使用Store构造器，避免查询时迁移、建库或写入。
    directory = Path(directory).absolute()
    path = directory / 'state.sqlite3'
    if (not same_review_directory(directory, directory) or path.is_symlink()
            or not path.is_file() or path.resolve() != path.absolute()):
        raise ValueError('Unsafe receipt state path')
    identity = path.stat()
    if getattr(identity, 'st_file_attributes', 0) & getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0):
        raise ValueError('Redirected receipt state file')
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        collaboration = dict(db.execute('SELECT * FROM collaborations').fetchone())
        stage = dict(db.execute('SELECT * FROM review_stages WHERE id=?', (expected['stage'],)).fetchone() or {})
        task = dict(db.execute('SELECT * FROM tasks WHERE id=?', (stage.get('task_id'),)).fetchone() or {})
        runtime = dict(db.execute('SELECT * FROM reviewer_runtime').fetchone() or {})
        parent = dict(db.execute("SELECT * FROM members WHERE id='A'").fetchone() or {})
        provider_row = db.execute("SELECT value FROM pair_settings WHERE key='reviewer_provider'").fetchone()
        provider = json.loads(provider_row[0]) if provider_row else 'openai'
        producer_row = db.execute("SELECT value FROM pair_settings WHERE key=?", (KEY+expected['execution'],)).fetchone()
    if (collaboration['id'] != expected['pair'] or collaboration['status'] != 'running'
            or directory != Path(collaboration['project']).absolute() / '.codex-pair' / 'state' / expected['pair']
            or not stage or stage['status'] != 'running' or not stage['turn_id']
            or not task or task['requirements_version'] != stage['requirements_version']
            or task['status'] not in ('review_wait', 'needs_fix', 'needs_validation')
            or stage['digest'] != expected['digest'] or runtime.get('state') != 'running'
            or runtime.get('epoch') != expected['epoch'] or stage['reviewer_epoch'] != expected['epoch']
            or stage['reviewer_session'] != runtime.get('session_uuid') or runtime.get('session_uuid') != expected['session']
            or stage['execution_id'] != runtime.get('execution_id') or runtime.get('execution_id') != expected['execution']
            or parent.get('session_uuid') != runtime.get('parent_session')
            or not parent.get('verified') or not parent.get('online') or parent.get('lifecycle') != 'active'):
        raise ValueError('Stale receipt execution or identity')
    try:
        from .pair_review_runtime import permission_binding_snapshot
    except ImportError:
        from pair_review_runtime import permission_binding_snapshot
    snapshot = permission_binding_snapshot(runtime, parent, stage['turn_id'])
    if (path.stat().st_dev, path.stat().st_ino) != (identity.st_dev, identity.st_ino):
        raise ValueError('Receipt state file changed')
    snapshot['state_file'] = (identity.st_dev, identity.st_ino)
    snapshot['stage'] = {k: stage[k] for k in ('id', 'digest', 'status', 'turn_id', 'requirements_version', 'payload')}
    snapshot['collaboration'] = collaboration
    snapshot['task'] = {k: task[k] for k in ('id', 'status', 'requirements_version')}
    runtime['modelProvider'] = provider
    snapshot['reviewer_provider'] = provider
    proof = json.loads(parent['proof'])
    if (provider not in ('openai','apinebula') or proof.get('modelProvider') != provider or proof.get('model') != runtime.get('model')
            or not same_review_directory(runtime['codex_home'], parent['codex_home'])):
        raise ValueError('Reviewer parent identity mismatch')
    record = json.loads(producer_row[0]) if producer_row else None
    snapshot['producer'] = record
    version = producer_context(record,execution_binding(SimpleNamespace(pair_id=expected['pair']),runtime,stage)) if record is not None else None
    return stage, runtime, version, snapshot


def receipt_items(items, version, *, rollout_items=None):
    if version is None:return []
    # 生产入口显式分开API和rollout，字段形状不能冒充另一来源。
    if rollout_items is not None:
        if strict_version(version):
            checked = []
            for source, group in (('display', items), ('raw', rollout_items)):
                for item in group:
                    value = command_receipt(item)
                    if value and ('raw' if value.get('command_argv') is not None else 'display') != source:
                        item = {'type': 'conflictingReceipt', 'id': value['id']}
                    checked.append(item)
            items = checked
        else:
            items = list(items) + list(rollout_items)
    receipts, conflicts = {}, set()
    representations = {}
    for item in items:
        if not isinstance(item, dict):continue
        key = item.get('id')
        if item.get('type') not in ('commandExecution', 'CommandExecution', 'conflictingReceipt'):
            if isinstance(key, str):conflicts.add(key)
            continue
        if not isinstance(key, str) or not key:
            return []
        if item['type'] == 'conflictingReceipt':
            # 持久事件已确认矛盾，该ID不能被任何来源或顺序的成功项复活。
            conflicts.add(key)
            continue
        value = command_receipt(item)
        if value:
            representations.setdefault(key,set()).add('raw' if value.get('command_argv') is not None else 'display')
        if value and key in receipts:
            value = merge_command_receipts(receipts[key], value, producer_version=version)
        if value is None:
            conflicts.add(key)
        else:
            receipts[key] = value
    return [value for key, value in receipts.items() if key not in conflicts and (not strict_version(version) or representations.get(key)=={'raw','display'})]


def verified_turn(directory, expected, *, observer_factory=AppServer):
    stage, runtime, version, before = current_binding(directory, expected)
    options = dict(codex_home=runtime['codex_home'],cwd=runtime['cwd'],metadata_only=True)
    if before['producer'] is not None:options['executable']=before['producer']['launch']['file']['path']
    with observer_factory(**options) as observer:
        thread = observer.read_thread(runtime['session_uuid'])
        reviewer_identity(thread)
        if (thread.get('modelProvider') != runtime['modelProvider']
                or thread.get('id') != runtime['session_uuid'] or thread.get('model') != runtime['model']
                or receipt_cwd(thread.get('cwd')) != receipt_cwd(runtime['cwd'])):
            raise ValueError('Official receipt thread mismatch')
        matches = [t for t in observer.turns(runtime['session_uuid'], items_view='full') if t.get('id') == stage['turn_id']]
        if len(matches) != 1 or matches[0].get('threadId', runtime['session_uuid']) != runtime['session_uuid']:
            raise ValueError('Missing unique official receipt turn')
        turn = dict(matches[0], threadId=runtime['session_uuid'], receipt_cwd=receipt_cwd(runtime['cwd']))
        member = {'codex_home': runtime['codex_home'], 'cwd': runtime['cwd'], 'session_uuid': runtime['session_uuid']}
        fallback = rollout_command_items(member, thread, stage['turn_id'], producer_version=version, require_terminal=False)
        turn['rollout_items'] = fallback or []
    if current_binding(directory, expected)[3] != before:
        raise ValueError('Receipt binding changed during read')
    return turn, version


def read_receipts(directory, expected, *, observer_factory=AppServer):
    turn, version = verified_turn(directory, expected, observer_factory=observer_factory)
    # 仅暴露必要的脱敏终态工具字段；候选文件和查询输出都不能成为最终信任源。
    records = []
    for item in receipt_items(turn['items'], version, rollout_items=turn['rollout_items']):
        value = command_receipt(item)
        if value is None or value['status'] != 'completed' or value['cwd'] != turn['receipt_cwd']:
            continue
        if len(json.dumps(value)) > 16000:
            raise ValueError('Receipt item too large')
        if redact_diagnostic(value) != value:
            continue
        records.append(value)
    if len(records) > 128:
        raise ValueError('Too many receipt items')
    if len(json.dumps(records)) > 64000:
        raise ValueError('Receipt view too large')
    return dict(expected, turn=turn['id'], receipts=records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-directory', required=True)
    parser.add_argument('--binding', required=True, help='当前提示中的固定JSON绑定')
    args = parser.parse_args()
    try:
        print(json.dumps(read_receipts(args.state_directory, json.loads(args.binding)), ensure_ascii=True))
    except Exception as exc:
        # 不输出原始异常参数或账号响应。
        print(json.dumps({'ok': False, 'error': type(exc).__name__, 'detail': 'Current official receipts unavailable; do not guess IDs'}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
