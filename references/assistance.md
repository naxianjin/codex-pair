# 协助执行、额度交接与恢复

本页描述未发布的源码功能。历史 v1.1.3 原窗口验收不包含这些新增流程。普通窗口入口新建协作时默认开启低额度交接；旧状态迁移保留关闭设置。A 协助执行始终需要显式开启。

所有命令在本窗口 `actor_cwd` 执行，并保留信封中的 `--pair`、`--state-root`、`--codex-home` 和 `--json`。下文的 `<actor>` 表示 `--member A/B --session <本窗口UUID> --worktree <actor_cwd>`。CLI 会校验当前 cwd、账号目录及官方会话身份，不能通过填写 A 的名字借用它的身份。

## A 在当前任务授权内协助执行

新建协作可使用 `start --execution-assistance task-scoped`。已存在协作由 A 使用：

```text
<Python> <pair.py> <公共参数> assistance-policy <actor> --execution on
```

B 必须已经领取父任务，并保留其消息 ID 和领取令牌。请求使用 UTF-8 JSON，字段必须完整；工作树不可写时优先使用 --request-stdin，不为提交请求而先申请创建请求文件：

```json
{
  "argv": ["python", "project_script.py", "--output", "result.txt"],
  "cwd": "D:/work/project",
  "purpose": "生成用户要求的结果文件",
  "paths": ["result.txt"],
  "expected_effects": "在任务工作树内写入 result.txt",
  "authorization_basis": "用户已明确要求生成这个文件",
  "reason": "capability_unavailable"
}
```

- B 使用 `request-execution <actor> --message <父消息ID> --token <父令牌> --request <唯一ID> --request-file <JSON文件>`，随后结束回合；父任务进入 `waiting_execution`，不能提前 complete。
- 也可使用 `--request-stdin` 从标准输入直接提交相同 JSON，与 --request-file 二选一。PowerShell 将完整 JSON 放入单引号 here-string，通过管道传给 Python；不要写 D 盘临时请求文件，不使用恢复前的 load() 内存变量拼接命令。协调命令若也受限，由官方 Hook 交给 A 单次审核，不能冒用 A 身份或直接改数据库。
- 同一请求重试复用 ID 和相同内容；改变命令、目录或影响范围必须使用新 ID，且先核实旧请求。路径必须在父任务允许范围内；这项检查不能证明命令实际没有其他副作用。
- A 按信封领取并完整读取请求和父任务，核对用户原始授权、完整脚本、参数、重定向及预期副作用。`authorization_basis` 是 B 的说明，不是用户新授权。不得因 A 能运行就自行批准，也不得绕过用户拒绝或系统禁止。
- A 审核清楚后运行 `begin-execution <actor> --request <ID> --token <请求消息领取令牌> --digest <请求摘要> --body-file <审核依据>`。仅返回 `status=running` 时，才通过 A 自己的正常工具执行返回的准确 argv/cwd；工具的审批规则继续有效。桥不运行任何请求命令。
- A 使用 `finish-execution <actor> --request <ID> --token <同一令牌> --outcome success/failed/declined/needs_authorization/unknown --result-file <JSON结果>`。结果包含 `summary`；实际执行后还必须提供原 `argv`、`cwd`、整数 `exit_code` 和 `evidence`，并记录 stdout、stderr、实际改动及回读证据。非零退出码不能声明成功。
- `finish-execution` 关闭请求消息并生成独立结果记录，不能再用普通 complete 关闭请求。B 领取结果，按信封分页 read-delivery、ack-delivery，核对实际产物后 complete 结果消息。成功、失败或拒绝均只恢复父任务，未完成整个任务。
- 授权不清时向用户说明缺口；明确拒绝的操作不得重新包装转交。`unknown` 和 `needs_authorization` 保持阻塞。A 核实现场或取得必要授权后，使用 `resolve-execution <actor> --request <ID> --body-file <核实证据>`，只解除阻塞，不重跑命令。已有结果必须先由 B 读完并收尾。

开始执行后回合异常结束而未交付，桥会标记 `unknown`。再次 begin 被拒绝；不能因为没有 stdout 或等待时间较长就推断没执行过。诊断输出会脱敏，但提交前仍须主动去掉密钥、密码和认证内容。

恢复原会话后，桥只在同一账号和 UUID 的日志中看到新回合已持久化完成，并有当前任务真实获准的协调记录或已登记的协助请求时，才释放旧回合的调度占用。父任务和领取令牌保持不变，不能据此宣称业务完成；新回合仍运行、缺少证据或存在业务执行未知时继续等待。

验收分别记录是否经过权限审核，以及是否需要用户逐次手动同意。用户首次信任 Hook 后由 A 自动审核，不等于取消审批；不能因协调命令经过了 A 审核就判定自动审批验收失败。普通权限探针失败只证明 B 的实际限制，不伪造已经提交协助请求或完成业务写入。

## 额度触发与保存

新窗口协作默认开启；旧协作由 A 使用 `assistance-policy <actor> --checkpoints on` 开启。`status.assistance` 展示开关、负责人、保留线、窗口状态、执行请求和检查点。

初始保留线为剩余 9%，取适用周期的最低值。只有可靠、未过期的额度参与判断；共享账号池合并最低值，不把换窗口当作增加额度。DeepSeek 当前没有可靠额度读取，保持未知，不按模型名或对话长度推算百分比。每次开始较大任务前先 `quota --refresh`，低于保留线时不接新业务。

达到保留线时，桥只登记一次 `checkpoint` 通知并停止向该窗口派发业务。正在执行的工具必须先结束；通知在原回合结束后进入。窗口也可在工具结束后主动保存，避免等通知才行动：

```text
<Python> <pair.py> <公共参数> save-checkpoint <actor> --message <已领取任务或交接通知ID> --token <对应令牌> --body-file <现场说明>
```

现场说明写明目标及授权、已完成、未完成、验证结果、未提交改动、阻塞、正在运行或结果不明的操作和下一步。脚本补入任务、完成记录及消息现场；领取令牌不写进文档。没有已领取通知时可以省略 message/token。保存成功后立即结束回合，不再修改业务文件，也不使用 complete --partial 代替额度交接。

固定文件为项目内 `.codex-pair/checkpoints/<窗口标识摘要>.md`，使用本地 Git 排除。标识关联本窗口账号目录与会话；恢复原会话沿用同一文件。每窗口只留一份正式文档，未消费文件不得覆盖。临时写入后原子替换；失败保留现场，不宣称成功。

如本次交接前后额度桶、资源池和重置时间一致，才用实测百分比消耗更新保留线：`max(原保留线, 2 × 本次消耗 + 2)`，上限 95%。不使用 token 数换算套餐额度。9% 仍不能保证够写完交接。

## 接管、阅读与清理

每次通过本技能开始或继续项目工作，先运行信封的 `checkpoint_check_argv`，或 `check-checkpoint <actor>`。只检查固定路径，不递归扫描，不读取其他项目；没有文档直接继续。技能不能拦截绕过它的所有 CLI 操作。

有文档时使用 `read-checkpoint <actor> --source A/B [--offset <next_offset>]`，分页至 null。接管前核对文件、任务和操作证据，然后：

```text
<Python> <pair.py> <公共参数> consume-checkpoint <actor> --source A/B --digest <返回摘要> --body-file <核实后的恢复计划>
```

脚本验证全部页面已读、文件摘要未变、原操作结束，持久化恢复计划与任务归属后才清理文档。无法直接删除时尝试系统回收站；仍失败就保留已消费标记，报告 `cleanup=retained`，不提权强删。可由原窗口 `recover-checkpoint <actor>` 重试清理。文件变更时保留原文件，不删除未知内容。

另一窗口额度充足且使用同一工作树时，可接管剩余任务及统筹职责；原数据库 A/B 身份、权限和账号不变。新统筹方由 `status.assistance.coordinator` 指定，后续自动规划发给它。原领取消息闭合，不算业务完成；未满足的依赖仍等待。额度恢复的旧窗口不自动抢回工作。

不同 worktree 的未提交修改不自动搬运；存在未知执行或未确认结果交付时不自动换负责人。普通问题随交接移交决策，审批资格不随之转移，详见 [approval-review.md](approval-review.md)。先保存文档并核实现场，必要时完成 Git 交付再转交。恢复窗口继续原任务时，可通过 recover-claim 取回原消息令牌，先核对已完成部分，不能从头重做。

## 双方暂停与恢复

双方额度都低时分别保存现场。桥继续本地只读刷新，模型结束回合，不轮询。可靠额度至少达到该窗口保留线加 5 个百分点后，才投递恢复通知，最高要求恢复至 100%；不因到达重置时间自行假定额度增加。窗口关闭时等待用户重新打开原会话，不新建模型会话。

已拒绝的操作不能换请求 ID 继续尝试。后续确有新增用户授权时，由 A 核对并用 resolve-execution 记录依据，状态变为 resolved；这不批准系统禁止的操作，也不自动执行旧命令。

收到 `checkpoint_resume` 后先读完、consume-checkpoint，再 complete 恢复通知并继续剩余任务。确认阅读只证明读取了记录，仍须核对现场；不得承诺模型永久记住。实际恢复依据保存在项目状态库中。

额度不可读取时不自动恢复。用户明确要求继续原窗口后，可运行 `resume-checkpoint <actor> --body-file <用户要求继续的依据>`，记录为人工确认，不宣称额度验证通过；已知额度仍低于恢复线时拒绝。

若文件已落盘但状态事务中断，check-checkpoint 会报告 `Unregistered checkpoint` 并保留文件。原窗口用 recover-checkpoint 核对项目、会话及任务版本后登记原文件，不重做业务；任务已变化时停止并保留现场。未结束的协助执行、未接管文档或保存中的窗口会阻止自动规划新阶段。

## 兼容与验证范围

状态库升级至 schema 6，先按旧机制备份并取得 daemon/派发锁，保留成员、令牌、交付及分页确认。升级前先停止旧版桥，再以新版启动；不要让两个版本同时操作同一状态库。普通业务交付仍使用 `delivery:<任务ID>`，辅助结果使用 `execution:<请求ID>`，不会误当作依赖已完成。

新增自动化测试使用临时目录、模拟传输和真实的本地可撤销文件命令。它们不能证明两个真实 CLI 的权限差异、额度恢复唤醒或系统回收站在受限权限下通过；新增流程的原窗口验收应单独记录。
