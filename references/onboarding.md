# 当前两个窗口的发起与加入

未发布源码新增了任务内 A 协助执行及低额度交接，详见 [assistance.md](assistance.md)。新 start 默认开启额度交接、A 代答及原生权限审批委托；较低权限的 B 首次在 Hook 安装后输入 /hooks 并审阅、信任对应项，详见 [approval-review.md](approval-review.md)。任务内代执行仍需显式 --execution-assistance task-scoped。每次继续项目工作先检查返回的 checkpoint_check 或信封 checkpoint_check_argv，有交接才分页读取并核实接管；没有则不读取历史。旧轮次迁移保留关闭策略。以下 v1.1.3 原窗口验收不覆盖新增流程。

普通使用不把维修时的 .codex-b/.codex-c、协作 ID 或测试 worktree 当成用户默认配置。

当前源码版本 v1.1.3。两个 OpenAI 原窗口的自动规划接续已有历史实测；OpenAI A × DeepSeek B 的基础握手、工具写入回读、技能 CLI 和专项单元测试也已通过。DeepSeek 侧测试后半程使用用户授权的 Full access / never，完整组合和 Git 交付矩阵仍未验证，详见 [验收记录](verification.md)。升级后显式读取新版 SKILL.md，运行中的旧桥须安全停止并用新版脚本启动。

## 窗口 A

1. 用户在本窗口调用 $codex-pair 并给出目标时，确认目标仓库，读取适用 AGENTS.md，检查 Git 状态。可发现的信息自行查询，不让用户提供账号目录或 UUID。
2. 从当前技能的 scripts/pair.py --help 确认接口，选择实际 Python，执行 start --project <仓库绝对路径> --goal <目标> --json；长正文可用 UTF-8 --goal-file。不传固定 codex-home，不另建模型会话。
3. start 使用本窗口 CODEX_THREAD_ID、有效 CODEX_HOME 与实际 cwd，经 app-server 验证后登记 A。会话 cwd 可在项目外；任务目标另记为 workspace，不虚假登记已改变会话根目录。
4. 没有 B 时完整输出返回的 join_instruction。它填实 Python、技能路径与邀请路径，用户只需贴到已经打开的另一个窗口，无需修改账号或 UUID。
5. 桥由脚本后台启动，日志在项目内，随机挑战确认存活；已有桥复用。启动未确认时保留 PID、日志并报告，不反复创建 daemon。
6. 结束当前回合等待 B。收到连接验证问题后先领取、发送关联 reply、complete 问题，再结束回合。两个回合和业务回执均完成后，桥给 A 一条 window-ready 通知；确认 communication=verified 后按原目标分工，不要求用户重复描述项目。

普通 start 发起新轮次，原来是 B 的窗口也可成为新 A。只有明确 start --resume-pair <ID> 才复用旧角色；pause/resume 命令继续兼容。项目锁下并发发起只产生一轮，另一窗口返回已有邀请与 requires_accept=true，执行 accept 后登记为 B。旧工作未安全收尾时列出待处理项并停止新建，不能口头换角色，也不自动重做或换绑。

## 窗口 B

1. 用户粘贴 CODEX_PAIR_JOIN_V1 时，读取邀请指向的最新 SKILL.md，即使 B 自己的环境尚未安装此技能。
2. 在这个现有 CLI 的自身工具环境执行邀请中的 accept --invite <实际JSON路径> --json。不新建协作或 CLI，不切换 CODEX_HOME。
3. accept 校验邀请哈希、项目、基准和 A 绑定，再从 B 自身环境验证会话。重复接受会复用绑定及连接验证消息。
4. 按 after_join 执行。自动模式下结束当前回合，收到 A 的 reply 后领取并完成。手动模式明确需要用户转交消息，不能承诺后台唤醒。

分工登记后续任务、依赖、effort 和 report_to。A/B 均以本轮数据库为准。建议普通任务用 assign --to auto --effort 3；复杂任务用5或8，系统在安全条件内参考额度与工作量选择。固定 --to A/B 保持指派。前置有提交时必须先整合到后续工作所需工作树。收到交付引用后先分页读取并 ack-delivery，再交付后续任务。没有工作时结束模型回合，由桥10秒兜底检查；额度60秒刷新。

DeepSeek 窗口沿用同一 start/accept 流程，必须由用户选中原窗口；先读 [DeepSeek 接入与验收](deepseek.md)。不要求提供 Key，不替窗口配置 API，不用 OpenAI 账号邮箱验证代替 API 会话身份验证。


回合检测对 A、B 使用相同规则。非拥有者 API 可能返回 `notLoaded`，或把已结束回合合成为缺少完成时间的 `interrupted`；这些状态及等待时长都不单独证明完成。桥会只读官方返回的本账号会话日志，核对目录、会话和 cwd，按准确 `turn_id` 匹配持久化 `task_complete`。旧通知已经处理完成且有后续回合开始并完成时，可确认旧回合被取代并保存证据引用；最新回合仍在执行、日志不完整或证据缺失时继续等待，不重派业务。无需每次交接重新加入协作。

## 窗口识别边界

窗口身份是经验证的会话 UUID 与自身环境，不是 Windows 标题或 .codex-* 名称。A 不能仅凭“两个窗口”可靠知道所有环境里哪一个会话是 B；首次的一次邀请用于确认用户选中的目标，加入后后续消息自动路由。

缺少 CODEX_THREAD_ID 或 API 验证失败时保留原错误，不选最近历史会话，不把普通 PowerShell 伪装成当前 CLI。不擅自用 managed 新会话替代用户窗口。已有窗口需加载新版技能时显式读取最新 SKILL.md，不要求重开窗口。

## 工作目录与并发

start 保留两个窗口的原会话 cwd：当前 cwd 属于目标仓库时沿用该工作树，否则使用用户指定的项目根目录作为 workspace。任务工具使用 workspace，领取/完成工具使用 actor_cwd。

同工作树的代码任务和自动投递回合串行，避免共享暂存区冲突；问答跨回合交接。已有不同 worktree 时允许不重叠的并行任务。用户明确选择隔离工作树模式时才使用低层 init，不为并行偷偷重建窗口。

新状态在 <项目>/.codex-pair/state/<随机ID>/，邀请在 handoffs，日志在 logs。写入前校验真实路径、源文件与 Git common dir 的 info/exclude；本地 /.codex-pair/ 排除不得隐藏已跟踪内容。保留原有修改、历史与旧协作，不静默搬迁。

## 避免反复审批与重复确认

任务中已经授权的常规步骤由执行方继续完成，遇到缺失信息先向 A 报告，不重复向用户询问是否运行每条命令。工具审批由 CLI 的实际权限决定，技能正文不能代替权限切换。

修改磁盘默认配置不证明正在运行的回合已切换。用户选择 Full access 时，在目标原窗口使用 `/permissions` 确认当前模式；若当前回合仍显示旧权限，先结束旧回合，再在同一会话从核实的进度继续，保留领取令牌，不重复 claim 或重建已生成文件。Full access 取消本地沙箱边界，只在用户明确选择时使用；它不是 DeepSeek 的默认使用要求。

任务协议见 [protocol.md](protocol.md)；维修、旧状态和安装诊断见 [repair-multi-account.md](repair-multi-account.md)。维修中的目录实例和分工不决定普通协作窗口。
