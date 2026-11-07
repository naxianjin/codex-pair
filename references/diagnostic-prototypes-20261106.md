# 固定读取器与系统覆盖诊断源码

本轮保存三份已独立审核的诊断源码：`validation/bytecode-provenance-20261105/check-fothk-consistency.py`、`tests/test_fothk_consistency.py` 和 `validation/bytecode-provenance-20261105/Invoke-FixedReaderPrototype.ps1`。它们不依赖本地尚未放行的准入、回执或程序集解析器改动，也未加入技能发行清单。

checker 解析固定构建的 PE、DVRT 和 BDD，复算 SCP 页并保留未验证项。它的命令行在当前诊断契约下返回非零状态；测试通过不代表正式映像认证通过。PowerShell 原型核对调用者提供的程序集摘要，并从同次读取的字节加载程序集。调用者摘要不能证明编译器或发行来源可信。

准确三文件包含在八文件官方只读复审中。审核会话为 `01a1b0ae-5e37-7c80-812b-9953f2b0371f`，提供方 `openai`，模型 `gpt-6.1-sol`，报告回合 `01a1b44a-2203-78b2-92c8-91e7ff0ce7c8`。报告 SHA256 为 `b0cef1759d6a99fb2844044bac45ed00bce0bcc9f8e6bf6bea94e920930e7e25`；结论仅为诊断原型源码 `passed`，`formal=false`。

独立复审实际运行八文件相关测试：普通模式和 `-O` 模式各 64 项，均退出 0、无跳过。本轮另对可独立运行的 `FothkConsistencyTests` 和 `BddEvaluationTests` 执行普通模式及 `-O` 检查，各 31 项通过。实际客体实录测试依赖本机诊断材料；此提交不携带这些材料，缺少实录时该项会跳过。

这三文件是源码诊断工具，不构成正式安装、受限执行、系统完整映像等价、JIT 认证或 Host→Store 放行。其余五份新原型及原有 28 文件待验证候选未随本轮提交；原报告和失败历史保留。后续保存状态复算有独立候选与审核，不能沿用本次结论。
