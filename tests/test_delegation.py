"""委托决策、过期恢复和授权竞争；合成事件不等于真实双 CLI 验收。"""
import json
import sqlite3
import time
import unittest
from unittest.mock import patch

from scripts.pair_core import Store,PairError,Daemon
from scripts.pair_approvals import approval_package_digest
from tests import test_core


class DelegationTests(unittest.TestCase):
    setUp=test_core.CoreTests.setUp
    tearDown=test_core.CoreTests.tearDown

    def binding(self,member):return member,self.sessions[member],self.directories[member]

    def enable(self):
        self.store.delegation_policy(*self.binding('A'),'用户要求当前项目内由 A 代答并审核',enabled=True)

    def event(self,turn='b-1',**changes):
        event={'hook_event_name':'PermissionRequest','session_id':self.sessions['B'],'turn_id':turn,
            'cwd':str(self.root),'tool_name':'Bash','tool_input':{'command':'python -m pip --version'}}
        event.update(changes)
        return event

    def request(self,**changes):return self.store.capture_approval('B',self.event(**changes))

    def allow(self,request):
        claim=self.store.claim(*self.binding('A'),message_id=request['message_id'])
        return self.store.review_approval(*self.binding('A'),request['id'],claim['claim_token'],request['digest'],'allow',
            '用户已授权项目依赖检查，准确输入无额外副作用',within_permissions=True,
            capability_evidence='当前 A 工具可在该目录执行此命令，无额外权限要求')

    def expire(self,request):
        with self.store.transaction() as db:db.execute('UPDATE approval_requests SET expires=? WHERE id=?',(time.time()-1,request['id']))
        return self.store.approval_outcome('B',self.sessions['B'],request['id'],request['digest'])

    def test_taskless_outside_directory_request_and_single_consumption(self):
        self.enable();request=self.request();self.allow(request)
        self.assertIsNone(self.store.message(request['message_id'])['task_id'])
        first=self.store.approval_outcome('B',self.sessions['B'],request['id'],request['digest'])
        second=self.store.approval_outcome('B',self.sessions['B'],request['id'],request['digest'])
        self.assertEqual(first['status'],'allowed');self.assertEqual(second['status'],'granted')
        with self.assertRaisesRegex(PairError,'reconciliation'):self.request(turn='b-2')
        self.store.report_approval(*self.binding('B'),request['id'],'success','退出码 0；实际返回依赖版本')
        next_request=self.request(turn='b-3')
        self.assertNotEqual(next_request['id'],request['id'])

    def test_revoke_between_review_and_hook_consumption(self):
        self.enable();request=self.request();self.allow(request)
        self.store.delegation_policy(*self.binding('A'),'用户取消该操作',enabled=False)
        self.assertEqual(self.store.approval_outcome('B',self.sessions['B'],request['id'],request['digest'])['status'],'expired')
        with self.assertRaisesRegex(PairError,'disabled'):self.request()

    def test_pause_and_resume_does_not_resurrect_allow(self):
        self.enable();request=self.request();self.allow(request)
        self.store.control('paused');self.store.control('running')
        self.assertEqual(self.store.approval_outcome('B',self.sessions['B'],request['id'],request['digest'])['status'],'expired')

    def test_changed_authorization_rejects_claimed_review(self):
        self.enable();request=self.request()
        claim=self.store.claim(*self.binding('A'),message_id=request['message_id'])
        self.store.delegation_policy(*self.binding('A'),'用户改为只读检查')
        with self.assertRaises(PairError):
            self.store.review_approval(*self.binding('A'),request['id'],claim['claim_token'],request['digest'],'allow','旧授权',within_permissions=True,capability_evidence='可写')

    def test_timeout_requires_a_ready_and_second_timeout_needs_user(self):
        self.enable();first=self.request();self.expire(first)
        with self.assertRaisesRegex(PairError,'arrange retry'):self.request(turn='b-2')
        ready=self.store.prepare_approval_retry(*self.binding('A'),first['id'],'回读工具结果确认未执行，B 原回合已结束')
        duplicate=self.store.prepare_approval_retry(*self.binding('A'),first['id'],'重复核查')
        self.assertEqual(ready['message_id'],duplicate['message_id'])
        second=self.request(turn='b-2');self.expire(second)
        rows=self.store.status()['assistance']['approvals']
        row=next(r for r in rows if r['id']==second['id'])
        self.assertEqual((row['attempt'],row['retry_state']),(2,'needs_user'))
        with self.assertRaisesRegex(PairError,'Retry unavailable'):
            self.store.prepare_approval_retry(*self.binding('A'),second['id'],'第三次不允许')

    def test_no_reissue_while_b_turn_unresolved(self):
        self.enable();request=self.request();self.expire(request)
        message=self.store.send('A','B','B 仍在执行其他回合')
        with self.store.transaction() as db:db.execute("UPDATE messages SET delivery_status='accepted',remote_status='inProgress' WHERE id=?",(message['id'],))
        with self.assertRaisesRegex(PairError,'turn unresolved'):
            self.store.prepare_approval_retry(*self.binding('A'),request['id'],'不能只凭时间判断已结束')

    def test_description_change_keeps_timeout_retry_chain(self):
        self.enable()
        first=self.request(tool_input={'command':'python -m pip --version','description':'第一次请求'})
        self.expire(first)
        with self.store.transaction() as db:
            db.execute("UPDATE approval_requests SET operation_digest='legacy-digest' WHERE id=?",(first['id'],))
        self.store.prepare_approval_retry(*self.binding('A'),first['id'],'已核实未执行')
        second=self.request(turn='b-2',tool_input={'command':'python -m pip --version','description':'第二次请求'})
        self.assertNotEqual(first['digest'],second['digest'])
        with self.store.transaction() as db:
            row=db.execute('SELECT previous_id,attempt FROM approval_requests WHERE id=?',(second['id'],)).fetchone()
            self.assertEqual(tuple(row),(first['id'],2))
        self.expire(second)
        row=next(r for r in self.store.status()['assistance']['approvals'] if r['id']==second['id'])
        self.assertEqual(row['retry_state'],'needs_user')
        with self.assertRaisesRegex(PairError,'arrange retry'):
            self.request(turn='b-3',tool_input={'command':'python -m pip --version','description':'第三次请求'})

    def test_unknown_execution_not_replayed(self):
        self.enable();request=self.request();self.allow(request)
        self.store.approval_outcome('B',self.sessions['B'],request['id'],request['digest'])
        self.store.report_approval(*self.binding('B'),request['id'],'unknown','连接中断，不能确定退出码或副作用')
        with self.assertRaisesRegex(PairError,'reconciliation'):self.request(turn='b-2')

    def test_operation_identity_preserves_execution_fields(self):
        from scripts.pair_approvals import operation_fingerprint
        original=self.event(tool_input={'command':'echo first','description':'说明','timeout_ms':1000})
        baseline=operation_fingerprint(original)
        for event in (
            self.event(tool_input={'command':'echo second','description':'说明','timeout_ms':1000}),
            self.event(tool_input={'command':'echo first','description':'说明','timeout_ms':2000}),
            self.event(cwd=str(self.root/'other'),tool_input=original['tool_input']),
            self.event(tool_name='Other',tool_input=original['tool_input']),
        ):
            self.assertNotEqual(operation_fingerprint(event),baseline)
        self.assertNotEqual(operation_fingerprint(self.event(tool_name='Other',tool_input={'command':'echo first','description':'changed','timeout_ms':1000})),
            operation_fingerprint(self.event(tool_name='Other',tool_input=original['tool_input'])))

    def test_question_dedupe_reply_and_adoption(self):
        self.enable()
        q=self.store.send('B','A','选择项目已有方案？',kind='question',dedupe_key='q-1')
        self.assertEqual(self.store.send('B','A','选择项目已有方案？',kind='question',dedupe_key='q-1')['id'],q['id'])
        self.store.send('A','B','沿用已有方案，避免增加维护成本',kind='reply',reply_to=q['id'],progress_note='已找到现有实现')
        result=self.store.question_state(*self.binding('B'),q['id'],'adopted','已采用并继续原任务')
        self.assertEqual(result['state'],'adopted')

    def test_question_needs_user_and_revoke(self):
        self.enable();q=self.store.send('B','A','缺少用户本人提供的信息',kind='question')
        self.assertTrue(self.store.question_state(*self.binding('A'),q['id'],'needs_user','请用户补充收件人')['notify_user'])
        with self.assertRaisesRegex(PairError,'no longer pending'):
            self.store.send('A','B','不能猜测私人信息',kind='reply',reply_to=q['id'])
        self.store.delegation_policy(*self.binding('A'),'用户取消原问题')
        with self.assertRaises(PairError):self.store.question_state(*self.binding('B'),q['id'],'adopted','不能采用')

    def test_revoked_reply_cannot_be_claimed_or_dispatched(self):
        self.enable();q=self.store.send('B','A','选择原方案？',kind='question')
        reply=self.store.send('A','B','采用原方案',kind='reply',reply_to=q['id'])
        self.store.delegation_policy(*self.binding('A'),'用户改变要求，旧方案失效')
        self.assertFalse(self.store.claim(*self.binding('B'),message_id=reply['id'])['claimed'])
        self.assertFalse(Daemon(self.store,test_core.FakeTransport(self.store)).dispatch_one())

    def test_two_taskless_rounds_stop_loop_and_new_evidence_resumes(self):
        self.enable()
        for i in range(2):
            q=self.store.send('B','A','问题 '+str(i),kind='question')
            self.store.send('A','B','没有新增证据的答复',kind='reply',reply_to=q['id'])
        with self.assertRaisesRegex(PairError,'loop stopped'):self.store.send('B','A','继续重复',kind='question')
        self.store.send('B','A','有新错误日志',kind='question',progress_note='已实际运行检查，错误位置发生变化')
        self.assertTrue(any(q['state']=='needs_user' for q in self.store.status()['assistance']['questions']))

    def test_credentials_never_persist_in_question_or_approval(self):
        self.enable()
        with self.assertRaisesRegex(PairError,'Credentials'):self.store.send('B','A','Bearer sk-private-value',kind='question')
        with self.assertRaisesRegex(PairError,'Credentials'):self.request(tool_input={'command':'echo sk-private-value'})
        self.assertEqual(self.store.status()['assistance']['approvals'],[])

    def coordination_command(self,command,*args):
        import sys
        from pathlib import Path
        from scripts.pair_windows import ps_command
        return ps_command([sys.executable,str(Path(__file__).resolve().parents[1]/'scripts'/'pair.py'),
            '--pair','test','--state-root',str(self.store.state_root),'--codex-home',self.store.member('B')['codex_home'],
            command,'--member','B','--session',self.sessions['B'],'--worktree',self.directories['B'],*args])

    def test_reporting_an_approval_does_not_require_reporting_itself_forever(self):
        self.enable()
        command=self.coordination_command('report-approval','--approval','original','--outcome','success','--body','exit_code=0; 实际结果已核实')
        request=self.request(tool_input={'command':command});self.allow(request)
        self.store.approval_outcome('B',self.sessions['B'],request['id'],request['digest'])
        with self.store.transaction() as db:
            self.assertFalse(self.store._approval_unresolved(db))
        row=self.store.status()['assistance']['approvals'][0]
        self.assertEqual(row['status'],'granted');self.assertTrue(row['coordination_only'])

    def test_granted_coordination_can_request_fresh_review(self):
        self.enable()
        command=self.coordination_command('check-checkpoint')
        first=self.request(tool_input={'command':command})
        self.allow(first)
        self.store.approval_outcome('B',self.sessions['B'],first['id'],first['digest'])
        second=self.request(turn='b-2',tool_input={'command':command})
        self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(self.store.approval_outcome('B',self.sessions['B'],second['id'],second['digest'])['status'],'pending')

    def test_arbitrary_command_or_appended_shell_is_not_coordination(self):
        self.enable()
        for command in ('python task_script.py',self.coordination_command('check-checkpoint')+'; Write-Output extra'):
            request=self.request(tool_input={'command':command});self.allow(request)
            self.store.approval_outcome('B',self.sessions['B'],request['id'],request['digest'])
            with self.store.transaction() as db:self.assertTrue(self.store._approval_unresolved(db))
        self.assertTrue(all(not r['coordination_only'] for r in self.store.status()['assistance']['approvals']))

    def test_resumed_b_completion_releases_notice_without_finishing_parent(self):
        from pathlib import Path
        from scripts.pair_transport import AppServer
        from tests.test_assistance import AssistanceTests
        from tests.test_turn_completion import event
        AssistanceTests.prepare_checkpoints(self);self.enable()
        message,claim=AssistanceTests.task(self)
        with self.store.transaction() as db:db.execute("UPDATE messages SET delivery_status='accepted',remote_status='inProgress',turn_id='old' WHERE id=?",(message['id'],))
        command=self.coordination_command('check-checkpoint')
        request=self.request(turn='new',tool_input={'command':command});self.allow(request)
        self.store.approval_outcome('B',self.sessions['B'],request['id'],request['digest'])
        directory=self.home/'sessions';directory.mkdir()
        path=directory/'rollout.jsonl'
        records=[{'type':'session_meta','payload':{'id':self.sessions['B'],'cwd':self.directories['B']}},
            event('task_started','old'),event('task_started','new'),event('task_complete','new',completed_at=124)]
        path.write_text(''.join(json.dumps(r)+'\n' for r in records),encoding='utf-8')
        client=AppServer.__new__(AppServer);client.metadata_only=True
        fake=test_core.FakeTransport(self.store);fake.observe=client.observe
        notice=self.store.send('A','B','修复后继续协调，不重做旧操作')
        daemon=Daemon(self.store,fake)
        with patch.object(client,'validate',return_value={'path':str(path),'status':{'type':'notLoaded'}}), \
                patch.object(client,'turns',side_effect=lambda _:iter([{'id':'old','status':'interrupted','completedAt':None,'items':[]}])):
            daemon.reconcile()
        self.assertEqual(self.store.message(message['id'])['remote_status'],'interrupted')
        self.assertEqual(self.store.message(message['id'])['process_status'],'claimed')
        self.assertEqual(self.store.task('work')['status'],'active')
        self.assertTrue(daemon.dispatch_one())
        self.assertEqual(fake.calls[-1][1]['id'],notice['id'])

    def test_hook_trust_is_separate_from_install_and_callback(self):
        self.enable()
        base={'statusMessage':'codex-pair:test:B','enabled':True,'eventName':'permissionRequest','trustStatus':'untrusted','currentHash':'hash'}
        report=self.store.record_hook_status({'data':[{'hooks':[base],'errors':[]}]})
        self.assertTrue(report['trust_required']);self.assertFalse(report['active_verified'])
        base['trustStatus']='trusted'
        self.assertEqual(self.store.record_hook_status({'data':[{'hooks':[base],'errors':[]}]})['status'],'trusted')
        base['trustStatus']='modified'
        self.assertTrue(self.store.record_hook_status({'data':[{'hooks':[base],'errors':[]}]})['trust_required'])

    def test_package_digest_changes_when_code_changes(self):
        path=self.root/'pair_example.py';path.write_text('version=1',encoding='utf-8')
        before=approval_package_digest(self.root)
        path.write_text('version=2',encoding='utf-8')
        self.assertNotEqual(before,approval_package_digest(self.root))

    def test_other_session_hook_exits_before_loading_state(self):
        import subprocess,sys
        from pathlib import Path
        script=Path(__file__).resolve().parents[1]/'scripts'/'pair_permission_hook.py'
        process=subprocess.run([sys.executable,str(script),'--pair','absent','--state-root',str(self.root/'absent'),
            '--member','B','--session',self.sessions['B'],'--package-digest','outdated'],
            input=json.dumps(self.event(session_id=self.sessions['A'])),text=True,encoding='utf-8',capture_output=True,check=True)
        self.assertEqual(json.loads(process.stdout),{})
        self.assertFalse((self.root/'absent').exists())

    def test_hook_upgrade_preserves_neighbor_handler_and_invalidates_old_allow(self):
        import sys
        from pathlib import Path
        self.enable();request=self.request();self.allow(request)
        other=self.root/'b-config';other.mkdir()
        with self.store.transaction() as db:db.execute("UPDATE members SET codex_home=? WHERE id='B'",(str(other),))
        script=Path(__file__).resolve().parents[1]/'scripts'/'pair_permission_hook.py'
        path=other/'hooks.json'
        neighbor={'type':'command','command':'echo neighbor'}
        path.write_text(json.dumps({'hooks':{'PermissionRequest':[{'matcher':'*','hooks':[
            {'type':'command','command':'old pair_permission_hook.py','statusMessage':'codex-pair:test:B'},neighbor]}]}}),encoding='utf-8')
        self.store.install_approval_hook(*self.binding('A'),python=sys.executable,script=str(script))
        groups=json.loads(path.read_text(encoding='utf-8'))['hooks']['PermissionRequest']
        self.assertIn(neighbor,[h for g in groups for h in g['hooks']])
        self.assertEqual(self.store.approval_outcome('B',self.sessions['B'],request['id'],request['digest'])['status'],'expired')

    def test_migration_eight_preserves_claim_and_is_atomic(self):
        self.enable();request=self.request()
        claim=self.store.claim(*self.binding('A'),message_id=request['message_id'])
        # 从现有表构造真实 v7 形状，验证已有领取凭据不变。
        with self.store.transaction() as db:
            db.execute('ALTER TABLE approval_requests RENAME TO saved_approvals')
            db.execute('DROP TABLE delegation_questions')
            db.execute('''CREATE TABLE approval_requests AS SELECT id,member,task_id,source_message,review_message,event,digest,status,expires,reason,capability_evidence,created,updated FROM saved_approvals''')
            db.execute('DROP TABLE saved_approvals');db.execute('PRAGMA user_version=7')
        upgraded=Store('test',home=self.home)
        self.assertEqual(upgraded.message(request['message_id'])['claim_token'],claim['claim_token'])
        with upgraded.transaction() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],9)
            self.assertFalse(db.execute('PRAGMA foreign_key_check').fetchall())
        class Interrupted(sqlite3.Connection):
            def execute(self,sql,*args):
                if sql=='PRAGMA user_version=8':raise sqlite3.OperationalError('interrupted migration')
                return super().execute(sql,*args)
        db=sqlite3.connect(':memory:',factory=Interrupted)
        try:
            db.execute('CREATE TABLE approval_requests AS SELECT '+','.join('NULL AS '+name for name in ('id','member','task_id','source_message','review_message','event','digest','status','expires','reason','capability_evidence','created','updated'))+' WHERE 0')
            db.execute('PRAGMA user_version=7')
            with self.assertRaises(sqlite3.OperationalError):self.store._upgrade_delegation(db)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],7)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='delegation_questions'").fetchone())
        finally:db.close()

    def test_checkpoint_transfers_question_decision_but_not_a_permissions(self):
        from tests.test_assistance import AssistanceTests
        from tests.test_handoff_balance import quota
        AssistanceTests.prepare_checkpoints(self)
        AssistanceTests.separate_pools(self)
        self.enable()
        q=self.store.send('B','A','需要决定后续实现方式',kind='question')
        self.store.save_quota('A',quota(8,pool='a'));self.store.save_quota('B',quota(80,pool='b'))
        saved=self.store.save_checkpoint(*self.binding('A'),'待决定实现方案，任务现场已核对')
        self.store.schedule()
        fingerprint=AssistanceTests.read_all(self,'B','A')
        self.store.consume_checkpoint(*self.binding('B'),'A',fingerprint,'接管普通问题，审批资格仍属于 A')
        row=next(qr for qr in self.store.status()['assistance']['questions'] if qr['id']==q['id'])
        self.assertEqual((row['owner'],row['state']),('B','pending'))
        self.store.question_state(*self.binding('B'),q['id'],'adopted','接管后选择沿用项目现有实现')
        with self.assertRaisesRegex(PairError,'no longer pending'):
            self.store.send('A','B','旧窗口不得抢回决策',kind='reply',reply_to=q['id'])
        self.assertFalse(__import__('pathlib').Path(saved['path']).exists())
        request=self.request()
        outcome=self.store.approval_outcome('B',self.sessions['B'],request['id'],request['digest'])
        self.assertEqual(outcome['status'],'expired')
        self.assertIn('需要用户',outcome['reason'])


if __name__=='__main__':unittest.main()
