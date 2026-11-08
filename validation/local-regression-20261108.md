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
- 真实 Codex transport 隔离测试：1 项通过，退出码 0；Unicode app-server 消息、空闲/忙碌 queue 回执均已写入隔离证据目录 `validation/real-transport-20261108/`。
- 真实 daemon 业务闭环：已执行但失败于实现的 `Original visible CLI required` 身份门禁；没有伪造 session 或重放。
- 真实双 CLI 集成：已执行预检但因未设置 `CODEX_PAIR_HOME_B` 退出；未发送业务消息。

跳过项均为显式真实环境门禁：真实 daemon 业务闭环、真实双 CLI 集成、真实 Codex transport；本轮未设置 `CODEX_PAIR_REAL_CORE_TEST`、`CODEX_PAIR_REAL_INTEGRATION` 或 `CODEX_PAIR_REAL_TEST`，也未启动新模型会话、VM 或活动 Store。

正式缺口保持不变：可信原生启动、系统初始化与完整系统映像等价、实际验证器 workload 的动态/JIT 内容、正式 Host→Store 回执接线、正式动态调度与发行安装。所有本地结果仍为 `formal=false / needs_validation`，不能替代已核实身份的独立审核或真实窗口验收。

本轮 Host→Store 接线补充（本地、非正式）：

- 新增原宿主回执预登记入口 `register-validator-host-execution`，要求 dispatch admission、固定 run/turn/item/command、producer 起止生命周期；记录不可变且幂等，冲突拒绝。
- 新增正式补充认证记录入口及结算消费绑定；缺少官方 Host、原生启动、系统映像、动态内容或 JIT 证据时保持 `needs_validation`，不能由 JSON 布尔字段提升。
- 更新 `package-manifest.json` 中受影响源码摘要。
- 实际运行：准入、回执、调度、owned Store、正式补充、结算、打包、安装和 CLI 兼容测试共 128 项，0 失败，0 错误；`compileall` 通过；`git diff --check` 通过。
- 本轮没有获得原 A 真实宿主回执、`CODEX_PAIR_HOME_B`、可信原生启动证据、完整 CLR/系统映像等价证明、JIT 机器码证明或正式发行环境，因此整体仍为 `formal=false / needs_validation`。
