"""固定版本送审、依赖放行、稳定问题返修与发布证据；不根据模型退出码放行。"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid

try:
    from .pair_reviewer import fail, safe_evidence
except ImportError:
    from pair_reviewer import fail, safe_evidence


def encoded(value): return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def digest(value): return hashlib.sha256(encoded(value).encode()).hexdigest()


class StageMixin:
    def _upgrade_stages(self,db):
        with db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('''CREATE TABLE review_stages(id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),
                version INTEGER NOT NULL,requirements_version INTEGER NOT NULL,artifact TEXT NOT NULL,
                digest TEXT NOT NULL UNIQUE,contributors TEXT NOT NULL,checks TEXT NOT NULL,payload TEXT NOT NULL,
                status TEXT NOT NULL,reviewer_epoch INTEGER,reviewer_session TEXT,execution_id TEXT,
                turn_id TEXT,verdict TEXT,evidence TEXT,priority INTEGER NOT NULL DEFAULT 0,
                created REAL NOT NULL,updated REAL NOT NULL,UNIQUE(task_id,version))''')
            db.execute('''CREATE TABLE review_issues(id TEXT PRIMARY KEY,root_task TEXT NOT NULL,
                author TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,rounds INTEGER NOT NULL DEFAULT 0,
                stage_id TEXT NOT NULL,repair_task TEXT,resolution TEXT,updated REAL NOT NULL)''')
            db.execute('''CREATE TABLE repair_links(task_id TEXT PRIMARY KEY,issue_id TEXT NOT NULL,
                round INTEGER NOT NULL,source_stage TEXT NOT NULL,UNIQUE(issue_id,round))''')
            db.execute('''CREATE TABLE publications(id TEXT PRIMARY KEY,stage_id TEXT NOT NULL,head TEXT NOT NULL,
                remote TEXT NOT NULL,branch TEXT NOT NULL,status TEXT NOT NULL,evidence TEXT NOT NULL,
                created REAL NOT NULL,updated REAL NOT NULL)''')
            db.execute('''CREATE TABLE pool_backoff(pool TEXT PRIMARY KEY,until_at REAL NOT NULL,
                reason TEXT NOT NULL,needs_fresh INTEGER NOT NULL DEFAULT 1)''')
            db.execute('''CREATE TABLE reserve_proposals(id TEXT PRIMARY KEY,member TEXT NOT NULL,
                previous REAL NOT NULL,proposed REAL NOT NULL,reason TEXT NOT NULL,sample TEXT,created REAL NOT NULL)''')
            db.execute('''CREATE TABLE managed_permissions(id TEXT PRIMARY KEY,epoch INTEGER NOT NULL,
                session TEXT NOT NULL,execution_id TEXT NOT NULL,request TEXT NOT NULL,digest TEXT NOT NULL,
                operation TEXT NOT NULL,authorization_version INTEGER NOT NULL,status TEXT NOT NULL,
                expires REAL NOT NULL,review_message TEXT NOT NULL,attempt INTEGER NOT NULL,
                retry_state TEXT NOT NULL DEFAULT 'none',reason TEXT,created REAL NOT NULL)''')
            db.execute('''CREATE TABLE member_context_reads(member TEXT NOT NULL,binding_version INTEGER NOT NULL,
                digest TEXT NOT NULL,start INTEGER NOT NULL,end INTEGER NOT NULL,
                PRIMARY KEY(member,binding_version,digest,start,end))''')
            db.execute('PRAGMA user_version=13')

    def submit_stage(self,member,session,cwd,task_id,artifact,contributors,checks,payload,*,priority=0):
        if not isinstance(artifact,dict) or artifact.get('kind') not in ('commit','snapshot'): fail('Fixed artifact required / 必须固定提交或快照')
        if artifact['kind']=='commit' and (not isinstance(artifact.get('head'),str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',artifact['head'])): fail('Full SHA required / 要求完整候选SHA')
        if artifact['kind']=='snapshot' and not artifact.get('manifest'): fail('Snapshot manifest required / 非提交成果必须固定快照清单')
        if not contributors or not all(isinstance(c,dict) and c.get('member') and c.get('model') and c.get('provider') in ('openai','deepseek','wokey','apinebula') for c in contributors): fail('Provenance required / 保留每位实际贡献者模型及提供方')
        if not isinstance(checks,list) or not isinstance(payload,dict): fail('Review material required / 检查、环境、目标、允许范围与验收材料必须结构化')
        for field in ('objective','acceptance','environment','lockfiles','baseline'):
            if field not in payload: fail('Missing review material / 缺少送审字段: '+field)
        safe_evidence(encoded({'artifact':artifact,'contributors':contributors,'checks':checks,'payload':payload}))
        with self.transaction() as db:
            actor=self._binding(db,member,session,cwd)
            task=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not task or task['assignee']!=member or task['task_kind'] in ('planning','coordination'): fail('Owned business stage required / 只对本人业务阶段送审，不递归审核回执')
            if task['status'] not in ('delivered','completed','review_wait','needs_fix','needs_validation'): fail('Stage not delivered / 先完成固定版本阶段交付')
            identity=json.loads(actor['proof'])
            if identity.get('modelProvider') and not any(c.get('member')==member and c.get('provider')==identity.get('modelProvider') and c.get('model')==identity.get('model') for c in contributors):fail('Actor provenance missing / 贡献来源必须包含本人实际模型和提供方')
            contributors=[dict(c) for c in contributors]
            actual={'member':member,'provider':identity.get('modelProvider'),'model':identity.get('model'),
                    'session_uuid':actor['session_uuid'],'binding_version':actor['binding_version'],'role':'stage-owner'}
            if actual['provider'] and actual['model'] and actual not in contributors:contributors.append(actual)
            for contributor in self._continuation_contributors(db,task_id):
                if contributor not in contributors:contributors.append(contributor)
            for previous_stage in db.execute('SELECT contributors FROM review_stages WHERE task_id=?',(task_id,)):
                for contributor in json.loads(previous_stage[0]):
                    if contributor not in contributors:contributors.append(contributor)
            repair=self._continuation_repair_link(db,task_id)
            if repair:
                source=db.execute('SELECT contributors FROM review_stages WHERE id=?',(repair['source_stage'],)).fetchone()
                if source:
                    for contributor in json.loads(source[0]):
                        if contributor not in contributors:contributors.append(contributor)
            material={'task':task_id,'requirements_version':task['requirements_version'],'allowed_paths':json.loads(task['allowed_paths']),
                'artifact':artifact,'contributors':contributors,'checks':checks,'payload':payload}
            payload=dict(payload,coordinator_acceptance=json.loads(task['acceptance']),coordinator_required_checks=json.loads(task['required_checks']))
            material['payload']=payload
            fingerprint=digest(material)
            old=db.execute('SELECT * FROM review_stages WHERE digest=?',(fingerprint,)).fetchone()
            if old:return dict(old)
            if db.execute("SELECT 1 FROM review_stages WHERE task_id=? AND status='running'",(task_id,)).fetchone():
                fail('Review running / 原版本正在审核，不能替换或使其失效')
            count=sum(db.execute('SELECT task_id FROM review_stages WHERE id=?',(key,)).fetchone()[0]!=task_id
                      for key in self._stage_backlog_ids(db,member))
            previous=db.execute('SELECT max(version) FROM review_stages WHERE task_id=?',(task_id,)).fetchone()[0] or 0
            try:from .pair_diagnostics import capacity
            except ImportError:from pair_diagnostics import capacity
            if count>=capacity(db,'stage_capacity'): fail('Review backlog full / 成员待审阶段已到当前上限；已保存交付不会丢弃')
            db.execute("UPDATE review_stages SET status='stale',updated=? WHERE task_id=? AND status<>'stale'",(time.time(),task_id))
            key=uuid.uuid4().hex;now=time.time()
            db.execute('''INSERT INTO review_stages(id,task_id,version,requirements_version,artifact,digest,contributors,checks,payload,status,priority,created,updated)
                VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?)''',
                (key,task_id,previous+1,task['requirements_version'],encoded(artifact),fingerprint,encoded(contributors),encoded(checks),encoded(payload),priority,now,now))
            db.execute("UPDATE tasks SET status='review_wait',review_required=1 WHERE id=?",(task_id,))
            if artifact['kind']=='commit':
                commits=artifact.get('commits') or [artifact['head']]
                db.execute('UPDATE tasks SET commits_json=? WHERE id=?',(encoded(commits),task_id))
            else:commits=[]
            body=encoded(material)
            db.execute('INSERT INTO deliveries VALUES(?,?,?,?,?,?)',('stage:'+key,task_id,body,encoded(commits),digest([body,encoded(commits)]),now))
            db.execute('DELETE FROM resource_locks WHERE task_id=?',(task_id,))
            self.event(db,'stage_submitted',stage=key,task=task_id,digest=fingerprint,actor=actor['session_uuid'])
            self._put(db,'review_queued_at:'+key,self._option(db,'review_selection_count',0))
            return dict(db.execute('SELECT * FROM review_stages WHERE id=?',(key,)).fetchone())

    def revise_check_contract(self,member,session,cwd,task_id,stage_id,expected_digest,reason,required_checks):
        """只修订检查契约；保留旧阶段，原作者仍须公开重新送审。"""
        try:from .pair_producer import strict_display
        except ImportError:from pair_producer import strict_display
        if not isinstance(reason,str) or not reason.strip() or len(reason)>4000:
            fail('Revision reason required / 必须提供有界修订理由')
        if not isinstance(required_checks,list) or not 1<=len(required_checks)<=64:
            fail('Complete checks required / 新检查契约必须是非空有界列表')
        names=set()
        for check in required_checks:
            if not isinstance(check,dict) or set(check)!={'name','command','argv','required'}:
                fail('Exact check fields required / 检查必须含name/command/argv/required')
            name=check['name'];argv=check['argv']
            if (not isinstance(name,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',name)
                    or name in names or check['required'] is not True
                    or not isinstance(argv,list) or not argv or not isinstance(argv[0],str) or not argv[0].strip()
                    or len(argv)>256 or any(not isinstance(v,str) or len(v)>16000 for v in argv)
                    or not isinstance(check['command'],str) or not check['command']
                    or strict_display(argv)!=check['command']):
                fail('Invalid exact check contract / 名称唯一、required=true且command须等于准确argv的严格展示')
            names.add(name)
        request={'task':task_id,'stage':stage_id,'digest':expected_digest,'reason':reason,'checks':required_checks}
        safe_evidence(encoded(request))
        with self.transaction() as db:
            actor=self._a_actor(db,member,session,cwd)
            task=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            stage=db.execute('SELECT * FROM review_stages WHERE task_id=? ORDER BY version DESC LIMIT 1',(task_id,)).fetchone()
            if (not task or not stage or stage['id']!=stage_id or stage['digest']!=expected_digest
                    or stage['status'] not in ('needs_validation','needs_fix')
                    or task['status'] not in ('review_wait','needs_validation','needs_fix','delivered','completed')
                    or task['task_kind'] in ('coordination','planning')):
                fail('Exact terminal stage required / 仅当前准确未通过终态业务阶段可修订检查契约')
            # 已接受回合与审批结果未知均不能被契约修订跳过。
            related={task_id}|{r[0] for r in db.execute('SELECT task_id FROM repair_links WHERE source_stage=?',(stage_id,))}
            reviewer=self._reviewer_row(db)
            if (reviewer and (reviewer['state'] not in ('idle','retired') or reviewer['owner'])
                    or db.execute("SELECT 1 FROM review_stages WHERE status='running'").fetchone()
                    or self._approval_unresolved(db)
                    or db.execute("SELECT 1 FROM approval_requests WHERE status IN ('pending','allowed','granted','unknown')").fetchone()
                    or db.execute("SELECT 1 FROM execution_requests WHERE status IN ('pending','running','unknown')").fetchone()
                    or any(m['task_id'] in related for m in self._running(db))
                    or any(db.execute("SELECT 1 FROM messages WHERE task_id=? AND kind='task' AND process_status<>'completed'",(related_id,)).fetchone() for related_id in related)
                    or db.execute("SELECT 1 FROM repair_links r JOIN tasks t ON t.id=r.task_id WHERE r.source_stage=? AND t.status NOT IN ('completed','delivered','review_wait','needs_fix','needs_validation','cancelled','integrated')",(stage_id,)).fetchone()):
                fail('Execution unresolved / 原业务、审核或审批尚未核实终态')
            key='check_contract_revision:'+stage_id
            previous=self._option(db,key)
            if previous:
                if previous['request']!=request or previous['new_requirements_version']!=task['requirements_version']:
                    fail('Revision conflict / 此旧阶段已有不同修订，须重新固定阶段后再修订')
                return dict(previous['result'],duplicate=True)
            if stage['requirements_version']!=task['requirements_version']:
                fail('Stale contract / 旧阶段契约版本已失效')
            old=json.loads(task['required_checks'])
            if any(c.get('required',True) and c.get('name') not in names for c in old):
                fail('Existing obligation missing / 不得删除旧必要检查名称')
            if old==required_checks:fail('No contract change / 检查契约未变化')
            new_version=task['requirements_version']+1
            result={'task':task_id,'source_stage':stage_id,'source_digest':expected_digest,
                    'requirements_version':new_version,'resubmit_required':True,'duplicate':False}
            audit={'request':request,'old_checks':old,'old_requirements_version':task['requirements_version'],
                   'new_requirements_version':new_version,'actor_session':actor['session_uuid'],
                   'binding_version':actor['binding_version'],'result':result}
            self._put(db,key,audit)
            db.execute('UPDATE tasks SET required_checks=?,requirements_version=?,review_required=1,updated=? WHERE id=?',
                       (encoded(required_checks),new_version,time.time(),task_id))
            self.event(db,'check_contract_revised',**audit)
            return result

    def stage_delivery(self,db,task_id):
        if db.execute('PRAGMA user_version').fetchone()[0]>=14:
            task_id=self._effective_obligation_task(db,task_id)
        row=db.execute("SELECT id FROM review_stages WHERE task_id=? AND status='passed' ORDER BY version DESC LIMIT 1",(task_id,)).fetchone()
        if not row:
            task=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            # 旧规划只有真实完成结果时才按原存储协议补存，不执行规划或发通知。
            if task and task['task_kind']=='planning':self._delivery(db,dict(task))
        return 'stage:'+row[0] if row else 'delivery:'+task_id

    def _stage_passed(self,db,task_id,*,allow_supersession=True):
        task=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
        if not task:return False
        modern=db.execute('PRAGMA user_version').fetchone()[0]>=14
        if modern and allow_supersession and self._task_has_effective_supersession(db,task_id):
            return self._stage_passed(db,self._effective_obligation_task(db,task_id),allow_supersession=False)
        if task['task_kind'] in ('planning','coordination') and (
                json.loads(task['commits_json'] or '[]') or json.loads(task['required_checks'] or '[]') or
                db.execute('SELECT 1 FROM review_stages WHERE task_id=?',(task_id,)).fetchone()):return False
        stage=db.execute('SELECT * FROM review_stages WHERE task_id=? ORDER BY version DESC LIMIT 1',(task_id,)).fetchone()
        auxiliary_required=db.execute('''SELECT 1 FROM auxiliary_candidates c
            JOIN review_stages s ON s.id=c.stage_id WHERE s.task_id=? LIMIT 1''',(task_id,)).fetchone() if modern else None
        if not task['review_required'] and not auxiliary_required:return True
        if not stage or stage['status']!='passed' or stage['requirements_version']!=task['requirements_version']:return False
        if not auxiliary_required:return True
        # 新候选不能借旧辅助结论放行；暂停辅助池也不撤销已登记的必需审核义务。
        candidate=db.execute('SELECT * FROM auxiliary_candidates WHERE stage_id=?',(stage['id'],)).fetchone()
        if (not candidate or candidate['status']!='passed' or candidate['candidate_digest']!=stage['digest']
                or candidate['requirements_version']!=stage['requirements_version'] or not candidate['result']):return False
        payload=json.loads(stage['payload'])
        contract=digest({'requirements_version':stage['requirements_version'],
            'acceptance':payload.get('coordinator_acceptance',[]),'checks':payload.get('coordinator_required_checks',[])})
        if candidate['contract_digest']!=contract:return False
        result=json.loads(candidate['result'])
        if (result.get('verdict')!='passed' or result.get('candidate_id')!=candidate['id']
                or result.get('stage_id')!=stage['id'] or result.get('reasons') or not result.get('report_digests')):return False
        if db.execute('''SELECT 1 FROM auxiliary_tasks t LEFT JOIN auxiliary_executions e ON e.id=t.last_execution
            LEFT JOIN auxiliary_reports r ON r.execution_id=t.last_execution WHERE t.candidate_id=?
            AND (t.status<>'completed' OR e.state<>'completed' OR r.execution_id IS NULL) LIMIT 1''',(candidate['id'],)).fetchone():return False
        current_reports=[r[0] for r in db.execute('''SELECT r.report_digest FROM auxiliary_tasks t
            JOIN auxiliary_reports r ON r.execution_id=t.last_execution WHERE t.candidate_id=?''',(candidate['id'],))]
        return sorted(result['report_digests'])==sorted(current_reports)

    def integration_candidate(self,member,session,cwd,sources,artifact,checks,payload):
        """整合后的HEAD独立送审，不沿用各分支通过结论。"""
        if not sources:fail('Integration sources required / 必须列出已审来源阶段')
        if not payload.get('required_checks'):fail('Integration validation required / A须定义整合后必要回归检查')
        with self.transaction() as db:
            self._a_actor(db,member,session,cwd)
            contributors=[];paths=[]
            for source in sources:
                stage=db.execute("SELECT * FROM review_stages WHERE id=? AND status='passed'",(source,)).fetchone()
                if not stage or not self._stage_passed(db,stage['task_id']):fail('Source not approved / 来源版本未有效通过')
                contributors.extend(json.loads(stage['contributors']))
                task=db.execute('SELECT * FROM tasks WHERE id=?',(stage['task_id'],)).fetchone()
                paths.extend(json.loads(task['allowed_paths']))
                if not db.execute("SELECT 1 FROM integrations WHERE task_id=? AND status='completed'",(task['id'],)).fetchone():fail('Source not integrated / 来源提交未在登记整合中完成')
            own=json.loads(self._member(db,member)['proof'])
            contributors.append({'member':member,'provider':own.get('modelProvider'),'model':own.get('model'),'role':'integration/conflict-resolution'})
            key='integration-'+digest({'sources':sorted(sources),'artifact':artifact})[:24]
            exists=db.execute('SELECT id FROM tasks WHERE id=?',(key,)).fetchone()
            if not exists:
                now=time.time()
                db.execute('''INSERT INTO tasks(id,title,body,assignee,allowed_paths,status,commits_json,created,updated,base_commit,report_to,task_kind,requirements_version,review_required)
                    VALUES(?,?,?,?,?,'completed',?,?,?,?,?,'integration',?,1)''',
                    (key,'整合后独立审核','来源阶段：'+encoded(sources),member,encoded(sorted(set(paths))),encoded([artifact['head']]),now,now,payload['baseline'],member,self._option(db,'requirements_version',1)))
                self.event(db,'integrated_stage_registered',task=key,sources=sources,head=artifact['head'])
            db.execute('UPDATE tasks SET acceptance=?,required_checks=? WHERE id=?',(encoded(payload['acceptance']),encoded(payload['required_checks']),key))
        return self.submit_stage(member,session,cwd,key,artifact,contributors,checks,payload,priority=90)

    def require_reviewed(self,task_id):
        with self.transaction() as db:
            if not self._stage_passed(db,task_id): fail('Independent review required / 该具体版本尚未独立审核通过')
            return dict(db.execute('SELECT * FROM review_stages WHERE task_id=? ORDER BY version DESC LIMIT 1',(task_id,)).fetchone())

    def next_review(self,epoch,session,proof):
        with self.transaction() as db:
            self._check_reviewer(db,epoch,session,proof)
            if self._cli_compatibility_gate(db,'A','new_delivery'):return None
            if self._role_handoff_pending(db,'reviewer'):return None
            if db.execute('SELECT status FROM collaborations').fetchone()[0]!='running':return None
            if self._member_suspended(db,'A'):return None
            if db.execute("SELECT 1 FROM review_stages WHERE status='running'").fetchone():return None
            # 最多三次优先审核后，最早等待的普通阶段获得一次机会。
            count=self._option(db,'review_selection_count',0)
            pending=[stage for stage in db.execute("SELECT id,task_id FROM review_stages WHERE status='pending' ORDER BY created,id")
                     if not self._recovery_gate(db,member='A',task_id=stage['task_id'],action='review')]
            eligible={stage['id'] for stage in pending}
            for stage in pending:
                if self._option(db,'review_queued_at:'+stage['id']) is None:self._put(db,'review_queued_at:'+stage['id'],count)
            aged=[stage['id'] for stage in pending if count-self._option(db,'review_queued_at:'+stage['id'],count)>=3]
            if aged:return dict(db.execute('SELECT * FROM review_stages WHERE id=?',(aged[0],)).fetchone())
            row=next((stage for stage in db.execute('''SELECT s.* FROM review_stages s JOIN tasks t ON t.id=s.task_id
                WHERE s.status='pending' ORDER BY (t.task_kind='repair') DESC,
                EXISTS(SELECT 1 FROM dependencies d WHERE d.depends_on=t.id) DESC,s.priority DESC,s.created,s.id''')
                if stage['id'] in eligible),None)
            return dict(row) if row else None

    def begin_review(self,stage_id,epoch,session,proof,owner,execution_id):
        with self.transaction() as db:
            if self._cli_compatibility_gate(db,'A','new_delivery'):fail('CLI compatibility unverified / 审核启动前核验当前CLI')
            reviewer=self._check_reviewer(db,epoch,session,proof)
            if reviewer['owner']!=owner or reviewer['execution_id']!=execution_id or reviewer['state']!='running': fail('Reviewer lease required / 只能由唯一当前审核执行持有者领取')
            row=db.execute('SELECT * FROM review_stages WHERE id=?',(stage_id,)).fetchone()
            if not row or row['status']!='pending': fail('Review unavailable / 阶段已被领取或版本失效')
            self._require_recovery_open(db,member='A',task_id=row['task_id'],action='review')
            db.execute("UPDATE review_stages SET status='running',reviewer_epoch=?,reviewer_session=?,execution_id=?,updated=? WHERE id=?",(epoch,session,execution_id,time.time(),stage_id))
            self._put(db,'review_selection_count',self._option(db,'review_selection_count',0)+1)
            self.event(db,'review_started',stage=stage_id,epoch=epoch,execution_id=execution_id)
            return dict(row)

    def record_review_turn(self,stage_id,epoch,execution_id,turn_id):
        with self.transaction() as db:
            changed=db.execute("UPDATE review_stages SET turn_id=? WHERE id=? AND reviewer_epoch=? AND execution_id=? AND status='running'",(turn_id,stage_id,epoch,execution_id)).rowcount
            if not changed: fail('Stale review turn / 旧审核回合不能登记新阶段')

    def finish_review(self,stage_id,epoch,session,proof,owner,execution_id,report,*,files_unchanged,receipt_snapshot=None):
        if not isinstance(report,dict) or report.get('verdict') not in ('passed','needs_fix','needs_validation'): fail('Structured verdict required / 必须有结构化审核结论')
        if not isinstance(report.get('findings'),list) or not isinstance(report.get('checks'),list): fail('Review evidence required / 必须提供问题列表及实际检查证据')
        safe_evidence(encoded(report))
        with self.transaction() as db:
            if receipt_snapshot is not None:
                try:from .pair_review_runtime import validate_final_snapshot
                except ImportError:from pair_review_runtime import validate_final_snapshot
                validate_final_snapshot(self,db,receipt_snapshot)
            reviewer=self._check_reviewer(db,epoch,session,proof)
            provider=self._option(db,'reviewer_provider','openai')
            report=dict(report, reviewer_provider=provider, review_service='third_party' if provider=='apinebula' else 'openai_official')
            row=db.execute('SELECT * FROM review_stages WHERE id=?',(stage_id,)).fetchone()
            if (not row or row['status']!='running' or row['reviewer_epoch']!=epoch or row['reviewer_session']!=session
                    or row['execution_id']!=execution_id or reviewer['owner']!=owner or reviewer['execution_id']!=execution_id): fail('Stale review / 审核者、代次、执行或版本已失效')
            task=db.execute('SELECT * FROM tasks WHERE id=?',(row['task_id'],)).fetchone()
            if report.get('candidate_digest')!=row['digest'] or task['requirements_version']!=row['requirements_version']: fail('Candidate changed / 结论必须绑定当前准确交付摘要及需求版本')
            verdict=report['verdict']
            reasons=list(report.get('validation_reasons',[]))
            if verdict=='passed' and self._cli_compatibility_gate(db,'A','review_settlement'):
                verdict='needs_validation';reasons.append('cli_compatibility_unverified')
            try:from .pair_producer import final_context
            except ImportError:from pair_producer import final_context
            if final_context(self,db,reviewer,row) is None:
                verdict='needs_validation';reasons.append('final_producer_evidence_unverified')
                db.execute("UPDATE managed_permissions SET status='unknown',reason=? WHERE epoch=? AND execution_id=? AND session=? AND status='granted'",
                    ('final producer evidence unverified',epoch,execution_id,session))
            if report.get('verdict')=='needs_validation':reasons.append('controller_validation_incomplete')
            if self._approval_unresolved(db,grants_only=True):
                verdict='needs_validation';reasons.append('permission_execution_unresolved')
            proposal=report.get('reserve_proposal')
            if proposal is not None:
                value=proposal.get('value') if isinstance(proposal,dict) else None
                reason=proposal.get('reason') if isinstance(proposal,dict) else None
                if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not self._reserve(db,'A')<=value<=95 or value<6 or not isinstance(reason,str) or not reason.strip():
                    self.event(db,'reviewer_reserve_proposal_rejected',stage=stage_id,reason='invalid_or_decreasing')
                else:
                    old=self._reserve(db,'A');self._put(db,'checkpoint_reserve:A',value)
                    db.execute('INSERT INTO reserve_proposals VALUES(?,?,?,?,?,?,?)',(uuid.uuid4().hex,'A',old,value,'隐藏审核共享池建议：'+reason,None,time.time()))
                    self.event(db,'reviewer_reserve_increased',stage=stage_id,previous=old,reserve=value,reason=reason)
            running=db.execute('SELECT status FROM collaborations').fetchone()[0]=='running' and not self._option(db,'goal_completed',False)
            if not running:verdict='needs_validation';reasons.append('collaboration_not_running')
            if not files_unchanged:verdict='needs_validation';reasons.append('candidate_files_changed')
            if verdict=='passed':
                expected=json.loads(row['payload']).get('coordinator_required_checks',[])
                if not expected:fail('Coordinator validation missing / A尚未定义必要检查，不能放行')
                supplied={c.get('name'):c for c in report['checks']}
                if any(c['name'] not in supplied or supplied[c['name']].get('status')!='passed' for c in expected):fail('Required check missing / 必要验收检查未全部通过')
                if any(f.get('blocking') and f.get('status') not in ('resolved','closed') for f in report['findings']): fail('Blocking findings remain / 仍有阻塞问题不能通过')
                if not report['checks'] or any(c.get('required',True) and (c.get('status')!='passed' or not c.get('evidence')) for c in report['checks']): fail('Necessary checks missing / 必要验证未通过不能放行')
            for item in report['findings']:
                if not isinstance(item,dict) or not item.get('id') or not item.get('body'): fail('Stable issue required / 问题须有稳定标识及可复现说明')
                if running and item.get('blocking') and item.get('status') not in ('resolved','closed'):
                    self._register_issue(db,row,task,item)
            if verdict=='passed':
                link=self._continuation_repair_link(db,task['id'])
                if link:
                    db.execute("UPDATE review_issues SET status='resolved',resolution=?,updated=? WHERE id=?",(encoded(report),time.time(),link['issue_id']))
                    issue=db.execute('SELECT root_task FROM review_issues WHERE id=?',(link['issue_id'],)).fetchone()
                    if issue:self._send(db,task['assignee'],'A','返修阶段已审，原阶段必须包含修复后重新固定版本送审。',kind='note',dedupe_key='repair-reviewed:'+stage_id)
            db.execute('UPDATE review_stages SET status=?,verdict=?,evidence=?,updated=? WHERE id=?',
                (verdict,verdict,encoded(report),time.time(),stage_id))
            db.execute('UPDATE tasks SET status=?,updated=? WHERE id=?',('delivered' if verdict=='passed' and json.loads(task['commits_json'] or '[]') else ('completed' if verdict=='passed' else verdict),time.time(),task['id']))
            self.event(db,'review_recorded',stage=stage_id,verdict=verdict,digest=row['digest'],epoch=epoch,files_unchanged=files_unchanged)
            self.event(db,'review_effective_verdict',stage=stage_id,digest=row['digest'],epoch=epoch,
                session=session,execution=execution_id,turn=row['turn_id'],normalized_verdict=report['verdict'],
                effective_verdict=verdict,reasons=reasons)
            # 同一固定阶段可复审；不同执行的结论通知不能共用去重键。
            self._send(db,task['assignee'],'A','独立审核结果：'+verdict+'；stage='+stage_id+'，digest='+row['digest']+'。只读取及收尾，后续工作单独登记。',kind='note',dedupe_key='review-result:'+stage_id+':'+execution_id)
            return {'stage':stage_id,'verdict':verdict,'digest':row['digest']}

    def _register_issue(self,db,stage,task,item):
        link=self._continuation_repair_link(db,task['id'])
        root=(db.execute('SELECT root_task FROM review_issues WHERE id=?',(link['issue_id'],)).fetchone()[0] if link else self._continuation_root(db,task['id']))
        anchor=item.get('anchor')
        reliable=(isinstance(anchor,dict) and isinstance(anchor.get('path'),str)
                  and isinstance(anchor.get('reproduction'),str) and bool(anchor['reproduction'].strip()))
        if reliable:
            path=anchor['path'].replace('\\','/')
            reliable=bool(path and not path.startswith('/') and ':' not in path and all(p not in ('','.','..','.git') for p in path.split('/')))
            anchor={'path':path,'reproduction':anchor['reproduction'].strip()}
        stable='issue-'+digest(anchor) if reliable else 'unanchored-'+digest({'root':root,'item':item})
        if link and stable!=link['issue_id']:
            # 返修报告改变复现锚点不能开启新自动轮次，交统筹核对。
            stable=link['issue_id'];reliable=False
        old=db.execute('SELECT * FROM review_issues WHERE id=?',(stable,)).fetchone()
        if reliable and not old:
            related=[r for r in db.execute('SELECT * FROM review_issues')
                     if r['root_task']==root or (
                         isinstance(old_anchor:=json.loads(r['body']).get('anchor'),dict)
                         and old_anchor.get('path')==anchor['path'])]
            if related:
                # 复现标签变化不能证明是新问题，先协调，避免用新ID重开自动轮次。
                reliable=False
                if len(related)==1:stable=related[0]['id'];old=related[0]
        if old:
            db.execute("UPDATE review_issues SET status='open',stage_id=?,updated=? WHERE id=?",(stage['id'],time.time(),stable))
        else:
            db.execute("INSERT INTO review_issues(id,root_task,author,body,status,stage_id,updated) VALUES(?,?,?,?,'open',?,?)",(stable,root,task['assignee'],encoded(item),stage['id'],time.time()))
        sources=self._option(db,'issue_sources:'+stable,[])
        source={'stage':stage['id'],'task':task['id'],'reported_id':item['id'],'author':task['assignee'],'contributors':json.loads(stage['contributors']),'finding':item}
        if source not in sources:sources.append(source)
        self._put(db,'issue_sources:'+stable,sources)
        paths=item.get('paths',[anchor['path']] if reliable else [])
        allowed=json.loads(task['allowed_paths'])
        origin=db.execute('SELECT allowed_paths FROM tasks WHERE id=?',(old['root_task'] if old else root,)).fetchone()
        original_allowed=json.loads(origin[0])
        in_scope=(reliable and isinstance(paths,list) and bool(paths) and anchor['path'] in paths and all(
            isinstance(p,str) and ':' not in p and not p.startswith('/') and '\\' not in p
            and all(part not in ('','.','..','.git') for part in p.split('/'))
            and all(any(p==a or a.endswith('/') and p.startswith(a) for a in scope) for scope in (allowed,original_allowed)) for p in paths))
        if not in_scope:
            db.execute("UPDATE review_issues SET status='needs_coordination' WHERE id=?",(stable,))
            self._send(db,'A','A','阻塞问题缺少可靠复现锚点或超出允许范围，须A决定，不自动扩大返修：'+stable,kind='blocked',dedupe_key='issue-coordinate:'+stable)
            return
        self._repair_issue(db,stable)

    def _repair_issue(self,db,issue_id):
        issue=db.execute('SELECT * FROM review_issues WHERE id=?',(issue_id,)).fetchone()
        task=db.execute('SELECT * FROM tasks WHERE id=?',(issue['root_task'],)).fetchone()
        responsible=self._option(db,'issue_responsible:'+issue_id,issue['author'])
        handoff_key=self._option(db,'role_handoff_current:'+responsible)
        handoff=self._role_handoff(db,handoff_key) if handoff_key else None
        awaiting_takeover=bool(handoff and handoff['state']=='accepted' and
            task['id'] in {t['id'] for t in handoff['snapshot']['tasks']})
        if self._member(db,responsible)['lifecycle']!='active' or self._role_handoff_pending(db,responsible) or awaiting_takeover:
            self._send(db,'A','A','原负责人让位或退出；A须指定继任者，既有作者来源及返修轮次保持。issue='+issue_id,
                       kind='blocked',dedupe_key='issue-owner-handoff:'+issue_id)
            return
        if issue['repair_task']:
            repair_task=issue['repair_task']
            while successor:=self._continuation_successor(db,repair_task):repair_task=successor
            existing=db.execute('SELECT status FROM tasks WHERE id=?',(repair_task,)).fetchone()
            if existing and existing[0] not in ('completed','integrated','needs_fix','needs_validation','handed_back'):return
        if issue['rounds']>=2:
            db.execute("UPDATE review_issues SET status='escalated' WHERE id=?",(issue_id,))
            self._send(db,issue['author'],'A','同一问题自动返修已两轮，须补需求解释、复现或方案，持续争议交用户：'+issue_id,kind='blocked',dedupe_key='repair-escalated:'+issue_id)
            return
        number=issue['rounds']+1;key='repair-'+hashlib.sha256(issue_id.encode()).hexdigest()[:20]+'-'+str(number)
        now=time.time()
        db.execute('''INSERT INTO tasks(id,title,body,assignee,allowed_paths,status,created,updated,base_commit,report_to,effort,assignment_mode,task_kind,requirements_version,priority,review_required)
            VALUES(?,?,?,?,?,'pending',?,?,?,?,?,'fixed','repair',?,100,1)''',
            (key,'阻塞返修 '+issue_id,issue['body']+'\n原问题='+issue_id+'\n待修固定阶段='+issue['stage_id']+'\n先完整读取固定交付及证据，不能视为已验收基线。',responsible,task['allowed_paths'],now,now,task['base_commit'],'A',task['effort'],task['requirements_version']))
        db.execute('INSERT INTO repair_links VALUES(?,?,?,?)',(key,issue_id,number,issue['stage_id']))
        db.execute('UPDATE tasks SET acceptance=?,required_checks=? WHERE id=?',(task['acceptance'],task['required_checks'],key))
        db.execute('UPDATE review_issues SET rounds=?,repair_task=?,updated=? WHERE id=?',(number,key,now,issue_id))
        self._send(db,'A',responsible,'阻塞返修已登记；先结束当前工具并保存现场，安全边界切换。task='+key,kind='blocked',dedupe_key='repair-notice:'+key)

    def resolve_issue(self,member,session,cwd,issue_id,evidence,*,user_decision=False):
        safe_evidence(evidence)
        with self.transaction() as db:
            self._a_actor(db,member,session,cwd)
            issue=db.execute('SELECT * FROM review_issues WHERE id=?',(issue_id,)).fetchone()
            if not issue:fail('Unknown issue / 问题不存在')
            # A 的解释只能要求复核，不直接覆盖审核结论，也不重置轮次。
            db.execute('UPDATE review_issues SET status=?,resolution=?,updated=? WHERE id=?',('user_decision' if user_decision else 'recheck',evidence,time.time(),issue_id))
            db.execute("UPDATE review_stages SET status='pending',updated=? WHERE id=? AND status IN ('needs_fix','needs_validation')",(time.time(),issue['stage_id']))
            self.event(db,'issue_recheck_requested',issue=issue_id,user_decision=user_decision,evidence=evidence)
            return {'requested_recheck':True,'rounds_unchanged':True,'passed':False}

    def release_task_boundary(self,member,session,cwd,task_id,evidence):
        safe_evidence(evidence)
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            row=db.execute('SELECT * FROM tasks WHERE id=?',(task_id,)).fetchone()
            if not row or row['status'] not in ('active','requirements_wait','blocked'):fail('Active owned task required / 只能在未完成任务安全边界切换')
            if row['assignee']!=member:self._a_actor(db,member,session,cwd)
            if db.execute("SELECT 1 FROM approval_requests WHERE task_id=? AND status IN ('pending','allowed','granted','unknown')",(task_id,)).fetchone():fail('Task approval unresolved / 本任务审批或执行未核实')
            if db.execute("SELECT 1 FROM execution_requests r JOIN messages m ON m.id=r.message_id WHERE m.task_id=? AND r.status IN ('running','unknown','needs_authorization')",(task_id,)).fetchone():fail('Task execution unresolved / 本任务协助执行未核实')
            if db.execute("SELECT 1 FROM review_stages WHERE task_id=? AND status='running'",(task_id,)).fetchone():fail('Review still running / 本任务仍在审核')
            # _idle 检查原远程回合，安全结束证明另行保留，不抢占尚在执行的工具。
            if any(m['task_id']==task_id and m['kind'] in ('task','execution_request','execution_result') for m in self._running(db)):fail('Turn still running / 先结束工具和原回合再切换')
            db.execute("UPDATE tasks SET status='handed_back',result=?,updated=? WHERE id=?",(evidence,time.time(),task_id))
            db.execute('DELETE FROM resource_locks WHERE task_id=?',(task_id,))
            self._record_boundary_continuation(db,task_id,evidence)
            self.event(db,'task_boundary_saved',task=task_id,evidence=evidence)
            return {'saved':True,'task':task_id}

    def propose_reserve(self,member,session,cwd,value,reason,*,sample=None):
        safe_evidence(reason)
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 6<=value<=95: fail('Invalid reserve / 保留线须是6到95的有限数值')
        with self.transaction() as db:
            self._binding(db,member,session,cwd)
            old=self._reserve(db,member)
            if value<old:fail('Reserve cannot decrease / 同轮保留线只能上调')
            if sample is not None:
                if not isinstance(sample,dict) or not sample.get('comparable') or sample.get('mixed_consumption') or not sample.get('concurrent_consumption_excluded') or not sample.get('evidence'):fail('Unreliable calibration / 混合消耗、并发未排除或不可比样本不能归因交接')
                before=sample.get('before',{});after=sample.get('after',{})
                if (not before.get('pool') or before.get('pool')!=after.get('pool') or before.get('limit_id')!=after.get('limit_id')
                        or not before.get('windows') or [(w['duration_mins'],w['resets_at']) for w in before['windows']]!=[(w['duration_mins'],w['resets_at']) for w in after.get('windows',[])]):fail('Incomparable sample / 账号池、桶、周期必须一致')
                cost=sample.get('consumed_percentage_points')
                if isinstance(cost,bool) or not isinstance(cost,(int,float)) or not math.isfinite(cost) or cost<0:fail('Invalid consumption / 实测交接消耗无效')
                if before.get('remaining',-1)-after.get('remaining',-1)!=cost:fail('Sample cost mismatch / 消耗必须与前后实测读数一致')
                value=max(value,min(95,2*cost+2),old,6)
            self._put(db,'checkpoint_reserve:'+member,value)
            db.execute('INSERT INTO reserve_proposals VALUES(?,?,?,?,?,?,?)',(uuid.uuid4().hex,member,old,value,reason,encoded(sample) if sample else None,time.time()))
            self.event(db,'reserve_increased',member=member,previous=old,reserve=value,reason=reason)
            return {'reserve':value,'resume':min(100,value+5),'previous':old}

    def quota_error(self,member,reason,*,seconds=60):
        safe_evidence(reason)
        if not 1<=seconds<=3600:fail('Invalid backoff / 限流退避须在1到3600秒')
        with self.transaction() as db:
            q=self._quotas(db).get(member,{})
            if not q:fail('Unknown member / 未知成员')
            pool=q.get('pool') or member
            db.execute('INSERT INTO pool_backoff VALUES(?,?,?,1) ON CONFLICT(pool) DO UPDATE SET until_at=max(until_at,excluded.until_at),reason=excluded.reason,needs_fresh=1',(pool,time.time()+seconds,reason))
            self.event(db,'quota_pool_backoff',member=member,pool=pool,reason=reason)
            return {'stopped_new_work':True,'pool':pool,'requires_fresh_reading':True}

    def _pool_blocked(self,db,member):
        quota=self._quotas(db).get(member,{})
        row=db.execute('SELECT * FROM pool_backoff WHERE pool=?',(quota.get('pool') or member,)).fetchone()
        if not row:return False
        # 到达退避/重置时间本身不能证明恢复，要求退避后的新可靠读数。
        return not (time.time()>=row['until_at'] and quota.get('reliability')=='fresh' and quota.get('observed_at',0)>row['until_at'] and quota.get('remaining',0)>self._reserve(db,member))

    def _stage_status(self,db):
        return {'queue':[dict(r) for r in db.execute('SELECT id,task_id,version,digest,status,reviewer_epoch,execution_id,created FROM review_stages ORDER BY created')],
            'issues':[dict(r) for r in db.execute('SELECT * FROM review_issues')],
            'publications':[dict(r) for r in db.execute('SELECT * FROM publications')],
            'pool_backoff':[dict(r) for r in db.execute('SELECT * FROM pool_backoff')],
            'reserve_proposals':[dict(r) for r in db.execute('SELECT * FROM reserve_proposals')],
            'unreviewed_work_is_not_accepted':True}
