// 时序状态估计与自适应平滑: 所有运动来源 (运动匹配、动作片段、待机库、HAST-SEC、脚部/手部 IK、接触层、头部)
// 写完之后、渲染之前, 对每根人形骨骼的最终姿态做一遍连续性处理.
//
// 1) 状态估计: 由滤波后的输出估计每根骨骼的角速度 (胯骨还有线速度).
// 2) 异常抑制: 一帧内要转的角度超过 "上一帧角速度 x dt + 最大角加速度 x dt^2" (且不超过最大角速度) 时
//    判为跳变 (来源切换、IK 翻转、状态机跳转), 这一帧只走允许的那一步, 剩下的在后面几帧以有限加速度追上.
//    真实的快动作 (挥手、转头) 是逐渐加速的, 不受影响.
// 3) 自适应平滑 (One Euro, 只用于上半身): 低速时截止频率低, 抹掉求解器的细小抖动; 高速时截止频率高, 不拖慢动作.
//    腿和胯骨不做这一步: 平滑带来的十几毫秒滞后会让支撑脚打滑.
//
// 只用骨骼节点的 quaternion.{x,y,z,w} 和 position.{x,y,z}, 不依赖 three.js, 可以在 Node 里单测.

import { restorePlain, snapshotPlain } from './plain-state.js';

const LEG = ['leftUpperLeg', 'leftLowerLeg', 'leftFoot', 'leftToes', 'rightUpperLeg', 'rightLowerLeg', 'rightFoot', 'rightToes'];
const CORE = ['spine', 'chest', 'upperChest', 'neck'];
const ARM = ['leftShoulder', 'leftUpperArm', 'leftLowerArm', 'rightShoulder', 'rightUpperArm', 'rightLowerArm'];
const HAND = ['leftHand', 'rightHand'];
const FINGER = /Thumb|Index|Middle|Ring|Little/;
const SKIP = new Set(['leftEye', 'rightEye', 'jaw']);  // 眼睛和下巴归面部驱动
const FACTORY_KEEP = ['_factory', 'nodes', 'enabled'];

// 每组的最大角加速度 (rad/s^2)、最大角速度 (rad/s)、One Euro 参数 (最低截止频率 Hz, 速度系数; null = 不平滑)
// 手臂和手: 真人挥手 (CMU 动捕) 时上臂内外旋约 ±35 度、2-3 Hz, 峰值角加速度 150 rad/s^2 上下; 原来的 110 / 160
// 会把真实的挥手当成跳变压掉 (每帧都在抑制, 动作变钝). IK 翻转这类一帧几十度的跳变仍远超这个限度.
// 胯骨的旋转和腿一样不做平滑 (原来归在 core 里被平滑了): 腿部 IK 是按平滑前的胯骨解的, 胯骨再滞后一点,
// 两条腿跟着一起转, 踩住的脚就离开落点 (原地耸肩时脚底滑 140 mm; 走一圈的支撑脚打滑 240-470 mm, 不平滑后 125-160 mm)
export const LIMITS = {
  hips: { acc: 45, vmax: 9, euro: null },
  core: { acc: 45, vmax: 9, euro: [7, 0.4] },
  head: { acc: 140, vmax: 18, euro: [9, 0.6] },
  arm: { acc: 300, vmax: 22, euro: [8, 0.5] },
  hand: { acc: 400, vmax: 28, euro: [9, 0.6] },
  finger: { acc: 220, vmax: 35, euro: null },
  leg: { acc: 140, vmax: 24, euro: null },
};
export const HIPS_POS = { acc: 30, vmax: 4 };  // m/s^2, m/s

export function groupOf(name) {
  if (name === 'hips') return 'hips';
  if (LEG.includes(name)) return 'leg';
  if (CORE.includes(name)) return 'core';
  if (ARM.includes(name)) return 'arm';
  if (HAND.includes(name)) return 'hand';
  if (FINGER.test(name)) return 'finger';
  if (name === 'head') return 'head';
  return 'arm';
}

const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3];
/** 两个单位四元数之间的转角 (弧度, 0..pi). */
export function qangle(a, b) {
  return 2 * Math.acos(Math.min(1, Math.abs(dot(a, b))));
}
/** 球面插值, 走短弧. */
export function qslerp(a, b, t) {
  let d = dot(a, b);
  const s = d < 0 ? -1 : 1;
  d *= s;
  if (d > 0.9995) {
    const r = [0, 1, 2, 3].map((i) => a[i] + (s * b[i] - a[i]) * t);
    const n = Math.hypot(...r);
    return r.map((x) => x / n);
  }
  const th = Math.acos(d), sn = Math.sin(th);
  const wa = Math.sin((1 - t) * th) / sn, wb = (Math.sin(t * th) / sn) * s;
  return [0, 1, 2, 3].map((i) => a[i] * wa + b[i] * wb);
}
const alpha = (dt, cutoff) => { const tau = 1 / (2 * Math.PI * cutoff); return 1 / (1 + tau / dt); };

export class PoseFilter {
  /** nodes: {骨骼名: 节点} (标准化骨骼). */
  constructor(nodes, { enabled = true } = {}) {
    this.nodes = Object.fromEntries(Object.entries(nodes).filter(([n, v]) => v && !SKIP.has(n)));
    this.enabled = enabled;
    this.state = {};
    this.hips = null;
    this.stats = { frames: 0, suppressed: 0, rate: 0, maxLagDeg: 0, byBone: {} };
    this._factory = snapshotPlain(this, FACTORY_KEEP);  // 出厂状态 (执行评估复位时放回, 见 plain-state.js)
  }

  /** 放回出厂状态 (执行评估复位用; 比 reset 全: 统计也清掉). 开没开 (enabled) 是外面的设置, 不动. */
  restoreFactory() {
    if (this._factory) restorePlain(this, this._factory, FACTORY_KEEP);
  }

  reset() {
    this.state = {};
    this.hips = null;
  }

  /** 读节点当前 (各层写完后的) 姿态, 写回处理后的姿态. 返回这一帧被抑制的骨骼数. */
  apply(dt) {
    if (!this.enabled || !(dt > 0)) return 0;
    dt = Math.min(dt, 0.1);
    let hits = 0, lag = 0;
    for (const [name, node] of Object.entries(this.nodes)) {
      const q = node.quaternion;
      const raw = [q.x, q.y, q.z, q.w];
      const st = this.state[name];
      if (!st) { this.state[name] = { q: raw, w: 0, dx: 0, g: LIMITS[groupOf(name)] }; continue; }
      const theta = qangle(st.q, raw);
      if (theta < 1e-6) { st.w *= 0.5; continue; }
      const g = st.g;
      let step = theta;
      if (g.euro) {  // One Euro: 截止频率随 (低通后的) 原始角速度升高
        st.dx += (theta / dt - st.dx) * alpha(dt, 4);
        step = theta * alpha(dt, g.euro[0] + g.euro[1] * st.dx);
      }
      const allowed = Math.min(g.vmax * dt, st.w * dt + g.acc * dt * dt);
      if (step > allowed + 1e-4) {
        step = allowed;
        hits++;
        this.stats.byBone[name] = (this.stats.byBone[name] || 0) + 1;
      }
      const out = qslerp(st.q, raw, Math.min(1, step / theta));
      st.w = qangle(st.q, out) / dt;
      st.q = out;
      lag = Math.max(lag, theta - step);
      q.x = out[0]; q.y = out[1]; q.z = out[2]; q.w = out[3];
    }
    const hips = this.nodes.hips;
    if (hips) {  // 胯骨位置: 只做异常抑制 (坐下/站起时的高度修正、过渡切换)
      const p = hips.position, raw = [p.x, p.y, p.z];
      if (!this.hips) this.hips = { p: raw, v: 0 };
      else {
        const d = Math.hypot(raw[0] - this.hips.p[0], raw[1] - this.hips.p[1], raw[2] - this.hips.p[2]);
        const allowed = Math.min(HIPS_POS.vmax * dt, this.hips.v * dt + HIPS_POS.acc * dt * dt);
        const k = d > allowed + 1e-5 ? allowed / d : 1;
        if (k < 1) { hits++; this.stats.byBone.hipsPosition = (this.stats.byBone.hipsPosition || 0) + 1; }
        const out = this.hips.p.map((x, i) => x + (raw[i] - x) * k);
        this.hips.v = (d * k) / dt;
        this.hips.p = out;
        p.x = out[0]; p.y = out[1]; p.z = out[2];
      }
    }
    const s = this.stats;
    s.frames++;
    s.suppressed += hits;
    s.rate += ((hits > 0 ? 1 : 0) / dt - s.rate) * Math.min(1, dt / 2);  // 近 2 秒里每秒有几帧被抑制
    s.maxLagDeg = Math.max(s.maxLagDeg * Math.exp(-dt / 3), (lag * 180) / Math.PI);
    return hits;
  }
}
