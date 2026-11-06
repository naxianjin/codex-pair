结论：**C# 变更保留了原限制和清理路径，但目前只完成根身份采集，尚未完成与 ETW 的绑定。阻塞“绑定已验证”的结论，不授予 formal。**

1. **阻塞：没有执行根身份与 ETW 的匹配。**
   [Invoke-ValidatorRootDiagnostic.ps1 第 59–78 行](D:/Codex_No_Projects/work/github/codex-pair/.codex-pair/worktrees/next-recovery-20261101/validation/bytecode-provenance-20261105/Invoke-ValidatorRootDiagnostic.ps1:59)只是同时返回 Native 和 Trace；没有解析事件并核对 `RootPid + RootCreationFileTime`，也没有检查匹配唯一性、事件丢失或时间字段语义。终态检查只有 Run 和 ActiveProcesses。即使 trace 缺少根进程事件，也可生成 terminal receipt。

2. **来源基线仅部分可证。**
   [基线文件](D:/Codex_No_Projects/work/github/codex-pair/.codex-pair/worktrees/next-recovery-20261101/validation/bytecode-provenance-20261105/root-identity-baseline.json:19)中的 runner SHA256 与实测一致：
   `8D9C99B445AA36317B9B36BE86AB8CDCA754472C8AC79F96AA6A82AACCE8A6F4`。
   脚本保留四项 control 的精确集合检查、复制后校验及执行前后摘要检查。但 baseline 自身没有摘要写入请求/回执，controller 也只是本次自算摘要；无法据此证明沿用了原登记来源。其余三个 control 和工具的登记值，本次未访问实际来源，不能确认。

3. **基线声明未全部成为约束。**
   脚本没有验证 baseline 的 `schema_version`、`scope`、`vm_id`、`limits`，而是另行硬编码 VM 和限制。目前数值一致，但未来基线漂移不会因此被拒绝。`logman.exe`、`tracerpt.exe` 也未纳入工具摘要集合。

已确认保留：

- C# 相对原文件只有四处差异：新增 P/Invoke、两个结果字段、创建时间读取、结果赋值。
- 创建时间直接取自新建且尚未恢复的 `child.process` 句柄，没有按 PID 重新打开进程；没有新增句柄。
- restricted token、桌面 ACL、环境、Job 限制、先入 Job 后恢复、超时终止、终态等待和全部原 `finally` 清理代码均未改变。
- 新增调用失败时，`assigned=false`，仍走原 `TerminateProcess` 和句柄关闭路径。
- 请求、Trace、receipt 和最终输出明确使用 `Formal=false`。
- ETW 停止失败在异常清理中未检查返回码，且 ETL/XML 未删除；不能声称诊断清理完全受验证。此项不影响上述 C# 清理保留结论。

**提供方及证据范围：**本会话身份为 OpenAI Codex；没有可独立核验的底层模型精确版本或路由证明。此次仅静态读取三个审核文件和指定原 C#，执行文本差异及 runner SHA256 核对；未编译、未运行诊断，未接触 B、活动 pair 库或 VM，未修改文件，未启动子模型。
