import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { curious, forPlan, Known, options, pickOne, sees, situation, skipHere, worth } from '../../desktop/web/room-known.js';
import { outcomeOf, soilLevel } from '../../desktop/web/room-world.js';

const SPEC = JSON.parse(readFileSync(new URL('../../desktop/web/room-scenes/petal-oak.afford.json', import.meta.url), 'utf8'));
const WORLD = JSON.parse(readFileSync(new URL('../../desktop/web/room-scenes/petal-oak.afford-world.json', import.meta.url), 'utf8'));
const withOut = (a) => ({ ...a, out: WORLD.out[a.id] });  // 真值后果 (环境用; 挑选那一层不读)
const BOOKS = Array.from({ length: 4 }, (_, i) => `IR_Item_Library_0${i}`);
const AT = { night: false, social: 70, spots: Object.keys(SPEC.spots), nodes: { books: BOOKS, bedside_books: ['b0'], phone: ['ph'] } };
const clock = (t0 = 1e6) => { const c = { t: t0 }; c.now = () => c.t; return c; };
const mind = (c = clock()) => new Known({ now: c.now, storage: null });
// 看见了这一样东西, 也看见了它的每一件在哪 (书架上的几本书)
const look = (K, thing) => { K.saw(thing); (AT.nodes[thing] || []).forEach((n, i) => K.seenAt(n, [i, 0.7, 0])); };

test('看不看得见: 离得够近、在前方 70 度以内; 贴着身边不看方向', () => {
  const me = { x: 0, z: 0, yaw: 0 };  // 脸朝 +z
  assert.equal(sees(me, [0, 1, 1.5], 2), true);
  assert.equal(sees(me, [0, 1, 2.5], 2), false);   // 太远
  assert.equal(sees(me, [0, 1, -1.5], 2), false);  // 在背后
  assert.equal(sees(me, [1.5, 1, 0.2], 2), false); // 在正侧面 (80 多度)
  assert.equal(sees(me, [0, 1, -0.5], 2), true);   // 背后但就在身边
});

test('没见过的东西想不到; 见过才进候选', () => {
  const K = mind();
  assert.deepEqual(options(SPEC.acts, K, AT).map((a) => a.id), ['explore']);  // 一开始只会四处看看
  assert.equal(K.saw('books'), true);
  assert.equal(K.saw('books'), false);  // 第二次不算新发现
  assert.deepEqual(options(SPEC.acts, K, AT).map((a) => a.id), ['explore']);  // 认识"书", 还不知道哪一本在哪
  look(K, 'books');
  assert.deepEqual(options(SPEC.acts, K, AT).map((a) => a.id), ['read_book', 'explore']);
  K.saw('plant');
  assert.ok(!options(SPEC.acts, K, AT).some((a) => a.id === 'water_plant'));  // 还要认识水壶
  K.saw('pitcher');
  assert.equal(options(SPEC.acts, K, AT).find((a) => a.id === 'water_plant').ctx, 'unknown');  // 没走近看过土
});

test('双胞胎: 只差她没看到的 -> 候选和权重一样; 看到了 -> 不一样', () => {
  const twins = () => { const K = mind(); for (const t of ['books', 'plant', 'pitcher', 'reading_lamp', 'cloth', 'bunny']) look(K, t); return K; };
  const a = twins(), b = twins();
  // 真值不同 (a 的花土干透了, b 的湿着; a 房间地上洒了一摊她没看见): options 不读这些, 只读她的了解
  const ra = options(SPEC.acts, a, AT), rb = options(SPEC.acts, b, AT);
  assert.deepEqual(ra, rb);
  a.see('plant', 'dry');
  b.see('plant', 'wet');
  const wa = options(SPEC.acts, a, AT).find((x) => x.id === 'water_plant'), wb = options(SPEC.acts, b, AT).find((x) => x.id === 'water_plant');
  assert.equal(wa.ctx, 'dry');
  assert.equal(wb.ctx, 'wet');
  a.sawSpill('p1', 'coffee', [0.4, 1.2]);
  assert.ok(options(SPEC.acts, a, AT).some((x) => x.id === 'wipe_spill'));
  assert.ok(!options(SPEC.acts, b, AT).some((x) => x.id === 'wipe_spill'));
});

test('经验: 试过的按她感到的算, 不同情境分开记; 好奇心随次数下降', () => {
  const c = clock(), K = mind(c);
  K.saw('plant'); K.saw('pitcher');
  const prior = SPEC.acts.find((a) => a.id === 'water_plant').prior;
  assert.deepEqual(K.expect('water_plant', 'wet', prior).gives, prior);
  K.tried('water_plant', 'wet', { felt: { fun: -3 } });  // 湿着浇: 渗出来, 不舒服
  K.tried('water_plant', 'dry', { felt: { fun: 9 } });
  assert.ok(worth(K.expect('water_plant', 'wet', prior).gives) < 0);
  assert.ok(worth(K.expect('water_plant', 'dry', prior).gives) > 6);
  K.see('plant', 'wet');
  const wet = options(SPEC.acts, K, AT).find((a) => a.id === 'water_plant');
  K.see('plant', 'dry');
  const dry = options(SPEC.acts, K, AT).find((a) => a.id === 'water_plant');
  assert.ok(dry.weight > wet.weight * 3, `${dry.weight} vs ${wet.weight}`);
  assert.ok(curious(0) > curious(1) && curious(1) > curious(5) && curious(5) > 1);
  // 没做成 (-2)、疼了 (-2): 记成负的; 先验 (0) 按半次算进去: -4 × 1 / 1.5
  K.tried('lamp', 'day-on', { ok: false, hurt: 2 });
  assert.equal(K.expect('lamp', 'day-on', {}).gives.fun, -2.7);
  // 看到的粗档太久没看就不算数了
  c.t += 13 * 3600;
  assert.equal(K.level('plant'), 'unknown');
});

test('情境: 书 (喜欢的 / 没看过的 / 都一般)、手机连着刷、灯、抱兔子、很久没去的地方', () => {
  const c = clock(), K = mind(c), read = SPEC.acts.find((a) => a.id === 'read_book');
  assert.equal(situation(read, K, AT), null);  // 一本都不知道在哪
  look(K, 'books'); look(K, 'phone');
  assert.equal(situation(read, K, AT), 'new');
  for (const b of BOOKS) K.liked(b, 2);
  assert.equal(situation(read, K, AT), 'meh');
  K.liked(BOOKS[1], 12); K.liked(BOOKS[1], 12);
  assert.equal(situation(read, K, AT), 'liked');
  const phone = SPEC.acts.find((a) => a.id === 'phone');
  assert.equal(situation(phone, K, AT), 'fresh');
  K.tried('phone', 'fresh', { felt: { fun: 7 } });
  assert.equal(situation(phone, K, AT), 'again');
  c.t += 2 * 3600;
  assert.equal(situation(phone, K, AT), 'fresh');
  const lamp = SPEC.acts.find((a) => a.id === 'lamp');
  assert.equal(situation(lamp, K, { ...AT, night: true }), 'night-on');
  K.see('reading_lamp', 'off');
  assert.equal(situation(lamp, K, { ...AT, night: true }), 'night-off');
  const hug = SPEC.acts.find((a) => a.id === 'hug_bunny');
  assert.equal(situation(hug, K, { ...AT, social: 30 }), 'lonely');
  const explore = SPEC.acts.find((a) => a.id === 'explore');
  assert.equal(situation(explore, K, AT), 'stale');
  for (const s of AT.spots) K.visit(s);
  assert.equal(situation(explore, K, AT), 'recent');
});

test('上行服务端的只有编号和数字', () => {
  const K = mind();
  look(K, 'books');
  const list = [{ id: 'yoga', weight: 1.6 }, ...options(SPEC.acts, K, AT)];
  const up = forPlan(list);
  assert.deepEqual(up.map((x) => x.act), ['read_book', 'explore']);
  for (const x of up) {
    assert.equal(typeof x.n, 'number');
    for (const [k, v] of Object.entries(x.expect)) { assert.match(k, /^(energy|food|water|fun|social)$/); assert.equal(typeof v, 'number'); }
  }
  // 看过一本喜欢的书以后情境换成 liked (这个情境 0 次), 上行说的是一共试过几次, 不是"还没试过"
  K.tried('read_book', 'new', { felt: { fun: 9 } });
  K.liked(BOOKS[0], 9);
  const again = forPlan(options(SPEC.acts, K, AT)).find((x) => x.act === 'read_book');
  assert.equal(options(SPEC.acts, K, AT).find((a) => a.id === 'read_book').ctx, 'liked');
  assert.equal(again.n, 1);
});

test('够不着: 在哪一层失败过, 那一层和更高的以后不去拿', () => {
  const K = mind();
  assert.equal(K.reachable(1.48), true);
  K.tooHigh(1.48);
  assert.equal(K.reachable(1.48), false);
  assert.equal(K.reachable(1.04), true);
  K.tooHigh(1.04);
  K.tooHigh(1.48);  // 更高的失败不会把记住的往上挪
  assert.equal(K.high, 1.04);
  assert.equal(K.reachable(0.6), true);
});

test('存下来、再打开还记得', () => {
  const mem = new Map(), storage = { getItem: (k) => mem.get(k) ?? null, setItem: (k, v) => mem.set(k, v) };
  const c = clock(), K = new Known({ now: c.now, storage });
  K.saw('bunny');
  K.tried('hug_bunny', 'fine', { felt: { social: 4, fun: 2 } });
  const K2 = new Known({ now: c.now, storage });
  assert.ok(K2.knows('bunny'));
  assert.equal(K2.expect('hug_bunny', 'fine', {}).n, 1);
});

test('认识"书"和知道"这一本在哪"分开: 看见才记位置, 扑空就忘掉, 不知道在哪的那件不进候选', () => {
  const K = mind();
  K.saw('books');
  assert.equal(K.whereOf(BOOKS[0]), null);
  K.seenAt(BOOKS[0], [-2.77, 0.72, 2.15]);
  assert.deepEqual(K.whereOf(BOOKS[0]), [-2.8, 0.7, 2.2]);  // 粗到 0.1 m
  assert.equal(situation(SPEC.acts.find((a) => a.id === 'read_book'), K, AT), 'new');
  K.lost(BOOKS[0]);
  assert.equal(K.whereOf(BOOKS[0]), null);
  assert.equal(situation(SPEC.acts.find((a) => a.id === 'read_book'), K, AT), null);
  // 挑哪一本: 只在给的 (知道在哪的) 里挑; 有喜欢的七成挑喜欢的
  K.liked(BOOKS[1], 10);
  let liked = 0;
  for (let i = 0; i < 200; i++) if (pickOne(K, [BOOKS[1], BOOKS[2]], 'liked', () => (i % 10) / 10) === BOOKS[1]) liked++;
  assert.ok(liked >= 140, `${liked}`);
});

test('同一个动词的经验能迁移 (换了一盆花), 最多算 2 次, 自己试过以后按自己的', () => {
  const K = mind(), water = SPEC.acts.find((a) => a.id === 'water_plant'), b = { ...water, id: 'water_plant_b', thing: 'plant_b' };
  K.tried('water_plant', 'wet', { felt: { fun: -3 }, verb: 'water' });
  K.tried('water_plant', 'wet', { felt: { fun: -3 }, verb: 'water' });
  const e = K.expect('water_plant_b', 'wet', water.prior, 'water');
  assert.equal(e.n, 0);
  assert.equal(e.shared, 2);
  assert.ok(worth(e.gives) < 0);
  assert.equal(skipHere(K, b, 'wet'), true);       // 走到新花跟前看到土湿, 按迁移来的经验不浇
  assert.equal(skipHere(K, b, 'dry'), false);      // 干的情境没有经验
  const off = new Known({ now: () => 1e6, storage: null, share: false });
  off.tried('water_plant', 'wet', { felt: { fun: -3 }, verb: 'water' });
  assert.equal(skipHere(off, b, 'wet'), false);    // 不迁移: 新花得自己再错一次
  K.tried('water_plant_b', 'wet', { felt: { fun: 4 }, verb: 'water' });  // 这盆喜欢湿: 自己的经验占上风
  K.tried('water_plant_b', 'wet', { felt: { fun: 4 }, verb: 'water' });
  K.tried('water_plant_b', 'wet', { felt: { fun: 4 }, verb: 'water' });
  assert.ok(worth(K.expect('water_plant_b', 'wet', water.prior, 'water').gives) > 0);
});

test('很久没试的经验会淡: 期望往先验回、好奇心回来; 冻住的不记', () => {
  const c = clock(), K = mind(c), lamp = SPEC.acts.find((a) => a.id === 'lamp');
  K.tried('lamp', 'night-on', { felt: { fun: -2 }, verb: 'switch' });
  const now = K.expect('lamp', 'night-on', lamp.prior);
  c.t += 8 * 24 * 3600;  // 8 天没再试
  const later = K.expect('lamp', 'night-on', lamp.prior);
  assert.ok(later.weight < now.weight / 5, `${later.weight} vs ${now.weight}`);
  assert.ok(later.gives.fun > now.gives.fun);
  const F = new Known({ now: c.now, storage: null, frozen: true });
  F.tried('lamp', 'night-on', { felt: { fun: -2 } });
  assert.equal(F.expect('lamp', 'night-on', lamp.prior).n, 0);
});

test('真值后果 (room-world.js): 书按好不好看、土湿着浇是负的、天黑了关灯不舒服', () => {
  const [read, water, lamp] = ['read_book', 'water_plant', 'lamp'].map((id) => withOut(SPEC.acts.find((a) => a.id === id)));
  const g = outcomeOf(read, { node: BOOKS[0] });
  assert.ok(g.fun >= 3 && g.fun <= 12);
  assert.equal(outcomeOf(read, { node: BOOKS[0], dark: true, seat: 'reading_chair', lampOff: true }).fun, g.fun - 4);
  assert.ok(outcomeOf(water, { level: 'wet' }).fun < 0 && outcomeOf(water, { level: 'dry' }).fun > outcomeOf(water, { level: 'ok' }).fun);
  assert.ok(outcomeOf(lamp, { dark: true, lampOn: true }).fun < 0 && outcomeOf(lamp, { dark: true, lampOn: false }).fun > 0);
  assert.equal(soilLevel(0.2), 'dry');
  assert.equal(soilLevel(1.1), 'wet');
});

test('公开的可供性表不带真值后果 (out 在 afford-world 那份)', () => {
  assert.ok(SPEC.acts.every((a) => a.out === undefined));
  assert.ok(SPEC.acts.every((a) => WORLD.out[a.id]));
});

test('服务端的发现簿换进本机 (load): "多久以前"按小屋的钟换回来; 单件位置留本机; 服务端空了本机也不留', () => {
  const c = clock(1000), K = mind(c);
  K.seenAt(BOOKS[0], [-2.77, 0.7, 2.15]);
  K.cannot(BOOKS[3]);
  K.load({ ok: true, things: { books: { n: 3, first_ago: 600, last_ago: 10 } }, states: { plant: { level: 'dry', ago: 60 } },
    spills: [{ id: 'p1', kind: 'water', at: [0.4, 1.2], y: 0, ago: 30 }], tries: { lamp: { 'night-off': { n: 2, ok: 2, v: { fun: 5 }, ago: 120 } } },
    skips: { water_plant: { wet: 1 } }, likes: { [BOOKS[1]]: { n: 1, v: 9 } }, visits: { bed: 5 }, high: 1.04, stuck: { [BOOKS[2]]: 50 } });
  assert.equal(K.things.books.first, 400);
  assert.equal(K.level('plant'), 'dry');
  assert.equal(K.tries.lamp['night-off'].last, 880);
  assert.equal(K.likeOf(BOOKS[1]), 'liked');
  assert.ok(K.blocked(BOOKS[2]) && K.blocked(BOOKS[3]));  // 两边的并集
  assert.equal(K.reachable(1.2), false);
  assert.ok(K.whereOf(BOOKS[0]));                          // 这一份没带单件位置 (老服务端): 留本机的
  K.load({ ok: true, things: { books: { n: 3, first_ago: 600, last_ago: 10 } }, tries: {}, where: { [BOOKS[4]]: { at: [-2.8, 0.7, 1.9], ago: 20 } },
    verbs: { read_book: 'read' } });
  assert.equal(K.whereOf(BOOKS[0]), null);                 // 带了: 以服务端的为准
  assert.deepEqual(K.whereOf(BOOKS[4]), [-2.8, 0.7, 1.9]);
  assert.equal(K.verbOf.read_book, 'read');
  K.load({ ok: true, things: {}, states: {}, spills: [], tries: {}, skips: {}, likes: {}, visits: {}, high: null, stuck: {} });
  assert.equal(K.whereOf(BOOKS[0]), null);                 // 服务端空了 (重置过): 本机的也不留
  assert.equal(K.blocked(BOOKS[3]), false);
  assert.equal(K.reachable(1.2), true);
});
