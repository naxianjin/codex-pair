"""只核对官方CLI原守护进程可见的控制能力；枚举存在不代表可以拦截。"""
from __future__ import annotations

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


def inspect_original_runtime(actor,configured,executable):
    """连接既有daemon，不启动/恢复线程；无法观察原执行器就保留unknown。"""
    if not configured or configured.get('session')!=actor['session_uuid'] or configured.get('binding_version')!=actor['binding_version']:
        configured=None
    try:
        with AppServer(backend='shared',codex_home=actor['codex_home'],cwd=actor['cwd'],executable=executable,
                metadata_only=True,account_metadata=True,hook_metadata=True,runtime_metadata=True,experimental_api=True) as client:
            thread=client.validate(actor)
            listing=client.rpc('hooks/list',{'threadId':actor['session_uuid']})
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
            return {'available':True,'source':'original_shared_daemon','thread_status':thread.get('status'),
                'background_terminals':terminals,'capabilities':hook_capabilities(configured,listing),
                'new_session_created':False,'processes_terminated':False}
    except (TransportError,OSError,ValueError,KeyError,TypeError) as error:
        return {'available':False,'source':'original_shared_daemon','reason':redact_diagnostic(str(error))[:1000],
            'background_terminals':None,'full_tool_control':False,'new_session_created':False,'processes_terminated':False,
            'blocker':'original_runtime_unobservable',
            'next_action':'核对原窗口是否由可连接的共享daemon承载；独立新app-server或单纯信任Hook不能证明原执行器空闲。'}
