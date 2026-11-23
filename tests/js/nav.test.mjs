import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { buildGrid, findPath, lineOfSight, nearestWalkable, pathLength, pointOnPath, projectOnPath, toCell, walkable } from '../../web/nav.js';

const scene = JSON.parse(readFileSync(new URL('../../web/scenes/room.json', import.meta.url), 'utf8'));
const grid = buildGrid(scene);
const byId = Object.fromEntries(scene.objects.map((o) => [o.id, o]));
const open = (x, z) => walkable(grid, ...toCell(grid, x, z));

test('出生点和所有目标的站位都可走, 家具本身不可走', () => {
  assert.ok(open(...scene.spawn.pos));
  for (const o of scene.objects) assert.ok(open(...o.approach), `${o.id} 的站位不可走`);
  assert.ok(!open(...byId.sofa.box.center));
  assert.ok(!open(...byId.desk.box.center));
  assert.ok(!open(-3.45, 0), '贴墙的位置应该被身体半径挡掉');
});

test('寻路能到达每个目标, 路径不穿过家具', () => {
  for (const o of scene.objects) {
    const path = findPath(grid, scene.spawn.pos, o.approach);
    assert.ok(path, `到 ${o.id} 不可达`);
    const end = path[path.length - 1];
    assert.ok(Math.hypot(end[0] - o.approach[0], end[1] - o.approach[1]) < 1e-6);
    for (let i = 1; i < path.length; i++) assert.ok(lineOfSight(grid, path[i - 1], path[i]), `${o.id} 的第 ${i} 段穿过障碍`);
  }
});

test('拉直后的路径很短, 直线可达时只有两个点', () => {
  const path = findPath(grid, [0, 0.9], [0, 1.5]);
  assert.equal(path.length, 2);
  const sofa = findPath(grid, [2.3, 1.5], byId.sofa.approach);
  assert.ok(sofa.length <= 4, `点数 ${sofa.length}`);
  const direct = Math.hypot(byId.sofa.approach[0] - 2.3, byId.sofa.approach[1] - 1.5);
  assert.ok(pathLength(sofa) < direct * 1.2);
});

test('起终点在障碍物里时吸附到最近可走处', () => {
  const p = nearestWalkable(grid, ...byId.sofa.box.center);
  assert.ok(open(...p));
  assert.ok(findPath(grid, byId.sofa.box.center, [0, 0]));
});

test('沿线投影和取点互逆', () => {
  const path = [[0, 0], [2, 0], [2, 2]];
  assert.equal(pathLength(path), 4);
  assert.deepEqual(pointOnPath(path, 3), [2, 1]);
  assert.ok(Math.abs(projectOnPath(path, [2.1, 1]) - 3) < 1e-9);
  assert.deepEqual(pointOnPath(path, 99), [2, 2]);
});
