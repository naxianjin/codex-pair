# 父级结算报告完整性补测

本轮发现：`saved_validator_inputs()` 核对保存报告摘要，`settle_audit()` 却直接消费报告。新增测试先复现缺少 `report_integrity_mismatch:aux` 的失败，再修复父结算入口；原始报告、适配报告变化或保存摘要缺失时，拒绝计入该报告的结论和覆盖。

实际验证：受影响辅助池、最终回执、worker、清单共60项通过；补充缺摘要用例后辅助池35项再次通过。未修改活动状态库或安装副本。正式隔离补充事实接线、完整运行库身份、原窗口全工具控制仍未完成，不宣称正式验收通过。

独立OpenAI客户端只读复审未发现阻塞；按建议补充适配摘要缺失及结算清单为空断言，最终60项相关回归通过。全量首轮运行器遗漏Windows spawn主入口保护，导致并发领取测试出错，属于运行器错误；保留日志，修正后重跑，不记为产品通过。

修正运行器后固定快照全量1206项通过，0失败、0错误、3显式跳过，210.181秒。源码摘要4fb5dc6c6f3c95b25dd4e23350e1a83bb040ed7bcdd8790d4a73262680375ea7。全量快照早于最后仅增加断言的测试补强；最终测试文件的60项相关回归已独立通过，未宣称全量覆盖最后断言版本。

## 执行绑定补测

隔离Store夹具先完整结算passed，再修改执行记录中的候选摘要或契约摘要，两分支原实现均错误保持passed。父结算现于消费报告前核对当前候选的两个摘要，不符记录execution_binding_mismatch并跳过报告。修复后61项相关测试通过。另在66b32a3运行真实官方CLI通信测试通过，正文精确一致，idle/busy队列均完成，41.195秒。完整正式隔离回执及原窗口控制仍未验收。

独立只读复审未发现执行摘要修复阻塞；补多任务混合场景，失配报告被排除、有效报告保留，整体仍needs_validation；57项辅助/结算/清单测试通过。新增混合测试晚于全量快照，分别记账。

执行摘要修复的固定快照全量1208项通过，0失败0错误3跳过，210.011秒。源码摘要85fad3c07de52ff2b2e6a947c4bdc424e4a60838491a8e23bec5117712dc76de。

## 历史报告与会话复用

继续核对epoch发现：reserve_auxiliary合法复用空闲session会递增epoch，已完成报告不应跟随当前session代次失效。撤销未提交的过严epoch/session检查，产品实现保持71bbe4c；新增历史报告跨epoch保持有效的回归，58项辅助/结算/清单通过。原epoch变化探针的passed不记为缺陷；未提交的旧检查复审不作为保留实现的通过证据。

## 隔离补充证据登记入口

新增 `record-validator-run`，输入 execution_id、lease、32位小写hex run_id、request、native、artifact、tool_identity。工具身份使用小写SHA256，node须与请求node_sha256一致。入口重新生成当前Store固定请求，保存前在事务内复核报告与执行绑定；按run_id幂等，冲突拒绝。负例可追溯保存，超时、残留进程及异常终态拒绝。父settle仅列匹配执行及三份报告摘要的validator_runs引用。

此入口接受协调者提交的自洽诊断证据，不能证明隔离执行真实发生；所有记录和父引用固定formal_checks_verified=false，不清除正式门禁。它不是可信宿主直接取证、完整工具链登记或正式放行接线。独立OpenAI只读复审在此限定范围内未发现阻塞；63项相关回归通过。仍需可信执行器实际调用与正式能力认证，不能由输入JSON授予formal。

证据入口限制为最多32项工具身份、总JSON不超过2MiB；新增Node不匹配与超限测试通过。冻结首轮漏带pair_next_commands.py导致清单不一致，1失败37错误已保存，不记为产品通过；修正完整快照后重跑。

完整快照全量1212项通过，0失败0错误3跳过。最后仅追加命令映射断言，45项辅助/清单复测通过。正式回执接线仍未完成，本轮交付仅非正式登记及父结算引用。

## 真实VM到Store补充登记联测

910b12c实际运行Store夹具→固定请求→断网Hyper-V→宿主直接读取产物→登记→父结算。run 4e081f34d25d46928aaa1068a4a4c52b，两验证器成功，Store保存及父引用成功；formal始终false，父结算needs_validation。身份和报告为合成夹具，不代表生产会话或正式工具链认证；夹具在结算后清理。VM关闭且无pending。证据见validation/store-registration-20261105。

58项相关回归及三项隔离脚本行为测试通过。曾误用不存在的测试模块，改正重跑通过。真实联测前三次分别遇到路径转义、宿主PSModulePath缺失和PSSessionOpenFailed，均在派发前失败；补正路径、模块路径及开机等待后成功。完整工具链身份、正式回执接线、原窗口控制和安装加载仍未完成。

最终910b12c冻结代码快照全量1212项，0失败、0错误、3跳过，208.073秒；本次包含最后命令映射断言。仅新增验收记录和证据，无产品代码修改。

## 打包、隔离安装和控制续测

b9dcc1b完整120文件包在两个新建隔离home安装，逐文件清单一致，两个实际pair.py --help入口返回0；重复打包SHA256一致（68e63ee0ef07f0674d43a15a96593a17247b92a9e7c3969397fe97051286c606）。首轮夹具漏建home失败，创建后重跑通过。未替换活动安装或宣称原窗口加载。9项安装/清单及123项控制/恢复/领取回归通过。

真实官方CLI通信测试通过，36.779秒；另外核对结果确认中文正文精确一致、idle/busy队列均实际处理。原B只读探针确认PermissionRequest enabled/trusted，缺失及错误token均401；新诊断服务中的原thread为notLoaded，后台查询thread not found，不能据此判原B空闲。原生hosted/unified/specialized工具控制缺口仍在。RealCore及RealIntegration仍使用--new-session旧路径，尚未迁移实际验收。

真实VM超时run f5c6c4097cd04f16b3f3877f6382527b：ExitCode=124、TimedOut=true、ActiveProcesses=0。首轮远程会话连接失败，延长开机等待后取得native终态；运行器错误要求超时artifact存在而失败。未重放该次执行，直接复用native结果调用真实Store登记入口，确认拒收且父结算validator_runs为空。该Store使用新合成夹具，不声称超时结果通过了候选绑定校验；终态先行拒绝。VM已关，无pending。正式完整身份和原窗口控制仍未通过。

## 非正式引用上下文完整性返修

新增负例复现：保存的补充记录中stage_id被改写后，父结算仍列引用。修复settle_audit，复用_context核对完整执行上下文；使用历史execution epoch/session，不跟随reviewer当前epoch。补充跨执行、阶段、候选、契约、会话、epoch篡改拒绝，以及历史validator引用跨reviewer epoch保留、2MiB ASCII/中文精确边界、保存期间来源变化测试。58项相关回归通过。同步注入的来源变化不是实际并发证明。

56项兼容性/验证器/Git集成及151项严格回执回归通过。真实旧RealCore用例实际失败：Original visible CLI required；不能放宽原窗口限制来伪造通过，RealIntegration同样仍用旧--new-session。用户明确B无额度，不向B发消息或启动模型；只读进程识别看到codex-a启动器，但无明确原执行器WebSocket记录。两项真实原窗口用例仍未完成。

官方独立只读复审未发现本次实现阻塞，明确非正式范围；建议的历史validator引用epoch用例随后补充通过。修复实现再次真实VM到Store通过（run 8ff98f7c520e4c0c914ea0369db85f74），formal=false且needs_validation；记录overlay摘要，不把基础HEAD冒充已提交修复版本。VM关闭、无pending。

修复实现冻结快照全量1212项，0失败0错误3跳过；快照早于最后扩展context字段及历史validator epoch断言，最终测试文件58项相关回归另行通过。全量日志及摘要已保存，未将最后断言称为全量覆盖。

准确提交1bf559a再次完整打包并安装至新隔离home：120文件摘要一致，pair.py --help实际加载返回0。未改活动home，原窗口加载仍未验收；证据exact-install.json。

## epoch类型精确性独立补测

用户要求暂跳过需B事项且禁止向无额度B发消息。本轮无B通信。新增负例先复现Python字典比较将epoch True、1.0与1视为相等，两项失败；settle_audit改用已有digest规范JSON比较完整context，保持历史execution epoch语义。修复后63项辅助/清单/结算/真实Node验证器回归通过，独立官方只读复审未发现阻塞；审核者因只读临时目录限制未运行目标测试，未冒称独立测试通过。

工具归档10322个文件逐项大小/SHA256匹配（Git9540、Python781、Node1）；仅归档基线，不涵盖Windows系统库或实际加载模块，formal=false。只读客体库存探针New-PSSession失败，未执行查询，finally已关闭VM；不记通过。未触碰活动库、安装或B。

最终epoch类型修复快照全量1212项，0失败0错误3跳过，包含本轮最终测试文件。独立真实诊断夹具确认数据库前后SHA256不变，read_only=true、runtime_observed=false；不代表活动库验收。

客体清单重试取得结果：786文件，原归档782个Python/Node文件全部存在且摘要匹配，但额外4个encoding __pycache__/*.pyc不在基线。精确库存比较失败，保留现场和差异，未删除缓存后冒称通过。VM Off、无pending。Python -I -B不等于禁用既有pyc读取，工具链正式身份仍未成立。运行器shell参数未改变实际PowerShell7承载，兼容代理警告已记录；显式子进程powershell.exe版本实测5.1。
