import { test } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';

const THREE_URL = new URL('../../web/vendor/three/build/three.module.js', import.meta.url).href;
const STATIC_URL = new URL('../../web/', import.meta.url).href;
register(`data:text/javascript,${encodeURIComponent(`export async function resolve(s,c,n) {
  if (s === 'three') return {url:${JSON.stringify(THREE_URL)},shortCircuit:true};
  if (s.startsWith('/static/')) return {url:new URL(s.slice(8),${JSON.stringify(STATIC_URL)}).href,shortCircuit:true};
  return n(s,c); }`)}`);
const { Body } = await import('../../desktop/web/room-body.js');
const fixture = scene => ({ scene, layer: {}, poses: { name: null }, life: {}, mover: {} });

test('actual Body consumes cached declared min/max geometry and ignores only current activity exemptions', () => {
  let reads = 0;
  const objects = [{ id: 'table', name: '高桌', kind: 'desk', walkable: true, box: { center: [0, 0], size: [2, 1], base: 1.2, height: .04 } }];
  const groups = [{ id: 'table', min: [-1, 1.2, -.5], max: [1, 1.24, .5], parts: [{ min: [-1, 1.2, -.5], max: [1, 1.24, .5] }] }];
  const scene = { objects, get collisionParts() { reads++; return groups; } };
  const r = fixture(scene), body = new Body(r), compiledReads = reads;
  const first = body._boxes();
  assert.deepEqual(first, [{ id: 'table', name: '高桌', center: [0, 0], size: [2, 1], y0: 1.2, y1: 1.24, soft: undefined }]);
  for (let i = 0; i < 30; i++) assert.equal(body._boxes()[0], first[0]);
  assert.equal(reads, compiledReads);
  r.life.squeezing = true; assert.equal(body._boxes()[0].soft, .05);
  r.mover = { moving: { key: 'push-table' }, cfg: { 'push-table': { objects: ['table'] } } };
  assert.deepEqual(body._boxes(), []);
  r.mover.moving = null; r.life.squeezing = false;
  assert.equal(body._boxes()[0], first[0]);
});

test('actual Body legacy path preserves old slab heights, walkable exclusion and seating exemptions', () => {
  const r = fixture({ objects: [
    { id: 'desk', kind: 'desk', box: { center: [0, 0], size: [2, 1], height: .8 } },
    { id: 'old-raised', kind: 'geometry', box: { center: [2, 0], size: [1, 1], base: .2, height: .3 } },
    { id: 'walkable', kind: 'geometry', walkable: true, box: { center: [3, 0], size: [1, 1], height: 2 } },
    { id: 'seat', kind: 'chair', seat: { pos: [0, 2] }, box: { center: [0, 2], size: [1, 1], height: .5 } },
  ] });
  const body = new Body(r);
  assert.equal(body._partBoxes, null);
  assert.deepEqual(body._boxes().map(b => [b.id, b.y0, b.y1]), [['desk', .75, .8], ['old-raised', .2, .3], ['seat', .38, .5]]);
  r.layer.action = { seat: { info: { pos: [0, 2] } } };
  assert.deepEqual(body._boxes().map(b => b.id), ['desk', 'old-raised']);
});

test('actual Body treats an explicit empty geometry declaration as empty, not a fallback request', () => {
  const r = fixture({ collisionParts: [], objects: [{ id: 'coarse-only', box: { center: [0, 0], size: [2, 2], height: 2 } }] });
  assert.deepEqual(new Body(r)._boxes(), []);
});
