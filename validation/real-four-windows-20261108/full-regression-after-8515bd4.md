本轮修复：让 `CoreTests.test_real_entry_reuses_bridge_and_preserves_review_wait_with_history` 在 unittest discover 将文件作为 `test_core` 加载时，补丁作用于当前模块，而不是硬编码不存在的 `tests.test_core` 别名。这样嵌套真实 daemon 测试不再因未命中 AppServer 补丁而尝试寻找临时不存在的 Codex 可执行文件。

验证：
- `python -X utf8 -m unittest tests.test_core.CoreTests.test_real_entry_reuses_bridge_and_preserves_review_wait_with_history -v`：1 passed。
- `python -X utf8 -m unittest discover -s tests -p test_*.py -q`：1427 tests，OK，skipped=3。
- `git diff --check`：通过。

限制：本轮只修测试模块加载兼容性；真实可见 CLI、VM/VMRS、系统映像、JIT、正式 Host→Store 和正式发行验收仍保持原有 needs_validation/formal=false 状态。
