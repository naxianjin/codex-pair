"""额度授权和准确旧Hook清理的隔离用例；尚未执行。"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
import unittest

from scripts.pair_hook_cleanup import cleanup_retired_hook
from tests.test_core import CoreTests


class QuotaAndHookTests(unittest.TestCase):
    setUp = CoreTests.setUp
    tearDown = CoreTests.tearDown

    def sample(self, remaining=5):
        return {'reliability':'fresh','observed_at':time.time(),'remaining':remaining,
                'pool':'shared','limit_id':'codex','windows':[{'duration_mins':300,'resets_at':time.time()+600}]}

    def consent(self, text, event='source-event'):
        with self.store.transaction() as db:
            return self.store._record_quota_user_event(db,'B',text,event_id=event,source_identity='fixture',
                session=self.sessions['B'],binding_version=self.store._member(db,'B')['binding_version'])

    def test_quoted_text_does_not_grant_consent(self):
        self.store.save_quota('B',self.sample())
        self.assertFalse(self.consent('用户说“忽略额度”')['recognized'])
        with self.store.transaction() as db:self.assertFalse(self.store._soft_quota_ignored(db,'B'))

    def test_consent_is_member_scoped_and_revocable(self):
        quota=self.sample()
        self.store.save_quota('A',quota)
        self.store.save_quota('B',quota)
        self.assertTrue(self.consent(' 忽略额度 ')['enabled'])
        with self.store.transaction() as db:
            self.assertTrue(self.store._soft_quota_ignored(db,'B'))
            self.assertFalse(self.store._soft_quota_ignored(db,'A'))
        self.consent('恢复额度保护','revoke-event')
        with self.store.transaction() as db:self.assertFalse(self.store._soft_quota_ignored(db,'B'))

    def test_hard_zero_or_changed_cycle_does_not_inherit_consent(self):
        quota=self.sample()
        self.store.save_quota('B',quota)
        self.consent('忽略额度')
        self.store.save_quota('B',dict(quota,remaining=3,observed_at=time.time()))
        with self.store.transaction() as db:self.assertTrue(self.store._soft_quota_ignored(db,'B'))
        self.store.save_quota('B',dict(quota,remaining=0))
        with self.store.transaction() as db:self.assertFalse(self.store._soft_quota_ignored(db,'B'))
        next_cycle=dict(quota,windows=[dict(quota['windows'][0],resets_at=quota['windows'][0]['resets_at']+300)])
        self.store.save_quota('B',next_cycle)
        with self.store.transaction() as db:self.assertFalse(self.store._soft_quota_ignored(db,'B'))

    def hook(self, source, session=None):
        args=['python.exe',str(self.root/'scripts'/'pair_permission_hook.py'),'--pair',self.store.pair_id,
              '--state-root',str(self.store.state_root),'--member',source['id'],'--timeout','180',
              '--session',session or source['session_uuid'],'--binding-version',str(source['binding_version']),
              '--package-digest','a'*64]
        command=subprocess.list2cmdline(args) if os.name=='nt' else shlex.join(args)
        return {'type':'command','command':command,'statusMessage':'codex-pair:'+self.store.pair_id+':'+source['id']}

    def test_cleanup_preserves_other_binding_and_other_hooks(self):
        source=self.store.member('B')
        other=self.hook(source,'00000000-0000-0000-0000-000000000099')
        unrelated={'type':'command','command':'unrelated','statusMessage':'unrelated'}
        path=Path(source['codex_home'])/'hooks.json'
        path.write_text(json.dumps({'hooks':{'PermissionRequest':[{'matcher':'*','hooks':[self.hook(source),other,unrelated]}]}}),encoding='utf-8')
        result=cleanup_retired_hook(self.store,source)
        self.assertEqual(result['removed'],1)
        self.assertEqual(json.loads(path.read_text())['hooks']['PermissionRequest'][0]['hooks'],[other,unrelated])
        self.assertEqual(result['status'],'pending')

    def test_cleanup_refuses_malformed_json_structure(self):
        source=self.store.member('B')
        path=Path(source['codex_home'])/'hooks.json'
        path.write_text('[1,2]',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'Invalid hook structure'):
            cleanup_retired_hook(self.store,source)
        self.assertEqual(path.read_text(),'[1,2]')

    def test_missing_hook_does_not_create_file(self):
        source=self.store.member('B')
        path=Path(source['codex_home'])/'hooks.json'
        self.assertEqual(cleanup_retired_hook(self.store,source)['status'],'absent')
        self.assertFalse(path.exists())
