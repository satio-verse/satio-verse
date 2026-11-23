# 工作台改版：对照 Codex / DSH / Lexora 的差距和顺序（2026-11-01）

用户："界面不美观，也不实用，跟 codex、dsh、claude desktop 比差远了"，要求照 Codex 桌面版的页面结构和交互基本一比一改，再加 Lexora 的多智能体和"引用"、DSH 的插件 / 设置页。**这一页是动手前的审计，给用户过目用；改完的结果另记。**

## 怎么看的

- 在 memory-ui 测试服务端（8798，假模型、临时工作台目录）里把现在的工作台实点了一遍：对话、文件、浏览器、协作任务、"任务与能力"、设置。
- 读了 `desktop/web/workbench*.js / .css`（约 400 KB）、宿主 `workbench.cpp`、`browser_pane.*`，以及 Codex 留下的 4 份说明和候选包。
- 正式宿主 `desktop/build/bin/wmpet.exe`（10-31 19:52）只认单个原生网页（有 `native_browser`，没有 `browser_tabs`、`browser_selection`）。多标签、选区、缩放锁在没发布的 v3 候选里。

## 总的问题

1. **结构不对**：右侧是"一列功能键 + 一次只放一种工具的工具区"，浏览器和文件各自又有一层标签，三层头部叠在一起；Codex 是一个统一的多标签面板（网页、文件、图片、变更、新标签页都是同一排标签）。
2. **样式是一层层补丁**：`workbench.css` 里 `.app` 的三栏宽度定义了 6 次，后面的文件互相覆盖，所以哪里都"差不多"，没有一处干净。
3. **对话里结果不成型**：改了哪些文件、加减多少行、能不能撤销、产物在哪，都埋在"已运行 N 个命令 ›"的折叠里；Codex 每轮结束有"已编辑 N 个文件 +x −y · 撤销 · 查看变更"这种结果卡片。
4. **浏览器在正式宿主里不可靠**：用户截图是白屏。正式宿主是单网页 v2，前端出错时只给一行"加载较慢"，没有退路；元素引用完全没有。
5. **能力入口散**：插件、预设、权限、扩展分在"任务与能力"弹窗、工具区"更多"、设置 iframe 三处，名字也不统一。

## 逐块差距

### 1. 布局、左栏、对话卡片、输入框

| | Codex / DSH 怎么做 | 现在 | 改法 |
|---|---|---|---|
| 左栏 | 新聊天、"你的 dot"、项目（工作区下挂会话）、最近；行高约 30 px，没有卡片 | 品牌块、新会话、搜索、工作区树、归档、"任务与能力"、桌前的她（大卡片）、钱包、设置 | 新聊天 / 搜索；**"绫音"一行**（对应"你的 dot"，显示她在做什么，点开名片和桌前的她）；插件、自动化任务（持续任务）；**项目**（工作区 → 会话，悬停出"新会话 / ⋯"）；**最近**（跨工作区按时间）；底下设置、余额一行。桌前的她留着，收成可折叠的一块 |
| 右侧功能键 | 没有 | 一列 7 个（文件、浏览器、终端、协作、更多、设置、回到桌宠） | 去掉这一列：工具从新标签页和快捷键开；麦克风、静音、人设、回到桌宠挪到左栏底部一排小图标（和宿主的 dock 状态照旧同步） |
| 顶栏 | 会话标题 + ⋯ + 开关右侧面板 | 标题、路径、花费、⋯ | 加"开关右侧面板"按钮；路径收到标题下面一行小字 |
| 她的回复 | 不带头像，正文全宽，列表、粗体、代码块 | 每条带头像和名字 | 照 Codex：不带头像；Markdown 用 `renderMarkdown`（现在对话里用的是只认粗体和代码的简化版） |
| 过程 | "已运行 3 个命令 ›""已浏览 5 个文件" | 已有同样的折叠，样式偏重 | 留逻辑，换成 Codex 的灰字一行 |
| **结果卡片** | "已编辑 17 个文件 +570 −62 · 撤销 · 查看变更"，下面每个文件一行，"再显示 14 个文件" | 没有 | 每轮按工具结果里的 `change`（改前 / 改后）汇总；点文件在右侧开 diff 标签；"查看变更"开全部 diff；**撤销**要新接口（见下） |
| 链接 / 产物卡片 | "工作台 · 网站 · 打开方式 ▾" | 没有 | 回复里的网址、交付的文件做成卡片：右侧打开 / 系统浏览器 / 在资源管理器显示 / 复制 |
| 等你点头 | 卡片 | 黄框 | 改成卡片样式，按钮和"以后都允许"照旧 |
| 输入框 | 图片缩略、+、"完全访问"、模型、麦克风、发送 | 两排：工作区 / 此计算机；添加内容 / 任务与能力；权限 / 智能搜索 / 模型 / 发送 | 一张卡：上面附件缩略图，下面一排 **+**（文件、图片、网页 / BV、引用当前标签页）、**权限**（完全权限橙色）、智能搜索、模型 ▾、发送；工作区放到输入框下面一行小字 |

**撤销**：只在"文件现在的内容 = 那次改后的内容"时才写回改前的内容，否则说明已被再改过、不动；走 `wm/agent/workspace_http.py` 加一个 `revert` 动作，过同一套黑名单。

### 2. 右侧多标签面板和新标签页

| | Codex | 现在 | 改法 |
|---|---|---|---|
| 标签 | 一排：图片、md、网页、新标签页，都带 × ；+ 新标签；放大；收起 | 工具区一次一种；浏览器、文件各自一排标签 | 新写一个面板管理（`workbench-panel.js`）：标签种类 新标签页 / 网页 / 文件 / 变更 / 图片 / 终端 / 协作；每个会话记自己的标签 |
| 新标签页 | 工具（文件 Ctrl+P、终端 Ctrl+`、更多工具…、侧边聊天 Ctrl+Alt+S、新建页面）、推荐、最近 | 浏览器空白页上有"工具 / 常用网站 / 最近浏览" | 照 Codex：工具一栏（文件、终端、更多工具…【协作任务、插件、记忆、上下文】、新建页面＝在工作区新建 md）；推荐＝她这轮交付的文件 + 收藏网站；最近＝最近开过的文件和网页（带时间） |
| 侧边聊天 | Ctrl+Alt+S 在面板里和当前页面聊 | 没有 | 先做成"把当前标签页引用进对话并聚焦输入框"；真正的第二个对话以后再说 |

### 3. 浏览器

| | Codex | 现在 | 改法 |
|---|---|---|---|
| 工具栏 | ← → ⟳、居中的地址栏、引用、⋯ | 有，但挤在浏览器自己那层标签下面 | 标签交给面板，工具栏一行 |
| ⋯ 菜单 | 页面内查找、打印、缩放 −100%+、设备工具栏、截屏、导入 Cookie 和密码、密码和自动填充、下载、历史记录、清除浏览数据、浏览器设置 | 首页、加入对话、收藏、缩放、切换阅读、系统浏览器 | 能做的做：页面内查找（文字阅读里能找；原生要宿主）、缩放（v3）、历史记录（本地记录，开成一页）、清除浏览数据（清本地记录；原生资料要宿主）、浏览器设置（搜索引擎、首页、失败时自动切文字阅读）、收藏、在系统浏览器打开、加入对话；打印、截屏、设备工具栏、下载显示成"需要新版宿主"并灰掉。**导入 Cookie 和密码、密码和自动填充不做**（桌宠浏览器不碰账号密码） |
| 白屏 | — | 只有 10 秒后一行"加载较慢" | 原生页面一直没回状态、报错或 HTTP 错误时，页面上给"切到文字阅读 / 系统浏览器打开 / 重试"；设置里可选"打不开时自动切文字阅读" |
| **元素引用** | Lexora：选中页面内容进对话（快照 + 定位） | 原生 v3 只能取选中的文字；文字阅读里不能引用 | 文字阅读：iframe 改成不跑脚本但同源（`sandbox="allow-same-origin"`），能"选取元素"（悬停高亮、点一下）和引用选中文字，带网址、标题、所在小标题；原生页面：要宿主加一个取元素的动作（ExecuteScript 注入选择器、轮询结果，不开桥），**前端先按能力开关接好，宿主等联合发布** |

### 4. 文件查看

| | Codex | 现在 | 改法 |
|---|---|---|---|
| 头部 | 面包屑（WorldModel_v1 › docs › 文件名）、显示更改、查看源代码、打开（VS Code）▾ | 两行头部，阅读 / 源码切换、查找、引用、更多 | 一行：面包屑 + 显示更改 + 预览 / 源码 + 打开 ▾（在资源管理器显示＝宿主 v3 的 `reveal_file`；用 VS Code / 默认程序打开要宿主，先灰掉） |
| 文件树 | 右边，带"筛选文件…" | 左边，筛选只筛已加载的 | 挪到右边，可收起；筛选先筛已加载的，Ctrl+P 用新的 `find` 接口全工作区找（只读，过黑名单，限 200 条） |
| 显示更改 | 看这个文件被改了什么 | 没有 | 用这个会话里该文件的 `change` 记录出 diff |
| 引用 | — | 能引用选段（只有文字快照） | 引用带行号范围；对话里的引用卡片点一下回到文件那几行（Lexora 的"快照 + 定位 + 回到原处"） |

### 5. 插件、设置（DSH）

| | DSH | 现在 | 改法 |
|---|---|---|---|
| 插件页 | 主区一整页：内置插件配置（子智能体、网页搜索、Agent 循环、终端）、实验性插件带开关、已安装插件带开关、右上"添加插件" | 分在"任务与能力 → 扩展"、工具区"更多 → 工具与扩展"、设置 | 左栏"插件"开一整页：内置能力配置卡片（数据来自 `/api/agent/runtime?view=capabilities`）、实验性（只列真有的开关）、已安装（扩展、技能、MCP 工具服务，启停走现有 studio 接口）、"添加插件 ▾"（通过聊天创建 / 从文件夹装） |
| 设置 | 弹窗：账号与余额、通用设置、模型、内置插件、Agent 预设；"打开配置文件" | 设置页是嵌进来的 `panel.html`，ChatGPT 样式 | 设置照旧用 `panel.html`（它有自己的页），外面换成 DSH 的弹窗框；工作台自己的几项（Agent 预设、内置插件）放在弹窗左栏 |
| Agent 预设 | 四张卡：说明、如何使用、"让 Agent 帮我创建预设模式" | 工具区"协作 → Agent 预设"一列卡片 | 照 DSH 的卡片排，数据照旧 |

### 6. 多智能体看板（Lexora / DSH 智能体团队）

现在有子任务（`TeamManager`）、任务树、持续任务、统一状态（`/api/agent/runtime?view=activity`，原会话今天刚做的）。缺的是**一眼看全队**：谁在做什么、在等什么、做出了什么。改法：面板里一个"协作"标签——上面成员（她 + 每个子任务，状态灯、在等什么），下面看板四列（排队 / 在做 / 等你 / 做完待验收），卡片点开到子会话；数据全来自 activity 和任务树，不另造状态。

**统一状态怎么接**（原会话 11-01 接好的后端，界面直接用，不另造状态）：
- `GET /api/agent/runtime?view=activity` → `{items, counts, at}`，已按"等你的在前"排好；每项有 `kind`（session / child / job / terminal / command / check）、`session`（点开用）、`state`、`waiting_for`、一句中文 `detail`、`since`。用在看板、左栏"绫音"一行（她在做什么 / 等你什么）和新标签页。
- `GET /api/agent` 里的 `activity: {you, working, background, needs_check}` 做角标（左栏"绫音"、顶栏开面板按钮）；会话简报里的 `check`（`needs_check`）在左栏会话行上标"待核查"。
- 改对话渲染时不能丢的事件：`job_done`（`delivered` steer / continue / note）、`check`（recheck / final + `reasons`）、`steered` / `steer_ack`、`user.steer`（"中途补充"）、`done.run.check.verdict === 'needs_check'`（结尾写"说做完了，但待核查"）。
- 测试服务端要重启一次才有 `/api/agent` 里的新字段（8798 是这边起的，随时能重启）；正式服务端还没重启。

### 7. 文档引用、Office

- 引用：统一成"快照 + 定位 + 回到原处"——文件（行号）、网页（网址 + 小标题 + 选中文字）、对话里的卡片点了回到原处。短的直接给她，长的给预览、她用现有的材料读取工具按需读全文（和 Lexora 的做法一样，不拷它的代码，它是 AGPL）。
- Office（生成 Word 报告 → 选段落引用改写 → 出 PDF / PPT）：要新组件（python-docx 等）和转 PDF 的办法，**先列名字、来源、大小、许可给你点头**再下。这一轮只出路线。

## 顺序和每块怎么算做完

| 顺序 | 块 | 做完的标准 |
|---|---|---|
| 1 | 布局、左栏、对话卡片、输入框 | 假模型跑一轮"改两个文件 + 跑命令"：结果卡片的文件数和 +/− 对，点文件开 diff，撤销能撤（再改过的不撤）；左栏项目 / 最近对；窄窗口不出横向滚动；明暗两套 |
| 2 | 右侧多标签 + 新标签页 | 网页、文件、变更、新标签页混开、切换、关闭，换会话再回来标签还在；Ctrl+P / Ctrl+` / Ctrl+T / Ctrl+W 对 |
| 3 | 浏览器 | 菜单每项要么能用、要么灰着说明原因；文字阅读里选元素、选文字能引用进对话（带网址和小标题），不自动发送；模拟宿主 v2 / v3 两种握手都对；原生取元素的宿主改动写好源码、不编译，等联合发布 |
| 4 | 文件查看 | 面包屑能跳目录；右侧树筛选、Ctrl+P 全工作区找；显示更改和对话里的 diff 一致；引用带行号，点引用回到原处 |
| 5 | 插件、设置页 | 插件页的开关真改到运行时（重开页面还在）；预设卡片切换生效 |
| 6 | 多智能体看板 | 假模型开两个子任务：看板的列和 activity 一致，点卡片进子会话 |
| 7 | 文档引用、Office | 先出路线和要下的组件清单 |

每块：浏览器里实点（假模型，不花钱）→ 跑 `python -m pytest -q tests` 和 `node --test tests/js/*.test.mjs` → 截图归档到知识库 `11 实验数据` → 更新知识库。界面实点用 Playwright（本机已有的 playwright-core 1.61.1 和 chromium-1228，不新下）出全分辨率截图，脚本放 `tools/`。

## 边界

- 只学结构和交互，不用 Codex / DSH 的图标、商标、名字；Lexora 是 AGPL-3.0，只参考设计，不拷代码。
- 原生宿主不编不发：浏览器多标签、选区、缩放锁要等 v3 联合发布；取元素、打印、截屏、下载、用 VS Code 打开要 v4，前端按能力开关先接好。
- 不碰 `wm/agent/core.py`、`wm/agent/runtime_http.py`；后端只在 `workspace_http.py` 加 `revert`、`find`，`wm/app.py` 只往静态文件白名单里加新文件名。
- 页面从磁盘读，改完重新打开工作台窗口就生效；`/agent` 浏览器入口和新接口要等服务端重启（重启前先问）。

## 改完了什么（2026-11-01，第 1–4 块）

用户回"按这个顺序改"以后做的。页面从磁盘读：**重新打开工作台窗口就生效**；服务端的两个新接口（撤销、找文件）和 no-store 已随 11-01 本机 10:47（北京 11-02 02:47）那次看守重启上线（启动编号 93fac200，核对过）。

| 块 | 现在的样子 | 文件 |
|---|---|---|
| 左栏 | 品牌行（搜索、收起）、新聊天、**绫音**（她在做什么，等你的件数做角标，点开是桌前的她 + 钱包）、插件、自动化任务、任务与成果（待核查件数做角标）；**项目**（工作区 → 会话，悬停出 ＋ / ⋯，待核查的会话标菱形）；**最近**（跨工作区按时间 12 条）；底下一排桌宠功能键（麦克风、静音、人设、置顶、回到桌宠、设置）。右边那列功能键去掉了 | `workbench.html`、`workbench.js` |
| 对话 | 她的回复不带头像、完整 Markdown（表格、列表、代码块）；每轮结尾 **"已编辑 N 个文件 +x −y · 撤销 · 查看变更"**（同一文件改几次合成一条，新建的标"新建"，点文件开它的 diff）；回复里的网址变成链接卡片（"打开方式 ▾"：右侧打开 / 系统浏览器 / 加入对话 / 复制）；改过的文件以外的成果单独出卡片；等你点头的是卡片 | `workbench.js`、`workbench-diff.js` |
| 撤销 | 先全部核对、文件现在正是那次改完的样子才写回改前（被再改过的一个都不动，并说出是哪个）；CRLF / BOM 原样；这一轮新建的挪进 `<工作区>/.archive/撤销-<时间>/`，不删；她在做这一轮时不让撤；对话里记一笔（带撤了哪些调用），卡片标"已撤销" | `wm/agent/workspace_http.py` |
| 输入框 | 一张圆角卡：附件 / 引用卡片在上；下面 ＋（文件、图片、链接、BV）、权限（完全权限橙色）、智能搜索、模型 ▾、发送；工作区和"此计算机"在卡片下面一行 | `workbench.html`、`workbench-shell.css` |
| 右侧面板 | 一排标签（网页 / 新标签页、文件、变更、终端、协作与能力），＋ 新标签、放大、收起，可拖宽；**每个会话记自己的标签**；新标签页照 Codex 分 **工具**（文件 Ctrl+P、终端 Ctrl+`、协作任务、自动化任务、更多工具…）/ **推荐**（这次的成果、改过的文件、收藏）/ **最近**（文件和网页按时间）；Ctrl+P 全工作区按名字找文件；Ctrl+T / Ctrl+W / Ctrl+J / Ctrl+Alt+S（把当前页选中的内容引用到对话） | `workbench-panel.js` |
| 浏览器 | 宿主 v2（正式版）一个原生网页，切标签时重新导航；v3 每个标签一个；没有宿主时服务端文字阅读。**打不开时**（报错、HTTP 错、10 秒没回）给"重试 / 切到文字阅读 / 系统浏览器"，默认自动切文字阅读（浏览器设置里可关）。⋯ 菜单照 Codex：页面内查找、打印、缩放、选取页面内容引用、加入对话、收藏、设备工具栏、截屏、下载、历史记录、清除浏览数据、切文字阅读 / 完整网页、系统浏览器、浏览器设置；做不了的灰着写原因；不做密码和 Cookie。**文字阅读里**能查找、缩放、选取一段 / 一个小标题（= 整节）引用、引用选中的字（带网址和所在小标题）；原生页面引用选中的字走 v3 的 `selection` | `workbench-browser.js` |
| 文件 | 一行头部：面包屑（点了展开那一层）、**显示更改**（这个会话里她对它的改动）、查看源代码 / 预览（Markdown）、打开 ▾（在文件夹中显示、整个文件加入对话、复制路径 / 全文、重新读取；用编辑器打开等宿主）；右边文件树带筛选，面板窄了自动收成浮层；正文带行号；**选中几行引用，带"第 a–b 行"** | `workbench-files.js` |
| 引用回到原处 | 输入框里从文件几行、网页一节引来的卡片，点名字就回到那里（文件那几行闪一下、网页滚到那一节） | `workbench-panel.js`、`workbench.js` |
| 统一状态 | `/api/agent` 的 `activity` 做角标、会话简报的 `check` 标待核查；看板留给第 6 块 | `workbench.js` |

顺带修的：导航模块只认宿主 v1 / v2 的握手（v3 发布后文件夹选择会失灵），改成 ≥1 都认；服务端给 `/pet/*` 加 `Cache-Control: no-store`（浏览器里打开工作台时会拿缓存的旧模块配新页面，一个 import 对不上整页白屏，宿主那边本来就不缓存）。

### 怎么验的

- `tools/test_workbench_ui.cjs`：先用 `tools/workbench_ui_seed.py` 在临时目录建一个工作区、起假云端（`tests/test_cloud.FakeCloud`）跑一轮"新建 1 个、改 2 个（一个 CRLF）、跑 1 条命令、回带链接和表格的 Markdown"，再用无头 Chromium 点 **45 项**：左栏、卡片计数、diff、打开方式、文字阅读查找和选取、Ctrl+P、行号引用、显示更改、引用回到原处、换会话标签还在、撤销（逐字节核对 CRLF、新建的进归档、重渲染后还标已撤销）、新标签页、协作 / 自动化 / 终端标签、模型和权限菜单、960 宽不横滚、深色；**假宿主 v2**（导航、无 tab_id 的状态、切回重新导航不关、报错切文字阅读）和 **假宿主 v3**（每标签各自导航、选区引用、关标签关原生页）。全过，没有 JS 报错。
- `tests/test_workbench_revert.py` 6 条（撤销逐字节、全部先核对、同文件多次、新建归档、越界 / 黑名单 / 非 UTF-8、HTTP 和忙时拒绝）；`tests/js/workbench-diff.test.mjs` 7 条。
- 截图和结果：知识库 `11 实验数据/会话诊断/工作台照Codex改版-20261101/`（改版前 6 张、改版后浅色 / 深色 / 窄窗口 / 宿主 v2 退路）。

### 没做到 / 要注意的

- **没在正式桌宠窗口里点过**：正式宿主是 v2，网页白屏能不能被"切到文字阅读"接住要你在真窗口里试一次（模拟的 v2 消息是照源码写的）。
- 文字阅读的正文是服务端抽出来的纯文字，一行一段，网站导航那些行也在里面。
- 撤销只管工作台工具（写文件、改文件）记下的改动；跑命令改的文件没有改前内容，撤不了。改前内容超过 6 万字的那次也撤不了（服务端本来就不存）。
- 引用卡片名字后面还带 `.txt`（材料库按上传文件存）。

## 后几块的路线（第 5–7 块，没做）

**5. 插件 / 设置（DSH）**：左栏"插件"现在打开面板里的"能力与插件"。下一步做成主区一整页：内置能力配置卡片（子智能体 = 并行数 / 嵌套层数，已有 `team_config`；网页搜索 = 搜索来源说明 + 新会话默认开不开；终端 = 超时和输出上限）、已安装（我的扩展 / 技能 / MCP 工具服务，启停和回退走现有 `/api/agent/studio`）、"添加插件 ▾"（通过聊天创建 / 从本地包装 / 插件社区）；只列真有的开关，不摆样子。设置照旧用 `panel.html`，外面换成 DSH 的弹窗框，左栏 账号与余额 / 通用设置 / 模型 / 内置插件 / Agent 预设；Agent 预设做成四张卡（说明、如何使用、"让她帮我设计一个预设"）。

**6. 多智能体看板（Lexora / DSH 智能体团队）**：面板"协作"标签上半部分成员（她 + 每个子任务，状态灯、在等什么），下半部分看板四列：排队 / 在做 / 等你 / 做完待验收，数据全来自 `/api/agent/runtime?view=activity` 和 `view=tree`，卡片点了进子会话；拆任务的表单保留。

**7. 文档引用 / Office**：引用已经有"快照 + 定位 + 回到原处"（文件行号、网页小标题）。下一步：原生网页的选区引用记下网址和那段字，回到原处时导航到 `网址#:~:text=那段字`（Chromium 的滚到文字）；PDF 记页码。Office（生成 Word 报告 → 选段落引用改写 → 出 PDF / PPT）要新组件：python-docx、python-pptx（MIT），转 PDF 用本机 Word（COM）或 LibreOffice（MPL，几百 MB）——**先列名字、来源、大小、许可给你点头再下**。

**原生宿主（等联合发布，不在这边编）**：前端已按能力开关接好 `browser_pick`（宿主注入一个选择器、轮询结果，不开桥，回 `workbench-browser-pick` {request_id, tab_id, url, title, text, heading}）。还没接的：`browser_find`（WebView2 的 Find 接口）、`browser_print`（ShowPrintUI）、`browser_capture`（CapturePreview → 图片材料）、下载落到工作区、用编辑器打开工作区里的文件；菜单里这几项现在灰着写"需要新版桌宠宿主"。

## 第二轮打磨（2026-11-01 晚）

用户觉得"并没有优化成 codex 或者 lexora 那么美观"。查到桌宠里的工作台页面 06:23 加载后一直没重新加载（开着的窗口不会自己换页面），看到的是旧版；同时对照 Lexora 的实际截图收拾了新版里杂乱的地方：每轮开头头像 + 名字 + 状态（Lexora）、消息按钮悬停才出、核对提示条、对话列间距（旧样式的 `gap: 20px`、`.note { align-self: center }`）、顶栏只写工作区名、左栏每个项目先列 5 个、柔和角标、空会话输入框居中、对话宽度和字号跟设置走；毛玻璃材质不再被实色背景盖住；新标签页推荐去重；页面文件变了顶栏提示"有新版本 · 刷新"（`watchPageFiles`，带着没发的草稿刷新）。`tools/test_workbench_ui.cjs` 50 项全过。

## 第三轮（2026-11-01 晚，用户按 F5 后的反馈）

- 侧栏来回跳：面板开关改成全局记（`wm.panel.open`），标签仍按会话；不再自动收左栏，放不下时面板加 `.panel-overlay` 浮在对话上；设置改成弹窗（`#settings` 固定定位 + 背后压暗，左右栏不动；设置页 4 秒没回应直接关）。设置半屏的根因是第二轮的 `.main.is-empty .chat` 规则盖过了旧的 `.main.show-settings > :not(.settings)`。
- 项目右键菜单（`workbench-navigation.js` 的 `openWorkspaceMenu`，右键或 ⋯）和左栏 ≡ 菜单（`workbench.js` 的 `toggleSideView`，分组 / 排序 / 显示 / 恢复默认，二级菜单往右弹）；"打开文件位置"按宿主能力 `reveal_file` 判断（正式宿主 v2 没有）。
- 联网搜索开关一直带字、开 / 关两种样子明显不同，点了输入框上方提示；桌宠侧菜单按钮（`ui-material.css` 的 `.wm-pet-surface .dock button`）改成透明底。
- `tools/test_workbench_ui.cjs` 63 项全过。

## 第四轮（2026-11-01 晚，点按钮的反馈换成橙黄）

小图标按钮和开关悬停淡橙黄、按下实心橙黄（#f59e0b）、开着的橙黄底；整行的（会话行、菜单项、卡片）悬停仍是浅灰、按下闪淡橙黄。变量在 `workbench-shell.css` 的 `--hot / --hot-soft / --hot-ink`，桌宠侧菜单在 `ui-material.css`。麦克风录音的红色没改。

## 第五轮（2026-11-01 晚，邮箱入口）

- 服务端的 `/mail`（`wm/channels/mailbox_http.py`，CSP `frame-ancestors 'self'`）从左栏"邮箱"和新标签页工具区打开，开在右侧面板的"页面"标签里：同源 iframe（`workbench-panel.js` 的 `PAGES / openPage / renderPageTab`）。原生网页标签挡着本服务端端口，所以不走网页标签。
- 打开前先 `fetch('/mail')`：不是 2xx 就提示"要等服务端重启后才有"并给"重试"；页面里的外站链接（`hookLinks`）转到工作台网页标签，没宿主时 `window.open`。
- 界面测试加了 `/mail` 没有 / 有两种，65 项全过。入口工作台 F5 就有；`/mail` 本身等 8790 重启。

## 第六轮（2026-11-01 晚，照 Linear / Notion / ChatGPT 精修）

清单：`docs/workbench-polish-plan-20261101.html`（15 项分三批，用户说"先做第一批"）。第一批的改前 / 改后对比：`docs/workbench-polish-batch1-20261101.html`。

- 统一的底子：`workbench-shell.css` 字号收成 12 / 13 / 14 / 16 / 20，`--fs-* / --r-* / --row-h` 变量，按钮 `.wb-btn`（次 / primary / text / icon）；`workbench.js` 的 `initTips` 把按钮的 `title` 换成小黑条提示（名字 + 快捷键）。
- 对话页：`renderMarkdown(text, { cite: true })` 把网址变成站点名小胶囊；`sourcesRow` 每轮一个"来源 · N"，`panel.openSources` 开一个复用的"来源"标签；`renderChat` 里 `settle()` 固定收尾顺序（过程摘要 → 正文 → 改动卡 → 来源 → 操作行）；表格换 Notion 样式；步数、花费并进头部（`turnMeta`），去掉 `.turn-end`。
- 输入框：短提示语、底栏只留工作区和运行位置、上下文小圆环、权限菜单一档一行 + 下方说明格、只有 unrestricted 用提醒色；附件卡（`workbench-studio.js` 的 `chipType`）。
- 新聊天：`#emptyExtras`（建议胶囊 + 接着上次，`renderEmptyExtras`）。
- 面板：标签收窄 / 只剩图标 / "∨ N" 列表（`fitTabs`、`toggleTabMenu`）；Ctrl+T 复用空标签。
- 工具页（`workbench-canvas.js`、`workbench-resident.js`、`workbench-platform.js`）：去掉页内子标签；页头 + 一行一项的列表 + 弹出的新建卡 + 空状态；上下文字段翻成人话（`contextHtml`）。
- 测试：`tools/test_workbench_ui.cjs` 80 项全过；JS 451/451；逐页截图工具 `tools/workbench_page_shots.cjs`。

## 第六轮第二批（2026-11-02）

对比：`docs/workbench-polish-batch2-20261102.html`。

- 左栏：`#pinned` 置顶分组；`sessionState()` 固定一格的状态小图标（`.ss-run / .ss-wait / .ss-check / .ss-unread`）；`sessionRowHtml()` 抽出来给项目树和置顶分组共用；她的卡片默认收着（`wm.workbench.herCard` 存过 1 才展开）；会话菜单绑到 `#sideScroll`（滚动关菜单照旧只绑 `#tree`）。
- 新标签页和 Ctrl+P 共用 `quick()` 分组搜索 + `paintList()`；命令由 `workbench.js` 的 `paletteCommands()` 经 `initPanel({ getCommands })` 交进来。
- 文件查看：`highlightLines()`（自写分词，`.wb-src.hl .tk-*`）、`propsHtml()` 属性行、`markdownWithLines()` 认 `- [ ]` / `- [x]`；跨行引用按 `.ln` 一行一行接换行。
- 设置弹窗：`panel-design.css` 末尾 `html body.embed …` 一段，只在工作台的 iframe 里生效。
- 测试：`tools/test_workbench_ui.cjs` 109 项全过；`tools/workbench_page_shots.cjs` 加 Markdown 预览、回最小设置画出设置页。

## 第六轮第三批（2026-11-02）

对比：`docs/workbench-polish-batch3-20261102.html`。

- 任务与成果开在面板标签里：`workbench-panel.js` 加了标签种类 `studio`（`STUDIO_PAGES`：任务 / 成果共用一个标签，扩展 / 权限 / 成长各一个）、`openStudioTab()`、`mountStudio()`（第一次显示时把 `#studioDialog` 的子元素整块搬进 `.wb-pview[data-view="studio"]`）、`studioShown()`、`studioTab()`（点"任务 | 成果"时记下这一页）；`workbench-studio.js` 加 `setHost({ show, shown, moved })` / `display(next)`，`open()` 有宿主就交给面板，定时刷新看 `shown()`；`workbench.js` 接上两头。样式在 `workbench-shell.css` 的"第六轮第三批"一节（面板里收紧、`@container studio`）。
- 任务页：`renderTasks()` 改成 `.st-issue`（标题 + `dl.st-props` 六格）+ `ol.st-timeline`（`runFlow()` 从最后一条非插话的用户消息算起，工具按种类数）；去掉 `showTaskCard()` 和 `.studio-taskcard*`。成果：交付文件 `.st-seg` 列表 / 网格（`wm.studio.deliveryView`），`.studio-delivery-files.grid` 文件卡，点开的那张 `:has(.studio-reader)` 占满一行。
- 协作看板：`workbench-canvas.js` 的 `BOARD_COLS` / `boardCol` / `treeCards` / `activityCards` / `cardHtml` / `boardHtml` / `paintTasks`；看法和范围记在 `wm.tasks.view` / `wm.tasks.scope`；范围"全部"读 `runtime('activity')`；`@container tool` 两列 / 四列；列表按同样顺序分组（`.cv-list-group`）。卡上 `data-wt-diff` / `data-wt-export` 走 `/api/agent/studio` 的 `worktree_diff` / `worktree_export`。
- 修：`.app.panel-wide` 的 `--panel-w` 加 `!important`（`fit()` 总在行内写 `--panel-w`，原来拉宽按钮不起作用）；`.wb-edits > header` 子项不折行、标题省略。
- 测试：`tools/test_workbench_ui.cjs` 127 项全过（看板数据截住 `/api/agent/runtime` 喂）；JS 451/451；全量 pytest 1504 过、4 跳过、0 挂（12 分 40 秒），这次没改 Python。

## 第七轮（2026-11-02）

对比：`docs/workbench-polish-round7-20261102.html`。

- 成员一栏：`workbench-canvas.js` 的 `teamHtml()`（她 = 当前会话 + 统一状态里这个会话那一条的说明；子任务 = 任务树的行），`paintTasks()` 填进 `#cvTeam`，页头说明跟着范围换；子任务卡的状态词用 `WAIT_TEXT`。
- 去重：`mergeActivity()` 把统一状态里同一个会话的 `session` / `child` 两条并成一张；QQ 审批的 `ask` 标"QQ 上也能批"。
- 工作经验页：`TOOL_PAGES.experiences`（`tools:experiences`），`renderExperiences()` / `expRow()` / `filterExperiences()`，读 `runtime('experiences')`、改 `command('experience', {exp_id, do})`；入口在能力与插件页头、"更多工具…"、Ctrl+P。样式在 `workbench-shell.css` 的"第七轮"一节（`.cv-team / .cv-member / .cv-stats / .cv-exp-*`）。
- 测试数据：`tools/workbench_ui_seed.py` 的假模型换成 `route()`（按会话第一句话 + 已回几次），多了工作经验一轮和分工一轮（放在 `ws2`），开头 `tidy_team()` 清上次留下的分工；输出多 `expsid / teamsid / children`。
- 测试：`tools/test_workbench_ui.cjs` 140 项全过（第七轮用真数据，最后停掉那一轮分工）；`tools/workbench_page_shots.cjs` 加 `37-team`、`38-experiences`；JS 451/451；全量 pytest 1513 过、4 跳过、0 挂（10 分 08 秒），这一轮没改产品的 Python。

## 第 5 块（2026-11-02，插件与设置）

对比：`docs/workbench-polish-block5-20261102.html`。没做成主区一整页，插件页留在面板里（第六轮起工具页都在面板标签里）。

- 能力与插件：`workbench-canvas.js` 的 `builtinHtml()`（子智能体表单 `#canvasTeamConfig` 走 `team_config`；联网搜索 `[data-web-now]` 经 `changeSetting('web')`、`#canvasWebDefault` 存 `wm.workbench.webDefault`；终端只写说明）、`installedHtml()`（`GET /api/agent/studio?id=` 的 `extensions.extensions / skills / servers`，扩展开关 `[data-ext-toggle]` 发 `extension_enable`）、`addMenu()`（`<details class="cv-add">`，点了哪项都收起，Esc / 点外面收起）。协作任务页的并行设置换成一行指过来。
- Agent 预设：`presetCard()` / `designCard()`，`PRESET_HOW` 写内置五种的"怎么用"。
- 接线：`initPanel` / `initCanvas` 多传 `changeSetting`；`workbench.js` 的 `webDefault()` 给 `newSession()` 用（菜单里选过的 > 新会话默认 > 当前会话）；设置 iframe 发 `{wmPanelUI: 'open-tool', tool}`，工作台 `closeSettings()` 后 `panel.openTool(tool)`。
- 设置页：`panel.js` 在 `EMBED` 时往"扩展与下载"前插一组"工作台"（`data-workbench-tool`，没有 `data-tab`，不进设置页自己的切页和搜索），样式在 `panel-design.css` 的 `html body.embed` 那一段。
- 修：`.wb-pview[data-view="tool"] .canvas-body { flex: 1 0 auto }` + 提示条 `position: sticky; bottom: 0`。
- 测试数据：`tools/workbench_ui_seed.py` 多 `extsid` / `extension`（`EXT_SPEC`）。测试：界面测试 153 项全过，这一块新加 13 项（内置能力三张卡且终端没有假开关、并行数存服务端且刷新还在、扩展草稿列在已安装、打开后技能里多出它且刷新还开着、关掉技能跟着没了、这个会话的联网开关和地球同步、新会话默认关、添加插件六个入口且第一项填进输入框、预设是卡片、用这个换预设、设置里多工作台一组、点内置插件关设置开插件页、没有脚本错误）。JS 451/451；全量 pytest 1513 过、4 跳过、0 挂（10 分 05 秒），这一块没改 Python 产品代码。

## 第 7 块组件清单（2026-11-02，等点头）

第 7 块的组件清单 2026-11-02 列好了，等用户点头（`docs/block7-components-20261102.md`）：本机已有 Microsoft Office 16（Word / PowerPoint COM 已注册，转 PDF 直接用）、python-docx 1.2.0、openpyxl 3.1.5、pywin32 311、pypdf / pypdfium2，读 docx / xlsx / pptx 的代码本来就有；要下的只有 python-pptx 1.0.2（MIT，462 KB）+ XlsxWriter 3.3.1（BSD-2，176 KB），可选 mammoth 1.13.0 + cobble 0.1.4（BSD-2，约 58 KB，Word 在面板里预览成网页）；不建议下 LibreOffice 26.8.2（358.5 MB，MPL-2.0，只有没装 Office 的电脑才要）和 docx2pdf（pywin32 直接调 Word 就行）。

第 7 块组件装好了：2026-11-02 用户回"装必需和可选"，装好了：python-pptx 1.0.2、XlsxWriter 3.3.1、mammoth 1.13.0、cobble 0.1.4（`python -m pip install` 固定版本，装前装后 `pip freeze` 对比只多了这四个，别的包没动）。试跑过：python-docx 写一份 Word → mammoth 转成网页（标题、列表、表格都在）；python-pptx 写出 PPT；本机 Word 经 COM（pywin32 `DispatchEx("Word.Application")`，`SaveAs2` FileFormat=17）把 docx 另存成 PDF。LibreOffice、docx2pdf 没下。第 7 块的功能还没开始做（要改后端，先和主会话说）。

## 第 7 块（2026-11-02，文档引用 / Office）

对比：`docs/workbench-polish-block7-20261102.html`。

- 后端：新 `wm/agent/office.py`（`TOOL_SPECS / NAMES / READ_ONLY / LABELS / decision / call_tool`，和 `notebook.py` 一个写法；`preview()` / `export_pdf()` 给面板）；`studio.py` 加 import、`SPECS +=`、`READ_ONLY |=`、`LABELS.update`、`decision` 和 `call` 里各一个分支；`workspace_http.py` 加 `docx_preview`（GET）、`office_export`（POST）、`raw`（GET，只给 .pdf，`raw_pdf()`）。没碰 `core.py` / `runtime_http.py`。
- 前端：`workbench-files.js` 的 `kind()` 认 docx / pptx / pdf；docx 走 `docx_preview`、`.wb-docx` 里画、`paraOf` / `flashParas`，选区和引用带 `unit: 'p'`；`export-pdf` / `open-pdf` 经 `openDoc` 开在原文标签；`reveal()` 不再先画。`workbench-panel.js` 的 `docURL()` 让原文标签也认 `workspace?action=raw&`，`remember` 记 `unit` 和网页引用的 `text`。`workbench-browser.js` 加 `textFragmentURL()`、`revealText()`、`revealQuote()`。
- 测试：`tests/test_agent_office.py` 8 条、`tests/js/workbench-text-fragment.test.mjs` 3 条；界面测试 161 项全过，这一块新加 7 项（Word 预览段号和 docx_read 一致、选段提示第几段、引用卡片写第 3 段且点了那一段闪、导出 PDF 走本机 Word 并开在原文标签、PPT 给导出、没有脚本错误、v3 原生网页点引用卡片跳 #:~:text=）。；全量 pytest 1532 过、4 跳过、0 挂（10 分 30 秒）。测试数据多 `officesid`（她写的周报）；界面测试要连到新后端的测试服务端（`office-ui`，8806）。

第 7 块上线：2026-11-02 04:06 用户回"现在重启"后上线：重启前查过她空着（QQ 没有在跑的学习任务、工作台没有在跑的会话），`python tools/serve.py restart`（先试跑新代码 4 秒，再请看守重启），启动编号 69c403fe → 39976d76，`/api/status` 通、QQ 在线；核对过：她的工具里有 docx_create / docx_read / docx_edit / pptx_create / office_to_pdf，`docx_preview`、`raw` 两个接口在（只读请求），那张"她想改进自己"的申请（QQ #A1，存在 asks.json）重启后还在。
