# CLI变化后的只读预检

本模块是 schema 14 静态候选的一部分。本轮仅编写与语法分析，尚未执行候选或兼容性测试，不能据此宣称新CLI已兼容。

## 只读检查与持久登记分开

`compatibility_probe(executable, *, expected_binding=None, trusted_executable=None, observer=None, previous=None, force=False, timeout=5)` 不实例化 Store、不建库或迁移，不消费消息、邀请、审批、检查点或恢复许可。它不创建模型会话、发送测试消息或修改配置。模块不打开认证文件、不采集认证响应、不记录凭据；官方元数据连接沿用原配置。

`executable` 必须是明确的绝对原生 `.exe` 路径。先读实际路径、文件身份、大小、修改时间和 SHA-256；不从 PATH 同名文件或类似版本号推断兼容。没有既有可信来源记录时，仅返回文件事实及 `not_verified`，不会执行这个二进制。已有可信记录准确匹配后，最多查询 `--version`、`--help`、`app-server --help`，默认每次5秒、允许1—15秒；不运行任何产品任务。子进程仅获得 Windows 启动所需环境白名单及准确 CODEX_HOME，不继承API密钥或令牌。报告保存退出状态、摘要及必要字段，不复制CLI原始诊断或会话正文。

实时身份从调用方已有的只读官方元数据连接读取，限定 `thread/read` 与一页 `thread/turns/list`。核对准确UUID、CODEX_HOME、cwd、模型和提供方；不翻阅整个聊天历史。若无法把该观察连接的实际原生进程、文件身份关联到被检exe，则标记 `metadata_observer_executable_not_verified`，不能用旧CLI的查询结果证明新CLI兼容。

环境指纹覆盖exe身份、所用适配器文件摘要、系统/Python环境和准确成员绑定。相同指纹复用原CLI帮助查询，重新核对当前身份和门禁，不重复启动模型诊断。只有变化才发一条新的兼容性变更事件。

## 首次来源与已有基线

没有可信exe记录时不能从本次计算出的摘要反过来证明该文件可信。显式刷新层可使用已经完成原CLI绑定核验的 `AppServer(track_producer=True, metadata_only=True)`，调用内部入口：

`_register_original_cli_baseline(member, session, cwd, observer, *, executable)`

该入口只接受实际受管 AppServer 对象及其原生启动证据，核对前后进程/文件身份和准确原会话，再首次保存：

`source_reference = original-visible-binding:<uuid>:<binding_version>`

这表示来源是原窗口的实际执行器和绑定核验，不是数字签名或上游认证。JSON中的 `verified=true`、手填路径或文件摘要不能调用这条内部来源通道。已有exe基线不同即拒绝覆盖；升级后的新路径、文件身份或摘要需要固定适配候选、独立审核及适用验证，不能借首次登记流程覆盖旧记录。

`refresh_cli_compatibility(member, session, cwd, executable, *, observer=None, force=False)` 是单独授权的持久写入口。它读取已有来源，在事务外预检，落库前复验成员及配置没有改变。`doctor` 或其他严格只读诊断不能调用它。显式刷新与守护层可以登记结果，但预检本身仍不保存。

## 能力与门禁

- `observed`：本地文件、版本或某种字段确实被查询到。
- `declared`：CLI帮助声明了入口，只证明帮助中存在它。
- `known_local_adapter`：该准确版本在当前安装代码的适配范围内，仍需按每次执行检查真实回执。
- `preflight_ready`：该操作所需的本轮预检项齐备，不代表授权、模型执行、通信或业务通过。
- `not_verified`：缺来源、身份不符、未知版本、字段未观察到、查询失败或exe发生变化。

`_cli_compatibility_gate(db, member, operation)` 按依赖分别阻止 `new_delivery`、`approval_execution`、`review_settlement`、`runtime_switch`。缺少实际工具字段时，不把可查会话等同于可以结算审核。无依赖的已授权本地工作不受该模块统一暂停；用户暂停、范围收尾、权限、额度及其他门禁仍然有效。

所有结果中的 `communication.status` 保持 `not_verified`。通信实际到达、完整阅读及业务处理必须由原正式握手和真实执行证据证明；不能靠帮助字段或进程存在代替。

## 在途操作

`bind_cli_execution(...)` 在实际开始前固定当时的exe、版本、适配摘要、契约、会话、执行ID及回合。只允许给原来尚未记录回合的准确执行补入该回合，不允许换版本、换契约或换会话重绑。新的兼容性结果不会改写旧执行记录。

`inspect_cli_execution(...)` 只检查原exe是否仍是同一文件。原路径消失或字节变化时返回 `needs_validation` 和结果 `unknown`，保留原记录；不自动改用新CLI、不重放、不终止原进程。下一步是核对原准确回合和工具终态，而不是重复执行。

## 接线与待验证

Store混入 `CompatibilityMixin`。显式刷新可首次登记实际原执行器来源；守护层的启动、重新连接、exe或环境变化事件只复查已有记录。派发、审批执行、正式结算和运行切换调用各自操作门禁；入口真正执行时继续使用原准确回执验证。当前没有可信来源或官方元数据连接时，保留 `not_verified`，不要静默设为通过。

`safe_preflight(store, executable, observers=None, member_id=None)` 已从 `pair_windows.bridge` 启动路径调用。无既有基线时，只有调用方提供的真实原绑定观察器能够首次登记；默认不会为未知exe先建观察器，再把自己的查询说成信任依据。已有基线准确匹配时才创建只读观察连接。控制桥存活不等于允许新投递。

`CompatibilityMonitor(..., interval=30).poll()` 供同一守护循环调用。文件身份提示或成员绑定变化立即复查，其他情况默认每30秒只读核验；相同版本/环境指纹复用原CLI帮助结果。30秒是候选实现的检查间隔，不是已经测得的真实发现延迟或性能保证。

`tests/test_compatibility_v14.py` 编写了无信任不运行、相同指纹去重、路径/版本/模型变化、旧观察器不能证明新exe、缺工具字段、凭据不进入报告、首次来源不能覆盖升级、在途记录不改绑等隔离测试。本轮没有执行这些测试，也未完成真实CLI升级或在途切换验收。
