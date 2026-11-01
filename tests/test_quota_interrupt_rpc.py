"""实际 RPC 门禁与假传输集成；不构造官方进程、不连接原窗口。"""
import copy
import json
import threading
import unittest
from unittest.mock import Mock

from scripts.pair_transport import AppServer,QueueTransport,TransportPool,TransportError,DeliveryUnknown
from tests import test_core
from scripts.pair_balance import pool_key


class QuotaInterruptRpcTests(unittest.TestCase):
    setUp=test_core.CoreTests.setUp
    tearDown=test_core.CoreTests.tearDown

    def fixture(self):
        sid=self.sessions['B'];cwd=self.directories['B']
        account={'account':{'type':'chatgpt','email':'isolated-fixture@example.invalid'}}
        member={'id':'B','backend':'queue','owned':0,'binding_version':1,'session_uuid':sid,
                'codex_home':str(self.home),'cwd':cwd,'proof':{'id':sid,'cwd':cwd,
                    'modelProvider':'openai','model':'gpt-test','accountFingerprint':pool_key(account,str(self.home))}}
        observer=object.__new__(AppServer)
        observer.backend='managed'
        observer.metadata_only=True;observer.account_metadata=True;observer.hook_metadata=False
        observer.identity={'codexHome':str(self.home)}
        observer._condition=threading.Condition();observer._dead=False;observer._next_id=0;observer._pending={};observer.timeout=.01
        sent=[]
        thread={'id':sid,'cwd':cwd,'modelProvider':'openai','model':'gpt-test','ephemeral':False,'status':{'type':'active'}}
        turns=[{'id':'exact-turn','status':'inProgress'}]
        def write(request):
            sent.append(request)
            method=request['method']
            result=({'thread':copy.deepcopy(thread)} if method=='thread/read' else account if method=='account/read' else
                    {'data':copy.deepcopy(turns),'nextCursor':None} if method=='thread/turns/list' else {})
            observer._pending[request['id']]={'id':request['id'],'result':result}
        observer._write=write
        client=object.__new__(QueueTransport);client.observer=observer
        pool=object.__new__(TransportPool);pool.client=Mock(return_value=client)
        return pool,observer,member,sent,thread,turns

    def test_exact_bound_original_turn_reaches_actual_rpc_write_once(self):
        pool,observer,member,sent,thread,turns=self.fixture()
        self.assertEqual(pool.stop_for_quota(member),{'state':'interrupt_requested','turn_id':'exact-turn'})
        interrupts=[r for r in sent if r['method']=='turn/interrupt']
        self.assertEqual([r['params'] for r in interrupts],[{'threadId':member['session_uuid'],'turnId':'exact-turn'}])
        with self.assertRaises(TransportError):pool.stop_for_quota(member)
        member['binding_version']+=1
        with self.assertRaises(TransportError):pool.stop_for_quota(member)
        self.assertEqual(len([r for r in sent if r['method']=='turn/interrupt']),1)

    def test_plain_metadata_observer_still_rejects_all_write_rpcs(self):
        _,observer,member,sent,_,_=self.fixture()
        for method in ('turn/interrupt','turn/start','thread/resume','thread/start'):
            with self.subTest(method=method),self.assertRaisesRegex(TransportError,'Metadata-only'):
                observer.rpc(method,{'threadId':member['session_uuid'],'turnId':'exact-turn'})
        self.assertEqual(sent,[])

    def test_wrong_binding_and_ambiguous_or_idle_turn_never_write_interrupt(self):
        for field,value in [('session_uuid',self.sessions['A']),('cwd',self.directories['A']),
                            ('codex_home',str(self.root)),('backend','managed'),('owned',1),('binding_version',None)]:
            with self.subTest(field=field):
                pool,observer,member,sent,_,_=self.fixture();member[field]=value
                with self.assertRaises(TransportError):pool.stop_for_quota(member)
                self.assertFalse(any(r['method']=='turn/interrupt' for r in sent))
        for field,value in [('accountFingerprint','wrong-account'),('model','other-model'),('modelProvider','other-provider'),('id',self.sessions['A'])]:
            with self.subTest(proof_field=field):
                pool,observer,member,sent,_,_=self.fixture();member['proof'][field]=value
                with self.assertRaises(TransportError):pool.stop_for_quota(member)
                self.assertFalse(any(r['method']=='turn/interrupt' for r in sent))
        for status,active in [('idle',[]),('active',[]),('active',[{'id':'a','status':'inProgress'},{'id':'b','status':'inProgress'}])]:
            with self.subTest(status=status,active=active):
                pool,observer,member,sent,thread,turns=self.fixture();thread['status']['type']=status;turns[:]=active
                if status=='idle':self.assertEqual(pool.stop_for_quota(member),{'state':'idle'})
                else:
                    with self.assertRaises(TransportError):pool.stop_for_quota(member)
                self.assertFalse(any(r['method']=='turn/interrupt' for r in sent))

    def test_unknown_rpc_does_not_replay_and_clears_scoped_capability(self):
        pool,observer,member,sent,_,_=self.fixture();write=observer._write
        def disconnected(request):
            if request['method']=='turn/interrupt':
                sent.append(request);raise OSError('isolated write outcome unknown')
            write(request)
        observer._write=disconnected
        with self.assertRaises(DeliveryUnknown):pool.stop_for_quota(member)
        with self.assertRaises(TransportError):pool.stop_for_quota(member)
        with self.assertRaisesRegex(TransportError,'Metadata-only'):
            observer.rpc('turn/interrupt',{'threadId':member['session_uuid'],'turnId':'exact-turn'})
        self.assertEqual(len([r for r in sent if r['method']=='turn/interrupt']),1)
