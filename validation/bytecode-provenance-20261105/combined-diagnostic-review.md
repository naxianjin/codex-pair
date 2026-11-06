结论：**限定诊断发布仍有阻塞；未发现完整认证的错误宣称。**

1. **来源清单绑定错入口。** [控制器第 7 行](validation/bytecode-provenance-20261105/Invoke-ValidatorCombinedDiagnostic.ps1:7)及 [inventory 第 8 行](validation/bytecode-provenance-20261105/combined-source-inventory.json:8)仍登记 `run-root-etw.ps1`，实际入口 `run-combined-etw.ps1` 未纳入校验。五项现有哈希均匹配，但不能据此声称 combined 入口已完成来源绑定。此项阻塞来源绑定诊断发布。

2. **combined 证据断言未进入终态门禁。** [第 93 行](validation/bytecode-provenance-20261105/Invoke-ValidatorCombinedDiagnostic.ps1:93)仅调用原 `Test-RootTrace`，检查零丢失和根进程身份；不检查受限树筛选、程序集配对或缺失 unload 负例。原始证据有 **152** 个映像，摘要保留 **106** 个，当前四文件未提供可复核的筛选及摘要生成检查。此项阻塞将 terminal receipt 表述为“combined 诊断验证通过”。

原运行器限制：与当前 root 控制器比较，未见新增代码放宽受限令牌、Job 限额、64 MiB 磁盘配额、180 秒运行时限、210 秒宿主等待及 pending 防重入逻辑。入口在控制器锁外启动、关闭 VM 的行为沿用原包装器，仍不能视为受锁保护的 VM 生命周期。

认证措辞：`formal=false` 和摘要限制说明清楚。只读复算确认 92 条托管事件按 PID、CLR、AppDomain、AssemblyID 和名称配对余额为零，支持 **46 对生命周期观察**；106 项仅支持**运行后磁盘摘要**。两者均不证明加载时内容身份、可信基线或完整 Host-to-Store 认证。

全程仅本地只读检查；未修改、未联系 B/VM、未启动子模型。