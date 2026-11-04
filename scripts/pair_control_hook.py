"""官方本地工具 Hook：只拦截准确绑定，异常时拒绝新的本地操作。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

try:
    from .pair_json import read_json
    from .pair_core import Store, FileLock
    from .pair_approvals import approval_package_digest
except ImportError:
    from pair_json import read_json
    from pair_core import Store, FileLock
    from pair_approvals import approval_package_digest


def main(argv=None):
    for stream in (sys.stdin,sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('pair','state-root','member','session','package-digest'):parser.add_argument('--'+name,required=True)
    parser.add_argument('--binding-version',type=int,required=True)
    parser.add_argument('--event',choices=['PreToolUse','PostToolUse','UserPromptSubmit','Stop','Interrupt'],required=True)
    args=parser.parse_args(argv)
    event=None
    try:
        event=read_json()
        if isinstance(event,dict) and event.get('session_id')!=args.session:
            print('{}');return 0
        if not isinstance(event,dict) or args.package_digest!=approval_package_digest(Path(__file__).parent):
            raise ValueError('Control package changed')
        if event.get('hook_event_name')!=args.event:raise ValueError('Control hook event differs from installed definition')
        store=Store(args.pair,state_root=args.state_root)
        timeout=0.5 if event.get('hook_event_name')=='Interrupt' else 15
        with FileLock(store.control_lock,timeout=timeout),store.transaction(timeout=timeout) as db:
            actor=store._member(db,args.member)
            if actor['session_uuid']!=args.session or actor['binding_version']!=args.binding_version or actor['lifecycle']=='retired':
                raise ValueError('Stale control binding')
            response=store._control_tool_event(db,args.member,event)
        print(json.dumps(response,ensure_ascii=False));return 0
    except Exception:
        # Stop的exit 2会要求模型继续，因此只能返回明确的停止输出。
        if args.event=='Stop':
            print(json.dumps({'continue':False,'stopReason':'codex-pair control unavailable / 控制核验失败，停止自动接续并保留原执行。'},ensure_ascii=False))
            return 0
        if args.event=='PostToolUse':
            print(json.dumps({'systemMessage':'codex-pair control record failed / 原工具结果保留；执行终态尚未核对。'},ensure_ascii=False))
            return 0
        print('codex-pair control unavailable / 控制核验失败，保留原操作并检查状态',file=sys.stderr)
        return 2


if __name__=='__main__':raise SystemExit(main())
