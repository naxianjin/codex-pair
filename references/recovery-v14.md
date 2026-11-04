# schema 14 恢复候选

本文件说明 2026-11-01 静态开发候选。代码未做动态执行、真实窗口验证或 Hyper-V 验收；不能据此启用活动协作、安装或切换运行副本。原安装版及其有效证据保持原有范围。

## 已写入候选的机制

- `RecoveryMixin` 复用 Store 的身份绑定、原 A 权限、事务、任务、消息、事件、交付分页及正式审核记录。
- `upgrade_recovery_v14(db)` 只创建恢复表及默认关闭的四域策略，不提交事务、不写 `PRAGMA user_version`。由 Store 的受控迁移入口调用并设置版本。
- 四域为 `transport`、`continuation`、`review_recovery`、`patch_application`。分别保存开关、版本、授权依据、目标摘要和 A 的准确绑定；修改一域不修改其他域。
- 暂停与明确继续先写控制记录和代次，再允许受支持入口核对；与派发共用 `dispatch.lock`。暂停初态为 `pausing`，不把请求受理说成全部工具已停。
- 继续即使遇到仍为 running 的库，也先建立核查门禁。相同代次重复请求复用原核查；新暂停使旧核查失效。可逐任务恢复，未核对任务继续受门禁约束；当前范围全部义务及在途操作均获可信核验时可完成整个范围的恢复，空任务协作也需要完整范围证明。
- `stopped`、`closed_scope` 和已完成目标不能由普通继续复活。恢复不改变任何域的策略开关，不转移 A 的权限。
- 安全交回的后续意图与 outbox 可以和原业务状态在同一事务保存。消息回合结束而业务没有交付时，只登记一次原负责人核对提示，不代写 complete 或重派业务。
- 处理计数按稳定事项及逻辑尝试保存：协调 3 轮、产品修复 2 轮、真实审批故障 2 次、桥恢复 3 次/600 秒、父审计实际启动 64 次。重复请求和 unknown 对账不增加或退还次数。计数原语不自动证明各调用方已经全部接线。
- 专用恢复提案核对当前只读诊断摘要，要求 `repair_links`、稳定问题、原阶段及容量占用构成实际准入循环。名字相似或报告自称循环不能放行。
- 恢复候选位于当前项目内，文件清单和允许范围固定；读取时拒绝越界、重复路径、链接重定向及字节变化。
- 恢复通道只有一个 claimed/running/report_submitted/unknown 记录。原会话可取回尚未开始的领取回执；旧绑定、旧代次、不同 token 均不能结算。
- 限定交付授权只针对准确恢复、交付、摘要、接收者及有效期；仍使用原完整分页和准确摘要确认。
- 应用计划固定旧新运行摘要、正式阶段、状态格式、备份和核验契约。当前实现只登记门禁及核验结果，**没有自动复制、切换进程或覆盖状态库**。

## Store 和 CLI 接入

所有公开变更入口先由现有 CLI `validate_actor` 验证当前真实会话、cwd、官方能力及绑定。下面的 `actor` 表示 `(member, session, cwd)`，不得从他人通知中借用。

| 方法 | 参数和行为 |
| --- | --- |
| `recovery_policy(*actor, domain, enabled=bool, basis=str, expected_version=int)` | 原 A 版本化登记一域；关闭策略不要求恢复业务。 |
| `request_pause(*actor, reason=str, idempotency_key=str, scope=None, source=None)` | 范围为 `{"kind":"pair"}`、`{"kind":"member","id":"B"}` 或准确 task；记录请求，不宣称全部停止。 |
| `confirm_pause(*actor, control_id)` | 仅可信入口和在途终态证明齐全后记 paused。 |
| `request_resume(*actor, instruction=str, idempotency_key=str, scope=None, source=None)` | 任一有效成员可登记明确继续的核查；来源仍须核验，登记本身不恢复业务。 |
| `reconcile_resume(*actor, control_id, tasks=[...])` | 原 A 核验准确任务、来源和工具门禁后逐任务解除本次适用门禁；其他任务和独立策略不变。 |
| `recovery_status(*actor)` | 查询恢复记录。此接口经过普通 Store，不是严格只读 doctor 的替代入口。 |
| `inspect_remaining_work(*actor, source_task)` | 原 A 读取已交回工作的当前摘要、义务、依赖及原执行状态。 |
| `continue_remaining_work(*actor, source_task, new_task, expected_digest, remaining_steps=[...], completed_effects=[...], candidate={...}, evidence=str, idempotency_key=str, target=None)` | 原 A 登记明确剩余步骤；原交付未完整读取时只返回 requires_source_read，条件不足时保存等待意图。条件满足才原子创建新任务/消息及原义务关联，不复活旧 claim。 |
| `supersede_obligations(*actor, source_task, replacement_stage, expected_source_digest, coverage=[...], idempotency_key=str, source_stage=None)` | 逐项结算正式替代。指定 source_stage 只释放该阶段；不指定则须覆盖任务及其全部未完阶段。旧状态、报告与回执不改写。 |
| `recover_safe(*actor, intent_ids=None)` | 对应 `recover --safe`；只消费 continuation 域允许的已登记剩余工作意图及已有 outbox。不会开启策略、启动模型、重放 unknown 或切换运行版。 |
| `propose_review_recovery(*actor, proposal, idempotency_key=str)` | 原 A 登记固定候选；重复相同请求返回原记录。 |
| `read_review_recovery(*actor, recovery_id, offset=0, limit=2000)` | 原 A 或准确审核者按摘要顺序分页。 |
| `authorize_review_recovery(*actor, recovery_id, expected_digest, basis=str, ttl=600)` | 原 A 复验策略、真实循环及未决执行，签发开始前许可；默认有效期 600 秒，可指定 1—86400 秒。 |
| `claim_review_recovery(*actor, recovery_id)` | 唯一领取；仅 claimed=true 可办理 begin。 |
| `recover_review_claim(*actor, recovery_id)` | 仅原会话恢复尚未开始的领取回执；不重建 token。 |
| `begin_review_recovery(*actor, recovery_id, token)` | 事务复验当前身份、策略、候选、控制、交付、循环及执行证据后取得新代次。 |
| `finish_review_recovery(*actor, recovery_id, token, epoch=int, report=dict)` | 结算准确执行；报告内的 passed 不构成证明。 |
| `reconcile_review_recovery(*actor, recovery_id)` | 原 A 对账原执行；没有准确终态保留 unknown 和通道。 |
| `revoke_review_recovery(*actor, recovery_id, reason=str)` | 仅撤销尚未开始的许可；不抹去已发生事实。 |
| `grant_recovery_delivery_access(*actor, recovery_id, delivery_id, expected_digest, ttl=600)` | 只授权许可中预先列明的准确交付。 |
| `prepare_recovery_application(*actor, recovery_id, stage_id, plan, idempotency_key=str)` | 原 A 核验正式通过与当前授权后，先登记 waiting_terminal 切换门禁；不执行切换。 |
| `begin_recovery_application(*actor, application_id)` | 保留切换门禁等待原执行结束，再核对准确候选、备份、兼容性和独占条件；仍不复制文件或启动进程。 |
| `reconcile_recovery_application(*actor, application_id)` | 只根据真实加载、schema、必要检查和原操作终态结算应用；只解除本次应用门禁。 |

`proposal` 的字段：

```json
{
  "source_task": "准确恢复候选任务",
  "stable_blocker": "issues:现有问题ID列表的稳定摘要前24位",
  "blocker_snapshot_digest": "当前diagnose-blockers快照摘要",
  "candidate_root": "项目内固定候选绝对目录",
  "manifest": [{"path": "相对路径", "sha256": "准确SHA256"}],
  "allowed_paths": ["允许的相对路径"],
  "source_manifest_digest": "准确来源摘要",
  "purpose": "本次恢复审核目的",
  "check_contract_digest": "准确检查契约摘要",
  "reviewer": "已核验且独立的可见官方GPT成员",
  "required_deliveries": [],
  "remediates": ["已经登记的review_issues ID"]
}
```

这些值都是结构示例，不能原样作为真实证据。稳定 ID 使用模块 `digest(sorted(issue_ids))` 计算；候选、窗口或任务改名不能生成新问题预算。

### 必须接入的内部边界

1. Store 继承 `RecoveryMixin`，schema 13→14 的受控迁移调用 `upgrade_recovery_v14`。旧程序须继续拒写不支持的新格式。
2. 调度、派发、任务领取后的业务 begin、受管辅助开始及批准实际发送调用 `_recovery_gate(db, member, task_id, action)` 或 `_require_recovery_open`。只读、控制、保存、记录和原执行对账为受限管理动作；不能将业务命令标成管理动作绕过。
3. `task-boundary` 写 handed_back 并释放本任务资源后，在同一事务调用 `_record_boundary_continuation(db, task_id, evidence)`。已有 complete/result/stage 事件可调用 `_record_continuation` 保存对应意图；后续业务仍通过原调度器正规登记。
4. 桥低频循环在控制事件处理后调用 `_recovery_tick(db)`。它只登记缺交付提醒并转写已有 outbox；带后续意图的通知还要求 continuation 域有效。不得另建模型轮询器。
5. 原 `_readable_delivery` 在正常权限不足时可调用 `_recovery_delivery_allowed(db, member, delivery_id, delivery_digest)`。后续仍走原 read/ack，不扩大为全历史读取。
6. 守护及执行入口在其真实“开始”事务中调用 `_start_recovery_attempt`；unknown/终态对账用 `_finish_recovery_attempt`。调用方传入继承的稳定根因和原逻辑请求，不以新窗口、许可或时间戳清零。
7. `ControlObserver(store, executable=None).poll()` 在桥首次启动受管审核者前，以及每轮处理审批和派发前调用；退出桥时调用 `close()`。观察器为每个当前可见成员维护独立 metadata-only 客户端，仅初次、绑定变化或重连时查询元数据，通过 `owned_rollout_path` 取得官方返回的准确会话日志，不扫描会话目录。
8. 观察器按文件实体、会话/绑定、字节游标及已读边界摘要读取。首次基准不重演历史；半行不消费；轮转、替换、截断或边界改写登记缺口门禁。游标和控制事件同事务落库，重启补读原位置。原 A 的真实 `turn_aborted` 记录触发统筹中断暂停，但不证明用户按了 Esc，也不证明工具已停止。其他成员原因不明的中断不被解释为全组手动暂停。
9. 仅准确 `event_msg/user_message` 的独立文本 `忽略额度`、`恢复额度保护` 接入窗口额度授权，允许首尾空白。原 A 另可输入 `忽略辅助额度 <32位proposal_id>` 或 `恢复辅助额度保护`，由辅助池核对预先展示的准确账号池、周期及原 A 绑定。引用、转发标识、附件、其它正文不授予授权；不保存普通用户消息正文。
10. 观察连接失败后只在 transport 域有效时有限重连；最多三次，退避 5/15/30 秒并持久记录，成功重新核实原来源后清除该连接故障记录。原始日志内容缺口不因连接恢复自动抹去。此重连不是桥进程重启，不消费桥恢复计数或创建模型会话。

### 已交回工作如何接续

原 A 先调用 `inspect-remaining-work` 取得 `source_digest`。接续计划沿用原任务的验收条件、检查、允许路径、需求版本和执行者约束；`target` 省略时保留原负责人。改交其他成员时，目标登记工作树必须已有同一候选清单及原基准，脚本不迁移脏目录。

`remaining_steps` 是非空的 `{"id":"...","description":"..."}` 列表；`completed_effects` 是 `{"id":"...","evidence":"..."}` 列表，两者 ID 不能重叠。`candidate` 包含 `root` 和 `manifest`，文件清单格式与恢复候选一致，根目录必须是原任务登记工作树。已完成副作用和准确来源写入新任务正文，接手者只办理剩余步骤。

首次调用若返回 `requires_source_read`，按返回的 delivery ID 完整分页读取并确认摘要，再用同一请求继续。原结果消息仍按原协议只阅读、确认和收尾。控制、权限、原执行、身份、依赖、资源或额度不满足时，保留等待意图；后续 `recover --safe` 重新核验，不能通过新任务名覆盖阻塞。原任务保持 handed_back，新任务取得新消息和领取令牌。

接续者的信封及完成入口必须要求读取并确认原交付；新阶段自动继承原模型/提供方/绑定来源，返修关系和累计轮次沿原根任务保留。新任务的首次 claim 再核对当前策略、来源、候选和原执行。旧终态消息被当前接续回合标为 workspace_busy，不等于原工具重新运行；真正 delivering/unknown 或非终态仍阻断。

### 正式替代如何核验

覆盖条目格式为 `{"old":"原义务ID","new":"新义务ID"}`。验收条件 ID 是 `acceptance:` 加正文的本模块稳定摘要，检查 ID 为 `check:<name>`。完整替代任务还须覆盖每个未完旧阶段，ID 为 `stage:<旧stage_id>:<义务ID>`。可用底层检查覆盖多个旧义务，但完整映射必须先写进新阶段的 `payload.supersedes`，让独立审核实际检查覆盖语义；事后 A 单方声明不放行。

`payload.supersedes` 每项包含 `source_task`、`source_stage`（整个任务使用 null）、`source_digest`、`requirements_version`、`source_obligations`、`coverage`。`source_obligations` 是原义务 ID 到完整条件/检查对象的映射，须与原记录一致，让审核者能检查具体覆盖语义，不能只有摘要或 ID。新阶段必须是当前需求下最新的正式 passed，具有准确独立审核结算、生产者、epoch/session/execution/turn、必要检查及辅助审核。缺证据、部分覆盖、未决操作或资源占用均不释放名额。

两套容量读取有效替代关系计算，不直接减缓存数字；重复结算只保存一条有效关系。阶段替代不能释放仍有其它义务的任务。完整替代后，依赖读取、交付和 Git 前置核验引用真正的新产出任务。新候选、需求、审核执行或报告变化会使旧关系停止生效，历史关系与原报告仍保留；禁止替代循环。

已接入的内部辅助方法为 `_continuation_required_deliveries`、`_continuation_delivery_allowed`、`_continuation_contributors`、`_continuation_repair_link`、`_stage_backlog_ids`、`_task_backlog_ids`、`_task_obligation_satisfied`、`_effective_obligation_task`。`_stage_passed(..., allow_supersession=False)` 用于核验替代候选本身，不借另一份替代关系跳过该候选的辅助审核。

## 真实证据边界

执行器能力、原生控制来源、隔离验收和实际加载不能从 JSON 中相信一个 `true`。模块没有公开“登记已通过适配”命令。

宿主控制器可在代码内提供 `_recovery_evidence_adapter.verify(kind, context)`，返回 `VerifiedRecoveryEvidence`，绑定当前 context 摘要、真实回执及相应事实。适配必须经过独立审核并接入实际入口，不能从用户文件加载可执行代码或把用户给出的字典包装成证明。第二批候选已接入`ProductionRecoveryAdapter`及实际运行切换适配，按原API/日志、准确执行、固定文件和实际进程取证；尚未完成动态验收。原生完整工具拦截与Hyper-V证据缺失时仍拒绝放行。新增命令和边界见[next-runtime.md](next-runtime.md)。

`finish` 可以正规引用已有正式阶段：必须匹配恢复 ID、候选清单、审核者、epoch、execution、需求版本、唯一 `review_effective_verdict` 及已完成的生产者记录。缺任何关联仍拒绝，当前阶段的 passed 不能嫁接给另一恢复。引用有效历史证据不把旧工具项说成本轮新执行。

暂停本身不使已完成事实失效。finish/reconcile 可以记录原执行事实，不解除新控制门禁；候选变化使通过无效，但不妨碍保留准确原执行终态。许可过期只影响开始，不能因此抢占已运行或 unknown 执行。

## 第二批候选与未验证项

通用依赖图/SCC、同协作修复继任、跨协作历史追加、真实来源查询、日志缺口新基准、恢复租约、项目内文件复制/备份/运行切换已补写并接线。隐藏审核主动交接和自动并发同样进入候选。具体操作和客户端限制见[next-runtime.md](next-runtime.md)。

当前仍缺原生CLI全部工具路径的可信拦截能力；本地Hook存在不代表完整门禁。原API可见空闲与“全组暂停已经确认”分别记录。能力不足时保持`pausing`，不开放严格恢复。Hyper-V及客体执行器仍暂缓。

所有行为用例、迁移、复制、切换、安装和真实多窗口验证均未执行。源码编译/AST、结构及清单检查不替代这些验证；测试中的证据夹具不能由生产JSON载入。当前安装、活动状态库、账号配置和A/B/C/D均未修改。正式验收等待用户另行通知。
