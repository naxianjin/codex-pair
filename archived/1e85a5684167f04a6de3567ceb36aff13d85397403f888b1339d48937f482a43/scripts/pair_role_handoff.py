"""按进度让位：先冻结派发、保存文档，A读完确认后才释放绑定。"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import uuid
from pathlib import Path

try:
    from .pair_reviewer import fail, safe_evidence
    from .pair_transport import canonical_path
except ImportError:
    from pair_reviewer import fail, safe_evidence
    from pair_transport import canonical_path


TERMINAL = ('completed', 'integrated', 'cancelled', 'handed_back')
DELIVERED = TERMINAL + ('delivered', 'review_wait', 'needs_fix', 'needs_validation')


class RoleHandoffMixin:
    def _role_handoff(self, db, key):
        value = self._option(db, 'role_handoff:' + key)
        if not isinstance(value, dict):
            fail('Unknown handoff / 交接记录不存在')
        return value

    def _role_handoff_pending(self, db, target):
        key = self._option(db, 'role_handoff_current:' + target)
        if not key:
            return None
        record = self._role_handoff(db, key)
        return record if record['state'] in ('requested', 'saved') else None

    def _handoff_tasks(self, db, target):
        if target == 'reviewer':
            return []
        return [dict(r) for r in db.execute('SELECT * FROM tasks WHERE assignee=? ORDER BY id', (target,))]

    def _handoff_actor(self, db, actor, record, *, a_only=False):
        who = self._binding(db, *actor)
        if a_only:
            self._a_actor(db, *actor)
        elif who['id'] not in ('A', record['target']):
            fail('Wrong handoff actor / 仅原窗口或A可登记交接')
        if record['target'] != 'reviewer':
            source = self._member(db, record['target'])
            if source['session_uuid'] != record['session'] or source['binding_version'] != record['version']:
                fail('Stale handoff binding / 交接不能作用于替换后的新窗口')
        else:
            source = self._reviewer_row(db)
            if not source or source['epoch'] != record['version'] or source['session_uuid'] != record['session']:
                fail('Stale reviewer handoff / 审核交接代次已变化')
        return who

    def _handoff_snapshot(self, db, record):
        tasks = self._handoff_tasks(db, record['target'])
        task_fields = ('id', 'title', 'status', 'allowed_paths', 'base_commit', 'commits_json',
                       'requirements_version', 'task_kind', 'acceptance', 'required_checks')
        source = self._member(db, record['target']) if record['target'] != 'reviewer' else None
        return {
            'pair': self.pair_id, 'source': record['target'], 'session': record['session'],
            'binding_or_epoch': record['version'], 'mode': record['mode'],
            'progress': record['progress'], 'progress_evidence': record['evidence'],
            'requirements_version': self._option(db, 'requirements_version', 1),
            'source_workspace': (json.loads(source['proof']).get('git', {}).get('path') or source['cwd']) if source else None,
            'source_identity': {k:json.loads(source['proof']).get(k) for k in ('modelProvider','model','accountFingerprint')} if source else None,
            'tasks': [{k: t[k] for k in task_fields} for t in tasks],
            'messages': [dict(r) for r in db.execute(
                "SELECT id,sender,recipient,kind,task_id,process_status,delivery_status,remote_status FROM messages "
                "WHERE (sender=? OR recipient=?) AND process_status<>'completed' ORDER BY created",
                (record['target'], record['target']))],
            'stages': [dict(r) for r in db.execute(
                "SELECT s.id,s.task_id,s.digest,s.status,s.version FROM review_stages s JOIN tasks t ON t.id=s.task_id "
                "WHERE (?='reviewer' AND s.status<>'passed') OR t.assignee=? ORDER BY s.created",
                (record['target'], record['target']))],
            'approvals': [dict(r) for r in db.execute(
                "SELECT id,member,task_id,status FROM approval_requests WHERE member=? AND status IN ('pending','allowed','granted','unknown')",
                (record['target'],))],
            'reviewer': self._reviewer_status(db)['reviewer'] if record['target'] == 'reviewer' else None,
            'task_policy': {t['id']: self._option(db, 'task_execution_policy:' + t['id'], {}) for t in tasks},
        }

    def request_role_handoff(self, member, session, cwd, *, target, progress, evidence):
        safe_evidence(evidence)
        if not isinstance(evidence, str) or not evidence.strip():
            fail('Evidence required / 需要用户指令和进度依据')
        if target == 'A':
            fail('A is fixed / 主角色A不能让位或退出')
        if target not in ('B', 'C', 'D', 'reviewer'):
            fail('Invalid handoff target / 只能让出B/C/D或隐藏审核者')
        if progress is not None and (isinstance(progress, bool) or not isinstance(progress, (int, float))
                                     or not math.isfinite(progress) or not 0 <= progress <= 100):
            fail('Invalid progress / 进度必须为0到100的有限数值或null')
        with self.transaction() as db:
            self._binding(db, member, session, cwd)
            if member not in ('A', target):
                fail('Wrong actor / 只能本人或A请求让位')
            old = self._role_handoff_pending(db, target)
            if old:
                if old['progress'] != progress or old['evidence'] != evidence:
                    fail('Handoff request changed / 已有交接，不能覆盖原进度和依据')
                return dict(old, duplicate=True)
            if target == 'reviewer':
                source = self._reviewer_row(db)
                if not source or source['state'] == 'retired' or not source['session_uuid']:
                    fail('Reviewer unavailable / 先核实审核会话身份与创建结果')
                version, sid = source['epoch'], source['session_uuid']
            else:
                source = self._member(db, target)
                if source['lifecycle'] == 'retired':
                    fail('Retired member / 原窗口已经退出')
                version, sid = source['binding_version'], source['session_uuid']
                db.execute("UPDATE members SET lifecycle='draining' WHERE id=?", (target,))
            key = uuid.uuid4().hex
            record = {'id': key, 'target': target, 'session': sid, 'version': version,
                      'progress': progress, 'evidence': evidence, 'state': 'requested',
                      'mode': 'undetermined' if progress is None else ('restart' if progress <= 50 else 'finish_current'),
                      'created': time.time(), 'reads': {}}
            # 只准收尾请求时已存在的当前任务，不能把后来的任务算作“超过一半”。
            record['current_tasks'] = [t['id'] for t in self._handoff_tasks(db, target)
                                       if t['status'] not in DELIVERED + ('pending',)]
            self._put(db, 'role_handoff:' + key, record)
            self._put(db, 'role_handoff_current:' + target, key)
            self._send(db, member, 'A', '窗口请求让位，停止新派发；按进度保存交接。handoff=' + key,
                       kind='note', dedupe_key='role-handoff-request:' + key)
            if target != 'reviewer':
                self._send(db, 'A', target, '用户要求让位；不领新业务。核对进度，在安全边界用save-role-handoff保存文档。handoff=' + key,
                           kind='note', dedupe_key='role-handoff-prepare:' + key)
            self.event(db, 'role_handoff_requested', handoff=key, target=target, progress=progress)
            return record

    def _handoff_unresolved(self, db, record, exclude=()):
        target = record['target']
        if target == 'reviewer':
            source = self._reviewer_row(db)
            return (source['state'] != 'idle' or bool(source['owner'])
                    or bool(db.execute("SELECT 1 FROM review_stages WHERE status='running'").fetchone())
                    or bool(db.execute("SELECT 1 FROM managed_permissions WHERE status IN ('pending','allowed','granted','unknown')").fetchone()))
        if not self._idle(db, [target], exclude=exclude):
            return True
        if db.execute("SELECT 1 FROM approval_requests WHERE member=? AND status IN ('pending','allowed','granted','unknown')", (target,)).fetchone():
            return True
        return bool(db.execute("SELECT 1 FROM execution_requests e JOIN tasks t ON t.id=e.task_id "
                               "WHERE t.assignee=? AND e.status NOT IN ('success','failed','declined','resolved')", (target,)).fetchone())

    def _handoff_path(self, db, key, revision=1):
        try:
            from .pair_git import prepare_pair_directory
        except ImportError:
            from pair_git import prepare_pair_directory
        project = db.execute('SELECT project FROM collaborations').fetchone()[0]
        root = Path(project) / '.codex-pair' / 'handoffs'
        prepare_pair_directory(project, str(root), 'handoffs')
        return root / ('role-' + key + '-' + str(revision) + '.md')

    def _handoff_coordination(self, db, member, record, message_id, token, *, saving=False):
        if not message_id and not token:
            return []
        msg = self._owned_message(db, member, message_id, token)
        own_task = saving and member == record['target'] and msg['kind'] == 'task' and msg['task_id'] in record['current_tasks']
        own_note = msg['kind'] == 'note' and str(msg['dedupe_key'] or '').startswith('role-handoff-') and record['id'] in msg['dedupe_key']
        if not self._message_current(db, msg) or not (own_task or own_note):
            fail('Wrong handoff coordination / 只能交回本人的原任务或本次专用协调通知')
        return [msg['id']]

    def save_role_handoff(self, member, session, cwd, key, *, body, execution_resolved, blocked_reason='', refresh=False,
                          message_id=None, token=None):
        safe_evidence(body)
        if blocked_reason:
            safe_evidence(blocked_reason)
        if execution_resolved is not True or not isinstance(body, str) or not body.strip():
            fail('Handoff evidence required / 先核实工具结束并说明文件、成果和副作用归属')
        with self.transaction() as db:
            record = self._role_handoff(db, key)
            self._handoff_actor(db, (member, session, cwd), record)
            if not isinstance(refresh, bool):
                fail('Invalid refresh / refresh须为布尔值')
            if record['state'] != 'requested' and not (refresh and record['state'] == 'saved'):
                return dict(record, duplicate=True)
            coordination = self._handoff_coordination(db, member, record, message_id, token, saving=True)
            if self._handoff_unresolved(db, record, exclude=coordination):
                fail('Execution unresolved / 先核实原工具、执行请求和审批结果，不按时间重放')
            tasks = self._handoff_tasks(db, record['target'])
            current = [t for t in tasks if t['id'] in record['current_tasks']]
            if record['mode'] == 'finish_current' and any(t['status'] not in DELIVERED for t in current) and not blocked_reason.strip():
                fail('Finish current task / 超过一半须完成实现与自测；不能完成时记录具体阻塞原因')
            snapshot = self._handoff_snapshot(db, record)
            text = '# 角色让位与退出交接\n\n' + body + '\n\n阻塞原因：' + (blocked_reason or '无')
            text += '\n\n```json\n' + json.dumps(snapshot, ensure_ascii=False, sort_keys=True, indent=2) + '\n```\n'
            safe_evidence(text)
            fingerprint = hashlib.sha256(text.encode('utf-8')).hexdigest()
            revision = record.get('revision', 0) + 1
            path = self._handoff_path(db, key, revision)
            # 数据库事务失败可能留下文件；只接纳完全相同的文档，不能覆盖未知现场。
            if path.exists():
                if path.is_symlink() or path.read_text(encoding='utf-8') != text:
                    fail('Unregistered handoff changed / 保留已有交接文件，不覆盖')
            else:
                with path.open('x', encoding='utf-8', newline='\n') as stream:
                    stream.write(text)
                    stream.flush()
                    os.fsync(stream.fileno())
            for task in tasks:
                if task['status'] not in DELIVERED:
                    db.execute("UPDATE tasks SET status='handoff_wait',updated=? WHERE id=?", (time.time(), task['id']))
            for message in coordination:
                db.execute("UPDATE messages SET process_status='completed',updated=? WHERE id=?",(time.time(),message))
            if record['target'] == 'reviewer':
                self._put(db, 'reviewer_auto', False)
            record.update(state='saved', revision=revision, path=str(path), digest=fingerprint, document=text,
                          snapshot=snapshot, blocked_reason=blocked_reason, reads={})
            self._put(db, 'role_handoff:' + key, record)
            self._send(db, member, 'A', '交接文档已保存，须分页读完并核对现场，再确认接收和退役。handoff=' + key,
                       kind='note', dedupe_key='role-handoff-saved:' + key + ':' + str(revision))
            record['work_stamp'] = self._handoff_work_stamp(db, record)
            self._put(db, 'role_handoff:' + key, record)
            self.event(db, 'role_handoff_saved', handoff=key, target=record['target'], digest=fingerprint)
            return {k: v for k, v in record.items() if k not in ('document', 'snapshot', 'reads')}

    def _handoff_work_stamp(self, db, record):
        snapshot = self._handoff_snapshot(db, record)
        # A读取通知会改变消息状态；只冻结任务、送审版本及需求，不把通知消费当作现场变化。
        return {k: snapshot[k] for k in ('tasks', 'stages', 'requirements_version', 'task_policy', 'source_identity')}

    def _verify_handoff_document(self, db, record):
        path = self._handoff_path(db, record['id'], record['revision'])
        if not path.is_file() or path.is_symlink() or canonical_path(path) != canonical_path(record['path']):
            fail('Handoff document missing / 交接文档缺失或路径变化')
        text = path.read_text(encoding='utf-8')
        if hashlib.sha256(text.encode('utf-8')).hexdigest() != record['digest']:
            fail('Handoff document changed / 交接文档变化，旧读取确认失效')
        return text

    def read_role_handoff(self, member, session, cwd, key, *, offset=0, limit=2000):
        if (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
                or isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 2000):
            fail('Invalid handoff page / 分页参数无效')
        with self.transaction() as db:
            self._binding(db, member, session, cwd)
            record = self._role_handoff(db, key)
            if record['state'] == 'requested':
                fail('Handoff not saved / 原窗口尚未保存文档')
            if member not in ('A', record['target'], record.get('successor')):
                fail('Handoff access denied / 尚未指定该窗口接管')
            text = self._verify_handoff_document(db, record)
            end = min(len(text), offset + limit)
            # 完整覆盖才可确认；跳页或只读末页不能伪装成读完。
            binding = self._member(db, member)
            reader = member + ':' + str(binding['binding_version'])
            covered = record['reads'].get(reader, 0)
            if offset <= covered:
                record['reads'][reader] = max(covered, end)
                self._put(db, 'role_handoff:' + key, record)
            return {'id': key, 'text': text[offset:end], 'digest': record['digest'],
                    'next_offset': end if end < len(text) else None}

    def _handoff_read_required(self, db, member, record):
        binding = self._member(db, member)
        reader = member + ':' + str(binding['binding_version'])
        if record['reads'].get(reader, 0) < len(record['document']):
            fail('Unread handoff / 必须完整分页读取当前交接文档')

    def ack_role_handoff(self, member, session, cwd, key, fingerprint, *, plan, message_id=None, token=None):
        safe_evidence(plan)
        if not isinstance(plan, str) or not plan.strip():
            fail('Recovery plan required / A须记录现场核对及接收计划')
        source = None
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd)
            record = self._role_handoff(db, key)
            if fingerprint != record.get('digest'):
                fail('Handoff digest mismatch / 确认摘要不符')
            self._verify_handoff_document(db, record)
            self._handoff_read_required(db, member, record)
            if record['state'] in ('accepted', 'taken'):
                duplicate = True
            else:
                duplicate = False
                self._handoff_actor(db, (member, session, cwd), record, a_only=True)
                coordination = self._handoff_coordination(db, member, record, message_id, token)
                if record['state'] != 'saved' or self._handoff_unresolved(db, record, exclude=coordination):
                    fail('Execution unresolved / 接收前重新核对原执行终态')
                if self._handoff_work_stamp(db, record) != record['work_stamp']:
                    fail('Handoff state changed / 任务、交付或消息已变化，先核实并重新保存文档')
                if record['target'] == 'reviewer':
                    runtime = self._reviewer_row(db)
                    db.execute('INSERT OR IGNORE INTO reviewer_history VALUES(?,?,?,?)',
                               (runtime['epoch'], json.dumps(runtime), plan, time.time()))
                    db.execute("UPDATE reviewer_runtime SET state='retired',owner=NULL,updated=? WHERE id=1", (time.time(),))
                    self._put(db,'reviewer_auto',False)
                else:
                    source = self._member(db, record['target'])
                    db.execute('INSERT OR IGNORE INTO member_history VALUES(?,?,?,?,?)',
                               (source['id'], source['binding_version'], json.dumps(source), plan, time.time()))
                    db.execute("UPDATE members SET lifecycle='retired',online=0,verified=0 WHERE id=?", (source['id'],))
                    self._put(db,'role_handoff_retired:'+source['id'],source['binding_version'])
                    # 已终态的旧领取不再占用；未启动消息取消，未知/在途已在前面拒绝。
                    db.execute("UPDATE messages SET process_status='completed',claim_token=NULL,"
                               "delivery_status=CASE WHEN delivery_status IN ('pending','retry','manual_handoff') THEN 'failed' ELSE delivery_status END,"
                               "error='Role handoff accepted' WHERE recipient=? AND recipient_version=?",
                               (source['id'], source['binding_version']))
                record.update(state='accepted', accepted_by='A', plan=plan, accepted=time.time())
                self._put(db, 'role_handoff:' + key, record)
                for message in coordination:
                    db.execute("UPDATE messages SET process_status='completed',updated=? WHERE id=?",(time.time(),message))
                self.event(db, 'role_handoff_accepted', handoff=key, target=record['target'], cli_closed=False)
            if record['target'] != 'reviewer':
                # 可重复执行跨库清理，修复本库提交成功但全局预约尚未释放的窗口。
                if source is None:
                    old = db.execute('SELECT snapshot FROM member_history WHERE member=? AND version=?',
                                     (record['target'], record['version'])).fetchone()
                    source = json.loads(old['snapshot'])
        if source:
            self._release_session(source['codex_home'], source['session_uuid'], source['id'])
        return {'accepted': True, 'retired': record['target'], 'duplicate': duplicate,
                'cli_closed': False, 'work_completed': False, 'document_preserved': True}

    def designate_role_successor(self, member, session, cwd, key, *, target):
        with self.transaction() as db:
            self._a_actor(db, member, session, cwd)
            record = self._role_handoff(db, key)
            if record['state'] != 'accepted' or record['target'] == 'reviewer':
                fail('Accepted visible handoff required / A先接收可见成员交接')
            bound = self._member(db, target)
            if not self._member_eligible(db, target) or self._member_suspended(db, target):
                fail('Successor unavailable / 继任窗口尚未完成验证或不可接新任务')
            record.update(successor=target, successor_version=bound['binding_version'])
            self._put(db, 'role_handoff:' + key, record)
            self._send(db, 'A', target, 'A指定接管交接；完整读取并核对后登记恢复计划。handoff=' + key,
                       kind='note', dedupe_key='role-handoff-successor:' + key + ':' + target + ':' + str(bound['binding_version']))
            return {'successor': target, 'id': key}

    def take_role_handoff(self, member, session, cwd, key, *, target, plan):
        safe_evidence(plan)
        if not isinstance(plan, str) or not plan.strip():
            fail('Recovery plan required / 必须记录继任者核对现场后的恢复计划')
        with self.transaction() as db:
            actor = self._binding(db, member, session, cwd)
            if member != target:
                fail('Successor must consume / 必须由继任窗口本人接管')
            record = self._role_handoff(db, key)
            if record['state'] == 'taken':
                if record.get('successor') != target or record.get('successor_version') != actor['binding_version']:
                    fail('Wrong successor / 不能重复消费其他绑定的接管')
                return {'taken': True, 'duplicate': True}
            if db.execute('SELECT status FROM collaborations').fetchone()[0] != 'running':
                fail('Collaboration not running / 停止或暂停时只保存，不恢复业务')
            if record['state'] != 'accepted' or record['target'] == 'reviewer':
                fail('Accepted handoff required / 先由A读完并确认接收')
            if target == 'A' and not record.get('successor'):
                record.update(successor='A', successor_version=actor['binding_version'])
            if record.get('successor') != target or record.get('successor_version') != actor['binding_version']:
                fail('Successor not designated / 先由A指定当前绑定接管')
            if not self._member_eligible(db, target) or self._member_suspended(db, target):
                fail('Successor unavailable / 继任窗口不可用')
            self._verify_handoff_document(db, record)
            self._handoff_read_required(db, member, record)
            tasks = self._handoff_tasks(db, record['target'])
            original = {t['id']: t for t in record['snapshot']['tasks']}
            pending = [t for t in tasks if t['id'] in original and t['status'] not in TERMINAL]
            # 预检全部任务，固定原窗口或独立性冲突不做部分接管。
            moved = [t['id'] for t in pending]
            for task in pending:
                if not self._task_member_allowed(db, task, target, takeover=moved):
                    fail('Execution policy prevents takeover / 固定原窗口或独立审核约束不允许接管')
            for task in pending:
                if task['id'] not in original or task['base_commit'] != original[task['id']]['base_commit']:
                    fail('Handoff task changed / 原任务或基准变化，先由A重新安排')
                destination = json.loads(actor['proof']).get('git', {}).get('path') or actor['cwd']
                source_workspace = record['snapshot']['source_workspace']
                if canonical_path(destination) != canonical_path(source_workspace):
                    fail('Cross-worktree handoff requires integration / 不搬运脏文件，先由A在目标树登记剩余任务')
            for task in pending:
                self._put(db, 'role_handoff_task:' + task['id'], {'source': record['target'], 'source_session': record['session'],
                    'source_version': record['version'], 'handoff': key, 'restart_from_goal': record['mode'] != 'finish_current', 'plan': plan})
                next_status = task['status'] if task['status'] in ('review_wait','delivered','needs_fix','needs_validation') else 'pending'
                db.execute('UPDATE tasks SET assignee=?,status=?,updated=? WHERE id=?', (target, next_status, time.time(), task['id']))
                if next_status == 'pending':
                    db.execute('DELETE FROM resource_locks WHERE task_id=?', (task['id'],))
                    db.execute("UPDATE messages SET dedupe_key=NULL WHERE task_id=? AND kind='task'", (task['id'],))
                # 修改责任不改作者来源、问题ID或既有返修轮数。
                for issue in db.execute('SELECT id FROM review_issues WHERE root_task=?',(task['id'],)).fetchall():
                    self._put(db,'issue_responsible:'+issue['id'],target)
            record.update(state='taken', successor=target, successor_version=actor['binding_version'], recovery_plan=plan)
            self._put(db, 'role_handoff:' + key, record)
            self.event(db, 'role_handoff_taken', handoff=key, successor=target, tasks=moved, replayed=False)
            return {'taken': True, 'tasks': moved, 'restart_from_goal': record['mode'] != 'finish_current', 'replayed': False}

    def role_handoff_status(self, member, session, cwd):
        with self.transaction() as db:
            self._binding(db, member, session, cwd)
            records = [json.loads(r[0]) for r in db.execute("SELECT value FROM pair_settings WHERE key LIKE 'role_handoff:%'")]
            return {'handoffs': [{k:v for k,v in r.items() if k not in ('document','snapshot','reads','work_stamp')}
                                  for r in records if member=='A' or member in (r['target'],r.get('successor'))]}
