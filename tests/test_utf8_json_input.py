"""JSON入口必须忽略宿主文本编码，严格按UTF-8读取原始字节。"""
import io
import json
from pathlib import Path
import tempfile
import os
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from scripts import pair_commands as commands, pair_permission_hook as hook, pair_json


class JsonInputTests(unittest.TestCase):
    def execute(self, data, *, encoding='gbk', file=False):
        store, cli = Mock(), Mock()
        args = SimpleNamespace(command='request-role-handoff', member='B', session='session', worktree='cwd',
                               input_stdin=not file, input_file=None)
        with tempfile.TemporaryDirectory() as directory:
            if file:
                path = Path(directory)/'input.json'; path.write_bytes(data); args.input_file = str(path)
            stream = io.TextIOWrapper(io.BytesIO(data), encoding=encoding, errors='surrogateescape')
            with patch.object(commands.sys, 'stdin', stream):
                commands.execute(store, args, cli)
            return store.request_role_handoff.call_args.kwargs['evidence']

    def test_unicode_stdin_matches_file_in_non_utf8_host(self):
        text = '中文😀\n"引号"和\\路径'
        data = json.dumps({'evidence':text}, ensure_ascii=False).encode('utf-8')
        self.assertEqual(self.execute(data), text)
        self.assertEqual(self.execute(data, file=True), text)

    def test_utf8_bom_stdin_matches_file(self):
        data = b'\xef\xbb\xbf' + json.dumps({'evidence':'中文😀'}, ensure_ascii=False).encode('utf-8')
        self.assertEqual(self.execute(data), self.execute(data, file=True))

    def test_invalid_bytes_rejected_before_business_call(self):
        for data in (b'{"evidence":"\xff"}', b'{"evidence":"\xe4\xb8"}', b'\xff\xfe{\x00}\x00'):
            for file in (False, True):
                with self.subTest(data=data, file=file), self.assertRaises(UnicodeDecodeError):
                    self.execute(data, file=file)

    def test_text_only_stdin_is_supported(self):
        store, cli = Mock(), Mock()
        args=SimpleNamespace(command='request-role-handoff',member='B',session='s',worktree='cwd',input_stdin=True)
        with patch.object(commands.sys,'stdin',io.StringIO('\ufeff{"evidence":"中文😀"}')):
            commands.execute(store,args,cli)
        self.assertEqual(store.request_role_handoff.call_args.kwargs['evidence'],'中文😀')

    def test_hook_accepts_bom_without_touching_store_for_other_session(self):
        stream=io.TextIOWrapper(io.BytesIO(b'\xef\xbb\xbf{"session_id":"other"}'),encoding='gbk')
        with patch.object(hook.sys,'stdin',stream), patch.object(hook.sys,'stdout',io.StringIO()) as output, patch.object(hook,'Store') as store:
            self.assertEqual(hook.main(['--pair','p','--state-root','unused','--member','B','--session','self']),0)
        self.assertEqual(json.loads(output.getvalue()),{})
        store.assert_not_called()

    def test_real_gbk_stdin_subprocess_reads_original_utf8(self):
        env=dict(os.environ,PYTHONIOENCODING='gbk:surrogateescape',PYTHONDONTWRITEBYTECODE='1')
        text='中文😀\n"引号"\\路径'
        code='from scripts.pair_json import read_json; import json,sys; print(json.dumps({"encoding":sys.stdin.encoding,"value":read_json()},ensure_ascii=True))'
        for bom in (b'',b'\xef\xbb\xbf'):
            child=subprocess.run([sys.executable,'-B','-c',code],input=bom+json.dumps({'text':text},ensure_ascii=False).encode('utf-8'),
                capture_output=True,env=env,cwd=Path(__file__).parent.parent,timeout=15)
            self.assertEqual(child.returncode,0,child.stderr)
            result=json.loads(child.stdout)
            self.assertEqual(result['encoding'],'gbk')
            self.assertEqual(result['value'],{'text':text})

    def test_helper_rejects_invalid_utf8_and_malformed_json(self):
        for data,error in ((b'\xff',UnicodeDecodeError),(b'{',json.JSONDecodeError)):
            with patch.object(pair_json.sys,'stdin',io.TextIOWrapper(io.BytesIO(data),encoding='gbk',errors='surrogateescape')):
                with self.assertRaises(error):pair_json.read_json()

    def test_invalid_hook_input_denies_before_store(self):
        stream=io.TextIOWrapper(io.BytesIO(b'{"session_id":"\xff"}'),encoding='gbk',errors='surrogateescape')
        with patch.object(hook.sys,'stdin',stream), patch.object(hook.sys,'stdout',io.StringIO()) as output, patch.object(hook,'Store') as store:
            self.assertEqual(hook.main(['--pair','p','--state-root','unused','--member','B']),0)
        self.assertEqual(json.loads(output.getvalue())['hookSpecificOutput']['decision']['behavior'],'deny')
        store.assert_not_called()
