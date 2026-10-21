from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import scripts.pair_git as pair_git

from scripts.pair_git import (
    GitError,
    abort_integration,
    check_delivery,
    continue_integration,
    create_worktrees,
    inspect_repo,
    integrate_commits,
    validate_worktree,
)


def git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", os.fspath(cwd), *args],
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


class GitModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="codex pair 中文 ")
        self.root = Path(self.temp.name)
        self.repo = self.root / "主 项目"
        self.repo.mkdir()
        git(self.repo, "init", "-b", "main")
        git(self.repo, "config", "user.name", "Codex Pair Test")
        git(self.repo, "config", "user.email", "pair@example.invalid")
        (self.repo / "shared.txt").write_text("base\n", encoding="utf-8")
        (self.repo / "docs").mkdir()
        (self.repo / "docs" / "guide.txt").write_text("guide\n", encoding="utf-8")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "base")
        self.base = git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _commit_file(self, worktree: Path, relative: str, content: str, message: str) -> str:
        path = worktree / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        git(worktree, "add", "--", relative)
        git(worktree, "commit", "-m", message)
        return git(worktree, "rev-parse", "HEAD").stdout.strip()

    def test_inspect_repo_and_create_two_worktrees_from_same_base(self) -> None:
        info = inspect_repo(os.fspath(self.repo))
        result = create_worktrees(os.fspath(self.repo), "pair-1", "HEAD", os.fspath(self.repo / ".codex-pair" / "worktrees" / "工作树 根"))
        self.assertEqual(info["head"], result["base_commit"])
        self.assertEqual(self.base, git(Path(result["a"]["path"]), "rev-parse", "HEAD").stdout.strip())
        self.assertEqual(self.base, git(Path(result["b"]["path"]), "rev-parse", "HEAD").stdout.strip())
        a = inspect_repo(result["a"]["path"])
        b = inspect_repo(result["b"]["path"])
        self.assertEqual(a["common_dir"].casefold(), b["common_dir"].casefold())
        self.assertEqual("codex-pair/pair-1/a", a["branch"])
        self.assertEqual("codex-pair/pair-1/b", b["branch"])

    def test_original_uncommitted_changes_are_preserved_and_excluded(self) -> None:
        changed = self.repo / "shared.txt"
        changed.write_text("local only\n", encoding="utf-8")
        result = create_worktrees(os.fspath(self.repo), "dirty", "HEAD", os.fspath(self.repo / ".codex-pair" / "worktrees"))
        self.assertTrue(result["original_dirty"])
        self.assertFalse(result["original_changes_included"])
        self.assertEqual("local only\n", changed.read_text(encoding="utf-8"))
        self.assertEqual("base\n", (Path(result["a"]["path"]) / "shared.txt").read_text(encoding="utf-8"))

    def test_existing_directory_or_branch_is_not_overwritten(self) -> None:
        tree_root = self.repo / ".codex-pair" / "worktrees"
        (tree_root / "occupied-a").mkdir(parents=True)
        with self.assertRaises(GitError) as caught:
            create_worktrees(os.fspath(self.repo), "occupied", "HEAD", os.fspath(tree_root))
        self.assertIn("目录已存在", str(caught.exception.details))
        git(self.repo, "branch", "codex-pair/branch-exists/a")
        with self.assertRaises(GitError) as caught:
            create_worktrees(os.fspath(self.repo), "branch-exists", "HEAD", os.fspath(tree_root))
        self.assertIn("分支已存在", str(caught.exception.details))

    def test_partial_creation_failure_preserves_and_reports_residual_resources(self) -> None:
        original_git = pair_git._git

        def fail_second(cwd, args, *, check=True):
            if args[:2] == ["worktree", "add"] and str(args[4]).endswith("partial-b"):
                raise GitError(
                    "simulated failure",
                    command=["git", "worktree", "add"],
                    returncode=1,
                    stderr="simulated failure",
                )
            return original_git(cwd, args, check=check)

        with patch("scripts.pair_git._git", side_effect=fail_second):
            with self.assertRaises(GitError) as caught:
                create_worktrees(
                    os.fspath(self.repo), "partial", "HEAD", os.fspath(self.repo / ".codex-pair" / "worktrees" / "partial trees")
                )
        details = caught.exception.details
        self.assertEqual("a", details["completed_steps"][0]["role"])
        self.assertTrue(Path(details["completed_steps"][0]["path"]).exists())
        self.assertTrue(details["residual_resources"])
        self.assertTrue(details["worktree_root_created"])

    def test_validate_rejects_wrong_repo_branch_and_non_root_path(self) -> None:
        result = create_worktrees(os.fspath(self.repo), "validate", "HEAD", os.fspath(self.repo / ".codex-pair" / "worktrees"))
        a_path = Path(result["a"]["path"])
        common = inspect_repo(os.fspath(a_path))["common_dir"]
        valid = validate_worktree(os.fspath(a_path), common, result["a"]["branch"])
        self.assertTrue(valid["valid"])
        wrong_branch = validate_worktree(os.fspath(a_path), common, "wrong")
        self.assertFalse(wrong_branch["valid"])
        wrong_repo = validate_worktree(os.fspath(a_path), os.fspath(self.root / "not-common"), result["a"]["branch"])
        self.assertFalse(wrong_repo["valid"])
        subdir = validate_worktree(os.fspath(a_path / "docs"), common, result["a"]["branch"])
        self.assertFalse(subdir["valid"])

    def test_invalid_repo_and_allowed_paths_are_rejected(self) -> None:
        with self.assertRaises(GitError):
            inspect_repo(os.fspath(self.root))
        commit = self._commit_file(self.repo, "ok.txt", "ok\n", "ok")
        for invalid in ("../secret", "/absolute", "C:\\absolute", "a/../b", "a/./b"):
            result = check_delivery(os.fspath(self.repo), self.base, [commit], [invalid])
            self.assertFalse(result["passed"], invalid)

    def test_delivery_scope_supports_exact_files_and_trailing_slash_directories(self) -> None:
        exact = self._commit_file(self.repo, "allowed.txt", "yes\n", "allowed")
        nested = self._commit_file(self.repo, "docs/nested/中文.txt", "yes\n", "nested")
        passed = check_delivery(
            os.fspath(self.repo), self.base, [exact, nested], ["allowed.txt", "docs/"],
        )
        self.assertTrue(passed["passed"], passed["reasons"])
        refused = check_delivery(os.fspath(self.repo), self.base, [exact, nested], ["allowed.txt"])
        self.assertFalse(refused["passed"])
        self.assertIn("docs/nested/中文.txt", refused["files"])
        self.assertTrue(any("超出允许范围" in reason for reason in refused["reasons"]))

    def test_delivery_rejects_commit_from_another_branch(self) -> None:
        git(self.repo, "switch", "-c", "other", self.base)
        foreign = self._commit_file(self.repo, "foreign.txt", "no\n", "foreign")
        git(self.repo, "switch", "main")
        result = check_delivery(os.fspath(self.repo), self.base, [foreign], ["foreign.txt"])
        self.assertFalse(result["passed"])
        self.assertTrue(any("不属于当前任务分支" in reason for reason in result["reasons"]))

    def test_integrates_explicit_commits_and_rejects_dirty_receiver(self) -> None:
        result = create_worktrees(os.fspath(self.repo), "integrate", "HEAD", os.fspath(self.repo / ".codex-pair" / "worktrees"))
        a = Path(result["a"]["path"])
        b = Path(result["b"]["path"])
        first = self._commit_file(b, "one.txt", "1\n", "one")
        second = self._commit_file(b, "two.txt", "2\n", "two")
        integrated = integrate_commits(os.fspath(a), [first, second])
        self.assertTrue(integrated["success"])
        self.assertEqual([first, second], integrated["integrated_commits"])
        self.assertTrue((a / "one.txt").exists())
        (a / "dirty.txt").write_text("dirty\n", encoding="utf-8")
        with self.assertRaises(GitError) as caught:
            integrate_commits(os.fspath(a), [first])
        self.assertIn("不干净", str(caught.exception))

    def test_all_commit_ids_are_validated_before_integration_starts(self) -> None:
        result = create_worktrees(os.fspath(self.repo), "preflight", "HEAD", os.fspath(self.repo / ".codex-pair" / "worktrees"))
        a = Path(result["a"]["path"])
        b = Path(result["b"]["path"])
        source = self._commit_file(b, "source.txt", "source\n", "source")
        before = git(a, "rev-parse", "HEAD").stdout.strip()
        with self.assertRaises(GitError):
            integrate_commits(os.fspath(a), [source, "not-a-commit"])
        self.assertEqual(before, git(a, "rev-parse", "HEAD").stdout.strip())
        self.assertFalse((a / "source.txt").exists())

    def _make_conflict(self, pair_id: str) -> tuple[Path, Path, str, str]:
        result = create_worktrees(os.fspath(self.repo), pair_id, "HEAD", os.fspath(self.repo / ".codex-pair" / "worktrees" / f"trees-{pair_id}"))
        a = Path(result["a"]["path"])
        b = Path(result["b"]["path"])
        self._commit_file(a, "shared.txt", "from A\n", "A change")
        before = git(a, "rev-parse", "HEAD").stdout.strip()
        source = self._commit_file(b, "shared.txt", "from B\n", "B change")
        return a, b, before, source

    def test_conflict_is_preserved_and_can_continue(self) -> None:
        a, _b, _before, source = self._make_conflict("continue")
        result = integrate_commits(os.fspath(a), [source])
        self.assertEqual("conflict", result["status"])
        self.assertEqual(["shared.txt"], result["conflict_files"])
        self.assertEqual([], result["remaining_commits"])
        (a / "shared.txt").write_text("resolved\n", encoding="utf-8")
        git(a, "add", "shared.txt")
        continued = continue_integration(os.fspath(a))
        self.assertTrue(continued["success"])
        self.assertFalse(git(a, "rev-parse", "--verify", "--quiet", "CHERRY_PICK_HEAD", check=False).returncode == 0)

    def test_conflict_is_preserved_and_can_abort(self) -> None:
        a, _b, before, source = self._make_conflict("abort")
        result = integrate_commits(os.fspath(a), [source])
        self.assertEqual("conflict", result["status"])
        aborted = abort_integration(os.fspath(a))
        self.assertEqual("aborted", aborted["status"])
        self.assertEqual(before, git(a, "rev-parse", "HEAD").stdout.strip())
        self.assertEqual("from A\n", (a / "shared.txt").read_text(encoding="utf-8"))

    def test_continue_and_abort_require_cherry_pick_state(self) -> None:
        with self.assertRaises(GitError):
            continue_integration(os.fspath(self.repo))
        with self.assertRaises(GitError):
            abort_integration(os.fspath(self.repo))

    def test_project_local_defaults_are_ignored_without_global_changes(self) -> None:
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.write_text("# 保留本地规则\nlocal-only\n", encoding="utf-8")
        result = create_worktrees(str(self.repo), "local", "HEAD")
        state = self.repo / ".codex-pair" / "state" / "local"
        pair_git.prepare_pair_directory(str(self.repo), str(state), "state")
        (state / "state.sqlite3").write_bytes(b"database")
        self.assertEqual("", git(self.repo, "status", "--porcelain").stdout)
        self.assertTrue(Path(result["a"]["path"]).is_relative_to(self.repo / ".codex-pair"))
        self.assertIn("# 保留本地规则", exclude.read_text(encoding="utf-8"))
        self.assertEqual(1, exclude.read_text(encoding="utf-8").count("/.codex-pair/"))
        # linked worktree 的 .git 是文件，必须仍定位到 common dir。
        linked = Path(result["a"]["path"])
        pair_git.prepare_pair_directory(str(linked), str(linked / ".codex-pair" / "logs"), "logs")

    def test_pair_directory_rejects_outside_wrong_kind_files_and_auth(self) -> None:
        for target, kind in [(self.root / "outside", "state"),
                             (self.repo / ".codex-pair" / "worktrees", "state"),
                             (self.repo / "shared.txt", "state")]:
            with self.assertRaises(GitError):
                pair_git.prepare_pair_directory(str(self.repo), str(target), kind)
        auth = self.repo / ".codex-pair" / "state" / "account"
        auth.mkdir(parents=True)
        (auth / "auth.json").write_text("do not touch", encoding="utf-8")
        with self.assertRaises(GitError):
            pair_git.prepare_pair_directory(str(self.repo), str(auth / "child"), "state")
        with self.assertRaises(GitError):
            create_worktrees(str(self.repo), "external", "HEAD", str(self.root / "external"))

    def test_tracked_pair_content_is_never_hidden_or_overwritten(self) -> None:
        tracked = self.repo / ".codex-pair" / "keep.txt"
        tracked.parent.mkdir()
        tracked.write_text("user", encoding="utf-8")
        git(self.repo, "add", ".codex-pair/keep.txt")
        with self.assertRaises(GitError):
            pair_git.prepare_pair_directory(str(self.repo), str(tracked.parent / "state"), "state")
        self.assertEqual("user", tracked.read_text(encoding="utf-8"))

    def test_junction_escape_is_rejected(self) -> None:
        pair = self.repo / ".codex-pair"
        pair.mkdir()
        outside = self.root / "outside"
        outside.mkdir()
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(pair / "state"), str(outside)],
                           capture_output=True, check=True)
        else:
            (pair / "state").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(GitError):
            pair_git.prepare_pair_directory(str(self.repo), str(pair / "state" / "evil"), "state")
        self.assertFalse((outside / "evil").exists())


if __name__ == "__main__":
    unittest.main()
