"""由官方受信 PermissionRequest Hook 运行；不执行待审批命令、不改 B 权限。"""
from __future__ import annotations

import argparse
import json
import sys
import time

try:
    from .pair_core import Store
    from .pair_approvals import hook_response,approval_package_digest
except ImportError:
    from pair_core import Store
    from pair_approvals import hook_response,approval_package_digest


def run_hook(store,member,event,*,timeout=180,sleep=time.sleep):
    # 全局配置可能匹配其他会话；这些会话不属于委托，交回各自正常审批。
    if not isinstance(event,dict) or event.get('session_id')!=store.member(member)['session_uuid']:
        return {}
    request=store.capture_approval(member,event,timeout=timeout)
    deadline=time.monotonic()+timeout
    while True:
        result=store.approval_outcome(member,event['session_id'],request['id'],request['digest'])
        if result['status']=='allowed':return hook_response('allow')
        if result['status'] in ('denied','expired','granted','success','failed','unknown'):return hook_response('deny',result['reason'])
        if time.monotonic()>=deadline:return hook_response('deny','A review timed out / A 审核超时，未批准此操作')
        sleep(.2)


def main(argv=None):
    for stream in (sys.stdin,sys.stdout):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pair',required=True)
    parser.add_argument('--state-root',required=True)
    parser.add_argument('--member',choices=['B'],required=True)
    parser.add_argument('--timeout',type=int,default=180)
    parser.add_argument('--package-digest')
    parser.add_argument('--session')
    try:
        args=parser.parse_args(argv)
        event=json.load(sys.stdin)
        if args.session and isinstance(event,dict) and event.get('session_id')!=args.session:
            print('{}')
            return 0
        from pathlib import Path
        if not args.package_digest or args.package_digest!=approval_package_digest(Path(__file__).parent):
            raise ValueError('Hook package changed / 审批代码已变化，重新安装并核验信任后再使用')
        store=Store(args.pair,state_root=args.state_root)
        output=run_hook(store,args.member,event,timeout=args.timeout)
    except Exception:
        # 不回落为 allow，也不因回调异常继续弹出用户审批；不输出可能带密钥的异常正文。
        output=hook_response('deny','Approval relay unavailable / A 审批通道不可用，本次操作未获批准')
    print(json.dumps(output,ensure_ascii=False))
    return 0


if __name__=='__main__':raise SystemExit(main())
