# codex-pair

让你当前打开的两个 Codex CLI 窗口一起完成项目。

在窗口 A 说明任务，把它生成的邀请粘贴到窗口 B。B 加入并通过连接验证后，双方通过本地消息桥分工、提问、回复和交付；A 负责统筹与整合。

协作对象由你选择，不固定是哪两个账号或配置目录。同一账号的两个会话、不同账号的两个会话都可以参与。普通入口沿用原窗口，不另开模型会话，也不要求你手填会话 UUID 或账号目录。

这是个人维护的项目，并非 OpenAI 官方项目。

## 适用范围

- 运行环境：Windows、Python 3.10+、Git，以及可用的官方 Codex CLI。
- CLI 接口：自动通信需要 `queue` 和 `app-server`。已有本机验证记录使用 Codex CLI 0.162.1；其他版本需要检查接口兼容性。
- 接入范围：OpenAI 官方账号，以及在官方 Codex CLI 中已经配置好的 DeepSeek API。不支持其他 AI 客户端或模型提供方。
- DeepSeek：代码适配与离线测试已完成，但 DeepSeek 双窗口及 OpenAI／DeepSeek 混合配对的完整自动协作流程仍待实测。使用前请读 [DeepSeek 接入与验收说明](references/deepseek.md)。

各窗口保留自己的 `CODEX_HOME`、登录信息、模型和权限设置。技能不配置 API，不收集或复制 Key，也不会借用另一个窗口的账号替它推理。

## 环境与安装

### 下载发布包

当前版本为 [v1.1.1](https://github.com/naxianjin/codex-pair/releases/tag/v1.1.1)。沿用 v1.1.0 的独立规划任务接续与额度耗尽保护，更新双窗口实测结论和安装说明，不改变运行逻辑。验证范围见 [验收记录](references/verification.md)。

[下载 codex-pair.zip](https://github.com/naxianjin/codex-pair/releases/latest/download/codex-pair.zip) · [SHA-256 校验文件](https://github.com/naxianjin/codex-pair/releases/latest/download/SHA256SUMS.txt) · [全部版本](https://github.com/naxianjin/codex-pair/releases)

在当前窗口使用的 PowerShell 环境中执行以下命令。`python` 应指向实际安装的 Python，不要使用 WindowsApps 占位程序。

```powershell
$pairAccountRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$pairDownload = Join-Path ([IO.Path]::GetTempPath()) ('codex-pair-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $pairDownload | Out-Null

Invoke-WebRequest 'https://github.com/naxianjin/codex-pair/releases/latest/download/codex-pair.zip' -OutFile (Join-Path $pairDownload 'codex-pair.zip')
Invoke-WebRequest 'https://github.com/naxianjin/codex-pair/releases/latest/download/SHA256SUMS.txt' -OutFile (Join-Path $pairDownload 'SHA256SUMS.txt')
$pairExpected = ((Get-Content (Join-Path $pairDownload 'SHA256SUMS.txt')) -split '\s+')[0]
$pairActual = (Get-FileHash (Join-Path $pairDownload 'codex-pair.zip') -Algorithm SHA256).Hash
if ($pairActual -ne $pairExpected) { throw 'SHA-256 mismatch / 下载包校验失败，停止安装' }

Expand-Archive -LiteralPath (Join-Path $pairDownload 'codex-pair.zip') -DestinationPath $pairDownload
python (Join-Path $pairDownload 'codex-pair/scripts/install_skill.py') --source (Join-Path $pairDownload 'codex-pair') --home $pairAccountRoot
```

安装位置是 `<有效 CODEX_HOME>/skills/codex-pair/`。两个窗口使用不同配置目录时，分别在各自环境安装；共用配置目录时只需安装一次。安装器先备份旧技能，再逐文件核对 SHA-256，不修改认证、模型配置或会话记录。

发布包解压后只有一层 `codex-pair/`，包含技能说明、脚本和参考文档，不含测试、Git 历史或本机协作记录。手动安装时，将这个文件夹放入对应环境的 `skills/`，替换前自行备份旧版。

### 安装当前源码

需要查看实现、运行测试或使用尚未发布的改动时，可以从源码安装：

```powershell
git clone https://github.com/naxianjin/codex-pair.git
Set-Location codex-pair
$pairAccountRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
python scripts/install_skill.py --source . --home $pairAccountRoot
```

已有窗口要使用更新后的说明，可以让它显式读取安装目录里的最新 `SKILL.md`。磁盘安装完成不等于当前会话已经加载新版说明；运行中的旧消息桥也需要停止后从新版脚本重新启动。确认新版安装、哈希和发布包一致后，可将不再需要的旧技能备份、旧 ZIP 及独立解压副本移入系统回收站；不要连带清理账号配置、协作数据库、日志或已登记的 Git worktree。

## 使用方式

当前源码支持每轮重新确定 A/B、显式恢复旧协作、交付分页读取确认与额度感知均衡。普通 start 创建新一轮，start --resume-pair <ID> 才保留旧角色；未决旧工作阻止自动换绑。assign 可用 --to auto --effort 5 --report-to A，已明确的后续工作用 --depends-on 登记。quota --refresh 查询双方自身账号额度；未知或不可比较时按工作量分配，不把百分比当作绝对容量。

完成通知按 report_to 路由，支持交给自身并等待当前回合结束。handoff --task <ID> --to A/B 幂等补发已有交付；不重做任务、不修改负责人。结果与前置交付先 read-delivery 分页读取，再 ack-delivery 确认。中途交回使用 complete --partial，不能满足完成依赖。后台10秒检查任务、60秒刷新额度；模型无需保持回合轮询。接口、数据可靠性及恢复边界见 [协作协议](references/protocol.md)。

结果回合只负责读取、确认和收尾。新轮次在阶段交付和整合完成、所有结果收尾后，会自动给 A 登记独立的规划任务；桥等原回合结束后投递。A 在规划任务中核对原目标、登记剩余任务及依赖，再由桥自动调度两个窗口，无需用户手动输入“继续”。原目标已全部实现时，A 用 `complete --goal-complete` 记录验收结论，停止接续。该机制只推进已授权的原目标；关键输入或新增授权缺失时仍需用户提供。

### 1. 在窗口 A 给出任务

打开两个已能正常使用的官方 Codex CLI 窗口。在 A 中输入，例如：

```text
使用 $codex-pair，与我另一个已打开的 Codex CLI 窗口协作。
项目：D:\work\my-project
目标：修复登录页问题，补充测试，由 A 检查并整合结果。
```

将示例项目路径和目标换成自己的。A 会核验当前窗口、建立项目内共享状态，并输出一整段以 `CODEX_PAIR_JOIN_V1` 标识的加入指令。

### 2. 将完整邀请粘贴到窗口 B

B 在自己的原窗口执行邀请中的命令，不另建协作，不切换账号目录。首次邀请用于确认你选中的目标窗口，不能仅靠窗口标题或最近会话猜测 B 是谁。

B 如果尚未安装技能，也可以读取邀请所指向的同一份技能文件。消息沿用本轮数据库绑定，不需要每条消息都重新邀请；普通 start 建立新轮次，只有 start --resume-pair <ID> 明确恢复才沿用旧角色。

### 3. 等待连接验证，再开始分工

本地消息桥由脚本在后台启动，普通使用无需另开第三个终端。双方完成关联问答、业务领取与完成回执，并确认模型回合结束后，`status.window_flow.communication` 才会变为 `verified`，随后通知 A 开始组织任务。

`awaiting_peer` 或 `awaiting_handshake` 仍表示等待。队列返回接受回执只代表消息入队，不代表 B 已执行或回复。

连接成立后，A 可以分配任务，双方都能提问和反馈。交付按允许修改的路径和完整提交 SHA 验收，再由 A 整合。详细操作见 [首次接入流程](references/onboarding.md) 和 [协作协议](references/protocol.md)。

## 工作目录与记录

普通入口不强制创建 worktree。两个窗口共用工作树时，代码任务和模型回合串行执行，避免同时操作文件与 Git 暂存区；已有独立 worktree 时，允许范围不重叠的任务并行。

原会话的工作目录与任务工作目录可以不同。任务文件和 Git 操作用 `workspace`，领取与完成用通知中的 `actor_cwd`，不修改原会话根目录。

新协作产生的共享状态、worktree、日志、邀请和交接文件集中在目标项目内：

```text
<项目>/.codex-pair/
├── state/       # SQLite 与协作状态
├── worktrees/   # 选择隔离工作树模式时使用
├── logs/        # 消息桥日志
├── handoffs/    # 邀请和手动交接文件
└── archives/    # 归档与诊断记录
```

脚本设置本地 Git 排除规则，防止这些数据库和嵌套工作树进入提交。旧状态保留原位置，不静默搬迁；暂停或停止协作也不会删除代码、历史或工作树。不要把真实凭据放进任务正文、结果或日志。

## 消息失败与恢复

消息先持久化再投递，通过原子领取防止重复执行。`claimed=false` 时不能继续处理正文；结果不明的投递不会因为等待时间长就自动重派。

| 情况 | 处理方式 |
| --- | --- |
| 审查结果或任务正文很长 | 长信封改为短通知，原文保留在数据库；领取后按通知分页读取，任务还需读完范围与基准 |
| 领取成功，但工具输出截断、令牌回执丢失 | 不重复领取；原会话确认上一回合已结束并核实进度后，按协议用 `recover-claim` 取回原令牌 |
| 旧版发送前因 Windows 命令行长度检查失败，且消息未领取 | 可显式用 `recover-delivery` 恢复原消息；保留消息 ID、去重键及尝试历史 |
| 投递为 `unknown`、已接受或已领取 | 不适用投递恢复入口，不复制正文重新分配；先核对原记录 |
| CLI 无法自动唤醒原窗口 | 报告限制，按需使用逐成员 relay；仍不可用时明确改为手动交接 |

恢复命令的身份校验、参数及停止条件见 [协作协议](references/protocol.md)。文件信箱只代表手动交接，不能当作自动通信通过。技能不模拟键盘、不自动批准权限请求，也不擅自新开托管会话替代原窗口。

## 验证与限制

最近一次完整回归记录为：148 项测试，145 项通过，3 项真实环境测试默认跳过，退出码 0。覆盖账号路由、会话识别、凭据隔离、领取去重与恢复、旧库兼容、Git 交付、安装、打包、长消息分页、角色重置、额度刷新失败边界和阶段结果收尾后的自动接续。

在源码目录运行：

```powershell
python -m unittest discover -v
```

Windows 下临时目录路径过深时，Git worktree 或 SQLite 备份可能报路径错误。可在源码仓库内指定较短的测试临时目录，再运行完整测试：

```powershell
$pairTestTemp = Join-Path (Get-Location) '.codex-pair\tmp'
New-Item -ItemType Directory -Force -Path $pairTestTemp | Out-Null
$env:TEMP = $pairTestTemp
$env:TMP = $pairTestTemp
$env:GIT_CEILING_DIRECTORIES = $pairTestTemp
python -m unittest discover -v
```

额度刷新失败单独记录 `refresh_status=unavailable`；最近一次尚未超过 120 秒且周期未重置的快照继续有效。复杂任务刷新等待超时也不会解除有效 `remaining=0` 快照的耗尽保护。过期或周期重置后仍需重新查询，不自行增加可用额度。

已有验证记录与尚未完成的验证分开列出：

- OpenAI：此前两个可见原窗口已通过邀请和双向握手，完成文档、测试的分工、提交与整合；另有独立账号后台托管会话的问答、去重、重启与冲突整合记录。这不保证每个 CLI 版本或新配对都能自动通信。
- 当前窗口接续：2026-10-16，两个不同 CODEX_HOME 的 OpenAI 原窗口通过两阶段真实测试。结果回合仅确认收尾，桥自动唤醒独立规划任务并调度下一阶段，无人工“继续”；4 个任务、9 条消息均完成，目标完成后不再生成任务，测试桥已停止。本轮未创建或整合 Git 提交，不代替此前的提交整合记录；详见 [验收记录](references/verification.md)。
- DeepSeek：只读观察进程已成功读取原会话的提供方与模型元数据；DeepSeek×DeepSeek、OpenAI×DeepSeek 及反向组合的原窗口双向通信、工具执行和完整交付仍待实测。

离线测试通过、模型能对话、队列接受、业务完成与原窗口自动协作是不同证据，不能互相替代。

## 更多文档

- [技能入口](SKILL.md)：调用条件和当前窗口流程。
- [首次接入](references/onboarding.md)：A 发起、B 加入与目录约定。
- [协作协议](references/protocol.md)：消息、任务、Git 交付、停止与恢复。
- [DeepSeek 接入与验收](references/deepseek.md)：配置边界和真实测试清单。
- [维修与安装诊断](references/repair-multi-account.md)：旧状态兼容、账号路由与安装校验。
- [验收记录](references/verification.md)：本轮自动接续的实测结果、证据层级与未验证范围。

遇到问题可在 [Issues](https://github.com/naxianjin/codex-pair/issues) 提供 CLI 版本、复现步骤和脱敏错误。不要上传认证文件、API Key、完整账号配置或未经检查的协作数据库。
