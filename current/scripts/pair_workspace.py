"""阶段工作树、基线、发布和清理；Git副作用先登记，未知结果先对账。"""
from __future__ import annotations

import json
from pathlib import Path
import re
import hashlib
import shutil
import time
import uuid

try:
    from .pair_review_runtime import git,tree_stamp
    from .pair_reviewer import fail,safe_evidence
    from .pair_stages import encoded
except ImportError:
    from pair_review_runtime import git,tree_stamp
    from pair_reviewer import fail,safe_evidence
    from pair_stages import encoded


def owned_tree(store,path):
    with store.transaction() as db:project=Path(db.execute('SELECT project FROM collaborations').fetchone()[0])
    path=Path(path).absolute()
    parent=(project/'.codex-pair'/'worktrees').resolve()
    if path.resolve()!=path or path==parent or not path.is_relative_to(parent):fail('Invalid managed worktree / 只操作本项目内明确登记的独立工作树')
    record=store.setting('managed_tree:'+str(path))
    if not record:fail('Unowned worktree / 未登记的工作树不自动接管或删除')
    return path,record


def freeze_snapshot(store,workspace,artifact,allowed):
    root=Path(workspace).resolve(strict=True)
    manifest=artifact.get('manifest')
    if not isinstance(manifest,dict) or not manifest:fail('Manifest required / 快照需要明确文件摘要清单')
    with store.transaction() as db:project=db.execute('SELECT project FROM collaborations').fetchone()[0]
    try:from .pair_git import prepare_pair_directory,filesystem_path
    except ImportError:from pair_git import prepare_pair_directory,filesystem_path
    try:from .pair_stages import digest
    except ImportError:from pair_stages import digest
    key=digest({'baseline':artifact['baseline'],'manifest':manifest})
    directory=Path(project)/'.codex-pair'/'archives'/(store.pair_id+'-snapshots')/key
    prepare_pair_directory(project,str(directory),'archives')
    for relative,expected in manifest.items():
        if not isinstance(relative,str) or not relative or Path(relative).is_absolute() or '..' in Path(relative).parts or '.git' in Path(relative).parts or '.codex-pair' in Path(relative).parts:fail('Invalid snapshot path / 快照路径不能越界或包含运行状态')
        if not any(relative==a or a.endswith('/') and relative.startswith(a) for a in allowed):fail('Snapshot scope exceeded / 快照超出任务允许范围')
        source=root/relative;target=directory/relative
        if source.resolve()!=source.absolute() or not source.resolve().is_relative_to(root):fail('Snapshot redirection / 快照源存在链接或重定向')
        source=filesystem_path(source);target=filesystem_path(target)
        actual=hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else None
        if actual!=expected:fail('Snapshot changed / 快照源与摘要不符')
        if expected is not None:
            target.parent.mkdir(parents=True,exist_ok=True)
            if target.exists():
                if hashlib.sha256(target.read_bytes()).hexdigest()!=expected:fail('Frozen snapshot changed / 已冻结快照被修改')
            else:
                with target.open('xb') as handle:handle.write(source.read_bytes())
            if hashlib.sha256(target.read_bytes()).hexdigest()!=expected:fail('Snapshot copy mismatch / 冻结期间源文件发生变化')
    return dict(artifact,frozen_root=str(directory),snapshot_digest=key)


def create_tree(store,member,session,cwd,target,base):
    if target not in ('A','B','C','D'):fail('Visible member required / 只能为可见成员登记业务工作树')
    with store.transaction() as db:
        store._a_actor(db,member,session,cwd)
        project=db.execute('SELECT project FROM collaborations').fetchone()[0]
        if not store._team_ready(db):fail('Two members required / 至少两个有效可见绑定')
    head=git(project,'rev-parse','--verify','--end-of-options',base+'^{commit}')
    root=Path(project)/'.codex-pair'/'worktrees'/(store.pair_id+'-'+target.lower())
    try:
        from .pair_git import prepare_pair_directory
    except ImportError:
        from pair_git import prepare_pair_directory
    prepare_pair_directory(project,str(root.parent),'worktrees')
    branch='codex-pair/'+store.pair_id+'/'+target.lower()
    if root.exists():
        owned_tree(store,root)
        if git(root,'rev-parse','HEAD')!=head:fail('Existing tree differs / 已有工作树不覆盖')
    else:
        # 保留意图，崩溃后不能因缺少成功回执而再创建。
        key='managed_tree:'+str(root)
        prior=store.setting(key)
        if prior and prior.get('state')=='creating':fail('Creation unknown / 先核实已有创建意图')
        store.setting(key,{'state':'creating','member':target,'base':head,'branch':branch,'path':str(root)})
        git(project,'worktree','add','-b',branch,str(root),head)
        store.setting(key,{'state':'ready','member':target,'base':head,'branch':branch,'path':str(root)})
    return {'path':str(root),'branch':branch,'base':head,'original_cli_unchanged':True}


def update_baseline(store,member,session,cwd,target,stage_id):
    with store.transaction() as db:
        store._a_actor(db,member,session,cwd)
        stage=db.execute("SELECT * FROM review_stages WHERE id=? AND status='passed'",(stage_id,)).fetchone()
        if not stage:fail('Reviewed baseline required / 基线必须是已审核固定版本')
        bound=store._member(db,target)
        if not store._idle(db,[target]) or db.execute("SELECT 1 FROM tasks WHERE assignee=? AND status='active'",(target,)).fetchone():fail('Unsafe phase boundary / 成员仍有业务或工具运行')
    path=json.loads(bound['proof']).get('git',{}).get('path') or bound['cwd']
    if tree_stamp(path)['changes']:fail('Dirty tree / 保留脏改动，先核对归属')
    artifact=json.loads(stage['artifact'])
    if artifact['kind']!='commit':fail('Commit baseline required / 基线更新需要提交')
    git(path,'merge','--no-edit',artifact['head'])
    return {'head':git(path,'rev-parse','HEAD'),'requires_new_review':True,'history_preserved':True}


def cleanup_tree(store,member,session,cwd,path,*,processes_stopped,evidence):
    safe_evidence(evidence)
    if processes_stopped is not True:fail('Process evidence required / 先核实本工作树无运行进程')
    path,record=owned_tree(store,path)
    with store.transaction() as db:
        store._a_actor(db,member,session,cwd)
        if record.get('role')!='reviewer':
            if not store._idle(db,[record['member']]):fail('Running operations / 工作树仍在使用')
            if db.execute("SELECT 1 FROM tasks WHERE assignee=? AND status NOT IN ('completed','integrated','handed_back','cancelled')",(record['member'],)).fetchone():fail('Unpreserved work / 仍有未收尾任务或交付')
        reviewer=store._reviewer_row(db)
        if reviewer and Path(reviewer['cwd'])==path and reviewer['state']!='retired':fail('Reviewer still active / 先核实并退役审核者')
        project=db.execute('SELECT project FROM collaborations').fetchone()[0]
    if tree_stamp(path)['changes']:fail('Uncommitted work / 有未提交内容，不能清理')
    head=git(path,'rev-parse','HEAD')
    # detached HEAD 也先保存在本地专用引用，不能把 worktree remove 当作提交保留。
    ref='refs/codex-pair/saved/'+store.pair_id+'/'+uuid.uuid4().hex
    git(project,'update-ref',ref,head)
    if git(project,'rev-parse',ref)!=head:fail('Commit preservation failed / 提交保留未确认')
    git(project,'worktree','remove',str(path))
    store.setting('managed_tree:'+str(path),dict(record,state='removed',saved_ref=ref,head=head,evidence=evidence))
    return {'removed':str(path),'recoverable_commit':ref,'evidence_preserved':True}


def publish_stage(store,member,session,cwd,stage_id,remote,branch,*,reconcile=False):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]{0,200}',branch) or '..' in branch or branch.endswith('/'):fail('Invalid branch / 分支名称无效')
    with store.transaction() as db:
        actor=store._a_actor(db,member,session,cwd)
        from_proof=json.loads(actor['proof'])
        if from_proof.get('modelProvider')!='openai' or not str(from_proof.get('model','')).startswith('gpt-'):fail('Official publisher required / 统一发布者必须是已核验官方GPT')
        stage=db.execute('SELECT * FROM review_stages WHERE id=?',(stage_id,)).fetchone()
        if not stage or stage['status']!='passed':fail('Review not passed / 不发布待审成果')
        task=db.execute('SELECT * FROM tasks WHERE id=?',(stage['task_id'],)).fetchone()
        if not task or not store._stage_passed(db,task['id']):fail('Stale review / 当前需求或最新阶段未通过审核，不能沿用旧放行记录')
        if task['task_kind']!='integration':fail('Integrated candidate required / 最终发布必须有整合后的独立审核及回归阶段')
        project=db.execute('SELECT project FROM collaborations').fetchone()[0]
        if not store._option(db,'publish_enabled',False):fail('Publishing disabled / 用户当前要求不推送，发布开关关闭')
        allowed=store._option(db,'publish_target',{})
        if allowed.get('remote')!=remote or allowed.get('branch')!=branch:fail('Unconfirmed publish target / 用户仓库、远端和分支须经明确核对登记')
    artifact=json.loads(stage['artifact']);root=artifact.get('workspace') or project
    if artifact['kind']!='commit' or tree_stamp(root)!={'head':artifact['head'],'changes':'','diff':''}:fail('Final candidate changed / 发布树须与已审版本一致且干净')
    url=git(root,'remote','get-url','--push',remote)
    if url!=allowed.get('url') or not re.match(r'^(https://github\.com/[^/]+/[^/]+|git@github\.com:[^/]+/[^/]+?)(\.git)?$',url):fail('Remote changed / 只发布经核对的用户GitHub仓库')
    head=artifact['head']
    with store.transaction() as db:
        old=db.execute('SELECT * FROM publications WHERE stage_id=? AND remote=? AND branch=? ORDER BY created DESC LIMIT 1',(stage_id,remote,branch)).fetchone()
        if old and old['status'] in ('pushing','unknown') and not reconcile:fail('Push outcome unknown / 先只读核对远端，不重复推送')
    observed=git(root,'ls-remote',remote,'refs/heads/'+branch)
    remote_head=observed.split()[0] if observed else None
    if remote_head==head:
        with store.transaction() as db:
            if old:db.execute("UPDATE publications SET status='confirmed',evidence=?,updated=? WHERE id=?",(encoded({'remote_head':head,'reconciled':True}),time.time(),old['id']))
        return {'confirmed':True,'head':head,'remote':remote,'branch':branch,'replayed':False}
    if reconcile:return {'confirmed':False,'remote_head':remote_head,'requires_investigation':True,'replayed':False}
    if remote_head:
        git(root,'fetch','--no-tags',remote,'refs/heads/'+branch)
        git(root,'merge-base','--is-ancestor','FETCH_HEAD',head)
    key=uuid.uuid4().hex;now=time.time()
    with store.transaction() as db:db.execute('INSERT INTO publications VALUES(?,?,?,?,?,?,?, ?,?)',(key,stage_id,head,remote,branch,'pushing',encoded({'before_remote':remote_head}),now,now))
    try:git(root,'push',remote,head+':refs/heads/'+branch)
    except Exception as exc:
        with store.transaction() as db:db.execute("UPDATE publications SET status='unknown',evidence=?,updated=? WHERE id=?",(encoded({'error':str(exc),'before_remote':remote_head}),time.time(),key))
        raise
    observed=git(root,'ls-remote',remote,'refs/heads/'+branch)
    confirmed=bool(observed and observed.split()[0]==head)
    with store.transaction() as db:db.execute('UPDATE publications SET status=?,evidence=?,updated=? WHERE id=?',('confirmed' if confirmed else 'unknown',encoded({'remote_head':observed.split()[0] if observed else None}),time.time(),key))
    return {'confirmed':confirmed,'head':head,'branch':branch,'publication':key}
