# 第八批：冷热观察持久化与群聊归档设置

2026-10-31。**候选源码与所列隔离检查完成，尚未发布，等待与工作台联合原生构建。** 第七批群聊原文默认关闭已经生效；本批增加可见、可保存的设置入口，以及咖啡热历史和再次观察的节奏。咖啡动作没有通过整体安全验收，两份实页原始结果均保留 `failed`。

## 已实现的范围

咖啡只记自己确实见过、尝过的粗证据：温度类别、粗液面、时间、推断标记和热尝试阶段。记录与液体世界同次持久化，不从旧账本或隐藏世界真值追补认识；精确温度与 ml 不进入 `coffeeEvidence` 或角色观察。刚尝过的温度证据 30 秒后仍为 unknown；过去烫过则保留。热尝后的复查间隔依次为 300、600、1200 秒，重启保留正常截止时间；到期只允许再次小口验证，不能直接说已经温热。多帧入口只算同一热尝试。

过去 30 分钟见过的非空粗液面可支持无豆时走近复查，但实际使用仍需现场观察。坏版本不恢复；未来 warm 和液面记录不能授权，未来 hot 或时钟回拨保留风险、重新等待。v2 另外校验 attemptAt 的合理跨度，并修复回拨时向旧证据对象赋值的问题。未修改 sip 几何、consume/controller、关节限位、接触门槛或自然冷却物理；这并不等于已证明全身动作无回归。

设置页在“外部渠道 → 聊天记录”提供群聊默认和第三方访客原文归档；“访问权限”群列表可逐群覆盖。三态为沿用/不保存/保存，缺失继续沿用，不在渲染时写成 false。关闭不回放、不清理旧原文，短期上下文照常；私聊原机制保持。原文归档不等于可信长期知识，完全权限也不会自动打开归档，第三方框架自己的日志不受此开关控制。

宿主原先重建 channel_config 时遗漏的全局、API 和单群 log_raw 已透传，包括 disabled 群的显式 false；非法类型原样送服务端严格拒绝，不偷偷转成布尔值。UI 分开显示本机选择与服务最近回报；服务仅给覆盖数量时，不声称某个群已经生效。旧服务缺字段显示尚未确认。

## 两轮实页分别报告

隔离 mock 服务、V8 / Silvercat / seed5、正常 dt、刚体默认关闭；同一 run 内保持同 world。没有调用模型、正式 QQ 或生产配置，没有人工降温、改液量或快进等待。固定 seed 不是固定时序；两轮不能合成同一候选的成功率。

| 记录 | 实际证据 | 原始结果与边界 |
|---|---|---|
| A：`cooling-natural-s5-01`，冻结 v1 | 首次入口 0.136965 ml；立即重试明确 wait，新增液/入口/洒液/扣豆均 0。真实等 300.0009 秒（world 299.9992 秒），第三次复用原杯实际入口 **12.864064 ml**、洒 1.302754 ml；不重冲、不扣豆，入口前 unknown + pastHot + due，入口后才 warm；0 burn。 | 原始 **failed**，唯一失败 replayNoRewardOrDebit 是动态 coffeeBuff getter 在约 1 ms 内自然衰减 1.98e-7，被逐位相等断言误报。独立复核确认 98 条真实消费及 1 条准备回执均 duplicate，持久消费/需求/库存/revision 不变。复核不是重跑，不改原判定。等待中另有 2 次右膝 pain，不能写零痛觉。 |
| B：`cooling-history-reload-s5-01`，冻结 v2 | 首次入口 0.101326 ml；立即重试 wait 且无新增。真实等 35.0028 秒后同 tab 刷新，hot 历史、阶段和原截止时间保留，当前 unknown + pastHot + wait、记得粗液面 half；真实回执重放检查通过。 | 原始 **failed**，noBurn 是真实失败：首次动作总洒 **5.037737 ml**，入口后退杯阶段发生 **5 个 heat:burn 事件**，身体汇总 1 次 pain。冷热记忆/重放通过不能抵消退杯风险。 |

A 等待期间的调试温度由 68.4636°C 降到 58.7475°C，只用于工程记录，不触发请求，也不作为角色的测温能力。300 秒不是“保证凉了”的规则。A 使用 v1，B 使用 v2；独立正常分支审计检查 306 条热记录与 311 个快照，支持 v2 两处修正不改变 A 已记录的正常分支，但**不是 v2 浏览器复跑**。

## 检查、源码与证据

- 全套 JavaScript **359 项通过**；咖啡 v2 专项 56 项、UI 9 项已包含其中，不再相加。
- **20 项实际原生映射测试通过**：从最新 settings.cpp 提取真实 channel_config，与真实 json.cpp 编译执行，再交 Python parse_conf；模型/凭证查找为替身。证明字段保留和严格拒绝，不等同于完整宿主发布验收。
- **12 项真实浏览器 UI 断言通过**，1280×1050 与 640×1050；实际模块、原 HTML/CSS、合成设置和保存桥接，覆盖三态、disabled 覆盖、其它设置保存、服务/草稿区分和窄窗口操作；6 张截图。无真实渠道访问。

源码：[咖啡策略](G:/RunProgram/WorldModel_v1/desktop/web/room-coffee-policy.js)、[液体与观察保存](G:/RunProgram/WorldModel_v1/desktop/web/room-liquid.js)、[活动执行](G:/RunProgram/WorldModel_v1/desktop/web/room-acts.js)、[归档 UI](G:/RunProgram/WorldModel_v1/desktop/web/panel-qq-retention.js)、[原生配置映射](G:/RunProgram/WorldModel_v1/desktop/host/src/settings.cpp)、[映射测试](G:/RunProgram/WorldModel_v1/tests/test_native_settings_retention.py)。

证据工作目录：[咖啡交接](E:/WorldModel_CodexEvidence/codex-batch8-20261031/evidence/coffee-policy/handoff.md)、[两轮结果](E:/WorldModel_CodexEvidence/codex-batch8-20261031/evidence/cooling-coffee/cooling-summary.json)、[A 原始帧](E:/WorldModel_CodexEvidence/codex-batch8-20261031/evidence/cooling-coffee/cooling-natural-s5-01-frames.json)、[A 独立重放复核](E:/WorldModel_CodexEvidence/codex-batch8-20261031/evidence/cooling-coffee/cooling-natural-s5-01-replay-assessment.json)、[A 正常分支审计](E:/WorldModel_CodexEvidence/codex-batch8-20261031/evidence/coffee-policy/a-v1-normal-branch-audit.json)、[B 原始帧](E:/WorldModel_CodexEvidence/codex-batch8-20261031/evidence/cooling-coffee/cooling-history-reload-s5-01-frames.json)、[UI 结果](E:/WorldModel_CodexEvidence/codex-batch8-20261031/evidence/group-settings/browser-01/result.json)、[原生映射输出](E:/WorldModel_CodexEvidence/codex-batch8-20261031/evidence/native-retention/tests-native-map.txt)、[全套 JS 输出](E:/WorldModel_CodexEvidence/codex-batch8-20261031/evidence/root-checks/js-all.txt)。本节引用的公开证据已归档；本批尚未生产发布。

## 仍未完成

保留本批观察记忆、有限退避和设置入口；下一项明确问题是退杯飞溅与热伤。不能宣称安全连续喝完、喝干、长期稳定补水、多体型或刚体饮用已经通过。未开展新训练、疾病/药物、完整食物消费或多人世界；模拟提神不增加模型 token、API 余额或工具权限。既有 [身体状态方案](embodied-life-state.md) 与 [第七批](embodied-life-batch7.md) 历史记录保留。

## 发布与归档边界补充

发布前 root 的[只读核验](E:/WorldModel_CodexEvidence/codex-batch8-20261031/evidence/root-checks/production-before.json)记录：正式原生仍为第七批原 bin（SHA 987d273f…672ed），群/API 原文默认 false、单群例外 0；本批仍等待工作台原生修改与联合构建，不将旧服务在线误写成本批已发布。

公开源码、原始失败、独立复核及截图已归档到 `11 实验数据/会话诊断/冷热观察与群聊设置-20261031/`。源文件、知识库文件和归档清单逐项核对 SHA-256，目录与清单双向一致。设置测试目录改为 group-ui-evidence，避免被旧工具的 settings 后缀过滤误跳过；未放宽私有数据过滤。[最终核验](E:/WorldModel_CodexEvidence/codex-batch8-20261031/archive-verification-final.json)与[联合发布交接](E:/WorldModel_CodexEvidence/codex-batch8-20261031/release-handoff.md)已另存；正式原生发布仍待工作台联合验收。


## 联合部署补记（2026-11-01 03:37:23 UTC；用户当地 2026-10-31）

**已联合部署，启动与策略核查通过；本批尚未做真实小屋取消/热伤验收，不能视为全部功能验收。** 之前“候选/未发布”段落保留为部署前记录；外部工作台仍在做最后实际 UI 检查，不据此宣称全部功能验收完成。

正式原生 SHA-256 为 `64b85648f955d0ad841ce704d346419bc501bcfc1ce0eecc2f2e85be51bc087d`，PID **58588**；后端 **72884**，看守 **59692**。公开发布回执确认正常关闭、完整备份、重开成功并保留原工作台；随后只读核查确认 Resident 在线，群原文/API 归档仍关闭、单群覆盖 0，权限 global_unrestricted=true/revision=2/40 条规则及两场景注册均不变，本批源 SHA 与已测版一致。

5 份核心源的修改时间均早于对应新后端/原生进程启动，只证明启动前已在位，不能替代真实小屋动作验收。第八批退杯洒约 5.04 ml 和 5 次热伤仍是真实失败，没有因发布而消除；cleanupConfirmed 仍只表示登记的收尾程序确认。

[启动与策略核查](E:/WorldModel_CodexEvidence/codex-batch9-20261031/evidence/root-checks/production-latest.json) · [正常关闭、备份与重开](E:/WorldModel-terminal-browser-20261031/joint-release-final/native-publication.json) · [源码先于新进程启动](E:/WorldModel_CodexEvidence/codex-batch9-20261031/evidence/root-checks/production-source-order.json)。

这次补记的前后字节、diff、清单与选定公开发布 JSON 单独放入第九批 `archive-input/publication-update/`；先前已归档的 160 文件及其 frozen/清单不覆写。
