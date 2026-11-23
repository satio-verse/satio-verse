// 接触与碰撞层: 和运动来源无关 (HAST-SEC、运动匹配, 以后的生成模型都一样), 每帧在所有姿态写完之后、
// VRM 更新 (标准化骨骼 -> 原始骨骼、弹簧骨) 之前运行.
//
//   1. 身体碰撞体: 载入时从蒙皮网格量出骨盆、腹、胸 (椭圆截面) 和大腿、手臂各段的半径, 衣服厚度也算在内;
//      每帧按骨骼位置摆好.
//   2. 手臂避让: 先自动调整姿态 (肘绕"肩-手腕"轴转到不穿的角度, 手不动); 还穿着的部分 (肘、小臂、手、指尖,
//      以及手里拿着的东西插进躯干、骨盆或大腿) 再沿推出方向挪开, 用两次瞄准旋转改上臂和小臂.
//      只在穿进去时才动, 贴着身体 (手放在腿上) 是允许的.
//   3. 握持: 拿着东西的手弯曲手指, 按物体大小决定弯多少.
//   0. (最先) 按身体比例的手臂重定向: 动漫模型肩窄、胯宽, 动捕里真人垂下的手臂原样套上去, 手腕、指尖插进骨盆和大腿
//      (执行评估里手臂穿模的七八成是这一处). 载入时按这个模型的身体量出"手臂垂直下垂时至少要外展几度才碰不到";
//      每帧垂下的手臂要是会碰到身体, 就绕身体前后轴向外转到刚好不碰 (上限是这个角度). 手在身前没碰到时不动;
//      抬起的手臂、拿着东西、伸手、指向、坐着手放腿上时不加.
import * as THREE from 'three';
import { restorePlain, snapshotPlain } from './plain-state.js';

const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const damp = (halflife, dt) => 1 - Math.exp((-Math.LN2 * dt) / Math.max(1e-4, halflife));
const MARGIN = 0.008;  // 推出后留的缝 (米)
// 握持时各指节的弯曲角 (度): 球大, 手指张开包住; 书薄, 捏住

// 身体段: 端点骨骼 a、b, 量半径用的骨骼 (按顶点的主权重骨骼), 是否按椭圆截面 (左右宽、前后窄),
// skin: 只量皮肤材质 (大腿和骨盆: 裙子是软的, 手可以贴着裙子, 但不能插进腿里; 量上裙子手臂会被推得张开)
const SEGMENTS = [
  { id: 'pelvis', a: 'leftUpperLeg', b: 'rightUpperLeg', bones: ['hips'], skin: true, ends: true },
  { id: 'belly', a: 'hips', b: 'chest', bones: ['spine'], ellipse: true },
  { id: 'chest', a: 'chest', b: 'neck', bones: ['chest', 'upperChest'], ellipse: true },
  { id: 'leftThigh', a: 'leftUpperLeg', b: 'leftLowerLeg', bones: ['leftUpperLeg'], skin: true, tMin: 0.3 },
  { id: 'rightThigh', a: 'rightUpperLeg', b: 'rightLowerLeg', bones: ['rightUpperLeg'], skin: true, tMin: 0.3 },
];
// 大腿只量中段 (tMin 0.3): 大腿根被臀部包着, 臀部下半圈的皮肤主要跟大腿骨走, 一起量会把大腿量粗
// (silvercat revision_03 身体换成皮肤材质以后, 大腿从 5.8 cm 量成 9.1 cm, 坐下、拿放时手腕总被判成插进大腿)
const SKIN = /skin/i;
// 腹、胸的上限用的"身体"材质: 比 SKIN 宽 (miomio 的身体材质叫 "BODY.001"). 只用在腹、胸上: 手臂、骨盆、大腿也换成按它量的话,
// miomio 的手和小臂量粗了 6 mm、骨盆 9.5 -> 10.1 cm, 垂下的手又被骨盆推开、手肘越界 (执行评估里走路时越界 18 -> 201 帧)
const BODY = /skin|body|身体|肌/i;
const CLOTH = 0.02;  // 腹、胸按身体材质量出来后, 最多再放宽这么多给衣服 (米)
// 手臂也优先只量皮肤: 蓬松的袖子会把前臂量成 6 cm (和大腿一样粗), 手一放到腿上就被判成"插进去" 4-5 cm,
// 每帧都被推开, 下一帧动画又放回来, 来回打架. 袖子是软的, 可以贴着腿.
const LIMBS = [
  { id: 'upperArm', a: 'UpperArm', b: 'LowerArm', bones: ['UpperArm'], skin: true },
  { id: 'foreArm', a: 'LowerArm', b: 'Hand', bones: ['LowerArm'], skin: true },
  { id: 'hand', a: 'Hand', b: 'MiddleProximal', bones: ['Hand'], skin: true },
];
const LIMB_Q = 0.6;  // 手臂取 0.6 分位 (身体段 0.8)
const HANG_FWD = 5;  // 量最小外展角时手臂略向前 (度): 垂下的手臂一般不是正好竖直
// 可调 (自动调优的 contact.spread): 外展上限 = 最小外展角 x spread, 0 = 不修正. 两个模型上扫 0.75/1/1.25 差别很小, 1 略好.
// 试过"固定平移" (垂下的手臂一律外展 最小外展角 - 真人典型值 5 度): 穿模也降, 但放在身前交叠的手会被拉开,
// 痛觉和像真人分都比按需的差 (miomio 代价 0.946 对 0.930, wendy 0.953 对 0.944)
// fingerDir: 指尖取样点沿手实际的朝向 (腕 -> 中指根) 取 (1), 还是沿前臂延长 (0, 以前的做法, 对比用; 不参与调优).
// 手腕一弯两者就不一样: 坐着手顺着大腿放时, 沿前臂延长的点插进大腿, 手指其实贴在腿上面
// skirt: 垂着的手搭在裙子外面 (见 _skirt; 0 = 不管裙子, 以前的做法, 对比用; 不参与调优)
// skirtGap: 相邻两条裙子链隔开超过这么多弧度时中间当作没有裙子 (前面开口的裙子; 见 _skirtDepth)
// spreadMem: 手臂抬起来时记着垂下时的外展, 回来时先张开 (见 _spread; 0 = 以前那样从 0 再找, 对比用; 不参与调优)
export const CONTACT = { spread: 1, fingerDir: 1, skirt: 1, skirtGap: 1.4, spreadMem: 1 };
const SPREAD_MEM_MARGIN = 3;  // 度, 见 _spread
// 裙子 (2026-10-26, _skirt): 动捕按真人的细腿垂手, 套到裙子张开的模型上手垂在裙子圆锥里面 (silvercat 裙子在胯部张开约 20 cm,
// 手腕在裙子里 5 cm). 裙子的形状每帧由裙子弹簧链的关节位置给出 (跟着裙子摆), 加上载入时量的"网格在骨骼外面多远".
// 垂着的手腕、前臂下段在裙子里面时, 上臂绕肩往外转到手搭在裙子外面; 指尖还在里面 (裙子越往下越张开) 时手腕再往外弯 (最多 SKIRT_BEND),
// 手顺着裙子的斜面搭着, 不用把整条手臂张开. 允许压进去 SKIRT_PRESS (试过 1.2 cm: 手掌陷进裙子看得出来).
// 不算进"手臂被推开" (lastPush): 那一项量的是插进身体的修正, 裙子单独记在 stats.skirt
const SKIRT_PRESS = 0.004;
const SKIRT_BEND = (35 * Math.PI) / 180;
const SKIRT_ROOTS = /^(hips|leftUpperLeg|rightUpperLeg)$/;
// 半径上限 (米): 手腕上的袖口、荷叶边会把手量成 6 cm
const MAX_R = { hand: 0.035, foreArm: 0.05, upperArm: 0.06 };
const DEFAULT_R = { pelvis: 0.12, belly: 0.11, chest: 0.12, leftThigh: 0.075, rightThigh: 0.075, upperArm: 0.04, foreArm: 0.032, hand: 0.03 };

/** 点到线段: 距离、参数 t (0..1), 最近点. */
function segment(p, a, b) {
  const ab = b.clone().sub(a), t = clamp(p.clone().sub(a).dot(ab) / Math.max(1e-9, ab.lengthSq()), 0, 1);
  const q = a.clone().addScaledVector(ab, t);
  return { d: p.distanceTo(q), t, q };
}

/** 折线 pts (相对过髋竖直轴的点, 从上到下) 在高度 y 处离轴的水平距离; y 不在折线高度范围里时 null. */
function radialAt(pts, y) {
  for (let i = 0; i + 1 < pts.length; i++) {
    const a = pts[i], b = pts[i + 1];
    if (y > Math.max(a.y, b.y) || y < Math.min(a.y, b.y)) continue;
    const t = Math.abs(b.y - a.y) < 1e-6 ? 0 : (y - a.y) / (b.y - a.y);
    return Math.hypot(a.x + (b.x - a.x) * t, a.z + (b.z - a.z) * t);
  }
  return null;
}

function quantile(values, q) {
  if (!values.length) return null;
  const s = Float64Array.from(values).sort();
  return s[Math.min(s.length - 1, Math.floor(q * s.length))];
}

export class ContactLayer {
  constructor(vrm) {
    this.vrm = vrm;
    this.h = vrm.humanoid;
    this.grip = { left: 0, right: 0 };
    this.gripKind = { left: 'ball', right: 'ball' };
    this.stats = { pushes: 0, maxPen: 0, swivels: 0, lastPush: 0, by: {} };  // lastPush: 这一帧推出去的总距离 (米, 痛觉用)
    // by: 按"身体段:手臂取样点"累计的穿透深度 (米, 每帧第一轮), 看是哪里在穿模
    this.swivel = { left: 0, right: 0 };  // 肘绕"肩-手腕"轴的转角 (度), 帧间平滑
    this.radius = { ...DEFAULT_R };
    this.ellipse = {};
    this.endR = {};  // 两端 (轴线方向) 单独量的半径, 见 _measure 和 _push
    this.hang = { left: 0, right: 0 };     // 手臂垂直下垂 (略向前) 不碰身体的最小外展角 (度)
    this.spreadDeg = { left: 0, right: 0 }; // 手臂重定向修正: 这一帧外展了多少度 (帧间平滑)
    this.spreadMem = { left: 0, right: 0 }; // 手臂垂着时要外展多少度 (抬起来时记着, 见 _spread)
    try {
      this._measure();
      this.hang = this._hangAngles();
    } catch (err) {
      console.warn('接触层: 量身体半径失败, 用默认值', err);
    }
    this.skirt = [];                          // 裙子的弹簧链 (见 _skirtChains)
    this.skirtAng = { left: 0, right: 0 };    // 手搭到裙子外面: 上臂往外转了多少 (弧度, 帧间平滑)
    this.stats.skirt = { frames: 0, need: 0 };  // 手在裙子里面的帧数、要挪出来的距离之和 (米)
    try {
      this.skirt = this._skirtChains();
    } catch (err) {
      console.warn('接触层: 找裙子失败, 不管裙子', err);
    }
    this._factory = snapshotPlain(this, ['_factory']);  // 出厂状态 (执行评估复位时放回, 见 plain-state.js)
  }

  /** 放回出厂状态 (执行评估复位用): 平滑过的外展、肘的转角、外展记忆、统计都回到刚量完身体的样子. */
  restoreFactory() {
    if (this._factory) restorePlain(this, this._factory, ['_factory']);
  }

  /** 裙子的弹簧链: 根骨挂在髋、大腿下面的链 (从上到下的骨骼, 末端补上尾巴), 和网格在骨骼外面多远 (off).
   *  载入时 (标准姿态) 量: 主权重在这条链上的顶点, 到过髋竖直轴的水平距离减去同一高度骨骼折线的水平距离, 取 0.97 分位
   *  (最外面一层荷叶边也算上; 0.9 分位时手停在荷叶边里面).
   *  顶点太少的链 (飘带) 不要. */
  _skirtChains() {
    const sbm = this.vrm.springBoneManager;
    if (!sbm?.joints?.size) return [];
    const named = new Map(Object.keys(this.h.humanBones).map((n) => [this.h.getRawBoneNode(n), n]).filter(([b]) => b));
    const humanOf = (o) => { for (let p = o; p; p = p.parent) if (named.has(p)) return named.get(p); return null; };
    const joints = [...sbm.joints].filter((j) => SKIRT_ROOTS.test(humanOf(j.bone) || ''));
    const byBone = new Map(joints.map((j) => [j.bone, j]));
    const chains = [];
    for (const j of joints) {
      if (byBone.has(j.bone.parent)) continue;
      const bones = [];
      for (let cur = j; cur; cur = cur.child ? byBone.get(cur.child) : null) bones.push(cur.bone);
      const last = byBone.get(bones[bones.length - 1]);
      const tail = last.child && !byBone.has(last.child) ? last.child : null;
      chains.push({ bones: tail ? [...bones, tail] : bones, set: new Set(bones), off: 0, n: 0 });
    }
    if (!chains.length) return [];
    this.vrm.scene.updateMatrixWorld(true);
    const hips = this.h.getRawBoneNode('hips').getWorldPosition(new THREE.Vector3());
    const pts = chains.map((c) => c.bones.map((b) => b.getWorldPosition(new THREE.Vector3()).sub(hips)));
    const diffs = chains.map(() => []);
    const v = new THREE.Vector3();
    this.vrm.scene.traverse((o) => {
      if (!o.isSkinnedMesh) return;
      const pos = o.geometry.attributes.position, si = o.geometry.attributes.skinIndex, sw = o.geometry.attributes.skinWeight;
      if (!si || !sw) return;
      for (let i = 0; i < pos.count; i++) {
        let best = 0, bi = 0;
        for (let k = 0; k < 4; k++) { const w = sw.getComponent(i, k); if (w > best) { best = w; bi = si.getComponent(i, k); } }
        const bone = o.skeleton.bones[bi], ci = chains.findIndex((c) => c.set.has(bone));
        if (ci < 0) continue;
        o.getVertexPosition(i, v); o.localToWorld(v); v.sub(hips);
        const rp = radialAt(pts[ci], v.y);
        if (rp !== null) diffs[ci].push(Math.hypot(v.x, v.z) - rp);
      }
    });
    chains.forEach((c, i) => { c.n = diffs[i].length; c.off = clamp(quantile(diffs[i], 0.97) ?? 0, 0, 0.08); });
    const keep = chains.filter((c) => c.n >= 50 && c.bones.length >= 3);
    // 前面开口的裙子: 标准姿态下相邻两条链 (按方位角) 最大的间隔超过 skirtGap. 只在载入时判断一次: 弯腰、坐下时有的链
    // 够不到那个高度, 每帧判断的话间隔忽大忽小, 推手臂一会儿有一会儿没有 (坐下、拿放的冲击 16 -> 20、29 -> 35)
    const angs = keep.map((c, i) => { const m = pts[chains.indexOf(c)].reduce((a, q) => a.add(q), new THREE.Vector3()); return Math.atan2(m.x, m.z); })
      .sort((a, b) => a - b);
    const gaps = angs.map((a, i) => (i + 1 < angs.length ? angs[i + 1] - a : angs[0] + 2 * Math.PI - a));
    this.skirtOpen = angs.length > 1 && Math.max(...gaps) > CONTACT.skirtGap;
    return keep;
  }

  /** 点 p (世界, 半径 r) 在裙子里面多深: 返回 {need, out} (need: 要沿水平方向 out 往外挪多少米) 或 null.
   *  裙子按过髋的竖直轴看: 每条链在点的高度上离轴多远 (+ off), 按方位角在左右相邻两条链之间插值. */
  _skirtDepth(p, r, sk) {
    const dx = p.x - sk.c.x, dz = p.z - sk.c.z, dist = Math.hypot(dx, dz), ang = Math.atan2(dx, dz);
    let lo = null, hi = null;  // 方位角在点左右两侧最近的链 (有这个高度的)
    for (const c of sk.chains) {
      const R = radialAt(c.pts, p.y - sk.c.y);
      if (R === null) continue;
      const d = Math.atan2(Math.sin(c.ang - ang), Math.cos(c.ang - ang));
      if (d >= 0 && (!hi || d < hi.d)) hi = { d, R: R + c.off };
      if (d < 0 && (!lo || d > lo.d)) lo = { d, R: R + c.off };
    }
    if (!lo && !hi) return null;
    // 前面开口的裙子 (载入时判断, 见 _skirtChains): 开口处相邻两条链隔得太开, 中间当作没有裙子, 只在离某条链 20 度以内按那条链算
    const near = (x) => x && Math.abs(x.d) < 0.35;
    const open = this.skirtOpen && lo && hi && hi.d - lo.d > CONTACT.skirtGap;
    if (open && !near(lo) && !near(hi)) return null;
    const R = !lo || !hi ? (lo || hi).R : open ? (near(lo) ? lo : hi).R : lo.R + (hi.R - lo.R) * (-lo.d / Math.max(1e-6, hi.d - lo.d));
    const need = R + r - SKIRT_PRESS - dist;
    if (need <= 0 || dist < 1e-4) return null;
    return { need, out: new THREE.Vector3(dx / dist, 0, dz / dist) };
  }

  /** 这一帧裙子的形状 (相对髋, 世界方向): 弹簧链关节的位置是上一帧的 (弹簧在 VRM 更新时算), 按原始骨骼的髋取相对位置,
   *  再放到这一帧标准化骨骼的髋上. */
  _skirtNow() {
    const raw = this.h.getRawBoneNode('hips').getWorldPosition(new THREE.Vector3());
    const chains = this.skirt.map((c) => {
      const pts = c.bones.map((b) => b.getWorldPosition(new THREE.Vector3()).sub(raw));
      const m = pts.reduce((a, q) => a.add(q), new THREE.Vector3());
      return { pts, off: c.off, ang: Math.atan2(m.x, m.z) };
    });
    return { c: this._world('hips'), chains };
  }

  /** 垂着的手搭在裙子外面. 两步, 转角都帧间平滑 (出来快、收回慢):
   *  1. 手腕、前臂下段在裙子里面: 上臂绕肩往外 (沿裙子的径向) 转到刚好出来;
   *  2. 指尖 (中指末节再往外一节的 0.8) 还在里面: 手绕手腕往外弯, 最多 SKIRT_BEND.
   *  busy: 这只手在做别的 (拿放、伸手、指向、拿着东西、坐着放腿上)、坐着、或者没有裙子时不加. */
  _skirt(side, dt, busy, sk) {
    const up = this.node(`${side}UpperArm`), lo = this.node(`${side}LowerArm`), hand = this.node(`${side}Hand`);
    const deep = (pivot, pts) => {  // 绕 pivot 要往外转多少 (弧度) 才能让这些点都出来, 和转轴
      let ang = 0, axis = null;
      for (const [p, r] of pts) {
        const d = this._skirtDepth(p, r, sk);
        if (!d) continue;
        const arm = p.clone().sub(pivot), a = d.need / Math.max(0.03, arm.length());
        if (a > ang) { ang = a; axis = arm.cross(d.out).normalize(); }
      }
      return { ang, axis };
    };
    const S = this._world(up), E = this._world(lo), H = this._world(hand);
    const a1 = !busy && sk ? deep(S, [[H, this._r(side, 'hand')], [E.clone().lerp(H, 0.7), this._r(side, 'foreArm')]]) : { ang: 0, axis: null };
    this._skirtTurn(side, 'arm', up, a1, dt, Infinity);
    let a2 = { ang: 0, axis: null };
    if (!busy && sk) {
      const H2 = this._world(hand), F = this._fingerEnd(side) ?? this._fingerTip(side, this._world(lo), H2);
      a2 = deep(H2, [[F, this._r(side, 'hand') * 0.6]]);
      if (a1.ang > 0 || a2.ang > 0) { this.stats.skirt.frames++; this.stats.skirt.need += a1.ang + a2.ang; }
    }
    this._skirtTurn(side, 'hand', hand, a2, dt, SKIRT_BEND);
  }

  /** _skirt 的一步: node 往外转 want.ang (平滑后, 不超过 max), 转轴 want.axis (这一帧不在裙子里时沿上一次的转轴收回). */
  _skirtTurn(side, key, node, want, dt, max) {
    const st = (this._skirtSt ??= {})[`${side}:${key}`] ??= { ang: 0, axis: null };
    const goal = Math.min(max, want.ang);
    st.ang += (goal - st.ang) * damp(goal > st.ang ? 0.04 : 0.25, dt);
    if (want.axis) st.axis = want.axis;
    if (key === 'arm') this.skirtAng[side] = st.ang;
    if (st.ang < 1e-4 || !st.axis) return;
    const q = new THREE.Quaternion().setFromAxisAngle(st.axis, st.ang);
    const parent = node.parent.getWorldQuaternion(new THREE.Quaternion());
    const world = node.getWorldQuaternion(new THREE.Quaternion());
    node.quaternion.copy(parent.invert().multiply(q.multiply(world)));
    node.updateWorldMatrix(false, true);
  }

  /** 手搭在裙子外面的程度 (0-1): 手部动作层据此把手指放平、顺着裙子搭着 (手形 lap). */
  skirtRest(side) {
    const st = this._skirtSt || {};
    return clamp((st[`${side}:arm`]?.ang ?? 0) / 0.12 + (st[`${side}:hand`]?.ang ?? 0) / 0.3, 0, 1);
  }

  /** 中指尖 (世界): 末节骨骼再沿末节方向往外 0.8 节; 没有手指骨骼时 null. */
  _fingerEnd(side) {
    const mid = this._world(`${side}MiddleIntermediate`), dis = this._world(`${side}MiddleDistal`);
    if (!mid || !dis) return null;
    return dis.clone().addScaledVector(dis.clone().sub(mid), 0.8);
  }

  /** 载入时 (标准姿态) 量: 一条伸直、略向前 HANG_FWD 度的手臂从肩关节垂下, 外展多少度起肘、前臂、手腕、指尖都不碰身体. */
  _hangAngles() {
    this.vrm.scene.updateMatrixWorld(true);
    const caps = this._bodyCaps(), fr = this._frame(), down = new THREE.Vector3(0, -1, 0);
    const out = {};
    for (const side of ['left', 'right']) {
      const S = this._world(`${side}UpperArm`), E0 = this._world(`${side}LowerArm`), H0 = this._world(`${side}Hand`);
      if (!S || !E0 || !H0) { out[side] = 0; continue; }
      const Lu = S.distanceTo(E0), Lf = E0.distanceTo(H0), lat = side === 'left' ? fr.l : fr.l.clone().negate();
      const f = (HANG_FWD * Math.PI) / 180;
      out[side] = 40;
      for (let deg = -10; deg <= 40; deg += 0.5) {
        const t = (deg * Math.PI) / 180;
        const dir = down.clone().multiplyScalar(Math.cos(t) * Math.cos(f)).addScaledVector(lat, Math.sin(t))
          .addScaledVector(fr.fwd, Math.cos(t) * Math.sin(f)).normalize();
        const E = S.clone().addScaledVector(dir, Lu), H = E.clone().addScaledVector(dir, Lf);
        const F = H.clone().addScaledVector(dir, this._r(side, 'hand') * 2.6);
        const pts = [[E, this._r(side, 'upperArm') * 0.6], [E.clone().lerp(H, 0.5), this._r(side, 'foreArm')], [H, this._r(side, 'hand')],
          [F, this._r(side, 'hand')]];
        if (pts.every(([p, r]) => caps.every((c) => !this._push(p, r, c)))) { out[side] = deg; break; }
      }
    }
    return out;
  }

  /** 手臂重定向修正: 垂下的手臂 (肩 -> 手腕朝下) 会碰到身体时, 绕身体前后轴向外转到刚好不碰.
   *  busy: 这只手在拿东西、伸手、指向、放在腿上 (不加, 免得把手从目标上挪开). 随手臂朝下的程度渐入渐出. */
  _spread(side, dt, busy, caps) {
    const up = this.node(`${side}UpperArm`), hand = this.node(`${side}Hand`);
    if (!up || !hand) return;
    const S = this._world(up), H = this._world(hand);
    const downness = -H.clone().sub(S).normalize().y;  // 1 = 竖直朝下
    const reach = busy ? 0 : clamp((downness - 0.6) / 0.3, 0, 1);
    const base = up.quaternion.clone();
    // 只在垂下的手臂会碰到身体时外展, 转到刚好不碰 (2 度一档, 最多 最小外展角 x spread); 手放在身前没碰到时不动
    const cap = this.hang[side] * CONTACT.spread;
    let need = 0;
    if (reach > 0 && cap > 0 && this._armPen(side, caps) > 1e-4) {
      need = cap;
      for (let d = 2; d < cap; d += 2) {
        this._spreadBy(side, up, d, base);
        if (this._armPen(side, caps) <= 1e-4) { need = d; break; }
      }
      up.quaternion.copy(base);
      up.updateWorldMatrix(false, true);
    }
    // 外展记忆 (CONTACT.spreadMem): 手臂竖直垂着时 (downness 0.98 以上) 量到的外展记下来; 抬起来时 (蹲下手搭膝盖、做表情动作)
    // 记着不动, 回到快竖直朝下时 (0.9 -> 0.98) 按记住的量先张开. 以前抬起来时外展衰减到 0, 回来要等插进去再从 0 张开,
    // 蹲下站起来时手腕擦过大腿、骨盆 (每次 0-27 次推开, 最深 1-2 cm). 手搭在膝盖上时是 0.83-0.85, 不受影响
    // 试过只在"回来"的路上用 (抬起来过才算): 往下放的半路不张开, 但蹲下推开 61 -> 100 次, 不如一直用
    const hang = clamp((downness - 0.9) / 0.08, 0, 1);
    if (!busy && hang >= 1) this.spreadMem[side] += (need - this.spreadMem[side]) * damp(need > this.spreadMem[side] ? 0.08 : 0.5, dt);
    // 记住的量再多张 SPREAD_MEM_MARGIN 度 (不超过上限): 记下来的是别的待机姿势时的量, 换了片段差一点就擦 (桌宠自检 idle_11 右手腕擦大腿;
    // 30 帧 idle_11 上蹲 34 次: 多 0 度 21 次推开、3 度 2 次、6 度 19 次)
    const floor = CONTACT.spreadMem && !busy && this.spreadMem[side] > 0.5 ? Math.min(cap, this.spreadMem[side] + SPREAD_MEM_MARGIN) * hang : 0;
    // 张开快、收回慢, 免得手臂在"刚好碰到"附近来回跳
    const goal = Math.max(need * reach, floor);
    this.spreadDeg[side] += (goal - this.spreadDeg[side]) * damp(goal > this.spreadDeg[side] ? 0.08 : 0.3, dt);
    if (this.spreadDeg[side] > 1e-3) this._spreadBy(side, up, this.spreadDeg[side], base);
  }

  /** 上臂从 base 绕身体前后轴向外转 deg 度 (左臂往左、右臂往右). */
  _spreadBy(side, up, deg, base) {
    up.quaternion.copy(base);
    up.updateWorldMatrix(false, true);
    const q = new THREE.Quaternion().setFromAxisAngle(this._frame().fwd, ((side === 'left' ? 1 : -1) * deg * Math.PI) / 180);
    const parent = up.parent.getWorldQuaternion(new THREE.Quaternion());
    const world = up.getWorldQuaternion(new THREE.Quaternion());
    up.quaternion.copy(parent.invert().multiply(q.multiply(world)));
    up.updateWorldMatrix(false, true);
  }

  /** 肘、前臂中点、手腕、指尖插进身体的总深度 (米). */
  _armPen(side, caps) {
    const E = this._world(`${side}LowerArm`), H = this._world(`${side}Hand`);
    const F = H.clone().addScaledVector(H.clone().sub(E).normalize(), this._r(side, 'hand') * 2.6);
    let pen = 0;
    for (const [p, r] of [[E, this._r(side, 'upperArm') * 0.6], [E.clone().lerp(H, 0.5), this._r(side, 'foreArm')],
      [H, this._r(side, 'hand')], [F, this._r(side, 'hand')]]) {
      for (const c of caps) { const push = this._push(p, r, c); if (push) pen += push.length(); }
    }
    return pen;
  }

  node(n) { return this.h.getNormalizedBoneNode(n); }

  _world(n) { const b = typeof n === 'string' ? this.node(n) : n; return b ? b.getWorldPosition(new THREE.Vector3()) : null; }

  /** 身体朝向 (世界): 左 (角色自己的左边) 和前. 由髋部旋转得到, 和模型的 VRM 版本无关. */
  _frame() {
    const q = this.node('hips').getWorldQuaternion(new THREE.Quaternion());
    const l = this._world('leftUpperLeg').sub(this._world('rightUpperLeg')).normalize();
    const up = new THREE.Vector3(0, 1, 0).applyQuaternion(q);
    const fwd = l.clone().cross(up).normalize();  // 左 x 上 = 前
    return { l, fwd };
  }

  /** 载入时量半径: 每个顶点归给主权重骨骼, 算它到对应身体段轴线的距离, 取分位数 (衣服也算在内). */
  _measure() {
    const raw = (n) => this.h.getRawBoneNode(n);
    const nameOf = new Map();
    for (const n of Object.keys(this.h.humanBones)) { const b = raw(n); if (b) nameOf.set(b, n); }
    const segs = [...SEGMENTS.filter((s) => raw(s.a) && raw(s.b)),
      ...['left', 'right'].flatMap((side) => LIMBS.filter((s) => raw(side + s.a) && raw(side + s.b))
        .map((s) => ({ id: `${side}:${s.id}`, a: side + s.a, b: side + s.b, bones: s.bones.map((x) => side + x), skin: s.skin, limb: true })))];
    this.vrm.scene.updateMatrixWorld(true);
    const ends = segs.map((s) => [raw(s.a).getWorldPosition(new THREE.Vector3()), raw(s.b).getWorldPosition(new THREE.Vector3())]);
    const fr = this._frame();
    // all: 所有材质; skin: 只有皮肤材质 (模型里没有皮肤材质时退回 all)
    const samples = Object.fromEntries(segs.map((s) => [s.id, { all: { d: [], l: [], f: [] }, skin: { d: [], l: [], f: [] },
      body: { d: [], l: [], f: [] } }]));
    const v = new THREE.Vector3();
    this.vrm.scene.traverse((o) => {
      if (!o.isSkinnedMesh || /hair|face/i.test(o.name)) return;
      const g = o.geometry, pos = g.attributes.position, si = g.attributes.skinIndex, sw = g.attributes.skinWeight;
      if (!si || !sw) return;
      const mats = Array.isArray(o.material) ? o.material : [o.material];
      const skin = mats.some((m) => SKIN.test(m?.name || '')), body = mats.some((m) => BODY.test(m?.name || ''));
      // 合并网格的各个图元共用顶点缓冲, 只看这个图元自己用到的顶点
      const used = new Set();
      if (g.index) for (let k = 0; k < g.index.count; k += 3) used.add(g.index.getX(k));
      else for (let k = 0; k < pos.count; k += 2) used.add(k);
      for (const i of used) {
        let best = 0, bi = -1;
        for (let k = 0; k < 4; k++) { const w = sw.getComponent(i, k); if (w > best) { best = w; bi = si.getComponent(i, k); } }
        const name = nameOf.get(o.skeleton.bones[bi]);
        if (!name) continue;
        let got = false;
        segs.forEach((s, j) => {
          if (!s.bones.includes(name)) return;
          if (!got) { o.getVertexPosition(i, v); o.localToWorld(v); got = true; }
          const { d, t, q } = segment(v, ...ends[j]);
          // 两端外面 (沿轴线方向超出端点) 的点另外记: 骨盆的轴线连着两个髋关节, 胯侧离髋关节只有 4-5 cm, 前后却有 10 cm,
          // 都按一个半径量的话胯两侧往外多出 5-6 cm, 手垂在胯侧就被判成插进骨盆、被往外推 (手臂张着, 不像少女)
          // 量的是沿轴线超出端点多远 (胯侧的皮肤多在髋关节斜上、斜下方, 按到端点的距离量还是会量成前后那么胖)
          if (s.ends && (t <= 0 || t >= 1) && skin) {
            const along = Math.abs(v.clone().sub(q).dot(ends[j][1].clone().sub(ends[j][0]).normalize()));
            if (along > 0.005) (samples[s.id].end ??= []).push(along);
          }
          if (t <= (s.tMin ?? 0.08) || t >= 0.92) return;
          const off = v.clone().sub(q);
          const bags = [samples[s.id].all];
          if (skin) bags.push(samples[s.id].skin);
          if (body) bags.push(samples[s.id].body);
          for (const bag of bags) {
            bag.d.push(d);
            bag.l.push(Math.abs(off.dot(fr.l)));
            bag.f.push(Math.abs(off.dot(fr.fwd)));
          }
        });
      }
    });
    // 取 0.8 分位: 衣服褶皱和裙摆边缘不算, 手臂贴着身体时不至于被推开
    const hipW = ends[segs.findIndex((x) => x.id === 'pelvis')]?.reduce((a, b) => a.distanceTo(b)) ?? null;
    for (const s of segs) {
      const def = SEGMENTS.find((x) => x.id === s.id) || s;
      const hasSkin = samples[s.id].skin.d.length > 50;
      const sm = def?.skin && hasSkin ? samples[s.id].skin : samples[s.id].all;
      const key = s.id.includes(':') ? s.id.split(':')[1] : s.id;
      // 该量皮肤却找不到皮肤材质 (材质名里没有 skin) 时, 裙子、夹克下摆、袖口、荷叶边都会算进来;
      // 衣服只会让距离偏大, 皮肤在分布的下半部, 取 0.35 分位
      const q = def?.skin && !hasSkin ? 0.35 : s.limb ? LIMB_Q : 0.8;
      const r = quantile(sm.d, q);
      if (r) this.radius[s.id] = clamp(r, 0.015, MAX_R[key] ?? 0.25);
      else this.radius[s.id] = DEFAULT_R[key];
      // 骨盆: 不超过两髋关节间距的 0.65 倍 (Wendy 量成 12 cm, 把短短的大腿根整个盖住, 手一放腿上就判成插进骨盆)
      if (s.id === 'pelvis' && hipW) this.radius[s.id] = Math.min(this.radius[s.id], Math.max(0.05, hipW * 0.65));
      if (def?.ends && (samples[s.id].end?.length ?? 0) > 30) this.endR[s.id] = clamp(quantile(samples[s.id].end, 0.95), 0.025, this.radius[s.id]);  // 只量皮肤, 取到胯侧最宽处
      if (def?.ellipse && sm.l.length > 50) this.ellipse[s.id] = { l: clamp(quantile(sm.l, 0.85), 0.04, 0.25), f: clamp(quantile(sm.f, 0.85), 0.04, 0.25) };
      // 腹、胸量的是所有材质 (紧身的衣服要算进去, 手臂不能穿过它), 但敞开、下摆张开的外套会把腰量得比髋还宽
      // (miomio 腰部左右半宽 14 cm, 髋 7.8 cm), 垂下的手臂每帧都被推开、一直张着. 有身体材质时最多比它宽 CLOTH
      const body = samples[s.id].body;
      if (def?.ellipse && body.d.length > 50) {
        this.radius[s.id] = Math.min(this.radius[s.id], quantile(body.d, 0.8) + CLOTH);
        const e = this.ellipse[s.id];
        if (e) this.ellipse[s.id] = { l: Math.min(e.l, quantile(body.l, 0.85) + CLOTH), f: Math.min(e.f, quantile(body.f, 0.85) + CLOTH) };
      }
    }
    this.measured = Object.fromEntries(Object.entries(this.radius).map(([k, r]) => [k, +r.toFixed(3)]));
    for (const [k, r] of Object.entries(this.endR)) this.measured[`${k}End`] = +r.toFixed(3);
  }

  _r(side, limb) { return this.radius[`${side}:${limb}`] ?? DEFAULT_R[limb]; }

  /** 当前帧的身体碰撞体 (世界坐标). */
  _bodyCaps() {
    const fr = this._frame();
    const caps = [];
    for (const s of SEGMENTS) {
      const A = this._world(s.a), B = this._world(s.b);
      if (!A || !B) continue;
      caps.push({ id: s.id, A, B, r: this.radius[s.id] ?? DEFAULT_R[s.id], e: this.ellipse[s.id], rEnd: this.endR[s.id], fr });
    }
    return caps;
  }

  /** 点 p (半径 rs) 和碰撞体的穿透: 返回推出向量 (没穿透为 null). 椭圆截面按方向取有效半径. */
  _push(p, rs, c) {
    const { d, q } = segment(p, c.A, c.B);
    const off = p.clone().sub(q);
    let dir = d > 1e-6 ? off.clone().divideScalar(d) : c.fr.fwd.clone();
    let r = c.r;
    if (c.e) {
      // 截面内的方向分量 (hyp 接近 1 时在轴线中段, 接近 0 时在端部): 截面内按椭圆, 端部按圆
      const ul = dir.dot(c.fr.l), uf = dir.dot(c.fr.fwd), hyp = Math.hypot(ul, uf);
      const k = Math.sqrt((ul / c.e.l) ** 2 + (uf / c.e.f) ** 2);
      if (k > 1e-6) r = Math.min(c.r * 1.2, (hyp / k) * hyp + c.r * (1 - hyp));
    }
    if (c.rEnd && d > 1e-6) {
      // 两端按椭球: 沿轴线方向的半径 rEnd, 垂直方向 r (轴线中段的点方向垂直于轴线, 不受影响)
      const ax = Math.abs(dir.dot(c.B.clone().sub(c.A).normalize()));
      r = 1 / Math.sqrt((ax / c.rEnd) ** 2 + (1 - ax * ax) / (r * r));
    }
    const pen = r + rs + MARGIN - d;
    if (pen <= 0) return null;
    if (d < 1e-6) dir = c.fr.fwd.clone();
    return dir.multiplyScalar(pen);
  }

  /** 绕 pivot 转动 node, 让 pivot->cur 指向 pivot->tgt (世界). */
  _aim(node, pivot, cur, tgt) {
    const a = cur.clone().sub(pivot), b = tgt.clone().sub(pivot);
    if (a.lengthSq() < 1e-10 || b.lengthSq() < 1e-10) return;
    const q = new THREE.Quaternion().setFromUnitVectors(a.normalize(), b.normalize());
    const parent = node.parent.getWorldQuaternion(new THREE.Quaternion());
    const world = node.getWorldQuaternion(new THREE.Quaternion());
    node.quaternion.copy(parent.invert().multiply(q.multiply(world)));
    node.updateWorldMatrix(false, true);
  }

  /** 肘和前臂中点插进身体的总深度 (米). */
  _elbowPen(side, caps) {
    const E = this._world(`${side}LowerArm`), H = this._world(`${side}Hand`);
    let pen = 0;
    for (const [p, r] of [[E, this._r(side, 'upperArm') * 0.6], [E.clone().lerp(H, 0.5), this._r(side, 'foreArm')]]) {
      for (const c of caps) { const push = this._push(p, r, c); if (push) pen += push.length(); }
    }
    return pen;
  }

  /** 绕"肩-手腕"轴转动整条手臂 (手腕位置不变, 只换肘的方向), 角度为 deg. */
  _swivelBy(up, axis, deg, base) {
    up.quaternion.copy(base);
    up.updateWorldMatrix(false, true);
    if (!deg) return;
    const q = new THREE.Quaternion().setFromAxisAngle(axis, (deg * Math.PI) / 180);
    const parent = up.parent.getWorldQuaternion(new THREE.Quaternion());
    const world = up.getWorldQuaternion(new THREE.Quaternion());
    up.quaternion.copy(parent.invert().multiply(q.multiply(world)));
    up.updateWorldMatrix(false, true);
  }

  /** 自动调整姿态: 肘或前臂插进身体时, 先在两骨 IK 剩下的自由度 (肘绕"肩-手腕"轴的转角) 上找一个不穿的角度,
   *  手还在动画要的位置; 找不到完全不穿的就取穿得最浅的. 转角在帧间平滑, 免得左右来回跳. 剩下的穿透再推开. */
  _swivel(side, caps, dt) {
    const up = this.node(`${side}UpperArm`);
    const S = this._world(`${side}UpperArm`), W = this._world(`${side}Hand`);
    const axis = W.clone().sub(S);
    if (axis.lengthSq() < 1e-6) return;
    axis.normalize();
    const base = up.quaternion.clone();
    const pen0 = this._elbowPen(side, caps);
    let goal = 0;
    if (pen0 > 1e-4) {
      let best = { deg: 0, score: pen0 };
      for (const deg of [15, -15, 30, -30, 45, -45, 60, -60, 80, -80]) {
        this._swivelBy(up, axis, deg, base);
        const pen = this._elbowPen(side, caps);
        const score = pen + Math.abs(deg) * 2e-5;  // 一样不穿时取转得少的
        if (score < best.score) best = { deg, score };
        if (pen < 1e-4) break;
      }
      goal = best.deg;
    }
    this.swivel[side] += (goal - this.swivel[side]) * damp(goal ? 0.15 : 0.35, dt);  // 太快会超过姿态平滑层允许的角加速度
    this._swivelBy(up, axis, this.swivel[side], base);
    if (Math.abs(this.swivel[side]) > 0.5) this.stats.swivels++;
  }

  /** held: [{side, center, radius, kind}] (来自行动层); busy: {left, right} 这只手在做别的 (见 _spread). 返回这一帧有没有改动手臂. */
  update(dt, held = [], busy = {}) {
    this.vrm.scene.updateMatrixWorld(true);
    const caps = this._bodyCaps();
    let changed = false;
    this.stats.lastPush = 0;
    const sk = CONTACT.skirt && this.skirt.length && !busy.seated ? this._skirtNow() : null;
    this._emoteBusy = busy.emote || null;  // penetration() 用
    for (const side of ['left', 'right']) {
      const up = this.node(`${side}UpperArm`), lo = this.node(`${side}LowerArm`), hand = this.node(`${side}Hand`);
      if (!up || !lo || !hand) continue;
      const item = held.find((x) => x.side === side);
      this._spread(side, dt, busy[side] || !!item, caps);
      this._skirt(side, dt, busy[side] || !!item || !sk, sk);
      this._swivel(side, caps, dt);
      for (let iter = 0; iter < 2; iter++) {
        const S = this._world(up), E = this._world(lo), H = this._world(hand);
        const F = this._fingerTip(side, E, H);
        const samples = [
          { p: E, r: this._r(side, 'upperArm') * 0.6, wE: 1, wH: 0, n: 'elbow' },  // 肘: 上臂贴着身体是自然的, 只防插进去
          { p: E.clone().lerp(H, 0.5), r: this._r(side, 'foreArm'), wE: 0.5, wH: 0.5, n: 'forearm' },
          { p: H, r: this._r(side, 'hand'), wE: 0, wH: 1, n: 'wrist' },  // 手腕: 用手的半径 (前臂的会把放在腿上的手判成插进去)
          { p: F, r: this._r(side, 'hand'), wE: 0, wH: 1, n: 'finger' },
        ];
        if (item) samples.push({ p: item.center, r: item.radius, wE: 0, wH: 1, n: 'item' });
        const dE = new THREE.Vector3(), dH = new THREE.Vector3();
        for (const s of samples) {
          for (const c of caps) {
            // 表情动作 (托腮、捂嘴、捧脸) 的手本来就贴着下巴、脸: 胸部胶囊在脖子那头的球冠 (半径 11 cm) 包住了下巴和脸颊下半,
            // 手腕、指尖会被推出去 5-7 cm. 这只手在做表情动作时, 手腕、指尖, 以及比脖子关节高 (往下 2 cm 起) 的取样点不和胸部胶囊比:
            // 那一截是下巴和脖子, 不是身体. 以前只放过手腕、指尖, 双手捧脸时前臂在脖子前面合拢, 桌宠自检里被推了 113 次
            if (busy.emote?.[side] && c.id === 'chest' && (s.n === 'wrist' || s.n === 'finger' || s.p.y > c.B.y - 0.02)) continue;
            const push = this._push(s.p, s.r, c);
            if (!push) continue;
            this.stats.maxPen = Math.max(this.stats.maxPen * 0.999, push.length());
            if (iter === 0) { const k = `${c.id}:${s.n}`; this.stats.by[k] = (this.stats.by[k] || 0) + push.length(); }
            if (s.wE && push.length() * s.wE > dE.length()) dE.copy(push).multiplyScalar(s.wE);
            if (s.wH && push.length() * s.wH > dH.length()) dH.copy(push).multiplyScalar(s.wH);
          }
        }
        if (dE.lengthSq() < 1e-8 && dH.lengthSq() < 1e-8) break;
        this.stats.pushes++;
        this.stats.lastPush += Math.max(dE.length(), dH.length());
        changed = true;
        const goalH = H.clone().add(dH).add(dE.clone().multiplyScalar(0.5));
        if (dE.lengthSq() > 1e-8) this._aim(up, S, E, E.clone().add(dE));
        const E2 = this._world(lo), H2 = this._world(hand);
        this._aim(lo, E2, H2, goalH);
        if (item) item.center.add(this._world(hand).sub(H));
      }
    }
    this._grasp(dt, held);
    return changed;
  }

  /** 只量不改: 手臂各取样点 (肘、前臂中点、手腕、指尖) 和手里的东西插进身体的深度之和 (米). 接触层处理之后调用,
   *  量的是剩下没推开的穿模 (痛觉). 推开时留的缝 MARGIN 不算. */
  penetration(held = []) {
    this.vrm.scene.updateMatrixWorld(true);
    const caps = this._bodyCaps();
    let total = 0;
    for (const side of ['left', 'right']) {
      const lo = this.node(`${side}LowerArm`), hand = this.node(`${side}Hand`);
      if (!lo || !hand) continue;
      const E = this._world(lo), H = this._world(hand);
      const F = this._fingerTip(side, E, H);
      const pts = [[E, this._r(side, 'upperArm') * 0.6], [E.clone().lerp(H, 0.5), this._r(side, 'foreArm')],
        [H, this._r(side, 'hand')], [F, this._r(side, 'hand')]];
      const item = held.find((x) => x.side === side);
      if (item) pts.push([item.center, item.radius]);
      const face = this._emoteBusy?.[side];  // 表情动作的手贴着下巴、脸: 比脖子关节高的点不和胸部胶囊比 (同 update)
      for (const [i, [p, r]] of pts.entries()) {
        let worst = 0;
        for (const c of caps) {
          if (face && c.id === 'chest' && (i === 2 || i === 3 || p.y > c.B.y - 0.02)) continue;
          const push = this._push(p, r, c);
          if (push) worst = Math.max(worst, push.length() - MARGIN);
        }
        total += Math.max(0, worst);
      }
    }
    return total;
  }

  /** 只算不改: 点 p (半径 r) 挪到身体碰撞体 (骨盆、腹、胸、大腿) 外面以后在哪 (新的点; 本来就在外面原样返回).
   *  dir (世界方向) 给了就只沿它挪 (比如从身体里往上, 落在肚子上面), 没给就沿推出方向.
   *  别的层摆手的目标用 (小屋躺着时放在肚子上的手, 见 desktop/web/room-poses.js 的 _clear): 和这一层按同一个身体算, 放上去就不会被推开. */
  clearOf(p, r, dir = null) {
    this.vrm.scene.updateMatrixWorld(true);
    const caps = this._bodyCaps(), out = p.clone();
    const inside = (q) => caps.some((c) => this._push(q, r, c));
    if (dir) {
      if (!inside(out)) return out;
      const d = dir.clone().normalize();
      let lo = 0, hi = 0.6;  // 二分找沿 dir 最少挪多远才出来 (1 mm 以内)
      for (let i = 0; i < 10; i++) { const t = (lo + hi) / 2; if (inside(p.clone().addScaledVector(d, t))) lo = t; else hi = t; }
      return out.addScaledVector(d, hi);
    }
    for (let iter = 0; iter < 3; iter++) {
      let moved = false;
      for (const c of caps) {
        const push = this._push(out, r, c);
        if (push) { out.add(push); moved = true; }
      }
      if (!moved) break;
    }
    return out;
  }

  /** 指尖取样点: 手腕 H 沿手的朝向 (腕 -> 中指根; 没有中指骨骼或 fingerDir = 0 时沿前臂 E -> H) 往外 2.6 个手半径. */
  _fingerTip(side, E, H) {
    const mid = CONTACT.fingerDir ? this._world(`${side}MiddleProximal`) : null;
    const dir = (mid && mid.distanceToSquared(H) > 1e-6 ? mid.sub(H) : H.clone().sub(E)).normalize();
    return H.clone().addScaledVector(dir, this._r(side, 'hand') * 2.6);
  }

  /** 握紧程度: 拿着东西的手渐渐握起来, 放下后松开 (手指怎么弯在 hand.js). */
  _grasp(dt, held) {
    for (const side of ['left', 'right']) {
      const item = held.find((x) => x.side === side);
      if (item) this.gripKind[side] = item.kind;
      const goal = item ? 1 : 0;
      this.grip[side] += (goal - this.grip[side]) * damp(goal ? 0.1 : 0.2, dt);
    }
  }
}
