# 当前两个窗口的发起与加入

普通使用不把维修时的 .codex-b/.codex-c、协作 ID 或测试 worktree 当成用户默认配置。

## 窗口 A

1. 用户在本窗口调用 $codex-pair 并给出目标时，确认目标仓库，读取适用 AGENTS.md，检查 Git 状态。可发现的信息自行查询，不让用户提供账号目录或 UUID。
2. 从当前技能的 scripts/pair.py --help 确认接口，选择实际 Python，执行 start --project <仓库绝对路径> --goal <目标> --json；长正文可用 UTF-8 --goal-file。不传固定 codex-home，不另建模型会话。
3. start 使用本窗口 CODEX_THREAD_ID、有效 CODEX_HOME 与实际 cwd，经 app-server 验证后登记 A。会话 cwd 可在项目外；任务目标另记为 workspace，不虚假登记已改变会话根目录。
4. 没有 B 时完整输出返回的 join_instruction。它填实 Python、技能路径与邀请路径，用户只需贴到已经打开的另一个窗口，无需修改账号或 UUID。
5. 桥由脚本后台启动，日志在项目内，随机挑战确认存活；已有桥复用。启动未确认时保留 PID、日志并报告，不反复创建 daemon。
6. 结束当前回合等待 B。收到连接验证问题后先领取、发送关联 reply、complete 问题，再结束回合。两个回合和业务回执均完成后，桥给 A 一条 window-ready 通知；确认 communication=verified 后按原目标分工，不要求用户重复描述项目。

已有有效绑定时复用原协作和角色。目标冲突时报告，不静默改写。用户明确另建时先停止旧协作并保留历史，不替换正在运行的绑定。

## 窗口 B

1. 用户粘贴 CODEX_PAIR_JOIN_V1 时，读取邀请指向的最新 SKILL.md，即使 B 自己的环境尚未安装此技能。
2. 在这个现有 CLI 的自身工具环境执行邀请中的 accept --invite <实际JSON路径> --json。不新建协作或 CLI，不切换 CODEX_HOME。
3. accept 校验邀请哈希、项目、基准和 A 绑定，再从 B 自身环境验证会话。重复接受会复用绑定及连接验证消息。
4. 按 after_join 执行。自动模式下结束当前回合，收到 A 的 reply 后领取并完成。手动模式明确需要用户转交消息，不能承诺后台唤醒。

## 窗口识别边界

窗口身份是经验证的会话 UUID 与自身环境，不是 Windows 标题或 .codex-* 名称。A 不能仅凭“两个窗口”可靠知道所有环境里哪一个会话是 B；首次的一次邀请用于确认用户选中的目标，加入后后续消息自动路由。

缺少 CODEX_THREAD_ID 或 API 验证失败时保留原错误，不选最近历史会话，不把普通 PowerShell 伪装成当前 CLI。不擅自用 managed 新会话替代用户窗口。已有窗口需加载新版技能时显式读取最新 SKILL.md，不要求重开窗口。

## 工作目录与并发

start 保留两个窗口的原会话 cwd：当前 cwd 属于目标仓库时沿用该工作树，否则使用用户指定的项目根目录作为 workspace。任务工具使用 workspace，领取/完成工具使用 actor_cwd。

同工作树的代码任务和自动投递回合串行，避免共享暂存区冲突；问答跨回合交接。已有不同 worktree 时允许不重叠的并行任务。用户明确选择隔离工作树模式时才使用低层 init，不为并行偷偷重建窗口。

新状态在 <项目>/.codex-pair/state/<随机ID>/，邀请在 handoffs，日志在 logs。写入前校验真实路径、源文件与 Git common dir 的 info/exclude；本地 /.codex-pair/ 排除不得隐藏已跟踪内容。保留原有修改、历史与旧协作，不静默搬迁。

任务协议见 [protocol.md](protocol.md)；维修、旧状态和安装诊断见 [repair-multi-account.md](repair-multi-account.md)。维修中的目录实例和分工不决定普通协作窗口。
