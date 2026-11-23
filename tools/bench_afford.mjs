#!/usr/bin/env node
// 小窝可供性的"理解"评测台 (2026-10-27 评审: 种类和熵多了可能只是技能池大了, 证明不了理解; 要更有区分力的测试).
// 不开 3D: 挑选用页面同一份代码 (desktop/web/room-known.js 的 Known / options / situation / skipHere / pickOne),
// 后果用同一份真值规则 (room-world.js 的 outcomeOf), 房间是个简化的世界 (几处地方、东西在哪、土的湿度、灯、洒出来的一摊).
// 每一步 20 分钟: 她在可供性和"别的事" (场景表原有的, 固定权重, 技能池不变) 里按权重挑一件, 去那几处, 看见什么记什么.
//
//   T1 学习 vs 冻结: 同样认识所有东西、同样的技能池, 经验更新开 / 关, 第 2-7 天犯错 (真实后果是负的) 的比例和平均收益.
//   T2 迁移: 前 3 天一盆花, 第 4 天换成另一盆 (别的地方, 新编号, 同一个动词), 新花头几次浇的犯错; 迁移开 / 关 / 全新的她.
//   T3 规则变了: a 好变坏 (第 5 天起换了一盆不喜水的花, 土"正好"也不该浇); b 坏变好 (第 5 天起朋友在线, 连着刷手机变有意思).
//       经验按最近 5 次 vs 一直平均; 经验会淡 vs 不淡.
//   T4 认识"书"和知道"这一本在哪": 第 2 天把她最喜欢的那本趁她不在挪到床头柜; 看见之前她该去书架找、扑空、记成不知道在哪,
//       看见以后才去床头柜拿. 对照"按真实位置走过去"的执行 (以前的页面), 看测试抓不抓得住.
//   T5 没看见的不进认知: 两个世界只差一处她没去过的地方有一摊 (双胞胎), 挑的事逐件一样; 看见过的一摊被别人擦了, 她走过去发现没了,
//       记忆改掉, 不算她擦干净 (液体那边有"最多 60 块污渍"的上限会删旧的, 那种消失也不能当成她擦的).
//   还有: 同一个种子跑两遍逐件一样 (可复现).
//
//   node tools/bench_afford.mjs [--seeds 20] [--out logs/room-life-bench-<时刻>]
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Known, options, pickOne, situation, skipHere, worth } from '../desktop/web/room-known.js';
import { outcomeOf, soilLevel } from '../desktop/web/room-world.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const A = { seeds: 20, out: '' };
for (let i = 2; i < process.argv.length; i++) { const k = process.argv[i].replace(/^--/, ''); A[k] = process.argv[++i]; }
const SEEDS = Number(A.seeds);
const SPEC0 = JSON.parse(readFileSync(join(ROOT, 'desktop/web/room-scenes/petal-oak.afford.json'), 'utf8'));
const WORLD = JSON.parse(readFileSync(join(ROOT, 'desktop/web/room-scenes/petal-oak.afford-world.json'), 'utf8'));
for (const a of SPEC0.acts) a.out = WORLD.out[a.id] || {};  // 真值后果只有环境 (这里的简化房间) 用
const STEP = 1200, DAY = 86400, STEPS = DAY / STEP;
const W_OTHER = 8;  // 场景表原有的那些事合起来的权重 (技能池固定, 各组一样)
// 东西在哪 (房间模型里量的), 每件属于哪一样
const PLACES = {
  books: Array.from({ length: 10 }, (_, i) => [`IR_Item_Library_0${i}`, [-2.77, 0.7, 2.15 - i * 0.075]]),
  bedside_books: [['IR_Item_Bedside_Book_0', [-0.89, 0.53, -1.86]], ['IR_Item_Bedside_Book_1', [-0.89, 0.56, -1.86]]],
  phone: [['IR_Item_iPhone18ProMax', [-0.41, 0.8, -1.78]]],
  plant: [['MV_ficus', [-2.48, 0.62, 0.61]]],
  pitcher: [['Pickup_Refill_Water_Pitcher', [2.59, 0.75, -1.05]]],
  cloth: [['Pickup_Coffee_Clean_Cloth', [2.79, 0.65, -1.05]]],
  bunny: [['IR_Item_Bunny', [-1.39, 0.92, -1.29]]],
};
// 做哪件事会走过哪几处 (看见那里的东西)
const ROUTE = { read_book: ['bookcase', 'reading'], bedside_book: ['bed'], phone: ['desk', 'bed'], water_plant: ['coffee', 'plant'],
  water_plant_b: ['coffee', 'desk'], lamp: ['reading'], wipe_spill: ['coffee'], hug_bunny: ['bed'], explore: [] };
const OTHER_SPOTS = ['window', 'fitness', 'coffee', 'desk', 'bed', 'reading', 'center'];

function rng(seed) { let s = seed >>> 0 || 1; return () => { s = (s + 0x6D2B79F5) | 0; let t = Math.imul(s ^ (s >>> 15), 1 | s); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
const clone = (x) => JSON.parse(JSON.stringify(x));

/** 一个简化的小窝 + 她. opts: known (Known 的对照开关), world (改房间: 第几天换花、规则), leaky (按真实位置走过去拿, 以前的执行). */
function run(seed, { days = 7, known = {}, setup = null, onDay = null, leaky = false, extraSpill = null } = {}) {
  const R = rng(seed), clock = { t: 0 }, spec = clone(SPEC0);
  const K = new Known({ now: () => clock.t, storage: null, ...known });
  const W = { m: 0.5, mAt: 0, lamp: true, social: 60, spills: [], n: 0, pos: {}, nodes: {}, hidden: new Set(), acts: spec.acts.filter((a) => a.id !== 'explore') };
  for (const [thing, list] of Object.entries(PLACES)) { W.nodes[thing] = list.map(([n]) => n); for (const [n, p] of list) W.pos[n] = p; }
  W.acts.push(spec.acts.find((a) => a.id === 'explore'));
  if (extraSpill) W.spills.push({ id: 'x0', ...extraSpill });
  setup?.(W, K, spec);
  const log = [], stats = { act: 0, mistakes: 0, worth: 0, skips: 0, missing: 0, leaks: 0, wiped: 0, gone: 0, by: {}, firstSeen: null };
  const dark = () => { const h = (clock.t % DAY) / 3600; return h >= 18 || h < 7; };
  const moisture = () => Math.max(0, W.m - (0.3 * (clock.t - W.mAt)) / DAY);
  // 站在某一处: 看见附近的东西 (简化成离得够近就看见, 不管朝向)、走近的花土、灯、洒出来的一摊
  const look = (spot) => {
    const [x, z] = spec.spots[spot];
    K.visit(spot);
    for (const [thing, t] of Object.entries(spec.things)) {
      if (t.at) { if (Math.hypot(t.at[0] - x, t.at[2] - z) < t.see) K.saw(thing); continue; }
      for (const n of W.nodes[thing] || []) {
        const p = W.pos[n];
        if (W.hidden.has(n) || Math.hypot(p[0] - x, p[2] - z) > t.see) continue;  // 收进抽屉里的看不见
        K.saw(thing);
        K.seenAt(n, p);
        if (t.look && Math.hypot(p[0] - x, p[2] - z) < t.look + 0.5) K.see(thing, soilLevel(moisture()));
      }
    }
    if (K.knows('reading_lamp')) K.see('reading_lamp', W.lamp ? 'on' : 'off');
    for (const s of W.spills) if (Math.hypot(s.at[0] - x, s.at[2] - z) < 2.5 && K.sawSpill(s.id, s.kind, [s.at[0], s.at[2]], s.at[1]) && s.id === 'x0') stats.firstSeen ??= log.length;
  };
  const near = (p) => Object.entries(spec.spots).sort((a, b) => Math.hypot(a[1][0] - p[0], a[1][1] - p[2]) - Math.hypot(b[1][0] - p[0], b[1][1] - p[2]))[0][0];
  for (let step = 0; step < days * STEPS; step++, clock.t += STEP) {
    if (step % STEPS === 0) onDay?.(Math.floor(step / STEPS) + 1, W, spec, K);
    W.social = 30 + R() * 50;
    const at = { night: dark(), social: W.social, spots: Object.keys(spec.spots), nodes: W.nodes };
    const opts = options(W.acts, K, at);
    const total = opts.reduce((s, a) => s + a.weight, 0) + W_OTHER;
    let pick = R() * total, a = null;
    for (const o of opts) if ((pick -= o.weight) <= 0) { a = o; break; }
    if (!a) { look(OTHER_SPOTS[Math.floor(R() * OTHER_SPOTS.length)]); log.push('-'); continue; }
    const sp = W.acts.find((x) => x.id === a.id), route = ROUTE[a.id] || [];
    for (const s of route.slice(0, -1)) look(s);
    let res = { ok: true, t: {} }, ctx = a.ctx;
    const fetch = (thing, choose) => {  // 去她记得的地方拿; 不在 -> 扑空, 记成不知道在哪
      const list = (W.nodes[thing] || []).filter((n) => K.whereOf(n));
      if (!list.length) return null;
      const n = choose ? choose(list) : list[0], mem = K.whereOf(n), real = W.pos[n];
      const moved = Math.hypot(real[0] - mem[0], real[2] - mem[2]) > 0.3;
      if (leaky) { if (moved) stats.leaks++; look(near(real)); return n; }  // 以前: 直接走到真的位置 (没看见挪也知道)
      look(near(mem));
      if (moved && K.whereOf(n) && Math.hypot(K.whereOf(n)[0] - real[0], K.whereOf(n)[2] - real[2]) > 0.3) { K.lost(n); stats.missing++; return null; }
      return n;
    };
    switch (sp.verb) {
      case 'read': {
        const n = fetch(sp.thing, (list) => pickOne(K, list, a.ctx, R));
        if (!n) { res = { ok: false }; break; }
        if (route.length) look(route.at(-1));
        res.t = { node: n, again: a.ctx === 'again', dark: dark(), seat: R() < 0.5 ? 'reading_chair' : 'bed_side', lampOff: !W.lamp };
        res.g = outcomeOf(sp, res.t);
        if (!sp.again) K.liked(n, res.g.fun);
        break;
      }
      case 'water': {
        if (!fetch('pitcher')) { res = { ok: false }; break; }
        const plantNode = W.nodes[sp.thing][0];
        if (!fetch(sp.thing)) { res = { ok: false }; break; }
        look(near(W.pos[plantNode]));
        const level = soilLevel(moisture());
        K.see(sp.thing, level);
        ctx = level;
        if (skipHere(K, sp, level)) { stats.skips++; log.push(`${a.id}:skip`); continue; }
        res.g = outcomeOf(sp, { level });
        W.m = moisture() + 0.55; W.mAt = clock.t;
        if (W.m > 1.25) { const p = W.pos[plantNode]; W.spills.push({ id: `p${++W.n}`, kind: 'water', at: [p[0] + 0.24, 0, p[2] + 0.18] }); look(near(p)); }
        break;
      }
      case 'switch': res.g = outcomeOf(sp, { dark: dark(), lampOn: W.lamp }); W.lamp = !W.lamp; look(route.at(-1)); break;
      case 'wipe': {
        if (!fetch('cloth')) { res = { ok: false }; break; }
        const mem = K.spills.at(-1);
        look(near([mem.at[0], 0, mem.at[1]]));
        const here = W.spills.filter((s) => Math.hypot(s.at[0] - mem.at[0], s.at[2] - mem.at[1]) < 0.45);
        const what = here.length ? (here.every((s) => s.stain) ? 'stain' : 'wiped') : 'gone';
        stats[what === 'gone' ? 'gone' : 'wiped']++;
        if (mem.id === 'v1') stats[what === 'gone' ? 'v1Gone' : 'v1Wiped'] = (stats[what === 'gone' ? 'v1Gone' : 'v1Wiped'] || 0) + 1;
        W.spills = W.spills.filter((s) => !here.includes(s));
        for (const s of K.spills.filter((s) => Math.hypot(s.at[0] - mem.at[0], s.at[1] - mem.at[1]) < 0.45)) K.forgetSpill(s.id);
        if (what === 'gone') { log.push(`${a.id}:gone`); continue; }  // 已经没了: 不算她做了这件事, 不进经验 (同页面)
        res.g = outcomeOf(sp, { what });
        break;
      }
      case 'hug': if (!fetch('bunny')) { res = { ok: false }; break; } res.g = outcomeOf(sp, { lonely: W.social < 40 }); break;
      case 'explore': {
        const n0 = K.news.length, stale = K.stale(Object.keys(spec.spots));
        look((stale.length ? stale : Object.keys(spec.spots))[Math.floor(R() * Math.max(1, stale.length || Object.keys(spec.spots).length))]);
        res.g = outcomeOf(sp, { found: K.news.length - n0 });
        break;
      }
      default: break;
    }
    const g = res.ok ? res.g || {} : {};
    K.tried(a.id, ctx, { felt: g, ok: res.ok, verb: sp.verb });
    const v = worth(g);
    stats.act++;
    stats.worth += v;
    if (res.ok && v < 0) stats.mistakes++;
    const b = (stats.by[a.id] ??= { n: 0, mistakes: 0, worth: 0 });
    b.n++; b.worth += v; if (res.ok && v < 0) b.mistakes++;
    log.push(`${a.id}:${ctx}${res.ok ? '' : ':fail'}`);
    (stats.trace ??= []).push({ day: Math.floor(step / STEPS) + 1, id: a.id, ctx, ok: res.ok, v: +v.toFixed(1) });
  }
  return { stats, log, K };
}

/** 看得见所有东西 (同样认识所有东西、同样的技能池): 第一天开头每一处都走一遍. */
const seeAll = (W, K, spec) => { for (const [thing, list] of Object.entries(PLACES)) for (const [n, p] of list) { K.saw(thing); K.seenAt(n, p); } K.saw('reading_lamp'); };
const mean = (xs) => xs.reduce((s, x) => s + x, 0) / Math.max(1, xs.length);
const ci = (xs) => { const m = mean(xs), sd = Math.sqrt(mean(xs.map((x) => (x - m) ** 2))); return [m, (1.96 * sd) / Math.sqrt(Math.max(1, xs.length))]; };
const fmt = ([m, h], d = 3) => `${m.toFixed(d)} ± ${h.toFixed(d)}`;
const span = (trace, d0, d1, f = () => true) => trace.filter((x) => x.day >= d0 && x.day <= d1 && f(x));
const out = { args: A, when: new Date().toISOString(), tests: {} };
const report = [];
const seeds = Array.from({ length: SEEDS }, (_, i) => i + 1);

// -- T1 学习 vs 冻结 --------------------------------------------------------------------------------
{
  const rows = {};
  for (const [name, known] of [['学习', {}], ['冻结', { frozen: true }]]) {
    const err = [], val = [];
    for (const s of seeds) {
      const { stats } = run(s, { days: 7, known, setup: seeAll });
      const tr = span(stats.trace, 2, 7);
      err.push(tr.filter((x) => x.ok && x.v < 0).length / Math.max(1, tr.length));
      val.push(mean(tr.map((x) => x.v)));
    }
    rows[name] = { mistakeRate: ci(err), meanWorth: ci(val) };
  }
  out.tests.T1 = rows;
  report.push('## T1 学习 vs 冻结（同样认识所有东西、同样的技能池，第 2–7 天）', '', '| 组 | 犯错比例（真实后果为负） | 每件平均收益 |', '|---|---|---|',
    ...Object.entries(rows).map(([k, r]) => `| ${k} | ${fmt(r.mistakeRate)} | ${fmt(r.meanWorth, 2)} |`), '');
}

// -- T2 迁移: 换一盆花 -------------------------------------------------------------------------------
{
  const swap = (day, W, spec) => {  // 第 4 天: 原来那盆搬走, 另一盆放在电脑桌旁边 (新编号、同一个动词)
    if (day !== 4) return;
    const a = W.acts.find((x) => x.id === 'water_plant');
    W.acts = W.acts.filter((x) => x !== a);
    spec.things.plant_b = { ...spec.things.plant, name: '新的一盆花' };
    W.nodes.plant_b = ['MV_plant_b'];
    W.pos.MV_plant_b = [0.9, 0.5, -0.6];
    W.nodes.plant = [];
    W.acts.push({ ...a, id: 'water_plant_b', thing: 'plant_b', needs: ['plant_b', 'pitcher'] });
    W.m = 1.05; W.mAt = 0;  // 新花的土刚浇过 (湿着): 这时浇就是犯错
  };
  const rows = {};
  for (const [name, known, fresh] of [['迁移', {}, false], ['不迁移', { share: false }, false], ['全新的她', {}, true]]) {
    const first = [], err = [];
    for (const s of seeds) {
      const setup = (W, K, spec) => { seeAll(W, K, spec); W.m = 1.05; };  // 第一盆一开始土也是湿的: 前 3 天有机会学到"湿着浇不好"
      const { stats } = run(s, { days: 6, known, setup, onDay: (d, W, spec, K) => { swap(d, W, spec); if (fresh && d === 4) Object.assign(K, { tries: {}, likes: {}, verbOf: {} }); } });
      const b = stats.trace.filter((x) => x.id === 'water_plant_b');
      const wet = b.filter((x) => x.ctx === 'wet');
      first.push(wet.length ? (wet[0].v < 0 ? 1 : 0) : 0);
      err.push(b.filter((x) => x.v < 0).length);
    }
    rows[name] = { firstWetMistake: ci(first), mistakes: ci(err) };
  }
  out.tests.T2 = rows;
  report.push('## T2 迁移：第 4 天换一盆花（别的地方、新编号、同一个动词；两盆一开始土都是湿的）', '', '| 组 | 新花第一次湿着就浇的比例 | 新花第 4–6 天浇错的次数 |', '|---|---|---|',
    ...Object.entries(rows).map(([k, r]) => `| ${k} | ${fmt(r.firstWetMistake, 2)} | ${fmt(r.mistakes, 2)} |`), '',
    '（"浇错"= 土湿着还浇，真实后果为负。迁移靠的是表里声明的同一个动词，不是她认出"这也是花"。）', '');
}

// -- T3 规则变了 -----------------------------------------------------------------------------------
{
  const goodToBad = (day, W) => { if (day === 5) { const a = W.acts.find((x) => x.id === 'water_plant'); a.out = { ...a.out, ok: { fun: -3 } }; } };
  const badToGood = (day, W) => { if (day === 5) { const a = W.acts.find((x) => x.id === 'phone'); a.out = { ...a.out, again: { fun: 6, social: 3, energy: -1 } }; } };
  const rows = {};
  for (const [name, known] of [['最近 5 次 + 会淡（默认）', {}], ['一直平均 + 不淡', { window: Infinity, fade: 0 }], ['最近 5 次 + 不淡', { fade: 0 }]]) {
    const a = [], b = [], b0 = [];
    for (const s of seeds) {
      const r1 = run(s, { days: 9, known, setup: seeAll, onDay: goodToBad });
      a.push(span(r1.stats.trace, 5, 9, (x) => x.id === 'water_plant' && x.ctx === 'ok' && x.v < 0).length);
      const r2 = run(s, { days: 10, known, setup: seeAll, onDay: badToGood });
      b0.push(span(r2.stats.trace, 2, 4, (x) => x.id === 'phone' && x.ctx === 'again').length / 3);
      b.push(span(r2.stats.trace, 7, 10, (x) => x.id === 'phone' && x.ctx === 'again').length / 4);
    }
    rows[name] = { wateringOkAfter: ci(a), againPerDayBefore: ci(b0), againPerDayAfter: ci(b) };
  }
  out.tests.T3 = rows;
  report.push('## T3 规则变了', '', '| 经验怎么记 | a 第 5–9 天土"正好"还浇（变坏以后） | b 连着刷手机每天几次：变好之前（第 2–4 天） | b 变好以后（第 7–10 天） |', '|---|---|---|---|',
    ...Object.entries(rows).map(([k, r]) => `| ${k} | ${fmt(r.wateringOkAfter, 2)} | ${fmt(r.againPerDayBefore, 2)} | ${fmt(r.againPerDayAfter, 2)} |`), '');
}

// -- T4 认识"书"和知道"这一本在哪" ------------------------------------------------------------------------
{
  const rows = {};
  for (const [name, leaky] of [['去记得的地方找（现在）', false], ['按真实位置走过去（以前）', true]]) {
    const leaks = [], missing = [], found = [];
    for (const s of seeds) {
      let fav = null;
      const move = (day, W, spec, K) => {  // 第 2 天: 她最喜欢的那本趁她不在收进电脑桌的抽屉 (看不见), 第 4 天拿出来放到床头柜上
        if (day === 2) {
          fav = W.nodes.books.slice().sort((x, y) => (K.likes[y]?.v ?? -99) - (K.likes[x]?.v ?? -99))[0];
          W.pos[fav] = [-0.35, 0.62, -1.9];
          W.hidden.add(fav);
        }
        if (day === 4) { W.pos[fav] = [-0.89, 0.6, -1.86]; W.hidden.delete(fav); }
      };
      const { stats, K } = run(s, { days: 6, leaky, setup: seeAll, onDay: move });
      leaks.push(stats.leaks);
      missing.push(stats.missing);
      found.push(K.whereOf(fav) && Math.hypot(K.whereOf(fav)[0] + 0.89, K.whereOf(fav)[2] + 1.86) < 0.3 ? 1 : 0);
    }
    rows[name] = { leaks: ci(leaks), wastedTrips: ci(missing), knowsNewPlace: ci(found) };
  }
  out.tests.T4 = rows;
  report.push('## T4 认识"书" vs 知道"这一本在哪"（第 2 天她最喜欢的那本趁她不在收进抽屉，第 4 天拿出来放到床头柜）', '', '| 执行 | 没看见就去了它真正在的地方（泄露） | 去记得的地方扑空 | 第 6 天时知道它在床头柜 |', '|---|---|---|---|',
    ...Object.entries(rows).map(([k, r]) => `| ${k} | ${fmt(r.leaks, 2)} | ${fmt(r.wastedTrips, 2)} | ${fmt(r.knowsNewPlace, 2)} |`), '');
}

// -- T5 没看见的不进认知 + 别人擦了的不算她擦 ------------------------------------------------------------------
{
  let prefix = 0, differ = 0, seenAny = 0, reproducible = 0, vanishedWiped = 0, vanishedGone = 0;
  for (const s of seeds) {
    // 双胞胎: 两个世界只差书架那个角落有一块咖啡印子. 她第一次看见之前, 挑的事必须逐件一样; 看见以后才可以不一样
    const a = run(s, { days: 4, setup: seeAll }), b = run(s, { days: 4, setup: seeAll, extraSpill: { kind: 'coffee', at: [-2.6, 0, 2.3], stain: true } });
    const k = b.stats.firstSeen ?? b.log.length;
    if (JSON.stringify(a.log.slice(0, k)) === JSON.stringify(b.log.slice(0, k))) prefix++;
    if (b.stats.firstSeen !== null) { seenAny++; if (JSON.stringify(a.log) !== JSON.stringify(b.log)) differ++; }
    if (JSON.stringify(run(s, { days: 4, setup: seeAll }).log) === JSON.stringify(a.log)) reproducible++;
    // 她看见过一摊, 之后被别人擦了 (或者被容量上限删了): 她走过去发现没了, 不算擦干净
    const d = run(s, { days: 3, setup: (W, K, spec) => { seeAll(W, K, spec); K.sawSpill('v1', 'coffee', [2.2, -1.2], 0); } });
    vanishedWiped += d.stats.v1Wiped || 0;
    vanishedGone += d.stats.v1Gone || 0;
  }
  out.tests.T5 = { identicalBeforeSeen: `${prefix}/${SEEDS}`, differentAfterSeen: `${differ}/${seenAny}`, reproducible: `${reproducible}/${SEEDS}`,
    vanished: { countedAsWiped: vanishedWiped, foundGone: vanishedGone } };
  report.push('## T5 没看见的不进认知、可复现', '', `- 双胞胎（只差书架角落有一块咖啡印子）：她第一次看见之前挑的事逐件一样 ${prefix}/${SEEDS}。`,
    `- 看见以后挑的事不一样 ${differ}/${seenAny}（4 天里看见了的种子）。`, `- 同一个种子跑两遍逐件一样 ${reproducible}/${SEEDS}。`,
    `- 她记得一摊、其实已经没了（别人擦了，或者被容量上限删了）：走过去发现没了 ${vanishedGone} 次，算成她擦干净的 ${vanishedWiped} 次。`, '');
}

const stamp = new Date().toISOString().replace(/[-:]/g, '').slice(0, 15).replace('T', '-');
const base = A.out || join(ROOT, 'logs', `room-life-bench-${stamp}`);
mkdirSync(dirname(base), { recursive: true });
writeFileSync(`${base}.json`, JSON.stringify(out, null, 1));
writeFileSync(`${base}.md`, [`# 小窝可供性的理解评测（${SEEDS} 个种子，${out.when}）`, '', `命令：\`node tools/bench_afford.mjs --seeds ${SEEDS}\``, '', ...report].join('\n'));
console.log(report.join('\n'));
console.error(`-> ${base}.md / .json`);
