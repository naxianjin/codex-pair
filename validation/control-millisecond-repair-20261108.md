# 恢复来源毫秒关联修复记录

本轮只修复 `response_item` 原始用户记录与 API 用户消息的毫秒关联。没有恢复协作，没有执行原窗口 `resume`，没有完成 C/D 通信确认或其它历史验收。

## 问题与改动

原入口报错：`Original API user message required / 新格式用户事件尚无唯一原 API 关联`。原始记录的浮点秒数经 `int(created * 1000)` 截断后，比 API `startedAtMs` 小一毫秒，导致同一消息无法关联。

`scripts/pair_control_events.py` 改用 `round(created * 1000)` 与 API 比较。会话归属、准确回合、用户来源、原始日志时间、完整正文和唯一 API 项校验保留。新增回归覆盖实际舍入正例，并验证相邻的错误毫秒仍被拒绝；测试使用虚构回合 ID。

## 实际验证

- `D:\Anaconda3\python.exe -X utf8 -m unittest tests.test_control_user_format tests.test_control_rebaseline -v`：14 项通过，退出码 0。
- 完整 discover 使用临时诊断启动器，仅为每个测试增加 60 秒超时及线程栈输出，没有替换业务实现或跳过用例：1429 项，跳过 3 项，其余通过，退出码 0，耗时 263.540 秒。
- 跳过项为显式真实 AppServer/queue、真实 daemon 闭环、真实双 CLI 集成；本轮未启用这些实际窗口测试。日志中的临时 `real-daemon` 路径来自隔离夹具，不代表原窗口验收通过。
- `git diff --check`：通过。
- 内部子代理 `/root/audit_current_diff` 实际审查差异、独立重跑 14 项专项并核对测试前后摘要，结论为本次局部修复 `passed`、`formal=false`，没有发现阻塞缺陷；不替代原 B/C/D 窗口正式审核。
- A 安装副本只读核验：从 API 返回的原会话日志路径读取准确偏移和预期行摘要，再与同回合 API 项关联；截断值 `1794145699729`，舍入值及 API 值均为 `1794145699730`。未写 Store 或调用 resume，退出码 0。

完整回归日志：`validation/current-full-regression-20261108-r3.log`，SHA256 `fe7b08feac491aff3d8a9bdaf2d0935ef80477aeb0041cb93eb68ba3787ffe85`。日志保留本地，不把其中的临时目录当作仍然存在的证据。

此前一次完整运行在修改 CHANGELOG 后没有刷新清单，出现清单摘要不一致，不能算通过；刷新清单后的 r2 又停在 daemon 存活测试，已核实并仅停止本次测试进程，保留日志。该用例随后独立执行通过，r3 完整通过；r2 停滞的根因尚未确认，未为此修改产品或测试逻辑。

## 已发生的状态与安装操作

候选脚本成功执行过一次 `rebaseline-control-source`，持久化来源边界，返回 `history_gap_preserved=true`、`resumed=false`；这不是只读操作。

随后 `bridge --action stop` 返回成功，协作状态变为 `stopped`、revision 3。已通过安装器更新 A 的技能副本并保留备份；此安装发生在正式原窗口独立审核之前。安装后再次重基线被 `Control source has no unresolved gap` 拒绝，该结果不能算作第二次重基线成功。后续只读解析验证的是安装源码的关联行为。

A 安装源码与候选源码 SHA256 同为 `76a7dcc07210910fbd79453b485b36287b283243e69fd76db39988f021b6e481`。之后更新的产品 CHANGELOG 和清单没有再次安装到 A，因此不能声称两个完整目录一致。

最后通过严格只读 doctor 核实协作仍为 `stopped`。未重启桥，未重新派发历史任务，未修改 B/C/D 安装或身份。

用户随后允许 B/C/D 协助本次修复。已只读核对原绑定：B 已确认成员身份，C/D 尚未确认。按各自原账号目录探测原生 shared 控制连接，三个连接均在初始化失败：`failed to connect to socket`，`os error 10050`；三个原生控制目录均不存在。此结果不能推断窗口未打开，也没有因此猜测或替换会话。未向 B/C/D 发送业务，已请用户由各原窗口自行查询当前身份及控制连接。

## 交付边界

本地修复、专项测试、完整隔离回归和安装源码只读解析均有实际结果。正式原窗口独立审核、实际恢复及整体协作验收仍待完成；`formal=false`。本轮结果不覆盖系统映像、动态/JIT、Host→Store 或发行安装的历史未完项。
