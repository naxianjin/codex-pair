"""深路径快照冻结与物化，使用临时Git仓库，不调用模型。"""
import hashlib
import os
import subprocess
import shutil
import unittest
from unittest.mock import patch

from scripts.pair_core import PairError
from scripts.pair_git import filesystem_path
from scripts.pair_workspace import freeze_snapshot
from scripts.pair_review_runtime import materialize_snapshot
from tests import test_core


class SnapshotPathTests(unittest.TestCase):
    setUp=test_core.CoreTests.setUp
    def tearDown(self):
        # 只清理本用例TemporaryDirectory，普通路径清理不了超长子目录。
        self.assertEqual(str(self.root),self.temporary.name)
        shutil.rmtree(filesystem_path(self.root))
        self.temporary.cleanup()

    @unittest.skipUnless(os.name=='nt','Windows深路径快照')
    def test_freeze_materialize_and_corruption_guard_beyond_max_path(self):
        length=185-len(str(self.root))-1
        if length<5:self.skipTest('父夹具过深，无法构造固定长度项目')
        project=self.root/('深 空格'+'x'*(length-4))
        project.mkdir()
        subprocess.run(['git','init','-q',str(project)],check=True,capture_output=True)
        with self.store.transaction() as db:
            db.execute('UPDATE collaborations SET project=?',(str(project),))
        relative='source/nested/example.py'
        source=project/relative;source.parent.mkdir(parents=True)
        data=b'print("snapshot")\n';source.write_bytes(data)
        artifact={'kind':'snapshot','baseline':'a'*40,'manifest':{relative:hashlib.sha256(data).hexdigest()}}
        frozen=freeze_snapshot(self.store,str(project),artifact,['source/'])
        frozen_file=filesystem_path(frozen['frozen_root'])/relative
        self.assertGreater(len(frozen['frozen_root']+'/'+relative),260)
        self.assertFalse(frozen['frozen_root'].startswith('\\\\?\\'))
        self.assertEqual(frozen_file.read_bytes(),data)
        self.assertEqual(freeze_snapshot(self.store,str(project),artifact,['source/']),frozen)
        review=project/'review';review.mkdir()
        with patch('scripts.pair_review_runtime.tree_stamp',return_value={'fixed':True}):
            materialize_snapshot(self.store,str(review),frozen)
        self.assertEqual((review/relative).read_bytes(),data)
        frozen_file.write_bytes(b'changed')
        with self.assertRaisesRegex(PairError,'Frozen snapshot changed'):
            freeze_snapshot(self.store,str(project),artifact,['source/'])
        with self.assertRaisesRegex(PairError,'Frozen snapshot changed'):
            materialize_snapshot(self.store,str(review),frozen)
        self.assertEqual(source.read_bytes(),data)
