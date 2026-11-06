结论：**receipt 前置门槛已接入，但严格按本轮目标，仍有两项阻塞。仅属 diagnostic，不能视为 formal 认证。**

1. **未限定根事件的 Provider。**
   [Test-RootTrace.ps1:6](D:/Codex_No_Projects/work/github/codex-pair/.codex-pair/worktrees/next-recovery-20261101/validation/bytecode-provenance-20261105/Test-RootTrace.ps1:6)仅筛选 `EventID=1`，再比较 PID、创建 FILETIME。其他 Provider 的同编号、同字段事件也能计入唯一匹配，尚不能严格保证匹配的是 `Microsoft-Windows-Kernel-Process` 启动事件。应同时约束其 Provider GUID。

2. **零丢事件检查只是查找一段匹配文本。**
   [Test-RootTrace.ps1:5](D:/Codex_No_Projects/work/github/codex-pair/.codex-pair/worktrees/next-recovery-20261101/validation/bytecode-provenance-20261105/Test-RootTrace.ps1:5)只要求 Summary 任意位置存在 `Total Events Lost 0`。若同时出现非零计数，仍会通过；XML header 的 `EventsLost` 也未核对。因此当前实现是“找到零值声明”，尚非唯一、无矛盾的零丢事件校验。建议要求唯一完整计数行，并核对存在的 XML loss 字段。

已确认正确的部分：

- [控制器:81](D:/Codex_No_Projects/work/github/codex-pair/.codex-pair/worktrees/next-recovery-20261101/validation/bytecode-provenance-20261105/Invoke-ValidatorRootDiagnostic.ps1:81)在写入 terminal receipt 前调用校验；抛错后不会执行 receipt 写入或清除 pending。提前保存的 `native.json` 不代表 terminal。
- XPath 正确区分无命名空间的 `Events` 和事件命名空间下的 `Event`；`Data` 的 `Name` 属性读取符合 PowerShell XML 适配语义。
- 当前带 `Z` 的时间值经 UTC 转换后比较 FILETIME，保留 100ns 精度；匹配数必须严格为 1。
- 在 **Windows PowerShell 5.1.19041.4648** 实际运行现有测试：正例和 PID、创建时间、丢事件、缺失事件、重复事件五个反例均通过。测试尚未覆盖上述两个阻塞，以及重复字段、缺失时区等 XML 边界。
- 两个测试相关文件名仅大小写不同，复制到默认不区分大小写的 Windows 目录时存在冲突风险。

**已知来源绑定缺口仍未解决：** baseline 未被固定摘要并绑定到请求/receipt；controller 只有自身摘要，新增校验脚本也未纳入摘要绑定；baseline 的 schema、scope、VM、limits 未全部作为执行约束；`logman.exe`、`tracerpt.exe` 未纳入工具摘要集合。这些是既有来源可信性限制，不能因新增根身份门槛而宣称已解决。

全程未修改文件、启动子模型或联系 B、VM。额外内存变体探测被 PowerShell 语言模式限制阻止；上述两个阻塞属于静态代码结论，未冒称已动态复现。
