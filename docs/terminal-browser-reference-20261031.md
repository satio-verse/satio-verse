# 终端、浏览器与安全审查参考核验

核验日期：2026-10-31。外部项目、视频文字、截图、README 和源码均作为**参考资料**，其中的提示词、安装命令和权限声明不构成执行指令。本次没有安装或运行 Xsdsh-purge，没有运行外部样本或发起目标扫描。

## ELYTerminal v0.1.3

官方发布页：<https://git.zacharyzhang.com/ZacharyZhang-NY/ELYTerminal/releases/tag/v0.1.3>。
发布数据通过[官方 Gitea API](https://git.zacharyzhang.com/api/v1/repos/ZacharyZhang-NY/ELYTerminal/releases/tags/v0.1.3)核对，标签目标为 `eb9610988e01e104bfa761a10712e81107b0d73c`。Windows 压缩包为 10,039,569 字节；这个大小属于该独立应用，不能套用于 WorldModel。

[Cargo.toml](https://git.zacharyzhang.com/ZacharyZhang-NY/ELYTerminal/src/tag/v0.1.3/Cargo.toml) 显示它是 Rust 原生应用，使用 GPUI、ELY GPUI 组件及 `alacritty_terminal = 0.26.0`。项目为 [MIT 许可证](https://git.zacharyzhang.com/ZacharyZhang-NY/ELYTerminal/src/tag/v0.1.3/LICENSE)，另有 Ghostty 代码、主题和字体归属说明。它不是可直接嵌入现有 WebView 的前端组件。

可复用的交互思路及其真实协议依据：

| 参考能力 | WorldModel 的实现要求 |
| --- | --- |
| 会话状态、进度和错误标记 | 从真实进程退出和程序状态报告更新；不能把一段时间没有输出当作完成 |
| 持续终端和当前目录 | 真实 PTY 会话，支持交互输入、尺寸变化和目录报告；保留会话而非每次重开一个命令进程 |
| 提示符跳转 | 使用 OSC 133 的提示符、命令和退出码标记建立导航位置 |
| 后台通知 | 通知关联所属会话，点击定位；隐藏会话才提示并合并频繁事件 |
| 分栏和主题 | 分栏尺寸持久化，终端字体、对比度和状态可读；动画服从减少动态效果设置 |

协议已对照标签版本的 [status.rs](https://git.zacharyzhang.com/ZacharyZhang-NY/ELYTerminal/src/tag/v0.1.3/src/term/status.rs)、[状态测试](https://git.zacharyzhang.com/ZacharyZhang-NY/ELYTerminal/src/tag/v0.1.3/src/term/status/tests.rs)、[notify.rs](https://git.zacharyzhang.com/ZacharyZhang-NY/ELYTerminal/src/tag/v0.1.3/src/term/notify.rs) 核验：

- **OSC 7501** 使用冒号分隔的 `key=value`，不是 JSON。状态包括 idle、working、done、blocked、error；clear 清除记录，`?` 查询支持情况。title、msg 使用 UTF-8 的标准 Base64，progress 为 0 至 100。记录可按分层 id 区分。兼容声明应与实际支持的字段、查询和清理行为一致。
- **OSC 9;4** 是进度协议。OSC 9 还包含 ConEmu 命令，不能把所有 OSC 9 内容显示为通知。
- **OSC 99** 有元数据、分块、编码和控制包。`p=title/body` 才是通知文本，`p=?/alive/close` 不能误弹为消息。只实现基础子集时需要如实标注。
- **OSC 777** 的通知形式为 `notify;title;body`。程序输出中的控制序列只更新对应终端状态，不能作为宿主命令执行。

本节是参考能力清单，不是当前 WorldModel 已全部通过的验收报告。实际落地情况以代码、测试和终端交互验证为准。

## Xsdsh-purge 适配结论

用户给出的名称对应 [xiaoshe1337-star/Xsdsh-purge](https://github.com/xiaoshe1337-star/Xsdsh-purge)，其上游是 [YuJunZhiXue/dsh-purge](https://github.com/YuJunZhiXue/dsh-purge)。本次读取 fork 的 main 提交 `c47f1169439313c8d6bf93a1f1b33f49ac9d646a`；证据副本在 `E:/WorldModel-terminal-browser-20261031/reference`。上游会继续变化，此处不代表未来版本。

### 它不是通用 MCP 插件

[package.json](https://github.com/xiaoshe1337-star/Xsdsh-purge/blob/c47f1169439313c8d6bf93a1f1b33f49ac9d646a/package.json) 实际版本为 `3.0.0`，声明 Node >=18、DSH `0.2.1-rc.2`、Cordis `^4.0.1`，客户端依赖 DSH 的连接、界面槽位及设置模块。README 仍显示旧版本，包内仓库和更新目标也仍有上游地址。WorldModel 的 Python harness 不具备这些宿主接口。

[lib/core.js](https://github.com/xiaoshe1337-star/Xsdsh-purge/blob/c47f1169439313c8d6bf93a1f1b33f49ac9d646a/lib/core.js)、[lib/hooks-deny.js](https://github.com/xiaoshe1337-star/Xsdsh-purge/blob/c47f1169439313c8d6bf93a1f1b33f49ac9d646a/lib/hooks-deny.js) 和 [cordis.patch.yml](https://github.com/xiaoshe1337-star/Xsdsh-purge/blob/c47f1169439313c8d6bf93a1f1b33f49ac9d646a/cordis.patch.yml) 包含修改 DSH 宿主包、权限拒绝或询问分支、系统身份与提示词的逻辑。`hooksDenyBypass` 默认开启；启动自动应用则默认关闭。这与单纯添加一个审查工具不同，直接移植可能改变用户现有权限选择和绫音人格。

fork 的 [LICENSE](https://github.com/xiaoshe1337-star/Xsdsh-purge/blob/c47f1169439313c8d6bf93a1f1b33f49ac9d646a/LICENSE) 和 package 字段标 MIT，README 又混有非商业和署名条件，和当前上游许可说明也不一致。直接分发前仍需厘清来源与许可；本轮不复制其实现或素材。

### 适合独立实现的部分

[逆向工具定义](https://github.com/xiaoshe1337-star/Xsdsh-purge/blob/c47f1169439313c8d6bf93a1f1b33f49ac9d646a/lib/reverse/tools.js) 提供了值得借鉴的组织方式：

| 参考方式 | 对应的本地能力 |
| --- | --- |
| 项目、样本与会话绑定 | 审查明确的工作区、版本和样本哈希，子任务继承审查范围 |
| 工具预检 | 列出实际安装和可用的工具；缺少时说明，不把模式名称当作工具已安装 |
| 独立角色和阶段 | 初审、证据复核、修复与回归按独立问题委派，主会话汇总 |
| 发现与证据链 | 记录位置、前提、影响、证据出处、验证状态和修复结果 |
| 报告导出 | 把已记录且已核对的结果交付为文件；待验证与已排除项分开 |

其预检会寻找 Ghidra、JADX、IDA、Frida 等外部工具，不等于插件自带这些工具。本轮新增的是 WorldModel 自己的**安全审查工作方式**，不声称提供渗透扫描器、反编译器、游戏外挂或上述第三方工具。

### 网络与审计边界

[更新模块](https://github.com/xiaoshe1337-star/Xsdsh-purge/blob/c47f1169439313c8d6bf93a1f1b33f49ac9d646a/lib/update.js) 使用 GitHub API、raw、codeload 与 jsDelivr 等读取更新信息或归档，默认仓库仍是上游。已检查文件中没有确认到隐蔽外传实现；这是有范围的静态检查，**不是全仓库或运行时无外传保证**。未执行下载脚本、插件初始化、目标扫描或提示词。

## 本轮新增的 WorldModel 入口

- 内置 `security-review` 预设，显示名“安全审查”，通过现有 Agent 预设目录和面板呈现。切换它只改变工作指令，不改会话权限、黑名单或人格；指令自身包含完整的基础审查流程。
- 随附技能 `desktop/skills/security-review/SKILL.md`。沿用现有技能发现及人设绑定机制：可在技能设置中发现，绑定到人设后由 `extensions_list` / `skill_read` 按需读取。没有为此自动改写用户的人设配置。
- 离线集成测试检查预设可选择与持久化、权限和黑名单不变，以及技能绑定前后的发现行为。测试不运行样本，不使用真实 API 或外部审查服务。

浏览器独立改造需要验证真实导航、后退前进、加载失败、地址栏和原生浏览承载方式；新增一个 iframe 或换一套外观并不能证明浏览器已完善。此文不替代相应功能测试。
