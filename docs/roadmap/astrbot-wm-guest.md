# AstrBot 接 wm-guest：试接方案

2026-10-25 调研 + 方案。只读了 AstrBot 的官方文档、GitHub 源码和发布页，以及本机已有的文件；**没下载、没安装**。

**进度（2026-10-25）**：
- 用户定了：用手上的启动器 0.3.10，先 ChatUI / API 试，白名单只放自己，和 QQ 共用人设和额度；下载 AstrBot 之前再问。
- 第 6 节第 1、2 条已做：宿主认 `x-astrbot-conversation-id`、带工具记一笔并计数，面板 QQ 页显示。自检 `qqtest.ps1` 21 项全过（新增 7 项），`apitest`、`paneltest`、服务端 `test_channels` 回归全过。
- 第 3、4 条也做了（2026-10-25）：`wm-guest` 的渠道日志（`<库>/外部渠道/api/<会话>/日志/`）和今天的计数（状态里的 `api` 段）；面板的"QQ"页改成"外部渠道"页，加了"第三方框架"一节（怎么接、今天的计数、日志在哪、带工具的提醒）。自检 `qqtest.ps1` 25 项全过，`apitest` 33、`paneltest` 19、服务端 `test_channels` 28 全过。
- **AstrBot 装好并锁好了（2026-10-25）**：用户确认后，用手上的启动器 0.3.10 装了 AstrBot 4.29.3（实例 `wm-guest`，数据在 `C:\Users\ASUS\.astrbot_launcher`，WebUI `127.0.0.1:6185`），按第 4 节用 `desktop/scripts/astrbot_lock.py` 锁好；端到端自检 `desktop/scripts/astrbottest.ps1`（8794 mock + 测试桌宠 + AstrBot 的 HTTP API）。装好后逐项核对出 7 处方案没料到的行为，3 处改了我们的代码、4 处在锁配置里关掉，见第 11 节；怎么用见第 12 节。还没接任何平台账号。
- 是 [external-channels.md](external-channels.md) 的一部分（外部渠道），QQ 渠道和 `wm-guest` 的现状见那篇第 0 节和根目录 README"外部渠道：QQ"。

用户的原话："请 QQ 那个会话出一份 AstrBot 接 wm-guest 的试接方案（先不下载、不安装）：哪些平台值得接、AstrBot 的插件和函数调用怎么关、和我们自己的 QQ 渠道怎么分工。"

## 0. 结论先说

- **AstrBot 只当"接平台的转接头"**：
  - 它是一个独立程序，负责从各平台收消息、往各平台发消息；
  - 它的"大脑"（模型服务商）只配一个：桌宠本机接口 `http://127.0.0.1:8793/v1`，用**访客 token**、模型 `wm-guest`；
  - 回复由我们服务端的访客会话出：没有工具、读不到你的记忆、不写记忆、外发前查隐私。
- **AstrBot 自己的大脑功能全部关掉**：插件、人格里的工具、MCP、Skills、SubAgent、主动型能力、"使用电脑能力"、网页搜索、知识库、群聊上下文感知、主动回复、文转图；上下文压缩改成按轮数截断；管理员 ID 清空；WebUI 只听本机。
- **最要紧的几条不靠 AstrBot 的配置，靠我们这边已经做好的兜底**（6a / 6b）：
  - 访客 token 只能用 `wm-guest`，`/mcp` 回 403；
  - `wm-guest` 不看请求里的 `tools`、system 和历史，回复里从来不带 `tool_calls`。所以 AstrBot 那边就算开着工具，也永远不会被调用；
  - 访客会话读不到你的记忆、不写记忆，有外发检查、限频和每日额度。
- **我们这边要补两处小改动**（第 6 节，等确认）：
  - AstrBot 请求里**不带 OpenAI 的 `user` 字段**，会话号放在请求头 `x-astrbot-conversation-id` 里。宿主要改成认这个头，否则所有平台、所有人会挤进同一个访客会话；
  - 请求里带了工具时记一笔，并在面板上提示。
- **先接哪个**：
  - 先用 AstrBot 自带的 ChatUI 和 HTTP API 试，不连任何账号；
  - 第一个真平台推荐**个人微信**（AstrBot 现在走的是微信官方的 ClawBot 接口）或**飞书**（长连接，不用公网），看你平时用哪个。
- **QQ 继续走我们自己的渠道**，不交给 AstrBot 的 OneBot 适配器。同一个 QQ 号不能两边同时接，会回两次。

## 1. 和"不把 AstrBot 放进链路"的关系

external-channels.md 第 3 节说不把 AstrBot 放进 QQ 的链路，指的是不让它当**大脑**：它自己的人设、记忆、插件、函数调用都在回复路径上，我们的隔离管不到。

这个方案还是不让它当大脑，只当转接头，并且只用在**我们自己没做的平台**上。

许可：AstrBot 是 AGPL-3.0-or-later。我们不改它、不分发它、不抄它的代码，只当独立程序经 HTTP 用，我们自己的代码不受 AGPL 约束（这是对许可的理解，不是法律意见）。它的启动器是 MIT。

## 2. 架构

```
平台 (个人微信 ClawBot / 飞书 / Telegram …)
   │ 平台自己的接口 (长连接、长轮询、Bot API)
   ▼
AstrBot (独立进程; WebUI 127.0.0.1:6185; 插件、工具、记忆、知识库都关掉)
   │ POST http://127.0.0.1:8793/v1/chat/completions
   │   Authorization: Bearer <访客 token>   model: wm-guest   头: x-astrbot-conversation-id
   ▼
桌宠宿主 (访客 token -> 只走 wm-guest; /mcp 403)
   │ 页面的 GuestLink (web/api-bridge.js) -> 服务端访客连接 /guest (不是 /ws)
   ▼
wm/ ChannelManager.guest_chat -> 访客会话 (熟人档: 没有工具、读不到主人记忆、不写记忆) -> 外发检查 -> 回复
```

- 前提是桌宠开着，而且本机接口开着。没开时 AstrBot 收到连不上的错误，不会回消息。
- 宿主只取请求里**最后一条 user 消息**。AstrBot 拼的系统提示（它的人设）、它带的历史、它给的工具列表，都不进我们的模型。
- 人设、模型、每日额度用面板 QQ 页选的那个预设，和 QQ 渠道共用（第 5 节）。

## 3. 哪些平台值得接

按"价值高、风险低、不用对公网开端口"排。AstrBot 的平台列表见[快速接入指南](https://docs.astrbot.app/platform/start.html)；这次读了下面几个平台的接入页，没读的标了"没读"。

| 顺序 | 平台 | AstrBot 适配器、怎么连 | 要公网吗 | 账号风险 | 价值 | 建议 |
|---|---|---|---|---|---|---|
| 0 | AstrBot 自带的 ChatUI、HTTP API | 内置；API 是 `POST /api/v1/chat`（要在 WebUI 建 API Key，带 `chat` 权限） | 否 | 无 | 只是测试 | **第一步**，不连任何账号 |
| 1 | **个人微信** | `weixin_oc`：基于微信官方的 `openclaw-weixin` 接口，扫码登录、长轮询收发 | 否 | 官方接口，风险低。要求手机微信 iOS ≥ 8.0.70 / 安卓 ≥ 8.0.69，并且微信里有 ClawBot 插件。能不能被别人加、能不能进群、有没有使用限制，文档没写，**没核实** | 高：在手机微信里就能找她 | **推荐第一批**（你手机上有 ClawBot 的话） |
| 2 | **飞书** | `lark`：推荐 socket 长连接；要 App ID / Secret，v4.25.0 起可以扫码一键创建 | 否（长连接） | 官方，低 | 中：看你用不用飞书 | **第一批候选** |
| 3 | 钉钉 | `dingtalk`：Stream 模式；要 ClientID / Secret；应用要发布、设可见范围 | 文档没写；Stream 模式一般不用公网，**没核实** | 官方，低 | 中 | 第二批 |
| 4 | Telegram | `telegram`：BotFather 拿 token；群里要关 Bot 的 Privacy mode | 否，但要能连上 Telegram（国内要代理，填 `http_proxy`） | 官方，低 | 中：海外的朋友 | 第二批 |
| 5 | Discord | `discord`（py-cord）：Bot Token，开三个 Intents；权限只给发消息、读历史，不给管理员 | 否，要代理 | 官方，低 | 中 | 第二批 |
| 6 | QQ 官方机器人 | `qq_official`：Websockets 方式（推荐）或 Webhook | Websockets 方式不用（没读接入页，**没核实**） | 官方，无 | 中：以后要商用时的 QQ 路线（external-channels.md 第 3 节） | 以后：和我们的 SnowLuma 渠道二选一 |
| — | 企微智能机器人、企微应用、微信公众号 | 回调方式：企业微信往 AstrBot 的回调地址推消息，默认端口 6198 | **要**（公网 IP 或隧道） | 低 | 低 | 不接：要对公网开端口 |
| — | OneBot v11（`aiocqhttp`） | 反向 WebSocket，接 SnowLuma / NapCat | 否 | 非官方协议，有封号风险 | — | **不接**：QQ 走我们自己的渠道 |
| — | Slack、LINE、KOOK、Mattermost、Misskey、Satori、Matrix、VoceChat | （没读接入页） | — | — | 低 | 有需要再说 |

- 你说的"个人微信（第三方服务，有封号风险）"：AstrBot 现在文档里的个人微信适配器走的是**微信官方的 ClawBot**，不是 WeChatPadPro、gewechat 这类第三方协议。第三方的不接。
- **QQ 为什么继续走我们自己的渠道**：
  - 有草稿门（你点了才发）；
  - 分了"本人·远程"一档（你私聊她时知道你的名字和喜好）；
  - 核对账号、只实现白名单里的 OneBot 动作；
  - 直接说 OneBot，少一层。

## 4. AstrBot 这边要关掉、锁死的

键名对着 [AstrBot 配置文件文档](https://docs.astrbot.app/dev/astrbot-config.html)（`data/cmd_config.json`）和各功能页。文档里只给了界面名字的，就写 WebUI 路径。AstrBot 4.29.3（2026-10-24 发布）改得很勤，装好后逐项核对。**装好后对着 4.29.3 源码核对过，以第 11 节和 `astrbot_lock.py` 为准**（这张表是装之前按文档写的，有几处键名和默认值不对）。

| 项 | 风险 | 怎么关、怎么设 | 我们这边的兜底 | 核实情况 |
|---|---|---|---|---|
| 模型服务商 | 别的服务商会绕过我们 | 只配一个 OpenAI 兼容的：`api_base` 填 `http://127.0.0.1:8793/v1`，key 填访客 token，`model` 填 `wm-guest`。STT、TTS、嵌入模型一个都不配 | 访客 token 只能用 `wm-guest` | 源码确认：用 `chat.completions`，会话号在 `x-astrbot-conversation-id` 头里，不带 `user` |
| 插件 | 插件在 AstrBot 的 Python 进程里跑，没有隔离 | `plugin_set: []`（空列表就是一个都不启用），WebUI"插件"页全关，不从市场装 | 管不到：靠配置和不装 | `plugin_set: []` 会不会连系统插件也关掉，**没核实** |
| 扩展指令 | `/op`、`/deop`、`/plugin`、`/model`、`/persona`、`/llm` 等 | 都在 `builtin_commands_extension` 插件里，停用它就没了 | — | 文档确认 |
| 主程序内置指令 | `/provider`、`/dashboard_update` 只有管理员能用；`/reset`、`/new` 私聊人人能用（无害）；`/help`、`/sid`、`/stats`、`/set` 能不能关，文档没说 | WebUI"插件 → 管理行为 → 指令"（先勾"显示系统插件指令"）里关掉不需要的 | — | 能不能整体关，**没核实** |
| 管理员 | 管理员能用高权限指令；"使用电脑能力"里管理员默认能执行代码、联网 | `admins_id: []`（默认是 `["astrbot"]`） | — | 文档确认 |
| 函数调用（人格里的工具） | 模型调工具时，AstrBot 会执行（插件工具、MCP、沙盒） | 新建一个空人格，"人格能力"里一个工具都不选，并设成 `provider_settings.default_personality`；全局工具管理里也停用 | **`wm-guest` 回复从不带 `tool_calls`**，AstrBot 的工具永远不会被执行。请求里带了 `tools` 我们也不用（第 6 节加提示） | 人格默认是不是"全部工具"，**没核实** |
| MCP | 同上 | WebUI"插件 → MCP"里不加服务器，也不从 ModelScope 同步 | 同上 | 文档确认：没有总开关 |
| Skills、SubAgent 编排 | 同上 | 不加 | 同上 | 只看了侧边栏标题，**没细读** |
| 主动型能力（`future_task`） | 定时调模型、主动往群里发 | "AI → 能力 → 主动型能力"的"启用"关掉；"扩展功能 → 未来任务"里有的话也删掉 | 我们的限频和每日额度兜着 | 文档确认（v4.14.0 引入，实验性） |
| 使用电脑能力 | 本机环境：管理员默认能执行 Shell / Python、联网；**Windows 没有本机隔离** | "AI → 能力 → 使用电脑能力"：普通成员、管理员的"文件访问范围"都设"关闭"（这样执行代码、联网都勾不上）；不配第三方沙箱 | 模型从不调工具，不会执行 | 有没有总开关，**没核实** |
| 网页搜索 | 回复里带链接、把对话内容发给搜索服务商 | `provider_settings.web_search: false`（默认），不填任何搜索 key | 外发检查去掉链接 | 文档确认 |
| 知识库 | 检索内容进提示词 | `default_kb_collection: ""`，不建知识库 | 宿主只取最后一条 user 消息 | 文档确认 |
| 人格 | 它的系统提示 | 只留上面那个空人格 | 宿主不用 system 消息；人设由面板 QQ 页的预设决定 | — |
| 群聊上下文、主动回复 | 把群聊记录塞进上下文，按概率主动插话 | `provider_ltm_settings`：`group_icl_enable: false`、`image_caption: false`、`active_reply.enable: false`（都是默认值） | 宿主只取最后一条 | 文档确认 |
| **上下文压缩** | 默认是"由 LLM 压缩"：上下文快满时（约 82%）额外调一次模型写摘要，也就是给 `wm-guest` 发一条"请总结"的消息，**会混进访客会话的历史** | "AI → 高级 → 上下文管理策略"改成"按对话轮数截断"，轮数设小一点（比如 10） | — | 文档确认；`cmd_config` 里的键名文档没给 |
| 文转图 | `t2i_strategy: remote` 会把回复发给官方渲染服务 | `t2i: false`（默认） | — | 文档确认 |
| 白名单 | **空着就是不过滤** | `platform_settings.enable_id_white_list: true`，`id_whitelist` 填允许的会话（在聊天里发 `/sid` 查） | 我们这边看不到平台账号，只能 AstrBot 管 | 文档确认 |
| 限流 | 被刷 | `platform_settings.rate_limit`：`time` 60、`count` 10、`strategy: discard`（默认 30 条、`stall`） | 访客会话按 `channels.qq.private` 限频 + 每日 token 额度 | 文档确认 |
| 内容安全 | — | `content_safety.internal_keywords.enable: true`，`also_use_in_response: true`（默认 false） | 我们的外发检查（隐私值、手机号、key、链接） | 文档确认 |
| WebUI | 被别人登录就能改一切 | `dashboard.host: 127.0.0.1`、改初始密码；"系统设置 → OpenAPI"只在测试时建 key，测完删 | — | 配置文档写默认 `0.0.0.0`，FAQ 和启动器说默认只听本机，前后不一致，**装好核对** |
| 文件服务 | 对外暴露文件链接 | `callback_api_base: ""`（默认） | — | 文档确认 |
| 代理 | — | `http_proxy` 只在接 Telegram / Discord 时填 | — | — |
| 匿名统计、自动更新 | — | FAQ 没提遥测，也没有自动更新；插件市场要连 GitHub | — | **没核实** |

## 5. 和我们 QQ 渠道的分工

| 事 | 我们的 QQ 渠道 | AstrBot + `wm-guest` |
|---|---|---|
| 平台 | QQ（SnowLuma，非官方，用小号） | 别的平台（第 3 节） |
| 会话怎么分 | 每个群、每个私聊一个访客会话 | 按 OpenAI 的 `user` 字段分；AstrBot 不带，**改成认 `x-astrbot-conversation-id` 头**，会话键是 `astrbot:<会话号>`（第 6 节）。AstrBot 里发 `/new` 会开新会话，我们这边也跟着换一个。群里每人一份还是一群一份，看 AstrBot 的 `unique_session` |
| 信任档 | 熟人；你的号私聊她是"本人·远程"（知道名字和喜好） | **一律熟人**：我们看不到对面是谁，不给你的记忆。就算是你自己从微信找她，也当熟人 |
| 发不发 | 群和好友默认草稿，你点了才发 | **AstrBot 直接回，没有草稿门**。所以白名单要窄：先只接你自己和信得过的人 |
| 限流 | 渠道管理器：群和私聊各自的间隔、每小时句数 | AstrBot 的 `rate_limit` 一层 + 我们按会话键的限频（`private` 那组）一层 |
| 花费 | 每日 token 额度（只算云端） | **和 QQ 共用同一个每日额度**，面板上一个数 |
| 外发检查 | 服务端访客会话的出口（隐私值、手机号、key、链接、@全体） | 同一套，在同一个地方。查到时回空串、结束原因标 `content_filter`：AstrBot 不重试，给对方发人格里的"出错时回复"（锁成 `……`）。原来以为"收到空回复就不发"，实际它会连问三次再发错误详情（第 11 节） |
| 渠道日志 | `<库>/外部渠道/qq/群<号>/日志/`，保留 30 天 | 现在不落盘，只有 trace（`channel: api`）。建议加 `<库>/外部渠道/api/<会话>/日志/`（第 6 节） |
| 记忆候选 | 还没做 | 还没做；以后和 QQ 同一套：只进候选，你确认才进记忆 |
| 人设、模型 | 面板 QQ 页选的预设 | 同一个（QQ 关着也用这一个） |
| 面板 | QQ 页 | 建议 QQ 页改名"外部渠道"，加一块"第三方框架（wm-guest）"：今天的请求数、token、拦下几次、最近的错误、AstrBot 有没有带工具（第 6 节） |
| 要开着的程序 | SnowLuma + QQ 桌面版 | AstrBot + 桌宠（本机接口开着） |

## 6. 我们这边要补的（等确认后做）

1. **宿主认 AstrBot 的会话头**（`desktop/host/src/api.cpp`）：`wm-guest` 请求没有 `user` 时，取请求头 `x-astrbot-conversation-id`（截到 64 字节），会话键写成 `astrbot:<会话号>`；两个都没有时原来是"都算一个人"，装好后发现 AstrBot 给 ChatUI 会话起标题的请求就不带会话号，各会话的第一句会串到一起，改成每句单独一个会话、用完就扔（第 11 节）。**已做（2026-10-25）**。
2. **带了工具记一笔**：`wm-guest` 请求里有 `tools`、`functions` 或 `tool_choice`（不是 `none`）时，宿主日志记一行，计数放进 `pet_state` 的 `guest`；面板 QQ 页用红字提示"去那边的人格里把工具关掉"。**已做（2026-10-25）**。
3. **服务端**：
   - `wm-guest` 的统计放进 `channel_status`（新的 `api` 段：请求数、token、拦下、会话数）；
   - 渠道日志 `<库>/外部渠道/api/<会话>/日志/`，和 QQ 一样保留 30 天。**已做（2026-10-25）**。
4. **面板**：QQ 页改名"外部渠道"，加上第 5 节那块；写清访客 token 文件在哪（`%APPDATA%\WorldModel\pet-api-guest-token.txt`）、怎么填进 AstrBot。**已做（2026-10-25）**。
5. （以后）每个来源单独选人设；"本人·远程"能不能映射到某个平台会话（比如你自己的微信）：要先确认对面真的是你，现在不做。

## 7. 下载清单（2026-10-25 用户确认后装了，实际大小见本节最后）

| 名称 | 来源 | 大小 | 许可 | 用途 | 说明 |
|---|---|---|---|---|---|
| AstrBot 启动器 v0.3.13（Windows x64） | github.com/AstrBotDevs/astrbot-launcher 的 Releases：`AstrBot.Launcher_0.3.13_x64-setup.exe` | 6,313,444 字节（约 6.0 MB） | MIT | 装、启动、升级 AstrBot | Rust + Tauri。**你手上已经有 0.3.10**（`Bot用\astrbot相关\AstrBot.Launcher_0.3.10_x64-setup.exe`，6,187,972 字节），用它就不用下载，只是旧几个小版本 |
| AstrBot 本体 v4.29.3 | github.com/AstrBotDevs/AstrBot（启动器从 Release 取） | 源码几十 MB 以内（没细算）；发布页只挂了一个 `AstrBot-v4.29.3-dashboard.zip`，5,358,020 字节 | AGPL-3.0-or-later | 转接头 | 要 Python ≥ 3.12 |
| Python 3.12 运行时 | astral-sh/python-build-standalone（启动器自己下） | 约 25–30 MB（**没核实**） | PSF | 跑 AstrBot | 本机系统 Python 是 3.10，不够；启动器把它放在自己的目录里，不动系统 Python |
| uv | astral-sh/uv（启动器自己下） | 约 15–20 MB（**没核实**） | MIT / Apache-2.0 | 装依赖 | — |
| AstrBot 的依赖：62 个直接依赖，加上间接依赖 | PyPI | 估计下载 150–250 MB、装好 400–600 MB（按主要的大包估，**没逐个算**；装之前用 `uv sync --dry-run` 核对） | 各自的许可，多为 MIT / Apache-2.0 / BSD（pandas、numpy 是 BSD，faiss 是 MIT） | — | **各平台的适配器 SDK 都在核心依赖里**（`python-telegram-bot`、`lark-oapi`、`dingtalk-stream`、`py-cord`、`slack-sdk`、`wechatpy`、`qq-botpy`、`aiocqhttp`），接哪个平台都不用另装。大包：`pandas`、`faiss-cpu`、`pillow`、`markitdown`、`reportlab` |
| Node.js | 启动器的镜像列表里有 | — | MIT | 用途不明 | **没核实**启动器会不会下 |

- 装在哪：用启动器的话，它把运行环境和依赖放在自己的目录（具体路径装好核对）；不进 `G:\RunProgram\WorldModel_v1`。
- **实际（2026-10-25 装好后）**：启动器 0.3.10 用手上的安装包（SHA256 和 GitHub 发布页的一致，安装包没签名），装在 `%LOCALAPPDATA%\AstrBot Launcher`（12 MB）。数据目录 `C:\Users\ASUS\.astrbot_launcher` 一共 853 MB：AstrBot 4.29.3 安装包 7,844,413 字节、源码 46 MB；Python 3.12 运行时 68 MB，启动器还顺带下了 3.10（66 MB）；uv 32 MB；依赖装在 uv 缓存里 608 MB（实例的 venv 是硬链接，只占 27 MB）。Node.js 没下。走的是本机代理，没开启动器的"中国大陆一键加速"、没换源；Python 和 uv 第一次下载报"error decoding response body"，单独重试就好了。
- 网络：GitHub 和 PyPI 走代理会慢（见本机下载网络的记录），换源先问。

## 8. 怎么测（不连真账号；已写成 astrbottest.ps1，结果见第 11 节）

1. **准备**：
   - 8794 的 mock 服务（`--llm mock --tts mock`，临时库 + 单独的 `trace.log_dir`）；
   - 一个测试桌宠（设置在 `build\selftest` 下，本机接口开在 8795 或 8815）；
   - AstrBot 用单独的数据目录，WebUI 只听 127.0.0.1（端口用 6185，被占就换），服务商指到测试桌宠的 `/v1`，填访客 token。
2. **ChatUI 手测**：在 AstrBot 的 ChatUI 里说几句，看回复是不是从 `wm-guest` 来的（服务端 trace 里有 `channel: api`）。
3. **HTTP API 自动测**（`POST /api/v1/chat`，API Key 只给 `chat` 权限，测完删掉），写成 `desktop\scripts\astrbottest.ps1` + `astrbot_test.py`：
   - 一问一答能通（SSE 流式）；
   - 两个 `username` 是两个 AstrBot 会话，到我们这边是两个访客会话：甲说的金丝雀串不出现在乙的请求里（要第 6 节第 1 条）；
   - 人格里不选工具时，请求里没有 `tools`（看宿主日志的计数，要第 6 节第 2 条）；故意给人格开一个工具，AstrBot 日志里也没有工具被执行；
   - `/op`、`/plugin` 在聊天里没有，`/provider` 被拒；
   - 上下文改成按轮数截断以后，长对话里没有"请总结"类的请求打到 `wm-guest`（服务端 trace 的条数等于用户发的条数）；
   - mock 回复里放主人的住处，AstrBot 收到空回复；
   - 主人的记忆文件逐字节不变；访客 token 调 `/mcp` 回 403；
   - `netstat` 看 6185 只绑 127.0.0.1。
4. **真平台**：上面都过了以后，接一个平台（第 3 节第一批），用测试账号，白名单里先只放你自己。

## 9. 要用户定的（2026-10-25 定了：启动器、先 ChatUI / API、白名单只放自己、和 QQ 共用人设和额度、第 6 节都做）

1. **先接哪个平台**：推荐先 ChatUI / API 试，再接个人微信（ClawBot）或飞书，看你用哪个。
2. **装法**：用启动器（新版 0.3.13 约 6 MB；或者你手上的 0.3.10，不用下载），还是 uv 源码部署（要 Python 3.12）。推荐启动器。
3. **没有草稿门**：AstrBot 收到就直接回。你接受吗？还是只接你自己的会话？
4. **第 6 节的改动**：做不做。第 1 条（认会话头）不做的话，所有人会挤进同一个会话，推荐做。
5. **共用设置**：`wm-guest` 和 QQ 共用一个人设、一个每日额度，行不行。

## 10. 风险和限制

- AstrBot 是一个大的 Python 程序（62 个直接依赖），插件和它在同一个进程里，没有隔离。我们只能靠"大脑在我们这边"兜底，它自己的漏洞管不到（比如 WebUI 被别人登录）。所以 WebUI 只听本机、强密码、不装插件。
- AstrBot 改得很勤（4.29.3 在 2026-10-24 发布），配置项名字可能变；文档里也有前后不一致的地方（`dashboard.host` 的默认值、`tool_call_timeout` 的默认值）。
- 各平台有自己的机器人政策。不冒充真人，号的名字写明是 AI 角色。
- 第 4 节标了"没核实"的，装好后逐项核对，再写回这篇。

## 11. 装好后核对出的（2026-10-25，4.29.3 源码 + 自检）

**改了我们这边的三处**：
1. **空回复会被连问三次**。`wm-guest` 不回（外发检查拦下、她选了不说）时回空串，原来结束原因是 `stop`。AstrBot 把"`stop` 加空内容"当成模型出错（`EmptyModelOutputError`），写死重试 3 次（每次都进访客会话、各算一次额度），最后给对方发"LLM 响应错误: All chat models failed: …"。改成标 `content_filter`（宿主 `api.cpp`，流式和非流式都改）：它不重试，给对方发人格里的"出错时回复"。实测拦下的那句只问一次。
2. **起标题的请求会串会话**。ChatUI 和 HTTP API 的每个新会话，AstrBot 会让模型起个标题（没有开关），这个请求不带会话号，到我们这边全进同一个"某人"访客会话，各会话的第一句在那里互相看得到。改成没署名的请求每句单独一个会话、用完就扔（服务端 `manager.py`，单测 `test_guest_anonymous_requests_do_not_share_history`）。只有 ChatUI / HTTP API 起标题，接平台时没有这个请求。
3. **桌宠隐藏时一句要等一分多钟**。页面转 `wm-guest` 时每 100 毫秒用 `setTimeout` 轮询"访客连接连上没有"，桌宠隐藏（全屏程序、你把她藏起来）时 WebView2 会压页面的计时器，第一次自检第一句等了 95 秒。改成等 WebSocket 的 open / close 事件（`web/api-bridge.js`），实测 1.5–3 秒（测试桌宠一启动就因为检测到全屏程序藏起来了，正好测到这种情况）。

**在锁配置里关掉的四处**（`astrbot_lock.py`）：
4. **默认每次请求都带一个"未来任务"工具**：`provider_settings.proactive_capability.add_cron_tools` 默认 true，锁 false。另外，能主动发消息的平台（ChatUI 也算）会固定加一个 `send_message_to_user` 工具，没有开关；把模型的 `modalities` 写成 `["text"]`（不含 `tool_use`）后，AstrBot 自己就把工具从请求里拿掉。实测全程 0 次带工具。
5. **默认人格的工具是 null，意思是"全部工具"**；ChatUI 在默认人格下还会换成它内置的 ChatUI 人设，同样是全部工具。所以不能改 default 人格，要另建一个（`wm-guest-empty`，工具、Skills 都是空列表）并设成 `agent_runner.config.persona.persona_id`。
6. **往对方那句话后面拼 `<system_reminder>`**（当前时间；开了 `identifier` 还有平台用户 ID 和昵称，开了 `group_name_display` 还有群名）。宿主只取最后一条 user 消息，拼了就进她的上下文。`datetime_system_prompt` 默认 true，三项都锁 false。
7. **收到第一条消息后去 `api.soulter.top` 拉文转图的官方地址**（文转图关着也拉；本机连不上，55 秒后超时，不挡回复）：`t2i_endpoint` 填本机占位地址 `http://127.0.0.1:9` 就不拉了，反正用不到。

**其他核对结果**：
- **键名**：上下文压缩在 `agent_runner.config.compression`（`overflow_strategy: truncate_by_turns`、`max_turns: 10`），默认模型和人格在 `agent_runner.config.model` / `.persona`；模型服务商分两层，`provider_sources`（地址和 key）和 `provider`（模型）。
- **插件和指令**：`plugin_set: []` 停不掉内置的两个插件（`astrbot`、`builtin_commands` 是保留插件，照样加载）。要加 `disable_builtin_commands: true`（4.29.3 有）才不认 `/op`、`/plugin`、`/provider`、`/sid`、`/help`、`/new`，实测这些都当普通的话转给 `wm-guest`。
- **白名单**：空着就是不过滤；ChatUI / HTTP API 一律豁免；`admins_id` 里的人默认也豁免（`admins_id` 已清空）。锁成一条谁都对不上的占位，接平台以后对方一发消息，日志里就会打出他的会话号。
- **重试**：默认 429、5xx、连不上都重试，`request_max_retries` 默认 5（含第一次）。锁成 1（不重试），因为每次重试都会把同一句再塞进访客会话。
- **限流**：`rate_limit` 60 秒 10 句、`discard`，实测生效（超了的直接丢，对方什么也收不到）。
- **WebUI**：配置文件的默认值是 `0.0.0.0`，启动器用环境变量 `DASHBOARD_HOST=127.0.0.1` 传（优先于配置）；锁里配置也写 127.0.0.1。netstat 实测只听 127.0.0.1:6185。初始密码随机 24 位，第一次登录要改。
- **遥测**：有。收到消息、调模型时，把计数、主机名、安装 ID 发到 `tickstats.soulter.top`；锁 `disable_metrics: true`（也可以用环境变量 `ASTRBOT_DISABLE_METRICS=1`）。锁好之前没收过消息，所以一次也没发过，`~/.astrbot/.installation_id` 也没生成。
- **启动时的外部请求**：拉 `models.dev/api.json`（公开的模型列表，拉不到换 `models.opencode.ai`，不带对话内容），关不掉。启动器装的 AstrBot 不自己更新；不打开插件市场就不连。
- **"使用电脑能力"**：`computer_use_runtime` 默认就是 `none`；本机权限的默认值是管理员能执行、能联网、能访问整台电脑，锁成两档都是 `none`。
- **AstrBot 自己也存一份聊天**：`data/data_v4.db` 里的 conversations（对话内容）、platform_message_history、provider_stats。我们不读它，但别人说的话会在它那边留着；自检测完会删掉测试聊天。
- **对方看到的"……"**：收到 `content_filter` 或 429 时，AstrBot 给对方发人格的"出错时回复"；这个不能设成空（空了就发它自己的错误详情），所以"她选了不说"在 AstrBot 那边也会变成一个 `……`。

**自检**（`desktop\scripts\astrbottest.ps1`，测什么见 desktop/README.md 的"自检"一节）：2026-10-25 最后一遍 29 项全过，第一句 3.0 秒回来。前几遍没过的都在上面：空回复问了 4 次、起标题串会话、第一句 95 秒、对方的话后面拼了时间；还有两处是测试自己的问题（AstrBot 的限流把长对话挡掉两句；临时库没清干净，脚本里的清空改成删不掉就停）。另外有一遍和别的会话的开发版桌宠自检撞了（同一份 `build\dev`），测试桌宠没起来，重跑就好。回归：服务端 `test_channels` 29（新增 1 条）、`qqtest` 25（补了 `--no-autohide`）、`apitest` 33 全过。

## 12. 怎么用

1. 桌宠面板打开本机接口（默认 8793）。访客 token 在 `%APPDATA%\WorldModel\pet-api-guest-token.txt`。
2. AstrBot 的模型已经指到 `http://127.0.0.1:8793/v1`，key 是这个访客 token，模型 `wm-guest`（`astrbot_lock.py` 写进去的）。换了端口或重新生成了 token，停掉 AstrBot 后再跑一次 `astrbot_lock.py`。
3. 在启动器里启动 `wm-guest` 实例，浏览器开 `http://127.0.0.1:6185`，用户名 `astrbot`，密码在 `C:\Users\ASUS\.astrbot_launcher\wm-guest-webui-password.txt`。第一次登录要改密码，改完把这个文件删了。
4. 在 WebUI 的 ChatUI 里说话，就是在和 `wm-guest` 说话：熟人档，没有工具、读不到你的记忆、不写记忆。
5. 在 WebUI 里改过设置后，停掉 AstrBot，跑一次核对，看有没有被改松（去掉 `--check` 就改回来）：
   `python desktop\scripts\astrbot_lock.py --data C:\Users\ASUS\.astrbot_launcher\instances\c8c8d430-e1f0-496b-9bd6-7760a28a106d\core\data --check`
6. 接平台（第 3 节第一批）之前再定：白名单先换成你自己的会话号；没有草稿门，AstrBot 收到就直接回。
7. 启动器的"备份"页能给实例做备份（2026-10-25 试过：一份 5.4 MB 的 zip，在 `C:\Users\ASUS\.astrbot_launcher\backups`）。**备份里有 `cmd_config.json`，带着访客 token 和 WebUI 密码的哈希**，不要外传。启动器的界面和源码里学到的东西见 [harness-learning.md](harness-learning.md) 第 1 节。

## 资料来源（2026-10-25 查阅）

- [AstrBot 配置文件](https://docs.astrbot.app/dev/astrbot-config.html)、[内置指令](https://docs.astrbot.app/use/command.html)、[插件](https://docs.astrbot.app/use/plugin.html)、[MCP](https://docs.astrbot.app/use/mcp.html)、[Agent 沙盒环境](https://docs.astrbot.app/use/astrbot-agent-sandbox.html)、[主动型能力](https://docs.astrbot.app/use/proactive-agent.html)、[上下文压缩](https://docs.astrbot.app/use/context-compress.html)、[内置 Agent 执行器](https://docs.astrbot.app/providers/agent-runners/astrbot-agent-runner.html)、[WebUI](https://docs.astrbot.app/use/webui.html)、[HTTP API](https://docs.astrbot.app/dev/openapi.html)、[FAQ](https://docs.astrbot.app/faq.html)
- 平台：[个人微信](https://docs.astrbot.app/platform/weixin_oc.html)、[飞书](https://docs.astrbot.app/platform/lark.html)、[钉钉](https://docs.astrbot.app/platform/dingtalk.html)、[企微智能机器人](https://docs.astrbot.app/platform/wecom_ai_bot.html)、[Telegram](https://docs.astrbot.app/platform/telegram.html)、[Discord](https://docs.astrbot.app/platform/discord.html)
- 部署：[启动器一键部署](https://docs.astrbot.app/deploy/astrbot/launcher.html)、[源码部署](https://docs.astrbot.app/deploy/astrbot/cli.html)、[AstrBotDevs/astrbot-launcher](https://github.com/AstrBotDevs/astrbot-launcher)（[最新发布](https://github.com/AstrBotDevs/astrbot-launcher/releases/latest)）、[AstrBot 最新发布](https://github.com/AstrBotDevs/AstrBot/releases/latest)
- 源码：[openai_source.py](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/provider/sources/openai_source.py)（不带 `user`、`extra_headers=build_conversation_headers(...)`、只在有工具时发 `tools`）、[headers.py](https://github.com/AstrBotDevs/AstrBot/blob/master/astrbot/core/provider/headers.py)（`x-astrbot-conversation-id`）、[pyproject.toml](https://github.com/AstrBotDevs/AstrBot/blob/master/pyproject.toml)（Python ≥ 3.12、AGPL-3.0-or-later、62 个直接依赖）
- 本机：`Bot用\astrbot相关\AstrBot.Launcher_0.3.10_x64-setup.exe`（6,187,972 字节）；`Bot用\人格提示词及表情包\表情插件用法.docx`（你以前用的 meme_manager 插件，这个方案里不装）
