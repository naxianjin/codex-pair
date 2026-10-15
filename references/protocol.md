# codex-pair 协作协议

## 角色与状态

- A 是统筹方：创建协作、拆分任务、分配允许路径、验收证据并整合提交。
- B 是协作方：验证会话与 worktree、领取任务，只修改允许范围并提交交付。
- 状态位于 `$CODEX_HOME/codex-pair/<协作ID>/`。状态数据由 A 的通信与状态模块定义和维护；不得复制凭据到该目录。
- `init`、`join`、`assign`、`send`、`claim`、`complete`、`integrate`、`status`、`daemon`、`pause`、`resume`、`stop` 是统一命令入口，由 A 实现。
- 消息传输、自动唤醒和守护进程行为均为 **待通信模块验证**，在集成测试提供证据前不得声称已实现。

## 建立与加入

### A 创建协作

1. 使用 `inspect_repo(project)` 记录项目根目录、公共 Git 目录、当前 HEAD、分支和工作树状态。
2. 若原目录有未提交修改，保持原状，并向用户说明它们未进入协作基准。不得自动清理、暂存、提交或复制这些修改。
3. 选定明确的 `base_ref` 后调用 `create_worktrees`。它创建：
   - 分支 `codex-pair/<协作ID>/a`，目录 `<worktree_root>/<协作ID>-a`
   - 分支 `codex-pair/<协作ID>/b`，目录 `<worktree_root>/<协作ID>-b`
4. 两个 worktree 必须来自同一个基准 SHA 并共享同一 Git common dir。已有目录或分支会被拒绝，不强制覆盖。
5. 若第二步创建失败，报告异常中 `completed_steps` 和 `residual_resources`。不得删除归属不确定的目录或分支。

### B 加入协作

1. 从已有会话取得预期公共 Git 目录、B worktree 真实路径和分支。
2. 调用 `validate_worktree`。仅当 `valid` 为真时继续；不得在验证失败后另建仓库替代。
3. 对比协作 ID、基准 SHA、任务 ID、允许路径和当前状态。信息不一致时停止修改并向 A 报告实际值。

## 任务拆分与消息

- A 按互不重叠的仓库相对路径分配任务，并写明目标、基准 SHA、允许路径、验收条件和所需测试。
- 对同一文件或紧密耦合改动，只分配给一方；公共接口先确定契约，再让各方实现自己的文件。
- 提问包含任务 ID、缺失事实和阻塞影响。回复给出结论、依据以及是否可以继续。
- 阻塞报告包含已完成步骤、原始错误（凭据脱敏）、当前 Git 状态和需要 A 或用户决定的问题。
- `complete` 交付包含按顺序排列的提交 SHA、实际修改文件、测试命令与结果、已知限制。Git 模块测试通过不等于完整双 CLI 自动协作通过。
- 消息应有明确目的和停止条件；失败后采用有限重试。对方失联时记录状态并暂停，不无限互发消息。

## 允许路径规则

允许范围始终按仓库相对 POSIX 路径比较；输入中的反斜杠会转换为 `/`。

- `scripts/pair_git.py` 是明确文件，只匹配该文件。
- `tests/` 是目录规则，匹配 `tests/` 下任意深度的文件，但不匹配 `tests-other/file.py`。
- 目录必须以 `/` 结尾；不以 `/` 结尾的规则只作精确文件匹配。
- 空路径、绝对路径、盘符路径，以及包含 `.` 或 `..` 路径段的规则无效。
- 空允许列表不授权任何文件。

重命名会按 Git 报告的实际路径校验；任何报告路径超出范围都拒绝交付。A 应在分配时同时授权确需修改的源路径和目标路径。

## 交付验证与整合

1. B 在预期任务分支提交，不推送，不合并其他任务分支。
2. A 调用 `check_delivery(worktree, base_commit, commits, allowed_paths)`：确认基准和每个提交存在、提交可从当前任务分支 HEAD 到达、提交基于基准、顺序成立，并汇总实际文件。
3. `passed` 为假时拒绝整合，并将 `reasons` 反馈给 B。不要扩大允许范围来掩盖意外修改。
4. A 在接收方调用 `integrate_commits(worktree, commits)`。接收方必须无已跟踪、未跟踪或暂存修改，且没有进行中的 cherry-pick。
5. 只按已验收 SHA 的明确顺序 cherry-pick；不得直接合并整个协作分支。
6. 每次整合后运行任务测试和最终集成测试，再由 A 检查完整变更并验收。

## 冲突与恢复

- `integrate_commits` 返回 `status: conflict` 时保留 index、工作树和 `CHERRY_PICK_HEAD`，同时返回冲突文件。不得自动 reset 或覆盖文件。
- 用户或 A 明确解决冲突并 `git add` 后，调用 `continue_integration`。该接口只有在存在 cherry-pick 状态时才运行。
- 明确决定放弃本次 cherry-pick 时调用 `abort_integration`。该接口同样先确认 cherry-pick 状态。
- 继续失败且状态仍存在时，仍返回冲突状态；保留现场再次处理。

## 暂停、停止与审批

- `pause` 保留会话、任务和 worktree 状态；`resume` 必须重新验证会话及工作目录。
- `stop` 不等于授权删除分支、worktree 或状态。清理必须单独确认具体目标和归属。
- 命令需要审批时，将准确命令、目的和影响交给用户；审批被拒绝后暂停相关动作，不寻找绕过方式。
- 不扩大原任务授权，不把仓库凭据、令牌、环境机密或认证头写入消息、SQLite 或错误诊断。

## Git 接口返回结构

所有路径参数是字符串，`commits` 与 `allowed_paths` 是字符串列表，结果均可 JSON 序列化。Git 失败抛出 `GitError`；`to_dict()` 包含脱敏后的 `message`、`command`、`returncode`、`stdout`、`stderr` 和 `details`。

- `inspect_repo` → `{root, common_dir, head, branch, dirty, status}`
- `create_worktrees` → `{base_commit, original_dirty, original_changes_included, a: {path, branch}, b: {path, branch}}`
- `validate_worktree` → `{valid, path, common_dir, branch, reasons}`
- `check_delivery` → `{passed, branch, commits, files, reasons}`
- `integrate_commits` → 成功 `{success, status: completed, integrated_commits, remaining_commits, conflict_files}`；冲突另含 `pending_commit`，并在 `remaining_commits` 列出尚未尝试的 SHA
- `continue_integration` → `{success, status, head, conflict_files}`
- `abort_integration` → `{success, status: aborted, head, conflict_files}`

调用示例：

```python
from scripts.pair_git import check_delivery, integrate_commits

delivery = check_delivery(
    r"D:\work\pair-b",
    "<base-sha>",
    ["<task-commit-sha>"],
    ["scripts/pair_git.py", "tests/"],
)
if delivery["passed"]:
    result = integrate_commits(r"D:\work\pair-a", delivery["commits"])
```
