# 工作台近期改动审查与移动端边界

日期：2026-10-31。范围：10 月 29 日之后的工作台工具路由、附件预览、扩展快照及新增持续任务调度。只提取工程相关会话摘要；没有复制私人聊天、凭据或用户任务输出。没有重启正式程序或调用真实模型、社交服务。

## 近期实际改动

从本地会话 `20261030-011453-850f`、`20261030-194307-a5f4` 中的工程摘要和源码交叉核对，绫音做了以下工作：

- 在 `E:/Ayane_Use/tools/workbench-plugins/` 增加 clock、jobs、goal、todo、schedule、memo 六个 Creator 扩展，合计 27 个直通工具。
- `wm/agent/direct_tools.py` 读取 `extensions/direct-tools.json`，仅暴露与当前启用修订相符的快照；`studio.py` 执行这些工具。
- `core.py` 将直通工具纳入 Studio 分发，避免落入文件工具分支。
- 增加会话附件图片预览和相应的工作台缩略图入口。
- `core.py` 的历史折叠阈值调整至 1,000,000 字符。这是字符计数，不等同模型 token 预算；本次遵循用户已有选择，没有直接改回旧阈值。

10 月 31 日另一个开发线程正在修改 room、pet、host、pipeline。本审查没有覆盖或覆盖写回那些进行中的修改，也没有用 10 月 29 日备份替换现有源码。

## 已修复并验证的问题

| 问题 | 触发与影响 | 修复位置 |
| --- | --- | --- |
| 附件预览可能提供同源可执行文档（P1） | 旧接口按扩展名返回任意附件类型；HTML/SVG 可成为应用同源文档，且绕过了附件路径、大小、哈希校验。 | `wm/agent/studio_http.py:309`：复用 ContextStore 的不可变附件验证；按文件实际签名仅允许 PNG/JPEG/GIF/WebP/BMP；增加 nosniff、sandbox CSP、同源资源策略与 no-store。 |
| 直通工具绕过用户的通用 MCP 黑名单（P1） | 仅检查直通工具名时，用户针对 `mcp_call`、`mcp_connect` 或启动命令设置的规则失效。 | `wm/agent/studio.py:119`：加载当前有效扩展服务器，依次检查启动、通用 MCP 调用及具体工具规则。完全权限模式仍可自动执行，显式黑名单保持一致。 |
| 直通工具名称冲突与快照格式问题（P2） | 扩展同名工具可能覆盖内置分发；重复名称取决于快照顺序；空修订或错误字段可能被接受。 | `wm/agent/direct_tools.py:25`：保留内置名；所有歧义重名均不暴露；校验非空修订、工具名、schema、布尔 read_only。 |

`tests/test_agent_recent_security.py` 的 12 项回归在修复前全部失败、修复后通过。与 direct_tools、studio、studio_http、extensions 的联合回归合计 **78 passed**。只使用临时注册表和离线 MCP 测试服务。

## 持续任务调度的独立审查

新增 `wm/agent/resident.py` 使用 SQLite 保存明确创建的持续目标及事件，不自动导入旧 schedule 提醒。独立审查补齐：

- 费用以专用会话累计费用加所有子任务费用计算，跨轮次预算不会漏算子代理；重启时核对累计费用。
- 每轮绑定具体 run 对象和 run_id。排队的人类新请求不能替前一轮报告目标状态，也不会被误认为前一轮结果继续调度。
- 执行被取消时记录 interrupted，避免永远停留在 running。
- 服务异常退出后把原 running 转为 interrupted，不自动重放结果不明的外部操作。
- 用操作系统文件锁保证同一数据目录只有一个调度器，避免第二进程错误恢复第一个进程正在执行的任务。

相关位置：`resident.py:51`（所有者锁）、`:166`（费用）、`:175`（轮次报告）、`:190`（恢复）、`:254`（执行）。

`tests/test_agent_resident_review.py` 使用真实 AgentHub、临时数据目录和替代模型调用的确定性 `_turn`，覆盖以上五类情况，**5 passed**。这不表示操作系统休眠、断电或外部服务调用具有事务性；无法确认结果的动作仍需核对产物后继续。

## workbench-jobs 源码修复与发布状态

原扩展有三个明确问题：

1. 停止任务只判断 PID 是否存在。任务退出后 PID 被系统复用，会可能停止无关进程。
2. `jobs.json` 虽然原子替换，但整个读取、修改、写回没有跨进程锁；并发 MCP 调用会覆盖彼此的新记录。
3. 每次读取日志先全量读入，再统计所有行；日志很大时可阻塞或耗尽内存。

已在 **源目录** `E:/Ayane_Use/tools/workbench-plugins/workbench-jobs/` 修复 `server.py` 并更新 `SKILL.md`：

- 新任务记录 PID、GetProcessTimes 创建时间和启动命令；停止前核对三者，并保持已验证进程句柄到停止结束。旧记录缺少身份时拒绝只凭 PID 终止。
- 所有五个工具用操作系统文件锁保护完整事务；写入使用独立临时文件、fsync、原子替换；损坏记录保留原文件并报错。
- 日志每次最多读取 64 KiB，返回最多 8,000 字节，提供 `before` 游标向前翻页。
- 启动前预留编号；启动结果不确定时保留脚本/日志，不自动重试。

同目录 `test_safety.py` **12 passed**，包含六个独立 Python 进程的 72 次并发写入、PID 复用/命令不符/旧记录拒绝、分页边界、64 位 Windows 句柄以及真实 Windows 临时 WMI 任务的启动、身份验证和停止。真实任务仅在 E 盘临时测试目录执行 `Write-Output` 和 `Start-Sleep`，测试结束已停止。

发布状态：源码验证后，经授权使用现有注册机制完成单扩展升级：

- 原启用修订：`41138d81d104fc51a29a8181`。
- 当前启用修订：`b6a7f66c84a34bde726e0afb`，保留 enabled=True；已通过隔离 ExtensionRegistry 的路径、大小、Python 语法、内容哈希校验。
- 候选规格：`E:/WorldModel-validation-20261031/jobs-candidate/candidate-spec.json`。
- 真实任务记录：`C:/Users/ASUS/.worldmodel/agent/state/workbench-jobs/jobs.json`；审查时有一项，记录状态 exited，属于旧版身份格式。升级前验证其 PID 已不存在、工作台无忙碌会话；升级后逐字节验证记录未变。没有终止、删除或修改它。

升级通过 `ExtensionRegistry.create` 和 `set_enabled` 完成，仅替换 direct-tools 中 jobs 这一项；其他五项内容保持一致。正式 MCP 只做 initialize/tools/list，确认五个工具与 `before` 参数，没有调用真实任务工具。没有修改不可变修订中的文件，也没有重启整机服务。

可复核结果：`E:/WorldModel-validation-20261031/jobs-candidate/activation-result.json`；升级脚本同目录 `activate_verified_jobs.py`；原 registry/direct-tools 备份在 `before-activation/`。回滚时只调用 `registry.rollback('workbench-jobs', '41138d81d104fc51a29a8181', authorized=True)`，并从备份恢复 direct-tools 的 `servers['creator:workbench-jobs']`。不要整文件覆盖其他扩展随后发生的更新。

## 手机端需要补齐的基础层

当前代码适合“本机服务运行、桌宠与工作台连接”。`wm/app.py:216` 的 Host/Origin 校验防浏览器跨来源访问，但接受没有 Origin 的非浏览器请求，**不是远端身份认证**。不能仅绑定 `0.0.0.0`、添加 allowed_origins 或转发 8790 端口就当作手机端后端。

建议保留共用 Agent 核心，让桌面、手机都成为客户端；以下边界应先完成：

1. **设备与身份**：首次桌面确认配对、可撤销的设备凭据、TLS、短期访问令牌；明确区分读取会话、控制任务和运行本机工具。权限状态由服务端统一保存。
2. **状态同步**：版本化 API；会话、消息、任务稳定 ID；持久事件游标、重连补发与快照；变更带 expected_revision，命令带 idempotency_key。现有 Resident 的 revision/events 是基础，不能替代全应用同步。
3. **执行器分离**：手机发出任务，桌面执行本机文件/浏览器/模型操作；桌面离线应明确显示离线与待执行。若需要电脑关机后继续，另部署云端执行器，不能声称本地后台就是云端常驻。
4. **任务恢复**：统一登记外部动作及结果；对结果未知的发布、付款、社交发送避免自动重放；取消、暂停与子代理控制需要跨设备一致。已有六个外部扩展的 JSON 状态尚不是统一任务账本。
5. **附件与通知**：分块/可续传附件、内容哈希、大小上限、文件所属会话校验；推送只带必要摘要和可撤销设备目标。通知渠道不应兼作无认证远程执行入口。

旧 `workbench-schedule/server.py:13` 明确只在调用 `schedule_due` 时检查到期，不会自行唤醒宿主；旧 goal 工具只是全局 JSON 目标记录。它们不能单独构成常驻助手。新的 Resident 与未来的统一服务 API 应承担持久目标调度，插件只提供能力。

## 结论边界

本次是针对最近工程改动的审查与回归修复，不是全项目安全认证。没有审计所有已安装扩展、模型输出或第三方运行库，也没有完成移动端、云端执行或公网部署。修复保留用户最新开发成果，避免重置数据和旧版本覆盖。
