"""真实测试需显式 CODEX_PAIR_REAL_TEST=1；其余测试不请求模型。"""
import hashlib
import json
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts.pair_transport import (AppServer, DeliveryUnknown, TransportError,
                                    QueueTransport, canonical_path, codex_command, session_uuid)


class TransportTests(unittest.TestCase):
    def test_reject_invalid_session(self):
        for value in ('last', 'worker-b', '123', '../escape', '{00000000-0000-0000-0000-000000000000}'):
            with self.subTest(value=value), self.assertRaises(TransportError):
                session_uuid(value)

    def test_cwd_and_identity_checked_before_start(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            client = AppServer.__new__(AppServer)
            client.backend = 'managed'
            member = {'session_uuid': '00000000-0000-0000-0000-000000000001', 'cwd': first}
            with patch.object(client, 'read_thread', return_value={
                    'id': member['session_uuid'], 'cwd': second, 'status': {'type': 'idle'}}), patch.object(client, 'rpc') as rpc:
                with self.assertRaisesRegex(TransportError, 'working directory'):
                    client.start(member, {'id': 'message', 'envelope': '不可发送'})
                rpc.assert_not_called()

    def test_json_preserves_multiline_quotes_and_windows_paths(self):
        import io
        import threading
        client = AppServer.__new__(AppServer)
        client._evidence = None
        client._write_lock = threading.Lock()
        client.process = type('Process', (), {'stdin': io.BytesIO()})()
        text = '中文\n第二行 "引号" \'单引号\' C:\\用户\\项目\\a.py\n$(whoami) & echo x'
        client._write({'method': 'turn/start', 'params': {'input': [{'type': 'text', 'text': text}]}})
        wire = client.process.stdin.getvalue()
        self.assertTrue(wire.isascii())
        self.assertEqual(json.loads(wire)['params']['input'][0]['text'], text)

    def test_queue_ack_is_not_a_completion(self):
        client = AppServer.__new__(AppServer)
        client.notifications = queue.Queue()
        client.notifications.put({'id': 1, 'result': {'turn': {'id': 'turn', 'status': 'inProgress'}}})
        with self.assertRaises(DeliveryUnknown):
            client.wait_turn('thread', 'turn', timeout=.01)

    def test_observe_matches_client_message_not_any_completed_turn(self):
        client = AppServer.__new__(AppServer)
        with patch.object(client, 'validate'), patch.object(client, 'turns', return_value=iter([
                {'id': 'other', 'status': 'completed', 'items': []},
                {'id': 'correct', 'status': 'inProgress', 'items': [
                    {'type': 'userMessage', 'clientId': 'message'}]}])):
            result = client.observe({'session_uuid': 'thread'}, {'id': 'message'})
            self.assertEqual(result['turn_id'], 'correct')
            self.assertEqual(result['remote_status'], 'inProgress')

    def test_queue_uses_argument_array_and_ascii_envelope(self):
        transport=QueueTransport.__new__(QueueTransport)
        transport.command=['codex.exe'];transport.cwd=None;transport.environment={};transport.timeout=1
        member={'session_uuid':'00000000-0000-0000-0000-000000000001'}
        envelope=json.dumps({'body':'中文\n"引号" C:\\项目 a\\a.py'},ensure_ascii=True)
        response=subprocess.CompletedProcess([],0,('Queued message 00000000-0000-0000-0000-000000000002 for thread '+member['session_uuid']+'.').encode(),b'')
        with patch.object(transport,'validate'),patch('scripts.pair_transport.subprocess.run',return_value=response) as run:
            receipt=transport.start(member,{'envelope':envelope})
            self.assertEqual(receipt['remote_status'],'queued')
            self.assertIsNone(receipt['turn_id'])
            self.assertEqual(run.call_args.args[0][-1],envelope)
            self.assertNotIn('shell',run.call_args.kwargs)
        with patch.object(transport,'validate'),patch('scripts.pair_transport.subprocess.run',side_effect=subprocess.TimeoutExpired([],1)):
            with self.assertRaises(DeliveryUnknown):transport.start(member,{'envelope':envelope})

    def test_partial_history_interrupted_is_not_actual_completion(self):
        client=AppServer.__new__(AppServer)
        with patch.object(client,'validate'),patch.object(client,'turns',return_value=iter([
                {'id':'turn','status':'interrupted','completedAt':None,'items':[]}])):
            observation=client.observe({'session_uuid':'thread'},{'turn_id':'turn'})
            self.assertEqual(observation['remote_status'],'inProgress')
            self.assertEqual(observation['reported_status'],'interrupted')


@unittest.skipUnless(os.getenv('CODEX_PAIR_REAL_TEST') == '1', '需要显式启用真实 Codex 测试')
class RealTransportTests(unittest.TestCase):
    def test_real_appserver_and_queue(self):
        home = Path(os.getenv('CODEX_HOME', str(Path.home()/'.codex'))).resolve()
        evidence = Path(os.environ.get('CODEX_PAIR_EVIDENCE', str(home/'codex-pair'/'live-validation')))
        evidence.mkdir(parents=True, exist_ok=True)
        result = {'version': subprocess.run(codex_command()+['--version'], capture_output=True, text=True, check=True).stdout.strip(),
                  'queue_idle': {}, 'queue_busy': {}}
        with tempfile.TemporaryDirectory(prefix='codex-pair-live-') as project, (evidence/'appserver.jsonl').open('w', encoding='utf-8') as transcript:
            subprocess.run(['git', 'init', project], check=True, capture_output=True)
            def record(value):
                transcript.write(json.dumps(value, ensure_ascii=True)+'\n')
                transcript.flush()
            with AppServer(codex_home=home, cwd=project, evidence=record) as client:
                thread = client.create_session(project)
                member = {'session_uuid': thread['id'], 'cwd': project}
                result['thread'] = thread['id']
                result['project'] = project
                payload = '这是隔离的通信验收。只输出 PAIR_UNICODE_OK，不调用工具。\n原文：中文、多行、"双引号"、\'单引号\'\nWindows 路径 C:\\用户\\项目 a\\文件.py\n$(whoami) & echo x'
                message = {'id': 'live-unicode-'+str(time.time_ns()), 'envelope': payload}
                receipt = client.start(member, message)
                turn = client.wait_turn(thread['id'], receipt['turn_id'])
                self.assertEqual(turn['status'], 'completed')
                stored = list(client.turns(thread['id']))
                texts = [entry['text'] for t in stored for item in t['items'] if item.get('type') == 'userMessage' for entry in item['content'] if entry.get('type') == 'text']
                self.assertIn(payload, texts)
                result['appserver'] = {'receipt': receipt, 'completion': turn,
                                       'payload_sha256': hashlib.sha256(payload.encode()).hexdigest(), 'exact_payload_verified': True}
                # 同一空闲会话，不用额外用户请求触发它；只观察自己的测试会话。
                marker = 'PAIR_QUEUE_IDLE_'+str(time.time_ns())
                queued = subprocess.run(codex_command()+['queue', '--thread', thread['id'], '--message', 'Reply only '+marker+'; do not call tools.'], capture_output=True, text=True, encoding='utf-8', timeout=30, cwd=project)
                result['queue_idle'].update(returncode=queued.returncode, stdout=queued.stdout, stderr=queued.stderr)
                deadline = time.monotonic()+15
                observed = []
                while time.monotonic()<deadline:
                    try:
                        event = client.notifications.get(timeout=min(1, max(.01, deadline-time.monotonic())))
                    except queue.Empty:
                        continue
                    if event.get('method') == 'turn/completed' and event.get('params', {}).get('threadId') == thread['id']:
                        observed.append(event)
                        if any(marker in item.get('text','') for item in event['params']['turn']['items']):
                            break
                result['queue_idle'].update(observed=observed, observation_seconds=15,
                                           processed=any(marker in json.dumps(e) for e in observed))
                # 通过生产队列传输层验证 JSON 封装后的中文原文，而不是仅测试英文探针。
                unicode_envelope=json.dumps({'protocol':'codex-pair/test','body':'只输出 PAIR_QUEUE_UNICODE_OK，不调用工具。\n中文 "双引号" \'单引号\' C:\\用户\\项目 a\\文件.py'},ensure_ascii=True)
                with _queue_context(home,project) as queue_transport:
                    queued_receipt=queue_transport.start(member,{'id':'queue-unicode','envelope':unicode_envelope})
                    deadline=time.monotonic()+60;observation=None
                    while time.monotonic()<deadline:
                        observation=queue_transport.observe(member,{'id':'queue-unicode','envelope':unicode_envelope})
                        if observation and observation['remote_status'] in ('completed','failed','interrupted'):break
                        time.sleep(.25)
                    self.assertIsNotNone(observation)
                    self.assertEqual(observation['remote_status'],'completed')
                    result['queue_unicode']={'receipt':queued_receipt,'observation':observation,'exact_payload_verified':True}
                # 繁忙测试使用另一个独立会话，避免把旧队列混入新回合。
                busy = client.create_session(project)
                worker = {'session_uuid': busy['id'], 'cwd': project}
                seed = client.start(worker, {'id': 'seed-'+str(time.time_ns()), 'envelope': 'Reply only PAIR_SEED; do not call tools.'})
                self.assertEqual(client.wait_turn(busy['id'], seed['turn_id'])['status'], 'completed')
                first = client.start(worker, {'id': 'busy-'+str(time.time_ns()), 'envelope': '隔离测试：先用 shell 执行 Start-Sleep -Seconds 4，完成后只输出 PAIR_FIRST。'})
                marker = 'PAIR_QUEUE_BUSY_'+str(time.time_ns())
                queued = subprocess.run(codex_command()+['queue', '--thread', busy['id'], '--message', 'Reply only '+marker+'; do not call tools.'], capture_output=True, text=True, encoding='utf-8', timeout=30, cwd=project)
                result['queue_busy'].update(thread=busy['id'], first_turn=first['turn_id'], returncode=queued.returncode, stdout=queued.stdout, stderr=queued.stderr)
                first_done = client.wait_turn(busy['id'], first['turn_id'])
                deadline=time.monotonic()+30
                observed=[]
                while time.monotonic()<deadline:
                    try: event=client.notifications.get(timeout=min(1,max(.01,deadline-time.monotonic())))
                    except queue.Empty: continue
                    if event.get('method')=='turn/completed' and event.get('params',{}).get('threadId')==busy['id']:
                        observed.append(event)
                        if marker in json.dumps(event): break
                result['queue_busy'].update(first_completion=first_done, observed=observed,
                    processed=any(marker in json.dumps(e) for e in observed))
        (evidence/'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        print('真实证据:', evidence/'result.json')


from contextlib import contextmanager
@contextmanager
def _queue_context(home,project):
    transport=QueueTransport(codex_home=home,cwd=project)
    try:yield transport
    finally:transport.close()


if __name__ == '__main__':
    unittest.main()
