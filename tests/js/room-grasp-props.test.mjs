import { test } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';

// Resolve browser imports to the checked-in modules; do not replace Props or its dependencies.
const THREE_URL = new URL('../../web/vendor/three/build/three.module.js', import.meta.url).href;
const STATIC_URL = new URL('../../web/', import.meta.url).href;
register(`data:text/javascript,${encodeURIComponent(`export async function resolve(s,c,n) {
  if (s === 'three') return {url:${JSON.stringify(THREE_URL)},shortCircuit:true};
  if (s.startsWith('/static/')) return {url:new URL(s.slice(8),${JSON.stringify(STATIC_URL)}).href,shortCircuit:true};
  return n(s,c); }`)}`);
globalThis.location = { search: '' };
const THREE = await import('three');
const { Props } = await import('../../desktop/web/room-props.js');

const ID = 'IR_Item_Library_01', KEY = 'pet.room.layout';
const SIZE = [.298, .238606632, .05480194], PIVOT_OFFSET = [.04, -.02, .03];
function storage(layout) {
  const values = new Map();
  if (layout) values.set(KEY, JSON.stringify(layout));
  globalThis.localStorage = { getItem: k => values.get(k) ?? null, setItem: (k, v) => values.set(k, v) };
  return values;
}
function worldPose(node) {
  return { position: node.getWorldPosition(new THREE.Vector3()).toArray(),
    quaternion: node.getWorldQuaternion(new THREE.Quaternion()).toArray() };
}
function setWorld(node, position, quaternion) {
  // Independent THREE conversion for test input, not a substitute for the production setter.
  node.parent.updateWorldMatrix(true, false);
  node.position.copy(node.parent.worldToLocal(new THREE.Vector3(...position)));
  node.quaternion.copy(node.parent.getWorldQuaternion(new THREE.Quaternion()).invert()
    .multiply(new THREE.Quaternion(...quaternion)));
  node.updateWorldMatrix(false, true);
}
function near(a, b, tolerance = 1e-9) {
  assert.equal(a.length, b.length);
  a.forEach((v, i) => assert.ok(Math.abs(v - b[i]) <= tolerance, `${a} != ${b}`));
}
function samePose(a, b) {
  near(a.position, b.position);
  assert.ok(new THREE.Quaternion(...a.quaternion).angleTo(new THREE.Quaternion(...b.quaternion)) < 1e-7);
}
function geometryCenter(node) { return new THREE.Box3().setFromObject(node).getCenter(new THREE.Vector3()).toArray(); }
function fixture({ proxyVariant = false, id = ID, size = SIZE, asset = '/pet/rooms/petal-oak-life-v1/room.glb',
  experimental = true, query = experimental ? '?surfacegrasp=1' : '' } = {}) {
  globalThis.location.search = query;
  const scene = new THREE.Group(), room = new THREE.Group(), proxyParent = new THREE.Group(), itemGroup = new THREE.Group();
  room.position.set(2, 3, 4); room.rotation.set(.2, .4, -.1); scene.add(room);
  proxyParent.position.set(...(proxyVariant ? [4, -.2, -2] : [-3, 1, 2]));
  proxyParent.rotation.set(...(proxyVariant ? [.4, -.5, .1] : [-.1, .3, .2]));
  scene.add(proxyParent); proxyParent.add(itemGroup);
  const node = new THREE.Group(); node.name = id;
  node.position.set(.3, .6, -.5); node.rotation.set(.1, -.2, .3);
  const visible = new THREE.Mesh(new THREE.BoxGeometry(...size), new THREE.MeshBasicMaterial());
  visible.position.set(...PIVOT_OFFSET); node.add(visible); room.add(node); scene.updateMatrixWorld(true);
  const authored = { position: node.localToWorld(new THREE.Vector3(...PIVOT_OFFSET)).toArray(),
    quaternion: node.getWorldQuaternion(new THREE.Quaternion()).toArray() };
  const layer = { items: {}, itemGroup, scene: { glb: asset },
    vrm: { humanoid: { getNormalizedBoneNode: () => null } } };
  const props = new Props({ room, scene, layer, cache: { hidden: [], dirty: false } });
  props.asItem(id);
  return { scene, room, proxyParent, itemGroup, node, visible, layer, props, item: layer.items[id], authored };
}
function moved(f) {
  f.props.wake(ID);
  const q = new THREE.Quaternion().setFromEuler(new THREE.Euler(.6, -.4, .2)).toArray();
  setWorld(f.item.mesh, [1.23456789, 2.34567891, -.34567892], q);
  f.props.follow();
  return worldPose(f.item.mesh);
}

test('the production default leaves the reviewed book on the legacy path', () => {
  storage(); const f = fixture({ experimental: false });
  assert.equal(globalThis.location.search, ''); assert.equal(f.props.surfaceGrasps, false);
  assert.equal(f.item.grasp, undefined); assert.equal(f.item.graspHalfExtents, undefined);
  assert.equal(f.props.glb[ID].graspNodePose, null);
});

test('an explicit zero does not enable experimental surface grasps', () => {
  storage(); const f = fixture({ experimental: false, query: '?surfacegrasp=0' });
  assert.equal(f.props.surfaceGrasps, false);
  assert.equal(f.item.grasp, undefined); assert.equal(f.item.graspHalfExtents, undefined);
  assert.equal(f.props.glb[ID].graspNodePose, null);
});

test('the experimental switch cannot opt a matching book in another room into the contract', () => {
  storage(); const f = fixture({ asset: '/pet/rooms/another/room.glb' });
  assert.equal(globalThis.location.search, '?surfacegrasp=1'); assert.equal(f.props.surfaceGrasps, true);
  assert.equal(f.item.grasp, undefined); assert.equal(f.item.graspHalfExtents, undefined);
  assert.equal(f.props.glb[ID].graspNodePose, null);
});

test('reviewed proxy starts at the geometry centre, with authored rotation and a separate noncentral node pivot', () => {
  storage(); const f = fixture();
  assert.ok(f.item.grasp); samePose(worldPose(f.item.mesh), f.authored);
  near(worldPose(f.item.mesh).position, geometryCenter(f.node));
  near(f.props.glb[ID].graspNodePose.position, PIVOT_OFFSET.map(v => -v));
  near(f.item.graspHalfExtents, SIZE.map(v => v / 2), 1e-7);
  assert.ok(f.node.getWorldPosition(new THREE.Vector3()).distanceTo(new THREE.Vector3(...f.authored.position)) > .05);
});

test('follow preserves full proxy/node agreement through distinct rigid parents and reparenting', () => {
  storage(); const f = fixture(), target = moved(f);
  near(geometryCenter(f.node), target.position);
  near(f.node.getWorldQuaternion(new THREE.Quaternion()).toArray(), target.quaternion);
  assert.equal(f.node.parent, f.props.live);
  f.props.toRoom(f.node);
  near(geometryCenter(f.node), target.position);
  f.props.toLive(f.node); f.props.follow();
  near(geometryCenter(f.node), target.position);
});

test('save and reload preserve unrounded world pose even when the proxy parent changes', () => {
  const values = storage(), f = fixture(), target = moved(f);
  f.props._remember(f.props.glb[ID]);
  const saved = JSON.parse(values.get(KEY));
  assert.equal(saved.itemPoses[ID].version, 1); samePose(saved.itemPoses[ID], target);
  const reload = fixture({ proxyVariant: true }); reload.props.restoreItems();
  samePose(worldPose(reload.item.mesh), target); near(geometryCenter(reload.node), target.position);
  assert.equal(reload.item.graspBinding, undefined); assert.equal(reload.item.graspOrigin, undefined);
});

test('reset persists removal of both saved forms, clears attachment state and survives reload', () => {
  const values = storage(), f = fixture(); moved(f); f.props._remember(f.props.glb[ID]);
  f.item.graspBinding = { version: 1, side: 'left', pose: worldPose(f.item.mesh) };
  f.item.graspOrigin = { pose: worldPose(f.item.mesh) };
  f.props.reset(ID);
  samePose(worldPose(f.item.mesh), f.authored); near(geometryCenter(f.node), f.authored.position);
  assert.equal(f.item.graspBinding, undefined); assert.equal(f.item.graspOrigin, undefined);
  const stored = JSON.parse(values.get(KEY));
  assert.equal(stored.items[ID], undefined); assert.equal(stored.itemPoses[ID], undefined);
  const reload = fixture(); reload.props.restoreItems();
  samePose(worldPose(reload.item.mesh), reload.authored); near(geometryCenter(reload.node), reload.authored.position);
});

test('reset refuses to move an item that is still held', () => {
  storage(); const f = fixture(), target = moved(f);
  const binding = { version: 1, side: 'left', pose: target }; f.item.graspBinding = binding; f.item.held = 'left';
  f.props.reset(ID, false);
  samePose(worldPose(f.item.mesh), target); assert.equal(f.item.graspBinding, binding); assert.equal(f.node.visible, true);
});

test('legacy positions remain proxy-parent-local and legacy yaw composes with authored world rotation', () => {
  for (const yaw of [0, .7]) {
    const local = [.5, .8, -.3]; storage({ items: { [ID]: [...local, yaw] } });
    const f = fixture(), position = f.itemGroup.localToWorld(new THREE.Vector3(...local)).toArray();
    const q = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), yaw)
      .multiply(new THREE.Quaternion(...f.authored.quaternion)).toArray();
    f.props.restoreItems();
    samePose(worldPose(f.item.mesh), { position, quaternion: q }); near(geometryCenter(f.node), position);
    assert.ok(new THREE.Vector3(...position).distanceTo(new THREE.Vector3(...local)) > .1);
  }
});

test('an invalid present v1 record restores the complete authored pose instead of applying legacy data partially', () => {
  for (const saved of [null, { version: 2, position: [1, 2, 3], quaternion: [0, 0, 0, 1] },
    { version: 1, position: [1, 2, 3], quaternion: [0, 0, 0, 0] },
    { version: 1, position: [1, null, 3], quaternion: [0, 0, 0, 1] },
    { version: 1, position: [1, 2, 3] }, { version: 1, position: [1, 2, 3], quaternion: [0, 0, 0, 1], scale: [2, 2, 2] }]) {
    storage({ items: { [ID]: [100, 200, 300, 1.2] }, itemPoses: { [ID]: saved } });
    const f = fixture(); f.item.graspBinding = { side: 'left' }; f.item.graspOrigin = {};
    assert.doesNotThrow(() => f.props.restoreItems());
    samePose(worldPose(f.item.mesh), f.authored); near(geometryCenter(f.node), f.authored.position);
    assert.equal(f.item.graspBinding, undefined); assert.equal(f.item.graspOrigin, undefined);
  }
});

test('metadata remains limited to the reviewed asset, object and dimensions', () => {
  for (const options of [{ id: 'IR_Item_Library_02' }, { asset: '/pet/rooms/another/room.glb' }, { size: [.4, .24, .05] }]) {
    storage(); const f = fixture(options); assert.equal(f.item.grasp, undefined);
    assert.equal(f.props.glb[f.item.id].graspNodePose, null);
  }
});
