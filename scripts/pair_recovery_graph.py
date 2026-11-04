"""只从持久依赖、明确修复关系和实际容量构造恢复环。"""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

try:
    from .pair_recovery import digest,encoded
    from .pair_reviewer import fail,safe_evidence
except ImportError:
    from pair_recovery import digest,encoded
    from pair_reviewer import fail,safe_evidence


def components(edges):
    forward={};reverse={}
    for source,target in edges:
        forward.setdefault(source,set()).add(target);forward.setdefault(target,set())
        reverse.setdefault(target,set()).add(source);reverse.setdefault(source,set())
    seen=set();order=[]
    for root in sorted(forward):
        if root in seen:continue
        stack=[(root,False)]
        while stack:
            node,exit_node=stack.pop()
            if exit_node:order.append(node);continue
            if node in seen:continue
            seen.add(node);stack.append((node,True))
            stack.extend((child,False) for child in sorted(forward[node],reverse=True) if child not in seen)
    seen=set();result=[]
    for root in reversed(order):
        if root in seen:continue
        part=[];stack=[root];seen.add(root)
        while stack:
            node=stack.pop();part.append(node)
            for parent in reverse[node]:
                if parent not in seen:seen.add(parent);stack.append(parent)
        if len(part)>1 or root in forward[root]:result.append(sorted(part))
    return sorted(result)


def graph_snapshot(store,db):
    tasks={r['id']:dict(r) for r in db.execute('SELECT * FROM tasks')}
    stages={r['id']:dict(r) for r in db.execute('SELECT id,task_id,status FROM review_stages')}
    edges=set();relations=[]
    for row in db.execute('SELECT task_id,depends_on FROM dependencies'):
        if not store._task_obligation_satisfied(db,row['depends_on']):edges.add(('task:'+row['task_id'],'task:'+row['depends_on']))
    for row in stages.values():
        if row['status'] not in ('passed','stale'):edges.add(('task:'+row['task_id'],'stage:'+row['id']))
    for row in db.execute('SELECT task_id,source_stage,issue_id FROM repair_links'):
        relations.append({'id':'issue:'+row['issue_id'],'repair_task':row['task_id'],'blocked_stages':[row['source_stage']],'blocked_tasks':[]})
    for row in db.execute("SELECT value FROM pair_settings WHERE key LIKE 'recovery_relation:%' ORDER BY key"):
        value=json.loads(row[0])
        if value['state']=='active':relations.append(value)
    for relation in relations:
        repair=tasks.get(relation['repair_task'])
        if not repair or repair['status'] in ('completed','integrated','cancelled'):continue
        for stage in relation['blocked_stages']:
            if stage in stages and stages[stage]['status'] not in ('passed','stale'):
                edges.add(('stage:'+stage,'task:'+repair['id']))
        for task in relation['blocked_tasks']:
            if task in tasks and not store._task_obligation_satisfied(db,task):edges.add(('task:'+task,'task:'+repair['id']))
        owner=repair['assignee']
        task_occupants=[key for key in store._task_backlog_ids(db,owner) if key!=repair['id']]
        stage_occupants=[key for key in store._stage_backlog_ids(db,owner) if stages[key]['task_id']!=repair['id']]
        for kind,occupants in (('task',task_occupants),('stage',stage_occupants)):
            if len(occupants)<store._review_capacity(db,kind):continue
            resource='capacity:'+kind+':'+owner
            edges.add(('task:'+repair['id'],resource))
            edges.update((resource,kind+':'+key) for key in occupants)
    loops=components(edges)
    return {'edges':[list(edge) for edge in sorted(edges)],'components':loops,
            'fingerprint':digest(sorted(edges)),'relations':relations}


class RecoveryGraphMixin:
    def register_recovery_relation(self,member,session,cwd,repair_task,blocked_tasks,blocked_stages,cause_key,evidence,source_stable_id=None):
        safe_evidence(evidence)
        if (not isinstance(cause_key,str) or not cause_key.strip() or len(cause_key)>256
                or not isinstance(blocked_tasks,list) or not isinstance(blocked_stages,list)
                or not blocked_tasks and not blocked_stages
                or any(not isinstance(key,str) or not key for key in blocked_tasks+blocked_stages)):
            fail('Explicit recovery relation required / 须明确根因、受阻任务或阶段及证据')
        with self.transaction() as db:
            actor=self._a_actor(db,member,session,cwd)
            task=db.execute('SELECT * FROM tasks WHERE id=?',(repair_task,)).fetchone()
            if not task or task['task_kind'] not in ('repair','implementation','environment'):
                fail('Registered remediation required / 先登记修复或环境任务')
            for key in blocked_tasks:
                if key==repair_task or not db.execute('SELECT 1 FROM tasks WHERE id=?',(key,)).fetchone():fail('Invalid blocked task')
            for key in blocked_stages:
                stage=db.execute('SELECT task_id FROM review_stages WHERE id=?',(key,)).fetchone()
                if not stage or stage[0]==repair_task:fail('Invalid blocked stage')
            scope={'blocked_tasks':sorted(set(blocked_tasks)),'blocked_stages':sorted(set(blocked_stages))}
            source={'origin_pair':self.pair_id,'cause_key':cause_key,**scope}
            stable='blocker:'+digest(source)[:32]
            if source_stable_id is not None:
                imported=self._option(db,'recovery_import:'+source_stable_id)
                previous=self._option(db,'recovery_relation:'+source_stable_id)
                if previous:
                    old_task=db.execute('SELECT status FROM tasks WHERE id=?',(previous['repair_task'],)).fetchone()
                    if previous['repair_task']!=repair_task and (not old_task or old_task[0] not in ('handed_back','cancelled','completed','integrated')):fail('Original remediation unresolved')
                    if any(previous[key]!=scope[key] for key in ('blocked_tasks','blocked_stages')) or previous['cause_key']!=cause_key:fail('Original problem identity must be preserved')
                    stable=source_stable_id;source=previous['origin']
                else:
                    if not imported or imported['repair_task']!=repair_task:fail('Verified history import required')
                    stable=source_stable_id;source=imported['origin']
            value={'id':stable,'repair_task':repair_task,**scope,'cause_key':cause_key,'origin':source,
                   'evidence':evidence,'state':'active','actor_session':actor['session_uuid']}
            old=self._option(db,'recovery_relation:'+stable)
            if old:
                if old['repair_task']!=repair_task and source_stable_id==stable:
                    value['predecessors']=old.get('predecessors',[])+[old['repair_task']]
                    self._put(db,'recovery_relation:'+stable,value)
                    self.event(db,'recovery_relation_successor',stable_id=stable,previous=old['repair_task'],successor=repair_task,evidence=evidence)
                    return dict(value,duplicate=False,history_retained=True)
                if any(old[key]!=value[key] for key in ('repair_task','blocked_tasks','blocked_stages','cause_key')):
                    fail('Recovery relation immutable / 原问题换负责人或任务须保留准确继承，不重新计数')
                return dict(old,duplicate=True)
            self._put(db,'recovery_relation:'+stable,value)
            self.event(db,'recovery_relation_registered',relation=value)
            return dict(value,duplicate=False)

    def import_recovery_history(self,member,session,cwd,source_state_root,source_pair,stable_id,repair_task,evidence):
        """只读源库，保留累计历史；不复制源许可、令牌、账号或执行权。"""
        try:from .pair_diagnostics import _read_guard
        except ImportError:from pair_diagnostics import _read_guard
        try:from .pair_core import identifier
        except ImportError:from pair_core import identifier
        identifier(source_pair);safe_evidence(evidence)
        path=Path(source_state_root).absolute()/source_pair/'state.sqlite3'
        if source_pair==self.pair_id or path.resolve()!=path or not path.is_file():fail('Distinct source state required')
        with _read_guard(path) as stable_main,closing(sqlite3.connect(path.as_uri()+'?mode=ro'+('&immutable=1' if stable_main else ''),uri=True)) as source:
            source.row_factory=sqlite3.Row;source.execute('PRAGMA query_only=ON');source.execute('BEGIN')
            if source.execute('PRAGMA user_version').fetchone()[0]!=14:fail('Source history schema unsupported / 来源须有明确恢复历史')
            if source.execute('SELECT id FROM collaborations').fetchone()[0]!=source_pair:fail('Source pair mismatch')
            row=source.execute('SELECT value FROM pair_settings WHERE key=?',('recovery_relation:'+stable_id,)).fetchone()
            if not row:fail('Stable source relation missing')
            relation=json.loads(row[0])
            history=[dict(row) for row in source.execute('SELECT kind,stable_id,logical_id,state,started,updated,evidence FROM recovery_attempts WHERE stable_id=?',(stable_id,))]
        with self.transaction() as db:
            self._a_actor(db,member,session,cwd)
            if not db.execute('SELECT 1 FROM tasks WHERE id=?',(repair_task,)).fetchone():fail('Destination task missing')
            reference={'source_pair':source_pair,'relation':relation,'history':history}
            key='recovery_import:'+stable_id;old=self._option(db,key)
            if old and (old['origin']!=relation['origin'] or old['repair_task']!=repair_task):fail('History import identity changed')
            for item in history:
                logical=item['logical_id']
                previous=db.execute('SELECT * FROM recovery_attempts WHERE kind=? AND stable_id=? AND logical_id=?',(item['kind'],stable_id,logical)).fetchone()
                if previous and (previous['started']!=item['started'] or previous['updated']>item['updated']):fail('Imported history conflicts / 不覆盖较新的原记录')
                db.execute('INSERT OR IGNORE INTO recovery_attempts VALUES(?,?,?,?,?,?,?)',
                           (item['kind'],stable_id,logical,item['state'],item['started'],item['updated'],item['evidence']))
                if previous and item['updated']>previous['updated']:
                    db.execute('UPDATE recovery_attempts SET state=?,updated=?,evidence=? WHERE kind=? AND stable_id=? AND logical_id=?',
                        (item['state'],item['updated'],item['evidence'],item['kind'],stable_id,logical))
            record={'stable_id':stable_id,'repair_task':repair_task,'source_digest':digest(reference),
                    'origin':relation['origin'],'source_pair':source_pair,'evidence':evidence,'permissions_imported':False}
            self._put(db,key,record);self.event(db,'recovery_history_imported',**record)
            return record

    def _general_recovery_cycle(self,db,task,proposal):
        relation=self._option(db,'recovery_relation:'+proposal['stable_blocker'])
        if relation is None:return None
        if relation['repair_task']!=task['id'] or proposal['remediates']!=[relation['id']]:fail('Exact remediation relation required')
        graph=graph_snapshot(self,db);node='task:'+task['id']
        component=next((part for part in graph['components'] if node in part and any(key.startswith('capacity:') for key in part)),None)
        if component is None:fail('Recovery cycle no longer exists / 已无实际准入环，回普通流程')
        obligations={'task:'+key for key in relation['blocked_tasks']}|{'stage:'+key for key in relation['blocked_stages']}
        if not obligations.intersection(component):fail('Unrelated recovery cycle / 实际受阻义务不在此准入环中')
        for parent in db.execute('SELECT depends_on FROM dependencies WHERE task_id=?',(task['id'],)):
            if not self._task_obligation_satisfied(db,parent[0]) and 'task:'+parent[0] not in component:
                fail('Normal prerequisite unresolved / 独立正常前置须先处理')
        return {'fingerprint':digest(component),'nodes':component,
                'edges':[edge for edge in graph['edges'] if all(key in component for key in edge)],'explicit_relation':relation['id']}
