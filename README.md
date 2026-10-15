# codex-pair

让当前打开的两个 Codex CLI 窗口协作：窗口 A 发起，窗口 B 接受一次邀请，随后通过本地消息桥完成问答、分工、交付和 Git 整合。

**仅适用于通过 OpenAI 官方账号登录的官方 Codex CLI，不适用于其他 AI 的 API、第三方模型提供方或兼容 API 接入方式。**

这是个人维护的技能项目，并非 OpenAI 官方项目。“官方账号”是指参与协作的登录方式，不表示本项目由 OpenAI 发布或背书。

## 使用方式

1. 打开两个已有的官方 Codex CLI 窗口，同账号或不同官方账号均可。
2. 在窗口 A 调用 `$codex-pair`，说明目标项目和任务。
3. A 自动输出 `CODEX_PAIR_JOIN_V1` 指令，将它首次粘贴到你选中的窗口 B。
4. B 在原窗口加入；脚本自动读取双方各自的会话和环境，并后台启动本地消息桥，无需手动配置账号目录或再开第三个终端。
5. 双方完成连接问答验证后，A 按原目标分配任务，双方依照允许范围开发、测试和交付。

首次邀请用于确认用户选中的第二个窗口；本项目不会根据窗口标题或最近会话猜选 B，也不会向尚未登记的任意窗口直接发送初次邀请。

详细说明见 [SKILL.md](SKILL.md)、[首次接入流程](references/onboarding.md) 和 [协作协议](references/protocol.md)。

## 环境与安装

- Windows；Python 3.10+；Git。
- 已使用官方账号登录的官方 Codex CLI，需要可用的 `queue` 和 `app-server` 接口。本机曾使用 CLI 0.162.1 验证，其他版本的接口兼容性需自行验证。

克隆仓库：

```powershell
git clone https://github.com/naxianjin/codex-pair.git
Set-Location codex-pair
```

使用实际可运行的 Python，将技能安装到当前 CLI 的有效账号目录：

```powershell
python scripts/install_skill.py --source . --home $env:CODEX_HOME
```

如果未设置 `CODEX_HOME`，则使用官方 CLI 默认账号目录：

```powershell
python scripts/install_skill.py --source . --home (Join-Path $env:USERPROFILE '.codex')
```

按各窗口自身实际环境安装，不要统一账号目录。安装器先备份已有 `skills/codex-pair`，再复制技能包并逐文件校验 SHA-256；不复制或修改认证文件。WindowsApps 的 `python` 若只是占位程序，请改用实际 Python 可执行路径。

现有窗口也可以直接读取克隆目录的 `SKILL.md`，无需为加载新版技能重开窗口。B 未安装技能时，可以读取邀请指向的同一份技能。

## 工作目录与记录

- 保留两个现有 CLI 会话，不自动新建模型会话，也不强制创建 worktree。
- 会话根目录与任务工作目录分开；同一工作树的代码任务和模型回合串行，已有独立 worktree 可以并行。
- 共享 SQLite、邀请、日志与归档集中在目标项目 `.codex-pair/`，与双方账号目录分离。
- `.codex-pair/`、数据库、认证与缓存均排除提交。暂停、停止保留代码与历史，不删除工作树或复制凭据。

## 验证与限制

```powershell
python -m unittest discover -v
```

当前离线测试共 88 项：85 项通过，3 项真实测试默认跳过。覆盖账号路由、原子领取、重复邀请、同账号两个会话、项目外 cwd、共享工作树串行、独立工作树并行、消息桥恢复、启动锁竞争、旧库升级和安装哈希。

此前使用两个独立官方账号验证过后台托管会话的问答、交付、去重、重启和冲突整合；当前两个可见 TUI 的新入口流程尚未完成完整实机问答验收。请以实际 `status.window_flow.communication=verified` 为自动通信通过的证据。

queue 接受回执不等于模型已回复。若 CLI 接口无法唤醒原窗口，应明确使用手动交接；文件信箱不能被当成自动通信完成。脚本不会模拟键盘，也不会自动批准服务端请求。
