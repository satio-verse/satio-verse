import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { buildGrid } from '../../web/nav.js';
import { boxGroups, furnitureBoxes, partsDepth } from '../../web/pain.js';
import { defaultScenarios, parseScenario, sceneChecks, scenePicks, sceneTasks, sweepScenarios } from '../../web/scene-tasks.js';

const room = JSON.parse(readFileSync(new URL('../../web/scenes/room.json', import.meta.url), 'utf8'));

test('客厅: 默认评估的目标就是 eval 段写的 (和以前手写的场景一样), 不写时自动挑', () => {
  assert.deepEqual(scenePicks(room), { walk: ['window', 'center'], sit: 'sofa', pickput: ['ball', 'coffee_table'] });
  assert.deepEqual(defaultScenarios(room), ['idle', 'walk', 'sit', 'pickput', 'gesture', 'talk']);
  const auto = scenePicks({ ...room, eval: undefined });
  // 离出生点最远的目标走过去, 再走到离它最远的; 地上的球放到最矮的台面
  assert.equal(auto.walk.length, 2);
  assert.notEqual(auto.walk[0], auto.walk[1]);
  assert.equal(auto.sit, 'sofa');
  assert.deepEqual(auto.pickput, ['ball', 'coffee_table']);
});

test('全套: 走到每个目标、坐每个座位、每件东西放到每个别的台面', () => {
  const t = sceneTasks(room), all = sweepScenarios(room);
  assert.ok(t.walk.includes('window') && !t.walk.includes('ball'));
  for (const id of t.walk) assert.ok(all.includes(`walk:${id}`));
  assert.ok(all.includes('sit:sofa'));
  assert.ok(all.includes('pickput:ball>coffee_table') && all.includes('pickput:book>sofa'));
  assert.ok(!all.includes('pickput:book>desk'), '书本来就在书桌上');
  assert.equal(new Set(all).size, all.length);
  assert.deepEqual(parseScenario('pickput:ball>coffee_table'), ['pickput', ['ball', 'coffee_table']]);
  assert.deepEqual(parseScenario('idle'), ['idle', undefined]);
});

test('场景里做不了的那类不跑', () => {
  const bare = { floor: room.floor, spawn: room.spawn, objects: room.objects.filter((o) => !o.seat && !o.item) };
  assert.deepEqual(defaultScenarios(bare, { gestDB: null }), ['idle', 'walk', 'talk']);
  assert.ok(!sweepScenarios(bare).some((n) => n.startsWith('sit') || n.startsWith('pickput')));
});

// 一个小场景: 一张桌子 (桌面 + 四条腿), 一张有坐垫的凳子
const mini = {
  floor: [[-2, -2], [2, -2], [2, 2], [-2, 2]], agentRadius: 0.3, spawn: { pos: [0, 1] },
  objects: [
    { id: 'table', box: { center: [0, -1], size: [1, 0.6], height: 0.75 }, approach: [0, -0.3], place: [0, 0.76, -1] },
    { id: 'stool', box: { center: [1.2, 0.5], size: [0.4, 0.4], height: 0.45 }, approach: [0.6, 0.5],
      seat: { pos: [1.2, 0.5], yaw: -1.57, height: 0.43 } },
    { id: 'cup', virtual: true, item: { shape: 'box', size: [0.08, 0.1, 0.08], pos: [0.3, 0.8, -1], on: 'table' }, approach: [0.3, -0.3] },
    { id: 'nowhere', box: { center: [-1.5, 1.5], size: [0.3, 0.3], height: 0.5 }, approach: [-1.5, 1.5] },
  ],
};
const miniParts = [
  { id: 'table', min: [-0.5, 0, -1.3], max: [0.5, 0.75, -0.7], parts: [
    { part: 'solid', min: [-0.5, 0.72, -1.3], max: [0.5, 0.75, -0.7] },
    ...[[-0.45, -1.25], [0.45, -1.25], [-0.45, -0.75], [0.45, -0.75]].map(([x, z]) => ({ part: 'leg', min: [x - 0.02, 0, z - 0.02], max: [x + 0.02, 0.72, z + 0.02] })),
  ] },
  { id: 'stool', min: [1, 0, 0.3], max: [1.4, 0.45, 0.7], parts: [{ part: 'seat', min: [1, 0, 0.3], max: [1.4, 0.45, 0.7] }] },
];
const fakeLoco = (scene) => ({ scene, grid: buildGrid(scene), items: { cup: { half: 0.05 } }, acts: null, hipHeight: 0.85 });

test('场景检查: 数据和房间模型对得上时只报真问题', () => {
  const out = sceneChecks(fakeLoco(mini), miniParts);
  const by = (id) => out.filter((c) => c.what === id).map((c) => c.msg).join(' | ');
  assert.equal(by('table'), '', `桌子不该报: ${by('table')}`);
  assert.equal(by('stool'), '', `凳子不该报: ${by('stool')}`);
  assert.equal(by('cup'), '', `杯子不该报: ${by('cup')}`);
  assert.match(by('nowhere'), /站位/);       // 站位在家具里, 不能走
  assert.match(by('nowhere'), /没有它的网格/);
});

test('场景检查: 放置点悬空、座位悬在座面上、物品陷进桌面、能坐却没标部件', () => {
  const bad = structuredClone(mini);
  bad.objects[0].place = [0, 0.76, -0.55];           // 桌子前沿外 15 cm
  bad.objects[1].seat.height = 0.5;                  // 比座面高 5 cm
  bad.objects[2].item.pos = [0.3, 0.74, -1];         // 底面在桌面下 6 cm
  const parts = structuredClone(miniParts);
  parts[1].parts[0].part = 'solid';
  const out = sceneChecks(fakeLoco(bad), parts).filter((c) => c.level === 'warn');
  const has = (id, re) => out.some((c) => c.what === id && re.test(c.msg));
  assert.ok(has('table', /下面没有台面/));
  assert.ok(has('stool', /座位高度/));
  assert.ok(has('stool', /没标部件角色/));
  assert.ok(has('cup', /底面/));
});

test('碰撞部件: 桌子底下是空的, 坐着时可以不算座面', () => {
  const parts = [miniParts[0]];
  const under = [0, 0.4, -1];                         // 桌面下面、四条腿中间
  assert.equal(partsDepth(parts, under, 0.05), 0);
  assert.ok(partsDepth(boxGroups(furnitureBoxes(mini.objects)), under, 0.05) > 0.3, '整块包围盒会把桌子底下也算进去');
  assert.ok(partsDepth(parts, [0, 0.74, -1], 0.05) > 0);
  const seat = [1.2, 0.44, 0.5];
  assert.ok(partsDepth([miniParts[1]], seat, 0.05) > 0);
  assert.equal(partsDepth([miniParts[1]], seat, 0.05, (g, p) => g.id === 'stool' && p.part === 'seat'), 0);
});


test('场景检查的高度分类与执行一致: 浮点边界稳定, 毫米差和缺省高度保留', () => {
  const cases = [
    [0.71930330991745 - 0.11930331587791443, null],
    [0.6, null], [0.600000006, null], [0.5999, '蹲下拿放'],
    [0.9999, null], [0.999999994, '前倾伸手'], [1, '前倾伸手'], [1.000000006, '前倾伸手'],
  ];
  for (const type of ['place', 'item']) {
    const objects = cases.map(([y], i) => ({ id: String(i),
      ...(type === 'place' ? { place: [0, y, 0] } : { item: { pos: [0, y, 0] } }) }));
    objects.push({ id: 'no_height' });
    const out = sceneChecks({ scene: { objects }, hipHeight: 1, acts: null }, null);
    cases.forEach(([, expected], i) => {
      const found = out.filter((c) => c.what === String(i));
      assert.equal(found.length, expected ? 1 : 0, `${type} at ${cases[i][0]}`);
      if (expected) assert.ok(found[0].msg.includes(expected), found[0].msg);
    });
    assert.ok(!out.some((c) => c.what === 'no_height'));
  }
});

test('高度边界的片段范围和提示使用同一种分类', () => {
  const scene = { objects: [{ id: 'low', place: [0, 0.5999, 0] }, { id: 'high', place: [0, 0.999999994, 0] }] };
  const acts = { groups: { pick: [{ hand: [0, 0.1, 0] }], put: [], reach: [{ hand: [0, 0.75, 0] }] } };
  const out = sceneChecks({ scene, acts, hipHeight: 1 }, null);
  assert.equal(out.length, 2);
  assert.ok(out.every((c) => c.level === 'info'));
  assert.match(out.find((c) => c.what === 'low').msg, /蹲下拿放.*0\.10-0\.10/);
  assert.match(out.find((c) => c.what === 'high').msg, /前倾伸手.*0\.75-0\.75/);
});
