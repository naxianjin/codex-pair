"""codex-pair 的 Git/worktree 安全操作。

本模块只使用 Python 标准库，并且导入时不访问文件系统或执行 Git。
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Sequence
from urllib.parse import urlsplit, urlunsplit


class GitError(RuntimeError):
    """Git 命令或 Git 前置条件失败。"""

    def __init__(
        self,
        message: str,
        *,
        command: Sequence[str] | None = None,
        returncode: int | None = None,
        stdout: str = "",
        stderr: str = "",
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.command = _redact_command(command or [])
        self.returncode = returncode
        self.stdout = _redact_text(stdout)
        self.stderr = _redact_text(stderr)
        self.details = details or {}

    def to_dict(self) -> dict:
        """返回可安全序列化并适合 CLI 展示的诊断。"""

        return {
            "message": str(self),
            "command": self.command,
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "details": self.details,
        }


def _redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if not parsed.scheme or not parsed.netloc or "@" not in parsed.netloc:
        return value
    host = parsed.netloc.rsplit("@", 1)[1]
    return urlunsplit((parsed.scheme, f"***@{host}", parsed.path, parsed.query, parsed.fragment))


def _redact_text(value: str) -> str:
    value = re.sub(
        r"(?i)(authorization\s*:\s*(?:basic|bearer)\s+)[^\s]+",
        r"\1***",
        value,
    )
    value = re.sub(r"(?i)(https?://)[^\s/@]+@", r"\1***@", value)
    value = re.sub(r"(?i)((?:password|token)\s*[=:]\s*)[^\s]+", r"\1***", value)
    return value.strip()


def _redact_command(command: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    hide_next = False
    for arg in command:
        if hide_next:
            redacted.append("***")
            hide_next = False
            continue
        lower = arg.lower()
        if lower in {"--password", "--token", "--authorization", "http.extraheader"}:
            redacted.append(arg)
            hide_next = True
        elif any(key in lower for key in ("authorization:", "password=", "token=")):
            redacted.append("***")
        else:
            redacted.append(_redact_url(arg))
    return redacted


def _git(
    cwd: str | os.PathLike[str],
    args: Sequence[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", os.fspath(cwd), *args]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise GitError(
            "无法启动 Git",
            command=command,
            returncode=getattr(exc, "winerror", None),
            stderr=str(exc),
        ) from exc
    if check and result.returncode != 0:
        raise GitError(
            "Git 命令执行失败",
            command=command,
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    return result


def _real(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _display_path(path: str | os.PathLike[str]) -> str:
    return os.path.realpath(os.path.abspath(os.fspath(path)))


def _repo_paths(project: str) -> tuple[str, str]:
    requested = _display_path(project)
    top = _git(requested, ["rev-parse", "--show-toplevel"]).stdout.strip()
    top = _display_path(top)
    common_raw = _git(top, ["rev-parse", "--git-common-dir"]).stdout.strip()
    common = _display_path(common_raw if os.path.isabs(common_raw) else os.path.join(top, common_raw))
    return top, common


def _current_branch(worktree: str) -> str | None:
    result = _git(worktree, ["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
    if result.returncode == 0:
        return result.stdout.strip()
    if result.returncode == 1:
        return None
    raise GitError(
        "无法读取当前分支",
        command=["git", "-C", worktree, "symbolic-ref", "--quiet", "--short", "HEAD"],
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _status_entries(worktree: str) -> list[str]:
    output = _git(
        worktree,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
    ).stdout
    return [entry for entry in output.split("\0") if entry]


def inspect_repo(project: str) -> dict:
    """检查仓库身份、HEAD、分支和工作树状态。"""

    root, common = _repo_paths(project)
    head = _git(root, ["rev-parse", "HEAD"]).stdout.strip()
    status = _status_entries(root)
    return {
        "root": root,
        "common_dir": common,
        "head": head,
        "branch": _current_branch(root),
        "dirty": bool(status),
        "status": status,
    }


def _validate_pair_id(pair_id: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", pair_id):
        raise GitError(
            "协作 ID 非法：仅允许 1–64 个字母、数字、点、下划线或连字符，且必须以字母或数字开头"
        )


def _branch_exists(repo: str, branch: str) -> bool:
    result = _git(repo, ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], check=False)
    if result.returncode not in (0, 1):
        raise GitError(
            "检查分支失败",
            command=["git", "-C", repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    return result.returncode == 0


def create_worktrees(project: str, pair_id: str, base_ref: str, worktree_root: str) -> dict:
    """从同一基准创建 A、B 分支和 worktree，不触碰原工作树修改。"""

    _validate_pair_id(pair_id)
    repo = inspect_repo(project)
    base = _git(
        repo["root"],
        ["rev-parse", "--verify", "--end-of-options", f"{base_ref}^{{commit}}"],
    ).stdout.strip()
    root = Path(_display_path(worktree_root))
    branches = {
        "a": f"codex-pair/{pair_id}/a",
        "b": f"codex-pair/{pair_id}/b",
    }
    paths = {
        "a": root / f"{pair_id}-a",
        "b": root / f"{pair_id}-b",
    }

    conflicts: list[str] = []
    for role in ("a", "b"):
        if paths[role].exists():
            conflicts.append(f"目录已存在：{paths[role]}")
        if _branch_exists(repo["root"], branches[role]):
            conflicts.append(f"分支已存在：{branches[role]}")
    if conflicts:
        raise GitError("拒绝覆盖已有目录或分支", details={"conflicts": conflicts})

    root_existed = root.exists()
    root.mkdir(parents=True, exist_ok=True)
    completed: list[dict] = []
    for role in ("a", "b"):
        command_args = ["worktree", "add", "-b", branches[role], os.fspath(paths[role]), base]
        try:
            _git(repo["root"], command_args)
        except GitError as exc:
            residual = [
                {"role": item["role"], "path": item["path"], "branch": item["branch"]}
                for item in completed
            ]
            if paths[role].exists() or _branch_exists(repo["root"], branches[role]):
                residual.append(
                    {"role": role, "path": os.fspath(paths[role]), "branch": branches[role]}
                )
            raise GitError(
                f"创建 {role.upper()} worktree 失败；已完成资源保持原状",
                command=exc.command,
                returncode=exc.returncode,
                stdout=exc.stdout,
                stderr=exc.stderr,
                details={
                    "completed_steps": completed,
                    "residual_resources": residual,
                    "worktree_root_created": not root_existed,
                    "worktree_root": os.fspath(root),
                },
            ) from exc
        completed.append(
            {
                "role": role,
                "path": _display_path(paths[role]),
                "branch": branches[role],
            }
        )

    return {
        "base_commit": base,
        "original_dirty": repo["dirty"],
        "original_changes_included": False,
        "a": {"path": completed[0]["path"], "branch": branches["a"]},
        "b": {"path": completed[1]["path"], "branch": branches["b"]},
    }


def validate_worktree(path: str, expected_common_dir: str, expected_branch: str) -> dict:
    """验证真实根路径、公共 Git 目录和当前分支。"""

    requested = _display_path(path)
    root, common = _repo_paths(requested)
    branch = _current_branch(root)
    reasons: list[str] = []
    if _real(requested) != _real(root):
        reasons.append("给定路径不是 worktree 根目录")
    if _real(common) != _real(expected_common_dir):
        reasons.append("公共 Git 目录与预期不符")
    if branch != expected_branch:
        reasons.append("当前分支与预期不符")
    return {
        "valid": not reasons,
        "path": root,
        "common_dir": common,
        "branch": branch,
        "reasons": reasons,
    }


def _normalize_allowed_path(value: str) -> tuple[str, bool]:
    if not isinstance(value, str) or not value.strip():
        raise GitError("允许路径不能为空")
    raw = value.replace("\\", "/")
    is_directory = raw.endswith("/")
    raw = raw.rstrip("/")
    pure = PurePosixPath(raw)
    raw_parts = raw.split("/")
    if (
        raw.startswith("/")
        or re.match(r"^[A-Za-z]:", raw)
        or pure.is_absolute()
        or any(part in ("", ".", "..") for part in raw_parts)
    ):
        raise GitError(f"允许路径必须是无越界部分的仓库相对路径：{value}")
    return pure.as_posix(), is_directory


def _path_allowed(path: str, rules: Sequence[tuple[str, bool]]) -> bool:
    return any(path == rule or (is_dir and path.startswith(f"{rule}/")) for rule, is_dir in rules)


def _resolve_commit(repo: str, commit: str) -> str | None:
    result = _git(
        repo,
        ["rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}"],
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _is_ancestor(repo: str, ancestor: str, descendant: str) -> bool:
    result = _git(repo, ["merge-base", "--is-ancestor", ancestor, descendant], check=False)
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise GitError(
        "无法验证提交祖先关系",
        command=["git", "-C", repo, "merge-base", "--is-ancestor", ancestor, descendant],
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _commit_files(repo: str, commit: str) -> list[str]:
    output = _git(
        repo,
        [
            "diff-tree",
            "--root",
            "--no-renames",
            "--no-commit-id",
            "--name-only",
            "-r",
            "-z",
            commit,
        ],
    ).stdout
    return [path for path in output.split("\0") if path]


def check_delivery(
    worktree: str,
    base_commit: str,
    commits: list[str],
    allowed_paths: list[str],
) -> dict:
    """验证交付提交属于当前任务分支且只修改允许路径。"""

    root, _ = _repo_paths(worktree)
    branch = _current_branch(root)
    reasons: list[str] = []
    files: set[str] = set()
    resolved: list[str] = []
    try:
        rules = [_normalize_allowed_path(path) for path in allowed_paths]
    except GitError as exc:
        return {
            "passed": False,
            "branch": branch,
            "commits": [],
            "files": [],
            "reasons": [str(exc)],
        }

    base = _resolve_commit(root, base_commit)
    if base is None:
        reasons.append(f"基准提交不存在：{base_commit}")
    if branch is None:
        reasons.append("worktree 当前为 detached HEAD，不属于任务分支")
    if not commits:
        reasons.append("交付提交列表为空")
    if not rules:
        reasons.append("允许路径列表为空")

    head = _git(root, ["rev-parse", "HEAD"]).stdout.strip()
    previous = base
    for supplied in commits:
        commit = _resolve_commit(root, supplied)
        if commit is None:
            reasons.append(f"提交不存在：{supplied}")
            continue
        resolved.append(commit)
        if not _is_ancestor(root, commit, head):
            reasons.append(f"提交不属于当前任务分支：{supplied}")
        if base is not None and not _is_ancestor(root, base, commit):
            reasons.append(f"提交不基于指定基准：{supplied}")
        if previous is not None and not _is_ancestor(root, previous, commit):
            reasons.append(f"提交顺序错误或提交链不连续：{supplied}")
        previous = commit
        for changed in _commit_files(root, commit):
            files.add(changed)
            if not _path_allowed(changed, rules):
                reasons.append(f"修改超出允许范围：{changed}")

    return {
        "passed": not reasons,
        "branch": branch,
        "commits": resolved,
        "files": sorted(files),
        "reasons": list(dict.fromkeys(reasons)),
    }


def _cherry_pick_in_progress(worktree: str) -> bool:
    result = _git(worktree, ["rev-parse", "--verify", "--quiet", "CHERRY_PICK_HEAD"], check=False)
    if result.returncode in (0, 1):
        return result.returncode == 0
    raise GitError(
        "无法检查 cherry-pick 状态",
        command=["git", "-C", worktree, "rev-parse", "--verify", "--quiet", "CHERRY_PICK_HEAD"],
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
    )


def _conflict_files(worktree: str) -> list[str]:
    output = _git(
        worktree,
        ["diff", "--name-only", "--diff-filter=U", "-z"],
    ).stdout
    return sorted(path for path in output.split("\0") if path)


def integrate_commits(worktree: str, commits: list[str]) -> dict:
    """按给定 SHA 顺序 cherry-pick；冲突时保留现场。"""

    root, _ = _repo_paths(worktree)
    if _cherry_pick_in_progress(root):
        raise GitError("已有 cherry-pick 正在进行；请先继续或中止")
    status = _status_entries(root)
    if status:
        raise GitError("接收方工作树不干净，拒绝整合", details={"status": status})

    requested: list[str] = []
    for supplied in commits:
        commit = _resolve_commit(root, supplied)
        if commit is None:
            raise GitError(f"提交不存在：{supplied}", details={"integrated_commits": []})
        if commit in requested:
            raise GitError(f"提交列表包含重复项：{supplied}", details={"integrated_commits": []})
        requested.append(commit)

    integrated: list[str] = []
    for index, commit in enumerate(requested):
        result = _git(root, ["cherry-pick", commit], check=False)
        if result.returncode != 0:
            if _cherry_pick_in_progress(root):
                return {
                    "success": False,
                    "status": "conflict",
                    "integrated_commits": integrated,
                    "pending_commit": commit,
                    "remaining_commits": requested[index + 1 :],
                    "conflict_files": _conflict_files(root),
                }
            raise GitError(
                "cherry-pick 失败",
                command=["git", "-C", root, "cherry-pick", commit],
                returncode=result.returncode,
                stdout=result.stdout,
                stderr=result.stderr,
                details={"integrated_commits": integrated},
            )
        integrated.append(commit)
    return {
        "success": True,
        "status": "completed",
        "integrated_commits": integrated,
        "remaining_commits": [],
        "conflict_files": [],
    }


def continue_integration(worktree: str) -> dict:
    """继续用户已经解决并暂存冲突的 cherry-pick。"""

    root, _ = _repo_paths(worktree)
    if not _cherry_pick_in_progress(root):
        raise GitError("不存在可继续的 cherry-pick")
    result = _git(root, ["-c", "core.editor=true", "cherry-pick", "--continue"], check=False)
    if result.returncode != 0:
        if _cherry_pick_in_progress(root):
            return {
                "success": False,
                "status": "conflict",
                "head": _git(root, ["rev-parse", "HEAD"]).stdout.strip(),
                "conflict_files": _conflict_files(root),
            }
        raise GitError(
            "继续 cherry-pick 失败",
            command=["git", "-C", root, "-c", "core.editor=true", "cherry-pick", "--continue"],
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    return {
        "success": True,
        "status": "completed",
        "head": _git(root, ["rev-parse", "HEAD"]).stdout.strip(),
        "conflict_files": [],
    }


def abort_integration(worktree: str) -> dict:
    """仅在存在 cherry-pick 状态时显式中止并恢复到操作前。"""

    root, _ = _repo_paths(worktree)
    if not _cherry_pick_in_progress(root):
        raise GitError("不存在可中止的 cherry-pick")
    _git(root, ["cherry-pick", "--abort"])
    return {
        "success": True,
        "status": "aborted",
        "head": _git(root, ["rev-parse", "HEAD"]).stdout.strip(),
        "conflict_files": [],
    }
