# 固定协调命令重复送审修复（2026-10-19）

安装摘要恢复一致后，真实 B 窗口再次执行 check-checkpoint 仍返回 `Approval relay unavailable`。此前的安装维修只解决版本不一致，不能作为通道已恢复的结论。

代码中已获批的相同操作统一按 granted 拦截，但固定协调命令又明确不要求 report-approval 汇报自身，导致正常恢复检查无法再次送审。新增回归用例先复现 `Operation requires reconciliation`，修复后通过。

最小修复：仅当旧记录为 granted 且通过现有完整协调命令解析、成员、解释器、脚本、账号、会话、cwd、pair 和 state-root 校验时，允许创建全新 pending 审核请求。不会复用旧批准或自动允许；业务命令以及 denied、unknown、pending、allowed、expired 的限制保持不变。

本地两窗口维护树与发布树分别运行 `python -m unittest tests.test_delegation tests.test_approvals`，各 37 项通过；安装器备份并更新两个测试 home，逐文件校验通过。未运行本轮全量测试。Hook 已按新代码重新安装，需要 B 用户重新审阅信任，尚未完成真实回调复测和第二次业务超时测试。

未修改 B 沙箱、模型、登录或内部信任数据，不发布状态库、账号文件或完整会话日志。旧任务和审批历史保留；当前不以时间流逝、安装成功或单元测试代替真实验收。
