// 杯里的液体和洒出去以后 (小窝的物理与后果 P0, 见 docs/roadmap/room-physics.md): 液量、温度、晃动、洒出、落到哪、烫到哪、
// 一摊摊的水渍会冷、会干. 纯计算, 不依赖 three.js (Node 里直接单测); 杯子怎么动由宿主每帧给 (现在是掌心, 以后是刚体).
//
// 物理环境决定实际发生什么: 洒没洒、洒多少、落在哪, 都在这里算; 她 (认知) 只能通过看到、碰到、烫到知道结果.
// 这里的量 (液量、温度、每一摊) 是真值, 不能直接进她的候选和提示词.
//
// 单位: 长度米, 液量毫升 (ml), 温度摄氏度, 时间秒. 位置、速度是世界坐标 [x, y, z] (y 朝上); 朝向是四元数 [x, y, z, w],
// 杯子局部 +Y 是杯口方向.
//
// 晃动: 一阶晃动的等效阻尼振子 (每个水平方向一个). 杯子的比力 f = a - g (加速度减重力) 换到杯子坐标里, 液面的静态坡度
// 是 -f_水平 / f_轴向 (倾斜和加速度一起算). 圆柱杯液面贴壁处的升高里, 第一阶振型占 2 / (1.841² - 1) = 0.837,
// 其余高阶 (频率高得多) 当作立刻跟上. 第一阶固有频率 ω² = (g·1.841 / R)·tanh(1.841·h / R).
// 洒出: 液面 (按坡度、液量解出来的平面, 杯口以上截掉) 在杯沿以上的部分按堰流 Q = Cd·(2/3)·√(2g)·∫(超出高度)^1.5 沿杯沿流出.
// 倾斜很大时 (喝、倒) 液面碰到杯底, 用数值积分算装得下多少.

export const G = 9.81;

/** 可调参数 (评测台按实测调, 见知识库实验记录). */
export const LIQUID = {
  // 模型的版本: 改了行为 (不只是常数) 就加一, 评测台的物理指纹 (motion-eval.js 的 carryEnv) 才分得出来.
  // 1 P0; 2 失重; 3 高阶振型按振子跟上 (临界阻尼)、倒过来时直接倒光; 4 印子合并 + 条数上限 (撤了);
  // 5 印子只按写明的规则变: 被擦掉、挨着的并成一块 (面积、干掉的量守恒), 不按条数删 (2026-10-27)
  version: 5,
  // 印子 (干了的咖啡、牛奶) 怎么变, 只有三种, 都写在这里: 她擦掉 (wipe); 刚干成印子时和同一处台面上边缘相距 stainMerge 以内的
  // 同种印子并成一块 (面积相加、干掉的量相加, 中心按面积取); 不会自己淡掉 (咖啡、牛奶印子放着不会自己没). 世界里的印子不按条数删:
  // 她看见的、擦的都是这份状态, 按条数删的话房间会无缘无故变干净. 画的时候怎么合并、实例化、省细节是画的那边的事 (不改这份状态)
  stainMerge: 0.05,
  zeta: 0.06,        // 第一阶晃动的阻尼比
  cd: 0.6,           // 堰流系数
  fast: 0.163,       // 贴壁升高里高阶振型的份额 (按第一阶 3 倍频率的振子跟上, 见 _cupAt)
  // 杯子跟着手: 动画给的掌心位置不是物理上连续的 (换段、混合时一帧跳 1-2 cm, 算出来 30-50 m/s²), 真的手腕和握持有弹性,
  // 杯子也有惯性. 杯子按弹簧阻尼跟着掌心走 (gripHz 赫兹、临界阻尼), 加速度最多 aMax. 刚体抓持 (P2) 以后由约束代替
  gripHz: 4,
  aMax: 6,           // 米/秒²
  teleport: 0.25,    // 一帧挪这么远 (米) 当作瞬移 (拿起、放下时的跳变), 不算加速度
  handShare: 0.8,    // 慢慢溢出时顺着杯身流到握杯那只手上的份额 (流得急时甩出去, 少)
  jetMlS: 15,        // 流量到这么大 (ml/s) 时基本是甩出去的
  film: 0.4,         // 每个身体部位最多沾住多少 (ml), 多的滴下去
  // 喝只看几何, 不看她想不想喝: 握着的杯子, 流出来的那处杯沿离嘴这么近 (米)、嘴在流出来的那一侧, 才算喝进去.
  // 宿主说正在喝 (drinking) 只是提示: 放宽到 drinkHint (嘴的位置估得差一两厘米), 离得更远照样洒.
  // (以前 drinking 一标, 溢出来的全算喝掉, 不看杯子在哪; 杯子离嘴 12 cm 内也都算. 2026-10-27 用户转来的评估指出来的)
  // 每秒最多咽这么多 (ml), 涌得更多的照样洒
  drinkReach: 0.03,
  drinkHint: 0.045,
  gulpMlS: 25,
  mouthMl: 25,       // 嘴里一下子能含多少 (ml)
  env: 22,           // 室温
  kCup: 9e-4,        // 60 ml 的浓缩咖啡杯 (R 3 cm) 的冷却速率 (1/秒): 70 ℃ 十分钟左右到 50 ℃
  kSkin: 0.05,       // 沾在身上的一层: 很快凉到体温附近
  skin: 33,
  filmDepth: 0.0008, // 洒在台面、地上摊开的厚度 (米)
  evap: 5.6e-8,      // 蒸发 (米/秒, 约 0.2 mm/小时)
  merge: 0.03,       // 落点离一摊的边这么近就并进去 (米)
  crest: 0.008,      // 浪翻过杯沿时最多比杯沿高多少 (米), 再高的那截翻出去了 (见 _cup)
};

/** 液体种类: stain 干了以后留下印子 (咖啡、牛奶; 水不留). */
export const KINDS = {
  coffee: { name: '咖啡', color: '#4a2a17', stain: true },
  water: { name: '水', color: '#cfe4f2', stain: false },
  milk: { name: '牛奶', color: '#f3eee4', stain: true },
};

const ML = 1e6;  // 立方米 -> 毫升
const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const scale = (a, k) => [a[0] * k, a[1] * k, a[2] * k];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const len = (a) => Math.hypot(a[0], a[1], a[2]);

/** 四元数转向量. */
export function rotate(q, v) {
  const [x, y, z, w] = q, [vx, vy, vz] = v;
  const tx = 2 * (y * vz - z * vy), ty = 2 * (z * vx - x * vz), tz = 2 * (x * vy - y * vx);
  return [vx + w * tx + y * tz - z * ty, vy + w * ty + z * tx - x * tz, vz + w * tz + x * ty - y * tx];
}
const inverse = (q) => [-q[0], -q[1], -q[2], q[3]];

/** 液面平面 z = m + t·x (杯子局部, x 朝溢出那边, 0 是杯底) 截在 [0, H] 之间的体积 (立方米). */
function clippedVolume(R, H, m, t) {
  if (t * R <= Math.min(m, H - m)) return Math.PI * R * R * m;  // 液面不碰杯底也不过杯沿
  const N = 48;
  let v = 0;
  for (let i = 0; i < N; i++) {
    const x = -R + ((i + 0.5) * 2 * R) / N;
    v += 2 * Math.sqrt(R * R - x * x) * clamp(m + t * x, 0, H) * ((2 * R) / N);
  }
  return v;
}

/** 液量 V (立方米)、坡度 t 时液面在杯中心的高度 m (杯口以上的部分截掉以后体积对得上). */
export function surfaceAt(R, H, V, t) {
  const h = V / (Math.PI * R * R);
  if (h - t * R >= 0 && h + t * R <= H) return h;
  let lo = -t * R, hi = H + t * R + h;
  for (let i = 0; i < 40; i++) {
    const mid = (lo + hi) / 2;
    if (clippedVolume(R, H, mid, t) < V) lo = mid; else hi = mid;
  }
  return (lo + hi) / 2;
}

/** 坡度 t 时杯子最多装多少 (毫升): 液面正好贴着杯沿最低处. */
export function capacityMl(R, H, t) {
  return clippedVolume(R, H, H - t * R, t) * ML;
}

/** 液面超出杯沿的部分每秒流出多少 (立方米/秒): 沿杯沿积分堰流. */
function overflow(R, H, m, t, g) {
  const k = LIQUID.cd * (2 / 3) * Math.sqrt(2 * g) * R;
  if (t * R < 1e-9) return m > H ? k * 2 * Math.PI * (m - H) ** 1.5 : 0;
  const c = (H - m) / (t * R);
  if (c >= 1) return 0;
  const th = Math.acos(clamp(c, -1, 1)), N = 16;
  let s = 0;
  for (let i = 0; i < N; i++) {
    const a = -th + ((i + 0.5) * 2 * th) / N;
    s += Math.max(0, m + t * R * Math.cos(a) - H) ** 1.5;
  }
  return k * s * ((2 * th) / N);
}

/**
 * 碰到液体时的后果 (要校准的模拟模型, 不是医学): 40 ℃ 以下只是湿; 40-48 烫一下 (缩手, 不伤); 48 以上越烫、量越大越重,
 * 几滴 (程度不到 0.1) 也只是烫一下, 溅上一毫升以上才算烫伤. 返回 {level: 'wet' | 'hot' | 'burn', severity 0..1, sore: 疼多久 (秒)}.
 * (以前 0.1 ml 80 ℃ 就算烫伤 0.39: 评测台里半杯端稳洒几滴的得分反而比不端稳的低)
 * part 'mouth' (喝): 小口喝 60-70 ℃ 是平常的温度, 75 ℃ 以上才开始烫嘴, 门槛整体高 22 ℃ (70 ℃ 一口只是烫一下, 80 ℃ 轻微烫伤).
 * (高 15 ℃ 时刚冲好的 70 ℃ 浓缩咖啡每一口都算烫伤, 她每次喝咖啡都喊疼)
 */
export function heatEffect(temp, ml, part = null) {
  if (part === 'mouth') temp -= 22;
  if (temp < 40) return { level: 'wet', severity: 0, sore: 0 };
  const s = temp < 48 ? 0 : clamp((temp - 48) / 30) * clamp(0.6 * Math.log10(1 + ml / 0.5));
  if (s < 0.1) return { level: 'hot', severity: +Math.max(s, 0.1 * clamp((temp - 40) / 8)).toFixed(3), sore: 0 };
  return { level: 'burn', severity: +s.toFixed(3), sore: Math.round(90 + 900 * s) };
}

/** 一只杯子: 形状 (内半径 R、内高 H, 米)、里面装着什么 (毫升、摄氏度、种类)、液面晃动的状态. */
export class Cup {
  constructor({ R = 0.04, H = 0.09, ml = 0, temp = LIQUID.env, kind = 'coffee' } = {}) {
    Object.assign(this, { R, H, ml, temp, kind });
    this.eta = [0, 0];   // 第一阶振型的贴壁升高 (杯子局部 x, z; 米)
    this.etaV = [0, 0];
    this.etaF = [0, 0];  // 高阶振型 (合成一个, 频率是第一阶的 3 倍)
    this.etaFV = [0, 0];
    this.A = [0, 0];     // 此刻贴壁升高 (含高阶)
    this.mouthRoom = LIQUID.mouthMl;  // 喝的时候嘴里还能含多少 (咽下去慢慢腾出来)
    this.pos = null;     // 杯子实际在哪 (按弹簧跟着掌心, 见 LIQUID.gripHz)
    this.vel = [0, 0, 0];
    this.acc = [0, 0, 0];
    this.lift = 0;       // 失重时液体整块离开杯底往杯口走了多远 (米), 见 _cupAt
    this.liftV = 0;
    this.liftOut = 0;    // 这一回冒出杯沿、甩出去的 (毫升)
  }

  get capacity() { return Math.PI * this.R * this.R * this.H * ML; }

  /** 倒进去 (温度按液量混). 返回实际倒进去多少 (满了倒不进). */
  fill(ml, temp = this.temp, kind = this.kind) {
    const put = Math.max(0, Math.min(ml, this.capacity - this.ml));
    if (put <= 0) return 0;
    this.temp = (this.temp * this.ml + temp * put) / (this.ml + put);
    if (this.ml < 1e-6) this.kind = kind;
    this.ml += put;
    return put;
  }

  /** 杯子被瞬移 (拿起、放下、换地方): 下一帧从那里静止开始跟. */
  teleported() { this.pos = null; this.palm = null; this.physV = null; this.vel = [0, 0, 0]; this.acc = [0, 0, 0]; }

  /** 静止放着: 晃动归零 (放下以后过一会儿, 或者存档读回来). */
  settle() { this.eta = [0, 0]; this.etaV = [0, 0]; this.etaF = [0, 0]; this.etaFV = [0, 0]; this.A = [0, 0]; this.teleported(); }

  toJSON() { return { R: this.R, H: this.H, ml: +this.ml.toFixed(3), temp: +this.temp.toFixed(2), kind: this.kind }; }
}

/**
 * 液体世界: 所有杯子、正在往下落的、洒出来的一摊摊、沾在身上的. step 每帧调一次, 返回这一帧发生的事 (事件).
 * 宿主给的回调 (都可以不给):
 *   hit(p0, p1) -> {part} | null      从 p0 落到 p1 的一段碰到身体哪个部位 (手、大腿、脚...)
 *   ground(x, z, y) -> {y, surface}   (x, z) 处、y 以下最高的台面或地面 (没给就是 y = 0 的地面)
 *   mouth -> [x, y, z] | null         嘴的位置 (喝的时候)
 * 记账 (ledger): 倒进去的 = 杯里 + 在落的 + 每一摊 + 身上 + 喝掉的 + 干掉的, 任何时候都成立 (测试守着).
 */
export class LiquidWorld {
  constructor({ env = LIQUID.env } = {}) {
    this.env = env;
    this.cups = {};
    this.drops = [];
    this.puddles = [];
    this.body = {};       // 部位 -> {ml, temp}
    this.ledger = { poured: 0, spilled: 0, drunk: 0, dried: 0 };
    this.n = 0;
    this.time = 0;
  }

  /** 拿到 (没有就新建) 一只杯子. */
  cup(id, opts = {}) {
    return (this.cups[id] ??= new Cup(opts));
  }

  /** 往杯里倒 (记账). */
  fill(id, ml, temp, kind) {
    const put = this.cup(id).fill(ml, temp, kind);
    this.ledger.poured += put;
    return put;
  }

  /** 从 at 那一点倒出 ml (咖啡机出水底下没有杯子、杯子满了接不下): 往下落, 落到哪成一摊. */
  pour(at, ml, temp, kind, v = [0, -0.3, 0]) {
    if (ml <= 0) return;
    this.ledger.poured += ml;
    this.ledger.spilled += ml;
    this._drop(at, v, ml, temp, kind, null);
  }

  /** 喝 (不经过倾斜, 宿主直接扣, 比如吸管): 返回喝到多少. */
  sip(id, ml) {
    const c = this.cups[id];
    if (!c) return 0;
    const got = Math.min(c.ml, Math.max(0, ml));
    c.ml -= got;
    this.ledger.drunk += got;
    return got;
  }

  /** 倒掉 (水槽): 返回倒掉多少; 记作喝掉以外的去处 (dried 里不算, 单独记 dumped). */
  dump(id) {
    const c = this.cups[id];
    if (!c) return 0;
    const out = c.ml;
    c.ml = 0;
    this.ledger.dumped = (this.ledger.dumped || 0) + out;
    return out;
  }

  /** 擦掉一摊 (擦桌子、拖地): 返回擦掉多少. */
  wipe(id) {
    const i = this.puddles.findIndex((p) => p.id === id);
    if (i < 0) return 0;
    const [p] = this.puddles.splice(i, 1);
    this.ledger.wiped = (this.ledger.wiped || 0) + p.ml;
    return p.ml;
  }

  /** 现在一共有多少液体在哪 (记账核对用). */
  balance() {
    const cups = Object.values(this.cups).reduce((s, c) => s + c.ml, 0);
    const drops = this.drops.reduce((s, d) => s + d.ml, 0);
    const puddles = this.puddles.reduce((s, p) => s + p.ml, 0);
    const body = Object.values(this.body).reduce((s, b) => s + b.ml, 0);
    const L = this.ledger;
    return { cups, drops, puddles, body, drunk: L.drunk, dried: L.dried, dumped: L.dumped || 0, wiped: L.wiped || 0, poured: L.poured,
      error: L.poured - (cups + drops + puddles + body + L.drunk + L.dried + (L.dumped || 0) + (L.wiped || 0)) };
  }

  /**
   * 推进一帧. poses: {杯子 id: {p, q, hold, drinking}} 这一帧动着的杯子 (hold: 握着它的手 'left' / 'right', 放着的不用给;
   * drinking: 宿主说她正在喝, 只是提示, 进不进嘴看几何, 见 _intoMouth);
   * 没给位姿的杯子当作静止放着. 返回事件: {type: 'spill', cup, ml, at} 杯里洒出去;
   * {type: 'contact', part, ml, temp, effect} 碰到身体; {type: 'land', puddle, surface, ml, at} 落到台面、地上;
   * {type: 'drink', cup, ml, temp} 流进嘴里.
   */
  step(dt, { poses = {}, hit = null, ground = null, mouth = null } = {}) {
    const ev = [];
    this.time += dt;
    for (const [id, c] of Object.entries(this.cups)) {
      const pose = poses[id];
      c.mouthRoom = Math.min(LIQUID.mouthMl, c.mouthRoom + LIQUID.gulpMlS * dt);  // 咽下去, 嘴里腾出地方
      if (pose) this._cup(id, c, pose, dt, { hit, mouth }, ev);
      else c.settle();
      c.temp = this.env + (c.temp - this.env) * Math.exp(-this._k(c) * dt);
    }
    this._drops(dt, hit, ground, ev);
    this._age(dt, ev);
    return ev;
  }

  /** 离线过了 secs 秒 (桌宠关着、最小化): 冷却、蒸发按解析解一次算完; 在落的直接落下. 返回这段时间里干了、并了的事件. */
  advance(secs, { ground = null } = {}) {
    const ev = [];
    if (secs <= 0) return ev;
    for (const c of Object.values(this.cups)) {
      c.temp = this.env + (c.temp - this.env) * Math.exp(-this._k(c) * secs);
      c.settle();
    }
    if (this.drops.length) this._drops(10, null, ground, []);
    this._age(secs, ev);
    return ev;
  }

  toJSON() {
    const r = (x, k = 3) => +x.toFixed(k);
    return { v: 1, env: this.env, time: r(this.time, 1), n: this.n,
      cups: Object.fromEntries(Object.entries(this.cups).map(([id, c]) => [id, c.toJSON()])),
      puddles: this.puddles.map((p) => ({ ...p, at: p.at.map((x) => r(x)), ml: r(p.ml, 4), temp: r(p.temp, 2), r: r(p.r, 4), dried: r(p.dried || 0, 4) })),
      body: Object.fromEntries(Object.entries(this.body).map(([k, b]) => [k, { ml: r(b.ml, 4), temp: r(b.temp, 2) }])),
      ledger: Object.fromEntries(Object.entries(this.ledger).map(([k, x]) => [k, r(x, 4)])),
      drops: this.drops.map((d) => ({ ...d, p: d.p.map((x) => r(x)), v: d.v.map((x) => r(x)), ml: r(d.ml, 4) })) };
  }

  static from(json) {
    const w = new LiquidWorld({ env: json?.env ?? LIQUID.env });
    if (!json || json.v !== 1) return w;
    w.time = json.time || 0;
    w.n = json.n || 0;
    for (const [id, c] of Object.entries(json.cups || {})) w.cups[id] = new Cup(c);
    w.puddles = (json.puddles || []).map((p) => ({ ...p, at: [...p.at] }));
    w.body = Object.fromEntries(Object.entries(json.body || {}).map(([k, b]) => [k, { ...b }]));
    w.drops = (json.drops || []).map((d) => ({ ...d, p: [...d.p], v: [...d.v] }));
    Object.assign(w.ledger, json.ledger || {});
    return w;
  }

  // -- 内部 ------------------------------------------------------------------------------
  /** 杯里液体的冷却速率: 按 (表面积 / 体积) 和 60 ml 浓缩咖啡杯比. */
  _k(c) {
    const V = Math.max(c.ml, 1) / ML, h = V / (Math.PI * c.R * c.R);
    const A = 2 * Math.PI * c.R * c.R + 2 * Math.PI * c.R * h;
    const R0 = 0.03, V0 = 60 / ML, h0 = V0 / (Math.PI * R0 * R0), A0 = 2 * Math.PI * R0 * R0 + 2 * Math.PI * R0 * h0;
    return LIQUID.kCup * (A / V) / (A0 / V0);
  }

  _cup(id, c, { p, q = [0, 0, 0, 1], hold = null, drinking = false, physical = false, v: pv0 = null }, dt, { hit, mouth }, ev) {
    if (physical) {  // 刚体 (P1) 的杯子: 位置、速度直接用刚体的, 加速度按速度差 (稍微低通: 接触时一步一步的抖动), 不过抓持的弹簧
      const v = pv0 || [0, 0, 0];
      c.acc = c.physV ? add(c.acc, scale(sub(scale(sub(v, c.physV), 1 / dt), c.acc), 1 - Math.exp(-dt / 0.015))) : [0, 0, 0];
      c.physV = [...v];
      c.pos = [...p];
      c.vel = [...v];
      c.palm = null;
      return this._cupAt(id, c, p, q, v, dt, { hold, drinking, hit, mouth }, ev);
    }
    c.physV = null;
    // 杯子按临界阻尼的弹簧跟着掌心 (加速度最多 aMax); 刚开始、瞬移以后从掌心处静止开始
    // (阻尼按掌心和杯子的相对速度: 按杯子自己的速度算的话, 1.3 m/s 匀速走时杯子落后掌心 10 cm)
    if (!c.pos || len(sub(p, c.pos)) > LIQUID.teleport) { c.pos = [...p]; c.vel = [0, 0, 0]; c.palm = null; }
    // 这一帧里掌心按匀速从上一帧走到这里: 偏差 e = 杯 - 掌心 满足 ë = -ω²e - 2ωė, 临界阻尼的解析解 (显式积分在 gripHz 高时发散)
    const pv = c.palm ? scale(sub(p, c.palm), 1 / dt) : c.vel;
    const p0 = c.palm || p;
    c.palm = [...p];
    const w0 = 2 * Math.PI * LIQUID.gripHz, ex = Math.exp(-w0 * dt);
    const e0 = sub(c.pos, p0), d0 = sub(c.vel, pv);
    const B = add(d0, scale(e0, w0));
    const e1 = scale(add(e0, scale(B, dt)), ex), d1 = scale(sub(d0, scale(B, w0 * dt)), ex);
    let vNew = add(pv, d1);
    let dv = sub(vNew, c.vel);
    const lim = LIQUID.aMax * dt;
    if (len(dv) > lim) { dv = scale(dv, lim / len(dv)); vNew = add(c.vel, dv); c.pos = add(c.pos, scale(add(c.vel, vNew), dt / 2)); }
    else c.pos = add(p, e1);
    c.acc = scale(dv, 1 / dt);
    c.vel = vNew;
    return this._cupAt(id, c, c.pos, q, c.vel, dt, { hold, drinking, hit, mouth }, ev);
  }

  /** 杯子此刻在 p、朝 q、速度 v、加速度 c.acc: 晃动、溢出. */
  _cupAt(id, c, p, q, v, dt, { hold, drinking, hit, mouth }, ev) {
    if (c.ml <= 1e-6) { c.eta = [0, 0]; c.etaV = [0, 0]; c.etaF = [0, 0]; c.etaFV = [0, 0]; c.A = [0, 0]; c.lift = c.liftV = c.liftOut = 0; return; }
    const R = c.R, H = c.H;
    const f = rotate(inverse(q), add(c.acc, [0, G, 0]));  // 比力, 杯子局部 (+Y 杯口)
    const fax = f[1], fl = [f[0], f[2]], gAll = len(f);
    // 失重: 杯子正着, 但往下的加速度比重力还大 (往上端着急停、撞一下、掉下去落地前) 时液体不再压着杯底, 按惯性整块往杯口走,
    // 冒出杯沿的那截甩出去; 回落到杯底以前晃动停着不算. 以前这时当成杯子倒过来了, 一帧倒光一杯 (刚体端杯子时急停一下就倒光)
    if (rotate(q, [0, 1, 0])[1] > 0.3 && (fax <= 0.3 || c.lift > 0)) {
      if (!c.lift) c.liftRoom = H - c.ml / ML / (Math.PI * R * R);  // 这一回离开杯底时液面离杯沿多远
      c.liftV -= fax * dt;
      c.lift += c.liftV * dt;
      if (c.lift <= 0) { c.lift = c.liftV = c.liftOut = 0; return; }
      const above = Math.PI * R * R * Math.max(0, c.lift - c.liftRoom) * ML;  // 冒出杯沿的 (毫升)
      const out = Math.min(c.ml, Math.max(0, above - c.liftOut));
      if (out <= 1e-9) return;
      c.liftOut += out;
      c.ml -= out;
      this.ledger.spilled += out;
      const lip = add(p, rotate(q, [0, H, 0]));
      ev.push({ type: 'spill', cup: id, ml: out, at: lip });
      this._drop(lip, add(v, rotate(q, [0, Math.max(0, c.liftV), 0])), out, c.temp, c.kind, null);
      return;
    }
    // 静态贴壁升高 (指向液面高的那边): -R·f_水平 / f_轴向; 杯子快倒过来时当作坡度很大
    const BIG = 40;
    let Aeq;
    if (fax > 0.3) Aeq = [(-R * fl[0]) / fax, (-R * fl[1]) / fax];
    else {
      const n = Math.hypot(fl[0], fl[1]) || 1;
      Aeq = [(-R * BIG * fl[0]) / n, (-R * BIG * fl[1]) / n];
    }
    const mA = Math.hypot(Aeq[0], Aeq[1]);
    if (mA > R * BIG) Aeq = scale([Aeq[0], Aeq[1], 0], (R * BIG) / mA).slice(0, 2);
    const V = c.ml / ML, h = V / (Math.PI * R * R);
    const g = Math.max(fax, 0.5);
    const w = Math.sqrt(((g * 1.841) / R) * Math.tanh((1.841 * Math.max(h, 0.002)) / R));
    const slow = 1 - LIQUID.fast;
    const n = Math.max(1, Math.ceil((w * dt) / 0.2));
    for (let s = 0; s < n; s++) {
      const h2 = dt / n;
      for (let i = 0; i < 2; i++) {
        const acc = -2 * LIQUID.zeta * w * c.etaV[i] - w * w * (c.eta[i] - slow * Aeq[i]);
        c.etaV[i] += acc * h2;
        c.eta[i] += c.etaV[i] * h2;
      }
    }
    // 高阶振型也要时间才跟得上 (频率是第一阶的 3 倍): 以前当作立刻跟上, 往下拽得差不多和重力一样快时 (轴向比力只剩 0.3 m/s²)
    // 静态倾斜 = 横向 / 轴向 趋于无穷, 一帧里贴壁升高跳到 16 cm, 一下倒掉 223 ml (刚体放下杯子时)
    const wF = 3 * w, nF = Math.max(1, Math.ceil((wF * dt) / 0.2));
    c.etaF ??= [0, 0];
    c.etaFV ??= [0, 0];
    for (let s = 0; s < nF; s++) {
      const h2 = dt / nF;
      for (let i = 0; i < 2; i++) {
        const acc = -2 * wF * c.etaFV[i] - wF * wF * (c.etaF[i] - LIQUID.fast * Aeq[i]);  // 临界阻尼: 好几阶合在一起, 相位错开, 不会一起来回晃
        c.etaFV[i] += acc * h2;
        c.etaF[i] += c.etaFV[i] * h2;
      }
    }
    c.A = [c.etaF[0] + c.eta[0], c.etaF[1] + c.eta[1]];
    let amp = Math.hypot(c.A[0], c.A[1]), t = amp / R;
    let m = surfaceAt(R, H, V, t);
    // 线性振子在浪翻过杯沿以后不成立: 翻出去的那截没了, 贴壁升高最多比杯沿高 crest (多出来的从振型里扣掉)
    const over = amp - (H - m + LIQUID.crest);
    const em = Math.hypot(c.eta[0], c.eta[1]);
    if (over > 0 && em > 1e-9) {
      const k2 = Math.max(0, em - over) / em;
      c.eta = [c.eta[0] * k2, c.eta[1] * k2];
      c.etaV = [c.etaV[0] * k2, c.etaV[1] * k2];
      c.A = [c.etaF[0] + c.eta[0], c.etaF[1] + c.eta[1]];
      amp = Math.hypot(c.A[0], c.A[1]);
      t = amp / R;
      m = surfaceAt(R, H, V, t);
    }
    // 快倒过来 (比力几乎不往杯底压, 杯子又不是正着的; 正着的失重在上面另算): 不管晃没晃, 0.15 秒内倒光
    const tipped = fax <= 0.3;
    if (!tipped && m + t * R <= H) return;
    // 溢出: 从杯沿最高处 (液面高的那边) 流出去
    let Q = tipped ? 0 : overflow(R, H, m, t, Math.max(gAll, 1)) * ML;
    if (tipped) Q = Math.max(c.ml, 5) / 0.15;  // 倒到最后几毫升也照样流光 (只按剩多少算的话越倒越慢, 永远倒不干净)
    let out = Math.min(c.ml, Q * dt);
    if (out <= 1e-9) return;
    c.ml -= out;
    const fh = Math.hypot(fl[0], fl[1]);
    const dir = tipped && fh > 1e-9 ? [-fl[0] / fh, 0, -fl[1] / fh] : amp > 1e-9 ? [c.A[0] / amp, 0, c.A[1] / amp] : [1, 0, 0];
    const lip = add(p, rotate(q, [dir[0] * R, H, dir[2] * R]));
    const head = Math.max(0, m + t * R - H);
    const vOut = add(v, rotate(q, scale(dir, Math.sqrt(2 * G * head) + 0.05)));
    // 流进嘴里 (喝的时候杯沿凑在嘴边): 嘴里一下子能含 mouthMl, 每秒咽下去 gulpMlS 腾出地方; 涌得更多的照样洒出来
    // (只按每秒咽多少算时, 杯子在嘴边一晃一股 3-4 ml 涌出来, 一帧只能进 0.8 ml, 剩下的都算洒到胸口)
    if (this._intoMouth(p, q, lip, dir, H, { hold, drinking, mouth })) {
      const take = Math.min(out, c.mouthRoom);
      c.mouthRoom -= take;
      this.ledger.drunk += take;
      ev.push({ type: 'drink', cup: id, ml: take, temp: c.temp });
      out -= take;
      if (out <= 1e-9) return;
    }
    this.ledger.spilled += out;
    ev.push({ type: 'spill', cup: id, ml: out, at: lip });
    // 慢慢溢出的大半顺着杯身流到握着它的手上; 流得急就甩出去
    const share = hold ? LIQUID.handShare * clamp(1 - Q / LIQUID.jetMlS) : 0;
    const toHand = out * share;
    if (toHand > 1e-9) {
      const part = `${hold}Hand`;
      const stay = this._wet(part, toHand, c.temp);
      ev.push({ type: 'contact', part, ml: toHand, temp: c.temp, effect: heatEffect(c.temp, toHand) });
      const drip = toHand - stay;
      if (drip > 1e-9) this._drop(add(p, rotate(q, [0, -0.01, 0])), v, drip, c.temp, c.kind, part);
    }
    if (out - toHand > 1e-9) this._drop(lip, vOut, out - toHand, c.temp, c.kind, null);
  }

  /**
   * 从这处杯沿 (lip, 流出方向 dir 在杯子局部) 流出来的进不进嘴, 只看几何: 有人握着、嘴离这处杯沿 drinkReach 以内 (宿主说正在喝时
   * 放宽到 drinkHint)、嘴在流出来的那一侧 (从杯口中心往流出方向看, 嘴在前面). 杯子离嘴远、手被挡住送不到嘴边、杯子脱手、
   * 动作中断了旧的 drinking 没清, 都进不了嘴 (tests/js/liquid.test.mjs 守着).
   */
  _intoMouth(p, q, lip, dir, H, { hold, drinking, mouth }) {
    if (!mouth || !hold) return false;
    if (len(sub(lip, mouth)) > (drinking ? LIQUID.drinkHint : LIQUID.drinkReach)) return false;
    return dot(rotate(q, dir), sub(mouth, add(p, rotate(q, [0, H, 0])))) > 0;
  }

  /** 沾到身体某处: 返回沾住多少 (最多 film, 多的滴下去). 温度按量混. */
  _wet(part, ml, temp) {
    const b = (this.body[part] ??= { ml: 0, temp: this.env });
    const stay = Math.max(0, Math.min(ml, LIQUID.film - b.ml));
    if (stay > 0) { b.temp = (b.temp * b.ml + temp * stay) / (b.ml + stay); b.ml += stay; }
    return stay;
  }

  _drop(p, v, ml, temp, kind, from) {
    this.drops.push({ p: [...p], v: [...v], ml, temp, kind, from });
  }

  /** 往下落的: 受重力, 碰到身体沾一点、烫一下, 剩下的接着往下滴; 落到台面、地上并进一摊. */
  _drops(dt, hit, ground, ev) {
    const keep = [];
    for (const d of this.drops) {
      const steps = Math.max(1, Math.ceil(dt / (1 / 60)));
      let landed = false;
      for (let s = 0; s < steps && !landed; s++) {
        const h = dt / steps, p1 = add(d.p, scale(d.v, h));
        p1[1] -= 0.5 * G * h * h;
        d.v[1] -= G * h;
        const touch = hit?.(d.p, p1);
        if (touch && touch.part !== d.from) {
          const stay = this._wet(touch.part, d.ml, d.temp);
          ev.push({ type: 'contact', part: touch.part, ml: d.ml, temp: d.temp, effect: heatEffect(d.temp, d.ml) });
          d.ml -= stay;
          d.from = touch.part;
          d.v = [d.v[0] * 0.2, Math.min(d.v[1], 0) * 0.3, d.v[2] * 0.2];  // 顺着身体往下流
          if (d.ml <= 1e-9) { landed = true; break; }
        }
        const gr = ground?.(p1[0], p1[2], d.p[1]) || { y: 0, surface: 'floor' };
        if (p1[1] <= gr.y) {
          this._land([p1[0], gr.y, p1[2]], gr.surface, d, ev);
          landed = true;
        }
        d.p = p1;
      }
      if (!landed && d.p[1] > -5) keep.push(d);
      else if (!landed) this._land([d.p[0], 0, d.p[2]], 'floor', d, ev);
    }
    this.drops = keep;
  }

  _land(at, surface, d, ev) {
    const near = this.puddles.find((p) => p.surface === surface && Math.hypot(p.at[0] - at[0], p.at[2] - at[2]) < p.r + LIQUID.merge
      && Math.abs(p.at[1] - at[1]) < 0.05);
    let pd = near;
    if (pd) {
      const tot = pd.ml + d.ml;
      pd.at = [(pd.at[0] * pd.ml + at[0] * d.ml) / tot, pd.at[1], (pd.at[2] * pd.ml + at[2] * d.ml) / tot];
      pd.temp = (pd.temp * pd.ml + d.temp * d.ml) / tot;
      if (d.kind !== pd.kind && d.ml > pd.ml) pd.kind = d.kind;
      pd.ml = tot;
      pd.stain = false;
    } else {
      pd = { id: `p${++this.n}`, surface, at: [...at], ml: d.ml, temp: d.temp, kind: d.kind, r: 0, stain: false, born: +this.time.toFixed(1) };
      this.puddles.push(pd);
    }
    pd.r = Math.sqrt(pd.ml / ML / (Math.PI * LIQUID.filmDepth));
    ev.push({ type: 'land', puddle: pd.id, surface, ml: d.ml, at: [...at] });
  }

  /** 刚干成印子的一块: 和同一处台面上挨着 (边缘相距 LIQUID.stainMerge 以内) 的同种印子并成一块 (面积相加、干掉的量相加,
   *  中心按面积取). 并了的话事件里记一笔 {type: 'merged', into, from}. */
  _mergeStain(p, ev) {
    for (const o of this.puddles) {
      if (o === p || !o.stain || o.merged || o.surface !== p.surface || o.kind !== p.kind || Math.abs(o.at[1] - p.at[1]) > 0.05) continue;
      if (Math.hypot(o.at[0] - p.at[0], o.at[2] - p.at[2]) > o.r + p.r + LIQUID.stainMerge) continue;
      const a = o.r * o.r, c = p.r * p.r;
      o.at = [(o.at[0] * a + p.at[0] * c) / (a + c), o.at[1], (o.at[2] * a + p.at[2] * c) / (a + c)];
      o.r = Math.sqrt(a + c);
      o.dried = (o.dried || 0) + (p.dried || 0);
      p.merged = true;
      ev?.push({ type: 'merged', into: o.id, from: p.id });
      return;
    }
  }

  /**
   * 一摊摊的冷却、蒸发 (薄的一层冷得快); 身上沾的凉到体温附近、慢慢干. 干了的咖啡、牛奶留下印子 (p.dried 记这一处一共干掉多少,
   * 合并时相加), 水干了就没了. 事件: {type: 'dried', puddle, stain} 一摊干了 (留没留印子)、{type: 'merged', into, from}.
   */
  _age(dt, ev = null) {
    const L = this.ledger;
    for (const p of this.puddles) {
      if (p.ml <= 0) continue;
      const k = LIQUID.kCup * (1 / LIQUID.filmDepth) / 160;
      p.temp = this.env + (p.temp - this.env) * Math.exp(-k * dt);
      const area = Math.PI * p.r * p.r;
      const gone = Math.min(p.ml, LIQUID.evap * area * dt * ML);
      p.ml -= gone;
      p.dried = (p.dried || 0) + gone;
      L.dried += gone;
      if (p.ml <= 1e-6) {
        L.dried += p.ml;
        p.dried += p.ml;
        p.ml = 0;
        p.stain = !!KINDS[p.kind]?.stain;
        ev?.push({ type: 'dried', puddle: p.id, stain: p.stain });
        if (p.stain) this._mergeStain(p, ev);
      }
    }
    this.puddles = this.puddles.filter((p) => (p.ml > 0 || p.stain) && !p.merged);
    for (const [part, b] of Object.entries(this.body)) {
      b.temp = LIQUID.skin + (b.temp - LIQUID.skin) * Math.exp(-LIQUID.kSkin * dt);
      const gone = Math.min(b.ml, b.ml * (1 - Math.exp(-dt / 600)));  // 身上的一层十分钟左右干
      b.ml -= gone;
      L.dried += gone;
      if (b.ml < 1e-4) { L.dried += b.ml; delete this.body[part]; }
    }
  }
}
