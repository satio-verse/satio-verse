// 小屋里她的体感 (用户要的: 穿模、姿势不对要有负反馈 —— 痛觉; 姿态不对会摔倒、受伤; 这样越做越好).
// 每帧 (画完之后, 骨骼是最终的样子) 看身体哪里不对, 按程度累计成"疼" (慢慢消退):
//   - 碰到家具: 膝盖、小腿、脚、屁股、身子、头、胳膊肘插进家具的碰撞盒 (坐着的那张、躺着的床、正在推的家具不算;
//     桌子、小边桌、椅子只算台面那一层, 下面是空的). 手本来就要碰东西, 不算.
//   - 关节超出活动范围 (运动学监测 web/kinematics.js 的越界, 按超出多少算).
//   - 手臂插进身体 (防穿模层这一帧要推开).
//   - 自己穿模: 两条腿、两只脚、两条胳膊互相插进去, 胳膊插进腿、头 (各段当胶囊, 插进去 1.5 cm 以上才算; 贴着不算).
//     树式抬起的脚贴着另一条腿、盘腿时脚压在对侧膝盖下面本来就要碰, 放宽.
//   - 坐着时屁股陷进座面或者悬空 (髋部离座面的高度不对).
// 疼过了线她有反应: 皱眉、"哎呀, 膝盖撞到了", 记下哪里疼. 站着 (站定、蹲着、做瑜伽) 时质心出了支撑面 0.45 秒以上 -> 摔一跤:
// 往后坐到地上、手撑地, 疼, 过一会儿爬起来; 之后几分钟算受伤 (走得慢, 心情差一点).
// 一件事的每一步是一个回合 (episode), 结束时汇总成分数, 给练习 (room-practice.js) 调参数用.
// 外来的刺激 (stimulus, 2026-10-27 小窝的物理与后果 P0: 洒出来的热咖啡烫到手、腿, 喝太烫的烫嘴) 分几段:
//   即时 (烫一下: 皱眉、缩一下; 正端着东西就放慢、端稳, 本地马上做, 不等大模型) -> 持续 (烫伤的部位疼几分钟, 那只手握不稳, 见 grip)
//   -> 恢复 (到时候就好). 只是湿了记下来, 慢慢干. 摔跤伤到屁股也是同一套部位状态 (以前写死 4 分钟).
// 部位状态存在本机 (pet.room.body), 关了再开接着算.
import * as THREE from 'three';
import { centerOfMass, convexHull, footPoints, SEGMENTS, signedMargin } from '/static/kinematics-core.js';
import { createBodyPartBoxes } from '/static/body-collision-parts.js';

const PARTS = [  // 取样点: 名字、骨骼 (或两根骨骼的中点)、半径 (按髋高 0.78 m 换算)
  ['膝盖', 'leftLowerLeg', null, 0.06], ['膝盖', 'rightLowerLeg', null, 0.06],
  ['小腿', 'leftLowerLeg', 'leftFoot', 0.05], ['小腿', 'rightLowerLeg', 'rightFoot', 0.05],
  ['脚', 'leftFoot', null, 0.05], ['脚', 'rightFoot', null, 0.05],
  ['屁股', 'hips', null, 0.11], ['身子', 'chest', null, 0.11], ['头', 'head', null, 0.1],
  ['胳膊肘', 'leftLowerArm', null, 0.04], ['胳膊肘', 'rightLowerArm', null, 0.04],
];
const SLAB = { desk: 0.05, sidetable: 0.05, chair: 0.12 };  // 下面空着的家具: 只算台面往下这么厚
const SKIP = new Set(['plant', 'lamp', 'ball']);            // 软的、细的: 不算
const SOFT = { pillow: 0.05, bed: 0.05, armchair: 0.04 };   // 软的 (枕头、床沿、沙发): 陷进去这么深以内不算, 疼也只算一半
// 自己穿模用的胶囊: 名字、两端骨骼 (第二个是 null: 球; '+head' 是头的中心)、半径 (按髋高 0.78 m 换算)
const LIMB = {
  lThigh: ['左大腿', 'leftUpperLeg', 'leftLowerLeg', 0.06], rThigh: ['右大腿', 'rightUpperLeg', 'rightLowerLeg', 0.06],
  lShin: ['左小腿', 'leftLowerLeg', 'leftFoot', 0.048], rShin: ['右小腿', 'rightLowerLeg', 'rightFoot', 0.048],
  lFoot: ['左脚', 'leftFoot', 'leftToes', 0.038], rFoot: ['右脚', 'rightFoot', 'rightToes', 0.038],
  lUpper: ['左上臂', 'leftUpperArm', 'leftLowerArm', 0.038], rUpper: ['右上臂', 'rightUpperArm', 'rightLowerArm', 0.038],
  lFore: ['左小臂', 'leftLowerArm', 'leftHand', 0.032], rFore: ['右小臂', 'rightLowerArm', 'rightHand', 0.032],
  head: ['头', 'head', null, 0.085],
};
// 查哪几对 (不相邻的): 两条腿之间、两只胳膊之间、胳膊和腿、胳膊和头 (手可以碰头: 揉眼睛, 只算小臂)
const SELF_PAIRS = [['lThigh', 'rThigh'], ['lShin', 'rShin'], ['lFoot', 'rFoot'], ['lShin', 'rThigh'], ['rShin', 'lThigh'],
  ['lFoot', 'rShin'], ['rFoot', 'lShin'], ['lFoot', 'rThigh'], ['rFoot', 'lThigh'],
  ['lFore', 'rFore'], ['lUpper', 'rFore'], ['rUpper', 'lFore'], ['lFore', 'lShin'], ['rFore', 'rShin'], ['lFore', 'head'], ['rFore', 'head']];
const SELF_OK = { treeR: ['rFoot', 'rShin', 'lFore', 'rFore', 'head'], treeL: ['lFoot', 'lShin', 'lFore', 'rFore', 'head'],  // 树式双手合十举过头顶
  lotus: ['lFoot', 'rFoot', 'lShin', 'rShin'], uncross: ['lFoot', 'rFoot', 'lShin', 'rShin'] };  // 这些姿势里本来就贴着的段: 多放宽 3 cm
const FALL_S = 0.45;          // 失衡多久摔倒
const PAIN_AT = 0.6;          // 疼到多少有反应
const KEY = 'pet.room.body';  // 部位状态 (烫伤、摔伤、湿了) 存在本机
const damp = (halflife, dt) => 1 - Math.exp((-Math.LN2 * dt) / Math.max(1e-4, halflife));

/** 两条线段 (p1-q1, p2-q2) 之间最近的距离. */
function segDist(p1, q1, p2, q2) {
  const d1 = q1.clone().sub(p1), d2 = q2.clone().sub(p2), rr = p1.clone().sub(p2);
  const a = d1.dot(d1), e = d2.dot(d2), f = d2.dot(rr);
  let s = 0, t = 0;
  if (a < 1e-9 && e < 1e-9) return rr.length();
  if (a < 1e-9) t = Math.max(0, Math.min(1, f / e));
  else {
    const c = d1.dot(rr);
    if (e < 1e-9) s = Math.max(0, Math.min(1, -c / a));
    else {
      const b = d1.dot(d2), den = a * e - b * b;
      s = den > 1e-9 ? Math.max(0, Math.min(1, (b * f - c * e) / den)) : 0;
      t = (b * s + f) / e;
      if (t < 0) { t = 0; s = Math.max(0, Math.min(1, -c / a)); } else if (t > 1) { t = 1; s = Math.max(0, Math.min(1, (b - c) / a)); }
    }
  }
  return p1.clone().addScaledVector(d1, s).distanceTo(p2.clone().addScaledVector(d2, t));
}

/** 球 (中心 p, 半径 r) 插进竖直的盒子 (xz 范围, y 从 y0 到 y1) 多深; 不碰返回 0. */
function depthInBox(p, r, b) {
  const [cx, cz] = b.center, [sx, sz] = b.size;
  const x0 = cx - sx / 2, x1 = cx + sx / 2, z0 = cz - sz / 2, z1 = cz + sz / 2;
  const qx = Math.max(x0, Math.min(x1, p.x)), qy = Math.max(b.y0, Math.min(b.y1, p.y)), qz = Math.max(z0, Math.min(z1, p.z));
  const d = Math.hypot(p.x - qx, p.y - qy, p.z - qz);
  if (d > 0) return Math.max(0, r - d);
  return r + Math.min(p.x - x0, x1 - p.x, p.y - b.y0, b.y1 - p.y, p.z - z0, z1 - p.z);  // 中心已经在里面
}

export class Body {
  /** r: 小屋的各个部件 (行动层 layer、角色 v、姿势层 poses、场景 scene、挪家具 mover、需求 needs、status、log). */
  constructor(r) {
    this.r = r;
    this._partBoxes = createBodyPartBoxes(r.scene, SOFT);  // 导入房间的静态细部件只校验/换算一次
    this.pain = 0;
    this.where = null;          // 这一阵最疼的地方
    this.unstable = 0;          // 连续失衡了多久 (秒)
    this.fallen = false;
    this.injury = null;         // { part, until } 最重的那处伤 (老接口: 状态栏、提示词看它)
    this.parts = {};            // 部位 -> { kind: 'burn' | 'bruise' | 'wet', severity 0-1, until (墙上时间 ms), since }
    try { this.parts = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch { /* 存储不可用 */ }
    this._injury();
    this.cooldown = 0;
    this.ep = null;
    this.log = [];              // 最近的疼、摔 (自检看)
    this.totals = { pains: 0, falls: 0 };
    this._v = new THREE.Vector3();
    this._w = new THREE.Vector3();
    this._q = new THREE.Quaternion();
    this.still = 0;             // 站定 (没在走、没在播动作片段、姿势也摆好了) 多久了
    this.selfStats = {};        // 自己穿模: "姿势:哪两段" -> 多少帧、最深多少 (自检看)
  }

  get injured() { return !!this.injury && performance.now() < this.injury.until; }
  /** 受伤时走得慢. */
  get speed() { return this.injured ? 0.7 : 1; }

  /** 这只手 ('left' / 'right') 现在握得多稳 (0-1): 烫伤了的手握不稳 (刚体抓持 P2 用). */
  grip(side) {
    const p = this.parts[side === 'left' ? '左手' : '右手'];
    return p?.kind === 'burn' && Date.now() < p.until ? 1 - 0.6 * p.severity : 1;
  }

  /**
   * 外来的刺激. part: 中文部位 ('右手'、'左腿'、'嘴'...); id: 英文部位 (body-hit.js); effect: liquid.js 的 heatEffect
   * ({level: 'wet' | 'hot' | 'burn', severity, sore}). 正常扶着、握着不会走到这里 (只有洒出来的液体碰到身上才算).
   */
  stimulus({ part, kind = 'heat', ml = 0, temp = null, effect }) {
    const r = this.r, L = r.layer;
    if (!effect) return;
    this.log.push({ t: +(performance.now() / 1000).toFixed(1), kind: `${kind}:${effect.level}`, part, ml: +ml.toFixed(2), temp: temp && +temp.toFixed(0),
      severity: effect.severity, act: r.life?.current?.id || null });
    if (this.log.length > 30) this.log.shift();
    this._feel(part, effect.level);
    if (effect.level === 'wet') {
      if (!this.parts[part] || this.parts[part].kind === 'wet') this._mark(part, 'wet', 0, 600);
      return;
    }
    // 即时: 缩一下、皱眉; 正端着东西就放慢、端稳 (这一趟; 放下以后恢复), 本地马上做
    const held = Object.values(L.items).some((i) => i.held);
    if (held) {
      this._careReflex = true;
      L.carryCare = 1;
      if (L.mode === 'go') L.speedScale = Math.min(L.speedScale ?? 1, 0.7);
    }
    if (this.cooldown > 0 && effect.level === 'hot') return;
    if (effect.level === 'hot') {
      this.cooldown = 1.5;
      r.v.setEmotion('surprised', 0.8);
      r.status?.(`烫! (${part})`);
      setTimeout(() => r.v.setEmotion('neutral'), 1500);
      return;
    }
    // 烫伤: 疼 (走一遍喊疼), 部位记几分钟
    this._mark(part, 'burn', effect.severity, effect.sore);
    this.where = `${part}烫到了`;
    this.pain = Math.max(this.pain, PAIN_AT + 0.1);
    if (this.cooldown <= 0 && !this.fallen) this._ouch();
  }

  /** 外面的模块报她身上感觉到的 (用这个, 别直接调 _feel): 刚体那边 (room-rigid.js) 手里的东西在滑 (part '右手' / '左手', feel 'slip')、
   *  身上被东西碰了一下 ('bump'); 动作成长那边 (room-growth.js) 她自己的手抖了一下 ('jerk', 动作本身的毛病, 不是被碰的). */
  sense(part, feel) { this._feel(part, feel); }

  /** 身上的感觉 (给认知, 取走就清; 不是真值, 是她感觉到的): [{part: '右手'、'左腿'、'嘴'、'屁股'..., feel: 'wet' 湿 | 'hot' 烫一下 |
   *  'burn' 烫伤 | 'bruise' 摔疼了 | 'slip' 手里的东西在滑 | 'bump' 被碰了一下 | 'jerk' 手 (身上哪里) 自己抖了一下, t}]. 同一处同一种 1.5 秒内只记一次 (洒出来的一串小滴不刷屏). 要马上知道就设 onFeel(f). */
  feelings({ take = true } = {}) {
    this.felt ??= [];
    return take ? this.felt.splice(0) : this.felt.slice();
  }

  _feel(part, feel) {
    this.felt ??= [];
    const t = performance.now() / 1000, last = this.felt.findLast?.((f) => f.part === part && f.feel === feel);
    if (last && t - last.t < 1.5) return;
    const f = { part, feel, t: +t.toFixed(1) };
    this.felt.push(f);
    if (this.felt.length > 30) this.felt.shift();
    try { this.onFeel?.(f); } catch (err) { console.warn(err); }  // 设了 onFeel (认知那边) 也马上告诉它
  }

  /** 记一处部位状态 (取更重的), 存下来. */
  _mark(part, kind, severity, secs) {
    const now = Date.now(), old = this.parts[part];
    if (old && old.kind !== 'wet' && now < old.until && old.severity > severity) return;
    this.parts[part] = { kind, severity: +severity.toFixed(3), until: now + secs * 1000, since: now };
    this._injury();
    try { localStorage.setItem(KEY, JSON.stringify(this.parts)); } catch { /* 存不了就算了 */ }
  }

  /** 过期的部位状态去掉; 最重的伤 (烫伤、摔伤) 当 injury (老接口). */
  _injury() {
    const now = Date.now();
    for (const [k, p] of Object.entries(this.parts)) if (!(now < p.until)) delete this.parts[k];
    const worst = Object.entries(this.parts).filter(([, p]) => p.kind !== 'wet').sort((a, b) => b[1].severity - a[1].severity)[0];
    this.injury = worst ? { part: worst[0], kind: worst[1].kind, until: performance.now() + (worst[1].until - now) } : null;
  }

  /** 一步开始. label: 做什么 (练习按它记). */
  begin(label) {
    this.ep = { label, t: 0, frames: 0, pain: 0, falls: 0, pen: 0, penMax: 0, joints: 0, pushes: 0, imbalance: 0, seatErr: 0, seatFrames: 0, self: 0, parts: {} };  // pain: 这一步里疼的总量
    this._push0 = this.r.v.contact?.stats?.pushes ?? 0;
    return this.ep;
  }

  /** 一步结束: 汇总. 分数: 没摔、没疼、关节和座面都对就高; 每样不对扣分 (每帧的比例, 摔一次扣 2). */
  end(ok = true) {
    const e = this.ep;
    this.ep = null;
    if (!e) return null;
    const f = Math.max(1, e.frames);
    const seat = e.seatFrames ? e.seatErr / e.seatFrames : 0;
    const pushes = (this.r.v.contact?.stats?.pushes ?? 0) - this._push0;  // 防穿模层推手臂: 不算疼, 稍微扣一点
    const score = (ok ? 1 : 0) - e.falls * 2 - Math.min(1.5, e.pain * 0.4) - (e.pen / f) * 1.5 - (e.joints / f) * 0.6
      - (e.imbalance / f) * 0.8 - Math.min(1, seat * 12) - Math.min(0.3, (pushes / f) * 0.15) - (e.self / f) * 1.2;
    return { label: e.label, ok, score: +score.toFixed(3), seconds: +e.t.toFixed(1), falls: e.falls, pain: +e.pain.toFixed(2),
      pen_frames: e.pen, pen_max_cm: +(e.penMax * 100).toFixed(1), joint_frames: e.joints, imbalance_frames: e.imbalance,
      seat_err_cm: +(seat * 100).toFixed(1), arm_pushes: pushes, self_frames: e.self, parts: e.parts };
  }

  /** 家具的碰撞盒 (这一帧不算的除外). */
  _boxes() {
    const r = this.r, L = r.layer, out = [];
    const seat = L.seated?.seat || L.action?.seat?.info;  // 坐着的, 或者正在坐下 / 站起来的那张
    const lying = /^(lie|tuck|longSit)/.test(r.poses.name || '');
    const pushing = r.mover?.moving ? new Set([...(r.mover.cfg[r.mover.moving.key].objects || []), ...(r.mover.cfg[r.mover.moving.key].decor || [])]) : null;
    if (this._partBoxes) return this._partBoxes({ seat, lying, pushing, squeezing: r.life?.squeezing });
    for (const o of [...r.scene.objects, ...(r.scene.decor || [])]) {
      const b = o.box;
      if (!b || o.virtual || o.walkable || SKIP.has(o.kind) || pushing?.has(o.id)) continue;
      if (seat && o.seat && Math.hypot(o.seat.pos[0] - seat.pos[0], o.seat.pos[1] - seat.pos[1]) < 0.05) continue;  // 坐着的那张
      if ((lying || seat) && o.kind === 'bed' && (lying || Math.hypot(seat.pos[0] - b.center[0], seat.pos[1] - b.center[1]) < 1.6)) continue;
      const h = b.height ?? 0.8;
      // 侧着身子从窄缝挤过去时 (阅读椅前面): 擦着家具过去不算撞, 5 cm 以内都算贴着
      const soft = r.life?.squeezing ? Math.max(0.05, SOFT[o.kind] ?? 0) : SOFT[o.kind];
      out.push({ center: b.center, size: b.size, y0: b.base ?? (SLAB[o.kind] ? h - SLAB[o.kind] : 0), y1: h, id: o.id, name: o.name || o.id, soft });
    }
    return out;
  }

  /** 每帧 (画完之后). */
  tick(dt) {
    if (!(dt > 0)) return;
    const r = this.r, v = r.v, L = r.layer, h = v.vrm.humanoid, e = this.ep;
    const s = L.hipHeight / 0.78;
    let hurt = 0, worst = null, worstAmt = 0;
    const note = (part, amt) => { hurt += amt; if (amt > worstAmt) { worstAmt = amt; worst = part; } if (e) e.parts[part] = +((e.parts[part] || 0) + amt * dt).toFixed(2); };
    // 1. 碰到家具
    const boxes = this._boxes();
    let penFrame = 0;
    for (const [part, a, b, rad] of PARTS) {
      const na = h.getNormalizedBoneNode(a);
      if (!na) continue;
      const p = na.getWorldPosition(this._v);
      if (b) { const nb = h.getNormalizedBoneNode(b); if (nb) p.lerp(nb.getWorldPosition(this._w), 0.5); }
      if (a === 'head') p.add(this._w.set(0, 0.09 * s, 0).applyQuaternion(na.getWorldQuaternion(this._q)));  // 头骨在脖子顶上: 挪到头的中心
      for (const box of boxes) {
        const d = depthInBox(p, rad * s, box) - (box.soft ?? 0.015);  // 碰撞盒是粗的: 1.5 cm 以内算贴着
        if (d <= 0) continue;
        penFrame = Math.max(penFrame, d);
        note(`${part}撞到${box.name}`, Math.min(1, d / 0.05) * (box.soft ? 1 : 2));
      }
    }
    // 3. 自己穿模: 不相邻的两段胶囊插进去 1.5 cm 以上 (这个姿势本来就贴着的段放宽 3 cm).
    //    腿按这个模型的网格量: 小腿、脚用行动层的 legCapsules (小腿中心线挪到网格截面中心, 半径按网格; 脚按鞋底宽),
    //    大腿半径用防穿模层量的 (thighRadius); 量不到时按髋高比例 (LIMB). 以前都按比例, 腿细的模型 (silvercat) 量粗了 1-2 cm.
    //    每一对都记账 (以前只记每帧最深的那一对, 小腿交叉时盖住了大腿); 疼按最深的一对
    const seg = {};
    for (const [k, [, a, b, rad]] of Object.entries(LIMB)) {
      const na = h.getNormalizedBoneNode(a);
      if (!na) continue;
      const p = na.getWorldPosition(new THREE.Vector3());
      let q = b ? h.getNormalizedBoneNode(b)?.getWorldPosition(new THREE.Vector3()) : null;
      if (a === 'head') { p.add(this._w.set(0, 0.09 * s, 0).applyQuaternion(na.getWorldQuaternion(this._q))); q = p.clone(); }
      seg[k] = [p, q || p.clone(), rad * s];
    }
    const caps = L.legCapsules?.();
    for (const [side, sh, ft, th] of [['left', 'lShin', 'lFoot', 'lThigh'], ['right', 'rShin', 'rFoot', 'rThigh']]) {
      const c = caps?.[side];
      if (c) seg[sh] = [new THREE.Vector3(...c.a), new THREE.Vector3(...c.b), c.r];
      if (c?.foot) seg[ft] = [new THREE.Vector3(...c.foot.a), new THREE.Vector3(...c.foot.b), c.foot.r];
      const tr = L.thighRadius?.(side);
      if (seg[th] && tr > 0.02 && tr < 0.15) seg[th][2] = tr;
    }
    const loose = new Set(SELF_OK[r.poses.name] || []);
    let selfDepth = 0, selfPair = null;
    const hits = [];
    for (const [x, y] of SELF_PAIRS) {
      const A = seg[x], B = seg[y];
      if (!A || !B) continue;
      const d = A[2] + B[2] - segDist(A[0], A[1], B[0], B[1]) - 0.015 - (loose.has(x) || loose.has(y) ? 0.03 : 0);
      if (d <= 0) continue;
      const pair = `${LIMB[x][0]}插进${LIMB[y][0]}`;
      hits.push([pair, d]);
      if (d > selfDepth) { selfDepth = d; selfPair = pair; }
    }
    this.self = selfPair ? { pair: selfPair, cm: +(selfDepth * 100).toFixed(1) } : null;
    if (selfPair) {
      // 只在她自己的程序姿势 (瑜伽、躺下、喝东西…) 摆好以后算疼: 换姿势的那一下两只手会从对方身上扫过去; 走路、待机是动捕,
      // 两条腿擦过去是动捕本身的, 她练不了, 喊疼只会让她走着走着"哎呀, 左脚插进右脚" (自然运行时出过). 这些照样记账
      const P = r.poses;
      if (P.pose && P.t > 0.8 && P.W > 0.9) note(selfPair, Math.min(1, selfDepth / 0.04));
    }
    const at = +(performance.now() / 1000).toFixed(1);
    for (const [pair, d] of hits) {
      const st = (this.selfStats[`${r.poses.name || L.mode}:${pair}`] ??= { frames: 0, max_cm: 0, at: 0 });
      st.frames++;
      if (d * 100 > st.max_cm) { st.max_cm = +(d * 100).toFixed(1); st.at = at; st.act = r.life?.current?.id || null; }  // 最深那一帧: 页面时间 (秒)、在做哪件事
    }
    // 2. 关节超出范围 (超出 10% 范围算 1)
    const joints = v.kin?.now?.joints || [];
    let bad = 0;
    const flexible = r.poses.name === 'lotus' || r.poses.name === 'uncross';  // 盘腿 (和起身前松开腿): 瑜伽本来就要拉开胯, 不算疼
    for (const j of joints) if (j.violated && j.margin < -0.05 && !flexible) { bad++; note(JOINT[j.joint] || '关节', Math.min(1, -j.margin * 4) * 0.8); }
    // 4. 坐着、躺着: 屁股离座面 / 床面 (骨盆半径约 0.09 m). 座面按家具真实的高度 (练习时试的高度可能不一样)
    let seatErr = null;
    const bed = r.poses.opts?.bed, lying = bed && /^lie/.test(r.poses.name || '') && r.poses.W > 0.9;
    if ((L.seated && L.mode === 'seated' && !r.poses.pose && r.poses.W < 0.05) || lying) {
      const hips = h.getNormalizedBoneNode('hips').getWorldPosition(this._v);
      seatErr = hips.y - (lying ? bed.top + 0.085 * s : (r.seatTop?.(L.seated.seat) ?? L.seated.seat.height) + 0.09 * s);
      if (seatErr < -0.04) note(lying ? '腰陷进床里' : '屁股陷进座位里', Math.min(1, -seatErr / 0.08));
    }
    // 疼: 累计、消退
    this.pain += hurt * dt;
    this.pain *= 1 - damp(1.2, dt);
    if (worst) this.where = worst;
    // 5. 失衡: 站定 (没在走、没在播动作片段、地上的姿势也不算) 0.6 秒以后, 质心出了支撑面.
    //    支撑面自己算: 运动学监测按行动层的着地标记取脚, 不知道姿势层把一只脚抬起来了 (树式), 抬着的脚有时也被算进去
    const P = r.poses, k = v.kin?.now;
    const lifted = P.name === 'treeR' ? 'right' : P.name === 'treeL' ? 'left' : null;
    const pos = {};
    for (const [a, b] of SEGMENTS) for (const n of [a, b]) {
      if (n && !pos[n]) { const nd = h.getNormalizedBoneNode(n); if (nd) pos[n] = nd.getWorldPosition(this._w).toArray(); }
    }
    const pts = [];
    for (const f of ['left', 'right']) {
      const ankle = pos[`${f}Foot`], toe = pos[`${f}Toes`] || ankle;
      if (f !== lifted && ankle && ankle[1] < (L.ankle?.[f] ?? 0.1) + 0.03) pts.push(...footPoints(ankle, toe));
    }
    const com = centerOfMass(pos), hull = convexHull(pts);
    const margin = com && hull.length >= 3 ? signedMargin(hull, [com[0], com[2]]) : null;
    this.margin = margin;
    const settled = P.pose ? P.t > 0.8 && P.W > 0.9 && !P.pose.floor && !/^(lie|tuck|longSit|lotus|fallen)/.test(P.name) : P.W < 0.05;
    // 两只脚都踩稳了才判 (刚走到、换脚的那一下只有一只脚着地, 支撑面很小); 树式本来就单腿站
    const planted = ['left', 'right'].every((f) => L.feet?.[f]?.lock && L.feet[f].w > 0.5) || /^tree/.test(P.name || '');
    const standing = margin !== null && planted && !L.seated && !L.action && !L.reaching && L.mode === 'idle' && L.groundOn !== false && !this.fallen && settled;
    this.still = standing ? this.still + dt : 0;
    const off = this.still > 0.6 && margin < -0.02 * s;
    this.unstable = off ? this.unstable + dt : Math.max(0, this.unstable - dt * 2);
    if (e) {
      e.t += dt;
      e.frames++;
      e.pain += hurt * dt;
      if (penFrame > 0) { e.pen++; e.penMax = Math.max(e.penMax, penFrame); }
      if (bad) e.joints++;
      if (selfPair) e.self++;
      if (off) e.imbalance++;
      if (seatErr !== null) { e.seatErr += Math.abs(seatErr); e.seatFrames++; }
    }
    this.cooldown = Math.max(0, this.cooldown - dt);
    // 被烫以后端稳的那一趟: 东西放下了就恢复
    if (this._careReflex && !Object.values(L.items).some((i) => i.held)) { this._careReflex = false; L.carryCare = null; }
    if (this.injury && !this.injured) this._injury();
    if (!this.fallen && this.unstable > FALL_S && !/^(lie|tuck|longSit|lotus|fallen)/.test(r.poses.name || '')) this._fall();
    else if (this.pain > PAIN_AT && this.cooldown <= 0 && !this.fallen) {
      if (r.life?.sleeping) { this.cooldown = 4; this.pain *= 0.5; }  // 睡着了不喊 (练习照样记分)
      else this._ouch();
    }
  }

  _ouch() {
    const r = this.r, part = this.where || '身上';
    this.cooldown = 4;
    this.totals.pains++;
    this.log.push({ t: +(performance.now() / 1000).toFixed(1), kind: 'pain', part, during: this.ep?.label || r.life?.current?.id || null,
      act: r.life?.current?.id || null });
    if (this.log.length > 30) this.log.shift();
    r.v.setEmotion('sad', 0.9);
    r.v.headAction?.('shake', { amplitude: 0.25, repetitions: 1 });
    r.needs.add({ fun: -2 });
    r.status(`哎呀, ${part}…`);
    r.log(`小屋: 疼 (${part}, ${this.ep?.label || '-'})`);
    setTimeout(() => r.v.setEmotion('neutral'), 2500);
  }

  /** 摔一跤: 往后坐到地上、手撑地, 疼一下, 过一会儿爬起来. 正在做的事收尾 (hurry). */
  async _fall() {
    const r = this.r, L = r.layer, during = this.ep?.label || r.life?.current?.id || null;
    this.fallen = true;
    this.unstable = 0;
    this.still = 0;
    this.totals.falls++;
    if (this.ep) this.ep.falls++;
    this.log.push({ t: +(performance.now() / 1000).toFixed(1), kind: 'fall', during, act: r.life?.current?.id || null });
    if (this.log.length > 30) this.log.shift();
    this._mark('屁股', 'bruise', 0.5, 4 * 60);
    this._feel('屁股', 'bruise');
    r.log(`小屋: 摔倒了 (${during || '-'})`);
    r.status('哎呀! 她没站稳, 摔了一跤');
    r.v.setEmotion('surprised', 1);
    r.needs.add({ fun: -6, energy: -3 });
    if (r.life) r.life.hurry = true;
    L.groundOn = false;
    r.poses.lock('fallen', {}, 2.6);
    await new Promise((ok) => setTimeout(ok, 900));
    r.v.setEmotion('sad', 0.9);
    r.status('好疼…她揉了揉, 慢慢爬起来');
    await new Promise((ok) => setTimeout(ok, 2000));
    L.groundOn = true;
    this.fallen = false;
    setTimeout(() => r.v.setEmotion('neutral'), 1500);
  }
}

const JOINT = { leftLowerLeg: '左膝', rightLowerLeg: '右膝', leftLowerArm: '左手肘', rightLowerArm: '右手肘',
  leftUpperLeg: '左边胯', rightUpperLeg: '右边胯', neck: '脖子' };
