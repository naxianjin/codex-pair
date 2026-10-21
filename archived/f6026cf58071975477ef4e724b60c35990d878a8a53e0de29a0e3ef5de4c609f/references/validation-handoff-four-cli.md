# 四可见 CLI 与隐藏审核：分模块验证交接

## 20. 原窗口与独立核验接管约束修复（2026-10-20）

真实首现：r4 将原B历史交接交给不同额度池的C，consume 后把B全部pending待办转给C，正文中的“原B执行、C独立核验”没有机器约束。原B随后可靠额度50%仍paused，因为接管后分支直接continue。真实对账见隔离夹具 CHECKPOINT-RECONCILIATION.md；不将C接管说成B自行恢复。

修复在开发源码，未修改活动r4：assign新增 --pin-member（成员及binding_version）与可重复 --independent-of（已登记其他任务）；configure-task对应用pin_member/independent_of字段，仅pending可改，省略保留。约束存入已有options，不变更数据库schema。统一执行者校验用于调度，接管候选及consume再次验证全部待转任务和接管者已有核验任务，禁止同一窗口接管实现后自审；失配保留原交接，不做危险部分转移。原窗口转交后达到可靠恢复线、身份可用、池未阻塞且无在途工作时恢复ready，只接新任务，不收回已转交或执行中工作。

新增7项隔离回归：固定成员及代次、独立核验不得接管实现、offer后新增限制消费拒绝、恢复资格但不抢回、无效规则事务回滚及CLI、规则更新保留、实现归属变化后独立性重检。首次新增测试误用不存在的set_control产生4项AttributeError；修正夹具为control。另1项paused恢复断言失败，定位为检查_member_suspended时paused本身即返回true，改为实际成员资格及池门禁后通过。保留低额度、未验证身份不恢复的断言。

实际执行：D:/Anaconda3/python.exe -B -m unittest tests.test_assistance tests.test_team tests.test_handoff_balance -q，85项通过，7.808秒，exit0。全量 D:/Anaconda3/python.exe -B -m unittest discover -q，328项（325通过、3跳过），77.556秒，exit0；未启用真实模型开关。git diff --check exit0，仅既有换行警告。全量后仅补充测试断言和文档，专项已复跑。

限制：新规则必须结构化登记，不能从旧正文自动猜约束；不回滚历史C转交、不抢回在途任务。原C独立复核、四配置目录安装及新冻结副本真实B恢复仍待完成，不把源码单测说成运行实例已修复。当前协作安全暂停，旧r4未热替换；不提交、推送或Release。

## 0. 交接状态和使用边界

2026-10-19后续更新：用户已允许先做小部分测试，并要求发现问题及时修复、在本文件保留修复说明。第11节记录当前双原窗口的额度身份专项，覆盖范围外的模块仍待验证；前文“本轮未运行测试”指该次连续编写草稿的历史状态，不适用于第11节。

### 最新补充：优先核验接口与已知风险

以下均未验证，不能按实现名称推断已通过：

1. 上下文分页：用新成员调用 `read-member-context`，故意漏一页、重复一页、改需求版本或替换绑定，再调用 `confirm-member`。只有完整覆盖当前摘要、实际身份及握手成立时才能接任务。
2. 必要检查：A 固定 `required_checks`，分别提交缺项、同名不同命令、无实际工具回执、退出码非零和完整成功证据。缺项与失败不能通过；整合阶段也必须重新提供检查。当前未独立核实 App Server 工具事件的命令/回执字段兼容性。
3. 审核检查点：在隔离副本模拟文件写完但数据库登记中断，显式 recover 后完整分页、持久化 plan，再消费。旧执行 unknown 时禁止清理和复跑；删除失败必须保留恢复索引。创建回执丢失且没有审核 UUID 的情况仍需人工调查，不自动猜测会话。
4. 取消与迟到：运行中取消应拒绝；明确终态后取消保留文件和历史，迟到结论不能重新放行。阻塞返修通知应先于普通任务，但不能打断或重放在途工具。
5. 跨库会话占用：全局绑定库与项目库不是同一事务；在两次写入之间中断，核对孤立预约不会导致双绑定，且能在调查后恢复。普通非法加入不应先占席；数据库提交失败窗口仍须专门注入验证。
6. 发布门禁：旧 passed 阶段之后改需求或提交新版本，即便旧记录仍在，也须拒绝发布。保持发布开关关闭；此项先以隔离 Git 远端替身检查，不执行真实 GitHub 推送。
7. 尚需重点审查：脏基线的用户确认、受管审核工作树清理登记、A 永久不可用且旧审核结果未知时的职责迁移、所有可见执行器的实际额度错误识别。当前不能据已有接口断言这些边界全部闭合；若源码检查发现缺口，记录为待修复，不降低预期。

本轮仅做 Python AST 语法解析与 `git diff --check`，没有导入项目、运行测试或启动会话。最后一次静态结果应绑定接手时的差异；历史功能通过数字仍只适用于对应历史候选。

后续源码补充了脏基线确认选项与审核工作树登记，仍须测试：给 start 缺失、过期或遗漏路径的 baseline-plan-file 应拒绝；完整确认不应提交既有改动。审核工作树仅在审核者退役、无进程、无未知执行且干净时允许清理，先保存提交引用；不因 A 的其他工作误判审核树可删除。第7项列出的迁移及额度错误边界继续保持待核验。

本文件由实现者于2026-10-19整理，供其他测试者逐部分验证。用户已要求：先写实现，不运行后续测试；测试完成前不推送GitHub、不安装到配置目录、不创建Release和标签。因此下面的“预期”均是验收标准，不是已观察到的成功。

开发分支：`feat/four-visible-reviewer-20261019`。基础发布版：`a0cb4b927697b831cc9bef34394a4c0178dff3c9`（v1.2.1）。请记录接手时的完整HEAD和工作树差异；有未提交实现时先固定隔离副本或本地候选，不能直接拿历史SHA冒充当前版本。

原始主工作树仍有用户/此前维护改动，不包含在本轮开发树中。不要重置、清理或把主树内容顺手提交。已安装v1.2.1和历史验收库不能用于新版实验。先用临时Git仓库、临时状态根和专用测试账号/会话；真实多窗口测试由用户选定原窗口。

### 证据分层

| 层级 | 可证明的内容 | 不能代替的内容 |
| --- | --- | --- |
| 静态代码阅读 | 预期状态转换、调用路径、潜在问题 | 运行成功、权限实际生效 |
| 临时SQLite/假transport | 事务、幂等、版本、边界条件 | 原窗口唤醒、官方账号、真实额度 |
| 真实工具及受管CLI | 本机Git/进程/官方RPC行为 | 四个原可见窗口全流程 |
| 四个原窗口端到端 | 成员、通信、权限、审核、返修与整合协同 | 其他账号/提供方组合全部通过 |

每项结果写“通过 / 失败 / 阻塞 / 未运行”，附准确版本、环境、命令或操作、退出码、预期与实际、证据位置、测试者模型/提供方。不能把队列接受、模型结束、文字含“通过”或单次偶发成功当作验收。发现失败保留第一次证据，修复后单独复测。

## 1. P1 历史结果：审核身份、租约和迁移

涉及：`scripts/pair_reviewer.py`、`scripts/pair_core.py`、`tests/test_reviewer.py`。

### 已经做过，但只对应旧候选

- 业务候选 `13710a972eb82a11f6e5a67bcdb117f13b2294ce`：17项专项通过；54项受影响旧迁移模块通过；完整246项中243通过、3跳过，用时55.964秒。
- 前一轮完整245项中242通过、3跳过；迁移返修过程中曾有三处旧版本号断言失败，更新期望后才重跑。不能隐去失败。
- 独立隐藏会话的实际模型/提供方核验为 `gpt-6.1-sol / openai`，账号API确认ChatGPT官方账号。只创建过一个审核原型会话，后续复审恢复该同一会话。
- 独立文本审核关闭：P1-REV-001（隐藏绑定后又加入可见成员）、P1-REV-002（租约回执丢失无法显式对账）、P1-REV-003（schema10缺列无法升级）。
- 最终审核结论为 `needs_validation`，不是通过。审核原型的只读工具被策略拒绝；文本材料由A从固定Git对象提供。审核者没有亲自运行Git、迁移或测试。
- 本轮继续修改了核心、迁移和调用链，以上数字不能证明现在的版本通过。本轮当前版本未运行测试。

### P1 复测步骤

1. 在上述旧候选的隔离worktree先复核历史测试；记录3个跳过项及原因。用 `python -m unittest tests.test_reviewer` 和 `python -m unittest discover -s tests`，保存原始stdout/stderr。
2. 在当前候选重新运行相同命令。当前schema目标为13；旧测试中硬编码11和人为删表降版的fixture需要按真实旧结构构造，不能只把所有失败改成新数字。
3. 分别构造OpenAI GPT、DeepSeek、缺提供方、非GPT、ephemeral、错误cwd、错误UUID的proof。预期仅经实际API核验的原A及独立官方GPT可绑定审核者；字典校验本身不能证明API真实性。
4. 并发调用reserve：预期仅一个starting意图。模拟thread/start回执丢失：unknown保留，禁止自动再建。
5. 审核绑定后用同一UUID加入可见B；反向先加入B再绑定审核；两操作并发竞争。预期只能保留一种有效身份。
6. 同epoch并发acquire：只一个owner；普通status不得暴露owner。丢失领取回执后，原A通过公开execution_id和实际终态证据显式对账；B、错误epoch/session/execution_id、unknown终态均拒绝。旧owner释放及旧对账不能结束新执行。
7. 599秒无提醒、600秒仅一次提醒；有进展后计时重置。检查未释放租约、未判任务失败、未杀进程。
8. 用真实schema8、两种schema10（有/无execution_id）、schema11、schema12复制库升级13。检查备份integrity_check、成员、claim_token、任务归属、交付digest、分页确认、历史事件及旧保留线不变。拒绝历史实验schema9与高于当前的版本。
9. 在DDL执行后、更新user_version前注入SQLite失败；预期该迁移事务回滚，备份可读。实际磁盘满、断电和备份恢复仍须独立做，不能用authorizer注入代替。

## 2. P2 成员、邀请、身份与退出替换

涉及：`pair_team.py`、`pair_windows.py`、`pair_commands.py`、core.join/_binding/_message_current。

前置：临时Git仓库，A/B两个原CLI；扩容验收再由用户选C/D原CLI。命令JSON格式见 `team-commands.md`。

1. A执行start，B接受初始邀请。预期仅建立A/B；不枚举最近窗口、不更改会话根cwd、不另开可见CLI。
2. B完成与A关联问答后confirm-member，确认需求、能力、工作树及上下文摘要。未确认前assign给B应保持等待；仅queue接受不算通信通过。
3. A发两个独立邀请，让C/D在业务中途接受。重复同一窗口接受同一邀请必须幂等；另一个窗口使用已消费邀请须拒绝。
4. 用两个进程竞争最后席位；预期最多四个可见绑定。第五人拒绝；隐藏审核者单独统计，满员总计五个模型会话。
5. 邀请过期、需求版本变化、stop和goal-complete后旧邀请失效，不因迟到加入重启桥或业务。
6. 同一会话从不同项目尝试加入：预期配置目录的全项目注册表阻止重复绑定；不同UUID可以同时参加不同项目。注册表回执丢失只核查，不删除未知绑定。
7. 临时offline或低额度不等于retired；其他安全独立工作可继续。正式退出导致不足两个有效绑定时，不开始新业务；只允许保存、收尾与协调。
8. B/C/D替换前保留旧提交、交付和脏内容；进行中或unknown拒绝退出。退役后替换邀请必须匹配旧binding_version；新窗口接受后递增代次。旧消息领取/完成、旧Hook和旧邀请不能作用于新成员。
9. 切换模型/提供方/账号，预期检测并暂停；保存现场后reverify，再确认上下文、权限和额度池。目录名、模型别名不能作为官方资格证明。
10. A永久不可用的替换必须有用户明确指定和新审批者实际资格证据；不能仅因超时转移。检查角色重登记、旧审批撤销、历史保留，以及普通工作接管不改变审批资格。

未验证：全项目注册表与本地事务之间崩溃窗口、邀请与替换竞争、同home不同会话隔离、真实四窗口加入、永久A替换。

## 3. P3 阶段送审、放行、返修和规划

涉及：`pair_stages.py`、`pair_review_runtime.py`、core.assign/complete/_schedule/_stage_complete、`pair_commands.py`。

1. A为任务登记小阶段、allow、base-ref、依赖、capability/resource、task-kind、effort和优先级。同阶段零碎修改合并提交。提交材料必须含目标、验收、需求版本、固定提交/快照、实际贡献者模型来源、锁文件、环境和检查结果。
2. 业务complete只保存交付，不表示验收；相关依赖在未审、needs_fix或needs_validation时不能激活，integrate不能绕过。
3. 审核者直接读取候选隔离worktree，不先整合；证明没有“整合等审核、审核等整合”的循环。
4. 每成员两个待审上限，running也计入；已完成交付仍能保存。第三阶段不开始，优先返修、补证据；同优先级FIFO，跨成员不得饥饿。
5. 伪造完成回执、普通文字“通过”、错误digest、旧epoch、旧session、旧execution_id、缺必要checks、非零工具回执均不得放行。
6. 送审后修改需求、代码、必要验证条件，预期旧结论失效。测试修改业务树或产生未知文件，预期needs_validation并保留，不混入候选。
7. 对同一稳定问题连续返修：第一、第二轮仍由原贡献者、原allow范围负责；换任务名/负责人不能重置计数；第三轮转A解释/方案，持续争议交用户，不由A覆盖结论。
8. 原执行者另有任务：保存当前阶段、等工具结束后task-boundary再切换，不同时领取两个业务任务；返修优先级高于普通开发。
9. 非阻塞建议只记录；缺测试、不稳定失败未查明保持needs_validation，不能只取最后一次成功。
10. A自身实现、冲突修复、DeepSeek实质贡献均须独立审；合并或转交不抹去来源。审核报告、结果收尾、返修通知不得递归生成相同审核任务。
11. 一个模块完成且结果收尾后可登记后续规划，不等待无关模块；规划具有独立ID和依赖，保持唯一统筹、去重。规划不能修改业务文件。
12. 非提交快照需另测路径、摘要、物化和清理；任何未支持的自动路径须明确needs_validation，不能偷换成HEAD版本放行。

未验证：全部新阶段队列/返修/门禁流程、快照审核、整合后复审、模型真实结构化输出、必要工具回执与候选的绑定。

## 4. P3-R 隐藏受管审核生命周期

前置：A实际OpenAI官方GPT；原A配置已登录；Codex app-server版本记录；隔离reviewer worktree可写但业务源码不能由审核者修复。

1. 触发第一个待审阶段，预期只创建一个独立隐藏模型会话；与A同真实账号及模型，不改可见会话，不切账号。A不合格/启动失败，最多保留每成员两个待审阶段，相关整合与发布等待。
2. 空闲十分钟不新增模型回合；本地队列等待不算模型轮询。长工具无进展只通知核查，不按时间杀进程或抢占。
3. 重启桥：优先恢复原审核UUID；重新核验提供方、模型、cwd、账号。配置变化先暂停，不静默换模型。
4. 模拟thread/start和turn/start回执丢失，原执行是否开始未知。预期留starting/unknown，禁止重放。按准确turn_id查询持久终态后才能显式对账；缺ID时人工核查。
5. 明确替换：保存队列、进度、证据；核实旧执行结束；retire撤销旧资格，再用同一确认配置建立下一代；旧会话迟到结果拒绝。
6. 两个控制器竞争reviewer-process.lock：仅一个有效实例；第二个不得派发或放行。常驻空闲进程不能占业务锁，等待审核的A仍能处理只读协调及审批。
7. stop/revoke时取消可安全取消回合；interrupt回执不当作工具确定结束；unknown留存。关闭后不得自动恢复模型或业务。

未验证：真实受管原生权限通道、进程崩溃/Job行为、恢复原模型、双进程与迟到结果；历史只读审核原型没有证明这些产品化路径通过。

## 5. P4 审批路由与同home Hook

涉及：`pair_approvals.py`、`pair_permission_hook.py`、`pair_managed.py`、transport.server_requests。

1. B/C/D每人一个精确session及binding_version处理器。测试A/B同home、B/C同home、其他项目邻接处理器；A自己的事件必须回正常审批，不能进入本轮回调递归。
2. 两进程并发更新hooks.json。预期文件级锁、原子替换、保留相邻/其他协作定义和备份；只更新本轮自有marker，不移除所有旧`:B`处理器。
3. 首次/hooks信任由用户操作，程序不能写内部信任记录；包摘要变化后重新核验，不把安装当作生效。
4. shell、文件、MCP受支持请求核对完整输入、目录、会话、回合、摘要、授权版本及副作用；不含密钥。普通答复/成果通过不能作为权限允许。
5. A allow一次后仅原操作消费一次；改参数/目录需要新请求。撤销与消费竞争：事务先后决定，granted不假称可撤回；未知执行先对账。
6. 等待180秒超时拒绝；A核实未执行并prepare-approval-retry后原成员重发；连续第二次超时needs_user，禁止自动循环。
7. 固定协调命令不要求递归汇报自己；含管道、额外shell操作、不同脚本、不同home/session/state-root不能伪装协调命令。
8. 隐藏审核RPC分别覆盖commandExecution、fileChange、permissions请求；单次accept，不用acceptForSession或持久规则。未知RPC拒绝；严格按实际CLI协议适配，不能复用可见Hook回包。
9. A不可用、委托撤销、成员替换、模型变化、旧epoch及协作停止，原生审批必须拒绝；审核者不能审批自己。原生获批后的实际结果必须核验，不能只记授权成功。

未验证：以上新多人及受管RPC场景全部；旧双窗口审批历史测试不能覆盖同home与隐藏审核适配。

## 6. P4-Q 6%保留线、共享池与恢复

涉及：balance/quota/assistance、stages.propose_reserve/quota_error。

1. 新轮次reserve=6、resume=11；旧库9或更高保留，升级不降低。读取status核对真实持久值，不只看文档。
2. 在5.99、6、6.01、10.99、11边界注入可靠快照。达到保留线不开始新业务/审核；只有恢复线以上新可靠读数才恢复。未知、过期、到reset时间不等于恢复。
3. 非数值、NaN/Infinity、布尔、低于6、高于95、低于当前线均拒绝。上调持久化；恢复线加5、最高100。
4. 可比交接样本核对账号池、额度桶、周期和前后读数，公式max(当前,6,2×消耗+2)、最高95。并发或其他项目消耗无法排除时不归因交接。
5. A与审核者同账号池只算一份；同账号不同可见窗口不能规避暂停。不同额度桶/周期不能错误混合；共享池使用保守适用线。
6. 未知额度仍可按能力和负担分配，但明确百分比保护不可用；实际quota/rate-limit错误后同池停止新派发并退避。退避到期仍需新可靠读数。
7. 恢复优先审批/协调，再阻塞审核，最后新开发。A及审核者暂停时B/C/D独立授权任务仍可继续，不能用待审成果做下游。
8. 保存检查点中断、文档已写DB未登记、消费DB已提交清理失败，必须分别恢复，保留claim_token和唯一现场，不重复业务。

未验证：动态保留线和共享池新逻辑全部；真实账号的周期耗尽、短限流及恢复需单列，不用人为阈值模拟冒充。

## 7. P5 工作树、集成、发布和清理

涉及：`pair_workspace.py`、`pair_git.py`、CLI integrate、stage/publication记录。

1. 已有用户脏改动时不擅自提交；启动前确认纳入基线范围。相同工作树业务修改串行；独立树可并行，公共接口/锁文件/数据库结构由A指定单人及resource互斥。
2. 测试端口、数据库、浏览器目录等用不同资源或同名锁；依赖环境准备任务通过后才启动验证。缓存可复用但不能共享未经核对的可写环境。
3. 干净阶段合入已审基线，落后树不能直接忽略新接口；冲突保留给明确负责人，新版本重新审核。
4. integrate仅接受对应固定已审版本；A串行整合，整合后另建integration阶段、保留各贡献者来源，运行跨模块回归并独立审。不能沿用所有分支各自通过结论。
5. 发布开关当前必须off。先测试关闭时无网络写入；后续用户明确允许测试推送时，另建专用仓库分支，核对owner/remote/branch/URL、受审HEAD、干净树、敏感信息和更新记录。
6. 远端前进/推送拒绝不得强推；成功但回执丢失记unknown，先ls-remote对账，同SHA确认成功，不再次推业务。
7. 清理只针对项目内已登记worktree；脏改动、运行任务/进程、未保留提交、有效审核身份均拒绝。先保留引用再git worktree remove，不递归强删。历史证据与恢复索引保留。

未验证：全部新发布/清理门禁、受管资源冲突、整合后审核，以及“推送成功但回执丢失”的真实场景。本轮禁止实际推送，故发布测试须由用户另行授权。

## 8. 故障注入矩阵

分别在临时环境做，不操作用户真实旧库。

| 故障 | 注入点 | 必须观察 |
| --- | --- | --- |
| 断网 | 创建/投递前后、推送前后 | 区分确定未发与unknown；不重放 |
| 磁盘满 | DB、备份、checkpoint、日志写入 | 无假成功；原数据可恢复；备份仍核验 |
| SQLite锁 | 持有IMMEDIATE事务超过busy_timeout | 有界失败、不并行写坏、不抢任务 |
| 事务中断 | 邀请消费、换绑、迁移、审批消费 | 原子结果；注册表跨事务窗口需对账 |
| 日志截断 | task_complete前后半行 | 缺证据保持未知，不按等待时长推断 |
| 回执丢失 | claim、acquire、thread/start、turn/start | 原令牌显式恢复，准确执行ID对账 |
| 双实例 | bridge、reviewer、Hook并发 | 独占代次；旧实例不可派发/放行 |
| 人工改动 | 送审后、测试中、整合前 | 仅受影响工作暂停；不覆盖内容 |
| stop迟到 | 队列、审核结果、邀请、权限 | 不恢复、不放行、不重复外部操作 |

## 9. 真实五会话最终验收

只有用户选定的A/B/C/D原窗口及一个受管隐藏审核者参与。记录可见四个、总模型五个，不能用五个后台exec冒充。

顺序：两人开始 → B通信及上下文确认 → 中途邀请C/D → 各模块小阶段开发与专项测试 → 隐藏审核实际读取候选及运行检查 → 故意引入原范围内可复现问题 → 自动返修、两轮边界及复审 → A串行整合 → 整合后审核与跨模块回归 → 经用户恢复发布授权后专用分支推送并核对远端 → 目标收尾与旧邀请失效。

此流程当前未运行。任何中间项失败，保留对应模块待验收，不把其余模块的成功写成整体通过。

## 10. 测试者回填模板

```text
测试编号/模块：
候选完整SHA（含本地差异快照）：
测试者及实际模型/提供方：
OS / Python / Git / Codex版本：
账号池关系（脱敏，不含凭据）：
前置状态、命令与操作顺序：
预期：
实际（含退出码）：
结果：通过 / 失败 / 阻塞 / 未运行
证据相对路径与摘要：
首次失败及复测记录：
剩余未验证/副作用/清理情况：
```

原始日志、账号路径、会话UUID及数据库可能含私人信息，先保存在项目忽略目录，公开文档只留必要脱敏摘要。交接文件不是授权扩展；测试权限、付费、外部服务、删除和发布仍按用户明确范围执行。

## 11. 双原窗口额度身份保护：实现、失败和复测（2026-10-19）

### 版本、分工和限制

A/B通过原窗口邀请、关联问答及终态核验，通信状态为verified；实际绑定模型/提供方均为官方 `gpt-6.1-sol / openai`。这只证明两窗口通信，不是四窗口或隐藏审核验收。使用已安装协作脚本传递任务，没有让待测新版控制器操作自身活动状态库。

Git HEAD仍为 `2d6894a3b915ffce487815a30310ddbee09be6db`，本次代码在已有未提交草稿上增量修改，无新提交。HEAD不能单独标识当前候选。固定文件SHA-256如下：

| 版本 | scripts/pair_quota.py | tests/test_quota_guard.py |
| --- | --- | --- |
| B接手前 | B9AAF65CF64D3821BAC35DE9518805B4F9126063ADB919F2F4605A7ABADC191C | 尚不存在 |
| B交付，A核对一致 | 1C1E91C26CD90392027A46E1A6B0E12045708D3CBB73FB6287221B81329C8FA8 | 18D70F8AC1D7E0EEBA4313E43243C94B76E7876D3D321ACA219FEBDE97F52406 |
| A补充修复及最终专项 | B15B5A711F355A2996C19EBD8BC43E0E6E9E6150233332F8A067E6F47A639311 | 180C2A3DDA0482694D5910C0F0BDC292D1B443CC461C1F0B1BE199D10C75234E |

B实现同步refresh与后台QuotaMonitor的共用错误处理，准确模型/提供方变化暂停对应成员；账号变化暂停同home有效成员；退役成员不查询、不复活。未消费的可见审批失效，unknown业务及claim_token不改。普通网络失败保留旧fresh和0额度保护。A完整读取交付、确认摘要后独立审查及运行测试，再补下述竞争修复。

### 发现的问题与修复

1. **B首次测试收集和夹具问题（B报告）。** `from tests.test_core import CoreTests` 意外收集旧Core测试；首次29项运行还出现2个同home子用例失败。B改为模块导入，并从数据库读取规范化home，随后新专项9项通过。不能因此隐藏被意外收集的旧Core失败。
2. **旧查询失败暂停新绑定（A复现，已修复）。** 获取成员后、处理错误前发生替换时，原实现只按成员字母找当前行，旧账号错误会暂停替换后的成员。新增回归修复前为 `AssertionError: 'suspended' != 'active'`。修复为在事务内重新比对session、home、cwd、proof和binding_version，旧代次不暂停当前成员。
3. **旧查询成功覆盖新额度（A复现，已修复）。** 原实现将查询结果直接按成员字母保存，替换后的63%被旧提供方查询覆盖为None。新增回归修复前为 `AssertionError: None != 63`。成功和失败保存均改为事务内核对当前绑定后写入；不接受退役或已更换代次的回执。
4. **后台客户端跨绑定复用（A源码发现，已修复并补测）。** 缓存原来只以成员字母索引。绑定变化后关闭旧客户端，清理旧刷新时限，再按新home创建客户端；新增用例验证两个客户端及最终关闭。不触碰真实账号登录。

以上修改仅涉及 `scripts/pair_quota.py` 和 `tests/test_quota_guard.py`。没有修改调度、真实提供方配置或其他现有草稿。

### 实际执行和结果

均在开发工作树运行，解释器为 `D:/Anaconda3/python.exe`；假客户端与临时SQLite，不调用真实模型或实际额度耗尽。

| 执行者/阶段 | 命令（前缀均为上述Python） | 结果 |
| --- | --- | --- |
| B交付报告 | `-m unittest tests.test_quota_guard -v` | 9项通过，退出0；A随后独立复跑相同9项通过，0.701秒 |
| A初始独立复跑 | `-m unittest tests.test_handoff_balance.HandoffTests.test_monitor_timeout_preserves_snapshot_until_expiry_or_reset -v` | 1项通过，退出0，0.055秒 |
| A修复前复现 | `-m unittest tests.test_quota_guard.QuotaGuardTests.test_old_query_failure_cannot_suspend_or_overwrite_replacement tests.test_quota_guard.QuotaGuardTests.test_old_success_cannot_overwrite_replacement_quota -v` | 2项均失败，退出1，0.114秒 |
| A补修复后 | `-m unittest tests.test_quota_guard -v` | 11项通过，退出0，0.800秒；随后增加缓存用例 |
| A最终专项 | `-m unittest tests.test_quota_guard tests.test_handoff_balance.HandoffTests.test_monitor_timeout_preserves_snapshot_until_expiry_or_reset -v` | 12项新专项＋1项旧额度测试，共13项通过，退出0，0.910秒 |
| A核查已知失败 | `-m unittest tests.test_core.CoreTests.test_overlapping_scopes_not_active_together -v` | 1项失败，退出1，0.053秒；详见下项 |

最终专项与旧Core核查是两次独立运行：前者退出0，后者退出1；不能将串行命令最后退出1解释成13项专项失败，也不能用前者覆盖后者。

### 保留的失败、未验证与下一步

- 旧Core用例仍为 `AssertionError: 'active' != 'pending'`（test_core.py的重叠范围调度断言）。A已独立复现，未改成新期望来消除失败。涉及现有调度与新独立worktree语义，须单独核查需求和修复，当前不判整体通过。
- 未执行全量回归、真实切换账号/模型、真实限流耗尽、四可见窗口或隐藏审核全流程。可见执行器所有限流入口的共享池退避也不包含在这13项中。
- B原交付得到A独立阅读和复跑；A随后追加修复改变了候选，最终版本尚未获得另一审核者或产品隐藏审核器的独立放行，不能沿用旧结论发布。
- 未提交、推送、安装、打标签、创建Release或清理用户内容。本小阶段保存后暂停，后续优先处理调度失败和最终版本独立审核。

## 12. 可用候选推进：调度兼容性修复（2026-10-19）

用户随后要求两原窗口继续做到可用skill，优先解决大问题。上节的阶段暂停不再限制后续工作；不推送、不覆盖安装、不创建Release的要求保持。当前仍有未提交草稿，不能把HEAD或历史P1测试数字当作本次候选证据。

### 调度失败与修复

B先复现 `tests.test_core.CoreTests.test_overlapping_scopes_not_active_together`：1项失败，退出1，`AssertionError: 'active' != 'pending'`。根因是新版调度只保留成员/workspace锁，旧轮次跨工作树的重叠范围锁被移除。

修复在 `Store._schedule` 区分旧策略与新版成员上下文：legacy或未启用member_context_required的旧入口保留active/delivered全局范围锁；新版quota-balanced且已核验成员上下文时，独立workspace可并行，同workspace仍串行，跨树共享资源锁不变。原Core失败断言没有删改。新增3项Team用例覆盖旧范围锁、新版独立树同路径并行、同树不同路径串行，并让原资源用例显式启用新版模式。

B新增4项相关用例首次为3通过1失败，保留legacy范围锁复现。第一次四模块回归另有 `test_same_home_is_one_resource_pool` 失败：`AssertionError: 70 != 0`。夹具分别读取time.time造成微秒级不同重置时间，不能代表相同周期；修改夹具共用now，保留0额度断言，不放松生产周期比较。

### 固定版本与独立核验

| 文件 | B交付后SHA-256 |
| --- | --- |
| scripts/pair_core.py | 007E0904D73EC95C89894D6DE597241D8993DB112D142B3E0B0209C91DE3ECFB |
| tests/test_team.py | 6EACC5E4884E54C0A59C934C8C9612069E72D43B51982967F1F8CE45E996EB91 |
| tests/test_handoff_balance.py | 2C868009A18EFC85ED39E759760160ED116A9BE3881C4E18120DDF29A5922060 |

A完整分页读取交付并核对摘要后实际阅读修复及用例，再独立运行：

```text
D:/Anaconda3/python.exe -m unittest tests.test_core tests.test_team tests.test_handoff_balance tests.test_quota_guard -v
```

结果退出0，74项中73通过、1跳过，6.322秒。跳过的是需显式启用的真实Core集成，不当作通过。上节旧调度失败在此候选已通过；不能将上节历史失败删除，也不能据此判四窗口全流程通过。

B同时独立阅读并复跑第11节A修复后的额度候选，12项新额度用例通过，两个文件摘要保持不变。该核验限于所述代码及模拟边界，不是产品隐藏审核器的端到端放行。

本阶段另运行 `D:/Anaconda3/python.exe -m unittest discover -s tests -q` 进行本地回归清点，未设置真实测试开关：272项，62.055秒，退出1，14项failure、4项error、3项skip。四模块专项通过不覆盖此失败，不能据此打包为已验收可用版。

首次失败清单（保留，后续逐项记录修复和复测）：

- ERROR：ApprovalTests.test_install_preserves_other_hooks_and_does_not_forge_trust；DelegationTests.test_hook_upgrade_preserves_neighbor_handler_and_invalidates_old_allow。均为 `JSONDecodeError: Extra data`。源码已定位Hook写入末尾追加了字面量反斜杠n，而不是换行；尚待修复，不能安装该草稿。
- ERROR：LongMessageTests.test_cli_read_after_compact_claim_and_explicit_delivery_recovery，`Current original CLI required`。须核对新增原会话身份校验与测试环境，不允许为过测试取消身份防护。
- ERROR：WindowTests.test_changed_invitation_is_rejected_before_contacting_window，`Invalid session UUID`。邀请篡改预检未在窗口接触前拦截，待核查。
- FAILURE：AssistanceTests.test_observed_handoff_cost_raises_reserve_without_token_conversion（9!=14）；test_shared_pool_and_unknown_do_not_offer_false_capacity（ready!=saving）；test_takeover_transfers_coordinator_and_keeps_dependency_unfinished（A!=B）。须分别核对旧策略兼容性和新版共享池、不可直接移交审批者的需求，不能盲改断言。
- FAILURE：DeepSeekTests.test_current_window_uses_provider_metadata_without_account_rpc的openai子用例（account RPC forbidden）；MultiAccountTests.test_cli_actor_and_relay_reject_wrong_home_before_contact（错误顺序）；test_shared_state_reopened_from_other_account_and_envelope_routes_back（新增全局绑定索引目录）。须区分合理的新身份验证与实际回归。
- FAILURE：WindowTests.test_communication_requires_both_model_and_business_receipts；test_completed_handshake_recovers_after_original_turn_interrupted；test_existing_distinct_worktrees_are_reused_and_can_have_parallel_tasks；test_manual_receipts_never_mark_automatic_window_communication_verified；test_previous_b_starts_new_round_as_a_and_old_invite_stops；test_ready_notification_waits_for_real_handshake_terminal_states；test_same_home_and_same_cwd_are_valid_but_same_session_is_not；test_shared_workspace_serializes_tasks_and_remote_turns。表现包括握手不放行、任务不派发、旧邀请未拒绝、错误提示和manual说明不符；新成员确认不能靠弱化生产门禁解决。

审核门禁、受管审批、上述失败、实际多窗口和包完整性仍需后续阶段核验。本次没有推送或覆盖已安装技能。

## 13. Hook、邀请和握手入口修复及独立复测（2026-10-19）

B在允许范围内修改6文件，A完整读取3588字符交付、核对全部摘要后阅读生产修改。无提交，仍是未提交候选；不把HEAD或上轮摘要当成本轮版本。

| 文件 | 固定SHA-256 |
| --- | --- |
| scripts/pair_approvals.py | C2C7E93333C25181D0867919F3DC12DED01A98A7134F1F9F27819639524201C5 |
| scripts/pair_team.py | 5D0F60C0BEDA3BAE90F66AB0D85E7298C9C74D738E4BEEAFFCD8E9548EBB55BA |
| scripts/pair_windows.py | E948D8FF888E804F0A5E424035E2EF9555485497C02144898A90A189061CE6F2 |
| tests/test_approvals.py | 4C098231BDD76AB49CE291BA7C7E51F81D4E83EE48ECCA00484092CA87CB1C1D |
| tests/test_deepseek.py | A09A2F365B9355D1DBA90C55F05D5880CDC99459D0F95CA12A2C178C1033863A |
| tests/test_windows.py | 990E5DEEDC4F6375471B75C2080EABC9FD40FDAB854459B3FE82C9D78FBE2AD1 |

### 产品修复与测试适配分别记录

- Hook末尾改为真实换行，序列化后先实际解析JSON，再写临时文件、flush/fsync及原子替换；保留锁、原字节备份、相邻handler和首次信任。新增原JSON非法不覆盖、双线程更新保留邻居等用例。只写临时home，未修改用户真实Hook。
- 邀请固定规范化payload摘要和邀请人会话/绑定代次；接受前校验协作状态、目标完成、有效期、需求版本及原A身份。accepted幂等分支不再绕过失效检查。同一有效绑定重复接受仍幂等，改home/cwd/proof拒绝。初始邀请还校验原文件字节摘要与路径，篡改在接触窗口前拒绝。
- confirm_member要求准确绑定代次的关联问答、业务completed及模型终态证据，并完整分页读取当前上下文。queued/accepted不能放行。B初代与C/D/替换代次通知分别去重；manual提示明确不会自动唤醒。
- Windows旧测试补齐新上下文确认流程；OpenAI用例允许并检查账号指纹RPC，DeepSeek仍不调用账号RPC。这些是夹具按新接口适配，不是取消身份验证或降低生产门禁。一个只测共享树回合串行的用例显式关闭审核门禁以隔离变量，审核门禁本身仍需独立覆盖。

### 首次失败、复测和范围

B首次四模块70项：9 failure、3 error，退出1；对应第12节的JSON、篡改邀请、Windows及OpenAI旧用例。修复后70项通过；增加边界用例80项通过；补C成员通知隔离后81项通过，28.048秒。B另跑Team9项通过。

A独立执行：

```text
D:/Anaconda3/python.exe -m unittest tests.test_approvals tests.test_delegation tests.test_windows tests.test_deepseek tests.test_team -q
```

90项全部通过，29.012秒，退出0，无跳过。A另执行下列剩余模块，不用本次90通过覆盖其失败：

```text
D:/Anaconda3/python.exe -m unittest tests.test_assistance tests.test_multi_account tests.test_long_messages -q
```

45项，13.406秒，退出1：5 failure、1 error。仍失败的是第12节Assistance三项（校准、共享池和统筹移交）、MultiAccount两项（当前会话错误顺序、全局索引目录），以及LongMessage一项原CLI身份环境错误。下一阶段须修复合理的旧策略兼容及夹具，不能删除真实身份防护、忽略共享池低额度或把默认混合消耗样本视为可靠校准。

### 限制及剩余风险

- 缺少invitation_digest的旧开发草稿windows/2邀请会被拒绝，需由A重新生成；没有自动信任旧不完整凭据。额外invite-member邀请按持久化规范化payload验证，不固定JSON空白或复制路径；初始邀请固定原文件字节及路径。
- 本次没有重跑全量272项，也没有真实Hook信任、四可见原窗口或隐藏审核全流程验收。旧失败清单按上述结果逐项更新，不判完整skill可用。
- 未推送、安装、创建Release或标签，未触碰活动协作业务库做故障注入。继续处理剩余6项以及审核/审批门禁，完成前不覆盖安装版。

## 14. 旧交接策略兼容与身份夹具修复、独立验收（2026-10-19）

本阶段B修改4文件，A分页读完交付、确认摘要，并独立阅读实现与测试。所有改动仍在未提交工作树；不能用HEAD代表候选版本，也不能将整个工作树差异归为本阶段。

| 文件 | 固定SHA-256 |
| --- | --- |
| scripts/pair_assistance.py | 057CE3D4B9C16EEF9134C0E782FCCFD5F423724F561D288B01606D75CE747E05 |
| tests/test_assistance.py | 1C1B6AA0EFBF6CF7CF4E15A81EAD39EC9D88F0B20A3ED6EE4EDDAC312F428AE3 |
| tests/test_multi_account.py | 35E90C3C6A4FA9EDB4335C76F00C0BA5D7585439CDF991DDFE91966F287E10B4 |
| tests/test_long_messages.py | B64B30CA38D100DB9799D8559BA21166CBA10A13A76E921E17E1A96A2486AA60 |

### 失败原因、产品修复与夹具修正

B首先复现第13节剩余失败：三模块45项，5 failure、1 error，13.062秒，退出1。原始错误包括 `9 != 14`、`ready != saving`、`A != B`、`Current original CLI required`，以及不允许home中存在全局绑定索引的旧断言。

- 产品问题：旧交接与新模式混用。`consume_checkpoint`现在按`member_context_required`区分；旧策略恢复原统筹随接管转移，新模式保持A统筹、既有report_to及普通问题owner不随执行者转移。两种模式均不移交审批资格，任务接管不代表依赖已完成。新增新旧两个用例在修复前均失败（旧coordinator A!=B、新report_to B!=A），修复后通过。
- 额度夹具：共享池用例原先分别生成重置时间，微秒差导致不再表示同一周期。现统一now；未修改生产共享池保护。校准夹具明确设置临时隔离样本证据，仍断言14%；新增缺少隔离证据的混合消耗不校准用例，仍为9%。不是把混合消耗冒充交接成本。
- 身份夹具：模拟CLI显式设置目标CODEX_THREAD_ID，再检查错误home/cwd；生产validate_actor未修改。新增正确home但错误线程在接触AppServer前拒绝。长消息同样补齐当前线程环境。
- 目录断言：允许home中的全局session-bindings.sqlite3，但递归核对没有项目state.sqlite3，且两home打开同一共享Store.path；不复制或删除认证和状态。

### 实际复测

复跑命令：

```text
D:/Anaconda3/python.exe -m unittest tests.test_assistance tests.test_multi_account tests.test_long_messages -q
D:/Anaconda3/python.exe -m unittest discover -s tests -q
```

B修复后先47项通过，13.444秒；加强问题owner和错误线程边界后48项通过，14.184秒。B报告全量286项，71.465秒，退出0：283通过、3跳过。

A独立复跑三模块48项全部通过，14.140秒，退出0；再独立运行全量286项，71.595秒，退出0：283通过、0失败、0错误、3跳过。跳过对应RealTransport、RealCore和RealIntegration，不能当作通过。执行前检查`Get-ChildItem Env:CODEX_PAIR_REAL*`无输出；没有启用真实模型开关。第12、13节历史本地失败在当前候选已复测通过，原失败证据保留。测试后4文件SHA-256与表中一致；本阶段文档及4文件`git diff --check`退出0。

### 尚未验证的部分与下一步验证方法

- 新审核门禁不因全量回归绿色就视为齐全。A静态检查仍见满两阶段重送审先计数后替换、范围外blocking直接自动返修、稳定问题来源与轮次可能被新根任务绕开、严格优先级可能饥饿，以及原生审批超时只有event、未决状态未完整进入接管安全检查。已登记B5修复与A5独立验收，保持依赖串行。
- B5应在临时SQLite和fake审核控制器中先复现，再覆盖：两阶段上限内安全替换非running版本、在途版本保护；范围外问题通知A且不扩权派发；同一问题跨任务/负责人仍最多两轮；持续高优先级下普通阶段有界等待；两次180秒原生超时持久化去重通知；granted/unknown先核实而非重放；固定版本送审、返修、复审到依赖放行的正常链路及旧epoch迟到拒绝。A5阅读具体文件版本并独立复跑；mock结果单独标注。
- 真实四可见原窗口、中途加入、受管隐藏审核端到端、真实Hook首次信任、实际账号切换、额度耗尽/恢复、发布与安装均未验收。仍按前文各模块步骤，由其他测试者在隔离项目执行并保留原始回执与受审摘要；不在活动协作状态库注入故障。
- 未打包、安装、推送、打标签或创建Release。局部兼容性已通过不等于完整需求已验收。

## 15. B5审核返修及原生审批门禁的独立核验（2026-10-20）

A恢复原窗口并完整消费自己的检查点后，分页读完B5交付，确认5文件版本。本阶段仅独立审查、测试及记录；未重新实现B5，不使用候选程序给活动协作自我放行。A/B来源为原已核实的OpenAI官方gpt-6.1-sol；以下独立人工代理核验不能冒充产品隐藏审核器端到端验收。

| 文件 | 固定SHA-256 |
| --- | --- |
| scripts/pair_managed.py | 1725EF7220700E91EACD65CFE7552E40BAAE4F715FB4E5D0BE247F0800D430D9 |
| scripts/pair_review_runtime.py | AB5D66821C13E33142A0FE85CF3CEE7DB51DDC7C171AE7E02E94E231807EED8B |
| scripts/pair_stages.py | 107126CEC476DB76EBD565F4604C47BF91835405C1BB54463D74DC699B373489 |
| tests/test_stages.py | 9DC88BD42AD9F4935453007DBCB21ACF5865B56A9C18EF734E9B9A6D9472E8B1 |
| tests/test_managed_review.py | AA7BF74E1F36BA4307D174D9E691E8B91E24F8AA7ABE3CCC063A98004AC12527 |

### 首次失败与修复依据（B记录，A检查实现与用例）

- Stage初轮9项：3 failure、1 error，退出1。原始包含`Review backlog full`、running版本可覆盖、范围外生成返修以及新task/id重建问题。容量计算现在排除本任务非running旧阶段，在同一事务中替换；running拒绝替换，准确摘要重复提交不新建版本。
- 阻塞问题增加规范化path和明确reproduction锚点、所需paths及原任务/当前任务双范围核对。超范围、缺锚点、改标签而无法确认新问题时持久化needs_coordination与给A的blocked消息，不自动扩大业务范围。
- 问题主键不含任务名、负责人或模型临时id；追加来源记录，继承repair_links，不重置两轮计数。无法自动判断跨位置改写是否同一语义bug，必须由A依据复现协调；这不是通用语义判重。保守策略也可能把同文件的不同问题交A确认，而不是自动返修。
- 审核选择计数持久化；返修及阻塞依赖优先，等待3次已启动审核后的最早阶段获得机会。计数依据begin_review，不按轮询次数；有限队列按等待顺序避免被后来高优先级无限挤压，不承诺有更早等待者时每项严格3次内开始。
- Managed初轮4项有3 failure，退出1：未决未纳入保护、下一审核未阻断、第二次超时没有blocked。新增统一超时处理及持久化去重通知；180秒和显式重试要求不变。授权撤销、拒绝不默认放行。
- managed pending/allowed/granted/unknown加入现有未决检查；已批准结果不明阻止接管/恢复和新审核，业务结论降为needs_validation。只有准确epoch/execution/turn/item工具回执才结算；缺证据保持unknown，不重放。控制器和显式对账共用结算函数。
- 17项扩展正常链路初轮1 error为临时项目未初始化Git，`GitError`来自既有prepare_pair_directory校验；补临时seed后通过，没有取消生产Git检查。后续扩展到78项。B全量303项在最终提示词增量之前运行（300通过、3跳过，76.685秒）；提示词增量后78项通过（6.127秒）。不把这两次运行冒充同一精确版本的全量。

### A独立执行及证据边界

```text
D:/Anaconda3/python.exe -m unittest tests.test_managed_review tests.test_stages tests.test_reviewer tests.test_approvals tests.test_delegation -q
D:/Anaconda3/python.exe -m unittest discover -s tests -q
```

A独立专项78项全部通过，7.176秒，退出0；最终精确版本全量303项，110.249秒，退出0：300通过、0失败、0错误、3跳过。三项为显式开关控制的真实集成测试，不当作通过。测试前后5文件SHA-256与表中一致；适用`git diff --check`退出0。真实测试开关检查无输出，不启用真实模型。本轮未新增产品修改，B5所列局部门禁在此固定候选的本地验收通过；此结论不扩大到尚未执行的真实端到端范围。

正常链路测试实际调用Store送审、问题登记、返修与结论接口，但工具回执是fake，部分任务/整合终态在临时SQLite中显式设置；因此只证明这些协议门禁组合，不证明真实Git整合或实际控制器执行模型的完整链路。旧epoch、缺回执、错误turn以及准确迟到回执分别有边界用例。

### 仍需验证与后续变更

- 真实隐藏审核者创建、官方工具回执结构与权限适配、Hook首次信任、完整四原窗口、真实额度和身份变化仍未测试。需在隔离项目按第2、3、5节步骤保留会话身份、固定候选摘要、实际RPC/工具回执与版本审核记录，不能用上述mock代替。
- 包完整性与隔离试用候选尚未产出；当前未安装、推送、打标签或创建Release。
- 用户新增检查点命名已另登记B6与A6，依赖本阶段后执行。旧版已按账号目录加会话UUID隔离，并非每账号一份；最终要求为每窗口最多一份，A/B/C/D分别`交接文档1.md`、`交接文档2.md`、`交接文档3.md`、`交接文档4.md`。须隔离协作与替换代次并兼容未消费旧摘要文档，不覆盖既有记录。本阶段尚未实现该命名，不把登记任务说成修改完成。
- B6/A6完成后，B7/A7另行构建并核对项目忽略目录内的本地候选包、逐文件摘要及隔离解压检查；不覆盖安装，不上传。包完整性与真实业务验收分别记录。

## 16. 每窗口交接文档命名、隔离和旧记录兼容（2026-10-20）

用户最终要求A/B/C/D对应`交接文档1.md`、`交接文档2.md`、`交接文档3.md`、`交接文档4.md`，不是最初任务正文中的`1.md`等简名。B6按后续明确要求修改3文件；A6完整读取交付并独立审查。旧实现本来就以账号目录和会话UUID摘要隔离，并非每账号只能一份，此次新增可识别命名与协作、代次隔离。

| 文件 | 固定SHA-256 |
| --- | --- |
| scripts/pair_assistance.py | D49BDC5F1BCEAF6A6B958A19B406579A5983181137B7AD2C718DF1CA5CA86AB9 |
| tests/test_assistance.py | B16932DA8B8494BB7BDC09497843CF30404B9874467C3C5FC11CAE6EC2F4ECAE |
| references/assistance.md | F527515CE5B5DF396CDCD38AE44832DF7996C073FF543938AE2F170F11A2A903 |

### 实现与首次失败

- 新路径为`.codex-pair/checkpoints/<协作ID>/<绑定摘要>/交接文档N.md`；摘要包含canonical账号目录、准确会话UUID及binding_version。同账号不同会话、不同轮次和替换代次分目录；同一绑定消费清理后再保存复用同一正式路径。隐藏审核者保留自身reviewer摘要文件，不占可见编号。
- 未消费旧行或清理失败文件优先沿用数据库登记路径，不覆盖、不搬迁。旧摘要孤儿仅检查其准确固定路径，不扫描历史；显式recover校验manifest项目、轮次、成员、会话及可用代次。消费清理后下一次保存才用新名。登记路径同样验证项目checkpoints子树及无重定向。
- 新文件原子落盘但事务中断、仍存在旧consumed行时，检查会报告`Unregistered checkpoint`；显式恢复核实新manifest，不把残留当作已消费历史而忽略。
- B首次5项新增边界：3 failure、1 error，退出1。其中旧路径缺协作/代次隔离和登记越界是产品问题；C/D夹具直接join触发`Invitation required`、字符串路径大小写比较是夹具问题，改为邀请accept和Path比较，没有放宽生产验证。
- B首次完整Assistance 34项有2 failure，退出1：旧glob只看根目录，不适配新嵌套布局。改rglob且核对无文件残留/仅一份md，保留原子失败和单文档约束，不为了过测试删除空目录。之后补旧代次、重定向、异轮孤儿、第二次同路径保存中断及隐藏审核独立命名用例，Assistance共39项。

### 实际复测

```text
D:/Anaconda3/python.exe -m unittest tests.test_assistance tests.test_delegation tests.test_managed_review tests.test_reviewer -q
D:/Anaconda3/python.exe -m unittest discover -s tests -q
```

B报告专项92项通过，8.397秒；全量313项，74.067秒，退出0，310通过、3跳过。

A独立专项92项全部通过，8.445秒，退出0；最终精确版本全量313项，74.210秒，退出0：310通过、0失败、0错误、3跳过。测试前后3文件SHA-256与表中一致，适用`git diff --check`退出0。本阶段未新增产品代码修改，所列命名、隔离及兼容边界本地验收通过。真实开关检查无输出，三项显式真实集成跳过不当作通过，未启动真实模型、安装或迁移活动协作库。

### 测试者仍需验证

- 同账号四个真实原窗口保存/恢复，以及显式替换后的真实身份路由；当前C/D用临时SQLite邀请与fake证明，不冒充原CLI验收。
- Windows真实junction/symlink环境：当前新增重定向测试通过模拟Path.resolve验证拒绝分支，没有创建真实重解析点。应在可丢弃目录构造指向外部测试文件的链接，确认保存/读取/清理拒绝且外部文件未变，不在用户目录构造。
- 跨轮次路径单元测试部分通过切换pair_id验证命名，foreign-pair旧孤儿恢复实际走manifest拒绝；真实双轮协作竞争及极端中断仍需隔离环境验收。
- 旧文档分页、读取摘要、消费清理和重复保存中断使用真实临时文件通过；未消费文件和旧绑定文件保留。空隔离目录允许存在，不算多份正式文档。
- 最终安装包尚未构建；B7/A7待本阶段完成后做本地包及隔离解压验证。没有推送、安装、标签或Release。

## 17. 本地候选包构建与A7独立完整性核验（2026-10-20）

本地候选已经构建并通过本节范围检查，未安装、未发布，不表示完整真实业务验收通过。B7没有修改源码/测试，A7仅补本记录及CHANGELOG；前置A6的313项结果不冒充本轮重跑。

产物位于项目忽略目录：

```text
.codex-pair/archives/local-candidate-20261020-f3320622bf/
  candidate/codex-pair-local-candidate.zip
  candidate/SHA256SUMS.txt
  input/codex-pair/
  INPUT-SHA256.json
  PACKAGE-VERIFICATION.json
```

| 固定产物 | SHA-256 |
| --- | --- |
| codex-pair-local-candidate.zip | a1a393fcc3fe26c60f330f81dfb3abe6a09daec8b9cdcd4db959ad30ff42227e |
| INPUT-SHA256.json | 08b16d665da215f0f16bb269f77c47423ffcfac2b17b9a9f148d08fc2f42edf1 |
| PACKAGE-VERIFICATION.json | 62f84d88e75cd845bce57a1e2fd62c987c8852ab552eb01774aad129c4c08aec |

### B构建及首次失败

复用scripts/package_skill.py，由固定41文件输入构建唯一目录内的包，不覆盖旧包。B检查单层codex-pair/、名单/逐文件摘要/CRC、隔离解压及重构建一致，并在隔离副本加入合成认证配置、状态库、会话日志、缓存，核验它们被排除。敏感文本模式无命中不等于穷尽式隐私审计。

首次验证包装器退出1：`UnicodeDecodeError: 'utf-8' codec can't decode byte 0xb9 in position 66: invalid start byte`，继而`TypeError: data must be str, not NoneType`。原因是Windows子进程本地编码和包装器UTF-8不一致；保留原ZIP不覆盖，只给验证子进程显式设置PYTHONIOENCODING=utf8及PYTHONUTF8=1，复验通过，未改产品代码或当前窗口身份。

### A7独立检查

- 实际读取ZIP、校验清单、固定输入和逐文件manifest；41个唯一成员均位于单层codex-pair/，没有绝对/穿越路径。逐项核对ZIP字节、冻结副本及检查时当前源码相同，包含全部19个运行模块与所需references。
- 实际执行ZIP CRC检查；按名单与路径规则核对没有认证/config/hooks、.codex-pair状态、sessions、SQLite、日志、测试、pyc/cache或Git目录。本次只核对本地包，不向外上传任何内容。
- 在自动清理的临时目录解压，设置上述UTF-8环境和PYTHONDONTWRITEBYTECODE=1，分别运行`D:/Anaconda3/python.exe scripts/pair.py --help`、`scripts/package_skill.py --help`，以及通过pkgutil/importlib导入全部19个运行模块；3个子进程均退出0且stderr为空。运行后逐项摘要仍相同，没有调用安装命令或模型。
- 独立运行`D:/Anaconda3/python.exe -m unittest tests.test_package.PackageTests.test_repeat_build_is_reproducible_and_existing_files_are_preserved -v`：1项通过，0.089秒，退出0。未运行另一个会调用安装的PackageTests用例；这里的“不安装”指本阶段未执行安装，本地历史全量的临时安装测试与真实配置安装必须区分。
- `git check-ignore`确认候选归档已排除；适用`git diff --check`退出0。未暂存、提交、推送、创建标签或Release。

### 交给其他测试者的方法与限制

1. 先用Get-FileHash核对ZIP与上表及SHA256SUMS.txt，再以Python zipfile.testzip检查CRC，比较INPUT-SHA256.json完整名单与每个ZIP成员字节SHA-256。不要只验证ZIP能打开。
2. 在新临时目录安全解压，不覆盖当前安装；使用明确Python路径，子进程指定UTF-8。先做上面的CLI帮助与导入，再按前文各节开展隔离业务测试。
3. 包中的validation-handoff-four-cli.md冻结在第16节，本节为打包后的外部验收记录，因此没有重打包把自身记录循环写入固定候选；SKILL中的“未验证”提示仍保守，实际本地检查结果以第11～17节为准。
4. 真实四原窗口、隐藏审核创建/恢复及官方原生RPC权限、Hook首次信任、真实额度与身份变化、故障矩阵、真实Git整合发布、用户配置安装仍未全部验收。需要真实窗口和可丢弃项目完成对应步骤后才能扩大结论。当前交付定位为“已通过所列本地检查的候选”，不是正式发布或完整实测可用声明。

## 19. 三项P1受管审核门禁修复（2026-10-20）

### 四个配置目录统一测试候选安装

实际执行：上述四目录安装器均返回verified=true；64个包文件重新计算清单全部与源码相同，各目录pair.py --help均退出0。旧版备份分别为各自codex-pair-skill-backups下：.codex的codex-pair-e6a211e1628e4471bd5db9a65b75ae6f、.codex-b的codex-pair-aa21c609ad6c431a9d8f92b5fc011270、.codex-c的codex-pair-a3efe87234e74fe5b268107d700a889e、.codex-deepseek的codex-pair-32465b4055f647198b1ee550f6028b53。本段完成记录随后同样追加到源码与四处文档，未改其他文件。switch不在安装列表，未执行其安装命令。

用户明确要求将目前最多支持四可见窗口的skill安装到各配置目录供测试，随后明确排除switch。目标限定D:/Codex/.codex、.codex-b、.codex-c、.codex-deepseek各自skills/codex-pair；.codex-cc-switch不安装、不改动。安装使用当前开发源码和项目install_skill.py逐目录备份、暂存、全清单校验、原子替换；各目录只复制skill，不改auth/config/Hook/模型。最新代码测试依据为321项（318通过、3跳过）和B专项独立复核；本次另做安装清单一致及各入口帮助检查，安装结果以实际工具回执为准。运行中协作仍使用冻结r3，不因安装而热切换；真实四窗口及隐藏审核验收不能由安装替代。不推送、不打标签、不创建Release。

### 用户授权安装到新成员C

用户优先要求给C安装最新skill。正式成员状态显示C配置目录`D:/Codex/.codex`、原会话`01a1443f-ac3d-7f82-8828-c955ad246410`，实际官方OpenAI/gpt-6.1-sol，未按标题或目录排序猜测。使用项目install_skill.py将当前开发副本安装至该目录skills/codex-pair，返回verified=true，旧版备份`D:/Codex/.codex/codex-pair-skill-backups/codex-pair-bbe5a58a93cb4aefa7489c07af8237bc`。独立重新计算源码与安装副本64文件清单完全一致，CLI --help退出0；随后本安装记录同步到开发、A/C安装文档，不改代码。未改C账号、模型、权限、Hook和运行中r3冻结副本。此前321项本地结果及B独立复核属于开发版本验证，不代表C已完成握手/上下文或隐藏审核实测。最新安装入口下回合可用，本轮协议测试仍明确使用r3路径，后续安全切换须单独登记。不推送、Release或安装其他账号目录。

### B原窗口检查点CRLF误判修复

B只读复核回报`2a8deb7d-bcc8-4246-90bd-7a15b6abd343`：未发现阻塞缺陷，独立运行84项，8.098秒，退出0；审查前后摘要相同。A核对源码摘要一致：pair_assistance.py=`933FF8FE94659BE7BF2DFCBE8023EE6DEA87A2DA044082EC56DD693E217EDE47`，test_assistance.py=`CE40A37984D11684793BD01E8AE56005AE93418BEC649C4019007172E18D1E3F`。六处读取保留原始换行，原摘要和变更文件保护保留；本回报不代表原B交接已恢复，仍须新固定副本安全切换后实测。原B文件、数据库及r3保持不变。

B报告`42cfe8c2-15ab-4616-bed3-b9707638fd1b`：原文件字节SHA256=`ad6eb605262bbeb77ab358758311c16fb33aa238551f5c718ea4a067a3eaacaf`与登记一致，read_text换行转换后SHA=`ea5ed3a8b5643d9213917efe8fea04a6c9dcea2629b90fcb1398f069fcf8fd7b`，含1个CRLF。未改原文件/库或绕过校验。根因保存正文保留传入CRLF，读取却使用通用换行转换。A新增mixed-newlines测试先复现`Checkpoint changed`，随后将check/read/consume/cleanup/recover全部检查点读取改为read_bytes().decode('utf-8')，保留精确UTF-8内容和原摘要；不迁移、不重算活动登记摘要，真实文件变更仍拒绝。混合CRLF/LF/CR用例验证保存摘要、完整分页、字节未变化及消费删除。

专项命令`D:/Anaconda3/python.exe -m unittest tests.test_assistance tests.test_handoff_balance tests.test_managed_review -q`：84项通过，7.785秒，退出0。全量`D:/Anaconda3/python.exe -m unittest discover -s tests -q`：321项，318通过、3跳过，76.223秒，退出0。diff --check退出0。独立复核及新固定候选原B恢复尚待完成；当前r3不热修改，B原交接保留。按用户既有授权同步A当前安装skill，备份旧版，不改其他home。五项仍在第1项A独立验收之前，此修复为解除恢复阻塞，不把第2项提前判通过。

### 握手后自动接续缺口修复

真实接续复测已安全切换r3：归档`two-cli-fixed-r3/package/codex-pair.zip`，SHA256=`9b942ac6659fdd904321d81bbb0ebfe5a3655e66ca9267bea6ba4de2ea5f4d2b`；先核对旧消息四条均业务/模型completed、无任务/审核工具，再停桥备份state-before-switch.sqlite3，经显式resume启动r3桥pid7948。r2保持不变。新桥已生成A/B各一条member-context通知，当前仅证明通知生成，实际读取确认和后续小任务等待原窗口执行，不记整体通过。

B只读回报`2011ebd2-ae18-44f1-a6b7-c2b941a3d111`：62项独立复测（61通过、1跳过），29.376秒，退出0；另4个临时SQLite探针验证停止、旧A/B绑定均不唤醒，正常重复tick各一条通知且verified仍0。A核对摘要一致：pair_team.py=`A28939427A3BB26F59FDB72DB5B2AAE7D18F65738EAE31C35DDA2C8491870069`，pair_core.py=`18FD5E7379F43C583F562BD97014477A221E0921C9FA7E17F09582DE36F1D44B`，test_team.py=`5355B2601A6B0DC1DCFA87E00931070AD1282D135ADECF75484944EAACE26EC0`。额外探针由B执行，未持久化回归用例；不是隐藏审核验收。非阻塞已知项：B最后确认可能同时产生旧window-ready和新members-ready各一条协调通知，分别去重，尚未合并；执行者应核对当前已登记任务，不能因提醒重复派业务。下一步新冻结副本真实接续验证，不改r2。

真实r2状态显示握手问题`8c32ceb4-5636-42ba-82e8-da85f6a25afa`与回复`515f26d7-105b-4cfb-ae3d-96a7cb44857d`均业务completed、模型completed，但A/B均verified=0、任务为空、waiting为空。新成员握手使用`window-handshake:B:1`，旧complete接续仅匹配`window-handshake`；新confirm_member又要等成员被唤醒读上下文，只有非A确认时才通知ready，形成没有消息驱动的空等。A未及时继续确认也是流程执行遗漏，不能全部归咎传输。

修复增加本地调度tick：仅匹配准确双方绑定代次及握手业务/模型终态后，为尚未确认、active且在线成员发送含当前上下文摘要的去重note，要求实际read-member-context全分页和confirm-member；不直接修改verified。全部确认后补发去重统筹接续通知，覆盖A最后确认的顺序。明确停止、目标完成、旧绑定或未结束回合不得据此唤醒。首次新增测试复现通知列表为空；修复后测试确认两条通知仅各一条，B先确认不放行，A最后确认仅一条ready通知。

运行`D:/Anaconda3/python.exe -m unittest tests.test_team tests.test_windows tests.test_core -q`：62项，61通过、1跳过，28.286秒，退出0；追加A最后确认断言后单项通过0.081秒。全量`D:/Anaconda3/python.exe -m unittest discover -s tests -q`：320项，317通过、3跳过，75.571秒，退出0。diff --check退出0。B独立复核请求已入队`01a15732-a294-7713-a559-9157a2e9b250`，未收到实际报告；原窗口新候选复测待补，不声称当前运行r2已自动修好；不热改冻结包和数据库，不推送。适用本地检查通过后按用户既有授权同步A当前配置目录skill，安装器保留旧版备份。

### 用户要求五项测试按序推进：第一项启动

新固定候选`.codex-pair/archives/two-cli-fixed-r2/package/codex-pair.zip`，SHA256=`f91d8db5d0528859771602ec57d79600e0f4754a7e5b9bf92f62bb2f3c2a23c6`，独立解压到同归档runtime/codex-pair。检查旧协作无任务、两条交接消息业务和模型回合均completed、无审核会话，停止旧桥后备份state.sqlite3到该归档state-before-switch.sqlite3。首次直接启动新桥得到`Explicit resume required`，按正式resume后启动新桥pid16416，未绕过明确停止保护；旧冻结输入和数据库历史保留。原A重新核验返回成功、需上下文确认；新B邀请`invite-eb615c34cfa64fdc928a57e5b06827ee.json`已生成并通过官方queue发给已确认原B。当前仅完成安全切换和邀请送入队列，B加入、双向握手、小任务仍待实际执行，不记为第一项完成。其后第二项B交接、第三项强制刷新加强测试、第四项P1加强验证、第五项消息恢复按依赖继续，不跳步宣称通过。

### B独立复核回报

收到原B只读回报`d29f55ae-ad43-4850-a7c3-88160454ebfc`：未发现阻塞缺陷；独立复跑47项，3.662秒，退出0。B另报告使用临时SQLite夹具执行8个内存探针：pending/allowed失效，granted/unknown保留；lifecycle、verified、online及quota_state失效均不能消费批准。8个探针通过，未将内存脚本存成持久回归用例；A未重跑这些额外探针，不冒称A执行。

A收到后实际核对三个摘要与B报告相同：pair_managed.py=`13068A5F6E8DDD74255BDCF1E2EE51D5BCED33551067C820231CA6E7AD2233AF`；pair_review_runtime.py=`D9C43444B4C6E974BB012B29D6C079BF6CBCC84BA160644CB8DC6CB063EC339F`；test_managed_review.py=`ED27BB0968EF2FF62E751801FC6E086FEA28341CDA296B5D99F02D2334F74171`。此回报结束本次固定文件的原B独立静态/本地复核，不是隐藏审核业务放行记录，不覆盖真实RPC或双原窗口完整流程。当前安装同步保留旧版备份`D:/Codex/.codex-c/codex-pair-skill-backups/codex-pair-6b6c3d7528734ac8ad3d524a0cdf746a`，两个产品模块安装后摘要与源码一致。

用户提供三项隔离复现后，A修改开发副本，未修改运行中冻结候选和状态库。

1. **身份失效后消费旧批准**：原`_invalidate_approvals`遗漏managed_permissions，且`_member_suspended`仅检查额度状态。ManagedApprovalMixin现在覆写统一失效入口，过期pending/allowed并取消重试；granted/unknown保留待核实，绝不记作未执行。capture、review及消费前均检查原审批者active、verified、online及额度可用。新增两项测试分别直接暂停A后消费、以及走Account changed统一失效路径。首次均复现旧allowed未失效，修复后通过。
2. **在核实旧执行前切换审核树**：`_session`把starting/running/unknown检查移到prepare_review_tree和AppServer创建之前。新增三状态子用例断言两项副作用接口均未调用。首次测试夹具缺codex_home报KeyError；补齐夹具后重新运行，三状态均复现prepare_review_tree已经被调用，再实施修复后通过。这是调用顺序隔离测试，不冒称本轮实际运行旧审核工具。
3. **必要检查回执缺cwd绑定**：`_report`要求每条必要检查的实际commandExecution回执cwd为非空绝对路径，解析真实路径后与固定state.cwd相同；无关目录、缺失、空、不可解析路径均needs_validation，命令/状态/退出码检查保留。新增正确cwd可通过及错误/缺失cwd不能通过测试，首次错误cwd仍passed；第一版修复遇不存在目录FileNotFoundError后补上拒绝处理，不能因路径错误让控制器异常退出。限制：不解析命令内嵌cd或证明子进程最终访问范围，真实工具回执和真实隔离审核仍须验收。

适用专项：`D:/Anaconda3/python.exe -m unittest tests.test_managed_review tests.test_quota_guard tests.test_reviewer -q`，47项通过，3.600秒，退出0。全量`D:/Anaconda3/python.exe -m unittest discover -s tests -q`：319项，316通过、3跳过，74.368秒，退出0。diff --check退出0，仅既有换行警告。B原窗口只读复核请求已入队`01a1571d-e83a-7620-8d5a-db84d08c66f2`，尚待实际报告；本段不代表已通过真实隐藏审核。按用户此前明确授权，将通过适用本地检查的开发副本同步至A当前配置目录，保留安装器备份，不热替换固定候选。未推送、Release。

复测方法：运行上述专项；检查managed granted/unknown记录不被失效入口清除；在真实审核工具在途时触发桥恢复，核对审核树HEAD与文件摘要不变且要求reconciliation；在另一个隔离仓库运行同名命令，将其真实cwd回执交门禁，必须needs_validation；然后在固定审核树运行同命令，正常回执应满足目录条件。完整业务放行仍须其余版本、回合与证据门禁全部满足。

## 18. 真实双窗口试验启动、A交接恢复与账号通知误暂停（2026-10-20）

### 用户授权更新A当前配置目录

用户随后明确要求修复同步到A自己使用的skill。使用项目`install_skill.py`仅更新`D:/Codex/.codex-c/skills/codex-pair`，安装器完成暂存、全清单哈希校验与原子替换，返回verified=true；旧版保留在`D:/Codex/.codex-c/codex-pair-skill-backups/codex-pair-d825bf5d931a4a1c9898c45d1028244e`，可恢复。本项覆盖此前“不安装”的范围仅限此次明确授权的当前目录；未更新其他配置、登录、模型、权限或Hook，未推送或Release。安装后`pair.py --help`退出0，pair_quota.py摘要与已独立复核修复一致。这里安装的是开发候选，不代表真实四窗口验收已完成。当前隔离桥仍使用项目内旧冻结包，未热替换；后续测试必须明确选择新版本并安全切换。后续每项修复继续登记原因、影响、首次失败、实际复测及未验证范围；更新当前安装前核对相同版本和运行安全，不覆盖固定测试包。

### 后续本地全量复测与两原窗口复核安排

B原窗口只读回报`0718942f-c150-4c59-90dc-8df95253fcb7`已收到：检查通知→强制刷新→validate、实际身份不匹配暂停与审批撤销、退役过滤及旧绑定结果保护，未发现阻塞缺陷；独立执行相同专项42项，3.156秒，退出0。B报告检查前后两个文件摘要与下列固定摘要一致；A收到后再次核对一致。本回报为原B的独立只读审查，不是隐藏审核门禁记录，也不证明真实双窗口业务完成。

B指出非阻塞覆盖缺口：`test_account_notification_forces_validation_with_fresh_snapshot`只跑monitor首轮，due为空，即使删除通知的force也可能通过。后续应在第二轮、未到due时注入account/updated，断言再次调用validate，并用删除force的变异试验确认测试会失败。此建议尚未实施，不能记为已覆盖。

用户要求优先完成两窗口能做的测试后，A运行`D:/Anaconda3/python.exe -m unittest discover -s tests -q`：315项，312通过、3跳过，75.245秒，退出0。`git diff --check`退出0，仅提示既有三文件LF/CRLF转换警告。未启用真实模型测试开关，不能把此全量结果算作真实双窗口或隐藏审核通过。

送B原窗口独立只读复核的文件摘要：`scripts/pair_quota.py` SHA256=`20F5CFCEF353459DBEF28D3AE3919DF7BA81CD6567607EE43174BAA0C4A55115`；`tests/test_quota_guard.py` SHA256=`C176FFC6108C32099D6E06E449A9661A54F767337D5B94384E6D2AF57D2D45AC`。官方queue回执`01a15714-b36f-7482-913d-16095334d4d0`仅说明送入原B队列，尚未收到B复核报告，不记为独立通过。旧固定候选的误暂停仍存在；先等独立结论，再构建和核对新固定候选，安全停旧桥后再恢复真实邀请/握手测试，不热替换运行程序或直接修改数据库解除保护。

- 隔离项目为`.codex-pair/archives/real-two-cli-20261020-r1`，协作为`windows-0815474c0604`，基准`bcb8a650229939ab2276bc3e9d737a34824cc0c9`。使用第17节固定候选，不覆盖安装。审批委托及隐藏审核自动启动已关闭，未安装Hook、未创建审核模型。B原窗口邀请仅取得queue回执，尚无加入和双向握手通过证据，小任务尚未派发。
- A在真实剩余额度6%、保留线6%时收到checkpoint通知，保存`交接文档1.md`。额度刷新至69%后桥自动发出恢复通知；A分三页读至null，核对隔离项目干净、无业务任务，持久化恢复计划，consume返回`consumed=true, cleanup=deleted`，恢复通知完成。这里只验证A的保存、读取和清理，不代表B或四窗口交接通过。检查点摘要为`d238fe45ecc734ec5905d22df4660e5090da1cfe1a213e4a780d8282aa527fb3`。
- 真实阻塞：监测收到`account/updated`便无条件暂停A；官方身份重新核验成功后又被同类通知暂停。代码未比较实际身份就把更新通知当成账号变更。另有中文PowerShell管道输入报`UnicodeEncodeError: surrogates not allowed`，ASCII核验依据成功；尚未修复该输入编码边界，后续应使用UTF-8文件或显式编码复测。
- 开发副本修复：通知仍强制刷新，但仅由`validate`的准确账号/模型/提供方不匹配错误触发身份暂停和审批失效；网络错误保留已有额度快照，不把通知本身当作身份变化证据。真实账号变化保护及旧绑定代次保护不删除。固定包及运行中候选未修改，因此当前真实协作仍含旧缺陷，不能宣称已在原窗口修好。
- 首次修改回归断言后，单独运行`tests.test_quota_guard.QuotaGuardTests.test_account_notification_keeps_retired_binding_retired`复现1项失败：`AssertionError: 'suspended' != 'active'`。最小产品修复后40项通过；补充通知后真实不匹配仍暂停、fresh额度收到通知仍重新校验两项用例后，运行`D:/Anaconda3/python.exe -m unittest tests.test_quota_guard tests.test_handoff_balance -q`，42项通过，2.741秒，退出0。
- 下一位测试者：先复跑上述42项；再用模拟客户端分别发送初始化/重复account通知，确认验证被调用且同身份不暂停；制造准确Account changed错误，确认同home非退役成员暂停、pending和未消费allow失效。构建新的固定候选后，安全停止旧桥，保留库与检查点，按受支持方式开展原窗口复测；禁止直接替换运行中的固定包或手改数据库解除suspended。还须完成B加入、上下文确认、真实小任务交付与A复核、B的`交接文档2.md`恢复。本轮未做全量回归、独立B复核、真实账号切换、隐藏审核、安装或GitHub发布。
