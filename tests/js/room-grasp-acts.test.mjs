import { test } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';

// Only resolve the browser module URLs to local files; load the actual exported actions.
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
const { grab, place, homeOf } = await import('../../desktop/web/room-acts.js');

function actionFixture(kind, error) {
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(.298, .238606632, .05480194));
  mesh.position.set(-2.766, .7193033, 2.0821474); mesh.rotation.set(.2, -.1, .3);
  const item = { id: 'reviewed-book', mesh, half: .119303316, physical: false,
    grasp: { version: 1 }, held: kind === 'put' ? 'left' : null, on: kind === 'put' ? null : 'room',
    origin: { on: 'room', pos: mesh.position.clone() },
    graspBinding: kind === 'put' ? { version: 1, side: 'left', pose: { position: [.1, .2, .3], quaternion: [0, 0, 0, 1] } } : null };
  const calls = [], forbidden = name => () => { calls.push(name); throw new Error(`Forbidden fallback: ${name}`); };
  const layer = { items: { [item.id]: item }, targets: { home: { place: [-2.766, .6, 2.0821474], itemId: item.id } },
    _freeHand: forbidden('freeHand'), _release: forbidden('release') };
  const ctx = { r: { layer, practice: { attempt: forbidden('practice') }, poses: { set: forbidden('pose') },
    body: { begin: forbidden('body') } },
    run: async action => { calls.push(action); throw error; }, walk: forbidden('walk'), wait: forbidden('wait') };
  return { ctx, item, calls };
}

for (const [kind, operation] of [['pick', grab], ['put', place]]) {
  for (const [label, message] of [['navigation', '走不到抓握点：没有可达站位'], ['contact', '掌心还没有接触抓握点']]) {
    test(`${kind}: opt-in ${label} failure retains original Error identity and never enters legacy fallback`, async () => {
      const error = Object.assign(new Error(message), { code: `test-${label}` });
      const f = actionFixture(kind, error), before = {
        position: f.item.mesh.position.toArray(), quaternion: f.item.mesh.quaternion.toArray(),
        held: f.item.held, on: f.item.on, binding: f.item.graspBinding, origin: f.item.origin,
      };
      const to = kind === 'pick' ? f.item.id : 'home';
      await assert.rejects(() => operation(f.ctx, to), caught => {
        assert.equal(caught, error, 'Caller must retain navigation/contact identity for its recovery policy');
        return true;
      });
      assert.deepEqual(f.calls, [{ do: kind, to }], 'No crouch, release, binding or second action is allowed');
      assert.deepEqual(f.item.mesh.position.toArray(), before.position);
      assert.deepEqual(f.item.mesh.quaternion.toArray(), before.quaternion);
      assert.equal(f.item.held, before.held); assert.equal(f.item.on, before.on);
      assert.equal(f.item.graspBinding, before.binding); assert.equal(f.item.origin, before.origin);
    });
  }
}

for (const scoped of ['reviewed-book', 'missing-book']) {
  test(`put: scoped ${scoped} never falls back to the other held legacy item`, async () => {
    const error = new Error('走不到归还目标'), f = actionFixture('put', error);
    const other = { id: 'legacy-cup', held: 'right', on: null, mesh: new THREE.Object3D(), half: .06 };
    other.mesh.position.set(.4, .8, -.2);
    // Insert the other item first: an unscoped Object.values(...).find(held) would select it.
    f.ctx.r.layer.items = { [other.id]: other, [f.item.id]: f.item };
    f.ctx.r.layer.targets.home.itemId = scoped;
    const binding = f.item.graspBinding, cupPosition = other.mesh.position.toArray(), bookPosition = f.item.mesh.position.toArray();
    await assert.rejects(() => place(f.ctx, 'home'), caught => { assert.equal(caught, error); return true; });
    assert.deepEqual(f.calls, [{ do: 'put', to: 'home' }]);
    assert.equal(other.held, 'right'); assert.equal(f.item.held, 'left');
    assert.equal(other.on, null); assert.equal(f.item.on, null); assert.equal(f.item.graspBinding, binding);
    assert.deepEqual(other.mesh.position.toArray(), cupPosition); assert.deepEqual(f.item.mesh.position.toArray(), bookPosition);
  });
}

test('homeOf records the exact item, detached world pose and rotated local-box support height', () => {
  const scene = new THREE.Group(), parent = new THREE.Group(); scene.add(parent);
  parent.position.set(1.2, .5, -.7); parent.rotation.set(0, .4, .3);
  const half = [.149, .119303316, .02740097], originalHalf = half.slice();
  const mesh = new THREE.Mesh(new THREE.BoxGeometry(...half.map(v => 2 * v)));
  mesh.position.set(.3, .8, -.4); mesh.rotation.set(0, 0, Math.PI / 2 - .3); parent.add(mesh); scene.updateMatrixWorld(true);
  const item = { id: 'reviewed-book', mesh, grasp: { version: 1 }, graspHalfExtents: half, half: .119303316 };
  const r = { layer: { items: { [item.id]: item }, targets: {} } }, approach = [.1, -.2];
  const center = mesh.getWorldPosition(new THREE.Vector3()), quaternion = mesh.getWorldQuaternion(new THREE.Quaternion());
  // Independent geometry oracle: transform the actual box vertices via THREE, not the implementation's axis formula.
  const bottom = new THREE.Box3().setFromObject(mesh).min.y;
  const key = homeOf(r, item.id, 'Book home', approach), target = r.layer.targets[key];
  assert.equal(target.itemId, item.id); assert.equal(target.name, 'Book home');
  assert.deepEqual(target.objectPosition, center.toArray()); assert.deepEqual(target.look, center.toArray());
  assert.ok(new THREE.Quaternion(...target.objectQuaternion).angleTo(quaternion) < 1e-7);
  assert.ok(Math.abs(target.place[1] - bottom) < 1e-7);
  assert.ok(Math.abs(target.place[1] - (center.y - item.half)) > .01, 'Rotated extent must not collapse to the old half height');
  assert.equal(target.place[0], center.x); assert.equal(target.place[2], center.z);
  assert.equal(item.half, .119303316); assert.deepEqual(item.graspHalfExtents, originalHalf);
  const saved = JSON.stringify(target); mesh.position.set(9, 9, 9); mesh.rotation.set(0, 0, 0);
  assert.equal(JSON.stringify(target), saved, 'Home is a plain-data pose snapshot, not live Three instances');
});

test('homeOf during a held retry keeps the original shelf pose instead of the rotated hand pose', () => {
  const f = actionFixture('put', new Error('unused')), item = f.item;
  item.graspHalfExtents = [.149, .119303316, .02740097];
  const originQ = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 0, 1), Math.PI / 2).toArray();
  item.graspOrigin = { on: 'shelf', pose: { position: [1, .8, 2], quaternion: originQ } };
  item.mesh.position.set(4, 5, 6); item.mesh.rotation.set(.7, -.4, .2);
  const heldPose = { p: item.mesh.position.toArray(), q: item.mesh.quaternion.toArray() }, binding = item.graspBinding;
  const key = homeOf(f.ctx.r, item.id, 'Original shelf', null), target = f.ctx.r.layer.targets[key];
  assert.equal(target.itemId, item.id); assert.deepEqual(target.objectPosition, [1, .8, 2]);
  assert.deepEqual(target.objectQuaternion, originQ);
  assert.ok(Math.abs(target.place[1] - (.8 - .149)) < 1e-12, 'Original local X is vertical at this shelf pose');
  assert.equal(item.held, 'left'); assert.equal(item.graspBinding, binding);
  assert.deepEqual(item.mesh.position.toArray(), heldPose.p); assert.deepEqual(item.mesh.quaternion.toArray(), heldPose.q);
  const saved = JSON.stringify(target); item.graspOrigin.pose.position[0] = 99; item.graspOrigin.pose.quaternion[0] = 99;
  assert.equal(JSON.stringify(target), saved, 'The preserved origin is copied into a detached home target');
});

test('homeOf rejects a held item without an origin, preserving the old target and attachment', () => {
  const f = actionFixture('put', new Error('unused')), item = f.item, key = `home_${item.id}`;
  item.graspHalfExtents = [.149, .119303316, .02740097];
  const old = { id: key, itemId: item.id, place: [1, .6, 2], objectPosition: [1, .719303316, 2], objectQuaternion: [0, 0, 0, 1] };
  f.ctx.r.layer.targets[key] = old;
  const before = JSON.stringify(old), binding = item.graspBinding, position = item.mesh.position.toArray();
  assert.throws(() => homeOf(f.ctx.r, item.id, 'Invalid retry', null), /缺少抓握原位置/);
  assert.equal(f.ctx.r.layer.targets[key], old); assert.equal(JSON.stringify(old), before);
  assert.equal(item.held, 'left'); assert.equal(item.graspBinding, binding);
  assert.deepEqual(item.mesh.position.toArray(), position); assert.deepEqual(f.calls, []);
});
