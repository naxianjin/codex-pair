# v1.4.0-preview.1 安装与发行记录

用户明确要求把已完成部分测试的技能安装到所有现有 `.codex` 配置，推送 GitHub 并更新发行。本轮以 `991834cb093996347f47a5b22f84364965a738c8` 的已发布运行代码为基准，使用独立工作树准备 `v1.4.0-preview.1`，保留原开发树的 4 项未提交改动和其他历史文件。

## 改动范围

修改 README、SKILL、CHANGELOG、POST_RELEASE_TESTS 和开发状态，新增随包的预发行说明，再由原打包器生成发行清单。业务脚本、测试、审核资源及许可与基准字节一致。未整合仍在验证或返修的停止会话释放、受限创建见证与 guest 测试候选。

说明明确区分本次部署与正式能力验收，保留 `formal=false / needs_validation`。旧协作保持 stopped。认证、账号配置、原生快照、原始会话日志和本机验证目录不随安装包发布。

## 本版实际检查

| 检查 | 实际结果 |
| --- | --- |
| `python -B -X utf8 -m unittest -v tests.test_install tests.test_package tests.test_package_paths tests.test_producer_environment` | 24 项，退出码 0，无跳过。 |
| `python -B -X utf8 -m unittest discover -s tests -v` | 1434 项，退出码 0，跳过 3 项。 |
| 技能结构校验 | `Skill is valid!`，退出码 0。 |
| 测试输入保护 | 452 项准确源文件检查前后未变。 |
| ZIP 结构与摘要 | 单根目录，133 个文件，CRC 与全部清单摘要一致。 |
| 重复构建 | 两次 ZIP 字节完全一致；未覆盖已有包。 |
| Git 差异检查 | `git diff --check` 通过。 |

3 个跳过项为 `test_real_daemon_claim_and_complete`、`test_two_real_clis_parallel_questions_duplicate_restart_and_integration` 和 `test_real_appserver_and_queue`。定向检查与完整回归存在重叠，不相加作为唯一用例数。历史 1479 项属于不同冻结候选，本次不借用其通过数。

ZIP SHA256：`f81d614010b93c8a63d26a9ed14ed7eb9b57f09da7afcf48093517e3f66909ec`。包名：`codex-pair-v1.4.0-preview.1.zip`；附带 `SHA256SUMS.txt`。

## 独立复核

准确源码与说明交原 D 独立复核，实际提供方为 `apinebula`，模型为 `gpt-6.1-sol`，归类 `third_party`。源码固定后未替换候选。首次只读 Git 调用被 `fatal: detected dubious ownership` 阻止，原收据保留；后续仅单命令显式核对准确目录，没有改全局 Git 配置。

D 的源码与清单检查通过，但其受限身份下独立运行 24 项检查时有 1 项失败，其余 23 项通过。失败为安装后 Node coverage validator 返回 `Failed to read coverage ledger: Windows handle-based input read failed or exceeded its limit`；同用例之前的 Python 帮助检查成功。D 登记阻塞 `D2-PR-001`，原报告为 `needs_validation`，摘要为 `42a4ef94dcebea2d26247b90af98936ac6cced92eeee6bff8b18c5a908138aec`。不能因 A 同版测试通过而覆盖该失败，也不能把它归入原有 8 个延期范围。

后续由原 D 对同一稳定问题执行限定诊断。原始读取器先报 `System.InvalidOperationException: Directory open refused`；仅在诊断副本的异常消息中增加祖先索引、目录名和 NTSTATUS 后，准确定位为 `Directory open refused: index=1 component=XOS NTSTATUS 0xC0000022`，即受限账号无法打开 `C:\Users\XOS` 祖先目录。诊断副本保留原条件、访问权限和共享参数，不进入运行包。

A 在项目内新建测试夹具目录并只为该目录配置受限账号权限，没有放宽用户目录或系统祖先目录的 ACL。D 将测试进程的 TMP/TEMP 指向该目录，使用未修改的原测试，原失败单项退出 0，随后完整 24 项检查也退出 0，无跳过。单项包含在 24 项中，不重复计数。原始 23 通过、1 失败的执行和全部错误输出继续保留。

| 独立复核与协调 | 实际结果 |
| --- | --- |
| 原 D 最终限定就绪结论 | `bounded_prerelease_readiness_accepted=true`；`D2-PR-001` 按已证实的测试环境前置条件关闭。 |
| 最终报告 SHA256 | `653f02eff79a44bfb5b285b8ae45d7d6790bedce5ec58024832cd16c9457fb73`。 |
| 准确会话与工具收尾 | 原 D 会话、实际模型和提供方一致；16 次工具调用均有输出，准确回合已完成。 |
| 固定证据引用 | 118 项文件引用逐项核对，无摘要差异。 |
| 源码与测试保护 | 原始 452 项和独立副本 452 项均未变；未弱化断言或过滤条件。 |
| 发行记录 `D2-PR-002` | 原 D 已读本文件的此前版本，确认记录存在；本文件后续只追加协调、安装和发布事实，不在运行包中。 |

限定就绪审核只覆盖本版预发行源码、说明与包，不认证后续真实安装或 GitHub 发布。D 的身份继续标记 `third_party`，不是 OpenAI 官方服务审核。首次诊断、报告整理的模块导入错误及工具收据均保留，未重放已完成测试。

另一次内部只读检查确认 79 项脚本及资源与基准相同、清单摘要匹配、安装命令及备份语义一致；它没有执行安装或发布，不能代替原 D 的准确审核结果。

## 本机安装

已发现并去重 6 个有效配置目录：`.codex`、`.codex-001`、`.codex-apinebula`、`.codex-deepseek`、`.codex-Hhc02`、`.codex-wokey`。一个旧路径是同一真实目录的链接，不重复安装；5 个旧链接的目标已不存在，其他两个同名前缀目录没有配置文件，均不创建空配置。

2026-11-10 实际调用已审包内安装器，一次安装上述 6 个配置，每处 133 个文件摘要与发行包完全一致。各处旧技能的完整文件清单与独立备份一致；14 个配置、认证及 Hook 文件摘要未变，未输出其内容或纳入发布。备份位于各配置的 `codex-pair-skill-backups`，准确路径保存在本地安装回执。

从无关目录对每处运行 root、start、accept、assign、claim、read-delivery、submit-stage、bridge 的 `--help`，共 48 次，全部退出 0，运行后安装清单仍一致。这些帮助命令没有启动协作、领取业务或替换活动桥。已有窗口是否实际加载新版仍未验证。

真实安装前两次本地准备失败分别为 `FileNotFoundError: [Errno 2] No such file or directory`（解包路径过长）和 `OSError: [WinError 123]`（本地驱动路径转义错误）。两次均发生在调用安装器之前，没有写入真实配置；原驱动和失败目录保留。改用较短解包路径及正确路径字面量后完成安装，发行包和产品源码未改。

## GitHub 发行

目标仓库为 `https://github.com/naxianjin/codex-pair.git`，发布分支为 `feat/next-recovery-20261101`，标签为 `v1.4.0-preview.1`。本版标记 prerelease，保留 v1.3.1 历史标签与附件，不更改 main 或默认分支。提交、远端推送和附件下载核对将在实际操作后追加登记。

## 剩余条件

真实 daemon、双 CLI、transport、可信原生启动、完整系统映像及 CLR/内核差异、动态/JIT、正式 Host→Store、正式调度发行安装共 8 个验收范围仍未关闭。当前预发行的磁盘安装及发布检查不证明原窗口已加载新版，也不提供正式回执或整体认证。
