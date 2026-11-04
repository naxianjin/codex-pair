"""第二批候选行为规格；仅编写，等待用户通知后在批准的隔离环境运行。"""
import json
from pathlib import Path
import subprocess
import time
import unittest
from unittest.mock import patch

from scripts.pair_core import PairError
from scripts.pair_control_events import _MemberObserver
from scripts.pair_control_runtime import ProductionRecoveryAdapter,management_command
from scripts.pair_recovery import digest
from scripts.pair_recovery_graph import components,graph_snapshot
from scripts.pair_autoscale import autoscale_tick
from scripts.pair_hidden_handoff import handle_tool,seal,adopt,task_gate,blocked,reconcile_consumption
from scripts.pair_runtime_apply import ApplicationAdapter,drain_requested
from tests.test_auxiliary_v14 import AuxiliaryTests
from tests.test_recovery_v14 import RecoveryV14Tests
from tests import test_core


class ControlRuntimeTests(unittest.TestCase):
    setUp=test_core.CoreTests.setUp
    tearDown=test_core.CoreTests.tearDown

    def event(self,kind='PreToolUse',tool='one'):
        return {'session_id':self.sessions['B'],'turn_id':'original','tool_use_id':tool,
            'hook_event_name':kind,'tool_name':'Bash','tool_input':{'command':'git status'}}

    def test_direct_user_control_excludes_quoted_material(self):
        user={'type':'event_msg','payload':{'type':'user_message','message':'继续协作'}}
        self.assertEqual(_MemberObserver._control_event(user)['kind'],'direct_user_control')
        user['payload']['quoted']=True
        self.assertEqual(_MemberObserver._control_event(user)['kind'],'non_control')

    def test_local_tool_denied_after_pause_without_marking_previous_finished(self):
        with self.store.transaction() as db:
            self.assertEqual(self.store._control_tool_event(db,'B',self.event()),{})
        self.store.request_pause('A',self.sessions['A'],self.directories['A'],reason='用户暂停',idempotency_key='pause')
        with self.store.transaction() as db:
            result=self.store._control_tool_event(db,'B',self.event(tool='two'))
            self.assertEqual(result['hookSpecificOutput']['permissionDecision'],'deny')
            values=[json.loads(row[0]) for row in db.execute("SELECT value FROM pair_settings WHERE key LIKE 'control_tool:%'")]
            self.assertEqual(sum(value['state']=='admitted' for value in values),1)

    def test_post_tool_does_not_resume_or_grant_permission(self):
        with self.store.transaction() as db:
            self.store._control_tool_event(db,'B',self.event())
            result=self.store._control_tool_event(db,'B',self.event('PostToolUse'))
            self.assertEqual(result,{})
            self.assertEqual(db.execute('SELECT count(*) FROM control_releases').fetchone()[0],0)

    def test_management_command_rejects_shell_composition(self):
        event=self.event();event['tool_input']['command']='python pair.py status; git push'
        self.assertFalse(management_command(self.store,self.store.member('B'),event))

    def test_idle_session_cannot_settle_unbound_recovery_execution(self):
        adapter=ProductionRecoveryAdapter(self.store,None)
        adapter.snapshot={'kind':'reconcile','captured':time.time(),'state_digest':'snapshot','sessions':{
            'A':{'verified':True,'session':self.sessions['A'],'version':1,'idle':True,'local_hook_trust':True}},
            'receipts':['fixture:official-thread'],'executions':{}}
        proof=adapter.verify('reconcile',{'state_digest':'snapshot','session':self.sessions['A'],'binding_version':1,'epoch':1,'execution_id':'missing'})
        self.assertFalse(proof.facts['execution_terminal']);self.assertFalse(proof.facts['execution_observed'])

    def test_visible_local_hooks_cannot_claim_complete_hosted_tool_boundary(self):
        adapter=ProductionRecoveryAdapter(self.store,None)
        adapter.snapshot={'kind':'pause_confirmation','captured':time.time(),'state_digest':'snapshot','sessions':{
            'A':{'verified':True,'idle':True,'local_hook_trust':True}},'receipts':['fixture:official-thread'],'task_owners':{}}
        proof=adapter.verify('pause_confirmation',{'state_digest':'snapshot','control':{'scope':'{"kind":"pair"}'}})
        self.assertFalse(proof.facts['all_scope_executors_stopped']);self.assertTrue(proof.facts['observed_idle'])
        self.assertFalse(proof.facts['all_required_gates_verified'])

    def test_stale_observation_is_not_consumed(self):
        adapter=ProductionRecoveryAdapter(self.store,None)
        adapter.snapshot={'kind':'resume','captured':time.time()-16,'state_digest':'same'}
        with self.assertRaisesRegex(PairError,'stale'):adapter.verify('resume',{'state_digest':'same'})

    def test_runtime_proof_is_bound_to_exact_state(self):
        adapter=ApplicationAdapter('application','old',{},['fixture:copied'])
        with self.assertRaisesRegex(PairError,'changed'):
            adapter.verify('application_prepare',{'application':{'id':'application'},'state_digest':'new'})

    def test_plain_worker_pause_stops_pair_scope(self):
        with self.store.transaction() as db:
            member=self.store._member(db,'B')
            self.store._record_user_control(db,'B','停一下',event_id='fixture:user',source_identity='fixture:rollout',session=member['session_uuid'],binding_version=member['binding_version'])
            scope=json.loads(db.execute('SELECT scope FROM control_requests').fetchone()[0])
            self.assertEqual(scope,{'kind':'pair'})

    def test_control_hook_cleanup_preserves_neighbor(self):
        from scripts.pair_hook_cleanup import cleanup_retired_hook
        path=self.home/'hooks.json';neighbor={'matcher':'*','hooks':[{'type':'command','command':'neighbor','statusMessage':'unrelated'}]}
        path.write_text(json.dumps({'hooks':{'PreToolUse':[neighbor]}}),encoding='utf-8')
        self.store.install_control_hooks('A',self.sessions['A'],self.directories['A'],'B')
        result=cleanup_retired_hook(self.store,self.store.member('B'))
        self.assertEqual(result['removed'],5)
        self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['hooks']['PreToolUse'],[neighbor])

    def test_new_bridge_does_not_obey_old_process_drain(self):
        with self.store.transaction() as db:
            self.store._put(db,'runtime_drain_v14',{'state':'draining','old':{'owner':'old'}})
            db.execute("UPDATE runtime SET owner='new' WHERE id=1")
        self.assertFalse(drain_requested(self.store))


class RecoveryGraphTests(unittest.TestCase):
    setUp=test_core.CoreTests.setUp
    tearDown=test_core.CoreTests.tearDown
    actor=RecoveryV14Tests.actor
    enable=RecoveryV14Tests.enable
    fixture=RecoveryV14Tests.fixture

    def test_components_keep_only_actual_cycles(self):
        self.assertEqual(components([('a','b'),('b','a'),('b','c'),('d','d')]),[['a','b'],['d']])

    def test_registered_relation_requires_actual_blocked_obligation_in_cycle(self):
        self.fixture()
        record=self.store.register_recovery_relation(*self.actor(),'repair',[],['stage-old-one'],'missing-contract','明确修复此阶段缺少的契约')
        with self.store.transaction() as db:
            task=db.execute("SELECT * FROM tasks WHERE id='repair'").fetchone()
            result=self.store._general_recovery_cycle(db,task,{'stable_blocker':record['id'],'remediates':[record['id']]})
            self.assertIn('stage:stage-old-one',result['nodes'])
            self.assertTrue(any(node.startswith('capacity:') for node in result['nodes']))

    def test_repair_cannot_create_capacity_cycle_using_itself(self):
        self.fixture()
        with self.store.transaction() as db:
            db.execute("UPDATE tasks SET status='completed' WHERE id IN ('old-one','old-two')")
            db.execute("UPDATE tasks SET status='needs_validation' WHERE id='repair'")
            db.execute("UPDATE review_stages SET status='passed'")
            graph=graph_snapshot(self.store,db)
            self.assertFalse(any(edge[0]=='task:repair' and edge[1].startswith('capacity:task') for edge in graph['edges']))

    def test_task_replacement_keeps_stable_problem_identity(self):
        self.fixture()
        first=self.store.register_recovery_relation(*self.actor(),'repair',[],['stage-old-one'],'same-cause','原根因')
        self.store.assign('repair-next','接续修复','B',['next.py'],task_kind='repair')
        with self.store.transaction() as db:db.execute("UPDATE tasks SET status='handed_back' WHERE id='repair'")
        result=self.store.register_recovery_relation(*self.actor(),'repair-next',[],['stage-old-one'],'same-cause','保留原根因和累计历史',source_stable_id=first['id'])
        self.assertEqual(first['id'],result['id']);self.assertEqual(result['predecessors'],['repair'])


class PoolAndHandoffTests(unittest.TestCase):
    tearDown=test_core.CoreTests.tearDown
    actor=AuxiliaryTests.actor
    candidate=AuxiliaryTests.candidate
    queue=AuxiliaryTests.queue
    reserve=AuxiliaryTests.reserve
    start_fixture=AuxiliaryTests.start_fixture
    end_fixture=AuxiliaryTests.end_fixture

    def setUp(self):
        AuxiliaryTests.setUp(self)
        subprocess.run(['git','init','-q',str(self.project)],check=True,capture_output=True)

    def requested(self):
        candidate=self.queue();ticket=self.reserve();session=self.start_fixture(ticket)
        with self.store.transaction() as db:epoch=db.execute('SELECT epoch FROM auxiliary_sessions WHERE id=?',(ticket['reviewer_id'],)).fetchone()[0]
        subject={'kind':'auxiliary','id':ticket['reviewer_id'],'session':session,'epoch':epoch}
        record=handle_tool(self.store,subject,{'action':'request','progress':30,'body':'已读固定文件；尚待验证。原贡献、轮次、候选版本全部保留。'},execution=ticket['execution_id'])
        return candidate,ticket,subject,record

    def saved(self):
        candidate,ticket,subject,record=self.requested()
        with self.store.transaction() as db:
            execution=self.store._auxiliary_execution(db,ticket['execution_id'],ticket['lease'])
            self.store._end_auxiliary(db,execution,'suspended',{'fixture_only':True})
            db.execute("UPDATE auxiliary_tasks SET status='waiting' WHERE id='aux'")
        seal(self.store,subject,receipt='fixture:original-official-terminal')
        return candidate,ticket,subject,record

    def accept(self,record):
        offset=0
        while True:
            page=self.store.read_hidden_handoff(*self.actor(),record['id'],offset)
            if page['next_offset'] is None:break
            offset=page['next_offset']
        return self.store.accept_hidden_handoff(*self.actor(),record['id'],page['digest'],'读取后继续原准确任务')

    def test_waiting_task_cannot_create_new_reviewer_before_a_accepts(self):
        self.saved()
        with self.assertRaisesRegex(PairError,'handoff awaiting acceptance'):self.reserve()

    def test_a_cannot_accept_only_last_page(self):
        _,_,_,record=self.saved()
        with self.assertRaisesRegex(PairError,'Sequential'):
            self.store.read_hidden_handoff(*self.actor(),record['id'],offset=999)

    def test_handoff_cannot_release_unknown_execution(self):
        _,_,subject,_=self.requested()
        with self.assertRaisesRegex(PairError,'not idle'):seal(self.store,subject,receipt='fixture:unverified')

    def test_new_context_is_bound_to_exact_accepted_task_and_candidate(self):
        candidate,_,_,record=self.saved();self.accept(record)
        ticket=self.reserve();session=self.start_fixture(ticket)
        with self.store.transaction() as db:epoch=db.execute('SELECT epoch FROM auxiliary_sessions WHERE id=?',(ticket['reviewer_id'],)).fetchone()[0]
        successor={'kind':'auxiliary','id':ticket['reviewer_id'],'session':session,'epoch':epoch}
        self.assertIsNone(adopt(self.store,successor,'allowed-a','other',candidate['id']))
        inherited=adopt(self.store,successor,'allowed-a','aux',candidate['id'])
        self.assertEqual(inherited['id'],record['id'])
        with self.store.transaction() as db:self.assertTrue(blocked(self.store,db,successor))

    def test_unrelated_business_progress_does_not_invalidate_handoff(self):
        _,_,_,record=self.saved()
        self.store.assign('other','无关工作','A',['other.py'])
        self.assertTrue(self.accept(record)['accepted'])

    def test_consumed_preparation_recovers_same_reservation_without_business_replay(self):
        candidate,_,_,record=self.saved();self.accept(record)
        ticket=self.reserve();session=self.start_fixture(ticket)
        with self.store.transaction() as db:epoch=db.execute('SELECT epoch FROM auxiliary_sessions WHERE id=?',(ticket['reviewer_id'],)).fetchone()[0]
        successor={'kind':'auxiliary','id':ticket['reviewer_id'],'session':session,'epoch':epoch}
        adopt(self.store,successor,'allowed-a','aux',candidate['id'],ticket['execution_id'])
        with self.store.transaction() as db:
            value=self.store._option(db,'hidden_handoff:'+record['id'])
            value.update(state='read_confirmed',resume_turn='fixture-preparation-turn',resume_attempt='fixture-preparation')
            self.store._put(db,'hidden_handoff:'+record['id'],value)
            db.execute("UPDATE auxiliary_executions SET state='unknown',turn_id=NULL WHERE id=?",(ticket['execution_id'],))
            db.execute("UPDATE auxiliary_sessions SET state='unknown' WHERE id=?",(ticket['reviewer_id'],))
        class OriginalObserver:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def validate(self,member):return {'id':member['session_uuid']}
            def turns(self,*args,**kwargs):yield {'id':'fixture-preparation-turn','status':'completed','items':[]}
        with patch('scripts.pair_transport.AppServer',return_value=OriginalObserver()):
            result=self.store.reconcile_hidden_handoff(*self.actor(),record['id'],'fixture:exact-cli')
        self.assertTrue(result['preparation_reconciled'])
        with self.store.transaction() as db:
            execution=db.execute('SELECT state,turn_id,charged FROM auxiliary_executions WHERE id=?',(ticket['execution_id'],)).fetchone()
            self.assertEqual(tuple(execution),('reserved',None,1))

    def test_manual_pool_setting_disables_automatic_changes(self):
        self.store.auxiliary_policy(*self.actor(),concurrency=3,reason='明确固定并发')
        result=autoscale_tick(self.store,{'observed_at':time.time(),'available_bytes':1,'cpu_busy_percent':99})
        self.assertFalse(result['changed']);self.assertEqual(result['reason'],'manual_or_disabled')

    def test_resource_pressure_lowers_new_admission_without_releasing_slots(self):
        self.store.auxiliary_policy(*self.actor(),concurrency=3,automatic=True,reason='测试自动收敛')
        candidate=self.candidate()
        tickets=[]
        for index in range(3):self.queue('task-'+str(index),candidate=candidate);tickets.append(self.reserve('task-'+str(index)))
        result=autoscale_tick(self.store,{'observed_at':time.time(),'available_bytes':1,'cpu_busy_percent':99})
        self.assertEqual(result['to'],2)
        self.assertEqual(self.store.auxiliary_status(*self.actor())['active_slots'],3)

    def test_unknown_quota_never_scales_up(self):
        self.queue();self.reserve()
        result=autoscale_tick(self.store,{'observed_at':time.time(),'available_bytes':16*1024**3,'cpu_busy_percent':10})
        self.assertEqual(result['to'],1)

    def test_queue_throughput_and_fresh_quota_allow_one_slot_increase(self):
        candidate=self.candidate()
        with self.store.transaction() as db:
            quota={'observed_at':time.time(),'reliability':'fresh','pool':'unit-pool','limit_id':'unit',
                'remaining':80,'windows':[{'duration_mins':300,'resets_at':time.time()+3600,'remaining':80}]}
            db.execute('INSERT OR REPLACE INTO quota_snapshots VALUES(?,?)',('A',json.dumps(quota)))
            self.store._put(db,'auxiliary_scale',{'observed_at':time.time()-90,'changed_at':time.time()-90})
        for index in range(2):
            key='done-'+str(index);self.queue(key,candidate=candidate);ticket=self.reserve(key)
            self.start_fixture(ticket);self.end_fixture(ticket)
        self.queue('running',candidate=candidate);self.start_fixture(self.reserve('running'))
        self.queue('ready',candidate=candidate)
        result=autoscale_tick(self.store,{'observed_at':time.time(),'available_bytes':16*1024**3,'cpu_busy_percent':10})
        self.assertEqual((result['from'],result['to']),(1,2))

