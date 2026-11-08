"""可见成员控制事件增量观察；仅读取官方返回的准确会话日志路径。"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import time

try:
    from .pair_core import FileLock, PairError
    from .pair_recovery import VerifiedRecoveryEvidence, digest
    from .pair_transport import AppServer, TransportError, canonical_path, owned_rollout_path, redact_diagnostic
except ImportError:
    from pair_core import FileLock, PairError
    from pair_recovery import VerifiedRecoveryEvidence, digest
    from pair_transport import AppServer, TransportError, canonical_path, owned_rollout_path, redact_diagnostic


MAX_RECORD_BYTES = 4 * 1024 * 1024
MAX_POLL_RECORDS = 128


class ControlObserver:
    def __init__(self, store, executable=None):
        self.store = store
        self.executable = executable
        self.observers = {}

    def poll(self):
        with self.store.transaction() as db:
            members = [row[0] for row in db.execute("SELECT id FROM members WHERE lifecycle<>'retired' ORDER BY id")]
        for member in set(self.observers) - set(members):
            self.observers.pop(member).close()
        results = {}
        for member in members:
            if member not in self.observers:
                self.observers[member] = _MemberObserver(self.store, self.executable, member)
            observer = self.observers[member]
            results[member] = observer.poll()
        return results

    def close(self):
        for observer in self.observers.values():
            observer.close()
        self.observers.clear()


class _MemberObserver:
    def __init__(self, store, executable=None, member_id='A'):
        self.store = store
        self.executable = executable
        self.member_id = member_id
        self.state_key = 'control_observer:' + member_id
        self.retry_key = 'control_observer_retry:' + member_id
        self.client = None
        self.binding = None
        self.thread = None

    def close(self):
        if self.client is not None:
            self.client.close()
            self.client = None

    def poll(self):
        """由桥在审核控制器和派发之前调用；本函数不创建或继续模型会话。"""
        with self.store.transaction() as db:
            row = db.execute('SELECT * FROM members WHERE id=?', (self.member_id,)).fetchone()
            if not row or row['lifecycle'] == 'retired':
                return {'observed': False, 'reason': 'original_member_unavailable'}
            member = dict(row)
        binding = (member['session_uuid'], member['binding_version'], member['codex_home'], member['cwd'])
        try:
            if binding != self.binding:
                self.close()
                self.binding = binding
                self.thread = None
            if self.client is None:
                with self.store.transaction() as db:
                    retry = self.store._option(db, self.retry_key, {})
                    if retry:
                        if not self.store._recovery_policy_valid(db, 'transport'):
                            return {'observed': False, 'reason': 'transport_recovery_disabled', 'new_work_gated': True}
                        if retry['attempts'] >= 3 or time.time() < retry['next_attempt']:
                            return {'observed': False, 'reason': 'observer_reconnect_wait', 'new_work_gated': True,
                                    'attempts': retry['attempts'], 'next_attempt': retry['next_attempt']}
                        retry = dict(retry, attempts=retry['attempts'] + 1)
                        self.store._put(db, self.retry_key, retry)
                self.client = AppServer(codex_home=member['codex_home'], cwd=member['cwd'],
                                        executable=self.executable, metadata_only=True)
                self.client.__enter__()
            if self.thread is None:
                self.thread = self.client.validate(member)
            path = owned_rollout_path(member, self.thread)
            result = self._read_member(member, path)
            if result.get('observed'):
                with self.store.transaction() as db:
                    if self.store._option(db, self.retry_key):
                        self.store._put(db, self.retry_key, {})
            return result
        except (OSError, ValueError, TypeError, KeyError, TransportError, PairError) as exc:
            self.close()
            self.thread = None
            with self.store.transaction() as db:
                retry = self.store._option(db, self.retry_key, {})
                attempts = retry.get('attempts', 0)
                self.store._put(db, self.retry_key, {'attempts': attempts,
                    'next_attempt': time.time() + (5, 15, 30, 30)[min(attempts, 3)],
                    'session': member['session_uuid'], 'binding_version': member['binding_version']})
            return self._gap(member, 'member_control_source_unavailable', redact_diagnostic(str(exc)))

    def _read_member(self, member, path):
        """从已校验的准确路径读取，不执行日志正文中的任何指令。"""
        with path.open('rb') as stream:
            stat = Path(path).stat()
            opened = os.fstat(stream.fileno())
            if (stat.st_dev, stat.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError('Control source changed during open / 打开时控制来源已替换')
            identity = digest({'path': canonical_path(path), 'device': opened.st_dev, 'inode': opened.st_ino})
            first = stream.readline(MAX_RECORD_BYTES + 1)
            if not first.endswith(b'\n') or len(first) > MAX_RECORD_BYTES:
                raise ValueError('Incomplete control session metadata / 会话元数据未完整落盘')
            meta = json.loads(first)
            if (not isinstance(meta, dict) or meta.get('type') != 'session_meta'
                    or not isinstance(meta.get('payload'), dict)
                    or meta['payload'].get('id') != member['session_uuid']
                    or canonical_path(meta['payload'].get('cwd', '')) != canonical_path(member['cwd'])):
                raise ValueError('Control session identity mismatch / 控制日志不属于原 A 绑定')
            metadata_end = stream.tell()
            with self.store.transaction() as db:
                cursor = db.execute('''SELECT * FROM control_event_cursors WHERE member=? AND session=?
                    AND binding_version=? AND source_identity=?''', (member['id'], member['session_uuid'], member['binding_version'], identity)).fetchone()
                prior = self.store._option(db, self.state_key)
                any_source = db.execute('SELECT 1 FROM control_event_cursors WHERE member=?', (member['id'],)).fetchone()
            if cursor is None:
                if any_source or prior and prior.get('source_identity'):
                    return self._gap(member, 'control_source_or_binding_changed', 'Control source identity changed / 日志轮转、替换或身份变化须核对新事件边界', identity)
                position = self._complete_boundary(stream, opened.st_size, metadata_end)
                anchor = self._anchor(stream, position)
                prefix_digest = self._prefix_digest(stream, position)
                with FileLock(self.store.control_lock, timeout=30), self.store.transaction() as db:
                    self._same_binding(db, member)
                    # 两个启动入口竞争时，只有先登记的入口建立首次基准。
                    if db.execute('SELECT 1 FROM control_event_cursors WHERE member=?', (member['id'],)).fetchone():
                        return {'observed': False, 'reason': 'control_baseline_already_registered'}
                    result = self.store._ingest_recovery_control(db, member['id'], member['session_uuid'], member['binding_version'],
                                                                identity, position, {}, initial_baseline=True)
                    self.store._put(db, self.state_key, self._state(member, identity, position, anchor, 'ready', prefix_digest))
                    self.store.event(db, 'control_observer_baseline', member=member['id'], source_identity=identity, position=position,
                                     history_replayed=False, binding_version=member['binding_version'])
                return dict(result, observed=True, historical_interruptions_replayed=False)
            position = cursor['position']
            if opened.st_size < position or position < metadata_end:
                return self._gap(member, 'control_source_truncated', 'Control source truncated / 日志已截断，不能跳过缺失控制事件', identity)
            if not prior or prior.get('source_identity') != identity or prior.get('position') != position:
                return self._gap(member, 'control_cursor_state_mismatch', 'Control cursor metadata mismatch / 游标与来源核验记录不一致', identity)
            if self._anchor(stream, position) != prior['anchor']:
                return self._gap(member, 'control_source_rewritten', 'Control source content changed / 已读边界内容变化，保留门禁等待核对', identity)
            if not prior.get('prefix_digest'):
                return self._gap(member, 'control_prefix_integrity_unavailable', 'Control source prefix digest unavailable / 已读控制前缀缺少完整摘要，保留门禁等待重建边界', identity)
            if self._prefix_digest(stream, position) != prior['prefix_digest']:
                return self._gap(member, 'control_source_rewritten', 'Control source content changed / 已读控制前缀内容变化，保留门禁等待核对', identity)
            if prior.get('state') == 'gap' and prior.get('reason') != 'member_control_source_unavailable':
                return {'observed': False, 'reason': prior['reason'], 'requires_explicit_rebaseline': True}
            stream.seek(position)
            records = []
            api_turns = None
            for _ in range(MAX_POLL_RECORDS):
                start = stream.tell()
                line = stream.readline(MAX_RECORD_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_RECORD_BYTES:
                    return self._gap(member, 'control_record_too_large', 'Control record exceeds bound / 单条控制记录超过读取上限', identity)
                if not line.endswith(b'\n'):
                    stream.seek(start)
                    break
                record = json.loads(line)
                if (isinstance(record, dict) and record.get('type') == 'response_item' and isinstance(record.get('payload'), dict)
                        and record['payload'].get('role') == 'user' and self.client is not None
                        and api_turns is None):
                    api_turns = list(self.client.turns(member['session_uuid'], items_view='full'))
                event = self._control_event(record, member['id'], api_turns=api_turns)
                records.append((start, stream.tell(), hashlib.sha256(line).hexdigest(), event))
                if stream.tell() - position >= MAX_RECORD_BYTES:
                    break
            end = stream.tell()
            current = os.fstat(stream.fileno())
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino) or current.st_size < end:
                return self._gap(member, 'control_source_changed_while_reading', 'Control source changed during read / 读取中来源发生变化', identity)
            if not records and prior.get('state') != 'gap':
                return {'observed': True, 'records': 0, 'partial_record_preserved': end < current.st_size}
            anchor = self._anchor(stream, end)
            pause_ids = []
            with FileLock(self.store.control_lock, timeout=30), self.store.transaction() as db:
                self._same_binding(db, member)
                saved = db.execute('''SELECT position FROM control_event_cursors WHERE member=? AND session=?
                    AND binding_version=? AND source_identity=?''', (member['id'], member['session_uuid'], member['binding_version'], identity)).fetchone()
                if not saved or saved['position'] != position:
                    return {'observed': False, 'reason': 'control_cursor_advanced_concurrently'}
                for start, stop, line_digest, event in records:
                    user_key = None
                    if event.get('api_item_id'):
                        user_key = 'control_user_message:' + digest([member['session_uuid'], event['turn_id'], event['api_item_id']])
                        if self.store._option(db, user_key):
                            event = {'kind': 'duplicate_user_control'}
                    terminal_key = None
                    if event.get('kind') == 'coordinator_interrupted':
                        terminal_key = 'control_terminal:' + digest([member['session_uuid'], event['turn_id']])
                        previous_terminal = self.store._option(db, terminal_key)
                        if previous_terminal:
                            if previous_terminal['event_digest'] != digest(event):
                                raise ValueError('Conflicting coordinator terminal / 原 A 同一回合存在矛盾中断终态')
                            event = {'kind': 'duplicate_terminal'}
                    context = {'pair': self.store.pair_id, 'member': member['id'], 'session': member['session_uuid'],
                               'binding_version': member['binding_version'], 'source_identity': identity,
                               'from_position': start, 'position': stop, 'event': event}
                    facts = {'exact_source_verified': True, 'complete_record': True,
                             'confirmed_coordinator_interruption': event.get('kind') == 'coordinator_interrupted',
                             'confirmed_user_pause': False}
                    proof = VerifiedRecoveryEvidence('control_event', digest(context),
                                                      ('rollout:' + identity + ':' + str(start) + ':' + line_digest,), facts)
                    result = self.store._ingest_recovery_control(db, member['id'], member['session_uuid'], member['binding_version'],
                                                                identity, stop, event, verified_evidence=proof)
                    if event.get('kind') == 'quota_user_instruction':
                        self.store._record_quota_user_event(db, member['id'], event['text'],
                            event_id='rollout:' + identity + ':' + str(start) + ':' + line_digest,
                            source_identity=identity, session=member['session_uuid'], binding_version=member['binding_version'])
                    elif event.get('kind') == 'auxiliary_quota_user_instruction':
                        self.store._record_auxiliary_quota_user_event(db, member['id'], event['text'],
                            event_id='rollout:' + identity + ':' + str(start) + ':' + line_digest,
                            source_identity=identity, session=member['session_uuid'], binding_version=member['binding_version'])
                    elif event.get('kind') == 'direct_user_control':
                        self.store._record_user_control(db, member['id'], event['text'],
                            event_id='rollout:' + identity + ':' + str(start) + ':' + line_digest,
                            source_identity=identity, session=member['session_uuid'], binding_version=member['binding_version'])
                    if user_key:
                        self.store._put(db, user_key, {'source_identity': identity, 'position': stop})
                    if event.get('kind') == 'coordinator_interrupted':
                        pause_ids.append({'turn_id': event['turn_id'], 'generation': result['generation']})
                        self.store._put(db, terminal_key, {'event_digest': digest(event), 'source_identity': identity,
                                                         'position': stop, 'generation': result['generation']})
                self.store._put(db, self.state_key, self._state(member, identity, end, anchor, 'ready', self._prefix_digest(stream, end)))
            return {'observed': True, 'records': len(records), 'interruptions': pause_ids,
                    'all_executors_stopped': False, 'user_key_proven': False}

    def _gap(self, member, reason, error, source_identity=None):
        with FileLock(self.store.control_lock, timeout=30), self.store.transaction() as db:
            current = db.execute('SELECT session_uuid,binding_version FROM members WHERE id=?', (member['id'],)).fetchone()
            if not current or tuple(current) != (member['session_uuid'], member['binding_version']):
                return {'observed': False, 'reason': 'binding_changed_during_observation'}
            previous = self.store._option(db, self.state_key, {})
            fingerprint = digest({'reason': reason, 'error': error, 'binding': member['binding_version'], 'source_identity': source_identity})
            if previous.get('gap_digest') != fingerprint:
                saved = dict(previous, state='gap', reason=reason, error=error, gap_digest=fingerprint,
                             observed_at=time.time(), session=member['session_uuid'], binding_version=member['binding_version'])
                if source_identity and not saved.get('source_identity'):
                    saved['source_identity'] = source_identity
                self.store._put(db, self.state_key, saved)
                self.store.event(db, 'control_observation_gap', member=member['id'], reason=reason, error=error,
                                 source_identity=source_identity, user_pause_proven=False)
            return {'observed': False, 'reason': reason, 'error': error, 'new_work_gated': True,
                    'user_pause_proven': False}

    @staticmethod
    def _control_event(record, member_id='A', *, api_turns=None):
        if not isinstance(record, dict):
            raise ValueError('Invalid control record / 日志记录不是对象')
        if record.get('type') == 'response_item':
            payload = record.get('payload')
            if not isinstance(payload, dict) or payload.get('type') != 'message' or payload.get('role') != 'user':
                return {'kind': 'non_control'}
            content = payload.get('content')
            metadata = payload.get('internal_chat_message_metadata_passthrough')
            if (not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict)
                    or content[0].get('type') != 'input_text' or not isinstance(content[0].get('text'), str)
                    or not isinstance(metadata, dict) or metadata.get('content_item_kinds') != ['user.text']):
                return {'kind': 'non_control'}
            attribution = metadata.get('content_item_metadata')
            if (not isinstance(attribution, list) or len(attribution) != 1 or not isinstance(attribution[0], dict)
                    or attribution[0].get('provenance') != {'type': 'user'}
                    or any(source.get(key) for source in (record, payload, metadata, attribution[0], content[0])
                           for key in ('images', 'local_images', 'attachments', 'forwarded', 'quoted',
                                       'harness_injected', 'source_tool_namespace'))):
                return {'kind': 'non_control'}
            text = content[0]['text']
            event = _MemberObserver._control_event({'type': 'event_msg', 'payload': {'type': 'user_message', 'message': text}}, member_id)
            if event['kind'] == 'non_control':
                return event
            turn_id = metadata.get('turn_id')
            created = metadata.get('create_time')
            if not isinstance(turn_id, str) or not turn_id or type(created) not in (int, float) or not isinstance(record.get('timestamp'), str):
                raise ValueError('Incomplete original user evidence / 新格式用户事件缺少回合或时间证据')
            timestamp = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00'))
            if timestamp.tzinfo is None or int(timestamp.timestamp() * 1000) != int(created * 1000):
                raise ValueError('User timestamp mismatch / 新格式用户事件时间不一致')
            # raw 与公开 API 的消息 ID 不同，按原会话内的准确回合、唯一正文及毫秒时间关联。
            matches = [item for turn in api_turns or () if turn.get('id') == turn_id
                       for item in turn.get('items', []) if item.get('type') == 'userMessage'
                       and isinstance(item.get('content'), list) and len(item['content']) == 1
                       and isinstance(item['content'][0], dict) and item['content'][0].get('type') in ('text', 'inputText')
                       and item['content'][0].get('text') == text
                       and type(item.get('startedAtMs')) is int and item['startedAtMs'] == round(created * 1000)]
            if len(matches) != 1 or not isinstance(matches[0].get('id'), str) or not matches[0]['id']:
                raise ValueError('Original API user message required / 新格式用户事件尚无唯一原 API 关联')
            return dict(event, turn_id=turn_id, api_item_id=matches[0]['id'])
        if record.get('type') != 'event_msg':
            return {'kind': 'non_control'}
        payload = record.get('payload')
        if not isinstance(payload, dict):
            raise ValueError('Invalid control payload / 事件正文不是对象')
        if payload.get('type') == 'user_message':
            text = payload.get('message')
            if not isinstance(text, str) or any(payload.get(key) for key in ('images', 'local_images', 'attachments', 'forwarded', 'quoted')):
                return {'kind': 'non_control'}
            if text.strip() in ('忽略额度', '恢复额度保护'):
                return {'kind': 'quota_user_instruction', 'text': text.strip()}
            if member_id == 'A' and (text.strip() == '恢复辅助额度保护' or re.fullmatch(r'忽略辅助额度 [0-9a-f]{32}', text.strip())):
                return {'kind': 'auxiliary_quota_user_instruction', 'text': text.strip()}
            if text.strip() in ('继续','继续原任务','继续协作','暂停','停一下','全部暂停','暂停协作','暂停所有工作') or re.fullmatch(r'(继续|暂停)协作 [A-Za-z0-9][A-Za-z0-9_.-]{0,63}',text.strip()):
                return {'kind': 'direct_user_control', 'text': text.strip()}
            return {'kind': 'non_control'}
        if payload.get('type') != 'turn_aborted' or member_id != 'A':
            return {'kind': 'non_control'}
        turn = payload.get('turn_id'); timestamp = record.get('timestamp')
        if not isinstance(turn, str) or not turn or not isinstance(timestamp, str):
            raise ValueError('Incomplete interrupted turn / 中断记录缺少准确回合或时间')
        parsed = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError('Ambiguous interruption timestamp / 中断时间没有时区')
        reason = payload.get('reason')
        if reason is not None and not isinstance(reason, str):
            raise ValueError('Invalid interruption reason / 中断原因类型无效')
        return {'kind': 'coordinator_interrupted', 'turn_id': turn, 'timestamp': timestamp,
                'reported_reason': reason, 'user_key_proven': False}

    @staticmethod
    def _same_binding(db, member):
        current = db.execute('SELECT session_uuid,binding_version,cwd,codex_home,lifecycle FROM members WHERE id=?', (member['id'],)).fetchone()
        if (not current or current['lifecycle'] == 'retired' or any(current[key] != member[key]
                for key in ('session_uuid', 'binding_version', 'cwd', 'codex_home'))):
            raise ValueError('Member binding changed / 读取期间成员绑定已变化')

    @staticmethod
    def _state(member, identity, position, anchor, state, prefix_digest=None):
        return {'session': member['session_uuid'], 'binding_version': member['binding_version'],
                'source_identity': identity, 'position': position, 'anchor': anchor, 'state': state,
                'observed_at': time.time(), 'tool_boundary_verified': False,
                **({'prefix_digest': prefix_digest} if prefix_digest else {})}

    @staticmethod
    def _anchor(stream, position):
        current = stream.tell()
        start = max(0, position - 256)
        stream.seek(start)
        data = stream.read(position - start)
        stream.seek(current)
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _prefix_digest(stream, position):
        current = stream.tell()
        stream.seek(0)
        value = hashlib.sha256()
        remaining = position
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError('Control source prefix truncated / 控制来源前缀已截断')
            value.update(chunk)
            remaining -= len(chunk)
        stream.seek(current)
        return value.hexdigest()

    @staticmethod
    def _complete_boundary(stream, size, metadata_end):
        start = max(metadata_end, size - MAX_RECORD_BYTES)
        stream.seek(start)
        tail = stream.read(size - start)
        newline = tail.rfind(b'\n')
        if newline >= 0:
            return start + newline + 1
        if size - metadata_end >= MAX_RECORD_BYTES:
            raise ValueError('Initial control tail too large / 首次日志尾部没有可核验的完整记录边界')
        return metadata_end
