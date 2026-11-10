# v1.4.0-preview.1 安装与验证说明

这是已完成部分测试的预发行包，供用户统一安装并继续验证。整体结论为 `formal=false / needs_validation`，不作为完整产品验收通过或正式宿主回执的证明。

## 本版内容与来源

运行代码来自已发布提交 `991834cb093996347f47a5b22f84364965a738c8`。本轮只修改版本说明、安装说明和程序生成的文件清单；业务实现沿用该提交。此前的读取器流清理和版本探针环境修复保留，相关独立审核及失败历史见仓库的 `validation/offline-package-and-guard-20261109.md`。

安装包按 `package-manifest.json` 精确列出的文件构建，包含技能、协议、脚本、审核资源与许可。不打包账号认证、配置、会话日志、协作数据库、本机快照或临时测试目录。仍在返修的受限创建见证、guest 测试和停止会话释放候选留在原验证目录，未作为本版运行实现发布。

## 已有验证与适用范围

历史冻结候选的完整回归运行了 1479 项，进程退出 0，其中 3 项跳过。该冻结候选与本版有代码差异，这份历史记录不作为本版准确源码的新一轮全量通过数。版本探针环境修复的整合候选另有普通和优化模式各 145 项的检查记录，其中各 1 项真实 transport 跳过；独立审核另外运行了 5 项新增边界测试。

本版准确文件的检查、独立复核、统一安装及 GitHub 附件复核结果保存在仓库 `validation/release-preview-20261110.md`。检查只有实际完成后才登记为通过；源码审查、纯夹具、真实磁盘安装及原窗口协作分别记录。

## 尚未完成的验收

| 范围 | 剩余条件 |
| --- | --- |
| 真实 daemon | 在准确原始可见 CLI 身份下完成专用闭环；保留 `Original visible CLI required` 门禁。 |
| 真实双 CLI 集成 | 原窗口参加专用协作，完成真实邀请、消息和业务收尾验收。 |
| 真实 transport | 执行原先 opt-in 跳过的真实传输检查，保留准确原回合及工具证据。 |
| 可信原生启动 | 受限 token、创建见证、Job 与失败清理、CLI 宿主来源等尚需完整现场验证。 |
| 完整系统映像 | CLR、内核及运行时 owner 的剩余差异和 M3–M6 条件尚未完成。 |
| 动态内容与 JIT | 局部程序集/IL 采集不构成完整动态或 JIT 内容认证。 |
| 正式 Host→Store | 可信生产适配器、正式回执与父结算尚未完整接线和认证。 |
| 正式调度、发行和安装 | 本次预发行部署不代替正式阶段的全链路验收与实际加载验证。 |

这里的 8 是验收范围数，不是剩余测试用例数。局部检查通过不自动关闭对应范围。

## 安装

在[本版发行页](https://github.com/naxianjin/codex-pair/releases/tag/v1.4.0-preview.1)下载 `codex-pair-v1.4.0-preview.1.zip` 与 `SHA256SUMS.txt`。在 PowerShell 中执行以下命令，人工核对输出与校验文件一致，再解压：

```powershell
Get-FileHash -Algorithm SHA256 -LiteralPath .\codex-pair-v1.4.0-preview.1.zip
Get-Content -LiteralPath .\SHA256SUMS.txt
Expand-Archive -LiteralPath .\codex-pair-v1.4.0-preview.1.zip -DestinationPath .\codex-pair-preview
Set-Location .\codex-pair-preview\codex-pair
$pairAccountRoot = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $env:USERPROFILE '.codex' }
python -B -X utf8 scripts/install_skill.py --source . --home $pairAccountRoot
```

使用实际 Python 3.10+，不使用 WindowsApps 占位程序。多配置安装时重复添加 `--home <真实配置目录>`；目录必须存在。安装器只处理 `skills/codex-pair`，为旧技能生成独立备份，并核对安装前后的文件清单。配置、认证和其他技能不属于覆盖范围。

安装后从无关目录运行目标技能的 `scripts/pair.py --help`，并重新读取目标的 `SKILL.md`。磁盘安装成功不证明已有窗口、Hook 或桥已经加载新版。旧 `windows-f93e3110d73b` 保持 stopped；需要恢复或启动新业务时继续遵守原身份、原执行与明确授权的规则。

## 恢复旧技能

每个配置目录的安装回执给出 `backup`，对应旧技能位于该配置的 `codex-pair-skill-backups` 下。先确认没有工具或桥正在使用待替换目录，保存现场；把当前技能移到另一个保留目录，再将准确旧备份移回 `skills/codex-pair`。不要删除配置、认证或历史协作数据。旧备份若没有本版完整清单，应按原安装记录核对，不能伪称本版校验通过。

本版以 GitHub prerelease 发布，v1.3.1 历史附件和标签保持原样。预发行与正式能力验收继续分别记账。
