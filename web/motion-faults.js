// 动作成长 (docs/roadmap/motion-growth.md) 的尺子: 她在日常里 (小屋, desktop/web/room-growth.js) 和评估里
// (web/motion-growth-probe.js, tools/motion_eval.mjs --probe) 用同一套信号、同一套阈值判"这一下动作出了毛病".
//
// 每帧由 Sensor 从行动层读一份信号 (纯数据), FaultDetector 按种类判出失败片段. 失败分两类 (cls):
//   motion: 动作本身不是人做得出来的 —— 一帧里掌心跳一下、拿起放下时东西离手老远 (瞬移)、放下时手砸下去、握着时掌心离东西老远、
//           手臂插进身体、脚打滑 … 交给调参 (tools/motion_growth.py) 或者修代码 (调不好的出问题报告);
//   env:    动作本身正常, 后果来自环境 (满杯端着走就是容易洒) —— 交给她自己的经验学习 (认知那边的 CarryMind), 这里不调.
// 判据看的是动作像不像人, 不看后果大小: 后果 (洒了、脱手) 发生前 LOOKBACK 秒到之后 SETTLE 秒里, 有 motion 类的信号过了阈值
// -> 这次后果是动作缺陷的症状 (cause 记是哪一种); 没有 -> 环境约束. 同样的手部轨迹交给真人也会洒的, 才算环境.
//
// 只有纯函数和不依赖 three.js 的类 (行动层的对象按约定的方法名读), 可以在 Node 里单测.
// 信号里有真值 (东西离掌心多远、洒了多少): 这是身体自检, 和痛觉一样; 只进动作成长的记录, 不进她的认知和提示词.

// 种类: cls, family (哪类本事: carry 拿放端东西 / sit 坐下站起 / walk 走路 / stand 站着 / arms 伸手按东西、手势;
//   只有拿放那几种固定是 carry, 别的 (手臂被推开、脚打滑、甩一下…) 是 null, 按当时在做什么算 —— 坐下时被推开和端着杯子喝时被推开
//   是两个问题, 复现的场景也不一样),
//   src: 读哪个信号 ('f.<名>' 本模块算的, 'pain.<名>' 痛觉 pain.js 的这一帧的值; 列了几个时取第一个有的),
//   rate: 痛觉的值是这一帧的量, 除以 dt 换成每秒 (和帧率无关), thr: 阈值, hold: 连续超过多少秒才算 (0 = 一帧就算),
//   only: 只在这些本事里算 (别的时候这个信号不代表毛病).
// 动作会话往 pain.js 加了"拿着的东西"那几项以后, 在 src 里把 'pain.<新名>' 放到 'f.<名>' 前面就换成官方的量.
export const FAULTS = {
  'grab.gap': { cls: 'motion', family: 'carry', src: ['pain.grab'], unit: 'm', thr: 0.05, hold: 0,
    label: '拿起、放下那一刻东西离手太远 (隔空取物、瞬移)' },
  'hand.jump': { cls: 'motion', family: 'carry', src: ['f.handJump'], unit: 'm', thr: 0.025, hold: 0, only: ['carry'],
    label: '一帧里掌心跳了一下' },
  'put.slam': { cls: 'motion', family: 'carry', src: ['f.handDrop'], unit: 'm/s', thr: 1.2, hold: 0.1, only: ['carry'],
    label: '拿着东西往下放时手砸下去' },
  // 动作会话 2026-10-27 加的痛觉 heldAcc: 拿着东西时掌心持物点的加速度超过 20 m/s² 的部分 x dt; 除以 dt 就是超出的加速度.
  // 拿放起步那一帧跳 5 cm 约 200 m/s², 端着走一步一颠约 10 (不端稳): 超出 30 (也就是 50 m/s² 以上) 算
  'held.acc': { cls: 'motion', family: 'carry', src: ['pain.heldAcc'], rate: true, unit: 'm/s²', thr: 30, hold: 0, only: ['carry'],
    label: '拿着东西时手猛地一顿、一甩 (加速度太大)' },
  'hold.gap': { cls: 'motion', family: 'carry', src: ['f.holdGap'], unit: 'm', thr: 0.06, hold: 0.3, only: ['carry'],
    label: '握着的时候掌心离东西太远' },
  'arm.push': { cls: 'motion', family: null, src: ['pain.pushed'], rate: true, unit: 'm/s', thr: 0.3, hold: 0.3,
    label: '手臂插进身体, 被防穿模层推开' },
  'self.pen': { cls: 'motion', family: null, src: ['pain.selfPen'], unit: 'm', thr: 0.02, hold: 0.2,
    label: '手臂、手里的东西插在身体里' },
  'leg.pen': { cls: 'motion', family: null, src: ['pain.legPen'], unit: 'm', thr: 0.015, hold: 0.2, label: '两条腿互相插进去' },
  furniture: { cls: 'motion', family: null, src: ['pain.furniture'], unit: 'm', thr: 0.04, hold: 0.3, label: '手脚膝盖插进家具' },
  'item.pen': { cls: 'motion', family: 'carry', src: ['pain.itemPen'], unit: 'm', thr: 0.02, hold: 0.3, label: '东西插进家具、地面' },
  'foot.slide': { cls: 'motion', family: null, src: ['pain.slide'], rate: true, unit: 'm/s', thr: 0.15, hold: 0.15,
    label: '支撑脚打滑' },
  floating: { cls: 'motion', family: null, src: ['pain.floating'], unit: 'm', thr: 0.03, hold: 0.3, label: '站着时两只脚离地' },
  balance: { cls: 'motion', family: null, src: ['pain.balance'], unit: 'm', thr: 0.04, hold: 0.5, label: '重心出了支撑面 (要摔)' },
  joint: { cls: 'motion', family: null, src: ['pain.joint'], unit: 'deg', thr: 20, hold: 0.3, label: '关节超出活动范围' },
  jerk: { cls: 'motion', family: null, src: ['pain.jerk'], rate: true, unit: 'rad/s²', thr: 40, hold: 0.1, label: '骨骼突然甩一下' },
  // 后果: 先记下, 按前后有没有动作缺陷分到 motion / env (cls 由 classify 定)
  spill: { cls: null, family: 'carry', consequence: true, src: ['f.spillMl'], unit: 'ml', thr: 3, window: 1, label: '洒出来了' },
  drop: { cls: null, family: 'carry', consequence: true, src: ['f.dropped'], unit: 'count', thr: 1, label: '东西脱手掉了' },
  // 刚体的抓持在滑 (web/physics/rigid-scene.js 的 slip 事件: 东西偏离掌心超过一定距离): 小屋读 r.rigid.layer.events, 评估事后读 carry.phys.events
  slip: { cls: null, family: 'carry', consequence: true, src: ['f.slipped'], unit: 'count', thr: 1, label: '握着的东西在手里滑了' },
};

export const LOOKBACK = 1.0;  // 后果之前多少秒里有动作缺陷就算它的症状
export const SETTLE = 0.3;    // 后果之后再等这么久 (要连续超过 hold 秒才算的缺陷, 判出来会晚一点)
export const COOLDOWN = 8;    // 同一种失败这么多秒内不重复记 (同一个动作里也只记一次)
const RING_S = 1.6;           // 记下最近这么多秒的逐帧摘要, 失败片段带上

/** marks ([{t, kind, r}], motion 类信号超过阈值的时刻) 里, 时刻 t 之前 lookback 秒到之后 settle 秒最严重的那种; 没有返回 null. */
export function causeAt(marks, t, faults = FAULTS, lookback = LOOKBACK, settle = SETTLE) {
  let best = null;
  for (const m of marks) {
    if (m.t < t - lookback - 1e-9 || m.t > t + settle + 1e-9) continue;
    if (faults[m.kind]?.cls !== 'motion') continue;
    if (!best || m.r > best.r) best = m;
  }
  return best ? best.kind : null;
}

/** 评估里事后给洒的事件分类: spills = [[秒, 毫升]] (motion-eval.js 的 carry.spillTimes), 1 秒里超过阈值算一次. */
export function spillEvents(spills, marks, faults = FAULTS) {
  const def = faults.spill, out = [];
  let win = [], last = -Infinity;
  for (const [t, ml] of spills || []) {
    win.push([t, ml]);
    win = win.filter(([s]) => t - s <= (def.window ?? 1));
    const sum = win.reduce((s, [, m]) => s + m, 0);
    if (sum >= def.thr && t - last >= COOLDOWN) {
      const cause = causeAt(marks, t, faults);
      out.push({ kind: 'spill', t, value: +sum.toFixed(2), peak: +sum.toFixed(2), sev: +(sum / def.thr).toFixed(2), cls: cause ? 'motion' : 'env', cause });
      last = t;
      win = [];
    }
  }
  return out;
}

/** 评估里事后给滑、脱手分类: events = [[阶段, 'slip' | 'drop' | ..., 秒]] (motion-eval.js 的 carry.phys.events). */
export function gripEvents(events, marks, faults = FAULTS) {
  const out = [], last = {};
  for (const [, type, t] of events || []) {
    if (type !== 'slip' && type !== 'drop') continue;
    if (last[type] !== undefined && t - last[type] < COOLDOWN) continue;
    last[type] = t;
    const cause = causeAt(marks, t, faults);
    out.push({ kind: type, t, value: 1, peak: 1, sev: 1, cls: cause ? 'motion' : 'env', cause });
  }
  return out;
}

/** 一种失败的信号值: src 里第一个有的 (pain 的按 rate 换成每秒). 没有返回 null. */
export function signalValue(def, sig) {
  for (const s of def.src) {
    const [where, name] = s.split('.');
    const v = where === 'pain' ? sig.pain?.[name] : sig.f?.[name];
    if (v === undefined || v === null || !Number.isFinite(v)) continue;
    return def.rate ? v / Math.max(1e-4, sig.dt) : v;
  }
  return null;
}

/** 这一帧在做哪类本事: 拿着东西或者在拿放 -> carry; 坐下站起、坐着 -> sit; 走 -> walk; 伸手按东西 -> arms; 别的 -> stand. */
export function familyOf(ctx) {
  if (ctx?.held?.length || ctx?.action?.kind === 'pick' || ctx?.action?.kind === 'put') return 'carry';
  if (ctx?.action?.kind === 'sit' || ctx?.action?.kind === 'stand' || ctx?.seated) return 'sit';
  if (ctx?.mode === 'go') return 'walk';
  if (ctx?.action?.kind === 'reach') return 'arms';
  return 'stand';
}

/** 一次动作 (同一段片段、同一次伸手) 的标识, 同一种失败在同一次动作里只记一次; 也用来数做了多少次 (曝光). */
export function actionKey(ctx) {
  const a = ctx?.action;
  if (a) return `${a.kind}|${a.clip || ''}|${a.t0 ?? ''}`;
  if (ctx?.mode === 'go') return `go|${ctx.goT0 ?? ''}`;
  return null;
}

/**
 * 检测器: push(信号) 每帧一次, 返回这一帧判出的失败 [{kind, cls, cause, family, t, value, peak, thr, sev, ctx, ring}].
 * 信号 sig = { t 秒, dt, ctx: {mode, action: {kind, clip, f, n, t0, to, toY, item}, held: [{id, side, physical}], seated, activity, pose},
 *   pain: {痛觉这一帧的各项}, f: {handJump, handDrop, holdGap, spillMl, dropped} }.
 * 也数曝光 (每类本事做了几次: 一次拿放、一次坐下、一段路、站着每分钟), 算失败率用.
 */
export class FaultDetector {
  constructor({ faults = FAULTS, lookback = LOOKBACK, settle = SETTLE, cooldown = COOLDOWN, keepMarks = false } = {}) {
    Object.assign(this, { faults, lookback, settle, cooldown, keepMarks });
    this.over = {};      // 种类 -> {since, peak, value} 连续超过阈值的这一段
    this.last = {};      // 种类 -> 上次记的时刻
    this.done = {};      // 种类 -> 上次记的那次动作
    this.ring = [];      // 最近 RING_S 秒的逐帧摘要 {t, fam, act, r: 最大的 值/阈值 (motion 类), k: 是哪一种, v: 几个特征}
    this.marks = [];     // 最近的 motion 类超阈值 (不管连续够不够久) {t, kind, r}: 给后果找原因
    this.pending = [];   // 等 SETTLE 秒再分类的后果
    this.spillWin = [];  // 最近 1 秒洒出来的 [t, ml]
    this.exposure = {};  // 本事 -> 次数 (拿放按次、坐下站起按次、走按段, 站着按分钟)
    this.standS = 0;
    this.prevKey = null;
    this.faultsSeen = 0;
  }

  _count(fam) { this.exposure[fam] = (this.exposure[fam] || 0) + 1; }

  push(sig) {
    const out = [], t = sig.t, ctx = sig.ctx || {}, fam = familyOf(ctx), key = actionKey(ctx);
    if (key && key !== this.prevKey) this._count(fam);
    this.prevKey = key;
    if (fam === 'stand' && !key) {
      this.standS += sig.dt;
      if (this.standS >= 60) { this.standS -= 60; this._count('stand'); }
    }
    let worst = 0, worstK = null;
    for (const [kind, def] of Object.entries(this.faults)) {
      if (def.consequence) continue;
      if (def.only && !def.only.includes(fam)) { delete this.over[kind]; continue; }
      const v = signalValue(def, sig);
      if (v === null) continue;
      const r = v / def.thr;
      if (r > worst) { worst = r; worstK = kind; }
      if (r < 1) { delete this.over[kind]; continue; }
      this.marks.push({ t, kind, r });
      const o = this.over[kind] ??= { since: t, peak: v };
      o.peak = Math.max(o.peak, v);
      if (t - o.since + sig.dt < def.hold - 1e-9) continue;
      if (this._recent(kind, t, key)) continue;
      out.push(this._fault(kind, def, def.cls, null, fam, t, v, o.peak, ctx));
    }
    this.ring.push({ t: +t.toFixed(3), fam, act: ctx.action ? `${ctx.action.kind}:${ctx.action.clip || ''}@${ctx.action.f ?? ''}` : ctx.mode || null,
      r: +worst.toFixed(2), k: worst >= 0.5 ? worstK : null,
      v: Object.fromEntries(Object.entries(sig.f || {}).filter(([, x]) => Number.isFinite(x) && x).map(([k, x]) => [k, +x.toFixed(4)])) });
    while (this.ring.length && t - this.ring[0].t > RING_S) this.ring.shift();
    while (!this.keepMarks && this.marks.length && t - this.marks[0].t > this.lookback + this.settle + 0.5) this.marks.shift();
    // 后果: 洒 (1 秒里洒出来超过阈值)、脱手 (这一帧掉了)
    const spill = this.faults.spill;
    const ml = sig.f?.spillMl || 0;
    if (spill && ml > 0) this.spillWin.push([t, ml]);
    while (this.spillWin.length && t - this.spillWin[0][0] > (spill?.window ?? 1)) this.spillWin.shift();
    const inWin = this.spillWin.reduce((s, [, m]) => s + m, 0);
    if (spill && inWin >= spill.thr && !this._recent('spill', t, null)) {
      this.pending.push({ kind: 'spill', def: spill, t, value: inWin, fam, ctx });
      this.last.spill = t;
      this.spillWin.length = 0;
    }
    for (const [kind, key] of [['drop', 'dropped'], ['slip', 'slipped']]) {
      const def = this.faults[kind];
      if (def && (sig.f?.[key] || 0) >= def.thr && !this._recent(kind, t, null)) {
        this.pending.push({ kind, def, t, value: sig.f[key], fam, ctx });
        this.last[kind] = t;
      }
    }
    // 等够 SETTLE 秒的后果分类
    for (let i = this.pending.length - 1; i >= 0; i--) {
      const p = this.pending[i];
      if (t - p.t < this.settle - 1e-9) continue;
      this.pending.splice(i, 1);
      const cause = this.causeOf(p.t);
      out.push(this._fault(p.kind, p.def, cause ? 'motion' : 'env', cause, p.fam, p.t, p.value, p.value, p.ctx));
    }
    return out;
  }

  /** 时刻 t 附近 (之前 lookback 秒到之后 settle 秒) 最严重的 motion 类超阈值, 没有返回 null. */
  causeOf(t) { return causeAt(this.marks, t, this.faults, this.lookback, this.settle); }

  _recent(kind, t, key) {
    if (key && this.done[kind] === key) return true;
    return this.last[kind] !== undefined && t - this.last[kind] < this.cooldown;
  }

  _fault(kind, def, cls, cause, fam, t, value, peak, ctx) {
    this.last[kind] = t;
    const key = actionKey(ctx);
    if (key) this.done[kind] = key;
    this.faultsSeen++;
    return { kind, cls, cause, family: def.family || fam, t: +t.toFixed(3), value: +value.toFixed(4), peak: +peak.toFixed(4),
      thr: def.thr, unit: def.unit, sev: +(peak / def.thr).toFixed(2), label: def.label, ctx, ring: this.ring.slice(-45) };
  }

  /** 场景、一段日常的汇总 (评估里每个场景一份): 每种失败几次、最高多少, 超过阈值一半以上的部分的积分 (调参用的平滑量). */
  static summarize(records, excess) {
    const by = {};
    for (const r of records) {
      const b = by[r.kind] ??= { n: 0, peak: 0, motion: 0, env: 0 };
      b.n++;
      b.peak = Math.max(b.peak, r.peak);
      if (r.cls === 'motion') b.motion++; else if (r.cls === 'env') b.env++;
    }
    return { faults: by, excess };
  }
}

/**
 * 调参用的平滑量: 每帧每种 motion 类信号超过阈值一半的部分 (按阈值归一) 乘 dt 累加; hold 为 0 的 (一帧就算的) 不乘 dt, 按次累加.
 * 毛病越轻越接近 0, 没超过一半就是 0. 只数种类里 only 允许的本事.
 */
export class Excess {
  constructor(faults = FAULTS) { this.faults = faults; this.sum = {}; }

  push(sig) {
    const fam = familyOf(sig.ctx);
    for (const [kind, def] of Object.entries(this.faults)) {
      if (def.consequence || (def.only && !def.only.includes(fam))) continue;
      const v = signalValue(def, sig);
      if (v === null) continue;
      const e = Math.max(0, v / def.thr - 0.5);
      if (e > 0) this.sum[kind] = (this.sum[kind] || 0) + (def.hold ? e * sig.dt : e);
    }
  }

  get values() { return Object.fromEntries(Object.entries(this.sum).map(([k, v]) => [k, +v.toFixed(4)])); }
}

/**
 * 从行动层读信号 (每帧, 行动层、姿势层、刚体都摆完以后): 掌心位置算跳变和下落速度、握着的东西离掌心多远.
 * loco 要有 _palmHold(item, side)、items、action、mode、seated、char; 位置对象有 x、y、z (three.js 的 Vector3 也行).
 * extra: 这一帧另外的 {spillMl, dropped, activity, pose} (液体、刚体在评估和小屋里接法不一样, 由调用方给).
 */
export class Sensor {
  constructor(loco) {
    this.loco = loco;
    this.hist = { left: [], right: [] };  // 每只手最近 4 帧的掌心 [x, y, z]
    this.actT0 = null;
    this.lastAction = null;
    this.goT0 = null;
    this.wasDropped = {};
  }

  _itemNear(c) {
    let best = null, bd = 0.25;
    for (const it of Object.values(this.loco.items || {})) {
      const m = it.mesh?.position;
      if (!m) continue;
      const d = Math.hypot(m.x - c.x, m.y - c.y, m.z - c.z);
      if (d < bd) { bd = d; best = it.id; }
    }
    return best;
  }

  _palm(side) {
    try {
      const p = this.loco._palmHold({ half: 0 }, side);
      return [p.x, p.y, p.z];
    } catch { return null; }
  }

  read(t, dt, pain = {}, extra = {}) {
    const L = this.loco, a = L.action;
    if (a !== this.lastAction) { this.lastAction = a; this.actT0 = a ? t : null; }
    if (L.mode === 'go' && this.goT0 === null) this.goT0 = t;
    if (L.mode !== 'go') this.goT0 = null;
    const held = Object.values(L.items || {}).filter((i) => i.held).map((i) => ({ id: i.id, side: i.held, physical: !!i.physical }));
    const kind = a?.grab?.kind || (a?.seat ? (a.rise ? 'stand' : 'sit') : a ? 'act' : null);
    if (!a && L.reaching && this.actT0 === null) this.actT0 = t;
    if (!a && !L.reaching) this.actT0 = null;
    // 拿放的台面多高 (要拿的东西 / 放下后东西的底面), 是哪件东西 (离那一点最近的)
    const hold = a?.grab?.hold || L.reaching?.hold || null, c = hold?.center;
    const y = c ? +(c.y - (hold.half || 0)).toFixed(3) : null, at = c ? [+c.x.toFixed(2), +c.z.toFixed(2)] : null;
    const item = c ? this._itemNear(c) : null;
    const action = a ? { kind, clip: a.c?.name || null, f: Math.round((a.frame ?? 0) - (a.start ?? 0)), n: Math.round((a.end ?? 0) - (a.start ?? 0)),
      t0: +this.actT0.toFixed(2), y, at, item } : (L.reaching ? { kind: L.reaching.kind || 'reach', clip: 'reach', f: null, n: null,
      t0: +(this.actT0 ?? t).toFixed(2), y, at, item } : null);
    const ctx = { mode: L.mode, action, held, seated: L.seated?.seat?.id || (L.seated ? 'seat' : null), goT0: this.goT0,
      activity: extra.activity ?? null, pose: extra.pose ?? null };
    const f = { handJump: 0, handDrop: 0, holdGap: 0, spillMl: extra.spillMl || 0, dropped: extra.dropped || 0, slipped: extra.slipped || 0 };
    const busy = held.length || kind === 'pick' || kind === 'put' || !!L.reaching;
    for (const side of ['left', 'right']) {
      const p = this._palm(side), h = this.hist[side];
      if (!p) continue;
      h.push(p);
      if (h.length > 4) h.shift();
      if (!busy) continue;
      const d = (i, j) => Math.hypot(h[i][0] - h[j][0], h[i][1] - h[j][1], h[i][2] - h[j][2]);
      // 跳变: 上一帧的位移比前后两帧都大出一截 (一下子跳过去); 连着快速移动 (前后位移都大) 不算
      if (h.length === 4) f.handJump = Math.max(f.handJump, d(2, 1) - Math.max(d(1, 0), d(3, 2)) - 0.005);
      // 往下的速度: 拿着东西、在拿放时
      if (h.length >= 2) f.handDrop = Math.max(f.handDrop, (h[h.length - 2][1] - h[h.length - 1][1]) / Math.max(1e-4, dt));
    }
    for (const it of Object.values(L.items || {})) {
      // 握着的刚体: 掌心目标 (item.target) 和东西实际在哪差多远; 往落点放的时候 (target.place) 目标是落点, 不算
      if (it.held && it.physical && it.target?.p && !it.target.place) {
        const m = it.mesh.position, q = it.target.p;
        f.holdGap = Math.max(f.holdGap, Math.hypot(m.x - q[0], m.y - q[1], m.z - q[2]));
      }
      // 脱手: item.dropped 由 false 变成 true 的那一下 (物理那边再握住时会清成 false; 不交给物理的东西永远不设)
      if (it.dropped && !this.wasDropped[it.id]) f.dropped += 1;
      this.wasDropped[it.id] = !!it.dropped;
    }
    f.handJump = Math.max(0, f.handJump);
    f.handDrop = Math.max(0, f.handDrop);
    return { t, dt, ctx, pain, f };
  }
}

/** 用户能看懂的一句: 她换上 / 退回一版动作参数时说. family 见 FAULTS. */
export const LINES = {
  carry: { better: ['我最近把拿杯子练稳了', '放东西的时候手不会砸下去了', '端东西比以前稳多了'],
    back: ['前两天练的拿东西的法子不太对, 我先换回原来的了'] },
  sit: { better: ['坐下站起比以前顺了'], back: ['新练的坐法不太舒服, 先换回原来的了'] },
  walk: { better: ['走路脚不打滑了'], back: ['新练的走法不太稳, 先换回原来的了'] },
  stand: { better: ['站着比以前稳了'], back: ['新练的站姿不太对, 先换回原来的了'] },
  arms: { better: ['手臂不会老撞到身上了'], back: ['新练的手上动作不太对, 先换回原来的了'] },
};
