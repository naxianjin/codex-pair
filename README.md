# codex-pair

让两个已经打开的 Codex CLI 窗口一起开发项目。A 发起协作，B 接受邀请，双方通过本地消息桥讨论问题、分配任务、交付代码，由 A 负责 Git 整合。

**仅适用于通过 OpenAI 官方账号登录的官方 Codex CLI，不适用于其他 AI 的 API、第三方模型提供方或兼容 API 接入方式。**

本项目由个人维护，与 OpenAI 无隶属关系。上面的“官方账号”仅指 Codex CLI 的登录方式。

## 使用方式

1. 在两个已登录的官方 Codex CLI 窗口中操作，可以使用同一账号，也可以使用不同账号。
2. 在窗口 A 调用 `$codex-pair`，告诉它项目位置和要做的任务。
3. 把 A 输出的 `CODEX_PAIR_JOIN_V1` 指令粘贴到窗口 B。
4. B 在原窗口加入。脚本读取各窗口的会话和账号环境，在后台启动本地消息桥，不需要手动填写账号目录或打开第三个终端。
5. 连接问答验证通过后，A 分配任务，双方在约定的文件范围内开发、测试并交付。

第一次邀请需要手动粘贴，用来确定哪个窗口是 B。脚本不会根据窗口标题或最近使用的会话替你选择，也不会向未登记的窗口发送邀请。

详细说明见 [SKILL.md](SKILL.md)、[首次接入流程](references/onboarding.md) 和 [协作协议](references/protocol.md)。

## 环境与安装

- Windows；Python 3.10+；Git。
- 已通过官方账号登录的官方 Codex CLI，且 `queue` 和 `app-server` 接口可用。已有验证使用 CLI 0.162.1，其他版本仍需检查接口兼容性。

### 直接下载 Skill（推荐）

[下载 codex-pair.zip](https://github.com/naxianjin/codex-pair/releases/latest/download/codex-pair.zip) · [SHA-256 校验文件](https://github.com/naxianjin/codex-pair/releases/latest/download/SHA256SUMS.txt) · [版本发布页](https://github.com/naxianjin/codex-pair/releases/latest)

解压后会得到 `codex-pair/` 文件夹，包含安装所需的 `SKILL.md`、`agents/`、`scripts/` 和 `references/`。下载包不含源码测试、Git 历史、账号配置和协作记录。

在各窗口使用的账号环境中运行以下 PowerShell 命令，可以直接下载安装，无需克隆仓库：

```powershell
$pairAccountRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
$pairDownload = Join-Path ([IO.Path]::GetTempPath()) ('codex-pair-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $pairDownload | Out-Null
Invoke-WebRequest 'https://github.com/naxianjin/codex-pair/releases/latest/download/codex-pair.zip' -OutFile (Join-Path $pairDownload 'codex-pair.zip')
Expand-Archive -LiteralPath (Join-Path $pairDownload 'codex-pair.zip') -DestinationPath $pairDownload
python (Join-Path $pairDownload 'codex-pair/scripts/install_skill.py') --source (Join-Path $pairDownload 'codex-pair') --home $pairAccountRoot
```

安装位置为 `<有效 CODEX_HOME>/skills/codex-pair/SKILL.md`。安装器会备份已有版本，并校验安装文件的哈希。手动安装时，将整个 `codex-pair` 文件夹放入对应环境的 `skills` 目录；替换旧版前请自行备份。

### 从源码安装

克隆仓库：

```powershell
git clone https://github.com/naxianjin/codex-pair.git
Set-Location codex-pair
```

用可运行的 Python 安装到当前 CLI 的账号目录：

```powershell
python scripts/install_skill.py --source . --home $env:CODEX_HOME
```

如果未设置 `CODEX_HOME`，则使用官方 CLI 默认账号目录：

```powershell
python scripts/install_skill.py --source . --home (Join-Path $env:USERPROFILE '.codex')
```

两个窗口分别使用各自的账号目录，不要为协作而改成同一个目录。安装器对文件逐一校验 SHA-256，不复制或修改认证文件。如果 WindowsApps 下的 `python` 只是占位程序，请换成实际的 Python 可执行路径。

已有窗口可以直接读取克隆目录中的 `SKILL.md`，不必为此重开窗口。B 没有安装技能时，也可以读取邀请中指向的那份技能。

## 工作目录与记录

- 使用两个现有 CLI 会话，不自动另开模型会话，也不强制创建 worktree。
- 会话根目录和任务工作目录可以不同。共用一个工作树时，代码任务和模型回合串行执行；已有独立 worktree 时可以并行。
- 共享 SQLite、邀请、日志和归档保存在目标项目的 `.codex-pair/` 中，不放进双方的账号目录。
- `.codex-pair/`、数据库、认证文件和缓存不提交到 Git。暂停或停止会保留代码及历史，不删除工作树，也不复制凭据。

## 验证与限制

```powershell
python -m unittest discover -v
```

已有离线测试记录共 90 项：87 项通过，3 项需要真实 Codex 环境的测试默认跳过。测试涉及账号路由、任务原子领取、重复邀请、同账号双会话、项目外 cwd、工作树串行与并行、消息桥恢复、启动锁竞争、旧库升级、安装哈希，以及下载包解压安装。

两个独立官方账号的后台托管会话已验证过问答、交付、去重、重启和冲突整合。两个可见 TUI 窗口的新入口流程尚未完成完整实机问答验收。使用时应检查 `status.window_flow.communication=verified`，确认自动通信验证通过。

`queue` 返回接受回执，只能说明消息已入队，不能证明模型已经回复。CLI 接口无法唤醒原窗口时，需要手动转交消息；文件信箱不代表自动通信成功。脚本不模拟键盘，也不自动批准服务端请求。
