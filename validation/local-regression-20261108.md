# 本地回归记录（2026-11-08）

基线工作树：`feat/next-recovery-20261101`，HEAD `a1719eb`。本轮没有修改业务源码；保留工作树中已有的未提交验证现场。

实际执行：

- 定向 admission/dispatch/owned Store/Host/receipt/core/多账户/reader/CLI 兼容：151 项，0 失败，0 错误，1 项按正式真实栈条件跳过。
- 全量正常模式：1383 项，0 失败，0 错误，3 项跳过，退出码 0。
- 全量优化模式（`python -O`）：1383 项，0 失败，0 错误，3 项跳过，退出码 0。
- 源码编译检查：`python -m compileall -q -f scripts tests`，退出码 0。
- 安装/打包/清单专项：26 项，0 失败，0 错误，退出码 0。
- DeepSeek 提供方/身份/额度隔离专项：16 项，0 失败，0 错误，退出码 0。
- 系统映像一致性、FOTHK、受限验证器、动态读取和证据 profile：65 项，0 失败，0 错误，退出码 0。

跳过项均为显式真实环境门禁：真实 daemon 业务闭环、真实双 CLI 集成、真实 Codex transport；本轮未设置 `CODEX_PAIR_REAL_CORE_TEST`、`CODEX_PAIR_REAL_INTEGRATION` 或 `CODEX_PAIR_REAL_TEST`，也未启动新模型会话、VM 或活动 Store。

正式缺口保持不变：可信原生启动、系统初始化与完整系统映像等价、实际验证器 workload 的动态/JIT 内容、正式 Host→Store 回执接线、正式动态调度与发行安装。所有本地结果仍为 `formal=false / needs_validation`，不能替代已核实身份的独立审核或真实窗口验收。
