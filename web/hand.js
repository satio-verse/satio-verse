// 手部动作层: 手指的姿势. 动捕 (CMU、StayStill) 和 HAST-SEC 都没有手指, 以前只有拿着东西时按"球 / 书"弯一下,
// 其他时候是模型零位的平手 (五指伸直并拢), 看起来像手没有关节.
// 每帧按情境 (行动层的 handContext: 伸手去拿、松手、指向、手搭在腿上、挥手 / 耸肩 / 说话, 接触层的握紧程度) 给几种手形
// 配权重 (见 hand-shapes.js), 混合出每个关节的目标角度, 再平滑地转过去 (张开、握紧都是一个过程, 不跳);
// 不握东西、不指向时再叠一点很小、很慢的起伏, 手不会像冻住的.
// 写在标准化骨骼上 (零位是单位旋转): 左手手指沿 +X, 绕 Z 轴负转是往掌心弯, 绕 Y 轴负转是往拇指那侧张; 右手都反过来.
// 每帧整个写一遍 (不在上一帧上累加), 手指骨骼只有这一层写. 别的模块要特殊握法 (桌宠小屋的杯子、零食袋) 就设 hold[side]
// = 手形 {f, t} (格式见 hand-shapes.js), 握紧时用它代替按种类的握法; 不握了设回 null.
import * as THREE from 'three';
import { DOF, fingerDrift, targetAngles } from './hand-shapes.js';
import { restorePlain, snapshotPlain } from './plain-state.js';

const FINGERS = ['Index', 'Middle', 'Ring', 'Little'];
const RAD = Math.PI / 180;
const damp = (halflife, dt) => 1 - Math.exp((-Math.LN2 * dt) / Math.max(1e-4, halflife));

export class HandLayer {
  constructor(vrm) {
    this.h = vrm.humanoid;
    this.bones = {};
    for (const side of ['left', 'right']) {
      const n = (name) => this.h.getNormalizedBoneNode(`${side}${name}`);
      this.bones[side] = {
        fingers: FINGERS.map((f) => ['Proximal', 'Intermediate', 'Distal'].map((s) => n(`${f}${s}`))),
        thumb: ['Metacarpal', 'Proximal', 'Distal'].map((s) => n(`Thumb${s}`)),
      };
    }
    this.has = Object.values(this.bones).some((b) => b.fingers.some((f) => f.some(Boolean)));
    this._qa = new THREE.Quaternion();
    this._qb = new THREE.Quaternion();
    this._y = new THREE.Vector3(0, 1, 0);
    this._z = new THREE.Vector3(0, 0, 1);
    this.reset();
    this._factory = snapshotPlain(this, ['_factory']);  // 出厂状态 (执行评估复位时放回, 见 plain-state.js)
  }

  /** 放回出厂状态 (执行评估复位用; 比 reset 全: 手腕速度这些跟着跑出来的也清掉). */
  restoreFactory() {
    if (this._factory) restorePlain(this, this._factory, ['_factory']);
  }

  reset() {
    this.cur = { left: null, right: null };  // 每只手此刻的关节角度 (度, 见 hand-shapes.js 的顺序)
    this.hold = { left: null, right: null }; // 外面指定的握法 (见文件头)
    this.release = { left: 0, right: 0 };    // 松手以后还要张开多久 (秒)
    this.wasGrip = { left: 0, right: 0 };
    this.t = 0;
  }

  /** 手腕相对骨盆的速度 (米/秒, 平滑过), 每帧都量 (说话开始时已经是热的). */
  _wristSpeed(side, dt) {
    const hand = this.h.getNormalizedBoneNode(`${side}Hand`), hips = this.h.getNormalizedBoneNode('hips');
    if (!hand || !hips) return 0;
    const p = hand.getWorldPosition(this._p ??= new THREE.Vector3()).sub(hips.getWorldPosition(this._p2 ??= new THREE.Vector3()));
    const prev = (this.prevWrist ??= {})[side];
    const v = prev ? p.distanceTo(prev) / dt : 0;
    (this.prevWrist[side] ??= new THREE.Vector3()).copy(p);
    const s = (this.speed ??= { left: 0, right: 0 });
    s[side] += (Math.min(v, 3) - s[side]) * damp(0.06, dt);
    return s[side];
  }

  /** ctx: {left: {open, talk, point, lap, offer, clasp}, right: ...} (行动层 handContext); grip / kind: 接触层的握紧程度和握的是什么.
   *  说话时 (talk) 手指随手臂的快慢张合: 手腕动得快 (比划的发力段) 时张开, 停住时收回到微弯. 真人比划时手指在发力段伸开、
   *  停顿时放松; 以前说话时固定张开 25%, 手像僵着的. */
  update(dt, ctx = {}, grip = {}, kind = {}) {
    if (!this.has || !(dt > 0)) return;
    this.t += dt;
    for (const side of ['left', 'right']) {
      const c = ctx[side] || {}, g = grip[side] || 0;
      const sp = this._wristSpeed(side, dt);
      const talkOpen = c.talk ? 0.12 + 0.6 * Math.min(1, Math.max(0, (sp - 0.15) / 0.5)) ** 2 : 0;
      // 松手的那一下: 握紧程度从高往下掉时先张开 0.5 秒, 再回到放松
      if (g < this.wasGrip[side] - 1e-3 && this.wasGrip[side] > 0.5) this.release[side] = 0.5;
      this.wasGrip[side] = g;
      this.release[side] = Math.max(0, this.release[side] - dt);
      // shapes: 别的手形的权重 ({heart: 0.8} 之类, 表情动作用)
      const w = { open: Math.max(c.open || 0, talkOpen, this.release[side] > 0 ? 1 : 0), point: c.point || 0, lap: c.lap || 0,
        offer: c.offer || 0, clasp: c.clasp || 0, ...(c.shapes || {}) };
      const goal = targetAngles(w, g, this.hold[side] || kind[side]);
      const fixed = Math.min(1, w.point + (w.peace || 0) + (w.heart || 0));  // 指向、比耶、比心时手指不起伏
      const drift = fingerDrift(this.t, side).map((x) => x * (1 - g) * (1 - fixed));
      for (let i = 0; i < 4; i++) goal[i * 4] += drift[i];
      const cur = this.cur[side] ??= goal.slice();
      // 握紧快、松开慢一点
      for (let i = 0; i < DOF; i++) cur[i] += (goal[i] - cur[i]) * damp(goal[i] > cur[i] ? 0.08 : 0.12, dt);
      this._apply(side, cur);
    }
  }

  _apply(side, a) {
    const sgn = side === 'left' ? -1 : 1, { fingers, thumb } = this.bones[side];
    const rot = (axis, deg) => this._qa.setFromAxisAngle(axis, sgn * deg * RAD);
    fingers.forEach(([p, m, d], i) => {
      const [mcp, pip, dip, spread] = a.slice(i * 4, i * 4 + 4);
      if (p) p.quaternion.setFromAxisAngle(this._y, sgn * spread * RAD).multiply(rot(this._z, mcp));
      if (m) m.quaternion.copy(rot(this._z, pip));
      if (d) d.quaternion.copy(rot(this._z, dip));
    });
    const [add, flex, ip] = a.slice(16, 19);
    const [mc, pr, ds] = thumb;
    // 拇指: 往掌心收 (绕竖轴) 再往下弯; 第一节收得多、弯得少, 第二节反过来
    if (mc) mc.quaternion.setFromAxisAngle(this._y, -sgn * add * RAD).multiply(rot(this._z, flex * 0.5));
    if (pr) pr.quaternion.setFromAxisAngle(this._y, -sgn * add * 0.3 * RAD).multiply(rot(this._z, flex));
    if (ds) ds.quaternion.copy(rot(this._z, ip));
  }
}
