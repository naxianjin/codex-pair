"""多人注册、独立成果审核与额度建议；不转移原 A 的权限审批资格。"""
from __future__ import annotations

import json
import math
from pathlib import Path
import time

MEMBERS = ('A', 'B', 'C', 'D')


class MembersMixin:
    def _upgrade_members(self, db, *, fresh=False):
        db.execute('PRAGMA foreign_keys=OFF')
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("""CREATE TABLE members_v9(id TEXT PRIMARY KEY CHECK(id IN ('A','B','C','D')),
                    session_uuid TEXT NOT NULL UNIQUE,cwd TEXT NOT NULL,backend TEXT NOT NULL,
                    owned INTEGER NOT NULL,proof TEXT NOT NULL,joined REAL NOT NULL,codex_home TEXT,
                    delivery_mode TEXT NOT NULL DEFAULT 'auto')""")
                db.execute('INSERT INTO members_v9 SELECT * FROM members')
                db.execute('DROP TABLE members')
                db.execute('ALTER TABLE members_v9 RENAME TO members')
                db.execute('''CREATE TABLE IF NOT EXISTS work_reviews(task_id TEXT PRIMARY KEY REFERENCES tasks(id),
                    version TEXT NOT NULL,contributors TEXT NOT NULL,required INTEGER NOT NULL,
                    reviewer TEXT,status TEXT NOT NULL,evidence TEXT,identity TEXT,updated REAL NOT NULL)''')
                self._put(db,'checkpoint_default',6 if fresh else 9)
                self._put(db,'coordinator_epoch',1)
                if db.execute('PRAGMA foreign_key_check').fetchall():
                    raise ValueError('Migration foreign key failure / 多人成员迁移外键校验失败')
                db.execute('PRAGMA user_version=9')
        finally:
            db.execute('PRAGMA foreign_keys=ON')

    def member_ids(self):
        with self.transaction() as db:
            return [r[0] for r in db.execute('SELECT id FROM members ORDER BY id')]

    def _transfer_workspaces_safe(self,db,source,target):
        try:
            from .pair_core import member_workspace
            from .pair_git import inspect_repo
        except ImportError:
            from pair_core import member_workspace
            from pair_git import inspect_repo
        first=member_workspace(self._member(db,source));second=member_workspace(self._member(db,target))
        if first==second:return True
        try:
            a,b=inspect_repo(first),inspect_repo(second)
            return a['common_dir']==b['common_dir'] and a['head']==b['head'] and not a['dirty'] and not b['dirty']
        except Exception:
            return False

    def _member_ready(self, db, member):
        # 低层及旧轮次保留原策略；邀请加入的新成员必须完成握手与材料确认。
        observation=self._option(db,'availability:'+member,{})
        if observation.get('state')=='unreachable' and time.time()-observation.get('observed_at',0)<120:
            return False
        if not self._option(db,'onboarding:'+member):
            return True
        return self._option(db,'briefing:'+member,False) and self.peer_verified(db,member)

    @staticmethod
    def peer_verified(db, member):
        if member=='A':return bool(db.execute("SELECT 1 FROM messages q JOIN messages r ON r.reply_to=q.id WHERE q.dedupe_key='window-handshake' AND q.process_status='completed' AND r.process_status='completed' AND q.remote_status='completed' AND r.remote_status='completed'").fetchone())
        key='window-handshake' if member=='B' else 'window-handshake:'+member
        return bool(db.execute("""SELECT 1 FROM messages q JOIN messages r ON r.reply_to=q.id
            WHERE q.dedupe_key=? AND r.kind='reply' AND q.process_status='completed'
            AND r.process_status='completed' AND q.remote_status='completed'
            AND r.remote_status='completed'""",(key,)).fetchone())

    def confirm_briefing(self,member,session,cwd,evidence):
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            if not evidence.strip():raise ValueError('Briefing evidence required / 必须记录目标、授权和相关交付的阅读依据')
            self._put(db,'briefing:'+member,True)
            self.event(db,'briefing_confirmed',member=member,evidence=evidence)
            self._schedule(db)
            return {'confirmed':True,'communication_verified':self.peer_verified(db,member)}

    def suggest_reserve(self,member,session,cwd,value,reason):
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 6<=value<=95:
                raise ValueError('Invalid reserve / 保留线必须为 6～95 的有限数值')
            if value<self._reserve(db,member):raise ValueError('Reserve cannot decrease / 模型不能降低已建立的保留线')
            if not reason.strip():raise ValueError('Reserve reason required / 必须提供现场估计依据')
            self._put(db,'checkpoint_reserve:'+member,value)
            record={'value':value,'source':'model_estimate','reason':reason,'updated':time.time()}
            self._put(db,'reserve_evidence:'+member,record)
            self.event(db,'reserve_suggested',member=member,**record)
            return record

    def adjust_reserve(self,member,session,cwd,target,value,reason):
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            self._member(db,target)
            if member!='A':raise ValueError('Original A required / 仅原 A 可登记用户明确要求调整保留线')
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 6<=value<=95 or not reason.strip():
                raise ValueError('Explicit adjustment required / 必须提供6～95保留线及用户明确调整依据')
            record={'value':value,'source':'user_adjustment','reason':reason,'updated':time.time()}
            self._put(db,'checkpoint_reserve:'+target,value)
            self._put(db,'reserve_evidence:'+target,record)
            self.event(db,'reserve_adjusted',member=target,**record)
            return record

    def _work_version(self,task):
        import hashlib
        return hashlib.sha256(json.dumps([task['result'],task['commits_json']],sort_keys=True).encode()).hexdigest()

    def _current_work_version(self,db,task):
        artifact=self._option(db,'review-artifact:'+task['id'])
        source=self._option(db,'review-source:'+task['id'])
        import hashlib
        return hashlib.sha256(json.dumps([self._work_version(task),artifact,
            self._option(db,'work_revision:'+task['id'],0),source,
            self._review_artifact_valid(db,task)],sort_keys=True).encode()).hexdigest()

    def _review_artifact_valid(self,db,task):
        artifact=self._option(db,'review-artifact:'+task['id']) or self._option(db,'review-source:'+task['id'])
        if not artifact:return False
        try:
            if 'head' in artifact:
                try:from .pair_git import review_artifact_matches
                except ImportError:from pair_git import review_artifact_matches
                return review_artifact_matches(artifact['workspace'],artifact['head'],json.loads(task['allowed_paths']))
            return all(not (Path(artifact['workspace'])/p).exists() and
                not (Path(artifact['workspace'])/p).is_symlink() for p in artifact['absent_paths'])
        except (OSError,ValueError,RuntimeError):
            return False

    def _seal_review_source(self,db,task):
        try:
            from .pair_core import member_workspace
            from .pair_git import inspect_repo,check_delivery,GitError
        except ImportError:
            from pair_core import member_workspace
            from pair_git import inspect_repo,check_delivery,GitError
        workspace=member_workspace(self._member(db,task['assignee']))
        paths=json.loads(task['allowed_paths']);commits=json.loads(task['commits_json'] or '[]')
        if not commits:
            # 纯文本交付可以审核；声明范围内已有文件时，必须先固定为提交。
            if any((Path(workspace)/p).exists() or (Path(workspace)/p).is_symlink() for p in paths):
                raise ValueError('Committed artifact required / 含文件的待审成果必须提交后交付，保留原文件和领取令牌')
            artifact={'workspace':workspace,'absent_paths':paths}
            try:repo=inspect_repo(workspace)
            except GitError:repo=None  # 低层纯文本测试/协作允许非 Git 工作目录。
            if repo:
                if repo['dirty']:raise ValueError('Committed artifact required / 待审工作树有未提交修改，不能作为纯文本成果验收')
                artifact['head']=repo['head']
        else:
            repo=inspect_repo(workspace)
            delivery=check_delivery(workspace,task['base_commit'],commits,paths)
            if repo['dirty'] or repo['head']!=commits[-1] or not delivery['passed']:
                raise ValueError('Committed artifact required / 待审文件须为干净工作树中的完整可验收提交')
            artifact={'workspace':workspace,'head':repo['head']}
        self._put(db,'review-source:'+task['id'],artifact)

    def record_contribution(self,member,session,cwd,task_id,identity):
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            task=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not task or self._option(db,'review-task:'+task_id):raise ValueError('Implementation task required / 必须指定实现工作项')
            self._capture_work(db,task,member,identity=identity,allow_unsealed=True)
            self._register_reviews(db)
            self.event(db,'contribution_recorded',task=task_id,member=member)
            return {'recorded':True,'review_invalidated':True}

    def _capture_work(self,db,task,member,identity=None,allow_unsealed=False):
        proof=identity or json.loads(self._member(db,member)['proof'])
        old=db.execute('SELECT * FROM work_reviews WHERE task_id=?',(task['id'],)).fetchone()
        contributors=json.loads(old['contributors']) if old else []
        if member not in contributors:contributors.append(member)
        required=bool(old and old['required']) or proof.get('modelProvider')=='deepseek'
        if required and task['status'] in ('delivered','completed','integrated'):
            try:self._seal_review_source(db,task)
            except (OSError,ValueError,RuntimeError):
                if not allow_unsealed:raise
                # 贡献登记必须先使旧审核失效，不能因尚未提交而丢失 DeepSeek 来源。
                self._put(db,'review-source:'+task['id'],None)
                self._put(db,'review-artifact:'+task['id'],None)
                self.event(db,'work_artifact_unsealed',task=task['id'],member=member)
        self._put(db,'work_revision:'+task['id'],self._option(db,'work_revision:'+task['id'],0)+1)
        db.execute('''INSERT INTO work_reviews VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET
            version=excluded.version,contributors=excluded.contributors,required=excluded.required,
            reviewer=NULL,status=excluded.status,evidence=NULL,identity=NULL,updated=excluded.updated''',
            (task['id'],self._current_work_version(db,task),json.dumps(contributors),int(required),None,
             'pending' if required else 'not_required',None,None,time.time()))
        if db.execute('SELECT 1 FROM deliveries WHERE id=?',('delivery:'+task['id'],)).fetchone():
            self._delivery(db,task)

    def _review_candidates(self,db,row):
        quotas=self._quotas(db)
        candidates=[]
        for member in db.execute('SELECT * FROM members ORDER BY id'):
            name=member['id'];proof=json.loads(member['proof']);quota=quotas[name]
            if (self._member_ready(db,name) and not self._member_suspended(db,name)
                    and not (quota.get('reliability')=='fresh' and quota.get('remaining')==0)
                    and name not in json.loads(row['contributors'])
                    and proof.get('modelProvider')=='openai' and str(proof.get('model','')).startswith('gpt-')):
                candidates.append(name)
        return candidates

    @staticmethod
    def _review_unsent(db,task_id):
        return not db.execute("""SELECT 1 FROM messages m WHERE m.task_id=? AND
            (m.process_status<>'unclaimed' OR m.delivery_status NOT IN ('pending','retry')
             OR m.attempts<>0 OR m.claim_token IS NOT NULL OR m.queue_id IS NOT NULL OR m.turn_id IS NOT NULL
             OR m.remote_status IS NOT NULL OR EXISTS (SELECT 1 FROM attempts a WHERE a.message_id=m.id))""",(task_id,)).fetchone()

    def _close_stale_reviews(self,db):
        for task in db.execute("SELECT * FROM tasks WHERE id LIKE 'gpt-review-%' AND status IN ('pending','active')").fetchall():
            assignment=self._option(db,'review-task:'+task['id'])
            if not assignment:continue
            review=db.execute('SELECT * FROM work_reviews WHERE task_id=?',(assignment['task'],)).fetchone()
            source=db.execute('SELECT * FROM tasks WHERE id=?',(assignment['task'],)).fetchone()
            if not review or not source:continue
            # 工作树暂时不匹配只暂停审核；只有登记了替代成果才永久关闭旧任务。
            stale=assignment['version']!=review['version']
            if not stale or not self._review_unsent(db,task['id']):continue
            now=time.time()
            db.execute("UPDATE tasks SET status='completed',result=?,updated=? WHERE id=?",('过期审核未投递，安全收尾；不代表审核通过',now,task['id']))
            db.execute("UPDATE messages SET process_status='completed',delivery_status='failed',error=?,updated=? WHERE task_id=?",('Stale review closed before delivery / 过期审核在投递前收尾',now,task['id']))
            self.event(db,'stale_review_closed',task=task['id'],accepted=False,automatic=True)

    def _register_reviews(self,db):
        import hashlib
        self._close_stale_reviews(db)
        for row in db.execute("SELECT * FROM work_reviews WHERE required=1 AND status='pending'").fetchall():
            task=db.execute('SELECT * FROM tasks WHERE id=?',(row['task_id'],)).fetchone()
            if task['status'] not in ('delivered','completed','integrated'):continue
            if row['version']!=self._current_work_version(db,task) or not self._review_artifact_valid(db,task):continue
            review_id='gpt-review-'+hashlib.sha256((row['task_id']+row['version']).encode()).hexdigest()[:24]
            existing=db.execute('SELECT * FROM tasks WHERE id=?',(review_id,)).fetchone()
            if (existing and existing['status']=='completed'
                    and existing['result']=='过期审核未投递，安全收尾；不代表审核通过'):
                messages=db.execute('SELECT * FROM messages WHERE task_id=?',(review_id,)).fetchall()
                # 兼容旧版误关闭：仅恢复有明确自动关闭痕迹、且从未尝试投递的消息。
                if messages and all(m['process_status']=='completed' and m['delivery_status']=='failed'
                        and m['error']=='Stale review closed before delivery / 过期审核在投递前收尾'
                        and m['attempts']==0 and all(m[k] is None for k in ('claim_token','queue_id','turn_id','remote_status'))
                        and not db.execute('SELECT 1 FROM attempts WHERE message_id=?',(m['id'],)).fetchone()
                        for m in messages):
                    now=time.time()
                    db.execute("UPDATE tasks SET status='pending',result=NULL,updated=? WHERE id=?",(now,review_id))
                    db.execute("UPDATE messages SET process_status='unclaimed',delivery_status='pending',error=NULL,envelope=NULL,next_attempt=0,updated=? WHERE task_id=?",(now,review_id))
                    self.event(db,'work_review_restored',review_task=review_id,reason='artifact_restored')
                    existing=db.execute('SELECT * FROM tasks WHERE id=?',(review_id,)).fetchone()
            candidates=self._review_candidates(db,row)
            if existing and (existing['status'] not in ('pending','active') or existing['assignee'] in candidates or not self._review_unsent(db,review_id)):continue
            if not candidates:continue
            reviewer=min(candidates,key=lambda m:(sum(r[0] for r in db.execute("SELECT effort FROM tasks WHERE assignee=? AND status NOT IN ('completed','integrated')",(m,))),m))
            if existing:
                try:from .pair_core import dumps
                except ImportError:from pair_core import dumps
                now=time.time()
                # 返回调度门重新检查新成员的工作区互斥，不直接占用繁忙成员。
                db.execute("UPDATE tasks SET assignee=?,status='pending',updated=? WHERE id=?",(reviewer,now,review_id))
                for message in db.execute("SELECT * FROM messages WHERE task_id=? AND kind='task'",(review_id,)).fetchall():
                    sender=self._coordinator(db)
                    fingerprint=hashlib.sha256(dumps([sender,reviewer,message['body'],'task',review_id,message['reply_to'],None]).encode('utf-8')).hexdigest()
                    db.execute('UPDATE messages SET sender=?,recipient=?,fingerprint=?,envelope=NULL,next_attempt=0,updated=? WHERE id=?',
                        (sender,reviewer,fingerprint,now,message['id']))
                self.event(db,'work_review_reassigned',review_task=review_id,previous=existing['assignee'],reviewer=reviewer)
                continue
            body='独立审核成果 '+row['task_id']+'，版本 '+row['version']+'。完整读取原目标、差异/提交和测试证据；核验当前官方 GPT 提供方及模型，使用 review-work 记录 passed/blocked，再完成本审核任务。不得修改业务实现或自审。'
            artifact=self._option(db,'review-artifact:'+row['task_id'])
            if artifact:body+='\n整合后版本，必须检查此工作树和 HEAD：'+json.dumps(artifact)
            now=time.time()
            db.execute('''INSERT INTO tasks(id,title,body,assignee,allowed_paths,status,created,updated,base_commit,report_to,effort,assignment_mode)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',(review_id,'GPT 独立成果审核',body,reviewer,'[]','pending',now,now,task['base_commit'],self._coordinator(db),2,'fixed'))
            db.execute('INSERT INTO dependencies VALUES(?,?)',(review_id,row['task_id']))
            self._put(db,'review-task:'+review_id,{'task':row['task_id'],'version':row['version']})
            self.event(db,'work_review_registered',task=row['task_id'],review_task=review_id,reviewer=reviewer)

    def _review_clear(self,db,task_id):
        replacement=self._option(db,'replacement:'+task_id)
        if replacement:
            task=db.execute('SELECT status FROM tasks WHERE id=?',(replacement,)).fetchone()
            return bool(task and task['status'] in ('completed','integrated') and self._review_clear(db,replacement))
        row=db.execute('SELECT * FROM work_reviews WHERE task_id=?',(task_id,)).fetchone()
        if not row or not row['required']:return True
        task=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
        if row['status']!='passed' or row['version']!=self._current_work_version(db,task):return False
        import hashlib
        review_id='gpt-review-'+hashlib.sha256((task_id+row['version']).encode()).hexdigest()[:24]
        finished=db.execute("SELECT 1 FROM tasks WHERE id=? AND status='completed'",(review_id,)).fetchone()
        return bool(finished)

    def _effective_task(self,db,task_id):
        seen=set()
        while self._option(db,'replacement:'+task_id):
            if task_id in seen:raise ValueError('Repair cycle / 修复关联不能循环')
            seen.add(task_id);task_id=self._option(db,'replacement:'+task_id)
        return dict(db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone())

    def review_work(self,member,session,cwd,task_id,version,decision,evidence,identity):
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            row=db.execute('SELECT * FROM work_reviews WHERE task_id=?',(task_id,)).fetchone()
            task=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not row or row['version']!=version or self._current_work_version(db,task)!=version:
                raise ValueError('Review version changed / 成果版本已变化')
            if not self._review_artifact_valid(db,task):raise ValueError('Review artifact changed / 成果未固定为可核验版本或文件已变化')
            if member in json.loads(row['contributors']):raise ValueError('Independent reviewer required / 实质贡献者不能审核自身工作项')
            assignment=next((t for t in db.execute("SELECT * FROM tasks WHERE assignee=? AND status='active'",(member,))
                if self._option(db,'review-task:'+t['id'],{})=={'task':task_id,'version':version}),None)
            if not assignment or not db.execute("SELECT 1 FROM messages WHERE task_id=? AND recipient=? AND process_status='claimed'",(assignment['id'],member)).fetchone():
                raise ValueError('Claimed review task required / 必须先领取本版本独立审核任务')
            self._require_ack(db,member,'delivery:'+task_id)
            # identity 由当前 CLI 官方 thread/read 验证取得，不能由目录名或模型自述推定。
            if identity.get('modelProvider')!='openai' or not str(identity.get('model','')).startswith('gpt-'):
                raise ValueError('Official GPT reviewer required / 必须核验 OpenAI 官方 GPT 会话')
            if decision not in ('passed','blocked') or not evidence.strip():raise ValueError('Review evidence required / 必须记录审核结论及证据')
            db.execute('UPDATE work_reviews SET reviewer=?,status=?,evidence=?,identity=?,updated=? WHERE task_id=?',
                (member,decision,evidence,json.dumps({k:identity[k] for k in ('modelProvider','model')}),time.time(),task_id))
            self.event(db,'work_reviewed',task=task_id,reviewer=member,version=version,decision=decision)
            self._schedule(db)
            return {'task':task_id,'version':version,'decision':decision}
