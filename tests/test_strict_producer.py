"""真实临时 Store/官方格式 fixture；不启动 Codex 或执行脚本载荷。"""
import copy
import json
from pathlib import Path
import tempfile
import queue
import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from scripts import pair_producer as p
from scripts import pair_transport as t
from scripts import pair_review_receipts as access
from scripts import pair_review_runtime as runtime
from tests import test_final_receipt_settlement as final_tests
from tests.compatibility_fixture import register_cli_fixture


class FormatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.context = p.VerifiedProducer(p.ALPHA, {}, 'unit-only')
        self.raw = dict(id='one',type='CommandExecution',command=['pwsh','-Command','echo hello'],
                        status='completed',exit_code=0,cwd=self.temp.name,source='unified_exec_startup')

    def pair(self,argv):
        raw = dict(self.raw,command=argv)
        api = dict(raw,type='commandExecution',command=p.rust_shlex_join(argv),source='unifiedExecStartup')
        return api, raw

    def test_actual_bound_sample_matches_exact_forward_renderer(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/producer-alpha.json').read_text())
        for item in fixture.values():item['cwd']=self.temp.name
        self.assertEqual(p.rust_shlex_join(fixture['raw']['command']),fixture['api']['command'])
        items=access.receipt_items(list(fixture.values()),self.context)
        self.assertEqual(len(items),1)
        self.assertTrue(t.command_matches(fixture['api']['command'],items[0],producer_version=self.context))

    def test_pinned_upstream_quote_vectors(self):
        cases=[('',"''"),('abc','abc'),('a b',"'a b'"),("'",'"\'"'),('"',"'\"'"),
               ('a\nb',"'a\nb'"),('^a^b',"'^a''^b'"),('中',"'中'"),('C:\\a','"C:\\\\a"'),
               ("'\"",'"\'\\\""')]
        for word,expected in cases:
            with self.subTest(word=word):self.assertEqual(p.rust_shlex_join([word]),expected)

    def test_empty_argument_and_complex_payload_remain_exact(self):
        for script in ['',"echo 'single'",'echo "double"','echo a\necho b',"echo '$x' \\ ^ ! `"]:
            api,raw=self.pair(['pwsh','-Command',script,''])
            merged=access.receipt_items([api,raw],self.context)
            self.assertEqual(len(merged),1)
            self.assertTrue(t.command_matches(api['command'],merged[0],producer_version=self.context))
            self.assertFalse(t.command_matches(api['command']+' ',merged[0],producer_version=self.context))

    def test_nul_and_surrogate_fail_closed(self):
        for argv in [['pwsh','\0'],['pwsh','\ud800']]:
            self.assertIsNone(p.rust_shlex_join(argv))
        self.assertIsNone(t.command_receipt(dict(self.raw,command=['pwsh','\0'])))

    def test_version_string_or_unknown_source_cannot_enable_alpha(self):
        api,raw=self.pair(self.raw['command'])
        for version in [None,p.ALPHA,'future']:
            self.assertIsNone(t.merge_command_receipts(t.command_receipt(api),t.command_receipt(raw),producer_version=version))
        for source in ['agent','unknown',None]:
            self.assertEqual(access.receipt_items([dict(api,source=source),dict(raw,source=source)],self.context),[])

    def test_display_or_raw_alone_insufficient_and_duplicates_safe(self):
        api,raw=self.pair(self.raw['command'])
        for items in [[api],[raw],[api,api],[raw,raw]]:self.assertEqual(access.receipt_items(items,self.context),[])
        for items in [[api,raw],[raw,api],[api,api,raw],[raw,api,raw]]:
            self.assertEqual(len(access.receipt_items(items,self.context)),1)

    def test_wrong_argv_quote_semantics_and_redacted_collisions_rejected(self):
        api,raw=self.pair(self.raw['command'])
        for argv in [['pwsh','-File','echo hello'],['pwsh','-Command','echo','hello'],['pwsh','-Command','echo other']]:
            self.assertEqual(access.receipt_items([api,dict(raw,command=argv)],self.context),[])
        for display in ['pwsh -Command "echo hello"','pwsh -Command [REDACTED]']:
            self.assertEqual(access.receipt_items([dict(api,command=display),raw],self.context),[])
        for secret in ['sk-pretend-secret','sk-another-secret']:
            a,b=self.pair(['pwsh','-Command',secret])
            self.assertEqual(access.receipt_items([dict(a,command=a['command'].replace(secret,'[REDACTED]')),b],self.context),[])
        for marker in ['[REDACTED]','[REDACTED_SECRET]']:
            self.assertIsNone(p.strict_display(['echo',marker]))
            a,b=self.pair(['pwsh','-Command',marker])
            self.assertEqual(access.receipt_items([a,b],self.context),[])

    def test_conflicts_never_resurrect(self):
        api,raw=self.pair(self.raw['command'])
        for change in [{'exit_code':1},{'exit_code':False},{'status':'failed'},{'cwd':'relative'},
                       {'command':['different']},{'source':'agent'}]:
            for items in [[api,dict(raw,**change),raw],[raw,dict(raw,**change),api]]:
                self.assertEqual(access.receipt_items(items,self.context),[])


class NativeProcessTests(unittest.TestCase):
    @unittest.skipUnless(os.name=='nt','Windows native process evidence')
    def test_actual_non_model_child_identity_and_exit(self):
        # 仅普通Python子进程测试kernel32接口，不创建或恢复Codex。
        child=subprocess.Popen([sys.executable,'-B','-c','import sys; sys.stdin.read()'],stdin=subprocess.PIPE,
                               creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:
            first=p.process_identity(child.pid)
            self.assertEqual(first,p.process_identity(child.pid))
            self.assertEqual(first['path'],p.file_identity(sys.executable)['path'])
        finally:
            child.communicate(b'',timeout=10)
        with self.assertRaises(ValueError):p.process_identity(child.pid)


class ProducerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.f=final_tests.FinalSettlementTests();self.f.setUp();self.addCleanup(self.f.tearDown);self.addCleanup(self.f.doCleanups)
        self.file=self.f.project/'codex.exe';self.file.write_bytes(b'isolated build, never executed')
        self.proc={'pid':123,'created':456,'path':p.file_identity(self.file)['path']}
        self.launch={'file':p.file_identity(self.file),'version':p.ALPHA,'process':self.proc}
        self.patch=patch.object(p,'process_identity',return_value=self.proc);self.patch.start();self.addCleanup(self.patch.stop)
        self.f.controller.client=Mock(producer_launch=self.launch)
        self.f.command=p.rust_shlex_join(self.f.argv)
        self.f.item['source']='unified_exec_startup'
        api=dict(self.f.item,command=self.f.command,source='unifiedExecStartup',type='commandExecution')
        self.f.full['items']=[api,self.f.message];self.f.rollout([self.f.item])
        with self.f.store.transaction() as db:
            payload=json.loads(self.f.stage['payload']);payload['coordinator_required_checks'][0]['command']=self.f.command
            db.execute('UPDATE review_stages SET payload=? WHERE id=?',(json.dumps(payload),self.f.stage['id']))
            self.f.stage=dict(db.execute('SELECT * FROM review_stages WHERE id=?',(self.f.stage['id'],)).fetchone())
            row=db.execute('SELECT request FROM managed_permissions WHERE id=?',(self.f.permission['id'],)).fetchone()
            request=json.loads(row[0]);request['params']['command']=self.f.command
            db.execute('UPDATE managed_permissions SET request=? WHERE id=?',(json.dumps(request),self.f.permission['id']))
        with self.f.store.transaction() as db:
            db.execute('DELETE FROM pair_settings WHERE key=?',(p.KEY+self.f.lease['execution_id'],))
        self.record=p.record_producer(self.f.store,self.f.stage['id'],self.f.controller.state,self.launch)

    def test_final_query_settlement_and_report_use_bound_producer(self):
        self.assertEqual(self.f.finish()['verdict'],'passed')
        self.assertEqual(self.f.permission_status(),'success')
        record=self.f.store.setting(p.KEY+self.f.lease['execution_id'])
        self.assertTrue(record['finished'])
        self.assertEqual(record['binding']['turn'],'turn-one')

    def test_live_query_returns_exact_raw_argv_and_no_database_mutation(self):
        before=self.f.store.path.read_bytes()
        result=access.read_receipts(self.f.store.directory,self.f.controller._receipt_binding(self.f.stage),observer_factory=self.f.observer)
        self.assertEqual(result['receipts'][0]['command_argv'],self.f.argv)
        self.assertEqual(before,self.f.store.path.read_bytes())

    def test_changed_file_or_process_rejects_before_settlement(self):
        self.file.write_bytes(b'changed')
        self.assertEqual(self.f.finish()['verdict'],'needs_validation')
        self.assertEqual(self.f.permission_status(),'unknown')

    def test_pid_reuse_rejected(self):
        self.patch.stop()
        with patch.object(p,'process_identity',return_value=dict(self.proc,created=999)):
            self.assertEqual(self.f.finish()['verdict'],'needs_validation')
            self.assertEqual(self.f.permission_status(),'unknown')

    def test_missing_or_unknown_evidence_does_not_use_alpha_proof(self):
        self.f.controller.client=None
        with self.f.store.transaction() as db:
            db.execute('DELETE FROM pair_settings WHERE key=?',(p.KEY+self.f.lease['execution_id'],))
            parent=json.loads(self.f.store._member(db,'A')['proof']);parent['cliVersion']=p.ALPHA
            db.execute("UPDATE members SET proof=? WHERE id='A'",(json.dumps(parent),))
        self.assertEqual(self.f.finish()['verdict'],'needs_validation')
        self.assertEqual(self.f.permission_status(),'unknown')

    def test_missing_evidence_does_not_use_old_a_proof_either(self):
        self.f.controller.client=None
        with self.f.store.transaction() as db:
            db.execute('DELETE FROM pair_settings WHERE key=?',(p.KEY+self.f.lease['execution_id'],))
        self.assertEqual(self.f.finish()['verdict'],'needs_validation')
        self.assertEqual(self.f.permission_status(),'unknown')

    def test_record_cannot_rebind_another_turn_or_build(self):
        with self.f.store.transaction() as db:db.execute("UPDATE review_stages SET turn_id='other'")
        with self.assertRaises(ValueError):p.record_producer(self.f.store,self.f.stage['id'],self.f.controller.state,self.launch)

    def test_controller_records_before_turn_start_then_binds_returned_turn(self):
        register_cli_fixture(self.f.store,executable=self.file,version=self.launch['version'])
        with self.f.store.transaction() as db:
            db.execute('DELETE FROM pair_settings WHERE key=?',(p.KEY+self.f.lease['execution_id'],))
            db.execute('UPDATE review_stages SET turn_id=NULL')
        controller=self.f.controller
        client=controller.client
        client.read_thread.return_value=self.f.proof
        client.turns.side_effect=self.f.observer().turns
        client.server_requests=queue.Queue();client.progress_events=queue.Queue();client._dead=False
        client.wait_turn.return_value=self.f.notification
        def rpc(method,params):
            self.assertEqual(method,'turn/start')
            record=self.f.store.setting(p.KEY+self.f.lease['execution_id'])
            self.assertIsNone(record['binding']['turn'])
            self.assertFalse(record['finished'])
            pin=self.f.store.setting('cli_execution:'+self.f.lease['execution_id'])
            self.assertEqual(pin['task_id'],self.f.stage['task_id'])
            self.assertEqual(pin['executable'],self.launch['file'])
            self.assertIsNone(pin['turn_id'])
            return {'turn':{'id':'turn-one'}}
        client.rpc.side_effect=rpc
        def finish(*args,**kwargs):
            record=self.f.store.setting(p.KEY+self.f.lease['execution_id'])
            self.assertEqual(record['binding']['turn'],'turn-one')
            self.assertEqual(self.f.store.setting('cli_execution:'+self.f.lease['execution_id'])['turn_id'],'turn-one')
            return {'verdict':'needs_validation'}
        with patch.object(controller,'_parent'),patch.object(controller,'_prompt',return_value='isolated'), \
             patch.object(self.f.store,'acquire_reviewer',return_value=self.f.lease), \
             patch.object(self.f.store,'begin_review'),patch.object(controller,'_finish_final_turn',side_effect=finish), \
             patch.object(runtime,'tree_stamp',return_value={'changes':'','head':'fixed','diff':''}):
            controller._execute(self.f.stage)
        self.assertEqual(client.rpc.call_count,1)
        client.turns.assert_called_once_with(self.f.proof['id'],items_view='full')

    def test_wrong_binding_fields_fail_closed(self):
        expected=self.record['binding']
        for key in ['pair','stage','digest','epoch','session','execution','turn','cwd','home']:
            with self.subTest(key=key),self.assertRaises(ValueError):
                p.producer_context(self.record,dict(expected,**{key:'wrong'}))
        bad=copy.deepcopy(self.record);bad['launch']['version']='future'
        with self.assertRaises(ValueError):p.producer_context(bad,expected)

    def test_unfinished_evidence_cannot_be_used_after_controller_loss(self):
        with self.assertRaises(ValueError):p.producer_context(self.record,self.record['binding'],live=False)

    def test_evidence_change_during_read_rejects(self):
        def mutate():
            changed=copy.deepcopy(self.record);changed['launch']['process']['created']=999
            self.f.store.setting(p.KEY+self.f.lease['execution_id'],changed)
        self.f.mutate=mutate
        with self.assertRaises(ValueError):self.f.finish()
        self.assertEqual(self.f.permission_status(),'granted')

    def test_nonzero_exact_receipt_is_failed_not_success(self):
        self.f.item['exit_code']=2;self.f.full['items'][0]['exit_code']=2;self.f.rollout([self.f.item])
        self.assertEqual(self.f.finish()['verdict'],'needs_validation')
        self.assertEqual(self.f.permission_status(),'failed')

    def test_other_item_or_turn_never_settles_permission(self):
        self.f.full['items'][0]['id']='wrong-item'
        self.assertEqual(self.f.finish()['verdict'],'needs_validation')
        self.assertEqual(self.f.permission_status(),'unknown')

    def test_version_check_fixes_file_before_and_after(self):
        result=Mock(returncode=0,stdout='codex-cli 0.163.0-alpha.11\n')
        with patch.object(p.subprocess,'run',return_value=result) as run:
            launch=p.prepare_launch([str(self.file)])
        self.assertEqual(launch['version'],p.ALPHA)
        self.assertEqual(run.call_args.args[0],[p.file_identity(self.file)['path'],'--version'])
        with self.assertRaises(ValueError):p.prepare_launch(['node','codex.js'])

    def test_version_query_file_mutation_rejected(self):
        def query(*args,**kwargs):
            self.file.write_bytes(b'updated during version query')
            return Mock(returncode=0,stdout='codex-cli 0.163.0-alpha.11\n')
        with patch.object(p.subprocess,'run',side_effect=query),self.assertRaises(ValueError):
            p.prepare_launch([str(self.file)])

    def test_attach_checks_owned_process_and_file(self):
        proc=Mock(pid=123);proc.poll.return_value=None
        self.assertEqual(p.attach_process({k:self.launch[k] for k in ('file','version')},proc),self.launch)
        proc.poll.return_value=1
        with self.assertRaises(ValueError):p.attach_process(self.launch,proc)

    def test_failed_status_zero_code_rejected(self):
        self.f.full['items'][0]['status']='failed'
        self.assertEqual(self.f.finish()['verdict'],'needs_validation')
        self.assertEqual(self.f.permission_status(),'unknown')


class ReconcilePairingTests(unittest.TestCase):
    """C-STRICT-RECONCILE-RAW-ONLY，首次返修；实际Store及对账入口。"""
    def setUp(self):
        case=ProducerLifecycleTests();case.setUp();self.addCleanup(case.doCleanups)
        self.case=case;self.f=case.f
        p.record_producer(self.f.store,self.f.stage['id'],self.f.controller.state,case.launch,finished=True)
        self.api=copy.deepcopy(self.f.full['items'][0]);self.raw=copy.deepcopy(self.f.item)

    def reconcile(self,api,raw,expected):
        f=self.f
        f.full['items']=api+[f.message];f.rollout(raw)
        f.store.release_reviewer(f.ticket['epoch'],f.proof['id'],f.lease['owner'],outcome='unknown',evidence='isolated lost receipt')
        with patch.object(runtime,'AppServer',side_effect=f.observer):
            result=runtime.reconcile_review(f.store,*f.actor(),f.stage['id'],'C-STRICT-RECONCILE-RAW-ONLY r1 isolated')
        self.assertEqual(f.permission_status(),expected)
        self.assertFalse(result['work_approved']);self.assertFalse(result['replayed'])
        with f.store.transaction() as db:
            self.assertEqual(db.execute('SELECT status FROM review_stages WHERE id=?',(f.stage['id'],)).fetchone()[0],'needs_validation')

    def test_raw_only_stays_unknown(self):self.reconcile([],[self.raw],'unknown')
    def test_api_only_stays_unknown(self):self.reconcile([self.api],[],'unknown')
    def test_raw_duplicates_stay_unknown(self):self.reconcile([],[self.raw,self.raw],'unknown')
    def test_api_duplicates_stay_unknown(self):self.reconcile([self.api,self.api],[],'unknown')
    def test_complete_pair_settles_success(self):self.reconcile([self.api],[self.raw],'success')
    def test_complete_pair_with_duplicates_settles_success(self):self.reconcile([self.api,self.api],[self.raw,self.raw],'success')
    def test_nonzero_pair_settles_failed(self):self.reconcile([dict(self.api,exit_code=2)],[dict(self.raw,exit_code=2)],'failed')
    def test_conflict_cannot_resurrect(self):self.reconcile([self.api],[dict(self.raw,exit_code=2),self.raw],'unknown')
    def test_api_conflict_cannot_resurrect(self):self.reconcile([self.api,dict(self.api,exit_code=2),self.api],[self.raw],'unknown')
    def test_unknown_source_stays_unknown(self):self.reconcile([dict(self.api,source='unknown')],[self.raw],'unknown')
    def test_wrong_type_cannot_resurrect(self):self.reconcile([self.api,dict(self.api,type='fileChange'),self.api],[self.raw],'unknown')
    def test_unknown_raw_source_stays_unknown(self):self.reconcile([self.api],[dict(self.raw,source='unknown')],'unknown')
    def test_wrong_cwd_stays_unknown(self):self.reconcile([dict(self.api,cwd=str(self.f.home))],[self.raw],'unknown')
    def test_wrong_status_stays_unknown(self):self.reconcile([dict(self.api,status='failed')],[self.raw],'unknown')
    def test_wrong_argv_stays_unknown(self):self.reconcile([self.api],[dict(self.raw,command=['other'])],'unknown')
    def test_api_containing_both_shapes_is_not_two_sources(self):self.reconcile([self.api,self.raw],[],'unknown')
    def test_rollout_containing_both_shapes_is_not_two_sources(self):self.reconcile([],[self.api,self.raw],'unknown')
    def test_swapped_sources_stay_unknown(self):self.reconcile([self.raw],[self.api],'unknown')

    def test_direct_settlement_cannot_bypass_pairing(self):
        f=self.f
        for api,raw in [([], [self.raw]),([self.api],[]),([self.api,self.raw],[])]:
            with self.subTest(api=api,raw=raw):
                runtime.settle_permissions(f.store,f.ticket['epoch'],f.lease['execution_id'],dict(f.full,items=api,rollout_items=raw))
                self.assertEqual(f.permission_status(),'unknown')
        runtime.settle_permissions(f.store,f.ticket['epoch'],f.lease['execution_id'],dict(f.full,items=[self.api],rollout_items=[self.raw]))
        self.assertEqual(f.permission_status(),'success')

    def test_live_before_permission_uses_same_pairing(self):
        f=self.f;controller=f.controller
        request={'params':{'threadId':f.proof['id'],'turnId':'turn-one'}}
        controller.client=f.observer()
        f.full['status']='inProgress';f.full['items']=[f.message]
        controller._settle_before_permission(request)
        self.assertEqual(f.permission_status(),'unknown')
        f.full['items']=[self.api,f.message]
        controller._settle_before_permission(request)
        self.assertEqual(f.permission_status(),'success')

    def test_final_raw_only_remains_unknown(self):
        self.f.full['items']=[self.f.message]
        self.assertEqual(self.f.finish()['verdict'],'needs_validation')
        self.assertEqual(self.f.permission_status(),'unknown')


class FinalProducerGateTests(unittest.TestCase):
    """原链round=2；不依靠managed_permissions存在来阻止阶段通过。"""
    def fixture(self):
        case=ProducerLifecycleTests();case.setUp();self.addCleanup(case.doCleanups)
        f=case.f
        p.record_producer(f.store,f.stage['id'],f.controller.state,case.launch,finished=True)
        f.controller.client=None
        with f.store.transaction() as db:db.execute('DELETE FROM managed_permissions')
        return case

    def status(self,f):
        with f.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM managed_permissions').fetchone()[0],0)
            return db.execute('SELECT status FROM review_stages WHERE id=?',(f.stage['id'],)).fetchone()[0]

    def finish_direct(self,f,**kwargs):
        return f.store.finish_review(f.stage['id'],f.ticket['epoch'],f.proof['id'],f.proof,
            f.lease['owner'],f.lease['execution_id'],json.loads(f.raw),files_unchanged=True,**kwargs)

    def test_missing_producer_without_permissions_needs_validation(self):
        case=self.fixture();f=case.f
        with f.store.transaction() as db:db.execute('DELETE FROM pair_settings WHERE key=?',(p.KEY+f.lease['execution_id'],))
        result=f.finish()
        self.assertEqual(result['verdict'],'needs_validation');self.assertEqual(self.status(f),'needs_validation')

    def test_complete_producer_without_permissions_can_pass(self):
        f=self.fixture().f
        self.assertEqual(f.finish()['verdict'],'passed');self.assertEqual(self.status(f),'passed')

    def test_explicit_legacy_producer_without_permissions_can_pass(self):
        f=self.fixture().f;key=p.KEY+f.lease['execution_id']
        record=f.store.setting(key);record['launch']['version']='0.163.0';f.store.setting(key,record)
        self.assertEqual(f.finish()['verdict'],'passed');self.assertEqual(self.status(f),'passed')

    def test_each_missing_wrong_or_incomplete_evidence_rejects_without_permissions(self):
        variants=[('finished',False),('launch',{}),('binding',{}),('launch',None),('finished',1),('record',None),
                  ('version','future'),('turn',None),('epoch',True)]
        variants += [(key,'wrong') for key in ['pair','stage','digest','session','execution','cwd','home']]
        variants += [('missing:'+key,None) for key in ['pair','stage','digest','epoch','session','execution','turn','cwd','home']]
        for key,value in variants:
            case=self.fixture();f=case.f;name=p.KEY+f.lease['execution_id']
            try:
                record=f.store.setting(name)
                if key=='record':record=value
                elif key.startswith('missing:'):record['binding'].pop(key.split(':')[1])
                elif key in ('finished','launch','binding'):record[key]=value
                elif key=='version':record['launch']['version']=value
                else:record['binding'][key]=value
                with f.store.transaction() as db:f.store._put(db,name,record)
                with self.subTest(key=key,value=value):
                    self.assertEqual(f.finish()['verdict'],'needs_validation')
                    self.assertEqual(self.status(f),'needs_validation')
            finally:case.doCleanups()

    def test_stage_write_without_controller_or_snapshot_still_requires_producer(self):
        f=self.fixture().f
        with f.store.transaction() as db:db.execute('DELETE FROM pair_settings WHERE key=?',(p.KEY+f.lease['execution_id'],))
        self.assertEqual(self.finish_direct(f)['verdict'],'needs_validation')
        self.assertEqual(self.status(f),'needs_validation')

    def test_stage_write_unfinished_producer_rejected_even_with_snapshot(self):
        f=self.fixture().f;key=p.KEY+f.lease['execution_id']
        record=f.store.setting(key);record['finished']=False;f.store.setting(key,record)
        snapshot=runtime.final_review_snapshot(f.store,f.stage,f.controller.state)
        with f.store.transaction() as db:self.assertIsNone(runtime.validate_final_snapshot(f.store,db,snapshot))
        self.assertEqual(self.finish_direct(f,receipt_snapshot=snapshot)['verdict'],'needs_validation')
        self.assertEqual(self.status(f),'needs_validation')

    def test_valid_evidence_direct_stage_write_can_pass(self):
        f=self.fixture().f
        self.assertEqual(self.finish_direct(f)['verdict'],'passed');self.assertEqual(self.status(f),'passed')

    def test_final_transaction_rechecks_actual_file_not_only_record(self):
        case=self.fixture();f=case.f;original=f.store.finish_review
        def changed(*args,**kwargs):
            case.file.write_bytes(b'changed after report')
            return original(*args,**kwargs)
        with patch.object(f.store,'finish_review',side_effect=changed):
            self.assertEqual(f.finish()['verdict'],'needs_validation')
        self.assertEqual(self.status(f),'needs_validation')

    def test_missing_start_record_cannot_be_backfilled_at_finish(self):
        case=self.fixture();f=case.f
        f.controller.client=Mock(producer_launch=case.launch)
        with f.store.transaction() as db:db.execute('DELETE FROM pair_settings WHERE key=?',(p.KEY+f.lease['execution_id'],))
        self.assertEqual(f.finish()['verdict'],'needs_validation')
        self.assertIsNone(f.store.setting(p.KEY+f.lease['execution_id']))
        self.assertEqual(self.status(f),'needs_validation')

    def test_no_permissions_does_not_bypass_alpha_pairing(self):
        f=self.fixture().f;f.full['items']=[f.message]
        self.assertEqual(f.finish()['verdict'],'needs_validation');self.assertEqual(self.status(f),'needs_validation')

    def test_incomplete_start_binding_cannot_be_completed_at_finish(self):
        case=self.fixture();f=case.f;key=p.KEY+f.lease['execution_id']
        record=f.store.setting(key);record['binding']['turn']=None;record['finished']=False
        f.store.setting(key,record);f.controller.client=Mock(producer_launch=case.launch)
        self.assertEqual(f.finish()['verdict'],'needs_validation')
        self.assertIsNone(f.store.setting(key)['binding']['turn'])
        self.assertEqual(self.status(f),'needs_validation')
