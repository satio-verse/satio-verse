// 开放空间行动层: 场景图 + 寻路 + 运动匹配 (走、跑、转身) + 脚部/手部 IK + 视线, 叠在 HAST-SEC 之上.
//
// 分工 (每帧在 HAST-SEC 写完骨骼之后执行):
//   - 移动或转身时, 根位移和全身姿态由运动匹配接管, 按权重从 HAST-SEC 的姿态平滑过渡过来;
//     HAST-SEC 正在播放手臂技能时, 那一侧手臂让给它 (可以边走边挥手).
//   - 停稳后权重降到 0, 交回 HAST-SEC 的待机/聊天/倾听; 没有 HAST-SEC 时运动匹配自己播待机.
//   - 触地的脚锁在地面上 (两骨骼 IK), 指向物体时手臂用 IK 对准, 看向物体时胸/颈/头分担转角, 余下交给眼睛.
//   - 物体交互 (坐下/站起/拿起/放下) 播放动作库里的整段动捕: 先走到片段要求的起点和朝向, 播放时把剩余误差
//     均匀补到根部, 让关键帧 (臀部落座, 手碰到物体) 准确落位, 关键帧前后再用手臂 IK 收尾.
//     物体高度 < 0.6 m 用蹲/弯腰片段, 0.6-1.0 m 站着用手臂 IK 伸手, >= 1.0 m 用前倾伸手片段.
// 动捕数据: 100STYLE (CC BY 4.0) 与 CMU 69/13/14/15/75/143 号, 由 tools/build_motion_db.py 重定向成 VRM 标准骨架.
// 站定后的待机: 有 StayStill 待机库 (MIT) 时由它驱动全身 (几个人各一分钟的自然待机, 不时插一个换重心、环顾、摸脸之类的
// 小动作); 说话时把身体让给 HAST-SEC 的聊天手势 (没有 HAST-SEC 时继续播待机).
import * as THREE from 'three';
import { buildGrid, findPath, nearestWalkable, pathLength, pointOnPath, projectOnPath, toCell, walkable } from './nav.js';
import { centerOfMass, convexHull, footPoints, segmentDistance, signedMargin, soleContact } from './kinematics-core.js';
import { CONTACT } from './contact.js';
import { applyParams } from './motion-params.js';
import { boxGroups, furnitureBoxes, partsDepth } from './pain.js';
import { handActionBand } from './reach-safety.js';
import { usesGrasp, putItem, supportGap, poseOnSupport, worldPose, palmPose, palmAim, aimPalm, taskPalm, syncGrasp, bindGrasp, releaseGrasp, finalizeGrasp, cancelGrasp } from './grasp-controller.js';
import { normalizePose, targetPalm, validateGrasp } from './grasp-transform.js';
import { cupPickItem, cupPickError, cupPutError, CUP_BIND_TOLERANCE, liveCupCenter, planCupPick, cupBodyDepth } from './cup-pick.js';
import { captureCupHandoff, applyCupHandoff } from './cup-handoff.js';
import { snapshotPlain, restorePlain } from './plain-state.js';
import { LIMITS } from './pose-filter.js';
import { ROM } from './rom.js';
import { buildQuery, frameCost, Inertializer, loadActions, loadDB, predict, qfromTo, qinv, qmul, samplePose, search,
  springVelocity, springYaw, wrapAngle } from './mm.js';

// 情绪 -> 步态风格库 (tools/build_motion_db.py 的 STYLES). 害羞、平静 2026-10-24 补: CMU 的害羞走、慢走 (含直角转弯)
const STYLE_OF = { happy: 'elated', excited: 'elated', sad: 'depressed', angry: 'angry', shy: 'shy', calm: 'calm' };
// 步态: 动捕是成年人的正常步态, 按腿长缩放后仍是"大步向前". stride 缩小髋关节摆幅 (步子变小),
// cadence 加快播放 (步频变高); 根运动乘 stride x cadence, 支撑脚由脚部锁定钉住, 不打滑. 跑步不缩.
export const GAIT = {
  neutral: { stride: 0.72, cadence: 1.25 },   // 小步快走
  elated: { stride: 0.78, cadence: 1.32 },
  angry: { stride: 0.85, cadence: 1.2 },
  depressed: { stride: 0.7, cadence: 0.95 },  // 小步慢走
};
// 每根骨骼的父骨骼候选 (取模型里第一个存在的)
const PARENTS = {
  hips: [], spine: ['hips'], chest: ['spine', 'hips'], upperChest: ['chest', 'spine'],
  neck: ['upperChest', 'chest', 'spine'], head: ['neck', 'upperChest', 'chest'],
  leftShoulder: ['upperChest', 'chest'], rightShoulder: ['upperChest', 'chest'],
  leftUpperArm: ['leftShoulder', 'upperChest', 'chest'], rightUpperArm: ['rightShoulder', 'upperChest', 'chest'],
  leftLowerArm: ['leftUpperArm'], rightLowerArm: ['rightUpperArm'], leftHand: ['leftLowerArm'], rightHand: ['rightLowerArm'],
  leftUpperLeg: ['hips'], rightUpperLeg: ['hips'], leftLowerLeg: ['leftUpperLeg'], rightLowerLeg: ['rightUpperLeg'],
  leftFoot: ['leftLowerLeg'], rightFoot: ['rightLowerLeg'], leftToes: ['leftFoot'], rightToes: ['rightFoot'],
};
const ARM = { left: ['leftShoulder', 'leftUpperArm', 'leftLowerArm', 'leftHand'],
  right: ['rightShoulder', 'rightUpperArm', 'rightLowerArm', 'rightHand'] };
const SIDE = { L: 'left', R: 'right', B: 'right' };
const HEAD_AHEAD = 0.18;  // 站着时头的中心在根前面多远 (米, 按髋高 0.78 m 换算; 小屋诊断量的 silvercat 站着、走路 0.17-0.23 m; 见 _headDepth)
// 放回出厂状态 (restoreFactory) 时不拍也不动的: 动作库、场景、外面给的设置和开关 (端法、测试用的不转身、小窝的姿势层让手)
const LOCO_KEEP = ['_factory', 'db', 'dbs', 'acts', 'idleDB', 'gestDB', 'idleRank', 'seatRank', 'scene', 'targets', 'grid', 'body',
  'carryCare', 'handSpeed', 'noTurn', 'holdIdle', 'carryYield', 'squatting'];
// 可调参数 (秒的都是半衰期). 默认值是手调的; tools/tune_motion.py 按执行评估自动搜索, 结果写进动作库目录的
// params.json, 载入时覆盖 (见 motion-params.js). 改这里的名字要同步改 motion-params.js 的参数表.
export const TUNE = {
  simVel: 0.27, simRot: 0.27,       // 模拟骨的速度/朝向弹簧
  searchEvery: 0.1,                 // 运动匹配搜索间隔
  blendMatch: 0.12,                 // 运动匹配切片段的惯性化过渡
  blendLeaveIdle: 0.2,              // 待机 -> 运动匹配 (开始走动)
  blendToLoco: 0.25,                // 动作片段 -> 运动匹配
  blendIdle: 0.45,                  // 待机换段、插小动作
  blendIdleBack: 0.6,               // 小动作播完换回一分钟待机
  blendGestureIn: 0.3, blendGestureOut: 0.3,  // 动捕手势进出
  blendAction: 0.15,                // 拿放片段
  weightIn: 0.12, weightOut: 0.25,  // 行动层接管 / 交回 HAST-SEC
  lockIn: 0.04, lockOut: 0.07,      // 脚着地锁定权重升 / 降
  lockSettle: 0.06,                 // 锁定点高度落到地面
  replant: 0.3,                     // 锁定点离动画脚踝超过这么远 (米) 就放开重新落脚 (转身用)
  hipDropDown: 0.08, hipDropUp: 0.2,  // 腿够不着地时骨盆下降 / 回升
  groundRaise: 0.01, groundLower: 0.03,  // 落地: 鞋底踩进地面时抬起 / 离地时放下
  balanceMargin: 0.02, balanceMax: 0.1,  // 配平: 稳定裕度低于 margin (米) 时把骨盆往支撑面里挪, 双脚站着时最多 max
  // 配平时整个身体跟着绕脚踝倾多少 (1 = 踝策略, 骨盆和上身跟腿转同样的角度; 0 = 只平移骨盆, 以前的做法). 对比过:
  // 只平移上限 10 cm 时挥手髋越界 1 -> 56; 全踝倾过头 (miomio 待机重心 18 -> 73, 像真人分变差); 一半最好
  balanceAnkle: 0.5,
  balanceMaxSingle: 0.08,           // 配平: 一只脚着地或在做动作时骨盆最多挪多少 (米)
  balanceIn: 0.35, balanceOut: 0.5,
  // 拿放选站位: 0 = 每件家具的整体外框 (保守, 默认), 1 = 实际部件 (桌子底下是空的). 对比过: 用部件时站得更贴近桌子,
  // 碰家具更少, 但关节越界、物品穿模、手臂被推开变多 (两个模型代价都约 1.003); 量痛觉用部件, 规划留余量. 不参与调优
  standParts: 0,
  // 坐姿按座位和腿长修正 (见 _sitGroups、_seatReach): 1 = 默认; 0 = 以前的坐法 (不挑片段, 腿够不着时骨盆往下拉), 对比用, 不参与调优
  seatReach: 1,
  // 坐着手放腿上: 0 = 掌心转成朝下 (默认); 1 = 手指顺着大腿朝膝盖、掌心贴大腿. 对比过: 顺着大腿时指尖被推开的反而多
  // (坐下场景 miomio 5.9 -> 8.0, wendy 2.9 -> 5.6, 不往里偏、手腕放宽到 75 度也一样), 不用; 不参与调优
  lapAlign: 0,
  // 选拿放片段时, 片段本身超出关节活动范围的每 1 度算多少米"插进家具" (和 _clipBump 的深度相加比): 0.001 = 1 mm
  strainCost: 0.001,
  // 配平层的目标用鞋底的着地部分 (1) 还是脚踝到脚尖的长方形 (0). 执行评估按鞋底量; 配平目标用长方形更好 (见 _balance). 不参与调优
  soleSupport: 0,
  // 锁定着的脚放平的程度 (见 _feet; 0 = 跟着动画的倾角, 以前的做法). 不参与调优
  footFlat: 1,
  // 预判迈步: 往后看多少秒的着地标记, 要抬的脚提前不算支撑 (见 _liftSoon; 0 = 不预判, 以前的做法)
  balanceAhead: 0.35,
  // 站着时要重新落脚的脚最多等几秒 (等重心挪到另一只脚上再抬; 0 = 不等, 以前的做法)
  stepWait: 0.4,
  // 两腿防穿插 (见 _separateLegs; 0 = 不管, 以前的做法) 和两条小腿之间额外留的缝 (米)
  legSep: 1, legGap: 0.005,
  // 推开量变大 / 变小的半衰期 (秒). 变大 0.04 / 0.02 / 0.01: 走路两腿穿插 30 / 21 / 17 (8 个种子), 动作冲击不变
  legSepIn: 0.01, legSepOut: 0.15,
  // 坐下 / 站起时从坐下程度多少开始把手放到腿上、到多少放满. 以前 0.6 / 0.8: 坐下那段手跟着动捕往凳子上撑, 骨盆按座面
  // 抬到正确高度以后手腕、指尖擦进大腿和骨盆 (坐下场景手臂被推开 miomio 276 -> 321, wendy 211 -> 283); 提早到 0.15 / 0.5
  // 以后 268 / 265. 再早 (0.05 / 0.35) 反而多. 不参与调优
  lapFrom: 0.15, lapFull: 0.5,
  // 手搭到腿上 / 离开腿的半衰期 (秒). 搭上去以前 0.3: 坐下只有 1 秒左右, 手有一大段停在动捕的手和腿上中间, 正好穿过大腿
  // (wendy 坐下手臂被推开 265); 0.12 时 205 (改坐姿前 211), 0.06 推得更少但动作冲击、像真人分略差. 不参与调优
  lapIn: 0.12, lapOut: 0.15,
  // 风格: 安静、少女、优雅 (2026-10-25 用户: "太多动了, 姿势也不够少女和优雅"). 见 calmIdle、_style、_armIn、_narrow.
  // 这一组不参与调优: 像真人分按动捕的统计量, 会把它们推回动捕原样 (更多动、更开), 和用户要的正相反
  idlePool: 5,                      // 待机库只用最安静的几段 (按上半身每秒转动的角度排; StayStill 12 段里差 7 倍)
  idleSpeed: 0.85,                  // 待机和换重心放慢播放
  fidgetGap: 25,                    // 两次小动作 (换重心) 之间至少隔多久 (秒), 再加 0-1 倍随机
  calm: 0.4, calmTau: 0.9,          // 待机时上半身朝慢变化的姿态收 calm (削掉小抖动、晃动), calmTau: 慢变化的时间常数 (秒)
  gestureSpeed: 0.85, gestureCalm: 0.25,  // 动捕手势 (挥手、耸肩) 放慢播放, 上半身同样收一点: 挥手轻一些 (双手挥手手腕冲击最大)
  walkArm: 0.35,                    // 走路时手臂摆幅收掉多少
  armIn: 0.5, armInMax: 8,          // 垂着的手臂向身体收: 比防穿模的最小外展角多出来的部分收掉 armIn, 最多 armInMax 度
  stance: 0.8, stanceSoft: 0.35,    // 站着时脚到骨盆中线不超过半个髋宽的 stance 倍, 超出的部分只留 stanceSoft
  stepWidth: 0.6,                   // 走路时落脚点同样往中线收 (走成一条线)
  fidgetsOnly: 1,                   // 小动作只留换重心 (FIDGETS); 0 = 原来的全部 (对比用)
  fidgetPlant: 1,                   // 换重心时脚不动 (只挪骨盆)
  // 站起来时根部跟着脚往前移 (秒, 时间常数; 0 = 不跟, 对比用): 动捕是坐凳子录的, 起身时演员把脚收到凳子下面再站,
  // 套到实心沙发上脚被 _seatFoot 留在前沿外, 髋部却按片段停在原处. 以前起身最后一步脚落回片段里的位置, 插进沙发底座
  // 12 cm, 交回运动匹配时根部再往前跳 20 cm
  riseTau: 0.25,
  seatHeel: 0.1,                    // 坐下/坐着/站起时脚踝离座位前沿至少多远 (米, 脚后跟在脚踝后面 5-6 cm)
  // 坐着待机时根拉回对准座位的那一点的半衰期 (秒; 0 = 不拉, 以前的做法, 对比用; 见 _playAction). 不参与调优
  seatAnchor: 0.5,
  // 坐着待机: 上半身朝慢变化的姿态收多少 (同 calm; 0 = 不收, 以前的做法), 只在坐着那段最安静的几组里挑 (0 = 都用; 见 calmSeats).
  // 2026-10-27 用户说坐在床边乱动、乱晃. 不参与调优, 理由同下面的风格一组
  seatCalm: 0.4, seatPool: 10,
  // 站起来时两只脚踩在坐着时的位置不动 (0 = 照片段, 对比用). 以前脚从坐着的落脚点滑回坐下前锁定的位置 (横向 20 -> 30 cm),
  // 有一组片段最后还迈一步, 单脚支撑时重心偏出 25 cm. 站稳以后脚太开由待机的 _narrow 和换脚收回来
  risePlant: 1,
  // 拿放片段里脚离锁定点多远才放开重新落脚 (米; 别的时候 TUNE.replant). 拿放片段开头的站姿常和走到位时差 20 cm 以上,
  // 以前一开始就放开一只脚换到片段的站姿, 单脚撑着弯腰放东西, 重心出支撑面 10-19 cm (放下时 128)
  grabReplant: 0.35,
  lapClear: 0.08,                   // 手往腿上放时贴着大腿表面走, 不穿过去 (见 _lap): 推开量的平滑半衰期 (秒); 0 = 直线挪 (对比用), 1 = 不平滑
  // 拿放的交接 (最小冲击曲线, 见 _carry; 2026-10-27 动作成长会话要的可调键). 0 = 关掉 (以前那样: 拿住以后伸手 / 拿放 IK 退掉、端姿再升上来;
  // 放下前端姿先退回待机姿势、片段和拿放 IK 把手送下去), 对比用, 不在搜索范围里.
  // handoffPick: 拿住以后手送到端着的位置用 handoffPick x (1 + 端稳) / 拿放快慢 秒.
  // handoffPut: 放下前手从端着的位置走到放下的点, 从关键帧前这么多秒 (片段时间; 站着伸手按它的时间线) 开始走; 片段更短就整段走.
  // 默认 1.5 = 动作库里最长的放下片段 (开头到关键帧 0.43-1.5 秒, 站着伸手 0.7), 都是整段走
  handoffPick: 0.9, handoffPut: 1.5,
  // 仪态 (2026-10-26 用户: "开始优化人物的行为、姿态"; 见 _poise、_clasp). 不参与调优, 理由同上面的风格一组
  // 脖子前倾 (颈根到头的连线偏离竖直往前的角度) 超过 headTiltMax 度的部分收掉 headUp, 头的朝向大致不变. 动捕待机 10-27 度, 探着头
  headUp: 0.6, headTiltMax: 8,
  clasp: 0.6,                       // 站着待机时双手在腰前交叠的时间占比 (每 12-25 秒重选一次; 0 = 不交叠, 对比用)
  claspIn: 0.5, claspOut: 0.25,     // 交叠 / 放开的半衰期 (秒)
  headCock: 4,                      // 待机看着用户时头往一边歪的角度 (度; 0 = 不歪)
  mood: 1, moodHold: 90,            // 情绪体态 (见 MOOD; 0 = 不跟情绪变, 对比用); 多少秒没有新情绪回到平静
  // 托腮 / 捧脸时头相对上胸最多低到多少度 (负的是仰着; 见 _emote). 捧脸仰到 -12 以上时指尖翻到额头上 (手腕最多弯 75 度, 手指被拉向前臂)
  chinLift: -16, cheeksLift: -8,
  // 端东西有多稳 (2026-10-27, 小窝物理 P0: 满杯咖啡端着走洒得多, 主要因为手跟着髋部每一步上下颠、左右晃 2-3 cm, 和走多快关系不大).
  // 0 = 原来那样手跟着髋部; 1 = 手跟着身体平滑后的路径, 手臂把颠和晃抵掉, 拿起后端到身前也慢一些. 行动层的 carryCare 没设时用这个
  carryCare: 0,
  // 拿放的快慢 (行动层的 handSpeed 没设时用这个): 站着伸手拿放 (1.6 秒)、蹲下弯腰拿放的动作片段、拿起后端到身前, 都按它放慢 / 加快.
  // 端着满杯时慢一点洒得少 (小窝物理 P0 评测台: 端稳以后剩下的洒大半在拿起、端到身前、放下这几段). 她以后自己挑
  handSpeed: 1,
  cupCarry: 1,                      // 杯子 (grip 'cup') 端在腰前 (和小窝姿势层一样); 0 = 和书一样夹在身侧 (对比用)
};
// 情绪体态 (2026-10-26 用户: "继续优化, 特别是手部动作、肢体语言"): 按对话的情绪 (服务端每轮发的 emo) 改待机的体态, 以前只有脸和走法跟着变.
// head: 头往下低多少度 (负 = 抬下巴); chest: 胸往前含多少度 (负 = 挺胸); shoulder: 肩往上收多少度 (害羞、吃惊时缩肩, 难过时塌肩);
// cock: 看着用户时歪头的角度; cockIdle: 没看着人时也时不时歪头; clasp: 站着待机时双手在腰前交叠的时间占比; low: 交叠的手放低多少 (米);
// akimbo: 叉腰的时间占比 (生气). 试过换成待机库里抱臂的那段 (idle_09): 片段里多数时候是一只手抬在肩前、手指张着, 看不出抱臂.
// 走路时头和胸只加一半 (情绪步态的动作库本身就带姿态)
export const MOOD = {
  neutral: { head: 0, chest: 0, shoulder: 0, cock: 4, cockIdle: 0, clasp: 0.6, low: 0, akimbo: 0 },
  calm: { head: 2, chest: 0, shoulder: 0, cock: 3, cockIdle: 0, clasp: 0.75, low: 0, akimbo: 0 },
  happy: { head: -3, chest: -2, shoulder: 0, cock: 6, cockIdle: 1, clasp: 0.4, low: 0, akimbo: 0 },
  excited: { head: -4, chest: -3, shoulder: 1, cock: 7, cockIdle: 1, clasp: 0.25, low: 0, akimbo: 0 },
  sad: { head: 12, chest: 6, shoulder: -2, cock: 0, cockIdle: 0, clasp: 0.85, low: 0.015, akimbo: 0 },
  shy: { head: 9, chest: 2, shoulder: 4, cock: 6, cockIdle: 1, clasp: 1, low: 0.005, akimbo: 0 },
  angry: { head: -3, chest: -3, shoulder: 0, cock: 0, cockIdle: 0, clasp: 0, low: 0, akimbo: 1 },
  surprised: { head: -4, chest: -3, shoulder: 3, cock: 0, cockIdle: 0, clasp: 0.3, low: 0, akimbo: 0 },
};
// 表情动作 (2026-10-26 用户: "给大模型加新动作: 比心、捂嘴笑、托腮、拍手等"; 服务端 wm/intent.py 的 EMOTES, 见 _emote):
// dur: 做多久 (秒, 除以 spd); hands: one = side 那只手, both = 两只手; shape: 手形 (hand-shapes.js), 托腮另一只手横在腰前托着肘
export const EMOTES = {
  heart: { dur: 2.8, hands: 'both', shape: 'heart' },
  giggle: { dur: 2.2, hands: 'one', shape: 'flat' },
  chin: { dur: 3.5, hands: 'both', shape: 'clasp' },
  clap: { dur: 2.2, hands: 'both', shape: 'flat' },
  peace: { dur: 2.6, hands: 'one', shape: 'peace' },
  cheeks: { dur: 2.8, hands: 'both', shape: 'clasp' },
};
// 待机里插的小动作只留左右换重心. 摸脸、挠头、挠胳膊、看表、伸懒腰、环顾在角色身上都像"乱动", 手还会贴到脸、头发、袖子上穿模
// (桌宠的桌面待机从 2026-10-24 起就只留这两种, 2026-10-25 起小屋和主页面也一样)
export const FIDGETS = ['wei_lr', 'wei_rl'];
const Z_AXIS = new THREE.Vector3(0, 0, 1);
const STYLE_BONES = new Set(['spine', 'chest', 'upperChest', 'neck', 'head', ...ARM.left, ...ARM.right]);

/** 待机库的一分钟待机按上半身转动排序 (db.calmOrder, 安静的在前); 换段时只在前 TUNE.idlePool 段里挑 (见 _idleRandom). */
export function calmIdle(db) {
  const ids = db?.groups?.idle;
  if (!ids?.length) return null;
  const ranked = ids.map((ci) => ({ ci, deg: upperSpeed(db, ci) })).sort((x, y) => x.deg - y.deg);
  db.calmOrder = ranked.map((r) => r.ci);
  return ranked.map((r) => ({ name: db.clips[r.ci].name, deg: +r.deg.toFixed(0) }));
}

/** 坐姿组按"坐着"那段同样排 (g.calm, 度/秒): 坐着待机只在最安静的 TUNE.seatPool 组里挑 (见 _sitGroups).
 *  动作库的坐着段差得很多: 最安静的上半身每秒转二三十度, cmu19_12 那段两百多度 (一直在比划, 不像坐着发呆). */
export function calmSeats(db) {
  const groups = db?.groups?.sit;
  if (!groups?.length) return null;
  for (const g of groups) g.calm = g.idle === null || g.idle === undefined ? Infinity : upperSpeed(db, g.idle);
  return groups.slice().sort((x, y) => x.calm - y.calm).map((g) => ({ name: db.clips[g.idle]?.name, deg: +g.calm.toFixed(0) }));
}

/** 一段片段中间 (去掉头尾各 1 秒) 上半身 (脊柱、胸、颈、头、手臂) 每秒转多少度. */
function upperSpeed(db, ci) {
  const upper = db._upper ??= db.bones.map((n, i) => [n, i]).filter(([n]) => /spine|chest|neck|head|Arm|Hand/i.test(n)).map(([, i]) => i);
  const a = [], b = [];
  const c = db.clips[ci], step = Math.max(1, Math.round(db.fps / 10));
  let s = 0, n = 0;
  for (let f = c.start + db.fps; f + step < c.end - db.fps; f += 2 * step) {
    samplePose(db, f, a);
    samplePose(db, f + step, b);
    for (const i of upper) s += 2 * Math.acos(Math.min(1, Math.abs(a[i][0] * b[i][0] + a[i][1] * b[i][1] + a[i][2] * b[i][2] + a[i][3] * b[i][3])));
    n++;
  }
  return (s / Math.max(1, n)) * (db.fps / step) * (180 / Math.PI);  // 度/秒
}
const FUTURE = [1 / 3, 2 / 3, 1];
const IDENTITY_Q = new THREE.Quaternion();
const deg = (r) => (r * 180) / Math.PI;
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const damp = (halflife, dt) => 1 - Math.exp((-Math.LN2 * dt) / Math.max(1e-4, halflife));
const pick = (list) => list[Math.floor(Math.random() * list.length)];
/** 根坐标系里的 (x, z) 按朝向 yaw 转到世界 (yaw=0 时 +Z 朝前). */
const rotate = (x, z, yaw) => [Math.cos(yaw) * x + Math.sin(yaw) * z, -Math.sin(yaw) * x + Math.cos(yaw) * z];
const smooth = (t) => { const x = clamp(t, 0, 1); return x * x * (3 - 2 * x); };
const minJerk = (t) => { const x = clamp(t, 0, 1); return x * x * x * (10 - 15 * x + 6 * x * x); };  // 起止速度、加速度都是 0

export async function fetchScene(url = '/static/scenes/room.json') {
  return (await fetch(url)).json();
}

async function fetchDB(style) {
  const [meta, buf] = await Promise.all([
    fetch(`/motiondb/${style}.json`).then((r) => { if (!r.ok) throw new Error(`没有运动数据库 ${style}`); return r.json(); }),
    fetch(`/motiondb/${style}.motion`).then((r) => r.arrayBuffer()),  // 就是 .bin (服务端的别名, 见 wm/app.py)
  ]);
  return loadDB(meta, buf);
}

async function fetchActions(name = 'actions') {
  const [meta, buf] = await Promise.all([
    fetch(`/motiondb/${name}.json`).then((r) => { if (!r.ok) throw new Error(`没有动作库 ${name}`); return r.json(); }),
    fetch(`/motiondb/${name}.motion`).then((r) => r.arrayBuffer()),
  ]);
  return loadActions(meta, buf);
}

/** 可以拿起的东西: 球或书本形状的简单几何体. */
function makeItem(o) {
  const color = new THREE.Color(o.color || '#888888');
  const mat = new THREE.MeshStandardMaterial({ color, roughness: 0.6 });
  const [a, b, c] = o.item.size;
  const mesh = o.item.shape === 'ball'
    ? new THREE.Mesh(new THREE.SphereGeometry(a, 24, 16), mat)
    : new THREE.Mesh(new THREE.BoxGeometry(a, b, c), [mat, mat, new THREE.MeshStandardMaterial({ color: '#f3efe6' }), mat, mat, mat]);
  mesh.name = o.id;
  mesh.position.fromArray(o.item.pos);
  return mesh;
}

/** 动作库目录里有自动调优的结果 (params.json, tools/tune_motion.py 写的) 就覆盖默认参数; 没有就用默认值. 只载入一次. */
let tunedLoaded = null;
export function loadTunedParams() {
  tunedLoaded ??= fetch('/motiondb/params.json').then((r) => (r.ok ? r.json() : null)).then((p) => {
    if (p?.values) applyParams({ tune: TUNE, filter: LIMITS, contact: CONTACT }, p.values);
    return p;
  }).catch((err) => { console.warn('调优参数没载入, 用默认值', err); return null; });
  return tunedLoaded;
}

export async function createLocomotion(avatar, scene, { body = null } = {}) {
  const layer = new Locomotion(avatar, scene, body);
  layer.dbs.neutral = await fetchDB('neutral');
  layer.db = layer.dbs.neutral;
  layer.acts = await fetchActions().catch((err) => { console.warn(err); return null; });
  layer.idleDB = await fetchActions('idle').catch(() => null);  // StayStill 待机库, 没生成就用原来的方式
  layer.gestDB = await fetchActions('gestures').catch(() => null);  // CMU 手势库 (挥手/耸肩/说话手势), 没生成就用原来的方式
  await loadTunedParams();
  layer.idleRank = calmIdle(layer.idleDB);  // 调过参数 (TUNE.idlePool) 之后再挑
  layer.seatRank = calmSeats(layer.acts);
  layer.init();
  return layer;
}

class Locomotion {
  constructor(avatar, scene, body) {
    this.avatar = avatar;
    this.vrm = avatar.vrm;
    this.scene = scene;
    this.body = body;
    this.dbs = {};
    this.style = 'neutral';
    this.grid = buildGrid(scene);
    this.targets = Object.fromEntries(scene.objects.map((o) => [o.id, o]));
    this.v0 = this.vrm.meta?.metaVersion === '0';
    this.baseYaw = this.vrm.scene.rotation.y;
    this.weight = 0;
    this.mode = 'idle';
    this.queue = [];
    this.stats = { searches: 0, switches: 0, searchMs: 0 };
    this._q = new THREE.Quaternion();
    this._v = new THREE.Vector3();
  }

  init() {
    const vrm = this.vrm, h = vrm.humanoid;
    const node = (n) => h.getNormalizedBoneNode(n);
    this.nodes = Object.fromEntries(this.db.bones.map((n) => [n, node(n)]).filter(([, b]) => b));
    this.parentOf = Object.fromEntries(Object.keys(this.nodes).map((n) => [n, PARENTS[n].find((p) => this.nodes[p]) || null]));
    // 零位关节位置 (模型坐标, 换成 VRM 1.0 朝向): 标准化骨骼零位旋转都是单位阵, 局部位置沿父链相加即可.
    const rest = h.normalizedRestPose;
    const byNode = new Map(Object.entries(h.humanBones).map(([n, b]) => [node(n), n]).filter(([k]) => k));
    const world = {};
    const pos = (n) => {
      if (world[n]) return world[n];
      const p = new THREE.Vector3().fromArray(rest[n]?.position || [0, 0, 0]);
      let parent = node(n).parent;
      while (parent && !byNode.has(parent)) parent = parent.parent;
      if (parent) p.add(pos(byNode.get(parent)));
      world[n] = p;
      return p;
    };
    const fix = (p) => (this.v0 ? new THREE.Vector3(-p.x, p.y, -p.z) : p.clone());
    this.rest = Object.fromEntries(Object.keys(this.nodes).map((n) => [n, fix(pos(n))]));
    if (node('leftMiddleProximal')) this.rest.leftHandEnd = fix(pos('leftMiddleProximal'));
    if (node('rightMiddleProximal')) this.rest.rightHandEnd = fix(pos('rightMiddleProximal'));
    // 没有 HAST-SEC 时自己把脚放到地面上 (HAST-SEC 已经做过)
    if (!this.body) {
      vrm.scene.position.y -= new THREE.Box3().setFromObject(vrm.scene).min.y;
    }
    this.groundY = -vrm.scene.position.y;   // 世界地面 y=0 在模型坐标里的高度
    this.hipHeight = this.rest.hips.y - this.groundY;
    // 站直时头的中心 (头骨往上 9 cm x 身材, 同小屋体感层) 离地多高: 选站位、伸手前倾时看头会不会伸进家具 (见 _headDepth)
    this.headY = (this.rest.head ? this.rest.head.y - this.groundY : this.hipHeight * 1.9) + 0.09 * this.hipHeight / 0.78;
    this.ankle = { left: this.rest.leftFoot.y - this.groundY, right: this.rest.rightFoot.y - this.groundY };
    // 模型的骨骼方向对齐: 标准方向 -> 这个模型零位时的方向
    const canon = this.db.meta.canon, aim = this.db.meta.aim;
    this.align = {};
    for (const n of Object.keys(this.nodes)) {
      const target = this.rest[aim[n]];
      const dir = target ? target.clone().sub(this.rest[n]).toArray() : canon[n];
      this.align[n] = qinv(qfromTo(canon[n], dir));  // 存 A^-1
    }
    // 角色位置: 出生点
    const [sx, sz] = this.scene.spawn.pos;
    this.char = { x: sx, z: sz, yaw: this.scene.spawn.yaw || 0 };
    this.sim = { x: sx, z: sz, vx: 0, vz: 0, ax: 0, az: 0 };
    this.simRot = { yaw: this.char.yaw, w: 0 };
    this.desired = { vx: 0, vz: 0, yaw: this.char.yaw };
    this.inert = new Inertializer(this.db.B, 0.12);
    this.pose = [];
    this.out = Array.from({ length: this.db.B }, () => [0, 0, 0, 1]);
    this.outHips = [0, 1, 0];
    this.query = new Float32Array(27);
    this.frame = this._idleFrame();
    this.searchTimer = 0;
    this.feet = { left: { lock: null, w: 0, was: false }, right: { lock: null, w: 0, was: false } };  // w: 锁定权重 (0..1, 连续变化)
    this.sole = this._soleSamples();  // 每只脚的鞋底取样顶点, 落地时用
    this.legShape = this._legShape();  // 小腿胶囊的半径和中心偏移、脚的半径: 两腿防穿插用
    this._waistFront();               // 腰前衣服离脊柱多远 (双手交叠用), 趁还是静止姿势时量
    this._face = this._faceGeom();    // 脸、胸前的尺寸 (表情动作的手放哪), 同样趁静止姿势量
    this.groundOff = 0;               // 落地: 身体整体上下挪的量 (米, 世界坐标)
    this.look = { target: null, until: 0, yaw: 0, pitch: 0, gaze: false };
    this.pointing = { side: null, target: null, until: 0, w: 0 };
    this.offer = { side: null, until: 0, w: 0 };  // 伸手邀请牵手 (见 offerHand)
    this.settle = 1;  // 一开始就是站稳的, 不要先接管再放开
    this.action = null;     // 正在播放的动作片段 (坐下/站起/拿/放)
    this.seated = null;     // 坐着时: {group, seat, next}
    this.reaching = null;   // 站着伸手 (手臂 IK) 的时间线
    this.hipsExtraY = 0;    // 坐下时按座面高度修正的髋部高度 (米)
    this.seatAmount = 0;    // 坐下的程度 (0 站着, 1 坐稳), 决定落脚点往座位前沿外挪多少
    this.idle = null;       // StayStill 待机播放状态: {db, clip, frame, end, kind, spd, nextSwitch, nextAction}; kind 为 gesture 时播的是手势库
    this.gestureReq = null; // 等着开始的动捕手势: {clip, spd, plant, until}
    this.speaking = false;  // 角色在说话 (app.js 设置): 没有 HAST-SEC 时说话间隙插说话手势
    this.nextTalk = 0;
    this.rawHips = [0, 1, 0];
    this.idleLog = [];      // 调试: 最近播放的待机片段
    this.items = {};
    this.carryW = { left: 0, right: 0 };  // 持物姿态的权重 (拿着东西、这只手又没在做别的动作时升到 1)
    this.carryU = { left: 0, right: 0 };  // 持物姿态的进度 (匀速走, carryW 是它的 S 形, 见 _carry)
    this.lapW = { left: 0, right: 0 };    // 坐着时手放在腿上的权重
    this.claspW = 0;                      // 站着待机时双手在腰前交叠的权重 (见 _clasp)
    this.claspPlan = { on: false, until: 0, top: 'right', mode: null, next: null };
    this.poiseW = 0; this.cock = { w: 0, side: 1, target: null };  // 仪态层 (见 _poise)
    this.mood = { name: 'neutral', at: -1e9, k: { ...MOOD.neutral } };  // 情绪体态: 当前情绪、什么时候来的、平滑后的参数
    this.noTurn = false;   // 测试用: 不转身 (见 turn)
    this.holdIdle = false; // 测试用: 不换待机段、不插换重心 (见 _idleStep)
    this.emoteSt = null;   // 正在做的表情动作 (见 emote): {kind, side, amp, dur, asked, start, w}
    this.emoteNext = null; // 排在它后面的一个
    this.itemGroup = new THREE.Group();
    this.itemGroup.name = 'items';
    for (const o of this.scene.objects.filter((x) => x.item)) {
      const mesh = makeItem(o);
      this.itemGroup.add(mesh);
      this.items[o.id] = { id: o.id, mesh, held: null, half: o.item.shape === 'ball' ? o.item.size[0] : o.item.size[1] / 2,
        on: o.item.on || 'floor', origin: null };  // on: 现在放在哪 (floor/放置点); origin: 拿起之前在哪, "放回去"用
    }
    this._applyRoot();
    // 出厂状态 (还没跑过一帧; 执行评估每个场景复位时放回, 见 plain-state.js). 惯性化过渡是类的实例, 另外拍; 人形骨骼的局部姿态也拍
    // (出厂时行动层的权重是 0, 头几帧不写骨骼, 骨骼停在上一个场景最后的样子, 平滑层也从那里起算)
    const hum = this.vrm.humanoid;
    const bones = Object.keys(hum.humanBones).map((n) => hum.getNormalizedBoneNode(n)).filter(Boolean);
    this._factory = { self: snapshotPlain(this, LOCO_KEEP), inert: snapshotPlain(this.inert),
      bones: [this.vrm.scene, ...bones].map((b) => [b, b.position.clone(), b.quaternion.clone(), b.scale.clone()]) };
  }

  /** 放回出厂状态 (执行评估复位用): 跑过的状态都清掉, 骨骼回到出厂的姿态, 动作库、场景、外面给的设置 (LOCO_KEEP) 不动. */
  restoreFactory() {
    if (!this._factory) return;
    this._graspCancel('抓握评估复位', false);
    this._cancelCupPick('取杯评估复位');
    this._cancelCupPut('放杯评估复位');
    restorePlain(this, this._factory.self, LOCO_KEEP);
    restorePlain(this.inert, this._factory.inert);
    for (const [b, p, q, s] of this._factory.bones) { b.position.copy(p); b.quaternion.copy(q); b.scale.copy(s); }
    this.vrm.scene.updateMatrixWorld(true);
  }

  _idleFrame() {
    const id = this.db.clips.find((c) => c.kind === 'ID');
    return id ? id.start + 30 : this.db.clips[0].start;
  }

  // -- 指令 ---------------------------------------------------------------------------------
  /** 走到场景目标的站位, 到了以后面向它. after: 到达后再执行的回调. */
  goTo(id, { speed = 1, after = null } = {}) {
    const t = this.targets[id];
    if (!t) return false;
    const face = this._lookPoint(t);
    const goal = id === 'user' ? this._userSpot() : this._headRoom(t.approach, face);
    return this._walkTo(goal, { id, face, speed, after });
  }

  /** 走到任意一点; 到了以后面向 face (世界点) 或朝向 yaw. */
  _walkTo(goal, { id = null, face = null, yaw = null, speed = 1, after = null, graspTask = null } = {}) {
    if (this._graspTask && this._graspTask !== graspTask) this._graspCancel('抓握被新的移动打断');
    const path = findPath(this.grid, [this.char.x, this.char.z], goal);
    if (!path) return false;
    if (yaw !== null) face = new THREE.Vector3(goal[0] + Math.sin(yaw) * 2, 1.2, goal[1] + Math.cos(yaw) * 2);
    this.path = path;
    this.pathLen = pathLength(path);
    this.goal = { id, face, run: speed >= 1.6 };
    this.speedScale = speed >= 1.6 ? 1 : clamp(speed, 0.6, 1.4);
    this.mode = 'go';
    this.queue = after ? [after] : [];
    this.searchTimer = 0;
    return true;
  }

  /**
   * 执行一个要花时间的动作 (走到/转身/坐下/站起/拿起/放下), 做完调用 done(失败原因或 null).
   * 坐着时先站起来再执行 (站起本身除外).
   */
  run(a, done = () => {}) {
    if (this._graspTask && ['go', 'turn', 'sit', 'stand'].includes(a.do)) this._graspCancel('抓握被新的动作打断');
    const fail = (msg) => { this.onNotice?.(msg); done(msg); };
    const standing = (fn) => (this.seated ? this.standUp(fn) : fn());
    const name = (id) => this.targets[id]?.name || id;
    switch (a.do) {
      case 'go': return standing(() => this.goTo(a.to, { speed: a.spd, after: () => done(null) }) || fail(`走不到${name(a.to)}`));
      case 'turn': return standing(() => { this.turn({ side: a.side, to: a.to }); this.queue = [() => done(null)]; });
      case 'sit': return this.seated ? done(null) : this.sit(a.to || 'sofa', done, fail);
      case 'stand': return this.seated ? this.standUp(() => done(null)) : done(null);
      case 'pick': return standing(() => this.pick(a.to, done, fail));
      case 'put': return standing(() => this.put(a.to, done, fail));
      default: return done(null);
    }
  }

  /** 坐到座位上: 选一段从站定开始的坐下片段, 走到它要求的起点和朝向, 播放时把臀部对准座面. */
  sit(id, done, fail) {
    const seat = this._seatInfo(id);
    if (!seat) return fail(`${this.targets[id]?.name || id}上坐不了`);
    if (!this.acts) return fail('没有动作库, 坐不了');
    // 能站起来、膝盖在座位前沿外的组 (见 _sitGroups); 再优先从站定开始坐下的
    const all = this._sitGroups(seat, true);
    const calm = all.filter((g) => g.startSpeed < 0.45);
    const g = pick(calm.length ? calm : all);
    if (!g) return fail('动作库里没有合适的坐下片段');
    const H = this.hipHeight, startYaw = seat.yaw - g.yaw;
    const [ox, oz] = rotate(g.hips[0] * H, g.hips[2] * H, startYaw);
    const start = nearestWalkable(this.grid, seat.pos[0] - ox, seat.pos[1] - oz);
    const ok = this._walkTo(start, { yaw: startYaw, after: () => this._sitDown(g, seat, done) });
    if (!ok) fail('走不到座位前');
  }

  /** 这个座位上能用的坐姿组: 坐着时脚着地 (不是踩着凳子横档), needUp 时还要能站起来. 膝盖要在座位前沿外 (按这个模型的
   *  髋高换算, 见 build_motion_db.py 的 knee_forward): 坐凳子录的有几段把腿收到座位下面, 套到沙发上膝盖插进坐垫前沿.
   *  没有合适的就都用. */
  _sitGroups(seat, needUp) {
    const all = this.acts.groups.sit.filter((g) => (!needUp || g.up !== null) && g.feetY < 0.15);
    const fit = all.filter((g) => !TUNE.seatReach || g.kneeFwd === undefined || g.kneeFwd * this.hipHeight >= seat.front - 0.03);
    const list = fit.length ? fit : all;
    // 只用坐着那段最安静的 TUNE.seatPool 组 (见 calmSeats; 0 = 都用, 以前的做法)
    if (!(TUNE.seatPool > 0) || list.length <= TUNE.seatPool || list.some((g) => g.calm === undefined)) return list;
    return list.slice().sort((a, b) => a.calm - b.calm).slice(0, Math.round(TUNE.seatPool));
  }

  _sitDown(g, seat, done) {
    const H = this.hipHeight;
    const [ox, oz] = rotate(g.hips[0] * H, g.hips[2] * H, this.char.yaw);
    this._startAction(g.down, {
      warp: { x: seat.pos[0] - (this.char.x + ox), z: seat.pos[1] - (this.char.z + oz), yaw: wrapAngle(seat.yaw - (this.char.yaw + g.yaw)) },
      seat: this._seatLift(g, seat), halflife: 0.2,
      onDone: () => { this._seatedIdle(g, seat); done(null); },
    });
  }

  /** 坐着待机: 循环播放这一组的"坐着"段; 每 10-16 秒换一段别的坐姿, 并把髋部重新对准座位. */
  _seatedIdle(g, seat) {
    const clip = this.acts.clips[g.idle];
    const hy = this.acts.hips, s = clip.start * 3, H = this.hipHeight;
    const [ox, oz] = rotate(hy[s] * H, hy[s + 2] * H, this.char.yaw);
    this.seated = { group: g, seat, next: performance.now() / 1000 + 10 + Math.random() * 6 };
    this._startAction(g.idle, {
      loop: true, seat: this._seatLift(g, seat), halflife: 0.35, warpUntil: 20,
      warp: { x: seat.pos[0] - (this.char.x + ox), z: seat.pos[1] - (this.char.z + oz), yaw: wrapAngle(seat.yaw - this.char.yaw) },
    });
    // 对准座位的根 (补完 warp 以后在哪): 坐着时根拉回这里 (TUNE.seatAnchor, 见 _playAction)
    const [ax, az] = rotate(hy[s] * H, hy[s + 2] * H, seat.yaw);
    this.action.anchor = { x: seat.pos[0] - ax, z: seat.pos[1] - az, yaw: seat.yaw };
    this.mode = 'seated';
  }

  /** 站起来: 播放当前这组的"站起"段, 然后交回运动匹配. */
  standUp(after = null) {
    if (!this.seated) { after?.(); return; }
    const g = this.seated.group;
    const up = g.up ?? this.acts.groups.sit.find((x) => x.up !== null).up;
    const seat = this.seated.seat;
    if (TUNE.risePlant) {
      for (const side of ['left', 'right']) {
        const st = this.feet[side];
        if (st.seatT) { st.lock = st.seatT.clone(); st.w = 1; st.replant = false; }
      }
    }
    this._startAction(up, { seat: this._seatLift(g, seat), halflife: 0.2, rise: true,
      onDone: () => { this.seated = null; this._returnToLocomotion(after); } });
  }

  /** 源片段的坐姿髋高 -> 这个角色坐在这个座面上的髋高, 差值随"坐下的程度"加到髋部上. */
  _seatLift(g, seat) {
    const H = this.hipHeight;
    return { src: g.seatHips, delta: seat.height + 0.12 * H - g.seatHips * H, info: seat };
  }

  /** 座位 + 座位前沿到髋部落点的距离 (沿坐下后的朝向, 由家具的包围盒算出). 脚要放在前沿外面. */
  _seatInfo(id) {
    const t = this.targets[id], seat = t?.seat;
    if (!seat) return null;
    const f = [Math.sin(seat.yaw), Math.cos(seat.yaw)];
    let front = 0.25;
    if (t.box) {
      const [cx, cz] = t.box.center, [sx, sz] = t.box.size;
      front = Math.max(...[[-1, -1], [1, -1], [-1, 1], [1, 1]].map(([a, b]) =>
        (cx + (a * sx) / 2 - seat.pos[0]) * f[0] + (cz + (b * sz) / 2 - seat.pos[1]) * f[1]));
    }
    return { ...seat, front, id };
  }

  /** 坐着时的落脚点: 动捕多是在凳子上录的, 脚常收在座位下面, 套到实心的沙发上小腿会插进前脸.
   *  这里把脚放到前沿外 0.12-0.42 m, 左右各 7-20 cm (不交叉), 保留片段原来的前后/左右变化. */
  _seatFoot(side, t, seat) {
    const f = [Math.sin(seat.yaw), Math.cos(seat.yaw)], l = [Math.cos(seat.yaw), -Math.sin(seat.yaw)];
    const rx = t.x - seat.pos[0], rz = t.z - seat.pos[1];
    const fw = clamp(rx * f[0] + rz * f[1], seat.front + 0.12, seat.front + 0.42);
    const sgn = side === 'left' ? 1 : -1;
    const lt = sgn * clamp((rx * l[0] + rz * l[1]) * sgn, 0.07, 0.2);
    return new THREE.Vector3(seat.pos[0] + f[0] * fw + l[0] * lt, t.y, seat.pos[1] + f[1] * fw + l[1] * lt);
  }

  /** 坐着时的落脚点 t (已经按座位挪到前沿外) 再按这个角色的腿长修一遍. 坐姿片段是坐凳子录的, 座位高度、腿长都和角色不一样:
   *  1. 腿伸直也够不着 (有的片段把腿伸得很远): 沿座位朝向把落脚点往回收到刚好够得着, 但不收进前沿外 0.1 m 以内
   *     (再往里小腿就插进座位前脸); 还够不着就让脚离地悬着. 以前是把骨盆往下拉 (站着时"腿够不着地"的补偿),
   *     骨盆陷进座位 5-20 cm, 膝盖跟着插进沙发底座.
   *  2. 膝盖还在座面上方却低于座面 (腿短的角色坐高沙发, 腿斜着往下够地面, 膝盖压进坐垫前沿): 膝盖抬到座面高度,
   *     大腿搭在座面上, 小腿从膝盖往下, 够得着地就踩地, 够不着就悬着 (矮个子坐高沙发本来就这样).
   *  hip: 大腿根的世界位置, a / b: 大腿、小腿 (到脚踝) 长. 改的就是 t, 返回 t. */
  _seatReach(hip, t, a, b, seat) {
    const f = [Math.sin(seat.yaw), Math.cos(seat.yaw)], max = (a + b) * 0.98, floor = t.y;
    const hipAlong = (hip.x - seat.pos[0]) * f[0] + (hip.z - seat.pos[1]) * f[1];
    let dx = t.x - hip.x, dz = t.z - hip.z;
    const dy = hip.y - t.y;
    if (Math.hypot(dx, dy, dz) > max) {
      const fw = dx * f[0] + dz * f[1], sx = dx - fw * f[0], sz = dz - fw * f[1];
      const minFw = seat.front + 0.1 - hipAlong;
      const room = max * max - dy * dy - sx * sx - sz * sz;
      const goal = Math.min(fw, Math.max(minFw, room > 0 ? Math.sqrt(room) : minFw));
      t.set(hip.x + sx + f[0] * goal, t.y, hip.z + sz + f[1] * goal);
      const flat = Math.hypot(sx, goal);
      if (Math.hypot(flat, dy) > max) t.y = hip.y - Math.sqrt(Math.max(0, max * max - flat * flat));
      dx = t.x - hip.x;
      dz = t.z - hip.z;
    }
    // 膝盖在哪 (两骨 IK 朝前上方的那个解), 在"髋 -> 落脚点"的竖直平面里算: u 水平朝落脚点, v 向上
    const D = Math.hypot(dx, dz), h = hip.y - t.y, d = Math.hypot(D, h);
    if (D < 1e-3) return t;
    const ux = dx / D, uz = dz / D, ahead = ux * f[0] + uz * f[1];
    const phi = Math.atan2(-h, D) + Math.acos(clamp((a * a + d * d - b * b) / (2 * a * d), -1, 1));
    let ku = a * Math.cos(phi), kv = a * Math.sin(phi);
    const vMin = seat.height - hip.y;
    if (kv >= vMin || hipAlong + ku * ahead > seat.front + 0.03 || a <= -vMin || ahead < 0.3) return t;
    kv = vMin;
    ku = Math.sqrt(a * a - kv * kv);
    const drop = hip.y + kv - floor;  // 膝盖离落脚高度多高
    // 小腿尽量踩到地 (脚在膝盖前下方), 但脚不进前沿外 6 cm 以内; 够不着就往前斜 15 度悬着
    const uMin = (seat.front + 0.06 - hipAlong) / ahead;
    const du = Math.min(b * 0.99, Math.max(uMin - ku, b > drop ? Math.sqrt(b * b - drop * drop) : b * 0.26));
    const fv = Math.hypot(du, drop) <= b ? floor - hip.y : kv - Math.sqrt(b * b - du * du);
    t.set(hip.x + ux * (ku + du), hip.y + fv, hip.z + uz * (ku + du));
    return t;
  }

  /** 拿起东西: 低处用蹲/弯腰片段, 桌面高度站着伸手, 高处用前倾伸手片段. */
  pick(id, done, fail) {
    const item = this.items[id];
    if (!item) return fail(`${this.targets[id]?.name || id}拿不起来`);
    if (item.held) return done(null);
    if (usesGrasp(item)) return this._graspPick(item, done, fail);
    if (cupPickItem(item)) return this._cupPick(item, done, fail);
    const side = this._freeHand();
    if (!side) return fail('两只手都拿着东西');
    const p = item.mesh.position.clone();
    const grip = p.clone().add(new THREE.Vector3(0, item.half + 0.05, 0));
    this.lookAt(id, 4);
    this._handAction(side, p, grip, 'pick', () => {
      this._grabGap(item, side, item.mesh.position);
      item.origin = { on: item.on, pos: item.mesh.position.clone() };
      item.on = null;
      item.held = side;
      // 拿住以后 _carry 从这里把手送到端着的位置 (起点钉在世界里拿的那一点; 试过记在髋部坐标系里跟着身体走: 蹲着拿起来再站起来时
      // 髋部转得多, 手跟着甩, 拿放场景拿起后那段 heldAcc 4.2 -> 14.8, 撤了). TUNE.handoffPick 0 = 不交接
      if (TUNE.handoffPick > 0) (this._pickHand ??= {})[side] = { from: this._handPos(side), t: 0 };
    }, done, fail, { center: item.mesh.position, half: item.half, cup: item.cupBinding ? item : null });
  }

  /** 放下手里的东西: 指定了台面就放到台面的放置点, 否则放在脚前的地上. */
  put(to, done, fail) {
    const target = to && this.targets[to], item = putItem(this.items, target);
    if (!item) return fail(target && Object.hasOwn(target, 'itemId') ? '指定的物件没有拿在手里' : '手里没有东西');
    if (usesGrasp(item)) return this._graspPut(item, to, done, fail);
    if (item.cupBinding && cupPickItem(item)) return this._cupPut(item, to, done, fail);
    const side = item.held;
    if (to === 'home') {
      // 放回原处: 原来在台面上就放回那个放置点, 原来在地上就走回原来的位置放下
      const o = item.origin;
      if (!o) return fail('不知道它原来在哪');
      if (o.on !== 'floor') to = o.on;
      else {
        const p = o.pos.clone().setY(0);
        const grip = p.clone().add(new THREE.Vector3(0, item.half * 2 + 0.05, 0));
        return this._handAction(side, p, grip, 'put', () => this._release(item, p, 'floor'), done, fail,
          { center: p.clone().setY(p.y + item.half), half: item.half, cup: item.cupBinding ? item : null });
      }
    }
    const place = to && this.targets[to]?.place;
    if (to && !place) return fail(`${this.targets[to]?.name || to}上放不了东西`);
    if (!place) {
      // 放地上: 原地播放放下片段, 松手时落在手的正下方
      const g = this._clipGroup('put', side);
      if (!g) return fail('没有放下的动作');
      return this._startAction(g.clip, { event: g.eventOffset, halflife: TUNE.blendAction,
        onEvent: () => this._release(item, this._handPos(side).setY(0), 'floor'),
        onDone: () => this._returnToLocomotion(() => done(null)) });
    }
    const p = this._placePoint(to, side);
    const grip = p.clone().add(new THREE.Vector3(0, item.half * 2 + 0.05, 0));
    this.lookAt(to, 4);
    this._handAction(side, p, grip, 'put', () => this._release(item, p, to), done, fail,
      { center: p.clone().setY(p.y + item.half), half: item.half, cup: item.cupBinding ? item : null });
  }

  // Cups share the visible center convention for both acquisition and support placement.
  _cupPick(item, done, fail) {
    return this._cupTransfer(item, 'pick', item.mesh.position.clone(), null, done, fail);
  }

  _cupPut(item, to, done, fail) {
    let center, on = to;
    if (to === 'home') {
      if (!item.origin) return fail(cupPutError('UNREACHABLE', '不知道杯子原来放在哪里'));
      center = item.origin.pos.clone(); on = item.origin.on;
    } else {
      const place = to && this.targets[to]?.place;
      if (!place || !place.every(Number.isFinite)) return fail(cupPutError('UNREACHABLE', '暂时没有明确的杯子放置位置'));
      center = new THREE.Vector3(...place).add(new THREE.Vector3(0,item.half,0));
    }
    return this._cupTransfer(item, 'put', center, on, done, fail, to);
  }

  _cupTransfer(item, kind, target, on, done, fail, requestedTo = null) {
    const errorFor = kind === 'pick' ? cupPickError : cupPutError;
    if (this._cupPickTask || this._cupPutTask) return fail(errorFor('UNREACHABLE', '上一次拿放杯子还未收尾'));
    const side = kind === 'pick' ? this._freeHand() : item.held;
    if (!side) return fail(errorFor('UNREACHABLE', kind === 'pick' ? '两只手都拿着东西' : '杯子已经不在手里'));
    const band = handActionBand(target.y), groups = this._clipGroups(band === 'low' ? kind : 'reach', side);
    const previousPickPlan = this.lastCupPickPlan;
    const stand = planCupPick(this, item, side, groups, target);
    if (kind === 'put') { this.lastCupPutPlan = this.lastCupPickPlan; this.lastCupPickPlan = previousPickPlan; }
    if (!stand) return fail(errorFor('UNREACHABLE', kind === 'pick' ? '暂时找不到能安全拿到杯子的位置' : '暂时找不到能安全放下杯子的位置'));
    const key = kind === 'pick' ? '_cupPickTask' : '_cupPutTask';
    const task = { kind, key, item, side, target, on, requestedTo, ready: false, resolved: false,
      bound: false, released: false, wait: 0, finished: false };
    task.finish = () => {
      if (task.finished) return;
      task.finished = true;
      if (this[key] === task) this[key] = null;
      if (task.bound || task.released) done(null);
      else fail(task.error || errorFor('CONTACT', kind === 'pick' ? '还没稳稳拿住杯子，先停下了' : '杯子还没放稳，继续拿在手里'));
    };
    this[key] = task;
    this.lookAt(item.id, 4);
    const ok = this._walkTo(stand.pos, { yaw: stand.yaw, after: () => {
      if (this[key] !== task || task.finished || task.resolved) return;
      if (task.isOperationActive?.() === false) { this._cancelCupTransfer(task, '拿放杯子的请求已过期', task); return; }
      // Preflight sampled an in-place clip. Source root drift must not walk into the counter.
      this._startAction(stand.g.clip, { event: stand.g.eventOffset, halflife: TUNE.blendAction,
        grab: { side, target: target.clone(), kind, slow: this._putSlow(kind),
          hold: { center: target, half: item.half, cup: item, cupTask: task } },
        warp: { x: stand.pos[0] - this.char.x, z: stand.pos[1] - this.char.z,
          yaw: wrapAngle(stand.yaw - this.char.yaw) },
        onEvent: () => { if (!task.resolved) task.ready = true; },
        onDone: () => this._returnToLocomotion(task.finish) });
    } });
    if (!ok) { task.error = errorFor('UNREACHABLE', '走不到拿放杯子的位置'); task.finish(); }
    return ok;
  }

  _cancelCupPick(reason = '取杯已停止', expectedTask = null) {
    return this._cancelCupTransfer(this._cupPickTask, reason, expectedTask);
  }
  _cancelCupPut(reason = '放杯已停止', expectedTask = null) {
    return this._cancelCupTransfer(this._cupPutTask, reason, expectedTask);
  }
  _cancelCupTransfer(task, reason, expectedTask) {
    if (!task || (expectedTask && task !== expectedTask) || task.finished || task.bound || task.released) return false;
    task.resolved = true; task.ready = false;
    task.error = (task.kind === 'put' ? cupPutError : cupPickError)('CANCELLED', reason);
    if (this.action?.grab?.hold?.cupTask === task) this.action.fired = true;
    else { this.queue = []; this.path = null; task.finish(); }
    return true;
  }

  _cupRelease(item, center, on) {
    // Preserve the measured final world center. Never snap to the requested support point.
    item.mesh.position.copy(center);
    item.held = null; item.on = on;
  }

  // Called after all displayed-pose constraints. No corrective IK or physics bypass here.
  _cupPickFinalize(dt) {
    const task = this._cupPickTask || this._cupPutTask, action = this.action;
    if (!task || !task.ready || task.resolved || task.finished) return false;
    const put = task.kind === 'put', errorFor = put ? cupPutError : cupPickError;
    if (task.isOperationActive?.() === false) { this._cancelCupTransfer(task, '拿放杯子的请求已过期', task); return false; }
    const changed = put ? task.item.held !== task.side : task.item.held || task.item.mesh.position.distanceTo(task.target) > .001;
    if (changed) {
      task.error = errorFor('CONTACT', '杯子的位置或占用已经改变，停止这次拿放');
      task.resolved = true; task.ready = false; if (action?.grab?.hold?.cupTask === task) action.fired = true; return false;
    }
    if (action?.grab?.hold?.cupTask !== task) { this._cancelCupTransfer(task, '拿放杯子的动作已被替换', task); return false; }
    const center = liveCupCenter(this, task.item, task.side);
    const error = center ? center.distanceTo(task.target) : Infinity;
    const supportError = center ? Math.abs(center.y - task.target.y) : Infinity;
    const boxes = this.avatar.collision?.() || this._standBoxes(), points = {};
    for (const n of ['head','chest','hips','leftLowerLeg','rightLowerLeg']) if (this.nodes[n]) points[n] = this._world(this.nodes[n]);
    if (points.head) points.head.add(new THREE.Vector3(0,.09*this.hipHeight/.78,0).applyQuaternion(this.nodes.head.getWorldQuaternion(new THREE.Quaternion())));
    const depth = cupBodyDepth(this, boxes, points), report = { error, supportError, depth,
      tolerance: CUP_BIND_TOLERANCE, supportTolerance: put ? .003 : null, kind: put ? 'support-center' : 'binding-center', passed: false };
    if (put) this.lastCupPutContact = report; else this.lastCupPickContact = report;
    if (Number.isFinite(error) && error <= CUP_BIND_TOLERANCE && (!put || supportError <= .003) && depth <= 1e-6) {
      const {item,side} = task;
      this._grabGap(item, side, task.target, true); // separate historical convention
      if (put) { this._cupRelease(item, center, task.on); task.released = true; }
      else {
        item.origin = { on: item.on, pos: item.mesh.position.clone() };
        item.on = null; item.held = side; item.cupBinding = true;
        if (TUNE.handoffPick > 0) (this._pickHand ??= {})[side] = { from: this._handPos(side), t: 0, cupPose: captureCupHandoff(this, side) };
        task.bound = true;
      }
      task.resolved = true; task.ready = false; action.fired = true;
      report.passed = true;
      return true;
    }
    task.wait += Math.max(0, dt);
    if (task.wait >= .75) {
      task.error = errorFor('CONTACT', put ? '杯子还没放稳，继续拿在手里' : '还没稳稳拿住杯子，先停下了');
      task.resolved = true; task.ready = false; action.fired = true;
    }
    return false;
  }

  // Explicit surface grasps are opt-in: old items and physical cups never enter here.
  _graspPalm(side) { return palmPose(this, side); }
  _graspAim(side, pose, weight = 1) { return aimPalm(this, side, pose, weight); }
  _graspBind(item, side) { return bindGrasp(this, item, side); }
  _graspRelease(item, targetPose, on) { return releaseGrasp(this, item, targetPose, on); }
  _graspFinalize(dt) { return finalizeGrasp(this, dt); }
  _graspCancel(reason, notify = true) { return cancelGrasp(this, reason, notify); }

  _graspPick(item, done, fail) {
    let grasp;
    try { grasp = validateGrasp(item.grasp); this._graspPalm(grasp.preferredHand); }
    catch (e) { return fail(e.message); }
    const side = grasp.preferredHand;
    if (Object.values(this.items).some(i => i.held === side)) return fail('指定的抓握手已被占用');
    return this._graspStart({ kind: 'pick', item, side, grasp, targetPose: worldPose(item.mesh) }, done, fail);
  }

  _graspPut(item, to, done, fail) {
    if (!item.graspBinding || item.graspBinding.side !== item.held) return fail('抓握绑定不一致，不能直接换手');
    const origin = item.graspOrigin;
    let targetPose, surfaceY, on = to;
    if (to === 'home') {
      if (!origin) return fail('不知道它原来在哪');
      targetPose = origin.pose; on = origin.on;
      // A tilted original pose has a different support extent from its initial half-height.
      surfaceY = supportGap(item, origin.pose, 0);
    } else {
      const t = to && this.targets[to];
      if (!t?.place) return fail('没有明确的抓握放置目标');
      // A true home target supplies both arrays. Generic surfaces preserve the held
      // orientation; unsupported/tilted targets must pass the same support gate.
      const quaternion = t.objectQuaternion || worldPose(item.mesh).quaternion;
      try { targetPose = t.objectPosition ? { position: t.objectPosition, quaternion } : poseOnSupport(item, t.place, quaternion); }
      catch (e) { return fail(e.message); }
      surfaceY = t.place[1];
    }
    let grasp;
    try { grasp = validateGrasp(item.grasp); targetPose = normalizePose(targetPose); }
    catch (e) { return fail(e.message); }
    return this._graspStart({ kind: 'put', item, side: item.held, grasp, targetPose, surfaceY, on, binding: item.graspBinding.pose }, done, fail);
  }

  _graspStart(task, done, fail) {
    if (this._graspTask) return fail('另一个抓握动作尚未结束');
    const { item, side, grasp, targetPose } = task;
    // This first contract supports standing reach only. Do not silently fall back
    // to a clip event that attaches an object before measured contact.
    const contact = targetPalm(targetPose, grasp), point = new THREE.Vector3(...contact.position);
    if (handActionBand(point.y) !== 'middle') return fail('这个抓握高度暂不支持站立伸手');
    if (task.kind === 'put' && (!Array.isArray(item.graspHalfExtents) || !Number.isFinite(task.surfaceY)))
      return fail('缺少物件支撑几何');
    const out = new THREE.Vector3(...grasp.normal).applyQuaternion(new THREE.Quaternion(...targetPose.quaternion));
    const lat = this._shoulderLat(side);
    let stand = null;
    for (const radius of [0.35, 0.4, 0.45, 0.5, 0.55, 0.65, 0.75, 0.85]) {
      const candidate = this._standFor(point, [lat, radius], c => {
        const delta = new THREE.Vector3(c.pos[0] - point.x, 0, c.pos[1] - point.z);
        // Reach the visible side of the actual surface; never through the object.
        if (delta.dot(out) < 0.12) return Infinity;
        return this._headDepth(c.pos, c.yaw);
      });
      if (candidate && Number.isFinite(candidate.bump) && (!stand || candidate.bump < stand.bump - 1e-4)) stand = candidate;
      if (stand && stand.bump <= 1e-4) break;
    }
    if (!stand) return fail('走不到抓握点的可达站位');
    if (stand.bump > 1e-4) return fail('抓握站位的头部空间不足');
    task.wait = 0; task.ready = false; task.finished = false;
    task.finish = () => {
      if (task.finished) return;
      task.finished = true;
      if (this._graspTask === task) this._graspTask = null;
      if (task.succeeded) done(null); else fail(task.error || '抓握动作未完成');
    };
    this._graspTask = task;
    this.lookAt(item.id, 4);
    const ok = this._walkTo(stand.pos, { yaw: stand.yaw, graspTask: task, after: () => {
      if (this._graspTask !== task || task.finished) return;
      if (Math.hypot(this.char.x - stand.pos[0], this.char.z - stand.pos[1]) > 0.15) {
        task.error = '没有到达抓握站位'; task.finish(); return;
      }
      const palm = taskPalm(task), grip = new THREE.Vector3(...palm.position);
      this._reachStart(side, grip, null, task.finish, { kind: task.kind,
        hold: { graspTask: task, center: new THREE.Vector3(...targetPose.position), half: item.half } });
    } });
    if (!ok) { task.error = '走不到抓握点'; task.finish(); }
    return ok;
  }

  /** 放到 to 上的哪一点: 场景给的放置点 (place). 那里站不下时 (个子小的放下片段手离脚近, wendy 最远 0.48 m; 沙发的放置点正对着茶几,
   *  得斜着站到茶几那边, 最近也要 0.58 m, 放到沙发上 3/3 "走不到那里") 往站位 (approach) 那边挪, 留在这件家具的外框里 (离边 8 cm),
   *  每次 5 cm、最多 0.8 m, 挪到站得下为止 (人够不着里面就放在靠外的地方); 都不行用原来的点. 高度不变 (同一个台面). */
  _placePoint(to, side) {
    const t = this.targets[to], p0 = new THREE.Vector3(...t.place);
    if (!t.approach || !t.box || this._canReach(p0, side, 'put')) return p0;
    const [cx, cz] = t.box.center, [sx, sz] = t.box.size, m = 0.08;
    const dir = new THREE.Vector3(t.approach[0] - p0.x, 0, t.approach[1] - p0.z).normalize();
    for (let d = 0.05; d <= 0.8 + 1e-6; d += 0.05) {
      const p = p0.clone().addScaledVector(dir, d);
      p.x = clamp(p.x, cx - sx / 2 + m, cx + sx / 2 - m);
      p.z = clamp(p.z, cz - sz / 2 + m, cz + sz / 2 - m);
      if (this._canReach(p, side, 'put')) return p;
    }
    return p0;
  }

  /** 在 point 拿 / 放有没有站得下的站位 (和 _handAction 一样的挑法, 只看站位能不能走; 不打乱片段的顺序, 不动随机数). */
  _canReach(point, side, kind) {
    const h = point.y, H = this.hipHeight, band = handActionBand(h);
    if (band === 'middle') return [0.45, 0.55, 0.65, 0.75, 0.85].some((r) => this._standFor(point, [this._shoulderLat(side), r]));
    const groups = (this.acts?.groups[band === 'low' ? (kind === 'put' ? 'put' : 'pick') : 'reach'] || []).filter((g) => g.side === side);
    return groups.some((g) => this._standFor(point, [g.hand[0] * H, g.hand[2] * H]));
  }

  /** 伸手那边的肩离身体中线多远 (米, 往左为正; 站着伸手时让这边的肩正对着目标, 见 _handAction). */
  _shoulderLat(side) {
    this.vrm.scene.updateMatrixWorld(true);
    const shN = this.nodes[`${side}UpperArm`];
    return shN ? this._world(shN).sub(new THREE.Vector3(this.char.x, 0, this.char.z))
      .dot(new THREE.Vector3(Math.cos(this.char.yaw), 0, -Math.sin(this.char.yaw))) : 0;
  }

  _release(item, surface, on) {
    if (item.held) this._grabGap(item, item.held, new THREE.Vector3(surface.x, surface.y + item.half, surface.z));
    item.held = null;
    item.on = on;
    // 交给物理的: 不瞬移到落点, 给物理层一个落点 (手接着拿着往下放, 放稳了才松开; 见 web/physics/rigid-scene.js)
    if (item.physical) { item.target = { p: [surface.x, surface.y + item.half, surface.z], q: item.target?.q, place: true }; return; }
    item.mesh.position.set(surface.x, surface.y + item.half, surface.z);
    item.mesh.rotation.set(0, item.mesh.rotation.y, 0);
  }

  _freeHand() {
    const used = new Set(Object.values(this.items).map((i) => i.held).filter(Boolean));
    return ['right', 'left'].find((s) => !used.has(s)) || null;
  }

  /** 按种类和手挑一段动作: 起始站定、手在身前 0.3-0.75 髋高之内的优先. */
  _clipGroup(kind, side) {
    return this._clipGroups(kind, side)[0] || null;
  }

  /** 同上, 按优先顺序排好的全部候选 (好的在前, 各自随机打乱): 首选的站位被家具挡住时换下一段. */
  _clipGroups(kind, side) {
    const groups = (this.acts?.groups[kind] || []).filter((g) => g.side === side);
    const isGood = (g) => g.startSpeed < 0.25 && Math.abs(g.hand[0]) < 0.25 && g.hand[2] > 0.3 && g.hand[2] < 0.75;
    const shuffle = (list) => list.map((g) => [Math.random(), g]).sort((a, b) => a[0] - b[0]).map(([, g]) => g);
    return [...shuffle(groups.filter(isGood)), ...shuffle(groups.filter((g) => !isGood(g)))];
  }

  /**
   * 手到某一点去拿/放: 按目标高度选做法, 先走到合适的站位再执行.
   * point: 物体或放置点 (地面/台面上的点), grip: 手腕要到的位置 (没有 hold 时用; 也用来定前倾).
   * hold: {center, half} 要拿的东西 / 放下后东西的中心: 手腕按"掌心持物点落在 center"对准 (见 _grab).
   */
  _handAction(side, point, grip, kind, onEvent, done, fail, hold = null) {
    const h = point.y, band = handActionBand(h);
    if (band === 'middle') {
      // 站着伸手: 站在离目标 0.45 m 起、够得着的最近处 (桌子挡着时往外让), 面向它; 伸手那边的肩正对着它 (往另一边让开肩宽).
      // 以前正对着身体中线: 书桌上靠里的东西, 肩到目标 0.58 m, 前倾到 70 度也够不着, 拿起那一刻手还差 10-12 cm
      // 头会伸进家具 (书架隔板里的书) 的站位往后排: 每个距离按头碰不碰挑朝向, 要头碰不到的里面最近的; 都碰就要碰得最浅的
      const lat = this._shoulderLat(side);
      let stand = null;
      for (const r of [0.45, 0.55, 0.65, 0.75, 0.85]) {
        const s = this._standFor(point, [lat, r], (c) => this._headDepth(c.pos, c.yaw));
        if (s && (!stand || s.bump < stand.bump - 1e-4)) stand = s;
        if (stand && stand.bump <= 1e-4) break;
      }
      if (!stand) return fail('走不到那里');
      const ok = this._walkTo(stand.pos, { yaw: stand.yaw,
        after: () => this._reachStart(side, grip, onEvent, () => done(null), { hold, kind }) });
      return ok || fail('走不到那里');
    }
    const groups = this._clipGroups(band === 'low' ? (kind === 'put' ? 'put' : 'pick') : 'reach', side);
    if (!groups.length) return fail('没有合适的动作');
    // 每段片段要求的站位不同 (手离脚多远、在哪一侧): 首选那段的站位被家具挡住时换下一段, 都站不下才算走不到
    // (以前只随机挑一段, 茶几旁边放东西时有时直接失败; 执行评估查出来的)
    // 蹲下、弯腰时膝盖和脚尖会往前伸: 每段片段的每个站位都量一下会不会插进家具 (放到矮桌上时膝盖会插进桌子),
    // 优先选不碰的; 都碰就选插得最浅的
    // 片段本身超出关节活动范围的 (蛙式全蹲: 髋屈 150 度以上、外展 50 度以上) 也往后排: 每超 1 度算插进家具 1 mm
    const H = this.hipHeight;
    let g = null, stand = null, best = Infinity;
    for (const c of groups) {
      const s = this._standFor(point, [c.hand[0] * H, c.hand[2] * H], (cand) => this._clipBump(c, cand));
      const cost = s ? s.bump + this._clipStrain(c) * TUNE.strainCost : Infinity;
      if (s && cost < best - 1e-4) { g = c; stand = s; best = cost; }
      if (best <= 1e-4) break;
    }
    if (!stand) return fail('走不到那里');
    const ok = this._walkTo(stand.pos, { yaw: stand.yaw, after: () => {
      const [ox, oz] = rotate(g.hand[0] * H, g.hand[2] * H, this.char.yaw);
      this._startAction(g.clip, {
        // 有交给物理的东西时过渡也按拿放的快慢 (只放慢片段不放慢过渡时, 端着满杯放下, 手在过渡里 0.3 秒往下甩 55 cm, 刚体的杯子
        // 跟着手, 洒掉大半; 不交给物理时液体的替身弹簧把杯子加速度限在 6 m/s², 过渡快慢影响不大, 保持原样)
        event: g.eventOffset, halflife: TUNE.blendAction / (Object.values(this.items).some((i) => i.physical) ? this._handSpeed() : 1),
        grab: { side, target: grip, hold, kind, slow: this._putSlow(kind) },
        warp: { x: point.x - (this.char.x + ox), z: point.z - (this.char.z + oz), yaw: 0 },
        onEvent, onDone: () => this._returnToLocomotion(() => done(null)),
      });
    } });
    return ok || fail('走不到那里');
  }

  /** 让"根坐标系里的偏移 offset (米)" 正好落在 point 上的站位: 试 16 个朝向, 取可走的; bump(站位) 给出插进家具的深度时
   *  先比深度 (浅的优先), 再比离角色多近. */
  _standFor(point, [ox, oz], bump = null) {
    let best = null;
    for (let k = 0; k < 16; k++) {
      const yaw = (k / 16) * Math.PI * 2;
      const [dx, dz] = rotate(ox, oz, yaw);
      const pos = [point.x - dx, point.z - dz];
      if (!walkable(this.grid, ...toCell(this.grid, ...pos))) continue;
      const d = Math.hypot(pos[0] - this.char.x, pos[1] - this.char.z);
      const b = bump ? bump({ pos, yaw }) : 0;
      if (!best || b < best.bump - 1e-4 || (Math.abs(b - best.bump) <= 1e-4 && d < best.d)) best = { pos, yaw, d, bump: b };
    }
    return best;
  }

  /** 站在 pos、面朝 yaw 时头 (中心离根往前 ahead 米, 站直的高度) 伸进家具多深 (米; 0 = 没碰). 家具按场景 JSON 的外框 (实心,
   *  和小屋体感层算"头撞到"的一样: 书架整个是实心的, 头伸进书架前面就算撞). 头的半径同体感层 (0.1 x 身材, 贴着 1.5 cm 内不算) 再留 2 cm.
   *  站着、走路时头在根前面 0.17-0.23 m, 伸手前倾时到 0.4 m (silvercat, 小屋诊断量的) */
  _headDepth(pos, yaw, ahead = HEAD_AHEAD * this.hipHeight / 0.78) {
    const s = this.hipHeight / 0.78;
    const p = [pos[0] + Math.sin(yaw) * ahead, this.headY, pos[1] + Math.cos(yaw) * ahead];
    return partsDepth(this._furnitureGroups(), p, 0.1 * s + 0.015 + 0.02);
  }

  /** 头会撞上的家具: 场景 JSON 的外框 (按物体分组) 里顶比头的中心低不到 0.25 m 的 (书架、挂墙的架子、落地灯). 矮的 (沙发、桌子、
   *  茶几) 不算: 外框是实心的, 弯腰往沙发上放东西时头在座面上方也落在外框里 (沙发外框连靠背高 0.85 m), 算进去的话拿放片段里
   *  把人往后推, wendy 放到沙发上碰家具 0.6 -> 24、失衡 2.3 -> 8. 小屋挪家具时换掉 o.box, 换了就重算. */
  _furnitureGroups() {
    const src = this.scene.objects.map((o) => o.box);
    if (!this._fgSrc || src.length !== this._fgSrc.length || src.some((b, i) => b !== this._fgSrc[i])) {
      this._fgSrc = src;
      this._fg = boxGroups(furnitureBoxes(this.scene.objects)).filter((g) => g.max[1] >= this.headY - 0.25);
    }
    return this._fg;
  }

  /** 走到 goal 以后面向 face (世界点) 站着, 头会伸进家具时往后退 (每次 5 cm, 最多 0.4 m, 得是能走的地方), 退到头碰不到为止.
   *  场景给的站位离书架前面只有 0.26 m: 站着看书架时头 (往前 0.18 m) 就伸进书架, 小屋里"在书架前挑书"站着也撞头. 都不行用原来的 */
  _headRoom(goal, face) {
    for (let d = 0; d <= 0.4 + 1e-6; d += 0.05) {
      const yaw0 = Math.atan2(face.x - goal[0], face.z - goal[1]);
      const pos = [goal[0] - Math.sin(yaw0) * d, goal[1] - Math.cos(yaw0) * d];
      if (d > 0 && !walkable(this.grid, ...toCell(this.grid, ...pos))) break;
      if (this._headDepth(pos, Math.atan2(face.x - pos[0], face.z - pos[1])) <= 0) return pos;
    }
    return goal;
  }

  /** 拿放选站位用的碰撞体 (见 TUNE.standParts): 房间里实际的家具部件, 或每件家具的整体外框 (由部件算, 没有房间时用场景 JSON). */
  _standBoxes() {
    const fine = this.avatar.collision?.();
    if (fine && TUNE.standParts) return fine;
    if (fine) return (this._outer ??= fine.map((g) => ({ ...g, parts: [{ part: 'solid', min: g.min, max: g.max }] })));
    return (this._boxGroups ??= boxGroups(furnitureBoxes(this.scene.objects)));
  }

  /** 片段本身的腿超出关节活动范围多少度 (建库时按源动捕量的髋屈、髋外展、膝屈最大值, 见 build_motion_db.py 的 leg_angles;
   *  和调姿层一样留 5 度). 没有这项数据的片段算 0. */
  _clipStrain(g) {
    const l = g.legs;
    if (!l) return 0;
    const over = (v, lim) => Math.max(0, v - lim - 5);
    return over(l.hipFlex, ROM.hip.flex) + over(l.hipAbd, ROM.hip.abd) + over(l.knee, ROM.knee.flex);
  }

  /** 片段 g 从站位 stand 开始播放时, 膝盖、脚尖 (片段里几个时刻, 见 build_motion_db.py 的 body_points) 插进家具的深度之和. */
  _clipBump(g, stand) {
    if (!g.body) return 0;
    const parts = this._standBoxes();
    const H = this.hipHeight;
    let pen = 0;
    // (试过取 12 个时刻、半径多留 2 cm: miomio 反而挑了更不合适的片段, wendy 没变化)
    for (const [kind, r] of [['knee', 0.045], ['toe', 0.025]]) {
      for (const [x, y, z] of g.body[kind] || []) {
        const [dx, dz] = rotate(x * H, z * H, stand.yaw);
        pen += partsDepth(parts, [stand.pos[0] + dx, y * H, stand.pos[1] + dz], r);
      }
    }
    // 头: 片段里没记头的位置 (建库时只记了膝盖、脚尖), 按站直时的头算 (片段开头、结尾都站着); 书架前拿放时站得太近头伸进书架
    return pen + this._headDepth(stand.pos, stand.yaw);
  }

  /** 转身: 有目标就面向它, 否则 L/R 各 90 度, B 转到背后. noTurn 为真时不转 (测试时固定朝向, 桌宠的动作自检用;
   *  看向、指向、伸手牵手时超出角度也只转头). */
  turn({ side = 'B', to = null } = {}) {
    if (this.noTurn) return;
    let yaw;
    if (to && this.targets[to]) {
      const p = this._lookPoint(this.targets[to]);
      yaw = Math.atan2(p.x - this.char.x, p.z - this.char.z);
    } else {
      yaw = this.char.yaw + (side === 'L' ? Math.PI / 2 : side === 'R' ? -Math.PI / 2 : Math.PI);
    }
    this.mode = 'turn';
    this.desired = { vx: 0, vz: 0, yaw: wrapAngle(yaw) };
    this.searchTimer = 0;
  }

  /** 看向目标: 胸/颈/头/眼分担; 超出头部能转的范围时身体也转过去. */
  lookAt(id, seconds = 4) {
    const t = this.targets[id];
    if (!t) return;
    const p = this._lookPoint(t);
    const rel = wrapAngle(Math.atan2(p.x - this.char.x, p.z - this.char.z) - this.char.yaw);
    if (Math.abs(rel) > (75 * Math.PI) / 180 && this.mode === 'idle') this.turn({ to: id });
    this.look = { ...this.look, target: id, until: performance.now() / 1000 + seconds };
  }

  /** 用手臂 IK 指向目标. */
  pointAt(side, id, seconds = 2.5) {
    if (!this.targets[id]) return;
    const p = this._lookPoint(this.targets[id]);
    const rel = wrapAngle(Math.atan2(p.x - this.char.x, p.z - this.char.z) - this.char.yaw);
    if (Math.abs(rel) > (70 * Math.PI) / 180 && this.mode === 'idle') this.turn({ to: id });
    this.pointing = { ...this.pointing, side: SIDE[side] || 'right', target: id, until: performance.now() / 1000 + seconds };
    this.lookAt(id, seconds + 0.5);
  }

  /** 伸手邀请牵手: 转向用户 (偏得多时先转身, 转过来再伸), 一只手伸到身前略低处、掌心朝上、手指半张, 停 seconds 秒
   *  再收回; 看着用户. 走动、转身时不伸 (转身转到一半就伸的话, 转完那一下肘会反折). */
  offerHand(side, seconds = 4) {
    const t = this.targets.user;
    let wait = 0;
    if (t && !this.seated) {
      const p = this._lookPoint(t);
      const rel = wrapAngle(Math.atan2(p.x - this.char.x, p.z - this.char.z) - this.char.yaw);
      if (Math.abs(rel) > (40 * Math.PI) / 180 && this.mode === 'idle') { this.turn({ to: 'user' }); wait = 0.5 + Math.abs(rel) * 0.6; }
    }
    this.offer = { ...this.offer, side: SIDE[side] || 'right', until: performance.now() / 1000 + seconds + wait };
    if (t) this.lookAt('user', seconds + 0.5);
  }

  stop() {
    this._graspCancel('抓握动作被停止');
    this._cancelCupPick();
    this._cancelCupPut();
    this.offer.until = 0;
    this.queue = [];
    this.gestureReq = null;
    if (this.idle?.kind === 'gesture') this._idleRandom(performance.now() / 1000);
    if (this.action) return;  // 坐着、正在拿放东西时不打断, 做完这一段自然停下
    this.mode = 'idle';
    this.path = null;
    this.desired = { vx: 0, vz: 0, yaw: this.char.yaw };
  }

  /**
   * 站着时用动捕做手势 (a.do: wave / shrug, 手势库里有的): 从待机过渡进来原地整段播放, 播完回到待机.
   * 手势库里没有、或者正在走动、坐着、做别的动作时返回 false, 由调用方用原来的方式 (HAST-SEC 或程序动作) 做.
   */
  gesture(a) {
    const groups = this.gestDB?.groups[a.do];
    if (!groups?.length || !this.idleDB || this.action || this.seated || this.reaching || this.mode !== 'idle' || this.queue.length
      || Math.hypot(this.sim.vx, this.sim.vz) > 0.05) return false;
    const side = a.do === 'wave' ? a.side || 'R' : null;
    const fit = groups.filter((g) => g.side === side);
    const g = pick(fit.length ? fit : groups);
    // 刚停下还没进待机 (站稳 0.4 秒) 时先记着, 进了待机马上开始; 1.5 秒内没开始就作废 (又走开了)
    this.gestureReq = { clip: g.clip, spd: clamp(a.spd || 1, 0.6, 1.5), plant: g.plant, until: performance.now() / 1000 + 1.5 };
    return true;
  }

  /** 情绪体态跟着对话情绪 (见 MOOD). 换情绪时马上重选手放哪 (交叠 / 叉腰 / 空着). */
  setMood(emotion) {
    const name = MOOD[emotion] ? emotion : 'neutral', now = performance.now() / 1000;
    const was = this._moodName(now);
    this.mood.name = name;
    this.mood.at = now;
    if (name === was) return;
    this.claspPlan.until = 0;
  }

  /** 此刻起作用的情绪: TUNE.mood 关着、或者 moodHold 秒没有新情绪时是平静. */
  _moodName(now) {
    return TUNE.mood && now - this.mood.at <= TUNE.moodHold ? this.mood.name : 'neutral';
  }

  async setStyle(emotion) {
    this.setMood(emotion);
    const style = STYLE_OF[emotion] || 'neutral';
    if (style === this.style) return;
    this.style = style;
    try {
      this.dbs[style] ??= await fetchDB(style);
    } catch (err) {
      // 这个风格库没生成 (比如还没重建动作库): 回到中性, 不要停在上一个情绪的步态上 (原来会一直用比如"生气"的走法)
      console.warn(err);
      if (this.style === style) {
        this.style = 'neutral';
        if (this.dbs.neutral && this.dbs.neutral !== this.db) this._pendingDB = this.dbs.neutral;
      }
      return;
    }
    if (this.style === style && this.dbs[style] !== this.db) this._pendingDB = this.dbs[style];
  }

  get active() { return this.mode !== 'idle' || this.weight > 0.01; }

  state() {
    const clip = this.action ? this.action.c : this.db.clips[this.db.clipOf[Math.floor(this.frame)]];
    return { mode: this.mode, style: this.style, weight: +this.weight.toFixed(3), x: +this.char.x.toFixed(3),
      z: +this.char.z.toFixed(3), yaw: +deg(this.char.yaw).toFixed(1), speed: +Math.hypot(this.sim.vx, this.sim.vz).toFixed(3),
      clip: clip?.name, frame: Math.floor(this.action ? this.action.frame : this.frame), goal: this.goal?.id || null,
      gesture: this.idle?.kind === 'gesture' ? this.idle.name : null,
      look: this.look.target, contacts: this.lastContacts, seated: !!this.seated, reaching: !!this.reaching,
      held: Object.fromEntries(Object.values(this.items).filter((i) => i.held).map((i) => [i.id, i.held])),
      stats: { ...this.stats } };
  }

  /** 给 LLM 的状态: 离哪个目标最近 (坐着时是座位), 姿势, 每样东西在谁手里/放在哪/原来在哪. */
  snapshot() {
    let at = null, best = 1.3;
    for (const o of this.scene.objects) {
      if (this.items[o.id] || !o.approach) continue;
      const spot = o.id === 'user' ? this._userSpot() : o.approach;
      const d = Math.hypot(spot[0] - this.char.x, spot[1] - this.char.z);
      if (d < best) { best = d; at = o.id; }
    }
    if (this.seated) at = Object.keys(this.targets).find((k) => this.targets[k].seat === this.seated.seat) || at;
    const posture = this.seated ? 'seated' : this.mode === 'go' ? 'walking' : 'standing';
    const items = Object.fromEntries(Object.values(this.items).map((i) => [i.id, { held: i.held, on: i.on, origin: i.origin?.on ?? null }]));
    return { at, posture, items };
  }

  _userSpot() {
    return this.targets.user.approach;
  }

  _lookPoint(t) {
    if (t.look === 'camera' || t.id === 'user') {
      const c = this.avatar.camera.position;
      return new THREE.Vector3(c.x, Math.min(c.y, 1.7), c.z);
    }
    if (this.items[t.id]) return this.items[t.id].mesh.getWorldPosition(new THREE.Vector3());
    return new THREE.Vector3(...(t.look || [t.approach[0], 1.2, t.approach[1]]));
  }

  // -- 每帧 ---------------------------------------------------------------------------------
  update(dt) {
    if (dt <= 0) return;
    const now = performance.now() / 1000;
    if (this.pending?.length) for (const fn of this.pending.splice(0)) fn();
    let s;
    if (this.action) {
      s = this._playAction(dt, now);
    } else {
      this._commands(dt);
      // 模拟骨
      this.sim = springVelocity(this.sim, this.desired.vx, this.desired.vz, TUNE.simVel, dt);
      this.simRot = springYaw(this.simRot, this.desired.yaw, TUNE.simRot, dt);
    }
    this._gait(dt);
    // 站定 0.4 秒后进入 StayStill 待机; 一开始走动/转身就交回运动匹配
    const still = !this.action && this.mode === 'idle' && Math.hypot(this.desired.vx, this.desired.vz) < 1e-3
      && Math.hypot(this.sim.vx, this.sim.vz) < 0.05 && Math.abs(this.simRot.w) < 0.2 && (this.idle || this.settle > 0.4);
    if (this.idleDB && still) {
      s = this._idleStep(dt, now);
      this._rootMotion([0, 0, 0], dt);  // 待机不移动, 只让模拟骨跟住角色
    } else if (!this.action) {
      if (this.idle) this._leaveIdle();
      // 匹配
      this.searchTimer -= dt;
      const clip = this.db.clips[this.db.clipOf[Math.floor(this.frame)]];
      const atEnd = this.frame + 1 >= clip.end - 1;
      if (this.searchTimer <= 0 || atEnd || this._pendingDB) {
        this.searchTimer = TUNE.searchEvery;
        this._match(atEnd);
      }
      // 播放 (走路时按步频加快)
      this.frame = Math.min(this.frame + dt * this.db.fps * (this.cadence || 1), this.db.clips[this.db.clipOf[Math.floor(this.frame)]].end - 1);
      s = samplePose(this.db, this.frame, this.pose);
      this._rootMotion(s.root, dt);
    }
    this.lastContacts = s.contacts;
    // 风格层的权重 (见 _style): 站着待机 (含换重心) 时收小动作, 走路时收手臂摆幅; 别的时候 (动作片段、手势、伸手、坐着) 不管
    this._dt = dt;
    // 上半身收多少 (_style 的 k): 待机、换重心 TUNE.calm, 动捕手势 TUNE.gestureCalm (挥手时手腕甩得没那么猛),
    // 坐着待机 TUNE.seatCalm (坐着那段是演员坐在凳子上的动捕, 手和头一直在动, 循环的接缝还会甩一下), 别的不收
    const quiet = !this.action && !this.seated && this.mode === 'idle' && !this.reaching && !this.pointing?.side && !this.offer?.side;
    const seatedQuiet = this.seated && this.mode === 'seated' && this.action?.loop && !this.reaching && !this.pointing?.side && !this.offer?.side;
    const calmGoal = quiet && (this.idle?.kind === 'idle' || this.idle?.kind === 'action') ? 1 : 0;
    const calmK = calmGoal ? TUNE.calm : quiet && this.idle?.kind === 'gesture' ? TUNE.gestureCalm : seatedQuiet ? TUNE.seatCalm : 0;
    const walkGoal = !this.action && this.mode === 'go' ? 1 : 0;
    this._calmW = (this._calmW || 0) + (calmGoal - (this._calmW || 0)) * damp(0.4, dt);
    this._calmK = (this._calmK || 0) + (calmK - (this._calmK || 0)) * damp(0.4, dt);
    this._walkW = (this._walkW || 0) + (walkGoal - (this._walkW || 0)) * damp(0.3, dt);
    for (let b = 0; b < this.db.B; b++) { const q = this.pose[b]; this.out[b][0] = q[0]; this.out[b][1] = q[1]; this.out[b][2] = q[2]; this.out[b][3] = q[3]; }
    this.rawHips = s.hips.slice();
    this.outHips = s.hips.slice();
    this.inert.update(dt);
    this.inert.apply(this.out, this.outHips);
    // 权重: 走动/转身/播放动作时接管, 停稳 0.5 秒后交回 HAST-SEC
    const moving = this.action || Math.hypot(this.sim.vx, this.sim.vz) > 0.05 || Math.abs(this.simRot.w) > 0.25 || this.mode !== 'idle';
    this.settle = moving ? 0 : this.settle + dt;
    let goal = !this.body ? 1 : moving || this.settle < 0.5 ? 1 : 0;
    // 待机库驱动全身; 说话时让给 HAST-SEC 的聊天手势 (动捕手势播放中除外)
    if (this.idle) goal = this.body?.speaking && this.idle.kind !== 'gesture' ? 0 : 1;
    this.weight += (goal - this.weight) * damp(goal > this.weight ? TUNE.weightIn : TUNE.weightOut, dt);
    // 运动仲裁表 (见 arbiter.js): 每一步写完记一笔. 动作来源盖在 HAST-SEC 上 (权重 weight), 影响按权重分给两边
    const arb = this.arbiter;
    if (this.weight > 0.002) {
      this._write();
      arb?.mark('base', { absolute: true, all: true, split: this.body ? { hast: 1 - this.weight } : null });
      this._balance(dt);
      arb?.mark('balance');
      this._feet(dt);
      arb?.mark('feet');
      this._grab();
      arb?.mark('grab');
    } else {
      arb?.mark(this.body ? 'hast' : 'base', { absolute: true, all: true });
    }
    this._applyRoot();
    this._reachLean(dt);
    this._carry(dt);
    arb?.mark('carry');
    this._lap(dt);
    arb?.mark('lap');
    this._point(dt, now);
    arb?.mark('point');
    this._offer(dt, now);
    arb?.mark('offer');
    this._reachStep(dt);
    arb?.mark('reach');
    this._poise(dt, now);
    this._clasp(dt, now);
    arb?.mark('poise');
    this._look(dt, now);
    arb?.mark('look');
    this._emote(dt, now);  // 在看向之后: 手按转过来以后的脸放 (捂嘴、托腮、捧脸)
    arb?.mark('gesture');
    this._handsDone();
    this._items();
  }

  /** 此刻的动作来源 (运动仲裁表用): 动作片段 / 待机库、小动作、手势库 / 运动匹配 (风格). */
  motionSource() {
    if (this.action) return `act:${this.action.c.name}`;
    if (this.idle) return `${this.idle.kind}:${this.idle.name}`;
    return `mm:${this.style || 'neutral'}`;
  }

  // -- 动作片段 -------------------------------------------------------------------------------
  /**
   * 开始播放动作库里的一段. warp: 到 warpUntil 帧为止要补到根部的位置/朝向误差 (均匀分摊);
   * event: 关键帧 (相对片段起点), 到了调用 onEvent; seat: 坐下时的髋高修正; grab: 关键帧前后的手臂 IK 目标.
   */
  _startAction(clipIdx, { event = null, onEvent = null, onDone = null, warp = null, warpUntil = null, loop = false,
    seat = null, grab = null, halflife = TUNE.blendAction, rise = false } = {}) {
    if (grab) grab.carryFrom = this._cupPutFrom(grab.side, grab.kind);
    const db = this.acts, c = db.clips[clipIdx];
    let prev = [], prevHips;
    const next = [];
    if (this.idle && !this.action) {  // 从待机开始的动作: 从此刻的待机姿态过渡
      prev = this.pose.map((q) => q.slice());
      prevHips = this.rawHips.slice();
      this.idle = null;
    } else {
      // 正在播的动作 (换一段坐姿)、刚播完的那段 (见 _playAction 的 _chain) 或运动匹配. 以前坐下播完接坐着待机时
      // 两者都不是, 从运动匹配库里早就过时的站姿过渡: 那一帧平滑层压跳变, 手臂乱甩, 髋部往上弹 (坐下的程度 0.97 -> 0.29)
      const from = this.action ?? this._chain;
      prevHips = samplePose(from ? db : this.db, from ? from.frame : this.frame, prev).hips;
    }
    const nextHips = samplePose(db, c.start, next).hips;
    this.inert.halflife = halflife;
    this.inert.transition(prev, next, prevHips, nextHips);
    const ev = event === null ? null : c.start + event;
    this._seatGap = 0;
    this.action = { c, frame: c.start, start: c.start, end: c.end - 1, event: ev, fired: false, onEvent, onDone, loop, seat, grab, rise,
      warp: warp || { x: 0, z: 0, yaw: 0 }, warpUntil: c.start + (warpUntil ?? (event ?? c.end - 1 - c.start)), progress: 0 };
    this.mode = 'act';
    this.path = null;
  }

  /** Careful non-rigid cup put starts at the displayed wrist, before the clip moves its root.
   *  This fixes the path origin only; endpoint, timing, joint/contact filters and physical items are unchanged. */
  _cupPutFrom(side, kind) {
    const item = kind === 'put' && Object.values(this.items).find(i => i.held === side);
    if (!item || item.grip !== 'cup' || item.physical || !(this.carryCare > 0) || !(TUNE.handoffPut > 0)) return null;
    const hand = this.nodes[side + 'Hand'];
    return hand ? this._world(hand).clone() : null;
  }

  /** 拿放的快慢 (TUNE.handSpeed, 行动层的 handSpeed 设了就用它), 0.4-1.5. */
  _handSpeed() { return clamp(this.handSpeed ?? TUNE.handSpeed, 0.4, 1.5); }

  /** 拿放片段、站着伸手开始时定下的放慢系数: 端着东西放下时按端得多稳 (carryCare) 放慢到 1 / (1 + care), 整段都这么慢
   *  (松手以后照旧会让片段一下快一倍). 端着从腰前送到矮茶几上 (60 cm) 只有到关键帧的 0.42 秒, 最小冲击曲线也要 2.7 m/s;
   *  端稳 (care 1) 时 0.83 秒、1.4 m/s. */
  _putSlow(kind) {
    const held = kind === 'put' ? Object.values(this.items).find((i) => i.held) : null;
    return held ? 1 / (1 + this._handCare(held)) : 1;
  }

  /** 拿起来送到身前、端着放下时按多稳来 (0-1): carryCare; 杯子 (grip 'cup', 装着东西) 至少按 1. 不端稳 (care 0) 时放下只有 0.42 秒
   *  从腰前送到茶几上, 刚体的杯子放下这段洒 234 ml (按 0.5 是 234 -> 还多, 按 1 是 44, 改之前 82); 拿起来送回身前 0.9 秒, 洒 50 ml (改之前 25). */
  _handCare(item) {
    return Math.max(clamp(this.carryCare ?? TUNE.carryCare, 0, 1), item?.grip === 'cup' ? 1 : 0);
  }

  _playAction(dt, now) {
    const a = this.action, db = this.acts, H = this.hipHeight;
    const spd = a.grab ? this._handSpeed() * (a.grab.slow ?? 1) : 1;  // 拿放的片段按拿放的快慢
    a.frame = Math.min(a.frame + dt * db.fps * spd, a.end);
    const cupTask = a.grab?.hold?.cupTask;
    if (cupTask && !cupTask.resolved && a.event !== null) a.frame = Math.min(a.frame, a.event);
    const s = samplePose(db, a.frame, this.pose);
    // 根部: 片段自己的位移 (按片段时间: 放慢播放时以前还按真实时间积分, 拿放快慢 0.6 时根部多走 1.7 倍、端稳放下时 3 倍,
    // 人从桌边退开, 放下那一刻手离落点 18 cm), 再把起点时预测的误差在关键帧之前均匀补上
    const rootDt = cupTask ? 0 : dt * spd; // cup preflight and execution share an in-place root
    const [dx, dz] = rotate(s.root[0] * H * rootDt, s.root[1] * H * rootDt, this.char.yaw);
    this.char.x += dx;
    this.char.z += dz;
    this.char.yaw = wrapAngle(this.char.yaw + s.root[2] * rootDt);
    const rawProgress = clamp((a.frame - a.start) / Math.max(1, a.warpUntil - a.start), 0, 1);
    const p = cupTask ? minJerk(rawProgress) : rawProgress, dp = p - a.progress;
    a.progress = p;
    this.char.x += a.warp.x * dp;
    this.char.z += a.warp.z * dp;
    this.char.yaw = wrapAngle(this.char.yaw + a.warp.yaw * dp);
    if (p >= 1) a.warped = true;
    // 拿放片段里头不伸进家具: 上一帧摆完以后头 (中心) 伸进家具多深 (_headIn, 见 _handsDone), 根就沿面朝的反方向往后让
    // (半衰期 0.08 秒追上, 让出去的不收回来). 书架前拿书: 片段自带往前走 0.13 m, 弯腰时头伸进书架 (小屋体感层"头撞到书架",
    // 站位选得再远也挡不住片段里这一段); 手臂 IK 照样去够, 够不着的那点手差一些
    if (a.grab && this._headIn > 0) {
      const k = this._headIn * damp(0.08, dt);
      this.char.x -= Math.sin(this.char.yaw) * k;
      this.char.z -= Math.cos(this.char.yaw) * k;
    }
    // 坐着待机: 片段的根位移 (演员在凳子上挪、转) 每圈净往前 3-8 cm、转到 22 度, 循环播放时一圈圈累加下去, 人从座位上滑出去
    // (床边坐 12 秒往前滑了 38 cm, 躺下以后底下的根 40 秒走了 3 m). 补完 warp 以后根按半衰期 TUNE.seatAnchor 拉回对准座位的那一点,
    // 片段里的小挪动还在
    if (a.anchor && a.warped && TUNE.seatAnchor > 0) {
      const k = damp(TUNE.seatAnchor, dt);
      this.char.x += (a.anchor.x - this.char.x) * k;
      this.char.z += (a.anchor.z - this.char.z) * k;
      this.char.yaw = wrapAngle(this.char.yaw + wrapAngle(a.anchor.yaw - this.char.yaw) * k);
    }
    // 站起来: 根部沿座位朝向往前追, 让片段里的脚踝回到踩着的落脚点上 (差多少见 _feet 的 _seatGap, 上一帧量的)
    if (a.rise && a.seat && TUNE.riseTau > 0 && this._seatGap > 0) {
      const k = this._seatGap * (1 - Math.exp(-dt / TUNE.riseTau)), yaw = a.seat.info.yaw;
      this.char.x += Math.sin(yaw) * k;
      this.char.z += Math.cos(yaw) * k;
    }
    this.sim = { x: this.char.x, z: this.char.z, vx: 0, vz: 0, ax: 0, az: 0 };
    this.simRot = { yaw: this.char.yaw, w: 0 };
    this.desired = { vx: 0, vz: 0, yaw: this.char.yaw };
    // 坐下的程度 (源片段髋高从站立降到坐姿) 决定加多少座面修正
    // 用过渡偏移之后的髋高 (切片段的那一帧原始髋高会突变), 再做时间平滑: 以前坐下 -> 坐着那一帧髋部跳了 30 cm
    const seatGoal = a.seat ? smooth((0.98 - (s.hips[1] + this.inert.pos[1])) / Math.max(0.05, 0.98 - a.seat.src)) : 0;
    this.seatAmount += (seatGoal - this.seatAmount) * damp(0.1, dt);
    this.hipsExtraY = a.seat ? a.seat.delta * this.seatAmount : 0;
    if (a.event !== null && !a.fired && a.frame >= a.event && !(a.grab && this.weight > 0.002) && !a.grab?.hold?.cupTask) { a.fired = true; a.onEvent?.(); }  // 拿放的在 _grab 里触发
    if (a.frame >= a.end) {
      if (a.loop) {
        const prev = this.pose.map((q) => q.slice()), next = [];
        const nextHips = samplePose(db, a.start, next).hips;
        this.inert.halflife = 0.35;
        this.inert.transition(prev, next, s.hips, nextHips);
        a.frame = a.start;
        // 补到座位上的误差第一圈已经补完: 以前这里没管, 下一帧 progress 从 1 掉回 0, 整个补偿撤回去再用 warpUntil 帧补一遍,
        // 坐着时每一圈 (3-10 秒) 朝向跳 0.1-0.2 rad、根前后跳 5-7 cm (床边坐着头的角速度到 150-275 度/秒)
        a.warp = { x: 0, z: 0, yaw: 0 };
      } else {
        const done = a.onDone;
        this._lastAction = a;
        this.action = null;
        this._chain = a;  // onDone 里直接接着开始的下一段 (坐下播完 -> 坐着待机) 从这一段的最后一帧过渡
        done?.();
        this._chain = null;
      }
    }
    // 坐着时隔一阵换一段坐姿
    if (this.seated && this.action?.loop && now > this.seated.next) {
      const others = this._sitGroups(this.seated.seat, false).filter((g) => g !== this.seated.group);
      this._seatedIdle(pick(others), this.seated.seat);
    }
    return s;
  }

  // -- StayStill 待机 ------------------------------------------------------------------------
  /** 换到待机库 (或手势库 db) 的某一段 (clip 下标, 起始帧), 惯性化过渡. kind: idle (一分钟待机)、action (小动作) 或 gesture (手势). */
  _idleSwitch(clip, frame, kind, now, halflife = TUNE.blendIdle, db = this.idleDB, { spd = 1, plant = false } = {}) {
    const c = db.clips[clip];
    if (this.pose.length === db.B) {  // 第一帧还没有姿态, 直接开始, 不用过渡
      const prev = this.pose.map((q) => q.slice()), next = [];
      const nextHips = samplePose(db, frame, next).hips;
      this.inert.halflife = halflife;
      this.inert.transition(prev, next, this.rawHips, nextHips);
    }
    const keep = this.idle;
    // 手势记下开始前在播的那段待机 (和播到哪一帧), 做完接着播, 不随机换一段: 挥完手站姿不变
    const resume = kind !== 'gesture' ? null : keep?.kind === 'idle' ? { clip: keep.clip, frame: keep.frame } : keep?.resume ?? null;
    this.idle = { db, clip, frame, end: c.end - 1, kind, spd: spd * (kind === 'gesture' ? TUNE.gestureSpeed : TUNE.idleSpeed), plant, resume, name: c.name,
      nextSwitch: kind === 'idle' ? now + 30 + Math.random() * 15 : keep?.nextSwitch ?? now + 30,
      nextAction: kind === 'idle' ? now + TUNE.fidgetGap * (1 + Math.random()) : now + 999 };
    this.idleLog.push(c.name);
    if (this.idleLog.length > 20) this.idleLog.shift();
  }

  /** 随机挑一段一分钟待机, 从中间某处开始 (避开片段两端 2 秒). */
  _idleRandom(now, exclude = -1, halflife = this.idle ? TUNE.blendIdleBack : 0.35) {
    const db = this.idleDB, all = db.groups.idle;  // 桌宠会从 groups.idle 里去掉手总压在身前的段
    const pool = db.calmOrder ? db.calmOrder.filter((i) => all.includes(i)).slice(0, Math.max(1, Math.round(TUNE.idlePool))) : all;
    const ids = pool.filter((i) => i !== exclude);
    const clip = pick(ids.length ? ids : pool), c = db.clips[clip];
    const margin = 2 * this.idleDB.fps;
    this._idleSwitch(clip, c.start + margin + Math.random() * Math.max(1, c.end - c.start - 2 * margin - 10 * this.idleDB.fps), 'idle', now,
      halflife);
  }

  _idleStep(dt, now) {
    const db = this.idleDB;
    const req = this.gestureReq;
    if (req) {
      this.gestureReq = null;
      if (now < req.until) this._idleSwitch(req.clip, this.gestDB.clips[req.clip].start, 'gesture', now, TUNE.blendGestureIn, this.gestDB, req);
    }
    if (!this.idle) this._idleRandom(now);
    const st = this.idle;
    st.frame = Math.min(st.frame + dt * st.db.fps * st.spd, st.end);
    const speaking = !!(this.body?.speaking || this.speaking);
    // 没有 HAST-SEC 时说话没有聊天手势: 待机中隔几秒插一段动捕说话手势 (做完再等 1.5-4.5 秒)
    const talk = !this.body && speaking && this.gestDB?.groups.talk?.length ? this.gestDB.groups.talk : null;
    // 程序动作的手势正叠在待机上 (没有 HAST-SEC 时, 见 avatar-vrm.js 的 _gestures): 不插小动作, 做完再缓 3 秒
    if (this.avatar.gesturing && st.kind === 'idle') st.nextAction = Math.max(st.nextAction, now + 3);
    const back = st.kind === 'gesture' && st.resume;
    const backFrame = back && back.frame + ((st.end - st.db.clips[st.clip].start) / (st.db.fps * st.spd)) * db.fps;
    if (st.frame >= st.end - 1 && back && backFrame < db.clips[back.clip].end - 2 * db.fps) {
      // 手势做完: 回到之前那段待机, 接着手势期间本该播到的位置; 手可能还举着 (单手挥手), 过渡快一点
      this._idleSwitch(back.clip, backFrame, 'idle', now, TUNE.blendGestureOut);
    } else if (st.frame >= st.end - 1) {
      // 播完 (小动作、手势做完) 换一段待机
      this._idleRandom(now, st.kind === 'idle' ? st.clip : -1, st.kind === 'gesture' ? TUNE.blendGestureOut : undefined);
    } else if (talk && st.kind === 'idle' && now > this.nextTalk && !this.avatar.gesturing && !this.pointing.side && !this.reaching
      && !this.emoteSt) {  // 做表情动作时 (含排着的) 不插: 说话手势点头、晃上身, 托腮、捧脸的手跟着脸往胸口收, 前臂插进胸
      const g = pick(talk), c = this.gestDB.clips[g.clip];
      this._idleSwitch(g.clip, c.start, 'gesture', now, TUNE.blendGestureIn, this.gestDB, g);
      this.nextTalk = now + (c.end - c.start) / this.gestDB.fps + 1.5 + Math.random() * 3;
    } else if (st.kind === 'idle' && now > st.nextAction && !speaking && !this.pointing.side && !this.reaching && !this.holdIdle) {
      // 不时插一个小动作: 现在只有左右换重心 (TUNE.fidgetsOnly; 关掉时还有环顾、摸脸、看表、挠头、伸懒腰)
      const kinds = Object.entries(db.groups.actions).filter(([k, v]) => v.length && (!TUNE.fidgetsOnly || FIDGETS.includes(k)));
      if (!kinds.length) {
        st.nextAction = now + 999;
      } else {
        const weights = kinds.map(([k]) => (k.startsWith('wei') ? 3 : k === 'st_arm' ? 0.5 : 1));
        let r = Math.random() * weights.reduce((a, b) => a + b, 0), i = 0;
        while (r > weights[i] && i < kinds.length - 1) r -= weights[i++];
        const clip = pick(kinds[i][1]);
        // 换重心时两只脚踩住不动 (TUNE.fidgetPlant), 只有骨盆左右挪: 动捕里换重心常顺带迈一步, 落脚放得很开, 重心跑出支撑面
        this._idleSwitch(clip, db.clips[clip].start, 'action', now, TUNE.blendIdle, db, { plant: !!TUNE.fidgetPlant });
      }
    } else if (st.kind === 'idle' && now > st.nextSwitch && !this.holdIdle) {  // holdIdle: 不换待机段、不插换重心 (测试时固定姿势)
      this._idleRandom(now, st.clip);
    }
    const s = samplePose(this.idle.db, this.idle.frame, this.pose);
    // 待机和手势都不移动根部. 原地的手势 (挥手、耸肩、说话) 两只脚一直踩着待机时的落点: 动捕演员做手势时常顺带挪半步,
    // 根部不动时这半步就成了脚在地上滑, 锁定点放开那一帧脚还会跳 (以前耸肩时重心偏出支撑面 14 cm)
    return { ...s, root: [0, 0, 0], contacts: this.idle.plant ? [1, 1] : s.contacts };
  }

  /** 开始走动/转身: 从此刻的待机姿态惯性化过渡到运动匹配的待机帧, 之后照常搜索. */
  _leaveIdle() {
    this.gestureReq = null;
    const prev = this.pose.map((q) => q.slice()), next = [];
    this.frame = this._idleFrame();
    const nextHips = samplePose(this.db, this.frame, next).hips;
    this.inert.halflife = TUNE.blendLeaveIdle;
    this.inert.transition(prev, next, this.rawHips, nextHips);
    this.idle = null;
    this.searchTimer = 0;
  }

  /** 动作播完, 交回运动匹配 (从待机帧开始, 惯性化过渡), 然后执行 after. */
  _returnToLocomotion(after = null) {
    const last = this._lastAction;
    const prev = [], next = [];
    const prevHips = samplePose(this.acts, last ? last.frame : 0, prev).hips;
    this.frame = this._idleFrame();
    const nextHips = samplePose(this.db, this.frame, next).hips;
    this.inert.halflife = TUNE.blendToLoco;
    this.inert.transition(prev, next, prevHips, nextHips);
    this.mode = 'idle';
    this.settle = 0;
    this.hipsExtraY = 0;
    this.seatAmount = 0;
    this.searchTimer = 0;
    if (after) this._defer(after);
  }

  /** 回调放到下一帧开头执行 (不在本帧的状态更新中途改状态, 也不依赖真实计时器). */
  _defer(fn) {
    (this.pending ??= []).push(fn);
  }

  /** 此刻的步态缩放: 走路时按风格的 stride/cadence, 跑步、待机、动作时回到 1 (权重 gaitW 平滑过渡). */
  _gait(dt) {
    const walking = !this.action && this.mode === 'go' && !this.goal?.run;
    this.gaitW = (this.gaitW || 0) + ((walking ? 1 : 0) - (this.gaitW || 0)) * damp(0.25, dt);
    const g = GAIT[this.style] || GAIT.neutral, w = this.gaitW;
    this.stride = 1 + (g.stride - 1) * w;
    this.cadence = 1 + (g.cadence - 1) * w;
  }

  _commands(dt) {
    const run = this.goal?.run;
    // 数据库的步速中位数包含转弯和起停, 直线行走取它的 1.3 倍更接近日常步速; 走路时再乘步态缩放 (小步)
    const gait = run ? 1 : (GAIT[this.style] || GAIT.neutral);
    const speed = run ? this.db.meta.speeds.FR * 1.15 * this.hipHeight * (this.speedScale || 1)
      : this.db.meta.speeds.FW * 1.3 * gait.stride * gait.cadence * this.hipHeight * (this.speedScale || 1);
    if (this.mode === 'go' && this.path) {
      const s = projectOnPath(this.path, [this.char.x, this.char.z]);
      const remain = this.pathLen - s;
      const ahead = pointOnPath(this.path, Math.min(this.pathLen, s + 0.6));
      let dx = ahead[0] - this.char.x, dz = ahead[1] - this.char.z;
      const len = Math.hypot(dx, dz) || 1;
      // 到达前减速 (匀减速到 0), 并提前转向目标
      const v = Math.min(speed, Math.sqrt(2 * 1.2 * Math.max(0, remain - 0.05)));
      dx /= len; dz /= len;
      this.desired.vx = dx * v;
      this.desired.vz = dz * v;
      let yaw = Math.atan2(dx, dz);
      const face = this.goal.face;
      if (face && remain < 0.8) {
        const fy = Math.atan2(face.x - this.char.x, face.z - this.char.z);
        const t = clamp(1 - remain / 0.8, 0, 1);
        yaw = yaw + wrapAngle(fy - yaw) * t;
      }
      this.desired.yaw = wrapAngle(yaw);
      // 卡住 (例如被挤在墙角) 超过 2 秒就当作到了, 不让角色原地踏步
      this.stuck = Math.hypot(this.char.x - (this.lastPos?.x ?? 0), this.char.z - (this.lastPos?.z ?? 0)) < 0.002 ? (this.stuck || 0) + dt : 0;
      this.lastPos = { x: this.char.x, z: this.char.z };
      if ((remain < 0.08 && Math.hypot(this.sim.vx, this.sim.vz) < 0.15) || this.stuck > 2) {
        this.stuck = 0;
        this.path = null;
        this.mode = 'face';
        const end = this.goal.face;
        this.desired = { vx: 0, vz: 0, yaw: end ? Math.atan2(end.x - this.char.x, end.z - this.char.z) : this.char.yaw };
        if (this.goal.id !== 'user' && this.goal.id !== 'center') this.lookAt(this.goal.id, 1.6);
      }
    } else if (this.mode === 'face' || this.mode === 'turn') {
      this.desired.vx = this.desired.vz = 0;
      if (Math.abs(wrapAngle(this.char.yaw - this.desired.yaw)) < 0.12 && Math.abs(this.simRot.w) < 0.3) {
        this.mode = 'idle';
        for (const fn of this.queue.splice(0)) this._defer(fn);
      }
    } else {
      this.desired.vx = this.desired.vz = 0;
    }
  }

  _match(force) {
    const db = this._pendingDB || this.db;
    // 步态缩放下, 数据库里 t 秒的轨迹对应现实 t / cadence 秒, 距离要除以 髋高 x stride 才是数据库单位
    const r = this.cadence || 1, sl = this.stride || 1;
    const fut = predict(this.sim, this.simRot, this.desired.vx, this.desired.vz, this.desired.yaw, FUTURE.map((t) => t / r), TUNE.simVel, TUNE.simRot);
    const cur = Math.min(Math.round(this.frame), this.db.clips[this.db.clipOf[Math.floor(this.frame)]].end - 1);
    buildQuery(db, cur, this.char, fut, this.hipHeight * sl, this.query, this.db);
    const t0 = performance.now();
    const best = search(db, this.query);
    this.stats.searches++;
    this.stats.searchMs = +(this.stats.searchMs * 0.9 + (performance.now() - t0) * 0.1).toFixed(3);
    if (best.frame < 0) return;
    const sameDB = db === this.db;
    const curClip = this.db.clipOf[cur];
    const near = sameDB && db.clipOf[best.frame] === curClip && Math.abs(best.frame - this.frame) < 4;
    const better = !sameDB || force || best.cost < frameCost(db, this.query, cur) - 0.01;
    if (near || !better) return;
    // 切换: 记下当前动画姿态, 新帧姿态, 让惯性化吸收差值
    const prev = this.pose.map((q) => q.slice()), prevHips = samplePose(this.db, this.frame, prev).hips;
    const next = [];
    const nextHips = samplePose(db, best.frame, next).hips;
    this.inert.halflife = TUNE.blendMatch;
    this.inert.transition(prev, next, prevHips, nextHips);
    this.db = db;
    this._pendingDB = null;
    this.frame = best.frame;
    this.stats.switches++;
  }

  /** 角色根: 按动画的根运动前进, 再向模拟骨靠拢 (半衰期 0.2 秒), 不让它偏离期望轨迹. */
  _rootMotion([vx, vz, yawRate], dt) {
    const r = this.cadence || 1, H = this.hipHeight * (this.stride || 1) * r, c = Math.cos(this.char.yaw), s = Math.sin(this.char.yaw);
    yawRate *= r;
    const wx = (c * vx + s * vz) * H, wz = (-s * vx + c * vz) * H;
    const moving = this.weight > 0.5;
    if (moving) {
      this.char.x += wx * dt;
      this.char.z += wz * dt;
      this.char.yaw = wrapAngle(this.char.yaw + yawRate * dt);
    }
    const k = damp(0.2, dt);
    this.char.x += (this.sim.x - this.char.x) * k;
    this.char.z += (this.sim.z - this.char.z) * k;
    this.char.yaw = wrapAngle(this.char.yaw + wrapAngle(this.simRot.yaw - this.char.yaw) * k);
    // 偏离模拟骨太远时拉回; 不让根部走出可行走区域
    const dx = this.char.x - this.sim.x, dz = this.char.z - this.sim.z, d = Math.hypot(dx, dz);
    if (d > 0.25) { this.char.x = this.sim.x + (dx / d) * 0.25; this.char.z = this.sim.z + (dz / d) * 0.25; }
    if (!walkable(this.grid, ...toCell(this.grid, this.char.x, this.char.z))) {
      // 例如刚从沙发上站起来: 平滑挪回可走区域, 不瞬移
      const p = nearestWalkable(this.grid, this.char.x, this.char.z);
      const kk = damp(0.15, dt);
      if (p) { this.char.x += (p[0] - this.char.x) * kk; this.char.z += (p[1] - this.char.z) * kk; this.sim.x = this.char.x; this.sim.z = this.char.z; }
    }
    if (!moving) { this.sim.x = this.char.x; this.sim.z = this.char.z; }  // 静止时让模拟骨跟住角色, 下次出发不弹
  }

  _applyRoot() {
    this.vrm.scene.position.x = this.char.x;
    this.vrm.scene.position.z = this.char.z;
    this.vrm.scene.rotation.y = this.baseYaw + this.char.yaw;
  }

  /** 把数据库姿态换算成这个模型的标准化骨骼局部旋转, 按权重叠到 HAST-SEC 的姿态上. */
  _write() {
    const bones = this.db.bones, w = this.weight;
    const world = {};
    const armFree = this._armFree();
    for (let b = 0; b < bones.length; b++) {
      const n = bones[b];
      if (!this.nodes[n]) continue;
      world[n] = qmul(this.out[b], this.align[n]);
    }
    for (const n of Object.keys(this.nodes)) {
      const parent = this.parentOf[n];
      let q = parent ? qmul(qinv(world[parent]), world[n]) : world[n];
      if (this.v0) q = [-q[0], q[1], -q[2], q[3]];
      let bw = w;
      if (ARM.left.includes(n)) bw *= armFree.left;
      else if (ARM.right.includes(n)) bw *= armFree.right;
      if (bw <= 0) continue;
      const node = this.nodes[n];
      this._q.set(q[0], q[1], q[2], q[3]);
      if (STYLE_BONES.has(n)) this._style(n, this._q);
      // 小步: 髋关节 (大腿) 的摆幅按步幅缩小; 标准化骨骼里单位四元数就是双腿直立
      if ((this.stride || 1) < 0.999 && (n === 'leftUpperLeg' || n === 'rightUpperLeg')) this._q.copy(IDENTITY_Q.clone().slerp(this._q, this.stride));
      node.quaternion.slerp(this._q, bw);
    }
    const H = this.hipHeight, [hx, hy, hz] = this.outHips;
    const target = this._v.set(hx * H, this.groundY + hy * H + this.hipsExtraY, hz * H);
    if (this.v0) target.set(-target.x, target.y, -target.z);
    this.nodes.hips.position.lerp(target, w);
  }

  /**
   * 风格 (q: 这根骨骼的局部旋转, 原地改). 每根上半身骨骼记一个慢变化的姿态 (时间常数 TUNE.calmTau), 待机时朝它收 TUNE.calm:
   * 动捕里的小抖动、晃头、手臂晃荡削掉, 慢的姿势变化 (换重心、换站姿) 保留. 走路时手臂朝它收 TUNE.walkArm (摆幅变小).
   * 垂着的上臂再向身体收一点 (_armIn).
   */
  _style(n, q) {
    const ema = (this._ema ??= {})[n] ??= q.clone();
    ema.slerp(q, 1 - Math.exp(-(this._dt || 1 / 60) / TUNE.calmTau));
    const arm = ARM.left.includes(n) || ARM.right.includes(n);
    const k = Math.max(this._calmK || 0, arm ? (this._walkW || 0) * TUNE.walkArm : 0);
    if (k > 1e-3) q.slerp(ema, k);
    if (n === 'leftUpperArm' || n === 'rightUpperArm') this._armIn(n, q);
  }

  /** 垂着的上臂向身体收: 外展角 (手臂偏离竖直向外的角度) 比防穿模量出的最小外展角 (contact.hang) 多 2 度以上的部分, 收掉 TUNE.armIn.
   *  站着待机和走路时. 抬起来的手臂 (挥手、叉腰、拿东西) 不管. */
  _armIn(n, q) {
    const w = Math.max(this._calmW || 0, this._walkW || 0) * TUNE.armIn;
    if (w < 1e-3) return;
    const left = n === 'leftUpperArm', s = left ? 1 : -1;
    const dir = (this._armDir ??= new THREE.Vector3()).set(s, 0, 0).applyQuaternion(q);  // 上臂零位沿 ±X (T 姿态); 父骨骼 (肩) 接近零位
    if (dir.y > -0.6) return;
    const out = deg(Math.atan2(s * dir.x, -dir.y));
    const floor = (this.avatar.contact?.hang?.[left ? 'left' : 'right'] ?? 10) + 2;
    const cut = Math.min(TUNE.armInMax, Math.max(0, out - floor) * w);
    if (cut < 0.05) return;
    q.premultiply((this._armQ ??= new THREE.Quaternion()).setFromAxisAngle(Z_AXIS, (-s * cut * Math.PI) / 180));
  }

  /** 落脚点往骨盆中线收 (p: 世界坐标, 原地改): 站着时到中线不超过半个髋宽的 TUNE.stance 倍, 走路时 TUNE.stepWidth 倍,
   *  超出的部分只留 TUNE.stanceSoft. 少女站姿两脚并得近、走路走成一条线; 交叉着的不管 (两腿防穿插管). */
  _narrow(p, side) {
    const standing = this.mode === 'idle' && !this.action && !this.seated && this.seatAmount < 0.05;
    const walking = this.mode === 'go' && !this.action;
    if (!standing && !walking) return p;
    const half = (this._hipHalf ??= Math.abs(this.rest.leftUpperLeg.x - this.rest.rightUpperLeg.x) / 2);
    const fwd = this._forward(), left = new THREE.Vector3(fwd.z, 0, -fwd.x), s = side === 'left' ? 1 : -1;
    // 站着时按"离另一只脚多远"量 (两脚间距不超过髋宽的 stance 倍): 换重心时骨盆偏到一只脚上, 按骨盆量的话另一只脚反而被放得更开
    const other = this.feet[side === 'left' ? 'right' : 'left'];
    const ref = standing && other.lock && other.w > 0.5 ? other.lock : this._world(this.nodes.hips);
    const out = s * p.clone().sub(ref).dot(left);
    const limit = ref === other.lock ? 2 * half * TUNE.stance : half * (walking ? TUNE.stepWidth : TUNE.stance);
    if (out <= limit) return p;
    return p.addScaledVector(left, -s * (out - limit) * (1 - TUNE.stanceSoft));
  }

  /** HAST-SEC 正在播放的手臂技能让出对应手臂: 返回每侧运动匹配可用的比例. */
  _armFree() {
    const free = { left: 1, right: 1 };
    const tracks = this.body?.state?.().tracks || [];
    for (const t of tracks) if (t.part !== 'leg' && free[t.side] !== undefined) free[t.side] = Math.min(free[t.side], 1 - t.weight);
    if (this.pointing.w > 0 && this.pointing.side) free[this.pointing.side] = 1;
    if (this.action?.grab) free[this.action.grab.side] = 1;  // 拿放动作的那只手不让给 HAST-SEC
    if (this.idle?.kind === 'gesture') free.left = free.right = 1;  // 动捕手势是全身的, 两只手都不让
    for (const side of ['left', 'right']) if (this.emoteUses(side)) free[side] = 1;  // 表情动作的手不让给 HAST-SEC
    return free;
  }

  _handPos(side) {
    const hand = this.nodes[`${side}Hand`];
    return hand ? this._world(hand) : new THREE.Vector3(this.char.x, 0.8, this.char.z);
  }

  /** 拿放片段的关键帧前后 (前 0.35 秒升到 1, 后 0.3 秒降回 0), 手臂 IK 把手腕对准目标. 有 hold (要拿的、要放下的东西) 时
   *  目标是"掌心持物点落在东西中心"的手腕位置 (以前是东西顶上 5 cm, 拿起、放下那一刻掌心离东西还差 10-13 cm, 东西是跳过去的).
   *  关键帧在 IK 之后触发 (拿起、放下按这一帧手真正到的位置算) */
  _grab() {
    const a = this.action;
    if (!a?.grab || a.event === null) return;
    const t = (a.frame - a.event) / this.acts.fps;
    const w = t < 0 ? smooth(1 + t / 0.35) : 1 - smooth(t / 0.3);
    const g = a.grab;
    // 端着放下、关键帧之前手由 _carry 摆 (按杯子的路径), 这里不摆: 这里的 IK 权重 0.35 秒升上来, 改了 _carry 起算的姿势,
    // 两骨 IK 沿用的弯曲平面跟着变, 肘换地方, 平滑层压跳变 (放下前十几帧手一帧挪 1-2 cm). 松手那一帧起由这里接着, 起算的姿势一样
    // 只对杯子 (grip 'cup', 端在腰前、掌心朝里, 和片段里的手臂差得远): 端着书、球放下时不摆这里, 手腕直线走到低处的台面会穿过大腿,
    // 接触层推开 12-87 mm (拿放场景放下这段 heldAcc 9.6 -> 13.2); 杯子放到茶几上这段 6.6 -> 1.6
    const carried = TUNE.handoffPut > 0 && g.kind === 'put' && a.frame < a.event && Object.values(this.items).some((i) => i.held === g.side && i.grip === 'cup');
    // 拿住以后手由 _carry 送到端着的位置 (_pickHand), 这里不再把手拉向东西 (东西已经跟着手走; 和站着伸手 _reachStep 一样)
    const handed = a.fired && this._pickHand?.[g.side];
    if (w > 0.001 && !carried && !handed) {
      const t = g.hold ? this._holdTarget(g.side, g.hold) : g.target;
      this._armTo(g.side, t, w);
      this._anchor(g.side, w);
    }
    if (!a.fired && a.frame >= a.event) {
      if (!g.hold?.cupTask) a.fired = true;
      a.onEvent?.();
    }
  }

  /** 拿放、伸手时肘的提示方向 (两骨 IK 只在手臂快伸直时用它). 世界坐标的 (0, -1, -0.3), 和以前一样; 试过按角色朝向 (朝下、略往后往外):
   *  拿放场景拿起来以后那段 heldAcc 9.7 -> 15.4、隔空拿放 0.25 -> 0.47, 撤了 */
  _grabHint() {
    return new THREE.Vector3(0, -1, -0.3);
  }

  /** 手臂 IK 把手腕往 target 挪 (按位置混权重 w; 两骨 IK, 手臂弯着时沿用片段里的弯曲平面). 试过换成按铰链弯、按关节角混 (_armHinged):
   *  端着杯子放到茶几上好了 (heldAcc 放下这段 10.4 -> 3.5), 但地上、矮处拿起来以后拿放 IK 0.3 秒退掉时两套姿势差得远, 手一扫而过
   *  (拿放场景 heldAcc 22 -> 51), 撤了 */
  _armTo(side, target, w, hint = null) {
    const [up, lo, hand] = ARM[side].slice(1).map((n) => this.nodes[n]);
    if (!up || !lo || !hand) return;
    this.vrm.scene.updateMatrixWorld(true);
    const wrist = this._world(hand);
    this._twoBone(up, lo, hand, wrist.clone().lerp(target, w), hint || this._grabHint(side), true);
  }

  /** 这只手这一帧在世界里定住的程度 w (拿放、伸手、端着放下、拿起来送回; 手臂 IK 之后调). 后调的盖掉先调的 (最后摆这只手的是谁就按谁).
   *  _look 按它让胸口少转: 以前低头看茶几时胸口在手臂 IK 之后转 0.12 弧度, 端着放到茶几上时掌心比放下的点低 3 cm (连 IK 差的一共 4.5 cm、偏 8 cm),
   *  交给物理的杯子先碰到台面、手接着往下按, 滑脱. */
  _anchor(side, w) {
    if (w < 0.001) { if (this._anchors) delete this._anchors[side]; return; }
    (this._anchors ??= {})[side] = w;
  }

  /** 这一帧的手都摆完了 (行动层最后): 量拿放离东西多远 (_grabGap), 清掉定住的程度; 量头伸进家具多深 (_headIn, 拿放片段里下一帧往后让). */
  _handsDone() {
    this._anchors = null;
    for (const g of this._gapLater?.splice(0) || []) this._grabGap(g.item, g.side, g.at, true);
    this._headIn = 0;
    const head = this.nodes.head;
    if (head && this.action?.grab) {
      // 头的中心: 头骨沿头的朝向往上 9 cm x 身材 (同小屋体感层); 半径同 _headDepth
      const s = this.hipHeight / 0.78, p = this._world(head).add(new THREE.Vector3(0, 0.09 * s, 0).applyQuaternion(head.getWorldQuaternion(this._q)));
      const groups = this.action.grab.hold?.cupTask ? (this.avatar.collision?.() || this._furnitureGroups()) : this._furnitureGroups();
      this._headIn = partsDepth(groups, p.toArray(), 0.1 * s + 0.015 + 0.02);
    }
  }

  /** 让掌心持物点落在 hold.center 的手腕位置 (见 _wristFor), 按 IK 解完以后手的朝向算: 先按此刻的朝向解一次, 再按解出来的朝向重算,
   *  手臂还原. 只按此刻的朝向算时, 端着放下、松手那一帧 (换成拿放的 IK、肘的提示方向不同, 手的朝向不同) 手腕跳 8 cm. */
  _holdTarget(side, hold, hint = null) {
    if (hold.graspTask) return palmAim(this, side, taskPalm(hold.graspTask)).wrist;
    const wristFor = () => this._wristFor(side, hold.center, hold.half, hold.cup);
    const nodes = ARM[side].slice(1).map((n) => this.nodes[n]);
    if (nodes.some((n) => !n)) return wristFor();
    const q0 = nodes.map((n) => n.quaternion.clone());
    this._armTo(side, wristFor(), 1, hint);
    const t = wristFor();
    nodes.forEach((n, i) => n.quaternion.copy(q0[i]));
    nodes[0].updateWorldMatrix(false, true);
    return t;
  }

  /** 站着伸手 (桌面高度): 1.6 秒时间线, 0.7 秒时手到位触发 onEvent, 胸部跟着前倾一点.
   *  hold: {center, half} 要拿的 / 要放下的东西 (见 _grab); kind: 'pick' / 'put' (端着放下时 _carry 把手从端着的位置送过去). */
  _reachStart(side, target, onEvent, done, { hold = null, kind = null } = {}) {
    if (this._graspTask && this._graspTask !== hold?.graspTask) this._graspCancel('抓握被另一次伸手打断');
    this.reaching = { side, target, t: 0, fired: false, onEvent, done, hold, kind, slow: this._putSlow(kind), carryFrom: this._cupPutFrom(side, kind) };
  }

  _reachStep(dt) {
    const r = this.reaching;
    if (!r || r.w === undefined) return;
    if (r.hold?.graspTask) {
      const task = r.hold.graspTask;
      if (!(r.fired && task.succeeded && task.kind === 'pick')) this._graspAim(r.side, taskPalm(task), r.w);
      if (!r.fired && r.t >= 0.7) task.ready = true;
      if (r.t >= 1.6 && r.fired) { this.reaching = null; r.done?.(); }
      return;
    }
    // 拿住以后手由 _carry 送到端着的位置 (_pickHand), 这里不再把手拉回拿的地方; 前倾照旧慢慢收回 (_reachLean)
    if (!(r.fired && this._pickHand?.[r.side])) {
      const t = r.hold ? this._holdTarget(r.side, r.hold) : r.target;
      this._armTo(r.side, t, r.w);
      this._anchor(r.side, r.w);
    }
    if (!r.fired && r.t >= 0.7) { r.fired = true; r.onEvent?.(); }  // 在 IK 之后: 拿起、放下按这一帧手真正到的位置算 (离东西多远在 _handsDone 量)
    if (r.t >= 1.6) { this.reaching = null; r.done?.(); }
  }

  /** 站着伸手的时间线和胸部前倾 (在 _carry 之前: 端着放下、拿起来送回时 _carry 摆好的手不再被前倾带着转走;
   *  以前前倾在摆手之后, 拿起来那几帧手被带开 18 cm, 平滑层几帧追上). 手臂 IK、触发在 _reachStep. */
  _reachLean(dt) {
    const r = this.reaching;
    if (!r) return;
    r.t += dt * this._handSpeed() * (r.slow ?? 1);
    if (r.hold?.graspTask && !r.fired) r.t = Math.min(r.t, 0.7); // hold contact while final filters converge
    const w = r.w = r.t < 0.7 ? smooth(r.t / 0.7) : r.t < 0.85 ? 1 : 1 - smooth((r.t - 0.85) / 0.75);
    // 手腕目标: 有 hold 时让掌心持物点落在东西中心 (见 _grab); 这里只用来定前倾
    const tgt = r.hold?.graspTask ? palmAim(this, r.side, taskPalm(r.hold.graspTask)).wrist
      : r.hold ? this._wristFor(r.side, r.hold.center, r.hold.half) : r.target;
    // 胸部前倾: 目标越低、离得越远倾得越多, 最多约 35 度. 肩到目标比手臂 (上臂 + 前臂) 的 0.97 还远时再往前倾到够得着, 也最多 35 度 (试过放到 55 度: 胸口倾到 82 度, 拿起来以后直起身时杯子跟着甩; 够不着的靠站位让肩正对着补)
    // (书桌上靠里放的东西: 以前肩到目标 0.58 m、手臂 0.45 m, 拿起那一刻手还差十几厘米, 东西是跳到手里的)
    const chest = this.nodes.chest || this.nodes.spine;
    if (chest) {
      const far = Math.hypot(r.target.x - this.char.x, r.target.z - this.char.z);
      let lean = clamp(clamp((1.15 - r.target.y) / 0.6, 0, 1) * 0.35 + clamp((far - 0.45) * 0.9, 0, 0.3), 0, 0.6);
      const left = new THREE.Vector3(Math.cos(this.char.yaw), 0, -Math.sin(this.char.yaw));
      this.vrm.scene.updateMatrixWorld(true);
      const [up, lo, hand] = ARM[r.side].slice(1).map((n) => this.nodes[n]);
      if (up && lo && hand) {
        const S = this._world(up), E = this._world(lo), C = this._world(chest), L = S.distanceTo(E) + E.distanceTo(this._world(hand));
        const gap = (ang) => S.clone().sub(C).applyAxisAngle(left, ang).add(C).distanceTo(tgt) - 0.97 * L;
        while (lean < 0.6 && gap(lean) > 0) lean += 0.05;
      }
      // 头不伸进家具: 前倾到头 (中心, 同 _headDepth 的半径) 快碰到家具就停, 二分找最多倾多少 (不按 0.05 一档减, 免得一帧跳一档).
      // 书架隔板里的书: 以前前倾时头往前到 0.4 m, 伸进书架 (小屋体感层记"头撞到书架"); 现在头留在外面, 够不着的那点手差一些
      const head = this.nodes.head;
      if (head && lean > 0) {
        const s = this.hipHeight / 0.78, C = this._world(chest);
        // Match the body sensor: the head center follows the head bone, including its current tilt.
        const Hd = this._world(head).add(new THREE.Vector3(0, 0.09 * s, 0)
          .applyQuaternion(head.getWorldQuaternion(new THREE.Quaternion())));
        const fg = this._furnitureGroups();
        const hit = (ang) => partsDepth(fg, Hd.clone().sub(C).applyAxisAngle(left, w * ang).add(C).toArray(), 0.1 * s + 0.015 + 0.02) > 0;
        if (hit(Math.min(lean, 0.6))) {
          let a = 0, b = Math.min(lean, 0.6);
          if (hit(0)) b = 0;
          else for (let i = 0; i < 10; i++) { const m = (a + b) / 2; if (hit(m)) b = m; else a = m; }
          lean = a;
        }
      }
      const rot = new THREE.Quaternion().setFromAxisAngle(left, w * Math.min(lean, 0.6));
      const parentWorld = chest.parent.getWorldQuaternion(new THREE.Quaternion());
      chest.quaternion.copy(parentWorld.invert().multiply(rot.multiply(chest.getWorldQuaternion(new THREE.Quaternion()))));
      chest.updateWorldMatrix(false, true);
    }
  }

  /** 这只手在做别的 (拿放、伸手、指向、拿着东西、坐着放在腿上): 接触层不给它加手臂重定向修正. */
  armsBusy() {
    const held = new Set(Object.values(this.items).map((i) => i.held).filter(Boolean));
    const busy = (side) => this.action?.grab?.side === side || this.reaching?.side === side
      || (this.pointing.side === side && this.pointing.w > 0.05) || held.has(side) || this.lapW[side] > 0.05
      || (this.offer.side === side && this.offer.w > 0.05) || (this.emoteUses(side) && this.emoteSt.w > 0.5);
    // 表情动作只在做满一半以上时算: 渐出的时候手臂经过胯部, 外展修正和裙子要照常管 (托腮放下时托肘的手穿过骨盆, 被一路推)
    // 腰前交叠 (_clasp) 不算: 交叠好的手不碰身体, 接触层本来就不动它; 算进去的话外展修正在抬手的半路撤掉, 肘插进腰侧
    // seated: 坐着、坐下站起时裙子铺在腿上, 不按"垂着的裙子"推手 (见 contact.js 的 _skirt)
    // squatting: 桌宠的蹲下 (desk-body.js 在行动层之后叠上去, 行动层不知道) 蹲下的程度; 蹲着时裙子也不是垂着的
    return { left: busy('left'), right: busy('right'), seated: !!this.seated || this.seatAmount > 0.05 || !!this.action?.seat || (this.squatting || 0) > 0.05,
      emote: { left: this.emoteUses('left'), right: this.emoteUses('right') } };  // 表情动作的手腕、指尖可以贴着下巴、脸 (contact.js)
  }

  /** 手部动作层 (hand.js) 要的情境, 每只手: open 张开 (伸手去拿之前、挥手的那只手、耸肩、说话手势), talk 在说话
   *  (手指随手臂的快慢张合, 见 hand.js), point 指向, lap 搭在腿上, offer 伸手邀请牵手, clasp 腰前交叠 (上面那只手包得多一点).
   *  握持由接触层的握紧程度决定. */
  handContext() {
    const g = this.idle?.kind === 'gesture' ? this.idle.name || '' : '';
    const wave = g.includes('wave') ? this.gestDB?.groups.wave?.find((x) => x.clip === this.idle.clip)?.side || 'B' : null;
    // 说话手势 (动捕的 talk) 也按手腕快慢张合 (以前固定张开 45%, 手一直摊着), 见 hand.js
    const talk = !!(this.body?.speaking || this.speaking || g.includes('talk')), hold = this.claspPlan.mode;  // 叉腰时手指并拢平贴着腰 (lap)
    const out = {};
    for (const side of ['left', 'right']) {
      const reach = (this.action?.grab?.side === side && !this.action.fired) || (this.reaching?.side === side && !this.reaching.fired);
      const gesture = wave ? (wave === 'B' || wave === side[0].toUpperCase() ? 1 : 0) : g.includes('shrug') ? 0.8 : 0;
      out[side] = { open: Math.max(reach ? 1 : 0, gesture), talk, point: this.pointing.side === side ? this.pointing.w : 0,
        lap: Math.max(this.lapW[side] * Math.min(1, this.seatAmount / 0.8), hold === 'akimbo' ? 0.9 * smooth(this.claspW) : 0),
        offer: this.offer.side === side ? this.offer.w : 0,
        clasp: hold === 'clasp' ? smooth(this.claspW) * (this.claspPlan.top === side ? 1 : 0.6) : 0 };
      if (this.emoteUses(side)) {
        // 表情动作的手形 (托腮时托肘的那只手是平手); 不随说话张合
        const st = this.emoteSt, shape = st.kind === 'chin' && st.side !== side ? 'flat' : EMOTES[st.kind].shape, w = smooth(st.w);
        Object.assign(out[side], { open: 0, talk: false, shapes: { [shape]: w } });
      }
    }
    return out;
  }

  /** 手里拿着的东西: 哪只手、物体中心 (世界坐标)、碰撞半径. 给接触层算手臂避让. */
  heldItems() {
    return Object.values(this.items).filter((i) => i.held).map((i) => ({
      side: i.held, center: i.mesh.position.clone(), radius: this.heldRadius(i),
      kind: i.mesh.geometry.type === 'SphereGeometry' ? 'ball' : 'book' }));
  }

  /** 拿着的东西在接触层里当多大的球 (米). 球按半径; 杯子 (grip 'cup') 按自己的大小 (3-5 cm): 当成 6 cm 的书时端在腰前会被推出肚子 10 cm,
   *  端稳就失效了 (小窝物理 P0 评测台). 小窝里带尺寸 (size) 的东西按最薄的那边的一半 (1-6 cm): 本子、手机平放在腿上时, 6 cm 的球
   *  怎么摆都插进大腿, 写日记时防穿模层一直推 (1.4-1.7 次/帧). 网页版的书没有尺寸, 还按 6 cm */
  heldRadius(i) {
    if (i.mesh.geometry.type === 'SphereGeometry') return i.half;
    if (i.grip === 'cup') return i.size ? clamp(Math.max(i.size.x, i.size.z) / 2, 0.03, 0.05) : 0.045;
    return i.size ? clamp(Math.min(i.size.x, i.size.y, i.size.z) / 2, 0.01, 0.06) : 0.06;
  }

  /** 拿着东西时的持物姿态: 球端在腰前 (掌心朝上托着), 书贴在身侧 (掌心朝里夹着).
   *  这只手在拿放、伸手、指向时让出来; 走路、说话时都保持. 不这样做的话手臂自然下垂, 大球会插进腿里. */
  _carry(dt) {
    const busy = (side) => this.action?.grab?.side === side || this.reaching?.side === side
      || (this.pointing.side === side && this.pointing.w > 0.05) || (this.action && !this.action.loop && !this.seated);
    // 端稳 (carryCare): 手的目标不跟着髋部一步一颠一晃, 跟着"角色根位置 + 髋部相对根的平滑偏移" (0.5 秒低通去掉每一步的颠和晃,
    // 蹲、弯腰这种慢的照样跟); 手臂 IK 把差的那点抵掉
    const care = clamp(this.carryCare ?? TUNE.carryCare, 0, 1);
    const hipsN = this.nodes.hips;
    let anchor = null;
    if (hipsN && Object.values(this.items).some((i) => i.held)) {
      this.vrm.scene.updateMatrixWorld(true);
      const hp = this._world(hipsN), root = new THREE.Vector3(this.char.x, 0, this.char.z);
      const off = hp.clone().sub(root);
      this._carryOff = this._carryOff ? this._carryOff.lerp(off, 1 - Math.exp(-dt / 0.5)) : off;
      anchor = care > 0 ? hp.lerp(root.add(this._carryOff), care) : hp;
      // 杯子的水平位置: 角色根 (char) 每一步前后冲一下 (水平加速度 3 m/s²), 走法的模拟轨迹 (sim) 平滑得多 (0.9);
      // 端稳时跟着 "sim + (char - sim) 的 0.5 秒低通", 身体慢慢偏开的那点照样跟
      const drift = new THREE.Vector3(this.char.x - this.sim.x, 0, this.char.z - this.sim.z);
      // 开始播动作片段时 sim 一帧被拉到 char (见 _playAction), char - sim 跳 2-3 cm: 低通跟着一起跳, 杯子不跟着跳
      // (以前端着放下那一帧掌心跳 2.4 cm)
      const prevRaw = this._carryDriftRaw;
      this._carryDriftRaw = drift.clone();
      if (this._carryDrift && prevRaw && drift.distanceTo(prevRaw) > 0.01) this._carryDrift.add(drift.clone().sub(prevRaw));
      this._carryDrift = this._carryDrift ? this._carryDrift.lerp(drift, 1 - Math.exp(-dt / 0.5)) : drift;
      // 朝向也平滑 (杯子在身前侧面 0.3 m 外, 每一步身体朝向左右摆一点, 杯子就跟着横甩)
      this._carryYaw = this._carryYaw === undefined || this._carryYaw === null ? this.char.yaw
        : this._carryYaw + wrapAngle(this.char.yaw - this._carryYaw) * (1 - Math.exp(-dt / 0.3));
    } else { this._carryOff = null; this._carryDrift = null; this._carryDriftRaw = null; this._carryYaw = null; }
    const cyaw = this._carryYaw === null || this._carryYaw === undefined ? this.char.yaw
      : this.char.yaw + wrapAngle(this._carryYaw - this.char.yaw) * care;
    const cupBase = this._carryDrift
      ? new THREE.Vector3(this.char.x, 0, this.char.z).lerp(new THREE.Vector3(this.sim.x, 0, this.sim.z).add(this._carryDrift), care) : null;
    this._carryTarget = care > 0 ? {} : null;  // 给 _carrySteady: 落地挪过骨盆以后再按这个目标解一次
    for (const side of ['left', 'right']) {
      const item = Object.values(this.items).find((i) => i.held === side);
      // 端着放下 (put 的拿放片段或站着伸手, 关键帧还没到): 不退出端姿, 手从端着的位置沿最小冲击曲线直接走到放下的点
      // (以前端姿先按指数退回待机姿势、片段再把手送下去: 起步那一帧掌心跳 5.5 cm (约 200 m/s²), 接着 0.18 秒往下掉 32 cm)
      const g = this.action?.grab, r = this.reaching;
      const put = item && TUNE.handoffPut > 0
        && (g?.side === side && g.kind === 'put' && !this.action.fired ? g : r?.side === side && r.kind === 'put' && !r.fired ? r : null);
      // 刚拿住 (pick 里记下拿住那一刻的手腕): 不先松开伸手、退回待机姿势再端起来, 手从拿住的地方沿最小冲击曲线直接送到端着的位置,
      // 用 (0.9 + 0.9 x care) / 拿放快慢 秒 (以前伸手按 0.75 秒放开、手 2.3 m/s 退回来, 端着满杯刚拿起就洒: 拿起后到起步这段 13 -> 41 ml)
      const hs = this._handSpeed();
      let ph = item ? this._pickHand?.[side] : null;
      if (this._pickHand?.[side] && !item) delete this._pickHand[side];
      if (ph) {
        ph.t += dt;
        ph.phase = ph.t / ((TUNE.handoffPick * (1 + this._handCare(item))) / hs);
        ph.u = minJerk(ph.phase);
        if (ph.u >= 1) { delete this._pickHand[side]; ph = null; }
      }
      const goal = item && (put || ph || !busy(side)) ? 1 : 0;
      // 权重按匀速的进度 u 的 S 形升降 (以前按指数逼近, 第一帧变化最大); 放下时也按拿放的快慢、端得多稳放慢
      this._carryWset ??= { left: 0, right: 0 };
      if (this.carryW[side] !== this._carryWset[side]) this.carryU[side] = this.carryW[side];  // 外面改过 (执行评估每个场景清零)
      const T = goal ? (0.54 + 1.5 * care) / hs : (0.4 + 0.4 * care) / hs;
      this.carryU[side] = ph ? 1 : clamp(this.carryU[side] + (goal ? dt : -dt) / T, 0, 1);  // 送的时候整个由这里摆
      this.carryW[side] = this._carryWset[side] = smooth(this.carryU[side]);
      const w = this.carryW[side];
      if (w < 0.005 || !item) continue;
      const [up, lo, hand] = ARM[side].slice(1).map((n) => this.nodes[n]);
      const hips = this.nodes.hips;
      if (!up || !lo || !hand || !hips || !anchor) continue;
      this.vrm.scene.updateMatrixWorld(true);
      const sgn = side === 'left' ? 1 : -1;
      const f = this._forward(), l = new THREE.Vector3(Math.cos(this.char.yaw), 0, -Math.sin(this.char.yaw)), y = new THREE.Vector3(0, 1, 0);
      const ball = item.mesh.geometry.type === 'SphereGeometry', cup = item.grip === 'cup' && TUNE.cupCarry > 0;
      const H = this.hipHeight, s = H / 0.78;
      // 杯子 (小窝的 grip 'cup'): 和小窝姿势层 (desktop/web/room-poses.js 的 _carry) 一样端在腰前, 小臂差不多平着, 掌心朝里;
      // 高度按地面算 (不跟着髋部颠). 球抱在身前, 书夹在身侧 (跟着髋部, 端稳时跟着平滑后的路径)
      const cf = new THREE.Vector3(Math.sin(cyaw), 0, Math.cos(cyaw)), cl = new THREE.Vector3(Math.cos(cyaw), 0, -Math.sin(cyaw));
      const target = cup
        ? cupBase.clone().addScaledVector(cl, sgn * 0.15 * s).addScaledVector(y, H * 1.18).addScaledVector(cf, 0.24 * s)
        : anchor.clone()
          .addScaledVector(y, ball ? 0.1 * H : -0.02 * H)
          .addScaledVector(f, ball ? 0.3 * H : 0.06 * H)
          .addScaledVector(l, sgn * (ball ? 0.16 * H : 0.24 * H));
      const hint = cup ? l.clone().multiplyScalar(sgn).addScaledVector(y, -0.8).addScaledVector(f, -0.3)
        : y.clone().multiplyScalar(-1).addScaledVector(f, -0.4).addScaledVector(l, sgn * 0.5);
      // 端着放下: 从端着的位置到"掌心持物点落在放下的点上"的手腕位置, 按片段 (或伸手) 到关键帧的进度走最小冲击曲线 (起止速度、加速度都是 0);
      // 手掌朝向的修正同时退掉, 关键帧那一刻是片段自己的朝向 (松手以后不用再转一下); 肘的提示方向也换成拿放的, 终点按换过去以后
      // 手的朝向算 (_holdTarget): 松手那一帧交给拿放的 IK 时手腕不跳. 拿起来送回身前反过来.
      // 试过按杯子 (掌心持物点) 规划路径、每帧挪手腕把持物点拉回路径上: 交给物理的杯子拿起来这段洒 22 -> 150 ml, 端稳走路洒 29 -> 66 ml, 撤了
      // 世界里定住的程度 (pin, 见 _anchor): 端着放下按进度升上去, 拿起来送回按进度退掉; 平常端着不定
      let ow = w, wt = target, hv = hint, pin = 0;
      if (put) {
        // 关键帧前 TUNE.handoffPut 秒开始走 (片段更短就整段), 之前端姿不动
        const a = this.action, fps = this.acts.fps;
        const pr = put === r ? clamp((r.t - Math.max(0, 0.7 - TUNE.handoffPut)) / Math.min(0.7, TUNE.handoffPut), 0, 1)
          : clamp((a.frame - Math.max(a.start, a.event - TUNE.handoffPut * fps)) / Math.max(1, Math.min(a.event - a.start, TUNE.handoffPut * fps)), 0, 1);
        const u = minJerk(pr);
        wt = (put.carryFrom || target).clone().lerp(put.hold ? this._holdTarget(side, put.hold) : put.target, u);
        hv = hint.clone().lerp(this._grabHint(side), u);
        ow = w * (1 - u);
        pin = w * u;
      } else if (ph) {
        wt = ph.from.clone().lerp(target, ph.u);
        hv = this._grabHint(side).lerp(hint, ph.u);
        ow = w * ph.u;
        pin = w * (1 - ph.u);
      }
      const wrist = this._world(hand);
      // The new in-place cup pickup has a measured final binding and reachable handoff.
      // Correct the non-commuting rotation order only during that stationary handoff.
      const exactPick = !!(ph && item.cupBinding && cupPickItem(item) && !put && !this.path
        && (this.mode === 'idle' || (this.mode === 'act' && this.action?.grab?.hold?.cupTask?.bound)));
      this._twoBone(up, lo, hand, wrist.clone().lerp(wt, w), hv, exactPick && !ph?.cupPose);
      // 端着放下: 定住的那部分再按准确的解法解一次 (两骨 IK 默认的解法端着放到茶几上时差 2.4 cm; 平常端着不换, 见 _twoBone 的 exact).
      // 拿起来送回不解: 地上拿起来以后手臂快伸直, 再解一次肘的平面晃, 拿放场景拿起来以后那段 heldAcc 4.6 -> 7.4
      if (put && pin > 0.001) this._twoBone(up, lo, hand, this._world(hand).lerp(wrist.clone().lerp(wt, w), pin), hv, true);
      // 端着放下时 _carrySteady 的修正随进度退掉 (fade): 松手那一帧它不再修, 以前手一帧从修过的位置跳回平滑层留下的位置 (14 cm)
      if (this._carryTarget) this._carryTarget[side] = { target: wrist.clone().lerp(wt, w), hint: hv, fade: ow / Math.max(w, 1e-6) };
      // 手掌朝向: 标准化骨骼零位掌心朝 -Y; 球: 掌心朝上, 书: 掌心朝身体, 杯子: 掌心朝里、略往后
      lo.updateWorldMatrix(true, true);
      const hq = hand.getWorldQuaternion(new THREE.Quaternion());
      const palm = new THREE.Vector3(0, -1, 0).applyQuaternion(hq);
      const want = ball ? y.clone().addScaledVector(l, -sgn * 0.35).normalize()
        : cup ? l.clone().multiplyScalar(-sgn).addScaledVector(f, -0.25).normalize() : l.clone().multiplyScalar(-sgn);
      const rot = new THREE.Quaternion().slerp(new THREE.Quaternion().setFromUnitVectors(palm, want), ow);
      const parent = hand.parent.getWorldQuaternion(new THREE.Quaternion());
      hand.quaternion.copy(parent.invert().multiply(rot.multiply(hq)));
      hand.updateWorldMatrix(false, true);
      if (exactPick && ph.cupPose) applyCupHandoff(this, side, ph, wrist.clone().lerp(wt, w), hv, want);
      this._anchor(side, pin);
    }
  }

  /** 手腕要到哪, 掌心持物点 (_palmHold) 才正好落在 center: 按这只手此刻的朝向反推 (持物点跟着手走, 和手腕的相对位置不变). */
  _wristFor(side, center, half, cup = null) {
    this.vrm.scene.updateMatrixWorld(true);
    const heldCenter = cup && cupPickItem(cup) ? liveCupCenter(this, cup, side) : this._palmHold({ half }, side);
    return this._handPos(side).add(center.clone().sub(heldCenter || this._palmHold({ half }, side)));
  }

  /** 端稳的最后一步 (avatar-vrm.js 在落地之后调): 接触层、平滑层、落地 (_ground 每帧上下挪骨盆让鞋底贴地) 都改过骨盆以后,
   *  端着东西的手臂再按 _carry 算的目标解一次 (手的朝向不变). 不然手跟着骨盆的落地修正一帧一颠: 端稳以后 _carry 里手腕的竖直加速度
   *  0.8 m/s², 一帧最后又回到 11.9 (和骨盆一样). 返回有没有改. */
  _carrySteady() {
    const T = this._carryTarget, care = clamp(this.carryCare ?? TUNE.carryCare, 0, 1);
    this._carryTarget = null;
    if (!T || care <= 0) return false;
    let changed = false;
    for (const [side, { target, hint, fade = 1 }] of Object.entries(T)) {
      const w = (this.carryW[side] || 0) * care * fade;
      if (w < 0.01 || !Object.values(this.items).some((i) => i.held === side)) continue;
      const [up, lo, hand] = ARM[side].slice(1).map((n) => this.nodes[n]);
      if (!up || !lo || !hand) continue;
      this.vrm.scene.updateMatrixWorld(true);
      // 别的层在摆这只手 (小窝姿势层把杯子送到嘴边喝, 它在 carryYield 里标出来), 不去抢; 手离目标 25 cm 以上也当成有人在摆 (15 cm 起渐弱).
      // (以前不看这个: 洒出来的热咖啡烫到手, 她本能地端稳以后, 每一口都被拉回腰前, 杯子在胸前倾过来洒了.
      //  先试过按 8 cm 判: 杯子当成 6 cm 的书被接触层推出肚子 10 cm, 端稳走路时 85% 的帧不修, 满杯洒 53 -> 87 ml)
      // 15-25 cm 之间渐变 (以前在 20 cm 硬切: 刚拿起、端的权重还在升时手离目标在 20 cm 上下来回, 一帧修一帧不修, 掌心目标一帧跳 3-4 cm,
      // 刚体的杯子跟着手就洒)
      const d = this._world(hand).distanceTo(target), k = w * clamp((0.25 - d) / 0.1, 0, 1);
      if (this.carryYield?.[side] || k < 0.01) continue;
      const hq = hand.getWorldQuaternion(new THREE.Quaternion());
      this._twoBone(up, lo, hand, this._world(hand).lerp(target, k), hint);
      lo.updateWorldMatrix(true, true);
      hand.quaternion.copy(hand.parent.getWorldQuaternion(new THREE.Quaternion()).invert().multiply(hq));
      hand.updateWorldMatrix(false, true);
      changed = true;
    }
    return changed;
  }

  /** 自动平衡 (髋策略): 站定或原地做动作 (拿放、伸手、转身、坐下前站着的那段) 时, 质心在地面的投影离支撑多边形边缘
   *  不到 MARGIN, 就把骨盆往支撑中心平移 (平滑), 脚由随后的脚部 IK 钉在原地.
   *  支撑多边形由着地的脚组成 (锁定着的, 或者脚踝贴着地面, 和执行评估同一个判定): 一只脚抬起来 (待机换重心、动作里迈步)
   *  时只按另一只脚. 双脚站着时最多挪 balanceMax, 单脚或做动作时最多 balanceMaxSingle. 走路 (动态平衡)、坐着时不介入.
   *  (以前只在待机、两只脚都锁定得很牢时才配平: 执行评估里"重心出支撑面"一半以上是单脚着地和动作片段里的) */
  _balance(dt) {
    const MARGIN = TUNE.balanceMargin;
    const still = this.mode !== 'go' && Math.hypot(this.sim.vx, this.sim.vz) < 0.05;
    const onSeat = this.seated || this.seatAmount > 0.05;
    const goal = this._balanceGoal ??= new THREE.Vector3();
    goal.set(0, 0, 0);
    if (still && !onSeat) {
      this.vrm.scene.updateMatrixWorld(true);
      const pos = {}, v = new THREE.Vector3();
      for (const n of ['hips', 'chest', 'head', 'leftUpperArm', 'leftLowerArm', 'leftHand', 'rightUpperArm', 'rightLowerArm', 'rightHand',
        'leftUpperLeg', 'leftLowerLeg', 'leftFoot', 'leftToes', 'rightUpperLeg', 'rightLowerLeg', 'rightFoot', 'rightToes']) {
        if (this.nodes[n]) pos[n] = this.nodes[n].getWorldPosition(v).toArray();
      }
      // 配平的目标默认还是"脚踝到脚尖"的长方形 (TUNE.soleSupport 0), 执行评估按鞋底量 (kinematics.js). 长方形大致是脚掌中段,
      // 往这里挪重心, 真人站着也是这样; 按鞋底着地部分配平的几种都更差 (silvercat 8 个种子, 见 logs/motion-tune-20261024.md):
      // 此刻的鞋底 (脚还斜着, 只剩一小条)、上一帧最终的鞋底 (正反馈: 配平挪髋把抬着的脚带高, 下一帧就不算着地)、往中心缩一圈.
      // soleSupport 1: 用此刻的鞋底, 锁定的脚平移到锁定点; 没锁定的脚和低的那只比, 差不到 2 cm 算着地
      const soles = this.sole && TUNE.soleSupport ? this._solePoints() : null;
      const lows = soles?.map((P) => Math.min(...P.map((p) => p[1])));
      // 预判迈步: 动作片段 / 待机库里接下来要抬起的脚现在就不算支撑, 重心提前挪到另一只脚上 (真人抬脚前先换重心).
      // 以前等脚抬起来才挪, 半衰期 0.13 秒追不上: 放东西、坐下时迈一步, 重心在两脚中间偏出支撑面 5-16 cm
      const soon = this._soon = this._liftSoon();
      const pts = [];
      let planted = 0;
      ['left', 'right'].forEach((s, i) => {
        const st = this.feet[s], foot = pos[`${s}Foot`], toe = pos[`${s}Toes`];
        if (!foot || !toe || soon[i]) return;
        if (st.lock && st.w > 0.5) {
          const dx = st.lock.x - foot[0], dz = st.lock.z - foot[2];
          pts.push(...(soles ? soleContact(soles[i]).map(([x, z]) => [x + dx, z + dz])
            : footPoints(st.lock.toArray(), [toe[0] + dx, 0, toe[2] + dz])));
          planted++;
        } else if (soles ? lows[i] < Math.min(...lows) + 0.02 : foot[1] < (this.ankle?.[s] ?? 0.1) + 0.025) {
          pts.push(...(soles ? soleContact(soles[i]) : footPoints(foot, toe)));
          planted++;
        }
      });
      const hull = convexHull(pts), com = centerOfMass(pos);
      if (com && hull.length >= 3) {
        const m = signedMargin(hull, [com[0], com[2]]);
        this.balanceMargin = m;
        if (m < MARGIN) {
          const cx = hull.reduce((a, p) => a + p[0], 0) / hull.length, cz = hull.reduce((a, p) => a + p[1], 0) / hull.length;
          const dx = cx - com[0], dz = cz - com[2], len = Math.hypot(dx, dz) || 1;
          const max = planted === 2 && !this.action ? TUNE.balanceMax : TUNE.balanceMaxSingle;
          const need = Math.min(max, MARGIN - m);
          goal.set((dx / len) * need, 0, (dz / len) * need);
        }
      }
    }
    // 偏移在世界坐标里平滑, 再换到骨盆父节点的局部坐标加到骨盆上
    const off = this.balanceOff ??= new THREE.Vector3();
    off.lerp(goal, damp(goal.lengthSq() > off.lengthSq() ? TUNE.balanceIn : TUNE.balanceOut, dt));
    if (off.lengthSq() < 1e-8) return;
    const hips = this.nodes.hips;
    const inv = hips.parent.getWorldQuaternion(new THREE.Quaternion()).invert();
    hips.position.add(off.clone().applyQuaternion(inv).multiplyScalar(this.weight));
    // 踝策略: 整个身体绕脚踝一起往同一边倾 (骨盆和上身跟着腿转同样的角度), 髋关节角度不变. 只平移骨盆的话脚钉着,
    // 大腿相对骨盆转了: 上限放到 10 cm 时挥手 (上身前倾) 髋后伸越界; 上限压到 4.2 cm 又不够, wendy 待机重心一直在外面
    const lean = Math.atan2(off.length(), this.hipHeight) * TUNE.balanceAnkle * this.weight;
    if (lean > 1e-4) {
      const axis = new THREE.Vector3(0, 1, 0).cross(off).normalize();
      const pq = hips.parent.getWorldQuaternion(new THREE.Quaternion());
      const world = pq.clone().multiply(hips.quaternion);
      world.premultiply(new THREE.Quaternion().setFromAxisAngle(axis, lean));
      hips.quaternion.copy(pq.invert().multiply(world));
    }
    hips.updateWorldMatrix(false, true);
    this.stats.balanceFrames = (this.stats.balanceFrames || 0) + 1;
  }

  /** 哪只脚在接下来 TUNE.balanceAhead 秒里要抬起 (正在播的动作片段或待机库的着地标记连续 3 帧以上不着地; 坐下、站起时不看).
   *  两只脚都要抬 (跳) 或者都不抬时都返回 false. */
  _liftSoon() {
    // 等着重新落脚的脚 (见 _feet 的 stepWait) 也算要抬的
    const out = [!!this.feet.left.stepWait, !!this.feet.right.stepWait];
    const src = this.action ? { db: this.acts, f: this.action.frame, end: this.action.end, spd: 1 }
      : this.idle ? { db: this.idle.db, f: this.idle.frame, end: this.idle.end, spd: this.idle.spd || 1 } : null;
    // 坐下、站起不按标记预判: 演员的脚挪到座位前沿时标成短暂离地, 这时人是往后坐向座位, 提前把重心挪到另一只脚反而
    // 出支撑面 (坐沙发 200 -> 249)
    if (TUNE.balanceAhead && src?.db.contacts && !this.action?.seat) {
      const f0 = Math.round(src.f), n = Math.round(TUNE.balanceAhead * src.db.fps * src.spd);
      for (let i = 0; i < 2; i++) {
        if (out[i] || !src.db.contacts[f0 * 2 + i]) continue;  // 此刻已经没着地: 不在这里管
        let off = 0;
        for (let k = 1; k <= n && f0 + k <= src.end; k++) {
          off = src.db.contacts[(f0 + k) * 2 + i] ? 0 : off + 1;
          if (off >= 3) { out[i] = true; break; }
        }
      }
    }
    if (out[0] && out[1]) out[0] = out[1] = false;
    return out;
  }

  /** 坐着时手放在大腿上: 坐姿片段是演员坐在凳子上录的, 手搭在膝盖附近, 套到大腿更粗的角色上会插进腿里,
   *  防穿模层每帧推开、下一帧又插回去. 这里在坐着、没在说话、手臂没被别的事占用时, 用两骨 IK 把手腕放到
   *  大腿上表面 (髋到膝 55% 处, 高出大腿半径一点, 稍往内收), 肘部朝外后方; 按坐下程度渐入渐出. */
  _lap(dt) {
    const free = this._armFree();
    const want = (this.seated && this.mode === 'seated') || (this.action?.seat && this.seatAmount > TUNE.lapFrom);
    for (const side of ['left', 'right']) {
      const busy = !want || this.body?.speaking || free[side] < 0.99 || this.action?.grab?.side === side
        || this.reaching?.side === side || (this.pointing.side === side && this.pointing.w > 0.05)
        || Object.values(this.items).some((i) => i.held === side) || this.emoteUses(side);
      this.lapW[side] += ((busy ? 0 : 1) - this.lapW[side]) * damp(busy ? TUNE.lapOut : TUNE.lapIn, dt);
      const w = this.lapW[side] * Math.min(1, this.seatAmount / TUNE.lapFull);
      if (w < 0.005) continue;
      const [up, lo, hand] = ARM[side].slice(1).map((n) => this.nodes[n]);
      const hip = this.nodes[`${side}UpperLeg`], knee = this.nodes[`${side}LowerLeg`];
      const other = this.nodes[`${side === 'left' ? 'right' : 'left'}UpperLeg`];
      if (!up || !lo || !hand || !hip || !knee || !other) continue;
      this.vrm.scene.updateMatrixWorld(true);
      const H = this.hipHeight, y = new THREE.Vector3(0, 1, 0);
      // 高度按防穿模层的阈值来 (大腿半径 + 手半径 + 留缝), 否则差几毫米也会被每帧推一下; 前臂往下斜时指尖更低, 再留一点
      // 前臂中段比手腕低, 手腕高度也要让前臂 (半径 rf) 架在腿上
      const r = this.thighRadius?.(side) ?? 0.07 * H / 0.8, rh = this.handRadius?.(side) ?? 0.025 * H / 0.8;
      const rf = this.foreArmRadius?.(side) ?? rh;
      const hp = this._world(hip), kp = this._world(knee), inward = this._world(other).sub(hp).setY(0).normalize();
      const target = hp.clone().lerp(kp, 0.5).addScaledVector(y, r + Math.max(rh * 1.6, rf * 1.3) + 0.012).addScaledVector(inward, 0.25 * r);
      const out = inward.clone().multiplyScalar(-1);
      const hint = out.addScaledVector(y, -0.8).addScaledVector(this._forward(), -0.15);  // 肘朝外下方, 少往后 (往后会插进后仰的胸口)
      const goal = this._world(hand).lerp(target, w), raw = goal.clone();
      // 放上去的路上不穿过大腿: 动捕的手按演员的细腿放 (坐下时手常在大腿里面), 从那儿直线挪到腿上面会一路陷在腿里,
      // 以前坐下时手腕、指尖插进大腿, 由防穿模层每帧硬推出来 (坐下场景手臂被推开 155). 按落点同样的离缝把手腕沿径向推到两条大腿外
      if (TUNE.lapClear) {
        for (const s2 of ['left', 'right']) {
          const a = this._world(this.nodes[`${s2}UpperLeg`]), b = this._world(this.nodes[`${s2}LowerLeg`]);
          const ab = b.clone().sub(a), t = clamp(goal.clone().sub(a).dot(ab) / Math.max(1e-6, ab.lengthSq()), 0, 1);
          const c = a.addScaledVector(ab, t), d = goal.clone().sub(c);
          const need = (this.thighRadius?.(s2) ?? 0.07 * H / 0.8) + Math.max(rh * 1.6, rf * 1.3) + 0.012;
          // 软投影 (离轴距离 len -> need + s*ln(1 + e^((len - need) / s))): 离得远不动, 越近推得越多, 不出拐点 (硬投影时冲击 26 -> 49)
          const len = d.length(), s = 0.015, out = need + s * Math.log1p(Math.exp((len - need) / s));
          if (len > 1e-6 && out > len + 1e-4) goal.copy(c.addScaledVector(d.normalize(), out));
        }
        // 推开量再做时间平滑: 坐下时大腿抬得快, 手直接贴着腿面走会被拖着甩 (前臂、手的冲击 26 -> 48); 剩下的一点由防穿模层推
        const off = (this._lapOff ??= { left: new THREE.Vector3(), right: new THREE.Vector3() })[side];
        off.lerp(goal.sub(raw), TUNE.lapClear >= 1 ? 1 : damp(TUNE.lapClear, dt));
        goal.copy(raw).add(off);
      }
      this._twoBone(up, lo, hand, goal, hint);
      // 手腕放平: 掌心朝下贴着腿 (标准化骨骼零位掌心朝 -Y). TUNE.lapAlign 时改成顺着大腿 (手指朝膝盖、略往里, 掌心贴大腿
      // 上表面, 手腕最多比前臂弯 50 度), 对比过不如放平, 见 TUNE
      lo.updateWorldMatrix(true, true);
      const hq = hand.getWorldQuaternion(new THREE.Quaternion());
      const palm = new THREE.Vector3(0, -1, 0).applyQuaternion(hq);
      const mid = this.vrm.humanoid.getNormalizedBoneNode(`${side}MiddleProximal`);
      let rot;
      if (TUNE.lapAlign && mid) {
        const hw = this._world(hand), fing = this._world(mid).sub(hw).normalize();
        const along = kp.clone().sub(hp).normalize().addScaledVector(inward, 0.3).normalize();
        const fore = hw.clone().sub(this._world(lo)).normalize(), bend = along.angleTo(fore), maxBend = (50 * Math.PI) / 180;
        if (bend > maxBend) along.lerp(fore, 1 - maxBend / bend).normalize();
        const down = y.clone().addScaledVector(along, -along.dot(y)).normalize().negate();  // 贴着大腿时掌心朝向
        const q1 = new THREE.Quaternion().setFromUnitVectors(fing, along);
        const p1 = palm.clone().applyQuaternion(q1);
        p1.addScaledVector(along, -p1.dot(along)).normalize();
        rot = new THREE.Quaternion().slerp(new THREE.Quaternion().setFromUnitVectors(p1, down).multiply(q1), w * 0.9);
      } else {
        rot = new THREE.Quaternion().slerp(new THREE.Quaternion().setFromUnitVectors(palm, y.clone().multiplyScalar(-1)), w * 0.8);
      }
      const parent = hand.parent.getWorldQuaternion(new THREE.Quaternion());
      hand.quaternion.copy(parent.invert().multiply(rot.multiply(hq)));
      hand.updateWorldMatrix(false, true);
    }
  }

  /** 仪态 (头颈): 动捕待机里脖子前倾 10-27 度 (探着头), 角色看着没精神、不优雅. 颈根到头的连线偏离竖直往前超过
   *  TUNE.headTiltMax 度的部分, 脖子绕角色的左右轴往后收 TUNE.headUp, 头再往前补回 3/4 (脸的朝向大致不变, 下巴略抬).
   *  走路、站着、坐着都加; 坐下 / 站起 / 拿放片段 (要低头看东西) 不加. 看向 (_look) 在这之后叠加.
   *  待机看着用户时头往一边歪 TUNE.headCock 度 (每次看过来随机选边). */
  _poise(dt, now) {
    const neck = this.nodes.neck, head = this.nodes.head;
    if (!neck || !head) return;
    const want = (TUNE.headUp > 0 || TUNE.mood) && !(this.action && !this.action.loop) && !this.reaching ? 1 : 0;
    this.poiseW += (want - this.poiseW) * damp(0.3, dt);
    const lk = this.look, atUser = lk.target === 'user' && now < lk.until;
    const calm = this.mode === 'idle' && !this.reaching && (!this.action || this.action.loop);
    // 情绪体态的参数朝当前情绪平滑过去 (0.8 秒半衰期), 体态慢慢变, 不跳
    const mood = this.mood.k, goal = MOOD[this._moodName(now)];
    for (const key of Object.keys(mood)) mood[key] += (goal[key] - mood[key]) * damp(0.8, dt);
    const hash = (x) => { const v = Math.sin(x * 12.9898) * 43758.5453; return v - Math.floor(v); };  // 不用 Math.random (评估按种子复现)
    // 开心、害羞时没看着人也时不时歪一下头: 每 9 秒一段, 六成的段里歪 4 秒, 每段随机选边
    const win = Math.floor(now / 9), idleCock = mood.cockIdle > 0.5 && calm && now - win * 9 < 4 && hash(win + 0.5) < 0.6;
    const cocking = atUser || idleCock, cockKey = atUser ? lk.target : idleCock ? `idle${win}` : null;
    if (cocking && cockKey !== this.cock.target) this.cock.side = hash(now) < 0.5 ? 1 : -1;
    this.cock.target = cockKey;
    const cockDeg = TUNE.mood ? mood.cock : TUNE.headCock;
    this.cock.w += ((cocking && calm && cockDeg > 0.5 ? 1 : 0) - this.cock.w) * damp(0.5, dt);
    const moodOn = TUNE.mood && (Math.abs(mood.head) + Math.abs(mood.chest) + Math.abs(mood.shoulder) > 0.05);
    if (this.poiseW < 0.005 && this.cock.w < 0.005 && !moodOn) return;
    this.vrm.scene.updateMatrixWorld(true);
    const { fwd, left } = this._bodyFrame();
    const turn = (node, axis, ang) => {
      const parentWorld = node.parent.getWorldQuaternion(new THREE.Quaternion());
      const rot = new THREE.Quaternion().setFromAxisAngle(axis, ang).multiply(node.getWorldQuaternion(new THREE.Quaternion()));
      node.quaternion.copy(parentWorld.invert().multiply(rot));
      node.updateWorldMatrix(false, true);
    };
    const d = this._world(head).sub(this._world(neck));
    const tilt = deg(Math.atan2(d.dot(fwd), d.y));
    const a = Math.max(0, tilt - TUNE.headTiltMax) * TUNE.headUp * this.poiseW * (Math.PI / 180);
    if (a > 1e-4) {
      turn(neck, left, -a);
      turn(head, left, 0.75 * a);
    }
    if (moodOn) {
      // 情绪: 胸往前含 / 挺起 (上胸), 头低下 / 抬下巴 (脖子四成、头六成), 肩往上收 / 塌下. 走路时头和胸只加一半;
      // 坐下 / 站起 / 拿放片段跟着 poiseW 渐出
      const k = this.poiseW * (this.mode === 'go' ? 0.5 : 1) * (Math.PI / 180);
      const chest = this.nodes.upperChest || this.nodes.chest;
      if (chest && Math.abs(mood.chest) > 0.05) turn(chest, left, mood.chest * k);
      if (Math.abs(mood.head) > 0.05) { turn(neck, left, 0.4 * mood.head * k); turn(head, left, 0.6 * mood.head * k); }
      if (Math.abs(mood.shoulder) > 0.05) {
        for (const [side, s] of [['left', 1], ['right', -1]]) {
          const sh = this.nodes[`${side}Shoulder`];
          if (sh) turn(sh, fwd, s * mood.shoulder * this.poiseW * (Math.PI / 180));
        }
      }
    }
    if (this.cock.w > 0.005) turn(head, fwd, this.cock.side * this.cock.w * cockDeg * (Math.PI / 180));
  }

  /** 仪态 (手): 站着待机时双手在腰前交叠 (clasp), 生气时叉腰 (akimbo). 动捕待机的手多半垂在胯前各晃各的, 不够文静.
   *  每 12-25 秒按情绪 (MOOD 的 clasp / akimbo; TUNE.mood 关着时按 TUNE.clasp) 定这一段手放哪; 换姿势时先放下再换.
   *  走动、坐下、说话、做手势 (动捕的和程序的)、拿着东西、指向、伸手时放开.
   *  交叠: 手腕放在腰前 (脊柱骨高度往上一点, 往前让出腰的厚度和一只手): 这身裙子在胯前张开 20 cm, 放在胯前会插进裙子.
   *  在上面的那只手再往前让出一个手的厚度. 肘朝外后下方; 手指朝里、略往下, 掌心朝身体.
   *  叉腰: 手腕放在腰两侧 (载入时量的腰半宽外面一点), 肘往两边张, 手指朝前下, 掌心贴腰. */
  _clasp(dt, now) {
    const P = this.claspPlan;
    const r = (k) => { const x = Math.sin(now * 12.9898 + k * 78.233) * 43758.5453; return x - Math.floor(x); };  // 同 _poise, 不用 Math.random
    if (now >= P.until || P.until - now > 30) {  // 后一个条件: 时钟往回跳了 (执行评估每个场景从头计时)
      // 跟着情绪 (害羞时一直交叠, 生气时叉腰, 开心时手多半空着); TUNE.clasp 为 0 时都不加
      const m = MOOD[this._moodName(now)], byMood = TUNE.mood && TUNE.clasp > 0;
      const pa = byMood ? m.akimbo : 0, pc = byMood ? m.clasp : TUNE.clasp, x = r(1);
      P.next = x < pa ? 'akimbo' : x < pa + pc ? 'clasp' : null;
      P.until = now + 12 + 13 * r(2);
      if (this.claspW < 0.01) P.top = r(3) < 0.5 ? 'left' : 'right';
    }
    if (P.mode !== P.next && this.claspW < 0.02) P.mode = P.next;  // 换姿势: 先放下再换
    P.on = !!P.mode;
    const free = this._armFree();
    const standing = this.mode === 'idle' && !this.action && !this.seated && this.seatAmount < 0.05 && this.settle > 0.6
      && this.idle && this.idle.kind !== 'gesture';
    const want = TUNE.clasp > 0 && P.mode && P.mode === P.next && standing && !this.body?.speaking && !this.speaking && !this.avatar.gesturing
      && !(this.emoteSt && this.emoteSt.start !== null)
      && free.left > 0.99 && free.right > 0.99 && !this.reaching && !(this.pointing.side && this.pointing.w > 0.05)
      && !(this.offer.side && this.offer.w > 0.05) && !Object.values(this.items).some((i) => i.held);
    this.claspW += ((want ? 1 : 0) - this.claspW) * damp(want ? TUNE.claspIn : TUNE.claspOut, dt);
    const w = smooth(this.claspW);
    if (w < 0.005) return;
    const spine = this.nodes.spine || this.nodes.hips;
    this.vrm.scene.updateMatrixWorld(true);
    const H = this.hipHeight, k = H / 0.9, y = new THREE.Vector3(0, 1, 0);
    const { fwd, left } = this._bodyFrame();
    const rh = this.handRadius?.('left') ?? 0.025 * k;
    const low = TUNE.mood ? this.mood.k.low : 0;  // 难过、害羞时交叠的手放低一点
    const akimbo = P.mode === 'akimbo';
    const C = this._world(spine).addScaledVector(y, (0.035 - low) * k).addScaledVector(fwd, this._waistFront() + 1.3 * rh);
    for (const side of ['left', 'right']) {
      const [up, lo, hand] = ARM[side].slice(1).map((n) => this.nodes[n]);
      if (!up || !lo || !hand) continue;
      const s = side === 'left' ? 1 : -1, top = P.top === side;
      const target = akimbo
        ? this._world(spine).addScaledVector(y, 0.04 * k).addScaledVector(left, s * (this._waistS + 0.9 * rh))
        : C.clone().addScaledVector(left, s * 0.05 * k).addScaledVector(fwd, top ? 1.8 * rh : 0).addScaledVector(y, top ? -0.01 * k : 0);
      const hint = akimbo ? left.clone().multiplyScalar(s).addScaledVector(fwd, -0.35).addScaledVector(y, 0.05)
        : left.clone().multiplyScalar(s * 0.7).addScaledVector(fwd, -0.4).addScaledVector(y, -0.6);
      // 先按交叠的姿势整个解出来, 最后按权重在关节角上混 (手沿身前的弧线抬起、放下). 以前混的是手腕的世界位置 (直线挪过去),
      // 抬到一半肘插进胸侧、放下时手腕穿过骨盆, 接触层一路推 (走路、手势场景手臂被推开 14 -> 49、1 -> 39)
      const q0 = [up, lo, hand].map((n) => n.quaternion.clone());
      this._twoBone(up, lo, hand, target, hint);
      // 肘转到提示方向 (绕肩-手腕轴转上臂, 手腕不动): 片段里手臂弯着时两骨 IK 沿用片段的弯曲平面, 肘可能朝里插进腰侧,
      // 片段一变还会翻过去 (一分钟待机里一个种子跳 36 次)
      const S = this._world(up), axis = this._world(hand).sub(S).normalize();
      const cur = this._world(lo).sub(S), want = hint.clone();
      cur.addScaledVector(axis, -cur.dot(axis));
      want.addScaledVector(axis, -want.dot(axis));
      if (cur.lengthSq() > 1e-8 && want.lengthSq() > 1e-8) {
        // 角度按轴算: 肘正好朝反方向时 setFromUnitVectors 随便挑一根转轴, 手腕跟着被甩开
        const ang = Math.atan2(cur.clone().cross(want).dot(axis), cur.dot(want));
        const sw = new THREE.Quaternion().setFromAxisAngle(axis, ang);
        const parentWorld = up.parent.getWorldQuaternion(new THREE.Quaternion());
        up.quaternion.copy(parentWorld.invert().multiply(sw.multiply(up.getWorldQuaternion(new THREE.Quaternion()))));
        up.updateWorldMatrix(false, true);
      }
      // 手指朝里、略往下 (再往下指尖就插进张开的裙子), 掌心朝身体; 手腕比前臂最多弯 55 度
      lo.updateWorldMatrix(true, true);
      const mid = this.vrm.humanoid.getNormalizedBoneNode(`${side}MiddleProximal`);
      const blend = () => {
        [up, lo, hand].forEach((n, i) => n.quaternion.copy(q0[i].slerp(n.quaternion, w)));
        up.updateWorldMatrix(false, true);
      };
      if (!mid) { blend(); continue; }
      // 手的朝向直接由 "手指朝 D、掌心朝 N" 两个方向定 (手的局部坐标里: 手腕 -> 中指根、掌心 -Y), 和片段里手原来怎么转的无关
      // (以前从当前朝向转过去, 两个方向正好相反时转轴随便挑, 片段一换手就甩一下)
      const hq = hand.getWorldQuaternion(new THREE.Quaternion());
      const hw = this._world(hand);
      // 叉腰: 手指朝前下、略往里贴着腰, 掌心朝腰 (手腕弯得多, 最多 70 度)
      const D = akimbo ? fwd.clone().multiplyScalar(0.7).addScaledVector(y, -0.6).addScaledVector(left, -s * 0.15).normalize()
        : left.clone().multiplyScalar(-s * 0.8).addScaledVector(y, -0.3).addScaledVector(fwd, 0.05).normalize();
      const fore = hw.clone().sub(this._world(lo)).normalize(), bend = D.angleTo(fore), maxBend = ((akimbo ? 70 : 55) * Math.PI) / 180;
      if (bend > maxBend) D.lerp(fore, 1 - maxBend / bend).normalize();
      const toBody = akimbo ? left.clone().multiplyScalar(-s) : fwd.clone().negate();
      const N = toBody.addScaledVector(D, -toBody.dot(D)).normalize();
      const inv = hq.clone().invert();
      const fl = this._world(mid).sub(hw).applyQuaternion(inv).normalize();
      const pl = new THREE.Vector3(0, -1, 0).addScaledVector(fl, fl.y).normalize();  // 掌心方向去掉沿手指的分量
      const basis = (a, b) => new THREE.Matrix4().makeBasis(a, b, a.clone().cross(b));
      const rot = new THREE.Quaternion().setFromRotationMatrix(basis(D, N).multiply(basis(fl, pl).transpose()));
      const parent = hand.parent.getWorldQuaternion(new THREE.Quaternion());
      hand.quaternion.copy(parent.invert().multiply(rot));
      blend();
    }
  }

  /** 表情动作 (EMOTES): 比心、捂嘴笑、托腮、拍手、比耶、捧脸. 走动、转身、坐下站起、拿放时先等着 (最多 8 秒);
   *  正在做别的表情动作时排在它后面. 做的时候看着用户. 返回是否接下. */
  emote(kind, { side = 'R', amp = 1, spd = 1 } = {}) {
    const def = EMOTES[kind];
    if (!def) return false;
    const req = { kind, side: SIDE[side] || 'right', amp: clamp(amp ?? 1, 0.3, 1), dur: def.dur / clamp(spd ?? 1, 0.5, 2),
      asked: performance.now() / 1000, start: null, w: 0 };
    if (this.emoteSt && (this.emoteSt.start === null || this.emoteSt.w > 0.02)) this.emoteNext = req;
    else this.emoteSt = req;
    return true;
  }

  /** 这只手在做表情动作 (接触层、程序手势、交叠、手放腿上都让出这只手). */
  emoteUses(side) {
    const st = this.emoteSt;
    return !!st && st.start !== null && st.w > 0.02 && (EMOTES[st.kind].hands === 'both' || st.side === side);
  }

  /** 表情动作: 手臂 IK 到脸旁 / 胸前 (位置按载入时量的脸、胸尺寸, 见 _faceGeom), 手的朝向由 "手指朝哪、掌心朝哪" 定,
   *  手形交给手部动作层 (handContext 的 shapes). 先歪头 / 低头, 再按转过的头放手. 按权重渐入渐出 (0.12 / 0.2 秒).
   *  捂嘴笑: 一只手指尖朝上、掌心朝脸挡在嘴前, 低头歪头, 肩膀一抖一抖; 托腮: 一只手托着下巴, 另一只手横在腰前托着肘, 头歪向托腮的手;
   *  比耶: 一只手在脸旁比 V, 掌心朝前; 捧脸: 两只手捧着两颊, 头左右轻轻晃, 缩肩; 比心: 两只手在胸前比心;
   *  拍手: 两只手在胸前, 掌心相对, 每秒拍 2.8 下. */
  _emote(dt, now) {
    const st = this.emoteSt;
    if (!st) return;
    const blocked = this.mode === 'go' || this.mode === 'turn' || (this.action && !this.action.loop) || !!this.reaching;
    if (st.start === null) {
      if (blocked) { if (now - st.asked > 8) { this.emoteSt = this.emoteNext; this.emoteNext = null; } return; }
      st.start = now;
      // 看着用户, 但只转头不转身 (lookAt 在用户偏出 75 度时会原地转身: 桌宠自检里拍手、比耶时脚挪了 21-23 cm)
      if (this.targets.user) this.look = { ...this.look, target: 'user', until: now + st.dur + 0.6 };
    }
    const t = now - st.start, on = t < st.dur && !blocked;
    st.w += ((on ? 1 : 0) - st.w) * damp(on ? 0.18 : 0.25, dt);  // 0.12 / 0.2 时手甩上去太快 (表情动作场景冲击 36)
    if (!on && st.w < 0.02) { this.emoteSt = this.emoteNext; this.emoteNext = null; return; }
    const G = this._face, headN = this.nodes.head, chestN = this.nodes.upperChest || this.nodes.chest;
    if (!G || !headN || !chestN) return;
    const w = smooth(st.w), a = st.amp, sl = st.side === 'left' ? 1 : -1, RAD = Math.PI / 180;
    this.vrm.scene.updateMatrixWorld(true);
    const turn = (node, axis, ang) => {
      const parentWorld = node.parent.getWorldQuaternion(new THREE.Quaternion());
      const rot = new THREE.Quaternion().setFromAxisAngle(axis, ang).multiply(node.getWorldQuaternion(new THREE.Quaternion()));
      node.quaternion.copy(parentWorld.invert().multiply(rot));
      node.updateWorldMatrix(false, true);
    };
    const frame = () => {
      const q = headN.getWorldQuaternion(new THREE.Quaternion());
      return { F: G.fwdL.clone().applyQuaternion(q), U: G.upL.clone().applyQuaternion(q), L: G.leftL.clone().applyQuaternion(q) };
    };
    // 头: 低头 (度) 和往手那边歪 (度; 绕脸朝前的轴转负角是往角色左边歪); 肩: 往上收 (度)
    const hold = clamp((t - 0.3) / 0.3, 0, 1) * (t < st.dur ? 1 : 0);
    const [pitch, roll, shrug] = {
      giggle: [7, 6 * sl, 2.5 * Math.abs(Math.sin(2 * Math.PI * 4.5 * t)) * hold],
      chin: [3, 9 * sl, 0],
      peace: [0, 7 * sl, 0],
      cheeks: [3, 5 * Math.sin(2 * Math.PI * 0.6 * t) * hold, 3],
      heart: [0, 5 * sl, 0],
      clap: [0, 0, 0],
    }[st.kind];
    let f0 = frame();
    if (pitch) turn(headN, f0.L, pitch * a * w * RAD);
    if (roll) turn(headN, f0.F, -roll * a * w * RAD);
    for (const [side, s] of [['left', 1], ['right', -1]]) {
      const sh = this.nodes[`${side}Shoulder`];
      if (sh && shrug) turn(sh, f0.F, s * shrug * a * w * RAD);
    }
    if (st.kind === 'chin' || st.kind === 'cheeks') {
      // 手托着下巴时脸抬着: 头相对上胸最多低到 TUNE.chinLift / cheeksLift (负的是仰着). 头一低下巴往胸口收, 托下巴的前臂贴着胸前往上插进胸,
      // 接触层一直推 (说话时插进来的说话手势点头、idle_07 这种低着头的待机段: 托腮推开 200-300 次). 脖子、头各抬一半
      const cq = chestN.getWorldQuaternion(new THREE.Quaternion());
      const cf = new THREE.Vector3(0, 0, 1).applyQuaternion(cq), cu = new THREE.Vector3(0, 1, 0).applyQuaternion(cq);
      const F1 = frame().F, down = Math.atan2(-F1.dot(cu), F1.dot(cf)) / RAD, lift = Math.max(0, down - (st.kind === 'chin' ? TUNE.chinLift : TUNE.cheeksLift)) * w;
      if (lift > 0.01) {
        if (this.nodes.neck) turn(this.nodes.neck, frame().L, -0.5 * lift * RAD);
        turn(headN, frame().L, -(this.nodes.neck ? 0.5 : 1) * lift * RAD);
      }
    }
    const { F, U, L } = frame(), Hd = this._world(headN), C = this._world(chestN);
    const { fwd, left } = this._bodyFrame(), y = new THREE.Vector3(0, 1, 0);
    const P = (f, u, l) => Hd.clone().addScaledVector(F, f).addScaledVector(U, u).addScaledVector(L, l);
    const rh = this.handRadius?.('left') ?? 0.02;
    const dir = (...parts) => parts.reduce((acc, [v, k]) => acc.addScaledVector(v, k), new THREE.Vector3()).normalize();
    // 每只手: [手腕目标, 肘的提示方向, 手指方向 D, 掌心朝向 N]. 手在脸旁的动作肘朝前下、往外: 肘垂直往下时前臂贴着胸前往上,
    // 插进胸 (托腮、捧脸手臂插进身体 11 / 25), 肘也弯过了头 (捧脸右肘越界 177)
    const plan = {};
    const other = st.side === 'left' ? 'right' : 'left';
    if (st.kind === 'giggle') {
      plan[st.side] = [P(G.front + 1.4 * rh, G.mouth - 0.06, sl * 0.045), dir([y, -0.65], [left, sl * 0.5], [fwd, 0.35]),
        dir([U, 0.6], [L, -sl * 0.75], [F, 0.05]), F.clone().negate()];
    } else if (st.kind === 'peace') {
      plan[st.side] = [P(0.04, G.nose - 0.01, sl * (G.cheek + 0.075)), dir([y, -0.8], [left, sl * 0.5], [fwd, 0.1]),
        dir([U, 0.95], [L, sl * 0.25]), F.clone()];
    } else if (st.kind === 'chin') {
      // 掌心朝上托着下巴: 手腕在下巴下面、靠手那边, 手指朝另一边、往前 (手形 clasp, 弯着的手指兜住下巴).
      // 肘朝前 (提示里往前 1.3): 往前 0.55 时肘低、贴着身体, 前臂顺着胸前往上, 中段插进胸 1-2 cm (按网格量; 桌宠镜头低、头低时最明显)
      plan[st.side] = [P(G.front - 0.05, G.chin - 0.035, sl * 0.04), dir([y, -0.6], [left, sl * 0.3], [fwd, 1.3]),
        dir([L, -sl * 0.7], [F, 0.5], [U, 0.35]), U.clone()];
    } else if (st.kind === 'cheeks') {
      // 双手捧着脸: 两只手掌心朝上从两边兜着下巴 (托腮的两只手版), 手指朝前、往中间. 试过四版贴脸颊的: 手指往里斜 -> 两只手在脸前
      // 交叉; 手腕在下颌角、手指朝太阳穴 -> 像抱着头; 手腕在下巴高度或更低、手指往上 -> 正面看像一只手捂着嘴.
      // 肘朝前 (提示里往前 1.3, 和托腮一样): 往前 0.6 时前臂斜着贴过胸口、往脖子上走, 头一低就插进胸
      for (const [side, s] of [['left', 1], ['right', -1]]) {
        plan[side] = [P(G.front - 0.05, G.chin - 0.035, s * 0.05), dir([y, -0.55], [left, s * 0.5], [fwd, 1.3]),
          dir([L, -s * 0.6], [F, 0.6], [U, 0.35]), U.clone()];
      }
    } else if (st.kind === 'heart') {
      // 胸前白色的衣身前面 (放在领口那么高时手和露出来的皮肤一个颜色, 看不出来)
      const c = C.clone().addScaledVector(y, -0.02).addScaledVector(fwd, G.chestF + 0.12);
      for (const [side, s] of [['left', 1], ['right', -1]]) {
        // 手腕隔开 15 cm, 手指往上、往里, 指尖在上面中间碰到 (隔 11 cm 时两只手的手指交叉在一起, 像握拳)
        plan[side] = [c.clone().addScaledVector(left, s * 0.075).addScaledVector(y, -0.035), dir([y, -0.8], [left, s * 0.6]),
          dir([y, 0.75], [left, -s * 0.66]), fwd.clone().negate()];
      }
    } else if (st.kind === 'clap') {
      const gap = 0.13 * a * (0.5 - 0.5 * Math.cos(2 * Math.PI * 2.8 * Math.max(0, t - 0.35)));
      const c = C.clone().addScaledVector(y, -0.03).addScaledVector(fwd, G.chestF + 0.15);
      for (const [side, s] of [['left', 1], ['right', -1]]) {
        plan[side] = [c.clone().addScaledVector(left, s * (rh + gap / 2)), dir([y, -0.8], [left, s * 0.6]),
          dir([fwd, 0.7], [y, 0.7]), left.clone().multiplyScalar(-s)];
      }
    }
    const solve = (side, [target, hint, D, N]) => {
      const [up, lo, hand] = ARM[side].slice(1).map((n) => this.nodes[n]);
      if (!up || !lo || !hand) return null;
      const q0 = hand.quaternion.clone();
      const full = this._armHinged(up, lo, hand, target, hint, w, side);
      lo.updateWorldMatrix(true, true);
      const mid = this.vrm.humanoid.getNormalizedBoneNode(`${side}MiddleProximal`);
      if (!mid) return full;
      const hq = hand.getWorldQuaternion(new THREE.Quaternion()), hw = this._world(hand);
      // 手腕比前臂最多弯 75 度
      const fore = hw.clone().sub(this._world(lo)).normalize(), bend = D.angleTo(fore), maxBend = 75 * RAD;
      if (bend > maxBend) D.lerp(fore, 1 - maxBend / bend).normalize();
      const fl = this._world(mid).sub(hw).applyQuaternion(hq.clone().invert()).normalize();
      const pl = new THREE.Vector3(0, -1, 0).addScaledVector(fl, fl.y).normalize();
      const Nn = N.clone().addScaledVector(D, -N.dot(D)).normalize();
      const basis = (u, v) => new THREE.Matrix4().makeBasis(u, v, u.clone().cross(v));
      const rot = new THREE.Quaternion().setFromRotationMatrix(basis(D, Nn).multiply(basis(fl, pl).transpose()));
      const parent = hand.parent.getWorldQuaternion(new THREE.Quaternion());
      hand.quaternion.copy(q0.slerp(parent.invert().multiply(rot), w));
      hand.updateWorldMatrix(false, true);
      if (w > 0.95) (st.err ??= {})[side] = +hw.distanceTo(target).toFixed(3);  // 调试: 手腕离目标多远 (够不着时变大)
      return full;
    };
    const elbow = {};  // 每只手做满时肘在哪 (世界)
    for (const [side, p] of Object.entries(plan)) elbow[side] = solve(side, p);
    st.targets = Object.fromEntries(Object.entries(plan).map(([side, p]) => [side, p[0].toArray()]));  // 调试: 手腕目标 (世界)
    if (st.kind === 'chin') {
      // 另一只手横在腰前, 托着托腮那只手的肘: 手指朝托腮那边, 掌心朝上
      const so = other === 'left' ? 1 : -1, E = elbow[st.side] || this._world(this.nodes[`${st.side}LowerArm`]);
      // 往前放一点 (贴着肚子时手腕、指尖一直被接触层推). 肘按托腮那只手做满时的位置 (不是渐入渐出到一半的):
      // 以前跟着此刻的肘, 放下时托肘的手追着往下落的肘, 手臂横着扫过胸口, 肘、前臂被推 (桌宠自检托腮 21 次).
      // 试过托肘的手晚到早走 (托腮的手七成以后): 放下时手一下子落回待机姿势, 接触层的外展还没跟上, 手腕插进骨盆、大腿 7-9 cm
      solve(other, [E.clone().addScaledVector(y, -0.04).addScaledVector(left, so * 0.04).addScaledVector(fwd, 0.05), dir([y, -0.8], [left, so * 0.6], [fwd, -0.1]),
        dir([left, -so], [fwd, 0.2]), y.clone()]);
    }
  }

  /** 载入时 (标准姿态) 量表情动作要的脸和胸 (米, 角色朝向的坐标): 头骨的朝向记成头骨局部坐标里的 前 / 上 / 左 (fwdL / upL / leftL),
   *  运行时按头骨此刻的朝向转回世界. 主权重在头骨上的顶点 (不算头发): 鼻尖在头骨前面多远 (front)、鼻尖和下巴比头骨高多少
   *  (nose / chin, 下巴在头骨下面是负的)、嘴 (下巴往上四成)、脸颊半宽 (cheek). 胸: 上胸骨往下 5 cm 到往上 8 cm、左右 7 cm 以内
   *  最靠前的地方 (chestF, 0.98 分位, 胸和蝴蝶结). 量不到时按身高给默认值. */
  _faceGeom() {
    const h = this.vrm.humanoid, head = this.nodes.head, chest = this.nodes.upperChest || this.nodes.chest;
    if (!head || !chest) return null;
    this.vrm.scene.updateMatrixWorld(true);
    const { fwd, left } = this._bodyFrame(), up = new THREE.Vector3(0, 1, 0), k = this.hipHeight / 0.9;
    const inv = head.getWorldQuaternion(new THREE.Quaternion()).invert();
    const headRaw = h.getRawBoneNode('head');
    const arm = new Set(Object.keys(h.humanBones).filter((n) => /Shoulder|Arm|Hand|Thumb|Index|Middle|Ring|Little/.test(n))
      .map((n) => h.getRawBoneNode(n)).filter(Boolean));
    const H0 = this._world(head), C0 = this._world(chest), v = new THREE.Vector3(), face = [], chestF = [];
    this.vrm.scene.traverse((o) => {
      if (!o.isSkinnedMesh || /hair/i.test(o.name)) return;
      const pos = o.geometry.attributes.position, si = o.geometry.attributes.skinIndex, sw = o.geometry.attributes.skinWeight;
      if (!si || !sw) return;
      for (let i = 0; i < pos.count; i++) {
        let best = 0, bi = 0;
        for (let j = 0; j < 4; j++) { const x = sw.getComponent(i, j); if (x > best) { best = x; bi = si.getComponent(i, j); } }
        const b = o.skeleton.bones[bi];
        if (b !== headRaw && arm.has(b)) continue;
        o.getVertexPosition(i, v); o.localToWorld(v);
        if (b === headRaw) {
          const d = v.clone().sub(H0);
          face.push([d.dot(fwd), d.y, d.dot(left)]);
        } else {
          const d = v.clone().sub(C0);
          if (d.y > -0.05 * k && d.y < 0.08 * k && Math.abs(d.dot(left)) < 0.07 * k && d.dot(fwd) > 0) chestF.push(d.dot(fwd));
        }
      }
    });
    const q = (arr, p) => { if (!arr.length) return null; const s = [...arr].sort((x, y2) => x - y2); return s[Math.floor(p * (s.length - 1))]; };
    const mid = face.filter((p) => Math.abs(p[2]) < 0.02 * k);
    const front = q(mid.map((p) => p[0]), 0.98) ?? 0.09 * k;
    const nose = q(mid.filter((p) => p[0] > front - 0.006 * k).map((p) => p[1]), 0.5) ?? 0.03 * k;
    const chin = q(mid.filter((p) => p[0] > front - 0.045 * k).map((p) => p[1]), 0.01) ?? -0.02 * k;
    const cheek = q(face.filter((p) => p[0] > 0 && p[1] > chin && p[1] < nose).map((p) => Math.abs(p[2])), 0.95) ?? 0.055 * k;
    return {
      fwdL: fwd.clone().applyQuaternion(inv), upL: up.clone().applyQuaternion(inv), leftL: left.clone().applyQuaternion(inv),
      front, nose, chin, mouth: chin + 0.4 * (nose - chin), cheek, chestF: q(chestF, 0.98) ?? 0.12 * k,
    };
  }

  /** 腰前衣服 / 身体最靠前的地方在脊柱骨前面多远 (米, 沿角色朝前), 交叠的手放在它前面. 接触层的腹部椭圆不够:
   *  它只给衣服留 2 cm, silvercat 腰前的腰带、蝴蝶结在脊柱骨前 13 cm, 按椭圆放的手插进衣服 3 cm.
   *  载入时 (还是静止姿势) 量一次: 脊柱骨高度到往上 5 cm、左右 8 cm 以内、在脊柱骨前面的顶点 (不算头发、脸和手臂上的), 取 0.98 分位.
   *  往下不量: 裙子从脊柱骨下面 3 cm 起张开到 20 cm, 量进去的话随待机姿态在 7-20 cm 之间跳 (手伸得老远).
   *  不在待机里量: 脊柱骨和裙子上沿的相对高度随片段变, 量出 8-14 cm 不等. */
  _waistFront() {
    if (this._waistF !== undefined) return this._waistF;
    const h = this.vrm.humanoid, spine = this.nodes.spine || this.nodes.hips;
    const arm = new Set(Object.keys(h.humanBones).filter((n) => /Shoulder|Arm|Hand|Thumb|Index|Middle|Ring|Little/.test(n))
      .map((n) => h.getRawBoneNode(n)).filter(Boolean));
    this.vrm.scene.updateMatrixWorld(true);
    const c = this._world(spine), { fwd, left } = this._bodyFrame(), H = this.hipHeight / 0.9;
    const v = new THREE.Vector3(), out = [], side = [];
    this.vrm.scene.traverse((o) => {
      if (!o.isSkinnedMesh || /hair|face/i.test(o.name)) return;
      const pos = o.geometry.attributes.position, si = o.geometry.attributes.skinIndex, sw = o.geometry.attributes.skinWeight;
      if (!si || !sw) return;
      for (let i = 0; i < pos.count; i++) {
        let best = 0, bi = 0;
        for (let j = 0; j < 4; j++) { const w = sw.getComponent(i, j); if (w > best) { best = w; bi = si.getComponent(i, j); } }
        if (arm.has(o.skeleton.bones[bi])) continue;
        o.getVertexPosition(i, v); o.localToWorld(v);
        const d = v.sub(c);
        if (d.y < 0 || d.y > 0.05 * H) continue;
        if (Math.abs(d.dot(fwd)) < 0.05 * H) side.push(Math.abs(d.dot(left)));  // 腰两侧 (叉腰的手放在它外面)
        if (Math.abs(d.dot(left)) > 0.08 * H || d.dot(fwd) < 0) continue;
        out.push(d.dot(fwd));
      }
    });
    out.sort((a, b) => a - b);
    side.sort((a, b) => a - b);
    this._waistF = out.length > 20 ? clamp(out[Math.floor(out.length * 0.98)], 0.05 * H, 0.18 * H) : 0.11 * H;
    this._waistS = side.length > 20 ? clamp(side[Math.floor(side.length * 0.98)], 0.06 * H, 0.2 * H) : 0.1 * H;
    return this._waistF;
  }

  /** 拿在手里的东西跟着手走: 放在手掌那一面 (掌心中点沿掌心法线让出物体的半径). */
  _items() {
    for (const item of Object.values(this.items)) {
      if (!item.held) continue;
      if (syncGrasp(this, item)) continue;
      const p = this._palmHold(item, item.held);
      // 交给物理的 (item.physical, 小窝物理 P2): 只给抓持一个掌心目标 (竖着、朝她面朝的方向), 不再把东西摆到掌心
      if (item.physical) { item.target = { p: p.toArray(), q: [0, Math.sin(this.char.yaw / 2), 0, Math.cos(this.char.yaw / 2)] }; continue; }
      item.mesh.position.copy(p);
      item.mesh.rotation.y = this.char.yaw;
    }
  }

  /** 拿在这只手里时物品中心该在的位置: 掌心 (手腕到中指根 0.6 处) 沿掌心法线往外物品半径 + 1.2 cm. */
  _palmHold(item, side) {
    if (usesGrasp(item) && item.graspBinding) {
      syncGrasp(this, item);
      return new THREE.Vector3(...worldPose(item.mesh).position);
    }
    if (item.cupBinding && cupPickItem(item)) return liveCupCenter(this, item, side) || this._legacyPalmHold(item, side);
    return this._legacyPalmHold(item, side);
  }

  _legacyPalmHold(item, side) {
    const handNode = this.nodes[`${side}Hand`];
    const wrist = this._handPos(side);
    const mid = this.vrm.humanoid.getNormalizedBoneNode(`${side}MiddleProximal`);
    const palmC = mid ? wrist.clone().lerp(this._world(mid), 0.6) : wrist;
    const normal = handNode ? new THREE.Vector3(0, -1, 0).applyQuaternion(handNode.getWorldQuaternion(new THREE.Quaternion())) : new THREE.Vector3(0, -1, 0);
    return palmC.addScaledVector(normal, item.half + 0.012);
  }

  /** 痛觉: 拿起时物品原来的位置、放下时的落点, 离此刻手里该在的位置多远 (这么远的一段是瞬移过去的).
   *  拿放的事件在手臂 IK 当中触发, 后面还有层改手 (_look 转胸口): 先记下, 这一帧的手摆完 (_handsDone) 再量. */
  _grabGap(item, side, at, now = false) {
    if (!now) { (this._gapLater ??= []).push({ item, side, at: at.clone() }); return; }
    this.vrm.scene.updateMatrixWorld(true);
    const gap = this._legacyPalmHold(item, side).distanceTo(at);
    this.stats.grabGap = (this.stats.grabGap || 0) + gap;
    this.stats.grabs = (this.stats.grabs || 0) + 1;
    this.lastGrabGap = gap;
  }

  // -- IK -----------------------------------------------------------------------------------
  _world(node) { return node.getWorldPosition(new THREE.Vector3()); }

  _feet(dt) {
    this.vrm.scene.updateMatrixWorld(true);
    const contacts = this.lastContacts || [0, 0];
    // 原地手势 (见 _idleStep): 两只脚踩在开始时的落点不动. 演员的站姿和角色待机的站姿不同, 动画里的脚踝常离落点 30 cm 以上,
    // 这时也不放开重新落脚 (那是给转身用的), 否则脚会跟着动画在地上滑一段 (耸肩时滑了 150 mm)
    const plant = (this.idle?.kind === 'gesture' || this.idle?.kind === 'action') && this.idle.plant;
    const legs = [];
    ['left', 'right'].forEach((side, i) => {
      const upper = this.nodes[`${side}UpperLeg`], lower = this.nodes[`${side}LowerLeg`], foot = this.nodes[`${side}Foot`];
      if (!upper || !lower || !foot) return;
      const st = this.feet[side];
      const ankle = this._world(foot);
      const floorY = this.ankle[side];
      let contact = (contacts[i] && this.weight > 0.5) || this.mode === 'seated' || (!!this.action?.rise && !!TUNE.risePlant);  // 坐着、站起来时脚一直踩地
      // 锁定点偏太远 (转身) 时先放开, 等锁定权重降下来再重新落脚, 不瞬移. 坐着/坐下/站起时脚由座位落点决定, 不做这一步
      // (座位落点和动画里的脚踝常差 0.3 m 以上, 会一直放开-重锁, 小腿和脚来回跳)
      const onSeat = this.mode === 'seated' || this.seatAmount > 0.05;
      // 站着时先换重心再抬脚: 要重新落脚的脚先等配平层把重心挪到另一只脚上 (_liftSoon 把它算成要抬的脚) 再放开, 最多等
      // TUNE.stepWait 秒. 以前一超过距离就放开: 放东西时根部对位把动画里的脚带走 30 cm, 脚抬起来时重心还在两脚中间
      // 只在动画本来没打算抬脚 (着地标记着地)、脚也还牢牢锁着时等; 动画自己在迈步 (标记已经不着地、锁正在放开) 时照旧马上放开,
      // 不然旧的锁定点留着, 标记恢复着地后脚又被拉回 20 cm 外的旧位置
      if (st.replant) contact = false;
      else if (!onSeat && !plant && st.lock && Math.hypot(st.lock.x - ankle.x, st.lock.z - ankle.z) > (this.action?.grab ? TUNE.grabReplant : TUNE.replant)) {
        // 另一只脚也要牢牢站着 (两只脚都离锁定点太远时谁也不等, 照旧: 站起来回到站姿时两只都在等, 重心一直在外面)
        const other = this.feet[side === 'left' ? 'right' : 'left'];
        const hold = contact && st.w > 0.9 && this.mode !== 'go' && TUNE.stepWait > 0
          && !!other.lock && other.w > 0.9 && !other.replant && !other.stepWait;
        st.stepWait = hold ? (st.stepWait || 0) + dt : 0;
        if (!hold || st.stepWait >= TUNE.stepWait || (this._soon?.[i] && this.balanceMargin >= 0)) {
          contact = false;
          st.replant = true;
          st.stepWait = 0;
        }
      } else st.stepWait = 0;
      if (st.replant && st.w < 0.05) { st.replant = false; st.lock = null; }
      // 着地时在脚踝此刻的位置 (含高度) 锁定, 高度再慢慢落到地面; 抬脚时锁定权重连续降到 0.
      // 以前是着地那一帧直接把高度设成地面、抬脚时切换分支, 每一步大腿小腿都会跳一下.
      if (contact && !st.lock) st.lock = this._narrow(ankle.clone(), side);  // 落脚时往中线收 (锁定后不再挪, 不打滑)
      st.w = (st.w ?? 0) + ((contact ? 1 : 0) - (st.w ?? 0)) * damp(contact ? TUNE.lockIn : TUNE.lockOut, dt);
      if (st.lock) st.lock.y += (floorY - st.lock.y) * damp(TUNE.lockSettle, dt);
      if (!contact && st.w < 0.02) st.lock = null;
      st.was = contact;
      const free = this._narrow(ankle.clone(), side);
      free.y = Math.max(free.y, floorY);
      const target = st.lock ? free.lerp(st.lock, st.w) : free;
      // 坐下/坐着/站起: 按坐下的程度把脚挪到座位前沿外面
      const seat = this.action?.seat?.info;
      const thigh = this._world(upper).distanceTo(this._world(lower)), length = thigh + this._world(lower).distanceTo(ankle);
      if (seat && this.seatAmount > 0.01) {
        target.lerp(this._seatFoot(side, target, seat), this.seatAmount);
        if (TUNE.seatReach) target.lerp(this._seatReach(this._world(upper), target.clone(), thigh, length - thigh, seat), this.seatAmount);
      }
      if (seat) {
        // 脚不进座位前沿 (不管坐下的程度): 起身最后一步、坐下前转身时片段里的脚常在座位下面
        const f = [Math.sin(seat.yaw), Math.cos(seat.yaw)];
        const fw = (target.x - seat.pos[0]) * f[0] + (target.z - seat.pos[1]) * f[1], min = seat.front + TUNE.seatHeel;
        if (TUNE.seatHeel > 0 && fw < min) { target.x += f[0] * (min - fw); target.z += f[1] * (min - fw); }
        st.seatT = target.clone();  // 站起来时从这里锁 (见 standUp)
        // 落脚点比片段里的脚踝靠前多少 (两只脚平均), 站起来时根部按这个往前追
        if (i === 0) this._seatGap = 0;
        this._seatGap += ((target.x - ankle.x) * f[0] + (target.z - ankle.z) * f[1]) / 2;
      }
      legs.push({ upper, lower, foot, ankle, target, st, lockW: st.lock ? st.w : 0, length });
    });
    // 两腿防穿插 (坐着时不管: 翘腿、盘腿是动作本身)
    if (TUNE.legSep && legs.length === 2 && this.legShape && !(this.mode === 'seated' || this.seatAmount > 0.05)) this._separateLegs(legs, dt);
    // 骨盆补偿: 源动捕和这个模型的腿长/脚踝高度比例不同, 腿伸直还够不到地面时把髋部降下来.
    // 按锁定权重加权 (不随着地/抬脚突变), 平滑也放慢, 免得每一步髋部和双腿一起跳.
    // 坐着时不降: 骨盆由座面托着, 够不着的落脚点已经在 _seatReach 里收回来了 (以前坐着也降, 骨盆陷进座位 15 cm,
    // 膝盖跟着插进沙发底座)
    let drop = 0;
    for (const l of legs) {
      const reach = (this._world(l.upper).distanceTo(l.target) - l.length * 0.995) * l.lockW * (TUNE.seatReach ? 1 - this.seatAmount : 1);
      if (reach > drop) drop = reach;
    }    this.hipDrop = (this.hipDrop || 0) + (drop - (this.hipDrop || 0)) * damp(drop > (this.hipDrop || 0) ? TUNE.hipDropDown : TUNE.hipDropUp, dt);
    if (this.hipDrop > 1e-4) {
      this.nodes.hips.position.y -= this.hipDrop * this.weight;
      this.nodes.hips.updateWorldMatrix(false, true);
    }
    const seat = this.action?.seat?.info, seatW = seat ? this.seatAmount * this.weight : 0;
    for (const { upper, lower, foot, ankle, target, st } of legs) {
      const footWorld = foot.getWorldQuaternion(new THREE.Quaternion());
      // 原地手势: 脚的朝向也踩住. 演员做手势时脚尖常转来转去, 只锁脚踝的话脚尖还在地上扫 (耸肩时脚底打滑 160 mm)
      const hold = plant && st.lock;
      st.qw = (st.qw ?? 0) + ((hold ? 1 : 0) - (st.qw ?? 0)) * damp(0.08, dt);
      if (hold && !st.lockQ) st.lockQ = footWorld.clone();
      if (!hold && st.qw < 0.01) st.lockQ = null;
      if (st.lockQ) footWorld.slerp(st.lockQ, st.qw);
      // 锁定着的脚放平 (按锁定权重): 动画里站着的脚常斜 10° 以上 (动作数据本身, 加上模型脚骨骼的倾角和标准方向不同),
      // 鞋底只有一头着地、另一头悬空几厘米, 支撑面也只剩脚跟或前掌. 放平 = 回到零位时相对身体的倾角, 只留脚尖朝向.
      // 抬脚、蹬地时锁定权重低, 不动; 斜得多的 (深蹲脚跟抬起、踮脚) 是动作本身, 20° 以上渐渐不放平, 35° 起不管
      // 走路时不放平 (每一步锁定权重快速升降, 放平跟着跳: 走路动作冲击 16 -> 46); 权重单独平滑, 起止都不突变
      const still = this.mode !== 'go' && Math.hypot(this.sim.vx, this.sim.vz) < 0.05;
      st.flat = (st.flat ?? 0) + ((still && st.lock ? st.w : 0) - (st.flat ?? 0)) * damp(0.2, dt);
      const fw0 = TUNE.footFlat * st.flat * st.flat * (3 - 2 * st.flat);
      if (fw0 > 0.01) {
        const sq = this.vrm.scene.getWorldQuaternion(new THREE.Quaternion());
        const d = sq.clone().invert().multiply(footWorld);  // 标准化骨骼零位时脚和身体同向
        const flat = sq.multiply(new THREE.Quaternion(0, d.y, 0, d.w).normalize());
        const fw = fw0 * clamp((35 - deg(flat.angleTo(footWorld))) / 15, 0, 1);
        if (fw > 0.01) footWorld.slerp(flat, fw);
      }
      const t = this._world(foot).lerp(target, this.weight);
      if (this.hipDrop <= 1e-4) t.copy(ankle).lerp(target, this.weight);
      this._twoBone(upper, lower, foot, t, this._forward());
      // 坐着时膝盖朝前 (略向外): 动捕的弯曲平面常把膝盖分得很开, 绕"髋-踝"轴把膝盖转到前方
      if (seatW > 0.01) this._pole(upper, lower, foot, upper === this.nodes.leftUpperLeg ? 1 : -1, seat, seatW);
      lower.updateWorldMatrix(true, false);
      foot.quaternion.copy(lower.getWorldQuaternion(new THREE.Quaternion()).invert().multiply(footWorld));
      foot.updateWorldMatrix(false, true);
    }
  }

  /** 两腿防穿插: 按"膝盖此刻的位置 -> 脚踝目标"预测两条小腿 (胶囊见 _legShape), 比两条小腿半径之和加 TUNE.legGap 还近时, 沿身体左右方向
   *  把落脚点分开 (左脚往左、右脚往右, 交叉着的拉回各自那一侧), 分开到正好不穿. 锁定着的脚挪得少 (锁死的只挪 20%),
   *  锁定点跟着挪, 下一帧不会再被拉回去. 待机库里演员常把一只脚收到另一只脚前面, 套到小腿靠得近的模型上 (silvercat)
   *  两条小腿就插在一起. */
  _separateLegs(legs, dt) {
    const [L, R] = legs, sh = this.legShape;
    const fwd = this._forward(), left = new THREE.Vector3(fwd.z, 0, -fwd.x);  // 身体的左边 (水平)
    // 预测的胶囊: 小腿 = 膝盖此刻的位置 -> 脚踝目标 (都按网格中心挪开, 见 _legShape); 脚 = 脚踝目标 -> 脚尖 (脚的朝向不变).
    // lever: 胶囊上一点随脚踝挪多少 (小腿上 0 膝盖 .. 1 脚踝, 脚上都是 1)
    const caps = [L, R].map((g, i) => {
      const s = sh[i ? 'right' : 'left'], o = s.off.clone().applyQuaternion(g.lower.getWorldQuaternion(new THREE.Quaternion()));
      const k = this._world(g.lower).add(o), a = g.target.clone().add(o);
      const toes = this.nodes[`${i ? 'right' : 'left'}Toes`];
      const toe = toes ? this._world(toes).sub(g.ankle).add(g.target) : null;
      return { shin: [k, a, s.r], foot: toe ? [g.target, toe, s.foot] : null };
    });
    let worst = null;
    for (const [x, y, lx, ly] of [['shin', 'shin', 1, 1], ['foot', 'shin', 0, 1], ['shin', 'foot', 1, 0], ['foot', 'foot', 0, 0]]) {
      const A = caps[0][x], B = caps[1][y];
      if (!A || !B) continue;
      const { d, s, t } = segmentDistance(A[0].toArray(), A[1].toArray(), B[0].toArray(), B[1].toArray());
      const minD = A[2] + B[2] + TUNE.legGap;
      if (d >= minD) continue;
      const sep = A[0].clone().lerp(A[1], s).sub(B[0].clone().lerp(B[1], t));
      const lat = sep.dot(left), other = sep.addScaledVector(left, -lat).length();
      const need = Math.sqrt(Math.max(0, minD * minD - other * other)) - lat;  // 左右方向还要分开多少 (在最近点那里)
      // 只挪脚踝: 小腿上离膝盖近的点要挪得多才分得开
      const shift = need / Math.max(0.35, ((lx ? s : 1) + (ly ? t : 1)) / 2);
      if (need > 0 && (!worst || shift > worst)) worst = shift;
    }
    // 推开量平滑 (变大快、变小慢): 最近点在两条腿上来回跳时每帧算出来的量会突变 (伸手转身时动作冲击 47 -> 89); 一帧最多 10 cm
    const want = Math.min(0.1, worst || 0);
    this._legSep = (this._legSep || 0) + (want - (this._legSep || 0)) * damp(want > (this._legSep || 0) ? TUNE.legSepIn : TUNE.legSepOut, dt);
    if (this._legSep < 1e-4) return;
    const fL = 1 - 0.8 * L.lockW, fR = 1 - 0.8 * R.lockW, sum = fL + fR;
    for (const [g, f, sign] of [[L, fL, 1], [R, fR, -1]]) {
      const dv = left.clone().multiplyScalar((sign * this._legSep * f) / sum);
      g.target.add(dv);
      // 牢牢锁着的脚, 锁定点跟着挪 (下一帧不会再被拉回去); 半锁着的不挪锁定点, 不然每帧累加, 越挪越远
      if (g.st.lock && g.st.w > 0.9 && want > 0) g.st.lock.addScaledVector(dv, damp(0.15, dt));
    }
    if (want > 0) this.stats.legSep = (this.stats.legSep || 0) + 1;
  }

  /** 小腿的形状: 主要蒙皮到小腿骨骼的顶点 (膝盖、脚踝附近 15% 不算; 袜子、裤管都算, 鞋在脚上不算), 零位时量.
   *  骨骼贴着小腿前侧 (胫骨), 腿肚子在后面: silvercat 的小腿网格整个在骨骼轴线后 0.6-7.6 cm. 所以胶囊的中心线按网格的
   *  截面中心挪开 (off, 存在标准化小腿骨骼的局部坐标里, 跟着小腿转), 半径是到各段截面中心距离的 90% 分位数; 只按骨骼轴线量的话
   *  把"往后偏"也算成了粗细 (silvercat 6.8 cm). 脚的半径取鞋底宽度的一半. 量不到时按髋高比例估、不偏. */
  _legShape() {
    const h = this.vrm.humanoid, out = {}, v = new THREE.Vector3(), c = new THREE.Vector3();
    this.vrm.scene.updateMatrixWorld(true);
    ['left', 'right'].forEach((side, i) => {
      const shin = h.getRawBoneNode(`${side}LowerLeg`), foot = h.getRawBoneNode(`${side}Foot`), norm = this.nodes[`${side}LowerLeg`];
      const sole = this.sole?.[i]?.map(([m, k]) => m.localToWorld(m.getVertexPosition(k, new THREE.Vector3())).x);
      const footR = sole ? (Math.max(...sole) - Math.min(...sole)) / 2 : 0.035 * this.hipHeight / 0.8;
      out[side] = { r: 0.04 * this.hipHeight / 0.8, off: new THREE.Vector3(), foot: footR };
      if (!shin || !foot || !norm) return;
      const A = shin.getWorldPosition(new THREE.Vector3()), AB = foot.getWorldPosition(new THREE.Vector3()).sub(A), L2 = AB.lengthSq();
      const offs = [];
      this.vrm.scene.traverse((m) => {
        if (!m.isSkinnedMesh) return;
        const idx = m.skeleton.bones.indexOf(shin);
        if (idx < 0) return;
        const pos = m.geometry.attributes.position, si = m.geometry.attributes.skinIndex, sw = m.geometry.attributes.skinWeight;
        for (let j = 0; j < pos.count; j++) {
          let best = 0, bw = 0;
          for (let k = 0; k < 4; k++) { const w = sw.getComponent(j, k); if (w > bw) { bw = w; best = si.getComponent(j, k); } }
          if (best !== idx) continue;
          v.fromBufferAttribute(pos, j).applyMatrix4(m.matrixWorld);
          const t = c.copy(v).sub(A).dot(AB) / L2;
          if (t > 0.15 && t < 0.85) offs.push([Math.min(4, Math.floor(((t - 0.15) / 0.7) * 5)), v.clone().sub(c.copy(A).addScaledVector(AB, t))]);
        }
      });
      if (offs.length < 20) return;
      // 分 5 段, 每段到自己的截面中心量 (小腿上粗下细、截面中心也在变, 都对一条平均中心线量会把这些算成粗细)
      const bins = Array.from({ length: 5 }, () => [new THREE.Vector3(), 0]);
      for (const [b, o] of offs) { bins[b][0].add(o); bins[b][1]++; }
      for (const bin of bins) if (bin[1]) bin[0].divideScalar(bin[1]);
      const mid = offs.reduce((a, [, o]) => a.add(o), new THREE.Vector3()).divideScalar(offs.length);
      const d = offs.map(([b, o]) => o.distanceTo(bins[b][0])).sort((x, y) => x - y);
      const q = norm.getWorldQuaternion(new THREE.Quaternion()).invert();
      out[side] = { r: clamp(d[Math.floor(d.length * 0.9)], 0.02, 0.08), off: mid.applyQuaternion(q), foot: footR };
    });
    return out;
  }

  /** 此刻两条腿的胶囊 (世界坐标数组): 小腿 [a 膝, b 踝] 按网格中心挪开, 脚 [踝, 脚尖]. 执行评估和两腿防穿插用. */
  legCapsules() {
    const out = {};
    for (const side of ['left', 'right']) {
      const s = this.legShape?.[side], lo = this.nodes[`${side}LowerLeg`], ft = this.nodes[`${side}Foot`], toe = this.nodes[`${side}Toes`];
      if (!s || !lo || !ft) return null;
      const off = s.off.clone().applyQuaternion(lo.getWorldQuaternion(new THREE.Quaternion()));
      const knee = this._world(lo), ankle = this._world(ft);
      out[side] = { a: knee.add(off).toArray(), b: ankle.clone().add(off).toArray(), r: s.r,
        foot: toe ? { a: ankle.toArray(), b: this._world(toe).toArray(), r: s.foot } : null };
    }
    return out;
  }

  /** 鞋底取样: 每只脚零位时最低的一圈顶点 (主要蒙皮到这条腿上, 离最低点 1.5 cm 以内, 最多 40 个).
   *  落地按它们此刻的蒙皮位置量鞋底离地多高, 平底鞋、高跟鞋、厚底鞋都一样; 只看脚踝骨骼的话, 脚尖往下压时会穿地. */
  _soleSamples() {
    const h = this.vrm.humanoid, v = new THREE.Vector3(), out = [];
    this.vrm.scene.updateMatrixWorld(true);
    for (const side of ['left', 'right']) {
      const root = h.getRawBoneNode(`${side}UpperLeg`);
      if (!root) return null;
      const cand = [];
      this.vrm.scene.traverse((m) => {
        if (!m.isSkinnedMesh) return;
        const under = m.skeleton.bones.map((b) => { for (let p = b; p; p = p.parent) if (p === root) return true; return false; });
        const pos = m.geometry.attributes.position, si = m.geometry.attributes.skinIndex, sw = m.geometry.attributes.skinWeight;
        for (let i = 0; i < pos.count; i++) {
          let best = 0, bw = 0;
          for (let c = 0; c < 4; c++) { const w = sw.getComponent(i, c); if (w > bw) { bw = w; best = si.getComponent(i, c); } }
          if (under[best]) cand.push([m, i, v.fromBufferAttribute(pos, i).applyMatrix4(m.matrixWorld).y]);
        }
      });
      if (!cand.length) return null;
      const lo = Math.min(...cand.map((c) => c[2]));
      const near = cand.filter((c) => c[2] < lo + 0.015);
      const stepN = Math.max(1, Math.floor(near.length / 40));
      out.push(near.filter((_, k) => k % stepN === 0).slice(0, 40).map(([m, i]) => [m, i]));
    }
    return out;
  }

  /** 两只脚鞋底的最低点 (世界坐标 y, 地面是 0). 蒙皮跟的是实际骨骼, 先把标准化骨骼的姿势同步过去 (平时在 vrm.update 里做). */
  _soleLow() {
    return this._solePoints().map((pts) => {
      let lo = Infinity;
      for (const p of pts) if (p[1] < lo) lo = p[1];
      return lo;
    });
  }

  /** 两只脚鞋底取样点的世界坐标 [[x, y, z], ...] (蒙皮后). 支撑面用它的着地部分 (kinematics-core.js 的 soleContact). */
  _solePoints() {
    this.vrm.humanoid.update();
    this.vrm.humanoid.getRawBoneNode('hips').updateWorldMatrix(true, true);
    const v = this._sv ??= new THREE.Vector3();
    return this.sole.map((pts) => pts.map(([m, i]) => {
      m.getVertexPosition(i, v);
      m.localToWorld(v);
      return [v.x, v.y, v.z];
    }));
  }

  /** 落地: IK 解完后量支撑脚 (锁定着的脚; 都没锁定时取低的那只) 的鞋底离地多高, 把整个身体上下挪这么多, 腿的姿势不变.
   *  源动捕的髋高和这个模型的腿长对不上时, 前面的骨盆补偿只按锁定的脚算、平滑又慢, 起步和换脚时双脚会一起悬空
   *  (走到窗边再回来 600 帧: AvatarSample_B 52% 的帧两只脚都离地 1 cm 以上、最高 10 cm, Wendy 80%、最高 12 cm;
   *  加上这一步后 3% 和 6%, 鞋底穿地 1.5 cm 以上的帧 15 和 10 -> 0).
   *  用积分的方式更新偏移 (每帧按量到的误差修一部分), 交回 HAST-SEC 的过渡中骨盆位置部分沿用上一帧时也不会累加出错. 坐着时不管.
   *  在姿态平滑层之后调用 (见 avatar-vrm.js). 返回这一帧有没有挪动身体. */
  _ground(dt) {
    if (!this.sole || this.groundOn === false) return false;  // groundOn = false: 关掉落地 (调试对比用)
    const onSeat = this.mode === 'seated' || this.seatAmount > 0.05;
    if (onSeat) {
      this.groundOff *= 1 - damp(0.15, dt);
      this._shiftHips(this.groundOff);
      return Math.abs(this.groundOff) > 1e-6;
    }
    this._shiftHips(this.groundOff);
    const low = this._soleLow(), w = [this.feet.left.w || 0, this.feet.right.w || 0];
    if (!low.every(Number.isFinite)) return false;
    // 支撑脚: 锁定着的脚里低的那只; 都没锁定时取两只里低的 (按锁定权重加权试过, 支撑脚误差反而大)
    const stance = [0, 1].filter((i) => w[i] > 0.5);
    const c = stance.length ? Math.min(...stance.map((i) => low[i])) : Math.min(low[0], low[1]);
    // 踩进地面时抬得快 (groundRaise 半衰期), 离地时平滑地往下放 (groundLower), 换脚时骨盆不跳
    const hl = c < 0 ? TUNE.groundRaise : TUNE.groundLower;
    const step = -c * (hl <= 0 ? 1 : damp(hl, dt));
    const next = Math.max(-0.15, Math.min(0.15, this.groundOff + step));
    this._shiftHips(next - this.groundOff);
    this.groundOff = next;
    this.groundErr = c;  // 调试: 这一帧修正前支撑脚鞋底离地多高
    // 摆动脚 (没锁定) 的鞋底穿到地面以下: 这只脚单独抬起来 (两骨 IK, 脚的朝向不变)
    const sides = ['left', 'right'];
    for (const i of [0, 1]) {
      const depth = -(low[i] + step);
      if (this.liftSwing === false || w[i] > 0.5 || depth < 0.003) continue;
      const upper = this.nodes[`${sides[i]}UpperLeg`], lower = this.nodes[`${sides[i]}LowerLeg`], foot = this.nodes[`${sides[i]}Foot`];
      const footWorld = foot.getWorldQuaternion(new THREE.Quaternion());
      const t = this._world(foot).add(new THREE.Vector3(0, depth, 0));
      this._twoBone(upper, lower, foot, t, this._forward());
      lower.updateWorldMatrix(true, false);
      foot.quaternion.copy(lower.getWorldQuaternion(new THREE.Quaternion()).invert().multiply(footWorld));
      foot.updateWorldMatrix(false, true);
    }
    return true;
  }

  /** 骨盆沿世界竖直方向挪 dy 米. */
  _shiftHips(dy) {
    if (Math.abs(dy) < 1e-6) return;
    const hips = this.nodes.hips;
    const inv = hips.parent.getWorldQuaternion(new THREE.Quaternion()).invert();
    hips.position.add(new THREE.Vector3(0, dy, 0).applyQuaternion(inv));
    hips.updateWorldMatrix(false, true);
  }

  /** 极向量: 绕"髋-踝"轴转动大腿, 让膝盖指向座位前方偏外 (sgn: 左腿 +1, 右腿 -1), 按 w 混合. */
  _pole(upper, lower, foot, sgn, seat, w) {
    const hip = this._world(upper), knee = this._world(lower), ankle = this._world(foot);
    const axis = ankle.clone().sub(hip).normalize();
    const f = new THREE.Vector3(Math.sin(seat.yaw), 0, Math.cos(seat.yaw)), l = new THREE.Vector3(Math.cos(seat.yaw), 0, -Math.sin(seat.yaw));
    const want = f.addScaledVector(l, 0.18 * sgn).addScaledVector(new THREE.Vector3(0, 1, 0), 0.3);
    const proj = (v) => v.clone().sub(axis.clone().multiplyScalar(v.dot(axis)));
    const cur = proj(knee.clone().sub(hip)), goal = proj(want);
    if (cur.lengthSq() < 1e-8 || goal.lengthSq() < 1e-8) return;
    cur.normalize(); goal.normalize();
    const angle = Math.atan2(cur.clone().cross(goal).dot(axis), cur.dot(goal)) * w;
    const q = new THREE.Quaternion().setFromAxisAngle(axis, angle);
    const parent = upper.parent.getWorldQuaternion(new THREE.Quaternion());
    const world = upper.getWorldQuaternion(new THREE.Quaternion());
    upper.quaternion.copy(parent.invert().multiply(q.multiply(world)));
    upper.updateWorldMatrix(false, true);
  }

  _forward() {
    return new THREE.Vector3(Math.sin(this.char.yaw), 0, Math.cos(this.char.yaw));
  }

  /** 身体此刻实际的水平朝向 {fwd, left}: 左 = 两髋连线、两肩连线的平均, 前 = 左 x 上. 待机片段里身体常和根部朝向差 30-45 度,
   *  仪态层按根部朝向放的手落到了胯侧、插进张开的裙子 (裙子弹簧骨被手的碰撞体掀起来). 缺骨骼时退回根部朝向. */
  _bodyFrame() {
    const n = this.nodes, up = new THREE.Vector3(0, 1, 0);
    if (!n.leftUpperLeg || !n.rightUpperLeg || !n.leftUpperArm || !n.rightUpperArm) {
      const fwd = this._forward();
      return { fwd, left: new THREE.Vector3(fwd.z, 0, -fwd.x) };
    }
    const legs = this._world(n.leftUpperLeg).sub(this._world(n.rightUpperLeg)).setY(0).normalize();
    const arms = this._world(n.leftUpperArm).sub(this._world(n.rightUpperArm)).setY(0).normalize();
    const left = legs.add(arms).normalize();
    return { fwd: left.clone().cross(up), left };
  }

  /** 两骨骼 IK (a-b-c 三个关节, 让 c 到达 target), 写回 a、b 的局部旋转. hint: 关节弯曲的朝向.
   *  exact: a 的修正按 "先绕弯曲平面的法线转 (a 到 c 的方向不变)、再对准" 合成, 弯着时一次解准. 默认是 "先对准、再绕对准之前的法线转",
   *  差一截 (端着放到茶几上时手腕差 2.4 cm, 肩到目标 0.275 m、手臂 0.389 m 够得着). 快伸直时 (按提示方向混弯曲平面) 两种都不准.
   *  只给世界里定住的手用 (拿放、伸手 _armTo, 端着放下、拿起来送回 _carry): 换到所有手臂 IK 上, 不端稳走路时杯子跟着骨盆颠得更实
   *  (掌心竖直加速度 1.5-3 Hz 那段 1.8 -> 2.6), 交给物理的不端稳走路洒 91 -> 136 ml (原来解不准, 碰巧抵掉一点颠) */
  _twoBone(a, b, c, target, hint, exact = false) {
    const pa = this._world(a), pb = this._world(b), pc = this._world(c);
    const lab = pa.distanceTo(pb), lcb = pb.distanceTo(pc);
    const lat = clamp(pa.distanceTo(target), 0.01, lab + lcb - 0.002);
    const n = (v) => v.clone().normalize();
    const acos = (x) => Math.acos(clamp(x, -1, 1));
    const ac = pc.clone().sub(pa), ab = pb.clone().sub(pa), at = target.clone().sub(pa);
    const ac_ab_0 = acos(n(ac).dot(n(ab)));
    const ba_bc_0 = acos(n(pa.clone().sub(pb)).dot(n(pc.clone().sub(pb))));
    const ac_at_0 = acos(n(ac).dot(n(at)));
    const ac_ab_1 = acos((lcb * lcb - lab * lab - lat * lat) / (-2 * lab * lat));
    const ba_bc_1 = acos((lat * lat - lab * lab - lcb * lcb) / (-2 * lab * lcb));
    // 弯曲平面: 弯得明显时用当前的平面; 接近伸直时这个叉积又小又不稳, 弯曲方向会在相邻两帧之间翻转
    // (拿着东西走路时前臂一帧转 167 度), 按弯曲程度连续过渡到提示方向 (膝盖朝前、肘部朝下后方).
    let axis0 = ac.clone().cross(ab);
    const bend = axis0.length() / Math.max(1e-9, ac.length() * ab.length());
    const hintAxis = ac.clone().cross(hint);
    if (hintAxis.lengthSq() > 1e-12) {
      hintAxis.normalize();
      if (axis0.lengthSq() < 1e-12) axis0.copy(hintAxis);
      else {
        axis0.normalize();
        if (bend < 0.3 && axis0.dot(hintAxis) < 0) axis0.negate();
        const k = clamp((bend - 0.05) / 0.25, 0, 1);
        axis0.lerp(hintAxis, 1 - k * k * (3 - 2 * k));
      }
    }
    axis0.normalize();
    const axis1 = ac.clone().cross(at);
    const qa = a.getWorldQuaternion(new THREE.Quaternion()).invert(), qb = b.getWorldQuaternion(new THREE.Quaternion()).invert();
    const r0 = new THREE.Quaternion().setFromAxisAngle(axis0.clone().applyQuaternion(qa), ac_ab_1 - ac_ab_0);
    const r1 = new THREE.Quaternion().setFromAxisAngle(axis0.clone().applyQuaternion(qb), ba_bc_1 - ba_bc_0);
    const r2 = axis1.lengthSq() > 1e-10
      ? new THREE.Quaternion().setFromAxisAngle(axis1.normalize().applyQuaternion(qa), ac_at_0) : new THREE.Quaternion();
    a.quaternion.multiply(exact ? r2.multiply(r0) : r0.multiply(r2));
    b.quaternion.multiply(r1);
    a.updateWorldMatrix(false, true);
  }

  _point(dt, now) {
    const p = this.pointing;
    const on = p.target && now < p.until && this.mode !== 'go';
    p.w += ((on ? 1 : 0) - p.w) * damp(on ? 0.12 : 0.2, dt);
    if (!p.side || p.w < 0.01) { if (!on) p.side = null; return; }
    const [up, lo, hand] = ARM[p.side].slice(1).map((n) => this.nodes[n]);
    if (!up || !lo || !hand) return;
    this.vrm.scene.updateMatrixWorld(true);
    const shoulder = this._world(up), elbow = this._world(lo), wrist = this._world(hand);
    const reach = shoulder.distanceTo(elbow) + elbow.distanceTo(wrist);
    const goal = this._lookPoint(this.targets[p.target]);
    const dir = goal.clone().sub(shoulder).normalize();
    const target = shoulder.clone().add(dir.multiplyScalar(reach * 0.97));
    this._twoBone(up, lo, hand, wrist.clone().lerp(target, p.w), new THREE.Vector3(0, -1, 0));
  }

  /** 伸手邀请牵手的手臂: 手伸到身前 (臂长的 0.86, 往身体中线收一点)、肩下面约 0.26 倍髋高处, 肘略弯、朝下; 手指朝着用户、
   *  掌心朝上 (标准化骨骼零位掌心朝 -Y), 按权重渐入渐出. 手指半张由手部动作层 (hand.js 的 offer 手形) 做. */
  _offer(dt, now) {
    const o = this.offer;
    const on = now < o.until && this.mode !== 'go' && this.mode !== 'turn';
    o.w += ((on ? 1 : 0) - o.w) * damp(on ? 0.18 : 0.25, dt);
    if (!o.side || o.w < 0.01) { if (now >= o.until) o.side = null; return; }  // 转身时先等着, 时间到了才算结束
    const [up, lo, hand] = ARM[o.side].slice(1).map((n) => this.nodes[n]);
    if (!up || !lo || !hand) return;
    this.vrm.scene.updateMatrixWorld(true);
    const H = this.hipHeight, y = new THREE.Vector3(0, 1, 0);
    const shoulder = this._world(up), elbow = this._world(lo), wrist = this._world(hand);
    const reach = shoulder.distanceTo(elbow) + elbow.distanceTo(wrist);
    const user = this.targets.user ? this._lookPoint(this.targets.user) : null;
    const fwd = new THREE.Vector3(Math.sin(this.char.yaw), 0, Math.cos(this.char.yaw));
    const toUser = user ? user.clone().sub(shoulder).setY(0).normalize() : fwd.clone();
    if (toUser.dot(fwd) < 0.3) toUser.copy(fwd);  // 用户在身后或侧后方 (还没转过来): 先往前伸
    const mid = new THREE.Vector3(this.char.x, shoulder.y, this.char.z);
    const inward = mid.sub(shoulder).setY(0);
    const target = shoulder.clone().addScaledVector(toUser, reach * 0.86).addScaledVector(y, -0.26 * H)
      .addScaledVector(inward, 0.15);
    const out = inward.clone().normalize().negate();
    const hint = new THREE.Vector3(0, -1, 0).addScaledVector(out, 0.4).addScaledVector(toUser, -0.3);
    this._armHinged(up, lo, hand, target, hint, o.w, o.side);
    // 手: 手指朝着用户略往上, 掌心朝上
    lo.updateWorldMatrix(true, true);
    const mp = this.vrm.humanoid.getNormalizedBoneNode(`${o.side}MiddleProximal`);
    if (!mp) return;
    const hq = hand.getWorldQuaternion(new THREE.Quaternion());
    const hw = this._world(hand), fing = this._world(mp).sub(hw).normalize();
    const palm = new THREE.Vector3(0, -1, 0).applyQuaternion(hq);
    const along = toUser.clone().addScaledVector(y, 0.15).normalize();
    const q1 = new THREE.Quaternion().setFromUnitVectors(fing, along);
    const p1 = palm.clone().applyQuaternion(q1);
    p1.addScaledVector(along, -p1.dot(along)).normalize();
    const palmUp = y.clone().addScaledVector(along, -along.dot(y)).normalize();
    const rot = new THREE.Quaternion().slerp(new THREE.Quaternion().setFromUnitVectors(p1, palmUp).multiply(q1), o.w);
    const parent = hand.parent.getWorldQuaternion(new THREE.Quaternion());
    hand.quaternion.copy(parent.invert().multiply(rot.multiply(hq)));
    hand.updateWorldMatrix(false, true);
  }

  /** 手臂按位置摆到 target (按权重 w 和动画混合): 肘朝 hint 那边, 上臂顺着弯曲平面拧好, 肘按铰链方向弯 (不反折).
   *  _twoBone 沿用动画里的弯曲平面; 动画里手臂在别的平面上 (转身以后那段待机) 时会解出反折的肘, 再被活动范围层扳走,
   *  伸手邀请牵手时手跑到了肩膀上面. 铰链轴和运动学约束的一样 (kinematics-core.js 的 HINGE: 右上臂局部 +Y, 左 -Y).
   *  返回解满 (权重 1) 时肘的世界位置. */
  _armHinged(up, lo, hand, target, hint, w, side) {
    const q0 = [up.quaternion.clone(), lo.quaternion.clone()];
    const S = this._world(up), E0 = this._world(lo), H0 = this._world(hand);
    const a = S.distanceTo(E0), b = E0.distanceTo(H0);
    const d = target.clone().sub(S), L = clamp(d.length(), 0.01, a + b - 0.002);
    d.normalize();
    const cosA = clamp((a * a + L * L - b * b) / (2 * a * L), -1, 1);
    const p = hint.clone().addScaledVector(d, -hint.dot(d));
    if (p.lengthSq() < 1e-8) p.set(0, -1, 0).addScaledVector(d, d.y);
    p.normalize();
    const E = S.clone().addScaledVector(d, a * cosA).addScaledVector(p, a * Math.sqrt(1 - cosA * cosA));
    const T = S.clone().addScaledVector(d, L);
    const aim = (node, pivot, cur, goal) => {
      const q = new THREE.Quaternion().setFromUnitVectors(cur.clone().sub(pivot).normalize(), goal.clone().sub(pivot).normalize());
      const pq = node.parent.getWorldQuaternion(new THREE.Quaternion()).invert();
      node.quaternion.copy(pq.multiply(q.multiply(node.getWorldQuaternion(new THREE.Quaternion()))));
      node.updateWorldMatrix(false, true);
    };
    aim(up, S, E0, E);
    // 上臂绕自身拧到铰链轴 = 弯曲平面的法线 (肘往前折时 (上臂 x 前臂) 和铰链轴同向)
    const u = E.clone().sub(S).normalize(), n = u.clone().cross(T.clone().sub(E)).normalize();
    const ax = new THREE.Vector3(0, side === 'left' ? -1 : 1, 0).applyQuaternion(up.getWorldQuaternion(new THREE.Quaternion()));
    ax.addScaledVector(u, -ax.dot(u));
    if (ax.lengthSq() > 1e-8 && n.lengthSq() > 0.5) {
      const tw = new THREE.Quaternion().setFromUnitVectors(ax.normalize(), n);
      const pq = up.parent.getWorldQuaternion(new THREE.Quaternion()).invert();
      up.quaternion.copy(pq.multiply(tw.multiply(up.getWorldQuaternion(new THREE.Quaternion()))));
      up.updateWorldMatrix(false, true);
    }
    aim(lo, this._world(lo), this._world(hand), T);
    up.quaternion.copy(q0[0].slerp(up.quaternion, w));
    lo.quaternion.copy(q0[1].slerp(lo.quaternion, w));
    up.updateWorldMatrix(false, true);
    return E;  // 解满 (权重 1) 时肘在哪
  }

  _look(dt, now) {
    const lk = this.look;
    let goalYaw = 0, goalPitch = 0, on = false;
    if (lk.target && now < lk.until && this.targets[lk.target]) {
      on = true;
      const head = this.nodes.head ? this._world(this.nodes.head) : new THREE.Vector3(this.char.x, 1.5, this.char.z);
      const p = this._lookPoint(this.targets[lk.target]);
      // 做表情动作时头最多转 25 度: 手按头放 (捂嘴、托腮、捧脸), 头转得太多手臂要横过胸前 (桌宠自检里托腮被推开 114 次)
      const yawMax = this.emoteSt && this.emoteSt.start !== null ? 0.45 : 1.4;
      goalYaw = clamp(wrapAngle(Math.atan2(p.x - head.x, p.z - head.z) - this.char.yaw), -yawMax, yawMax);
      goalPitch = clamp(Math.atan2(p.y - head.y, Math.hypot(p.x - head.x, p.z - head.z)), -0.6, 0.6);
    } else if (this.mode === 'go' && this.path) {
      // 走路时看向前方路线
      const ahead = pointOnPath(this.path, Math.min(this.pathLen, projectOnPath(this.path, [this.char.x, this.char.z]) + 1.5));
      goalYaw = clamp(wrapAngle(Math.atan2(ahead[0] - this.char.x, ahead[1] - this.char.z) - this.char.yaw), -0.8, 0.8) * 0.6;
      goalPitch = -0.15;
    } else if (lk.target) {
      lk.target = null;
    }
    const k = damp(0.15, dt);
    lk.yaw += (goalYaw - lk.yaw) * k;
    lk.pitch += (goalPitch - lk.pitch) * k;
    // 手在世界里定住时 (拿放、伸手, 见 _anchor) 胸口按定住的程度少转, 留给脖子、头、眼睛 (这一层在手臂 IK 之后, 胸口转了手就被带走:
    // 端着放到茶几上时掌心低 3 cm, 低头看地上的球时手被带开 11 cm. 试过转完以后把手再解回去: 手臂得伸直才够得着, 肘一帧甩 11 cm)
    const held = Math.max(0, ...Object.values(this._anchors || {}));
    const share = [['chest', 0.2 * (1 - held)], ['neck', 0.3], ['head', 0.35]];
    let applied = 0;
    if (Math.abs(lk.yaw) > 1e-3 || Math.abs(lk.pitch) > 1e-3) {
      this.vrm.scene.updateMatrixWorld(true);
      const up = new THREE.Vector3(0, 1, 0);
      const left = new THREE.Vector3(Math.cos(this.char.yaw), 0, -Math.sin(this.char.yaw));
      for (const [n, f] of share) {
        const node = this.nodes[n] || (n === 'chest' ? this.nodes.upperChest : null);
        if (!node) continue;
        const rot = new THREE.Quaternion().setFromAxisAngle(up, lk.yaw * f)
          .multiply(new THREE.Quaternion().setFromAxisAngle(left, -lk.pitch * f));
        const parentWorld = node.parent.getWorldQuaternion(new THREE.Quaternion());
        const world = node.getWorldQuaternion(new THREE.Quaternion());
        node.quaternion.copy(parentWorld.invert().multiply(rot.multiply(world)));
        node.updateWorldMatrix(false, true);
        applied += f;
      }
    }
    // 眼睛补上剩下的角度 (Soullink 的视线, 正值看角色自己的左边/上方)
    const engine = this.avatar.engine;
    if (on || Math.abs(lk.yaw) > 0.02) {
      engine.setGazeDegrees({ yaw: deg(lk.yaw * (1 - applied)), pitch: deg(lk.pitch * (1 - applied)) });
      lk.gaze = true;
    } else if (lk.gaze) {
      engine.releaseGazeControl();
      lk.gaze = false;
    }
  }
}
