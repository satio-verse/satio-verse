// 她对小窝的了解 (可供性, 见 docs/roadmap/room-afford.md): 看见过哪些东西、看到的粗档 (花土干不干、灯开着没有、哪里洒了一摊)、
// 每件事在什么情境下试过几次、觉得怎么样. 挑下一件事时只读这里, 不读房间的真值 (书好不好看、土里还有多少水).
// 不 import three, 不碰房间: 她在哪、东西在哪、看到的粗档由调用方 (room-afford.js) 给, node --test 能测 (tests/js/room-known.test.mjs).
// 存在本机存储 (pet.room.known), 和练习 (room-practice.js) 一样只是这只桌宠自己的. 上行服务端的只有编号和数字 (forPlan).
export const KEY = 'pet.room.known';
export const NEEDS = ['energy', 'food', 'water', 'fun', 'social'];
const HOUR = 3600;
const N0 = 0.5;          // 常识的先验算几次 (试过一次以后就主要按经验)
const WINDOW = 5;        // 经验按最近几次平均 (东西会变: 书看完了、花浇多了)
const FADE_H = 96;       // 经验多久没再试就淡一半多 (e 倍): 规则变了 (换了一盆不喜水的花) 隔几天会再试一次, 不永远照旧
const SHARE = 2;         // 同一个动词别的事的经验 (换了一盆花、另一本书) 最多算几次: 能迁移, 但自己试过以后按自己的
const STALE_H = 12;      // 看到的粗档多久以后算"不知道现在怎样了"
const VISIT_H = 6;       // 一处多久没去算"很久没去"
const SIDE = (70 * Math.PI) / 180;

/** 好奇心: 一种情境下试过 n 次以后还想不想试 (没试过的多一点分, 试过几次后按经验). 认知会话阶段 5 做"能学会、有进步"的版本时换掉这个函数. */
export function curious(n) { return 1 + 0.9 / (1 + 1.5 * n); }

/** 走到跟前看清楚了 (土是湿的…), 按经验 (自己的或同一动词迁移来的) 这一下做了不好: 不做了. */
export function skipHere(known, a, ctx) {
  const e = known.expect(a.id, ctx, a.prior, a.verb);
  return e.weight >= 0.5 && worth(e.gives) < 0;
}

/** 一套东西里挑哪一件 (哪本书): 有喜欢的七成挑喜欢的, 不然挑没看过的, 都看过就随便挑. list 只给她知道在哪、以为拿得到的. */
export function pickOne(known, list, sit, rand = Math.random) {
  const by = (k) => list.filter((n) => known.likeOf(n) === k), any = (l) => l[Math.floor(rand() * l.length)];
  if (sit === 'liked' && by('liked').length && rand() < 0.7) return any(by('liked'));
  if (by('new').length && sit !== 'again' && sit !== 'fresh') return any(by('new'));
  return any(list);
}

/** 站在 (x, z)、脸朝 yaw 时看不看得见 p ([x, y, z]): 离得够近 (see 米), 在前方 ±70° 里; 贴着身边 0.9 m 以内不看方向. */
export function sees({ x, z, yaw }, p, see = 2) {
  const dx = p[0] - x, dz = p[2] - z, d = Math.hypot(dx, dz);
  if (d > see) return false;
  if (d < 0.9) return true;
  return (dx * Math.sin(yaw) + dz * Math.cos(yaw)) / d > Math.cos(SIDE);
}

/** 补的需求合起来值多少 (精力、饱腹、水分、开心、陪伴同等算). */
export const worth = (g) => NEEDS.reduce((s, k) => s + (g?.[k] || 0), 0);
const round = (g) => Object.fromEntries(Object.entries(g).filter(([, v]) => Math.abs(v) >= 0.05).map(([k, v]) => [k, +v.toFixed(1)]));

function readStore(storage) {
  try { return JSON.parse(storage?.getItem(KEY) || 'null'); } catch { return null; }
}

export class Known {
  /**
   * now(): 秒 (小屋的钟, 和需求同一个); storage: 本机存储 (测试给 null 就不存).
   * 评测对照用 (tools/bench_afford.mjs; 平时都是默认): window 经验按最近几次平均 (Infinity 是一直平均), fade 经验淡掉的小时数
   * (0 不淡), share 同一动词的经验能不能迁移, frozen 经验冻住 (只认识东西, 试过也不记).
   */
  constructor({ now = () => Date.now() / 1000, storage = globalThis.localStorage ?? null, window = WINDOW, fade = FADE_H, share = true,
    frozen = false } = {}) {
    this.now = now;
    this.storage = storage;
    Object.assign(this, { window, fade, share, frozen });
    const s = readStore(storage) || {};
    this.things = s.things || {};    // 东西 -> { first, last, n } 什么时候第一次 / 最近一次看见
    this.states = s.states || {};    // 东西 -> { level, at } 看到的粗档 (土 dry/ok/wet, 灯 on/off)
    this.spills = s.spills || [];    // 看见过的一摊: { id, kind (coffee/water), at: [x, z], seen }
    this.tries = s.tries || {};      // 事 -> 情境 -> { n, ok, v: {需求: 平均补了多少}, last }
    this.likes = s.likes || {};      // 单件东西 (哪本书) -> { n, v 平均开心多少 }
    this.visits = s.visits || {};    // 去过的地方 -> 最近一次
    this.high = s.high ?? null;      // 够不着的最低高度 (米, 试过才知道; 书架上层的书)
    this.stuck = s.stuck || {};      // 走不到跟前拿不到的东西 -> 什么时候 (家具会挪, 过一天再试)
    this.where = s.where || {};      // 单件东西 (哪本书、手机) -> { at: [x, y, z] 最后看见在哪, seen }; 认识"书"不等于知道这本在哪
    this.verbOf = s.verbOf || {};    // 事 -> 动词 (同一个动词的经验能迁移: 换了一盆花、另一本书)
    this.skips = s.skips || {};      // 事 -> 情境 -> 走到跟前看清了、按经验没做的次数
    this.news = [];                  // 这一阵新看见的 (状态栏说一句; 四处看看算发现了几样)
  }

  /**
   * 服务端的发现簿 (wm/cognition/afford.py, 下行 room_known) 换进本机这一份: 那边是准的 (卸载不删, 双胞胎和重放在那边守着),
   * 本机这份只是断线时接着用的. 时刻是"多久以前"的秒数, 按小屋的钟换回来 (快进时两边的钟不一样).
   * 单件东西在哪 (where) 和动词 (verbs) 服务端也记 (2026-10-27 起); 动词和公开表的合在一起. 拿不到的那几件、够不着的高度取两边的
   * 并集 (本机把同一层挨着的几本一起记了, 服务端只收了试的那一本).
   */
  load(st) {
    if (!st || typeof st !== 'object') return;
    const t = this.now(), at = (ago) => t - (Number(ago) || 0), map = (o, f) => Object.fromEntries(Object.entries(o || {}).map(([k, v]) => [k, f(v)]));
    this.things = map(st.things, (v) => ({ first: at(v.first_ago), last: at(v.last_ago), n: v.n || 1 }));
    this.states = map(st.states, (v) => ({ level: v.level, at: at(v.ago) }));
    this.spills = (st.spills || []).map(({ ago, ...p }) => ({ ...p, seen: at(ago) }));
    this.tries = map(st.tries, (d) => map(d, (k) => ({ n: k.n, ok: k.ok, v: k.v || {}, last: at(k.ago) })));
    this.skips = st.skips || {};
    this.likes = st.likes || {};
    this.visits = map(st.visits, at);
    if (st.where) this.where = map(st.where, (v) => ({ at: v.at, seen: at(v.ago) }));
    Object.assign(this.verbOf, st.verbs || {});
    const stuck = map(st.stuck, at), empty = !Object.keys(this.things).length && !Object.keys(this.tries).length;
    if (empty) {  // 服务端那份是空的 (你在设置里重置了她的小窝学习, 或者第一次连上): 本机记的也不留
      if (!st.where) this.where = {};
      this.high = st.high ?? null;
    } else {
      for (const [n, when] of Object.entries(this.stuck)) if (!(stuck[n] >= when)) stuck[n] = when;
      if (st.high !== null && st.high !== undefined) this.tooHigh(st.high);
    }
    this.stuck = stuck;
    this.save();
  }

  /** 走到跟前看清了, 按经验没做 (不算试了一次). */
  skipped(act, ctx) { const d = (this.skips[act] ??= {}); d[ctx] = (d[ctx] || 0) + 1; this.save(); }

  /** 走不到 node 跟前, 拿不到. */
  cannot(node) { this.stuck[node] = this.now(); this.save(); }

  /** 她以为 node 拿不到 (一天以内试过拿不到). */
  blocked(node) { return this.now() - (this.stuck[node] ?? -Infinity) < 24 * HOUR; }

  /** 伸手够 y 那么高的东西没够着: 记下, 这么高和更高的以后不去拿了 (看得出东西在多高). */
  tooHigh(y) { if (this.high === null || y < this.high) { this.high = +y.toFixed(2); this.save(); } }

  /** 她以为够得着 (没在这么高的地方失败过). */
  reachable(y) { return this.high === null || y < this.high - 0.03; }

  /** 看见了 node 在 at (粗到 0.1 m): 记下这一件在哪. 挪了 10 cm 以上才存. */
  seenAt(node, at) {
    const old = this.where[node], p = at.map((x) => Math.round(x * 10) / 10);
    this.where[node] = { at: p, seen: this.now() };
    if (!old || Math.hypot(old.at[0] - p[0], old.at[1] - p[1], old.at[2] - p[2]) > 0.1) this.save();
  }

  /** 走到记得的地方没看见它: 不知道它在哪了 (下次看见再记). */
  lost(node) { if (this.where[node]) { delete this.where[node]; this.save(); } }

  /** 她以为 node 在哪 ([x, y, z]); 没看见过、或者去找没找到, 返回 null. */
  whereOf(node) { return this.where[node]?.at ?? null; }

  /** 看见了 thing (第一次看见返回 true). */
  saw(thing) {
    const t = this.now(), k = this.things[thing];
    if (k) { k.last = t; k.n++; return false; }
    this.things[thing] = { first: t, last: t, n: 1 };
    this.news.push(thing);
    this.save();
    return true;
  }

  knows(thing) { return !!this.things[thing]; }

  /** 看到 thing 现在的粗档 (只记看到的, 不是真值). */
  see(thing, level) {
    const old = this.states[thing];
    this.states[thing] = { level, at: this.now() };
    if (old?.level !== level) this.save();
  }

  /** 最近看到的粗档; 太久没看 (STALE_H 小时) 返回 'unknown'. */
  level(thing, staleH = STALE_H) {
    const s = this.states[thing];
    return s && this.now() - s.at < staleH * HOUR ? s.level : 'unknown';
  }

  /** 看见一摊洒出来的 (同一摊 id 只记一次; at 地面上的粗位置 [x, z], y 多高: 地上还是台面上). */
  sawSpill(id, kind, at, y = 0) {
    if (this.spills.some((s) => s.id === id)) return false;
    this.spills.push({ id, kind, at: at.map((x) => +x.toFixed(2)), y: +y.toFixed(1), seen: this.now() });
    if (this.spills.length > 8) this.spills.shift();
    this.save();
    return true;
  }

  /** 那一摊她处理完了 (擦掉了, 或者走过去发现已经干了). */
  forgetSpill(id) { this.spills = this.spills.filter((s) => s.id !== id); this.save(); }

  visit(spot) { this.visits[spot] = this.now(); this.save(); }

  /** 很久没去 (或者没去过) 的地方. */
  stale(spots, h = VISIT_H) { return spots.filter((s) => !(this.now() - (this.visits[s] ?? -Infinity) < h * HOUR)); }

  /** 经验的分量: 最近几次 (window) 以内按次数, 很久没试 (fade 小时) 就淡掉. */
  _w(k) {
    if (!k?.n) return 0;
    const fade = this.fade && k.last !== undefined ? Math.exp(-Math.max(0, this.now() - k.last) / (this.fade * HOUR)) : 1;
    return Math.min(k.n, this.window) * fade;
  }

  /**
   * act 在情境 ctx 下她期望补哪些需求、试过几次: 常识的先验 (prior)、同一动词别的事在这个情境下的经验 (迁移, 最多算 SHARE 次)、
   * 自己的经验, 按分量混. weight 是经验一共算几次 (好奇心看它: 很久没试的又会想试试).
   */
  expect(act, ctx, prior = {}, verb = this.verbOf[act]) {
    const k = this.tries[act]?.[ctx], w = this._w(k);
    const parts = [[prior, N0], [k?.v || {}, w]];
    let shared = 0;
    if (this.share && verb) {
      const others = Object.entries(this.tries).filter(([id, t]) => id !== act && this.verbOf[id] === verb && t[ctx]?.n).map(([, t]) => t[ctx]);
      const sw = others.reduce((s, o) => s + this._w(o), 0);
      if (sw > 0) {
        const v = {};
        for (const o of others) for (const [key, x] of Object.entries(o.v)) v[key] = (v[key] || 0) + (x * this._w(o)) / sw;
        shared = Math.min(sw, SHARE);
        parts.push([v, shared]);
      }
    }
    const total = parts.reduce((s, [, x]) => s + x, 0), g = {};
    for (const key of new Set(parts.flatMap(([v]) => Object.keys(v)))) g[key] = parts.reduce((s, [v, x]) => s + (v[key] || 0) * x, 0) / total;
    return { gives: round(g), n: k?.n || 0, weight: +(w + shared).toFixed(2), shared: +shared.toFixed(2), fails: (k?.n || 0) - (k?.ok ?? 0) };
  }

  /** 做完一次: felt 是她感到的需求变化 (做成了的话), ok 做没做成, hurt 疼了、摔了扣的 (按开心算). */
  tried(act, ctx, { felt = {}, ok = true, hurt = 0, verb = null } = {}) {
    if (verb) this.verbOf[act] = verb;
    if (this.frozen) return { n: 0, v: {} };
    const k = ((this.tries[act] ??= {})[ctx] ??= { n: 0, ok: 0, v: {} });
    const obs = { ...felt };
    if (!ok) obs.fun = (obs.fun || 0) - 2;  // 没做成: 有点扫兴
    if (hurt) obs.fun = (obs.fun || 0) - hurt;
    k.n++;
    if (ok) k.ok++;
    const w = Math.min(k.n, this.window);
    for (const key of new Set([...Object.keys(k.v), ...Object.keys(obs)])) k.v[key] = (k.v[key] || 0) + ((obs[key] || 0) - (k.v[key] || 0)) / w;
    k.v = round(k.v);
    k.last = this.now();
    this.save();
    return { n: k.n, v: k.v };
  }

  /** 单件东西 (哪本书) 她觉得怎么样: 'new' 没碰过 / 'liked' / 'meh'. */
  likeOf(node) {
    const l = this.likes[node];
    return !l ? 'new' : l.v >= 6 ? 'liked' : 'meh';
  }

  liked(node, fun) {
    if (this.frozen) return;
    const l = (this.likes[node] ??= { n: 0, v: 0 });
    l.n++;
    l.v = +(l.v + (fun - l.v) / Math.min(l.n, 3)).toFixed(1);
    this.save();
  }

  /** 最近一次做 act (任何情境) 是多久以前 (秒); 没做过 Infinity. */
  since(act) {
    const last = Math.max(...Object.values(this.tries[act] || {}).map((k) => k.last ?? -Infinity), -Infinity);
    return this.now() - last;
  }

  summary() {
    return { things: Object.keys(this.things), states: Object.fromEntries(Object.entries(this.states).map(([k, s]) => [k, s.level])),
      spills: this.spills.length, tries: this.tries, likes: this.likes, high: this.high, stuck: Object.keys(this.stuck).filter((n) => this.blocked(n)),
      where: Object.keys(this.where).length };
  }

  save() {
    if (!this.storage) return;
    try {
      this.storage.setItem(KEY, JSON.stringify({ things: this.things, states: this.states, spills: this.spills, tries: this.tries,
        likes: this.likes, visits: this.visits, high: this.high, stuck: this.stuck, where: this.where, verbOf: this.verbOf, skips: this.skips }));
    } catch { /* 存不了就算了 */ }
  }
}

/**
 * 她这一刻能想到的可供性 (挑下一件事的候选): spec 是可供性表的 acts, things 东西 -> 要认识哪几样 (needs), known 她的了解,
 * at {night, social, nodes: 东西 -> 节点名表 (哪几本书, 调用方只给她以为拿得到的)}. 只读她的了解和她自己的需求, 不读房间:
 * 一套东西里只有她知道在哪的那几件算 (认识"书"不等于知道这一本在哪).
 * 每件: { id, name, going, doing, script: 'afford', verb, ctx 情境, gives 她期望补的, want, weight, n 这个情境试过几次 }.
 */
export function options(spec, known, { night = false, social = 70, spots = [], nodes = {} } = {}) {
  const out = [];
  for (const a of spec) {
    // 要认识这几样东西; 是一件件实物的 (书、水壶、兔子) 还要知道至少一件在哪
    if (!(a.needs || []).every((t) => known.knows(t) && (!nodes[t]?.length || nodes[t].some((n) => known.whereOf(n))))) continue;
    const ctx = situation(a, known, { night, social, spots, nodes });
    if (ctx === null) continue;
    const { gives, n, weight: seen } = known.expect(a.id, ctx, a.prior, a.verb);
    const want = Object.fromEntries(NEEDS.filter((k) => gives[k] > 0).map((k) => [k, Math.min(2, gives[k] / 10)]));
    const weight = Math.max(0.06, 0.2 + Math.max(-5, Math.min(15, worth(gives))) / 6) * curious(seen);
    const tried = Object.values(known.tries[a.id] || {}).reduce((s, k) => s + k.n, 0);  // 不分情境一共试过几次 (提示词里说)
    out.push({ id: a.id, name: a.name, going: a.going, doing: a.doing, script: 'afford', verb: a.verb, thing: a.thing, ctx, gives, want,
      weight: +weight.toFixed(3), n, tried });
  }
  return out;
}

/** 这件事现在算哪种情境 (她以为的); null 是现在做不了 (没东西可擦、没地方可去). */
export function situation(a, known, { night = false, social = 70, spots = [], nodes = {} } = {}) {
  switch (a.verb) {
    case 'read': {  // 有喜欢的书 -> liked; 还有没看过的 -> new; 都看过、都一般 -> meh; 一件都不知道在哪 -> 做不了
      const list = (nodes[a.thing] || []).filter((n) => known.whereOf(n)).map((n) => known.likeOf(n));
      if (!list.length) return null;
      if (a.again) return known.since(a.id) < a.again * HOUR ? 'again' : 'fresh';
      return list.includes('liked') ? 'liked' : list.includes('new') ? 'new' : 'meh';
    }
    case 'water': return known.level(a.thing);
    case 'switch': return `${night ? 'night' : 'day'}-${known.level(a.thing, 1e9) === 'off' ? 'off' : 'on'}`;
    case 'wipe': return known.spills.length ? known.spills.at(-1).kind : null;  // 最近看见的那一摊
    case 'hug': return social < 40 ? 'lonely' : 'fine';
    case 'explore': return known.stale(spots).length ? 'stale' : 'recent';
    default: return 'any';
  }
}

/**
 * 上行 room_think 带的: 每个候选可供性现在算哪种情境 (固定词表), 一共试过几次、这个情境下她期望补哪项多少 (只有编号和数字).
 * 认知开着时服务端按自己的发现簿算次数和经验, 只用这里的情境.
 */
export function forPlan(list) {
  return list.filter((a) => a.script === 'afford').map((a) => ({ act: a.id, ctx: a.ctx, n: a.tried ?? a.n, expect: a.gives }));
}
