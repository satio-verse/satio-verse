import { test } from 'node:test';
import assert from 'node:assert/strict';
import { normalizePose, composePose, inversePose, transformPoint, validateGrasp,
  targetPalm, bind, poseFromPalm, palmForObject, graspErrors } from '../../web/grasp-transform.js';

const I = [0, 0, 0, 1], S = Math.SQRT1_2;
const pose = (position = [0, 0, 0], quaternion = I) => ({ position, quaternion });
const grasp = (extra = {}) => ({ version: 1, point: [0.14, 0, 0], normal: [1, 0, 0], up: [0, 1, 0], ...extra });
function near(actual, expected, eps = 1e-11) {
  assert.equal(actual.length, expected.length);
  actual.forEach((v, i) => assert.ok(Math.abs(v - expected[i]) < eps, `${actual} != ${expected}, component ${i}`));
}
function samePose(a, b) {
  near(a.position, b.position);
  // Compare transformed basis, not quaternion signs or shared normalization internals.
  for (const axis of [[1, 0, 0], [0, 1, 0], [0, 0, 1]]) {
    near(transformPoint(a, axis), transformPoint(b, axis));
  }
}
function freezeDeep(value) {
  if (value && typeof value === 'object') { Object.values(value).forEach(freezeDeep); Object.freeze(value); }
  return value;
}

test('column-vector composition uses xyzw, local translation rotates, and rotation order matters', () => {
  const a = pose([2, 3, 4], [0, 0, S, S]), b = pose([1, 0, 0], [S, 0, 0, S]);
  const combined = composePose(a, b);
  near(combined.position, [2, 4, 4]);
  near(combined.quaternion, [0.5, 0.5, 0.5, 0.5]);
  // Rz(90) Rx(90) cycles local X->Y, Y->Z, Z->X.
  near(transformPoint(combined, [1, 0, 0]), [2, 5, 4]);
  near(transformPoint(combined, [0, 1, 0]), [2, 4, 5]);
  near(transformPoint(combined, [0, 0, 1]), [3, 4, 4]);
  near(transformPoint(composePose(b, a), [0, 1, 0]), [2, -4, 3]);
});

test('inverse cancels a known rotated translation and general poses on both sides', () => {
  const p = pose([2, 3, 4], [0, 0, S, S]);
  near(inversePose(p).position, [-3, 2, -4]);
  near(transformPoint(inversePose(p), [2, 4, 4]), [1, 0, 0]);
  for (let n = 1; n <= 40; n++) {
    const arbitrary = pose([n / 3, 2 - n / 7, -n], [n, 2 - n, n / 7, 0.3]);
    samePose(composePose(arbitrary, inversePose(arbitrary)), pose());
    samePose(composePose(inversePose(arbitrary), arbitrary), pose());
    const point = [0.14, -0.07, n / 10];
    near(transformPoint(inversePose(arbitrary), transformPoint(arbitrary, point)), point);
  }
});

test('object-local surface anchor and outward normal produce the opposite inward palm normal', () => {
  const local = targetPalm(pose(), grasp());
  near(local.position, [0.14, 0, 0]);
  near(transformPoint(local, [0, 0, 1]), [-0.86, 0, 0]);
  near(transformPoint(local, [0, 1, 0]), [0.14, 1, 0]);
  near(transformPoint(local, [1, 0, 0]), [0.14, 0, 1]);
  const world = targetPalm(pose([10, 20, 30], [0, 0, S, S]), grasp());
  near(world.position, [10, 20.14, 30]);
  near(transformPoint(world, [0, 0, 1]), [10, 19.14, 30]);
  near(transformPoint(world, [0, 1, 0]), [9, 20.14, 30]);
});

test('surface frame conversion covers 180-degree branches and projects a tangent hint', () => {
  for (const [normal, up, expectedQ] of [
    [[0, 0, -1], [0, 1, 0], I],
    [[0, 0, 1], [0, -1, 0], [1, 0, 0, 0]],
    [[0, 0, 1], [0, 1, 0], [0, 1, 0, 0]],
    [[0, 0, -1], [0, -1, 0], [0, 0, 1, 0]],
  ]) samePose(targetPalm(pose(), grasp({ point: [0, 0, 0], normal, up })), pose([0, 0, 0], expectedQ));
  const g = validateGrasp(grasp({ normal: [10, 0, 0], up: [1, 3, 0] }));
  near(g.normal, [1, 0, 0]); near(g.up, [0, 1, 0]);
  assert.equal(g.preferredHand, 'left');
  assert.equal(g.positionTolerance, 0.025);
  assert.equal(g.angleTolerance, Math.PI / 9);
});

test('bind preserves actual object pose without snapping away a residual contact error', () => {
  const object = pose([2, 3, 4], [S, 0, 0, S]);
  const desired = targetPalm(object, grasp());
  const actualPalm = pose(desired.position.map((v, i) => v + (i === 0 ? 0.02 : 0)), desired.quaternion);
  const binding = bind(object, actualPalm);
  samePose(poseFromPalm(actualPalm, binding), object);
  assert.ok(Math.abs(graspErrors(object, grasp(), actualPalm).position - 0.02) < 1e-12);
  // Recomputing from an ideal palm would snap the object's centre; this API keeps actual binding.
  near(poseFromPalm(desired, binding).position, [1.98, 3, 4]);
});

test('saved binding follows palm translation and noncommuting rotation', () => {
  const object = pose([3, 1, 0], [S, 0, 0, S]), initialPalm = pose([2, 1, 0]);
  const binding = bind(object, initialPalm);
  near(binding.position, [1, 0, 0]);
  const moved = poseFromPalm(pose([10, 0, 0], [0, 0, S, S]), binding);
  near(moved.position, [10, 1, 0]); near(moved.quaternion, [0.5, 0.5, 0.5, 0.5]);
  near(transformPoint(moved, [0, 0, 1]), [11, 1, 0]);
});

test('home orientation inverts the saved attachment rotation when solving the required palm', () => {
  const binding = pose([1, 0, 0], [S, 0, 0, S]);
  const home = pose([5, 6, 7], [0, 0, S, S]);
  const palm = palmForObject(home, binding);
  near(palm.position, [5, 5, 7]); near(palm.quaternion, [-0.5, -0.5, 0.5, 0.5]);
  samePose(poseFromPalm(palm, binding), home);
});

test('noncentral GLB pivot stays a separate rigid transform and does not shift the grasp anchor', () => {
  const object = pose([2, 3, 4], [0, 0, S, S]);
  const objectToNode = pose([0.2, -0.4, 0.3], [0, S, 0, S]);
  const visible = composePose(object, objectToNode);
  near(visible.position, [2.4, 3.2, 4.3]);
  near(targetPalm(object, grasp()).position, [2, 3.14, 4]);
  const palm = targetPalm(object, grasp()), binding = bind(object, palm);
  samePose(composePose(poseFromPalm(palm, binding), objectToNode), visible);
  samePose(composePose(visible, inversePose(objectToNode)), object);
});

test('binding is invariant under a common rigid change of world frame', () => {
  const object = pose([1.2, -3, 0.4], [0.3, -0.7, 0.2, 1]);
  const palm = pose([-0.4, 1, 2], [-0.2, 0.1, 0.3, 0.8]);
  const worldChange = pose([20, -4, 0.3], [0.6, -0.4, 0.1, 0.8]);
  samePose(bind(composePose(worldChange, object), composePose(worldChange, palm)), bind(object, palm));
  samePose(targetPalm(composePose(worldChange, object), grasp()), composePose(worldChange, targetPalm(object, grasp())));
});

test('contact errors report metres and shortest full-frame radians without a pass/fail policy', () => {
  const object = pose(), desired = targetPalm(object, grasp());
  assert.deepEqual(graspErrors(object, grasp(), desired), { position: 0, angle: 0 });
  const negated = pose(desired.position, desired.quaternion.map(x => -x));
  assert.equal(graspErrors(object, grasp(), negated).angle, 0);
  const turned = composePose(desired, pose([0.03, 0.04, 0], [0, 0, Math.sin(Math.PI / 12), Math.cos(Math.PI / 12)]));
  const error = graspErrors(object, grasp(), turned);
  assert.ok(Math.abs(error.position - 0.05) < 1e-12);
  assert.ok(Math.abs(error.angle - Math.PI / 6) < 1e-12);
  const halfTurn = composePose(desired, pose([0, 0, 0], [1, 0, 0, 0]));
  assert.ok(Math.abs(graspErrors(object, grasp(), halfTurn).angle - Math.PI) < 1e-12);
});

test('inputs remain untouched; outputs are detached plain arrays and quaternions normalize safely', () => {
  const object = freezeDeep(pose([1, 2, 3], [0, 0, 2, 2])), g = freezeDeep(grasp());
  const before = JSON.stringify([object, g]), palm = targetPalm(object, g);
  const binding = freezeDeep(bind(object, palm));
  poseFromPalm(palm, binding); palmForObject(object, binding); graspErrors(object, g, palm);
  const normalized = normalizePose(object); normalized.position[0] = 99;
  const copiedGrasp = validateGrasp(g); copiedGrasp.point[0] = 99;
  assert.equal(JSON.stringify([object, g]), before);
  assert.ok(Array.isArray(palm.position) && Array.isArray(palm.quaternion));
  near(normalizePose(pose([0, 0, 0], [0, 0, 1e308, 1e308])).quaternion, [0, 0, S, S]);
  near(normalizePose(pose([0, 0, 0], [0, 0, 1e-300, 1e-300])).quaternion, [0, 0, S, S]);
});

test('malformed coordinates, rotations, metadata, scale and degenerate frames fail explicitly', () => {
  for (const p of [null, [], {}, pose(Array(3)), pose([0, 0, 0], Array(4)), pose([0, 0]), pose([0, 0, '1']), pose([0, NaN, 0]),
    pose([0, 0, Infinity]), pose([0, 0, 0], [0, 0, 0, 0]), pose([0, 0, 0], [1, 0, 0]),
    pose([0, 0, 0], [1, NaN, 0, 1]), { ...pose(), scale: [1, 1, 1] }, { ...pose(), matrix: [] }]) {
    assert.throws(() => normalizePose(p));
    assert.throws(() => bind(p, pose()));
  }
  for (const g of [null, grasp({ version: 2 }), grasp({ point: [0, Infinity, 0] }),
    grasp({ normal: [0, 0, 0] }), grasp({ normal: [NaN, 1, 0] }), grasp({ up: [0, 0, 0] }),
    grasp({ up: [1, 0, 0] }), grasp({ up: [-1, 0, 0] }), grasp({ up: [1, 1e-10, 0] }),
    grasp({ preferredHand: 'both' }), grasp({ positionTolerance: -1 }), grasp({ angleTolerance: Math.PI + 0.01 }),
    grasp({ positionTolerance: '0.025' }), grasp({ angleTolerance: NaN })]) assert.throws(() => targetPalm(pose(), g));
  assert.throws(() => transformPoint(pose(), [0, null, 0]));
  assert.throws(() => composePose(pose([1e308, 0, 0]), pose([1e308, 0, 0])));
  assert.throws(() => graspErrors(pose([-1e308, 0, 0]), grasp(), pose([1e308, 0, 0])));
});
