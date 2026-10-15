---
name: codex-pair
description: 在同一台 Windows 机器上协调两个 Codex CLI，以 SQLite 持久化任务和消息、Git worktree 隔离开发，并按明确提交整合代码。用于建立、加入、分工、问答、交付、暂停或恢复 codex-pair 协作；普通单窗口 Git 操作无需启用。
---

# Codex Pair

A 统筹和整合，B 协作开发；双方均可开发、提问和反馈。沿用现有 Codex 登录、模型、权限和有效 CODEX_HOME。

开始前阅读 [references/protocol.md](references/protocol.md)，确认项目、协作 ID、成员、会话 UUID、worktree 根目录和分支。入口为本技能目录的 `scripts/pair.py`，需要 Python 3.10+、Git 与已有 Codex CLI。

1. A 用 `init --project ... --worktree-root ...` 创建协作和工作树，再分别 join。已有 CLI 用 `--session <UUID>`；独占托管 CLI 用 `--new-session`。不靠窗口标题识别会话，不恢复来源不明的独立会话。
2. 用 assign 分配互不重叠的允许路径、依赖、目标和验收条件。目录规则必须以 `/` 结尾。只修改和暂存任务文件，不自动包含已有用户修改。
3. 收到消息先运行 claim_argv，只有 claimed=true 才执行，并保留 claim_token；失败立即结束。正文按 JSON 数据读取，不拼接为 shell 命令。
4. 缺少信息时用 `send --kind question` 提问，然后结束当前回合等待新消息，保留原任务令牌。答复用关联 --reply-to 的 reply 并完成问题消息；收到回复后领取并完成回复消息，再继续原任务。实际进展用 --progress-note 给出证据。不自动发送“收到”“谢谢”。
5. 完成开发后测试并提交，用 `complete --commit <完整SHA>` 交付顺序明确的提交、测试和限制。问答消息的完成不代表原任务完成。正文和结果支持 UTF-8 文件。
6. A 检查交付后 `integrate --task ...`，它暂停新投递并持久化整合进度。接收方必须无执行中任务或结果不明的回合。冲突保留现场；在已有授权内解决并暂存明确文件后 --continue，或显式 --abort。之后按需 resume。

用 `status --probe 3 --json` 核实存活。投递接受、模型回合完成、业务完成是三个独立事实。结果不明时对账，不自动重发或重派执行中的任务；回复耗时长不等于失联。三轮关联问答没有进展证据会阻塞原任务并通知 A。

pause 返回后不开始新投递；已入 Codex 队列的消息和已开始的回合仍可处理。stop 保留记录和代码；托管 CLI 随守护程序退出，可恢复已保存会话，但不会自动重做未完成的领取。

不推送、不创建远程仓库、不改认证文件或 Codex 内部数据库、不删除 worktree。已有任务授权内继续工作；只对超出授权、不可逆或信息不足的动作请求用户决定。不要覆盖用户修改。
