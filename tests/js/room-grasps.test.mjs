import { test } from 'node:test';
import assert from 'node:assert/strict';
import { roomGrasp } from '../../desktop/web/room-grasps.js';

const asset = '/pet/rooms/petal-oak-life-v1/room.glb';
const size = [0.298, 0.238606632, 0.05480194];
test('only the reviewed room, book and geometry opt into a surface grasp', () => {
  const g = roomGrasp(asset, 'IR_Item_Library_01', size);
  assert.deepEqual(g.point, [0.1409999430179596, 0, 0]);
  assert.deepEqual(g.normal, [1, 0, 0]);
  assert.ok(Math.abs(size[0] / 2 - g.point[0] - 0.0080000569820404) < 1e-12);
  assert.equal(g.positionTolerance, 0.01);
  assert.equal(g.preferredHand, 'left');
  assert.equal(roomGrasp('/new-room.glb', 'IR_Item_Library_01', size), null);
  assert.equal(roomGrasp(asset, 'IR_Item_Library_02', size), null);
  assert.equal(roomGrasp(asset, 'IR_Item_Library_01', [0.5, size[1], size[2]]), null);
  for (const dimensions of [null, [NaN, 1, 1], [Infinity, 1, 1], new Array(3), [0.298, 0.239]]) {
    assert.equal(roomGrasp(asset, 'IR_Item_Library_01', dimensions), null);
  }
});
test('metadata instances are independent and do not change support geometry', () => {
  const dimensions = size.slice(), a = roomGrasp(asset, 'IR_Item_Library_01', dimensions);
  a.point[0] = 10; a.normal[0] = 0;
  const b = roomGrasp(asset, 'IR_Item_Library_01', dimensions);
  assert.deepEqual(dimensions, size);
  assert.deepEqual(b.point, [0.1409999430179596, 0, 0]);
  assert.deepEqual(b.normal, [1, 0, 0]);
  assert.equal(b.half, undefined);
});
