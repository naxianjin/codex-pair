"""单层消息桥守护：只恢复登记的桥，未知执行和存活占锁都不强制接管。"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sys
import time
import uuid

try:
    from .install_skill import checked_path, package_manifest
    from .pair_recovery import digest, encoded
    from .pair_reviewer import fail, safe_evidence
except ImportError:
    from install_skill import checked_path, package_manifest
    from pair_recovery import digest, encoded
    from pair_reviewer import fail, safe_evidence


SETTING = 'bridge_watchdog_v14'
CHECK_INTERVAL = 30
BACKOFF = (5, 15, 30)
WINDOW = 600


def configure_watchdog(store, member, session, cwd, *, cli_path, python_path, codex_path):
    """原A显式登记准确存活桥；策略关闭、未知启动或桥未响应时不启用。"""
    status = store.status(probe_timeout=.3)
    runtime = status['runtime']
    if not runtime['lock_held'] or not runtime['probe_confirmed'] or not runtime.get('pid'):
        fail('Bridge identity unverified / 须先核验已登记原桥的独占锁和随机探测')
    identity = process_identity(runtime['pid'])
    if identity['state'] != 'alive':
        fail('Bridge process unverified / 无法读取原桥准确进程身份')
    cli = checked_path(cli_path)
    if cli.name != 'pair.py' or cli.parent.name != 'scripts':
        fail('Pinned bridge entry required / 必须登记固定运行副本的scripts/pair.py')
    root = cli.parent.parent
    manifest = package_manifest(root)
    if manifest.get('scripts/pair.py') != _hash(cli):
        fail('Bridge entry missing from manifest / 固定清单必须包括准确桥入口')
    python = checked_path(python_path)
    codex = checked_path(codex_path)
    if _path(identity['image']) != _path(python):
        fail('Bridge executable mismatch / 实际桥进程不是登记的Python')
    with store._recovery_control_transaction() as db:
        actor = store._a_actor(db, member, session, cwd)
        policy = store._require_recovery_policy(db, 'transport')
        pair = db.execute('SELECT * FROM collaborations').fetchone()
        if pair['status'] == 'stopped':
            fail('Collaboration stopped / 明确停止后不能启用守护')
        current = dict(db.execute('SELECT * FROM runtime WHERE id=1').fetchone())
        if current['owner'] != runtime['owner'] or current['pid'] != runtime['pid']:
            fail('Bridge changed / 登记期间原桥身份发生变化')
        old = _read(store, db)
        if old and old.get('pending'):
            fail('Bridge restart unresolved / 上次桥启动结果未知，先对账')
        config = {'enabled': True, 'generation': (old or {}).get('generation', 0) + 1,
            'pair_id': store.pair_id, 'stable_id': 'bridge:' + store.pair_id,
            'policy_version': policy['version'], 'authorization_version': store._authorization_version(db),
            'actor_session': actor['session_uuid'], 'binding_version': actor['binding_version'],
            'project': pair['project'], 'runtime_root': str(root), 'manifest_digest': digest(manifest),
            'cli_path': str(cli), 'python_path': str(python), 'codex_path': str(codex),
            'python_sha256': _hash(python), 'codex_sha256': _hash(codex),
            'bridge': {'owner': runtime['owner'], **identity}, 'pending': None,
            'last_check': None, 'exit_observed': None, 'next_attempt_at': None,
            'state': 'watching', 'reason': None}
        _save(db, config)
        store.event(db, 'watchdog_configured', generation=config['generation'], pid=identity['pid'])
        return _public(config)


def disable_watchdog(store, *, reason):
    """先持久化关闭恢复意图；调用方随后停止守护/桥，永不终止其他PID。"""
    safe_evidence(reason)
    with store._recovery_control_transaction() as db:
        config = _read(store, db)
        if config is None:
            return {'enabled': False, 'changed': False}
        changed = config['enabled']
        config.update(enabled=False, state='disabled', reason=reason, next_attempt_at=None)
        _save(db, config)
        if changed:
            store.event(db, 'watchdog_disabled', reason=reason, pending_preserved=bool(config.get('pending')))
        return {**_public(config), 'changed': changed}


def watchdog_tick(store, cli, *, now=None):
    """一次观察/有限恢复；调用恢复计数器，失败状态跨守护重启保留。"""
    try:
        from .pair_core import FileLock
        from .pair_windows import bridge
    except ImportError:
        from pair_core import FileLock
        from pair_windows import bridge
    now = time.time() if now is None else now
    with FileLock(store.directory / 'watchdog-tick.lock', timeout=0):
        with store.transaction() as db:
            config = _read(store, db)
            if config is None:
                return {'enabled': False, 'state': 'not_configured'}
            if not config['enabled']:
                return _public(config)
            if db.execute('SELECT status FROM collaborations').fetchone()[0] == 'stopped':
                config.update(enabled=False, state='disabled', reason='collaboration_stopped')
                _save(db, config)
                return _public(config)
            if not store._recovery_policy_valid(db, 'transport', config['policy_version']):
                return _blocked(store, db, config, 'transport_policy_unavailable')
            if config['last_check'] is not None and now < config['last_check']:
                return _blocked(store, db, config, 'clock_moved_backwards')
            config['last_check'] = now
            if config.get('pending'):
                return _blocked(store, db, config, 'bridge_restart_result_unknown')
        observation = process_identity(config['bridge']['pid'])
        status = store.status(probe_timeout=.3)
        with store.transaction() as db:
            current = _read(store, db)
            if current['generation'] != config['generation'] or not current['enabled']:
                return _public(current)
            if status['runtime']['lock_held'] or any(item['lock_held'] for item in status['relays']):
                if _same_process(observation, config['bridge']) and status['runtime']['probe_confirmed']:
                    config.update(state='watching', reason=None, exit_observed=None, next_attempt_at=None)
                    _save(db, config)
                    return _public(config)
                return _blocked(store, db, config, 'bridge_alive_unresponsive_or_locked')
            if not _exit_proven(observation, config['bridge']):
                if observation.get('error'):
                    config['last_error'] = observation['error']
                return _blocked(store, db, config, 'bridge_exit_unconfirmed')
            runtime = dict(db.execute('SELECT * FROM runtime WHERE id=1').fetchone())
            if runtime['owner'] != config['bridge']['owner'] or runtime['pid'] != config['bridge']['pid']:
                return _blocked(store, db, config, 'unregistered_bridge_change')
            attempts = list(db.execute("SELECT started FROM recovery_attempts WHERE kind='bridge_restart' AND stable_id=? AND started>? ORDER BY started",
                                       (config['stable_id'], now - WINDOW)))
            if len(attempts) >= len(BACKOFF):
                config['next_attempt_at'] = attempts[0]['started'] + WINDOW
                return _blocked(store, db, config, 'bridge_restart_budget_exhausted')
            if config['exit_observed'] is None:
                config['exit_observed'] = now
                config['next_attempt_at'] = now + BACKOFF[len(attempts)]
            if now < config['next_attempt_at']:
                config.update(state='backoff', reason='confirmed_exit')
                _save(db, config)
                return _public(config)
            _save(db, config)
        # bridge持有原有启动锁；guard把控制锁保持到Popen结束，停止不能穿过此边界。
        try:
            if _path(cli.__file__) != _path(config['cli_path']) or _path(sys.executable) != _path(config['python_path']):
                fail('Watchdog launch context changed / 守护须从登记的Python和固定运行副本启动')
            result = bridge(store, cli, config['codex_path'],
                            before_start=lambda: _restart_guard(store, config, now))
        except Exception as error:
            with store.transaction() as db:
                saved = _read(store, db)
                if saved and saved['generation'] == config['generation']:
                    saved['last_error'] = str(error)
                    _blocked(store, db, saved, 'bridge_start_unconfirmed' if saved.get('pending') else 'bridge_preflight_failed')
            raise
        with store.transaction() as db:
            saved = _read(store, db)
            if result.get('reused') or not result.get('running'):
                return _blocked(store, db, saved, 'bridge_changed_before_restart')
            # daemon须先完成控制补读和在途对账，再调用acknowledge_bridge_start。
            if saved.get('pending'):
                return _blocked(store, db, saved, 'bridge_bootstrap_unacknowledged')
            return {**_public(saved), 'bridge_result': result}


def acknowledge_bridge_start(store, nonce):
    """仅新桥进程在补读控制与在途对账后调用，不作为公开布尔回执入口。"""
    identity = process_identity(os.getpid())
    with store._recovery_control_transaction() as db:
        config = _read(store, db)
        pending = config.get('pending') if config else None
        if not pending or pending['nonce'] != nonce or identity['state'] != 'alive':
            fail('Watchdog bootstrap mismatch / 启动代次或新桥身份不符')
        runtime = dict(db.execute('SELECT * FROM runtime WHERE id=1').fetchone())
        if (runtime['pid'] != os.getpid() or _path(identity['image']) != _path(config['python_path'])
                or _path(sys.argv[0]) != _path(config['cli_path'])):
            fail('Watchdog process mismatch / 只能由当前登记的新桥确认启动')
        _fixed_runtime(config)
        evidence = {'nonce': nonce, 'process': identity, 'runtime_owner': runtime['owner'],
                    'manifest_digest': config['manifest_digest']}
        store._finish_recovery_attempt(db, 'bridge_restart', config['stable_id'], pending['attempt_id'], 'success', evidence)
        config.update(bridge={'owner': runtime['owner'], **identity}, pending=None,
                      exit_observed=None, next_attempt_at=None,
                      state='watching' if config['enabled'] else 'disabled', reason=None)
        _save(db, config)
        store.event(db, 'watchdog_bridge_recovered', **evidence)
        return _public(config)


def watchdog_loop(store, cli, *, stop_event=None):
    """不派生守护或模型；下一次正常入口负责发现本守护自身异常。"""
    try:
        from .pair_core import FileLock
    except ImportError:
        from pair_core import FileLock
    with FileLock(store.directory / 'watchdog.lock', timeout=0):
        while stop_event is None or not stop_event.is_set():
            with store.transaction() as db:
                config = _read(store, db)
                if not config or not config['enabled']:
                    return {'state': 'disabled'}
                if not store._recovery_policy_valid(db, 'transport', config['policy_version']):
                    return _blocked(store, db, config, 'transport_policy_unavailable')
            result = watchdog_tick(store, cli)
            if not result.get('enabled'):
                return result
            if stop_event is not None:
                stop_event.wait(CHECK_INTERVAL)
            else:
                time.sleep(CHECK_INTERVAL)
    return {'state': 'loop_stopped', 'recovery_intent_preserved': True}


def process_identity(pid):
    """Windows原生只读句柄：PID、创建时间、映像和等待状态共同识别进程。"""
    if type(pid) is not int or pid <= 0:
        return {'state': 'unknown', 'pid': pid, 'reason': 'invalid_pid'}
    if os.name != 'nt':
        return {'state': 'unknown', 'pid': pid, 'reason': 'windows_process_api_required'}
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        code = ctypes.get_last_error()
        return {'state': 'absent' if code == 87 else 'unknown', 'pid': pid,
                'error': str(ctypes.WinError(code))}
    try:
        created, exited, system, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(system), ctypes.byref(user)):
            return {'state': 'unknown', 'pid': pid, 'error': str(ctypes.WinError(ctypes.get_last_error()))}
        image = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(image))
        if not kernel.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)):
            return {'state': 'unknown', 'pid': pid, 'error': str(ctypes.WinError(ctypes.get_last_error()))}
        wait = kernel.WaitForSingleObject(handle, 0)
        return {'state': 'alive' if wait == 258 else 'exited' if wait == 0 else 'unknown',
                'pid': pid, 'created': (created.dwHighDateTime << 32) | created.dwLowDateTime,
                'image': image.value}
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def _restart_guard(store, expected, now):
    try:
        from .pair_core import FileLock
    except ImportError:
        from pair_core import FileLock
    with FileLock(store.control_lock, timeout=10):
        with store.transaction() as db:
            config = _read(store, db)
            if not config or not config['enabled'] or config['generation'] != expected['generation'] or config.get('pending'):
                fail('Watchdog intent changed / 桥恢复意图已变化或存在未决启动')
            if db.execute('SELECT status FROM collaborations').fetchone()[0] == 'stopped':
                fail('Collaboration stopped / 停止状态禁止桥恢复')
            store._require_recovery_policy(db, 'transport')
            if not store._recovery_policy_valid(db, 'transport', config['policy_version']):
                fail('Transport policy changed / 传输恢复授权已变化')
            actor = store._member(db, 'A')
            if actor['session_uuid'] != config['actor_session'] or actor['binding_version'] != config['binding_version']:
                fail('Watchdog binding changed / 原A绑定已变化，不能沿用守护授权')
            if not _exit_proven(process_identity(config['bridge']['pid']), config['bridge']):
                fail('Bridge exit unconfirmed / 原桥退出事实不再可确认')
            _fixed_runtime(config)
            nonce = uuid.uuid4().hex
            attempt = 'bridge-restart:' + nonce
            evidence = {'bridge': config['bridge'], 'manifest_digest': config['manifest_digest'], 'nonce': nonce}
            store._start_recovery_attempt(db, 'bridge_restart', config['stable_id'], attempt, evidence, now=now)
            config.update(pending={'nonce': nonce, 'attempt_id': attempt, 'state': 'starting'},
                          state='starting', reason=None)
            _save(db, config)
        yield nonce


def _fixed_runtime(config):
    if (digest(package_manifest(Path(config['runtime_root']))) != config['manifest_digest']
            or _hash(checked_path(config['python_path'])) != config['python_sha256']
            or _hash(checked_path(config['codex_path'])) != config['codex_sha256']):
        fail('Pinned bridge runtime changed / 固定运行副本或执行器摘要已变化，停止自动恢复')


def _same_process(observed, expected):
    return (observed.get('state') == 'alive' and observed.get('pid') == expected['pid']
            and observed.get('created') == expected['created']
            and _path(observed.get('image', '')) == _path(expected['image']))


def _exit_proven(observed, expected):
    return observed.get('pid') == expected['pid'] and (observed.get('state') == 'absent'
            or observed.get('state') == 'exited' and observed.get('created') == expected['created']
            or observed.get('state') == 'alive' and observed.get('created') != expected['created'])


def _path(value):
    path = os.path.realpath(value)
    if path.startswith('\\\\?\\UNC\\'):
        path = '\\\\' + path[8:]
    elif path.startswith('\\\\?\\'):
        path = path[4:]
    return os.path.normcase(path)


def _hash(path):
    checksum = hashlib.sha256()
    with Path(path).open('rb') as file:
        for block in iter(lambda: file.read(1024 * 1024), b''):
            checksum.update(block)
    return checksum.hexdigest()


def _read(store, db):
    return store._option(db, SETTING)


def _save(db, config):
    db.execute('INSERT INTO pair_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (SETTING, encoded(config)))


def _public(config):
    return {key: config.get(key) for key in ('enabled', 'generation', 'state', 'reason',
                                           'last_check', 'next_attempt_at', 'last_error')}


def _blocked(store, db, config, reason):
    if config.get('reason') != reason:
        store.event(db, 'watchdog_blocked', reason=reason, generation=config['generation'])
        notice = 'watchdog-blocked:' + digest({'pair': store.pair_id, 'reason': reason})
        db.execute('''INSERT OR IGNORE INTO recovery_outbox(id,recipient,body,created)
                      VALUES(?,'A',?,?)''', (notice,
            '消息桥自动恢复受阻：' + reason + '。已保留进程占用、重启计数及未知结果；请从正常入口核查，不重派原业务。',
            time.time()))
    config.update(state='blocked', reason=reason)
    _save(db, config)
    return _public(config)
