---
name: codex-pair
description: 让用户当前打开的两个官方 Codex CLI 窗口协作，通过邀请加入、本地消息桥问答、分工、交付与整合。支持 OpenAI 官方账号与已配置 DeepSeek API 的官方 Codex CLI，不支持其他提供方；不固定账号目录，不另开模型会话，普通单窗口 Git 操作无需启用。
---

# Codex Pair

## 适用范围

本技能适用于官方 Codex CLI 的 OpenAI 官方账号与已配置的 DeepSeek API，可同类或混合配对；不支持其他提供方。各窗口保留自身 CODEX_HOME、模型与权限设置。本项目由个人维护，并非 OpenAI 官方项目。

涉及 DeepSeek 时，先读 [接入边界与验收清单](references/deepseek.md)。仅使用原 CLI 已配置的 Responses API，不安装或改写 API 配置，不收集或复制 Key。模型对话成功、工具执行成功、原窗口自动协作通过必须分别验证。

协作对象是用户选中的两个现有窗口。同账号或不同账号均可；各窗口的账号目录、会话 UUID 和实际 cwd 由自身环境与 CLI API 验证，用户无需配置是哪两个 .codex-*。A 统筹整合，B 参与开发，双方均可提问与反馈。

## 判断当前输入

- 收到消息信封：先在 actor_cwd 运行 claim_argv，只有 claimed=true 才处理；不要重新 start。
- 收到用户转交的 CODEX_PAIR_JOIN_V1：本窗口是 B，读取邀请中的技能路径并执行 accept 命令；不要另建协作。
- 用户在本窗口调用技能并给出项目目标：本窗口发起 A，先读 [onboarding.md](references/onboarding.md)。
- 普通 start 建立新一轮，第一个成功登记的窗口为数据库 A，接受本轮邀请者为 B；只有 start --resume-pair <ID> 明确恢复时保留旧角色。口头分工不能改变底层角色。

## 当前窗口入口

脚本为本技能目录 scripts/pair.py。确认可运行的 Python 3.10+，不用 WindowsApps 占位程序。以实际路径执行：

```text
<Python> <pair.py> start --project <目标仓库> --goal <用户目标> --json
```

从本 Codex 窗口的工具环境执行。start 验证本窗口，保留原有 CLI 和会话工作目录，在目标项目 .codex-pair/ 建立共享状态、邀请和日志，自动启动可探测的后台消息桥；不会新建 CLI/model 会话或强制创建 worktree。长目标可用 UTF-8 --goal-file。

首次配对时，将返回的 join_instruction 作为正常文本完整输出，说明“把这段贴到你当前打开的另一个 Codex 窗口”。B 执行邀请中的完整命令后，自动读取 B 自己的环境并加入，不询问账号目录或 UUID。B 没安装技能也可读取邀请指向的同一份技能。

项目级锁保证并发 start 只生成一轮。另一窗口获得 requires_accept=true 与已有邀请，接受后才登记为 B。旧协作有执行中、已领取、未整合或结果不明工作时拒绝自动换绑并列出待处理项；安全结束后停止旧协作、保留历史。旧邀请与信封不能进入新轮次，处理信封不得重新配对。无法可靠识别另一窗口时，通过邀请确认目标，不按窗口标题、最近会话或账号目录顺序猜选。

## 加入后自动协作

accept 经本地桥发起关联问答验证。双方按信封领取、回复和完成后，status.window_flow.communication 才变为 verified，随后桥通知 A 按原目标分工。B 按 after_join 结束当前回合，给新消息进入下一轮的机会；不要阻塞轮询等回复。

awaiting_peer/awaiting_handshake 只表示等待加入/验证；queue 接受回执不等于模型已回复。若接口无法唤醒原窗口，明确报告限制。manual 只表示用户转交消息的手动模式，不是自动通信成功；不模拟键盘，不擅自新建 managed 会话。

开发与交付遵守 [protocol.md](references/protocol.md)：允许范围、原子领取、关联问答、完整 SHA 交付与整合。任务文件和 Git 操作用 workspace，claim/complete 用 actor_cwd，二者可能不同。同一工作树的代码任务和模型回合串行，已有独立 worktree 可并行。保留用户修改，不把用户未提交文件一起提交。

分工时把已明确的后续工作登记为任务，用 --depends-on 保存依赖；不要只在对话里说“做完我再继续”。assign 支持 --to auto、--effort 1/2/3/5/8（默认3）及 --report-to A/B（默认本轮数据库A）。方案细化、审查、测试和整合准备也要登记工作量，避免统筹独揽。新轮次默认额度感知均衡，旧轮次保留原策略；额度未知时使用工作量，不推算套餐容量。

完成任务后按 report_to 生成结果，即使交给自身也排队等待当前回合结束。结果消息仅阅读交付及收尾，绝不重跑原任务，也不生成新的结果通知。信封有 required_deliveries 时，逐条运行 read_delivery_argv_template，以 next_offset 分页至 null，再按返回 digest 执行 ack_delivery_argv_template。任务交付前会检查前置记录确认，结果消息完成前会检查本次交付确认。证据引用也须实际检查，不把脚本读取确认当作理解或测试证据。

仅中途交回工作时用 complete --partial，任务为 handed_back，不满足依赖；由统筹核实已完成进度并登记剩余工作，不能宣称整体完成。遗失交接可显式 handoff --task <ID> --to A/B，复用已有结果或创建一次补发，不改负责人、不重做代码测试。已领取、unknown 的旧消息先核实；recover-claim 对结果消息不要求原业务任务仍 active。

quota [--refresh]、status.quota、status.balance 显示数据时间、可靠性、负担和理由。额度后台60秒刷新、变化通知触发重读；复杂自动任务派发前超过30秒请求刷新，超过120秒或已到重置时间的数据不作为可用额度。查询失败不阻塞桥，不兑换重置权益、不购买、不修改配置。依赖、范围、能力、Git安全优先，仅尚未派发的自动任务可重新选择成员。等待由本地脚本完成，模型结束回合。

结果消息只读取、确认和收尾。新轮次的阶段任务完成或整合、所有结果消息收尾后，系统自动登记一条 `pair-plan-` 开头的独立规划任务给 A；桥在原模型回合结束后投递，无需用户再次输入。规划任务先完整读取并确认前置交付，再核对原目标；有已授权的剩余工作就登记具体任务、允许路径与依赖，然后完成规划任务并结束回合，让桥继续调度双方。原目标全部实现时，使用 `complete --goal-complete` 附验收依据结束规划任务，停止接续。规划任务不改业务文件、不重跑已完成任务、不重新 start，也不循环生成结果通知。需要新增授权或缺失关键信息时报告具体缺口，不自行扩大目标。

自动领取使用 claim_argv 中的 --compact，只返回小型领取凭据。工具输出截断或 JSON 解析失败不代表领取失败，不自动重复 claim。若原回执遗失，先确认原回合已结束且无工作在执行，再由原领取会话在 actor_cwd 显式执行 recover-claim --member <原成员> --session <原UUID> --worktree <actor_cwd> --message <原消息ID>（附原 pair/state-root 与 --json）。仅 recovered=true 才取回原令牌；核实已完成进度后继续原任务，不从头重复执行、不重派。不满足条件则报告限制。

长通知的 content_external=true 时，领取成功后运行 read_argv，按 next_offset 追加 --offset 分页读到 null；任务还须用 --field task 读完允许范围与基准。不可把空 body 当作无任务。claimed=false 且 reason=delivery_failed 时不执行；经 status 确认发送前长度预检失败，可显式 recover-delivery --message <原ID>（附原 pair/state-root 与 --json），仅 recovered=true 才恢复同一消息派发。不复制正文另派一份；unknown 或已有领取不能用此入口。长输入使用 --body-file。详细恢复见 protocol。

停止桥用 bridge --pair <ID> --state-root <返回路径> --action stop；恢复桥用 --action start。普通用户无需手工开第三个终端跑 daemon。pause/resume、relay、手动交接、旧状态和隔离 worktree 的低层入口见 protocol。

不改登录/模型/权限配置，不复制认证，不移动已有会话，不删除 worktree，不推送。新产物留在目标项目 .codex-pair/ 并使用本地 Git 排除。维修及安装诊断才读 [repair-multi-account.md](references/repair-multi-account.md)。
