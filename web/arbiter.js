// 运动仲裁表: 每一帧哪一层在驱动哪个部位, 各占多少.
//
// 一帧里写骨骼的层很多, 按先后: 动作来源 (HAST-SEC / 运动匹配 / 待机库、手势库 / 动作片段 / 程序动作) ->
// 行动层的 IK (配平、脚、拿放、持物、手放腿上、指向、伸手、伸手够、看向) -> 程序手势 -> 关节活动范围 -> 防穿模 ->
// 手形 -> 表演层的颈部 -> 头部 (Soullink) -> 平滑 -> 落地. 以前各层按权重叠在一起, 出了怪动作只能猜是哪一层.
//
// 量法: 每层写完调一次 mark(层), 量这一层让每根骨骼转了多少度 (和上一层写完时比), 按部位加起来就是这一层对这个部位的
// "影响". 动作来源和手形是整个写的 (absolute): 影响按姿态离零位 (标准化骨骼零位是单位旋转) 多远算. 骨盆位移
// 按 1 cm = 1 度并进躯干. 某层在某部位的权重 = 它的影响 / 这个部位所有层的影响之和.
// 行动层盖在 HAST-SEC 上 (行动层权重 w) 时, 动作来源的影响按 w 分给 HAST-SEC 和行动层的来源.
//
// 每帧的影响和骨骼姿态存进环形缓存 (默认最近 20 秒), 约束面板可以暂停、拖动、回放, 也能导出成 JSON 再载入回放.
// 每个动作结束时汇总 (各部位各层的平均权重 + 每 0.2 秒一行的时间线) 跟着执行评估写进服务端日志.
//
// 纯计算的部分 (部位、层、权重、汇总) 不依赖 three.js, 可以在 Node 里单测.

export const PARTS = [
  { id: 'head', name: '头颈', bones: ['neck', 'head'] },
  { id: 'torso', name: '躯干', bones: ['hips', 'spine', 'chest', 'upperChest'], pos: true },
  { id: 'leftArm', name: '左臂', bones: ['leftShoulder', 'leftUpperArm', 'leftLowerArm', 'leftHand'] },
  { id: 'rightArm', name: '右臂', bones: ['rightShoulder', 'rightUpperArm', 'rightLowerArm', 'rightHand'] },
  { id: 'leftFingers', name: '左手指', bones: fingers('left'), fingers: true },
  { id: 'rightFingers', name: '右手指', bones: fingers('right'), fingers: true },
  { id: 'leftLeg', name: '左腿', bones: ['leftUpperLeg', 'leftLowerLeg', 'leftFoot', 'leftToes'] },
  { id: 'rightLeg', name: '右腿', bones: ['rightUpperLeg', 'rightLowerLeg', 'rightFoot', 'rightToes'] },
];

function fingers(side) {
  const out = [];
  for (const f of ['Thumb', 'Index', 'Middle', 'Ring', 'Little']) {
    for (const s of f === 'Thumb' ? ['Metacarpal', 'Proximal', 'Distal'] : ['Proximal', 'Intermediate', 'Distal']) out.push(`${side}${f}${s}`);
  }
  return out;
}

// 层: 编号、名字、颜色 (面板里用; 同一类用相近的颜色: 动作来源蓝、行动层 IK 绿、修正橙红、手紫、头青、平滑灰)
export const LAYERS = [
  { id: 'hast', name: 'HAST-SEC', color: '#3b6fd8' },
  { id: 'base', name: '动作来源', color: '#5b8def' },
  { id: 'proc', name: '程序动作', color: '#8aa9f0' },
  { id: 'balance', name: '配平', color: '#2e9e6b' },
  { id: 'feet', name: '脚部 IK', color: '#46b37f' },
  { id: 'grab', name: '拿放 IK', color: '#5fc493' },
  { id: 'carry', name: '持物', color: '#79cfa3' },
  { id: 'lap', name: '手放腿上', color: '#2f8f8a' },
  { id: 'point', name: '指向', color: '#3fa9a3' },
  { id: 'offer', name: '伸手牵手', color: '#58bdb6' },
  { id: 'reach', name: '伸手够', color: '#7bcfc9' },
  { id: 'poise', name: '仪态', color: '#93d9c4' },
  { id: 'look', name: '看向', color: '#1f9bb5' },
  { id: 'gesture', name: '程序手势', color: '#9c7ce0' },
  { id: 'rom', name: '活动范围', color: '#e0823a' },
  { id: 'contact', name: '防穿模', color: '#d9534f' },
  { id: 'hands', name: '手形', color: '#b46cd6' },
  { id: 'neck', name: '表演颈部', color: '#4fb3c8' },
  { id: 'head', name: '头部动作', color: '#2aa7c9' },
  { id: 'filter', name: '平滑', color: '#8b919c' },
  { id: 'ground', name: '落地', color: '#a8956b' },
];
export const LAYER_INDEX = Object.fromEntries(LAYERS.map((l, i) => [l.id, i]));
const NP = PARTS.length, NL = LAYERS.length;
const MOVED = 0.01;  // 一根骨骼这一层转了不到这么多度 (骨盆挪了不到这么多厘米) 算没动

/** 一帧的影响矩阵 (NL x NP, 度) -> 每个部位各层的权重 (同样排列, 每列加起来是 1; 这个部位没动时全 0). */
export function shares(infl) {
  const out = new Float32Array(NL * NP);
  for (let p = 0; p < NP; p++) {
    let sum = 0;
    for (let l = 0; l < NL; l++) sum += infl[l * NP + p];
    if (sum <= 1e-6) continue;
    for (let l = 0; l < NL; l++) out[l * NP + p] = infl[l * NP + p] / sum;
  }
  return out;
}

/** 某个部位权重最大的 n 层: [[层编号, 权重], ...] (权重 < min 的不要). */
export function topLayers(w, part, n = 3, min = 0.02) {
  const p = typeof part === 'number' ? part : PARTS.findIndex((x) => x.id === part);
  const list = [];
  for (let l = 0; l < NL; l++) if (w[l * NP + p] >= min) list.push([LAYERS[l].id, w[l * NP + p]]);
  return list.sort((a, b) => b[1] - a[1]).slice(0, n);
}

/** 一段帧 (每帧的影响矩阵) 的汇总: 每个部位各层的平均权重 (先按帧算权重再平均, 每帧一样重; 只在有层动了这个部位的帧里
 *  平均), 只留 >= 1% 的. 返回 {部位: {层: 权重}}. */
export function summarize(frames) {
  const acc = new Float64Array(NL * NP), n = new Float64Array(NP);
  for (const infl of frames) {
    const w = shares(infl);
    for (let p = 0; p < NP; p++) {
      let any = false;
      for (let l = 0; l < NL; l++) { const v = w[l * NP + p]; if (v > 0) { acc[l * NP + p] += v; any = true; } }
      if (any) n[p]++;
    }
  }
  const out = {};
  PARTS.forEach((part, p) => {
    if (!n[p]) return;
    const row = {};
    for (let l = 0; l < NL; l++) {
      const v = acc[l * NP + p] / n[p];
      if (v >= 0.01) row[LAYERS[l].id] = +v.toFixed(3);
    }
    if (Object.keys(row).length) out[part.id] = row;
  });
  return out;
}

/** 两个四元数 [x, y, z, w] (分量分开传, 免得每帧建数组) 之间的转角 (度). */
function angleDeg(ax, ay, az, aw, bx, by, bz, bw) {
  const d = Math.abs(ax * bx + ay * by + az * bz + aw * bw);
  return d >= 1 ? 0 : (2 * Math.acos(d) * 180) / Math.PI;
}

/**
 * 仲裁记录器. humanoid: three-vrm 的 VRMHumanoid (取标准化骨骼); root: 角色根节点 (vrm.scene, 回放时恢复位置朝向).
 * 用法: 每帧 begin() -> 每层写完 mark(层) -> end(时间, 来源, 来源说明).
 */
export class Arbiter {
  constructor(humanoid, root = null, { seconds = 20, fps = 60 } = {}) {
    this.root = root;
    this.nodes = [];
    this.partOf = [];
    PARTS.forEach((part, p) => {
      for (const b of part.bones) {
        const node = humanoid.getNormalizedBoneNode(b);
        if (node) { this.nodes.push(node); this.partOf.push(p); }
      }
    });
    this.hips = humanoid.getNormalizedBoneNode('hips');
    this.torso = PARTS.findIndex((x) => x.id === 'torso');
    this.fingers = PARTS.map((x) => !!x.fingers);
    const nb = this.nodes.length;
    // 上一层写完时的姿态: 用 64 位存 (32 位的舍入误差在 0 度附近量出来约 0.05 度, 每层每个部位都会记上一点)
    this.prev = new Float64Array(nb * 4);
    this.prevHips = new Float64Array(3);
    this.infl = new Float32Array(NL * NP);  // 这一帧各层对各部位的影响 (度)
    this.cap = Math.round(seconds * fps);
    this.poseSize = nb * 4 + 3 + 7;  // 骨骼四元数 + 骨盆位置 + 根节点位置和朝向
    this.ring = { infl: new Float32Array(this.cap * NL * NP), pose: new Float32Array(this.cap * this.poseSize),
      t: new Float64Array(this.cap), src: new Array(this.cap), mix: new Float32Array(this.cap) };
    this.count = 0;      // 缓存里有几帧
    this.next = 0;       // 下一帧写到哪
    this.enabled = true;
    this.active = false;
    this.last = null;    // 最近一帧的 {t, src, mix, w}
  }

  /** 清空缓存 (执行评估每个场景从同一个模拟时刻开始, 不清的话上一个场景的帧会混进来). */
  clear() {
    this.count = 0;
    this.next = 0;
    this.last = null;
  }

  _snap() {
    const q = this.prev;
    this.nodes.forEach((n, i) => { const o = n.quaternion; q[i * 4] = o.x; q[i * 4 + 1] = o.y; q[i * 4 + 2] = o.z; q[i * 4 + 3] = o.w; });
    if (this.hips) { const p = this.hips.position; this.prevHips[0] = p.x; this.prevHips[1] = p.y; this.prevHips[2] = p.z; }
  }

  begin() {
    this.active = this.enabled;
    if (!this.active) return;
    this.infl.fill(0);
    this._snap();
  }

  /**
   * layer 写完以后调用. absolute: 这一层整个写了它管的骨骼, 影响按姿态离零位多远算 (all: 所有身体骨骼都算, 用于动作来源;
   * fingers: 手指都算, 用于手形 (每帧整个写, 手指不动的帧也是它在驱动); 都没给时只算这一层改了的骨骼);
   * 否则按这一层改了多少算. split: {层: 比例} 把影响分一部分给别的层 (行动层盖在 HAST-SEC 上时 {hast: 1 - w}).
   */
  mark(layer, { absolute = false, all = false, fingers = false, split = null } = {}) {
    if (!this.active) return;
    const li = LAYER_INDEX[layer];
    if (li === undefined) return;
    const q = this.prev, infl = this.infl;
    const add = (l, p, v) => {
      if (!split) { infl[l * NP + p] += v; return; }
      let rest = v;
      for (const [other, k] of Object.entries(split)) {
        const oi = LAYER_INDEX[other];
        if (oi === undefined || !(k > 0)) continue;
        infl[oi * NP + p] += v * k;
        rest -= v * k;
      }
      infl[l * NP + p] += Math.max(0, rest);
    };
    for (let i = 0; i < this.nodes.length; i++) {
      const o = this.nodes[i].quaternion, j = i * 4, p = this.partOf[i];
      const d = angleDeg(q[j], q[j + 1], q[j + 2], q[j + 3], o.x, o.y, o.z, o.w);
      if (absolute) {
        if ((all && !this.fingers[p]) || (fingers && this.fingers[p]) || d > MOVED) add(li, p, angleDeg(0, 0, 0, 1, o.x, o.y, o.z, o.w));
      } else if (d > MOVED) {
        add(li, p, d);
      }
      q[j] = o.x; q[j + 1] = o.y; q[j + 2] = o.z; q[j + 3] = o.w;
    }
    if (this.hips && this.torso >= 0) {
      const h = this.hips.position, ph = this.prevHips;
      const cm = Math.hypot(h.x - ph[0], h.y - ph[1], h.z - ph[2]) * 100;
      if (!absolute && cm > MOVED) add(li, this.torso, cm);
      ph[0] = h.x; ph[1] = h.y; ph[2] = h.z;
    }
  }

  /** 一帧结束: 存进环形缓存. src: 动作来源 (如 "idle:idle_07"、"act:cmu14_30_sit0_down"、"mm:neutral"、"hast"), mix: 行动层权重. */
  end(t, src = '', mix = 1) {
    if (!this.active) return;
    this.active = false;
    const k = this.next, r = this.ring;
    r.infl.set(this.infl, k * NL * NP);
    const pose = r.pose, o = k * this.poseSize;
    this.nodes.forEach((n, i) => { const q = n.quaternion; pose[o + i * 4] = q.x; pose[o + i * 4 + 1] = q.y; pose[o + i * 4 + 2] = q.z; pose[o + i * 4 + 3] = q.w; });
    const b = o + this.nodes.length * 4;
    if (this.hips) { const p = this.hips.position; pose[b] = p.x; pose[b + 1] = p.y; pose[b + 2] = p.z; }
    if (this.root) {
      const p = this.root.position, q = this.root.quaternion;
      pose.set([p.x, p.y, p.z, q.x, q.y, q.z, q.w], b + 3);
    }
    r.t[k] = t;
    r.src[k] = src;
    r.mix[k] = mix;
    this.next = (k + 1) % this.cap;
    this.count = Math.min(this.cap, this.count + 1);
    this.last = { t, src, mix, w: shares(this.infl) };
  }

  /** 缓存里第 i 帧 (0 = 最早的) 在环里的位置. */
  _slot(i) { return (this.next - this.count + i + this.cap * 2) % this.cap; }

  /** 缓存里第 i 帧: {t, src, mix, infl, w}. */
  frame(i) {
    const k = this._slot(i), r = this.ring;
    const infl = r.infl.slice(k * NL * NP, (k + 1) * NL * NP);
    return { t: r.t[k], src: r.src[k], mix: r.mix[k], infl, w: shares(infl) };
  }

  /** 时间在 [t0, t1] 之间的帧的影响矩阵 (按先后). */
  framesBetween(t0, t1) {
    const out = [];
    for (let i = 0; i < this.count; i++) {
      const k = this._slot(i), t = this.ring.t[k];
      if (t >= t0 && t <= t1) out.push(this.ring.infl.subarray(k * NL * NP, (k + 1) * NL * NP));
    }
    return out;
  }

  /** 一段时间的记录 (跟着执行评估写进日志): 各部位各层的平均权重, 每 step 秒一行的时间线 (每个部位前两层). */
  report(t0, t1, step = 0.2) {
    const frames = [], times = [], srcs = [];
    for (let i = 0; i < this.count; i++) {
      const k = this._slot(i), t = this.ring.t[k];
      if (t < t0 || t > t1) continue;
      frames.push(this.ring.infl.subarray(k * NL * NP, (k + 1) * NL * NP));
      times.push(t);
      srcs.push(this.ring.src[k]);
    }
    const timeline = [];
    let nextT = -Infinity;
    frames.forEach((infl, i) => {
      if (times[i] < nextT) return;
      nextT = times[i] + step;
      const w = shares(infl), parts = {};
      PARTS.forEach((part, p) => {
        const top = topLayers(w, p, 2, 0.05);
        if (top.length) parts[part.id] = top.map(([l, v]) => [l, +v.toFixed(2)]);
      });
      timeline.push({ t: +(times[i] - t0).toFixed(2), src: srcs[i], parts });
    });
    return { frames: frames.length, summary: summarize(frames), timeline };
  }

  /** 把缓存里第 i 帧的姿态放回骨骼和根节点 (回放用; 调用方再更新 VRM、渲染). */
  restore(i) {
    const o = this._slot(i) * this.poseSize, pose = this.ring.pose;
    this.nodes.forEach((n, j) => n.quaternion.set(pose[o + j * 4], pose[o + j * 4 + 1], pose[o + j * 4 + 2], pose[o + j * 4 + 3]));
    const b = o + this.nodes.length * 4;
    if (this.hips) this.hips.position.set(pose[b], pose[b + 1], pose[b + 2]);
    if (this.root) {
      this.root.position.set(pose[b + 3], pose[b + 4], pose[b + 5]);
      this.root.quaternion.set(pose[b + 6], pose[b + 7], pose[b + 8], pose[b + 9]);
    }
  }

  /** 导出整个缓存 (JSON 能存的格式). */
  dump() {
    const frames = [];
    for (let i = 0; i < this.count; i++) {
      const k = this._slot(i);
      frames.push({ t: this.ring.t[k], src: this.ring.src[k], mix: this.ring.mix[k],
        infl: Array.from(this.ring.infl.subarray(k * NL * NP, (k + 1) * NL * NP), (v) => +v.toFixed(2)),
        pose: Array.from(this.ring.pose.subarray(k * this.poseSize, (k + 1) * this.poseSize), (v) => +v.toFixed(5)) });
    }
    return { version: 1, layers: LAYERS.map((l) => l.id), parts: PARTS.map((p) => p.id), poseSize: this.poseSize, frames };
  }

  /** 载入导出的记录 (换掉缓存; 层、部位、骨骼数对不上就报错). */
  load(data) {
    if (data?.version !== 1 || data.poseSize !== this.poseSize || data.layers?.join() !== LAYERS.map((l) => l.id).join()
      || data.parts?.join() !== PARTS.map((p) => p.id).join()) throw new Error('记录的格式或骨骼数和这个模型对不上');
    const n = Math.min(this.cap, data.frames.length), from = data.frames.length - n;
    for (let i = 0; i < n; i++) {
      const f = data.frames[from + i];
      this.ring.infl.set(f.infl, i * NL * NP);
      this.ring.pose.set(f.pose, i * this.poseSize);
      this.ring.t[i] = f.t;
      this.ring.src[i] = f.src;
      this.ring.mix[i] = f.mix;
    }
    this.count = n;
    this.next = n % this.cap;
  }
}
