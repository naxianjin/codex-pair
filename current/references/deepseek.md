# DeepSeek 接入边界与验收

> 开发候选说明：本页保留v1.2.1的双窗口基础/历史限制。新候选采用最多四个可见原CLI、一个受管隐藏审核者及新轮次6%保留线；新规则与入口见 [team-commands.md](team-commands.md)，未验证项及复测步骤见 [validation-handoff-four-cli.md](validation-handoff-four-cli.md)。旧文中的“只能两个”“不新增任何会话”、不同home限制和9%新轮次默认值不覆盖本轮规格。当前禁止安装或推送待验证候选。


当前仅支持两个用户已打开的官方 CLI 原窗口 A/B，不支持多人扩容。最新结论及历史测试边界见 [测试汇总](validation-summary-20261019.md)；下文历史版本号不代表当前包版本。

下述 v1.1.3 基础验收为历史记录；后续受限审批、分页和撤销等结果见测试汇总。2026-10-16 已完成 OpenAI A × DeepSeek B 的真实原窗口基础测试：邀请、双向握手、工具写入回读、技能 CLI、专项单元和结果确认。测试后半程使用用户明确授权的 Full access / never；组合矩阵与未验证项如下，完整记录见 [验收记录](verification.md)。

本文适用于官方 Codex CLI 中已配置 DeepSeek API 的原窗口。支持范围仅为 OpenAI 官方账号与 DeepSeek API，不因兼容接口而扩展其他提供方。普通入口、消息恢复和 Git 交付继续遵守 [protocol.md](protocol.md)。

## 原窗口与配置边界

- 用户先在原 CLI 配好可用的 Responses API；技能不安装、写入或修复 API 配置，不切换 CODEX_HOME、模型或权限，不收集、复制、输出 Key。
- A 用 start 发起，用户把邀请交给自己选中的 B，B 在自身工具环境 accept。不猜 API 窗口、账号目录或最近 UUID，不另建 CLI 来冒充原窗口成功。
- 每个成员的 UUID、实际 cwd、有效 CODEX_HOME 仍须验证；workspace 与 actor_cwd 分开，同一工作树的任务与模型回合仍串行。
- 中央桥按目标成员环境调用 queue 并观察回合，只承担投递和观察。推理与 API 凭据由原 CLI 自身处理；桥不得借用 A 的凭据替 B 推理，失败不得回退其他成员账号。
- sandbox/approval 拒绝应保留原始错误并附中文解释，去掉其中的凭据。模型能回答不证明能执行工具；不得自动放宽沙箱、审批或其他权限。
- 旁路观察失败须报告为未验证。可显式使用逐成员 relay；原窗口接口仍不可用时使用 manual，并明确手动交接。两者均不得把队列接受或文件落盘说成原窗口自动协作成功。

## 提供方与能力证据

核心实现从目标 thread/read 元数据识别 modelProvider/model，采用仅支持 OpenAI 与 DeepSeek 的明确判定，缺失或未知值应报告限制，不自动推测兼容。DeepSeek 不要求 account/read 返回 OpenAI 邮箱；OpenAI 双账号验收仍按原匿名身份摘要确认。

proof 只保留会话身份、提供方/模型、cwd、持久性及 Git 校验所需的白名单字段，不持久化完整 thread/read 响应或任意嵌套元数据。status.members[].connection 包含 provider、model、metadata_status、source。source=binding_snapshot 表示加入时的绑定快照，不是持续探测；metadata_status 为 verified、missing 或 unsupported，只表明提供方元数据判定，不能证明 API、工具或自动通信可用。旧 proof 缺少提供方时返回 missing，不重写历史记录。

能力分开取证：API 对话、工具执行、原窗口 queue 唤醒、回合终态观察、业务 claim/complete、关联问答与交付。一个能力通过不得替代其他能力。

## 状态字段与只读环境隔离

status.members[].capabilities 分别展示以下证据；这些状态只描述已有记录，不构成一次新的实机测试：

| 字段 | 状态含义 |
| --- | --- |
| session_identity | verified：已完成会话绑定身份核验 |
| queue_delivery | accepted：auto/queue 成员至少有一条接受回执；否则 unverified |
| turn_completion | verified：自动模式至少有一条回合持久化完成记录；否则 unverified |
| business_processing | verified：至少有一条业务 complete；否则 unverified |
| automatic_communication | verified：自动模式的关联握手双方业务与回合均完成；manual：手动模式；否则 unverified |
| task_tools | 始终 unverified，工具能力必须另行取证 |

单条历史记录不代表所有消息均完成，也不证明所有能力可用。观察层遇到 completed/failed/interrupted 且 completedAt 为空时按 inProgress 保留，不能从历史合成终态判定回合完成。若官方会话日志存在同会话、同回合的持久化 task_complete 事件，可按协作协议的目录与身份核对后补充确认；缺失事件时仍等待，不用超时推断完成。

metadata_only 的 app-server 只允许 initialize、thread/read、thread/turns/list RPC；start、resume、create_session 同时设有拒绝门。QueueTransport 与只读观察进程通过 control_environment 构造子环境，剔除 OPENAI_BASE_URL、原 CODEX_THREAD_ID 和 *_API_KEY、*_AUTH_TOKEN、*_ACCESS_TOKEN、*_BEARER_TOKEN，保留目标成员自身 CODEX_HOME，不修改父进程环境。该隔离不删除磁盘配置，也不表示磁盘里不存在凭据；桥仍不得加载另一窗口凭据替其推理。

新增绑定的 proof 只落盘身份与 Git 白名单。TransportError、RpcError、证据回调及观察错误对已支持的 Key/Bearer 文本和敏感字典字段脱敏；这不保证任意自然语言都不含秘密，任务内容仍禁止放入真实凭据。

## 验收矩阵与证据层级

| 配对 | 必测消息方向 | 真实原窗口状态 |
| --- | --- | --- |
| DeepSeek A × DeepSeek B | A→B、B→A | 待执行 |
| OpenAI A × DeepSeek B | A→B、B→A | 基础握手、任务交付与工具验证通过；完整 Git 交付矩阵未执行 |
| DeepSeek A × OpenAI B | A→B、B→A | 待执行 |

分别记录三层结果，不能合并为一个“通过”：

1. 模拟测试：提供方判定、字段缺失、拒绝未知提供方、敏感字段白名单、队列/回合状态、旧库和旧 proof 兼容；不调用真实 API。
2. 独立真实测试会话：证明测试会话自身 API、工具和传输能力，不证明用户选择的可见原窗口会自动唤醒。
3. 可见原窗口：用户已打开且选中的 A/B，真实执行邀请、握手、双向业务任务、交付与整合，才可报告该组合的原窗口协作结果。

本次使用用户接受邀请的现有 DeepSeek CLI 会话，官方只读元数据确认 modelProvider=deepseek、model=deepseek-flash；不是根据模型自述或配置目录名推断。通信达到 verified，B 实际领取任务、执行测试并 complete；A 分页读取全部交付、ack 并收尾，独立规划任务最终记录 goal_completed=true。权限切换时旧执行回合被用户中断，同一会话核对进度后继续，续行回合的 task_complete 已核对；没有把旧 interrupted 当作正常完成，也没有重复创建标记文件。

## 本次权限环境与 Windows 临时目录

此前原 DeepSeek 窗口在 Python 3.13.9 下执行 15 项专项用例，因测试初始化创建的临时目录内写入被拒而出现 30 个错误（每个用例的初始化及清理各一次），退出码 1、无跳过。原始 `PermissionError: [WinError 5]` 证据保留；不是 15 项断言全部失败，也不是提供方不支持技能的证明。仅移动 TEMP/TMP/TMPDIR 的变体同样失败。

本机探针表明，Windows 上 `0o700` 创建方式没有保留沙箱 capability 的写入 ACL，默认 `mkdir()` 创建的目录继承 ACL 后可写。v1.1.3 只调整 `tests/test_deepseek.py` 的 Windows 模拟目录：使用随机名称、默认 `mkdir()` 和创建成功后的清理；其他系统继续使用 `TemporaryDirectory`，15 个测试方法和断言未改。这批目录只存模拟数据，不用于真实凭据或生产文件；未修改系统 ACL。

后续原窗口实测使用已有 Python 3.12.13。用户授权并在原窗口选择 Full access / never 后，独立文件排他创建与第二进程回读、技能 CLI status、15 项专项单元均通过，退出码 0、跳过 0。解释器、测试目录修复与实际权限均与最初失败环境不同，所以不能将通过单独归因于任一变化，也不能声称受限沙箱故障已经验证修复。

默认不放宽权限。若用户明确要求调整，区分磁盘默认值和运行中回合：修改 `config.toml` 不能证明当前回合已生效；使用目标原窗口的 `/permissions` 核对模式。旧回合仍持有旧权限时，结束旧回合，在同一会话保留原令牌并从核实的进度继续，禁止重复 claim 或覆盖既有产物。Full access 取消本地沙箱边界，不是 DeepSeek 接入的默认前提。

本机尝试远程共享控制通道曾返回 `os error 10050`，没有用远程 resume、新建模型会话或模拟键盘绕过；权限切换由用户在原窗口完成。执行方对已授权的常规步骤直接推进，缺失信息先向统筹方报告，工具的强制审批仍须遵守。

## 每种组合的真实验收步骤

1. 记录 CLI 版本、组合、两个原窗口 UUID、cwd、workspace、提供方与模型；不记录 Key、认证头、环境变量值或原始配置。核实各窗口能在原权限下完成最小对话及一个获授权的可逆工具操作，分别记录结果。
2. 原窗口 A start、用户选择的 B accept；保留 pair ID、消息 ID 和目标会话映射。验证技能没有新建 CLI/model 会话、替换 home 或擅自修改配置；用户明确授权的权限调整必须另记调整前后实际状态。
3. 让桥投递握手问题，A 原子 claim、关联 reply、complete，B claim/complete 回复，双方模型回合具有持久化完成时间后检查 status.window_flow.communication=verified。Queued/accepted、单方 complete 或无 completedAt 的 interrupted 均不足。
4. 在每个方向发送一个明确任务并取证：目标窗口从空闲自动启动、繁忙时排队且不重复执行、业务领取成功、工具操作产生预期结果、complete 与模型回合完成。共享工作树内按串行顺序执行，不以并发写同一暂存区测试传输。
5. 双向各做一次关联 question/reply，核对 reply_to、消息 ID、claim 令牌与业务终态。重复 claim 必须 false 且停止；长任务用 compact。仅原回执丢失时按协议 recover-claim 核实进度后续做，不能重跑已完成步骤。
6. 按任务允许路径提交完整 SHA，complete --commit 交付，A 验证实际文件、基准与提交顺序后整合并检查结果；共享工作树交付期间由当前任务持有，不同时编辑。
7. 在隔离测试状态验证超时/崩溃后的 unknown 不自动重发、重启保留 claimed、旧状态/旧 proof 能读取且记录不搬迁。不得故意中断正在交付的用户任务来制造故障。
8. 核查新状态、日志、信封、proof 与诊断只含最小必要元数据，无认证材料；使用模拟敏感值检验脱敏，不把真实 Key 注入测试或输出。旧记录中已有扩展字段的处理由实现明确说明，不能宣称所有历史数据已自动清理。
9. 记录错误原文（凭据脱敏）、复现步骤、实际结果与限制。观察失败时分别报告 relay/manual 的结果；manual 不计入自动矩阵。

验收记录至少包含：组合/方向、证据层级、pair/message/turn ID、回合持久化终态、业务终态、工具结果、提交 SHA、已通过项目与未执行项目。最终完整流程验收必须在核心实现后运行；本文档提交不代表实现或上述真实验收已完成。
