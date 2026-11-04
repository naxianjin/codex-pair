# 下一版需求与验收索引

此表保留指南第21章182个编号（原163项＋新增19项）和第06章30项调度规则。编号完整仅证明需求未遗漏，不证明实现或测试已覆盖全部条件。

当前为静态候选。行为测试、真实CLI、隔离执行、迁移、安装及发行均未运行。接入点表示核查位置，不是通过证据；现状和缺口见[下一版状态](next-version-status.md)。

| 编号 | 原要求 | 接入点 | 本轮结果 |
| --- | --- | --- | --- |
| DIAG01 | 成员待审任务满，但无待审阶段 ； 准确报告任务计数阻塞 | `scripts/pair_diagnostics.py` | 静态候选待逐项核验；未判通过 |
| DIAG02 | 阶段满但审核者空闲 ； 报告准入限制，不误称审核者忙 | `scripts/pair_diagnostics.py` | 静态候选待逐项核验；未判通过 |
| DIAG03 | 显式恢复依赖形成环 ； 返回稳定循环指纹和证据边 | `scripts/pair_diagnostics.py` | 静态候选待逐项核验；未判通过 |
| DIAG04 | 名称相似但无依赖证据 ； 不自动认定恢复关系 | `scripts/pair_diagnostics.py` | 静态候选待逐项核验；未判通过 |
| DIAG05 | 正常前置任务仍可运行 ； 优先普通调度，不启用例外 | `scripts/pair_diagnostics.py` | 静态候选待逐项核验；未判通过 |
| AUTH01 | 无用户许可 ； 恢复开始被拒绝 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| AUTH02 | 许可仅含审查，尝试安装 ； 拒绝范围扩张 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| AUTH03 | 作者（包括参与实现或实质修改的A）尝试阶段或最终整合自审 ； 拒绝，并说明贡献冲突；由合格独立审核者审查 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| AUTH04 | 原会话或binding变化 ； 拒绝旧许可 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| AUTH05 | 授权撤销后才开始 ； 拒绝开始，不删除旧记录 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| AUTH06 | 许可过期但操作已开始 ； 保留在途状态，不强行重放或伪造撤销 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| CLAIM01 | 两窗口同时领取同一恢复 ； 只允许一个成功 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| CLAIM02 | 同会话重复领取 ； 返回false或规定的幂等结果，不重复执行 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| CLAIM03 | 领取回执丢失 ； 原会话按恢复协议取回，不换令牌 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| DATA01 | 清单文件有一字节变化 ； 摘要不匹配，拒绝开始 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| DATA02 | 路径经junction指向范围外 ； 拒绝访问或写入 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| DATA03 | 漏读交付中间页直接ack ； 拒绝确认 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| DATA04 | 许可只授权一个交付 ； 其他交付仍访问拒绝 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| EXEC01 | 审查运行时第二恢复请求 ； 保持排队或明确拒绝，不抢租约 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| EXEC02 | 工具结果unknown ； 不允许重复执行 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| EXEC03 | 回执仅有chunk_id ； 不冒充正式工具项ID | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| EXEC04 | 官方回合与raw记录冲突 ； 拒绝正式通过 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| EXEC05 | 报告有commentary和final ； 仅按既有已审规则选择终态报告 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| EXEC06 | 报告解析失败 ； 保存证据，不重跑工具来掩盖错误 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| CONTRACT01 | 裸Python与PowerShell包装不同 ； 旧契约不匹配，不放宽规则 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| CONTRACT02 | 单双反斜杠运输和展示差异 ； 分层处理，实际argv保持正确 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| CONTRACT03 | 命令相同但cwd不同 ； 不认定同一检查 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| CONTRACT04 | 空契约或重复名称 ； 修订被拒绝 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| CONTRACT05 | 删除原必要检查 ； 修订被拒绝 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| CONTRACT06 | 同名检查被改成无意义命令 ； 审查发现语义弱化，不能只靠结构校验通过 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| CONTRACT07 | 两个相同修订并发 ； 仅递增一次版本 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| CONTRACT08 | 两个不同修订并发 ； 一个成功，另一个版本冲突 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| CONTRACT09 | 修订与审核结算竞态 ； 不形成旧新版本混合通过 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| HISTORY01 | 恢复复核通过 ； 旧阶段仍保持原历史结论 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| HISTORY02 | 新阶段替代旧阶段 ； 旧材料保留，替代关系可追踪 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| HISTORY03 | 同问题换任务名 ； 不清零累计修复轮次 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| FLOW01 | complete后桥立即崩溃 ； outbox恢复下一动作，不丢失计划 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| FLOW02 | 同一事件投递两次 ； 不重复创建或执行任务 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| FLOW03 | 窗口在线但绑定不匹配 ； 不投递业务 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| FLOW04 | 多次用户说继续但条件不变 ； 输出具体诊断，不重复登记 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| FLOW05 | 无可执行动作且需要授权 ； 单次提出明确决定 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| MIG01 | 迁移前存在unknown执行 ； 阻止不安全升级或保持隔离对账 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| MIG02 | 新状态已发生外部动作后回滚 ； 不恢复旧库抹掉执行事实 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| LIVE01 | 新协作原四窗口恢复审查完整链 ； 保存真实身份、领取、审批、工具和结果证据 | `scripts/pair_recovery.py` | 环境准备与动态验证暂缓 |
| LIVE02 | 原生单次审批过期 ； 不重放旧许可，按现有重试规则处理 | `scripts/pair_recovery.py` | 环境准备与动态验证暂缓 |
| LIVE03 | 审查通过后重新送审原业务 ； 新契约和新回执匹配，旧结论不冒用 | `scripts/pair_recovery.py` | 环境准备与动态验证暂缓 |
| REC01 | 第13.1节策略的生效、越界及结束失效，结合AUTH01/02/05。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| REC02 | 单活动恢复、公平排队及无关任务继续，结合EXEC01/CLAIM01。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| REC03 | 局部拒绝触发诊断，普通等待不误判，结合DIAG01—05。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| REC04 | 等待安全释放，工具在途/资源未清/unknown不释放，结合EXEC02和STATE02。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| REC05 | 候选变化失效、范围内重签与越界询问，结合DATA01和HISTORY03。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| REC06 | 开始前合格替换；pin-member、贡献冲突、非官方来源、已开始或unknown阻止替换。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| REC07 | 迟到旧许可拒绝，新旧运行权互斥，结合AUTH04/05及STATE02。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| REC08 | begin前循环消失转普通审核，不直接通过。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| REC09 | 同根因同候选经适用性核对共用一次；根因/候选/范围不同不误合并。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| REC10 | 恢复共用结论不替代各原任务验收，结合HISTORY01/02。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| REC11 | 重复事件、重启、撤销、取消和暂停不重复副作用、不复活，结合FLOW01—04/STATE01。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| REC12 | 额度、1执行+1等待、普通两套计数、10分钟汇报、清理及现场核验的有效安装目录范围与第02/06/09/11/15/17章一致。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| BOOT01 | 新A不限home；新旧pair、策略、许可和令牌隔离，接收成果不复活旧协作。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| BOOT02 | 来源、固定摘要、未审状态、失败轮次及pin-member随接收保留，旧角色字母不冒充新身份。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| BOOT03 | 首次由现有合格窗口独立审核；作者自审、未审加载及伪造正式领取均拒绝。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| BOOT04 | 首次审核、授权应用和原业务通过分别结算；已获授权的准确范围不重复询问，越界拒绝。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| BOOT05 | 原协作入口未受信时保留原A及绑定，按当前授权独立复核和切换；不被第19章新建流程强制另建协作，不继承新协作预授权，已受信入口不重复启用。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| STATE01 | 暂停/停止与投递/begin竞争不启动失效动作，迟到outbox不能复活。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| STATE02 | 旧执行代次不能提交新结果或释放新资源；租约过期但工具在途/unknown不能抢占。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| STATE03 | 同根因共享恢复保留每个业务义务，部分检查通过不等于整个候选通过。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| STATE04 | 时钟跳变、过期、续期/写盘失败和部分持久化均可对账，不按超时重放。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| EVID01 | 包装器回执与内层日志分别标识，子进程日志不能冒充独立官方双源工具项。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| EVID02 | 历史盘点/文档有准确报告审查契约；不借空checks放行，不擅自重做安装。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| GIT01 | 本地提交和整合只含授权范围，保留用户改动与来源；无对应授权时拒绝推送、标签或Release。已有常规Git同步授权时，只推送本轮已核验成果及脱敏说明，确认准确远端、分支和提交，不夹带无关变更；标签、Release及部署继续按各自授权核验，不能由普通Git同步授权推导。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| CLI01 | 协作启动或exe路径/版本/摘要变化触发只读预检，重复同指纹事件去重。 | `scripts/pair_compatibility.py` | 静态候选待逐项核验；未判通过 |
| CLI02 | 路径缺失、非预期可执行文件、未知回执字段/展示适配均不冒充兼容；明确暂停受影响操作。 | `scripts/pair_compatibility.py` | 静态候选待逐项核验；未判通过 |
| CLI03 | 预检不创建会话、发任务、消费许可、读取密钥或重放操作；只读通过不等于真实通信通过。 | `scripts/pair_compatibility.py` | 静态候选待逐项核验；未判通过 |
| CLI04 | 在途升级保留原执行及适配绑定，只核对终态，不强杀或用新版重跑；新规则不追认旧回执。 | `scripts/pair_compatibility.py` | 静态候选待逐项核验；未判通过 |
| CLI05 | 适配修复经独立审核与适用验证后才能恢复，旧用户暂停不自动解除；无关本地工作不被无谓阻塞。 | `scripts/pair_compatibility.py` | 静态候选待逐项核验；未判通过 |
| BLOCK01 | 同一协调阻塞可在有效授权和安全条件下最多处理3轮，第三轮成功正常结算，失败则暂停该事项及依赖并询问。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| BLOCK02 | 改任务名、换窗口/候选/许可或跨协作接收不重置累计轮次；不同根因必须记录分类依据。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| BLOCK03 | 并发开始只计一次；中断/unknown归原轮对账，不以第2或第3轮名义重复副作用。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| BLOCK04 | 只读查询、重复通知及准入拒绝不算处理轮，但不得忙轮询；三轮后普通“继续”等事件不能自动启动第4轮。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| BLOCK05 | 产品修复两轮、审批两次真实故障超时、暂停/取消或权限限制先触发时立即按对应规则处理，不等待耗尽3轮；确认的暂停中止按08.3节单独计数，不清零原故障。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| APPLY01 | 当前协作有效策略、准确固定补丁及正式独立通过满足条件时，A自动应用并通知。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| APPLY02 | 仅自测/人工部分复核、候选变化、越界或授权失效拒绝应用；不附带全配置目录安装、账号修改、Hook改动或发布权限。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| APPLY03 | 工具在途、未决审批、unknown或用户主动暂停/停止时不切换或自动恢复。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| APPLY04 | 切换后核验实际加载、schema及必要检查，文件复制或启动受理不足以记应用成功。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| APPLY05 | 回执丢失/崩溃按原应用记录对账，不重复覆盖；失败维持相关暂停，不循环切换。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| APPLY06 | 安全回退仅限已审计划和兼容代码；禁止恢复旧库抹掉新事实。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| APPLY07 | 切换准备与新派发/领取/begin并发时，先建立门禁，再等待在途操作终态和独占写入；桥重启也不得绕过门禁。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| APPLY08 | 切换验证成功时若出现用户暂停、授权撤销或其他阻塞，只解除本次切换门禁，相关工作仍保持停止；验证失败不解除切换门禁。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| SUPER01 | 正式新阶段完整覆盖旧义务且无未决操作时，A自动登记替代并通知，旧报告及结论不变。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| SUPER02 | 部分覆盖、本地/人工复核、无正式通过或unknown均不释放旧名额。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| SUPER03 | 同一结算重复或并发只生效一次，任务与阶段计数分别正确，不形成负数或多释放。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| SUPER04 | 替代与暂停/停止竞争不派发新业务；跨协作引用不自动改写或恢复旧pair。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| SUPER05 | 新结论失效或证据冲突时保留历史并阻断受影响依赖，不恢复旧令牌重跑。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| TREE01 | 同workspace活动任务修改不同子目录 ； 按实际能力拒绝冲突派发并指出准确占用任务，不误称已有隔离。 | `scripts/pair_diagnostics.py` | 静态候选待逐项核验；未判通过 |
| TREE02 | 工具和关联回合安全结束后交回 ； handed_back、对应资源释放、证据保留；任务依赖仍不满足。 | `scripts/pair_diagnostics.py` | 静态候选待逐项核验；未判通过 |
| TREE03 | 工具运行/本任务审批未决/执行unknown ； 安全边界拒绝；不释放占用、不抢工具、不改完成状态。 | `scripts/pair_diagnostics.py` | 静态候选待逐项核验；未判通过 |
| TREE04 | A交回后B仍saving、D仍满额 ； 同时报告剩余阻塞，不把A释放当作全组恢复。 | `scripts/pair_diagnostics.py` | 静态候选待逐项核验；未判通过 |
| TREE05 | 已交回任务重复交回或重复继续 ； 明确拒绝或协议规定的幂等结果，不重做业务、不伪造完成。 | `scripts/pair_diagnostics.py` | 静态候选待逐项核验；未判通过 |
| TREE06 | 独立工作树与同名共享资源 ； 非冲突业务可实际重叠执行；公共资源继续串行，actor_cwd不变。 | `scripts/pair_diagnostics.py` | 静态候选待逐项核验；未判通过 |
| TREE07 | 工作树创建成功但登记/基准失败 ； 不派业务，保留原树和脏改动，明确未完成步骤。 | `scripts/pair_diagnostics.py` | 静态候选待逐项核验；未判通过 |
| TREE08 | 交回、派发、资源重新分配并发 ； 事务复验；旧执行代次不能释放新任务资源。 | `scripts/pair_diagnostics.py` | 静态候选待逐项核验；未判通过 |
| TREE09 | 原树含用户修改、junction或其他任务文件 ； 保留并核对归属，拒绝越界/盲迁；不凭路径字符串判断隔离。 | `scripts/pair_diagnostics.py` | 静态候选待逐项核验；未判通过 |
| SAVE01 | saving无登记且无文件 ； 本人安全保存，不能直接改ready；保存失败不消费或退出。 | `scripts/pair_assistance.py` | 静态候选待逐项核验；未判通过 |
| SAVE02 | 文件已落盘但登记丢失 ； 仅按恢复协议核对后登记；不覆盖原文件或重复保存成果。 | `scripts/pair_assistance.py` | 静态候选待逐项核验；未判通过 |
| SAVE03 | 文件摘要变化、漏页或缺恢复计划 ； 拒绝消费，保留原检查点；A不冒用成员身份。 | `scripts/pair_assistance.py` | 静态候选待逐项核验；未判通过 |
| TURN01 | interrupted回合含准确已完成工具 ； 双源、身份、唯一回合、时间、命令/cwd及事务复验均满足才结算工具，不直接通过业务。 | `scripts/pair_review_receipts.py` | 静态候选待逐项核验；未判通过 |
| TURN02 | 错误threadId、空值或类型错误 ； 拒绝；不得用当前线程覆盖原矛盾；拒绝路径不修改许可。 | `scripts/pair_review_receipts.py` | 静态候选待逐项核验；未判通过 |
| TURN03 | 同ID重复/矛盾、返回顺序不同或后页才重复 ； 完整消费原线程分页后拒绝，不能只取第一条。 | `scripts/pair_review_receipts.py` | 静态候选待逐项核验；未判通过 |
| TURN04 | 首次匹配后分页失败或无准确匹配 ； 不结算成功，保留失败/未知证据；不改数据库补证。 | `scripts/pair_review_receipts.py` | 静态候选待逐项核验；未判通过 |
| TURN05 | 缺省身份的合法唯一正例及无关turn ； 仅按经审核的上下文规则处理；不误拒正常回执，不扫描其他会话。 | `scripts/pair_review_receipts.py` | 静态候选待逐项核验；未判通过 |
| TURN06 | 终态时间冲突、非有限时间、终态后工具或重启 ； 拒绝；完整检查原日志与官方终态。 | `scripts/pair_review_receipts.py` | 静态候选待逐项核验；未判通过 |
| TURN07 | 准确工具非零/只有API或raw一侧 ； 非零记failed，缺证据不记success；业务仍按契约判断。 | `scripts/pair_review_receipts.py` | 静态候选待逐项核验；未判通过 |
| FLOW06 | 安全交回后桥崩溃或释放事件重复 ； 后续意图可恢复，无重复派发，不丢未完义务。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| FLOW07 | 等待条件解除但用户同时暂停/撤销 ； begin前重新核验，不恢复用户已停业务。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| FLOW08 | handed_back缺合法接续能力 ； 保留现场并报告能力缺口，不改库、复活旧令牌或重复实现。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| SCOPE01 | 旧恢复方案与新协作方案并存 ； 依据当前明确目标选路，不因读文档就start、暂停或恢复。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| SCOPE02 | 当前会话与原A UUID不同但同home ； 拒绝冒用身份；目录相同不授予旧A权限。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| POOL01 | 四工作席位与辅助名额分别计数；辅助并发1—4，unknown不得凭超时释放。 | `scripts/pair_auxiliary.py` | 静态候选待逐项核验；未判通过 |
| POOL02 | 子代理再委派、重复事件及并发创建经过统一准入，不超过上限或重复启动。 | `scripts/pair_auxiliary.py` | 静态候选待逐项核验；未判通过 |
| POOL03 | 每成员任务/阶段容量在2、3、4下及并发升降时正确；子审核不重复占父阶段名额，已有成果不丢失。 | `scripts/pair_auxiliary.py` | 静态候选待逐项核验；未判通过 |
| POOL04 | 四个父辅助同时委派并等待时，确认安全挂起后释放计算槽，子任务及父接续统一准入且分别核算实际启动；有在途/unknown或不可交出资源时不伪造释放，由A拆解等待循环。 | `scripts/pair_auxiliary.py` | 静态候选待逐项核验；未判通过 |
| BUDGET01 | 64次累计包含复用会话的新任务、重试、独立复核和再委派；换候选/账号/名称不重置。 | `scripts/pair_auxiliary.py` | 静态候选待逐项核验；未判通过 |
| BUDGET02 | 开始、预留、unknown对账及重启恢复不多扣或漏扣；预留验证预算，用尽不记完整通过。 | `scripts/pair_auxiliary.py` | 静态候选待逐项核验；未判通过 |
| APOOL01 | 同账号不同配置正确合并额度；只使用已允许且实测合格的官方GPT配置。 | `scripts/pair_auxiliary.py` | 静态候选待逐项核验；未判通过 |
| APOOL02 | 工作窗口忽略额度不传给辅助池；辅助单独授权仅本协作/指定池/周期有效，不绕过硬限制和预算。 | `scripts/pair_auxiliary.py` | 静态候选待逐项核验；未判通过 |
| ROLE01 | 辅助可写隔离证据但不能修改产品、提交、合并、安装或发布；OCR修复步骤不可执行。 | `scripts/pair_audit_adapter.py` | 静态候选待逐项核验；未判通过 |
| ROLE02 | 建议最多一句方向，无补丁、额外调用或等待修复；报告保存并安全收尾后可接下一审核。 | `scripts/pair_audit_adapter.py` | 静态候选待逐项核验；未判通过 |
| ROLE03 | 无建议或违规建议被剔除时，有效问题证据仍可按适配schema校验；原版与适配版本不混淆，不弱化其他必填证据。 | `scripts/pair_audit_adapter.py` | 静态候选待逐项核验；未判通过 |
| CTX01 | 项目复用重验版本；独立复核新上下文，发现者/作者及实质贡献者不得自验。 | `scripts/pair_auxiliary.py` | 静态候选待逐项核验；未判通过 |
| CTX02 | 换角色名或账号不抹去贡献；历史结论不能作为新版本答案。 | `scripts/pair_auxiliary.py` | 静态候选待逐项核验；未判通过 |
| TRACK01 | 分层触发、固定覆盖和最终完整审计正确；必要轨道不得因失败而删除。 | `scripts/pair_stages.py` | 静态候选待逐项核验；未判通过 |
| TRACK02 | 多报告均结束不等于全部通过；缺轨道、缺覆盖、必要待验证或冲突阻止正式整合。 | `scripts/pair_stages.py` | 静态候选待逐项核验；未判通过 |
| REPORT01 | OCR覆盖率、JSON校验及Cloudflare三类发现正确映射；rejected和零发现均不被误解。 | `scripts/pair_audit_adapter.py` | 静态候选待逐项核验；未判通过 |
| REPORT02 | 多会话提交、迟到结果、候选变化和结算竞争不覆盖其他执行；缺准确回执不通过。 | `scripts/pair_audit_adapter.py` | 静态候选待逐项核验；未判通过 |
| WIN01 | 网络/环境/写入/资源隔离逐项实测；仅临时目录、工作树或WSL存在不能通过。 | `scripts/pair_recovery.py` | 环境准备与动态验证暂缓 |
| WIN02 | Windows特有行为在Windows验证；缺能力只读分析，必要动态验证不假报通过。 | `scripts/pair_recovery.py` | 环境准备与动态验证暂缓 |
| WIN03 | 子进程残留时不提取；句柄式白名单提取拒绝越界、junction、符号/硬链接、类型/大小变化和检查后替换竞态，目标独占创建，不暴露认证或真实状态库。 | `scripts/pair_recovery.py` | 环境准备与动态验证暂缓 |
| UPSTREAM01 | 固定版本、摘要、许可与本地差异可追溯；缺组件、升级不兼容或格式错误不静默降级。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| MPOOL01 | 旧单审核记录迁移保留历史；公开操作按准确目标绑定，旧客户端不误操作多个审核者。 | `scripts/pair_auxiliary.py` | 静态候选待逐项核验；未判通过 |
| MPOOL02 | 全局暂停、A掉线、会话退出、池限流及桥重启覆盖全部辅助任务；仅安全释放自身资源。 | `scripts/pair_auxiliary.py` | 静态候选待逐项核验；未判通过 |
| PAUSE01 | A领取审批后Esc，暂停超过180秒再继续 ； 全组停新增；许可关闭/到期原因准确，恢复核实后新申请，原目标可接续。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE02 | A可信中断但原因不明；仅API疑似interrupted ； 前者按统筹中断暂停，后者不冒充确认；均不伪称已证明用户按键。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE03 | B/C/D可靠手动暂停、明确局部暂停或原因不明异常 ； 按已授权范围处理，不把局部/未知原因擅自解释为已确认全组人为暂停。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE04 | 当前工具、自动允许工具和已入CLI队列同时存在 ； 已开始工具安全收尾；下一动作重新受门禁约束，未确认不释放资源。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE05 | 全组暂停后管理消息和明确继续到达 ； 查询、对账、保存和控制通道仍可用，不能借管理通道执行新业务或测试。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE06 | 待批、allowed与批准实际发送竞争 ； 未发出才关闭；可能已发出保留granted/unknown；无暂停后补发旧允许。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE07 | 工具已完成、仍运行或结果unknown ； 分别正规结算、等待终态或隔离相关依赖，不统一取消或重跑。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE08 | 只询问、修改要求、引用“继续”或要求执行其他任务 ； 原协作仍暂停；登记新要求不派业务，明确指向本协作才恢复。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE09 | 首次继续、重复继续、数据库仍running ； 都核对实际现场；首次产生一次恢复记录，重复不重复许可/业务。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE10 | 两工作窗口同时继续或A仍不可用 ； 恢复幂等；不转移A审批权，已授权安全独立工作可先恢复。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE11 | 旧中断迟到、恢复后再Esc、时钟跳变 ； 按来源事件与控制代次排序，旧事件不误停，新暂停阻止未开始恢复。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE12 | 游标与暂停登记间崩溃、通知重复或回执丢失 ； 事务原子、outbox可恢复，不漏暂停或重复业务。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE13 | 日志半行、截断、轮转、替换、缺失及首次启用 ； 正确补读或报告缺口，不重演历史暂停；明确继续且现场满足时可建立新基准。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE14 | 某执行器无可靠工具边界控制或窗口不响应 ； 明确暂停尚未确认、对象与原因，不假报全停或凭超时释放资源。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE15 | 旧证据有效、候选/环境变化或旧契约不支持引用 ； 有效部分只补缺项；变更部分重审，跨执行引用准确且不偷改旧契约。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE16 | 暂停期间收到完整报告、残缺报告或迟到旧报告 ； 有效事实可结算但不派后续；缺证据不通过，旧报告不覆盖新执行。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE17 | 暂停中止、真实超时、新审核启动及旧执行对账 ； 故障次数、暂停次数与64次启动分别累计；不漏扣、不清零、不重复扣对账。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE18 | 自动审核曾启用、后被用户禁用或额度保护触发 ； 只恢复本次暂停关闭且仍被允许的能力，不覆盖后来的用户决定或保护。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE19 | 暂停期间用户改文件/分支/路径、取消或退出 ； 保留用户修改与来源，核验现场；普通继续不复活取消/退出事项。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE20 | 四工作窗口和多审核会话真实暂停再跨窗口继续 ； 逐项确认停止新增与实际接续，展示部分恢复和准确剩余阻塞；模拟不冒充真实验收。 | `scripts/pair_control_events.py` | 环境准备与动态验证暂缓 |
| PAUSE21 | 恢复核查需要新批准的只读回执查询 ； 明确继续后可经受限通道取证，原A审批不转移；普通业务仍受门禁，不能重跑测试，必要新辅助计入预算。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE22 | B先登记继续，A中断日志延迟且先后关系不明 ； 保留相关会话事件边界，无法证明为旧中断时先核对；不凭接收顺序或时间戳忽略暂停，也不无限重复询问。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| PAUSE23 | 协作库仍running，恢复核查与普通派发并发或崩溃 ； 恢复记录及核查门禁同事务建立，核实安全前不派业务，重启和重复继续不重复执行。 | `scripts/pair_control_events.py` | 静态候选待逐项核验；未判通过 |
| CTL01 | 审批Hook通过，但自动允许工具、已入队任务或下一工具缺执行前检查 ； 只承认已证明的入口能力，显示具体未确认对象，不记全组已停。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| CTL02 | 支持检查的入口收到暂停、撤权与批准发送/业务开始竞争 ； 按同一控制代次复验；新暂停阻止尚未开始的动作，在途工具保留真实终态及资源，不擅改全局权限。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| RO01 | diagnose-blockers与doctor读取当前库、旧schema、缺失库或不可读库 ； 使用独立只读连接及一致性事务，不触发Store升级、创建、刷新或探测写入；前后业务状态、schema及文件内容不变，缺条件明确报错。 | `scripts/pair_diagnostics.py` | 静态候选待逐项核验；未判通过 |
| RO02 | 诊断与调度并用，或要求持久化快照/主动探测 ； 共用阻塞规则并标明新鲜度；写入只能走单独获准接口，严格只读入口无隐含变更。 | `scripts/pair_diagnostics.py` | 静态候选待逐项核验；未判通过 |
| POLICY01 | 分别启停传输恢复、业务接续、恢复审查和补丁应用 ； 四域授权及版本独立，其他域开关不扩大权限，旧协作新增策略默认关闭。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| POLICY02 | 用户暂停后另行关闭某策略，再明确继续、recover --safe或重启守护 ； 仅恢复本次暂停关闭且仍获准的能力，不覆盖后来禁用，不复活取消、退役、停止或closed_scope。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| COUNT01 | 各类处理开始后崩溃、重启、进入unknown并对账 ； 重启窗口、三轮协调、两轮修复、审批故障及64次启动分别持久化，不清零、不重复扣次、不把unknown当作新尝试。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| COUNT02 | 并发开始、改名、换候选/会话、纯读取及暂停关联审批中止 ； 相应开始与计数一致；历史不重置，纯读取不变成无限启动理由，暂停关联中止不抹去既有故障。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| ISOLATE01 | 隔离环境准备或能力验收未完成时请求候选动态检查 ； 拒绝动态执行，仅做安全静态分析；临时目录、工作树或装有Hyper-V不等于隔离能力通过。 | `scripts/pair_recovery.py` | 环境准备与动态验证暂缓 |
| ISOLATE02 | 宿主控制器、官方GPT客户端及客体目标同时存在 ； 认证和真实状态不进入客体；宿主外层回执与客体日志绑定但不混作同一官方工具项；资源、网络和证据提取按WIN01—03核验。 | `scripts/pair_recovery.py` | 环境准备与动态验证暂缓 |
| PACKAGE01 | 发行包脱离源码目录独立安装 ； 命令、JSON schema、规则、可信资产、固定依赖、许可证及文档引用完整；明确清单与逐文件摘要匹配。 | `scripts/install_skill.py` | 静态候选待逐项核验；未判通过 |
| PACKAGE02 | 工具依赖缺失、平台不符、摘要变化或文档引用失效 ； 报告准确能力缺口，不运行时下载、不静默降级，不将复制成功记成能力验证通过。 | `scripts/install_skill.py` | 静态候选待逐项核验；未判通过 |
| WATCH01 | 桥退出、桥活着但无响应、错误PID或锁被占用 ； 只对准确已退出且可独占锁者有限恢复；活挂不强杀、不抢锁、不启动第二实例。 | `scripts/pair_watchdog.py` | 静态候选待逐项核验；未判通过 |
| WATCH02 | 明确停止、守护自身退出或桥重启预算耗尽 ； 关闭恢复意图后不自启；下一正常入口报告守护异常；预算耗尽保留事实并停止循环。 | `scripts/pair_watchdog.py` | 静态候选待逐项核验；未判通过 |
| WATCH03 | 桥重启期间有暂停、撤销或迟到旧事件 ； 先补读控制事件与原执行对账，再决定调度；不略过停机期间事件，不恢复失效业务。 | `scripts/pair_watchdog.py` | 静态候选待逐项核验；未判通过 |
| WATCH04 | 发送前失败与发送接受状态未知分别发生 ； 前者仅在证明确未接受时沿用原ID有限重试；后者按原队列/回合对账，缺证据不重放。 | `scripts/pair_watchdog.py` | 静态候选待逐项核验；未判通过 |
| FLOW09 | 模型已结束但业务没有合法交付 ； 一次提示原负责人核对，不代complete、不重派；剩余工作使用合法新运行权及原义务关联。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |
| ASSIGN01 | 登记任务及必要检查时并发或中途失败 ； 范围、验收与非空契约同事务生效；不派发无契约业务，不借文档任务执行无关测试。 | `scripts/pair.py` | 静态候选待逐项核验；未判通过 |
| MIG03 | schema 13升级到拟新增14，旧单审核者与在途记录存在 ； 准确迁移旧绑定、epoch、许可、报告及历史；先对账，旧程序拒写不兼容新库，多审核者入口不能默认选第一项。 | `scripts/pair_recovery.py` | 静态候选待逐项核验；未判通过 |

## 调度规则

| 编号 | 规则 | 实施位置及边界 |
| --- | --- | --- |
| S01 | 动态分工 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S02 | 等待期间转做独立工作 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S03 | 公共部分唯一修改者 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S04 | 审核积压时收敛新增开发 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S05 | 优先解除后续依赖 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S06 | 接近额度预警时逐步收尾 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S07 | 默认独立工作树 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S08 | 小任务不强拆 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S09 | 慢任务先核对再拆分 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S10 | 测试分层 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S11 | A 协调优先 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S12 | 接口约定允许先行开发 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S13 | 仅受影响窗口更新基线 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S14 | 限制半成品数量 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S15 | 共享资源按需占用 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S16 | 按依赖及时小批整合 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S17 | 避免低优先级任务长期搁置 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S18 | 协助优先避免重叠编辑 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S19 | 本机资源紧张时降并发 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S20 | 同池临时限流统一退避 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S21 | 临时限流后分批恢复 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S22 | 共享可核验资料和检查结果 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S23 | 预排候选任务并校验后自动投递 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S24 | 默认隔离测试环境 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S25 | 请求机会优先保障协调与审核 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S26 | 普通进度由 A 汇总 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S27 | 恢复原任务前比较优先级 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S28 | 投递前检查候选依据 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S29 | 普通求助合并发送 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
| S30 | 验收实际并行效果 | SKILL.md、pair_core、pair_diagnostics、pair_auxiliary；实际并行及调度效果未验证 |
