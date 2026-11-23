/** Pure rigid grasp geometry: metres, right-handed axes, quaternion [x, y, z, w].
 * Pose = { position: [x,y,z], quaternion: [x,y,z,w] }; all results are fresh plain data.
 * Column-vector convention: T_A_B maps coordinates in B into A; composePose(a,b)
 * means T_A_B * T_B_C (apply b first). Positions/anchors use the object-centred frame,
 * NOT an arbitrary GLB node pivot. Preserve a separate object-to-node pose for that pivot.
 * Scale/shear are unsupported: bake scale into geometry and local anchors beforehand.
 * Finite nonzero quaternions are normalized; invalid coordinates/degenerate frames throw.
 * No contact gate, renderer, IK, physics or implicit snapping lives in this module.
 */

function vector(value, length, name) {
  if (!Array.isArray(value) || value.length !== length || !Array.from(value).every(Number.isFinite)) {
    throw new TypeError(`${name} must be an array of ${length} finite numbers`);
  }
  return value.slice();
}

function unit(value, length, name) {
  const v = vector(value, length, name), scale = Math.max(...v.map(Math.abs));
  if (scale === 0) throw new RangeError(`${name} must be nonzero`);
  // Scaling first also handles finite quaternions too large/small for a direct squared norm.
  const s = v.map(x => x / scale), n = Math.hypot(...s);
  return s.map(x => x / n);
}

const dot = (a, b) => a.reduce((sum, x, i) => sum + x * b[i], 0);
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const conjugate = q => [-q[0], -q[1], -q[2], q[3]];

function multiply(a, b) {
  const [x, y, z, w] = a, [u, v, t, s] = b;
  return [w * u + x * s + y * t - z * v, w * v - x * t + y * s + z * u,
    w * t + x * v - y * u + z * s, w * s - x * u - y * v - z * t];
}

function rotate(q, v) {
  // Unit quaternion rotation, including a nonzero object-to-node pivot offset.
  const u = q.slice(0, 3), uv = cross(u, v), uuv = cross(u, uv);
  return v.map((x, i) => x + 2 * (q[3] * uv[i] + uuv[i]));
}

/** Validate/copy a rigid pose. No defaults: a missing pose/rotation is a caller error. */
export function normalizePose(pose) {
  if (!pose || typeof pose !== 'object' || Array.isArray(pose)) throw new TypeError('pose must be an object');
  if (pose.scale !== undefined || pose.matrix !== undefined) throw new TypeError('pose must be rigid; scale/matrix are unsupported');
  return { position: vector(pose.position, 3, 'pose.position'), quaternion: unit(pose.quaternion, 4, 'pose.quaternion') };
}

/** Compose T_A_B and T_B_C into T_A_C. */
export function composePose(a, b) {
  const A = normalizePose(a), B = normalizePose(b), p = rotate(A.quaternion, B.position);
  return normalizePose({ position: p.map((x, i) => x + A.position[i]), quaternion: multiply(A.quaternion, B.quaternion) });
}

/** Invert T_A_B into T_B_A. */
export function inversePose(pose) {
  const P = normalizePose(pose), q = conjugate(P.quaternion);
  return normalizePose({ position: rotate(q, P.position.map(x => -x)), quaternion: q });
}

/** Transform a local point, including translation. For a direction use position:[0,0,0]. */
export function transformPoint(pose, point) {
  const P = normalizePose(pose), p = rotate(P.quaternion, vector(point, 3, 'point'));
  return vector(p.map((x, i) => x + P.position[i]), 3, 'transformed point');
}

function tolerance(value, fallback, name, max = Infinity) {
  const n = value === undefined ? fallback : value;
  if (!Number.isFinite(n) || n < 0 || n > max) throw new RangeError(`${name} is outside its finite nonnegative range`);
  return n;
}

/** Copy the v1 geometric metadata, normalizing its frame. Other metadata is not copied.
 * normal is the object's OUTWARD surface normal; up is an object-local tangent hint.
 * A nonperpendicular hint is projected onto the tangent plane; a parallel hint is invalid.
 * Tolerances are metres/radians, returned as data only; callers must enforce contact gates.
 */
export function validateGrasp(grasp) {
  if (!grasp || typeof grasp !== 'object' || Array.isArray(grasp) || grasp.version !== 1) throw new TypeError('grasp.version must be 1');
  const point = vector(grasp.point, 3, 'grasp.point');
  const normal = unit(grasp.normal, 3, 'grasp.normal'), hint = unit(grasp.up, 3, 'grasp.up');
  const projection = dot(hint, normal), tangent = hint.map((x, i) => x - projection * normal[i]);
  if (Math.hypot(...tangent) <= 1e-8) throw new RangeError('grasp.up must not be parallel to grasp.normal');
  const preferredHand = grasp.preferredHand === undefined ? 'left' : grasp.preferredHand;
  if (preferredHand !== 'left' && preferredHand !== 'right') throw new TypeError('grasp.preferredHand must be left or right');
  return { version: 1, point, normal, up: unit(tangent, 3, 'grasp.up'), preferredHand,
    positionTolerance: tolerance(grasp.positionTolerance, 0.025, 'grasp.positionTolerance'),
    angleTolerance: tolerance(grasp.angleTolerance, Math.PI / 9, 'grasp.angleTolerance', Math.PI) };
}

// Quaternion of a right-handed orthonormal frame whose basis vectors are matrix columns.
function quaternionFromAxes(x, y, z) {
  const m00 = x[0], m01 = y[0], m02 = z[0], m10 = x[1], m11 = y[1], m12 = z[1];
  const m20 = x[2], m21 = y[2], m22 = z[2], trace = m00 + m11 + m22;
  let q, s;
  if (trace > 0) {
    s = 2 * Math.sqrt(trace + 1);
    q = [(m21 - m12) / s, (m02 - m20) / s, (m10 - m01) / s, s / 4];
  } else if (m00 > m11 && m00 > m22) {
    s = 2 * Math.sqrt(1 + m00 - m11 - m22);
    q = [s / 4, (m01 + m10) / s, (m02 + m20) / s, (m21 - m12) / s];
  } else if (m11 > m22) {
    s = 2 * Math.sqrt(1 + m11 - m00 - m22);
    q = [(m01 + m10) / s, s / 4, (m12 + m21) / s, (m02 - m20) / s];
  } else {
    s = 2 * Math.sqrt(1 + m22 - m00 - m11);
    q = [(m02 + m20) / s, (m12 + m21) / s, s / 4, (m10 - m01) / s];
  }
  return unit(q, 4, 'grasp frame quaternion');
}

/** Desired world palm pose at the object's surface anchor.
 * Palm origin is the caller's measured contact centre, not necessarily the wrist bone.
 * Palm +Z points OUT of the palm TOWARDS the object (= -object outward normal).
 * Palm +Y follows the tangent up hint, and +X = +Y cross +Z. A wrist/VRM-bone adapter
 * must convert its local axes into this frame; raw wrist quaternions are not palm poses.
 */
export function targetPalm(objectPose, grasp) {
  const g = validateGrasp(grasp), z = g.normal.map(x => -x), y = g.up, x = cross(y, z);
  return composePose(objectPose, { position: g.point, quaternion: quaternionFromAxes(x, y, z) });
}

/** Capture T_palm_object from ACTUAL world poses. This preserves the binding frame's
 * world pose even with a small residual contact error; it does not snap to targetPalm.
 * Caller must validate contact before binding, and store this immutable plain-data snapshot.
 */
export function bind(objectPose, palmPose) {
  return composePose(inversePose(palmPose), objectPose);
}

/** Follow the actual palm using the saved T_palm_object (never recapture each frame). */
export function poseFromPalm(palmPose, binding) {
  return composePose(palmPose, binding);
}

/** Required palm pose for an object destination, including its original/home quaternion.
 * Q_world_palm = Q_world_object * inverse(Q_palm_object), not Q_world_object alone.
 */
export function palmForObject(targetObjectPose, binding) {
  return composePose(targetObjectPose, inversePose(binding));
}

/** Actual palm-to-anchor position error (metres) and full frame angle error (radians).
 * This is a geometric anchor error, not visible-mesh contact or the legacy proxy-centre gap.
 * Quaternion signs are equivalent; angle is the shortest SO(3) angle in [0, pi].
 */
export function graspErrors(objectPose, grasp, palmPose) {
  const desired = targetPalm(objectPose, grasp), actual = normalizePose(palmPose);
  const delta = multiply(conjugate(desired.quaternion), actual.quaternion);
  const position = Math.hypot(...actual.position.map((x, i) => x - desired.position[i]));
  if (!Number.isFinite(position)) throw new RangeError('grasp position error overflow');
  return { position, angle: 2 * Math.atan2(Math.hypot(...delta.slice(0, 3)), Math.abs(delta[3])) };
}
