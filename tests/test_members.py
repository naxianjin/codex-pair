"""多人协作的行为验收，使用临时项目和模拟原窗口，不调用模型。"""
from contextlib import closing
import concurrent.futures
import json
import math
from pathlib import Path
import sqlite3
import threading
import unittest
from unittest.mock import patch

from scripts import pair, pair_windows
from scripts.pair_core import PairError, Store
from tests import test_windows
from tests import test_core
from tests.test_handoff_balance import quota


class ExpansionTests(unittest.TestCase):
    setUp=test_windows.WindowTests.setUp
    tearDown=test_windows.WindowTests.tearDown
    run_cli=test_windows.WindowTests.run_cli
    start=test_windows.WindowTests.start
    joined=test_windows.WindowTests.joined

    def add_window(self,member,index):
        cwd=self.root/member;cwd.mkdir()
        home=self.root/('home-'+member);home.mkdir()
        sid='00000000-0000-0000-0000-'+str(index).zfill(12)
        self.windows[member]={'session_uuid':sid,'cwd':str(cwd).lower(),'codex_home':str(home).lower(),
            'proof':{'id':sid,'cwd':str(cwd).lower(),'modelProvider':'openai','model':'gpt-test'}}

    def invite(self,store):
        from types import SimpleNamespace
        a=store.member('A')
        args=SimpleNamespace(pair=store.pair_id,state_root=str(store.state_root),codex_home=a['codex_home'],
            member='A',session=a['session_uuid'],worktree=a['cwd'])
        from unittest.mock import patch
        with patch('scripts.pair.validate_actor'):
            return pair_windows.invite_member(args,pair)

    def test_expand_without_rebuilding_and_repeat_is_idempotent(self):
        _,_,store=self.joined()
        for index,member in enumerate(('C','D'),3):
            self.add_window(member,index)
            invite=self.invite(store)
            accepted=self.run_cli(member,'accept','--invite',invite['invite_path'])
            self.assertEqual(accepted['role'],member)
            self.assertTrue(accepted['briefing_required'])
            self.assertEqual(self.run_cli(member,'accept','--invite',invite['invite_path'])['role'],member)
            with store.transaction() as db:self.assertFalse(store._member_ready(db,member))
        self.assertEqual(store.member_ids(),['A','B','C','D'])
        with self.assertRaisesRegex(PairError,'Team full'):self.invite(store)

    def test_fifth_window_cannot_consume_earlier_pending_invite(self):
        _,_,store=self.joined()
        extra=self.invite(store)
        for index,member in enumerate(('C','D'),3):
            self.add_window(member,index)
            self.run_cli(member,'accept','--invite',self.invite(store)['invite_path'])
        self.add_window('E',5)
        with self.assertRaisesRegex(PairError,'Team full'):self.run_cli('E','accept','--invite',extra['invite_path'])

    def test_expired_or_revoked_invites_do_not_allocate_seats(self):
        _,_,store=self.joined();self.add_window('C',3)
        invite=self.invite(store)
        data=json.loads(Path(invite['invite_path']).read_text(encoding='utf-8'))
        record=store.setting('invite:'+data['nonce']);record['status']='revoked'
        store.setting('invite:'+data['nonce'],record)
        with self.assertRaisesRegex(PairError,'expired'):self.run_cli('C','accept','--invite',invite['invite_path'])
        self.assertEqual(store.member_ids(),['A','B'])

    def test_concurrent_accept_allocates_distinct_seats(self):
        _,_,store=self.joined()
        invitations=[self.invite(store) for _ in range(2)]
        self.add_window('C',3);self.add_window('D',4)
        local=threading.local()
        def accept(member,invite):
            local.member=member
            return pair.execute(pair.build_parser().parse_args(['accept','--invite',invite['invite_path']]))
        with patch('scripts.pair_windows.current_window',side_effect=lambda *a:self.windows[local.member]),patch('scripts.pair_windows.setup_approval',return_value={'status':'unverified'}):
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                jobs=[pool.submit(accept,m,i) for m,i in zip(('C','D'),invitations)]
                self.assertEqual({job.result()['role'] for job in jobs},{'C','D'})
        self.assertEqual(len(store.member_ids()),4)


class MembersTests(unittest.TestCase):
    setUp=test_core.CoreTests.setUp
    tearDown=test_core.CoreTests.tearDown

    def binding(self,m):return m,self.sessions[m],self.directories[m]

    def add(self,m,provider='openai'):
        n={'C':3,'D':4}[m];sid='00000000-0000-0000-0000-'+str(n).zfill(12)
        cwd=self.root/m;cwd.mkdir();self.sessions[m]=sid;self.directories[m]=str(cwd)
        return self.store.join(m,sid,str(cwd),backend='queue',owned=False,proof={'id':sid,'cwd':str(cwd),'modelProvider':provider,'model':'gpt-test'})

    def assign(self,name,m,paths=None,**kw):
        return self.store.assign(name,name,m,paths or [name+'.txt'],requester=self.binding('A'),coordinator_epoch=1,**kw)

    def test_four_members_parallel_in_separate_workspaces_and_one_task_each(self):
        self.add('C');self.add('D')
        for m in ('A','B','C','D'):self.assertEqual(self.assign('work-'+m,m)['status'],'active')
        self.assertEqual(self.assign('extra','C')['status'],'pending')

    def test_same_workspace_or_conflicting_scope_stays_serial(self):
        self.add('C')
        with self.store.transaction() as db:
            proof={'git':{'path':self.directories['A']}}
            db.execute('UPDATE members SET proof=? WHERE id=?',(json.dumps(proof),'B'))
        self.assign('first','A',['shared/'])
        self.assertEqual(self.assign('second','B')['status'],'pending')
        self.assertEqual(self.assign('third','C',['shared/file.py'])['status'],'pending')

    def test_stale_coordinator_cannot_assign(self):
        self.add('C')
        with self.store.transaction() as db:
            self.store._put(db,'coordinator','C');self.store._put(db,'coordinator_epoch',2)
        with self.assertRaisesRegex(PairError,'coordinator'):self.assign('stale','B')
        self.assertEqual(self.store.assign('valid','valid','B',['valid'],requester=self.binding('C'),coordinator_epoch=2)['status'],'active')

    def test_six_trigger_eleven_recovery_and_monotonic_suggestions(self):
        self.store.configure_assistance(*self.binding('A'),checkpoints=True)
        self.store.save_quota('A',quota(6,pool='pool-a'))
        self.store.save_quota('B',quota(80,pool='pool-b'))
        self.store.schedule()
        self.assertEqual(self.store.status()['assistance']['members']['A']['state'],'saving')
        with self.store.transaction() as db:
            self.assertFalse(self.store._healthy(db,'A',{'A':quota(10.99,pool='pool-a')}))
            self.assertTrue(self.store._healthy(db,'A',{'A':quota(11,pool='pool-a')}))
        self.store.suggest_reserve(*self.binding('B'),14,'现场证据和未决消息增多')
        for value in (5,13,96,math.nan,math.inf,True):
            with self.subTest(value=value),self.assertRaises(ValueError):self.store.suggest_reserve(*self.binding('B'),value,'不合法建议')
        self.assertEqual(self.store.status()['assistance']['members']['B']['reserve_evidence']['source'],'model_estimate')

    def test_shared_pool_uses_highest_reserve_and_unknown_never_recovers(self):
        self.store.suggest_reserve(*self.binding('B'),20,'保留复杂交接余量')
        self.store.configure_assistance(*self.binding('A'),checkpoints=True)
        for m in ('A','B'):self.store.save_quota(m,quota(19,pool='shared'))
        self.store.schedule()
        self.assertTrue(all(v['state']=='saving' for v in self.store.status()['assistance']['members'].values()))
        with self.store.transaction() as db:self.assertFalse(self.store._healthy(db,'A',{'A':{'reliability':'missing'}}))

    def test_independent_gpt_review_gates_downstream(self):
        self.add('C')
        with self.store.transaction() as db:
            db.execute('UPDATE members SET proof=? WHERE id=?',(json.dumps({'modelProvider':'deepseek','model':'deepseek-test'}),'B'))
        self.assign('implementation','B')
        msg=next(m for m in self.store.status()['messages'] if m['task_id']=='implementation')
        claim=self.store.claim(*self.binding('B'),message_id=msg['id'])
        self.store.complete(*self.binding('B'),msg['id'],claim['claim_token'],'实现交付，已核对测试')
        self.assign('downstream','A',dependencies=['implementation'])
        self.assertEqual(self.store.task('downstream')['status'],'pending')
        review=next(t for t in self.store.status()['tasks'] if t['id'].startswith('gpt-review-'))
        self.assertEqual(review['assignee'],'C')
        message=next(m for m in self.store.status()['messages'] if m['task_id']==review['id'])
        owned=self.store.claim(*self.binding('C'),message_id=message['id'])
        page=self.store.read_delivery(*self.binding('C'),'delivery:implementation')
        self.store.ack_delivery(*self.binding('C'),'delivery:implementation',page['digest'])
        version=self.store.status()['work_reviews'][0]['version']
        with self.assertRaisesRegex(ValueError,'Independent'):
            self.store.review_work(*self.binding('B'),'implementation',version,'passed','检查实现',{'modelProvider':'openai','model':'gpt-test'})
        with self.assertRaisesRegex(ValueError,'Official GPT'):
            self.store.review_work(*self.binding('C'),'implementation',version,'passed','检查实现',{'modelProvider':'deepseek','model':'gpt-test'})
        self.store.review_work(*self.binding('C'),'implementation',version,'passed','独立检查完整差异及测试证据',{'modelProvider':'openai','model':'gpt-test'})
        self.assertEqual(self.store.task('downstream')['status'],'pending','审核任务必须先完成收尾')
        self.store.complete(*self.binding('C'),message['id'],owned['claim_token'],'审核结论已登记')
        self.assertEqual(self.store.task('downstream')['status'],'active')

    def test_schema_eight_upgrade_preserves_claim_and_reserve(self):
        msg=self.store.send('A','B','待处理消息')
        claim=self.store.claim(*self.binding('B'),message_id=msg['id'])
        self.store.setting('checkpoint_reserve:B',24)
        with self.store.transaction() as db:
            db.execute('DROP TABLE work_reviews');db.execute('PRAGMA user_version=8')
        upgraded=Store('test',home=self.home)
        self.assertEqual(upgraded.message(msg['id'])['claim_token'],claim['claim_token'])
        self.assertEqual(upgraded.status()['assistance']['members']['B']['reserve_percent'],24)
        self.assertEqual(upgraded.status()['assistance']['members']['A']['reserve_percent'],9)
        self.assertTrue(list(upgraded.directory.glob('schema-v8-*.backup.sqlite3')))

    def test_member_migration_failure_rolls_back_schema_and_bindings(self):
        with closing(sqlite3.connect(self.store.path)) as db:
            db.execute('DROP TABLE work_reviews');db.execute('PRAGMA user_version=8');db.commit()
        class Interrupted(sqlite3.Connection):
            def execute(self,sql,*args):
                if sql=='ALTER TABLE members_v9 RENAME TO members':raise sqlite3.OperationalError('interrupted migration')
                return super().execute(sql,*args)
        with closing(sqlite3.connect(self.store.path,factory=Interrupted)) as db:
            with self.assertRaisesRegex(sqlite3.OperationalError,'interrupted migration'):self.store._upgrade_members(db)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],8)
            self.assertEqual(db.execute('SELECT count(*) FROM members').fetchone()[0],2)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='members_v9'").fetchone())
        self.assertEqual(Store('test',home=self.home).member_ids(),['A','B'])

    def test_approval_routes_by_requesting_member_and_cannot_self_approve(self):
        self.add('C');self.add('D');self.store.setting('approval_assistance',True)
        requests=[]
        for member in ('C','D'):
            event={'hook_event_name':'PermissionRequest','session_id':self.sessions[member],'turn_id':'turn-'+member,
                'cwd':self.directories[member],'tool_name':'Bash','tool_input':{'command':'python task.py'}}
            requests.append(self.store.capture_approval(member,event))
        first=requests[0];claim=self.store.claim(*self.binding('A'),message_id=first['message_id'])
        with self.assertRaises(PairError):
            self.store.review_approval(*self.binding('C'),first['id'],claim['claim_token'],first['digest'],'allow','自批',within_permissions=True,capability_evidence='本人权限')
        self.store.review_approval(*self.binding('A'),first['id'],claim['claim_token'],first['digest'],'allow','在既有任务授权内',within_permissions=True,capability_evidence='A 当前可执行')
        with self.assertRaises(PairError):self.store.approval_outcome('D',self.sessions['D'],first['id'],first['digest'])
        self.assertEqual(self.store.approval_outcome('C',self.sessions['C'],first['id'],first['digest'])['status'],'allowed')
        self.assertEqual(self.store.message(requests[1]['message_id'])['process_status'],'unclaimed')

    def test_new_contribution_invalidates_passed_review_and_tracks_all_contributors(self):
        self.add('C','deepseek')
        self.assign('work','B')
        identity={'modelProvider':'deepseek','model':'deepseek-test'}
        self.store.record_contribution(*self.binding('C'),'work',identity)
        with self.store.transaction() as db:
            db.execute("UPDATE work_reviews SET status='passed' WHERE task_id='work'")
        self.store.record_contribution(*self.binding('B'),'work',{'modelProvider':'openai','model':'gpt-test'})
        row=self.store.status()['work_reviews'][0]
        self.assertEqual(set(json.loads(row['contributors'])),{'B','C'})
        self.assertEqual(row['status'],'pending')
        self.assertTrue(row['required'])

    def test_unreachable_member_not_newly_scheduled_but_claim_never_reassigned(self):
        self.add('C')
        import time
        self.store.setting('availability:C',{'state':'unreachable','observed_at':time.time()})
        self.assertEqual(self.assign('offline','C')['status'],'pending')
        self.store.setting('availability:C',{'state':'accepted','observed_at':time.time()})
        self.store.schedule()
        self.assertEqual(self.store.task('offline')['status'],'active')
        msg=next(m for m in self.store.status()['messages'] if m['task_id']=='offline')
        claimed=self.store.claim(*self.binding('C'),message_id=msg['id'])
        self.store.setting('availability:C',{'state':'unreachable','observed_at':time.time()})
        self.store.schedule()
        self.assertEqual(self.store.message(msg['id'])['claim_token'],claimed['claim_token'])
        self.assertEqual(self.store.task('offline')['assignee'],'C')

    def test_explicit_user_adjustment_is_separate_from_model_suggestion(self):
        self.store.suggest_reserve(*self.binding('B'),24,'复杂现场')
        with self.assertRaises(ValueError):self.store.suggest_reserve(*self.binding('B'),6,'模型自行降低')
        with self.assertRaises(ValueError):self.store.adjust_reserve(*self.binding('B'),'B',6,'请求者自行调整')
        self.store.adjust_reserve(*self.binding('A'),'B',6,'用户明确要求采用新默认6%')
        self.assertEqual(self.store.status()['assistance']['members']['B']['reserve_percent'],6)
        with self.assertRaises(ValueError):self.store.adjust_reserve(*self.binding('A'),'B',5,'仍不能低于下限')

    def test_four_way_quota_selection_does_not_ignore_c_and_d(self):
        from scripts.pair_balance import choose
        quotas={m:quota(n,pool=m) for m,n in zip(('A','B','C','D'),(30,40,85,60))}
        self.assertEqual(choose(list(quotas),5,quotas,{m:0 for m in quotas})[0],'C')

    def test_questions_follow_new_coordinator_without_granting_approval_rights(self):
        self.add('C');self.store.setting('coordinator','C');self.store.setting('delegation_enabled',True)
        question=self.store.send('B','C','本阶段采用哪个实现？',kind='question')
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT owner FROM delegation_questions WHERE id=?',(question['id'],)).fetchone()[0],'C')
        self.store.send('C','B','按用户原目标和项目约定执行',kind='reply',reply_to=question['id'])
        with self.assertRaises(PairError):self.store.delegation_policy(*self.binding('C'),'新统筹不能改变原A授权',enabled=True)

    def test_repair_keeps_rejected_source_blocked_until_replacement_review_finishes(self):
        self.add('C')
        with self.store.transaction() as db:
            db.execute('UPDATE members SET proof=? WHERE id=?',(json.dumps({'modelProvider':'deepseek','model':'deepseek-test'}),'B'))
        self.assign('original','B')
        msg=next(m for m in self.store.status()['messages'] if m['task_id']=='original')
        owned=self.store.claim(*self.binding('B'),message_id=msg['id'])
        self.store.complete(*self.binding('B'),msg['id'],owned['claim_token'],'首次实现和测试证据')
        review=next(t for t in self.store.status()['tasks'] if t['id'].startswith('gpt-review-'))
        msg=next(m for m in self.store.status()['messages'] if m['task_id']==review['id'])
        owned=self.store.claim(*self.binding('C'),message_id=msg['id'])
        page=self.store.read_delivery(*self.binding('C'),'delivery:original')
        self.store.ack_delivery(*self.binding('C'),'delivery:original',page['digest'])
        version=next(r['version'] for r in self.store.status()['work_reviews'] if r['task_id']=='original')
        self.store.review_work(*self.binding('C'),'original',version,'blocked','发现需要修复的行为',{'modelProvider':'openai','model':'gpt-test'})
        self.store.complete(*self.binding('C'),msg['id'],owned['claim_token'],'阻塞问题已完整记录')
        self.assign('after','A',dependencies=['original'])
        fixed=self.assign('fix','B',paths=['original.txt'],repairs='original')
        self.assertEqual(fixed['status'],'active')
        self.assertEqual(self.store.task('after')['status'],'pending')
        self.assertEqual(self.store.setting('replacement:original'),'fix')
        with self.store.transaction() as db:self.assertFalse(self.store._review_clear(db,'original'))
