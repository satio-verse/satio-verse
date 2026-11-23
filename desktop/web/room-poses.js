// 小屋里的程序姿势: 叠在行动层 (web/locomotion.js: 走路、坐下、待机都是动捕) 之上, 做动捕库里没有的动作 ——
// 躺下睡觉、喝东西 (手送到嘴边)、推家具、瑜伽 (举手伸展、侧弯、前屈、树式、盘腿冥想).
// 每个姿势说清楚管哪些骨骼: fk 给躯干/颈的局部角度 (度, 标准姿态的轴: +X 是她的左边, +Y 上, +Z 前), ik 给手脚的目标点
// (两骨骼 IK, 用行动层的 _twoBone), body 给整个身体在世界里的位置和朝向 (躺着、坐在地上时). 没管的骨骼照旧是行动层的.
// 每帧在行动层更新之后算一遍目标, 每根骨骼的输出按半衰期追过去, 再按总权重 (进出各约 0.5 秒) 混到行动层的姿态上:
// 换姿势、开始、结束都是平滑的. 之后照常经过防穿模、关节范围、时序平滑 (avatar-vrm.js).
// 上床、躺着 (own: true) 整个身体归这一层: 行动层底下还是"坐在床边" (坐姿片段循环、隔一阵换一段、手放腿上、脚的 IK 都在跑),
// 以前没列的骨骼 (肩、脚趾、上胸、头) 跟着它动, 手的 IK 也从它这一帧的姿态开始解, 睡着时胳膊、脚趾一直在抖 (2026-10-27 用户:
// "在床上睡觉的时候为什么会乱晃"). own 的姿势所有骨骼都算在管, 每帧从自己上一帧的结果开始摆, 没列的保持进来时的样子.
import * as THREE from 'three';
import { centerOfMass, SEGMENTS } from '/static/kinematics-core.js';
import { palmForObject } from '/static/grasp-transform.js';
import { sipExitWeight, SIP_EXIT_SECONDS } from './room-sip-exit.js';

const D = THREE.MathUtils.degToRad;
const BONES = ['hips', 'spine', 'chest', 'upperChest', 'neck', 'head', 'leftShoulder', 'rightShoulder',
  'leftUpperArm', 'leftLowerArm', 'leftHand', 'rightUpperArm', 'rightLowerArm', 'rightHand',
  'leftUpperLeg', 'leftLowerLeg', 'leftFoot', 'leftToes', 'rightUpperLeg', 'rightLowerLeg', 'rightFoot', 'rightToes'];
const CHAIN = {
  leftHand: ['leftUpperArm', 'leftLowerArm', 'leftHand'], rightHand: ['rightUpperArm', 'rightLowerArm', 'rightHand'],
  leftFoot: ['leftUpperLeg', 'leftLowerLeg', 'leftFoot'], rightFoot: ['rightUpperLeg', 'rightLowerLeg', 'rightFoot'],
};
const damp = (halflife, dt) => 1 - Math.exp((-Math.LN2 * dt) / Math.max(1e-4, halflife));
const UP = new THREE.Vector3(0, 1, 0);

// -- 姿势表 -------------------------------------------------------------------------------------
// c: 每帧的上下文 (见 PoseLayer._ctx): c.at(局部偏移) 把"她的身体坐标" (左, 上, 前, 相对脚下地面) 换成世界点;
// c.bone(name) 是骨骼此刻的世界位置; c.side 是 +1 (左手) / -1 (右手), 给拿东西那只手用.
const side = (c, s) => c.left.clone().multiplyScalar(s);
// The opted-in cup can sit between wrist and mouth while the wrist stays in front
// of the chest. PoseLayer blend, wrist ROM and contact still constrain the result.
const sipPalm = (c) => {
  const a = c.opts.sipExact === true && Number.isFinite(c.opts.sipPalmIn)
    ? Math.max(0, Math.min(Math.PI / 6, c.opts.sipPalmIn)) : 0;
  const n = side(c, -c.side);
  return a ? n.multiplyScalar(Math.cos(a)).addScaledVector(c.fwd, -Math.sin(a)) : n;
};
/** 顺着床躺 / 坐时她的左边 (乘 s): 床朝向 bed.yaw, 和她坐在床边时的朝向差 90 度, 不能用 c.left. */
const bedLeft = (c, s) => new THREE.Vector3(Math.cos(c.bed.yaw) * s, 0, -Math.sin(c.bed.yaw) * s);
/** 朝 yaw (弧度) 的身体, 再前倾 pitch 度 (负的是后仰). */
const yawPitch = (yaw, pitch) => new THREE.Quaternion().setFromAxisAngle(UP, yaw).multiply(new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), D(pitch)));
/** 床上的一点: 以骨盆 hips 为原点、朝 yaw, 往左 x 米、往前 z 米, 高度是床面. */
const bedPoint = (c, hips, yaw, x, z) => new THREE.Vector3(hips.x + Math.cos(yaw) * x * c.s + Math.sin(yaw) * z * c.s, c.bed.top + 0.02,
  hips.z - Math.sin(yaw) * x * c.s + Math.cos(yaw) * z * c.s);
export const POSES = {
  /** 举手伸展: 双手举过头顶, 稍微后仰看上面. */
  // 举过头顶的手 (伸展、侧弯、树式) 用 pole IK: 行动层的 IK 把垂着的胳膊甩到头顶时手肘方向会翻, 两条小臂在头顶交叉 (自检量出 5 cm).
  // 两只手隔开 24 cm (以前 14 cm)
  reach: { fk: { spine: [-4, 0, 0], chest: [-5, 0, 0], neck: [-12, 0, 0] },
    ik: { leftHand: (c) => ({ target: c.bone('head').addScaledVector(UP, 0.3).add(side(c, 0.12)), hint: side(c, 1).addScaledVector(c.fwd, -0.3), pole: true }),
      rightHand: (c) => ({ target: c.bone('head').addScaledVector(UP, 0.3).add(side(c, -0.12)), hint: side(c, -1).addScaledVector(c.fwd, -0.3), pole: true }) } },
  /** 侧弯 (dir +1 往她的左边弯, -1 右边): 双手举过头顶, 跟着往那边倒. */
  // 两只手隔开 21 cm (以前 12 cm, 两条小臂在头顶交叉, 自检量出插进去 5 cm)
  bendL: { fk: { spine: [0, 0, -9], chest: [0, 0, -13], neck: [0, 0, -6] },
    ik: { leftHand: (c) => ({ target: c.bone('head').addScaledVector(UP, 0.24).add(side(c, 0.24)), hint: side(c, 1).addScaledVector(UP, 0.3), pole: true }),
      rightHand: (c) => ({ target: c.bone('head').addScaledVector(UP, 0.32).add(side(c, 0.03)), hint: side(c, -1).addScaledVector(UP, 0.6), pole: true }) } },
  bendR: { fk: { spine: [0, 0, 9], chest: [0, 0, 13], neck: [0, 0, 6] },
    ik: { leftHand: (c) => ({ target: c.bone('head').addScaledVector(UP, 0.32).add(side(c, -0.03)), hint: side(c, 1).addScaledVector(UP, 0.6), pole: true }),
      rightHand: (c) => ({ target: c.bone('head').addScaledVector(UP, 0.24).add(side(c, -0.24)), hint: side(c, -1).addScaledVector(UP, 0.3), pole: true }) } },
  /** 前屈: 上身往前折, 手垂到小腿前面. */
  fold: { fk: { spine: [42, 0, 0], chest: [30, 0, 0], neck: [8, 0, 0] }, halflife: 0.5,
    ik: { leftHand: (c) => ({ target: c.at(0.11, 0.3, 0.16), hint: c.fwd.clone().negate().addScaledVector(UP, 0.5) }),
      rightHand: (c) => ({ target: c.at(-0.11, 0.3, 0.16), hint: c.fwd.clone().negate().addScaledVector(UP, 0.5) }) } },
  /** 树式 (lift: 抬起的那条腿, 'right' / 'left'): 脚底贴在另一条腿的膝盖内侧, 双手合十举过头顶. */
  treeR: { fk: { chest: [-3, 0, 0] },
    // 先把支撑脚收到身体下面 (待机时两脚可能分得很开), 再抬另一条腿; 重心往支撑脚挪多少是练出来的 (hipShift)
    ik: { leftFoot: (c) => ({ target: c.at(0.065 * c.s, c.ankle, 0.02), hint: c.fwd.clone() }),
      rightFoot: (c) => ({ target: c.bone('leftLowerLeg').add(side(c, -0.075)).addScaledVector(UP, 0.03), hint: side(c, -1).addScaledVector(c.fwd, 0.35) }),
      leftHand: (c) => ({ target: c.bone('head').addScaledVector(UP, 0.36).add(side(c, 0.025)), hint: side(c, 1).addScaledVector(UP, 0.05), pole: true }),
      rightHand: (c) => ({ target: c.bone('head').addScaledVector(UP, 0.36).add(side(c, -0.025)), hint: side(c, -1).addScaledVector(UP, 0.05), pole: true }) } },
  treeL: { fk: { chest: [-3, 0, 0] },
    ik: { rightFoot: (c) => ({ target: c.at(-0.065 * c.s, c.ankle, 0.02), hint: c.fwd.clone() }),
      leftFoot: (c) => ({ target: c.bone('rightLowerLeg').add(side(c, 0.075)).addScaledVector(UP, 0.03), hint: side(c, 1).addScaledVector(c.fwd, 0.35) }),
      leftHand: (c) => ({ target: c.bone('head').addScaledVector(UP, 0.36).add(side(c, 0.025)), hint: side(c, 1).addScaledVector(UP, 0.05), pole: true }),
      rightHand: (c) => ({ target: c.bone('head').addScaledVector(UP, 0.36).add(side(c, -0.025)), hint: side(c, -1).addScaledVector(UP, 0.05), pole: true }) } },
  /** 盘腿冥想: 坐在垫子上 (髋部落到离地 hipY), 两只脚收在对侧膝盖下面, 手搭在膝盖上. */
  lotus: { halflife: 0.55, floor: true, breathe: 1,
    body: (c) => ({ pos: c.at(0, c.H * 0.2, -0.05), frame: new THREE.Quaternion().setFromAxisAngle(UP, c.yaw) }),
    fk: { spine: [6, 0, 0], chest: [-4, 0, 0], neck: [10, 0, 0] },
    // 膝盖往两边分开 (以前膝盖的方向偏前, 两条大腿交叉穿过去, 自检量出插进去 13 cm)
    // 两只脚错开高度: 左脚压低在后 (塞在右腿下), 右脚抬高在前, 交叉处一上一下 (同一高度时左脚插进右小腿, 自检量出 4 cm)
    ik: { leftFoot: (c) => ({ target: c.at(-0.07, 0.035, 0.25), hint: side(c, 1).addScaledVector(c.fwd, 0.2).addScaledVector(UP, 0.2), pole: true }),
      rightFoot: (c) => ({ target: c.at(0.08, 0.11, 0.35), hint: side(c, -1).addScaledVector(c.fwd, 0.2).addScaledVector(UP, 0.2), pole: true }),
      leftHand: (c) => ({ target: c.bone('leftLowerLeg').addScaledVector(UP, 0.07).addScaledVector(c.fwd, -0.04), hint: side(c, 1).addScaledVector(c.fwd, -0.5) }),
      rightHand: (c) => ({ target: c.bone('rightLowerLeg').addScaledVector(UP, 0.07).addScaledVector(c.fwd, -0.04), hint: side(c, -1).addScaledVector(c.fwd, -0.5) }) } },
  /** 盘腿起身前: 还坐在垫子上, 两只脚松开放到身体两边前面 (不交叉), 手撑在膝盖上. 从这里再站起来, 腿不会互相扫过. */
  uncross: { halflife: 0.3, floor: true,
    body: (c) => ({ pos: c.at(0, c.H * 0.22, -0.05), frame: new THREE.Quaternion().setFromAxisAngle(UP, c.yaw) }),
    fk: { spine: [10, 0, 0], chest: [-2, 0, 0], neck: [8, 0, 0] },
    ik: { leftFoot: (c) => ({ target: c.at(0.17, c.ankle, 0.36), hint: side(c, 1).addScaledVector(c.fwd, 0.6).addScaledVector(UP, 0.6), pole: true }),
      rightFoot: (c) => ({ target: c.at(-0.17, c.ankle, 0.36), hint: side(c, -1).addScaledVector(c.fwd, 0.6).addScaledVector(UP, 0.6), pole: true }),
      leftHand: (c) => ({ target: c.bone('leftLowerLeg').addScaledVector(UP, 0.06), hint: side(c, 1).addScaledVector(c.fwd, -0.5) }),
      rightHand: (c) => ({ target: c.bone('rightLowerLeg').addScaledVector(UP, 0.06), hint: side(c, -1).addScaledVector(c.fwd, -0.5) }) } },
  /** 躺在床上 (c.bed: 床面高度、头朝哪、躺在哪): 仰面, 腿伸直, 手放在身体两侧, 头枕在枕头上. */
  lie: { halflife: 0.6, breathe: 1.4, own: true,
    body: (c) => ({ pos: c.bed.hips, frame: new THREE.Quaternion().setFromAxisAngle(UP, c.bed.yaw).multiply(new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), -Math.PI / 2)) }),
    fk: { spine: [0, 0, 0], chest: [0, 0, 0], upperChest: [0, 0, 0], neck: [14, 0, 0], head: [4, 0, 0],
      leftUpperArm: [6, 0, -76], leftLowerArm: [0, -12, 0], leftHand: [0, 0, -6], rightUpperArm: [6, 0, 76], rightLowerArm: [0, 12, 0], rightHand: [0, 0, 6],
      leftUpperLeg: [-4, 0, 3], leftLowerLeg: [6, 0, 0], leftFoot: [22, 0, 0], rightUpperLeg: [-4, 0, -3], rightLowerLeg: [6, 0, 0], rightFoot: [22, 0, 0] } },
  /** 上床 (c.bed 见 sleep 脚本): 坐在床边, 两条腿收起来抬到床沿上 (骨盆还在原处, 稍微后仰). 先把手撑到床上, 再抬腿 (legDelay).
   *  撑床的手 clear: 换姿势的路上从大腿、骨盆里穿过去时挪出来 (见 _clearPath). */
  tuck: { halflife: 0.3, own: true, legDelay: 0.35,
    body: (c) => ({ pos: c.bed.edge, frame: yawPitch(c.bed.edgeYaw, -10) }),
    fk: { spine: [6, 0, 0], chest: [4, 0, 0], neck: [6, 0, 0],
      leftUpperLeg: [-112, 0, 6], leftLowerLeg: [108, 0, 0], leftFoot: [10, 0, 0], rightUpperLeg: [-112, 0, -6], rightLowerLeg: [108, 0, 0], rightFoot: [10, 0, 0] },
    ik: { leftHand: (c) => ({ target: bedPoint(c, c.bed.edge, c.bed.edgeYaw, 0.2, -0.12), hint: side(c, 1).addScaledVector(UP, 0.4), clear: true }),
      rightHand: (c) => ({ target: bedPoint(c, c.bed.edge, c.bed.edgeYaw, -0.2, -0.12), hint: side(c, -1).addScaledVector(UP, 0.4), clear: true }) } },
  /** 坐在床上: 腿伸直顺着床, 背朝枕头那头, 两只手撑在身体两边. 从床边转过来, 腿一起扫上床. */
  longSit: { halflife: 0.4, own: true,
    body: (c) => ({ pos: c.bed.sit, frame: yawPitch(c.bed.yaw, -4) }),
    fk: { spine: [4, 0, 0], chest: [2, 0, 0], neck: [8, 0, 0],
      leftUpperLeg: [-86, 0, 4], leftLowerLeg: [6, 0, 0], leftFoot: [20, 0, 0], rightUpperLeg: [-86, 0, -4], rightLowerLeg: [6, 0, 0], rightFoot: [20, 0, 0] },
    ik: { leftHand: (c) => ({ target: bedPoint(c, c.bed.sit, c.bed.yaw, 0.21, -0.1), hint: bedLeft(c, 1).addScaledVector(UP, 0.5), clear: true }),
      rightHand: (c) => ({ target: bedPoint(c, c.bed.sit, c.bed.yaw, -0.21, -0.1), hint: bedLeft(c, -1).addScaledVector(UP, 0.5), clear: true }) } },
  /** 喝 / 吃 (c.side: 拿东西的那只手): 手送到嘴边, 喝的时候头稍微后仰. */
  // 手在嘴前偏拿杯子那一侧 4.5 cm、手肘往外 (以前 2.5 cm, 换了肩窄的模型时喝的那条小臂和另一条交叉)
  sip: { halflife: 0.14, blend: 0.12, fk: { neck: [-10, 0, 0] },
    // opts.reach: 端着装液体的杯子时 room-liquid.js 按"杯沿 -> 真的嘴"的偏差一帧一帧修的手腕目标 (杯沿贴到嘴上才喝得到)
    ik: { hand: (c) => ({ target: c.opts.reach ? c.opts.reach.clone() : c.mouth.clone().addScaledVector(c.fwd, 0.07).add(side(c, 0.045 * c.side)).addScaledVector(UP, -0.05),
      hint: side(c, c.side * 1.4).addScaledVector(UP, -1).addScaledVector(c.fwd, -0.2), palm: sipPalm(c) }) } },
  bite: { halflife: 0.14, blend: 0.12, fk: { neck: [6, 0, 0] },
    ik: { hand: (c) => ({ target: c.mouth.clone().addScaledVector(c.fwd, 0.08).addScaledVector(UP, -0.02),
      hint: side(c, c.side).addScaledVector(UP, -1), palm: c.fwd.clone().negate() }) } },
  /** 蹲下够矮处 (c.target: 手要到的点, c.side: 哪只手): 骨盆往下、往前倾, 两只脚踩在原地, 一只手伸过去.
   *  小边桌、床头柜这种矮台面 (0.15-0.6 m) 挨着家具, 行动层的蹲下拿东西片段要站得太近, 走不过去时用这个. */
  crouch: { halflife: 0.3,
    body: (c) => ({ pos: c.at(0, c.H * (c.opts.drop ?? 0.64), c.opts.back ?? -0.05), frame: yawPitch(c.yaw, 16 + (c.opts.lean ?? 0)) }),
    fk: { spine: [20, 0, 0], chest: [14, 0, 0], neck: [-8, 0, 0] },
    ik: { leftFoot: (c) => ({ target: c.at(0.1, c.ankle, 0.03), hint: c.fwd.clone().addScaledVector(c.left, 0.25) }),
      rightFoot: (c) => ({ target: c.at(-0.1, c.ankle, 0.03), hint: c.fwd.clone().addScaledVector(c.left, -0.25) }),
      hand: (c) => ({ target: c.target, hint: side(c, c.side).addScaledVector(UP, -0.6).addScaledVector(c.fwd, -0.2) }) } },
  /** 摔了一跤: 往后坐到地上, 膝盖弯着, 两只手撑在身后. 很快 (半衰期 0.1 秒). */
  fallen: { halflife: 0.1, blend: 0.08, floor: true,
    body: (c) => ({ pos: c.at(0, c.H * 0.17, -0.32), frame: yawPitch(c.yaw, -18) }),
    fk: { spine: [10, 0, 0], chest: [6, 0, 0], neck: [12, 0, 0],
      leftUpperLeg: [-62, 0, 10], leftLowerLeg: [70, 0, 0], leftFoot: [-6, 0, 0], rightUpperLeg: [-55, 0, -8], rightLowerLeg: [58, 0, 0], rightFoot: [-6, 0, 0] },
    ik: { leftHand: (c) => ({ target: c.at(0.2, 0.03, -0.55), hint: side(c, 1).addScaledVector(c.fwd, -0.4) }),
      rightHand: (c) => ({ target: c.at(-0.2, 0.03, -0.55), hint: side(c, -1).addScaledVector(c.fwd, -0.4) }) } },
  /** 揉眼睛 (困的时候): 两只手的指节碰到眼睛, 来回揉, 头稍微低一点. */
  rubEyes: { halflife: 0.25, fk: { neck: [10, 0, 0] },
    ik: { leftHand: (c) => ({ target: c.eyes.clone().add(side(c, 0.035 + Math.sin(c.t * 9) * 0.008)).addScaledVector(UP, Math.cos(c.t * 9) * 0.006),
      hint: side(c, 1).addScaledVector(UP, -1), palm: c.fwd.clone() }),
      rightHand: (c) => ({ target: c.eyes.clone().add(side(c, -0.035 - Math.sin(c.t * 9) * 0.008)).addScaledVector(UP, Math.cos(c.t * 9 + 1) * 0.006),
        hint: side(c, -1).addScaledVector(UP, -1), palm: c.fwd.clone() }) } },
  /** 托腮 (坐在电脑前看你在做什么, c.side 那只手): 手背托着下巴, 胳膊肘往下撑, 头稍微歪一点. */
  chinRest: { halflife: 0.45, fk: { neck: [6, 0, 8], chest: [6, 0, 0] },
    ik: { hand: (c) => ({ target: c.mouth.clone().addScaledVector(UP, -0.06).addScaledVector(c.fwd, 0.05), hint: c.fwd.clone().addScaledVector(UP, -1),
      palm: UP.clone() }) } },
  /** 坐着, 左手把本子按在腿上 (本子跟着左手), 右手搭在本子边上, 低头看. */
  // item: 这只手拿着东西 (掌心朝下按着), 手往上挪到东西落在腿面上、不插进大腿 (见 _holdClear)
  diaryHold: { halflife: 0.45, fk: { neck: [22, 0, 0], chest: [8, 0, 0] },
    ik: { leftHand: (c) => ({ target: c.lap().add(side(c, 0.05)), hint: side(c, 1).addScaledVector(UP, -0.6), palm: UP.clone().negate(), item: UP }),
      rightHand: (c) => ({ target: c.lap().add(side(c, -0.12)).addScaledVector(UP, 0.03), hint: side(c, -1).addScaledVector(UP, -0.6), palm: UP.clone().negate(), clear: UP }) } },
  /** 写字: 左手按着本子, 右手 (握笔) 在本子上一行一行地小幅度画, 写完一行往下挪一点. */
  diaryWrite: { halflife: 0.18, fk: { neck: [26, 0, 0], chest: [10, 0, 0] },
    ik: { leftHand: (c) => ({ target: c.lap().add(side(c, 0.05)), hint: side(c, 1).addScaledVector(UP, -0.6), palm: UP.clone().negate(), item: UP }),
      rightHand: (c) => {
        const line = Math.floor(c.t / 1.6) % 5, x = ((c.t % 1.6) / 1.6) * 0.09;  // 每行 1.6 秒, 往右写 9 cm
        const t = c.lap().add(side(c, 0.0 - x)).addScaledVector(c.fwd, 0.04 - line * 0.018).addScaledVector(UP, 0.035 + Math.abs(Math.sin(c.t * 14)) * 0.006);
        return { target: t, hint: side(c, -1).addScaledVector(UP, -0.6), palm: c.fwd.clone().addScaledVector(UP, -0.8) };
      } } },
  /** 站着一只手扶着、拉着东西 (c.target: 手按在哪, 每帧可以挪; c.side: 哪只手): 拉椅子背. */
  hold: { halflife: 0.3, fk: { spine: [6, 0, 0], chest: [4, 0, 0] },
    ik: { hand: (c) => ({ target: c.target, hint: side(c, c.side).addScaledVector(UP, -0.7), palm: c.fwd.clone().addScaledVector(UP, -0.6) }) } },
  /** 推家具 (c.grip: 两只手按在哪): 两只手按着, 上身前倾. */
  push: { halflife: 0.3, fk: { spine: [10, 0, 0], chest: [8, 0, 0] },
    ik: { leftHand: (c) => ({ target: c.grip.left, hint: side(c, 1).addScaledVector(UP, -0.6) }),
      rightHand: (c) => ({ target: c.grip.right, hint: side(c, -1).addScaledVector(UP, -0.6) }) } },
};

const LIE_LEGS = Object.fromEntries(Object.entries(POSES.lie.fk).filter(([n]) => !/Arm|Hand/.test(n)));
/** 躺着, 两只手抓着被子边 (c.hands.left / right, 脚本每帧挪): 拉被子、推开被子. */
// clear: 手的目标先挪到防穿模层的身体碰撞体外面 (见 _clear)
POSES.lieHands = { ...POSES.lie, halflife: 0.25, fk: LIE_LEGS,
  ik: { leftHand: (c) => ({ target: c.hands.left, hint: bedLeft(c, 1).addScaledVector(UP, 0.6), clear: true }),
    rightHand: (c) => ({ target: c.hands.right, hint: bedLeft(c, -1).addScaledVector(UP, 0.6), clear: true }) } };
/** 躺着, 两只手放在肚子上 (c.hands 是放手的两点, 被子盖着; 从那一点往上挪到身体外面), 掌心朝下, 手指朝身体中间.
 *  (只定掌心时手指朝哪是随起点的, 指尖朝下插进骨盆、大腿, 防穿模层每帧推一下) */
POSES.lieRest = { ...POSES.lieHands, halflife: 0.5,
  ik: { leftHand: (c) => ({ target: c.hands.left, hint: bedLeft(c, 1).addScaledVector(UP, 0.6), clear: UP, palm: UP.clone().negate(), fingers: bedLeft(c, -1) }),
    rightHand: (c) => ({ target: c.hands.right, hint: bedLeft(c, -1).addScaledVector(UP, 0.6), clear: UP, palm: UP.clone().negate(), fingers: bedLeft(c, 1) }) } };
/** 躺着伸懒腰: 两只手举过头顶 (醒来时). */
POSES.lieStretch = { ...POSES.lie, halflife: 0.35, fk: { ...LIE_LEGS, neck: [4, 0, 0] },
  ik: { leftHand: (c) => ({ target: c.bone('head').add(bedLeft(c, 0.16)).add(new THREE.Vector3(-Math.sin(c.bed.yaw) * 0.32, 0.02, -Math.cos(c.bed.yaw) * 0.32)),
      hint: bedLeft(c, 1).addScaledVector(UP, 0.3) }),
    rightHand: (c) => ({ target: c.bone('head').add(bedLeft(c, -0.16)).add(new THREE.Vector3(-Math.sin(c.bed.yaw) * 0.32, 0.02, -Math.cos(c.bed.yaw) * 0.32)),
      hint: bedLeft(c, -1).addScaledVector(UP, 0.3) }) } };

/**
 * 姿势层. layer: 行动层 (用它的骨骼、IK 和角色位置). 用 set(名字, 参数) 换姿势, set(null) 回到行动层.
 * 参数: side ('left'/'right', 喝东西用哪只手), bed ({hips, yaw}), grip ({left, right}).
 */
export class PoseLayer {
  constructor(layer) {
    this.layer = layer;
    const h = layer.vrm.humanoid;
    this.nodes = Object.fromEntries(BONES.map((n) => [n, h.getNormalizedBoneNode(n)]).filter(([, b]) => b));
    this.v0 = layer.v0;
    // 动作的快慢: 平时比自检慢 1.6 倍 (用户说每个动作变得太快, 不像人); 自检 (fast=1) 照原来的速度
    this.tempo = new URLSearchParams(location.search).get('fast') === '1' ? 1 : 1.6;
    this.W = 0;
    this.wb = {};
    this.q = {};
    this.hips = null;
    this.pose = null;
    this.name = null;
    this.opts = {};
    this.t = 0;
    this._loco = {};
    this._goal = {};
  }

  set(name, opts = {}) {
    if (this.locked && performance.now() < this.locked) {  // 摔倒这种: 先做完, 期间别人要的姿势记下来, 完了再换
      this.after = [name, opts];
      return;
    }
    // Actual cup withdrawal keeps one owner until its finite handoff is complete.
    // Repeated cleanup must not restart the trajectory or forget the held side.
    if (!name && this._sipExit) return;
    const retiringSip = !name && this.name === 'sip' && this.opts.sipExact === true && this.W > 0
      && Object.values(this.layer.items || {}).some(i => i.held === (this.opts.side || 'right') && i.grip === 'cup' && !i.physical);
    const exit = retiringSip ? { elapsed: 0, duration: SIP_EXIT_SECONDS, side: this.opts.side || 'right', opts: this.opts,
      // Start from the last displayed, constrained local pose, not the unachieved IK goal.
      q: Object.fromEntries(Object.entries(this.nodes).filter(([n]) => (this.wb[n] || 0) > .001).map(([n, b]) => [n, b.quaternion.clone()])) } : null;
    this._sipExit = exit;
    if (this.pose) this._last = this.pose;
    if (name !== this.name) {
      this._shift = new THREE.Vector3();  // 换了姿势: 平衡从头找
      this._seedOf = null;  // own 的姿势从此刻显示的样子开始摆 (见 _main)
      this._fresh = this.W < 0.5;  // 从行动层 (没有姿势) 进来的: legDelay 时腿照行动层; 从别的姿势换过来的: 腿保持原样
    }
    this.name = name;
    this.pose = name ? POSES[name] : null;
    this.opts = exit ? exit.opts : opts;
    if (exit) this.W = 1;
    this.t = 0;
  }

  /** 强制做 seconds 秒 (摔倒), 期间 set() 先记着, 到时间换成最后要的那个. */
  lock(name, opts, seconds) {
    this.locked = 0;
    const before = [this.name, this.opts];
    this.set(name, opts);
    this.locked = performance.now() + seconds * 1000;
    this.after = before;
    clearTimeout(this._unlock);
    this._unlock = setTimeout(() => { this.locked = 0; const [n, o] = this.after || [null, {}]; this.after = null; this.set(n, o); }, seconds * 1000);
  }

  get exitingSip() { return !!this._sipExit; }
  get busy() { return !!this._sipExit || !!this.pose || this.W > 0.02; }
  /** 正在换姿势 (刚换的 2 秒内、进出的过渡): 页面升到 30 帧; 躺着、冥想不动时照空闲帧率. */
  get moving() { return !!this._sipExit || (!!this.pose && this.t < 2) || (this.W > 0.02 && this.W < 0.98); }

  /** 标准轴的局部角度 (度) -> 这个模型的标准化骨骼局部旋转 (VRM 0.x 绕 Y 转了半圈). */
  _fk([x, y, z]) {
    const q = new THREE.Quaternion().setFromEuler(new THREE.Euler(D(x), D(y), D(z), 'XYZ'));
    if (this.v0) { q.x = -q.x; q.z = -q.z; }
    return q;
  }

  _ctx() {
    const l = this.layer, c = l.char, yaw = c.yaw;
    const fwd = new THREE.Vector3(Math.sin(yaw), 0, Math.cos(yaw)), left = new THREE.Vector3(Math.cos(yaw), 0, -Math.sin(yaw));
    const head = this.nodes.head;
    const mouth = head ? head.getWorldPosition(new THREE.Vector3()) : new THREE.Vector3(c.x, 1.3, c.z);
    const hq = head ? head.getWorldQuaternion(new THREE.Quaternion()) : new THREE.Quaternion();
    const headFwd = new THREE.Vector3(0, 0, this.v0 ? -1 : 1).applyQuaternion(hq);
    const eyes = mouth.clone().addScaledVector(headFwd, 0.09).addScaledVector(UP, 0.05);
    mouth.addScaledVector(headFwd, 0.02).addScaledVector(UP, -0.06);
    return { yaw, fwd, left, H: l.hipHeight, s: l.hipHeight / 0.78, side: this.opts.side === 'left' ? 1 : -1, ankle: l.ankle?.left ?? 0.08,
      bed: this.opts.bed, grip: this.opts.grip, target: this.opts.target, hands: this.opts.hands, opts: this.opts, mouth, eyes, t: this.t,
      at: (x, y, z) => new THREE.Vector3(c.x, 0, c.z).addScaledVector(left, x).addScaledVector(UP, y).addScaledVector(fwd, z),
      // 坐着时腿上放本子的地方: 两条大腿中间、膝盖往里一点, 大腿面上面
      lap: () => {
        const a = (this.nodes.leftUpperLeg || l.nodes.leftUpperLeg).getWorldPosition(new THREE.Vector3());
        const b = (this.nodes.rightUpperLeg || l.nodes.rightUpperLeg).getWorldPosition(new THREE.Vector3());
        const ka = (this.nodes.leftLowerLeg || l.nodes.leftLowerLeg).getWorldPosition(new THREE.Vector3());
        const kb = (this.nodes.rightLowerLeg || l.nodes.rightLowerLeg).getWorldPosition(new THREE.Vector3());
        return a.add(b).multiplyScalar(0.5).lerp(ka.add(kb).multiplyScalar(0.5), 0.55).addScaledVector(UP, 0.1 * l.hipHeight / 0.78);
      },
      bone: (n) => (this.nodes[n] || l.nodes[n]).getWorldPosition(new THREE.Vector3()) };
  }

  /** 行动层每帧更新之后调. */
  update(dt) {
    this._main(dt);
    this._carry(dt);
    // 姿势在摆的手 (喝、吃、伸手...): 告诉行动层的端稳别抢 (locomotion.js 的 _carrySteady 在落地之后再摆一次端着的手)
    const ik = (this.W > 0.05 && this.pose?.ik) || {}, own = this.opts.side || 'right';
    this.layer.carryYield = { left: this._sipExit?.side === 'left' || !!(ik.leftHand || (ik.hand && own === 'left')),
      right: this._sipExit?.side === 'right' || !!(ik.rightHand || (ik.hand && own === 'right')) };
  }

  /**
   * 端着杯子 (握着的是 cup 一类, 这只手没有别的姿势在管、没在拿放伸手): 手在腰前, 小臂差不多平着, 掌心朝里, 杯子竖着.
   * 行动层原来按"书"夹在身侧, 走路时杯子跟着胳膊甩 (用户说咖啡都要洒了). 坐着时端在肚子前面.
   */
  _carry(dt) {
    const L = this.layer, nodes = this.nodes;
    this.cw ??= { left: 0, right: 0 };
    for (const lr of ['left', 'right']) {
      const it = Object.values(L.items).find((i) => i.held === lr);
      if (this._sipExit?.side === lr) { this.cw[lr] = 0; continue; }
      const ik = this.pose?.ik || {}, mine = ik[`${lr}Hand`] || (ik.hand && (this.opts.side || 'right') === lr);
      const busy = L.reaching?.side === lr || L.action?.grab?.side === lr || (L.action && !L.action.loop && !L.seated);
      const goal = it?.grip === 'cup' && !mine && !busy ? 1 : 0;
      this.cw[lr] += (goal - this.cw[lr]) * damp((goal ? 0.25 : 0.15) * this.tempo, dt);
      const w = this.cw[lr];
      if (w < 0.005) continue;
      const chain = CHAIN[`${lr}Hand`].map((n) => nodes[n]);
      if (chain.some((b) => !b)) continue;
      const c = this._ctx(), sgn = lr === 'left' ? 1 : -1;
      const target = L.seated ? c.bone('hips').addScaledVector(UP, 0.2 * c.s).add(side(c, sgn * 0.13 * c.s)).addScaledVector(c.fwd, 0.22 * c.s)
        : c.at(sgn * 0.15 * c.s, c.H * 1.18, 0.24 * c.s);
      const before = chain.map((b) => b.quaternion.clone());
      L._twoBone(chain[0], chain[1], chain[2], target, side(c, sgn).addScaledVector(UP, -0.8).addScaledVector(c.fwd, -0.3));
      this._palm(chain[2], side(c, -sgn).addScaledVector(c.fwd, -0.25));
      const after = chain.map((b) => b.quaternion.clone());
      chain.forEach((b, i) => b.quaternion.copy(before[i]).slerp(after[i], w));
      chain[0].updateWorldMatrix(false, true);
    }
  }

  _main(dt) {
    const want = this.pose ? 1 : 0;
    // 进出的快慢: 姿势自己可以定 (blend, 喝一口这种短动作快一些); 结束时按刚才那个姿势的
    const blend = (this.pose || this._last)?.blend;
    const exit = this._sipExit;
    if (exit) {
      exit.elapsed += Math.max(0, dt);
      this.W = sipExitWeight(exit.elapsed, exit.duration);
    } else this.W += (want - this.W) * damp((blend ?? (want ? 0.22 : 0.3)) * this.tempo, dt);
    if (!this.pose && (exit ? this.W === 0 : this.W < 0.002)) {
      if (exit) { this._sipExit = null; this.opts = {}; }
      this.W = 0;
      this.q = {};
      this.wb = {};
      this.hips = null;
      this.bodyW = null;
      return;
    }
    this.t += dt;
    const vrm = this.layer.vrm, nodes = this.nodes;
    // 行动层这一帧的姿态 (混合的起点)
    for (const [n, b] of Object.entries(nodes)) (this._loco[n] ??= new THREE.Quaternion()).copy(b.quaternion);
    const locoHips = nodes.hips.position.clone();
    const pose = this.pose, controlled = new Set();
    const k = damp((pose?.halflife ?? 0.35) * this.tempo, dt);
    let hipsGoal = locoHips, bodyHeld = false;
    if (!pose?.body) this.bodyW = null;
    if (pose) {
      if (pose.own) {
        // 起点: 同一个姿势接着上一帧的目标; 刚换过来从此刻显示的样子 (追到一半的输出) 开始. 行动层这一帧怎么动都不进来
        const same = this._seedOf === this.name;
        for (const [n, b] of Object.entries(nodes)) {
          if (n === 'hips') continue;
          const seed = same ? this._goal[n] : this.q[n];
          if (seed) b.quaternion.copy(seed);
          controlled.add(n);
        }
        this._seedOf = this.name;
      }
      // 目标: 先摆躯干和身体位置, 再按摆好的身体解手脚的 IK
      if (pose.body) {
        vrm.scene.updateMatrixWorld(true);
        const { pos, frame } = pose.body(this._ctx());
        const parent = nodes.hips.parent;
        const pw = parent.getWorldQuaternion(new THREE.Quaternion());
        const fix = this.v0 ? new THREE.Quaternion().setFromAxisAngle(UP, Math.PI) : new THREE.Quaternion();
        // 身体在世界里追目标 (以前在根的局部坐标里追: 行动层的根在底下漂、转, 躺着的人跟着在床上晃; 睡着 40 秒根走了 3 m、转了 23 度).
        // 刚进来从此刻显示的骨盆开始
        if (!this.bodyW) {
          const lq = this.q.hips && this.wb.hips ? this._loco.hips.clone().slerp(this.q.hips, this.W * this.wb.hips) : this._loco.hips;
          const lp = this.hips && this.wb.hips ? locoHips.clone().lerp(this.hips, this.W * this.wb.hips) : locoHips;
          this.bodyW = { pos: parent.localToWorld(lp.clone()), q: pw.clone().multiply(lq) };
        }
        this.bodyW.pos.lerp(pos, k);
        this.bodyW.q.slerp(frame.clone().multiply(fix), k);
        nodes.hips.quaternion.copy(pw.invert().multiply(this.bodyW.q));
        hipsGoal = parent.worldToLocal(this.bodyW.pos.clone());
        nodes.hips.position.copy(hipsGoal);
        controlled.add('hips');
        bodyHeld = true;
      }
      const pinned = {};  // 挪重心时踩在地上的脚不跟着走: 记下脚在哪, 挪完用腿的 IK 放回去 (不然脚和质心一起挪, 等于没挪)
      const shift = this.opts.balance ? (this._shift ??= new THREE.Vector3()) : this.opts.hipShift;
      if (shift && !pose.body) {
        vrm.scene.updateMatrixWorld(true);
        for (const f of ['leftFoot', 'rightFoot']) if (nodes[f] && !(pose.ik || {})[f]) pinned[f] = nodes[f].getWorldPosition(new THREE.Vector3());
        const parent = nodes.hips.parent;
        const w = parent.localToWorld(locoHips.clone()).add(shift);
        hipsGoal = parent.worldToLocal(w);
        nodes.hips.position.copy(hipsGoal);
        controlled.add('hips');
      }
      for (const [n, e] of Object.entries(pose.fk || {})) {
        if (!nodes[n]) continue;
        const q = this._fk(e);
        if (pose.breathe && n === 'chest') q.multiply(this._fk([Math.sin(this.t * 1.35) * pose.breathe, 0, 0]));
        nodes[n].quaternion.copy(q);
        controlled.add(n);
      }
      vrm.scene.updateMatrixWorld(true);
      const fwdNow = new THREE.Vector3(Math.sin(this.layer.char.yaw), 0, Math.cos(this.layer.char.yaw));
      for (const [f, at] of Object.entries(pinned)) {
        const chain = CHAIN[f].map((n) => nodes[n]);
        this.layer._twoBone(chain[0], chain[1], chain[2], at, fwdNow);
        chain[0].updateWorldMatrix(false, true);
        CHAIN[f].forEach((n) => controlled.add(n));
      }
      const ctx = this._ctx();
      this._target = null;
      this._clearShown = [];
      for (const [key, fn] of Object.entries(pose.ik || {})) {
        const end = key === 'hand' ? `${this.opts.side || 'right'}Hand` : key;
        const chain = CHAIN[end]?.map((n) => nodes[n]);
        if (!chain || chain.some((b) => !b)) continue;
        if (this.name === 'diaryHold' && end === 'leftHand' && this._readGrasp(ctx)) {
          this._target = this.layer._handPos('left');
          chain[0].updateWorldMatrix(false, true);
          CHAIN[end].forEach((n) => controlled.add(n));
          continue;
        }
        const goal = fn(ctx), { hint, palm, pole } = goal;
        let target = goal.clear ? this._clear(goal.target, end, goal.clear) : goal.target;
        if (goal.clear && pose.body) this._clearShown.push({ end, chain, hint });
        this._end = end;
        const solve = () => {
          if (pole) this._poleIK(chain[0], chain[1], chain[2], target, hint);
          else if (this.name === 'sip' && this.opts.sipExact === true) this.layer._twoBone(chain[0], chain[1], chain[2], target, hint, true);
          else this.layer._twoBone(chain[0], chain[1], chain[2], target, hint);
          if (palm) this._palm(chain[2], palm);
        };
        solve();
        if (goal.item) {
          const d = this._holdClear(end, goal.item);
          if (d) { target = target.clone().add(d); solve(); }
        }
        this._target = target;
        if (palm && goal.fingers) this._fingers(chain[2], end, palm, goal.fingers);
        chain[0].updateWorldMatrix(false, true);
        CHAIN[end].forEach((n) => controlled.add(n));
      }
    }
    for (const [n, b] of Object.entries(nodes)) this._goal[n] = b.quaternion.clone();
    // 每根骨骼追目标 (没管的追回行动层), 再按权重混到行动层的姿态上. 身体 (body) 已经在世界里追过了, 骨盆直接用.
    // legDelay: 腿先保持现在的样子, 过这么多秒 (按 tempo) 再动, 手先挪: 上床先把手撑到床上再抬腿, 起床先把手挪开再把腿放下床
    // (一起动时抬起来、转过来的大腿从手里扫过去, 防穿模层推手). 从行动层进来时腿本来就是行动层的样子
    const legWait = pose?.legDelay && this.t < pose.legDelay * this.tempo;
    for (const [n, b] of Object.entries(nodes)) {
      if (exit?.q[n]) {
        this.q[n] = exit.q[n].clone(); this.wb[n] = 1;
        b.quaternion.copy(this._loco[n]).slerp(this.q[n], this.W);
        continue;
      }
      const hold = legWait && /Leg|Foot|Toes/.test(n);
      const on = controlled.has(n) && !(hold && this._fresh);
      this.wb[n] = (this.wb[n] ?? 0) + ((on ? 1 : 0) - (this.wb[n] ?? 0)) * damp((blend ?? (on ? 0.2 : 0.3)) * this.tempo, dt);
      const goal = hold && !this._fresh && this.q[n] ? this.q[n] : on ? this._goal[n] : this._loco[n];
      if (bodyHeld && n === 'hips') this.q[n] = goal.clone();
      else this.q[n] = this.q[n] ? this.q[n].slerp(goal, k) : this._loco[n].clone().slerp(goal, k);
      b.quaternion.copy(this._loco[n]).slerp(this.q[n], this.W * this.wb[n]);
    }
    this.hips = bodyHeld ? hipsGoal.clone() : this.hips ? this.hips.lerp(hipsGoal, k) : locoHips.clone().lerp(hipsGoal, k);
    nodes.hips.position.copy(locoHips).lerp(this.hips, this.W * (this.wb.hips ?? 0));
    vrm.scene.updateMatrixWorld(true);
    if (pose && this._clearShown?.length) this._clearPath(dt);
    else this._clearOff = null;
    if (pose && this.opts.balance) this._balance(dt);
    if (this._target) this.err = +nodes[this._end].getWorldPosition(new THREE.Vector3()).distanceTo(this._target).toFixed(3);  // 自检看: 手离目标多远
  }

  /**
   * 站着找平衡 (opts.balance = { feet: 踩在地上的脚, gain 多快纠正 (1/秒) }): 每帧量质心离支撑脚的中心多远,
   * 下一帧骨盆往那边挪一点 (积分, 最多 0.2 m). 人单腿站也是这样一直在微调; gain 太小纠正不过来会倒, 是练出来的.
   */
  _balance(dt) {
    const h = this.layer.vrm.humanoid, pos = {}, v = new THREE.Vector3();
    for (const [a, b] of SEGMENTS) for (const n of [a, b]) {
      if (n && !pos[n]) { const nd = h.getNormalizedBoneNode(n); if (nd) pos[n] = nd.getWorldPosition(v).toArray(); }
    }
    const com = centerOfMass(pos);
    const feet = (this.opts.balance.feet || ['left', 'right']).map((f) => {
      const a = pos[`${f}Foot`], t = pos[`${f}Toes`] || a;
      return a ? [(a[0] * 0.6 + t[0] * 0.4), (a[2] * 0.6 + t[2] * 0.4)] : null;
    }).filter(Boolean);
    if (!com || !feet.length) return;
    const cx = feet.reduce((x, f) => x + f[0], 0) / feet.length, cz = feet.reduce((x, f) => x + f[1], 0) / feet.length;
    const k = Math.min(1, (this.opts.balance.gain ?? 3) * dt) * this.W;
    this._shift.x += (cx - com[0]) * k;
    this._shift.z += (cz - com[2]) * k;
    if (this._shift.length() > 0.2) this._shift.setLength(0.2);
    this.balanceErr = Math.hypot(cx - com[0], cz - com[2]);
  }

  /**
   * 两骨骼 IK, 膝盖 (肘) 放在"根 - 目标 - 提示方向"那个平面里、朝提示那边 (pole: true 的姿势用).
   * 行动层的 _twoBone 在已经弯着时沿用当前的弯曲平面, 再把整条腿甩到目标那边: 盘腿这种把脚抬到胯那么高的,
   * 膝盖跟着甩到别处, 两条大腿交叉穿过去 (自检量出来 11-13 cm).
   */
  _poleIK(a, b, c, target, pole) {
    const W = (n) => n.getWorldPosition(new THREE.Vector3());
    const pa = W(a), pb = W(b), pc = W(c);
    const l1 = pa.distanceTo(pb), l2 = pb.distanceTo(pc);
    const to = target.clone().sub(pa), d = Math.min(Math.max(to.length(), Math.abs(l1 - l2) + 0.002), l1 + l2 - 0.002);
    const dir = to.normalize();
    const p = pole.clone().addScaledVector(dir, -pole.dot(dir));
    if (p.lengthSq() < 1e-8) p.copy(pb).sub(pa).addScaledVector(dir, -pb.clone().sub(pa).dot(dir));
    if (p.lengthSq() < 1e-8) return this.layer._twoBone(a, b, c, target, pole);
    p.normalize();
    const cos = Math.max(-1, Math.min(1, (l1 * l1 + d * d - l2 * l2) / (2 * l1 * d)));
    const knee = pa.clone().addScaledVector(dir, cos * l1).addScaledVector(p, Math.sqrt(1 - cos * cos) * l1);
    this._aim(a, pb.clone().sub(pa), knee.clone().sub(pa));
    this._aim(b, W(c).sub(W(b)), pa.clone().addScaledVector(dir, d).sub(W(b)));
  }

  /** 转动骨骼 bone, 让它下面的世界方向 from 转到 to (最小的转动). */
  _aim(bone, from, to) {
    if (from.lengthSq() < 1e-10 || to.lengthSq() < 1e-10) return;
    const q = new THREE.Quaternion().setFromUnitVectors(from.normalize(), to.normalize());
    const world = bone.getWorldQuaternion(new THREE.Quaternion());
    const parent = bone.parent.getWorldQuaternion(new THREE.Quaternion());
    bone.quaternion.copy(parent.invert().multiply(q.multiply(world)));
    bone.updateWorldMatrix(false, true);
  }

  /**
   * 手的目标挪到防穿模层 (web/contact.js) 的身体碰撞体外面, 半径按前臂和手里粗的那个再留 1 cm. 以前躺着放在肚子上的手按被子面定高度,
   * 这个模型躯干短, 那一点在被子上边以外, 量出来的是床面: 目标在身体里 3-9 cm, 防穿模层每帧往外推 (睡着 40 秒推了 1500 多次),
   * 推开的方向和肘的转角在几个候选之间来回换, 手一直在抖. 两层按同一个身体算, 就不打架了.
   */
  /** 这只手 (刚按目标解过) 拿着的东西插进身体时, 手沿 dir 要挪多少 (世界向量; 不用挪返回 null): 东西在掌心持物点 (行动层的 _palmHold),
   *  按行动层给防穿模层的大小 (heldRadius)、和防穿模层同一个身体算, 挪过去就不会被推. 写日记、捧着书和手机时本子在掌心下面,
   *  以前手按在腿面上, 本子压进大腿约 1 cm, 防穿模层一直推 (见 locomotion.js 的 heldRadius) */
  // A spine grip must turn the held OBJECT flat, not press the palm down and leave its page vertical.
  // This only computes a pose target; the existing PoseLayer blend/filter provides the transition.
  _readGrasp(ctx) {
    const L = this.layer, it = Object.values(L.items).find((i) => i.held === 'left' && i.graspBinding && !i.physical);
    if (!it || !L._graspAim) return false;
    const q = new THREE.Quaternion().setFromRotationMatrix(new THREE.Matrix4().makeBasis(
      ctx.left.clone(), ctx.fwd.clone().negate(), UP.clone()));
    const center = ctx.lap().addScaledVector(UP, it.graspHalfExtents?.[2] ?? 0.03);
    const palm = palmForObject({ position: center.toArray(), quaternion: q.toArray() }, it.graspBinding.pose);
    L._graspAim('left', palm, 1);
    const shift = this._holdClear('leftHand', UP);
    if (shift) {
      palm.position = palm.position.map((x, i) => x + shift.getComponent(i));
      L._graspAim('left', palm, 1);
    }
    return true;
  }

  _holdClear(end, dir) {
    const L = this.layer, ct = L.avatar?.contact, side = end.startsWith('left') ? 'left' : 'right';
    const it = Object.values(L.items).find((i) => i.held === side);
    if (!it || !ct?.clearOf || !L._palmHold || !L.heldRadius) return null;
    L.vrm.scene.updateMatrixWorld(true);
    const c = L._palmHold(it, side), d = ct.clearOf(c, L.heldRadius(it), dir).sub(c);
    return d.lengthSq() > 1e-8 ? d : null;
  }

  _clear(target, end, how) {
    const ct = this.layer.avatar?.contact, side = end.startsWith('left') ? 'left' : 'right';
    if (!ct?.clearOf) return target;
    const r = Math.max(this.layer.handRadius?.(side) ?? 0.025, this.layer.foreArmRadius?.(side) ?? 0) + 0.01;
    return ct.clearOf(target, r, how?.isVector3 ? how : null);  // how: true 沿推出方向, 给方向就只沿它挪 (从身体里往上, 落在肚子上)
  }

  /**
   * 换姿势的路上 (起床时腿从床上放下来、从床边转到床上坐): 混合出来的手会从大腿、骨盆里穿过去, 防穿模层每帧硬推 (两次瞄准, 推 10-20 cm),
   * 小臂一帧甩几百度. 标了 clear 的手 (body 姿势): 显示出来的手腕、指尖在防穿模层的身体碰撞体里时, 整只手沿最短的方向挪出来
   * (两骨 IK, 从显示的姿势解, 手的朝向不变). 只改这一帧的输出, 追的状态不动, 下一帧照样从追到哪算.
   */
  _clearPath(dt) {
    const ct = this.layer.avatar?.contact;
    if (!ct?.clearOf) return;
    this._clearOff ??= {};
    for (const { end, chain, hint } of this._clearShown) {
      const side = end.startsWith('left') ? 'left' : 'right';
      const rh = this.layer.handRadius?.(side) ?? 0.025, r = rh + 0.012;
      const wrist = chain[2].getWorldPosition(new THREE.Vector3()), elbow = chain[1].getWorldPosition(new THREE.Vector3());
      const mid = this.layer.vrm.humanoid.getNormalizedBoneNode(`${side}MiddleProximal`);
      const dir = (mid ? mid.getWorldPosition(new THREE.Vector3()).sub(wrist) : wrist.clone().sub(elbow)).normalize();
      const tip = wrist.clone().addScaledVector(dir, rh * 2.6);  // 防穿模层的指尖取样点 (contact.js 的 _fingerTip)
      const dW = ct.clearOf(wrist, r).sub(wrist), dT = ct.clearOf(tip, r).sub(tip);
      const d = dW.lengthSq() > dT.lengthSq() ? dW : dT;
      // 挪出去的量做时间平滑 (变大快、变小慢; 同行动层 _lap 的推开量): 碰撞体每帧在动, 推出的方向跟着变, 直接用的话目标一帧跳几厘米,
      // 两骨 IK 的解跟着翻 (起床时小臂一帧甩 2000 多度/秒). 剩下没挪出去的一点由防穿模层推
      const off = (this._clearOff[end] ??= new THREE.Vector3());
      off.lerp(d, damp(d.lengthSq() > off.lengthSq() ? 0.05 : 0.15, dt));
      if (off.lengthSq() < 1e-6) continue;
      const hq = chain[2].getWorldQuaternion(new THREE.Quaternion());
      this.layer._twoBone(chain[0], chain[1], chain[2], wrist.add(off), hint);
      chain[1].updateWorldMatrix(true, false);
      chain[2].quaternion.copy(chain[1].getWorldQuaternion(new THREE.Quaternion()).invert().multiply(hq));
      chain[0].updateWorldMatrix(false, true);
    }
  }

  /** 掌心朝向定好以后, 绕掌心的法线转手, 让手指 (手腕 -> 中指根) 朝 want (世界方向, 投到手掌平面上). */
  _fingers(hand, end, palm, want) {
    const mid = this.layer.vrm.humanoid.getNormalizedBoneNode(`${end.replace('Hand', '')}MiddleProximal`);
    if (!mid) return;
    hand.updateWorldMatrix(true, true);
    const n = palm.clone().normalize(), hw = hand.getWorldPosition(new THREE.Vector3());
    const f = mid.getWorldPosition(new THREE.Vector3()).sub(hw), w = want.clone();
    f.addScaledVector(n, -f.dot(n));
    w.addScaledVector(n, -w.dot(n));
    if (f.lengthSq() < 1e-10 || w.lengthSq() < 1e-10) return;
    const ang = Math.atan2(f.clone().cross(w).dot(n), f.dot(w));
    const rot = new THREE.Quaternion().setFromAxisAngle(n, ang).multiply(hand.getWorldQuaternion(new THREE.Quaternion()));
    hand.quaternion.copy(hand.parent.getWorldQuaternion(new THREE.Quaternion()).invert().multiply(rot));
    hand.updateWorldMatrix(false, true);
  }

  /** 手掌朝向: 标准化骨骼零位掌心朝 -Y, 转到 want (世界方向). */
  _palm(hand, want) {
    hand.updateWorldMatrix(true, false);
    const hq = hand.getWorldQuaternion(new THREE.Quaternion());
    const palm = new THREE.Vector3(0, -1, 0).applyQuaternion(hq);
    const rot = new THREE.Quaternion().setFromUnitVectors(palm, want.clone().normalize());
    const parent = hand.parent.getWorldQuaternion(new THREE.Quaternion());
    hand.quaternion.copy(parent.invert().multiply(rot.multiply(hq)));
  }
}
