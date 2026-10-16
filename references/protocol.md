# codex-pair 协作协议

首次调用先读 [onboarding.md](onboarding.md)：主动发起者默认 A，收到用户转交的加入指令者为 B。双方保留独立 CODEX_HOME，通过共享状态与成员账号路由协作。角色提示不等于双账号传输已通过真实验收，不得统一双方登录目录。

默认接入当前两个窗口，不创建 worktree；用户选择新建隔离工作树时，新目录必须位于目标项目 `.codex-pair/worktrees/`。脚本通过 Git common dir 本地 exclude 设置 `/.codex-pair/` 排除。共享状态默认 `.codex-pair/state/<ID>/`，交接、日志与归档在对应子目录。旧状态保留原路径，不静默搬迁。维修清单见 [repair-multi-account.md](repair-multi-account.md)。

## 当前窗口入口

从 A 的当前 Codex 工具环境执行 `start --project <目标仓库> --goal <用户目标> --json`（长正文用 --goal-file）。它验证自身会话，使用随机 ID、原会话 cwd 与目标 workspace，不创建新 CLI，也不指定两个账号。

A 输出返回的 join_instruction；用户把它交给另一个已打开窗口，B 执行其中 `accept --invite <JSON路径> --json`。B 自动读取自身环境，重复接受复用绑定。invite 文件哈希、项目、基准与发起会话须匹配；B 不在目标仓库也不需要重开窗口，任务目录另存为 workspace。

start/accept 自动后台启动或复用本地桥，日志在项目 .codex-pair/logs，无需用户手工跑 daemon。`bridge --pair <ID> --state-root <返回路径> --action start|stop` 管理桥。启动有 OS 锁和随机挑战，存活不明时不重复启动；stop 不关闭原有 CLI。

accept 发出 window-handshake 问题，A 领取、发送关联 reply 并完成问题，B 领取完成回复。两个业务回执及模型回合均 completed 后，status.window_flow.communication 才为 verified；桥再发一条 window-ready 通知给 A，推动原目标继续。manual 始终表示手动，不因人工问答完成而宣称自动通信。

首次窗口选择采用用户转交一次邀请，不按账号名称、标题或最近历史会话猜 B。缺少 CODEX_THREAD_ID 时拒绝把普通终端当成当前窗口。

成员 cwd 是会话和工具调用身份，workspace（proof.git.path）是任务工作树。claim/complete 的 --worktree 保留旧接口语义，填 actor_cwd 并在该目录运行；提交校验、assign/integrate 使用 workspace。同 workspace 的代码任务串行，交付提交整合前持有该工作树；新回合等待共享工作树其他回合结束。已有不同 worktree 可并行。

## 角色与状态

角色由每轮数据库确定。普通 start 在项目锁内新建一轮，第一个登记窗口为 A，接受邀请者为 B；并发 start 的另一窗口取得同一邀请。start --resume-pair <ID> 才保留原角色，原 pause/resume 仍可用。原轮次的执行中、已领取、未整合、未完成交回或结果不明工作阻止自动换绑；安全结束后停止旧轮次并保留历史。处理旧信封不触发 start，旧邀请不能加入新轮次。新轮次默认 quota-balanced，旧轮次迁移后保持 legacy。

- A 是统筹方：创建协作、拆分任务、分配允许路径、验收证据并整合提交。
- B 是协作方：验证会话与 worktree、领取任务，只修改允许范围并提交交付。
- 新状态位于 `<项目>/.codex-pair/state/<协作ID>/`；全局 `--state-root` 指定共享 state 根目录，双方必须使用同一真实路径。旧 `$CODEX_HOME/codex-pair/<协作ID>/` 作为兼容记录保留，不静默迁移。状态数据由 A 的通信与状态模块定义和维护；不得复制凭据到该目录。
- `init`、`join`、`assign`、`send`、`claim`、`complete`、`integrate`、`status`、`daemon`、`relay`、`relay-stop`、`pause`、`resume`、`stop` 是统一命令入口，由统一入口实现。
- 本机已验证 Codex CLI 0.162.1、Python 3.14.5；运行需要 Python 3.10+。使用 [官方 queue](https://learn.chatgpt.com/docs/developer-commands?surface=cli) 和 [app-server](https://learn.chatgpt.com/docs/app-server)。脚本不改模型、登录或权限配置。

## 提供方与能力边界

仅支持官方 Codex CLI 的 OpenAI 官方账号与 DeepSeek API。DeepSeek 沿用原 CLI 已配置的 Responses API；桥只投递和观察，不代替另一窗口执行 API 推理，不接管其凭据。提供方与模型应依据目标 thread/read 的 modelProvider/model 识别，不把 account/read 的账号邮箱作为 API 必需条件。持久化 proof 使用身份和 Git 校验所需的白名单最小字段，不保存完整线程响应、认证头或 Key。status 中的提供方、模型及能力状态不得包含机密；未验证能力须明确标注，不能由提供方名称推定工具可用。实现与真实验收分别报告，详细清单见 [deepseek.md](deepseek.md)。

## 通信、回执与恢复

- 已有 CLI 默认 queue 后端，完整 UUID 与实际 cwd 从官方 API 验证。用参数数组调用 `codex queue`，正文封装为 ASCII JSON 避开 Windows 中文参数代码页损坏。已实测空闲自动启动、繁忙后排队；回执 Queued 仅说明入队。
- 旁路 app-server 只读目标元数据和回合，绝不 resume 已有 CLI。目标未加载时队列仍可持久化，不能声称已处理。历史 API 可能暂报 interrupted 且 completedAt 为空；只有持久化完成时间才能证明终态。
- `join --new-session` 使用 managed 后端，成员独占 app-server CLI，通过 turn/start 启动。仅恢复本协作创建的持久会话。shared proxy 是可选后端，本机 socket 曾报 os error 10050，未将它列为通过验收。
- delivery_status 是 pending/retry/delivering/accepted/unknown/failed；remote_status 是 queued/inProgress/有完成时间的 completed、failed、interrupted；process_status 是 unclaimed/claimed/completed。三者独立，模型结束不自动完成任务。
- 消息、信封与尝试先写入 SQLite，再调用传输。确定尚未发送的临时错误最多重试三次（可设 1–10）；发送后超时或崩溃进入 unknown，绝不自动重发。用消息 ID、原始信封和回合记录对账；查不到记录不是未投递证明。仍不明时保留记录，由操作者核实，禁止重派执行中的任务。
- 业务 complete 不释放尚在运行的模型回合；后续消息等待该回合结束。预检确认繁忙时尚未提交消息，记录 deferred 并等待，不消耗实际投递重试额度，不据此判断失联。
- SQLite 使用 WAL、外键和写事务。Windows junction 解析为真实有效 CODEX_HOME，未设置时用用户 ~/.codex。`join --member-codex-home` 保存每个成员实际账号，会话验证、投递、观察和恢复始终使用该成员账号，失败不得回退另一账号。新状态仅允许项目内经过真实路径、未跟踪内容和 Git 排除校验的 `.codex-pair/state` 子树。模式升级保留记录，旧成员无账号字段时继承原 home。
- 守护程序持有 OS 文件锁，不根据旧 PID、心跳或回复耗时抢占。Windows Job 清理其专属子进程；重启把 delivering 变成 unknown，对账 accepted/unknown，保留 claimed。status --probe 的随机挑战响应才确认可响应；无响应不等于失联。
- 保留原权限配置。服务端审批请求记录为 server_request_pending 事件，不自动批准。

## 统一入口示例

令 `$py` 为有效 Python 可执行路径，`$script` 为本技能 scripts/pair.py，`$pair` 为协作 ID。本机 WindowsApps 的 python 可能只是占位程序，请选择实际 Python。

```powershell
& $py $script init --pair $pair --project 'D:\work\project' --base-ref HEAD --worktree-root 'D:\work\project\.codex-pair\worktrees' --json
& $py $script join --pair $pair --state-root 'D:\work\project\.codex-pair\state' --member A --member-codex-home '<A账号目录>' --session '<A-UUID>' --worktree '<A目录>' --json
& $py $script join --pair $pair --state-root 'D:\work\project\.codex-pair\state' --member B --member-codex-home '<B账号目录>' --session '<B-UUID>' --worktree '<B目录>' --json
# 独占托管会话可用 --new-session 替代 --session。
& $py $script daemon --pair $pair --state-root 'D:\work\project\.codex-pair\state' --json
```

init 针对已有 Git 仓库；新项目先按授权初始化并提交最小骨架。省略 worktree-root 默认使用项目 `.codex-pair/worktrees`。两个计划目录都存在时验证复用，部分资源冲突只报告，绝不清理。所有命令有 --help、--json；公共选项可放在子命令前或后。成功 JSON 是 `{ok:true,result:...}`，失败是 `{ok:false,error,type,details}` 并非零退出。以下每条命令也需附加同一 `--state-root`，或从通知的 claim_argv 继承该路径。

中央 daemon 按目标成员账号路由。无法可靠接入双方时，分别在各自账号环境运行 relay，并保留同一 state-root：

```powershell
& $py $script relay --pair $pair --state-root '<共享state根目录>' --member A --member-codex-home '<A账号目录>' --json
& $py $script relay --pair $pair --state-root '<共享state根目录>' --member B --member-codex-home '<B账号目录>' --json
& $py $script relay-stop --pair $pair --state-root '<共享state根目录>' --member A --json
& $py $script relay-stop --pair $pair --state-root '<共享state根目录>' --member B --json
& $py $script stop --pair $pair --state-root '<共享state根目录>' --json
```

relay 通过成员 OS lease 防重复，仍服从 pause/stop，并复用 unknown 对账和 claim 去重。不能同时将文件信箱当作模型唤醒：`join --delivery-mode manual` 明确标为手动交接，消息生成 `.codex-pair/handoffs/<ID>/<message>.json`，状态 `delivery_status=manual_handoff`、`remote_status=manual`，不会调用模型。用户将通知转交目标 CLI，目标运行 claim_argv 后处理；自动模式用 `--delivery-mode auto`，需实测目标 CLI 能自动唤醒。

```powershell
& $py $script assign --pair $pair --task implementation --to A --title '实现' --allow 'src/adder.py' --body-file '.\implementation.txt' --json
& $py $script assign --pair $pair --task tests --to B --title '测试' --allow 'tests/' --body-file '.\tests.txt' --json
& $py $script send --pair $pair --from B --to A --kind question --task tests --body-file '.\question.txt' --dedupe-key 'tests-question-1' --json
& $py $script send --pair $pair --from A --to B --kind reply --task tests --reply-to '<问题ID>' --progress-note '已明确参数边界并给出示例' --body-file '.\answer.txt' --json
```

任务 pending → active → completed（无提交的工作）或 delivered（含提交）→ integrated。依赖只有 completed/integrated 后满足；同成员同时一个活跃任务；active/blocked 都持有范围。跨目录代码依赖需要 A 先整合到所需 worktree，再选正确基准分配后续任务；不会自动 rebase 或复制他人修改。

assign 增加 --report-to A/B（默认数据库 A），负责人和交接方分别持久化。complete 一律按 report_to 生成结果，包括交给自身；结果在原回合结束后进入下一回合。重复 complete 或 handoff --task <ID> --to A/B 复用结果，后者仅接受 completed/delivered/integrated，不改负责人、不重做业务；已有 claimed/unknown/failed 结果原样返回，不能借补发绕过去重或不明投递。complete --partial 表示中途交回，任务为 handed_back，不满足依赖，由统筹核实进度并登记剩余工作。结果消息 complete 仅收尾自身，不产生新的结果。

交付正文、证据路径及完整 SHA 保存在 deliveries；通知仅含有界摘要及记录引用。收到 required_deliveries 后，使用信封的 read_delivery_argv_template 按 next_offset 分页直到 null，再用返回 digest 执行 ack_delivery_argv_template。read-delivery 与 ack-delivery 均校验原成员身份及记录权限。确认按成员、记录版本与哈希保存，页区间必须完整覆盖；只读最后一页不能确认。结果消息及依赖任务交付前检查必要确认。确认只证明脚本读取记录，不替代实际核验证据。

分工时登记所有已明确的后续任务与 --depends-on，不将等待仅写在对话中。前置业务 complete 立即重新调度，桥每10秒兜底；有提交的任务仍等 integrated。后续消息带前置摘要、完整提交与交付引用。

新轮次采用 quota-balanced 策略：阶段任务完成或整合、所有结果消息收尾后，系统向 A 登记一条独立的 `pair-plan-` 规划任务，按阶段任务集合去重。结果回合只读完、确认并 complete；规划任务在原回合结束后由桥自动投递。规划任务的允许文件列表为空，只读取已完成交付并登记原目标内尚未完成、已获授权的后续任务及依赖；这些任务再由桥调度 A/B。规划 complete 前必须登记后续任务，或使用 `--goal-complete` 并附原目标完成的验收依据。规划任务不产生结果通知，避免自己触发自己；暂停、未整合、结果未读完、投递失败或结果不明时保持等待。目标完成后 status.window_flow.goal_completed 为 true，不再自动规划。旧 legacy 轮次保留原调度策略。

## 额度与负担

assign --to auto --effort 1/2/3/5/8（默认3）选择安全成员；固定指派、已激活、执行中、blocked、已交付任务不迁移。方案细化、审查、测试和整合准备均登记 effort。新轮次复杂任务（5、8）在可比较额度相差至少20个百分点时优先额度高者；70/50适用，70/51按负担均衡；轻任务按负担。40%～60%是软目标，依赖、范围、能力和工作树安全优先。已明确耗尽的成员不接新自动任务，两方均耗尽时保留 pending 并说明原因。

额度通过成员自身环境的 account/rateLimits/read 读取，启动读取、变化通知触发重读、后台60秒刷新；复杂派发前超过30秒请求刷新，最多等待本地查询预算后退回工作量，超过120秒过期。周期重置到时只触发重读，不能自行增加剩余额度。优先按可靠模型/limitId 对应选择 rateLimitsByLimitId，多桶映射不明时为 bucket_unknown；兼容单桶旧格式，不混用其他桶。有效周期剩余额度取 100-usedPercent 的最低值。周期长度不一致、未知、过期、提供方不支持时按工作量；百分比不代表不同套餐的相同绝对容量。同账号身份摘要及同 CODEX_HOME 作为共享资源池处理，不保存原始邮箱或认证响应。

额度线程与消息投递隔离，查询失败不阻塞桥；只允许必要只读账号 RPC，不批准请求、不兑换权益、不购买、不改登录或配置。thread/tokenUsage/updated 仅在 threadId/turnId 匹配本协作任务消息、含可靠 last.totalTokens 时计入；按回合主键更新，不累加重复通知，不使用账号总量或无关项目消耗。有实测任务用本协作实测校准 effort 单位并替换对应估算，缺少可靠用量时保留估算。

刷新失败记录 refresh_status=unavailable 与 attempted_at；最近一次仍未超过120秒且周期未重置的 fresh 快照保留有效性。复杂任务等待刷新超时也不能解除有效 remaining=0 快照的耗尽保护。

quota [--refresh] 与 status.quota 展示 observed_at、age_seconds、reliability、额度桶、周期与剩余比例；status.balance 展示负担、比例、策略及分配理由。status.waiting 同时列出等待前置、整合、结果领取/确认、后续安排、消息桥不可用或投递结果不明，不由心跳单独断言进程存活。

收到消息先运行信封 claim_argv。claimed=false 时立即结束，不能再次执行。保留 claim_token，complete 时提供实质结果。正文与结果可用 --body-file 输入 UTF-8（可带 BOM），保留中文、多行、引号、Windows 路径和原换行；与 --body 互斥。重复逻辑请求复用 --dedupe-key；同键不同正文会报错。

领取回执与输出恢复：自动信封使用 `claim --compact`，只输出消息标识、角色、类型和领取令牌，正文仍以原信封为准。完整领取输出为向后兼容保留，但长正文会超出工具的输出预算；`Warning: truncated output` 属于工具截断提示，不是可供 JSON.parse 的脚本 JSON。不要把 stderr 与 stdout 合并后解析，也不要用正则从已截断内容拼出 JSON。解析失败不证明事务失败，禁止自动重复领取或重发任务。

原领取者丢失回执时，先核对上一回合已结束、没有并行执行者及当前文件/提交进度，再在原 CLI 的 actor_cwd 执行 `recover-claim --member <原成员> --session <原UUID> --worktree <actor_cwd> --message <原消息ID>`，附同一 `--pair`、`--state-root`、`--json`。命令交叉检查自身 CODEX_THREAD_ID、CODEX_HOME、cwd 与官方 API，返回 `recovered=true`、原 claim_token、`resume_only=true`；它不返回新的 claimed=true，不改消息状态或生成新令牌。只有同一接收者仍为 claimed 才可恢复；业务 task 消息还须任务仍 active，result 等消息不能因原任务完成而拒绝恢复；已完成、协作 stopped 或身份不符时拒绝。恢复不是授权重复执行，必须从核实的进度继续。普通重复 claim 仍返回 false 并停止。

```powershell
# 必须在成员实际工作目录执行。
& $py $script claim --pair $pair --member B --session '<B-UUID>' --worktree '<B目录>' --message '<消息ID>' --json
& $py $script complete --pair $pair --member B --session '<B-UUID>' --worktree '<B目录>' --message '<消息ID>' --token '<claim_token>' --commit '<完整SHA>' --body-file '<结果文件>' --json
```

提问后结束当前回合，保留原任务令牌；不要阻塞轮询等待。问题和回复各自领取、完成；回复发送后完成问题消息，收到回复后完成回复消息再继续原任务。reply 必须关联同任务、方向相反的原问题；一问只计一次。没有 progress-note 的回复计为未进展一轮；三轮阻塞并只通知 A 一次。脚本根据显式证据计数，不声称能语义判断进展。

## 建立与加入

### A 创建协作

1. 使用 `inspect_repo(project)` 记录项目根目录、公共 Git 目录、当前 HEAD、分支和工作树状态。
2. 若原目录有未提交修改，保持原状，并向用户说明它们未进入协作基准。不得自动清理、暂存、提交或复制这些修改。
3. 选定明确的 `base_ref` 后调用 `create_worktrees`，本技能指定 `worktree_root` 为 `<项目>/.codex-pair/worktrees`。它创建：
   - 分支 `codex-pair/<协作ID>/a`，目录 `<worktree_root>/<协作ID>-a`
   - 分支 `codex-pair/<协作ID>/b`，目录 `<worktree_root>/<协作ID>-b`
4. 两个 worktree 必须来自同一个基准 SHA 并共享同一 Git common dir。底层创建函数拒绝已有资源；入口对完整已有计划验证后复用，不强制覆盖。
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
- 消息应有明确目的和停止条件；失败后采用有限重试。回复耗时长不判失联，结果不明时保留状态，不自动重新分配正在执行的任务。

## 允许路径规则

允许范围始终按仓库相对 POSIX 路径比较；输入中的反斜杠会转换为 `/`。

- `scripts/pair_git.py` 是明确文件，只匹配该文件。
- `tests/` 是目录规则，匹配 `tests/` 下任意深度的文件，但不匹配 `tests-other/file.py`。
- 目录必须以 `/` 结尾；不以 `/` 结尾的规则只作精确文件匹配。
- 空路径、绝对路径、盘符路径，以及包含 `.` 或 `..` 路径段的规则无效。
- 空允许列表不授权任何文件。
- 不支持通配符、.git 或盘符/ADS 路径。交付拒绝 merge、根提交、重复提交和基准提交本身。

重命名会按 Git 报告的实际路径校验；任何报告路径超出范围都拒绝交付。A 应在分配时同时授权确需修改的源路径和目标路径。

## 交付验证与整合

1. B 在预期任务分支提交，不推送，不合并其他任务分支。
2. A 调用 `check_delivery(worktree, base_commit, commits, allowed_paths)`：确认基准和每个提交存在、提交可从当前任务分支 HEAD 到达、提交基于基准、顺序成立，并汇总实际文件。
3. `passed` 为假时拒绝整合，并将 `reasons` 反馈给 B。不要扩大允许范围来掩盖意外修改。
4. A 在接收方调用 `integrate_commits(worktree, commits)`。接收方必须无已跟踪、未跟踪或暂存修改，且没有进行中的 cherry-pick。
5. 只按已验收 SHA 的明确顺序 cherry-pick；不得直接合并整个协作分支。
6. 每次整合后运行任务测试和最终集成测试，再由 A 检查完整变更并验收。

入口 `integrate --task <ID> [--worktree <接收目录>]` 自动暂停新投递，默认接收方 A，也可明确选择其他已绑定 worktree 或原项目。接收方不得有执行中任务或结果不明回合。指定 --commit 时必须与 complete 记录的顺序一致。SQLite 在每次 cherry-pick 前持久化 SHA、原 HEAD 和下标；冲突后 --continue 会继续当前和剩余提交。中断后核对 HEAD 父提交与稳定 patch-id，结果不明拒绝重复应用。已完成计划重复调用返回同一结果。--abort 仅中止当前冲突，之前成功提交仍保留并报告。

A 的当前 CLI 可在自身的串行工具调用中整合：必须同时匹配 CODEX_THREAD_ID 与真实工作目录，唯一在执行的投递必须是它已领取的当前消息，且开发任务已经完成。此例外不豁免其他任务、queued、delivering 或 unknown 请求。其他操作者仍等待接收方回合结束。

## 冲突与恢复

### 消息过长与领取失败

Windows queue 将通知放在命令参数中，ASCII JSON 会放大中文。长正文和任务保留在 SQLite，超过阈值的信封改发短通知：`content_external=true`、`body=null`、`task=null`。先执行 `claim_argv`；成功后运行 `read_argv`，用返回的 `next_offset` 追加 `--offset` 分页直到 null。任务另加 `--field task` 分页读取完整任务 JSON（含范围、基准、正文）。每页最多 2000 字符，不返回领取令牌或重复信封。完整读取后再执行。

`claimed=false` 的 `delivery_failed` 表示尚未领取但投递失败，不等于别人已经领取。不能执行正文或重复创建消息。管理员确认 status 中原消息错误为 `Queue message exceeds Windows command-line limit` 后，可运行 `recover-delivery --message <原消息ID> --pair <原协作ID> --state-root <原状态根目录> --json`。此入口只接受每次尝试都在发送前长度预检失败、无 queue/turn/远端状态且未领取的消息，保留原 ID、正文、去重键、错误与尝试历史，恢复 pending；桥仍需运行，协作 paused 时需按原意显式 resume。旧桥必须停止并从新版脚本启动，才能使用短通知。不把恢复 pending 或文件落盘报告成目标已经收到。

unknown、已接受、已领取、已完成或其他失败不适用此恢复；已领取且丢失回执遵循上文 recover-claim，绝不重置令牌或自动重新执行。

- `integrate_commits` 返回 `status: conflict` 时保留 index、工作树和 `CHERRY_PICK_HEAD`，同时返回冲突文件。不得自动 reset 或覆盖文件。
- 在已有任务授权内明确解决冲突并只暂存对应文件后，调用 `integrate --continue`。涉及未知用户修改或多种语义时请求用户决定，不强制覆盖。
- 明确决定放弃本次 cherry-pick 时调用 `abort_integration`。该接口同样先确认 cherry-pick 状态。
- 继续失败且状态仍存在时，仍返回冲突状态；保留现场再次处理。

## 暂停、停止与审批

- `pause` 与开始投递共用边界锁，返回后不开始新投递；已接受的 Codex 队列或回合可继续。resume 后每次投递重新验证会话和工作目录。
- `stop` 保留状态和代码；退出会清理 managed 子进程，未完成领取不自动重做。status --probe 3 --json 可确认守护程序挑战响应。
- `stop` 不等于授权删除分支、worktree 或状态。清理必须单独确认具体目标和归属。
- 命令需要审批时，将准确命令、目的和影响交给用户；审批被拒绝后暂停相关动作，不寻找绕过方式。
- 不扩大原任务授权，不把仓库凭据、令牌、环境机密或认证头写入消息、SQLite 或错误诊断。

## 验证与双窗口人工步骤

`python -m unittest discover -v` 运行本地测试。显式设置 CODEX_PAIR_REAL_TEST=1、CODEX_PAIR_REAL_CORE_TEST=1、CODEX_PAIR_REAL_INTEGRATION=1 后，可运行 tests.test_transport.RealTransportTests、tests.test_core.RealCoreTests、tests.test_integration.RealIntegrationTests。双账号集成测试还须设置 `CODEX_PAIR_HOME_A` 与 `CODEX_PAIR_HOME_B`，OpenAI 官方账号通过 account/read 的匿名身份摘要确认不同真实账号。此条件不套用于 DeepSeek API；API 会话按提供方、模型、UUID 与 cwd 核实，不能将不同 home 视为不同账号的证明。旧真实测试可能保留原验证状态位置；新双账号验证证据和 worktree 使用项目 `.codex-pair/`。真实测试只创建独立测试会话，集成 worktree 保留。不能把单账号真实测试或模拟路由测试报告成双账号通过。

在目标 Codex 会话内可用 `join --current-session` 替代手填 `--session`；它要求环境中的 CODEX_THREAD_ID 和当前实际 cwd，仍通过本成员账号的 app-server 验证会话。普通 PowerShell 中没有该上下文时会报错，不会猜最近 UUID。

旧数据库留在原 home/codex-pair/<ID>，只从原账号查找，不扫描其他账号。首次打开 schema 1/2/3/4 时，取得旧 daemon 与派发锁、确认无未决投递后，先创建 schema-v<版本>-<时间>.backup.sqlite3，再事务升级到 schema 5。补充旧账号字段、移除成员 cwd 唯一限制并增加窗口邀请设置，外键须完整。旧成员继承原 home，历史、领取令牌和路径不搬动。低层 init 默认项目内隔离布局，普通使用走 start/accept；旧 Python Store API 未传 state_root 时保留旧路径语义。

两个可见原窗口的验收优先使用上述 start/accept 邀请和后台桥，不要求第三个终端。以下仅为低层手动测试：在 A/B 各自目录启动 codex，让各自用实际 UUID 执行 join；第三个终端运行 daemon。按上述命令分配任务、提问、关联回复、提交交付，再 integrate 并测试。记录 UUID、消息 ID、提交 SHA 和 status，不能只检查 Queued 回执。禁止模拟键盘或根据窗口标题推测 UUID。未执行的可见双窗口验收必须明确列为未完成。

## Git 接口返回结构

所有路径参数是字符串，`commits` 与 `allowed_paths` 是字符串列表，结果均可 JSON 序列化。Git 失败抛出 `GitError`；`to_dict()` 包含脱敏后的 `message`、`command`、`returncode`、`stdout`、`stderr` 和 `details`。

- `inspect_repo` → `{root, common_dir, head, branch, dirty, status}`
- `prepare_pair_directory(project, path, kind)` → 真实目录字符串；kind 仅支持 state、worktrees、handoffs、archives、logs，验证对应项目内子树、真实路径、账号路径和未跟踪内容，设置并验证 common Git dir 本地排除后创建。
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
