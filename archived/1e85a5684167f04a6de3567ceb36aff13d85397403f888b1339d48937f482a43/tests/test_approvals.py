"""官方 Hook 审批的行为测试；不把合成 Hook 输入当成真实 CLI 验收。"""
import json
from pathlib import Path
import time
import unittest
from unittest.mock import patch

from scripts.pair_core import Daemon,PairError
from scripts.pair_permission_hook import run_hook
from tests import test_core


class ApprovalTests(unittest.TestCase):
    setUp=test_core.CoreTests.setUp
    tearDown=test_core.CoreTests.tearDown

    def binding(self,member):return member,self.sessions[member],self.directories[member]

    def prepare(self):
        self.store.setting('approval_assistance',True)
        with self.store.transaction() as db:
            for member in ('A','B'):
                proof=json.loads(self.store._member(db,member)['proof']);proof['git']={'path':str(self.project)}
                db.execute('UPDATE members SET proof=? WHERE id=?',(json.dumps(proof),member))
        self.store.assign('work','原授权任务','B',['output.txt'])
        source=next(m for m in self.store.status()['messages'] if m['kind']=='task')
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET delivery_status='accepted',remote_status='inProgress',turn_id='turn-b' WHERE id=?",(source['id'],))
        self.event={'hook_event_name':'PermissionRequest','session_id':self.sessions['B'],'turn_id':'turn-b',
            'cwd':self.directories['B'],'tool_name':'Bash','tool_input':{'command':'python task_script.py'},'permission_mode':'default'}
        return source

    def review(self,request,decision='allow',**kwargs):
        claim=self.store.claim(*self.binding('A'),message_id=request['message_id'])
        return self.store.review_approval(*self.binding('A'),request['id'],claim['claim_token'],request['digest'],decision,
            '已核对用户原任务、准确命令、影响范围及当前工具权限',**kwargs)

    def test_request_captured_before_b_can_claim_database(self):
        source=self.prepare()
        request=self.store.capture_approval('B',self.event)
        self.assertEqual(self.store.message(source['id'])['process_status'],'unclaimed')
        self.assertEqual(self.store.task('work')['status'],'active')
        self.assertEqual(self.store.message(request['message_id'])['kind'],'approval_review')

    def test_readonly_review_dispatches_while_b_waits_in_same_workspace(self):
        self.prepare();request=self.store.capture_approval('B',self.event)
        fake=test_core.FakeTransport(self.store)
        self.assertTrue(Daemon(self.store,fake).dispatch_one())
        self.assertEqual(fake.calls[0][1]['id'],request['message_id'])
        self.store.send('A','B','普通业务通知不能绕过串行锁')
        self.assertFalse(Daemon(self.store,fake).dispatch_one())

    def test_allow_requires_a_actual_permission_attestation_and_exact_digest(self):
        self.prepare();request=self.store.capture_approval('B',self.event)
        claim=self.store.claim(*self.binding('A'),message_id=request['message_id'])
        for member,summary,within,evidence in [('B',request['digest'],True,'B 不是审核方'),('A','changed',True,'当前可写'),('A',request['digest'],False,None)]:
            with self.subTest(member=member,summary=summary,within=within),self.assertRaises(PairError):
                self.store.review_approval(*self.binding(member),request['id'],claim['claim_token'],summary,'allow','审核依据',within_permissions=within,capability_evidence=evidence)
        result=self.store.review_approval(*self.binding('A'),request['id'],claim['claim_token'],request['digest'],'allow','当前任务明确授权，仅创建任务文件',within_permissions=True,capability_evidence='A 当前工具允许写此工作树；不需要额外提权')
        self.assertEqual(result['scope'],'single_request');self.assertFalse(result['execution_verified'])
        self.assertEqual(self.store.approval_outcome('B',self.sessions['B'],request['id'],request['digest'])['status'],'allowed')
        with self.assertRaises(PairError):
            self.store.review_approval(*self.binding('A'),request['id'],claim['claim_token'],request['digest'],'allow','不能重放',within_permissions=True,capability_evidence='可写')

    def test_hook_waits_for_a_instead_of_autoaccepting(self):
        self.prepare()
        def review(_):
            with self.store.transaction() as db:
                row=db.execute('SELECT * FROM approval_requests').fetchone()
                request={'id':row['id'],'digest':row['digest'],'message_id':row['review_message']}
            self.review(request,within_permissions=True,capability_evidence='当前 A 允许该文件操作')
        output=run_hook(self.store,'B',self.event,sleep=review)
        self.assertEqual(output,{'hookSpecificOutput':{'hookEventName':'PermissionRequest','decision':{'behavior':'allow'}}})

    def test_deny_returns_no_user_approval_fallback(self):
        self.prepare()
        def deny(_):
            with self.store.transaction() as db:row=dict(db.execute('SELECT * FROM approval_requests').fetchone())
            self.review({'id':row['id'],'digest':row['digest'],'message_id':row['review_message']},decision='deny')
        result=run_hook(self.store,'B',self.event,sleep=deny)
        self.assertEqual(result['hookSpecificOutput']['decision']['behavior'],'deny')

    def test_expiry_and_stopped_collaboration_deny(self):
        self.prepare();request=self.store.capture_approval('B',self.event)
        with self.store.transaction() as db:db.execute('UPDATE approval_requests SET expires=?',(time.time()-1,))
        self.assertEqual(self.store.approval_outcome('B',self.sessions['B'],request['id'],request['digest'])['status'],'expired')
        self.assertFalse(self.store.claim(*self.binding('A'),message_id=request['message_id'])['claimed'])
        self.store.control('stopped')
        with self.assertRaisesRegex(PairError,'not running'):self.store.capture_approval('B',self.event)

    def test_original_turn_change_cannot_approve_old_action(self):
        source=self.prepare();request=self.store.capture_approval('B',self.event)
        claim=self.store.claim(*self.binding('A'),message_id=request['message_id'])
        with self.store.transaction() as db:db.execute("UPDATE messages SET remote_status='completed' WHERE id=?",(source['id'],))
        with self.assertRaisesRegex(PairError,'turn ended'):
            self.store.review_approval(*self.binding('A'),request['id'],claim['claim_token'],request['digest'],'allow','旧动作',within_permissions=True,capability_evidence='当前 A 可写')

    def test_other_session_is_not_intercepted_and_no_secrets_copied(self):
        self.prepare()
        self.assertEqual(run_hook(self.store,'B',dict(self.event,session_id=self.sessions['A'])),{})
        with self.assertRaisesRegex(PairError,'Credentials'):
            self.store.capture_approval('B',dict(self.event,tool_input={'command':'echo sk-secret'}))
        self.assertEqual(self.store.status()['assistance']['approvals'],[])

    def test_bound_b_can_continue_owned_task_in_later_real_turn(self):
        source=self.prepare()
        with self.store.transaction() as db:db.execute("UPDATE messages SET remote_status='completed' WHERE id=?",(source['id'],))
        request=self.store.capture_approval('B',dict(self.event,turn_id='next-b-turn'))
        self.store.schedule()
        self.assertEqual(self.store.approval_outcome('B',self.sessions['B'],request['id'],request['digest'])['status'],'pending')
        self.review(request,within_permissions=True,capability_evidence='当前 A 可执行该授权操作')
        self.assertEqual(self.store.approval_outcome('B',self.sessions['B'],request['id'],request['digest'])['status'],'allowed')

    def test_hook_script_emits_utf8_deny_on_malformed_input(self):
        import subprocess,sys
        script=Path(__file__).resolve().parents[1]/'scripts'/'pair_permission_hook.py'
        result=subprocess.run([sys.executable,str(script),'--pair','test','--state-root',str(self.store.state_root),'--member','B'],
            input='这不是 JSON'.encode('utf-8'),capture_output=True,check=True)
        output=json.loads(result.stdout.decode('utf-8'))
        self.assertEqual(output['hookSpecificOutput']['decision']['behavior'],'deny')

    def test_cannot_close_review_using_ordinary_complete(self):
        self.prepare();request=self.store.capture_approval('B',self.event)
        claim=self.store.claim(*self.binding('A'),message_id=request['message_id'])
        with self.assertRaisesRegex(PairError,'Dedicated'):
            self.store.complete(*self.binding('A'),request['message_id'],claim['claim_token'],'假装已批准')

    def test_install_preserves_other_hooks_and_does_not_forge_trust(self):
        self.prepare()
        b_home=self.root/'b-home';b_home.mkdir()
        with self.store.transaction() as db:db.execute("UPDATE members SET codex_home=? WHERE id='B'",(str(b_home),))
        path=b_home/'hooks.json'
        original={'hooks':{'SessionStart':[{'hooks':[{'type':'command','command':'echo existing'}]}]}}
        path.write_text(json.dumps(original),encoding='utf-8')
        script=Path(__file__).resolve().parents[1]/'scripts'/'pair_permission_hook.py'
        import sys
        report=self.store.install_approval_hook(*self.binding('A'),python=sys.executable,script=str(script))
        self.assertTrue(report['trust_required']);self.assertFalse(report['sandbox_changed']);self.assertFalse(report['active_verified'])
        installed=json.loads(path.read_text(encoding='utf-8'))
        self.assertTrue(path.read_bytes().endswith(b'\n'))
        self.assertEqual(installed['hooks']['SessionStart'],original['hooks']['SessionStart'])
        self.store.install_approval_hook(*self.binding('A'),python=sys.executable,script=str(script))
        self.assertEqual(len(json.loads(path.read_text(encoding='utf-8'))['hooks']['PermissionRequest']),1)
        self.assertEqual(len(list(b_home.glob('hooks.codex-pair-backup-*.json'))),1)

    def test_install_keeps_invalid_original_without_replacement(self):
        self.prepare()
        b_home=self.root/'invalid-hook-home';b_home.mkdir()
        with self.store.transaction() as db:db.execute("UPDATE members SET codex_home=? WHERE id='B'",(str(b_home),))
        path=b_home/'hooks.json';original=b'{invalid json'
        path.write_bytes(original)
        script=Path(__file__).resolve().parents[1]/'scripts'/'pair_permission_hook.py'
        import sys
        with patch('os.replace') as replace:
            with self.assertRaises(json.JSONDecodeError):
                self.store.install_approval_hook(*self.binding('A'),python=sys.executable,script=str(script))
            replace.assert_not_called()
        self.assertEqual(path.read_bytes(),original)

    def test_concurrent_install_validates_temporary_json_and_preserves_neighbor(self):
        import concurrent.futures
        import os
        import sys
        self.prepare()
        b_home=self.root/'concurrent-hook-home';b_home.mkdir()
        with self.store.transaction() as db:db.execute("UPDATE members SET codex_home=? WHERE id='B'",(str(b_home),))
        path=b_home/'hooks.json'
        neighbor={'matcher':'*','hooks':[{'type':'command','command':'echo neighbor'}]}
        path.write_text(json.dumps({'hooks':{'PermissionRequest':[neighbor]}}),encoding='utf-8')
        script=Path(__file__).resolve().parents[1]/'scripts'/'pair_permission_hook.py'
        replace=os.replace;validated=[]
        def checked_replace(source,target):
            json.loads(Path(source).read_text(encoding='utf-8'));validated.append(True)
            return replace(source,target)
        def install(_):return self.store.install_approval_hook(*self.binding('A'),python=sys.executable,script=str(script))
        with patch('os.replace',side_effect=checked_replace),concurrent.futures.ThreadPoolExecutor(2) as pool:
            reports=list(pool.map(install,range(2)))
        self.assertTrue(all(r['trust_required'] for r in reports))
        self.assertEqual(validated,[True])
        groups=json.loads(path.read_text(encoding='utf-8'))['hooks']['PermissionRequest']
        self.assertEqual(groups[0],neighbor)
        self.assertEqual(len(groups),2)
        self.assertEqual(len(list(b_home.glob('hooks.codex-pair-backup-*.json'))),1)

    def test_schema_seven_migration_rolls_back_on_interruption(self):
        import sqlite3
        class Interrupted(sqlite3.Connection):
            fail_commit=True
            def execute(self,sql,*args):
                if self.fail_commit and sql=='PRAGMA user_version=7':raise sqlite3.OperationalError('interrupted migration')
                return super().execute(sql,*args)
        db=sqlite3.connect(':memory:',factory=Interrupted)
        try:
            db.execute('PRAGMA user_version=6')
            with self.assertRaises(sqlite3.OperationalError):self.store._upgrade_approvals(db)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],6)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='approval_requests'").fetchone())
            db.fail_commit=False
            self.store._upgrade_approvals(db)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],7)
        finally:db.close()


if __name__=='__main__':unittest.main()
