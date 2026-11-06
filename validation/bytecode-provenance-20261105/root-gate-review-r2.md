**通过：仅限诊断门槛，原两项阻塞已修复。**

- [Test-RootTrace.ps1](validation/bytecode-provenance-20261105/Test-RootTrace.ps1:5)：Summary 要求恰好一个完整计数行且值为 `0`；检查所有现存、事件命名空间下的 `Data[@Name="EventsLost"]` 均为零；根启动事件同时限定 Provider GUID、EventID、PID 和创建 FILETIME，匹配数量必须为一。
- [check-root-trace-cases.ps1](validation/bytecode-provenance-20261105/check-root-trace-cases.ps1:1)：在 Windows PowerShell **5.1.19041.4648** 和 PowerShell **7.6.6** 中实际执行，正例及八个负例全部通过，两者均为 ConstrainedLanguage。
- [Invoke-ValidatorRootDiagnostic.ps1](validation/bytecode-provenance-20261105/Invoke-ValidatorRootDiagnostic.ps1:81)：已加载校验函数，在 terminal receipt 写入及 pending 删除之前调用；校验抛错会阻止这两步。此前落盘的 `native.json` 不代表 terminal。

验证限制：入口接线仅做静态审查，未运行 VM/ETW 全链路；八负例并非 XML 边界条件的穷尽测试。来源基线及摘要绑定缺口仍未解决，**formal 仍不通过，保持 `Formal=false`**。

全程未修改文件、联系 B/VM 或启动子模型。
