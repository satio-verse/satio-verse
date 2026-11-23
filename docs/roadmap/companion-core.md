# 陪伴底层: 记忆鲜活度、会话寿命、人设分层、好感度与心情、主动聊天

2026-10-24 摸底 + 方案。参考了 DeepSeek Harness (dsh) 社区插件的公开资料（只读网页，没下载、没装）。

| 阶段 | 状态 |
|---|---|
| 一：记忆鲜活度 + 候选确认 | **完成**（2026-10-24，用户按推荐默认同意；做法和实测见根目录 README "记忆鲜活度和候选记忆"） |
| 二：好感度、心情、语气 | 未开始（协议要和桌宠会话对齐） |
| 三：人设分层 + 去 AI 腔 | 未开始 |
| 四：会话寿命 | 未开始 |
| 五：主动聊天 | 未开始 |
| 六：QQ / 微信 / 手机 | 方案见 [external-channels.md](external-channels.md)。用户按推荐定了；**6a（服务端 QQ 渠道 + 隔离测试）和 6b（桌宠面板 QQ 页、草稿卡片、`wm-guest`）完成**（2026-10-24，README"外部渠道：QQ"、`desktop/README.md`"QQ"），没下载 SnowLuma，没连过真 QQ |

第一阶段比方案多了一条：问句里有"之前""上次""还记得"这类主动回想的说法时，这一轮不按鲜活度压低（看了第五组剧本的结果后加的）。

第一阶段之后用户追加：**常提升长期**。同一件事在不同的日子提到或想起够 3 天（说的那天算第一天）就升成长期，候选提到两天以上标"常提"、仍要确认（README "记忆鲜活度和候选记忆"）。

之后用户提出**记忆图谱**：人、物、地点并组、两跳回答、按类列举、实体属性更正，Obsidian 里 `小流/图谱/` 每个实体一篇笔记（README "记忆图谱：人、物、地点"）。每个方向的尝试和结果记在知识库 `07 实验与评测/记忆方向实验记录.md`。

## 1. 现状摸底

### 服务端 (`wm/`)

| 能力 | 现在怎么做 | 文件 |
|---|---|---|
| 长期记忆 | Obsidian 库 `<vault>/小流/`。<br>• `记忆/关于用户.md`: 规则提取用户的直接陈述（我叫/我喜欢/住在/生日/记住…），**自动写入**，带日期和 `#auto-memory`。<br>• `记忆/对话摘要.md`: 空闲时整理日志，规则初筛 + 0.8B 判"值不值得记"，**自动写入**，带来源。<br>• 知识更新只划不删（"更正为…""用户要求忘掉"）。 | `memory.py`、`memory_ops.py`、`digest.py` |
| 检索 | BM25 / 向量（bge、Qwen3-Embedding），限时 80 ms；联想一步；问用户自己的事时由服务端直接回答（防 0.8B 把"我"答成自己）。结果拼成一行 `[记忆] …` 接在本轮输入后面（不进前缀缓存）。 | `memory.py` |
| 时间 | 只处理"安排"：近三天的安排第一轮带上；过去的安排最多留一条，显示"(7天前)"。**普通记忆没有按时间淡化**（待办里写着"按时间淡化旧记忆""检索时再巩固"没做）。 | `memory.py _fade_past`、`memory_ops.with_time` |
| 关系状态 | `关系.md` frontmatter：初次见面、见面次数、聊过几轮、熟悉程度（四档，按次数/轮数/天数）、**用户**上次的情绪。会话第一轮带一句关系概况。**没有角色自己的好感度和心情。** | `memory.py` |
| 社交后果 | 每轮记"角色做了什么 → 用户下一句怎么反应"（接着聊/转话题/敷衍/不满），只写日志，不参与决策。 | `social.py` |
| 会话历史 | 本地 0.8B：每个槽位 4096 token，前缀（系统提示 + few-shot）约 2500。历史 3 轮，超过 6 轮一次性截回 3 轮；超过 1600 字也截。**截掉的轮次就丢了**（日志里有原文，但日志默认不参与检索）。云端模型用同一套截断。 | `pipeline.py _remember` |
| 人设 | 系统提示里写死「小流」、输出 JSON（op/acts/emo/reply，reply ≤ 40 字）。桌宠的人设预设通过 `profile.persona`（≤ 2000 字）接在系统提示后面，本地和云端都用。 | `intent.py`、`cloud.py with_profile` |
| 情绪 | `emo` 是模型每轮自己给的 8 个标签之一，驱动表情和身体能量；不跨轮保持。 | `intent.py EMOTIONS` |
| 主动聊天 | 没有。只有"记忆里有近几天的安排时第一轮会带上"。 | — |

### 桌宠 (`desktop/`)

- 人设预设：操作面板里每个预设 = 一段自由文字 + 模型（本地 / DeepSeek / 千问 / Kimi / 自定义）+ MCP 服务器 + skill。默认预设不发 `profile`。
- `state.activity`（前台程序类别 + 时长）已经接上服务端。
- 小屋模式（`room-life.js`）：桌宠会话正在做小屋里的**心情和投喂**，打算像 `state.activity` 一样把心情、需求放进 `state`。

### 本机 dsh 0.2.1-rc.2 (`desktop/third_party/dsh`)

304 个官方包，**没有装社区插件**；`dsh-home/cordis.patch.yml` 只把桌宠接成 MCP 服务器。和这次相关、可以参考的：

| 包 | 做什么 | 能借鉴的 |
|---|---|---|
| `dsh-persona` / `dsh-system-prompt` | 人设前缀 (order 0) + 后缀 (order 10200) 两个具名段；前缀不变时 KV 缓存稳定 | 人设分段、固定顺序、变化的东西放后面 |
| `dsh-compaction-basic` + `dsh-token-meter` | 默认在上下文的 80% 触发，最旧的一段换成一条结构化摘要，近期 16% 逐字保留；溢出时压缩再重试；摘要复用会话前缀省缓存 | 云端长会话的压缩时机和"保留近期原文" |
| `dsh-schedule` | 一次性 / 每天 / 每周 / cron 提醒，重启后补发最近错过的一次，投递回原会话 | 约定类提醒、主动开口 |
| `dsh-time-context` | 每步给模型当前时间和"距上一条消息过了多久"（默认 10 分钟一次） | 主动聊天和心情需要的"隔了多久" |
| `dsh-plugin-market` | 默认不配任何索引源，不会自己联网 | — |

这些都是 TypeScript 的 cordis 插件，跑在 dsh 的智能体会话里（云端大模型、写代码场景）。**不能直接装进 `wm/`**（Python + 本地 0.8B），只借思路。

## 2. 社区插件公开资料（只读网页，未下载）

| 用户提到的名字 | 找到的仓库 | 核实情况 | 机制要点 | 许可 |
|---|---|---|---|---|
| 鱼的记忆 | [Yuuyuko-uu/dsh-fish-memory](https://github.com/Yuuyuko-uu/dsh-fish-memory) | README 已读 | 每条带时间；**鲜活度默认半衰期 30 天**，不删、只是越来越难想起；画像/喜好/约定标成"长期事实"不过期；事实键"对象.方面"，同键只留最新、旧的标"已被取代"可撤销；每天最多记 8 条；召回按命中词特定程度 + 鲜活度 + 重要度 + 情绪排序，最多注入 6 条 / 800 字；设置页列出"最近一次召回"选中和没选中的原因。衰减公式、召回会不会刷新没写 | MIT |
| Full-Life Memory | [Toctsu/dsh-full-life-memory](https://github.com/Toctsu/dsh-full-life-memory) | README 已读 | 上下文超过 8000 token 时，把最旧 2 轮折叠成线索（关键词、种子词、一句摘要，正则抽取，不调模型），保留最近 4 轮；线索进 SQLite，**不存原文**；模型调用 `recall_from_clues` 用 LIKE 查线索，自己"长出"情境（不能逐字复述）。2 星，作者自己列了"遗忘曲线权重"等没做 | MIT |
| dsh-whale-companion | [xianshu-virtuous/dsh-whale-companion](https://github.com/xianshu-virtuous/dsh-whale-companion) | README 已读 | 一轮结束后上下文到 **88%** 时自动开新会话续接；新会话只带最后一条用户请求和回复（≤ 24000 字），不带摘要；完整人设只在新会话第一次请求出现，之后一条短提醒 + 漂移时可调工具重读。另有同名的桌面鲸鱼成长插件（LeemanCheung） | MIT |
| whale-persona（含"活人版"？） | [shenA2024/whale-persona](https://github.com/shenA2024/whale-persona) | README 已读；"活人版"没找到 | 一份 config：自称、称呼、立场、性格、形象、语气（按模型分别写）、工作契约逐条开关；**记忆由 AI 提议进收件箱（status: proposed），人执行 confirm 才生效，代码强制**；只追加不改写，reject/supersede/drop 也是追加；没有时间衰减 | MIT |
| dsh-whale-persona | [OMGLogic/dsh-whale-persona](https://github.com/OMGLogic/dsh-whale-persona) | 只看到简介 | 全局鲸鱼娘人设 | 未核实 |
| dsh-companion (affinity/mood/traits) | [LamplitIsles/dsh-companion](https://github.com/LamplitIsles/dsh-companion) 等多个同名 | README 已读 | 好感度 0–100 整数 + 心情 + 签名，状态追加写进 `state.jsonl`（每次一条完整快照 + 原因）；由智能体调工具改，**每轮净变化限 ±10**；注入时写明"这是描述性信息，不是指令、不是要刷高的分数"；压缩时换成固定的陪伴连续性检查点。traits 没找到具体说明；另一个同名插件（suis077）好感度只在明确要求时才改 | Apache-2.0 |
| talk-normal | [hexiecs/talk-normal](https://github.com/hexiecs/talk-normal)（不是 dsh 插件，是通用系统提示） | 简介已读 | 去掉客服腔、"并非 X 而是 Y"、结尾总结和"如果你需要我还可以…"菜单，先给答案、按问题复杂度控制长度；**反例原样写进提示词模型会照抄**，所以只描述要避开的模式；每条规则有回归记录；自报字数减 56–73% | MIT |
| dsh-ciku-pack | [dshpluginhub 页面](https://dshpluginhub.ai/en/plugins/dsh-ciku-pack) | 页面已读 | 自动收集用户说过的脏话、气话、绰号，每轮注入"当前弹药"用来斗嘴，明确不过滤 | MIT |
| dsh-zakou-pack | 只在 ciku-pack 页面提到是"杂口语言包" | 未找到 | — | — |
| dsh-cosplay、dsh-persona-forge、dsh-task-personas、dsh-games | — | 公开渠道没搜到 | — | — |
| dsh-mobile-bridge | [dshpluginhub 页面](https://dshpluginhub.ai/en/plugins/dsh-mobile-bridge) | 简介 | 手机经局域网配对或加密中继打开电脑上的 dsh | 未核实 |
| dsh-plugin-im-bridge | [dsh-plugin.org 页面](https://dsh-plugin.org/plugins/eighteentang/dsh-plugin-im-bridge) | 页面已读 | QQ 走官方机器人（AppID/Secret，沙箱，家宽只能沙箱）、微信走 8.0.70+ 的 ClawBot 扫码；每个平台一个专属会话，结果回发到聊天 | MIT |

顺带看到但用户没提的：[derkinL/dsh-remembers](https://github.com/derkinL/dsh-remembers)（陪伴向记忆，原话→事实→画像→事件→里程碑分层、每晚巩固、"想不起就说想不起"，衰减参数没公开，MIT）、dsh-session-continuity（新会话开头注入上一会话的指针）、dsh-brainagent（情绪记忆 + 主动行为）。

## 3. 对比：需求 × 插件做法 × 我们现在 × 建议

先说两条贯穿全文的约束：
- **本地是 0.8B**：提示词每多一句都要量效果（实测"给小模型的东西越少越要紧"）；人设文字和心情描述对它的语气影响很弱，真正改语气要靠 LoRA 数据或服务端规则。云端模型（DeepSeek 等）才能吃下完整的分层人设。
- **前缀缓存**：系统提示 + few-shot 是固定前缀。每轮会变的东西（心情、好感度、想起来的事）只能像 `[记忆]`、`[状态]` 一样接在本轮输入后面。

| 需求 | 插件的做法 | 我们现在 | 建议 | 放哪 |
|---|---|---|---|---|
| 鲜活度 / 半衰期 | 鱼的记忆：半衰期 30 天，长期事实不过期 | 只有"过去的安排"会淡出 | 事件类记忆按半衰期淡化：越久，检索门槛越高（不是删，问得够准仍然想得起来）；长期事实（名字、住处、生日、喜好、约定、用户手写的笔记）不淡化；被想起来一次就刷新 | 服务端 |
| 候选记忆 + 用户确认 | whale-persona：提议进收件箱，confirm 才生效 | 规则提取和日志整理都直接写入 | 新增 `记忆/候选.md`（Obsidian 复选框），模型判断出来的（日志整理、以后云端的 remember 工具）先进候选；勾上转正，不要的删掉或划掉；候选不参与检索。WS 也给一对消息，桌宠面板可以做确认按钮 | 服务端（面板 UI 归桌宠） |
| 会话寿命 | whale-companion 88% 续接；Full-Life 折叠成线索、提到时重建；compaction-basic 80% 摘要 | 截掉的轮次直接丢；日志有原文但不检索 | 本地：截掉的轮次留一条线索（关键词 + 实体 + 指向日志那一行）；用户说"刚才/之前/上次…"且关键词对上时，把**日志原话**作为一行 `[想起]` 带上（比 Full-Life 好：我们有原文）。云端：历史预算放大，到 85% 时把最旧一半折成前情提要（默认用规则线索，不花钱；可选用云端模型写摘要） | 服务端 |
| 人设分层 | whale-persona 的自称/称呼/立场/语气；talk-normal；whale-companion 首轮全文、之后短提醒 | 一段自由文字 | 人设卡分层：身份（名字、自称、称呼用户）→ 说话风格（自己写的"说人话"规则）→ 个性缺陷（会怼、敷衍、闹别扭，带底线）→ 动态状态（心情、好感度，接在本轮输入后面）。桌宠面板给结构化字段，拼好再发 `profile.persona`，服务端协议可以不改 | 桌宠为主，服务端出风格包 |
| 好感度 / 心情 / 特质 | dsh-companion：0–100、每轮 ±10、只是描述性信息 | 只有熟悉程度和**用户的**情绪 | 好感度长期、跨设备 → 服务端存在 `关系.md`，按规则小步变（每轮 ±3、每天 ±8），原因写进日志；角色当下的心情和需求 → 桌宠（小屋）维护，经 `state` 上报；服务端把对话引起的心情变化作为提示下发，由桌宠合进去。特质先从人设卡读，攒数据后再做漂移 | 好感度服务端；心情桌宠 |
| 语气跟着心情 | 插件都是写进提示词 | — | 云端：本轮输入后面带一行心情 + 人设里写清各心情怎么说话。本地：同样带一行，再量 0.8B 跟不跟；跟不上就记成 LoRA 的数据需求。语音：以后按心情换 GPT-SoVITS 参考音 | 服务端 |
| 主动聊天 | dsh-schedule 提醒；brainagent 主动行为 | 没有 | 桌宠知道人在不在、全屏、免打扰，负责发起（`nudge`）；服务端知道记忆和安排，负责说什么；双方都限频。约定类提醒（"每天十点提醒我喝水"）由服务端排程 | 桌宠发起，服务端出内容 |
| QQ / 微信 / 手机 | im-bridge、mobile-bridge | 桌宠有本机 OpenAI 兼容接口和 MCP | 放最后。方案见 [external-channels.md](external-channels.md)：外部渠道一律是访客会话，渠道记忆另放、主人记忆检索里排除 | 服务端为主，面板归桌宠 |
| 斗嘴词库 | ciku-pack 收集用户脏话、不过滤地反用 | — | 不照搬。"会怼人"放进个性层，带底线（用户难过时收起来、不攻击外貌身份、闹别扭能哄好） | — |

## 4. 分阶段方案

### 第一阶段：记忆鲜活度 + 候选确认（服务端，先做这个）

理由：只动服务端记忆层，和桌宠、LoRA、TTS 会话都不冲突；正好是待办里的"按时间淡化旧记忆""检索时再巩固"；现有的检索基准和长记忆基准能直接量回归。

**做什么**
1. **记忆分两类**
   - 长期：`关于用户.md` 里规则提取的槽位和喜好、"记住…"、以后的约定、用户自己写的笔记、`关系.md` 正文；
   - 事件：`对话摘要.md` 的条目、带日期的经历和安排；
   - 行尾写 `#长期` / `#事件` 可以手动改类别。
2. **鲜活度**
   - 公式：`f = 0.5 ^ (天数 / 半衰期)`，天数从"记下的日期"和"上次被想起"里取较晚的一个；
   - 越旧越难想起：BM25 分数乘 `0.5 + 0.5f`；向量的相似度门槛加 `Δ·(1−f)`（Δ 先取 0.08，用基准定）。很久以前的事问得准仍然想得起来，擦边的不再冒出来；
   - 想起来时显示多久以前（复用 `with_time`，比如"(3个月前)"）。
3. **想起一次就刷新**（检索时再巩固）
   - 真的带进提示词的事件记忆，把"上次被想起"记到 `记忆/.recall.json`（点开头，Obsidian 不显示）；
   - 不改用户能看到的行。
4. **候选记忆** `记忆/候选.md`
   ```markdown
   ## 2026-10-24
   - [ ] 用户下周要去北京出差 (来源: [[2026-10-24]] 14:02) #候选
   ```
   - 日志整理（`digest.py`）出来的条目改写到这里；每天最多 8 条（`memory.confirm.daily_max`）；
   - 候选不参与检索；整理去重时算上候选和拒掉的，不会反复提；
   - **确认**：Obsidian 里勾上 `[x]`，下一次刷新就移到 `对话摘要.md`，注明"(确认于 日期)"；
   - **不要**：删掉那行，或划掉；
   - **WS**：下行 `memory_candidates {items:[{id,text,source,date}]}`（连上时和有新候选时发），上行 `memory_review {id, ok}`，回 `memory_review_done`。桌宠面板做按钮是桌宠会话的事。
5. **排查**：`recall()` 的埋点多记"差一点没想起来的"几条和原因（比如"太久了，鲜活度 0.21"），对应鱼的记忆里"为什么没想起来"那一栏。
6. **配置**
   - `memory.freshness {half_life_days: 30, refresh_on_recall: true}`；
   - `memory.confirm {mode: "candidates" | "auto", daily_max: 8}`；`auto` 就是现在的行为，评测和想省事时用。

**改哪些文件**：`wm/memory.py`、`wm/memory_ops.py`、`wm/digest.py`、`wm/pipeline.py`（两条 WS 消息）、`config/default.json`、新测试 `tests/test_memory_fresh.py`、`tools/bench_longmem.py`（加剧本）、README 记忆一节和协议一节。

**怎么测**
- 单元测试（假时钟）：
  - 衰减曲线；
  - 长期 / 事件分类和手动标签；
  - 旧事件擦边问法想不起、准确问法想得起；
  - 一年前的长期事实照样想得起；
  - 想起后刷新；
  - 候选的写入、去重、每日上限；
  - Obsidian 勾选转正，删掉、划掉算拒绝；
  - WS 确认和拒绝；
  - 用户手写的行不动。
- 回归：
  - 全量 pytest；
  - `bench_recall`（BM25 和本机已有的嵌入）改前改后对比，召回@3 和误命中不能变差；
  - `bench_longmem` 的 132 次，用 BM25 和本机已有的嵌入跑。它要真模型（8784，和别的会话共用），跑之前先说一声。
- 新剧本："3 个月前的考试"、"一年前说的名字"、"候选没确认时不该知道，确认后知道"、"同一件事隔一个月被问两次"。
- 端到端：单独端口起 mock 服务（`--tts mock`、`--set memory.vault=<临时库>`、单独的 `trace.log_dir`），走一遍 WS 的候选和确认。
- 不往 8790 发测试对话，不重启 8790 / 8784。

**完成标准**：测试全过、基准不退步、README 和知识库（关系记忆层、长记忆研究与设计、配置参考、WebSocket 协议、评测汇总、开发日志，有坑就写问题排查记录）更新。

### 第二阶段：好感度、心情、语气（服务端 + 桌宠协议，要和桌宠会话对齐）

协议草案（字段名待和桌宠会话商量）：
- 上行：
  - `state.mood`：角色当下的心情，固定取值，比如 `happy|calm|bored|sleepy|sulky|upset`；
  - `state.needs`：比如 `["hungry"]`；
  - 两者都由小屋维护。服务端只认固定取值，和 `activity` 一样清洗后变成一句状态。
- 下行：
  - `relation {affinity, level, delta, reason}`：好感度变了才发；
  - `mood_hint {mood, reason}`：对话引起的心情变化，比如被夸了、被凶了，由桌宠合进它的心情；
  - 网页版没有小屋时，服务端自己保留一个简单心情。

服务端：
- 好感度存在 `关系.md` 的 frontmatter，人能直接改；
- 规则变化：接着聊 +1（每天最多 +5）、道谢/夸她/分享自己的事 +2、不满/打断 −3（复用 `social.py` 的标注）、投喂之类的事件 +1（每天有上限）；每轮 ±3、每天 ±8；
- 每次变化写进日志，带原因；
- 进提示词时写明"这是你们关系的描述，不是要刷高的分数"（借 dsh-companion 的措辞）。

测试：
- 规则和限幅的单元测试；
- mock 端到端；
- 0.8B 同一句话配不同心情跑一组，看回复和 emo 有没有跟着变（预计变化弱，结果记成 LoRA 的数据需求）；
- 云端效果要花钱，跑之前问。

### 第三阶段：人设分层 + 去 AI 腔（桌宠面板 + 服务端风格包）

- 人设卡字段：名字、自称、怎么称呼用户、身份背景、说话风格、个性缺陷、底线；面板拼成固定顺序的文字（顺序固定，前缀缓存才稳定）。
- 服务端出两个可选的规则包，用自己的中文写，不抄原文：
  - "说人话"：不说"作为 AI"、不用客服腔、不列清单、不在结尾总结或给选项菜单、不确定就说不知道，按问题长短回答；
  - "有脾气"：会怼、会敷衍、会闹别扭。底线：用户难过时收起来；健康、安全的问题认真答；不攻击外貌和身份；闹别扭几轮内能哄好。
- 本地 0.8B：「小流」写死在提示词和 few-shot 里，人设文字影响有限，量一下就行，根本改变要靠 LoRA。
- 注意：现在角色叫小流，声音是爱弥斯，名字要不要跟着人设卡走，请用户定。
- 测试：写一个规则版"AI 腔检查"（数客服腔用语、清单、总结句、感叹号密度、字数），对同一组问题比较开关风格包前后。
- 用户 2026-10-25 加的要求"主动通过跟人对话迭代自己的说话语气"：建议做成"语气札记"（专用文件、每天最多一条、她提议你确认、能撤回、只管说话方式不管权限、外部消息不能产生），照 dsh-bili-humanism 的人格积累闸门，见 [harness-learning.md](harness-learning.md) 第 4 节，待用户定。

### 第四阶段：会话寿命（服务端）

- 本地：截掉的轮次留线索；用户说"刚才/之前/上次/你说的那个…"、关键词对得上时，带一行 `[想起] 今天 10:32 你们聊过: …`（日志原话，截到 60 字以内）。不调模型，跨会话只查近 7 天日志。
- 云端：历史预算单独配，到 85% 时把最旧一半折成前情提要（默认规则线索；`llm.cloud.summary: true` 才用云端模型写摘要，要花钱）。
- 测试：用 mock 跑长对话脚本，检查该想起的想起、不该带的不带；真模型抽查。

### 第五阶段：主动聊天（桌宠发起 + 服务端出内容）

- 上行 `nudge {reason: idle|return|activity|needs|time}`。桌宠在人在、没全屏、没开免打扰时发。
- 服务端结合安排、上次聊到的事、心情、活动，出一句话，走正常的说话流程，回复带 `proactive: true`。
- 限频：两次之间至少 20 分钟、每天上限，可配置。
- 约定类提醒由服务端排程，到点通知已连接的客户端。
- 测试：假时钟 + mock。

### 第六阶段：QQ / 微信 / 手机（方案见 [external-channels.md](external-channels.md)，待用户确认）

- 推荐：她用自己的 QQ 小号，SnowLuma 在本机挂上 → 服务端 `wm/` 新写 OneBot v11 客户端 → 每个群、每个私聊一个**访客会话**。
- 访客会话是另一个类，构造时就没有工具、没有本机 MCP、不写主人的记忆、看不到主人在电脑上做的事（准入靠代码，不靠提示词）；外部来源改不了人设和规则，渠道里整理出的事只进候选。
- 查代码发现的坑：`Memory.observe` 每轮都会把"我叫…""住在…"直接写进 `关于用户.md`，本机接口 `wm-pet` / `wm-text` 也走这条。所以不能把 QQ 接到本机接口，也不把 SnowLuma 自带的 MCP 挂进桌宠面板。
- SnowLuma 是非商业许可，EULA 不许打进安装包、不许脚本自动部署：只能用户自己装；要商用时换 QQ 官方机器人。
- 原来这里写的"QQ 官方机器人只能沙箱"：2026-03 起个人能扫码建机器人，现在的限制没核实。经 dsh + im-bridge 转的路不推荐（会话隔离管不到 dsh 那边）。
- 微信、刷视频、玩游戏、查 bug、查资料、自我迭代写成了以后的阶段（external-channels.md 第 9 节），先不做。
- 要用户定的问题在 external-channels.md 第 10 节。用户追问"能不能把 SnowLuma、NapCat 反推出来用 C 重写进桌宠"的答复在第 13 节（协议端不做，上面的全部自己写）；从各开源项目学什么在第 14 节。

## 5. 需要用户定的

1. **规则提取的直接陈述**（"我叫…""我喜欢…""记住…"）要不要也进候选？
   - 建议：**不进**，照旧直接记。这些是用户自己的原话，规则本来就"宁可漏记"；每句都确认很烦，现有基准也靠它。
   - 只有模型判断出来的才进候选。
2. **候选怎么确认**：第一阶段先做 Obsidian 勾选 + WS 消息（面板按钮由桌宠会话后续接）。语音确认（"对，记下来"）放到以后。
3. **"想起一次就刷新"**：建议开。
4. **长期事实的范围**：建议名字、住处、生日、出生年、生肖、工作、喜好、约定、用户手写的笔记。经历、计划、心情都算事件，会淡化。
5. 要不要在 dsh 里试装社区插件（比如给 dsh 当客户端时用的人设、记忆）？这和 `wm/` 无关，要先列下载清单。建议现在不装。

## 6. 风险和限制

- 0.8B 对人设、心情的跟随很弱。第二、三阶段在本地的效果要实测，可能主要体现在云端预设和语音上。
- 鲜活度会改变检索结果，所以基准要用固定时钟跑。现有剧本大多是"当天记、当天问"，影响应该很小，但要量。
- 候选模式会让日志整理出来的事不再自动被记住，要用户去确认。不想管的用户可以设 `memory.confirm.mode: "auto"`。
- 社区插件资料是 README 和第三方目录页的转述，标了"未核实"的没看到源码。

## 资料来源

- [Yuuyuko-uu/dsh-fish-memory](https://github.com/Yuuyuko-uu/dsh-fish-memory)
- [Toctsu/dsh-full-life-memory](https://github.com/Toctsu/dsh-full-life-memory)
- [xianshu-virtuous/dsh-whale-companion](https://github.com/xianshu-virtuous/dsh-whale-companion)
- [shenA2024/whale-persona](https://github.com/shenA2024/whale-persona)
- [OMGLogic/dsh-whale-persona](https://github.com/OMGLogic/dsh-whale-persona)
- [LamplitIsles/dsh-companion](https://github.com/LamplitIsles/dsh-companion)
- [derkinL/dsh-remembers](https://github.com/derkinL/dsh-remembers)
- [hexiecs/talk-normal](https://github.com/hexiecs/talk-normal)
- [dsh-ciku-pack（dshpluginhub）](https://dshpluginhub.ai/en/plugins/dsh-ciku-pack)
- [dsh-mobile-bridge（dshpluginhub）](https://dshpluginhub.ai/en/plugins/dsh-mobile-bridge)
- [dsh-plugin-im-bridge（dsh-plugin.org）](https://dsh-plugin.org/plugins/eighteentang/dsh-plugin-im-bridge)
- [0xsline/awesome-deepseek-harness](https://github.com/0xsline/awesome-deepseek-harness)
- 本机 `desktop/third_party/dsh/node_modules/@deepseek-ai/` 下 `dsh-persona`、`dsh-compaction-basic`、`dsh-schedule`、`dsh-time-context`、`dsh-plugin-market` 的 README.zh.md

