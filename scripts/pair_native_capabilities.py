"""只核对官方CLI原守护进程可见的控制能力；枚举存在不代表可以拦截。"""
from __future__ import annotations

import json
import time
import threading
from pathlib import Path
from urllib.parse import urlsplit

try:
    from .pair_transport import AppServer, TransportError, redact_diagnostic
except ImportError:
    from pair_transport import AppServer, TransportError, redact_diagnostic


CONTROL_EVENTS={'PreToolUse':'preToolUse','PostToolUse':'postToolUse',
                'UserPromptSubmit':'userPromptSubmit','Stop':'stop','Interrupt':'interrupt'}


def hook_capabilities(configured,listing):
    hooks=[hook for entry in listing.get('data',[]) for hook in entry.get('hooks',[])]+listing.get('hooks',[])
    errors=any(entry.get('errors') for entry in listing.get('data',[]))
    events={}
    for event,wire in CONTROL_EVENTS.items():
        marker=(configured or {}).get('events',{}).get(event)
        matches=[hook for hook in hooks if marker and hook.get('statusMessage')==marker]
        exact=bool(len(matches)==1 and matches[0].get('eventName')==wire
            and matches[0].get('command')==(configured or {}).get('commands',{}).get(event,(configured or {}).get('command'))
            and (event not in ('PreToolUse','PostToolUse') or matches[0].get('matcher')=='*')
            and matches[0].get('handlerType')=='command' and not matches[0].get('async',False)
            and matches[0].get('currentHash') and matches[0].get('enabled')
            and matches[0].get('trustStatus') in ('trusted','managed') and not errors)
        events[event]={'state':'trusted_definition' if exact else 'missing_modified_or_untrusted',
                       'behavior_validated':False,'current_hash':matches[0].get('currentHash') if exact else None}
    return {'events':events,'local_tool_definition_trusted':events['PreToolUse']['state']=='trusted_definition',
        'queued_message_definition_trusted':events['UserPromptSubmit']['state']=='trusted_definition',
        'full_tool_control':False,'model_request_control':'not_supported_by_observation_hook',
        'uncovered':['hosted_tools','existing_unified_exec_input','specialized_unhooked_tools'],
        'permission_policy_changed':False}


def inspect_original_runtime(actor,configured,executable,*,connection_file=None):
    """连接既有daemon，不启动/恢复线程；无法观察原执行器就保留unknown。"""
    if not configured or configured.get('session')!=actor['session_uuid'] or configured.get('binding_version')!=actor['binding_version']:
        configured=None
    source='original_websocket' if connection_file else 'original_shared_daemon'
    try:
        client=OriginalWebSocket(actor,connection_file) if connection_file else AppServer(
            backend='shared',codex_home=actor['codex_home'],cwd=actor['cwd'],executable=executable,
            metadata_only=True,account_metadata=True,hook_metadata=True,runtime_metadata=True,experimental_api=True)
        with client:
            thread=client.validate(actor)
            listing=client.rpc('hooks/list',{'threadId':actor['session_uuid']})
            if not isinstance(listing,dict):raise TransportError('Invalid original hook listing')
            if not isinstance(listing.get('hooks',[]),list) or any(not isinstance(hook,dict) for hook in listing.get('hooks',[])):raise TransportError('Invalid original hook listing')
            if not isinstance(listing.get('data',[]),list) or any(not isinstance(entry,dict) or not isinstance(entry.get('hooks',[]),list) or any(not isinstance(hook,dict) for hook in entry.get('hooks',[])) for entry in listing.get('data',[])):
                raise TransportError('Invalid original hook listing')
            terminals=[];cursor=None;seen=set()
            while True:
                params={'threadId':actor['session_uuid'],'limit':100}
                if cursor:params['cursor']=cursor
                page=client.rpc('thread/backgroundTerminals/list',params)
                if not isinstance(page.get('data'),list):raise TransportError('Invalid background terminal response')
                for item in page['data']:
                    if not isinstance(item,dict) or not all(isinstance(item.get(key),str) and item[key] for key in ('processId','itemId')):
                        raise TransportError('Incomplete background terminal identity')
                    terminals.append({key:item.get(key) for key in ('processId','itemId','osPid')})
                cursor=page.get('nextCursor')
                if not cursor:break
                if not isinstance(cursor,str) or cursor in seen:raise TransportError('Repeated background terminal cursor')
                seen.add(cursor)
            return {'available':True,'source':source,'thread_status':thread.get('status'),
                'background_terminals':terminals,'capabilities':hook_capabilities(configured,listing),
                'new_session_created':False,'processes_terminated':False}
    except (TransportError,OSError,ValueError,KeyError,TypeError) as error:
        return {'available':False,'source':source,'reason':redact_diagnostic(str(error))[:1000],
            'background_terminals':None,'full_tool_control':False,'new_session_created':False,'processes_terminated':False,
            'blocker':'original_runtime_unobservable',
            'next_action':'核对原窗口是否由可连接的共享daemon承载；独立新app-server或单纯信任Hook不能证明原执行器空闲。'}


class OriginalWebSocket:
    """只连接用户明确指定的本地服务，不创建或恢复原会话。"""
    backend='shared'
    account_metadata=True
    validate=AppServer.validate

    def __init__(self,actor,connection_file):
        try:from .pair_transport import canonical_path
        except ImportError:from pair_transport import canonical_path
        record=json.loads(Path(connection_file).read_text(encoding='utf-8-sig'))
        endpoint=urlsplit(record['endpoint'])
        if (record['session']!=actor['session_uuid']
                or canonical_path(record['codex_home'])!=canonical_path(actor['codex_home'])
                or endpoint.scheme!='ws' or endpoint.hostname!='127.0.0.1' or not endpoint.port
                or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment
                or endpoint.path not in ('','/')):
            raise ValueError('Original WebSocket binding mismatch')
        try:
            import websocket
        except ImportError as error:
            raise TransportError('WebSocket observer requires websocket-client') from error
        token=Path(record['token_file']).read_text(encoding='utf-8').strip()
        if not token or any(c in token for c in '\r\n'):
            raise ValueError('Invalid local capability token')
        try:
            self.socket=websocket.create_connection(record['endpoint'],header=['Authorization: Bearer '+token],
                suppress_origin=True,timeout=15,redirect_limit=0,http_no_proxy=['127.0.0.1'])
            if self.socket.getstatus()!=101:
                self.socket.shutdown()
                raise TransportError('Original WebSocket redirect refused')
        except (websocket.WebSocketException,OSError) as error:
            raise TransportError('Original WebSocket connection failed') from error
        self.sequence=0
        try:
            identity=self.rpc('initialize',{'clientInfo':{'name':'codex_pair_observer','version':'1'},
                'capabilities':{'experimentalApi':True}})
            if canonical_path(identity['codexHome'])!=canonical_path(actor['codex_home']):
                raise ValueError('Original WebSocket CODEX_HOME mismatch')
            self.socket.send(json.dumps({'method':'initialized','params':{}}))
        except BaseException:
            self.socket.shutdown()
            raise

    def read_thread(self,session):
        thread=self.rpc('thread/read',{'threadId':session,'includeTurns':False}).get('thread')
        if not isinstance(thread,dict) or not isinstance(thread.get('status'),dict):
            raise TransportError('Invalid original thread state')
        return thread

    def rpc(self,method,params):
        if method not in ('initialize','thread/read','hooks/list','thread/backgroundTerminals/list','account/read'):
            raise TransportError('Original WebSocket observer is read-only')
        import websocket
        self.sequence+=1
        deadline=time.monotonic()+15
        watchdog=threading.Timer(15,self.socket.abort)
        watchdog.daemon=True
        watchdog.start()
        try:
            self.socket.send(json.dumps({'id':self.sequence,'method':method,'params':params}))
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0:raise TransportError('Original WebSocket RPC deadline exceeded')
                self.socket.settimeout(remaining)
                response=json.loads(self.socket.recv())
                if time.monotonic()>=deadline:raise TransportError('Original WebSocket RPC deadline exceeded')
                if not isinstance(response,dict):raise TransportError('Invalid original WebSocket response')
                if 'method' in response and 'id' in response:
                    raise TransportError('Unexpected request on read-only WebSocket')
                if 'id' in response and type(response['id']) is not int:
                    raise TransportError('Invalid original WebSocket response ID')
                if response.get('id')==self.sequence:
                    if 'error' in response:raise TransportError('Original WebSocket metadata RPC failed')
                    if not isinstance(response.get('result'),dict):raise TransportError('Invalid original WebSocket result')
                    result=response['result']
                    if method=='account/read' and result.get('account') is not None and not isinstance(result['account'],dict):
                        raise TransportError('Invalid original account response')
                    return result
        except (websocket.WebSocketException,OSError) as error:
            raise TransportError('Original WebSocket metadata result unknown') from error
        finally:
            watchdog.cancel()

    def __enter__(self):return self
    def __exit__(self,*args):self.socket.shutdown()
