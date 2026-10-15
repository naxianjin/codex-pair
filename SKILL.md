---
name: codex-pair
description: Coordinate two Codex CLIs on one Windows Git project through isolated worktrees, explicit task commits, scoped delivery checks, and ordered integration. Use when starting, joining, operating, pausing, or recovering a codex-pair collaboration; do not use for ordinary single-agent Git work.
---

# Codex Pair

Coordinate one lead CLI (A) and one collaborator CLI (B) without widening the user's authorization.

## Before acting

- Read [references/protocol.md](references/protocol.md) for roles, commands, state, task lifecycle, path rules, failure handling, and Git return shapes.
- Confirm the project root, collaboration ID, role, expected worktree, shared Git directory, branch, and session state before changing anything.
- Treat transport, automatic wake-up, and daemon behavior as **待通信模块验证** until A's communication module provides verified evidence.

## Operating rules

- A creates the collaboration and worktrees; B joins the existing collaboration. Never create a replacement repository when validation fails.
- Split work into non-overlapping owned paths. Ask concise questions, report blockers with evidence, and deliver explicit commit SHA(s) plus tests.
- Preserve local modifications: never clean, stage, commit, copy, reset, or overwrite them automatically. State clearly that pre-existing uncommitted changes are outside the collaboration baseline.
- Validate every delivery against its base, task branch, and allowed repository-relative paths. A integrates only approved SHA(s), in the stated order, after confirming the receiving worktree is clean.
- On conflict, preserve the scene and ask the user to resolve and stage files before continuing, or explicitly abort. Never auto-reset or overwrite files to resolve it.
- Require fresh authorization for actions outside the task, including elevated commands, network access, credential use, pushing, or destructive cleanup. Never copy credentials into messages or state.
- Bound retries and messages. If the peer is unavailable, report status and pause instead of messaging indefinitely.

Use `scripts/pair_git.py` only through argument arrays or the unified CLI supplied by A. Do not install this unintegrated skill or mutate another role's branch.
