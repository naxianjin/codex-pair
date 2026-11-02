# 2026-11-02 新电脑环境核对与官方CLI安装

用户允许先完成可以独立进行的工作，并授权安装CLI。本轮核对迁移后的项目、安装日常开发工具并验证命令入口；没有执行候选、启动协作、安装候选skill或开展正式验收。

## 项目与候选

- 当前候选工作树仍为 `feat/next-recovery-20261101`，Git基准为 `1e3addf80b588904bf3da9eb66bc7afc4dffbb44`。
- 对交接包的184个候选文件逐项核对SHA256，与新电脑现有工作树全部一致；旧主目录的独立未提交改动保留。
- 本机Python 3.13.17对112个Python文件完成仅编译、不执行的语法检查；没有导入候选模块。Python自带SQLite版本为3.50.4。
- 候选发行清单的110个文件摘要全部匹配。本记录是环境检查文档，不进入当前冻结运行清单。
- 已安装skill的SKILL.md仍为 `a6c989899b49cda549b6915081e10960373ef9fcefc7924b1e3c084806bc923a`；未替换现有安装。

## CLI及开发工具

| 命令 | 实际版本 | 本轮操作 |
| --- | --- | --- |
| `codex` | 官方0.164.0 | 通过WinGet的OpenAI.Codex安装便携包，补充用户bin中的直接启动入口 |
| `codex-pair-cli` | 官方0.166.0-alpha.5.1 | 为电脑已有的官方桌面应用CLI登记固定启动入口，不复制或修改二进制 |
| `git` | 2.56.0.windows.4 | 安装Git for Windows；实际落点为系统Program Files |
| `python` | 3.13.17 | 安装Python Software Foundation发行版，解释器位于用户Programs目录 |
| `rg` | 15.2.0 | 通过WinGet安装便携版ripgrep |

来源：CLI包为OpenAI官方GitHub Release；Git来自git-for-windows；Python来自python.org。WinGet报告下载包摘要验证成功。没有使用定制CLI。

两个CMD启动入口只调用对应官方exe并透传参数，不设置模型、权限或CODEX_HOME，也不分配A/B角色。用户需要关闭旧终端并重新打开，使更新后的PATH生效。配对入口固定到当前桌面应用CLI路径，应用更新后须重新核对文件和版本，不能静默切回另一版本。

按注册表中的机器及用户PATH启动无配置文件的全新PowerShell子进程，五个命令的版本查询均退出0；两种CLI的 `login status` 均报告现有ChatGPT登录可用。没有发送模型请求或打开新的可见CLI会话。登录状态检查不是模型请求、权限工具执行或真实双账号协作通过证明。

本轮按用户范围请求WinGet安装。Git安装器实际安装到 `C:\Program Files\Git` 并增加机器PATH；这与开始时预期的纯用户目录不同，按实际结果记录，不声称所有工具均为用户级安装。

## 安装中遇到的问题

1. 下载官方PowerShell安装脚本的命令被自动审批拒绝，返回 `blocked by policy`，未提供具体原因；改用有固定源与摘要检查的WinGet包。
2. 用户级CLI默认安装的VC运行库依赖无法匹配，返回 `No suitable installer found for manifest: Microsoft.VCRedist.2015+.x64 version 14.51.36247.0`。之后先安装ripgrep，再使用WinGet便携CLI并跳过该依赖安装。没有安装或修改这项系统级运行库；CLI基础启动与登录状态检查通过，完整运行能力尚未验收。
3. WinGet报告命令别名已添加，但预期Links路径没有codex.exe。实际官方入口为包内 `codex-x86_64-pc-windows-msvc.exe`，因此补充直接调用它的CMD启动器并加入用户PATH。启动器不改写exe或账号配置。
4. 协议导出检查的首个PowerShell命令因 `An empty pipe element is not allowed` 未执行，修正管道语法后成功。最初新终端版本检查使用提前截断输出的管道导致非零退出；改为完整读取输出后，五个真实命令均正常。两者属于检查命令问题，不计作产品测试失败或通过。

## 版本差异与控制边界

从新安装的官方0.164.0导出了实验JSON schema：`HooksListParams`只有cwds，没有候选使用的threadId；后台终端列表则包含threadId和分页字段。普通CLI可使用，但不能把它直接认定为候选完整控制接口兼容。

已有官方0.166.0-alpha.5.1二进制摘要与交接包先前生成协议时记录的文件摘要一致，故保留专用启动入口供之后按准确版本验证。摘要相同和schema存在不代替Hook信任、实际加载、队列通信或工具控制的行为验收。候选 `full_tool_control`仍为false。

## 账号与配置保护

现有默认配置目录通过用户目录junction指向原配置存储。本轮没有移动该目录或复制账号登录材料。auth.json的安装前后摘要一致；hooks.json原本不存在，之后仍不存在。桌面应用CLI二进制摘要未改变。

config.toml在本轮期间摘要发生变化，第一次独立CLI调用前已观察到该变化；本轮命令没有写入该文件，来源未确定。保留当前文件，没有按猜测恢复旧配置，也不声称全部配置字节未变。没有输出认证内容、用户身份详情或私人配置正文。第二账号的独立登录与会话绑定尚未验证。

## 隔离环境的阻塞

新电脑为Windows 11家庭版中文版、64位、约31.5GB内存，系统报告HypervisorPresent=true。Hyper-V管理模块不可用，未找到vmms/vmcompute服务；可选功能查询提示“请求的操作需要提升”，所以没有把该查询算作功能状态验证。

微软文档明确Windows家庭版不支持安装完整Hyper-V角色。虚拟机监控器存在不能证明本指南要求的Hyper-V Windows客体可用；也不据当前CIM虚拟化字段推断BIOS需要重新开启。[微软Hyper-V安装要求](https://learn.microsoft.com/en-us/windows-server/virtualization/hyper-v/get-started/install-hyper-v)

本轮没有升级系统版本、下载Windows镜像、启用系统功能、修改BIOS、重启、使用非官方开启脚本或替换隔离方案。仍需合适的Windows版本/主机及客体，再按指南第25.8节准备可信执行器、断网、只读输入、scratch、资源限制和受控证据提取。

本地脱敏检查结果保存在项目 `.codex-pair/artifacts/new-machine-20261102/`。私人配置摘要基线仅保留本地。210个候选行为规格仍未执行；正式验收、迁移、候选安装和运行切换保持待验证。
