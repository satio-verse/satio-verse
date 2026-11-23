import { test } from 'node:test';
import assert from 'node:assert/strict';
import { observe, regionAt, REGIONS, standFor, visible, WALK, witnessed, zoneAt } from '../../desktop/web/room-sight.js';

const PINK = 'IR_Item_Espresso_Pink', WHITE = 'IR_Item_Espresso_White';
const at = (r) => REGIONS[r].at.slice();

test('在哪一处: 站位附近 (坐在阅读椅上也算), 半路上不算', () => {
  assert.equal(zoneAt(2.15, -1.24), 'coffee');
  assert.equal(zoneAt(1.78, -1.12), 'fridge');  // 和咖啡角只差 0.39 m: 按近的算
  assert.equal(zoneAt(-1.76, 1.6), 'reading');
  assert.equal(zoneAt(0.35, -0.95), 'desk');    // 坐在电竞椅上
  assert.equal(zoneAt(0.2, 0.2), null);         // 房间中间
  assert.equal(zoneAt(-2.0, -0.06), 'bed');     // 坐在床上休息 (离窗边站位只有 0.4 m)
  assert.equal(zoneAt(-2.3, 0.2), 'window');    // 站在窗边还是窗边
  assert.equal(zoneAt(-2.0, -1.13), null);      // 躺在床上睡觉: 闭着眼, 不报
});

test('东西放在哪一处', () => {
  assert.equal(regionAt(at('surf_desk')), 'surf_desk');
  assert.equal(regionAt([0.8, 0.8, -1.7]), 'surf_desk');
  assert.equal(regionAt([0.2, 0.0, 0.2]), null);  // 地上
  assert.equal(regionAt(null), null);
});

test('只看得到这一处的台面和开着的冰箱', () => {
  assert.deepEqual(visible('coffee'), ['coffee_machine', 'water_caddy']);
  assert.deepEqual(visible('fridge'), []);
  assert.deepEqual(visible('fridge', ['fridge']), ['fridge']);
  const o = observe({ x: 2.15, z: -1.24, items: { [PINK]: at('surf_desk'), [WHITE]: at('coffee_machine') } });
  assert.deepEqual(o, { at: 'coffee', seen: { coffee_machine: [WHITE], water_caddy: [] }, hand: [], open: [] });
  assert.equal(observe({ x: 0.2, z: 0.2, items: {} }), null);
  const held = observe({ x: 0.95, z: -1.15, items: { [PINK]: 'hand' } });
  assert.deepEqual(held.hand, [PINK]);
  assert.deepEqual(observe({ x: 1.78, z: -1.12, where: { [PINK]: 'fridge' }, open: ['fridge'] }).seen, { fridge: [PINK] });
});

test('双胞胎: 杯子藏在她看不到的两处, 她看到的一模一样', () => {
  const me = { x: -1.6, z: 2.12 };  // 在阅读角
  for (const [a, b] of [['surf_desk', 'surf_bed'], ['coffee_machine', 'surf_yoga'], ['nightstand', 'water_caddy']]) {
    const x = observe({ ...me, items: { [PINK]: at(a) } }), y = observe({ ...me, items: { [PINK]: at(b) } });
    assert.deepEqual(x, y, `${a} / ${b}`);
  }
  assert.notDeepEqual(observe({ ...me, items: { [PINK]: at('surf_table') } }), observe({ ...me, items: { [PINK]: at('surf_desk') } }));
});

test('你挪东西: 她在场才看得到', () => {
  assert.deepEqual(witnessed('reading', PINK, 'coffee_machine', 'surf_table'), { kind: 'moved', item: PINK, from: null, to: 'surf_table' });
  assert.deepEqual(witnessed('coffee', PINK, 'coffee_machine', 'surf_desk'), { kind: 'moved', item: PINK, from: 'coffee_machine', to: null });
  assert.equal(witnessed('reading', PINK, 'coffee_machine', 'surf_desk'), null);
  assert.equal(witnessed(null, PINK, 'coffee_machine', 'surf_table'), null);  // 走在路上
});

test('走过去找: 站位和朝向', () => {
  const s = standFor('desk', 'surf_desk');
  assert.deepEqual(s.xz, [0.95, -1.15]);
  assert.ok(Math.abs(s.yaw - Math.PI) < 0.2);  // 电脑桌在站位后面 (-z)
  // 阅读椅前面的缝寻路走不到 (自检里找了 16 次"去阅读角"): 走到小边桌这边, 站在那里也算在阅读角
  const r = standFor('reading', 'surf_table');
  assert.deepEqual(r.xz, WALK.reading);
  assert.equal(zoneAt(...r.xz), 'reading');
  assert.deepEqual(observe({ x: r.xz[0], z: r.xz[1], items: { [PINK]: at('surf_table') } }).seen, { surf_table: [PINK], surf_chair: [] });
});

test('感知门不在这里: 给了液体的真值也不报 (液面、洒、烫由液体和体感那边分档)', () => {
  const me = { x: 2.15, z: -1.24 }, items = { [PINK]: at('coffee_machine'), [WHITE]: 'hand' };
  const a = observe({ ...me, items }), b = observe({ ...me, items, levels: { [PINK]: 0.42 }, ml: { [PINK]: 61 } });
  assert.deepEqual(a, b);
  assert.deepEqual(Object.keys(a).sort(), ['at', 'hand', 'open', 'seen']);
});
