# 第九批：任务交接、取消收尾与研究方案落地

2026-10-31（用户当地日期；UTC 已为 11 月 1 日）。本轮对照四篇现有研究报告和当前源码，落实执行生命周期缺口。**这是对现有底座的修复，不是 Super-agent、通用工作流图或新动作模型已经上线。** 运行发布状态与最后测试结果在文末单独记录。

## 对方案的判断

总体支持原方案。最有价值的共同点是：让需求、执行过程、真实结果和来源可以对得上。现有 AgentHub、TeamManager、ContextStore、DeliveryStore、小屋请求与物理执行继续作为权威来源，避免引入第二套互相不认识的任务、权限和记忆。

| 机制 | 适合本项目的位置 | 先验证什么 |
|---|---|---|
| Super-agent 式协调 | 用户输入与现有任务执行器之间 | 补充要求归到正确任务，停止不被晚到消息撤销，旧产物不覆盖新要求 |
| Agent Loop | 开放问题的调查、工具执行和错误修正 | 每步使用真实工具结果，耗时作业不长期堵住输入 |
| 工作流 Graph | 视频资料研究或角色短片的具体步骤 | 依赖、失败、取消、部分重做与输入版本一致 |
| 引用关系图 | 要求、材料、运行和成果之间 | 结论能回到原文，关联本身不增加执行权限 |
| Auriga / RoboAPI 式执行契约 | 角色动作入口与结果验证 | 区分派发完毕、目标达成、收尾确认和不良后果 |
| EAT / ActionOPD | 研发评测与有条件的后续训练 | 在固定评测和同预算下证明改善；不能把团队调整或写记忆当作权重训练 |

一手核验支持以下限制：Super-agent 当前仍是依赖 DSH 的研究原型；Auriga 在推理与预览时暂停仿真；ActionOPD 的 action 是高层语义决策；RoboAPI 的 completed 不证明物理任务成功。它们提供可借鉴的机制，不能直接替换本项目持续渲染、语音与物理运行时。[Super-agent](https://github.com/deepseek-ai/dsh-superagent-prototype/blob/6337fc823ce20251c8de8085948165c81aaa701e/README.md)、[Auriga](https://github.com/lose4578/Auriga)、[ActionOPD](https://thu-nics.github.io/ActionOPD/)、[RoboAPI 协议](https://developers.pokebot.com/docs/roboapi/latest/model-contract.md)

本轮读完四份本地正文并核查相关官方来源，没有重看100个视频、逐句校听或重采2533条评论；原报告的覆盖边界原样保留。具体版本、访问限制、论文口径及完整来源见[研究复核](E:/WorldModel_CodexEvidence/codex-batch9-20261031/evidence/research-review/研究复核与实施顺序.md)。

## 实际修复一：排队要求的交接

改前在 `_next_queued` 先移除并保存队列项，然后等待事件通知，最后才调用 send。独立复现了三种问题：归档会话返回了接收结果但没有执行；事件通知期间另一个任务占用会话导致原项丢失；同一间隙停止任务后，send 又重置停止标志并启动工作。

修复后，只在接收条件仍满足时消费同一个队首；该项 ID、完整输入及材料快照与新 run 一起写入同一个会话 JSON。落盘失败不会宣称已接收或启动。拒绝时留在队列，停止信号变化后旧交接不能继续；多个交接和相同文字的不同请求不能互相吞掉。

这里采用现有临时文件加原子替换的写入方式，不宣称多个文件或外部副作用具有通用 exactly-once 保证。错误回滚范围是队列及运行元数据；已有保守协议修复和可回查的历史折叠不等同于全内存事务。服务重启后已接收的运行仍应标记 interrupted，先核对成果，不自动重放未知操作。

本项目已经有 WorkspaceJobs 的立即返回句柄和交互终端句柄；它们当前的作业记录主要在内存。**本轮没有把它们改成完整持久 Job 服务，也没有实现任务执行中的任意新约束注入。** 修复排队交接是后续异步协调的基础，不能混称为已经实现全双工 Loop。

## 实际修复二：停止回执与执行占用分开

改前 stop/timeout 立即结算并释放请求，但旧动作可能仍在收尾；另一同名请求可以抢进来。Life 还在收尾前把成功写入最近经历，取消若发生在收尾期间，旧成功可能进入后续观察。

新的请求生命周期分为排队、运行、取消中、收尾未确认和结束。运行中取消保留执行占用；只有实际收尾确认后才释放。等待收尾有有限期限，超出期限可以回报取消尚未确认，但不会释放占用或继续自主活动。晚到的真实收尾可解除占用，不能把已经返回的取消改成成功。未开始的请求能撤队才直接结束；停止适配器抛错不能伪装成停止成功。

最近经历在收尾后统一写入，取消覆盖旧成功。起身后延迟开始行走的入口也再检查原请求是否已经取消，避免停止后仍走向旧目标。这里没有修改 IK、咖啡温度、液体入口或热伤阈值。

## 实际修复三：服务端保留并检查生命周期证据

新回执包含 `outcome`、`cleanupConfirmed`、`cancelled`。服务端保留这些字段并检查类型与含义：带有新生命周期信息时，明确未收尾、失败、取消或矛盾字段都不能与旧 `ok=true` 一起转成成功。没有新字段的旧客户端仍兼容。

回执在 WebSocket 接收及对话完成路径中会规范化两次，因此增加了幂等检查和真实 mock WebSocket 回环。未确认收尾时对话明确说明“收尾尚未确认”，不能说流程已完成；精确物理真值没有因此进入角色观察。

这里验证的是请求协议与收尾程序是否完成，**不是全身无碰撞或物理任务成功的证明**。第八批真实退杯热伤仍未修复：实际入口量、归还状态、洒出与受伤需要保留为不同结果，不能通过放宽判定把它改成成功样本。

## 下一阶段的实施顺序

1. 给一个生活行为补独立目标判定和不良后果记录，分别统计完成、取消、收尾、洒出和受伤；保留真实失败，不能仅凭回调成功奖励。
2. 在现有执行器上选一个长作业，补持久 ID、约束版本、事件去重、重启对账与取消；异步协调不增加显存，仍需实际资源准入。
3. 实现一个视频资料研究工作流：获取失败有缺口，转写与画面阅读分开，重要结论有引用，改要求只重做受影响步骤。先做真实模板，再决定是否需要通用 Graph 框架。
4. 固定评测、数据来源和费用口径后，才比较单代理、固定团队与动态分工；有可靠失败数据和决策瓶颈证据后再评估 ActionOPD。原小动作模型方案保持独立，不因研究报告自动开租 GPU 或启动训练。

## 验证与发布记录

本节由本轮最终测试与发布核查补齐。当前工作证据在 `E:/WorldModel_CodexEvidence/codex-batch9-20261031/evidence/`；原始红灯、修复后结果和研究抓取失败均保留。测试使用合成会话、临时目录、mock 引擎，不向8790发送测试对话。


### 最终候选验证（未发布）

| 验证组 | 真实结果 | 口径 |
|---|---|---|
| 全套 JavaScript | **378/378 通过** | 含请求/Life 专项24项；不再单独相加。Node真实时钟的有限收尾检查是 mock 动作/物理，不是浏览器饮用或运动性能成绩。 |
| 工作台后端回归 | **128 项通过** | 包含当时20项新队列用例；核心、studio、取消生命周期、Resident和嵌套调度。1个既有jieba弃用warning。 |
| 队列最后补测 | **21/21 通过** | 前20项与128套件重叠；最后新增真实历史折叠后写盘失败检查。原文仍可回查，队列/磁盘不丢；不是全部内存回滚。 |
| room_action | **58 项通过** | 包含20个新增用例及本机mock WebSocket回环；1个既有jieba弃用warning。 |
| 真实JS回执→Python | **4个场景通过** | success、cancelled、cancel_unconfirmed、cleanup_failed；实际JS请求模块生成回执，实际Python规范化及回复，动作适配器为替身。 |

最终另补 `pet.js` 的原生 `room-do` JSON 回调透传 outcome/cleanupConfirmed/cancelled；7 项实际回调测试已包含在 JS378 中，不单独相加。它不是完整原生程序联合发布验收。

独立只读审阅未发现本轮范围内的新增阻断；纯函数额外遍历112,500种生命周期字段组合，未发现二次规范化变化或非法成功。这不是112,500次真实动作，也不与上述测试相加。原始红灯、夹具失败和第八批真实热伤保留。

证据：[JS全套](E:/WorldModel_CodexEvidence/codex-batch9-20261031/evidence/execution-audit/js-all-native-final-summary.json) · [后端128](E:/WorldModel_CodexEvidence/codex-batch9-20261031/evidence/backend-audit/tests-regression-final.txt) · [队列21](E:/WorldModel_CodexEvidence/codex-batch9-20261031/evidence/backend-audit/tests-queue-final.txt) · [room_action58](E:/WorldModel_CodexEvidence/codex-batch9-20261031/evidence/receipt-contract/tests-after.txt) · [JS→Python4场景](E:/WorldModel_CodexEvidence/codex-batch9-20261031/evidence/receipt-contract/js-python-contract-result.json) · [独立复核](E:/WorldModel_CodexEvidence/codex-batch9-20261031/evidence/review/backend-lifecycle-review.md)。

**发布状态：本批仍是已验证候选，未获联合发布完成核验。** 本条记录时正式原生仍沿用第七批 SHA `987d273f…672ed`；外部工作台会话报告独立浏览器17项真机检查通过，UI仍在回归，不能替代本批联合构建/部署验证。未向8790发送测试对话。

公开证据归档目标：`11 实验数据/会话诊断/执行可靠性与研究落地-20261031/`。公开证据已完成归档，并核对来源、知识库磁盘与清单的文件集合及全部SHA一致；发布仍另行核验。PDF原件保留E盘并登记路径/SHA，归档只收提取文本及现有过滤器允许的公开源码、结果、来源和报告，不收临时运行树、浏览器profile、私有配置、真实聊天、程序或模型。


## 联合部署补记（2026-11-01 03:37:23 UTC；用户当地 2026-10-31）

**已联合部署，启动与策略核查通过；本批尚未做真实小屋取消/热伤验收，不能视为全部功能验收。** 之前“候选/未发布”段落保留为部署前记录；外部工作台仍在做最后实际 UI 检查，不据此宣称全部功能验收完成。

正式原生 SHA-256 为 `64b85648f955d0ad841ce704d346419bc501bcfc1ce0eecc2f2e85be51bc087d`，PID **58588**；后端 **72884**，看守 **59692**。公开发布回执确认正常关闭、完整备份、重开成功并保留原工作台；随后只读核查确认 Resident 在线，群原文/API 归档仍关闭、单群覆盖 0，权限 global_unrestricted=true/revision=2/40 条规则及两场景注册均不变，本批源 SHA 与已测版一致。

5 份核心源的修改时间均早于对应新后端/原生进程启动，只证明启动前已在位，不能替代真实小屋动作验收。第八批退杯洒约 5.04 ml 和 5 次热伤仍是真实失败，没有因发布而消除；cleanupConfirmed 仍只表示登记的收尾程序确认。

[启动与策略核查](E:/WorldModel_CodexEvidence/codex-batch9-20261031/evidence/root-checks/production-latest.json) · [正常关闭、备份与重开](E:/WorldModel-terminal-browser-20261031/joint-release-final/native-publication.json) · [源码先于新进程启动](E:/WorldModel_CodexEvidence/codex-batch9-20261031/evidence/root-checks/production-source-order.json)。

这次补记的前后字节、diff、清单与选定公开发布 JSON 单独放入第九批 `archive-input/publication-update/`；先前已归档的 160 文件及其 frozen/清单不覆写。
