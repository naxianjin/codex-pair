"""交接与额度调度的行为回归；不调用真实账号或重跑历史业务。"""
import json
import sqlite3
import time
import unittest
from unittest.mock import patch

from scripts import pair_balance
from scripts.pair_core import Daemon, PairError, Store, SCHEMA, MIGRATION_2
from scripts.pair_transport import AppServer, TransportError
from tests import test_core
from tests.test_core import FakeTransport


def quota(remaining, *, pool='one', duration=300, now=None, secondary=None):
    now=time.time() if now is None else now
    raw={'rateLimits':{'limitId':'codex','primary':{'usedPercent':100-remaining,'windowDurationMins':duration,'resetsAt':now+600}}}
    if secondary is not None:raw['rateLimits']['secondary']={'usedPercent':100-secondary,'windowDurationMins':10080,'resetsAt':now+1200}
    return pair_balance.snapshot(raw,account=pool,now=now)


class HandoffTests(unittest.TestCase):
    setUp=test_core.CoreTests.setUp
    tearDown=test_core.CoreTests.tearDown

    def finish(self, task_id, *, result='具体交付与证据引用', partial=False, commits=()):
        task=self.store.task(task_id);member=task['assignee']
        message=next(m for m in self.store.status()['messages'] if m['task_id']==task_id and m['kind']=='task')
        claim=self.store.claim(member,self.sessions[member],self.directories[member],message_id=message['id'])
        receipt=self.store.complete(member,self.sessions[member],self.directories[member],message['id'],claim['claim_token'],result,partial=partial,commits=commits)
        return message,claim,receipt

    def acknowledge(self, member, delivery_id):
        offset=0
        while True:
            page=self.store.read_delivery(member,self.sessions[member],self.directories[member],delivery_id,offset=offset,limit=2000)
            if page['next_offset'] is None:break
            offset=page['next_offset']
        return self.store.ack_delivery(member,self.sessions[member],self.directories[member],delivery_id,page['digest'])

    def test_a_to_a_notification_is_idempotent_and_not_recursive(self):
        self.store.assign('self','自行实现','A',['src/'])
        message,claim,_=self.finish('self',result='交付内容'*10000)
        result=next(m for m in self.store.status()['messages'] if m['kind']=='result')
        self.assertEqual(result['recipient'],'A')
        self.assertEqual(self.store.handoff('self','A')['id'],result['id'])
        self.assertTrue(self.store.complete('A',self.sessions['A'],self.directories['A'],message['id'],claim['claim_token'],'交付内容'*10000)['duplicate'])
        self.assertLess(len(self.store.message(result['id'])['body']),2000)
        received=self.store.claim('A',self.sessions['A'],self.directories['A'],message_id=result['id'])
        with self.assertRaisesRegex(PairError,'acknowledgement'):
            self.store.complete('A',self.sessions['A'],self.directories['A'],result['id'],received['claim_token'],'已阅读')
        self.acknowledge('A',self.store.message(result['id'])['delivery_id'])
        self.store.complete('A',self.sessions['A'],self.directories['A'],result['id'],received['claim_token'],'已阅读')
        self.assertEqual(len(self.store.status()['messages']),2)
        self.assertFalse(self.store.claim('A',self.sessions['A'],self.directories['A'],message_id=message['id'])['claimed'])

    def test_report_to_separate_from_owner_and_self_task_sender(self):
        self.store.assign('one','后端','A',['src/'],report_to='B')
        self.finish('one')
        result=next(m for m in self.store.status()['messages'] if m['kind']=='result')
        self.assertEqual((result['sender'],result['recipient']),('A','B'))
        self.assertEqual(self.store.task('one')['assignee'],'A')

    def test_interrupted_result_recovers_original_token_after_task_completed(self):
        self.store.assign('one','后端','B',['src/'])
        self.finish('one')
        result=next(m for m in self.store.status()['messages'] if m['kind']=='result')
        claim=self.store.claim('A',self.sessions['A'],self.directories['A'],message_id=result['id'])
        with self.store.transaction() as db:db.execute("UPDATE messages SET remote_status='interrupted',delivery_status='accepted' WHERE id=?",(result['id'],))
        recovered=self.store.recover_claim('A',self.sessions['A'],self.directories['A'],result['id'])
        self.assertTrue(recovered['recovered'])
        self.assertEqual(claim['claim_token'],recovered['claim_token'])
        self.assertEqual(self.store.task('one')['status'],'completed')
        with self.store.transaction() as db:db.execute("UPDATE messages SET delivery_status='unknown' WHERE id=?",(result['id'],))
        self.assertEqual(self.store.handoff('one','A')['id'],result['id'])
        self.assertFalse(self.store.recover_claim('A',self.sessions['A'],self.directories['A'],result['id'])['recovered'])

    def test_dependency_read_ack_and_both_directions(self):
        for first,second in [('A','B'),('B','A')]:
            with self.subTest(first=first):
                name='first'+first;following='second'+first
                self.store.assign(name,'前置',first,[name+'.py'])
                self.store.assign(following,'后续',second,[following+'.py'],dependencies=[name])
                self.assertEqual(self.store.task(following)['status'],'pending')
                self.finish(name)
                self.assertEqual(self.store.task(following)['status'],'active')
                msg=next(m for m in self.store.status()['messages'] if m['task_id']==following and m['kind']=='task')
                claim=self.store.claim(second,self.sessions[second],self.directories[second],message_id=msg['id'])
                with self.assertRaisesRegex(PairError,'acknowledgement'):
                    self.store.complete(second,self.sessions[second],self.directories[second],msg['id'],claim['claim_token'],'继续完成')
                self.acknowledge(second,'delivery:'+name)
                self.store.complete(second,self.sessions[second],self.directories[second],msg['id'],claim['claim_token'],'继续完成')

    def test_skipping_pages_cannot_acknowledge(self):
        self.store.assign('one','后端','B',['src/'])
        self.finish('one',result='X'*6001)
        page=self.store.read_delivery('A',self.sessions['A'],self.directories['A'],'delivery:one',offset=6000)
        with self.assertRaisesRegex(PairError,'fully read'):
            self.store.ack_delivery('A',self.sessions['A'],self.directories['A'],'delivery:one',page['digest'])
        self.acknowledge('A','delivery:one')
        with self.assertRaisesRegex(PairError,'version'):
            self.store.ack_delivery('A',self.sessions['A'],self.directories['A'],'delivery:one','wrong')

    def test_partial_does_not_release_dependency(self):
        self.store.assign('one','未完成','A',['src/'])
        self.store.assign('two','依赖','B',['other/'],dependencies=['one'])
        self.finish('one',partial=True)
        self.assertEqual(self.store.task('one')['status'],'handed_back')
        self.assertEqual(self.store.task('two')['status'],'pending')
        with self.assertRaises(PairError):self.store.handoff('one','B')

    def test_self_result_waits_until_original_remote_turn_ends(self):
        self.store.assign('one','后端','A',['src/'])
        fake=FakeTransport(self.store);daemon=Daemon(self.store,fake)
        self.assertTrue(daemon.dispatch_one())
        self.finish('one')
        self.assertFalse(daemon.dispatch_one())
        fake.observation={'turn_id':'turn','remote_status':'completed'}
        daemon.reconcile()
        self.assertTrue(daemon.dispatch_one())
        self.assertEqual(len([m for m in self.store.status()['messages'] if m['kind']=='task']),1)

    def test_usage_is_turn_scoped_and_replaces_estimate(self):
        self.store.assign('one','开发','A',['src/'],effort=5)
        with self.store.transaction() as db:db.execute("UPDATE messages SET turn_id='turn' WHERE task_id='one'")
        params={'threadId':self.sessions['A'],'turnId':'turn','tokenUsage':{'last':{'totalTokens':700}}}
        self.assertTrue(self.store.record_usage('A',params));self.assertTrue(self.store.record_usage('A',params))
        self.assertEqual(self.store.status()['balance']['usage_turns'],1)
        params['turnId']='unrelated'
        self.assertFalse(self.store.record_usage('A',params))

    def test_auto_only_pending_tasks_and_effort_defaults(self):
        other=self.root/'other-home';other.mkdir()
        with self.store.transaction() as db:db.execute("UPDATE members SET codex_home=? WHERE id='B'",(str(other),))
        self.store.save_quota('A',quota(70,pool='a'));self.store.save_quota('B',quota(50,pool='b'))
        task=self.store.assign('one','复杂','auto',['src/'],effort=5)
        self.assertEqual(task['assignee'],'A')
        self.assertEqual(task['assignment_reason'],'quota_gap_at_least_20pp')
        self.store.save_quota('A',quota(0,pool='a'));self.store.save_quota('B',quota(90,pool='b'))
        self.store.schedule()
        self.assertEqual(self.store.task('one')['assignee'],'A')
        task=self.store.assign('two','固定','B',['other/'])
        self.assertEqual(task['effort'],3)
        self.assertEqual(task['assignment_mode'],'fixed')

    def test_same_home_is_one_resource_pool(self):
        self.store.save_quota('A',quota(70,pool='a'));self.store.save_quota('B',quota(0,pool='b'))
        data=self.store.quota()
        self.assertEqual(data['A']['pool'],data['B']['pool'])
        self.assertEqual(data['A']['remaining'],0)
        self.assertEqual(data['B']['remaining'],0)

    def test_v4_migration_preserves_claim_token_and_sender_reporting(self):
        directory=self.home/'codex-pair'/'legacy';directory.mkdir(parents=True)
        stub=Store('legacy',home=self.home,create=True)
        db=sqlite3.connect(directory/'state.sqlite3')
        db.executescript(SCHEMA);db.executescript(MIGRATION_2)
        stub._upgrade_accounts(db);stub._upgrade_windows(db)
        db.execute('INSERT INTO collaborations VALUES(?,?,?,?,?,?)',('legacy',str(self.project),'a'*40,'paused',0,0))
        for member in ('A','B'):
            source=self.store.member(member)
            db.execute('INSERT INTO members VALUES(?,?,?,?,?,?,?,?,?)',(member,source['session_uuid'],source['cwd'],'queue',0,source['proof'],0,source['codex_home'],'auto'))
        db.execute("INSERT INTO tasks(id,title,body,assignee,allowed_paths,status,result,commits_json,created,updated) VALUES('old','旧任务','正文','A','[]','completed','旧交付','[]',0,0)")
        db.execute("INSERT INTO messages(id,sender,recipient,task_id,kind,body,dedupe_key,fingerprint,process_status,claim_token,created,updated) VALUES('task','B','A','old','task','正文','task','fp','completed','task-token',0,0)")
        db.execute("INSERT INTO messages(id,sender,recipient,task_id,kind,body,dedupe_key,fingerprint,process_status,claim_token,delivery_status,remote_status,created,updated) VALUES('result','A','B','old','result','旧交付','result','fp','claimed','original-token','accepted','interrupted',0,0)")
        db.commit();db.close()
        legacy=Store('legacy',home=self.home)
        self.assertEqual(legacy.task('old')['report_to'],'B')
        self.assertEqual(legacy.task('old')['effort'],3)
        self.assertEqual(legacy.message('result')['claim_token'],'original-token')
        self.assertEqual(legacy.setting('scheduling_policy'),'legacy')
        self.assertTrue(legacy.recover_claim('B',self.sessions['B'],self.directories['B'],'result')['recovered'])
        self.assertEqual(len(list(directory.glob('schema-v4-*.backup.sqlite3'))),1)

    def test_quota_failure_falls_back_without_changing_account(self):
        from scripts.pair_quota import refresh
        with patch('scripts.pair_quota.AppServer',side_effect=TransportError('timeout')) as factory:
            refresh(self.store,max_age=0)
        self.assertEqual(factory.call_count,2)
        self.assertEqual(self.store.quota()['A']['reliability'],'unavailable')
        self.assertEqual(self.store.assign('one','轻任务','auto',['src/'])['status'],'active')

    def test_refresh_timeout_preserves_exhaustion_through_complex_wait(self):
        from scripts.pair_quota import refresh
        other=self.root/'other-home';other.mkdir()
        with self.store.transaction() as db:db.execute("UPDATE members SET codex_home=? WHERE id='B'",(str(other),))
        now=time.time()
        self.store.save_quota('A',quota(0,pool='a',now=now-40))
        self.store.save_quota('B',quota(80,pool='b',now=now-40))
        with patch('scripts.pair_quota.AppServer',side_effect=TransportError('timeout')):
            refresh(self.store,max_age=0)
        data=self.store.quota()['A']
        self.assertEqual(data['reliability'],'fresh')
        self.assertEqual(data['refresh_status'],'unavailable')
        self.assertEqual(data['observed_at'],now-40)
        task=self.store.assign('timeout','复杂','auto',['probe.py'],effort=5)
        self.assertEqual(task['status'],'pending')
        with patch('scripts.pair_core.time.time',return_value=now+6):self.store.schedule()
        task=self.store.task('timeout')
        self.assertEqual(task['status'],'active')
        self.assertEqual(task['assignee'],'B')

    def test_complex_wait_timeout_alone_keeps_exhausted_members_pending(self):
        now=time.time()
        for member in ('A','B'):self.store.save_quota(member,quota(0,now=now-40))
        task=self.store.assign('exhausted','复杂','auto',['probe.py'],effort=8)
        self.assertEqual(task['assignment_reason'],'quota_refresh_pending')
        with patch('scripts.pair_core.time.time',return_value=now+6):self.store.schedule()
        task=self.store.task('exhausted')
        self.assertEqual(task['status'],'pending')
        self.assertEqual(task['assignment_reason'],'quota_exhausted')

    def test_monitor_timeout_preserves_snapshot_until_expiry_or_reset(self):
        from scripts.pair_quota import QuotaMonitor
        now=time.time()
        original=quota(0,now=now-40)
        self.store.save_quota('A',original)
        monitor=QuotaMonitor(self.store,member_id='A')
        with patch('scripts.pair_quota.AppServer',side_effect=TransportError('timeout')):
            with patch.object(monitor.stop_event,'wait',side_effect=lambda _:monitor.stop_event.set()):monitor.run()
        data=self.store.quota()['A']
        self.assertEqual(data['reliability'],'fresh')
        self.assertEqual(data['remaining'],0)
        self.assertEqual(data['observed_at'],original['observed_at'])
        self.assertEqual(pair_balance.current(data,now+81)['reliability'],'stale')
        reset=dict(data,windows=[dict(original['windows'][0],resets_at=now+1)])
        self.assertEqual(pair_balance.current(reset,now+2)['reliability'],'reset_pending')

    def close_result(self, task_id):
        message=next(m for m in self.store.status()['messages'] if m['kind']=='result' and m['task_id']==task_id)
        member=message['recipient']
        claim=self.store.claim(member,self.sessions[member],self.directories[member],message_id=message['id'])
        self.acknowledge(member,'delivery:'+task_id)
        self.store.complete(member,self.sessions[member],self.directories[member],message['id'],claim['claim_token'],'已读取确认并收尾')

    def test_stage_registers_one_separate_planning_task_after_result_closes(self):
        self.store.setting('window_flow',{'goal':'尚未宣告结束'})
        self.store.assign('one','一阶段','A',['src/'])
        self.finish('one')
        self.assertEqual(len(self.store.status()['tasks']),1)
        self.close_result('one')
        self.store.schedule();self.store.schedule()
        plans=[t for t in self.store.status()['tasks'] if t['id'].startswith('pair-plan-')]
        self.assertEqual(len(plans),1)
        self.assertEqual(plans[0]['status'],'active')
        self.assertEqual(json.loads(plans[0]['allowed_paths']),[])
        msg=next(m for m in self.store.status()['messages'] if m['task_id']==plans[0]['id'])
        self.assertEqual(msg['kind'],'task')
        claim=self.store.claim('A',self.sessions['A'],self.directories['A'],message_id=msg['id'])
        with self.assertRaisesRegex(PairError,'Register next work'):
            self.store.complete('A',self.sessions['A'],self.directories['A'],msg['id'],claim['claim_token'],'只读完了')
        self.store.assign('two','已授权后续','B',['other/'],dependencies=['one'])
        self.store.complete('A',self.sessions['A'],self.directories['A'],msg['id'],claim['claim_token'],'已登记后续任务')
        self.assertEqual(self.store.task('two')['status'],'active')
        self.assertFalse(any(m['kind']=='result' and m['task_id']==plans[0]['id'] for m in self.store.status()['messages']))

    def test_planning_delivery_waits_for_result_model_turn(self):
        self.store.setting('window_flow',{'goal':'继续原目标'})
        self.store.assign('one','一阶段','A',['src/'])
        self.finish('one');self.close_result('one')
        result=next(m for m in self.store.status()['messages'] if m['kind']=='result')
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='inProgress',turn_id='result-turn' WHERE id=?",(result['id'],))
        fake=FakeTransport(self.store);daemon=Daemon(self.store,fake)
        self.assertFalse(daemon.dispatch_one())
        fake.observation={'turn_id':'result-turn','remote_status':'completed'}
        daemon.reconcile()
        self.assertTrue(daemon.dispatch_one())
        self.assertEqual(fake.calls[0][1]['kind'],'task')
        self.assertTrue(fake.calls[0][1]['task_id'].startswith('pair-plan-'))

    def test_goal_complete_stops_continuation_without_result_loop(self):
        self.store.setting('window_flow',{'goal':'只需一阶段'})
        self.store.assign('one','一阶段','A',['src/'])
        self.finish('one');self.close_result('one')
        plan=next(t for t in self.store.status()['tasks'] if t['id'].startswith('pair-plan-'))
        message=next(m for m in self.store.status()['messages'] if m['task_id']==plan['id'])
        claim=self.store.claim('A',self.sessions['A'],self.directories['A'],message_id=message['id'])
        self.store.complete('A',self.sessions['A'],self.directories['A'],message['id'],claim['claim_token'],'原目标验收已完成',goal_complete=True)
        self.store.schedule();self.store.schedule()
        self.assertTrue(self.store.setting('goal_completed'))
        self.assertEqual(len(self.store.status()['tasks']),2)
        self.assertEqual(len([m for m in self.store.status()['messages'] if m['kind']=='result']),1)

    def test_shared_workspace_auto_continuation_keeps_work_serial(self):
        with self.store.transaction() as db:
            for member in ('A','B'):
                proof=json.loads(db.execute('SELECT proof FROM members WHERE id=?',(member,)).fetchone()[0])
                proof['git']={'path':str(self.project)}
                db.execute('UPDATE members SET proof=? WHERE id=?',(json.dumps(proof),member))
        self.store.setting('window_flow',{'goal':'阶段完成后由另一窗口继续'})
        self.store.assign('one','第一阶段','B',['one.py'])
        self.finish('one');self.close_result('one')
        plan=next(t for t in self.store.status()['tasks'] if t['id'].startswith('pair-plan-'))
        message=next(m for m in self.store.status()['messages'] if m['task_id']==plan['id'])
        claim=self.store.claim('A',self.sessions['A'],self.directories['A'],message_id=message['id'])
        self.store.assign('two','第二阶段','B',['two.py'],dependencies=['one'])
        self.assertEqual(self.store.task('two')['status'],'pending')
        self.store.complete('A',self.sessions['A'],self.directories['A'],message['id'],claim['claim_token'],'已登记第二阶段')
        self.assertEqual(self.store.task('two')['status'],'active')

    def test_planner_reads_other_windows_delivery_before_goal_completion(self):
        self.store.setting('window_flow',{'goal':'核验双方交付'})
        self.store.assign('one','B自行交付','B',['one.py'],report_to='B')
        self.finish('one');self.close_result('one')
        plan=next(t for t in self.store.status()['tasks'] if t['id'].startswith('pair-plan-'))
        message=next(m for m in self.store.status()['messages'] if m['task_id']==plan['id'])
        claim=self.store.claim('A',self.sessions['A'],self.directories['A'],message_id=message['id'])
        with self.assertRaisesRegex(PairError,'acknowledgement'):
            self.store.complete('A',self.sessions['A'],self.directories['A'],message['id'],claim['claim_token'],'还没读交付',goal_complete=True)
        self.assertFalse(self.store.setting('goal_completed'))
        self.acknowledge('A','delivery:one')
        self.store.complete('A',self.sessions['A'],self.directories['A'],message['id'],claim['claim_token'],'已核验原目标全部交付',goal_complete=True)
        self.assertTrue(self.store.status()['window_flow']['goal_completed'])

    def test_planning_waits_for_integration_and_pause(self):
        self.store.setting('window_flow',{'goal':'后续验证'})
        self.store.assign('one','提交','A',['src/'])
        self.finish('one',commits=['b'*40]);self.close_result('one')
        self.assertEqual(len(self.store.status()['tasks']),1)
        self.store.control('paused')
        with self.store.transaction() as db:db.execute("UPDATE tasks SET status='integrated' WHERE id='one'")
        self.store.schedule()
        self.assertEqual(len(self.store.status()['tasks']),1)
        self.store.control('running')
        self.store.schedule()
        self.assertEqual(len(self.store.status()['tasks']),2)

    def test_ordinary_task_cannot_close_goal_or_use_planner_prefix(self):
        with self.assertRaisesRegex(PairError,'Reserved task prefix'):
            self.store.assign('pair-plan-user','伪造规划','A',['src/'])
        self.store.assign('one','业务','A',['src/'])
        message=next(m for m in self.store.status()['messages'] if m['task_id']=='one')
        claim=self.store.claim('A',self.sessions['A'],self.directories['A'],message_id=message['id'])
        with self.assertRaisesRegex(PairError,'Planning task required'):
            self.store.complete('A',self.sessions['A'],self.directories['A'],message['id'],claim['claim_token'],'未验收',goal_complete=True)


class BalanceTests(unittest.TestCase):
    def test_readonly_rpc_never_allows_account_mutations(self):
        client=AppServer.__new__(AppServer)
        client.metadata_only=True;client.account_metadata=True
        for method in ['account/logout','account/login/start','account/rateLimitResetCredit/consume','turn/start']:
            with self.subTest(method=method),self.assertRaisesRegex(TransportError,'Metadata-only'):
                client.rpc(method,{})
    def test_threshold_and_light_work_balance(self):
        q={'A':quota(70,pool='a'),'B':quota(50,pool='b')}
        self.assertEqual(pair_balance.choose(['A','B'],5,q,{'A':20,'B':0})[0],'A')
        q['B']=quota(51,pool='b')
        self.assertEqual(pair_balance.choose(['A','B'],5,q,{'A':20,'B':0})[0],'B')
        q['B']=quota(50,pool='b')
        self.assertEqual(pair_balance.choose(['A','B'],3,q,{'A':20,'B':0})[0],'B')

    def test_lowest_window_stale_reset_and_missing(self):
        data=quota(70,secondary=15,now=1000)
        self.assertEqual(data['remaining'],15)
        self.assertEqual(pair_balance.current(data,1121)['reliability'],'stale')
        data=quota(70,now=1000)
        data['windows'][0]['resets_at']=1010
        self.assertEqual(pair_balance.current(data,1011)['reliability'],'reset_pending')
        self.assertEqual(pair_balance.snapshot({})['reliability'],'missing')

    def test_model_buckets_not_mixed(self):
        payload={'rateLimitsByLimitId':{'x':{'limitId':'x','primary':{'usedPercent':20,'windowDurationMins':300,'resetsAt':2000}},
                                         'y':{'limitId':'y','primary':{'usedPercent':95,'windowDurationMins':300,'resetsAt':2000}}}}
        self.assertEqual(pair_balance.snapshot(payload,limit_id='x',now=1000)['remaining'],80)
        self.assertEqual(pair_balance.snapshot(payload,model='unknown',now=1000)['reliability'],'bucket_unknown')

    def test_same_account_uncomparable_and_unknown_are_workload_fallback(self):
        for qb in [quota(10,pool='same'),quota(10,pool='other',duration=60),{'reliability':'missing','remaining':None}]:
            q={'A':quota(90,pool='same'),'B':qb}
            self.assertEqual(pair_balance.choose(['A','B'],8,q,{'A':20,'B':0}),('B','workload_fallback'))

    def test_exhausted_members_keep_pending_work(self):
        q={'A':quota(0,pool='a'),'B':quota(0,pool='b')}
        self.assertIsNone(pair_balance.choose(['A','B'],8,q,{'A':0,'B':0})[0])
        q['A']=pair_balance.current(quota(0,now=1000),2000)
        self.assertEqual(pair_balance.choose(['A','B'],8,q,{'A':0,'B':0})[0],'A')


if __name__=='__main__':unittest.main()
