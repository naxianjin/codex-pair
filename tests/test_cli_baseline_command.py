"""可信 CLI 升级命令的入口参数与身份校验。"""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from scripts import pair
from scripts import pair_next_commands


class CliBaselineCommandTests(unittest.TestCase):
    def setUp(self):
        self.received = []

        def upgrade(member, session, cwd, executable, *, expected_baseline_digest, evidence):
            self.received.append((member, session, cwd, executable, expected_baseline_digest, evidence))
            return {'upgraded': True}

        self.store = SimpleNamespace(upgrade_cli_baseline=upgrade)
        self.cli = SimpleNamespace(validate_actor=Mock())
        self.payload = {'expected_baseline_digest': 'a' * 64, 'evidence': '用户确认原窗口 CLI 已更新'}

    def args(self, *extra):
        return pair.build_parser().parse_args([
            'upgrade-cli-baseline', '--member', 'A', '--session', 'original-session',
            '--worktree', 'original-cwd', '--input-file', 'upgrade.json', *extra])

    def execute(self, args):
        from unittest.mock import patch
        with patch('scripts.pair_next_commands.read_json', return_value=dict(self.payload)):
            return pair_next_commands.execute(self.store, args, self.cli)

    def test_explicit_cli_reaches_upgrade_after_actor_validation(self):
        args = self.args('--codex', 'original-codex.exe')
        self.assertTrue(self.execute(args)['upgraded'])
        self.cli.validate_actor.assert_called_once_with(self.store, args)
        self.assertEqual(self.received, [('A', 'original-session', 'original-cwd',
            'original-codex.exe', 'a' * 64, self.payload['evidence'])])

    def test_missing_or_conflicting_cli_and_failed_identity_never_upgrade(self):
        with self.assertRaisesRegex(ValueError, 'Exact CLI required'):
            self.execute(self.args())
        self.payload['executable'] = 'different-codex.exe'
        with self.assertRaisesRegex(ValueError, 'CLI mismatch'):
            self.execute(self.args('--codex', 'original-codex.exe'))
        self.cli.validate_actor.side_effect = ValueError('Current original CLI required')
        with self.assertRaisesRegex(ValueError, 'Current original CLI required'):
            self.execute(self.args('--codex', 'original-codex.exe'))
        self.assertEqual(self.received, [])


if __name__ == '__main__':
    unittest.main()
