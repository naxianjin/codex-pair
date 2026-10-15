# codex-pair

codex-pair 是给两个已打开的 Codex CLI 窗口用的协作技能。A 负责分工和整合，B 接受邀请后参与开发；双方通过本机消息桥提问、回复、测试和交付代码。

目前接入范围为官方 Codex CLI 中的 OpenAI 账号和已配置的 DeepSeek API，允许同类或混合配对。DeepSeek 的代码适配与离线测试已完成，原窗口双向通信、工具执行和完整交付仍待实测。其他模型提供方暂未接入。

这是个人维护的项目，与 OpenAI 无隶属关系。

## 使用方式

1. 打开两个可用的 Codex CLI 窗口。各自登录 OpenAI 账号或配置 DeepSeek API，安装技能。
2. 在 A 中输入 `$codex-pair`，给出项目路径和任务。
3. 把 A 返回的整段 `CODEX_PAIR_JOIN_V1` 指令粘贴到 B。
4. B 在原窗口接受邀请，消息桥自动在后台启动。连接问答通过后，A 开始分配任务。

粘贴邀请用来确认你选中的 B，只需在首次配对时做一次。会话 UUID、工作目录和有效 `CODEX_HOME` 由各窗口自行读取并核验，无需手填，也不用另开终端运行消息桥。

用法见 [SKILL.md](SKILL.md)、[首次接入流程](references/onboarding.md) 和 [协作协议](references/protocol.md)。使用 DeepSeek 前请阅读 [接入与验收说明](references/deepseek.md)：API 由原 CLI 配置，技能不代填或复制 Key，不改模型和权限设置。

## 环境与安装

- Windows、Python 3.10+、Git。
- 官方 Codex CLI，需要可用的 `queue` 和 `app-server` 接口。本机验证版本为 0.162.1，其他版本需检查接口兼容性。

### 安装当前源码

`main` 已包含 DeepSeek 适配和领取回执修复。克隆后，用当前窗口的配置目录安装：

```powershell
git clone https://github.com/naxianjin/codex-pair.git
Set-Location codex-pair
$pairAccountRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
python scripts/install_skill.py --source . --home $pairAccountRoot
```

安装位置为 `<有效 CODEX_HOME>/skills/codex-pair/`。两个窗口使用不同配置目录时，各安装一份；同一配置目录下的两个会话可以共用已安装的技能。安装器会备份旧版并逐文件核对 SHA-256，认证文件、模型配置和会话记录保持原状。

WindowsApps 里的 `python` 可能只是系统占位程序。命令无法运行时，换成已安装的 Python 可执行文件路径。

### 下载发布包

当前发布包为 [v1.0.0](https://github.com/naxianjin/codex-pair/releases/tag/v1.0.0)，早于本次改动，未包含 DeepSeek 适配和领取恢复命令。需要这两项功能请按上面的源码方式安装。

[下载 codex-pair.zip](https://github.com/naxianjin/codex-pair/releases/latest/download/codex-pair.zip) · [SHA-256 校验文件](https://github.com/naxianjin/codex-pair/releases/latest/download/SHA256SUMS.txt) · [版本发布页](https://github.com/naxianjin/codex-pair/releases/latest)

发布包解压后得到 `codex-pair/`，含 `SKILL.md`、`agents/`、`scripts/` 和 `references/`。包内不含测试、Git 历史、账号配置或本机协作记录。可在各窗口环境中下载安装：

```powershell
$pairAccountRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$pairDownload = Join-Path ([IO.Path]::GetTempPath()) ('codex-pair-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $pairDownload | Out-Null
Invoke-WebRequest 'https://github.com/naxianjin/codex-pair/releases/latest/download/codex-pair.zip' -OutFile (Join-Path $pairDownload 'codex-pair.zip')
Expand-Archive -LiteralPath (Join-Path $pairDownload 'codex-pair.zip') -DestinationPath $pairDownload
python (Join-Path $pairDownload 'codex-pair/scripts/install_skill.py') --source (Join-Path $pairDownload 'codex-pair') --home $pairAccountRoot
```

手动安装时，将整个 `codex-pair` 文件夹放入对应环境的 `skills` 目录，替换前备份旧版。已打开的窗口可以显式读取最新版 `SKILL.md`，无需重开；B 未安装时也能读取邀请所指向的技能文件。

## 工作目录与记录

技能沿用两个原会话，不另开模型会话，也不强制创建 worktree。会话根目录可以在项目外，任务使用的工作目录会单独记录。共用工作树时，代码任务和模型回合串行执行；已有独立 worktree 时允许并行。

共享 SQLite、邀请、日志和归档保存在项目的 `.codex-pair/` 下，通过本地 Git 排除规则避开提交。暂停或停止协作会保留代码和历史，不删除工作树。

自动领取使用 `claim --compact`，避免长任务的输出被工具截断。若领取成功但回执丢失，原会话可用 `recover-claim` 取回原令牌，核实进度后继续。重复领取仍返回 `claimed=false`，不会重置或重派任务；具体恢复条件见 [协作协议](references/protocol.md)。

长审查结果不再全文放入 Windows queue 命令行：短通知指向数据库原文，领取后用 `read-message` 分页读取。旧版因命令行长度预检而投递失败的未领取消息，可用 `recover-delivery --message <原ID>`（附原 `--pair`、`--state-root` 和 `--json`）恢复同一条消息；它不会重投结果不明或已领取的消息。运行中的旧桥需要停止并从新版脚本重新启动。详见协议中的“消息过长与领取失败”。

## 验证与限制

```powershell
python -m unittest discover -v
```

当前共 117 项测试：114 项通过，3 项需显式启用的真实环境测试默认跳过。覆盖会话和提供方识别、账号路由、领取去重与恢复、凭据隔离、回合状态、旧库兼容、Git 交付、安装、打包及长审查结果的短通知、分页与安全恢复。本次长消息修复未重新运行真实双窗口交接，不能用离线测试代替该验收。

本次开发中，两个 OpenAI 原窗口已通过邀请和双向握手，完成文档、测试的分工、提交与整合，当前配对的 `status.window_flow.communication=verified`。此前还用独立账号的后台托管会话验证过问答、去重、重启和冲突整合。

DeepSeek 目前已在不继承 API Key 的只读观察进程中，成功读取原会话的提供方和模型信息。DeepSeek×DeepSeek、OpenAI×DeepSeek 及反向组合的原窗口双向通信、工具执行和完整交付仍待实测，离线测试不能代替这些结果。

`queue` 返回接受回执只表示消息入队。自动通信通过还需要关联问答、业务回执和模型回合完成记录。接口无法唤醒原窗口时，可改用逐成员 relay 或手动交接；手动交接会明确标记。技能不模拟键盘，也不自动批准权限请求。
