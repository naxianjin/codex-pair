"""仅为隔离测试提供外部 CLI 元数据；来源登记、兼容性判定及门禁仍调用真实实现。"""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

from scripts.pair_producer import file_identity, SUPPORTED_VERSIONS
from scripts.pair_transport import AppServer


class MetadataObserver(AppServer):
    """模拟既有官方元数据连接；不创建模型或真实进程。"""
    metadata_only = True
    backend = 'managed'

    def __init__(self, binding, identity, version=None):
        self.identity = {'codexHome': binding['codex_home']}
        self.producer_launch = {'file': identity, 'process': {'pid': 123}, 'version': version or SUPPORTED_VERSIONS[0]}
        self.thread = {'id': binding['session_uuid'], 'cwd': binding['cwd'],
                       'model': binding['model'], 'modelProvider': binding['modelProvider']}
        self.turns = [{'id': 'fixture-turn', 'threadId': binding['session_uuid'], 'status': 'completed',
                       'items': [{'id': 'fixture-command', 'type': 'commandExecution', 'status': 'completed',
                                  'exitCode': 0, 'command': 'fixture check', 'cwd': binding['cwd']}]}]
        self.calls = []

    def __enter__(self):
        return self

    def validate(self, member):
        if self.thread['id'] != member['session_uuid'] or not Path(self.thread['cwd']).samefile(member['cwd']):
            raise AssertionError('Fixture session binding mismatch')
        return deepcopy(self.thread)

    def close(self):
        self.calls.append(('close', None))

    def read_thread(self, session):
        self.calls.append(('thread/read', session))
        return deepcopy(self.thread)

    def rpc(self, method, params):
        self.calls.append((method, deepcopy(params)))
        if method != 'thread/turns/list':
            raise AssertionError('兼容性预检不得调用额外RPC')
        return {'data': deepcopy(self.turns), 'nextCursor': None}


def register_cli_fixture(store, members=('A', 'B'), *, executable=None, version=None):
    executable = Path(executable) if executable is not None else Path(store.directory) / 'fixture-codex.exe'
    version = version or SUPPORTED_VERSIONS[0]
    if not executable.exists():
        executable.write_bytes(b'unit fixture executable; never launch')
    identity = file_identity(executable)

    def cli(argv, **kwargs):
        arguments = argv[1:]
        if arguments == ['--version']:
            output = 'codex-cli ' + version + '\n'
        elif arguments == ['--help']:
            output = 'Commands:\n  queue        Queue a message\n  app-server   Run app server\n'
        elif arguments == ['app-server', '--help']:
            output = 'Usage: codex app-server [OPTIONS]'
        else:
            raise AssertionError('Fixture permits only registered metadata queries')
        return subprocess.CompletedProcess(argv, 0, output, '')

    for member in members:
        with store.transaction() as db:
            actor = dict(store._member(db, member))
            proof = json.loads(actor['proof'])
            proof.setdefault('model', 'gpt-test')
            proof.setdefault('modelProvider', 'openai')
            db.execute('UPDATE members SET proof=? WHERE id=?', (json.dumps(proof), member))
        binding = {'session_uuid': actor['session_uuid'], 'cwd': actor['cwd'],
                   'codex_home': actor['codex_home'], 'model': proof['model'],
                   'modelProvider': proof['modelProvider']}
        observer = MetadataObserver(binding, identity, version)
        with patch('scripts.pair_compatibility.verify_launch'), patch('scripts.pair_compatibility.subprocess.run', side_effect=cli):
            store._register_original_cli_baseline(member, actor['session_uuid'], actor['cwd'], observer, executable=executable)
            result = store.refresh_cli_compatibility(member, actor['session_uuid'], actor['cwd'], executable, observer=observer)
        if result['operations']['new_delivery']['status'] != 'preflight_ready':
            raise AssertionError(result['reasons'])
    return executable, cli
