// 删除代次 (desktop/web/room-gen.js): 页面见到更大的代次时清哪些本机键.
import test from 'node:test';
import assert from 'node:assert/strict';
import { roomGenPlan, roomGenKeys } from '../../desktop/web/room-gen.js';

test('global forget includes scene-local knowledge without deleting physical or personal state', () => {
  assert.deepEqual(roomGenKeys(['pet.room.known'], ['pet.room.scene.casa-luce-v8.known', 'pet.room.scene.another.known',
    'pet.room.scene.casa-luce-v8.world', 'pet.room.scene.casa-luce-v8.liquid', 'pet.room.needs', 'pet.room.body',
    'pet.room.practice', 'pet.room.scene...known']), ['pet.room.known', 'pet.room.scene.casa-luce-v8.known', 'pet.room.scene.another.known']);
});

test('代次没变或更小: 不用管', () => {
  assert.equal(roomGenPlan(0, 0, null), null);
  assert.equal(roomGenPlan(3, 3, { last_reset: { gen: 3, scope: ['experience'] } }), null);
  assert.equal(roomGenPlan(2, 3, null), null);
  assert.equal(roomGenPlan(null, 0, null), null);
});

test('回包没拿到: 先不动, 下次再看', () => {
  assert.deepEqual(roomGenPlan(1, 0, null), { retry: true });
});

test('只删了经历: 只清发现簿, 练习参数留着', () => {
  const p = roomGenPlan(1, 0, { generation: 1, last_reset: { gen: 1, scope: ['experience'] } });
  assert.deepEqual(p.keys, ['pet.room.known']);
  assert.equal(p.guessed, false);
});

test('完整重置: 发现簿和练习参数都清, 环境本身 (world) 不清', () => {
  const p = roomGenPlan(2, 1, { last_reset: { gen: 2, scope: ['experience', 'growth', 'understanding', 'page'] } });
  assert.deepEqual(p.keys.sort(), ['pet.room.known', 'pet.room.practice']);
  assert.ok(!p.keys.includes('pet.room.world'));
});

test('中间漏了几次、只知道最后一次: 按全部范围清', () => {
  const p = roomGenPlan(3, 0, { last_reset: { gen: 3, scope: ['experience'] } });
  assert.equal(p.guessed, true);
  assert.deepEqual(p.keys.sort(), ['pet.room.known', 'pet.room.practice']);
});

test('有历史就照历史合起来清', () => {
  const p = roomGenPlan(3, 1, { history: [{ gen: 1, scope: ['page'] }, { gen: 2, scope: ['experience'] }, { gen: 3, scope: ['experience'] }] });
  assert.equal(p.guessed, false);
  assert.deepEqual(p.keys, ['pet.room.known']);  // 第 1 次的 page 已经清过 (mine = 1), 不再清练习参数
});
