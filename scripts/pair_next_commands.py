"""schema 14 候选公开入口；输入字段由实际方法签名约束。"""
from __future__ import annotations

import inspect
import re

try:
    from .pair_json import read_json
    from .pair_producer import strict_display
except ImportError:
    from pair_json import read_json
    from pair_producer import strict_display


COMMANDS = {
    'inspect-control-capabilities':'inspect_control_capabilities',
    'apply-recovery-runtime':'apply_recovery_runtime',
    'reconcile-runtime-application':'reconcile_runtime_application',
    'read-hidden-handoff':'read_hidden_handoff',
    'accept-hidden-handoff':'accept_hidden_handoff',
    'reconcile-hidden-handoff':'reconcile_hidden_handoff',
    'refresh-hidden-handoff':'refresh_hidden_handoff',
    'install-control-hooks':'install_control_hooks',
    'control-runtime-status':'control_runtime_status',
    'rebaseline-control-source':'rebaseline_control_source',
    'renew-review-recovery':'renew_review_recovery',
    'register-recovery-relation':'register_recovery_relation',
    'import-recovery-history':'import_recovery_history',
    'refresh-cli-compatibility': 'refresh_cli_compatibility',
    'cli-compatibility-status': 'cli_compatibility_status',
    'inspect-cli-execution': 'inspect_cli_execution',
    'inspect-remaining-work': 'inspect_remaining_work',
    'continue-remaining-work': 'continue_remaining_work',
    'supersede-obligations': 'supersede_obligations',
    'recover': 'recover_safe',
    'quota-consent-status': 'quota_consent_status',
    'confirm-pause': 'confirm_pause',
    'auxiliary-policy': 'auxiliary_policy',
    'auxiliary-quota-policy': 'auxiliary_quota_policy',
    'prepare-auxiliary-quota-consent': 'prepare_auxiliary_quota_consent',
    'retire-auxiliary': 'retire_auxiliary',
    'allow-auxiliary-config': 'allow_auxiliary_config',
    'register-audit': 'register_audit',
    'queue-auxiliary': 'queue_auxiliary',
    'reserve-auxiliary': 'reserve_auxiliary',
    'begin-auxiliary': 'begin_auxiliary',
    'auxiliary-unknown': 'auxiliary_unknown',
    'cancel-auxiliary-reservation': 'cancel_auxiliary_reservation',
    'reconcile-auxiliary': 'reconcile_auxiliary',
    'wait-auxiliary': 'wait_auxiliary',
    'finish-auxiliary': 'finish_auxiliary',
    'settle-audit': 'settle_audit',
    'auxiliary-status': 'auxiliary_status',
    'recovery-policy': 'recovery_policy',
    'request-pause': 'request_pause',
    'request-resume': 'request_resume',
    'reconcile-resume': 'reconcile_resume',
    'recovery-status': 'recovery_status',
    'propose-review-recovery': 'propose_review_recovery',
    'read-review-recovery': 'read_review_recovery',
    'authorize-review-recovery': 'authorize_review_recovery',
    'claim-review-recovery': 'claim_review_recovery',
    'recover-review-claim': 'recover_review_claim',
    'begin-review-recovery': 'begin_review_recovery',
    'finish-review-recovery': 'finish_review_recovery',
    'reconcile-review-recovery': 'reconcile_review_recovery',
    'revoke-review-recovery': 'revoke_review_recovery',
    'grant-recovery-delivery-access': 'grant_recovery_delivery_access',
    'prepare-recovery-application': 'prepare_recovery_application',
    'begin-recovery-application': 'begin_recovery_application',
    'reconcile-recovery-application': 'reconcile_recovery_application',
}


def register(command, actor):
    cmd=command('watchdog','运行单层本地桥守护，不启动模型、不设开机自启')
    cmd.add_argument('--action',choices=['configure','run','stop','tick'],required=True)
    actor(cmd)
    cmd.add_argument('--python-path')
    cmd.add_argument('--cli-path')
    for name in ('doctor', 'diagnose-blockers'):
        cmd = command(name, '严格只读阻塞快照，不刷新、迁移、写审计或启动模型')
        cmd.add_argument('--task', help='聚焦任务及其依赖，历史项另列')
    for name in COMMANDS:
        cmd = command(name, '下一版控制与恢复：'+name)
        actor(cmd)
        if name.endswith('-status'):
            continue
        if name=='recover':cmd.add_argument('--safe',action='store_true',required=True)
        group = cmd.add_mutually_exclusive_group(required=name not in ('recover','refresh-cli-compatibility'))
        group.add_argument('--input-file', help='UTF-8 JSON；字段见 references/recovery-v14.md')
        group.add_argument('--input-stdin', action='store_true')


def execute(store, args, cli):
    cli.validate_actor(store, args)
    if args.command=='watchdog':
        try:from . import pair_watchdog
        except ImportError:import pair_watchdog
        with store.transaction() as db:store._a_actor(db,args.member,args.session,args.worktree)
        if args.action=='configure':
            if not args.cli_path or not args.python_path or not getattr(args,'codex',None):
                raise ValueError('Pinned executables required / 登记守护须给出准确CLI、Python及Codex路径')
            return pair_watchdog.configure_watchdog(store,args.member,args.session,args.worktree,
                cli_path=args.cli_path,python_path=args.python_path,codex_path=args.codex)
        if args.action=='stop':return pair_watchdog.disable_watchdog(store,reason='Explicit watchdog stop / 明确停止守护')
        if args.action=='tick':return pair_watchdog.watchdog_tick(store,cli)
        return pair_watchdog.watchdog_loop(store,cli)
    payload = {} if args.command.endswith('-status') or not (getattr(args,'input_stdin',False) or getattr(args,'input_file',None)) else read_json(None if args.input_stdin else args.input_file)
    if not isinstance(payload, dict):
        raise ValueError('Object required / 输入须为JSON对象')
    if args.command=='refresh-cli-compatibility':
        if set(payload)-{'force'} or type(payload.get('force',False)) is not bool:
            raise ValueError('Invalid compatibility options / 预检输入仅允许force布尔值')
        executable=getattr(args,'codex',None)
        if not executable:raise ValueError('Exact CLI required / 预检须给出原CLI绝对路径 --codex')
        try:
            from .pair_transport import AppServer
            from .pair_producer import file_identity
        except ImportError:
            from pair_transport import AppServer
            from pair_producer import file_identity
        bound=store.member(args.member)
        trusted=store.setting('cli_trusted_executable:'+args.member)
        if trusted is not None and trusted.get('file')!=file_identity(executable):
            return store.refresh_cli_compatibility(args.member,args.session,args.worktree,executable,force=payload.get('force',False))
        with AppServer(codex_home=bound['codex_home'],cwd=bound['cwd'],executable=executable,
                       metadata_only=True,track_producer=True) as observer:
            if trusted is None:store._register_original_cli_baseline(args.member,args.session,args.worktree,observer,executable=executable)
            return store.refresh_cli_compatibility(args.member,args.session,args.worktree,executable,observer=observer,force=payload.get('force',False))
    method = getattr(store, COMMANDS[args.command])
    actor = (args.member, args.session, args.worktree)
    signature=inspect.signature(method)
    if 'executable' in signature.parameters:
        executable=getattr(args,'codex',None)
        if not executable:
            raise ValueError('Exact CLI required / 此执行入口须显式提供已核验的 --codex')
        if 'executable' in payload and payload['executable']!=executable:
            raise ValueError('CLI mismatch / JSON执行器与已核验 --codex 不一致')
        payload['executable']=executable
    try:
        signature.bind(*actor, **payload)
    except TypeError as exc:
        raise ValueError('Invalid command fields / 命令输入字段不符: '+str(exc)) from exc
    observation={'confirm-pause':'pause_confirmation','reconcile-resume':'resume',
                 'begin-review-recovery':'begin','reconcile-review-recovery':'reconcile','renew-review-recovery':'lease'}
    if args.command=='request-resume':
        try:from .pair_control_events import ControlObserver
        except ImportError:from pair_control_events import ControlObserver
        observer=ControlObserver(store,getattr(args,'codex',None))
        try:observer.poll()
        finally:observer.close()
    if args.command in observation:
        try:from .pair_control_runtime import ProductionRecoveryAdapter
        except ImportError:from pair_control_runtime import ProductionRecoveryAdapter
        adapter=ProductionRecoveryAdapter(store,getattr(args,'codex',None))
        adapter.capture(observation[args.command]);store._recovery_evidence_adapter=adapter
    return method(*actor, **payload)


def validate_checks(checks):
    if not isinstance(checks, list) or not 1 <= len(checks) <= 64:
        raise ValueError('Nonempty bounded checks required / 检查须为1至64项的JSON列表')
    names = set()
    for check in checks:
        if not isinstance(check, dict) or set(check) != {'name', 'command', 'argv', 'required'}:
            raise ValueError('Exact check fields required / 检查须包含name、command、argv、required')
        name, argv = check['name'], check['argv']
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', name)
                or name in names or check['required'] is not True or not isinstance(argv, list)
                or not 1 <= len(argv) <= 256 or any(not isinstance(v, str) or len(v) > 16000 for v in argv)
                or not argv[0].strip() or strict_display(argv) != check['command']):
            raise ValueError('Invalid exact check contract / 检查名称须唯一，命令须匹配准确argv')
        names.add(name)
