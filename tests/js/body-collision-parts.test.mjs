import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createBodyPartBoxes } from '../../web/body-collision-parts.js';

const part = (min, max) => ({ part: 'solid', min, max });
function group(id, parts) {
  return { id, parts, min: [0, 1, 2].map(k => Math.min(...parts.map(p => p.min[k]))),
    max: [0, 1, 2].map(k => Math.max(...parts.map(p => p.max[k]))) };
}
function distance(point, box) {
  const lo = [box.center[0] - box.size[0] / 2, box.y0, box.center[1] - box.size[1] / 2];
  const hi = [box.center[0] + box.size[0] / 2, box.y1, box.center[1] + box.size[1] / 2];
  return Math.hypot(...point.map((v, i) => v - Math.max(lo[i], Math.min(hi[i], v))));
}

test('absent declaration returns legacy sentinel; explicit empty declaration stays empty', () => {
  assert.equal(createBodyPartBoxes({ objects: [] }), null);
  assert.deepEqual(createBodyPartBoxes({ collisionParts: [] })(), []);
  assert.throws(() => createBodyPartBoxes({ collisionParts: null }), /碰撞/);
});

test('raised thin slab uses absolute min/max Y, even when navigation calls it walkable', () => {
  const g = group('shelf', [part([-1, 1.5, -.3], [1, 1.55, .3])]);
  const boxes = createBodyPartBoxes({ collisionParts: [g], objects: [{ id: 'shelf', name: '高架', kind: 'desk', walkable: true,
    box: { center: [0, 0], size: [2, .6], base: 1.5, height: .05 } }] })();
  assert.equal(boxes.length, 1); assert.equal(boxes[0].y0, 1.5); assert.equal(boxes[0].y1, 1.55);
  assert.equal(boxes[0].name, '高架'); assert.equal(distance([0, 1.52, 0], boxes[0]), 0);
  assert.ok(distance([0, .7, 0], boxes[0]) > .7);
});

test('table top and legs retain an empty under-table center instead of the enclosing group box', () => {
  const parts = [part([-1, .75, -.5], [1, .8, .5]), part([-.95, 0, -.45], [-.85, .75, -.35]), part([.85, 0, .35], [.95, .75, .45])];
  const boxes = createBodyPartBoxes({ collisionParts: [group('table', parts)], objects: [{ id: 'table', kind: 'desk' }] })();
  assert.equal(boxes.length, 3);
  assert.ok(boxes.every(box => distance([0, .4, 0], box) > .1));
  assert.ok(boxes.some(box => distance([-.9, .4, -.4], box) === 0));
  assert.ok(boxes.some(box => distance([0, .77, 0], box) === 0));
});

test('objects and decor supply labels and soft kinds, while unmapped declared solids remain present', () => {
  const g = id => group(id, [part([0, 0, 0], [1, 1, 1])]);
  const select = createBodyPartBoxes({ collisionParts: [g('bed'), g('pillow'), g('unknown')],
    objects: [{ id: 'bed', name: '床', kind: 'bed' }], decor: [{ id: 'pillow', name: '枕头', kind: 'pillow', walkable: true }] }, { bed: .05, pillow: .05 });
  const boxes = select();
  assert.deepEqual(boxes.map(b => [b.id, b.name, b.soft]), [['bed', '床', .05], ['pillow', '枕头', .05], ['unknown', 'unknown', undefined]]);
  assert.equal(select({ squeezing: true })[2].soft, .05);
  assert.equal(select()[2].soft, undefined, 'Squeezing must not mutate ordinary contact tolerance');
});

test('seat, lying and pushing exemptions change per frame without erasing other groups', () => {
  const g = id => group(id, [part([0, 0, 0], [1, 1, 1])]);
  const select = createBodyPartBoxes({ collisionParts: ['chair', 'bed', 'far-bed', 'table'].map(g), objects: [
    { id: 'chair', kind: 'chair', seat: { pos: [0, 0] } }, { id: 'bed', kind: 'bed', box: { center: [.5, 0] } },
    { id: 'far-bed', kind: 'bed', box: { center: [5, 0] } }, { id: 'table', kind: 'desk' }] });
  const ids = state => select(state).map(b => b.id);
  assert.deepEqual(ids({ seat: { pos: [.01, 0] } }), ['far-bed', 'table']);
  assert.deepEqual(ids({ lying: true }), ['chair', 'table']);
  assert.deepEqual(ids({ pushing: new Set(['table']) }), ['chair', 'bed', 'far-bed']);
  assert.deepEqual(ids({}), ['chair', 'bed', 'far-bed', 'table']);
});

test('geometry is validated and converted once; repeated frames reuse cached boxes', () => {
  const scene = { collisionParts: [group('shelf', [part([0, 2, 0], [1, 2.1, 1])])] };
  const select = createBodyPartBoxes(scene), first = select()[0];
  scene.collisionParts[0].parts[0].max[1] = -10;
  Object.defineProperty(scene, 'collisionParts', { get() { throw new Error('must not revalidate each frame'); } });
  for (let i = 0; i < 100; i++) { assert.equal(select()[0], first); assert.equal(first.y1, 2.1); }
});

test('invalid declared geometry fails closed instead of dropping into the legacy box path', () => {
  const g = group('bad', [part([0, 2, 0], [1, 1, 1])]);
  assert.throws(() => createBodyPartBoxes({ collisionParts: [g] }), /碰撞/);
  assert.throws(() => createBodyPartBoxes({ collisionParts: [group('same', [part([0, 0, 0], [1, 1, 1])]), group('same', [part([0, 0, 0], [1, 1, 1])])] }), /碰撞/);
});

test('frozen V8 profile keeps all authored parts including high walkable lintels', () => {
  const scene = JSON.parse(readFileSync(new URL('../../desktop/web/room-scenes/casa-luce-v8.json', import.meta.url), 'utf8'));
  const boxes = createBodyPartBoxes(scene, { pillow: .05, bed: .05, armchair: .04 })();
  assert.equal(boxes.length, scene.collisionParts.reduce((n, g) => n + g.parts.length, 0));
  for (const obj of scene.decor.filter(o => o.walkable)) {
    const expected = scene.collisionParts.find(g => g.id === obj.id);
    if (!expected) continue;
    assert.equal(boxes.filter(b => b.id === obj.id).length, expected.parts.length);
  }
});
