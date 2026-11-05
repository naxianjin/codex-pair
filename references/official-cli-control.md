# 官方CLI控制范围

用户已明确“只用官方的”。本skill对接官方CLI，保留当前账号、模型、权限和用户选定的可见窗口。当前目录仍是静态候选；本文说明已经补写的接口及边界，不证明已完成真实验收或安装。

## 支持的入口

| 入口 | 候选行为 | 不能据此推断 |
| --- | --- | --- |
| 本地`PreToolUse` | 按原会话、绑定和控制代次拒绝新的受支持工具 | 全部托管和特殊工具均已覆盖 |
| `UserPromptSubmit` | 核对本协作的准确排队消息；暂停、旧绑定和已领取/完成的重复消息不开始处理 | 普通用户文本已经授予恢复权 |
| `PostToolUse` | 保存返回记录，暂停时追加说明，保留原工具输出 | 原子进程已退出，或模型已经停止 |
| `Stop` | 暂停时抑制该停止点的自动继续 | 可以撤回已运行的工具 |
| `Interrupt` | 记录准确A中断，超时按官方上限3秒配置 | 已证明具体按键，或其他窗口已停 |

处理器命令固定`--event`。即使输入JSON解析失败，Stop也返回明确停止输出，不能用会触发继续的退出码。PreToolUse仍使用其支持的拒绝方式；不混用两类返回协议。配置更新后需要用户重新核验信任，不修改已有审批策略。[官方Hooks说明](https://learn.chatgpt.com/docs/hooks)

排队消息只认可数据库中完全相同的原envelope、pair ID、message ID、收件人、双方binding_version以及尚未领取的状态。原消息仍需按正常协议claim；Hook不代领、不完成任务、不重投。暂停中普通用户输入不被当作已排队业务，也不自动解除控制门禁。

## 原执行器查询

`inspect-control-capabilities`复用新增命令的公共参数，显式提供`--codex`及`--input-file`（内容为`{}`）。它查询原绑定所在的共享daemon：

- `hooks/list`使用准确`threadId`，核对事件、命令、同步执行、匹配范围及信任状态。只把定义记为`trusted_definition`，行为验证仍为false。
- `thread/backgroundTerminals/list`读完所有分页，只保留原进程/工具ID，不保存命令正文或操作后台进程。实验接口协商不会放开其他RPC。
- 原daemon不存在、线程未加载或接口不兼容时返回不可观察；不能启动一个空的新app-server，把其没有后台进程当成原窗口已经空闲。

该入口只查询元数据，不创建、恢复或中断模型回合，不更改权限。严格只读数据库诊断仍用`doctor`；这里的实时原生查询与数据库快照分别标注。

## 明确保留的限制

官方Hook不覆盖托管工具、已有unifiedexec的后续输入及部分特殊路径。状态保持`full_tool_control=false`；缺少原进程可见性时，后台终态为unknown。严格全组暂停/恢复不能因局部Hook定义受信就放行。

本机官方CLI `0.166.0-alpha.5.1`的协议导出包含`PreModelRequest`/`PostModelRequest`，但枚举不证明具备阻断能力。2026-11-01读取的[官方源码](https://github.com/openai/codex/blob/main/codex-rs/hooks/src/events/model_request.rs)将它们作为观察事件，输出不参与控制流；当次文件blob为`058d0b7016a7e04b57688e700748acb8dc6f653d`。该源码读取也不能替代本机准确构建的行为验证。因此本候选不安装这两个事件作为暂停开关，不用长时间阻塞观察Hook来模拟可靠门禁。

## 检查与验收

本轮读取了官方CLI帮助并导出协议，核对原生字段；候选只进行了语法、结构和源码审查。排队开始、Hook异常、后台终端分页和真实恢复的行为用例已经编写，尚未运行。Hyper-V及正式验收继续等待用户通知，当前安装版不替换。
# 六项静态审查后的入口修正

任务级暂停通过准确原回合和当前消息绑定进入PreToolUse、Stop及PostToolUse检查；暂停任务所属窗口的回合无法关联时保留阻塞，其他准确绑定的无关任务不因此变成全组暂停。PostToolUse仍只保存结果并提示，不宣称能够中断模型。

管理命令只接受独立字面量参数，保留Windows反斜杠、中文、空格和单引号转义；拒绝组合命令及插值，继续核对解释器、脚本、协作、状态根目录和成员。上述修改仅完成静态候选，行为验证仍延期；完整原生工具控制仍为false。


## 2026-11-05 本地 WebSocket 原执行器查询候选

当前Windows主机的AF_UNIX创建返回10050；原窗口迁移到官方loopback WebSocket服务后，第二只读连接已实际查询原B的idle状态及后台终端空列表。此结果只证明查询可用，不证明完整工具控制或恢复已通过。

inspect-control-capabilities 输入可显式提供 `connection_file`，指向启动脚本生成的本地连接记录。记录包含endpoint、原session、codex_home和临时token_file路径，不含令牌正文。适配核对原绑定与服务返回的配置目录，地址仅接受ws://127.0.0.1及明确端口；使用capability令牌认证且不发送Origin头。依赖已安装的websocket-client；缺失时明确报错，不回退到另一个账号或新执行器。

该入口只允许initialize、thread/read、hooks/list、thread/backgroundTerminals/list及身份核对所需account/read。禁止thread/resume、turn/start、turn/interrupt和审批操作；关闭观察连接不终止服务。连接记录须显式传入，不扫描临时目录猜选运行时。候选已接入显式登记及自动恢复读取；活动安装与桥未切换。


原A可通过 `bind-control-connection` 输入 `{"target":"B","connection_file":"准确连接记录绝对路径"}` 登记连接。登记前实际查询并复核原成员、配置和CLI摘要；登记只保存路径及准确session/binding_version，不保存令牌、不解除暂停。ProductionRecoveryAdapter消费该记录时仍复验session/binding_version；没有记录沿用原shared查询，不扫描或自动选择临时服务。完整工具控制仍明确不支持。
