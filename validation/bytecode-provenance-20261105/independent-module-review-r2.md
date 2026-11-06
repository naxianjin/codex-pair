# 独立只读代码复审报告（第二轮）

- 复审仓库：D:/Codex_No_Projects/work/github/codex-pair/.codex-pair/worktrees/next-recovery-20261101
- 范围：准确三份候选未提交文件；保留上一轮 `needs_validation` 历史
- 结论：**needs_validation**
- 未修改候选、源码、活动数据库、Git 状态，未联系 A/B，未加入或重启 codex-pair，未推送。

## 会话身份

- `CODEX_THREAD_ID`：`01a1b0ae-5e37-7c80-812b-9953f2b0371f`
- `CODEX_HOME`：`D:\Codex\.codex-lb`
- 实际 model、model_provider、session ID：本窗口没有可读取的准确会话元数据，未核实。
- 配置变量只能作为辅助证据，不能证明本次实际模型；因此不宣称满足 OpenAI 官方 GPT 审核资格。

## SHA256 复核

使用 `Get-FileHash -Algorithm SHA256` 直接计算，结果与 `pending-review-freeze.json` 的 `files` 清单一致（大小写仅格式差异）：

- `check-restricted-raw-modules.py`：`CE642357FF38315F181D80292AEE0F7439FF110C658835181DE0A9B21A961E`
- `check-restricted-raw-negative-cases.py`：`568F11B0A00D778BFA8287D0958A1335F640983B5FFE918A358C5A0831053BA6`
- `raw-module-source-inventory.json`：`C9A7307F82F457D431FF021FAE5B4BC25BB4B14F7EFD0A4509DF0F4A05539CDE`

冻结摘要：`formal=false`，base commit `11f619706cf74effc34c81bc43b4f55e7d31eab2`，`clean_base_apply_check=true`。

## 本轮实际执行

1. 负例检查器

命令：

```powershell
& 'D:/Anaconda3/python.exe' -X utf8 validation/bytecode-provenance-20261105/check-restricted-raw-negative-cases.py
```

结果：退出码 `0`。输出摘要：`formal=false`、`positive_pairs=46`；13 类负例全部 `rejected=true`，包括 changed_il_path、changed_native_path、changed_flags、float_event_version、float_event_id、float_template_version、bool_pid。

2. 正例检查器

命令：

```powershell
& 'D:/Anaconda3/python.exe' -X utf8 validation/bytecode-provenance-20261105/check-restricted-raw-modules.py validation/bytecode-provenance-20261105/restricted-raw-module-evidence.json
```

结果：退出码 `0`。输出摘要：`formal=false`、`tree_processes=13`、`module_pairs=46`。

3. 静态修复复核

- 模板与事件 `Id`、`Version`、`PID` 使用 `type(...) is int`，避免 Python 整数/浮点字典键相等造成错误匹配。
- 同一 `(PID, ModuleID, AssemblyID, ClrInstanceID)` 的重复记录要求完整字段一致，因此 IL 路径、Native 路径或 flags 改变会被拒绝。
- 模板字段数量、Reserved1、IL 路径、AssemblyID/CLR 关联、46 对平衡与 92 个事件计数约束仍保留。
- source inventory 仅更新 checker 哈希，且与现场哈希一致。

## 证据边界

`formal` 始终为 `false`。本轮只复核离线证据与 checker，不认证动态内容、加载时字节、官方宿主身份或事件时间顺序；不把历史 46 正例、13 负例和本轮结果扩大解释为正式阶段或整体产品通过。

## 最终判定

**needs_validation**：准确三文件的离线正例和负例 checker 本轮均实际通过，静态修复符合原问题；但实际模型、model_provider 和 session ID 未从准确会话元数据核实，故不能宣称官方审核资格或正式验收通过。
