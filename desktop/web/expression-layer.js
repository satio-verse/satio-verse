// 表情层 (2D 门面): 取代 Cubism 自带的表情管理器, 让情绪之间连续过渡、会随时间衰减.
// 纯 JS, 不碰 DOM 和 Cubism 本体 (参数的读写交给调用方给的 io), 所以不靠真模型也能测.
//
//   姿势 (pose)  = 一个表情文件 (exp3.json) 解出来的 {参数: 值, 混合方式}. 每个姿势一个权重, 权重用临界阻尼弹簧追目标:
//                  默认 0.4 秒到 95% (参考 LumenAct 的约 0.4 秒过渡); 中途改主意时位置和速度都连续, 所以任意两个情绪之间不跳.
//   情绪 (emotion) = 一个慢变量: setEmotion 把强度 level 置 1; 她说话时保持, 说完保持 holdS 秒, 然后按半衰期向平静衰减.
//   AU (面部动作单元) = 闲时状态机和对话反应给的 0..1 强度, 经能力表 (expression-caps.js) 换成参数, 以"加"的方式叠上去.
//
// 混合方式照 Framework 的算法 (cubismexpressionmotion.js calculateExpressionParameters):
//   Add: 加数 += 值;  Multiply: 乘数 = 值;  Overwrite: 覆盖值 = 值;  最后 (覆盖 + 加数) × 乘数 写回参数.
//   单个姿势稳态时与 Framework 逐位一致; 过渡中多个姿势按权重做线性混合 (Framework 是按序两两插值, 等价于权重和为 1 时的同一结果).
//   不认识的混合方式: 不当 Add 算, 去掉那一项并记进 report.unknown_blend.
import { AU_TABLE } from './expression-caps.js';

const OMEGA_95 = 4.744;  // 临界阻尼弹簧 (1 + x) e^-x = 0.05 的解: ω = OMEGA_95 / 到 95% 的秒数
const BLENDS = { add: 0, multiply: 1, overwrite: 2 };
const EPS = 1e-5;

export class ExpressionLayer {
  /**
   * poses: Map 姿势名 -> exp3 对象 ({Parameters: [{Id, Value, Blend}]});
   * emotions: {标签: {姿势名: 权重}} (expression-caps.js 的 emotionTable);
   * hasParam(名) / range(名): 模型有没有这个参数、参数全幅 (统计用);
   * 其余是时间常数 (秒): riseS 到 95%, holdS 说完后保持多久再衰减, halfLifeS 衰减半衰期.
   */
  constructor({ poses, emotions, hasParam = () => true, range = () => 2, au = AU_TABLE, riseS = 0.4, holdS = 3, halfLifeS = 20 } = {}) {
    this.omega = OMEGA_95 / riseS;
    this.holdS = holdS;
    this.halfLifeS = halfLifeS;
    this.names = [];
    this.index = new Map();
    this.ranges = [];
    this.report = { poses: {}, emotions: {}, degraded: [], unknown_blend: [], au_missing: {}, unknown_labels: [] };
    const pidx = (name) => {
      let i = this.index.get(name);
      if (i === undefined) {
        i = this.names.length;
        this.names.push(name);
        this.index.set(name, i);
        this.ranges.push(Math.max(1e-6, Number(range(name)) || 2));
      }
      return i;
    };
    // 姿势
    this.poses = [];
    this.poseIndex = new Map();
    for (const [name, exp] of poses || []) {
      const p = { name, add: [], mul: [], ov: [] };
      const rep = { params: 0, missing: [] };
      for (const e of exp?.Parameters || []) {
        const blend = BLENDS[String(e.Blend ?? 'Add').toLowerCase()];
        if (blend === undefined) { this.report.unknown_blend.push(`${name}:${e.Id}:${e.Blend}`); continue; }
        if (!hasParam(e.Id)) { rep.missing.push(e.Id); continue; }
        (blend === 0 ? p.add : blend === 1 ? p.mul : p.ov).push([pidx(e.Id), Number(e.Value) || 0]);
        rep.params++;
      }
      this.report.poses[name] = rep;
      this.poseIndex.set(name, this.poses.length);
      for (const k of ['add', 'mul', 'ov']) {  // 每帧用下标循环, 不解构
        p[k + 'I'] = Int32Array.from(p[k], (e) => e[0]);
        p[k + 'V'] = Float64Array.from(p[k], (e) => e[1]);
      }
      this.poses.push(p);
    }
    // 情绪 -> 姿势权重 (姿势不存在 = 去掉并记录, 这个情绪退化成平静)
    this.emotions = {};
    for (const [label, table] of Object.entries(emotions || {})) {
      const kept = {};
      const missing = [];
      for (const [pose, w] of Object.entries(table || {})) {
        if (this.poseIndex.has(pose)) kept[pose] = Number(w) || 0; else missing.push(pose);
      }
      this.emotions[label] = kept;
      const full = Object.keys(table || {}).length;
      const lost = missing.length || Object.keys(kept).every((k) => this.report.poses[k].missing.length > 0 && this.report.poses[k].params === 0);
      this.report.emotions[label] = { pose: Object.keys(table || {}), missing };
      if (full && lost) this.report.degraded.push(label);
    }
    for (const [pose, rep] of Object.entries(this.report.poses)) if (rep.missing.length) this.report.degraded.push(`pose:${pose}(缺 ${rep.missing.join(',')})`);
    // AU
    this.au = {};
    this.auI = [];
    this.auC = [];
    for (const [name, list] of Object.entries(au)) {
      const kept = [];
      const missing = [];
      for (const [param, k] of list) (hasParam(param) ? kept.push([pidx(param), k]) : missing.push(param));
      this.au[name] = kept;
      this.auI.push(Int32Array.from(kept, (e) => e[0]));
      this.auC.push(Float64Array.from(kept, (e) => e[1]));
      if (missing.length) this.report.au_missing[name] = missing;
    }
    // 状态
    const n = this.poses.length, m = this.names.length;
    this.w = new Float64Array(n);
    this.v = new Float64Array(n);
    this.target = new Float64Array(n);
    this.A = new Float64Array(m);  // 加数
    this.M = new Float64Array(m).fill(1);  // 乘数
    this.O = new Float64Array(m);  // 覆盖的权重和 (Σ w)
    this.OV = new Float64Array(m);  // 覆盖: Σ w × 值
    this.prevA = new Float64Array(m);
    this.complex = new Uint8Array(m);  // 有 Multiply / Overwrite 的参数
    for (const p of this.poses) for (const [i] of [...p.mul, ...p.ov]) this.complex[i] = 1;
    this.auVal = new Float64Array(Object.keys(this.au).length);
    this.auNames = Object.keys(this.au);
    this.auDirty = true;
    this.active = [];  // 这一帧有贡献的参数号
    this.emo = { label: 'neutral', weights: {}, level: 1, held: 0, sticky: false };
    this.settled = true;
    this.work = 0;      // 做过多少次重算 (测试: 稳态不该重算)
    this.stats = { maxStep: 0, frames: 0 };
    this.dirty = true;
  }

  /** 设情绪. strength 这一步忽略 (一律 1): 强度和情绪坐标的对应放后续, 这样稳态外观与改前一致. 返回这个标签认不认识. */
  setEmotion(label, _strength) {
    const known = Object.prototype.hasOwnProperty.call(this.emotions, label);
    if (!known && !this.report.unknown_labels.includes(label)) this.report.unknown_labels.push(String(label));
    this.emo = { label, weights: known ? this.emotions[label] : {}, level: 1, held: 0, sticky: false };
    this.settled = false;
    return known;
  }

  /** 直接用姿势名 (自检和面板预览): 不衰减. null 收掉. */
  setExpression(name) {
    const ok = name !== null && this.poseIndex.has(name);
    this.emo = { label: ok ? `#${name}` : 'neutral', weights: ok ? { [name]: 1 } : {}, level: 1, held: 0, sticky: ok };
    this.settled = false;
    return ok || name === null;
  }

  /** 闲时状态机和对话反应给的 AU 强度 {名: 0..1}. 没给的当 0. 没有这个 AU 的模型 (能力表里缺) 静默忽略, 缺了什么在 report.au_missing. */
  setAU(values) {
    for (let k = 0; k < this.auNames.length; k++) {
      const x = Math.max(0, Math.min(1, Number(values?.[this.auNames[k]]) || 0));
      if (Math.abs(x - this.auVal[k]) > EPS) { this.auVal[k] = x; this.auDirty = true; }
    }
  }

  /** 往前走 dt 秒. speaking: 她正在出声 (情绪保持). halfLifeS: 这一帧的衰减半衰期 (走神时缩短). */
  update(dt, opt) {
    const speaking = opt ? !!opt.speaking : false;
    const halfLifeS = opt && opt.halfLifeS ? opt.halfLifeS : this.halfLifeS;
    dt = Number.isFinite(dt) && dt > 0 ? Math.min(dt, 1e6) : 0;  // 页面藏起来再显示时 dt 会很大: 弹簧和衰减都是精确解, 吃得下
    const e = this.emo;
    if (!e.sticky) {
      if (speaking) e.held = 0;
      else {
        const before = e.held;
        e.held += dt;
        const decayDt = Math.min(dt, Math.max(0, e.held - Math.max(this.holdS, before)));
        if (decayDt > 0 && e.level > 0) {
          e.level *= Math.pow(0.5, decayDt / Math.max(0.5, halfLifeS));
          if (e.level < 0.01) e.level = 0;
          this.settled = false;
        }
      }
    }
    if (!this.settled) {
      const w0 = this.omega;
      let moving = false;
      for (let i = 0; i < this.poses.length; i++) {
        const t = (e.weights[this.poses[i].name] || 0) * e.level;
        this.target[i] = t;
        if (dt > 0) {  // 临界阻尼弹簧的精确解 (和帧率无关, dt 再大也不会发散)
          const d = this.w[i] - t, c = this.v[i] + w0 * d, ex = Math.exp(-w0 * dt);
          this.w[i] = t + (d + c * dt) * ex;
          this.v[i] = (this.v[i] - w0 * c * dt) * ex;
        }
        if (Math.abs(this.w[i] - t) > EPS || Math.abs(this.v[i]) > EPS) moving = true;
        else { this.w[i] = t; this.v[i] = 0; }
      }
      this.settled = !moving;  // 目标会变的两处 (setEmotion、衰减) 都会把它重新置 false
      this.dirty = true;
    }
    if (this.dirty || this.auDirty) this._recompute();
  }

  _recompute() {
    this.work++;
    this.dirty = this.auDirty = false;
    const { A, M, O, OV, active } = this;
    A.fill(0); M.fill(1); O.fill(0); OV.fill(0);
    for (let k = 0; k < this.poses.length; k++) {
      const w = this.w[k];
      if (Math.abs(w) < EPS) continue;
      const p = this.poses[k];
      for (let j = 0; j < p.addI.length; j++) A[p.addI[j]] += w * p.addV[j];
      for (let j = 0; j < p.mulI.length; j++) M[p.mulI[j]] += w * (p.mulV[j] - 1);
      for (let j = 0; j < p.ovI.length; j++) { O[p.ovI[j]] += w; OV[p.ovI[j]] += w * p.ovV[j]; }
    }
    for (let k = 0; k < this.auNames.length; k++) {
      const s = this.auVal[k];
      if (s > EPS) { const ai = this.auI[k], ac = this.auC[k]; for (let j = 0; j < ai.length; j++) A[ai[j]] += s * ac[j]; }
    }
    active.length = 0;
    let step = 0;
    for (let i = 0; i < A.length; i++) {
      if (A[i] !== 0 || M[i] !== 1 || O[i] !== 0) active.push(i);
      const d = Math.abs(A[i] - this.prevA[i]) / this.ranges[i];
      if (d > step) step = d;
      this.prevA[i] = A[i];
    }
    if (step > this.stats.maxStep) this.stats.maxStep = step;
  }

  /**
   * 写回参数: io = { get(名), set(名, 值), add(名, 值) }. Add 的参数走 add (不用读); 有 Multiply / Overwrite 的参数读当前值再 set.
   * 在 Framework 更新调度器里与原表情更新器同一个执行序 (见 createUpdater), 所以加在眨眼之后.
   */
  write(io) {
    this.stats.frames++;
    const { A, M, O, OV, active, names } = this;
    for (let k = 0; k < active.length; k++) {
      const i = active[k];
      if (!this.complex[i]) { io.add(names[i], A[i]); continue; }
      const c = io.get(names[i]);
      io.set(names[i], (c + OV[i] - O[i] * c + A[i]) * M[i]);
    }
  }

  /** 这一刻是不是还在快速变化 (页面据此决定要不要临时升帧; 慢衰减不算). */
  get animating() {
    for (let i = 0; i < this.v.length; i++) if (Math.abs(this.v[i]) > 0.05) return true;
    return false;
  }

  /** 自检: 层的输出 (加数) 相邻帧最大变化, 占参数全幅的比例; 读完清零. */
  takeStats() {
    const s = { max_step: +this.stats.maxStep.toFixed(4), frames: this.stats.frames, work: this.work };
    this.stats.maxStep = 0;
    this.stats.frames = 0;
    return s;
  }
}

/**
 * 给 Framework 的更新调度器用的更新器: Base 是 icubismupdater.js 的 ICubismUpdater, order 取 CubismUpdateOrder_Expression (300),
 * 和原来的表情更新器同一位置. layer.update 由调用方在每帧 onLateUpdate 之前调 (需要知道她有没有在说话).
 */
export function createUpdater(Base, order, layer, io) {
  return new (class LayerUpdater extends Base {
    constructor() { super(order); }
    onLateUpdate(_model, _dt) { layer.write(io); }
  })();
}
