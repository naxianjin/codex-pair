# 本地回归记录（2026-11-07）

本轮修复了 `package-manifest.json` 与当前工作树源码不一致的问题：清单从当前允许发行目录重新生成，包含 131 个文件；没有修改业务源码或删除既有现场。

实际执行：

- validator admission、dispatch、owned Store、Host、receipt、core、多账户、reader assembly/build/observations、fixed reader runtime、CLI 兼容：151 项，0 失败，0 错误，1 项按正式真实栈条件跳过。
- 独立定向系统映像、动态读取、受限验证器和 Store 边界回归：186 项，全部通过；证据保存在 `C:/Users/XOS/Downloads/system-image-review/local-validation-r304/`。
- 隔离副本的 40 项 core/multi-account 回归：0 失败，1 项跳过。

清单不一致此前导致 `record_loaded_runtime` 在测试 setUp 阶段抛出 `发行清单摘要不一致` 或 `Loaded module missing or changed`。更新清单后，原工作树的 151 项目标回归通过。

仍未完成：可信原生启动、系统初始化与完整系统映像等价、实际验证器 workload 的动态/JIT 内容、正式 Host→Store 回执接线和正式发行安装；原官方审核额度恢复前不重发被中止的审核。整体仍为 `formal=false / needs_validation`。
