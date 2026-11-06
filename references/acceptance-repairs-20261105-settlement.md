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

## 客体缓存来源核对

客体Python3.12.14实查4个额外encoding pyc：magic、时间戳、源大小和递归代码结构均匹配当前源码；源码SHA256逐项匹配原工具归档，缓存SHA256与先前库存一致。不能据此识别历史写入进程。__cached__路径不是实际读取证明，另取-v日志前不标加载通过。第二次探针PSSessionOpenFailed，未执行，保留失败并延长开机等待重试；无B通信。

既有离线控制器capability登记的11个源文件摘要全部一致，仅证明离线unittest范围基线未漂移，不扩大生产能力。

延长开机等待后客体加载探针成功：Python3.12 -I -B -v实际从4个pyc读取代码，完整代码对象与源码重编译相等。制作仅观察用途的786文件清单，精确库存匹配，改缓存/增加/缺文件比较均拒绝。未替换可信工具链基线、未提升formal；历史写入者及Windows系统/动态模块认证仍未知。VM已Off且无pending。

进一步从工具归档PE导入目录读取Python EXE/DLL及Node静态依赖，确认Windows系统DLL/API-set不在工具目录清单内。静态导入表不涵盖delay-load、动态LoadLibrary或实际映射，不能作为完整加载证明。

客体诊断Python进程实际模块枚举成功，共30个模块并保存文件SHA256。仅单进程单时刻，未覆盖受限validator整棵进程树、Node、PowerShell或编译器；不作完整工具链认证。VM关闭且无pending。

## Node与PowerShell依赖续测

未向B发送消息或启动B回合。40项Windows/验证器回归及16项审查适配回归通过；适配首轮缺少测试规定的符号链接夹具，补齐隔离夹具后通过，未修改产品实现。

客体探针首轮PSSessionOpenFailed，未派发命令；延长开机等待后成功。Node单时刻33个模块，PowerShell5.1单时刻86个模块、23个程序集，均保存路径及模块文件摘要；小型Add-Type检查返回42。发现Kernel-Process及.NET ETW事件源，只证明可查询，尚不证明受限验证器进程树完整加载取证。所有诊断Formal=false。

客体ETW动态试验成功：188个事件，初筛69个文本匹配，按EventData.ProcessID准确过滤后68个属于目标Node：启动1、退出1、映像加载33、卸载33；启动/退出ProcessSequenceNumber一致。已保存原始初筛及结构化核对证据。未验证丢事件计数、完整受限进程树、映像摘要绑定或编译器/托管程序集生命周期，formal仍false。

## ETW父子进程与丢事件续测

独立客体诊断执行Node→PowerShell→Add-Type，退出0。按ProcessID及ProcessSequenceNumber、父进程序号构建6进程树，包含两个conhost、csc及cvtres；6个启动均有匹配退出。tracerpt汇总482事件、丢失0。仅提升了诊断可观察性，不代表受限验证器或正式能力认证。原始事件和结构化摘要见validation/bytecode-provenance-20261105/guest-etw-tree*。

真实受限验证器ETW联测run 4a308fd866a142f9948bde7f1095412c成功：ExitCode0、ActiveProcesses0、TotalProcesses13、未超时，两验证器成功。按PID及进程序号关联13进程，全部匹配退出，与作业总数一致；464条映像加载事件，tracerpt共1077事件、丢失0。核对脚本正例通过，删除退出、错PID、错进程序号、非零丢事件4负例均拒绝。VM Off、无pending。

根进程目前由隔离观测选取，尚未绑定native根PID回执；映像加载摘要、托管程序集生命周期及正式宿主到Store认证仍缺失。诊断副本保留原控制和工具摘要校验，未改原运行器、活动安装或活动pair库，formal=false。

继续提取该受限进程树106个唯一映像路径：Windows目录93、工具目录11、Windows Defender目录2。库存数量和分类求和一致。路径观察不等于映像文件摘要绑定，也不涵盖仅托管程序集；完整工具链身份继续待验证。

## 原生根身份离线核对

诊断副本从创建进程持有句柄取得RootPid与GetProcessTimes创建FILETIME，在恢复线程前读取，原限制及清理保留。真实run cc23fcf635854ae39e8ddb2288437abf退出0、活动0、总进程13；原生PID792及创建时间134384057541274136与唯一ETW启动事件精确匹配至100ns，ETW序号124。PID或创建时间改写两负例均拒绝。最初生成脚本默认GBK读取失败，指定UTF-8后完成，未将失败算通过。

独立CLI元数据确认openai/gpt-6.1-sol，会话01a1aeaf-e777-7f70-b7d7-7aa87ac0c065。静态审核确认C#保留限制及清理；指出控制器本身不强制匹配、基线及ETW工具来源绑定不足。补充离线核对不消除这些控制器阻塞，保持formal=false，未整合正式能力或活动安装。VM已关闭、无pending，未联系B。

## 根身份诊断门槛接线返修

Test-RootTrace接到native保存后、terminal receipt及pending清理前；无效证据抛错并保留对账。首轮真实run e193d17f83eb4219ae08b89657f288d4通过。独立openai/gpt-6.1-sol复审发现Provider未限定及丢事件声明可矛盾，均保留为阻塞历史。随后限定Kernel-Process GUID、唯一零计数行及XML EventsLost；正例及8负例在PowerShell7和原生5.1通过，最新真实证据离线复核通过，尚未把修订后门槛称为再次VM实跑。脚本首次夹具运算符、空白匹配及重复回车问题已修正；不记首次失败为通过。测试脚本改名避免大小写路径冲突。来源基线/helper摘要、映像及程序集身份仍未完成，formal=false。

独立复审第二轮在原生5.1及7.6.6实际执行正例和8负例全部通过，确认上述两阻塞已修复，仅通过诊断门槛；VM接线仍为静态复核，来源绑定与formal未通过。VM Off、无pending，未联系B。

## 最终根身份门槛真实联测

准确543b8a1控制器及最终Provider/唯一零计数/XML丢事件检查实跑VM成功，run 00968f4359f64fc7bb7542d9edbb3fca。ExitCode0、ActiveProcesses0、TotalProcesses13、未超时，两验证器成功；门槛执行后生成terminal诊断回执。回执native摘要和控制器摘要实际核对一致，5份源码实跑前后无变化；8负例复测通过，来源核对脚本5个单文件摘要错配均拒绝。VM Off、无pending，未联系B。

新增来源清单仅供离线复核，未把helper/baseline摘要接成运行门禁，未声称独立可信能力。完整映像与托管程序集身份、正式Host→Store认证仍未完成，formal=false。

## 连续来源、程序集及Store续测

来源清单接到helper加载前校验及receipt前复核，五文件集合固定，请求/回执记录SourceInventorySHA256。5个真实入口单文件错配在VM派发前拒绝；首轮原生PSModulePath缺Get-FileHash，补测试环境后通过。独立官方GPT复审指出清单双读取，改为同一sourceBytes解析及计算SHA256；限定静态复审通过，helper加载替换窗口、ETW工具认证及独立可信基线仍未解决。

最终同字节修复真实Store联测run 6ae08ee4ec21432386dca2f612b183b3通过：固定请求→真实VM→根身份及来源门槛→Store保存→父引用；SourceInventorySHA256与请求/回执/当前清单一致。合成actor及报告夹具，父仍needs_validation、formal=false。记录source_head及overlay摘要，不将基础HEAD冒充工作区修订。

双Provider命令初次ETW启动失败，未运行测试进程；改为单独.NET Loader采集后成功，21加载/21卸载按PID+AssemblyID+CLR实例全部匹配，丢失0，包含动态程序集；缺一卸载负例被识别。此轮无kernel映像事件，不能称映像摘要已完成。provider文件形式的组合采集继续验证。

组合Provider改用-pf文件后成功：kernel映像与CLR Loader事件同次存在，零丢事件，110个映像路径全部取得运行后磁盘SHA256。仅提升诊断观察，不是加载瞬间文件绑定或可信基线认证；受限验证器组合采集继续运行。

受限验证器组合实跑run fdfe011361bf45fc802cb22690a946ad成功：13进程与作业总数一致，46程序集加载/46卸载按PID+AssemblyID+CLR实例全部匹配，缺一卸载负例识别，零丢事件；验证器树106映像均取得运行后磁盘SHA256。观察器事件已按树PID排除。该摘要不是加载瞬间文件绑定，没有独立可信系统基线或程序集内容摘要，正式认证仍未通过。

组合诊断独立复审确认46对生命周期及106映像观察，但发现组合入口清单误绑原root启动器，已改为run-combined-etw；修订后未实跑。组合终态只强制根身份及零丢事件，树筛选/程序集配对仍属离线核对，不将terminal receipt称为组合验证门禁通过。保留审核阻塞；后续须将组合检查接线。规范化新控制器换行后重生成清单，历史实跑证据保留原字节摘要，不能冒充当前检出版本。

## 组合门槛最终真实续测

修复后的六项来源清单和组合检查器已在真实VM运行，run 18408ceca21b432f89efd6361d3dee09。检查器在terminal receipt之前实际通过：13进程、46个程序集身份配对键、106个映像摘要记录；回执的NativeSHA256、ControllerSHA256、SourceInventorySHA256及六项来源摘要均重新核对一致。独立openai/gpt-6.1-sol第二轮只读复审确认原两阻塞修复，限定诊断范围内无新阻塞；该复审未执行VM，本段真实运行由统筹方另行核验。初审报告保留为修复历史。

check-combined-negative-cases.py使用此次真实证据，正例和六负例通过：缺卸载、错CLR实例、错AssemblyID、缺全部映像摘要、短摘要、错根PID均拒绝。VM已关闭（Hyper-V State=3），无vm-pending.json。控制器成功输出包含检查器文字和JSON两行，当前launcher仅输出、不解析该流；未将其宣称为纯JSON接口。

全部formal=false。配对只证明事件计数平衡，不证明时序完整；摘要只为运行后磁盘观察，门槛仅检查覆盖及64字符长度，不构成加载时内容认证。helper校验后加载窗口、调用者自选基线、ETW工具独立可信来源、程序集内容digest、正式Host→Store身份认证及依赖原B窗口的控制验收仍未完成。未联系B，未改活动安装、活动pair库或桥。本轮为诊断脚本和证据补充，产品实现未变，沿用此前1212项全量结果，未重复运行或声称本轮重跑。

发布前发现Git换行转换使清单与暂存字节不符；为清单引用的诊断源码设置精确字节保留及CRLF空白识别，重新暂存后两份清单全部逐项匹配，未修改实跑源码字节。git diff --cached --check通过。

另从13c9e09的Git archive解出全新副本，两份来源清单逐项摘要一致；在该副本执行组合正例及六负例全部通过，确认发布字节可复现。证据git-archive-reproduction.json。

## 来源校验后加载窗口修复与归档核对

两个诊断控制器对清单和全部来源文件从校验前开始持有FileShare.Read句柄，覆盖helper加载、实际执行及terminal receipt生成，finally释放。实际哈希从所持句柄读取。原生PowerShell5.1隔离副本测试确认写入/删除拒绝；摘要错误及部分打开失败使用实际控制器代码，失败后每个文件均可独占打开。首版测试直接尝试删除真实源码，经独立审核指出风险后改为一次性副本；首次测试生成及作用域错误已修，不计为通过。

真实VM run b970d7ae4dfb42f994e311eda76604ca成功：13进程、46程序集配对键、106映像摘要；receipt绑定当前控制器、清单及原始native字节。独立openai/gpt-6.1-sol复审确认原两测试阻塞消除；未由审核者重跑VM。VM Off，无pending。锁解决当前Windows普通写删路径的来源替换窗口，不代表系统映像加载身份、管理员对抗或独立可信基线。

另核对786项工具观察记录与安装前tools.zip及manifest：782项存在且三方摘要完全一致；4项为归档外生成的Python encodings缓存。归档来源记为existing local application distributions，不能改称发布者认证。保存toolchain-archive-comparison.json。formal始终false，尚缺系统/ETW独立可信来源、加载瞬间内容和程序集digest、正式Host→Store及原窗口验收；未联系B。

## 核心及全映像Windows签名诊断

断网客体Get-AuthenticodeSignature检查108文件（106观察映像及logman/tracerpt）完成：93 Valid、15 NotSigned；15项均为NativeImages目录.ni.dll。106映像当前磁盘摘要全部与既有受限运行观察一致。核心Python签名者为OpenAI OpCo, LLC，Node为OpenJS Foundation，Windows工具为Microsoft；不将Python臆称python.org发行。仅客体Windows信任评估（VM无网卡，未强制离线信任标志），未证明独立基线、吊销最新性、加载字节或native-image与IL对应关系，formal=false。

模块字段问题CLR-MODULE-DECODE-1：受限13进程树92个ModuleLoad/Unload记录中92个ModuleILPath为数字、28个ClrInstanceID空。宿主Get-WinEvent V2模板包含Reserved1，tracerpt XML缺该字段；当前只记录错位线索，尚未依原始事件属性修复，不使用猜测移动字段建立摘要身份。首次树筛选忘记去除ETW PID空白导致StopIteration，修正后取得13进程及92事件。程序集计数配对的先前诊断不等于模块路径解码通过。

独立签名证据审核核对108=93+15及106摘要一致，未发现formal误报；指出全轨迹94模块事件与树内92混用及offline措辞，已分别标注范围和信任标志限制。原始ETL在用户及系统临时目录未找到；新探针首次PSDirect会话建立失败，未派发加载，增加开机等待后再试。

## CLR原始属性解码续测

新小型提升权限Node→PowerShell→Add-Type探针在160秒开机等待后成功，ChildExit0；42个原始Module事件按同次客体V2模板具有13属性，包含Reserved1。新增check-clr-raw-properties.py按模板名字映射原始属性，21模块身份键加载/卸载计数平衡；缺属性、错版本、缺卸载三负例全部拒绝。已确认tracerpt旧XML字段映射错误，未猜测修订历史XML。此为新独立小探针，不是受限验证器重新取证，未解决模块内容digest或正式认证；formal=false。原ETL两次查找均未找到，失败保留，未记为成功。

原始小探针独立openai/gpt-6.1-sol审核通过，并实际复算42事件/21配对/3负例，确认Reserved1映射。随后新建独立raw-module诊断入口，保留旧已审核入口；真实受限run f26f12ac6b5e45049e83ac43cb31dba9成功，13进程、46程序集配对和106映像检查通过。新增原始模块离线核对：92模块事件形成46配对，每项PID/AssemblyID/CLR实例关联同树程序集；缺属性、错AssemblyID、错CLR和缺事件四负例拒绝。模块检查仍在terminal之后离线执行，未称为receipt前门槛。formal=false，内容摘要未完成。

受限原始模块复审确认资源限制保留、六项来源摘要一致、46模块关联及四负例通过；指出导出删ArtifactBase64导致回执摘要不一致及重复完整事件对未拒绝。已保存原始native字节并核对receipt SHA256；增加恰好一次约束及重复完整对拒绝。46加载记录有24个唯一非空IL值，细分为21绝对文件路径和3动态模块名称，尚未收集内容摘要。

IL文件续测取得21个绝对路径的磁盘SHA256及Windows签名观察，全部Valid；3个动态模块名称单列不当作文件。首次探针误传动态名称报FileNotFound，失败不计通过；修正输入后成功。仍为运行后取样，不证明加载时字节，也未取动态模块内容。

## 当前提交诊断回放复测

在398d73f复测组合正例及六负例、原始CLR小探针正例及三负例、受限原始模块正例、原生PowerShell5.1来源持锁正常/失败清理、根身份正例及八负例，全部通过。新增check-restricted-raw-negative-cases.py保存可重跑破坏测试，覆盖缺属性、错程序集、错CLR、缺事件、重复完整对及未知版本，六项均拒绝；执行真实检查器，未复制业务判断。三份当前来源清单逐项同时匹配磁盘和HEAD字节。证据restricted-raw-replay-results.json和current-source-replay-results.json。

本轮未修改生产实现、未启动VM或联系B，未重复此前1212项全量测试；动态模块内容、加载瞬间身份、独立可信基线和正式Host→Store仍未通过，formal=false。历史未跟踪日志原样保留，不纳入提交。

## 完整本地回归与Windows测试清理修复

当前源码先分组复测辅助池38、核心26（跳过1）、适配器16、控制等111（跳过1）、辅助审批等329、原生控制等260、其余290、集成11（跳过1），均通过。初次适配器缺预置reader-symlink夹具，补本地夹具后通过；几次误指定不存在模块产生ImportError，未计为产品失败或通过。

首次完整1212项发现Windows bridge生存测试清理触发WinError32：daemon锁已释放但进程仍持stderr日志句柄。仅修测试，启动后持有准确进程句柄，清理目录前等待该进程退出并关闭句柄；未改生产bridge或活动实例。定向及Windows组36项通过。首次用stdin包装全量触发multiprocessing无法重载<stdin>，该运行无效并停止；改标准模块入口，最终1212项206.698秒，0失败0错误3跳过。结果current-standard-regression.json及log，不沿用错误包装日志。标准运行期间曾误读旧日志错误标记，最终已按准确日志核清。

本轮未启动真实B或VM。三个跳过项目未记通过，生产正式认证、动态模块内容及原窗口验收仍未完成。formal=false。

## NativeImage与IL观察关联

新增check-native-il-links.py复核现有真实受限模块事件及后续磁盘签名观察：15个NotSigned NativeImage均有模块记录关联到签名Valid的IL路径及两端磁盘摘要。三个负例（缺IL观察、签名无效、缺Native关联）均拒绝，结果native-il-links-results.json。该关联不是NativeImage和IL内容等价证明，不是同次加载字节认证；两端签名/摘要来自后续单独探针。3个动态名称单列，4个加载记录分布在原始进程3948和3936，ETW确认两者是受限PowerShell；与输入读取器Add-Type有关只是实现线索，尚未认证动态内容。无生产代码修改，未联系B、未启动VM；整体formal=false。

从db34bc4全新Git archive复现四个检查器（Native/IL、组合六负例、CLR三负例、受限模块六负例）全部返回0；三份来源清单及原始native回执摘要均匹配。证据combined-fresh-archive-results.json，formal=false。

## 原始模块门槛接入回执前

Invoke-ValidatorRawModuleDiagnostic在terminal receipt前调用真实check-restricted-raw-modules.py并立即核对退出码；七项来源清单包括检查器，持锁贯穿回执。独立openai/gpt-6.1-sol只读复审无阻塞，确认失败在pending清理前抛错。原生PS5.1抽取真实门槛正例通过、缺事件和未知版本拒绝；另用夹具续接receipt/pending行为，正例生成回执清pending，负例无回执且保留pending。该失败注入不是完整控制器失败运行，不冒充端到端负例。

真实受限VM run 698b8bbda9574536bb11e923cfe0d9dc通过：13进程、46程序集配对、106映像及46原始模块配对门槛全部在回执前完成。receipt的Native、Controller、SourceInventory SHA256逐项与实跑文件核对一致；结果raw-module-gate-live-summary.json。首次测试生成命令被自动执行策略拒绝，改为无递归删除的一次性夹具后执行；未绕过拒绝。整体formal=false，动态内容及正式宿主身份认证仍未完成，未联系B。

新增七项来源真实入口拒绝测试：七个单独摘要错配和集合缺项/多项共九例，准确错误均在VM访问及派发前返回；每次摘要失败后，清单和所有源码均可独占只读打开，确认句柄释放。仅入口负例，不涉及新VM运行。证据seven-source-failure-results.json，formal=false。

## 模块字段一致性漏检及待审核修订

新增负例发现：保持身份键不变、只篡改卸载事件IL路径，原检查器仍接受。真实46对模块全部字段一致。已在本地补完整字段一致性约束，增加IL路径、Native路径及标志变化拒绝，正例及九负例通过。真实run 5f69277694e64130ac1f32ad34914dac亦通过，receipt原始证据和来源清单摘要核对一致；VM Off、无pending。

独立官方审核因You’ve hit your usage limit中断，未完成复审。修订检查器、负例及其来源清单保留本地未提交，不将此修复记为已审核发布；本次仅同步问题、实际测试和待审核状态。已发布门槛仍保留该已知漏检，formal=false，不影响恒false正式放行限制。未联系B。

## 原始模块类型边界续测（本地待审）

新增探针确认事件Id/Version及模板Version改为浮点数仍通过，原因是Python字典键整数/浮点相等。已本地要求事件Id、Version、PID及模板Id/Version为严格整数；46配对正例及13负例通过。新版离线复核f26f12ac、698b8bbd、5f692776三份真实历史native全部通过，未将其称为新版再次VM实跑。摘要和结果见raw-module-type-replay-summary.json及raw-module-type-negative-results.json。

事件列表倒序仍通过；当前只验证身份配对及字段一致，原始模块记录无保存时间戳，未声称生命周期时序验证。重复相同模板被字典合并，未发现由此改变解码，不增加无依据的限制。独立审核仍因额度不足未完成；字段及类型修订源码、负例脚本和来源清单继续本地未提交，本轮仅同步测试记录，不发布待审实现。formal=false，未联系B、未启动VM。

## 严格类型准确版本实跑及产物边界

待审字段一致性/严格整数检查器准确版本真实VM run 38fb91ca5e304f9483da78df618c288b通过，13进程、46程序集/模块配对及106映像，来源清单和原始证据摘要与receipt匹配。VM Off、无pending；未联系B。新版来源九类拒绝、回执前门槛及持锁释放复测通过；验证器/Store相关43项通过。

直接调用生产verify_validator_result验收两份真实产物：正例formal恒false，伪造formal通过、绑定漂移、活动残留、超时、bool退出码、诊断摘要篡改、汇总谎报七负例均拒绝；额外构造自洽业务失败，正确保留execution_completed=true、validators_succeeded=false、formal=false。此自洽失败是宿主验收逻辑测试，非客体再次执行失败。脚本check-live-artifact-boundaries.py及strict-types-live-artifact-results.json保留可复核过程。

独立复审仍未完成，待审检查器/负例及来源清单不发布；本次只同步实际测试记录及无生产改动的验收脚本，整体formal=false。证据strict-types-live-summary.json和pending-strict-source-checks.json。

待审三个文件已冻结本地补丁并记录SHA256，反向应用检查及从当前HEAD全新导出副本的正向git apply --check均通过。补丁只保留本地，不发布待审核源码；pending-review-freeze.json提供版本定位。

真实run38fb91ca产物边界续测扩至16负例，新增浮点native退出码、bool活动数、缺TimedOut、顺序倒置、缺结果、浮点validator退出码、非法摘要字符、缺files和自洽超限诊断，全部拒绝；正例及自洽业务失败保留均通过。待审补丁与三个源码摘要复核未变。曾试验从已提交产物派生request回放，因不能证明独立固定请求已弃用该入口；正式保留脚本仍读取原run/input/request.json，不把自派生回放作为绑定认证。无VM运行或B联系，formal=false。

产物诊断字节上限续测：直接调用生产verify_validator_result，ASCII和中文诊断恰好1048576字节均接受，中文1048578字节拒绝；16个既有负例仍拒绝，自洽业务失败状态保留。证据diagnostic-byte-limit-results.json；tests.test_validator_worker五项通过。仅扩展验收脚本，无生产实现修改，无VM运行，未联系B，formal=false。待审模块修订仍未发布；动态内容、加载瞬间字节身份、正式宿主身份接线及依赖B的窗口验收仍未完成。

不依赖B的独立测试批次：全量 `python -X utf8 -m unittest discover -s tests -p test_*.py` 实跑1212项，0失败、0错误、3跳过，207.360秒；结果见validation/remaining-tests-20261105/independent-final-regression.log。PowerShell四项check-root-trace-cases、check-seven-source-failures、check-source-locks、check-raw-module-receipt-gate均退出0，结果见independent-powershell-batch-results.json。Python证据复核10项中9项退出0，包含CLR、组合证据、Native/IL关联、ETW、根身份、原始模块负例/正例及产物边界；结果见independent-batch-replay-results.json。check-source-inventory.py唯一失败：历史五项清单与当前控制器/runner字节不匹配，保留失败，不修改历史清单。历史版本f0093dd复放同样不匹配。整体formal=false，未联系B、未运行VM；待审三个模块修订仍未发布。

历史来源失败已定位：root-gate清单对应旧版源码及Windows CRLF字节，而非当前控制器。新增check-source-inventory.py --source-directory入口，在一次性目录从Git历史重建五项准确字节；先逐项SHA256匹配，再核对原回执ControllerSHA256，正例通过。内部五摘要负例及逐个文件实际加字节的五负例均拒绝。证据historical-source-byte-reconstruction.json保留每文件提交、字节表示和摘要。不修改旧清单/回执，不将历史通过当作当前来源认证；前轮失败记录保留。修正前轮归因：历史直接导出失败也涉及CRLF表示，不仅是版本差异。formal=false，未联系B、未运行VM。

不依赖B续测：严格回执、argv、最终结算、提供方、验证器、producer兼容及诊断8组167项通过（38.562秒）；auxiliary_v14另38项通过（3.393秒）。新增本地PS5.1动态读取器探针：锁定实际WindowsSafeInput.cs，使用同次读取字节编译，记录源码SHA256、程序集名、MVID和3个托管方法IL摘要，实际读取[]正确，持锁写入拒绝。首次探针误预期4个托管方法而失败，核对源码后修正为实际3个，重跑通过；P/Invoke方法没有IL，不计入。证据local-dynamic-reader-results.json。不认证JIT机器码、历史动态模块或官方宿主身份；formal=false。剩余动态内容/加载时字节/正式Host→Store不是仅待跑测试，而是尚缺可信采集和接线实现，不能以此次探针宣布完成。独立复审额度阻塞仍在，三个旧修订不发布，未联系B。

继续完成不依赖B的验证：Core、Integration、Transport及回执提供方相关本地单元测试共85项通过、0失败、0错误、3项既有跳过（25.471秒）。这些是本地fixture单元测试，不等于真实CLI、真实Transport或原B窗口。Host→Store实现复核确认record_validator_run固定写入formal_checks_verified=false，settle_audit在缺正式回执时保留needs_validation；167项严格回执/绑定测试和38项auxiliary_v14已通过。当前正式接线缺口是可信宿主终态与验证器官方回执的接入，不是测试未运行。未联系B、未运行VM，formal=false。
