"""外部autocrlf配置下的审核检出和原始字节保护；只用临时Git库。"""
import hashlib
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from scripts import pair_review_runtime as runtime
from scripts.pair_core import PairError
from tests import test_core, test_integration


class ReviewCheckoutTests(unittest.TestCase):
    tearDown = test_core.CoreTests.tearDown

    def setUp(self):
        test_core.CoreTests.setUp(self)
        environment = patch.dict(os.environ, {'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.autocrlf', 'GIT_CONFIG_VALUE_0': 'true'})
        environment.start()
        self.addCleanup(environment.stop)
        self.project.rmdir()
        test_integration.seed(self.project)
        self.head = runtime.git(self.project, 'rev-parse', 'HEAD')
        self.review = self.project / '.codex-pair/worktrees' / (self.store.pair_id + '-reviewer')

    def blob(self):
        return subprocess.check_output(['git', '-C', str(self.project), 'show', 'HEAD:shared.txt'])

    def test_external_autocrlf_true_new_tree_matches_blob_without_config_change(self):
        before = runtime.git(self.project, 'config', '--show-origin', '--get', 'core.autocrlf')
        root = Path(runtime.prepare_review_tree(self.store, self.head))
        self.assertEqual((root / 'shared.txt').read_bytes(), self.blob())
        self.assertEqual(runtime.git(self.project, 'config', '--show-origin', '--get', 'core.autocrlf'), before)

    def test_later_switch_keeps_exact_blob_bytes(self):
        root = Path(runtime.prepare_review_tree(self.store, self.head))
        (self.project / 'shared.txt').write_bytes(b'second\n')
        runtime.git(self.project, 'add', 'shared.txt')
        runtime.git(self.project, 'commit', '-m', 'second')
        target = runtime.git(self.project, 'rev-parse', 'HEAD')
        self.assertEqual(runtime.prepare_review_tree(self.store, target), str(root.resolve()))
        self.assertEqual((root / 'shared.txt').read_bytes(), self.blob())

    def snapshot(self, root):
        manifest = {'shared.txt': hashlib.sha256((root / 'shared.txt').read_bytes()).hexdigest()}
        self.store.setting('reviewer_snapshot_applied', {'manifest': manifest, 'stamp': runtime.tree_stamp(root)})

    def test_snapshot_restore_keeps_exact_head_bytes(self):
        root = Path(runtime.prepare_review_tree(self.store, self.head))
        (root / 'shared.txt').write_bytes(b'snapshot\n')
        self.snapshot(root)
        runtime.prepare_review_tree(self.store, self.head)
        self.assertEqual((root / 'shared.txt').read_bytes(), self.blob())

    def test_user_dirty_tree_is_preserved(self):
        root = Path(runtime.prepare_review_tree(self.store, self.head))
        (root / 'shared.txt').write_bytes(b'user change\n')
        with self.assertRaises(PairError): runtime.prepare_review_tree(self.store, self.head)
        self.assertEqual((root / 'shared.txt').read_bytes(), b'user change\n')

    def test_explicit_isolated_reset_uses_the_same_checkout_policy(self):
        root = Path(runtime.prepare_review_tree(self.store, self.head))
        (root / 'shared.txt').write_bytes(b'controlled temporary change\n')
        runtime.review_git(root, 'reset', '--hard', self.head)
        self.assertEqual((root / 'shared.txt').read_bytes(), self.blob())

    def test_old_git_clean_crlf_tree_is_rejected_without_rewrite(self):
        runtime.git(self.project, 'worktree', 'add', '--detach', str(self.review), self.head)
        original = (self.review / 'shared.txt').read_bytes()
        self.assertEqual(original, b'base\r\n')
        self.assertEqual(runtime.tree_stamp(self.review)['changes'], '')
        with self.assertRaisesRegex(PairError, 'bytes differ'):
            runtime.prepare_review_tree(self.store, self.head)
        self.assertEqual((self.review / 'shared.txt').read_bytes(), original)

    def test_byte_only_snapshot_mutation_is_not_hidden_by_git_filter(self):
        root = Path(runtime.prepare_review_tree(self.store, self.head))
        (root / 'shared.txt').write_bytes(b'snapshot\n')
        self.snapshot(root)
        before = runtime.tree_stamp(root)
        (root / 'shared.txt').write_bytes(b'snapshot\r\n')
        self.assertEqual(runtime.tree_stamp(root), before)
        with self.assertRaisesRegex(PairError, 'Snapshot bytes changed'):
            runtime.prepare_review_tree(self.store, self.head)
        self.assertEqual((root / 'shared.txt').read_bytes(), b'snapshot\r\n')

    def test_explicit_crlf_attribute_fails_byte_gate_instead_of_normalizing(self):
        (self.project / '.gitattributes').write_bytes(b'shared.txt text eol=crlf\n')
        runtime.git(self.project, 'add', '.gitattributes')
        runtime.git(self.project, 'commit', '-m', 'explicit eol')
        target = runtime.git(self.project, 'rev-parse', 'HEAD')
        with self.assertRaisesRegex(PairError, 'bytes differ'):
            runtime.prepare_review_tree(self.store, target)
        self.assertEqual((self.review / 'shared.txt').read_bytes(), b'base\r\n')


if __name__ == '__main__':
    unittest.main()
