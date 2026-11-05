"""官方接口范围的行为规格；候选验收延期，本轮只做静态检查。"""
import contextlib
import io
import json
import unittest
from unittest.mock import patch

from scripts.pair_control_hook import main
from scripts.pair_native_capabilities import CONTROL_EVENTS,hook_capabilities,inspect_original_runtime
from scripts.pair_transport import AppServer,TransportError
from tests.test_core import CoreTests


class NativeControlTests(unittest.TestCase):
    setUp=CoreTests.setUp
    tearDown=CoreTests.tearDown

    def event(self,kind,**fields):
        return {'session_id':self.sessions['B'],'turn_id':'fixture-turn','hook_event_name':kind,**fields}

    def prompt(self):
        sent=self.store.send('A','B','已授权的原消息',dedupe_key='native-prompt')
        actor=self.store.member('B')
        with self.store.transaction() as db:message=dict(db.execute('SELECT * FROM messages WHERE id=?',(sent['id'],)).fetchone())
        text=self.store.envelope(actor,message)
        with self.store.transaction() as db:db.execute('UPDATE messages SET envelope=? WHERE id=?',(text,sent['id']))
        return sent['id'],text

    def paused(self):
        self.store.request_pause('A',self.sessions['A'],self.directories['A'],reason='用户暂停',idempotency_key='native-pause')

    def test_control_connection_binding_requires_original_probe_and_keeps_generation(self):
        import tempfile
        from pathlib import Path
        actor=self.store.member('A')
        with tempfile.TemporaryDirectory() as root:
            connection=Path(root)/'connection.json';connection.write_text('{}')
            with self.store.transaction() as db:
                self.store._put(db,'cli_trusted_executable:B',{'file':{'sha256':'fixture'}})
                generation=self.store._control_generation(db)
            with patch('scripts.pair_producer.file_identity',return_value={'sha256':'fixture'}),patch(
                    'scripts.pair_native_capabilities.inspect_original_runtime',return_value={'available':False}):
                with self.assertRaisesRegex(Exception,'not verified'):
                    self.store.bind_control_connection('A',actor['session_uuid'],actor['cwd'],'fixture',target='B',connection_file=str(connection))
            with patch('scripts.pair_producer.file_identity',return_value={'sha256':'fixture'}),patch(
                    'scripts.pair_native_capabilities.inspect_original_runtime',return_value={'available':True}):
                result=self.store.bind_control_connection('A',actor['session_uuid'],actor['cwd'],'fixture',target='B',connection_file=str(connection))
            self.assertFalse(result['full_tool_control'])
            with self.store.transaction() as db:
                self.assertEqual(self.store._control_generation(db),generation)
                self.assertEqual(self.store._option(db,'control_connection:B')['session'],self.sessions['B'])

    def test_original_queued_message_is_blocked_after_pause(self):
        key,text=self.prompt();self.paused()
        with self.store.transaction() as db:
            result=self.store._control_tool_event(db,'B',self.event('UserPromptSubmit',prompt=text))
            self.assertEqual(result['decision'],'block')
            self.assertEqual(db.execute('SELECT process_status FROM messages WHERE id=?',(key,)).fetchone()[0],'unclaimed')

    def test_current_queued_message_keeps_original_claim_contract(self):
        key,text=self.prompt()
        with self.store.transaction() as db:
            self.assertEqual(self.store._control_tool_event(db,'B',self.event('UserPromptSubmit',prompt=text)),{})
            self.assertIsNone(db.execute('SELECT claim_token FROM messages WHERE id=?',(key,)).fetchone()[0])

    def test_stale_member_queue_message_is_blocked(self):
        _,text=self.prompt()
        with self.store.transaction() as db:
            db.execute("UPDATE members SET binding_version=binding_version+1 WHERE id='B'")
            self.assertEqual(self.store._control_tool_event(db,'B',self.event('UserPromptSubmit',prompt=text))['decision'],'block')

    def test_completed_message_cannot_start_again(self):
        key,text=self.prompt()
        with self.store.transaction() as db:
            db.execute("UPDATE messages SET process_status='completed' WHERE id=?",(key,))
            self.assertEqual(self.store._control_tool_event(db,'B',self.event('UserPromptSubmit',prompt=text))['decision'],'block')

    def test_unrelated_human_prompt_does_not_grant_resume(self):
        self.paused()
        with self.store.transaction() as db:
            self.assertEqual(self.store._control_tool_event(db,'B',self.event('UserPromptSubmit',prompt='继续')), {})
            self.assertEqual(db.execute('SELECT status FROM collaborations').fetchone()[0],'paused')
            self.assertEqual(db.execute("SELECT count(*) FROM control_requests WHERE kind='resume'").fetchone()[0],0)

    def test_post_tool_notice_preserves_original_result_and_unproven_background(self):
        event=self.event('PreToolUse',tool_use_id='original-tool',tool_name='Bash',tool_input={'command':'fixture'})
        with self.store.transaction() as db:self.store._control_tool_event(db,'B',event)
        self.paused()
        with self.store.transaction() as db:
            result=self.store._control_tool_event(db,'B',dict(event,hook_event_name='PostToolUse'))
            self.assertIn('systemMessage',result);self.assertNotIn('continue',result);self.assertNotIn('decision',result)
            self.assertEqual(db.execute('SELECT state FROM control_requests').fetchone()[0],'pausing')

    def test_stop_hook_never_requests_a_new_continuation_on_pause(self):
        self.paused()
        with self.store.transaction() as db:
            result=self.store._control_tool_event(db,'B',self.event('Stop'))
            self.assertIs(result['continue'],False);self.assertNotIn('decision',result)

    def test_stop_handler_failure_returns_stop_not_exit_two(self):
        arguments=['--pair','test','--state-root',str(self.store.state_root),'--member','B',
            '--session',self.sessions['B'],'--binding-version','1','--package-digest','a'*64,'--event','Stop']
        output=io.StringIO()
        with (patch('scripts.pair_control_hook.read_json',return_value=self.event('Stop')),
             patch('scripts.pair_control_hook.approval_package_digest',side_effect=OSError('fixture failure')),
             contextlib.redirect_stdout(output)):
            self.assertEqual(main(arguments),0)
        self.assertIs(json.loads(output.getvalue())['continue'],False)

    def test_invalid_stop_stdin_does_not_request_continuation(self):
        arguments=['--pair','test','--state-root',str(self.store.state_root),'--member','B',
            '--session',self.sessions['B'],'--binding-version','1','--package-digest','a'*64,'--event','Stop']
        output=io.StringIO()
        with patch('scripts.pair_control_hook.read_json',side_effect=ValueError('invalid JSON')),contextlib.redirect_stdout(output):
            self.assertEqual(main(arguments),0)
        self.assertIs(json.loads(output.getvalue())['continue'],False)

    def test_interrupt_install_uses_three_second_limit(self):
        self.store.install_control_hooks('A',self.sessions['A'],self.directories['A'],'B')
        data=json.loads((self.home/'hooks.json').read_text(encoding='utf-8'))
        self.assertEqual(data['hooks']['Interrupt'][0]['hooks'][0]['timeout'],3)
        self.assertNotIn('PreModelRequest',data['hooks'])


class CapabilityDefinitionTests(unittest.TestCase):
    def listing(self):
        configured={'command':'exact-command','events':{event:'marker:'+event for event in CONTROL_EVENTS}}
        hooks=[{'statusMessage':configured['events'][event],'eventName':wire,'command':'exact-command',
            'handlerType':'command','enabled':True,'trustStatus':'trusted','async':False,'currentHash':'hash:'+event,'matcher':'*'}
            for event,wire in CONTROL_EVENTS.items()]
        return configured,{'data':[{'hooks':hooks,'errors':[],'warnings':[],'cwd':'fixture'}]}

    def test_trusted_local_hooks_never_become_full_tool_control(self):
        configured,listing=self.listing();result=hook_capabilities(configured,listing)
        self.assertTrue(result['local_tool_definition_trusted']);self.assertFalse(result['full_tool_control'])
        self.assertEqual(result['model_request_control'],'not_supported_by_observation_hook')

    def test_async_hook_does_not_count_as_blocking_hook(self):
        configured,listing=self.listing();listing['data'][0]['hooks'][0]['async']=True
        self.assertFalse(hook_capabilities(configured,listing)['local_tool_definition_trusted'])

    def test_wrong_event_with_matching_label_is_not_trusted(self):
        configured,listing=self.listing();listing['data'][0]['hooks'][0]['eventName']='preModelRequest'
        self.assertFalse(hook_capabilities(configured,listing)['local_tool_definition_trusted'])

    def test_shell_only_matcher_is_not_reported_as_all_local_tools(self):
        configured,listing=self.listing();listing['data'][0]['hooks'][0]['matcher']='Bash'
        self.assertFalse(hook_capabilities(configured,listing)['local_tool_definition_trusted'])

    def test_duplicate_definition_is_not_silently_selected(self):
        configured,listing=self.listing();listing['data'][0]['hooks'].append(dict(listing['data'][0]['hooks'][0]))
        self.assertFalse(hook_capabilities(configured,listing)['local_tool_definition_trusted'])

    def test_missing_original_daemon_does_not_fall_back_to_empty_new_server(self):
        actor={'session_uuid':'fixture-session','codex_home':'fixture-home','cwd':'fixture-cwd','binding_version':1}
        with patch('scripts.pair_native_capabilities.AppServer',side_effect=TransportError('Original daemon unavailable')) as factory:
            result=inspect_original_runtime(actor,None,'fixture-exe')
        self.assertFalse(result['available']);self.assertIsNone(result['background_terminals'])
        self.assertEqual(result['blocker'],'original_runtime_unobservable')
        self.assertIn('不能证明',result['next_action'])
        self.assertEqual(factory.call_count,1);self.assertEqual(factory.call_args.kwargs['backend'],'shared')

    def test_new_managed_process_cannot_serve_as_original_runtime_observer(self):
        with self.assertRaisesRegex(TransportError,'Original runtime metadata'):
            AppServer(backend='managed',metadata_only=True,runtime_metadata=True)

    def test_original_terminal_pages_preserve_ids_without_command_text(self):
        actor={'session_uuid':'fixture-session','codex_home':'fixture-home','cwd':'fixture-cwd','binding_version':1}
        requests=[]
        class OriginalDaemon:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def validate(self,bound):return {'id':bound['session_uuid'],'status':{'type':'idle'}}
            def rpc(self,method,params):
                requests.append((method,params))
                if method=='hooks/list':return {'data':[]}
                if params.get('cursor')=='page-2':return {'data':[],'nextCursor':None}
                return {'data':[{'processId':'p1','itemId':'i1','osPid':123,'command':'private-command-body'}],'nextCursor':'page-2'}
        with patch('scripts.pair_native_capabilities.AppServer',return_value=OriginalDaemon()) as factory:
            result=inspect_original_runtime(actor,None,'fixture-exe')
        self.assertTrue(result['available']);self.assertNotIn('private-command-body',json.dumps(result))
        self.assertTrue(factory.call_args.kwargs['experimental_api'])
        self.assertEqual(requests[0],('hooks/list',{'threadId':'fixture-session'}))
        self.assertEqual(requests[-1][1]['cursor'],'page-2')

    def test_invalid_top_level_hook_entries_remain_unobservable(self):
        actor={'session_uuid':'fixture-session','codex_home':'fixture-home','cwd':'fixture-cwd','binding_version':1}
        from unittest.mock import MagicMock
        for listing in ({'hooks':[1]},{'hooks':None},[]):
            client=MagicMock()
            client.__enter__.return_value=client
            client.validate.return_value={'status':{'type':'idle'}}
            client.rpc.return_value=listing
            with patch('scripts.pair_native_capabilities.AppServer',return_value=client):
                result=inspect_original_runtime(actor,None,'fixture-exe')
            self.assertFalse(result['available'])

    def test_repeating_terminal_cursor_remains_unknown(self):
        actor={'session_uuid':'fixture-session','codex_home':'fixture-home','cwd':'fixture-cwd','binding_version':1}
        class OriginalDaemon:
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def validate(self,bound):return {'status':{'type':'idle'}}
            def rpc(self,method,params):
                return {'data':[]} if method=='hooks/list' else {'data':[],'nextCursor':'same'}
        with patch('scripts.pair_native_capabilities.AppServer',return_value=OriginalDaemon()):
            result=inspect_original_runtime(actor,None,'fixture-exe')
        self.assertFalse(result['available']);self.assertIsNone(result['background_terminals'])
        self.assertEqual(result['blocker'],'original_runtime_unobservable')
        self.assertIn('不能证明',result['next_action'])


class OriginalWebSocketTests(unittest.TestCase):
    def test_binding_rejects_wrong_session_and_non_loopback_before_connect(self):
        import tempfile
        from pathlib import Path
        from scripts.pair_native_capabilities import OriginalWebSocket
        actor={'session_uuid':'original','codex_home':str(Path.cwd())}
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'connection.json'
            for session,endpoint in [('wrong','ws://127.0.0.1:1234'),('original','ws://example.com:1234')]:
                path.write_text(json.dumps({'session':session,'codex_home':actor['codex_home'],'endpoint':endpoint,'token_file':'unused'}),encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'binding mismatch'):
                    OriginalWebSocket(actor,path)

    def test_read_only_rpc_rejects_model_and_approval_operations(self):
        from scripts.pair_native_capabilities import OriginalWebSocket
        observer=OriginalWebSocket.__new__(OriginalWebSocket)
        for method in ('thread/resume','turn/start','turn/interrupt','item/commandExecution/requestApproval'):
            with self.assertRaisesRegex(TransportError,'read-only'):
                observer.rpc(method,{})


    def test_malformed_response_and_notification_deadline_fail_closed(self):
        from unittest.mock import Mock
        from scripts.pair_native_capabilities import OriginalWebSocket
        observer=OriginalWebSocket.__new__(OriginalWebSocket)
        observer.sequence=0;observer.socket=Mock()
        observer.socket.recv.return_value='[]'
        with self.assertRaisesRegex(TransportError,'Invalid original'):
            observer.rpc('thread/read',{})
        observer.socket.recv.return_value='{"method":"notice"}'
        with patch('scripts.pair_native_capabilities.time.monotonic',side_effect=[0,1,2,16]):
            with self.assertRaisesRegex(TransportError,'deadline'):
                observer.rpc('thread/read',{})


    def test_response_received_after_deadline_is_not_accepted(self):
        from unittest.mock import Mock
        from scripts.pair_native_capabilities import OriginalWebSocket
        observer=OriginalWebSocket.__new__(OriginalWebSocket)
        observer.sequence=0;observer.socket=Mock()
        observer.socket.recv.return_value='{"id":1,"result":{}}'
        with patch('scripts.pair_native_capabilities.time.monotonic',side_effect=[0,1,16]):
            with self.assertRaisesRegex(TransportError,'deadline'):
                observer.rpc('thread/read',{})


    def test_watchdog_aborts_blocked_receive_without_stopping_server(self):
        import threading,time
        import websocket
        from scripts.pair_native_capabilities import OriginalWebSocket
        observer=OriginalWebSocket.__new__(OriginalWebSocket);observer.sequence=0
        ended=threading.Event()
        class BlockedSocket:
            def send(self,value):pass
            def settimeout(self,value):pass
            def abort(self):ended.set()
            def recv(self):
                if not ended.wait(1):raise AssertionError('watchdog did not abort')
                raise websocket.WebSocketConnectionClosedException('aborted')
        observer.socket=BlockedSocket()
        timer=threading.Timer
        with patch('scripts.pair_native_capabilities.threading.Timer',side_effect=lambda delay,callback:timer(.02,callback)):
            with self.assertRaisesRegex(TransportError,'result unknown'):
                observer.rpc('thread/read',{})
        self.assertTrue(ended.is_set())


    def test_invalid_nested_thread_status_is_rejected(self):
        from unittest.mock import Mock
        from scripts.pair_native_capabilities import OriginalWebSocket
        observer=OriginalWebSocket.__new__(OriginalWebSocket)
        observer.rpc=Mock(return_value={'thread':{'status':[]}})
        with self.assertRaisesRegex(TransportError,'thread state'):
            observer.read_thread('original')

    def test_exit_shuts_down_observer_without_close_handshake(self):
        from unittest.mock import Mock
        from scripts.pair_native_capabilities import OriginalWebSocket
        observer=OriginalWebSocket.__new__(OriginalWebSocket);observer.socket=Mock()
        observer.__exit__()
        observer.socket.shutdown.assert_called_once_with()
        observer.socket.close.assert_not_called()

    def test_invalid_account_and_boolean_response_id_are_rejected(self):
        from unittest.mock import Mock
        from scripts.pair_native_capabilities import OriginalWebSocket
        for response, message in [({'id':1,'result':{'account':['invalid']}},'account response'),
                                  ({'id':True,'result':{}},'response ID')]:
            with self.subTest(response=response):
                observer=OriginalWebSocket.__new__(OriginalWebSocket)
                observer.sequence=0;observer.socket=Mock()
                observer.socket.recv.return_value=json.dumps(response)
                with self.assertRaisesRegex(TransportError,message):
                    observer.rpc('account/read',{})

    def test_invalid_connection_field_types_fail_before_connect(self):
        import tempfile
        from pathlib import Path
        from scripts.pair_native_capabilities import OriginalWebSocket
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'connection.json'
            for record in ([], {'endpoint':123}):
                path.write_text(json.dumps(record),encoding='utf-8')
                with self.assertRaisesRegex(ValueError,'connection record'):
                    OriginalWebSocket({},path)

    def test_invalid_false_cursors_remain_unobservable(self):
        from unittest.mock import MagicMock
        actor={'session_uuid':'original','binding_version':1,'codex_home':'unused','cwd':'unused'}
        for cursor in (0,False,[],{},''):
            with self.subTest(cursor=cursor):
                client=MagicMock();client.__enter__.return_value=client
                client.validate.return_value={'status':{'type':'idle'}}
                client.rpc.side_effect=[{'data':[]},{'data':[],'nextCursor':cursor}]
                with patch('scripts.pair_native_capabilities.OriginalWebSocket',return_value=client):
                    result=inspect_original_runtime(actor,None,'unused',connection_file='explicit')
                self.assertFalse(result['available'])
                self.assertIsNone(result['background_terminals'])
