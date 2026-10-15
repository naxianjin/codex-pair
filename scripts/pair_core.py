"""事务性任务、持久化发件箱与本地守护进程。不根据回复耗时重派任务。"""
from __future__ import annotations

from contextlib import closing, contextmanager
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
    from .pair_transport import DeliveryUnknown, TransportError, canonical_path, session_uuid
except ImportError:
    from pair_transport import DeliveryUnknown, TransportError, canonical_path, session_uuid


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


class Store:
    def __init__(self, pair_id, *, home=None, create=False):
        self.pair_id = identifier(pair_id)
        self.home = Path(home or effective_home()).resolve()
        self.directory = self.home/'codex-pair'/pair_id
        self.path = self.directory/'state.sqlite3'
        if not create and not self.path.is_file():
            raise PairError('Pair not initialized / 协作尚未初始化: '+pair_id)
        self.control_lock = self.directory/'dispatch.lock'
        self.daemon_lock = self.directory/'daemon.lock'
        if not create:
            self._upgrade()

    def _upgrade(self):
        with closing(sqlite3.connect(self.path)) as db:
            version=db.execute('PRAGMA user_version').fetchone()[0]
        if version==2:return
        if version!=1:raise PairError('Unsupported schema / 不支持的数据库版本')
        with FileLock(self.control_lock,timeout=30),closing(sqlite3.connect(self.path)) as db:
            if db.execute('PRAGMA user_version').fetchone()[0]==1:db.executescript(MIGRATION_2)

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
    def initialize(cls, pair_id, project, base_commit, *, home=None):
        project = canonical_path(project)
        store = cls(pair_id, home=home, create=True)
        if store.directory.is_relative_to(Path(project)):
            raise PairError('State must be outside the project / 状态库必须位于项目仓库之外')
        store.directory.mkdir(parents=True, exist_ok=True)
        with FileLock(store.control_lock, timeout=15):
            db = sqlite3.connect(store.path)
            try:
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version == 0:
                    db.execute('PRAGMA journal_mode=WAL')
                    db.executescript(SCHEMA)
                elif version not in (1,2):
                    raise PairError('Unsupported schema / 不支持的数据库版本')
                if db.execute('PRAGMA user_version').fetchone()[0]==1:db.executescript(MIGRATION_2)
            finally:
                db.close()
            with store.transaction() as db:
                existing = db.execute('SELECT * FROM collaborations').fetchone()
                if existing:
                    if existing['id'] != pair_id or existing['project'] != project or existing['base_commit'] != base_commit:
                        raise PairError('Existing pair differs / 已有协作的项目或基准不同，拒绝覆盖')
                else:
                    db.execute('INSERT INTO collaborations(id,project,base_commit,status,created) VALUES(?,?,?,?,?)',
                               (pair_id, project, base_commit, 'running', time.time()))
                    store.event(db, 'initialized', project=project, base_commit=base_commit)
        return store

    def _member(self, db, member_id):
        row = db.execute('SELECT * FROM members WHERE id=?', (member_id,)).fetchone()
        if not row:
            raise PairError('Member not joined / 成员尚未加入: '+member_id)
        return dict(row)

    def member(self, member_id):
        with self.transaction() as db:
            return self._member(db, member_id)

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

    def join(self, member_id, session, cwd, *, backend, owned, proof):
        if member_id not in ('A','B'):
            raise PairError('Member must be A or B / 成员必须为 A 或 B')
        session, cwd = session_uuid(session), canonical_path(cwd)
        if backend not in ('managed','shared','queue') or (backend == 'managed' and not owned):
            raise PairError('Managed session must be pair-owned / 独立会话必须由本协作创建')
        if session_uuid(proof['id']) != session or canonical_path(proof['cwd']) != cwd:
            raise PairError('Binding proof mismatch / 会话绑定证据不符')
        if proof.get('ephemeral'):
            raise PairError('Ephemeral session rejected / 不接受无法恢复的临时会话')
        with self.transaction() as db:
            existing=db.execute('SELECT * FROM members WHERE id=?',(member_id,)).fetchone()
            if existing:
                if (existing['session_uuid'],existing['cwd'],existing['backend']) != (session,cwd,backend):
                    raise PairError('Member already bound / 成员已有不同绑定，拒绝替换执行中的会话')
                return dict(existing)
            try:
                db.execute('INSERT INTO members VALUES(?,?,?,?,?,?,?)',
                           (member_id,session,cwd,backend,int(owned),dumps(proof),time.time()))
            except sqlite3.IntegrityError as exc:
                raise PairError('Session or worktree already bound / 会话或工作目录已绑定') from exc
            self.event(db,'member_joined', member=member_id, session=session, cwd=cwd, backend=backend)
            return self._member(db,member_id)

    def _binding(self, db, member_id, session, cwd):
        member=self._member(db,member_id)
        if member['session_uuid'] != session_uuid(session) or member['cwd'] != canonical_path(cwd):
            raise PairError('Invalid member session or cwd / 成员会话或实际工作目录不符')
        return member

    def _send(self, db, sender, recipient, body, *, kind='note', task_id=None,
              dedupe_key=None, reply_to=None, progress_note=None):
        self._member(db,sender);self._member(db,recipient)
        if (sender == recipient and kind != 'task') or not body.strip():
            raise PairError('Message needs another recipient and a body / 消息需要其他接收者和非空正文')
        if kind not in ('task','note','question','reply','result','blocked'):
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
        if kind=='reply':
            question=db.execute('SELECT * FROM messages WHERE id=?',(reply_to,)).fetchone()
            if not question or question['kind']!='question' or question['task_id']!=task_id or (question['sender'],question['recipient'])!=(recipient,sender):
                raise PairError('Reply must identify the matching question / 回复必须关联同任务的原问题')
            if db.execute("SELECT 1 FROM messages WHERE kind='reply' AND reply_to=?",(reply_to,)).fetchone():
                raise PairError('Question already answered / 问题已有回复')
        elif reply_to:
            raise PairError('reply_to only applies to replies / 只有回复可关联原问题')
        message_id=str(uuid.uuid4()); now=time.time()
        db.execute('INSERT INTO messages(id,sender,recipient,task_id,kind,body,dedupe_key,fingerprint,reply_to,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (message_id,sender,recipient,task_id,kind,body,dedupe_key,fingerprint,reply_to,now,now))
        self.event(db,'message_persisted',message=message_id,recipient=recipient,kind=kind)
        if kind=='reply' and task_id:
            db.execute('UPDATE tasks SET no_progress_rounds=CASE WHEN ? THEN 0 ELSE no_progress_rounds+1 END,updated=? WHERE id=?',
                       (bool(progress_note and progress_note.strip()),now,task_id))
            task=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if task['no_progress_rounds']>=3 and task['status']=='active':
                db.execute("UPDATE tasks SET status='blocked' WHERE id=?",(task_id,))
                self.event(db,'task_blocked', task=task_id, reason='three_rounds_without_progress')
                # A 自己发出的回复也要获得可审计的阻塞通知。
                other='B'
                self._send(db,other,'A','任务 '+task_id+' 连续三轮问答未提供实际进展证据，已阻塞；请检查原因并决定下一步。',
                           kind='blocked',task_id=task_id,dedupe_key='blocked:'+task_id)
        return dict(db.execute('SELECT * FROM messages WHERE id=?',(message_id,)).fetchone())

    def send(self, sender, recipient, body, **kwargs):
        with self.transaction() as db:
            return self._send(db,sender,recipient,body,**kwargs)

    def assign(self, task_id, title, assignee, allowed_paths, *, body='', dependencies=(),base_commit=None):
        identifier(task_id);allowed_paths=normalize_scopes(allowed_paths)
        if not title.strip():
            raise PairError('Task needs a title / 任务需要标题')
        with self.transaction() as db:
            self._member(db,'A');self._member(db,assignee)
            if db.execute('SELECT 1 FROM tasks WHERE id=?',(task_id,)).fetchone():
                raise PairError('Task already exists / 任务已存在')
            for dependency in dependencies:
                if not db.execute('SELECT 1 FROM tasks WHERE id=?',(dependency,)).fetchone():
                    raise PairError('Dependency not found / 依赖任务不存在: '+dependency)
            now=time.time()
            base_commit=base_commit or db.execute('SELECT base_commit FROM collaborations').fetchone()[0]
            db.execute('INSERT INTO tasks(id,title,body,assignee,allowed_paths,status,created,updated,base_commit) VALUES(?,?,?,?,?,?,?,?,?)',
                       (task_id,title,body,assignee,dumps(allowed_paths),'pending',now,now,base_commit))
            db.executemany('INSERT INTO dependencies VALUES(?,?)',[(task_id,d) for d in set(dependencies)])
            self.event(db,'task_assigned',task=task_id,assignee=assignee)
            self._schedule(db)
            return dict(db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone())

    def _schedule(self, db):
        if db.execute('SELECT status FROM collaborations').fetchone()[0]!='running':
            return
        active=list(db.execute("SELECT * FROM tasks WHERE status IN ('active','blocked')"))
        for task in db.execute("SELECT * FROM tasks WHERE status='pending' ORDER BY created,id").fetchall():
            unmet=db.execute("SELECT 1 FROM dependencies d JOIN tasks t ON t.id=d.depends_on WHERE d.task_id=? AND t.status NOT IN ('completed','integrated') LIMIT 1",(task['id'],)).fetchone()
            if unmet or any(a['assignee']==task['assignee'] or scopes_overlap(json.loads(a['allowed_paths']),json.loads(task['allowed_paths'])) for a in active):
                continue
            db.execute("UPDATE tasks SET status='active',updated=? WHERE id=?",(time.time(),task['id']))
            self._send(db,'A',task['assignee'],task['body'] or task['title'],kind='task',task_id=task['id'],dedupe_key='task:'+task['id'])
            active.append(task)
            self.event(db,'task_activated',task=task['id'])

    def schedule(self):
        with self.transaction() as db:
            self._schedule(db)

    def claim(self, member_id, session, cwd, *, message_id=None):
        with self.transaction() as db:
            self._binding(db,member_id,session,cwd)
            if db.execute('SELECT status FROM collaborations').fetchone()[0]!='running':
                return {'claimed':False,'reason':'collaboration_not_running'}
            params=[member_id]
            clause=' AND id=?' if message_id else ''
            if message_id:params.append(message_id)
            message=db.execute("SELECT * FROM messages WHERE recipient=? AND process_status='unclaimed' AND delivery_status<>'failed'"+clause+' ORDER BY created,id LIMIT 1',params).fetchone()
            if not message:
                return {'claimed':False,'reason':'already_claimed_or_unavailable'}
            if message['kind']=='task':
                task=db.execute('SELECT * FROM tasks WHERE id=?',(message['task_id'],)).fetchone()
                if task['status']!='active':
                    return {'claimed':False,'reason':'task_not_active'}
            token=secrets.token_hex(24)
            db.execute("UPDATE messages SET process_status='claimed',claim_token=?,updated=? WHERE id=?",(token,time.time(),message['id']))
            self.event(db,'message_claimed',message=message['id'],member=member_id)
            result=dict(db.execute('SELECT * FROM messages WHERE id=?',(message['id'],)).fetchone())
            result['claimed']=True
            if message['task_id']:
                result['task']=dict(db.execute('SELECT * FROM tasks WHERE id=?',(message['task_id'],)).fetchone())
            return result

    def complete(self, member_id, session, cwd, message_id, token, result, *, commits=()):
        if not result.strip():
            raise PairError('Completion requires a meaningful result / 完成任务需要实质结果')
        if any(not re.fullmatch(r'[0-9a-fA-F]{40,64}',c) for c in commits):
            raise PairError('Use full commit hashes / 请使用完整提交 SHA')
        with self.transaction() as db:
            self._binding(db,member_id,session,cwd)
            message=db.execute('SELECT * FROM messages WHERE id=?',(message_id,)).fetchone()
            if not message or message['recipient']!=member_id or not secrets.compare_digest(message['claim_token'] or '',token):
                raise PairError('Invalid claim token or owner / 领取令牌或领取者不符')
            if message['process_status']=='completed':
                return {'completed':True,'duplicate':True}
            if message['process_status']!='claimed':
                raise PairError('Message must be claimed / 必须先领取消息')
            db.execute("UPDATE messages SET process_status='completed',updated=? WHERE id=?",(time.time(),message_id))
            if message['kind']=='task':
                db.execute('UPDATE tasks SET status=?,result=?,commits_json=?,no_progress_rounds=0,updated=? WHERE id=?',
                           ('delivered' if commits else 'completed',result,dumps(list(commits)),time.time(),message['task_id']))
                if member_id!=message['sender']:
                    self._send(db,member_id,message['sender'],result,kind='result',task_id=message['task_id'],dedupe_key='result:'+message_id)
            self.event(db,'message_completed',message=message_id,member=member_id,result=result,commits=list(commits))
            self._schedule(db)
            return {'completed':True,'duplicate':False}

    def control(self, state):
        if state not in ('running','paused','stopped'):
            raise PairError('Invalid control state / 无效控制状态')
        # 和真正开始投递持有同一锁；返回后任何后续投递都会先看到新状态。
        with FileLock(self.control_lock,timeout=30), self.transaction() as db:
            db.execute('UPDATE collaborations SET status=?,revision=revision+1',(state,))
            self.event(db,'control',state=state)
        return {'status':state,'records_preserved':True}

    def recover(self, owner):
        with self.transaction() as db:
            interrupted=db.execute("SELECT id FROM messages WHERE delivery_status='delivering'").fetchall()
            for row in interrupted:
                db.execute("UPDATE messages SET delivery_status='unknown',error='daemon_crash_during_delivery',updated=? WHERE id=?",(time.time(),row['id']))
                db.execute("UPDATE attempts SET outcome='unknown',ended=? WHERE message_id=? AND outcome='started'",(time.time(),row['id']))
                self.event(db,'delivery_unknown',message=row['id'],reason='daemon_restart')
            now=time.time()
            db.execute('UPDATE runtime SET owner=?,pid=?,started=?,heartbeat=?,probe_ack=NULL WHERE id=1',(owner,os.getpid(),now,now))
            self.event(db,'daemon_started',owner=owner,recovered=len(interrupted))

    def heartbeat(self, owner):
        with self.transaction() as db:
            db.execute('UPDATE runtime SET heartbeat=?,probe_ack=probe WHERE id=1 AND owner=?',(time.time(),owner))

    def status(self, *, probe_timeout=0):
        nonce=None
        if probe_timeout:
            nonce=secrets.token_hex(12)
            with self.transaction() as db:
                db.execute('UPDATE runtime SET probe=? WHERE id=1',(nonce,))
            deadline=time.monotonic()+probe_timeout
            while time.monotonic()<deadline:
                with self.transaction() as db:
                    if db.execute('SELECT probe_ack FROM runtime').fetchone()[0]==nonce:
                        break
                time.sleep(.02)
        lock=FileLock(self.daemon_lock)
        try:
            lock.acquire();lock.release();held=False
        except PairError:
            held=True
        with self.transaction() as db:
            result={'pair':dict(db.execute('SELECT * FROM collaborations').fetchone()),
                    'members':[dict(r) for r in db.execute('SELECT id,session_uuid,cwd,backend,owned FROM members')],
                    'tasks':[dict(r) for r in db.execute('SELECT * FROM tasks ORDER BY created')],
                    'messages':[dict(r) for r in db.execute('SELECT id,sender,recipient,task_id,kind,delivery_status,process_status,attempts,queue_id,turn_id,remote_status,remote_result,error,created,updated FROM messages ORDER BY created')],
                    'integrations':[dict(r) for r in db.execute('SELECT * FROM integrations ORDER BY created')],
                    'runtime':dict(db.execute('SELECT * FROM runtime').fetchone()),
                    'recent_events':[dict(r) for r in db.execute('SELECT * FROM events ORDER BY seq DESC LIMIT 30')],
                    'last_event':db.execute('SELECT max(seq) FROM events').fetchone()[0], 'state_directory':str(self.directory)}
            result['runtime'].update(lock_held=held,probe_confirmed=bool(nonce and db.execute('SELECT probe_ack FROM runtime').fetchone()[0]==nonce))
            return result

    def envelope(self, member, message):
        import sys
        script=str(Path(__file__).with_name('pair.py').resolve())
        common=[sys.executable,script,'--pair',self.pair_id,'--codex-home',str(self.home),'--json']
        binding=['--member',member['id'],'--session',member['session_uuid'],'--worktree',member['cwd']]
        with self.transaction() as db:
            task=db.execute('SELECT * FROM tasks WHERE id=?',(message['task_id'],)).fetchone() if message['task_id'] else None
        payload={'protocol':'codex-pair/1','message_id':message['id'],'pair_id':self.pair_id,
                 'sender':message['sender'],'recipient':message['recipient'],'kind':message['kind'],
                 'task':dict(task) if task else None,'body':message['body'],
                 'skill_path':str(Path(script).parent.parent/'SKILL.md'),
                 'state_directory':str(self.directory),
                 'instructions':'先运行 claim_argv，只有 claimed=true 才执行；然后阅读 skill_path 和 references/protocol.md。领取失败则立即结束，不回复收到/谢谢。完成后调用 complete，带回 claim_token 与实质结果。提问后结束回合，保留原任务令牌；收到回复后完成回复消息并继续原任务。不要自行重派任务。',
                 'claim_argv':common+['claim']+binding+['--message',message['id']],
                 'complete_argv_template':common+['complete']+binding+['--message',message['id'],'--token','<claim_token>','--body-file','<UTF-8-result-file>']}
        return json.dumps(payload,ensure_ascii=True)


class Daemon:
    def __init__(self, store, transport, *, max_attempts=3, poll_interval=.25):
        if max_attempts<1 or max_attempts>10 or poll_interval<=0:
            raise PairError('Invalid retry or polling limit / 无效重试或轮询参数')
        self.store,self.transport=store,transport
        self.max_attempts,self.poll_interval=max_attempts,poll_interval
        self.owner=secrets.token_hex(16)
        self._observe_errors={}

    def _begin(self):
        with self.store.transaction() as db:
            if db.execute('SELECT status FROM collaborations').fetchone()[0]!='running':
                return None
            message=db.execute("""SELECT m.* FROM messages m WHERE m.delivery_status IN ('pending','retry')
                AND m.process_status='unclaimed' AND m.next_attempt<=? AND m.attempts<? AND NOT EXISTS
                (SELECT 1 FROM messages busy WHERE busy.recipient=m.recipient AND busy.id<>m.id AND busy.process_status<>'completed'
                 AND (busy.delivery_status IN ('delivering','unknown') OR
                      (busy.delivery_status='accepted' AND (busy.remote_status IS NULL OR busy.remote_status IN ('inProgress','queued')))))
                AND (m.kind<>'task' OR EXISTS (SELECT 1 FROM tasks t WHERE t.id=m.task_id AND t.status='active'))
                ORDER BY m.created,m.id LIMIT 1""",(time.time(),self.max_attempts)).fetchone()
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
            message['envelope']=message['envelope'] or self.store.envelope(member,message)
            with self.store.transaction() as db:
                db.execute('UPDATE messages SET envelope=? WHERE id=?',(message['envelope'],message['id']))
            try:
                receipt=self.transport.start(member,message)
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
                    db.execute("UPDATE messages SET delivery_status='accepted',turn_id=?,remote_status=?,queue_id=?,updated=? WHERE id=?",
                               (receipt['turn_id'],receipt['remote_status'],receipt.get('queue_id'),time.time(),message['id']))
                    db.execute("UPDATE attempts SET outcome='accepted',ended=? WHERE message_id=? AND outcome='started'",(time.time(),message['id']))
                    self.store.event(db,'delivery_accepted',message=message['id'],turn=receipt['turn_id'])
            return True

    def _failed(self,message,state,error):
        with self.store.transaction() as db:
            db.execute('UPDATE messages SET delivery_status=?,error=?,next_attempt=?,updated=? WHERE id=?',
                       (state,error,time.time()+2**message['attempts'],time.time(),message['id']))
            db.execute("UPDATE attempts SET outcome=?,detail=?,ended=? WHERE message_id=? AND outcome='started'",(state,error,time.time(),message['id']))
            self.store.event(db,'delivery_'+state,message=message['id'],error=error)

    def reconcile(self):
        with self.store.transaction() as db:
            messages=[dict(r) for r in db.execute("SELECT * FROM messages WHERE delivery_status IN ('accepted','unknown') AND (remote_status IS NULL OR remote_status IN ('inProgress','queued'))")]
        for message in messages:
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
                               (receipt['turn_id'],receipt['remote_status'],dumps({'messages':receipt.get('result'),'error':receipt.get('error')}),
                                dumps(receipt['error']) if receipt.get('error') else None,time.time(),message['id']))
                    if receipt['remote_status']!=message['remote_status']:
                        self.store.event(db,'remote_turn_status',message=message['id'],status=receipt['remote_status'])

    def run(self, *, max_ticks=None):
        with FileLock(self.store.daemon_lock):
            self.store.recover(self.owner)
            ticks=0
            try:
                while True:
                    self.store.heartbeat(self.owner)
                    with self.store.transaction() as db:
                        state=db.execute('SELECT status FROM collaborations').fetchone()[0]
                    if state=='stopped':break
                    self.reconcile()
                    if hasattr(self.transport,'pending_requests'):
                        for request in self.transport.pending_requests():
                            with self.store.transaction() as db:
                                self.store.event(db,'server_request_pending',**request)
                    self.store.schedule()
                    self.dispatch_one()
                    ticks+=1
                    if max_ticks is not None and ticks>=max_ticks:break
                    time.sleep(self.poll_interval)
            finally:
                self.transport.close()
                with self.store.transaction() as db:
                    self.store.event(db,'daemon_exited',owner=self.owner)
