# 多成员阶段命令与输入

这是开发候选的接口说明，不是完整验收通过声明。最新本地测试与真实窗口边界见 [validation-handoff-four-cli.md](validation-handoff-four-cli.md)。安装仅按用户明确范围进行；活动桥和冻结候选不能热替换，须核实旧执行终态、备份并安全切换。

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

普通complete保存业务交付后，须submit-stage；每人最多两个待审阶段，running计入。模型回合结束不等于通过。必要检查缺失或波动失败未查明时为needs_validation；不要自填“passed”覆盖工具事实。

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

## 协议来源和未验证状态

受管进程参考 [官方Codex App Server文档](https://learn.chatgpt.com/docs/app-server)，实现参数还对照本机CLI生成的JSON Schema；版本差异须实测。当前没有运行新命令、迁移、模型、故障注入或端到端测试，详见验证交接，不把“接口已写”称为“可用/通过”。
