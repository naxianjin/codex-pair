---
name: codex-pair
description: 让用户当前打开的两个官方 Codex CLI 窗口协作，通过邀请加入、本地消息桥问答、分工、交付与整合。仅适用于 OpenAI 官方账号登录，不适用于其他 AI 的 API；不固定账号目录，不另开模型会话，普通单窗口 Git 操作无需启用。
---

# Codex Pair

## 适用范围

本技能仅适用于通过 OpenAI 官方账号登录的官方 Codex CLI，不适用于其他 AI 的 API、第三方模型提供方或兼容 API 接入方式。账号目录可以不同，但参与窗口都应满足此范围。本项目由个人维护，并非 OpenAI 官方项目。

协作对象是用户选中的两个现有窗口。同账号或不同账号均可；各窗口的账号目录、会话 UUID 和实际 cwd 由自身环境与 CLI API 验证，用户无需配置是哪两个 .codex-*。A 统筹整合，B 参与开发，双方均可提问与反馈。

## 判断当前输入

- 收到消息信封：先在 actor_cwd 运行 claim_argv，只有 claimed=true 才处理；不要重新 start。
- 收到用户转交的 CODEX_PAIR_JOIN_V1：本窗口是 B，读取邀请中的技能路径并执行 accept 命令；不要另建协作。
- 用户在本窗口调用技能并给出项目目标：本窗口发起 A，先读 [onboarding.md](references/onboarding.md)。
- 已有有效绑定优先，再次调用不交换角色，不替换其他窗口。

## 当前窗口入口

脚本为本技能目录 scripts/pair.py。确认可运行的 Python 3.10+，不用 WindowsApps 占位程序。以实际路径执行：

```text
<Python> <pair.py> start --project <目标仓库> --goal <用户目标> --json
```

从本 Codex 窗口的工具环境执行。start 验证本窗口，保留原有 CLI 和会话工作目录，在目标项目 .codex-pair/ 建立共享状态、邀请和日志，自动启动可探测的后台消息桥；不会新建 CLI/model 会话或强制创建 worktree。长目标可用 UTF-8 --goal-file。

首次配对时，将返回的 join_instruction 作为正常文本完整输出，说明“把这段贴到你当前打开的另一个 Codex 窗口”。B 执行邀请中的完整命令后，自动读取 B 自己的环境并加入，不询问账号目录或 UUID。B 没安装技能也可读取邀请指向的同一份技能。

已经绑定 B 时复用现有协作，无需再次转交。无法可靠识别另一个窗口时，采用这一次邀请确认目标；不按窗口标题、最近会话或账号目录顺序猜选，不能宣称尚未选择的窗口已收到指令。

## 加入后自动协作

accept 经本地桥发起关联问答验证。双方按信封领取、回复和完成后，status.window_flow.communication 才变为 verified，随后桥通知 A 按原目标分工。B 按 after_join 结束当前回合，给新消息进入下一轮的机会；不要阻塞轮询等回复。

awaiting_peer/awaiting_handshake 只表示等待加入/验证；queue 接受回执不等于模型已回复。若接口无法唤醒原窗口，明确报告限制。manual 只表示用户转交消息的手动模式，不是自动通信成功；不模拟键盘，不擅自新建 managed 会话。

开发与交付遵守 [protocol.md](references/protocol.md)：允许范围、原子领取、关联问答、完整 SHA 交付与整合。任务文件和 Git 操作用 workspace，claim/complete 用 actor_cwd，二者可能不同。同一工作树的代码任务和模型回合串行，已有独立 worktree 可并行。保留用户修改，不把用户未提交文件一起提交。

停止桥用 bridge --pair <ID> --state-root <返回路径> --action stop；恢复桥用 --action start。普通用户无需手工开第三个终端跑 daemon。pause/resume、relay、手动交接、旧状态和隔离 worktree 的低层入口见 protocol。

不改登录/模型/权限配置，不复制认证，不移动已有会话，不删除 worktree，不推送。新产物留在目标项目 .codex-pair/ 并使用本地 Git 排除。维修及安装诊断才读 [repair-multi-account.md](references/repair-multi-account.md)。
