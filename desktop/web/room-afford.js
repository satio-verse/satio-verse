// 小窝的可供性 (见 docs/roadmap/room-afford.md): 房间里的东西能拿来干什么 (room-scenes/<场景>.afford.json), 她看见了才知道,
// 试过才知道好不好 (room-known.js). 挑下一件事时和场景表 life 里原有的事一起当候选 (room-acts.js 的 Life), 做的时候用已有的动作积木
// 组合: 拿起来 (行动层的 pick, 矮处蹲下够)、坐下、捧着 (diaryHold)、蹲下 / 伸手 (crouch / hold 的手目标每帧挪)、伸手按 (reach)、看 (lookAt).
// 这里是环境: 书好不好看、土里还有多少水、灯开没开、哪里洒了一摊是真值, 只用来摆动作、算后果、判"她这一刻看得见什么";
// 她的了解在 Known 里, 挑事情 (options) 只读它. 真值存在 pet.room.world (土的湿度、灯), 她的了解存在 pet.room.known.
import * as THREE from 'three';
import { buildGrid, nearestWalkable, toCell, walkable } from '/static/nav.js';
import { crouchReach, freePlace, grab, homeOf, place, standNear } from '/pet/room-acts.js';
import { Known, NEEDS, options, pickOne as pickInstance, sees, situation, skipHere } from '/pet/room-known.js';
import { outcomeOf, soilLevel } from '/pet/room-world.js';
import { herDate } from '/pet/tz.js';
import { loadRoomAfford } from '/pet/room-scene-config.js';
import { createRoomStorage, DEFAULT_ROOM_SCENE } from '/pet/room-scene-state.js';

const KEY = 'pet.room.world';
const DAY = 86400;
const QUIET_OFF = new URLSearchParams(location.search).get('quiet') === '0';
const rand = (a, b) => a + Math.random() * (b - a);
const pickOne = (list) => list[Math.floor(Math.random() * list.length)];
const dark = () => { const h = herDate().getHours(); return h >= 18 || h < 7; };  // 天黑了 (按她的时区; 开灯有用的时候, 和睡觉的"夜里"不一样)
const DRY = new THREE.Color(0.62, 0.5, 0.22);

function readWorld(storage) {
  try { return JSON.parse(storage?.getItem(KEY) || 'null') || {}; } catch { return {}; }
}

export class Afford {
  /** 新房间只读本场景的可供性/后果表; 旧房间保留 petal-oak 回退, 全部读不到才返回 null. */
  static async load(r, slug) {
    const spec = await loadRoomAfford(slug);
    return spec ? new Afford(r, spec) : null;
  }

  constructor(r, spec) {
    this.r = r;
    this.spec = spec;
    this.storage = r.storage || createRoomStorage(r.sceneId || DEFAULT_ROOM_SCENE);
    this.known = new Known({ now: () => r.needs.now(), storage: this.storage });
    this.world = readWorld(this.storage);
    this.world.plant ??= { m: 0.5, at: r.needs.now() };
    this.world.lamps ??= {};
    // 东西 -> 节点名表 (房间里没有的去掉; prefix 是一套: 书架上的 26 本书)
    this.nodes = {};
    for (const [id, t] of Object.entries(spec.things)) {
      const names = t.prefix ? this._prefixed(t.prefix) : (t.nodes || []);
      this.nodes[id] = names.filter((n) => r.props.node(n));
    }
    this.acts0 = spec.acts.filter((a) => (a.needs || []).every((t) => this.nodes[t]?.length || spec.things[t]?.at));
    for (const a of spec.acts) this.known.verbOf[a.id] = a.verb;  // 动词来自公开的表 (同一动词的经验能迁移)
    this.last = null;
    this._pos = {};
    this._n = 0;
    this.linked = false;  // 这条连接声明过可供性、要过一份她的了解 (服务端 wm/cognition/afford.py)
    this.sent = '';       // 上一份报给服务端的 (一样就不再报)
    this.gone = [];       // 走到记得的那一处、发现那一摊没了 (不是她擦的), 下一份报 gone
    this.lost = [];       // 走到记得的地方、没看见那一件, 下一份报 lost
    this.pend = {};       // 这一趟要一起报的: liked 那一本 / high 够不着的高度 / stuck 走不到的那件 / forget 擦掉的那几摊
    for (const [id, t] of Object.entries(spec.things)) if (t.lamp !== undefined) this._lamp(id, this.world.lamps[id] ?? true);
    this._leaves();
  }

  _prefixed(prefix) {
    const out = [];
    this.r.props.room.traverse((o) => { if (o.name.startsWith(prefix) && /^[A-Za-z_]+\d+$/.test(o.name)) out.push(o.name); });
    return out.sort();
  }

  // -- 真值 ---------------------------------------------------------------------------------------
  /** 东西 (或节点) 现在在哪 [x, y, z]; 没有返回 null. */
  where(id) {
    const t = this.spec.things[id];
    if (t?.at) return t.at;
    const n = this.r.props.node(this.nodes[id]?.[0] ?? id);
    return n ? new THREE.Box3().setFromObject(n).getCenter(new THREE.Vector3()).toArray() : null;
  }

  /** 土里的水 (0 干透, 1 正好浇透, 1 以上浇多了): 按小屋的钟每天干掉 decay_day. */
  moisture() {
    const p = this.world.plant, k = this.spec.acts.find((a) => a.verb === 'water')?.out?.decay_day ?? 0.3;
    return Math.max(0, p.m - (k * (this.r.needs.now() - p.at)) / DAY);
  }

  _setMoisture(m) {
    this.world.plant = { m, at: this.r.needs.now() };
    this._save();
    this._leaves();
  }

  /** 看到的土的粗档. */
  soil() { return soilLevel(this.moisture()); }

  /** 干了的时候叶子发黄一点 (后果看得见): 分 4 档, 变了才重画房间. */
  _leaves() {
    const node = this.r.props.node(this.nodes.plant?.[0]);
    if (!node) return;
    const k = Math.round(Math.max(0, Math.min(1, (0.3 - this.moisture()) / 0.3)) * 4) / 4;
    if (k === this._leafK) return;
    this._leafK = k;
    node.traverse((o) => {
      if (!o.isMesh || !o.material?.color) return;
      if (!o.userData.leaf0) { o.material = o.material.clone(); o.userData.leaf0 = o.material.color.clone(); }
      const c0 = o.userData.leaf0;
      if (c0.g > c0.r) o.material.color.copy(c0).lerp(DRY, k * 0.55);  // 只变绿的 (叶子), 盆不变
    });
    this.r.props.cache.dirty = true;
  }

  /** 灯 (落地灯): 点光源 (room-life.js 的 lights.lamp) 和灯罩、灯泡的自发光一起开关. */
  _lamp(id, on) {
    const t = this.spec.things[id];
    this.world.lamps[id] = on;
    this.r.lights?.lamp?.(t.lamp, on);
    if (t.glow) {
      this.r.props.room.traverse((o) => {
        if (!o.isMesh || !o.name.startsWith(t.glow) || !o.material?.emissive) return;
        if (o.userData.glow0 === undefined) { o.material = o.material.clone(); o.userData.glow0 = o.material.emissiveIntensity ?? 1; }
        o.material.emissiveIntensity = on ? o.userData.glow0 : o.userData.glow0 * 0.06;
      });
      this.r.props.cache.dirty = true;
    }
  }

  _save() { try { this.storage.setItem(KEY, JSON.stringify(this.world)); } catch { /* 存不了就算了 */ } }

  // -- 她看见什么 (每秒) ----------------------------------------------------------------------------
  /**
   * 她站在哪、脸朝哪: 离得够近、在视野里的东西记成"见过"; 走到 look 那么近才看得出土干不干; 灯开没开整个屋里都看得见 (认识这盏灯以后);
   * 洒出来的一摊只用液体那边的公开接口 seenPuddles() (她此刻看得见的、粗位置, 和判看没看见落点同一个规则). 睡着、躺着时不看.
   */
  tick() {
    const r = this.r, L = r.layer, K = this.known;
    if (r.life?.sleeping || r.poses.name === 'lie' || r.poses.name === 'lieRest') return;
    const me = { x: L.char.x, z: L.char.z, yaw: L.char.yaw };
    if (this._n++ % 5 === 0) { this._pos = {}; this._low = {}; }  // 东西的位置 5 秒量一次 (挪过家具、拿走放回)
    const up = { things: [], nodes: [], puddles: [], visited: [] };  // 这一刻看见的, 报给服务端 (编号、固定词表、粗位置)
    for (const [id, t] of Object.entries(this.spec.things)) {
      const list = t.at ? [id] : this.nodes[id] || [];
      // 看得见的那几件: 记下每一件在哪 (她拿在手里的不算; 认识"书"和知道"这一本在哪"分开记)
      const seen = list.filter((n) => sees(me, this._pos[n] ??= (t.at || this.where(n)), t.see));
      if (!seen.length) continue;
      if (K.saw(id)) this._notice(t.name);
      if (!t.at) for (const n of seen) if (!L.items[n]?.held) { K.seenAt(n, this._pos[n]); up.nodes.push({ id: n, at: K.whereOf(n) }); }
      const state = t.look && sees(me, this._pos[list[0]], t.look) ? this.soil() : t.lamp !== undefined ? (this.world.lamps[id] === false ? 'off' : 'on') : null;
      if (t.look && state) K.see(id, state);
      up.things.push(state ? { id, state } : { id });
    }
    for (const [id, t] of Object.entries(this.spec.things)) if (t.lamp !== undefined && K.knows(id)) K.see(id, this.world.lamps[id] === false ? 'off' : 'on');
    for (const p of r.liquid?.seenPuddles?.() || []) {  // 液体那边只给她此刻看得见的、粗的 (不读毫升、温度)
      const kind = p.kind === 'coffee' || p.stain ? 'coffee' : 'water';
      if (K.sawSpill(p.id, kind, [p.at[0], p.at[2]], p.at[1])) this._notice(p.surface === 'floor' ? '地上洒了一摊' : '台面上洒了一摊');
      up.puddles.push({ id: p.id, kind, at: [p.at[0], p.at[2]], y: p.at[1] });
    }
    for (const [spot, [x, z]] of Object.entries(this.spec.spots || {})) if (Math.hypot(me.x - x, me.z - z) < 0.7) { K.visit(spot); up.visited.push(spot); }
    this._report(up);
    if (this._n % 30 === 0) this._leaves();
  }

  /**
   * 报给服务端的发现簿 (room_observe 里的 affords / things / puddles / gone / visited; wm/cognition/afford.py): 这条连接第一次先声明
   * 有哪些东西、地方、事, 再要一份她的了解 (room_known) 换进本机; 之后看见的和上次不一样才报. 没连上不报, 本机这份接着用.
   */
  _report(up) {
    const S = this.r.server;
    if (!S?.connected?.()) { this.linked = false; this.sent = ''; return; }
    const msg = { ...up, ...(this.gone.length && { gone: this.gone.splice(0) }), ...(this.lost.length && { lost: this.lost.splice(0) }) };
    if (!this.linked) {
      this.linked = true;
      msg.affords = { things: Object.fromEntries(Object.entries(this.spec.things).map(([id]) => [id, this.nodes[id] || []])),
        spots: Object.keys(this.spec.spots || {}), acts: this.acts0.map((a) => a.id) };
      S.tell('room_observe', msg);
      this.sent = JSON.stringify(up);
      this._sync(S.ask?.('room_known', {}, ['room_known'], 3000));
      return;
    }
    const key = JSON.stringify(up);
    if (key === this.sent && !msg.gone && !msg.lost) return;
    this.sent = key;
    if (msg.things.length || msg.nodes.length || msg.puddles.length || msg.visited.length || msg.gone || msg.lost) S.tell('room_observe', msg);
  }

  /** 服务端回的她的了解 (room_known) 换进本机 (Known.load). */
  _sync(ask) {
    Promise.resolve(ask).then((st) => { if (st?.ok) this.known.load(st); }).catch(() => {});
  }

  /** 第一次注意到: 记一笔; 没在做事、或者正在四处看看时状态栏说一句. */
  _notice(name) {
    const r = this.r;
    r.log(`小屋: 她注意到了${name}`);
    if (!r.life?.current || r.life.current.verb === 'explore') r.status(`她注意到了${name}`);
  }

  // -- 候选 ---------------------------------------------------------------------------------------
  /** 东西底面多高 (放在哪一层; 看得出来). */
  low(n) {
    if (this._low?.[n] === undefined) {
      const node = this.r.props.node(n);
      (this._low ??= {})[n] = node ? new THREE.Box3().setFromObject(node).min.y : 0;
    }
    return this._low[n];
  }

  /** 她想得到的情境: 东西按她以为够得着的算 (书在哪一层看得出来, 够不着是她试过才知道的). */
  _at() {
    const nodes = Object.fromEntries(Object.entries(this.nodes).map(([id, list]) =>
      [id, list.filter((n) => this.known.reachable(this.low(n)) && !this.known.blocked(n))]));
    return { night: dark(), social: this.r.needs.v.social, spots: Object.keys(this.spec.spots || {}), nodes };
  }

  /**
   * 场景表里原有的事要不要淡一点 (Life.weight 乘上去): 可供性的 quiets 写着它替掉了哪件占位的事 (真的拿书看以后, 不再空着手
   * 坐在阅读椅上"看书"、站在书架前"翻书"). 只在她做成过那件可供性以后才淡. 页面参数 quiet=0 关掉 (A/B 用).
   */
  quiet(id) {
    if (QUIET_OFF) return 1;
    let k = 1;
    for (const a of this.acts0) {
      if (a.quiets?.[id] === undefined) continue;
      if (Object.values(this.known.tries[a.id] || {}).some((t) => t.ok > 0)) k *= a.quiets[id];
    }
    return k;
  }

  /** 给 Life 的事项: all 时连她还不认识的也给 (自检排的顺序、room_do 让她去做), 带 known 标她认不认识. */
  acts({ all = false } = {}) {
    const at = this._at(), mine = options(this.acts0, this.known, at), out = [];
    for (const a of this.acts0) {
      const m = mine.find((x) => x.id === a.id);
      if (m) out.push({ ...m, known: true });
      else if (all) out.push({ id: a.id, name: a.name, going: a.going, doing: a.doing, script: 'afford', verb: a.verb, known: false,
        ctx: situation(a, this.known, at), gives: a.prior, weight: 0.5, n: 0 });
    }
    return out;
  }

  // -- 做 -----------------------------------------------------------------------------------------
  /**
   * 做一件可供性 (Life 的脚本 afford 调这里): 按动词用积木做, 环境按真值算后果 (需求怎么变、世界怎么变), 她记下自己感到的
   * (需求真的变了多少、疼没疼、做没做成) 进这个情境的经验. 出错照样记 (没做成), 再抛给 Life.
   */
  async run(ctx, a) {
    const r = this.r, spec = this.acts0.find((x) => x.id === a.id);
    if (!spec) throw new Error('不认识这件事');
    let sit = a.ctx ?? situation(spec, this.known, this._at());
    // 叫她去看书, 她还不知道哪一本在哪: 先找找 (她自己挑的时候只挑知道在哪的, 走不到这里)
    if (sit === null && spec.verb === 'read' && await seek(this, ctx, spec.thing)) sit = situation(spec, this.known, this._at());
    if (sit === null) throw new Error('现在做不了');
    const t0 = { pains: r.body.totals.pains, falls: r.body.totals.falls };
    const out = { id: a.id, ctx: sit, felt: {}, ok: false };
    this.last = out;
    this.pend = {};
    try {
      Object.assign(out, await VERBS[spec.verb](this, ctx, spec, sit, out));
      out.ok = true;
    } finally {
      const hurt = (r.body.totals.pains - t0.pains) * 2 + (r.body.totals.falls - t0.falls) * 5;
      if (hurt) out.hurt = hurt;
      if (out.void) r.log(`小屋: ${spec.name} (${out.ctx}) 走过去发现已经没了, 不算做了`);  // 别人擦了、被容量上限删了: 不进经验
      else if (out.skip) {  // 到了跟前看清楚了, 按经验不该做: 不算试了一次
        this.known.skipped(a.id, out.ctx);
        r.log(`小屋: ${spec.name} (${out.ctx}) 走过去看了看, 没做`);
      } else {
        const k = this.known.tried(a.id, out.ctx, { felt: out.felt, ok: out.ok, hurt, verb: spec.verb });
        r.log(`小屋: ${spec.name} (${out.ctx}) ${out.ok ? '' : '没成, '}感到 ${JSON.stringify(out.felt)}${hurt ? ` 疼 ${hurt}` : ''} · 这个情境第 ${k.n} 次, 平均 ${JSON.stringify(k.v)}`);
      }
      // 报给服务端的发现簿 (那边是准的), 回来的了解换进本机. 没连上就只记本机的
      const S = r.server;
      if (!out.void && S?.connected?.() && this.linked) {
        const p = this.pend;
        this._sync(S.ask?.('room_afford_done', { act: a.id, ctx: out.ctx, verb: spec.verb, ok: out.ok, felt: out.felt, hurt: hurt || 0, skip: !!out.skip,
          ...(p.liked && { node: p.liked.node, liked: p.liked.fun }), ...(p.high !== undefined && { high: p.high }),
          ...(p.stuck?.length && { stuck: p.stuck }), ...(p.forget?.length && { forget: p.forget }) }, ['room_known'], 3000));
      }
    }
  }

  /**
   * 环境把后果加到需求上, 返回她感到的: 这件事本身的效果 (开心值已经满了, 好看的书照样是好看的; 试过按截断以后的记,
   * 满着的时候做什么都像"没意思", 学到的全偏低 —— 快进时开心常在 95 以上, 开灯记成 +0.2).
   */
  feel(gives) {
    this.r.needs.add(gives);
    return Object.fromEntries(NEEDS.map((k) => [k, +(gives[k] || 0).toFixed(1)]).filter(([, d]) => d));
  }

  summary() {
    return { ...this.known.summary(), soil: this.soil(), moisture: +this.moisture().toFixed(2), lamps: { ...this.world.lamps },
      options: this.acts().map((a) => ({ id: a.id, ctx: a.ctx, n: a.n, w: a.weight, gives: a.gives })) };
  }
}

// -- 动作积木的组合 (每个返回 { felt, ... }) ------------------------------------------------------------
const NAV = /够不着|走不到|没有合适/;

/** 在现在的摆法下, 东西旁边 (0.4-0.7 m) 有没有她走得到的地方站. */
function standable(r, p, grid = buildGrid(r.scene)) {
  const seen = r.mover._reach(grid, nearestWalkable(grid, r.layer.char.x, r.layer.char.z));
  for (const rr of [0.4, 0.5, 0.6, 0.7]) for (let k = 0; k < 16; k++) {
    const x = p.x + Math.sin((k / 16) * Math.PI * 2) * rr, z = p.z + Math.cos((k / 16) * Math.PI * 2) * rr, [i, j] = toCell(grid, x, z);
    if (walkable(grid, i, j) && seen[j * grid.w + i]) return true;
  }
  return false;
}

/** 能挪的家具 key 可以去的位置 (配置的几个 + 周围 0.3 / 0.45 / 0.6 m 八个方向), 挪得越少的在前. */
function spotsOf(m, key) {
  const b = m.base[key], cur = m.pose[key];
  const out = (m.cfg[key].spots || []).map(([x, z, yaw]) => ({ x, z, yaw: b.yaw + (yaw || 0) }));
  for (const rr of [0.3, 0.45, 0.6]) for (let k = 0; k < 8; k++) out.push({ x: cur.x + Math.sin((k / 8) * Math.PI * 2) * rr, z: cur.z + Math.cos((k / 8) * Math.PI * 2) * rr, yaw: cur.yaw });
  return out.filter((t) => Math.hypot(t.x - cur.x, t.z - cur.z) >= 0.15)
    .sort((u, v) => Math.hypot(u.x - cur.x, u.z - cur.z) - Math.hypot(v.x - cur.x, v.z - cur.z));
}

/** key 挪到哪个 (验得过的) 位置以后 ok() 成立; 没有返回 null. 只是试摆, 试完放回原处. */
function spotFor(m, key, ok) {
  const cur = { ...m.pose[key] };
  for (const to of spotsOf(m, key)) {
    if (!m.valid(key, to)) continue;
    m.apply(key, to);
    const good = ok();
    m.apply(key, cur);
    if (good) return to;
  }
  return null;
}

/** 家具 a 的盒子离 p (或另一件家具 b 的盒子) 近不近. */
const nearBox = (m, key, p, pad = 0.6) => m._boxes(key).some((b) => Math.abs(p.x - b.center[0]) < b.size[0] / 2 + pad && Math.abs(p.z - b.center[1]) < b.size[1] / 2 + pad);

/**
 * 东西被能挪的家具挡着, 她走不到跟前: 找一件离它近的家具、一个挪过去以后东西旁边站得下的位置 (Mover.valid 验过), 推过去;
 * 一件挪不动 (四面都挨着东西), 先把挨着它的另一件挪开, 再挪它. 阅读椅就是这样: 左边书架、右边小边桌、前面墙、后面落地灯,
 * 下层的书都在椅子和书架之间那条 0.34 m 的缝里; 小边桌往右 0.45 m、阅读椅再往右 0.45 m, 下层 10 本都拿得到 (试摆一次 0.3 秒).
 * 挪了返回 true. 摆法记住 (Mover), 下次直接拿得到.
 */
async function clearWay(A, ctx, p, name) {
  const r = A.r, m = r.mover;
  if (standable(r, p)) return false;  // 站得下却没拿成: 不是被挡着
  const keys = Object.keys(m.base).filter((k) => m.cfg[k].rearrange !== false);
  const near = keys.filter((k) => nearBox(m, k, p));
  const ok = () => standable(r, p);
  let plan = null;
  for (const key of near) { const to = spotFor(m, key, ok); if (to) { plan = [[key, to]]; break; } }
  for (const key of plan ? [] : near) {  // 两步: 先挪挨着它的
    for (const k2 of keys.filter((k) => k !== key && m._boxes(key).some((b) => nearBox(m, k, { x: b.center[0], z: b.center[1] }, Math.max(...b.size) / 2 + 0.3)))) {
      const cur2 = { ...m.pose[k2] };
      const first = spotFor(m, k2, () => { const to = spotFor(m, key, ok); if (to) plan = [[k2, null], [key, to]]; return !!to; });
      m.apply(k2, cur2);
      if (first) { plan[0][1] = first; break; }
      plan = null;
    }
    if (plan) break;
  }
  if (!plan) {
    r.log(`小屋: ${name}旁边站不下, 附近能挪的家具挪了也站不下`);
    return false;
  }
  const blocker = m.cfg[plan.at(-1)[0]].name;
  for (const [key, to] of plan) {
    const why = key === plan.at(-1)[0] ? `${blocker}挡着${name}, 她把${blocker}往外挪一挪` : `${blocker}挪不开, 她先把旁边的${m.cfg[key].name}挪开`;
    ctx.say(why);
    await ctx.beat(0.8, 1.4);
    await m.push(ctx, key, to, why);
    for (const id of [...(m.cfg[key].objects || []), ...(m.cfg[key].decor || [])]) delete m._entry(id).walkable;  // 推完马上恢复寻路
    r.layer.grid = buildGrid(r.scene);
  }
  r.log(`小屋: 挪开了挡着${name}的家具: ${plan.map(([k, to]) => `${k}->${to.x.toFixed(2)},${to.z.toFixed(2)}`).join(' ')}`);
  return true;
}

/**
 * 不知道东西在哪 (你叫她去做、自检排的, 她自己挑的时候只挑知道在哪的): 按离她远近一处处走 (可供性表的 spots),
 * 每处站一下看看, 看见了 (tick 记下位置) 就停. node 给了是找这一件, 不给是这一样里随便哪件. 找到返回 true.
 */
async function seek(A, ctx, thing, node = null) {
  const r = A.r, K = A.known, have = () => (node ? !!K.whereOf(node) : (A.nodes[thing] || []).some((n) => K.whereOf(n)));
  if (have()) return true;
  ctx.say(`她不知道${A.spec.things[thing]?.name || '它'}在哪, 四处找找`);
  const L = r.layer, spots = Object.values(A.spec.spots || {}).sort((p, q) => Math.hypot(p[0] - L.char.x, p[1] - L.char.z) - Math.hypot(q[0] - L.char.x, q[1] - L.char.z));
  for (const xz of spots) {
    if (ctx.hurry) break;
    await ctx.walk(xz).catch(() => {});
    A.tick();
    for (let i = 0; i < 3 && !have(); i++) {  // 转着看一圈: 行动层原地转 90 度, 每个方向看一眼 (看得见只算前方 ±70°)
      await ctx.run({ do: 'turn', side: 'L' }).catch(() => {});
      await ctx.beat(0.4, 0.8);
      A.tick();
    }
    if (have()) { ctx.say(`找到${A.spec.things[thing]?.name || '它'}了`); return true; }
  }
  return false;
}

/**
 * 去她记得 node 在的地方看一眼 (认识"书"不等于知道这一本在哪; 评审 2026-10-27): 她不知道在哪 -> 做不了; 真的还在那 (0.3 m 以内) ->
 * 接着拿 (之后行动层按真实位置伸手, 那是环境在做事); 被挪走了 -> 走到记得的地方看, 没看见, 记成"不知道在哪了", 这件事没成.
 * 不按真实位置直接走过去 (以前那样, 书被挪到床头柜上她没看见也会直接走到床头柜).
 */
async function goLook(A, ctx, node, name) {
  const r = A.r, K = A.known;
  if (!K.whereOf(node) && !(await seek(A, ctx, Object.keys(A.nodes).find((t) => A.nodes[t].includes(node)), node))) throw new Error(`不知道${name}在哪`);
  const mem = K.whereOf(node);
  const real = A.where(node);
  if (real && Math.hypot(real[0] - mem[0], real[1] - mem[1], real[2] - mem[2]) < 0.3) return;
  const at = standNear(r, new THREE.Vector3(...mem), 0.6);
  if (at) await ctx.walk(at.pos, at.yaw);
  await ctx.beat(0.6, 1.2);
  const me = { x: r.layer.char.x, z: r.layer.char.z, yaw: r.layer.char.yaw };
  if (real && sees(me, real, 2) && Math.hypot(real[0] - mem[0], real[2] - mem[2]) < 0.6) return;  // 挪得不远, 一眼看见了
  K.lost(node);
  A.lost.push(node);  // 也告诉服务端的发现簿
  ctx.say(`${name}不在原来的地方了…`);
  throw new Error(`${name}不在记得的地方`);
}

/**
 * 拿起房间里的一件东西 (行动层的 pick, 矮处蹲下够), 记下从哪拿的 (放回去用). 返回放回原处的目标.
 * 走不到跟前: 被能挪的家具挡着就挪开再拿一次; 挪不开的, 高处 (1 m 以上) 记成"这么高够不着", 低处记成"那里拿不到".
 */
async function take(A, ctx, node, name, grip = null) {
  const r = A.r, id = r.props.asItem(node);
  if (!id) throw new Error(`${name}不见了`);
  await goLook(A, ctx, node, name);
  if (grip) r.layer.items[id].grip = grip;  // 书架上的书 (节点名里没有 Book)、手机: 按书握, 不按杯子竖着握
  const home = homeOf(r, id, name, null);
  r.props.wake(id);
  ctx.later(() => r.props.settle(id));
  try {
    await grab(ctx, id);
  } catch (err) {
    if (!NAV.test(err.message)) throw err;
    const p = r.layer.items[id].mesh.position.clone();
    if (!(await clearWay(A, ctx, p, name).catch((e) => { r.log(`小屋: 挪开挡着的家具没成 (${e.message})`); return false; }))) {
      if (A.low(node) >= 1) A.known.tooHigh(A.pend.high = A.low(node));
      else { A.known.cannot(node); (A.pend.stuck ??= []).push(node); }
      throw err;
    }
    r.props.wake(id);
    await grab(ctx, id);
  }
  r.layer.targets[home].approach = [r.layer.char.x, r.layer.char.z];  // 放回去时站在拿起来的地方
  return home;
}

/** 手里的东西放回原处 (放不回去就放到小边桌上). */
async function putBack(ctx, home) {
  try {
    await place(ctx, home);
  } catch (err) {
    ctx.r.log(`小屋: 放不回原处 (${err.message}), 放到小边桌上`);
    await place(ctx, freePlace(ctx.r, 'surf_table'));
  }
}

/** 手 (crouch / hold 姿势的目标点) 在 c 附近画小圈: 擦东西、浇水时手微微晃. 返回停下的函数. */
function circle(A, target, c, r = 0.06, hz = 0.9) {
  let t = 0;
  return A.r.props.every((dt) => { t += dt; target.set(c.x + Math.cos(t * hz * 6.28) * r, c.y, c.z + Math.sin(t * hz * 6.28) * r * 0.7); });
}

const VERBS = {
  /** 拿着看 (书、手机): 挑一本 (喜欢的 / 没看过的) -> 拿起来 -> 坐下 -> 捧着看 -> 放回原处. 好不好看是真值, 看完她记下喜不喜欢. */
  async read(A, ctx, spec, sit, out) {
    const r = A.r, K = A.known, thing = A.spec.things[spec.thing], pains0 = r.body.totals.pains;
    const list = A._at().nodes[spec.thing].filter((n) => K.whereOf(n));  // 她以为拿得到、知道在哪的那几件
    if (!list.length) throw new Error(`不知道哪本${thing.name}拿得到`);
    const node = pickInstance(K, list, sit);
    out.thing = node;
    const candidate = r.props.asItem(node), readingItem = r.layer.items[candidate];
    if (readingItem?.grasp && !readingItem.physical
        && Object.values(r.layer.items).some((item) => item !== readingItem && item.held)) {
      throw new Error('阅读前需要先放好另一只手的东西');
    }
    ctx.say(`她${spec.going}…`);
    let home;
    try {
      home = await take(A, ctx, node, thing.name, thing.grip);
    } catch (err) {  // 够不着 (书架上层) / 挤不过去: take 记下了, 以后挑拿得到的
      if (NAV.test(err.message)) {
        ctx.say(A.low(node) >= 1 ? pickOne(['那本太高了, 够不着…', '踮着脚也够不着, 算了']) : '那边挤不过去, 拿不到…');
        // 挤不过去的: 同一层挨着的几本 (看得出在同一个角落) 一起记成拿不到
        const p = A.where(node);
        if (A.low(node) < 1) {
          for (const n of A.nodes[spec.thing]) {
            if (n === node || Math.abs(A.low(n) - A.low(node)) >= 0.1 || Math.hypot(A.where(n)[0] - p[0], A.where(n)[2] - p[2]) >= 0.6) continue;
            K.cannot(n);
            (A.pend.stuck ??= []).push(n);  // 服务端的发现簿收列表
          }
        }
      }
      throw err;
    }
    const it = r.layer.items[node];
    if (it.grasp && !it.physical) {
      if (it.held !== 'left') throw new Error('这本书需要左手接触后再阅读');
    } else if (it.held === 'right') it.held = 'left';  // 旧物件仍用原姿势；新契约不允许裸换手
    const seat = pickOne(thing.seats || ['reading_chair']);
    await ctx.sit(seat);
    r.poses.set('diaryHold', { side: 'right' });
    ctx.later(() => r.poses.name === 'diaryHold' && r.poses.set(null));
    r.v.setEmotion('calm');
    ctx.say(`她${spec.doing}`);
    const secs = ctx.fast ? 4 : rand(25, 60);
    for (let t = 0; t < secs && !ctx.hurry; t += 6) {  // 隔一会儿轻轻点一下头 (翻页、看进去了)
      await ctx.wait(Math.min(6, secs - t));
      r.v.headAction?.('nod', { amplitude: 0.08, repetitions: 1 });
    }
    // 后果 (room-world.js): 书按好不好看, 天黑了在阅读椅上没开落地灯看得眼睛累; 手机连着刷没意思
    const lampOff = A.world.lamps.reading_lamp === false;
    if (!spec.again && dark() && seat === 'reading_chair' && lampOff) ctx.say('天黑了, 灯没开, 看得眼睛有点累');
    out.felt = A.feel(outcomeOf(spec, { node, dark: dark(), seat, lampOff, again: sit === 'again' }));
    if (!spec.again) {
      const fun = (out.felt.fun || 0) - (r.body.totals.pains - pains0) * 2;  // 拿的时候撞到头了, 这本也没那么想再拿
      K.liked(node, fun);
      A.pend.liked = { node, fun: +fun.toFixed(1) };
      ctx.say(K.likeOf(node) === 'liked' ? pickOne(['这本真好看～', '看进去了, 下次接着看']) : pickOne(['这本有点看不进去…', '嗯…一般般']));
    } else ctx.say(sit === 'again' ? '刷来刷去也没什么新的…' : '看了会儿手机, 挺有意思');
    r.poses.set(null);
    r.v.setEmotion('neutral');
    await ctx.wait(0.8, { hurry: false });
    await putBack(ctx, home);
    return { thing: node };
  },

  /** 浇花: 拿补水壶 -> 走到花旁边蹲下 -> 壶嘴对着盆里倾斜一会儿 -> 放回推车. 土干了浇最舒服, 湿着再浇会从盆底渗出来 (地上一摊水). */
  async water(A, ctx, spec, sit, out) {
    const r = A.r, o = spec.out, plant = A.where('plant');
    if (!plant) throw new Error('找不到花');
    ctx.say(`她${spec.going}, 先去拿水壶`);
    const node = A.nodes.pitcher[0], home = await take(A, ctx, node, '补水壶');
    await goLook(A, ctx, A.nodes.plant[0], '无花果树');  // 去她记得花在的地方 (挪过家具的话, 没看见挪就不知道)
    const pot = r.props.node(A.nodes.plant[0]);
    const box = new THREE.Box3().setFromObject(pot), soil = new THREE.Vector3((box.min.x + box.max.x) / 2, 0.42, (box.min.z + box.max.z) / 2);
    const at = standNear(r, soil, 0.5);
    if (!at) throw new Error('花旁边站不下');
    await ctx.walk(at.pos, at.yaw);
    // 走到跟前看看土: 这时才知道干不干 (经验按看到的记). 看到湿着、以前湿着浇过觉得不好: 不浇了, 水壶放回去
    K_see(A);
    const level = A.soil();
    out.ctx = level;
    if (skipHere(A.known, spec, level)) {  // 按经验 (自己的, 或者别的花迁移来的) 这一下浇了不好
      ctx.say('她看了看, 土还湿着, 今天不用浇');
      out.skip = true;
      await putBack(ctx, home);
      return { ctx: level, skip: true };
    }
    ctx.say('她蹲下来, 给无花果树浇水');
    const side = r.layer.items[node].held;
    const target = soil.clone().setY(0.42);
    r.body.begin('crouch');
    r.poses.set('crouch', { side, target, drop: 0.7, back: -0.05 });
    ctx.later(() => { r.props.tilt(node, 0); if (r.poses.name === 'crouch') r.poses.set(null); });
    await ctx.wait(1.1, { hurry: false });
    const stop = circle(A, target, soil.clone().setY(0.42), 0.04, 0.4);
    r.props.tilt(node, 0.9);
    await ctx.wait(ctx.fast ? 2.5 : 4, { hurry: false });
    stop();
    r.props.tilt(node, 0);
    // 后果 (真值): 浇之前土里有多少水决定舒不舒服; 本来就湿, 多出来的从盆底渗到地上
    const m = A.moisture();
    A._setMoisture(m + o.add);
    if (m + o.add > 1.25 && r.liquid?.leak) {
      r.liquid.leak([soil.x + 0.24, 0.12, soil.z + 0.18], o.overflow_ml, { kind: 'water' });  // 盆底渗出来, 落到地上成一摊
      ctx.say('水从盆底渗出来了…浇多了');
    } else ctx.say(level === 'dry' ? '土都干透了, 喝饱了吧～' : '浇好啦');
    out.felt = A.feel(outcomeOf(spec, { level }));
    A.known.see('plant', A.soil());
    r.poses.set(null);
    await ctx.wait(0.6, { hurry: false });
    r.body.end(true);
    await putBack(ctx, home);
    return { ctx: level };
  },

  /** 开关灯: 走到灯旁边伸手按一下. 天黑了开灯暖和, 白天开着没用, 天黑了关掉屋里暗. */
  async switch(A, ctx, spec, sit) {
    const r = A.r, id = spec.thing, p = new THREE.Vector3(...A.where(id)), on = A.world.lamps[id] !== false;
    ctx.say(`她${on ? '去关' : '去开'}${A.spec.things[id].name}`);
    const at = standNear(r, p, 0.42);
    if (!at) throw new Error('灯旁边站不下');
    await ctx.walk(at.pos, at.yaw);
    await ctx.reach('right', p, () => A._lamp(id, !on));
    A._save();
    A.known.see(id, on ? 'off' : 'on');
    const felt = A.feel(outcomeOf(spec, { dark: dark(), lampOn: on }));
    ctx.say(on ? (dark() ? '屋里一下子暗了…' : '白天不用开灯') : (dark() ? '暖暖的灯光, 舒服多了' : '白天开灯好像没什么用'));
    return { felt, ctx: `${dark() ? 'night' : 'day'}-${on ? 'on' : 'off'}` };
  },

  /** 擦洒出来的: 拿抹布 -> 走到她记得的那一摊 -> 蹲下 (台面上就站着) 手画圈擦 -> 擦掉 -> 抹布放回推车. 走过去发现已经干了 (水) 就算了. */
  async wipe(A, ctx, spec, sit, out) {
    const r = A.r, K = A.known, mem = K.spills.at(-1), o = spec.out;
    if (!mem) throw new Error('没有要擦的');
    ctx.say(`她${spec.going}`);
    const home = await take(A, ctx, A.nodes.cloth[0], '抹布');
    const side = r.layer.items[A.nodes.cloth[0]].held;
    // 她记得的那一处 (看见过的), 走过去再看: 还在不在、是不是干了的印子 (液体那边的 seenPuddles 只给看得见的、粗的)
    const near = () => (r.liquid?.seenPuddles?.() || []).filter((p) => Math.hypot(p.at[0] - mem.at[0], p.at[2] - mem.at[1]) < 0.45);
    const spot = new THREE.Vector3(mem.at[0], mem.y ?? 0, mem.at[1]);
    const at = standNear(r, spot, spot.y < 0.3 ? 0.45 : 0.42);
    if (at) await ctx.walk(at.pos, at.yaw);
    const here = near();
    let what = 'gone';
    if (here.length) {
      what = here.every((p) => p.stain) ? 'stain' : 'wiped';
      ctx.say(what === 'stain' ? '咖啡干了留下一块印子, 她用力擦了擦' : '她蹲下来擦干净');
      const c = new THREE.Vector3(here[0].at[0], here[0].at[1] + 0.03, here[0].at[2]), target = c.clone();
      if (c.y < 0.3) {
        r.body.begin('crouchLow');
        r.poses.set('crouch', { side, target, drop: 0.46, back: -0.08, lean: 24 });
      } else r.poses.set('hold', { side, target });
      ctx.later(() => /^(crouch|hold)$/.test(r.poses.name || '') && r.poses.set(null));
      await ctx.wait(1, { hurry: false });
      const stop = circle(A, target, c, 0.07, 1.1);
      await ctx.wait(ctx.fast ? 2.5 : 4, { hurry: false });
      stop();
      if (!here.map((p) => r.liquid.wipe(p.id)).some(Boolean)) what = 'missed';  // 站得太远擦不到 (液体那边按几何判)
      r.poses.set(null);
      await ctx.wait(0.6, { hurry: false });
      if (c.y < 0.3) r.body.end(true);
    } else ctx.say('走过来一看, 已经没了');
    // 擦掉了: 记得的那几摊忘掉 (一起报 forget); 走过去发现已经没了 (别人擦了、被容量上限删了): 不算她做了这件事, 只报 gone;
    // 站得太远擦不到: 还记着
    const mine = K.spills.filter((s) => Math.hypot(s.at[0] - mem.at[0], s.at[1] - mem.at[1]) < 0.45).map((s) => s.id);
    if (what !== 'missed') for (const id of mine) K.forgetSpill(id);
    if (what === 'gone') { out.void = true; A.gone.push(...mine); } else if (what !== 'missed') A.pend.forget = mine;
    out.felt = A.feel(outcomeOf(spec, { what }));
    await putBack(ctx, home);
    return { what, ctx: sit };
  },

  /** 抱抱小兔子: 拿起来 -> 坐到床边抱在腿上 -> 坐一会儿 -> 放回去. 想你了的时候抱着更安心. */
  async hug(A, ctx, spec, sit, out) {
    const r = A.r, node = A.nodes[spec.thing][0];
    ctx.say(`她${spec.going}`);
    const home = await take(A, ctx, node, A.spec.things[spec.thing].name);
    await ctx.sit(pickOne(A.spec.things[spec.thing].seats || ['bed_side']));
    // 一只手扶着它立在大腿上 (hold 的手目标每帧跟着腿面, 抬高半个兔子): 放在腿上捧着 (diaryHold) 时兔子 53 cm 高,
    // 插进骨盆和两条大腿, 防穿模层每帧推 2.2 次; 抱在胸前推开少一半, 手臂反而抖得更厉害 (逐帧诊断, 实验记录 E5)
    const it = r.layer.items[node], side = it.held || 'right', h = r.v.vrm.humanoid;
    const legs = ['leftUpperLeg', 'rightUpperLeg', 'leftLowerLeg', 'rightLowerLeg'].map((n) => h.getNormalizedBoneNode(n));
    const target = new THREE.Vector3(), p = [0, 1, 2, 3].map(() => new THREE.Vector3());
    const follow = () => {  // 腿面: 两条大腿中间往膝盖那边 0.55 (同 room-poses.js 的 lap), 再抬高半个东西
      legs.forEach((b, i) => b.getWorldPosition(p[i]));
      target.copy(p[0]).add(p[1]).multiplyScalar(0.5).lerp(p[2].clone().add(p[3]).multiplyScalar(0.5), 0.55);
      target.y += 0.1 * r.layer.hipHeight / 0.78 + it.half + 0.02;
    };
    follow();
    const off = r.props.every(follow);
    r.poses.set('hold', { side, target });
    ctx.later(() => { off(); if (r.poses.name === 'hold') r.poses.set(null); });
    r.v.setEmotion(sit === 'lonely' ? 'shy' : 'happy');
    ctx.say(sit === 'lonely' ? '她抱着小兔子, 好像在想你' : '她抱着小兔子坐了一会儿');
    await ctx.wait(ctx.fast ? 4 : rand(15, 30));
    out.felt = A.feel(outcomeOf(spec, { lonely: sit === 'lonely' }));
    r.poses.set(null);
    r.v.setEmotion('neutral');
    await ctx.wait(0.8, { hurry: false });
    await putBack(ctx, home);
    return {};
  },

  /** 四处看看: 去一处很久没去 (没去过) 的地方, 看看周围的东西. 发现了新东西才有意思 (信息量), 都看过了就没什么意思. */
  async explore(A, ctx, spec, sit, out) {
    const r = A.r, K = A.known, spots = A.spec.spots || {};
    const stale = K.stale(Object.keys(spots));
    const spot = pickOne(stale.length ? stale : Object.keys(spots));
    const news0 = K.news.length;
    ctx.say(`她${spec.going}`);
    await ctx.walk(spots[spot]);
    K.visit(spot);
    // 看看周围 3 米以内的几样东西 (挨个看过去)
    const me = new THREE.Vector3(r.layer.char.x, 1.2, r.layer.char.z);
    const around = Object.keys(A.spec.things).map((id) => [id, A.where(id)]).filter(([, p]) => p && Math.hypot(p[0] - me.x, p[2] - me.z) < 3)
      .sort(() => Math.random() - 0.5).slice(0, 3);
    for (const [id, p] of around) {
      (r.layer.targets.__afford ??= { id: '__afford', name: '东西' }).look = p;
      r.layer.lookAt('__afford', 2);
      await ctx.wait(ctx.fast ? 1 : rand(1.5, 2.5));
      if (A.spec.things[id].look) K_see(A);
    }
    await ctx.wait(ctx.fast ? 0.5 : 1.5);
    const found = K.news.length - news0;
    out.felt = A.feel(outcomeOf(spec, { found }));
    ctx.say(found ? `转了一圈, 发现了 ${found} 样以前没注意的` : '转了一圈, 都是熟悉的东西');
    return { spot, found };
  },
};

/** 她站在花旁边时看一眼土 (走近了才看得出). */
function K_see(A) {
  const p = A.where('plant'), L = A.r.layer;
  if (p && Math.hypot(p[0] - L.char.x, p[2] - L.char.z) < (A.spec.things.plant.look ?? 1.3)) A.known.see('plant', A.soil());
}
