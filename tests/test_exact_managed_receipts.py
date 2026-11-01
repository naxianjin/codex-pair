"""只操作临时审批记录及合成官方事件，不触碰实际审核或原日志。"""
import copy
import json
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import pair_review_runtime as runtime, pair_transport as transport
from tests import test_managed_review, test_stages


class ExactReceiptTests(unittest.TestCase):
    tearDown=test_managed_review.ManagedReviewTests.tearDown
    actor=test_managed_review.ManagedReviewTests.actor
    allow=test_managed_review.ManagedReviewTests.allow

    def setUp(self):
        test_managed_review.ManagedReviewTests.setUp(self)
        # 本组只回归已验证旧格式的匹配与结算；生产者采集由专项真实Store测试覆盖。
        producer=patch.object(runtime,'stored_context',return_value='0.163.0')
        producer.start();self.addCleanup(producer.stop)
        self.params={'threadId':self.proof['id'],'turnId':'turn-one','itemId':'command-one',
                     'command':'python -m unittest','cwd':self.proof['cwd']}
        self.request=self.store.capture_managed_approval(self.ticket['epoch'],self.proof['id'],self.lease['execution_id'],
            {'id':1,'method':'item/commandExecution/requestApproval','params':self.params})
        self.allow(self.request);self.store.managed_approval_result(self.request['id'])
        self.item={'id':'command-one','type':'commandExecution','status':'completed','exitCode':0,
                   'command':self.params['command'],'cwd':self.params['cwd']}
        self.turn={'id':'turn-one','threadId':self.proof['id'],'status':'completed','completedAt':123,'items':[self.item]}

    def status(self):
        with self.store.transaction() as db:
            return db.execute('SELECT status FROM managed_permissions WHERE id=?',(self.request['id'],)).fetchone()[0]

    def settle(self,turn=None,epoch=None,execution=None):
        runtime.settle_permissions(self.store,epoch or self.ticket['epoch'],execution or self.lease['execution_id'],turn or self.turn)
        return self.status()

    def reset(self):
        with self.store.transaction() as db:db.execute("UPDATE managed_permissions SET status='granted' WHERE id=?",(self.request['id'],))

    def test_exact_api_success_and_failure(self):
        for code in (0,1,-1):
            with self.subTest(code=code):
                self.reset();turn=copy.deepcopy(self.turn);turn['items'][0]['exitCode']=code
                self.assertEqual(self.settle(turn),'success' if code==0 else 'failed')

    def test_raw_rollout_schema_and_uri(self):
        turn=copy.deepcopy(self.turn);item=turn['items'][0]
        item.update(type='CommandExecution',exit_code=item.pop('exitCode'),cwd=Path(self.proof['cwd']).as_uri())
        self.assertEqual(self.settle(turn),'success')

    def test_binding_version_parent_identity_and_execution_state_must_match(self):
        for sql,args in [
            ("UPDATE members SET binding_version=binding_version+1 WHERE id='A'",()),
            ("UPDATE members SET proof=? WHERE id='A'",(json.dumps(dict(self.proof,model='gpt-other')),)),
            ("UPDATE reviewer_runtime SET parent_session='other'",()),
            ("UPDATE reviewer_runtime SET state='retired'",()),
        ]:
            with self.subTest(sql=sql):
                with self.store.transaction() as db:
                    parent=dict(db.execute("SELECT * FROM members WHERE id='A'").fetchone())
                    reviewer=dict(db.execute('SELECT * FROM reviewer_runtime').fetchone())
                    db.execute(sql,args)
                self.reset();self.assertEqual(self.settle(),'unknown')
                with self.store.transaction() as db:
                    db.execute("UPDATE members SET binding_version=?,proof=? WHERE id='A'",(parent['binding_version'],parent['proof']))
                    db.execute('UPDATE reviewer_runtime SET parent_session=?,state=?',(reviewer['parent_session'],reviewer['state']))

    def test_legacy_binding_requires_unchanged_history(self):
        with self.store.transaction() as db:
            db.execute('DELETE FROM pair_settings WHERE key=?',('managed_binding:'+self.request['id'],))
        self.assertEqual(self.settle(),'success')
        self.reset()
        with self.store.transaction() as db:
            db.execute('INSERT INTO member_history VALUES(?,?,?,?,?)',('A',1,'{}','fixture binding changed',__import__('time').time()))
        self.assertEqual(self.settle(),'unknown')

    def test_neighbor_receipt_and_conflicting_status_cannot_authorize(self):
        turn=copy.deepcopy(self.turn);turn['items'][0]['id']='adjacent'
        self.assertEqual(self.settle(turn),'unknown')
        self.reset();turn=copy.deepcopy(self.turn);turn['items'][0]['status']='failed'
        self.assertEqual(self.settle(turn),'unknown')
        self.assertIsNone(transport.command_receipt(dict(self.item,id='')))

    def test_boolean_string_missing_and_conflicting_codes_unknown(self):
        for code in (True,False,'0',None):
            with self.subTest(code=code):
                self.reset();turn=copy.deepcopy(self.turn);turn['items'][0]['exitCode']=code
                self.assertEqual(self.settle(turn),'unknown')
        self.reset();turn=copy.deepcopy(self.turn);turn['items'][0]['exit_code']=1
        self.assertEqual(self.settle(turn),'unknown')

    def test_wrong_session_command_cwd_type_or_nonterminal_unknown(self):
        variants=[('threadId','other'),('status','inProgress'),('items',[])]
        for key,value in variants:
            with self.subTest(key=key):
                self.reset();turn=copy.deepcopy(self.turn);turn[key]=value
                self.assertEqual(self.settle(turn),'unknown')
        for key,value in [('command','different'),('cwd',self.directories['B']),('cwd','relative'),('type','agentMessage'),('id','other'),('status','inProgress')]:
            with self.subTest(item_field=key,value=value):
                self.reset();turn=copy.deepcopy(self.turn);turn['items'][0][key]=value
                self.assertEqual(self.settle(turn),'unknown')

    def test_duplicate_conflict_and_unaffected_records(self):
        turn=copy.deepcopy(self.turn);turn['items'].append(dict(self.item,exitCode=3))
        self.assertEqual(self.settle(turn),'unknown')
        self.reset();turn=copy.deepcopy(self.turn);turn['items'].append(dict(self.item))
        self.assertEqual(self.settle(turn),'success')
        self.reset();turn=dict(self.turn,id='unrelated')
        self.assertEqual(self.settle(turn),'granted')
        self.assertEqual(self.settle(epoch=self.ticket['epoch']+1),'granted')
        self.assertEqual(self.settle(execution='old-execution'),'granted')

    def test_missing_request_binding_and_failed_item_without_code_stay_unknown(self):
        for field in ('threadId','command','cwd','itemId'):
            with self.subTest(field=field):
                self.reset();params=dict(self.params);params.pop(field)
                with self.store.transaction() as db:
                    db.execute('UPDATE managed_permissions SET request=? WHERE id=?',
                        (json.dumps({'method':'item/commandExecution/requestApproval','params':params}),self.request['id']))
                self.assertEqual(self.settle(),'unknown')
        with self.store.transaction() as db:
            db.execute('UPDATE managed_permissions SET request=? WHERE id=?',
                (json.dumps({'method':'item/commandExecution/requestApproval','params':self.params}),self.request['id']))
        self.reset();turn=copy.deepcopy(self.turn);turn['items'][0].update(status='failed');turn['items'][0].pop('exitCode')
        self.assertEqual(self.settle(turn),'unknown')

    def test_exact_command_array_only_matches_explicit_serialization(self):
        import subprocess
        turn=copy.deepcopy(self.turn);turn['items'][0]['command']=['python','-m','unittest']
        if __import__('os').name=='nt':
            self.assertEqual(subprocess.list2cmdline(turn['items'][0]['command']),self.params['command'])
            self.assertEqual(self.settle(turn),'success')
        else:self.assertEqual(self.settle(turn),'unknown')

    def test_full_view_consumed_before_close_and_stage_stays_needs_validation(self):
        self.store.assign('api','test stage','B',['api.py'])
        with self.store.transaction() as db:db.execute("UPDATE tasks SET status='completed' WHERE id='api'")
        stage=self.store.submit_stage('B',self.sessions['B'],self.directories['B'],'api',
            {'kind':'commit','head':'b'*40},[{'member':'B','provider':'openai','model':'gpt-test'}],[],
            {'objective':'audit','acceptance':[],'environment':{},'lockfiles':[],'baseline':'a'*40})
        with self.store.transaction() as db:
            db.execute("UPDATE review_stages SET status='running',reviewer_epoch=?,reviewer_session=?,execution_id=?,turn_id='turn-one' WHERE id=?",
                (self.ticket['epoch'],self.proof['id'],self.lease['execution_id'],stage['id']))
        turn=self.turn;thread=dict(self.proof,path=str(self.root/'missing.jsonl'))
        class Observer:
            closed=False
            def __init__(self,**kwargs):pass
            def __enter__(self):return self
            def __exit__(self,*args):self.closed=True
            def read_thread(self,sid):return thread
            def turns(self,sid,*,items_view):
                assert items_view=='full'
                if self.closed:raise AssertionError('Lazy read after close')
                yield turn
        with patch.object(runtime,'AppServer',Observer):
            result=runtime.reconcile_review(self.store,*self.actor(),stage['id'],'exact isolated official receipt')
        self.assertFalse(result['work_approved'])
        self.assertEqual(self.status(),'success')
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT status FROM review_stages WHERE id=?',(stage['id'],)).fetchone()[0],'needs_validation')


class RolloutReceiptTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.home=self.root/'home';self.sessions=self.home/'sessions';self.sessions.mkdir(parents=True)
        self.path=self.sessions/'rollout.jsonl';self.cwd=self.root/'work';self.cwd.mkdir()
        self.member={'codex_home':str(self.home),'session_uuid':'session','cwd':str(self.cwd)}
        self.thread={'id':'session','cwd':str(self.cwd),'path':str(self.path)}
        self.item={'id':'command','type':'CommandExecution','status':'completed','exit_code':0,'command':'test','cwd':self.cwd.as_uri()}
        self.rows=[{'type':'session_meta','payload':{'id':'session','cwd':str(self.cwd)}},
            {'type':'event_msg','payload':{'type':'task_started','turn_id':'turn'}},
            {'type':'event_msg','payload':{'type':'item_completed','thread_id':'session','turn_id':'turn','item':self.item}},
            {'type':'event_msg','payload':{'type':'task_complete','turn_id':'turn','completed_at':123}}]

    def read(self,rows=None,partial=False):
        self.path.write_text('\n'.join(json.dumps(row) for row in (rows or self.rows))+('' if partial else '\n'),encoding='utf-8')
        return transport.rollout_command_items(self.member,self.thread,'turn')

    def test_exact_log_success(self):
        result=self.read();self.assertEqual(result[0]['id'],'command');self.assertEqual(result[0]['exitCode'],0)

    def test_ownership_metadata_partial_and_nonterminal_rejected(self):
        self.assertIsNone(self.read(partial=True))
        self.assertIsNone(self.read(self.rows[:-1]))
        rows=copy.deepcopy(self.rows);rows[0]['payload']['id']='wrong';self.assertIsNone(self.read(rows))
        rows=copy.deepcopy(self.rows);rows[0]['payload']['cwd']=str(self.home);self.assertIsNone(self.read(rows))
        rows=copy.deepcopy(self.rows);rows[2]['payload']['thread_id']='wrong';self.assertIsNone(self.read(rows))
        self.read();outside=self.root/'outside.jsonl';outside.write_bytes(self.path.read_bytes())
        self.thread['path']=str(outside);self.assertIsNone(transport.rollout_command_items(self.member,self.thread,'turn'))

    def test_conflicts_and_unknown_type_cannot_become_receipts(self):
        rows=copy.deepcopy(self.rows);duplicate=copy.deepcopy(rows[2]);duplicate['payload']['item']['exit_code']=1;rows.insert(3,duplicate)
        self.assertEqual(self.read(rows),[{'id':'command','type':'conflictingReceipt'}])

    def test_invalid_json_duplicate_keys_wrong_turn_and_ambiguous_paths(self):
        self.path.write_text('{"type":"session_meta","type":"event_msg"}\n',encoding='utf-8')
        self.assertIsNone(transport.rollout_command_items(self.member,self.thread,'turn'))
        self.path.write_text('[]\n',encoding='utf-8')
        self.assertIsNone(transport.rollout_command_items(self.member,self.thread,'turn'))
        rows=copy.deepcopy(self.rows);rows[2]['payload']['turn_id']='different'
        self.assertEqual(self.read(rows),[])
        rows=copy.deepcopy(self.rows);rows[2]['payload']['item']['cwd']='file://remote/share'
        self.assertEqual(self.read(rows),[{'id':'command','type':'conflictingReceipt'}])
        self.read();self.thread['path']='relative.jsonl'
        self.assertIsNone(transport.rollout_command_items(self.member,self.thread,'turn'))
        self.thread['path']=str(self.path)
        rows=copy.deepcopy(self.rows);rows[2]['payload']['item']['type']='agentMessage'
        self.assertEqual(self.read(rows),[{'id':'command','type':'conflictingReceipt'}])

    def test_oversize_and_false_terminal_stamp_rejected(self):
        with self.path.open('wb') as stream:stream.truncate(32*1024*1024+1)
        self.assertIsNone(transport.rollout_command_items(self.member,self.thread,'turn'))
        rows=copy.deepcopy(self.rows);rows[-1]['payload']['completed_at']=True
        self.assertIsNone(self.read(rows))

    def test_summary_default_full_opt_in_and_cursor_loop_rejected(self):
        observer=transport.AppServer.__new__(transport.AppServer)
        with patch.object(observer,'rpc',return_value={'data':[],'nextCursor':None}) as rpc:
            list(observer.turns('session'));self.assertEqual(rpc.call_args.args[1]['itemsView'],'summary')
            list(observer.turns('session',items_view='full'));self.assertEqual(rpc.call_args.args[1]['itemsView'],'full')
        with patch.object(observer,'rpc',return_value={'data':[],'nextCursor':'same'}):
            with self.assertRaises(transport.TransportError):list(observer.turns('session'))

    def test_disconnect_metadata_is_bounded_and_redacted(self):
        observer=transport.AppServer.__new__(transport.AppServer)
        observer._dead=True;observer._condition=threading.Condition();observer._diagnostic_stage='stdout_eof'
        observer.process=SimpleNamespace(poll=lambda:7)
        observer._stderr=['Bearer private-secret access_token=other-secret '+'x'*6000]
        with self.assertRaises(transport.TransportError) as error:observer.rpc('thread/read',{})
        self.assertNotIn('private-secret',str(error.exception));self.assertNotIn('other-secret',str(error.exception))
        self.assertIn('stdout_eof',str(error.exception));self.assertIn('7',str(error.exception));self.assertLess(len(str(error.exception)),2400)
