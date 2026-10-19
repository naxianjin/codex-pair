# codex-pair

让用户当前打开的两个官方 Codex CLI 窗口协作：A 发起，用户把邀请交给 B，双方通过本地消息桥问答、分工、交付和整合。各自保留原会话、配置目录、模型与权限。

## 当前支持范围

- 仅两个已有原窗口，固定 A/B；不支持三、四窗口扩容，不创建额外模型会话或用后台托管会话冒充原窗口。
- Windows、Python 3.10+、Git、支持 queue/app-server 的官方 Codex CLI。历史验证使用过 0.162.1、0.163.0，不保证任意版本兼容。
- 提供方仅限 OpenAI 官方账号和原 CLI 已配置的 DeepSeek API；不配置 API、不复制登录信息或 Key。
- 同工作树业务串行；已有独立 worktree 可在范围不冲突时并行。安装到多个配置目录不等于多个成员同时协作。
- 这是个人维护项目，并非 OpenAI 官方项目。

当前维护版为 2026-10-19 两窗口版，以本窗口保留审批和握手修复的源码为基准。此前三、四窗口实验实现从当前运行包撤回，历史提交与模拟测试记录保留；不要用本版打开 schema 9 多人数据库或做降级迁移。

## 安装当前版本

本轮只更新源码，不创建 Release 或标签。旧 v1.2.0 ZIP 不含本轮修复，不能用 releases/latest 下载结果声称安装了本轮版本。

```powershell
git clone https://github.com/naxianjin/codex-pair.git
Set-Location codex-pair
$pairAccountRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
python scripts/install_skill.py --source . --home $pairAccountRoot
```

已有仓库先检查工作树再获取更新，不覆盖用户修改。python 应为实际 Python，而非 WindowsApps 占位程序。安装器仅安装 skills/codex-pair，先备份旧版，再逐文件校验 SHA-256；不改认证、模型配置、会话或权限。可重复 --home 安装多个已确认真实配置目录，同一 junction 目标去重。

安装不等于当前会话已加载。已有窗口显式读取最新 SKILL.md；先安全停止旧桥，再由新版恢复原协作。审批代码摘要变化后，须安装匹配本轮的 Hook，并由 B 在 /hooks 中审阅信任，不写内部信任状态，不自动重放旧请求。

## 使用两个原窗口

1. 在 A 调用 `$codex-pair` 并说明目标仓库、任务。
2. A 执行 `python <技能目录>/scripts/pair.py start --project <仓库> --goal <目标> --json`。
3. 将返回的完整 CODEX_PAIR_JOIN_V1 邀请粘贴到已经打开的 B；B 从自身环境 accept。
4. 关联问答、业务回执及模型回合完成后，communication=verified，开始任务。Queued/accepted 不等于模型已执行。
5. 完成后暂停或停止桥并保留记录，不删除状态、worktree 或用户文件。

不按窗口标题、目录名称或最近会话猜成员；不要在普通终端手填别人的 UUID 冒充原窗口。原会话 cwd 与任务 workspace 分开，领取/完成从 actor_cwd 执行。

## 审批、恢复与审核边界

正式 PermissionRequest Hook 将 B 的受支持请求交给 A 单次审核；B 保留自己的限制。首次信任在 B 的 /hooks 完成，通信成功不代表审批就绪。用户授权变化须登记 delegation-policy，不能复用旧允许。

默认等待180秒。首次超时后核实未执行与原回合结束，再由 prepare-approval-retry 安排唯一第二次；连续两次后 needs_user，停止自动重试。granted/unknown 先核实结果，不重放。固定协调命令重复调用仍需新请求逐次审核。

DeepSeek 实质成果必须由经提供方/模型元数据确认的 OpenAI 官方 GPT 独立审核，登记审核任务及依赖，绑定交付版本；本版无多人实验分支的自动 review-work 门槛。无合格审核窗口则保留产物并报告，不能自行验收。

任务内 A 代执行须显式开启。低额度检查点初始保留线9%，未知额度不猜、共享池不算独立资源；真实额度耗尽、双低接管及恢复清理仍未完整验收，不因模拟测试通过宣传已完成。

## 测试与已知限制

[完整测试汇总](references/validation-summary-20261019.md) 汇总本窗口及此前其他 CLI 的实测、回归、失败与未验证项。历史全量统计不能相加成为一个测试总数。

```powershell
python -X utf8 -B -m unittest discover -q
```

本轮两窗口源码回归229项，226通过、3项需显式开启的真实环境测试跳过。最新发布副本与安装校验另见汇总，不把安装哈希一致当作每个账号都运行了真实双窗口矩阵。

已验证：原窗口基础协作、两阶段自动接续、长消息分页与受限写入协助、握手恢复、已有 HEAD 的 integrate 幂等、审批允许与拒绝、两次真实180秒超时停止，以及待审批 pending 时撤销授权。撤销项未覆盖 allowed 已返回但未消费竞争。

仍有限制：安装目录曾反复被旧版覆盖，来源未查明；共享工作树空闲判定可能令同回合 prepare 返回 B turn unresolved，原回合结束后再按协议处理，不绕过。DeepSeek×DeepSeek、反向组合、其他 CLI 版本及完整跨分支 Git 矩阵仍未完成实机验收。

## 文档

- [技能入口](SKILL.md)
- [首次接入](references/onboarding.md)
- [协作协议](references/protocol.md)
- [原生审批](references/approval-review.md)
- [协助执行与额度交接](references/assistance.md)
- [DeepSeek 边界](references/deepseek.md)
- [安装维修](references/repair-multi-account.md)
- [历史验收索引](references/verification.md)

反馈请提供脱敏版本、复现步骤及英文原错。不要上传认证文件、完整配置、会话日志、令牌或协作数据库。
