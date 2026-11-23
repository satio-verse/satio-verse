// 可失败的抓持 (小窝的物理与后果 P2, 见 docs/roadmap/room-physics.md): 手 (掌心此刻该在哪、朝哪、多快, 来自动画) 用有上限的力
// 拉着一件动态物件 (rigid.js). 受阻 (撞到桌沿、卡在家具里)、力气不够 (太重、手烫伤了握不紧) 时物件跟不上手: 先滑, 偏太远就脱手,
// 脱手的东西带着当时的速度继续飞、往下掉. 动画不再每帧把东西摆到掌心.
//
// 三件事分开记 (不能画等号):
//   requested 系统请求握住 (行动层说这只手拿着它);
//   fingers   手指看起来握住了 (手部动作层的握紧程度, 宿主给);
//   stable    实际稳住了 (离掌心不到 GRIP.stable、相对速度小).
//
// 手本身也不是瞬移的: 动画给的掌心目标偶尔一帧跳几厘米 (过渡、贴地修正开关), 真的手臂跟不了那么快. 这里先让"手"按临界阻尼的弹簧
// 跟着掌心目标 (ARM: 4 赫兹, 加速度最多 25 m/s², 真的手臂做得到的), 抓持再把东西拉向这只手.
// (试过和 P0 液体模型的替身一样限 6 m/s²: 放下的片段手往下甩、急停在台面上方, 手臂跟不上, 杯子落后十几厘米、1.7 m/s 砸到台面上
//  歪倒, 三个种子两个倒在桌上洒光. 动画本身的跳变、甩得太猛是动作那边的事, 物理按真的手臂跟)
// 朝向按握住那一刻东西和手的相对朝向保持 (拿起来不会在手里转半圈).
// 纯计算, 不依赖 three.js; 位置 [x, y, z]、四元数 [x, y, z, w].

export const GRIP = {
  version: 2,        // 改了行为就加一 (评测台的物理指纹): 1 第一版; 2 手臂滤波、握住时的相对朝向
  hz: 6,             // 位置弹簧 (赫兹, 临界阻尼)
  maxForce: 25,      // 一只手最多出多大的力 (牛顿, 含托住重力的那部分): 端一杯 0.7 kg 的满杯要 7 N 左右, 急停时 10 N
  rotHz: 4,          // 朝向: 角速度往目标拉 (赫兹)
  maxAngAcc: 80,     // 角加速度上限 (弧度/秒²): 手腕拧得动的
  stable: 0.015,     // 离掌心不到这么远 (米)、相对速度不到 stableV 算稳住了
  stableV: 0.25,
  engage: 0.25,      // 握住的那一刻东西离掌心的偏差在这么多秒内收到零
  slip: 0.04,        // 离掌心这么远算在滑
  drop: 0.09,        // 离掌心这么远就脱手; 或者滑了 dropAfter 秒还没回来
  dropAfter: 0.3,
  place: 0.6,        // 放下时 (松手但给了落点) 最多再拿着往落点放这么多秒, 放稳了就早松开
};

// 手臂: 手跟着动画的掌心目标 (临界阻尼, 赫兹; 加速度上限 米/秒²)
export const ARM = { hz: 4, aMax: 25 };

const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const len = (a) => Math.hypot(a[0], a[1], a[2]);
const qmul = (a, b) => [a[3] * b[0] + a[0] * b[3] + a[1] * b[2] - a[2] * b[1], a[3] * b[1] - a[0] * b[2] + a[1] * b[3] + a[2] * b[0],
  a[3] * b[2] + a[0] * b[1] - a[1] * b[0] + a[2] * b[3], a[3] * b[3] - a[0] * b[0] - a[1] * b[1] - a[2] * b[2]];
const qinv = (a) => [-a[0], -a[1], -a[2], a[3]];
const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const scale = (a, s) => [a[0] * s, a[1] * s, a[2] * s];

export class Grip {
  /** rigid: RigidWorld; id: 拿着的那件; side: 'left' / 'right'; strength: 0-1 (烫伤的手握不紧, 见 room-body.js 的 grip). */
  constructor(rigid, id, side, { strength = 1 } = {}) {
    Object.assign(this, { rigid, id, side, strength });
    // 握住的那一刻东西和掌心不一定正好重合 (拿起时差几厘米): 这段偏差在 GRIP.engage 秒内收到零, 收的时候不算滑
    const P = rigid.pose(id);
    this.offset = null;
    this.qOff = null;        // 东西相对手的朝向 (握住那一刻的)
    this.hand = null;        // 手实际在哪、多快 ({p, v}, 按 ARM 跟着掌心目标)
    this.engage = 0;
    this._p0 = P?.p || null;
    this.state = 'hold';     // hold | slip | drop
    this.requested = true;
    this.fingers = 0;
    this.stable = false;
    this.err = 0;
    this.slipT = 0;
    this.strained = 0;       // 这一帧力到上限的比例 (0-1, 平滑)
    this.stats = { maxErr: 0, strainedSteps: 0, steps: 0, slipSteps: 0 };
  }

  /**
   * 每一小步 h 秒. target: {p, q, v} 掌心此刻该在哪、朝哪、速度 (动画). 返回状态 'hold' | 'slip' | 'drop' (drop 以后不再出力).
   * 手先按 ARM 跟着掌心目标; 位置: 力 = m (ω² 偏差 + 2ω 相对速度) + 托住重力, 大小不超过 maxForce x strength.
   * 朝向: 角速度往 (手的朝向 x 握住时的相对朝向) 拉, 角加速度有上限.
   */
  step(h, target) {
    if (this.state === 'drop') return this.state;
    const r = this.rigid, P = r.pose(this.id);
    if (!P) { this.state = 'drop'; return this.state; }
    const m = r.mass(this.id), w = 2 * Math.PI * GRIP.hz;
    const hand = this._arm(h, target);
    this.offset ??= sub(this._p0 || P.p, hand.p);  // 第一步: 东西离手多远
    if (target.q) this.qOff ??= qmul(qinv(target.q), P.q);
    this.engage = Math.min(1, this.engage + h / GRIP.engage);
    const k = 1 - this.engage * this.engage * (3 - 2 * this.engage);
    const goal = add(hand.p, scale(this.offset, k));
    const e = sub(goal, P.p), ev = sub(hand.v, P.v);
    let F = [m * (w * w * e[0] + 2 * w * ev[0]), m * (w * w * e[1] + 2 * w * ev[1] + 9.81), m * (w * w * e[2] + 2 * w * ev[2])];  // 最后一项托住重力
    const Fmax = GRIP.maxForce * Math.max(0.05, this.strength), Fm = len(F);
    const over = Fm > Fmax;
    if (over) F = F.map((x) => (x * Fmax) / Fm);
    r.impulse(this.id, F.map((x) => x * h));
    // 朝向: 误差四元数 -> 轴角, 目标角速度; 角速度的变化有上限
    if (target.q) {
      let dq = qmul(qmul(target.q, this.qOff), qinv(P.q));
      if (dq[3] < 0) dq = dq.map((x) => -x);
      const s = Math.hypot(dq[0], dq[1], dq[2]), ang = 2 * Math.atan2(s, dq[3]);
      const axis = s > 1e-9 ? [dq[0] / s, dq[1] / s, dq[2] / s] : [0, 0, 0];
      const want = axis.map((a) => a * ang * 2 * Math.PI * GRIP.rotHz);
      const dw = sub(want, P.w), dm = len(dw), lim = GRIP.maxAngAcc * h;
      const step = dm > lim ? dw.map((x) => (x * lim) / dm) : dw;
      const it = r.items.get(this.id);
      it?.body.setAngvel({ x: P.w[0] + step[0], y: P.w[1] + step[1], z: P.w[2] + step[2] }, true);
    }
    // 状态
    this.err = len(e);
    const relV = len(ev);
    this.stable = this.err < GRIP.stable && relV < GRIP.stableV;
    this.strained += ((over ? 1 : 0) - this.strained) * 0.2;
    this.stats.steps++;
    if (over) this.stats.strainedSteps++;
    this.stats.maxErr = Math.max(this.stats.maxErr, this.err);
    if (this.err > GRIP.slip) { this.slipT += h; this.stats.slipSteps++; } else this.slipT = Math.max(0, this.slipT - h);
    if (this.err > GRIP.drop || this.slipT > GRIP.dropAfter) this.state = 'drop';
    else this.state = this.err > GRIP.slip ? 'slip' : 'hold';
    return this.state;
  }

  /**
   * 手跟着掌心目标走一小步 (临界阻尼的解析解, 阻尼按和目标的相对速度; 速度的变化有上限). 这一步里目标按 v 从上一步的位置走到这里
   * (偏差从上一步的目标算起, 和 liquid.js 一样; 按这一步的目标算会多出 v·h 的落后, 匀速时手一直落后几厘米). 第一步手就在目标上.
   */
  _arm(h, target) {
    const pv = target.v || [0, 0, 0];
    if (!this.hand) { this.hand = { p: [...target.p], v: [...pv], from: [...target.p] }; return this.hand; }
    const H = this.hand, w0 = 2 * Math.PI * ARM.hz, ex = Math.exp(-w0 * h);
    const e0 = sub(H.p, H.from), d0 = sub(H.v, pv), B = add(d0, scale(e0, w0));
    H.from = [...target.p];
    const e1 = scale(add(e0, scale(B, h)), ex), d1 = scale(sub(d0, scale(B, w0 * h)), ex);
    let vNew = add(pv, d1), dv = sub(vNew, H.v);
    const lim = ARM.aMax * h;
    if (len(dv) > lim) { dv = scale(dv, lim / len(dv)); vNew = add(H.v, dv); H.p = add(H.p, scale(add(H.v, vNew), h / 2)); }
    else H.p = add(target.p, e1);
    H.v = vNew;
    return H;
  }

  /** 给自检、评测看. */
  summary() {
    return { state: this.state, requested: this.requested, fingers: +this.fingers.toFixed(2), stable: this.stable, err: +this.err.toFixed(3),
      maxErr: +this.stats.maxErr.toFixed(3), strained: this.stats.steps ? +(this.stats.strainedSteps / this.stats.steps).toFixed(3) : 0,
      slipped: this.stats.slipSteps };
  }
}
