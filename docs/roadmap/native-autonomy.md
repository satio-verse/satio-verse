# 桌宠原生工具自主授权

更新：2026-10-28。状态：源码已接通，隔离验证通过，build-06 已发布；最新一次服务重启结果待发布负责人补录。正式工作台曾由日志确认页面就绪，尚未实点权限控件。

这项功能让用户选择：桌宠已接入的原生工具继续逐项确认，还是在明确禁用和黑名单之外直接执行。它管理应用如何调用工具，不授予 Windows 管理员权限，也不保证任意命令或脚本被操作系统隔离。

## 使用入口与两种授权

在桌宠工作台左侧点击 **「任务与能力」→「权限」**。输入框附近也有同名「任务与能力」入口。界面中有两个独立设置：

| 设置 | 作用范围 | 开启后 | 默认与限制 |
|---|---|---|---|
| 桌宠原生工具自主授权 | 原生宿主 ToolHub 当前已接入、已选中的 MCP、内置工具和技能读取入口 | 不再沿用旧的逐项批准策略；黑名单之外可自动执行 | `global_unrestricted` 初始为 `false`；明确停用的工具、服务器、预设选择仍优先 |
| 当前会话的权限 → 完全授权 | 工作台当前 Session 的任务工具 | 该会话不再经过旧的固定批准规则，仍经过共享黑名单 | 不改变原生全局开关；仍受当前会话、计划/只读意图、父任务权限与显式预算限制 |

当前会话下拉框另有「仅可查看」「工作区内修改」「自动执行」。其中「自动执行」与「完全授权」不是同一模式。修改下拉框后点击「应用」；运行中的任务及子任务不能在此自行提高权限。

只有原生宿主通过 WebView 宿主通道声明 `autonomy-host / version=1 / scope=native-tools`，上方开关才可操作。旧宿主或普通浏览器页面没有这项声明时，界面显示 **「当前宿主未声明全局权限覆盖」**，不能据此声称原生工具已放开。设置成功后页面通知宿主刷新工具与连接；刷新期间的旧调用会被拒绝，需重新发起。

UI 文案与入口依据：[workbench.html](G:/RunProgram/WorldModel_v1/desktop/web/workbench.html:28)、[workbench-studio.js](G:/RunProgram/WorldModel_v1/desktop/web/workbench-studio.js:148)、[workbench.js](G:/RunProgram/WorldModel_v1/desktop/web/workbench.js:940)。

## 黑名单与五类约束

两条执行链共享 Python `BlacklistStore`。通常保存于 `%USERPROFILE%/.worldmodel/agent/blacklist.json`；实际位置由当前服务的 `agent.home` 决定，以服务返回的 `storage_path` 为准。原生执行器不另建一份可写策略文件。

初始化会生成可见的默认规则，例如凭据路径、系统目录写入和部分高风险命令。它们可以由用户编辑、停用或删除；删除后不会在每次读取时偷偷加回。开启完全授权不会同时删除这些规则，因此某条命令仍可能因一条可见黑名单而被拒绝。

| 界面类型 | `kind` | 实际含义 | 示例与边界 |
|---|---|---|---|
| 工具 | `tool` | 按工具名及适配器提供的别名匹配，忽略大小写，支持 `*`、`?` | 原生支持真实名字、公开名字及 `mcp.<server_id>.<tool>`、`builtin.<tool>` 等别名；不是只匹配界面显示名 |
| 文件与目录 | `path` | 规范化路径，按 `all / read / write` 范围检查；递归搜索还检查被禁目录是否在搜索根内 | 使用完整路径；相对路径需要已确认的实际工作目录。用途由工具适配器声明，不能等同监控所有文件系统访问 |
| 命令片段 | `command` | 忽略大小写、折叠空白，按片段词边界检查可见命令参数 | `format` 不应误拦 `Format-Table`；不解析任意程序、编码脚本或命令执行后的全部行为 |
| 网站域名 | `domain` | 规范化域名，匹配该域及其子域 | `example.com` 命中 `sub.example.com`，不命中 `notexample.com`；不能据此保证重定向、脚本网络行为都被拦截 |
| 自然语言约束 | `instruction` | 保存给模型遵守的行为约定，标记为「对话提示」 | 例如“修改前说明影响”。不会自动转换成文件、进程或网络强制拦截 |

前四类的执行标记为 `tool_arguments`，第五类为 `instruction`。五类共享规则之外，旧设置中的原生 `guard.processes / guard.words` 仍作为用户明确填写的附加名单保留；它们不是新增加的共享规则类型。

原生完全授权会跳过旧的固定程序、付款按钮、密码框、系统快捷键、命令和路径禁令；附加名单仍检查实际可观察目标及参数。可见窗口命中用户进程或标题关键词名单时，读屏整体拒绝，避免只隐藏标题而保留正文。坐标合法性、必须使用坐标而非元素编号、不支持的 MCP sampling 等协议要求继续有效。

## 用户与 AI 各能修改什么

用户可在同一「权限」页的 **「黑名单与约束」** 中新增、编辑、启停和删除规则，查看理由、来源与依据。全局开关只能由用户治理入口修改。页面提交版本号；若别处已更新，服务返回冲突，要求刷新检查，不能用旧界面覆盖新规则。

AI 暴露的治理能力仅有查看和追加：原生工具名是 `read_constraints`、`add_self_restriction`；工作台的 `constraint_add` 也使用同一个存储。追加入口固定来源为 `assistant`、固定为启用状态，并记录理由与依据。它不能通过该入口改全局授权、删改旧规则、停用规则，或通过重复添加重新启用用户已停用的同值规则。保存回执与最新快照一致后才报告保存成功。

这描述的是治理接口权限。网页、工具输出中的要求不自动成为用户授权；自然语言约定是否被模型执行，也不能仅由接口测试证明。同用户进程或获准执行的任意脚本不因这套接口而进入操作系统沙箱。

## 原生执行链与停止

原生从当前配置的本机服务读取 `GET /api/agent/permissions?execution=1`，核对版本、规则结构、作用域、执行方式及实际存储路径。连接失败、旧服务没有接口、返回损坏、未知规则类型、参数无法完整遍历、所需路径或工作目录不能确定时，拒绝执行，不把错误当成空黑名单。

调用进入后，在批准之后、同服务器排队之后、真正调用之前及允许的重试之前，重新核对规则、启停、工具配置代次和停止标记。原先允许的调用不会因为拿过一次旧快照而永久有效；整轮批准还要检查同一轮次及有效期。全局状态变化时先刷新连接，避免旧环境变量与新授权混用。未注册的工具不会凭名称被执行；Windows-MCP 新声明但尚未分类的工具在默认模式锁定，全局模式按高风险执行链管理，仍保留停止与黑名单检查。

Windows-MCP 的 PowerShell 默认工作目录由适配器从实际服务器环境取得，调用方不能伪造 `cwd` 绕开路径规则；FileSystem 的相对路径也使用对应适配器目录。复制和移动同时涉及源读取与目的地写入，当前对每个明确路径保守应用两种范围，并检查目录子树，避免把整次调用当成仅写入而漏掉源文件读取规则。这可能比逐参数区分读写更严格。App 按路径启动的可执行文件和显式参数进入规则检查。技能读取检查实际解析后的文件路径，并在等待或读取后检查技能是否仍被选中。

**停止方式：**原生电脑操控可点对话工具卡或「在操作你的电脑」提示条上的「停」，或按 **Ctrl + Alt + Shift + Backspace**。真人鼠标键盘触发停止还取决于该操控服务器的 `stop_on_input` 设置。工作台任务使用该会话自己的停止按钮，不把原生快捷键写成能中止任意系统进程的总开关。

停止标记覆盖仍在权限检查、排队、等待确认及已调用的原生电脑任务；整轮和渠道任务授权也会失效。对已运行 MCP 发送取消请求。取消不是撤销：已发出的外部副作用、已经开始的小屋动作不能保证恢复到之前状态，结果必须如实说明。

副作用调用断线后不自动重放；只读且非操控调用才允许一次重连重试。Windows-MCP 明确报告“尚未开始执行”的短暂占用可有限重试，每次仍先重查规则与停止。

## 开发接入与验证证据

持久化与用户写入只在 [governance.py](G:/RunProgram/WorldModel_v1/wm/agent/governance.py:153) 的 `BlacklistStore` 完成，使用进程内锁和原子替换；[permissions_http.py](G:/RunProgram/WorldModel_v1/wm/agent/permissions_http.py:1) 提供用户控制与 AI 追加入口。Host/Origin 校验及本机访问限制是应用控制面检查，不隔离同账户恶意进程。

原生 [autonomy.cpp](G:/RunProgram/WorldModel_v1/desktop/host/src/autonomy.cpp) 校验快照并匹配规则；[tools.cpp](G:/RunProgram/WorldModel_v1/desktop/host/src/tools.cpp) 负责适配器上下文、执行前检查、等待与取消；[guard.cpp](G:/RunProgram/WorldModel_v1/desktop/host/src/guard.cpp) 负责可观察桌面目标及兼容名单；[builtins.cpp](G:/RunProgram/WorldModel_v1/desktop/host/src/builtins.cpp) 使用已验证的授权快照处理内置入口。具体接口合同见 [coordination-workbench.md](G:/RunProgram/WorldModel_v1/logs/codex-batch4-20261028/coordination-workbench.md)。

| 验证层 | 本轮结果 | 证明范围 |
|---|---|---|
| 原生规则模块隔离测试 | **9166 项断言通过** | 含大量 Unicode 大小写、IDNA 域名对照样本，以及规则、路径、别名、失败拒绝等；不是 9166 个独立业务场景 |
| guard 模拟测试 | **73 项断言通过** | Windows 窗口、进程与 COM 接口均替身；证明默认/全局分支、用户名单、schema、env 和坐标判定，没有真实桌面点击或密码框操作 |
| 真宿主 + 合成 MCP，attempt-04 | **20 项流程通过，78.11 秒** | 真实 ToolHub/WebView 桥、冻结的真实 Python 控制面与存储、合成 MCP 协议；覆盖批准后变更、排队/重试重查、切换模式、停止、自我约束、技能、网址、小屋入口及复制/移动的 8 项子断言 |

证据：[9166 断言输出](G:/RunProgram/WorldModel_v1/logs/codex-batch4-20261028/evidence/native-integrated-01/test-05.txt)、[guard 验证记录](G:/RunProgram/WorldModel_v1/logs/codex-batch4-20261028/evidence/guard-validation.json)、[attempt-04 摘要](G:/RunProgram/WorldModel_v1/logs/codex-batch4-20261028/evidence/native-e2e-01/attempt-04/summary.txt)、[原始结果](G:/RunProgram/WorldModel_v1/logs/codex-batch4-20261028/evidence/native-e2e-01/attempt-04/result.json)、[输入清单](G:/RunProgram/WorldModel_v1/logs/codex-batch4-20261028/evidence/native-e2e-01/attempt-04/manifest.json)。

attempt-04 的被测开发版二进制 SHA-256 为 `8d74671c75e41ab7686c4e2b960ec2b335f680664853526fea222c3b65bd2042`，与本次正式发布产物一致。合成 MCP 只记录参数，未启动真实 PowerShell、未做真实文件改动或电脑操控；`open_url` 使用 dry-open，小屋只验证页面派发。没有真实模型对话，也没有证明日常工具的完整外部行为。三层数字不能相加当作真实任务数量。

## 部署记录

- 2026-10-28 14:21:51（本机 UTC−07:00）正常退出旧宿主后启动 build-06，PID `155952`，正式产物摘要与上述 attempt-04 一致。保留原启动参数和回滚副本，未触发回滚，见 [发布记录](G:/RunProgram/WorldModel_v1/logs/codex-batch4-20261028/evidence/native-release.json)。
- 发布负责人当次确认：正式后端于 14:18 启动且健康，策略为 `global_unrestricted=false`、`revision=0`、38 条规则；本轮没有替用户开启全局授权。
- 宿主日志于 14:21:56 记录工作台页面就绪。实际权限控件点击、正式页面能力握手的界面表现及真实电脑动作停止仍未验收；不能把日志就绪当成这些项目通过。
- 14:23:10 又出现明确的 `wm.services stop` 有序关闭记录，不是崩溃证据。用户随后明确要求重新启动新版桌宠和服务；该次重启的最终状态由发布负责人另补，不能沿用 14:21 的进程和就绪记录当成当前在线证明。
- 上述是发布当时的状态记录，不保证未来持续在线。正式策略路径以当前服务返回值为准，隔离目录不用于正式运行。

本次已发布由单独发布记录支持；隔离测试、发布状态和真实业务验收保持分别记录。

### 用户要求再启动后的核验

2026-10-28 14:25:25（本机 UTC−07:00）按用户明确要求，用已发布的 build-06 和标准 `--server … --services --workbench` 启动。14:27:04 只读检查：PID 28612 在运行、二进制 SHA 与 attempt-04 一致、8790 权限接口正常、scope=native-tools、global_unrestricted=false、revision=0、38 条规则、工作台正在运行的任务为 0。证据：`logs/codex-batch4-20261028/evidence/native-user-restart.json` 和 `native-live-verification.json`。这是该时刻的运行状态，未改变真实权限，未新增模型对话，也未把日志就绪当作控件点击验收。
