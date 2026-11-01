"""官方审批shlex展示与原始argv逐项核验；不执行载荷。"""
import copy,json,shlex,unittest
from tests import test_exact_managed_receipts as exact
from scripts import pair_transport as t

class ArgvTests(unittest.TestCase):
    def setUp(self):
        exact.ExactReceiptTests.setUp(self)
        # 本组是旧0.163.0调用接口回归；合成已验证版本，不依赖生产环境A的proof。
        from unittest.mock import patch
        from scripts import pair_review_runtime as runtime
        def legacy(store,db,*args,**kwargs):
            return json.loads(store._member(db,'A')['proof']).get('cliVersion')
        handle=patch.object(runtime,'stored_context',side_effect=legacy)
        handle.start();self.addCleanup(handle.stop)
        with self.store.transaction() as db:
            parent=db.execute("SELECT proof FROM members WHERE id='A'").fetchone()[0]
            proof=json.loads(parent);proof['cliVersion']='0.163.0';value=json.dumps(proof)
            db.execute("UPDATE members SET proof=? WHERE id='A'",(value,))
            binding=self.store._option(db,'managed_binding:'+self.request['id'])
            binding['proof']=value;self.store._put(db,'managed_binding:'+self.request['id'],binding)
    tearDown=exact.ExactReceiptTests.tearDown
    actor=exact.ExactReceiptTests.actor
    allow=exact.ExactReceiptTests.allow
    status=exact.ExactReceiptTests.status
    settle=exact.ExactReceiptTests.settle
    reset=exact.ExactReceiptTests.reset
    def prepare(self,argv):
        self.params['command']=shlex.join(argv)
        with self.store.transaction() as db:
            db.execute('UPDATE managed_permissions SET request=? WHERE id=?',(json.dumps({'params':self.params}),self.request['id']))
        self.turn['items'][0]['command']=argv
    def test_official_shlex_request_matches_raw_windows_argv(self):
        self.prepare(['C:\\Program Files\\PowerShell\\7\\pwsh.exe','-Command','Get-Content a; Get-Content b'])
        self.assertEqual(self.settle(),'success')
    def test_script_payload_remains_exact(self):
        self.prepare(['pwsh','-Command','Get-Content a'])
        for script in ['Get-Content b','Get-Content a; Remove-Item b','Get-Content $x','Get-Content a\nGet-Content b']:
            self.reset();self.turn['items'][0]['command']=['pwsh','-Command',script]
            self.assertEqual(self.settle(),'unknown')
    def test_flags_order_and_argv_boundaries_must_match(self):
        self.prepare(['pwsh','-Command','Get-Content a'])
        for argv in [['pwsh','-File','Get-Content a'],['pwsh','-Command','Get-Content','a'],['pwsh','-NoProfile','-Command','Get-Content a']]:
            self.reset();self.turn['items'][0]['command']=argv
            self.assertEqual(self.settle(),'unknown')
    def test_normalized_roundtrip_keeps_raw_argv(self):
        self.prepare(['pwsh','-Command','Get-Content "a b"; echo \'$x\''])
        self.turn['items'][0]=t.command_receipt(self.turn['items'][0])
        self.assertEqual(self.settle(),'success')
    def test_malformed_and_nul_request_rejected(self):
        self.prepare(['pwsh','-Command','Get-Content a'])
        item=t.command_receipt(self.turn['items'][0])
        for value in ['pwsh "unterminated','pwsh\x00 -Command x','',None]:self.assertFalse(t.command_matches(value,item))
    def test_forged_internal_argv_rejected(self):
        item=copy.deepcopy(self.item);item['command_argv']=['different']
        self.assertIsNone(t.command_receipt(item))
    def test_conflicting_duplicate_and_boolean_code_remain_unknown(self):
        self.prepare(['pwsh','-Command','Get-Content a'])
        self.turn['items'].append(dict(self.turn['items'][0],exitCode=3))
        self.assertEqual(self.settle(),'unknown')
        self.reset();self.turn['items']=self.turn['items'][:1];self.turn['items'][0]['exitCode']=True
        self.assertEqual(self.settle(),'unknown')

    def test_unknown_formatter_version_remains_unknown(self):
        self.prepare(['pwsh','-Command','Get-Content a b'])
        # 非逐字展示必须有已核验版本；这里仅替换本地纯函数输入。
        item=t.command_receipt(self.turn['items'][0])
        self.assertFalse(t.command_matches(self.params['command'],item,producer_version='future'))
        self.assertFalse(t.command_matches(self.params['command'],item))

    def test_same_api_display_and_raw_argv_merge_in_either_order(self):
        self.prepare(['C:\\Program Files\\PowerShell\\7\\pwsh.exe','-Command','Get-Content a; Get-Content b'])
        raw=copy.deepcopy(self.turn['items'][0]);api=dict(raw,command=self.params['command'])
        for items in ([api,raw],[raw,api],[api,raw,api]):
            with self.subTest(items=items):
                self.reset();self.turn['items']=items
                self.assertEqual(self.settle(),'success')

    def test_mixed_duplicate_conflicts_are_not_overwritten(self):
        self.prepare(['pwsh','-Command','Get-Content a b'])
        raw=copy.deepcopy(self.turn['items'][0]);api=dict(raw,command=self.params['command'])
        for field,value in [('command','different script'),('exitCode',3),('status','failed'),
                            ('exitCode',False),('cwd',self.directories['B']),('type','fileChange')]:
            for items in ([api,dict(raw,**{field:value}),raw],[raw,dict(api,**{field:value}),api]):
                with self.subTest(field=field,value=value,items=items):
                    self.reset();self.turn['items']=items
                    self.assertEqual(self.settle(),'unknown')

    def test_unversioned_mixed_display_is_not_assumed_equivalent(self):
        self.prepare(['pwsh','-Command','Get-Content a b'])
        raw=copy.deepcopy(self.turn['items'][0]);self.turn['items'].append(dict(raw,command=self.params['command']))
        with self.store.transaction() as db:
            proof=json.loads(db.execute("SELECT proof FROM members WHERE id='A'").fetchone()[0]);proof['cliVersion']='future'
            value=json.dumps(proof);db.execute("UPDATE members SET proof=? WHERE id='A'",(value,))
            binding=self.store._option(db,'managed_binding:'+self.request['id']);binding['proof']=value
            self.store._put(db,'managed_binding:'+self.request['id'],binding)
        self.assertEqual(self.settle(),'unknown')

    def test_reconcile_real_full_api_plus_rollout_combination(self):
        self._reconcile_mixed()

    def test_reconcile_log_duplicates_merge_without_dropping_api(self):
        self._reconcile_mixed(duplicate_log=True)

    def test_reconcile_api_rollout_exit_code_conflict_stays_unknown(self):
        self._reconcile_mixed(conflicting_code=True)

    def test_different_raw_argv_cannot_be_bridged_by_a_success_display(self):
        self.prepare(['pwsh','-Command','Get-Content a b'])
        raw=copy.deepcopy(self.turn['items'][0]);api=dict(raw,command=self.params['command'])
        wrong=dict(raw,command=['pwsh','-Command','Get-Content c d'])
        for items in ([api,wrong,raw],[raw,wrong,api],[wrong,api,raw]):
            self.reset();self.turn['items']=items
            self.assertEqual(self.settle(),'unknown')

    def _reconcile_mixed(self,duplicate_log=False,conflicting_code=False):
        from unittest.mock import patch
        from scripts import pair_review_runtime as runtime
        self.prepare(['C:\\Program Files\\PowerShell\\7\\pwsh.exe','-Command','Get-Content a; Get-Content b'])
        raw=copy.deepcopy(self.turn['items'][0]);api=dict(raw,command=self.params['command'])
        self.store.assign('api','mixed receipts','B',['api.py'])
        with self.store.transaction() as db:db.execute("UPDATE tasks SET status='completed' WHERE id='api'")
        stage=self.store.submit_stage('B',self.sessions['B'],self.directories['B'],'api',
            {'kind':'commit','head':'b'*40},[{'member':'B','provider':'openai','model':'gpt-test'}],[],
            {'objective':'audit','acceptance':[],'environment':{},'lockfiles':[],'baseline':'a'*40})
        with self.store.transaction() as db:
            db.execute("UPDATE review_stages SET status='running',reviewer_epoch=?,reviewer_session=?,execution_id=?,turn_id='turn-one' WHERE id=?",
                (self.ticket['epoch'],self.proof['id'],self.lease['execution_id'],stage['id']))
        sessions=self.home/'sessions';sessions.mkdir(exist_ok=True);path=sessions/'mixed.jsonl'
        event=dict(raw,type='CommandExecution',exit_code=raw['exitCode']);event.pop('exitCode')
        if conflicting_code:event['exit_code']=1
        rows=[{'type':'session_meta','payload':{'id':self.proof['id'],'cwd':self.proof['cwd']}},
              {'type':'event_msg','payload':{'type':'task_started','turn_id':'turn-one'}},
              {'type':'event_msg','payload':{'type':'item_completed','thread_id':self.proof['id'],'turn_id':'turn-one','item':event}},
              {'type':'event_msg','payload':{'type':'task_complete','turn_id':'turn-one','completed_at':123}}]
        if duplicate_log:
            display=copy.deepcopy(rows[2]);display['payload']['item']=dict(api,type='CommandExecution')
            rows.insert(2,display)
        path.write_text('\n'.join(json.dumps(r) for r in rows)+'\n',encoding='utf-8')
        thread=dict(self.proof,path=str(path));turn=dict(self.turn,items=[api])
        class Observer:
            closed=False
            def __init__(self,**kw):pass
            def __enter__(self):return self
            def __exit__(self,*args):self.closed=True
            def read_thread(self,sid):return thread
            def turns(self,sid,*,items_view):
                assert not self.closed and items_view=='full'
                yield turn
        with patch.object(runtime,'AppServer',Observer):
            result=runtime.reconcile_review(self.store,*self.actor(),stage['id'],'isolated API full + actual rollout reader')
        self.assertEqual(self.status(),'unknown' if conflicting_code else 'success')
        self.assertFalse(result['work_approved'])
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT status FROM review_stages WHERE id=?',(stage['id'],)).fetchone()[0],'needs_validation')
