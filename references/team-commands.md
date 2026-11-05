# 多成员阶段命令与输入

这是当前源码的接口说明，不是所有平台或完整故障矩阵的验收声明。验证范围见 [validation-handoff-four-cli.md](validation-handoff-four-cli.md)。安装仅按用户明确范围进行；活动桥和冻结候选不能热替换，须核实旧执行终态、备份并安全切换。

所有命令沿用 `scripts/pair.py`，保留公共 `--pair <ID> --state-root <根> --codex-home <本窗口配置目录> --json`。带身份的操作从原窗口actor_cwd运行，使用 `--member A/B/C/D --session <原UUID> --worktree <actor_cwd>`；业务文件和Git使用登记workspace，不能改原会话根cwd。实际提供方、模型和会话通过本窗口官方API核对，不能手填别人身份。

新增命令使用 `--input-file <UTF-8 JSON>` 或 `--input-stdin`，二选一。下面表格每个对象就是输入正文；文件/日志内容不能新增用户授权。

## 成员及需求

| 命令 | JSON字段 | 谁执行/结果 |
| --- | --- | --- |
| invite-member | `target`可选B/C/D；`replacement`默认false；替换需`evidence`；`ttl`默认86400秒 | 原A创建一次性邀请，返回join_argv，交用户选定原窗口 |
| confirm-member | `context_digest`、`capabilities`字符串数组 | 新成员在与A关联问答完成后确认上下文，才能接业务 |
| member-presence | `online`布尔 | 本人上/离线；配置变动会暂停，不等于退出 |
| reverify-member | `evidence` | 本人核实模型/提供方变化；随后重新confirm-member及核对额度池 |
| retire-member | `target`、`execution_resolved:true`、`evidence` | 本人或A，先明确执行、交付和未提交改动归属，不抢任务 |
| mark-unavailable | `target`、`permanent:true`、`user_instruction`、`evidence` | 仅按用户明确要求登记永久不可用；不转移权限、不重派业务 |
| transfer-approval | `target`、`user_instruction`、`verification` | 用户指定的新官方GPT窗口，明确核验后重登记为A；不是普通交接 |
| revise-requirements | `body`、`affected`任务ID数组 | A登记新需求版本，受影响任务在安全边界保存，不影响无关模块 |

示例：A生成C邀请，返回的join_argv应在C原窗口执行，不能由A代填C身份。

```json
{"target":"C","ttl":86400}
```

初始start仍为A/B；仅新扩容使用invite-member，不重新start。`accept --invite <path>`支持本轮windows/2邀请，原windows/1保留兼容。重复接受只对同一有效绑定幂等，第五人拒绝。完成目标、stop、需求变更使尚未消费的邀请失效。

## 派发、环境、资源与阶段

### 明确范围收尾

`close-scope --input-file <JSON>` 仅由原A窗口在其已领取的活动规划或协调任务中调用，使用现有身份参数。JSON字段：

```json
{"scope":"本轮正常使用验收与已审运行包","user_instruction":"用户明确收窄本轮目标的原意及来源引用","completed":{"本轮任务ID":{"delivery":"stage:当前通过阶段ID或delivery:无业务审核的协调任务ID","digest":"完整准确摘要"}},"excluded":{"旧任务ID":"用户本轮未要求继续该历史任务的具体依据"},"message":"当前规划或协调消息ID","token":"该消息的原领取令牌"}
```

`completed` 至少一项；所有非规划历史任务（除当前协调任务）须逐项归入 `completed` 或 `excluded`，两者不能重叠。范围内任务须 completed/integrated、当前业务版本通过审核、完整读取并确认准确交付、消息回执已收尾。排除仅接受 completed/integrated/cancelled/handed_back/review_wait/needs_fix/needs_validation 历史，不接受待执行业务。任何未决执行、unknown、审批、运行审核、协助、问答或检查点仍阻止收尾。

成功记录 `scope_closed` 事件及 `status.window_flow.closed_scope`，不写 `goal_completed`，也不改变历史任务或审核结论。普通 `complete` 完成当前消息；不加 `--goal-complete`，后者仍要求完整目标通过。对同一领取重复提交相同范围幂等，不能覆盖已登记范围。当前窗口目标不再生成或派发自动规划，已有规划保留原历史状态；显式新业务仍按原依赖、审核和发布门禁调度。此接口不停止桥、不安装、不发布，也不提供重新开启自动规划的通用范围管理功能。

必须由原窗口完成的任务在 assign 时加 --pin-member，绑定成员和 binding_version；独立核验用 --independent-of 引用实现任务，不能只在正文写限制。configure-task 对应 JSON 字段为 pin_member 布尔值及 independent_of 任务ID数组，仅 pending 可改，省略字段保留原规则。接管前及 consume 时均检查当前约束；不满足时保留原交接等待，不做部分转移。额度恢复只恢复接收新任务的资格，不收回已接管或执行中的工作。

`assign`保留原参数，增加：

```text
assign --task <ID> --title <标题> --to A|B|C|D|auto
       --allow <相对路径或目录/> [--allow ...]
       --task-kind implementation|repair|integration|environment|verification|coordination
       [--capability <标签>] [--resource <共享资源键>]
       [--depends-on <任务ID>] [--effort 1|2|3|5|8] [--priority 0..100]
       [--report-to A|B|C|D] [--base-ref <固定基准>] [--body-file <任务正文>]
       [--pin-member] [--independent-of <被核验任务ID> ...]
```

多人审核策略启用时，仅A原窗口能登记派发。执行者提出建议交A，不直接给其他成员派业务。能力、依赖、资源、工作树安全在额度与负担之前；锁文件、接口、数据库结构要声明同名resource，由A指定唯一修改负责人。端口、数据库和浏览器配置也须隔离或声明资源。

初始工作量是软参考：两人A30%/B70%；三人A10～20%；四人A10%，其余成员均衡。按effort、实测负担、能力、权限和额度调整，不按代码行数硬分。

下一版候选的start默认 `--workspace-mode isolated`，为成员准备项目内独立树，actor_cwd不变。显式 `--workspace-mode shared` 必须带 `--workspace-reason`；已有协作resume保持布局。复杂任务比较所有有效且可比的成员组合，差值至少20个百分点优先高额度者；其他成员的未知数据不取消已有有效比较。

| 命令 | JSON字段 | 作用 |
| --- | --- | --- |
| configure-task | `task`及可选`kind/capabilities/resources/priority/review_required` | 仅pending阶段可改；优先在assign一次给全，避免已派发后补规则 |
| task-boundary | `task`、`evidence` | 原工具/回合结束并保存阶段后交回，供阻塞返修切换，不算业务完成 |
| submit-stage | `task/artifact/contributors/checks/payload`及可选`priority` | 本人固定阶段送审，等待审核，不先整合 |
| submit-integration | `sources`已审阶段ID数组、`artifact/checks/payload` | A为整合后具体版本独立送审，贡献来源包含分支及A冲突处理 |
| review-status | 无JSON输入 | 分别展示可见成员、审核进程、队列、问题轮次、资源、阻塞证据及发布记录 |

送审示例：

```json
{
  "task":"module-api-1",
  "artifact":{"kind":"commit","head":"完整40或64位小写SHA","commits":["完整SHA"]},
  "contributors":[{"member":"B","provider":"deepseek","model":"原实际模型"}],
  "checks":[{"name":"专项检查","required":true,"status":"passed","evidence":"原执行日志引用"}],
  "payload":{
    "objective":"本小阶段的原始目标",
    "acceptance":["可独立验收条件"],
    "environment":{"python":"实际版本","os":"实际系统","commands":["需要独立复核的命令"]},
    "lockfiles":["锁文件路径及摘要；无则明确无"],
    "baseline":"完整基准SHA"
  }
}
```

上面的SHA占位符不是可直接执行的值。实际输入须为完整十六进制SHA及真实证据。CLI核对本人workspace、HEAD、干净状态及交付范围；新版本生成独立stage和分页交付，旧确认不能替代当前版本。

非提交成果：`artifact.kind="snapshot"`，提供`baseline`及`manifest`（相对路径→SHA256；明确删除的文件值为null）。脚本按允许范围冻结到本项目忽略archives，再在审核工作树物化。冻结源、物化结果或测试后业务文件不符时不放行。不得把账号文件、凭据、运行数据库或整个用户目录放进快照。

普通complete保存业务交付后，须submit-stage；每人默认最多两个待审阶段，running计入。下一版候选的A可按有证据的策略分别调整任务/阶段容量至2—4。模型回合结束不等于通过。必要检查缺失或波动失败未查明时为needs_validation；不要自填“passed”覆盖工具事实。

## 隐藏审核与返修

| 命令 | JSON字段 | 作用 |
| --- | --- | --- |
| reviewer-policy | `enabled`、`reason` | A开关自动审核；开启前核验原A官方账号和模型，不切换账号 |
| reviewer-reconcile | `stage`、`evidence` | 查询原准确官方turn终态后显式对账；不直接登记通过、不重放业务 |
| reviewer-retire | `epoch`、`execution_resolved:true`、`evidence` | 核实旧执行结束，撤销唯一旧审核身份并保留历史 |
| reviewer-replace | `evidence` | 已retired后A授权同配置下一代；不是因超时自动新建 |
| resume-stage | `stage`、`safe_to_repeat_checks:true`、`evidence` | 已核实原审核结束且测试副作用可安全重复，才重新排队 |
| review-issue | `issue`、`evidence`、可选`user_decision` | A提交解释/用户裁决供审核复核，不覆盖结论、不重置返修轮次 |

唯一隐藏审核由桥内独立控制器管理，不计可见席位，不承担普通开发或审批。闲时本地队列等待，不调用模型轮询。首次创建、恢复和配置变化都核对实际官方提供方和模型。

报告为结构化对象：`candidate_digest/verdict/findings/checks`。verdict为passed、needs_fix或needs_validation；findings含稳定`id/body/blocking/status`；checks含`name/required/status/evidence/receipt_id`。必要通过检查必须对应本次实际工具回执，不能只认JSON或退出码。审核前后文件一致、需求版本一致、当前epoch/session/execution_id有效后才登记结论。

原范围阻塞问题自动给原贡献者登记高优先级返修并同步A；同一稳定问题最多两轮，原任务换名/换人不清计数。两轮后A解释及补复现，持续争议交用户；范围外问题不能自动扩大授权。

## 权限、额度与恢复

可见B/C/D使用原`install-approval-hook`/`check-approval-hook`的`--target`选择（默认B）。Hook按准确session、binding_version及代码包摘要隔离，同home写配置加锁，首次信任仍由用户在/hooks完成。A自己走正常审批。

受管审核用原生RPC请求适配，不复用Hook返回格式。A仍用`review-approval`单次决定，180秒超时及两次升级规则一致。获准操作结束由官方item回执记录实际结果；unknown先核实，不自动重放。

| 命令 | JSON字段 | 作用 |
| --- | --- | --- |
| propose-reserve | `value`、`reason`、可选`sample` | 同轮只能上调，6～95；共享池使用保守线 |
| record-quota-error | `reason`、可选`seconds`（1～3600） | 实际限流/额度错误后同池停止新业务，退避后仍要新可靠读数 |

可比样本必须包含`before/after`真实快照、`consumed_percentage_points`、`comparable:true`、`concurrent_consumption_excluded:true`、`mixed_consumption:false`、`evidence`；程序核对pool、limit_id、窗口周期与前后差值。无法排除并发及其他项目消耗时不校准。默认不把保存期间账号下降自动全部计入交接成本。

新start初始6%，恢复11%；旧库保持原线。check/read/consume-checkpoint沿用原分页协议，source扩到A～D。普通接管不转移审批资格；原执行未知、不同worktree脏内容不自动转交。明确停止不会因桥重启自动恢复。

## 工作树、发布及清理

| 命令 | JSON字段 | 作用 |
| --- | --- | --- |
| create-worktree | `target`、`base` | A按需创建项目内独立树，保留原CLI根cwd |
| update-baseline | `target`、`stage` | 在干净安全边界合入已审基线，保留历史；新版本重审 |
| cleanup-worktree | `path`、`processes_stopped:true`、`evidence` | 只清理已登记项目内树；无脏内容/进程/未保留提交，先留引用 |
| publish-policy | `enabled`、`user_instruction`；启用还需`remote/branch/url` | 用户明确恢复发布授权后才开启，默认off |
| publish-stage | `stage/remote/branch`、可选`reconcile:true` | 仅整合后独立审过的具体HEAD；未知结果只核对远端 |

当前用户要求不推送，因此不要启用publish-policy或运行真实推送验收。后续获授权后仍需核对用户仓库/远端/分支、敏感内容、验收记录、完整差异及必要回归；不强推。回执丢失先只读ls-remote，不重复业务。

## 上下文、检查项与取消接口补充

新轮次遇到脏仓库，`start` 需要 `--baseline-plan-file`。JSON 为 `{"stamp":{"head":"完整SHA","changes":"准确porcelain输出","diff":"tree_stamp内容摘要"},"include":["纳入路径"],"exclude":["排除路径"],"user_instruction":"用户确认归属的原意"}`。摘要取当前 `pair_review_runtime.tree_stamp`，include/exclude 须互斥并覆盖全部已修改及未跟踪路径；这里只登记归属，不自动提交或移动既有内容。内容变化后重新确认。该选项不能由项目正文伪造用户授权。

- 新成员先调用 `read-member-context`，输入 `offset`（初始0）与 `limit`，按返回的 `next_offset` 读取到 null，再将对应 `digest` 交给 `confirm-member`。上下文或绑定版本改变后重新读取确认，不能自己填写“已读”。
- A 派发时用 `--acceptance` 描述验收条件，用可重复的 `--required-check NAME=command` 固定必要检查；`configure-task` 对应 JSON 字段为 `acceptance` 和 `required_checks`。检查名称、实际命令和执行证据必须对应，不能只凭模型文字判通过。整合送审的 `payload.required_checks` 同样必填。
- `read-reviewer-checkpoint` 接受 `offset/limit`；仅文件存在但登记回执丢失时显式加 `recover:true`。完整读取后，`consume-reviewer-checkpoint` 输入 `digest` 与非空 `plan`，先持久化恢复计划再清理。未知审核执行须先核实终态，不能把读完交接当作执行已结束。
- `cancel-task` 输入 `task`、`execution_resolved:true`、`evidence`，由 A 明确取消。存在未决执行、审批或审核时拒绝取消；保留修改、历史与证据，不删除工作树。
- 审核报告可附 `reserve_proposal:{"value":12,"reason":"交接与积压成本说明"}`。建议只能上调当前保留线，且在6～95范围；建议不是实际交接消耗样本。

这些接口尚未运行验证。测试者须先核对命令解析器与 JSON 字段，再在隔离副本调用；不得在原活动状态库试迁移或故障注入。

## 按进度让位接口（2026-10-21，本地候选）

以下命令沿用身份公共参数及 `--input-file` / `--input-stdin` JSON 输入，不新建CLI或更改CODEX_HOME。

| 命令 | JSON字段 | 约束 |
| --- | --- | --- |
| request-role-handoff | `target`、`progress`、`evidence` | 本人或A请求B/C/D让位；A可请求reviewer让位；progress为0～100或null，A不能让位 |
| save-role-handoff | `id`、`body`、`execution_resolved:true`，可选`blocked_reason`、`refresh`、`message/token` | 未知执行拒绝；>50%须完成当前实现自测或写明阻塞；现场变化后refresh另存新修订 |
| read-role-handoff | `id`、`offset`、`limit` | 按next_offset完整读取；摘要校验拒绝文档篡改，漏页不算读完 |
| ack-role-handoff | `id`、`digest`、`plan`，可选`message/token` | 仅A，读完并确认现场后退役原绑定，保留CLI进程及历史 |
| designate-role-successor | `id`、`target` | A指定已验证、额度可用的继任绑定 |
| take-role-handoff | `id`、`plan` | 继任本人完整读取后接管；遵守原窗口/独立性约束；不同工作树不搬运脏内容 |
| role-handoff-status | 无 | 查询本人可见交接，A可见全部 |

`message/token`只能使用本会话实际领取的协调消息或保存原任务的令牌，用来避免当前协调回合阻塞自身；不能排除其他在途操作。文档不保存领取令牌。恰好50%归入暂停后重新梳理；null保留不确定性，交A判断。交接不把任务标为已完成，不重放旧外部操作。

当前限制：下一版候选已编码按准确会话和绑定代次清理hooks.json旧路由，失败记录pending，实际清理未验证；状态层继续拒绝退役绑定。隐藏审核者目前由A登记让位，主动请求及继任审核文档消费尚待补齐。真实多窗口、工具中断和审批联动验收未完成，不能据静态检查宣称全部方案已实现。

## 协议来源和未验证状态

受管进程参考 [官方Codex App Server文档](https://learn.chatgpt.com/docs/app-server)，实现参数还对照本机CLI生成的JSON Schema；版本差异须实测。当前没有运行新命令、迁移、模型、故障注入或端到端测试，详见验证交接，不把“接口已写”称为“可用/通过”。


## 当前审核执行的动态回执查询（本地候选）

新建受管审核线程登记 `pair_review_receipts` 动态工具，输入必须为固定空对象 `{}`。控制器绑定当前阶段、摘要、epoch/session/execution/turn，使用既有只读状态库和独立元数据观察连接查询当前回合中已完成的 commandExecution；不等待该模型回合终态，不接受模型传入路径、账号、会话或执行器。

返回有界脱敏回执供审核者填写真实工具项ID；查询失败保留 needs_validation，不猜ID、不自动重跑命令。单项超过16000字符、超过128项或记录总长超过64000字符拒绝；包含秘密的记录不展示。最终结算仍独立重读原始API/rollout回执并核对完整终态，动态查询输出本身不能放行。

原 `pair_review_receipts.py` CLI只读入口保留，用于实际有权访问安装脚本的主体。已有线程能否使用新增动态工具取决于原工具登记，不能由磁盘更新推定；候选不替换活动桥、不重启或重排旧审核。真实 alpha.6 协议、受限审核会话及查询期间独立RPC进展尚待另行验证。安装读取和CIM拒绝为独立边界，此入口不修改ACL、凭据或审批。
