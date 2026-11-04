"""正式审核后的项目内运行副本切换；先停旧桥、备份、复制，再核验实际加载。"""
from __future__ import annotations

from contextlib import ExitStack, closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
from types import SimpleNamespace
import uuid

try:
    from .install_skill import package_manifest, checked_path
    from .pair_recovery import VerifiedRecoveryEvidence, digest, encoded, candidate_manifest
    from .pair_reviewer import fail
    from .pair_watchdog import process_identity, _exit_proven
except ImportError:
    from install_skill import package_manifest, checked_path
    from pair_recovery import VerifiedRecoveryEvidence, digest, encoded, candidate_manifest
    from pair_reviewer import fail
    from pair_watchdog import process_identity, _exit_proven


CHECKS=['reviewed_full_package_unchanged','sqlite_integrity_and_schema_14','old_owned_process_exited',
        'exclusive_bridge_and_reviewer_locks','loaded_module_paths_and_hashes','new_process_identity_and_nonce_probe']
CHECK_CONTRACT=digest(CHECKS)
SELECTION='runtime_selection_v14'


def application_gate(store,db,application_id,generation=None):
    row=db.execute('SELECT * FROM recovery_applications WHERE id=?',(application_id,)).fetchone()
    if not row:fail('Runtime application missing')
    policy=store._require_recovery_policy(db,'patch_application')
    if row['policy_version']!=policy['version'] or row['authorization_version']!=store._authorization_version(db):fail('Runtime application authorization changed')
    recovery=store._recovery_request(db,row['recovery_id'])
    reasons=[reason for reason in store._recovery_gate(db,task_id=recovery['source_task'],action='patch_application') if reason.get('application')!=application_id]
    if reasons or generation is not None and store._control_generation(db)!=generation:fail('Runtime application paused / 新控制事件或其他门禁阻止应用')
    stage=db.execute('SELECT status,digest FROM review_stages WHERE id=?',(row['stage_id'],)).fetchone()
    if not stage or stage['status']!='passed' or stage['digest']!=row['candidate_digest']:fail('Runtime review changed')
    store._formal_passed_stage(db,row['stage_id'])


@contextmanager
def launch_guard(store,application_id,generation):
    try:from .pair_core import FileLock
    except ImportError:from pair_core import FileLock
    with FileLock(store.control_lock,timeout=30):
        with store.transaction() as db:
            application_gate(store,db,application_id,generation)
            selection=store._option(db,SELECTION)
            if not selection or selection['application']!=application_id or selection['state']!='selected':fail('Runtime launch already attempted / 只核对原进程，不重放')
            if selection.get('launch_attempts',0)>=3:fail('Runtime launch retry budget exhausted / 三次明确启动失败，保留现场并报告用户')
            selection['launch_attempts']=selection.get('launch_attempts',0)+1
            selection['state']='launching';selection['launch_requested']=time.time();store._put(db,SELECTION,selection)
        yield None


def launch_selected(store,application_id,executable,generation):
    try:from .pair_windows import bridge
    except ImportError:from pair_windows import bridge
    selected=store.setting(SELECTION)
    verify_cli(store,executable)
    def started(pid):
        with store.transaction() as db:
            value=store._option(db,SELECTION);value['process']=process_identity(pid);store._put(db,SELECTION,value)
    def not_started():
        with store.transaction() as db:
            value=store._option(db,SELECTION)
            if value['application']==application_id and value['state']=='launching' and not value.get('process'):
                value['state']='selected';value['last_start']='popen_failed_no_child';store._put(db,SELECTION,value)
                store.event(db,'runtime_launch_not_started',application=application_id)
    result=bridge(store,SimpleNamespace(__file__=str(Path(selected['root'])/'scripts'/'pair.py')),executable,
        before_start=lambda:launch_guard(store,application_id,generation),on_started=started,on_start_failed=not_started)
    return {'state':'switch_started','bridge':result,'next':'reconcile-runtime-application','installed_profiles_changed':False}


def verify_cli(store,executable):
    try:from .pair_producer import file_identity
    except ImportError:from pair_producer import file_identity
    trusted=store.setting('cli_trusted_executable:A')
    if not trusted or file_identity(executable)!=trusted['file']:fail('Runtime CLI differs / 本次CLI与原A已核验可执行文件不一致')


def selected_entry(store,default):
    selection=store.setting(SELECTION)
    if not selection:return Path(default).resolve()
    root=Path(selection['root'])
    if digest(package_manifest(root))!=selection['manifest_digest']:fail('Selected runtime changed / 固定运行副本变化，保留切换门禁')
    with store.transaction() as db:
        row=db.execute('SELECT state,plan FROM recovery_applications WHERE id=?',(selection['application'],)).fetchone()
        if not row or row['state'] not in ('prepared','unknown','applied_and_verified') or json.loads(row['plan'])['new_runtime_digest']!=selection['manifest_digest']:fail('Selected runtime not authorized')
    return root/'scripts'/'pair.py'


def drain_requested(store):
    with store.transaction() as db:
        request=store._option(db,'runtime_drain_v14')
        if not request:return False
        runtime=db.execute('SELECT owner FROM runtime WHERE id=1').fetchone()
        if runtime['owner']!=request['old']['owner']:return False
        if store._recovery_unresolved(db):return False
        if db.execute("SELECT 1 FROM auxiliary_executions WHERE state IN ('reserved','starting','running','unknown')").fetchone():return False
        return request.get('state')=='draining'


def record_loaded_runtime(store):
    root=Path(__file__).resolve().parent.parent
    manifest=package_manifest(root)
    imported={}
    for name,module in tuple(sys.modules.items()):
        path=getattr(module,'__file__',None)
        if name.rsplit('.',1)[-1].startswith('pair') and path:
            path=Path(path).resolve()
            if not path.is_relative_to(root):fail('Mixed runtime modules / 加载了其他副本模块')
            relative=path.relative_to(root).as_posix()
            actual=hashlib.sha256(path.read_bytes()).hexdigest()
            if manifest.get(relative)!=actual:fail('Loaded module missing or changed')
            imported[relative]=actual
    with store.transaction() as db:
        selection=store._option(db,SELECTION)
        if selection and (Path(selection['root']).resolve()!=root or digest(manifest)!=selection['manifest_digest']):fail('Loaded runtime differs from selected runtime')
        if db.execute('PRAGMA user_version').fetchone()[0]!=14 or db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':fail('Runtime state integrity failed')
        runtime=dict(db.execute('SELECT * FROM runtime WHERE id=1').fetchone())
        record={'root':str(root),'manifest_digest':digest(manifest),'modules':imported,'schema':14,
            'integrity':'ok','owner':runtime['owner'],'process':process_identity(os.getpid()),
            'application':selection.get('application') if selection else None,
            'nonce':selection.get('nonce') if selection else None,'loaded_at':time.time()}
        if record['process']['state']!='alive':fail('Loaded runtime process unknown')
        store._put(db,'runtime_loaded_v14',record)
        store.event(db,'runtime_loaded',application=record['application'],manifest_digest=record['manifest_digest'],owner=record['owner'])


class ApplicationAdapter:
    def __init__(self,application,state_digest,facts,receipts):
        self.application=application;self.state_digest=state_digest;self.facts=facts;self.receipts=receipts
    def verify(self,kind,context):
        if context['application']['id']!=self.application or context['state_digest']!=self.state_digest:fail('Application evidence changed')
        return VerifiedRecoveryEvidence(kind,digest(context),tuple(self.receipts),self.facts)


class RuntimeApplyMixin:
    def apply_recovery_runtime(self,member,session,cwd,application_id,executable):
        try:
            from .pair_core import FileLock
            from .pair_git import prepare_pair_directory
            from .pair_windows import bridge
        except ImportError:
            from pair_core import FileLock
            from pair_git import prepare_pair_directory
            from pair_windows import bridge
        with FileLock(self.directory/'runtime-apply.lock',timeout=0):
            verify_cli(self,executable)
            with self._recovery_control_transaction() as db:
                self._a_actor(db,member,session,cwd)
                row=db.execute('SELECT * FROM recovery_applications WHERE id=?',(application_id,)).fetchone()
                if not row or row['state'] not in ('waiting_terminal','prepared','unknown'):fail('Reviewed application required')
                application_gate(self,db,application_id)
                generation=self._control_generation(db)
                plan=json.loads(row['plan']);recovery=self._recovery_request(db,row['recovery_id']);proposal=json.loads(recovery['proposal'])
                if plan['verification_contract']!=CHECK_CONTRACT:fail('Runtime verification contract unsupported')
                policy=self._require_recovery_policy(db,'patch_application')
                if row['policy_version']!=policy['version'] or row['authorization_version']!=self._authorization_version(db):fail('Runtime application authorization changed')
                if self._cli_compatibility_gate(db,member,'runtime_switch'):fail('Exact CLI compatibility required')
                project=Path(db.execute('SELECT project FROM collaborations').fetchone()[0])
                loaded=self._option(db,'runtime_loaded_v14')
                drain=self._option(db,'runtime_drain_v14')
                if drain and drain.get('state')=='complete':
                    self._put(db,'runtime_drain_history:'+drain['application'],drain);drain=None
                if not drain:
                    if not loaded or loaded['manifest_digest']!=plan['old_runtime_digest']:fail('Old loaded runtime unverified / 原桥须先登记实际加载摘要')
                    if digest(package_manifest(Path(loaded['root'])))!=plan['old_runtime_digest']:fail('Old runtime bytes changed')
                    drain={'application':application_id,'state':'draining','old':loaded,'requested':time.time()}
                    self._put(db,'runtime_drain_v14',drain)
                    watchdog=self._option(db,'bridge_watchdog_v14')
                    if watchdog:
                        watchdog['enabled']=False;watchdog['state']='disabled_for_runtime_application';self._put(db,'bridge_watchdog_v14',watchdog)
                    self.event(db,'runtime_drain_requested',application=application_id)
                    return {'state':'waiting_old_terminal','dispatch_blocked':True,'retry_after_seconds':2}
                if drain['application']!=application_id:fail('Another runtime application owns drain')
                pending=self._option(db,SELECTION)
                if pending and pending['application']==application_id:
                    if pending['state']!='selected':return {'state':'switch_started','replay_forbidden':True,'next':'reconcile-runtime-application'}
            if pending and pending['application']==application_id:
                return launch_selected(self,application_id,executable,generation)
            old=drain['old']
            if not _exit_proven(process_identity(old['process']['pid']),old['process']):
                return {'state':'waiting_old_process_exit','force_kill':False,'retry_after_seconds':2}
            with ExitStack() as locks:
                for path in [self.directory/'bridge-start.lock',self.daemon_lock,self.directory/'reviewer-process.lock']+[self.directory/('relay-'+value+'.lock') for value in ('A','B','C','D')]:
                    locks.enter_context(FileLock(path,timeout=0))
                locks.enter_context(FileLock(self.control_lock,timeout=30))
                with self.transaction() as db:
                    application_gate(self,db,application_id,generation)
                    if self._recovery_unresolved(db):fail('Original execution unresolved')
                    if db.execute("SELECT 1 FROM auxiliary_executions WHERE state IN ('reserved','starting','running','unknown')").fetchone():fail('Original auxiliary execution unresolved')
                source=Path(proposal['candidate_root'])
                candidate_manifest(str(source),proposal['manifest'],proposal['allowed_paths'])
                manifest=package_manifest(source)
                if digest(manifest)!=plan['new_runtime_digest']:fail('Reviewed runtime manifest mismatch')
                reviewed={item['path']:item['sha256'] for item in proposal['manifest']}
                if reviewed!=manifest:fail('Full runtime must be reviewed / 运行副本全部文件及清单须包含在准确审核版本')
                destination=project/'.codex-pair'/'runtimes'/plan['new_runtime_digest']
                prepare_pair_directory(str(project),str(destination.parent),'runtimes')
                backup=Path(plan['backup_ref'])
                if not backup.is_absolute() or backup.resolve()!=backup.absolute() or backup.parent!=project/'.codex-pair'/'backups' or backup.suffix!='.sqlite3':fail('Exact project backup path required')
                prepare_pair_directory(str(project),str(backup.parent),'backups')
                intent={'destination':str(destination),'backup':str(backup),'manifest_digest':digest(manifest),'request_digest':row['request_digest']}
                with self.transaction() as db:
                    previous=self._option(db,'runtime_copy_intent:'+application_id)
                    if previous and previous!=intent:fail('Runtime copy intent changed')
                    if not previous and (destination.exists() or backup.exists()):fail('Unowned runtime destination / 保留未登记目录或备份')
                    self._put(db,'runtime_copy_intent:'+application_id,intent)
                destination.mkdir(exist_ok=True)
                for relative,checksum in manifest.items():
                    target=checked_path(destination/relative);target.parent.mkdir(parents=True,exist_ok=True)
                    temporary=checked_path(target.with_name(target.name+'.copy-'+application_id))
                    if target.exists():
                        if hashlib.sha256(target.read_bytes()).hexdigest()!=checksum:fail('Owned copy file changed / 不覆盖已复制文件')
                        continue
                    with temporary.open('wb') as output:output.write(checked_path(source/relative).read_bytes());output.flush();os.fsync(output.fileno())
                    if hashlib.sha256(temporary.read_bytes()).hexdigest()!=checksum:fail('Copied file digest mismatch')
                    os.replace(temporary,target)
                if package_manifest(destination)!=manifest:fail('Copied runtime differs')
                saved_copy=self.setting('runtime_copy:'+application_id)
                if backup.exists():
                    if saved_copy and (saved_copy['backup']!=str(backup) or saved_copy['backup_digest']!=hashlib.sha256(backup.read_bytes()).hexdigest()):fail('Registered runtime backup changed')
                    with closing(sqlite3.connect(backup.as_uri()+'?mode=ro',uri=True)) as target_db:
                        if target_db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or target_db.execute('PRAGMA user_version').fetchone()[0]!=14:fail('Existing runtime backup incomplete / 保留现场，未应用')
                        saved=target_db.execute('SELECT request_digest FROM recovery_applications WHERE id=?',(application_id,)).fetchone()
                        if not saved or saved[0]!=row['request_digest'] or target_db.execute('SELECT id FROM collaborations').fetchone()[0]!=self.pair_id:fail('Runtime backup identity differs')
                else:
                    with closing(sqlite3.connect(self.path)) as source_db,closing(sqlite3.connect(backup)) as target_db:
                        source_db.backup(target_db)
                        if target_db.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or target_db.execute('PRAGMA user_version').fetchone()[0]!=14:fail('Runtime backup verification failed')
                copied={'destination':str(destination),'manifest_digest':digest(manifest),'backup':str(backup),
                    'backup_digest':hashlib.sha256(backup.read_bytes()).hexdigest(),'old_exit_proven':True}
                with self.transaction() as db:
                    self._put(db,'runtime_copy:'+application_id,copied)
                # 已持有control锁，使用同一事务版本准备；不递归取得同一文件锁。
                if row['state']=='waiting_terminal':
                    with self.transaction() as db:
                        application_gate(self,db,application_id,generation)
                        db.execute("UPDATE recovery_applications SET state='prepared',evidence=?,updated=? WHERE id=?",(encoded({'receipts':['runtime-copy:'+digest(copied)]}),time.time(),application_id))
                        self.event(db,'recovery_application_prepared',application=application_id,evidence=['runtime-copy:'+digest(copied)])
                with self.transaction() as db:
                    application_gate(self,db,application_id,generation)
                    current=db.execute('SELECT * FROM recovery_applications WHERE id=?',(application_id,)).fetchone()
                    if current['state']!='prepared' or current['authorization_version']!=self._authorization_version(db):fail('Runtime application changed before selection')
                    stage=db.execute('SELECT status,digest FROM review_stages WHERE id=?',(current['stage_id'],)).fetchone()
                    if not stage or stage['status']!='passed' or stage['digest']!=current['candidate_digest']:fail('Runtime review changed before selection')
                    selection={'application':application_id,'root':str(destination),'manifest_digest':digest(manifest),'nonce':uuid.uuid4().hex,'state':'selected','created':time.time()}
                    self._put(db,SELECTION,selection)
                    self.event(db,'runtime_selected',application=application_id,manifest_digest=selection['manifest_digest'])
            # 已提交唯一启动意图；结果不明时仅对账，不再次Popen。
            return launch_selected(self,application_id,executable,generation)

    def reconcile_runtime_application(self,member,session,cwd,application_id):
        status=self.status(probe_timeout=.3)
        with self.transaction() as db:
            self._a_actor(db,member,session,cwd)
            row=db.execute('SELECT * FROM recovery_applications WHERE id=?',(application_id,)).fetchone()
            if not row:fail('Application missing')
            self._formal_passed_stage(db,row['stage_id'])
            plan=json.loads(row['plan']);selection=self._option(db,SELECTION);loaded=self._option(db,'runtime_loaded_v14');copy=self._option(db,'runtime_copy:'+application_id)
            if not selection or selection['application']!=application_id or not loaded or loaded['application']!=application_id or not copy:fail('New runtime loading unconfirmed')
            actual=process_identity(loaded['process']['pid'])
            try:from .pair_watchdog import _same_process
            except ImportError:from pair_watchdog import _same_process
            if not _same_process(actual,loaded['process']) or loaded['owner']!=status['runtime']['owner'] or not status['runtime']['probe_confirmed']:fail('New runtime probe unconfirmed')
            exact=loaded['nonce']==selection['nonce'] and loaded['manifest_digest']==plan['new_runtime_digest'] and digest(package_manifest(Path(selection['root'])))==plan['new_runtime_digest']
            schema=db.execute('PRAGMA user_version').fetchone()[0]==14 and db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            old=self._option(db,'runtime_drain_v14')['old']
            terminal=_exit_proven(process_identity(old['process']['pid']),old['process'])
            facts={'actual_loaded_digest_matches':exact,'schema_matches':schema,'original_execution_terminal':terminal,
                'verification_checks_passed':exact and schema and terminal and bool(loaded['modules']) and plan['verification_contract']==CHECK_CONTRACT}
            state_digest=self._recovery_state_digest(db)
        self._recovery_evidence_adapter=ApplicationAdapter(application_id,state_digest,facts,['runtime-loaded:'+digest(loaded),'runtime-probe:'+status['runtime']['probe_ack']])
        result=self.reconcile_recovery_application(member,session,cwd,application_id)
        with self.transaction() as db:
            drain=self._option(db,'runtime_drain_v14');drain['state']='complete';self._put(db,'runtime_drain_v14',drain)
        return dict(result,check_contract=CHECK_CONTRACT,checks=CHECKS,watchdog_requires_reconfigure=True)
