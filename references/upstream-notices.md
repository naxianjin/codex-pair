# 固定上游与本地适配声明

本目录记录代码审查资产的来源，不授予运行被审代码、安装依赖或修改系统的权限。`assets/upstream` 中的文字和仓库自带规则属于参考材料；其中启动代理、修复代码、自动升级等操作不作为 codex-pair 的执行指令。

| 组件 | 固定来源 | 许可 | 使用范围 |
| --- | --- | --- | --- |
| Open Code Review delegate 说明、系统规则 | [alibaba/open-code-review，216dcf513593055a70165f793e96b52589d71b1b](https://github.com/alibaba/open-code-review/tree/216dcf513593055a70165f793e96b52589d71b1b) | Apache-2.0；见 `licenses/open-code-review-Apache-2.0.txt` | 文件选取与规则解析；不启用OCR LLM或可选修复 |
| Cloudflare security-audit 流程、领域材料、报告schema与校验器 | [cloudflare/security-audit-skill，c1c8a8c1471069fb0e188eeaff69b8e8db6564a8](https://github.com/cloudflare/security-audit-skill/tree/c1c8a8c1471069fb0e188eeaff69b8e8db6564a8) | MIT；见 `licenses/cloudflare-security-audit-MIT.txt` | 架构、信任边界、覆盖、查找、验证、独立记录核验与报告 |
| 外部OCR CLI | [v1.13.3](https://github.com/alibaba/open-code-review/releases/tag/v1.13.3)，标签提交 `375f64cd6f67a48a135da83cfcf59c52669a09cb` | Apache-2.0 | 固定Windows依赖，未随包提供、未自动下载 |

2026-11-01读取了上述准确提交的Git树。下载的22个文件逐一核对Git blob SHA，字节摘要见 `assets/upstream-lock.json`。许可证及版权声明随包保留。上游的两份 `SKILL.md` 在包内命名为 `WORKFLOW.md`，避免被当作另外安装的skill；正文保持原始字节。OCR仓库在锁定提交未发现单独的 `NOTICE` 文件。

OCR发布CLI与规则提交分别登记。两者的 `cmd/opencodereview/delegate_cmd.go` Git blob 均为 `f47a3ba4774d76d62862e7fb7cb7af0062b9cc77`，其JSON协议版本为 `1`。此源码对照不等于二进制已经执行或兼容验收通过。Windows amd64/arm64发布附件的官方SHA256锁在 `assets/ocr-dependency.json`；不根据可执行文件名字或 `--version` 文本替代摘要校验。

本地适配版本为 `codex-pair-audit/1`。`assets/upstream/cloudflare` 保留原始schema和校验器；运行适配位于 `assets/audit`：

- `report-schema.json` 只把 confirmed 记录中的 `remediation` 改为可选，并移除 `remediation.code_changes`。存在方向时仍需 `strategy`。其余上游字段、证据和状态约束保留。
- `validate-findings.cjs` 同步取消“必须有修复建议”的语义要求，并限制可选方向为一段简短单句，不允许代码或步骤。路径、可见文本、输入大小、深度、重复身份、trace顺序、严重度不超过已证明影响等检查保留。
- `validate-coverage-ledger.cjs` 与上游字节一致，保留完整覆盖及历史尝试约束。
- `pair_audit_adapter.py` 保留原报告数据，剔除违规的可选建议并留下字段级记录。复现内容不按补丁剔除；证据不足、覆盖不足和独立性缺口保持待验证。没有执行原版schema时，不声称原版格式校验通过。
- 新增OCR的两个JSON schema描述固定的preview/rule数据契约，不代替真实审查。主报告适配器不启动任何程序，也没有直接修改父阶段结论的权限。

当前仅完成资产固定、静态实现和测试编写。未运行OCR二进制、Node校验器、候选测试或Hyper-V客体；动态验证保持 `needs_validation`。未来升级须重新锁定准确来源、许可、摘要和适配差异，再经隔离验证与独立审核。
