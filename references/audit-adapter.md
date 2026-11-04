# 审查适配与明确发行清单

本参考适用于 `codex-pair-audit/1`。代码和测试已编写，尚未在规定的Windows隔离环境运行。文件摘要一致、JSON可解析和报告预检都不代表审核通过。来源及适配差异见 [upstream-notices.md](upstream-notices.md)。

## 固定依赖与文件选取

`scripts/pair_audit_adapter.py` 提供以下接口，均不启动进程、不创建模型会话、不写产品文件：

- `load_assets(root=None)`：检查发行清单及上游锁定摘要。
- `dependency_status(ocr_executable=None, platform_name='windows-amd64', root=None)`：读取显式指定的OCR文件并核对固定SHA256。缺失或不匹配返回准确原因；不查找同名替代程序、不下载、不自动更新、不配置LLM。`bytes_verified`仅证明文件摘要，`capability_verified`仍为false。
- `ocr_command_plan(executable, repository, candidate_files=..., from_ref=None, to_ref=None, commit=None, ...)`：生成 `delegate preview --format json` 与 `delegate rule --format json` 的参数数组，交经过验收的执行器执行。禁止把参数拼成shell文本；两种引用模式不能混用。
- `adapt_ocr_preview(raw, candidate_files=...)`：用准确候选的 `(path,status)` 对齐所有文件，包括未跟踪文件、同路径删除和重新创建。排除项保留理由，未覆盖文件拒绝适配。
- `adapt_ocr_rules(raw, files=...)`：检查分组与文件覆盖。返回的规则文本始终标为数据，不提升为运行命令或权限依据。

OCR数据schema位于 `assets/audit/ocr-preview-schema.json` 与 `ocr-rules-schema.json`。原始delegate流程与系统规则见 `assets/upstream/ocr/WORKFLOW.md`、`system_rules.json`。禁用其可选产品修复步骤；修复交原工作窗口。

## 分层审核

固定代码阶段先做OCR代码审查；身份、权限、执行、路径、迁移、消息和恢复变化加安全轨道。含代码、可执行配置或安全边界变更的最终整合候选，按预登记的standard范围运行完整安全审计。纯文档按其登记验收契约处理。

Cloudflare材料按任务加载：先 `RECONNAISSANCE.md` 建立架构与信任边界，再用 `ATTACK-CLASSES.md` 和相关领域材料形成覆盖计划，按 `HUNTING.md` 查找，以 `VALIDATION-AND-REPORTING.md` 完成候选验证和独立记录核验。文件均位于 `assets/upstream/cloudflare`。父审计固定范围、轨道、覆盖单元与预算后，子任务只处理自己登记的部分；不能因发现缺陷缩减必要轨道。

## 报告接口

```text
adapt_report(raw, *, context, expected_units, root=None)
```

`context` 必须来自可信任务记录，且与 `raw.context` 精确相等，包含：

```json
{
  "pair_id": "当前协作", "audit_id": "父审计", "stage_id": "父阶段",
  "auxiliary_task_id": "辅助任务", "session": "准确会话", "execution_id": "准确执行",
  "epoch": 1, "candidate_digest": "准确候选摘要", "contract_digest": "准确契约摘要",
  "requirements_version": 1, "track": "ocr"
}
```

`track` 为 `ocr` 或 `cloudflare`。`expected_units` 是预登记的 `[{"id":"单元ID","required":true}]`，不能取自报告来反向决定覆盖。报告包含 `verdict`、`coverage`、`findings`、`checks`，以及适用的 `validation_records`、`unverified_candidates`：

- `coverage` 使用 `unit_id`、`status`、`evidence_refs`；跳过、范围外和待验证项还要 `reason`。跳过不算必要单元已覆盖，不能省略条目。
- OCR问题使用 `fingerprint`、`verdict`、`path`、`line`、`title`、`description`、`requirement`、`trigger`、`impact`、`confidence`、`evidence_refs`。
- Cloudflare问题保留适配schema的完整confirmed/needs_validation/rejected结构；`coverage_ledger` 另保留上游完整覆盖账本。原始发现尚未进入必要验证时，放在 `unverified_candidates`，使用 `state=unverified_candidate`，不赋确定严重度。
- `validation_records` 关联 `fingerprint`、`validator_session`、`discoverer_session`、`evidence_refs`。这些是待核验声明，控制器必须与贡献链、实际官方回执和独立上下文对账。
- `checks` 使用 `name`、`status`、`evidence_refs`。失败或证据缺口保留；普通命令退出码、零发现和rejected记录均不直接决定父阶段通过。

返回值同时包含 `raw_report`、`adapted_report`、两者摘要以及 `validation`。摘要格式 `json-utf8-sorted-compact-v1` 明确表示规范JSON载荷摘要，并非输入文件原始字节摘要。可信控制器须先按句柄式安全提取保存原始文件及其文件摘要，再保存适配结果；本模块不越过该提取边界自行读写报告路径。

confirmed问题的可选修复方向为 `remediation.strategy`，最多一条短句。输入含补丁字段或多步骤建议时，只剔除建议并记入 `stripped_fields`，不删除问题证据、不重跑测试、不启动修复讨论。400字符限制用于保守拒绝长篇可选建议，不能代替审核者对“修复方向”的语义核对。

`validation.errors` 是结构或绑定错误，`needs_validation` 是实际回执、独立性或覆盖缺口。`coverage_complete`仅为适配层的清单检查。`eligible_for_settlement`始终为false：适配器不查询官方工具回执，不允许报告自证通过。

Cloudflare返回 `required_validations`：准确校验器路径/摘要、输入字段及载荷摘要，findings另绑定schema摘要。可信执行器还须运行完整的 `assets/audit/validate-findings.cjs`、`validate-coverage-ledger.cjs` 并保存实际回执，Node运行时的版本、文件身份和路径登记在准确契约中。缺Node或隔离能力时保持待验证，不临时下载安装。父结算还需核对全部轨道、贡献独立性、候选/契约/控制代次、预算、未解决阻塞与官方证据；不能把子报告或传入的布尔值作为结算许可。

## Windows执行边界

宿主可信控制器和官方GPT客户端不进入客体；被审代码、OCR和目标测试经已验收的Hyper-V执行器运行。目标、工具链只读，写入限定scratch，禁外网并限制资源与总时长。全部子进程终态与证据提取须核对。当前未提供已验收的Hyper-V执行器；这项缺口不能以临时目录、worktree或宿主沙箱名称替代。

## 发行清单

安装器和打包器只接受根目录 `package-manifest.json`，不再按文件后缀扫描。格式：

```json
{"schema_version":1,"files":{"SKILL.md":"64位小写SHA256","assets/audit/report-schema.json":"64位小写SHA256"}}
```

路径必须为规范POSIX相对路径。清单列出全部运行脚本、规则、schema、校验器、许可证、上游锁、必需参考文档、`README.md` 和 `POST_RELEASE_TESTS.md`；不含测试、认证、配置、真实数据库、会话日志及本机证据。清单自身不写入 `files`，避免自摘要；安装/ZIP会追加其实际摘要并携带清单。

生成过程由维护者在完整候选冻结后完成：从经审阅的明确路径列表逐个读取字节，计算SHA256，按路径排序，以UTF-8无BOM写入该结构；不通过递归收集任意文件扩展发布范围。新增资源须显式加入路径列表。任何运行文件改动后重新生成并独立核对清单，不能修改原清单摘要来冒充旧审核仍适用。

清单缺失、私有路径、大小写别名、路径跳转、硬链接、重复字段、摘要变化均拒绝。原安装锁、逐目录预检、暂存复核、旧版备份、失败恢复与ZIP no-clobber发布继续保留。当前7份v1.3.0安装不因候选开发而重装。新包须在脱离源码的隔离环境验证资产、文档引用和帮助入口，再按授权安排安装；磁盘安装与活动运行副本切换分别留证。


## Windows 校验输入

适配后的两个 CJS 校验器在 Windows 调用 `read-windows-input.cjs`、`read-windows-input.ps1` 和 `WindowsSafeInput.cs`。系统 PowerShell 只对该子进程使用 RemoteSigned，不修改全局策略；它编译并调用读取辅助器，返回原始字节，再由 Node 严格解码 UTF-8。读取有 15 秒和 5 MiB 上限。

读取器从本地盘根句柄逐层打开目录，保留祖先句柄且不共享删除，拒绝重解析点；最终文件仅共享读，拒绝软链接、硬链接、非普通文件及超限内容，并核对同一文件句柄读取前后的身份和变化。支持中文、非 BMP 字符及超过 260 字符的输入路径。缺少系统 PowerShell、编译能力或必要访问权限时拒绝读取，不退回普通路径读取。

三个辅助资产随发行清单安装，`required_validations.reader_assets` 保留其摘要。正式执行契约还需要绑定 Node、系统 PowerShell 和 .NET/编译环境。读取器和离线测试通过不代表正式工具回执接线已完成；当前生产链路缺口见 GitHub 上的[宿主校验接线记录](https://github.com/naxianjin/codex-pair/blob/main/references/host-validation-gap.md)。
