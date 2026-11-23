import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { register } from 'node:module';

const THREE_URL = new URL('../../web/vendor/three/build/three.module.js', import.meta.url).href;
const STATIC_URL = new URL('../../web/', import.meta.url).href;
const PET_URL = new URL('../../desktop/web/', import.meta.url).href;
register(`data:text/javascript,${encodeURIComponent(`export async function resolve(s,c,n) {
  if (s === 'three') return {url:${JSON.stringify(THREE_URL)},shortCircuit:true};
  if (s.startsWith('/static/')) return {url:new URL(s.slice(8),${JSON.stringify(STATIC_URL)}).href,shortCircuit:true};
  if (s.startsWith('/pet/')) return {url:new URL(s.slice(5),${JSON.stringify(PET_URL)}).href,shortCircuit:true};
  return n(s,c); }`)}`);
globalThis.location = { search: '' };
const THREE = await import('three');
// Expose existing private scripts only in this in-memory test module; no runtime export/test branch.
const source = await readFile(new URL('../../desktop/web/room-acts.js', import.meta.url), 'utf8');
const { intoFridge, SCRIPTS, Life } = await import(`data:text/javascript;base64,${Buffer.from(source + '\nexport { intoFridge, SCRIPTS };').toString('base64')}`);

function fixture(scene, nodes = {}) {
  const calls = [], cleanup = [];
  const r = { scene, props: {
    node: id => nodes[id] || null, fridgeOpen: false,
    fridge: async open => { r.props.fridgeOpen = open; calls.push(['door', open]); },
    fed: [{ id: 'feed', kind: 'beans' }], remove: id => calls.push(['remove', id]),
  }, v: { setEmotion() {} }, layer: { lookAt() {} }, needs: { v: {} } };
  const ctx = { r, life: { acts: [] }, say() {}, wait: async () => {},
    run: async a => calls.push(['run', a]), walk: async (p, yaw) => calls.push(['walk', [...p], yaw]),
    later: f => cleanup.push(f), reach: async (side, p, event) => { calls.push(['reach', side, p.toArray()]); event?.(); },
  };
  return { ctx, calls, cleanup };
}
const reached = f => f.calls.find(c => c[0] === 'reach')?.[2];
const walked = f => f.calls.find(c => c[0] === 'walk')?.[1];

test('actual drink remains available while thirsty and energetic, with no scripted reward', () => {
  const coffee = { id: 'coffee', script: 'coffee', need: 'beans', want: { water: 2 } };
  const needs = { v: { energy: 98, water: 4 }, effectiveEnergy: 100, stock: { beans: 2 }, actualConsumption: true, fatigue: 0, worst: () => ({ key: 'water', gap: .94 }) };
  const life = Object.create(Life.prototype);
  Object.assign(life, { requests: { blocked: false }, r: { needs, props: { fed: [] }, scene: { life: [coffee] } }, order: [], busyWith: new Map() });
  assert.equal(life.can(coffee, false), true);
  assert.equal(life.urgent([coffee]), coffee);
  assert.equal(coffee.gives, undefined);
  needs.stock.beans = 0; assert.equal(life.can(coffee, false), false);
  needs.stock.beans = 2; needs.v.water = 90; assert.equal(life.can(coffee, false), false);
});

test('temporary alertness cannot suppress urgent sleep or replace sleep need', () => {
  const sleep = { id: 'sleep', script: 'sleep', want: { energy: 2 } };
  const needs = { v: { energy: 15 }, effectiveEnergy: 21, fatigue: .95, actualConsumption: true, stock: {} };
  const life = Object.create(Life.prototype);
  Object.assign(life, { requests: { blocked: false }, r: { needs, props: { fed: [] }, scene: { life: [sleep] } }, base: [sleep], order: [], busyWith: new Map() });
  assert.equal(life.can(sleep, false), true); assert.equal(life.urgent([sleep]), sleep);
  const w = life.weight(sleep, false); needs.effectiveEnergy = 100; assert.equal(life.weight(sleep, false), w);
});

test('configured fridge uses world point, opens before reach and closes after storage', async () => {
  const p = [2.7, .7575, -3.21], f = fixture({ fridge: { reach: p } }, { Fridge_Door_Hinge: {} });
  await intoFridge(f.ctx, () => f.calls.push(['stored']));
  assert.deepEqual(f.calls, [['run', { do: 'go', to: 'fridge' }], ['door', true], ['reach', 'right', p], ['stored'], ['door', false]]);
  assert.deepEqual(p, [2.7, .7575, -3.21]);
});

test('legacy fridge retains original point and does not gain a hinge requirement', async () => {
  const f = fixture({ fridge: {} });
  await intoFridge(f.ctx, () => f.calls.push(['stored']));
  assert.deepEqual(reached(f), [1.92, .72, -1.78]);
  assert.equal(f.calls.filter(c => c[0] === 'stored').length, 1);
});

test('configured fridge missing door fails before walking, callback or door animation', async () => {
  const f = fixture({ fridge: { reach: [1, 2, 3] } });
  await assert.rejects(() => intoFridge(f.ctx, () => assert.fail('must not store')), /找不到.*冰箱门/);
  assert.deepEqual(f.calls, []);
});

test('malformed explicit fridge points never fall back to the old room', async () => {
  for (const reach of [null, [], [1, 2], [1, NaN, 3], [1, Infinity, 3], [1, '2', 3], Array(3)]) {
    const f = fixture({ fridge: { reach } }, { Fridge_Door_Hinge: {} });
    await assert.rejects(() => intoFridge(f.ctx), /位置配置无效/);
    assert.deepEqual(f.calls, []);
  }
});

test('beans use explicit world point and separate stand without legacy height clamping', async () => {
  const cfg = { stand: [3, -2], beansNode: 'new-beans', beansReach: [.94, 1.6, -3.65], beansStand: [.95, -2.75] };
  const f = fixture({ coffee: cfg }, { 'new-beans': {} });
  await SCRIPTS.fetch(f.ctx);
  assert.deepEqual(walked(f), cfg.beansStand); assert.deepEqual(reached(f), cfg.beansReach);
  assert.equal(f.calls.filter(c => c[0] === 'remove').length, 1);
  assert.equal(cfg.beansReach[1], 1.6);
});

test('beans node-only configuration resolves actual world position through a parent transform', async () => {
  const parent = new THREE.Object3D(), node = new THREE.Object3D(); parent.add(node);
  parent.position.set(2, 1, -4); parent.rotation.y = Math.PI / 2; node.position.set(.2, .3, .4); parent.updateMatrixWorld(true);
  const f = fixture({ coffee: { stand: [0, 0], beansNode: 'beans' } }, { beans: node });
  await SCRIPTS.fetch(f.ctx);
  assert.deepEqual(reached(f), node.getWorldPosition(new THREE.Vector3()).toArray());
});

test('beans point-only configuration is supported', async () => {
  const f = fixture({ coffee: { stand: [0, 0], beansReach: [1, 2, 3] } });
  await SCRIPTS.fetch(f.ctx); assert.deepEqual(reached(f), [1, 2, 3]);
});

test('configured beans node cannot silently fall back even if a world point and old node exist', async () => {
  const f = fixture({ coffee: { stand: [0, 0], beansNode: 'missing', beansReach: [1, 2, 3] } }, { IR_Item_Canister_0: new THREE.Object3D() });
  await assert.rejects(() => SCRIPTS.fetch(f.ctx), /找不到.*咖啡豆罐/);
  assert.equal(walked(f), undefined); assert.equal(reached(f), undefined);
  assert.equal(f.calls.some(c => c[0] === 'remove'), false);
});

test('invalid explicit beans metadata fails without reach or consuming feed', async () => {
  for (const patch of [{ beansNode: null }, { beansReach: [1, NaN, 3] }, { beansReach: Array(3) }, { beansStand: [1, 2] }, { beansReach: [1, 2, 3], beansStand: [1] }]) {
    const f = fixture({ coffee: { stand: [0, 0], ...patch } });
    await assert.rejects(() => SCRIPTS.fetch(f.ctx), /配置无效|没有咖啡豆存放目标/);
    assert.equal(reached(f), undefined); assert.equal(f.calls.some(c => c[0] === 'remove'), false);
  }
});

test('legacy beans retain node preference, original fallback and the original height cap', async () => {
  const node = new THREE.Object3D(); node.position.set(4, 2, -5);
  for (const [nodes, expected] of [[{ IR_Item_Canister_0: node }, [4, 1.45, -5]], [{}, [2.3, 1.45, -1.9]]]) {
    const f = fixture({ coffee: { stand: [2.5, -1] } }, nodes);
    await SCRIPTS.fetch(f.ctx);
    assert.deepEqual(walked(f), [2.5, -1]); assert.deepEqual(reached(f), expected);
  }
  assert.equal(node.position.y, 2);
});
