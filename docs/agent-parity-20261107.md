# 工作台 agent 底层对照五家（2026-11-07）

用户原话：「需要重新编译桌宠整体，而且优化底层代码，保证 agent 底层的完善和先进程度（不劣于 claude、codex、deepseek、pi、Hermes 等底层）」。

第一至五节是 11-07 下午给你看的**对照和方案**（当时还没改代码）；你同意以后做完了，见第六节（实现情况）和第七节（评测结果）。没下载任何东西；11-07 20:21 重启 8790 上线。改前的代码快照在 `backups/agent-parity-20261107/before/`。

## 一、对照的是哪几个版本（2026-11-07 联网核对）

| 家 | 版本 | 依据 |
|---|---|---|
| Claude Code / Agent SDK | 2.1.319（npm，约 11-06） | [npm](https://registry.npmjs.org/@anthropic-ai/claude-code/latest)、[CHANGELOG](https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md)、官方文档 [hooks](https://code.claude.com/docs/en/hooks) / [checkpointing](https://code.claude.com/docs/en/checkpointing) / [how it works](https://code.claude.com/docs/en/how-claude-code-works)。闭源：**只对照行为**，不看还原源码 |
| OpenAI Codex CLI | 0.167.0（11-06；0.169.0-alpha.3 在测） | [npm](https://registry.npmjs.org/@openai/codex/latest)、[releases](https://github.com/openai/codex/releases)、官方文档 [hooks](https://learn.chatgpt.com/docs/hooks.md) / [subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents.md) / [approvals](https://learn.chatgpt.com/docs/agent-approvals-security.md) / [config](https://learn.chatgpt.com/docs/config-file/config-reference.md)（Apache-2.0） |
| DeepSeek → **DSH（DeepSeek Harness）** | 0.2.2-rc.1（npm latest，和你装的桌面版宿主同版）；本机 `desktop/third_party/dsh` 带着 0.2.1-rc.2 的源码 | [npm](https://registry.npmjs.org/@deepseek-ai/dsh)、[仓库](https://github.com/deepseek-ai/deepseek-harness)、**本机 MIT 源码逐模块读过**。`~/.dsh` 只读没改。**按"DeepSeek = DSH"理解，请你确认** |
| Pi（pi-mono coding agent） | 1.5.0（包名已从 `@mariozechner/pi-coding-agent` 改成 `@earendil-works/pi-coding-agent`） | [npm](https://registry.npmjs.org/@mariozechner/pi-coding-agent/latest)、docs：how-pi-works / compaction / sessions / extensions / codemode（MIT） |
| Hermes Agent（Nous Research） | v0.23.0 "The Hearth Release"（11-05） | [releases](https://github.com/NousResearch/hermes-agent/releases)、[README](https://github.com/NousResearch/hermes-agent)、文档 [skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills) / [memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory) / [configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration)（MIT） |

我们这边看的是：`wm/agent/`（core 循环、tools、policy、governance、studio、delegation、extensions、mcp_runtime、experience、acceptance、resident、jobwatch、prompt_context、presets）、`wm/cloud.py`（模型接入）、`wm/channels/manager.py`（QQ / 微信 / 桌宠怎么派活）、`wm/pipeline.py`（桌宠）。

**工作台和桌宠怎么是"同一个她"**：桌宠和 QQ 判断"这事要上工作台"时走 `manager.workbench_run`（一件事开一个工作台会话、她自己决定拆不拆子任务、做完回报到你最近找她的 QQ / 微信，你引用那条回报再说话就接着改）；长期目标走 `goal_start` → 常驻任务（`resident.py`，有预算）；工作台要你点头的卡片 15 秒没人点就经 `agent_ask` 上 QQ；她在工作台干活时桌宠坐到电脑前（`agent_doing`）；工作台里的 `her_skill` 又借回 QQ 那边的本事（看 B 站、留言）；人设和关于你的记忆两边是同一份。所以**模型接入层（cloud.py）的改进会同时让桌宠、QQ、工作台受益**，下面的"重试"就放在那一层。

## 二、对照表

记号：✅ 我们已经有、不比它们差；◐ 有但不全；❌ 没有。"补不补"一栏：**P0** 这轮先做，**P1** 这轮接着做，**P2** 记下以后做，**不补** 写了原因。

### 1. 循环、流式、并行、插话

| 项 | 五家怎么做 | 我们现在 | 差在哪 | 补不补 |
|---|---|---|---|---|
| agent 循环与流式 | 五家都是"想 → 调工具 → 结果回模型 → 再想"，边生成边推；CC 每轮自动验证结果；Pi 每轮之间可插扩展（turn_end 能要求再来一轮） | ✅ 边想边推（think_delta / delta），一轮最多 100 步（可改、0 = 不限），费用 / 时间 / 步数三种预算；她说"做完了"时程序做三层核对（说 / 做 / 真） | 空回复（没正文也没工具调用）当成做完了；DSH 把空回复算"可重试" | 空回复重试并进 P0-2 |
| 并行工具调用 | CC / Codex / DSH：只读的并行，写的串行；Pi 同一批里并行执行；Hermes 有"批"概念 | ◐ 连着的读文件 / 列目录 / 找 / 搜 / 上网一起做，结果按原顺序交回 | 只读的工作台能力（读材料、回查原文、看成果、读技能、读资料本 / Office）和只读扩展工具还是一个个来 | P2（省几秒，不急） |
| 中途插话、打断、排队 | CC：打字回车 = 这批工具做完马上交给她，Esc 立刻停；Pi：steer（这一步做完交）/ follow-up（整件做完再交）/ nextTurn；Codex、Hermes 也能打断改方向 | ✅ 三样都有：`steer`（这一步做完交，写明"做到一半补充的"）、`enqueue` 排队（做完自动接着做）、停（连命令的进程树一起结束，2 秒内收尾）；后台作业做完也走插话那条路回来 | 和 Pi、CC 同一水平 | 不用补 |

### 2. 工具集与编辑可靠性

| 项 | 五家怎么做 | 我们现在 | 差在哪 | 补不补 |
|---|---|---|---|---|
| 读文件 | 都有分页；CC 读图片 / PDF / Notebook；Hermes 每次最多 10 万字 | ✅ 行号、offset / limit、超长行 char_offset 续读；PDF、Word、Excel、压缩包交给本地解析器 | — | 不用补 |
| 精确编辑 | CC：Edit 要求先 Read、old_string 唯一，另有改多处；Codex：`apply_patch`（一次改多个文件、可增删挪，匹配时先精确、再忽略行尾空格、再忽略首尾空格、再统一引号破折号）；DSH：`str_replace_editor` + **"改前必读、读后被别人改过就不让覆盖"**（fs-observation-policy）；Pi：edit 可一次多处，改文件排队防并发；Hermes：patch 式 | ◐ `edit_file` 精确唯一替换，认 CRLF，找不到时报"最像的是第几行"；`write_file` 原子写 | ① **不检查"读过没有、读完以后被别人改过没有"**，可能把你刚改的覆盖掉；② 一次只能改一处，改五处要来回五趟；③ 只差空格 / 中英文引号就失败；④ GBK 编码的文件直接报错。**实测**（你 31 个会话里能看到的 3331 次工具结果）：`edit_file` 调了 322 次、失败 18 次（5.6%，其中"没找到 old"9 次、"出现了好几次"5 次）；另外 `artifact_register` 失败 32 次，**全是她猜错了"成果类型"**（说明里没写只能填 text / image / pdf / file） | **P0-4**：改前必读 + 过期检测、`edits` 一次改多处（全成或全不改）、宽松匹配兜底（只在唯一时用，并说明是宽松匹配改的）、GBK 文件原样按 GBK 写回；登记成果时类型只当提示、程序自己认。`apply_patch` 不补：那是 GPT 专门训练过的格式，DeepSeek 和 DSH 都用 str_replace 式 |
| shell 与后台进程 | CC：Bash 可后台跑 + Monitor 盯输出，长命令 3 分钟后转后台；Codex：PTY 统一执行、后台终端（最长 5 分钟一段）；DSH：持久 pwsh / bash + jobs；Hermes：6 种终端后端（本机、Docker、SSH、Modal…）；Pi：bash 一种 | ◐ `run_command` 每次新的 PowerShell、最多 600 秒、去掉像 key / token 的环境变量、停的时候连子进程一起杀；后台作业是**她自己做的扩展** workbench-jobs（job_start / job_output / job_stop），做完 jobwatch 自动把结果交回原会话 | 没有持久 shell（cd、环境变量不留）；但后台这块她已经自己学会了 | 不补（她自己学会的不替她重写；持久 shell 用 workdir 足够） |
| 搜索 | 都用 ripgrep 一类 | ✅ glob / grep 走 ripgrep，照 .gitignore 跳过 | — | 不用补 |
| 网页 | CC：WebSearch / WebFetch；Codex：默认用缓存索引搜（防注入），完全权限时才实时；DSH：DeepSeek 官方 Search / Fetch；Hermes：工具网关；Pi：没有内置 | ✅ DeepSeek 官方搜索 → DuckDuckGo + 必应兜底；读网页能按词跳到那一节、带小标题目录、10 分钟缓存、只读公网（每一跳都查） | — | 不用补 |
| 问主人（带选项） | CC：AskUserQuestion；Codex：request_user_input；DSH：ask-user / user-questions | ❌ 只有"要不要允许"的卡片；要问别的就停下这一轮 | 中途问一句、拿到答案接着做 | P2（要动 QQ 那边的回复解析和页面，等界面会话） |

### 3. 上下文管理

| 项 | 五家怎么做 | 我们现在 | 差在哪 | 补不补 |
|---|---|---|---|---|
| 自动压缩 | CC：快满时把对话压成摘要，单个结果太大反复撑满会停下报错，`/compact 重点`；Codex：超过模型的自动压缩阈值就压，另可每轮结束按百分比压；**DSH：到上下文 80% 时用模型写结构化检查点（目标 / 文件 / 报错与解决 / 待办 / 现在在做 / 下一步 / 关键上下文），原样保留最后约 16%，摘要请求复用前缀缓存（把整段对话原样重放、最后追加一句"请总结"）**；**Pi：超过"窗口 − 16384"时压，保留最近 2 万 token，绝不把工具调用和结果拆开，一段太长时拆成两份摘要再合，上下文超了先压再自动重试一次**；Hermes：到 50% 压，保头 3 条、尾 20 条，可另配便宜模型写摘要 | ◐ 超过 100 万字符（你 10-29 定的"到 1M 才整理一次"）时，把较早的工具结果折成两百字的头，原文留着可 `history_read` 回查；本机模型按它的上下文长度提早折；上下文报错时只留最后两条再折一次重试 | **只折工具结果，不写摘要**：对话本身很长（她说的话、你说的话、思考）时折了也装不下，最后报错停下；本机模型（3 万 token 左右）特别吃亏 | **P0-3**：折完还超、或模型报上下文装不下时，用同一个模型写**结构化检查点**（照 DSH / Pi 的栏目，中文），复用前缀缓存，保留最近一段原样，不拆工具调用和结果；被压掉的原文照样能 `history_search / history_read` 回查；`/compact 重点` 手动压。阈值照你定的 1M 字符不变 |
| 工具输出截断 | CC：太大存到文件给预览；DSH：超过 8192 字修成头 4096 + 尾 1024，超大的落盘给路径；Hermes：终端 5 万字、超 10 万字落盘；Codex：每个结果有 token 上限 | ✅ 每个结果最多 2 万字，命令留开头和结尾，原文留着可分页回查 | 原文存在会话文件里：两个会话已经 64–69 MB，每走一步整份写一次会卡住事件循环（复查会话量过约 0.5 秒） | **P1-12**：超过 64 KB 的原文落到 `agent/spill/`，会话里只留指针，回查照常 |
| 提示缓存 | CC：1 小时缓存、空闲时压缩配合缓存；Hermes：5 分钟 / 1 小时 / 自动；DSH：摘要复用缓存；DeepSeek 是自动前缀缓存 | ✅ 前缀刻意保持不变（背景接在你那句话后面、旧几轮的背景照旧带着），账上记了命中 / 没命中 | — | 压缩也要照顾缓存（进 P0-3） |
| 工具多时按需加载 | CC：MCP 工具默认只给名字，用到再搜（tool search）；Pi：codemode 里用 BM25 搜工具 | ❌ 每次请求带全部 50 多个工具说明 | 云端有缓存不心疼；本机小模型上下文短，工具说明占一大截 | P2（本机模型专用的精简工具表） |

### 4. 子 agent、计划与待办

| 项 | 五家怎么做 | 我们现在 | 差在哪 | 补不补 |
|---|---|---|---|---|
| 子 agent 与并行 | CC：子 agent 独立上下文 / 可分叉、自定义类型（.claude/agents）、后台跑、agent teams；Codex：并发上限、`.codex/agents/*.toml` 自定义（模型、思考强度、指令）、继承父的审批；DSH：spawn / fork、agent team（实验）；Hermes：delegation 可用别的模型、每轮最多 50 个；Pi：默认不带（示例里做成扩展） | ✅ `delegate_tasks` 一次最多 32 个、并发 3、嵌套 3 层、只读 / 共享目录 / Git worktree 三种隔离、预算和时间按父任务分、权限不能比父高、排队、等、停 | ① 子任务不能单独选预设 / 模型 / 思考强度（CC、Codex 的"自定义 agent"）；② 子任务转成"等你点头"时不发事件，对话里那张卡一直写"运行中"（工作台改版会话 11-02 提过） | **P1-11**：两样都补 |
| 计划与待办 | CC：TodoWrite / 任务工具 + 计划模式；Codex：update_plan；DSH：todo + plan-mode；Hermes：todo；Pi：默认没有 | ✅ `task_plan`（步骤 + 验收标准）、计划预设（`/plan` 只看不改，等你说"按计划做"）；她还自己做了 todo / goal 扩展 | — | 不用补 |

### 5. 权限、沙箱、审批、钩子

| 项 | 五家怎么做 | 我们现在 | 差在哪 | 补不补 |
|---|---|---|---|---|
| 权限模式与审批 | CC：Auto（后台分类器挡危险的）/ Manual / Accept edits / Plan，规则 allow / deny / ask；Codex：on-request / never / granular + 自动审查 agent（Guardian，高风险要人点头）；DSH：权限预设；Hermes：危险命令确认、YOLO；Pi：不问（建议放容器里） | ✅ 四档（仅查看 / 工作区内修改 / 自动执行 / 完全授权）+ 可编辑黑名单 + "以后都允许"名单；要点头的卡片**不过期**，15 秒没人点就上 QQ / 微信问 | 见下一行的漏洞 | — |
| **守门的硬底线** | CC：settings 里 deny 规则、受保护路径；Codex：`.git` `.codex` `.aws` 等在可写目录里也只读；DSH：沙箱策略 | ❌ **漏洞**：你的默认档是"完全授权"（31 个会话里 19 个），这一档只认黑名单；而黑名单里保护**桌宠设置和 token** 的三条规则写的是 Claude 会话沙箱里的副本路径（`…\Packages\Claude_…\LocalCache\Roaming\WorldModel\…`，10-28 初始化时记成了这个），**真正的 `%APPDATA%\WorldModel\pet-settings.json`、`pet-api-token.txt`、`pet-api-guest-token.txt` 很可能没被挡住** | 违反"守卫要一直挡住设置、token、保管箱、密钥和浏览器数据" | **P0-1**：代码里加一道**不随档位、不随名单变**的硬底线（按服务端运行时真正的路径算，原路径和解析后的路径都比），挡设置 / token / 保管箱 / 邮箱 / 微信 / Cookie / 密钥 / 浏览器登录数据，以及 agent 自己的权限文件和新加的钩子配置；黑名单再迁移一次补上真路径的规则（你删过的不回填） |
| 沙箱 | Codex：Windows 用 MXC / ACL 沙箱，可写目录外和联网要批准；CC：bash 沙箱（bubblewrap / seatbelt）；DSH：Windows ACL 沙箱；Hermes：Docker / 远程后端；Pi：建议容器 | ❌ 只有应用层的守门（按命令和路径字面判断），不是操作系统沙箱 | 真比它们弱的一项 | **不补（这轮）**：Windows 上做真沙箱要受限令牌 / ACL，改错了会挡住她正常干活，工作量和风险都大；这轮先补硬底线，沙箱以后单独评估（可参考 DSH 的 MIT 实现） |
| 钩子（hooks） | CC：30 多个事件（PreToolUse 可拦可改参数、PostToolUse 可换结果、UserPromptSubmit、Stop 可让她接着做、PreCompact、SubagentStop…），类型有命令 / HTTP / MCP 工具 / 提示 / agent；Codex：PreToolUse / PostToolUse / PermissionRequest / UserPromptSubmit / Stop / SubagentStart / SubagentStop / PreCompact / PostCompact / SessionStart / SessionEnd，项目里的钩子要先审过才跑；DSH：兼容 CC 和 Codex 的钩子；Pi：扩展事件 30 多个；Hermes：插件钩子 | ❌ 没有 | 你想加"改完必须跑测试才算完""某类命令一律拦""每次改文件后自动格式化"这类规矩，现在只能写进提示词，靠她自觉 | **P1-6**：照 Codex 的事件名做一套（PreToolUse / PostToolUse / UserPromptSubmit / Stop / SessionStart / PreCompact / PostCompact / SubagentStart / SubagentStop），先做"命令"类型（JSON 从标准输入进、退出码 2 = 拦下、标准输出 JSON 给决定）；只认 `~/.worldmodel/agent/hooks.json`（你的），**工作区里的钩子不认**（防仓库里藏命令）；钩子只能更严不能放宽守门；钩子文件在硬底线里，她自己改不了 |

### 6. skills、MCP、项目说明

| 项 | 五家怎么做 | 我们现在 | 差在哪 | 补不补 |
|---|---|---|---|---|
| skills | CC：SKILL.md，开局只看名字和说明，用到才读全文，可分叉跑；Codex：skills 占上下文不超过 2%，"录一遍就成技能"（Record & Replay）；DSH：skill 工具 + office 技能；Pi：按需加载 | ◐ 读桌面技能和她 Creator 扩展里的 SKILL.md，`skill_read` 按需读全文；她能自己做有版本的技能 / MCP / 界面（`extension_create`），启用要你点头；`growth_evaluate` 拿固定题集比新旧版本 | ① 技能目录不在提示词里，她得先调 `extensions_list` 才知道自己会什么；② 用没用、用了几次不记 | **P1-7** 一并补：提示词里放技能名单（名字 + 一句说明，限长、内容不变就不破缓存）；记使用次数 |
| **自己沉淀技能（学习闭环）** | **Hermes：系统提示要求把不简单的做法存成技能；做完后台复盘，可能提出或暂存技能修改；`skill_manage` 的 patch 改一小段；可设"写技能要审"；技能库有安全扫描** | ◐ 有"工作经验"层（命令报错又换做法成了，程序记下，下次同类报错时附上，用了成没成会回流）；有技能创建，但**没有"做完一件复杂的事，提醒她把做法存成技能"这一步** | 学习闭环缺了"沉淀成技能"这一环，正对"别替她解决，让她自己学" | **P1-7**：一轮做完、调了 8 次以上工具（或报过错又做成了）、这轮没建过技能时，程序给她一句"复盘提示"（工作台状态数据，不是你的话）：值得就用 `extension_create` 存成技能草稿（什么时候用 / 步骤 / 坑 / 怎么验证），不值得就直接答；**草稿不自动启用**，照旧要你点头或过题集 |
| MCP 客户端 | CC：stdio / HTTP / SSE，长连接，工具按需加载，MCP 任务后台跑；Codex：每个对话租用共享连接，审批卡带工具名；Pi 1.5：自带 MCP 扩展 + codemode；Hermes：任意 MCP 服务 | ◐ stdio + Streamable HTTP，只用 tools；**每调一次就新起一个进程**，调完就关；凭据解密不出模块、输出里的凭据打码 | ① 慢：Python 写的扩展每次启动 0.5–1 秒，她的直通工具（待办、定时、后台作业…）每次都要等；② 有状态的服务（比如浏览器 MCP）每次都是新的，记不住上一步 | **P1-9**：按会话保留连接（空闲 2 分钟关、出错重连、停的时候直接关掉那条），每次调用照旧过守门和审批 |
| MCP 服务端 | CC：`claude mcp serve`；Codex：原来的 mcp-server 已迁到 app-server；Hermes：有 | ◐ 桌宠宿主 `/mcp`（8793）暴露桌宠自己的说话、手势、显隐；工作台没暴露 | 别的 agent 没法把活派给她的工作台 | P2（要先想清楚外来指令的权限，按"外人的话不能进能动手的环节"，暂不做） |
| 项目说明文件 | CC：用户 / 项目 / 本地三层 CLAUDE.md，子目录的读到那里时再加载，也认 AGENTS.md；Codex：全局 `~/.codex/AGENTS.md` + 从仓库根到当前目录逐层合并，有字节上限；Hermes：上下文文件动态限长；Pi：发现到的上下文文件 | ◐ 只读工作区根目录的 AGENTS.md / CLAUDE.md（各 24 KB） | ① 没有"你给她的全局说明"；② 子目录里的 AGENTS.md 不会自动看到 | **P1-10**：加 `~/.worldmodel/agent/AGENTS.md`（全局，你写）；她读 / 改到某个子目录、那里有 AGENTS.md 或 CLAUDE.md 时，这个会话里第一次附在工具结果后面 |

### 7. 会话、检查点、出错与模型

| 项 | 五家怎么做 | 我们现在 | 差在哪 | 补不补 |
|---|---|---|---|---|
| 会话持久化、续接、分叉 | CC：JSONL，`--continue / --resume`，`/branch` 分叉；Codex：rollout 文件、续接分叉、换模型也能接着聊；**Pi：一个文件里存成树（每条有 id 和 parentId），`/tree` 原地回到任何一点开新枝，离开的枝自动写摘要**；Hermes：SQLite + FTS5 全文搜历史会话，v0.23 加了 `/rewind` | ✅ 每个会话一份 JSON + 每次任务一份记录；中断后"继续上次任务"（补齐没结果的调用、**绝不重放**可能已生效的操作）；整份分叉或从某条消息分叉；归档、回收站、恢复 | 没有 Pi 那种原地的树；但"从某条消息分叉"已经能做同样的事 | 不补（分叉够用） |
| 检查点与撤销 | CC：每次你发话前给它改的文件拍快照，`/rewind` 可只回对话 / 只回代码 / 都回，100 个检查点、30 天；DSH：模型请求和工具执行前先落盘，落不了盘就不执行；Hermes：`/rewind`；Codex：靠 git 和 worktree | ✅ 工具调用前先存盘（中断后不会重放）；工作台改版会话做了"撤销工具改动"（先全部核对、被你再改过的不动、CRLF / BOM 原样、新建的挪进 `.archive`） | 没有"回到第 N 条消息：对话和代码一起回去"的一键操作 | P2（后端可用现成的撤销 + 分叉拼出来，界面归改版会话） |
| **出错重试与模型回退** | **DSH：最多 5 次，0.5 秒起翻倍到 10 秒、抖动 10%，限流 / 服务出错 / 超时 / 连接断 / 空回复都重试，每次重试先落盘再等**；Codex：请求重试 4 次、流式断了重试 5 次、流 5 分钟没动静算断；Hermes：重试 3 次后换备用模型（fallback_providers），还不行再等着重试几轮；CC：指数退避自动重试、不可重试的不重试，模型不可用时退到同系列的旧模型 | ❌ **工作台一次限流、一次 502、一次网络抖动，这一轮就报错停下**（只有"参数不认就去掉可选参数再试一次""不收图就去掉图"这类一次性修正） | 最影响"能不能一口气做完"的一项 | **P0-2**：在模型接入层（cloud.py）加重试：限流 / 服务出错 / 超时 / 连接断 / 数据没传完 / 空回复，指数退避 + 抖动 + 认 `Retry-After`；已经推了一半字的那次也能重来（页面上提示"重新生成"）；钥匙不对、余额不足、超预算、参数错**不重试**。工作台 5 次，桌宠 / QQ 1 次（要快）。重试用完还不行时换**备用模型**（默认 v4-pro 退到 flash，可配；本机模型也能当备用） |
| 多模型、本地与 API 同等 | CC：Claude 系列 + 网关；Codex：各家提供方、本地 OSS；Pi：pi-ai 统一十几家、跨家接着聊；Hermes：Nous Portal / OpenRouter / 自定义；DSH：DeepSeek + pi-ai | ✅ DeepSeek / 千问 / Kimi / 自定义 + 本机模型（llama-server / Ollama / LM Studio / vLLM），工具、流式、打断、看图都一样，本机记 0 元、按它的上下文提早折 | 新加的重试、压缩、回退都要对本机模型一样能用（测试里覆盖） | 跟着各项一起做 |

### 8. 费用、定时、记忆、无界面、评测

| 项 | 五家怎么做 | 我们现在 | 差在哪 | 补不补 |
|---|---|---|---|---|
| 费用与 token 统计 | CC：`/cost` `/usage`、网关花费报表；Codex：/usage；DSH：token-meter、session-stats；Hermes：有；Pi：有 | ✅ 每个会话记命中 / 没命中 / 输出 / 元，账本里记"工作台"，三种预算，DeepSeek 搜索按次记钱 | — | 不用补 |
| 定时与心跳 | CC：`/loop` 自己定节奏、云端定时任务；Hermes：内置 cron，结果发到任意平台；DSH：schedule + goal 按轮推进；Codex：没有 | ◐ 常驻任务（SQLite，按轮推进、有预算、回报到聊天）；她自己做了 workbench-schedule，但她自己写着"不会叫醒我" | 心跳是调研会话排的第 2 件，等你定 | 不在这轮（另案） |
| 记忆与学习闭环 | Hermes：MEMORY.md 2200 字 + USER.md 1375 字，开局冻成快照（不破缓存），会话里写的下次才生效，FTS5 搜历史；Codex：可选的记忆（从旧对话里提炼、合并）；CC：自动记忆 MEMORY.md | ◐ 关于你的记忆每轮带上（和 QQ 私聊同一份）；工作经验层；固定题集验收 | 记忆归类脑记忆会话（session-3，第 5–9 阶段等你定）；这轮只补"沉淀技能"那一环（P1-7） | 记忆不在这轮 |
| 无界面运行与 SDK | CC：`claude -p`、stream-json、Python / TS 的 Agent SDK（钩子回调、会话续接）；Codex：`codex exec --json`、TS SDK、app-server；Pi：print / json / rpc / SDK；DSH：headless、JSON-RPC SDK、ACP；Hermes：CLI、网关、ACP | ◐ 有 HTTP + WebSocket 接口（工作台页面在用），QQ / 桌宠在进程里直接调 | 没有命令行无界面跑法、没有一行一个事件的 JSON 输出、没有给脚本用的接口 | **P0-5**：`python -m wm.agent.headless "任务" --workspace … --model … --mode … --jsonl`（默认临时家目录，不碰你的 ~/.worldmodel），外加给 Python 用的 `run_task()`；评测就用它 |
| 轨迹日志与评测 | Hermes：批量生成轨迹、压缩轨迹给训练用；Codex：OTel；DSH：OTel、会话日志导出；CC：OTel | ◐ 每次任务一份记录 + 会话事件；验收核对；经验推送记录 | 没有一次任务完整轨迹的导出（每次请求、用量、工具调用、耗时），也没有任务级评测 | **P0-5**：每次任务导出一份 JSONL 轨迹；做本地评测（见第四节） |
| 重复调用保护 | DSH：repeat-tool-reminder；Hermes：工具循环护栏（警告 / 硬停） | ❌ 只靠提示词"连着失败两次就停下来想" | **实测**：6 个会话里有参数一模一样的调用重复 3 次以上，其中 5 个重复 5 次以上，最多的一模一样调了 37 次 | **P1-8**：同一个工具同样参数第 3 次起在结果后面提醒"结果不会变，换个思路"；同一条失败命令第 3 次起提醒更重；第 8 次直接不执行 |

## 三、补齐方案（按优先级）

估时是"人工日"的粗估；每项都配 pytest（全用假模型，不花钱），能 A/B 的做 A/B，做法和结果记进实验记录。

### P0（先做，最影响"一口气做完"和安全）

| # | 做什么 | 动哪些文件 | 测试 / A/B | 估时 |
|---|---|---|---|---|
| P0-1 | 守门硬底线 + 黑名单路径修正 | `wm/agent/policy.py`、`governance.py`（LF）、`core.py`（CRLF，一两处） | 完全授权档读 / 写 / 命令碰真设置、token、保管箱都被挡；名单被删光也挡；你删过的规则不回填 | 0.5 天 |
| P0-2 | 模型请求重试 + 备用模型 + 空回复重试 | `wm/cloud.py`（CRLF，按字节）、`core.py` | 假服务器注入 429 / 500 / 断流 / 空回复：改前这一轮失败、改后做完；不该重试的不重试；停止键在等待里立刻生效 | 1 天 |
| P0-3 | 结构化压缩检查点 | 新 `wm/agent/compaction.py`、`core.py` | 切点不拆工具调用；被压掉的原文能回查；请求前缀和平时一样（缓存能命中）；本机小上下文的长任务 A/B（改前报错 / 改后做完） | 1.5 天 |
| P0-4 | 编辑可靠性：改前必读 + 过期检测、一次改多处、宽松匹配兜底、GBK 原样写回；登记成果的类型只当提示 | `wm/agent/tools.py`（LF）、`core.py`、`studio.py` | 用你历史会话里真实的 14 次"没找到 / 不唯一"失败离线重放：改后能自动改对几次（不调模型）；你在旁边改了文件她不会覆盖；历史里 32 次登记失败改后都能登记上 | 1 天 |
| P0-5 | 无界面运行 + 轨迹导出 + 本地评测 | 新 `wm/agent/headless.py`、`tools/agent_eval.py`、`tests/agent_eval/` | 见第四节 | 1.5 天 |

### P1（接着做）

| # | 做什么 | 动哪些文件 | 估时 |
|---|---|---|---|
| P1-6 | 钩子（9 个事件、命令类型、只认你的 hooks.json、只能更严） | 新 `wm/agent/hooks.py`、`core.py`、`delegation.py` | 1 天 |
| P1-7 | 技能名单进提示词 + 做完复杂的事提醒她沉淀成技能草稿 + 记使用次数 | `core.py`、`extensions.py`、`studio.py` | 1 天 |
| P1-8 | 重复调用保护 | `core.py` | 0.3 天 |
| P1-9 | MCP 长连接（按会话、空闲关、出错重连） | `mcp_runtime.py`、`extensions.py` | 1 天 |
| P1-10 | 全局 AGENTS.md + 子目录说明按需加载 | `prompt_context.py`、`core.py` | 0.5 天 |
| P1-11 | 子任务可选预设 / 模型 / 思考强度；转"等你点头"时发事件 | `delegation.py`、`studio.py` | 0.5 天 |
| P1-12 | 大的工具原文落盘，会话文件变小、存盘不卡 | `core.py` | 0.5 天 |

### P2（记下，以后做）

问主人带选项的工具、本机模型专用精简工具表 / 工具搜索、"回到第 N 条消息"一键回退、只读能力也并行、工作台对外的 MCP 服务、Windows 真沙箱。

### 不补（原因）

`apply_patch` 格式（DeepSeek 没专门训练过，DSH 也不用）、持久 shell（workdir 够用，后台她已经自己学会了）、Pi 那种原地会话树（从某条消息分叉已能做同样的事）、按 Hermes 换 SQLite 存会话（大改动，收益不抵风险）、"自动审查 agent"代替你点头（和你"申请卡片不过期、你来定"的规矩冲突）。

### 和别的会话的分工

- `desktop/web` 一行不碰。要界面的（钩子页、重试时"重新生成"的显示、技能草稿提醒、`/compact 重点`）发给工作台改版会话（codex-lexora）。
- 动 `wm/agent/core.py`、`studio.py`、`delegation.py`、`extensions.py` 前先和 codex-lexora 对一下（已发消息问它改了哪些，还没回）。
- `wm/channels/manager.py` 这轮不动；cloud.py 的重试对 QQ 只开 1 次、不改它的代码。
- 保管箱目录 `~/.worldmodel/vault` 本来就在保护名单里，硬底线照样挡（和保管箱会话的设计一致）。

## 四、本地评测怎么做

照你平时让工作台做的事出 12 道题，每题一个干净的临时工作区 + 程序判分（不让模型自己判）：

| 类 | 题 |
|---|---|
| 改代码 | ① 修一个让单测失败的 bug 并跑测试；② 跨三个文件加一个参数（调用处、实现、测试都要改）；③ 改一个 CRLF 的 Windows 脚本，换行不能变；④ 改一个 GBK 编码的配置文件 |
| 处理文件 | ⑤ 把一堆乱放的文件按类型整理进子文件夹并写清单；⑥ 读一个 CSV 算统计写成 Markdown；⑦ 从 Word / PDF 材料里找出指定信息写进答案文件 |
| 查网页 | ⑧ 查 Python 官方文档里一个函数的用法；⑨ 查一个开源项目的许可证（判分看关键词） |
| Office | ⑩ 按给的数据做一份带标题和表格的 Word；⑪ 做一个带公式合计的 Excel；⑫ 做一份 5 页的 PPT |

每题记：成没成、问了模型几步、工具调用几次、工具报错几次、token（命中 / 没命中 / 输出）、花了多少钱、用了多久。**改前（快照里的代码）和改后各跑一遍**，另用本机模型（Qwen3.5-2B，纯 CPU，免费但慢）跑改后的一遍。另做两组故障注入题（限流、断流）专测重试。

花费估计：DeepSeek flash 一题约 0.03–0.15 元，12 题 × 2 遍 ≈ **1–3 元**（上限 5 元，到了就停）。要你点头才跑。

## 五、你定的（2026-11-07）

1. P0 + P1 都做，P2 先记下。
2. （没问到，按 DSH 理解。）
3. 评测可以花钱，上限 5 元。
4. 备用模型：v4-pro 连着失败时退到 flash。
5. **不加硬底线，只修名单里记错的路径**。所以 P0-1 改成：比较路径前把打包应用重定向过的路径换回真正的位置（读名单时就换，不迁移也挡得住）；名单迁移到第 2 版时把文件里那三条的写法也改成真位置、记审计；以后新增的默认规则和你加的路径规则都记真位置；新的钩子配置 `hooks.json`（读写都不让）和全局说明 `AGENTS.md`（不让改、可以读）各加一条**默认规则**，和 `prefs.json` 一样在约束设置里能改能删。

## 六、实现情况（2026-11-07 做完，20:21 重启 8790 上线）

| # | 做了什么 | 文件 | 测试 |
|---|---|---|---|
| P0-1 | `policy.unvirtualize()`：`…\Packages\<包>\LocalCache\Roaming\…` 换回 `…\AppData\Roaming\…`，`inside()` 两边都换；名单读的时候每条规则重新校验成真位置；第 2 版迁移改写文件里的旧写法（审计记 `fixed`）、补两条默认规则。用你真名单的**副本**预演过：三条改对、补两条、真的设置和 token 读取被挡、技能文件照样能读 | `policy.py`、`governance.py`（LF） | `tests/test_agent_guard_paths.py` 6 项；旧的迁移测试按第 2 版更新 |
| P0-2 | `cloud.py` 的 `_events`：还没交出字的那次按退避重试（0.5 秒起翻倍到 10 秒、抖动 10%、认 `Retry-After`）；工作台 5 次（限流 / 服务出错 / 连不上 / 超时 / 数据没传完 / 空回复），桌宠和 QQ 默认 1 次、只管限流 / 服务出错 / 连不上；工作台里说到一半断了丢掉整步重来（最多 2 次，发 `stream_reset`）；重试用完换备用模型（`agent.fallback_models`，默认 v4-pro → flash），这一轮剩下的步骤都用它；每次重试页面上写一句 | `cloud.py`、`core.py`（CRLF，按字节） | `tests/test_agent_parity_p0.py` 重试 9 项 |
| P0-3 | `compaction.py`：切点只在 user / assistant 前面、不拆调用和结果、优先切在你说话处、整轮太长才切在中间（这一轮你的原话原样抄进检查点）；云端平时那份请求原样重放 + 最后一句"写检查点"（前缀一样、缓存命中），装不下 / 本机模型改成把那一段排成文字单独问；检查点栏目照 DSH / Pi（目标、要求与偏好、进展、关键决定、相关文件、报错与解决、后台作业与子任务、下一步、关键上下文）；被换掉的消息挪进 `Session.compacted`，`history_search / history_read` 照样查；一轮最多主动压 3 次；`/compact 重点` 手动压。阈值照你定的 1M 字符不变 | 新 `compaction.py`、`core.py` | 同上 压缩 6 项 |
| P0-4 | 改前必读 + 过期检测（会话里记她看过 / 写下的那一版：覆盖没读过的已有文件、读完被改过的都先让她再读）；`edit_file` 的 `edits` 一次改多处（全成或全不改）；精确找不到时按"行尾空格 → 整体缩进差一截（new 跟着补 / 去同一截）→ 引号破折号"宽松匹配，只在恰好一处时用、换行规则和精确替换一样；读写 GBK / UTF-16（带 BOM）/ UTF-8 BOM / 全 CRLF 的文件都照原样存回；登记成果的类型只当提示 | `tools.py`（LF）、`core.py`、`studio.py` | 同上 编辑 7 项 |
| P0-5 | `python -m wm.agent.headless "任务" --workspace … --jsonl`（默认临时家目录、要点头的默认拒绝、退出码 0 / 1 / 2 / 3）和给 Python 用的 `run_task()`；`AgentHub.trajectory()` 导出整次轨迹；每步用量记进 `run.steps_log`；本地评测 `tools/agent_eval.py` + 12 道题 `tools/agent_eval_tasks.py`（判分自检 `tests/test_agent_eval.py`），能跑改前快照、能注入故障 | 新 `headless.py`、`core.py`、`tools/agent_eval*.py` | `tests/test_agent_headless.py` 3 项、`tests/test_agent_eval.py` 13 项 |
| P1-6 | 钩子：9 个事件（PreToolUse 拦 / 改参数、PostToolUse 补一句 / 提醒、UserPromptSubmit 拦 / 补背景、Stop 让她接着做（一轮最多 3 次）、SessionStart 补背景、PreCompact 拦 / 给重点、PostCompact、SubagentStart 补说明、SubagentStop），命令类型（JSON 从标准输入进、退出码 2 拦、标准输出 JSON 给决定；`args` 写法不经过 PowerShell），只认 `<家>/hooks.json`；改过的参数照样过守门 | 新 `hooks.py`、`core.py`、`delegation.py` | `tests/test_agent_parity_p1.py` 钩子 5 项 |
| P1-7 | 系统提示里放技能名单（名字 + 一句说明，一分钟内不重读，名单不变前缀就不变）；`skill_read` 记使用次数（`extensions/skill-usage.json`）；一轮调了 12 次以上工具且（报错 2 次以上或 20 次以上）、这一轮没建过扩展时，给她一句复盘提示，值得就用 `extension_create` 存成技能草稿（照旧要你点头才启用）；`agent.skill_nudge: "off"` 关掉 | `core.py`、`extensions.py`、`studio.py` | 同上 3 项 |
| P1-8 | 重复调用保护：同一工具、同样参数、**同样结果**第 3 次起结果后面提醒，第 8 次不执行；结果变了不算；等子任务、看作业输出 / 状态、看时间这类轮询的不管 | `core.py` | 同上 2 项 |
| P1-9 | MCP 长连接：按（会话、服务、配置）留着，空闲 120 秒关、最多 8 条、进程没了自动重连、出错 / 被停的那条直接关；工具清单记在连接上；服务端关时全关；不给会话编号的（评测这类）照旧一次一连 | `extensions.py`、`studio.py`、`app.py` 一行 | 同上 1 项（假 MCP 记得住上一步、换会话另起、进程没了重连） |
| P1-10 | 全局说明 `<家>/AGENTS.md` 进系统提示；子目录的 AGENTS.md / CLAUDE.md 在她读 / 改 / 列到那个目录时附在结果后面（一个会话一次，页面上写一句） | `prompt_context.py`、`core.py` | 同上 1 项 |
| P1-11 | `delegate_tasks` 每项可带 `preset` / `model`（可只写名字）/ `effort`；子任务转去等你点头、恢复时给父会话发 `subagent` 事件 | `delegation.py`、`studio.py`、`core.py` | 同上 2 项 |
| P1-12 | 超过 6.4 万字的工具原文落到 `<家>/spill/<会话>/`，会话文件只留指针；回查、轨迹、压缩时读回原文；打开旧会话时顺手挪出去 | `core.py` | 同上 1 项 |

**顺带修的 bug**：子任务校验模型时变量名写重、把"选中的材料"覆盖了（写测试时抓到，没上线过）；重复调用保护一开始把 `list_dir` 当成轮询工具（名字里有 list），改成按词判断。

## 七、评测结果（2026-11-07）

做法和取舍见知识库 [[工作台底层对标实验记录]]，数据在 `logs/agent-eval/`。

| | 改前（两遍） | 改后（两遍） |
|---|---|---|
| 12 题成功 | 12、12 | 12、12 |
| 问模型的步数 | 111、103 | 71、72（约 −33%） |
| 工具调用 | 123、116 | 85、84 |
| 工具报错 | 11、10 | 0、1 |
| 花费 | 0.307、0.263 元 | 0.214、0.220 元（约 −24%） |
| 耗时 | 534、390 秒 | 199、269 秒（约 −49%） |

- 故障注入（20% 回 503、10% 流到一半断开，同一个种子，10 题）：改前 1 / 10 产物对、0 题正常做完；改后 10 / 10 正常做完（107 次请求里注入了 23 次 503、11 次断流）。
- 编辑离线 A/B（她 133 次真实多行编辑）：整体缩进差一截 0 / 76 → 76 / 76；引号写成弯引号 0 / 115 → 115 / 115；"真写错一个字"的反例 127 个两边都拒绝、零误改。
- 评测一共花了约 1.42 元（上限 5 元）。
- 本机模型（Qwen3.5-2B Q4_K_M，纯 CPU，改后的代码跑一遍）：12 题成 3 题（code_bugfix、code_gbk、office_pptx），150 步、工具 164 次（报错 91 次，其中 29 次是重复调用保护拦下的一模一样的调用），0 元，约 58 分钟。底层这条路能走通（CRLF / GBK 原样保留、前缀复用约 92%），瓶颈是 2B 自己：会跑去用和题目无关的工具（资料本、扩展），一模一样的调用反复发。这是 P2 里"本机模型专用精简工具表"的证据。
- 上线：2026-11-07 20:21 重启 8790（启动编号 39654c62 → 9ea03ee9），QQ / 微信在线；守门名单迁到第 2 版。
