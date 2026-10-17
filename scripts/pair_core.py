"""事务性任务、持久化发件箱与本地守护进程。不根据回复耗时重派任务。"""
from __future__ import annotations

from contextlib import closing, contextmanager, ExitStack
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

try:
    from .pair_transport import DeliveryUnknown, SessionBusy, TransportError, canonical_path, session_uuid, binding_proof, provider_identity, redact_diagnostic
    from . import pair_balance
    from .pair_assistance import AssistanceMixin
except ImportError:
    from pair_transport import DeliveryUnknown, SessionBusy, TransportError, canonical_path, session_uuid, binding_proof, provider_identity, redact_diagnostic
    import pair_balance
    from pair_assistance import AssistanceMixin


class PairError(RuntimeError):
    pass


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value):
        raise PairError('Invalid identifier / 标识只能含字母、数字、点、下划线、短横线，最多 64 字符')
    return value


def effective_home():
    return Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex').expanduser().resolve()


def member_workspace(member):
    """会话 cwd 不必等于项目目录；旧绑定仍兼容。"""
    proof=member.get('proof') or {}
    if isinstance(proof,str):proof=json.loads(proof)
    return canonical_path(proof.get('git',{}).get('path') or member['cwd'])


def normalize_scopes(paths):
    result = []
    for path in paths:
        path = path.replace('\\', '/')
        directory=path.endswith('/')
        path=path.rstrip('/')
        if not path or path.startswith('/') or ':' in path:
            raise PairError('Scopes must be repository-relative / 修改范围必须是仓库相对路径')
        parts = path.split('/')
        if any(p in ('', '.', '..') for p in parts) or '.git' in [p.casefold() for p in parts] or any(c in path for c in '*?['):
            raise PairError('Unsafe modification scope / 不安全的修改范围')
        result.append(path+'/' if directory else path)
    if not result:
        raise PairError('At least one allowed path is required / 至少指定一个修改范围')
    return sorted(set(result))


def scopes_overlap(first, second):
    def prefix(path):
        parts=[]
        for part in path.replace('\\','/').casefold().rstrip('/').split('/'):
            if any(char in part for char in '*?['):
                break
            parts.append(part)
        return '/'.join(parts) or '.'
    for a in map(prefix, first):
        for b in map(prefix, second):
            if a == '.' or b == '.' or a == b or a.startswith(b+'/') or b.startswith(a+'/'):
                return True
    return False


class FileLock:
    """由操作系统释放的字节锁；PID 与心跳不用于抢占锁。"""

    def __init__(self, path, timeout=0):
        self.path, self.timeout, self.stream = Path(path), timeout, None

    def acquire(self):
        if self.path.resolve()!=self.path.absolute():
            raise PairError('Redirected lock path / 锁文件路径存在重定向，拒绝写入')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        if self.stream.seek(0, 2) == 0:
            self.stream.write(b'0'); self.stream.flush()
        deadline = time.monotonic()+self.timeout
        while True:
            try:
                self.stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if time.monotonic() >= deadline:
                    self.stream.close(); self.stream = None
                    raise PairError('Lock held / 锁已被占用: '+str(self.path)) from exc
                time.sleep(.02)

    def release(self):
        if self.stream:
            self.stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close(); self.stream=None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_):
        self.release()


SCHEMA = '''
CREATE TABLE collaborations(id TEXT PRIMARY KEY, project TEXT NOT NULL, base_commit TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('running','paused','stopped')), created REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
CREATE TABLE members(id TEXT PRIMARY KEY CHECK(id IN ('A','B')), session_uuid TEXT NOT NULL UNIQUE,
 cwd TEXT NOT NULL UNIQUE, backend TEXT NOT NULL CHECK(backend IN ('managed','shared','queue')), owned INTEGER NOT NULL,
 proof TEXT NOT NULL, joined REAL NOT NULL);
CREATE TABLE tasks(id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, assignee TEXT NOT NULL REFERENCES members(id),
 allowed_paths TEXT NOT NULL, status TEXT NOT NULL, no_progress_rounds INTEGER NOT NULL DEFAULT 0,
 result TEXT, commits_json TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE dependencies(task_id TEXT NOT NULL REFERENCES tasks(id), depends_on TEXT NOT NULL REFERENCES tasks(id),
 PRIMARY KEY(task_id,depends_on), CHECK(task_id<>depends_on));
CREATE TABLE messages(id TEXT PRIMARY KEY, sender TEXT NOT NULL REFERENCES members(id), recipient TEXT NOT NULL REFERENCES members(id),
 task_id TEXT REFERENCES tasks(id), kind TEXT NOT NULL, body TEXT NOT NULL, dedupe_key TEXT NOT NULL UNIQUE,
 fingerprint TEXT NOT NULL, reply_to TEXT REFERENCES messages(id), envelope TEXT, queue_id TEXT, delivery_status TEXT NOT NULL DEFAULT 'pending',
 process_status TEXT NOT NULL DEFAULT 'unclaimed', claim_token TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 next_attempt REAL NOT NULL DEFAULT 0, turn_id TEXT, remote_status TEXT, remote_result TEXT,
 error TEXT, created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE events(seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, data TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE attempts(id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL REFERENCES messages(id),
 number INTEGER NOT NULL, outcome TEXT NOT NULL, detail TEXT, started REAL NOT NULL, ended REAL);
CREATE TABLE runtime(id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT, pid INTEGER, started REAL, heartbeat REAL,
 probe TEXT, probe_ack TEXT);
INSERT INTO runtime(id) VALUES(1);
CREATE INDEX messages_outbox ON messages(delivery_status, next_attempt, created);
PRAGMA user_version=1;
'''

MIGRATION_2='''
BEGIN IMMEDIATE;
ALTER TABLE tasks ADD COLUMN base_commit TEXT;
CREATE TABLE git_context(id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
CREATE TABLE integrations(id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id),
 receiver TEXT NOT NULL, commits_json TEXT NOT NULL, next_index INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL, before_head TEXT, current_commit TEXT, result_json TEXT,
 created REAL NOT NULL, updated REAL NOT NULL);
PRAGMA user_version=2;
COMMIT;
'''


class Store(AssistanceMixin):
    def __init__(self, pair_id, *, home=None, state_root=None, create=False):
        self.pair_id = identifier(pair_id)
        self.home = Path(home or effective_home()).resolve()
        # 不指定 state_root 的旧 Python 调用继续读取原位置，CLI 总是解析项目位置。
        self.state_root = Path(state_root).resolve() if state_root else self.home/'codex-pair'
        self.directory = self.state_root/pair_id
        self.path = self.directory/'state.sqlite3'
        if self.path.resolve()!=self.path.absolute():
            raise PairError('Redirected state path / 状态库路径存在重定向，拒绝读写')
        if not create and not self.path.is_file():
            raise PairError('Pair not initialized / 协作尚未初始化: '+pair_id)
        self.control_lock = self.directory/'dispatch.lock'
        self.daemon_lock = self.directory/'daemon.lock'
        if not create:
            if self.state_root!=self.home/'codex-pair':
                with closing(sqlite3.connect('file:'+self.path.as_posix()+'?mode=ro',uri=True)) as db:
                    row=db.execute('SELECT id,project FROM collaborations').fetchone()
                if not row or row[0]!=pair_id:raise PairError('State identity mismatch / 协作数据库身份不符')
                try:
                    from .pair_git import prepare_pair_directory
                except ImportError:
                    from pair_git import prepare_pair_directory
                prepare_pair_directory(row[1],str(self.directory),'state')
            self._upgrade()

    def _upgrade(self):
        with closing(sqlite3.connect(self.path)) as db:
            version=db.execute('PRAGMA user_version').fetchone()[0]
        if version==8:return
        if version not in (1,2,3,4,5,6,7):raise PairError('Unsupported schema / 不支持的数据库版本')
        with FileLock(self.daemon_lock,timeout=2), FileLock(self.control_lock,timeout=30),closing(sqlite3.connect(self.path)) as db:
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version==8:return
            unresolved=db.execute("SELECT 1 FROM messages WHERE delivery_status IN ('delivering','unknown') OR (delivery_status='accepted' AND (remote_status IS NULL OR remote_status IN ('queued','inProgress'))) LIMIT 1").fetchone()
            if unresolved and version<4:raise PairError('Legacy delivery unresolved / 请先停止旧派发并对账处理中消息，再升级；原状态未移动')
            with closing(sqlite3.connect(self.directory/('schema-v%d-%s.backup.sqlite3'%(version,time.time_ns())))) as backup:
                db.backup(backup)
            if version==1:db.executescript(MIGRATION_2)
            if version in (1,2):self._upgrade_accounts(db)
            if version<4:self._upgrade_windows(db)
            if version<5:self._upgrade_handoffs(db)
            if version<6:self._upgrade_assistance(db)
            if version<7:self._upgrade_approvals(db)
            self._upgrade_delegation(db)

    def _upgrade_handoffs(self, db):
        with db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("ALTER TABLE tasks ADD COLUMN report_to TEXT NOT NULL DEFAULT 'A'")
            db.execute('ALTER TABLE tasks ADD COLUMN effort INTEGER NOT NULL DEFAULT 3')
            db.execute("ALTER TABLE tasks ADD COLUMN assignment_mode TEXT NOT NULL DEFAULT 'fixed'")
            db.execute("ALTER TABLE tasks ADD COLUMN assignment_reason TEXT NOT NULL DEFAULT 'legacy_fixed'")
            db.execute('ALTER TABLE messages ADD COLUMN delivery_id TEXT')
            db.execute('''CREATE TABLE deliveries(id TEXT PRIMARY KEY,task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
                body TEXT NOT NULL,commits_json TEXT NOT NULL,digest TEXT NOT NULL,created REAL NOT NULL)''')
            db.execute('''CREATE TABLE record_reads(member TEXT NOT NULL,delivery_id TEXT NOT NULL REFERENCES deliveries(id),
                digest TEXT NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,
                PRIMARY KEY(member,delivery_id,digest,start,end))''')
            db.execute('''CREATE TABLE record_acks(member TEXT NOT NULL,delivery_id TEXT NOT NULL REFERENCES deliveries(id),
                digest TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(member,delivery_id,digest))''')
            db.execute('''CREATE TABLE quota_snapshots(member TEXT PRIMARY KEY REFERENCES members(id),data TEXT NOT NULL)''')
            db.execute('''CREATE TABLE turn_usage(thread_id TEXT NOT NULL,turn_id TEXT NOT NULL,member TEXT NOT NULL,
                task_id TEXT NOT NULL REFERENCES tasks(id),tokens INTEGER NOT NULL,PRIMARY KEY(thread_id,turn_id))''')
            db.execute("UPDATE tasks SET report_to=COALESCE((SELECT sender FROM messages WHERE task_id=tasks.id AND kind='task' ORDER BY created LIMIT 1),'A')")
            for task_id,body,commits,created in db.execute('SELECT id,result,commits_json,updated FROM tasks WHERE result IS NOT NULL').fetchall():
                digest=hashlib.sha256(dumps([body,commits or '[]']).encode('utf-8')).hexdigest()
                delivery_id='delivery:'+task_id
                db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?)',(delivery_id,task_id,body,commits or '[]',digest,created))
                db.execute("UPDATE messages SET delivery_id=? WHERE task_id=? AND kind='result'",(delivery_id,task_id))
            db.execute("INSERT OR IGNORE INTO pair_settings VALUES('scheduling_policy','\"legacy\"')")
            db.execute('PRAGMA user_version=5')

    def _upgrade_accounts(self, db):
        with db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('ALTER TABLE members ADD COLUMN codex_home TEXT')
            db.execute("ALTER TABLE members ADD COLUMN delivery_mode TEXT NOT NULL DEFAULT 'auto'")
            db.execute('''CREATE TABLE bridge_workers(member TEXT PRIMARY KEY, owner TEXT, pid INTEGER,
                heartbeat REAL, probe TEXT, probe_ack TEXT, stop_requested INTEGER NOT NULL DEFAULT 0)''')
            db.execute('UPDATE members SET codex_home=?',(str(self.home),))
            db.execute('PRAGMA user_version=3')

    def _upgrade_windows(self, db):
        # 移除 cwd 唯一约束：身份由会话 UUID 唯一确定，同目录按任务和回合串行。
        db.execute('PRAGMA foreign_keys=OFF')
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('''CREATE TABLE members_new(id TEXT PRIMARY KEY CHECK(id IN ('A','B')),
                    session_uuid TEXT NOT NULL UNIQUE,cwd TEXT NOT NULL,
                    backend TEXT NOT NULL CHECK(backend IN ('managed','shared','queue')),
                    owned INTEGER NOT NULL,proof TEXT NOT NULL,joined REAL NOT NULL,
                    codex_home TEXT,delivery_mode TEXT NOT NULL DEFAULT 'auto')''')
                db.execute('INSERT INTO members_new SELECT * FROM members')
                db.execute('DROP TABLE members')
                db.execute('ALTER TABLE members_new RENAME TO members')
                db.execute('CREATE TABLE pair_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                if db.execute('PRAGMA foreign_key_check').fetchall():
                    raise PairError('Migration foreign key failure / 窗口模式升级外键校验失败')
                db.execute('PRAGMA user_version=4')
        finally:
            db.execute('PRAGMA foreign_keys=ON')

    @contextmanager
    def transaction(self):
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA busy_timeout=15000')
        try:
            connection.execute('BEGIN IMMEDIATE')
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def event(db, event_type, **data):
        db.execute('INSERT INTO events(kind,data,created) VALUES(?,?,?)', (event_type, dumps(data), time.time()))

    @classmethod
    def initialize(cls, pair_id, project, base_commit, *, home=None, state_root=None):
        project = canonical_path(project)
        if state_root is not None:
            try:
                from .pair_git import prepare_pair_directory
            except ImportError:
                from pair_git import prepare_pair_directory
            state_root=prepare_pair_directory(project,str(Path(state_root)/identifier(pair_id)),'state')
            state_root=str(Path(state_root).parent)
        store = cls(pair_id, home=home, state_root=state_root, create=True)
        if state_root is None and store.directory.is_relative_to(Path(project)):
            raise PairError('State must be outside the project / 状态库必须位于项目仓库之外')
        store.directory.mkdir(parents=True, exist_ok=True)
        with FileLock(store.control_lock, timeout=15):
            db = sqlite3.connect(store.path)
            try:
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version == 0:
                    db.execute('PRAGMA journal_mode=WAL')
                    db.executescript(SCHEMA)
                elif version not in (1,2,3,4,5,6,7,8):
                    raise PairError('Unsupported schema / 不支持的数据库版本')
                if version==0:
                    db.executescript(MIGRATION_2)
                    store._upgrade_accounts(db)
                    store._upgrade_windows(db)
                    store._upgrade_handoffs(db)
                    store._upgrade_assistance(db)
                    store._upgrade_approvals(db)
                    store._upgrade_delegation(db)
            finally:
                db.close()
            if version in (1,2,3,4,5,6,7):
                raise PairError('Upgrade existing state first / 请先使用 status 升级旧状态，再初始化')
            with store.transaction() as db:
                existing = db.execute('SELECT * FROM collaborations').fetchone()
                if existing:
                    if existing['id'] != pair_id or existing['project'] != project or existing['base_commit'] != base_commit:
                        raise PairError('Existing pair differs / 已有协作的项目或基准不同，拒绝覆盖')
                else:
                    db.execute('INSERT INTO collaborations(id,project,base_commit,status,created) VALUES(?,?,?,?,?)',
                               (pair_id, project, base_commit, 'running', time.time()))
                    store.event(db, 'initialized', project=project, base_commit=base_commit)
                    db.execute("INSERT OR REPLACE INTO pair_settings VALUES('scheduling_policy','\"quota-balanced\"')")
        return store

    def _member(self, db, member_id):
        row = db.execute('SELECT * FROM members WHERE id=?', (member_id,)).fetchone()
        if not row:
            raise PairError('Member not joined / 成员尚未加入: '+member_id)
        return dict(row)

    def member(self, member_id):
        with self.transaction() as db:
            return self._member(db, member_id)

    def setting(self, key, value=None):
        with self.transaction() as db:
            if value is not None:
                db.execute('INSERT INTO pair_settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,dumps(value)))
            row=db.execute('SELECT value FROM pair_settings WHERE key=?',(key,)).fetchone()
            return json.loads(row[0]) if row else None

    def git_context(self):
        with self.transaction() as db:
            row=db.execute('SELECT data FROM git_context WHERE id=1').fetchone()
            return json.loads(row[0]) if row else None

    def configure_git(self,repo,worktrees=None):
        with self.transaction() as db:
            row=db.execute('SELECT data FROM git_context WHERE id=1').fetchone()
            data=json.loads(row[0]) if row else {'repo':repo,'worktrees':{}}
            if canonical_path(data['repo']['common_dir'])!=canonical_path(repo['common_dir']):
                raise PairError('Git repository identity changed / 公共 Git 目录发生变化')
            if worktrees:
                if data['worktrees'] and data['worktrees']!=worktrees:
                    raise PairError('Existing worktree plan differs / 已有 worktree 计划不同，拒绝覆盖')
                data['worktrees']=worktrees
            db.execute('INSERT INTO git_context VALUES(1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',(dumps(data),))
            return data

    def task(self,task_id):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not row:raise PairError('Task not found / 任务不存在: '+task_id)
            return dict(row)

    def message(self,message_id):
        with self.transaction() as db:
            row=db.execute('SELECT * FROM messages WHERE id=?',(message_id,)).fetchone()
            if not row:raise PairError('Message not found / 消息不存在: '+message_id)
            return dict(row)

    def join(self, member_id, session, cwd, *, backend, owned, proof, member_codex_home=None, delivery_mode='auto'):
        if member_id not in ('A','B'):
            raise PairError('Member must be A or B / 成员必须为 A 或 B')
        session, cwd = session_uuid(session), canonical_path(cwd)
        member_home=canonical_path(member_codex_home or self.home)
        if delivery_mode not in ('auto','manual'):raise PairError('Invalid delivery mode / 无效通信模式')
        if backend not in ('managed','shared','queue') or (backend == 'managed' and not owned):
            raise PairError('Managed session must be pair-owned / 独立会话必须由本协作创建')
        if session_uuid(proof['id']) != session or canonical_path(proof['cwd']) != cwd:
            raise PairError('Binding proof mismatch / 会话绑定证据不符')
        if proof.get('ephemeral'):
            raise PairError('Ephemeral session rejected / 不接受无法恢复的临时会话')
        proof=binding_proof(proof)
        with self.transaction() as db:
            existing=db.execute('SELECT * FROM members WHERE id=?',(member_id,)).fetchone()
            if existing:
                if (existing['session_uuid'],existing['cwd'],existing['backend'],canonical_path(existing['codex_home']),existing['delivery_mode']) != (session,cwd,backend,member_home,delivery_mode):
                    raise PairError('Member already bound / 成员已有不同绑定，拒绝替换执行中的会话')
                return dict(existing)
            try:
                db.execute('INSERT INTO members(id,session_uuid,cwd,backend,owned,proof,joined,codex_home,delivery_mode) VALUES(?,?,?,?,?,?,?,?,?)',
                           (member_id,session,cwd,backend,int(owned),dumps(proof),time.time(),member_home,delivery_mode))
            except sqlite3.IntegrityError as exc:
                raise PairError('Session already bound / 会话已绑定其他成员') from exc
            self.event(db,'member_joined', member=member_id, session=session, cwd=cwd, backend=backend)
            return self._member(db,member_id)

    def _binding(self, db, member_id, session, cwd):
        member=self._member(db,member_id)
        if member['session_uuid'] != session_uuid(session) or member['cwd'] != canonical_path(cwd):
            raise PairError('Invalid member session or cwd / 成员会话或实际工作目录不符')
        return member

    @staticmethod
    def window_verified(db):
        return bool(db.execute('''SELECT 1 FROM messages q JOIN messages r ON r.reply_to=q.id AND r.kind='reply'
            WHERE q.dedupe_key='window-handshake' AND q.process_status='completed' AND r.process_status='completed'
            AND q.remote_status='completed' AND r.remote_status='completed' LIMIT 1''').fetchone())

    def _send(self, db, sender, recipient, body, *, kind='note', task_id=None,
              dedupe_key=None, reply_to=None, progress_note=None):
        self._member(db,sender);self._member(db,recipient)
        if kind in ('question','reply') and self._option(db,'delegation_enabled',False):
            if redact_diagnostic(body)!=body:raise PairError('Credentials in question / 不向协作问题复制凭据')
            if progress_note and redact_diagnostic(progress_note)!=progress_note:raise PairError('Credentials in progress / 进展说明不能包含凭据')
        if (sender == recipient and kind not in ('task','result','checkpoint','checkpoint_resume')) or not body.strip():
            raise PairError('Message needs another recipient and a body / 消息需要其他接收者和非空正文')
        if kind not in ('task','note','question','reply','result','blocked','execution_request','execution_result','checkpoint','checkpoint_resume','approval_review'):
            raise PairError('Invalid message kind / 无效消息类型')
        if task_id and not db.execute('SELECT 1 FROM tasks WHERE id=?',(task_id,)).fetchone():
            raise PairError('Task not found / 任务不存在')
        fingerprint=hashlib.sha256(dumps([sender,recipient,body,kind,task_id,reply_to,progress_note]).encode('utf-8')).hexdigest()
        dedupe_key=dedupe_key or str(uuid.uuid4())
        existing=db.execute('SELECT * FROM messages WHERE dedupe_key=?',(dedupe_key,)).fetchone()
        if existing:
            if existing['fingerprint'] != fingerprint:
                raise PairError('Dedupe key reused with different content / 去重键被用于不同内容')
            return dict(existing)
        if kind=='question' and sender=='B' and dedupe_key!='window-handshake' and self._option(db,'delegation_enabled',False):
            if self._option(db,'delegated_no_progress',0)>=2 and not (progress_note and progress_note.strip()):
                raise PairError('Question loop stopped / 连续两轮无进展，请通知用户或提供新增信息')
            if progress_note and progress_note.strip():self._put(db,'delegated_no_progress',0)
        if kind=='reply':
            question=db.execute('SELECT * FROM messages WHERE id=?',(reply_to,)).fetchone()
            if not question or question['kind']!='question' or question['task_id']!=task_id or (question['sender'],question['recipient'])!=(recipient,sender):
                raise PairError('Reply must identify the matching question / 回复必须关联同任务的原问题')
            if db.execute("SELECT 1 FROM messages WHERE kind='reply' AND reply_to=?",(reply_to,)).fetchone():
                raise PairError('Question already answered / 问题已有回复')
            delegated=db.execute('SELECT * FROM delegation_questions WHERE id=?',(reply_to,)).fetchone()
            if delegated and (delegated['state']!='pending' or delegated['authorization_version']!=self._authorization_version(db)):
                raise PairError('Question no longer pending / 问题已取消或转交用户，不能采用旧回复')
            if delegated and delegated['owner']!=sender:raise PairError('Question owner changed / 问题已交接，旧回答方不能继续决策')
            if delegated and not task_id:
                rounds=0 if progress_note and progress_note.strip() else self._option(db,'delegated_no_progress',0)+1
                self._put(db,'delegated_no_progress',rounds)
        elif reply_to:
            raise PairError('reply_to only applies to replies / 只有回复可关联原问题')
        message_id=str(uuid.uuid4()); now=time.time()
        db.execute('INSERT INTO messages(id,sender,recipient,task_id,kind,body,dedupe_key,fingerprint,reply_to,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (message_id,sender,recipient,task_id,kind,body,dedupe_key,fingerprint,reply_to,now,now))
        self.event(db,'message_persisted',message=message_id,recipient=recipient,kind=kind)
        self._delegation_sent(db,dict(db.execute('SELECT * FROM messages WHERE id=?',(message_id,)).fetchone()))
        if kind=='reply' and delegated and not task_id and self._option(db,'delegated_no_progress',0)>=2:
            db.execute("UPDATE delegation_questions SET state='needs_user',reason='连续两轮没有新增进展，停止循环并通知用户' WHERE id=?",(reply_to,))
            self.event(db,'question_loop_stopped',question=reply_to,notify_user=True)
            self._send(db,'B','A','连续两轮问题未提供新增进展，停止自动问答并向用户报告。',kind='blocked',dedupe_key='question-loop:'+reply_to)
        if kind=='reply' and task_id:
            db.execute('UPDATE tasks SET no_progress_rounds=CASE WHEN ? THEN 0 ELSE no_progress_rounds+1 END,updated=? WHERE id=?',
                       (bool(progress_note and progress_note.strip()),now,task_id))
            task=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            limit=2 if self._option(db,'delegation_enabled',False) else 3
            if task['no_progress_rounds']>=limit and task['status']=='active':
                db.execute("UPDATE tasks SET status='blocked' WHERE id=?",(task_id,))
                self.event(db,'task_blocked', task=task_id, reason=str(limit)+'_rounds_without_progress',notify_user=True)
                # A 自己发出的回复也要获得可审计的阻塞通知。
                other='B'
                self._send(db,other,'A','任务 '+task_id+' 连续 '+str(limit)+' 轮问答未提供实际进展证据，已阻塞；请向用户报告原因。',
                           kind='blocked',task_id=task_id,dedupe_key='blocked:'+task_id)
        return dict(db.execute('SELECT * FROM messages WHERE id=?',(message_id,)).fetchone())

    def send(self, sender, recipient, body, **kwargs):
        with self.transaction() as db:
            return self._send(db,sender,recipient,body,**kwargs)

    def assign(self, task_id, title, assignee, allowed_paths, *, body='', dependencies=(),base_commit=None, report_to=None, effort=3):
        identifier(task_id);allowed_paths=normalize_scopes(allowed_paths)
        if task_id.startswith('pair-plan-'):raise PairError('Reserved task prefix / pair-plan- 为自动规划任务保留')
        if isinstance(effort,bool) or not isinstance(effort,int) or effort not in (1,2,3,5,8):raise PairError('Invalid effort / 工作量须为 1/2/3/5/8')
        if not title.strip():
            raise PairError('Task needs a title / 任务需要标题')
        with self.transaction() as db:
            report_to=report_to or self._coordinator(db)
            self._member(db,'A');self._member(db,report_to)
            auto=assignee=='auto'
            if auto:assignee='A'
            self._member(db,assignee)
            if db.execute('SELECT 1 FROM tasks WHERE id=?',(task_id,)).fetchone():
                raise PairError('Task already exists / 任务已存在')
            for dependency in dependencies:
                if not db.execute('SELECT 1 FROM tasks WHERE id=?',(dependency,)).fetchone():
                    raise PairError('Dependency not found / 依赖任务不存在: '+dependency)
            now=time.time()
            base_commit=base_commit or db.execute('SELECT base_commit FROM collaborations').fetchone()[0]
            db.execute('INSERT INTO tasks(id,title,body,assignee,allowed_paths,status,created,updated,base_commit,report_to,effort,assignment_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                       (task_id,title,body,assignee,dumps(allowed_paths),'pending',now,now,base_commit,report_to,effort,'auto' if auto else 'fixed'))
            db.executemany('INSERT INTO dependencies VALUES(?,?)',[(task_id,d) for d in set(dependencies)])
            self.event(db,'task_assigned',task=task_id,assignee=assignee)
            self._schedule(db)
            return dict(db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone())

    def _schedule(self, db):
        if db.execute('SELECT status FROM collaborations').fetchone()[0]!='running':
            return
        active=list(db.execute("SELECT * FROM tasks WHERE status IN ('active','blocked','waiting_execution','quota_wait','checkpoint_wait')"))
        workspaces={r['id']:member_workspace(dict(r)) for r in db.execute('SELECT * FROM members')}
        delivered=list(db.execute("SELECT * FROM tasks WHERE status='delivered'"))
        policy=db.execute("SELECT value FROM pair_settings WHERE key='scheduling_policy'").fetchone()
        balanced=bool(policy and json.loads(policy[0])=='quota-balanced')
        quotas=self._quotas(db)
        burden=pair_balance.loads(db)
        for row in db.execute("SELECT * FROM tasks WHERE status='pending' ORDER BY created,id").fetchall():
            task=dict(row)
            unmet=db.execute("SELECT 1 FROM dependencies d JOIN tasks t ON t.id=d.depends_on WHERE d.task_id=? AND t.status NOT IN ('completed','integrated') LIMIT 1",(task['id'],)).fetchone()
            if unmet:continue
            candidates=[]
            for member_id in workspaces:
                if self._member_suspended(db,member_id):continue
                if any(a['assignee']==member_id or scopes_overlap(json.loads(a['allowed_paths']),json.loads(task['allowed_paths'])) for a in active):continue
                if any(workspaces[a['assignee']]==workspaces[member_id] for a in active+delivered):continue
                parents=db.execute('''SELECT p.id,p.assignee,p.commits_json FROM dependencies d JOIN tasks p ON p.id=d.depends_on WHERE d.task_id=?''',(task['id'],)).fetchall()
                if task['allowed_paths']!='[]' and any(json.loads(p['commits_json'] or '[]') and workspaces[p['assignee']]!=workspaces[member_id]
                       and not db.execute("SELECT 1 FROM integrations WHERE task_id=? AND receiver=? AND status='completed'",(p['id'],workspaces[member_id])).fetchone() for p in parents):continue
                candidates.append(member_id)
            reason='fixed_assignment'
            if task['assignment_mode']=='auto':
                effective=quotas if balanced else {m:{'reliability':'missing'} for m in candidates}
                chosen,reason=pair_balance.choose(candidates,task['effort'],effective,burden)
                if balanced and task['effort']>=5 and any(quotas[m].get('age_seconds',121)>30 for m in candidates):
                    requested=db.execute("SELECT value FROM pair_settings WHERE key=?",('quota_wait:'+task['id'],)).fetchone()
                    if not requested:
                        db.execute('INSERT INTO pair_settings VALUES(?,?)',('quota_wait:'+task['id'],dumps(time.time())))
                    if not requested or time.time()-json.loads(requested[0])<5:
                        db.execute("UPDATE tasks SET assignment_reason='quota_refresh_pending' WHERE id=?",(task['id'],));continue
                    # 刷新超时不使仍有效的额度事实失效，尤其不能解除已耗尽保护。
                    chosen,reason=pair_balance.choose(candidates,task['effort'],effective,burden)
                if not chosen:
                    db.execute('UPDATE tasks SET assignment_reason=? WHERE id=?',(reason,task['id']));continue
                task['assignee']=chosen
            if task['assignee'] not in candidates:continue
            db.execute('UPDATE tasks SET assignee=?,assignment_reason=? WHERE id=?',(task['assignee'],reason,task['id']))
            db.execute("UPDATE tasks SET status='active',updated=? WHERE id=?",(time.time(),task['id']))
            prerequisites=[dict(r) for r in db.execute('''SELECT p.id,p.result,p.commits_json,d.id delivery_id FROM dependencies x
                JOIN tasks p ON p.id=x.depends_on LEFT JOIN deliveries d ON d.id='delivery:'||p.id WHERE x.task_id=?''',(task['id'],))]
            summary=[{'task':p['id'],'summary':(p['result'] or '')[:600],'commits':json.loads(p['commits_json'] or '[]'),'delivery_id':p['delivery_id']} for p in prerequisites]
            body=task['body'] or task['title']
            if summary:body+='\n前置交付（执行前分页读取并确认记录）：\n'+dumps(summary)
            self._send(db,self._coordinator(db),task['assignee'],body,kind='task',task_id=task['id'],dedupe_key='task:'+task['id'])
            active.append(task)
            if task['assignment_mode']=='auto':burden[task['assignee']]+=task['effort']
            self.event(db,'task_activated',task=task['id'])

    def schedule(self):
        with self.transaction() as db:
            self._assistance_tick(db)
            self._stage_complete(db)
            self._schedule(db)

    def _quotas(self, db):
        saved={r['member']:json.loads(r['data']) for r in db.execute('SELECT * FROM quota_snapshots')}
        members=list(db.execute('SELECT id,codex_home FROM members'))
        result={r['id']:pair_balance.current(saved.get(r['id'],{'observed_at':0,'reliability':'missing',
            'remaining':None,'pool':pair_balance.pool_key({},r['codex_home']),'windows':[]})) for r in members}
        for member in members:
            same_home=[saved[r['id']] for r in members if r['codex_home']==member['codex_home'] and r['id'] in saved and saved[r['id']].get('pool')]
            if same_home:result[member['id']]['pool']=max(same_home,key=lambda q:q.get('observed_at',0))['pool']
        # 同一身份的窗口共享最低已知剩余额度，不把同一资源池当成两个套餐。
        for member,data in result.items():
            peers=[q for q in result.values() if q.get('pool')==data.get('pool') and q.get('limit_id')==data.get('limit_id') and q.get('reliability')=='fresh']
            if peers and data.get('reliability')=='fresh':data['remaining']=min(q['remaining'] for q in peers)
        return result

    def quota(self):
        with self.transaction() as db:return self._quotas(db)

    def save_quota(self, member, data):
        with self.transaction() as db:
            self._member(db,member)
            db.execute('INSERT INTO quota_snapshots VALUES(?,?) ON CONFLICT(member) DO UPDATE SET data=excluded.data',(member,dumps(data)))

    def record_usage(self, member, params):
        """只接收本协作已匹配消息的单回合用量，线程累计/账号总量不能归因。"""
        thread,turn=params.get('threadId'),params.get('turnId')
        usage=params.get('tokenUsage') or {}
        last=usage.get('last') or {}
        tokens=last.get('totalTokens')
        if isinstance(tokens,bool) or not isinstance(tokens,int) or tokens<0:return False
        with self.transaction() as db:
            bound=self._member(db,member)
            if thread!=bound['session_uuid'] or not turn:return False
            matches=list(db.execute("SELECT DISTINCT m.task_id FROM messages m JOIN tasks t ON t.id=m.task_id WHERE m.recipient=? AND m.turn_id=? AND t.assignee=?",(member,turn,member)))
            if len(matches)!=1:return False
            db.execute('INSERT INTO turn_usage VALUES(?,?,?,?,?) ON CONFLICT(thread_id,turn_id) DO UPDATE SET tokens=excluded.tokens',
                (thread,turn,member,matches[0][0],tokens))
            return True

    def _delivery(self, db, task):
        row=db.execute('SELECT * FROM deliveries WHERE id=?',('delivery:'+task['id'],)).fetchone()
        if row:return dict(row)
        body=task['result'] or ''
        commits=task['commits_json'] or '[]'
        record={'id':'delivery:'+task['id'],'task_id':task['id'],'body':body,'commits_json':commits,
                'digest':hashlib.sha256(dumps([body,commits]).encode('utf-8')).hexdigest(),'created':time.time()}
        db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?)',tuple(record.values()))
        return record

    def _handoff(self, db, task, recipient):
        self._member(db,recipient)
        if task['status'] not in ('completed','delivered','integrated'):
            raise PairError('Task has no completed delivery / 仅完成或已交付任务可补发交接')
        record=self._delivery(db,task)
        old=db.execute("SELECT * FROM messages WHERE task_id=? AND kind='result' AND recipient=? ORDER BY created LIMIT 1",(task['id'],recipient)).fetchone()
        if old:return dict(old)
        body=dumps({'summary':record['body'][:600],'delivery_id':record['id'],'digest':record['digest'],
                    'commits':json.loads(record['commits_json']),'instruction':'分页读取交付正文及证据引用，ack-delivery 后完成本结果消息；不重新执行原任务。'})
        message=self._send(db,task['assignee'],recipient,body,kind='result',task_id=task['id'],
            dedupe_key='handoff:'+record['id']+':'+recipient)
        db.execute('UPDATE messages SET delivery_id=? WHERE id=?',(record['id'],message['id']))
        return dict(db.execute('SELECT * FROM messages WHERE id=?',(message['id'],)).fetchone())

    def handoff(self, task_id, recipient):
        with self.transaction() as db:
            task=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not task:raise PairError('Task not found / 任务不存在')
            message=self._handoff(db,dict(task),recipient)
            return {k:message[k] for k in ('id','recipient','task_id','delivery_id','delivery_status','process_status')}

    def _readable_delivery(self, db, member, delivery_id):
        record=db.execute('SELECT * FROM deliveries WHERE id=?',(delivery_id,)).fetchone()
        if not record:raise PairError('Delivery not found / 交付记录不存在')
        task=db.execute('SELECT * FROM tasks WHERE id=?',(record['task_id'],)).fetchone()
        allowed=member in (task['assignee'],task['report_to']) or db.execute('SELECT 1 FROM messages WHERE delivery_id=? AND recipient=?',(delivery_id,member)).fetchone()
        allowed=allowed or db.execute('''SELECT 1 FROM dependencies d JOIN tasks t ON t.id=d.task_id
            WHERE d.depends_on=? AND t.assignee=? AND t.status<>'pending' ''',(record['task_id'],member)).fetchone()
        if not allowed:raise PairError('Delivery access denied / 当前成员无权读取本交付')
        return dict(record)

    def read_delivery(self, member, session, cwd, delivery_id, *, offset=0, limit=2000):
        if offset<0 or not 1<=limit<=2000:raise PairError('Invalid page / 分页参数无效')
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            record=self._readable_delivery(db,member,delivery_id)
            if offset>len(record['body']):raise PairError('Invalid offset / 分页越界')
            end=min(offset+limit,len(record['body']))
            db.execute('INSERT OR IGNORE INTO record_reads VALUES(?,?,?,?,?)',(member,delivery_id,record['digest'],offset,end))
            return {'delivery_id':delivery_id,'digest':record['digest'],'text':record['body'][offset:end],
                    'commits':json.loads(record['commits_json']),'total':len(record['body']),'next_offset':end if end<len(record['body']) else None}

    def ack_delivery(self, member, session, cwd, delivery_id, digest):
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            record=self._readable_delivery(db,member,delivery_id)
            if digest!=record['digest']:raise PairError('Delivery version changed / 确认的交付版本不符')
            covered=0
            for start,end in db.execute('SELECT start,end FROM record_reads WHERE member=? AND delivery_id=? AND digest=? ORDER BY start,end',(member,delivery_id,digest)):
                if start>covered:break
                covered=max(covered,end)
            if covered<len(record['body']):raise PairError('Delivery not fully read / 交付记录尚未完整读取')
            db.execute('INSERT OR IGNORE INTO record_acks VALUES(?,?,?,?)',(member,delivery_id,digest,time.time()))
            return {'acknowledged':True,'delivery_id':delivery_id,'digest':digest}

    def _require_ack(self, db, member, delivery_id):
        record=self._readable_delivery(db,member,delivery_id)
        if not db.execute('SELECT 1 FROM record_acks WHERE member=? AND delivery_id=? AND digest=?',(member,delivery_id,record['digest'])).fetchone():
            raise PairError('Delivery acknowledgement required / 请先完整读取并确认交付记录: '+delivery_id)

    def claim(self, member_id, session, cwd, *, message_id=None):
        with self.transaction() as db:
            self._binding(db,member_id,session,cwd)
            collaboration_state=db.execute('SELECT status FROM collaborations').fetchone()[0]
            if collaboration_state=='stopped':
                return {'claimed':False,'reason':'collaboration_not_running'}
            params=[member_id]
            clause=' AND id=?' if message_id else ''
            if message_id:params.append(message_id)
            message=db.execute("SELECT * FROM messages WHERE recipient=? AND process_status='unclaimed' AND delivery_status<>'failed'"+clause+' ORDER BY created,id LIMIT 1',params).fetchone()
            if not message:
                if message_id:
                    unavailable=db.execute('SELECT process_status,delivery_status,error FROM messages WHERE id=? AND recipient=?',(message_id,member_id)).fetchone()
                    if unavailable:
                        return {'claimed':False,'reason':'delivery_failed' if unavailable['delivery_status']=='failed' and unavailable['process_status']=='unclaimed' else 'already_'+unavailable['process_status'],
                                'delivery_status':unavailable['delivery_status'],'process_status':unavailable['process_status']}
                return {'claimed':False,'reason':'already_claimed_or_unavailable'}
            if collaboration_state=='paused' and message['delivery_status'] not in ('accepted','delivering','unknown','manual_handoff'):
                return {'claimed':False,'reason':'collaboration_paused_before_delivery'}
            if message['kind']=='task':
                task=db.execute('SELECT * FROM tasks WHERE id=?',(message['task_id'],)).fetchone()
                if task['status']!='active':
                    return {'claimed':False,'reason':'task_not_active'}
            token=secrets.token_hex(24)
            if not self._assistance_claimable(db,message):
                return {'claimed':False,'reason':'assistance_waiting'}
            db.execute("UPDATE messages SET process_status='claimed',claim_token=?,updated=? WHERE id=?",(token,time.time(),message['id']))
            self.event(db,'message_claimed',message=message['id'],member=member_id)
            result=dict(db.execute('SELECT * FROM messages WHERE id=?',(message['id'],)).fetchone())
            result['claimed']=True
            if message['task_id']:
                result['task']=dict(db.execute('SELECT * FROM tasks WHERE id=?',(message['task_id'],)).fetchone())
            return result

    def read_message(self, member_id, session, cwd, message_id, *, field='body', offset=0, limit=2000):
        """领取后分段读取正文，不把长内容再次塞入工具回执。"""
        if field not in ('body','task') or offset<0 or not 1<=limit<=2000:
            raise PairError('Invalid page / 分页参数无效，limit 须在 1..2000')
        with self.transaction() as db:
            self._binding(db,member_id,session,cwd)
            message=db.execute('SELECT * FROM messages WHERE id=? AND recipient=?',(message_id,member_id)).fetchone()
            if not message or message['process_status'] not in ('claimed','completed'):
                raise PairError('Claim required / 必须由接收方先领取消息')
            task=db.execute('SELECT * FROM tasks WHERE id=?',(message['task_id'],)).fetchone() if message['task_id'] else None
            content=message['body'] if field=='body' else dumps(dict(task) if task else None)
            end=min(offset+limit,len(content))
            return {'id':message_id,'field':field,'text':content[offset:end],
                    'total':len(content),'next_offset':end if end<len(content) else None}

    def recover_delivery(self, message_id):
        """只恢复由本地长度预检明确拒绝、从未提交的消息，保留尝试历史。"""
        prefix='Queue message exceeds Windows command-line limit / '
        with FileLock(self.control_lock,timeout=30),self.transaction() as db:
            message=db.execute('SELECT * FROM messages WHERE id=?',(message_id,)).fetchone()
            attempts=db.execute('SELECT outcome,detail FROM attempts WHERE message_id=?',(message_id,)).fetchall()
            if (not message or message['delivery_status']!='failed' or message['process_status']!='unclaimed'
                    or message['turn_id'] or message['queue_id'] or message['remote_status']
                    or not (message['error'] or '').startswith(prefix)
                    or not attempts or any(a['outcome']!='failed' or not (a['detail'] or '').startswith(prefix) for a in attempts)):
                return {'recovered':False,'reason':'not_proven_unsent_length_failure'}
            if db.execute('SELECT status FROM collaborations').fetchone()[0]=='stopped':
                return {'recovered':False,'reason':'collaboration_not_running'}
            db.execute("UPDATE messages SET delivery_status='pending',envelope=NULL,next_attempt=0,updated=? WHERE id=?",(time.time(),message_id))
            self.event(db,'delivery_recovered',message=message_id,reason='proven_unsent_length_failure',previous_error=message['error'])
            return {'recovered':True,'id':message_id,'delivery_status':'pending','history_preserved':True}

    def recover_claim(self, member_id, session, cwd, message_id):
        """原领取者显式取回遗失的回执；不重置消息、不生成令牌、不重新投递。"""
        with self.transaction() as db:
            self._binding(db,member_id,session,cwd)
            if db.execute('SELECT status FROM collaborations').fetchone()[0]=='stopped':
                return {'recovered':False,'reason':'collaboration_not_running'}
            message=db.execute('SELECT * FROM messages WHERE id=? AND recipient=?',
                               (message_id,member_id)).fetchone()
            if not message or message['process_status']!='claimed' or not message['claim_token']:
                return {'recovered':False,'reason':'no_owned_claim'}
            if message['remote_status'] in ('queued','inProgress') or message['delivery_status'] in ('delivering','unknown'):
                return {'recovered':False,'reason':'previous_turn_unresolved'}
            if message['kind']=='task' and message['task_id']:
                task=db.execute('SELECT status FROM tasks WHERE id=?',(message['task_id'],)).fetchone()
                if not task or task['status']!='active':
                    return {'recovered':False,'reason':'task_not_active'}
            return {'recovered':True,'id':message['id'],'task_id':message['task_id'],
                    'kind':message['kind'],'claim_token':message['claim_token'],
                    'delivery_id':message['delivery_id'],
                    'process_status':message['process_status'],'resume_only':True}

    def complete(self, member_id, session, cwd, message_id, token, result, *, commits=(), partial=False, goal_complete=False):
        if not result.strip():
            raise PairError('Completion requires a meaningful result / 完成任务需要实质结果')
        if any(not re.fullmatch(r'[0-9a-fA-F]{40,64}',c) for c in commits):
            raise PairError('Use full commit hashes / 请使用完整提交 SHA')
        with self.transaction() as db:
            member=self._binding(db,member_id,session,cwd)
            message=db.execute('SELECT * FROM messages WHERE id=?',(message_id,)).fetchone()
            if not message or message['recipient']!=member_id or not secrets.compare_digest(message['claim_token'] or '',token):
                raise PairError('Invalid claim token or owner / 领取令牌或领取者不符')
            if message['process_status']=='completed':
                if message['kind']=='task' and db.execute('SELECT assignee FROM tasks WHERE id=?',(message['task_id'],)).fetchone()[0]!=member_id:
                    return {'completed':False,'duplicate':True,'transferred':True}
                return {'completed':True,'duplicate':True}
            if message['process_status']!='claimed':
                raise PairError('Message must be claimed / 必须先领取消息')
            planning=message['kind']=='task' and (message['task_id'] or '').startswith('pair-plan-')
            if goal_complete and not planning:raise PairError('Planning task required / 只能在自动规划任务中确认目标完成')
            if planning:
                if commits or partial:raise PairError('Planning is read-only / 规划任务不交付代码提交或中途交回')
                plan=db.execute('SELECT created FROM tasks WHERE id=?',(message['task_id'],)).fetchone()
                if goal_complete:
                    if db.execute("SELECT 1 FROM tasks WHERE id<>? AND status NOT IN ('completed','integrated') LIMIT 1",(message['task_id'],)).fetchone():
                        raise PairError('Work remains / 仍有未完成工作，不能确认目标完成')
                    db.execute("INSERT OR REPLACE INTO pair_settings VALUES('goal_completed','true')")
                elif not db.execute("SELECT 1 FROM tasks WHERE id NOT LIKE 'pair-plan-%' AND created>=? LIMIT 1",(plan['created'],)).fetchone():
                    raise PairError('Register next work / 请先登记后续任务，或用 --goal-complete 确认原目标完成')
            if message['delivery_id']:self._require_ack(db,member_id,message['delivery_id'])
            if message['kind']=='task':
                for record in db.execute('''SELECT r.id FROM dependencies d JOIN deliveries r ON r.id='delivery:'||d.depends_on WHERE d.task_id=?''',(message['task_id'],)).fetchall():
                    self._require_ack(db,member_id,record[0])
            elif partial:raise PairError('Partial applies to tasks / 只有业务任务可中途交回')
            self._assistance_complete(db,member_id,message,token,result)
            db.execute("UPDATE messages SET process_status='completed',updated=? WHERE id=?",(time.time(),message_id))
            if member['delivery_mode']=='manual':
                # 手动用户已明确领取并完成，可以闭合文件落盘后崩溃留下的 unknown。
                # 这不是自动投递成功，也不会自动重发。
                db.execute("UPDATE messages SET delivery_status='manual_handoff',remote_status='completed' WHERE id=?",(message_id,))
            if message['kind']=='reply' and message['reply_to'] and member['delivery_mode']=='auto':
                original=db.execute('SELECT dedupe_key FROM messages WHERE id=?',(message['reply_to'],)).fetchone()
                if original and original['dedupe_key']=='window-handshake':
                    self._send(db,'B','A','两个当前窗口的连接验证回复已完成。领取并完成本通知，查看 status.window_flow.communication；确认 verified 后按 window_flow.goal 组织任务，不再要求用户配置账号或重新开窗口。',
                        kind='note',dedupe_key='window-ready')
            if message['kind']=='task':
                db.execute('UPDATE tasks SET status=?,result=?,commits_json=?,no_progress_rounds=0,updated=? WHERE id=?',
                           ('handed_back' if partial else ('delivered' if commits else 'completed'),result,dumps(list(commits)),time.time(),message['task_id']))
                task=dict(db.execute('SELECT * FROM tasks WHERE id=?',(message['task_id'],)).fetchone())
                if planning:
                    self.event(db,'continuation_planned',task=task['id'],goal_completed=goal_complete)
                elif partial:
                    self._delivery(db,task)
                    notification=self._send(db,member_id,task['report_to'],'任务中途交回，尚未完成。\n'+result[:600],
                        kind='result',task_id=task['id'],dedupe_key='partial:'+message_id)
                    db.execute('UPDATE messages SET delivery_id=? WHERE id=?',('delivery:'+task['id'],notification['id']))
                else:self._handoff(db,task,task['report_to'])
            self.event(db,'message_completed',message=message_id,member=member_id,result=result,commits=list(commits))
            self._schedule(db)
            self._stage_complete(db)
            self._schedule(db)
            return {'completed':True,'duplicate':False}

    def _stage_complete(self, db):
        if self._approval_unresolved(db) or db.execute("SELECT 1 FROM approval_requests WHERE retry_state IN ('waiting','ready','needs_user')").fetchone():return
        if db.execute("SELECT 1 FROM delegation_questions WHERE state NOT IN ('adopted','cancelled')").fetchone():return
        if db.execute("SELECT 1 FROM checkpoints WHERE status IN ('saved','offered')").fetchone():return
        if any(self._option(db,'quota_state:'+m[0],'ready')=='saving' for m in db.execute('SELECT id FROM members')):return
        if db.execute("SELECT 1 FROM execution_requests WHERE status NOT IN ('success','failed','declined','resolved')").fetchone():return
        flow=db.execute("SELECT value FROM pair_settings WHERE key='window_flow'").fetchone()
        policy=db.execute("SELECT value FROM pair_settings WHERE key='scheduling_policy'").fetchone()
        if not flow or not policy or json.loads(policy[0])!='quota-balanced':return
        if db.execute('SELECT status FROM collaborations').fetchone()[0]!='running':return
        if db.execute("SELECT 1 FROM tasks WHERE status NOT IN ('completed','integrated') LIMIT 1").fetchone():return
        if db.execute("SELECT 1 FROM messages WHERE kind='result' AND (process_status<>'completed' OR delivery_status IN ('unknown','failed')) LIMIT 1").fetchone():return
        tasks=[r[0] for r in db.execute("SELECT id FROM tasks WHERE id NOT LIKE 'pair-plan-%' ORDER BY id")]
        if not tasks:return
        if db.execute("SELECT 1 FROM pair_settings WHERE key='goal_completed' AND value='true'").fetchone():return
        stage=hashlib.sha256(dumps(tasks).encode()).hexdigest()
        task_id='pair-plan-'+stage[:24]
        if db.execute('SELECT 1 FROM tasks WHERE id=?',(task_id,)).fetchone():return
        goal=json.loads(flow[0])['goal']
        body=('这是独立的后续规划任务，不是结果消息。已有阶段结果均已读取确认并收尾。'
              '请核对原目标和交付证据，只为原目标尚未完成且已授权的工作登记后续任务、允许路径、effort、report_to及depends-on。'
              '然后完成本规划任务并结束回合，由消息桥自动投递后续任务；两个窗口无需用户再次输入。'
              '本规划任务只允许读取与登记，不修改业务文件、不重跑已完成任务、不重新start。'
              '若原目标已经全部实现，用complete --goal-complete并附验收依据完成本任务，停止自动接续。'
              '需要新的授权或关键用户信息时，登记blocked任务说明具体缺口，不伪造完成或自行扩大范围。\n原目标：\n'+goal)
        now=time.time()
        base=db.execute('SELECT base_commit FROM collaborations').fetchone()[0]
        coordinator=self._coordinator(db)
        db.execute('INSERT INTO tasks(id,title,body,assignee,allowed_paths,status,created,updated,base_commit,report_to,effort,assignment_mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                   (task_id,'核验原目标并登记后续工作',body,coordinator,'[]','pending',now,now,base,coordinator,1,'fixed'))
        db.executemany('INSERT INTO dependencies VALUES(?,?)',[(task_id,parent) for parent in tasks])
        self.event(db,'continuation_registered',task=task_id,stage=stage)

    def control(self, state):
        if state not in ('running','paused','stopped'):
            raise PairError('Invalid control state / 无效控制状态')
        # 和真正开始投递持有同一锁；返回后任何后续投递都会先看到新状态。
        with FileLock(self.control_lock,timeout=30), self.transaction() as db:
            db.execute('UPDATE collaborations SET status=?,revision=revision+1',(state,))
            if state!='running':self._invalidate_approvals(db,'Collaboration paused or stopped / 协作暂停或停止')
            self.event(db,'control',state=state)
            if state=='running':
                self._schedule(db)
                self._stage_complete(db)
        return {'status':state,'records_preserved':True}

    def recover(self, owner, member_id=None):
        with self.transaction() as db:
            interrupted=db.execute("SELECT id FROM messages WHERE delivery_status='delivering'"+(' AND recipient=?' if member_id else ''),([member_id] if member_id else [])).fetchall()
            for row in interrupted:
                db.execute("UPDATE messages SET delivery_status='unknown',error='daemon_crash_during_delivery',updated=? WHERE id=?",(time.time(),row['id']))
                db.execute("UPDATE attempts SET outcome='unknown',ended=? WHERE message_id=? AND outcome='started'",(time.time(),row['id']))
                self.event(db,'delivery_unknown',message=row['id'],reason='daemon_restart')
            now=time.time()
            if member_id:
                db.execute('INSERT INTO bridge_workers(member,owner,pid,heartbeat) VALUES(?,?,?,?) ON CONFLICT(member) DO UPDATE SET owner=excluded.owner,pid=excluded.pid,heartbeat=excluded.heartbeat,probe_ack=NULL,stop_requested=0',(member_id,owner,os.getpid(),now))
            else:
                db.execute('UPDATE runtime SET owner=?,pid=?,started=?,heartbeat=?,probe_ack=NULL WHERE id=1',(owner,os.getpid(),now,now))
            self.event(db,'daemon_started',owner=owner,recovered=len(interrupted))

    def heartbeat(self, owner, member_id=None):
        with self.transaction() as db:
            if member_id:
                db.execute('UPDATE bridge_workers SET heartbeat=?,probe_ack=probe WHERE member=? AND owner=?',(time.time(),member_id,owner))
            else:
                db.execute('UPDATE runtime SET heartbeat=?,probe_ack=probe WHERE id=1 AND owner=?',(time.time(),owner))

    def status(self, *, probe_timeout=0):
        nonce=None
        if probe_timeout:
            nonce=secrets.token_hex(12)
            with self.transaction() as db:
                db.execute('UPDATE runtime SET probe=? WHERE id=1',(nonce,))
                db.execute('UPDATE bridge_workers SET probe=?',(nonce,))
            deadline=time.monotonic()+probe_timeout
            while time.monotonic()<deadline:
                with self.transaction() as db:
                    if db.execute('SELECT probe_ack FROM runtime').fetchone()[0]==nonce or db.execute('SELECT 1 FROM bridge_workers WHERE probe_ack=?',(nonce,)).fetchone():
                        break
                time.sleep(.02)
        lock=FileLock(self.daemon_lock)
        try:
            lock.acquire();lock.release();held=False
        except PairError:
            held=True
        with self.transaction() as db:
            result={'pair':dict(db.execute('SELECT * FROM collaborations').fetchone()),
                    'members':[dict(r) for r in db.execute('SELECT id,session_uuid,cwd,backend,owned,codex_home,delivery_mode,proof FROM members')],
                    'tasks':[dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY created')],
                    'messages':[dict(r) for r in db.execute('SELECT id,sender,recipient,task_id,kind,delivery_status,process_status,attempts,queue_id,turn_id,remote_status,remote_result,error,created,updated FROM messages ORDER BY created')],
                    'integrations':[dict(r) for r in db.execute('SELECT * FROM integrations ORDER BY created')],
                    'runtime':dict(db.execute('SELECT * FROM runtime').fetchone()),
                    'relays':[dict(r) for r in db.execute('SELECT * FROM bridge_workers')],
                    'recent_events':[dict(r) for r in db.execute('SELECT * FROM events ORDER BY seq DESC LIMIT 30')],
                    'last_event':db.execute('SELECT max(seq) FROM events').fetchone()[0], 'state_directory':str(self.directory)}
            result['quota']=self._quotas(db)
            result['assistance']=self._assistance_status(db)
            burden=pair_balance.loads(db)
            total=sum(burden.values())
            result['balance']={'policy':json.loads(db.execute("SELECT value FROM pair_settings WHERE key='scheduling_policy'").fetchone()[0]),
                'loads':burden,'shares':{m:burden[m]/total if total else None for m in burden},'soft_target':[.4,.6],
                'usage_turns':db.execute('SELECT count(*) FROM turn_usage').fetchone()[0],
                'reasons':{t['id']:t['assignment_reason'] for t in result['tasks']}}
            waiting=[]
            if result['assistance'].get('approval_hook',{} ) and result['assistance']['approval_hook'].get('status')=='installed_requires_trust':
                waiting.append({'reason':'B 的官方 PermissionRequest Hook 已安装，等待首次信任；尚未验证生效'})
            for approval in result['assistance']['approvals']:
                if approval['status']=='pending':waiting.append({'approval':approval['id'],'reason':'B 原审批由 Hook 等待 A 逐次审核'})
                if approval['retry_state'] in ('waiting','ready','needs_user'):
                    waiting.append({'approval':approval['id'],'reason':{'waiting':'等待 A 核实未执行并安排新请求','ready':'A 已准备审核，等待 B 核实后重新请求','needs_user':'连续两次超时，停止自动重试并通知用户'}[approval['retry_state']]})
                if approval['status']=='unknown' or approval['status']=='granted' and not approval['coordination_only']:waiting.append({'approval':approval['id'],'reason':'已返回允许或执行结果不明，等待核验实际结果'})
            for question in result['assistance']['questions']:
                if question['state'] not in ('adopted','cancelled'):
                    waiting.append({'question':question['id'],'reason':{'pending':'等待 A 回答','answered':'等待 B 确认采用回答','needs_user':'需要用户处理'}[question['state']]})
            for request in result['assistance']['requests']:
                reason={'review':'等待 A 审核执行请求','running':'A 执行中','needs_authorization':'等待用户补充授权','unknown':'执行结果不明，须核实后继续'}.get(request['status'])
                if reason:waiting.append({'request':request['id'],'reason':reason})
            for checkpoint in result['assistance']['checkpoints']:
                if checkpoint['status'] in ('saved','offered'):
                    waiting.append({'checkpoint':checkpoint['member'],'reason':'等待额度恢复或可接管窗口' if checkpoint['status']=='saved' else '等待完整阅读、核实与持久化接管'})
            for task in result['tasks']:
                reasons=[]
                for parent in db.execute('SELECT p.id,p.status FROM dependencies d JOIN tasks p ON p.id=d.depends_on WHERE d.task_id=?',(task['id'],)):
                    if parent['status']=='delivered':reasons.append('等待整合: '+parent['id'])
                    elif parent['status'] not in ('completed','integrated'):reasons.append('等待前置任务: '+parent['id'])
                if task['status']=='handed_back':reasons.append('等待核实中途交回与后续安排')
                if task['status']=='waiting_execution':reasons.append('等待 A 协助执行、核实或用户补充授权')
                if task['status']=='quota_wait':reasons.append('额度暂停，等待交接接收或恢复')
                if task['status']=='delivered':reasons.append('等待整合')
                if task['status']=='pending' and not reasons:reasons.append({'quota_exhausted':'等待额度恢复（已明确耗尽）','quota_refresh_pending':'等待额度刷新'}.get(task['assignment_reason'],'等待成员或工作树空闲'))
                if reasons:waiting.append({'task':task['id'],'reasons':reasons})
            for message in result['messages']:
                if message['delivery_status']=='unknown':waiting.append({'message':message['id'],'reason':'投递结果不明'})
                elif message['kind']=='result' and message['process_status']!='completed':waiting.append({'message':message['id'],'reason':'等待结果领取' if message['process_status']=='unclaimed' else '等待结果读取确认或收尾'})
                elif message['delivery_status']=='accepted' and message['remote_status']=='queued':waiting.append({'message':message['id'],'reason':'等待接收窗口执行队列消息'})
                elif message['delivery_status']=='accepted' and message['remote_status']=='inProgress':waiting.append({'message':message['id'],'reason':'等待模型回合结束（共享工作区串行保护）'})
            goal_done=db.execute("SELECT 1 FROM pair_settings WHERE key='goal_completed' AND value='true'").fetchone() is not None
            if result['pair']['status']=='running' and result['tasks'] and all(t['status'] in ('completed','integrated') for t in result['tasks']) and not goal_done:waiting.append({'reason':'等待自动登记后续规划任务'})
            result['waiting']=waiting
            result['runtime'].update(lock_held=held,probe_confirmed=bool(nonce and db.execute('SELECT probe_ack FROM runtime').fetchone()[0]==nonce))
            for member in result['members']:
                member['workspace']=member_workspace(member)
                member['connection']=provider_identity(json.loads(member['proof']))
                received=[m for m in result['messages'] if m['recipient']==member['id']]
                auto=member['delivery_mode']=='auto'
                member['capabilities']={
                    'session_identity':'verified',
                    'queue_delivery':('accepted' if auto and member['backend']=='queue' and any(m['delivery_status']=='accepted' for m in received) else 'unverified'),
                    'turn_completion':('verified' if auto and any(m['remote_status']=='completed' for m in received) else 'unverified'),
                    'business_processing':('verified' if any(m['process_status']=='completed' for m in received) else 'unverified'),
                    'task_tools':'unverified',
                    'automatic_communication':('verified' if auto and self.window_verified(db) else ('manual' if not auto else 'unverified'))}
                member.pop('proof')
            flow=db.execute("SELECT value FROM pair_settings WHERE key='window_flow'").fetchone()
            if flow:
                flow=json.loads(flow[0])
                verified=self.window_verified(db)
                manual=any(m['delivery_mode']=='manual' for m in result['members'])
                result['window_flow']={'goal':flow['goal'],'goal_completed':goal_done,'mode':'existing-windows','coordinator':self._coordinator(db),
                    'communication':'manual' if manual else ('verified' if verified else ('awaiting_peer' if len(result['members'])<2 else 'awaiting_handshake'))}
            for relay in result['relays']:
                relay['probe_confirmed']=bool(nonce and relay['probe_ack']==nonce)
                relay_lock=FileLock(self.directory/('relay-'+relay['member']+'.lock'))
                try:
                    relay_lock.acquire();relay_lock.release();relay['lock_held']=False
                except PairError:
                    relay['lock_held']=True
            if not held and not any(r['lock_held'] for r in result['relays']) and result['pair']['status']=='running':waiting.append({'reason':'消息桥不可用'})
            return result

    def envelope(self, member, message):
        import sys
        script=str(Path(__file__).with_name('pair.py').resolve())
        common=[sys.executable,script,'--pair',self.pair_id,'--state-root',str(self.state_root),'--codex-home',member['codex_home'],'--json']
        binding=['--member',member['id'],'--session',member['session_uuid'],'--worktree',member['cwd']]
        with self.transaction() as db:
            task=db.execute('SELECT * FROM tasks WHERE id=?',(message['task_id'],)).fetchone() if message['task_id'] else None
            required=[r[0] for r in db.execute("SELECT r.id FROM dependencies d JOIN deliveries r ON r.id='delivery:'||d.depends_on WHERE d.task_id=?",(message['task_id'],))] if message['kind']=='task' else []
        if message.get('delivery_id'):required.append(message['delivery_id'])
        payload={'protocol':'codex-pair/1','message_id':message['id'],'pair_id':self.pair_id,
                 'sender':message['sender'],'recipient':message['recipient'],'kind':message['kind'],
                 'task':dict(task) if task else None,'body':message['body'],
                 'skill_path':str(Path(script).parent.parent/'SKILL.md'),
                 'state_directory':str(self.directory),
                 'state_root':str(self.state_root),'member_codex_home':member['codex_home'],
                 'actor_cwd':member['cwd'],'workspace':member_workspace(member),
                 'instructions':'先在 actor_cwd 作为工具工作目录运行 claim_argv，只有 claimed=true 才执行；然后阅读 skill_path 和 references/protocol.md。任务文件与 Git 操作使用 workspace；claim/complete 仍从 actor_cwd 执行，不修改会话根目录。领取回执使用 --compact，避免工具截断 JSON；解析失败不代表领取失败，不得自动重试 claim。普通重复领取返回 false 时立即结束。原领取回执确实遗失时，仅原会话可显式 recover-claim 取回原令牌，核实进度后继续，不能从头重复执行。完成后调用 complete，带回 claim_token 与实质结果。提问后结束回合，保留原任务令牌；收到回复后完成回复消息并继续原任务。不要自行重派任务。',
                 'claim_argv':common+['claim']+binding+['--message',message['id'],'--compact'],
                 'complete_argv_template':common+['complete']+binding+['--message',message['id'],'--token','<claim_token>','--body-file','<UTF-8-result-file>']}
        if required:
            payload['required_deliveries']=required
            payload['read_delivery_argv_template']=common+['read-delivery']+binding+['--delivery','<delivery_id>']
            payload['ack_delivery_argv_template']=common+['ack-delivery']+binding+['--delivery','<delivery_id>','--digest','<digest>']
            payload['instructions']+=' 必须逐条 read-delivery 分页读至 next_offset=null，再用对应 digest 执行 ack-delivery；结果消息只阅读和收尾，不重新执行原任务。'
        if message['kind']=='result':payload['task']={'id':task['id'],'status':task['status']} if task else None
        payload['checkpoint_check_argv']=common+['check-checkpoint']+binding
        payload['instructions']+=' 每次恢复工作先执行 checkpoint_check_argv；存在交接则完整读取并持久化接管后清理，缺失则不读取历史。协助执行、额度交接消息先读 references/assistance.md，不能直接 complete 代替专用接口。'
        payload['instructions']+=' 委托问答、原生审批和超时通知先读 references/approval-review.md。普通提问使用 ask-question 并结束回合；回答不替代权限批准。用户变更授权时 A 先登记 delegation-policy；审批只用 review-approval，超时重发先用 prepare-approval-retry，获批操作结束后 B 用 report-approval 记录结果。'
        encoded=json.dumps(payload,ensure_ascii=True)
        if len(encoded)>8000:
            payload['body']=None
            payload['task']=None
            payload['content_external']=True
            payload['read_argv']=common+['read-message']+binding+['--message',message['id']]
            payload['instructions']+=' 长正文保存在原数据库中；领取成功后执行 read_argv，按 next_offset 追加 --offset 读取直到 null；任务消息还须加 --field task 分页读取任务范围与基准。完整阅读后再执行，不能把空 body 当作空任务。'
            encoded=json.dumps(payload,ensure_ascii=True)
        return encoded


class Daemon:
    def __init__(self, store, transport, *, max_attempts=3, poll_interval=.25, member_id=None):
        if max_attempts<1 or max_attempts>10 or poll_interval<=0:
            raise PairError('Invalid retry or polling limit / 无效重试或轮询参数')
        self.store,self.transport=store,transport
        self.max_attempts,self.poll_interval=max_attempts,poll_interval
        self.owner=secrets.token_hex(16)
        self.member_id=member_id
        self._observe_errors={}

    def _begin(self):
        with self.store.transaction() as db:
            if db.execute('SELECT status FROM collaborations').fetchone()[0]!='running':
                return None
            candidates=db.execute("""SELECT m.* FROM messages m WHERE m.delivery_status IN ('pending','retry')
                AND m.process_status='unclaimed' AND m.next_attempt<=?
                AND m.attempts-(SELECT count(*) FROM attempts a WHERE a.message_id=m.id AND a.outcome='failed'
                    AND a.detail LIKE 'Queue message exceeds Windows command-line limit / %')<? AND NOT EXISTS
                (SELECT 1 FROM messages busy WHERE busy.recipient=m.recipient AND busy.id<>m.id
                 AND (busy.delivery_status IN ('delivering','unknown') OR
                      (busy.delivery_status='accepted' AND (busy.remote_status IS NULL OR busy.remote_status IN ('inProgress','queued')))))
                AND (m.kind<>'task' OR EXISTS (SELECT 1 FROM tasks t WHERE t.id=m.task_id AND t.status='active'))
                AND (? IS NULL OR m.recipient=?)
                ORDER BY m.created,m.id""",(time.time(),self.max_attempts,self.member_id,self.member_id)).fetchall()
            members={r['id']:dict(r) for r in db.execute('SELECT * FROM members')}
            busy=db.execute("SELECT recipient FROM messages WHERE delivery_status IN ('delivering','unknown') OR (delivery_status='accepted' AND (remote_status IS NULL OR remote_status IN ('queued','inProgress')))").fetchall()
            busy_workspaces={member_workspace(members[r['recipient']]) for r in busy}
            busy_workspaces.update(member_workspace(members[r['member']]) for r in db.execute("SELECT member FROM approval_requests WHERE status='pending'"))
            message=next((m for m in candidates if self.store._assistance_dispatchable(db,m)
                and (m['kind']=='approval_review' or member_workspace(members[m['recipient']]) not in busy_workspaces)
                and (m['dedupe_key']!='window-ready' or self.store.window_verified(db))),None)
            if not message:return None
            if message['task_id'] and message['kind']=='task':
                if db.execute('SELECT status FROM tasks WHERE id=?',(message['task_id'],)).fetchone()[0]!='active':
                    return None
            now=time.time()
            db.execute("UPDATE messages SET delivery_status='delivering',attempts=attempts+1,updated=? WHERE id=?",(now,message['id']))
            db.execute("INSERT INTO attempts(message_id,number,outcome,started) VALUES(?,?,'started',?)",(message['id'],message['attempts']+1,now))
            self.store.event(db,'delivery_started',message=message['id'],attempt=message['attempts']+1)
            return self.store._member(db,message['recipient']),dict(message)

    def dispatch_one(self):
        with FileLock(self.store.control_lock,timeout=30):
            selected=self._begin()
            if not selected:return False
            member,message=selected
            # 这里只处理尚未提交的 pending/retry；绝不改写 unknown/accepted 的对账信封。
            if not message['envelope'] or len(message['envelope'])>8000 or (message['delivery_id'] and not json.loads(message['envelope']).get('required_deliveries')):
                message['envelope']=self.store.envelope(member,message)
            with self.store.transaction() as db:
                db.execute('UPDATE messages SET envelope=? WHERE id=?',(message['envelope'],message['id']))
            try:
                if member['delivery_mode']=='manual':
                    receipt=self.manual_handoff(member,message)
                else:
                    receipt=self.transport.start(member,message)
            except SessionBusy as exc:
                with self.store.transaction() as db:
                    db.execute("UPDATE messages SET delivery_status='pending',attempts=attempts-1,next_attempt=?,updated=? WHERE id=?",(time.time()+1,time.time(),message['id']))
                    db.execute("UPDATE attempts SET outcome='deferred',detail=?,ended=? WHERE message_id=? AND outcome='started'",(str(exc),time.time(),message['id']))
                    self.store.event(db,'delivery_deferred',message=message['id'],reason='session_busy_before_submission')
            except DeliveryUnknown as exc:
                self._failed(message,'unknown',str(exc))
            except TransportError as exc:
                retry=exc.retryable and message['attempts']+1<self.max_attempts
                self._failed(message,'retry' if retry else 'failed',str(exc))
            except Exception as exc:
                # 非预期异常也可能发生在提交请求之后，必须保守地保留结果不明。
                self._failed(message,'unknown',repr(exc))
            else:
                with self.store.transaction() as db:
                    db.execute("UPDATE messages SET delivery_status=?,turn_id=?,remote_status=?,queue_id=?,updated=? WHERE id=?",
                               (receipt.get('delivery_status','accepted'),receipt['turn_id'],receipt['remote_status'],receipt.get('queue_id'),time.time(),message['id']))
                    db.execute("UPDATE attempts SET outcome='accepted',ended=? WHERE message_id=? AND outcome='started'",(time.time(),message['id']))
                    self.store.event(db,'delivery_accepted',message=message['id'],turn=receipt['turn_id'])
            return True

    def manual_handoff(self, member, message):
        try:
            from .pair_git import prepare_pair_directory
        except ImportError:
            from pair_git import prepare_pair_directory
        project=self.store.status()['pair']['project']
        directory=Path(prepare_pair_directory(project,str(Path(project)/'.codex-pair'/'handoffs'/self.store.pair_id),'handoffs'))
        directory.mkdir(parents=True,exist_ok=True)
        path=directory/(message['id']+'.json')
        payload={'mode':'manual','label':'手动交接：目标 CLI 尚未自动唤醒',
                 'member':member['id'],'message_id':message['id'],
                 'state_root':str(self.store.state_root),
                 'claim_argv':json.loads(message['envelope'])['claim_argv']}
        try:
            with path.open('x',encoding='utf-8') as file:json.dump(payload,file,ensure_ascii=False,indent=2)
        except FileExistsError:
            if json.loads(path.read_text(encoding='utf-8'))!=payload:
                raise PairError('Handoff differs / 已有交接文件不符，拒绝覆盖')
        with self.store.transaction() as db:
            self.store.event(db,'manual_handoff',message=message['id'],path=str(path),automatic=False)
        return {'delivery_status':'manual_handoff','turn_id':None,'remote_status':'manual'}

    def _failed(self,message,state,error):
        with self.store.transaction() as db:
            db.execute('UPDATE messages SET delivery_status=?,error=?,next_attempt=?,updated=? WHERE id=?',
                       (state,error,time.time()+2**message['attempts'],time.time(),message['id']))
            db.execute("UPDATE attempts SET outcome=?,detail=?,ended=? WHERE message_id=? AND outcome='started'",(state,error,time.time(),message['id']))
            self.store.event(db,'delivery_'+state,message=message['id'],error=error)

    def reconcile(self):
        with self.store.transaction() as db:
            messages=[dict(r) for r in db.execute("SELECT * FROM messages WHERE delivery_status IN ('accepted','unknown') AND (remote_status IS NULL OR remote_status IN ('inProgress','queued')) AND (? IS NULL OR recipient=?)",(self.member_id,self.member_id))]
        for message in messages:
            if self.store.member(message['recipient'])['delivery_mode']=='manual':
                # 文件存在也不代表目标执行；未知状态须由操作者检查后在目标领取。
                continue
            if message['kind']=='task' and message['process_status']=='claimed':
                with self.store.transaction() as db:
                    # 只在已持久化协助请求后允许将后续完成回合作为让出依据，业务任务仍未完成。
                    message['protocol_yield']=bool(db.execute("SELECT 1 FROM execution_requests r JOIN tasks t ON t.id=r.task_id WHERE r.source_message=? AND r.status='review' AND t.status='waiting_execution'",(message['id'],)).fetchone())
                    # 原会话恢复后，原窗口真实获准的协调回合也可提供明确的接续完成依据。
                    if not self.store._approval_unresolved(db,task_id=message['task_id'],grants_only=True):
                        approvals=db.execute("SELECT * FROM approval_requests WHERE source_message=? AND member=? AND status IN ('granted','success')",(message['id'],message['recipient'])).fetchall()
                        message['protocol_yield_turns']=[json.loads(r['event'])['turn_id'] for r in approvals if self.store._coordination_approval(db,r)]
            try:
                receipt=self.transport.observe(self.store.member(message['recipient']),message)
            except TransportError as exc:
                if self._observe_errors.get(message['id'])!=str(exc):
                    with self.store.transaction() as db:
                        self.store.event(db,'observation_unavailable',message=message['id'],error=str(exc))
                    self._observe_errors[message['id']]=str(exc)
                continue  # 无回复、断线、等待批准均不重派正在执行的任务。
            if receipt:
                with self.store.transaction() as db:
                    db.execute("UPDATE messages SET delivery_status='accepted',turn_id=?,remote_status=?,remote_result=?,error=?,updated=? WHERE id=?",
                               (receipt['turn_id'],receipt['remote_status'],dumps({'messages':receipt.get('result'),'error':receipt.get('error'),
                                'completion_source':receipt.get('completion_source'),'evidence_turn_id':receipt.get('evidence_turn_id')}),
                                dumps(receipt['error']) if receipt.get('error') else None,time.time(),message['id']))
                    if receipt['remote_status']!=message['remote_status']:
                        self.store.event(db,'remote_turn_status',message=message['id'],status=receipt['remote_status'],
                            completion_source=receipt.get('completion_source'))

    def run(self, *, max_ticks=None):
        with ExitStack() as locks:
            # 同一成员的 relay 与中央 dispatcher 互斥；锁不会因超时被抢占。
            # status 的只读探测也会短暂取得此锁；给瞬时探测让路，
            # 真正存活的守护进程仍持锁，超时后拒绝重复启动，不抢占。
            gate=FileLock(self.store.daemon_lock,timeout=1).acquire()
            try:
                for member in ([self.member_id] if self.member_id else ['A','B']):
                    locks.enter_context(FileLock(self.store.directory/('relay-'+member+'.lock')))
                if self.member_id:gate.release()
                else:locks.callback(gate.release)
            except BaseException:
                gate.release();raise
            self.store.recover(self.owner,self.member_id)
            ticks=0
            quota_monitor=None
            if hasattr(self.transport,'signals'):
                try:from .pair_quota import QuotaMonitor
                except ImportError:from pair_quota import QuotaMonitor
                quota_monitor=QuotaMonitor(self.store,executable=self.transport.options.get('executable'),member_id=self.member_id)
                quota_monitor.start()
            next_schedule=0
            try:
                while True:
                    self.store.heartbeat(self.owner,self.member_id)
                    with self.store.transaction() as db:
                        state=db.execute('SELECT status FROM collaborations').fetchone()[0]
                        if self.member_id and db.execute('SELECT stop_requested FROM bridge_workers WHERE member=?',(self.member_id,)).fetchone()[0]:break
                    if state=='stopped':break
                    self.reconcile()
                    if hasattr(self.transport,'signals'):
                        for member,event in self.transport.signals():
                            if event.get('method')=='thread/tokenUsage/updated':self.store.record_usage(member,event.get('params',{}))
                            elif quota_monitor:quota_monitor.refresh_event.set()
                    if hasattr(self.transport,'pending_requests'):
                        for request in self.transport.pending_requests():
                            with self.store.transaction() as db:
                                self.store.event(db,'server_request_pending',**request)
                    if time.monotonic()>=next_schedule:
                        self.store.schedule()
                        next_schedule=time.monotonic()+10
                    self.dispatch_one()
                    ticks+=1
                    if max_ticks is not None and ticks>=max_ticks:break
                    time.sleep(self.poll_interval)
            finally:
                if quota_monitor:quota_monitor.close()
                self.transport.close()
                with self.store.transaction() as db:
                    self.store.event(db,'daemon_exited',owner=self.owner,member=self.member_id)
