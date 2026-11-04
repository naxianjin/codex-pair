# 辅助池候选接口

本文件描述 schema 14 候选实现。本轮只写代码并静态检查，未运行候选、隔离回归或真实多辅助会话；不能据此宣布池机制已验收。现有 schema 13 单审核者的运行证据不自动覆盖本模块。

## 角色、容量与预算

四个可见工作席位和辅助池分别计数。辅助仅分析、发现问题、验证及独立复核，产品修复仍交原工作窗口。辅助并发默认 1，上限 4；每成员任务待审容量与阶段待审容量分别默认 2，可以各自调整至 3 或 4。降额保留已有工作，只阻止新增准入。

一个审计沿原业务根任务关联后续候选与返修，不因重新命名、换配置、换会话或重启重新获得预算。默认最多 64 次实际辅助任务启动，预留 8 次给验证、最终核验和父接续：按最多 4 个在途发现可能产生的验证及接续需求给出保守初始值，A 可在登记审计时调整预留，不能借此扩大 64 次上限。预留不是已消费；确认回合实际开始后只记一次。启动结果未知同时占计算槽和剩余预算，不能按时间退款。

每个父辅助在当前执行有准确安全终态、无未决工具及资源后才能等待子任务；等待记录保留原预算和来源。子任务与父接续都重新准入。父子互相依赖时拒绝释放并循环等待，由 A 重排顺序。

## 公开状态接口

以下是 Python Store 接口，命令行 JSON 入口由 `pair.py` 对应命令提供。所有管理操作都带 `member, session, cwd` 并核验当前 A；状态查询允许有效可见成员。

- `auxiliary_policy(..., enabled, concurrency, task_capacity, stage_capacity, reason)`：明确启用及调节容量，保存前后值和依据。启用新池先停止旧审核自动派发，旧在途或 unknown 执行必须先对账。
- `allow_auxiliary_config(..., config_id, source_member, reason, enabled=True)`：仅登记本协作已核验的官方 GPT 配置。绑定包含真实成员会话、版本、home、cwd、模型；不复制认证。
- `prepare_auxiliary_quota_consent(..., config_id)`：先生成并向用户展示准确的协作、允许配置、账号池、桶及周期，返回 `忽略辅助额度 <proposal_id>` 确认文本。只有原 A 实际用户消息被可信控制观察器读取后才能生效；`恢复辅助额度保护` 撤销全部本轮辅助豁免。
- `auxiliary_quota_policy(..., config_id, allow_soft_limit, reason, source_event=...)`：授权查询必须引用上述准确用户事件，普通 reason 或 JSON 不能授权。撤销可以直接执行。配置、原 A 身份或周期变化、实际耗尽及服务限流仍阻止新增执行，64次预算不变。
- `register_audit(..., audit_id, stage_id, tracks, coverage_units, completion_reserve=8)`：绑定准确候选、需求和检查契约。轨道为 `ocr` / `cloudflare`，覆盖单元含 `id, track, required`。不能删轨道或覆盖义务来改写原审核结果。
- `queue_auxiliary(..., task_id, candidate_id, track, role, unit_ids, ...)`：角色为 `analysis / discovery / validation / final_verification / conflict_review`；登记依赖、独立性引用、父任务、资源与是否需要动态验证。
- `reserve_auxiliary(..., task_id, config_id, reviewer_id=None)`：原子检查控制状态、配置、额度、依赖、资源、计算槽及预算，创建准确执行 ID 和私有领取凭据。默认独立新会话；复用必须显式指定准确空闲辅助 ID，独立复核要求新上下文。
- `begin_auxiliary(..., execution_id, lease, executable=None)`：同步拥有受管执行，准备固定只读候选、核验真实官方身份及配置，然后开始准确回合。动态验证缺少已验收 Hyper-V 环境时返回 `started=false, status=needs_validation`；不得据此消费预算或执行候选。
- `finish_auxiliary(..., execution_id, lease, report, executable=None)`：从官方 API 与本地 rollout 核对准确回合，报告必须等于其最终消息；任意 JSON 或文字 `passed` 无效。保存原报告、适配报告与独立证据；不直接改变父阶段。
- `auxiliary_unknown(...)` / `reconcile_auxiliary(...)`：保存未知、核对原执行；不重放。缺会话或回合 ID 时保持未知并报告准确缺口。
- `cancel_auxiliary_reservation(...)`：仅尚未进入创建 RPC、没有生产者或回合的明确未开始预留可以释放；已经开始或未知必须对账。
- `wait_auxiliary(..., task_id, child_tasks, plan)`：登记安全父等待和剩余步骤，禁止用任务等待掩盖仍运行的工具。
- `retire_auxiliary(..., reviewer_id, epoch, reason)`：准确执行均已结算后退役指定辅助身份，保留贡献链、预算及报告；不删除文件或影响其他会话。
- `settle_audit(..., candidate_id)`：核对全部必要轨道、覆盖、独立最终核验、正式回执与父固定阶段结论。缺项保持 `needs_validation`；子报告无权把业务阶段改为通过。
- `auxiliary_status(...)`：列出父审计、候选、辅助任务、准确执行、预算与旧绑定数量，隐藏领取凭据和原始生产者信息。

## 静态执行与正式验证边界

`AuxiliaryController` 和旧 `ReviewerController` 互斥持有原审核进程锁，消息桥只启动其中一种。新池内部最多四个受管执行，所有创建和新回合经同一 SQLite 准入；排队不等于运行。控制器空闲只查本地事件和状态，不启动模型回合问进度。

静态模式在独立受管进程中关闭 shell、apps、其他辅助委派、computer use、code mode、hooks、goals、plugins、远程插件和技能依赖安装；禁用当前配置中的 MCP，禁用 web search，并要求只读 sandbox。配置通过进程级覆盖应用，`config/read` 逐键核对。字段缺失、被管理配置覆盖或不能核验时拒绝开始，不把提示词或只读标签作为能力证明。模型只能通过 `pair_read_source` 读取已固定且摘要仍一致的源文件页，不能执行候选或写产品。

这些配置及动态工具的协议依据为官方 [配置参考](https://learn.chatgpt.com/docs/config-file/config-reference) 和 [App Server 文档](https://learn.chatgpt.com/docs/app-server)，并对照本机 CLI 导出的 schema。文档和 schema 证明接口存在，不证明本机真实会话已经按此隔离；本候选尚未做该运行验收。

静态报告可能提供有用发现，但没有 `commandExecution` 必要检查回执时不能放行正式阶段。Cloudflare 完整 CJS 校验器需要准确执行证据；Python 适配成功不代替完整校验。当前动态执行入口仍受 Hyper-V 环境及可信执行器验收约束，不能把普通临时目录、worktree 或宿主 shell 当替代。

## 升级及剩余验证

`upgrade_auxiliary_v14(db)` 只增加本模块表并保存旧唯一审核绑定的原样快照，由主迁移统一备份、控制写入者、提交和更新 `user_version`。旧 epoch、许可、执行和报告保留；新表不会赋予旧成果新策略通过。多会话操作必须显式指定准确 ID，不能默认挑第一个。

新增测试文件为 `tests/test_auxiliary_v14.py`，涵盖容量、unknown、重复启动计数、取消未开始预留、身份变化、独立上下文、父子等待、资源、候选轨道、报告门禁和状态脱敏。本轮未执行。真实四窗口、多辅助并行、正式审批回执、Hyper-V 隔离、长期运行、退出交接与全目录安装另留验收状态。
