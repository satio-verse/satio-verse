// Opt-in non-rigid grasps. Geometry lives in grasp-transform; this bridge owns
// measured contact, final-pose transitions and world/local Object3D conversion.
import * as THREE from 'three';
import { bind, graspErrors, normalizePose, palmForObject, poseFromPalm, targetPalm, validateGrasp } from './grasp-transform.js';
import { boxDepth, furnitureBoxes } from './pain.js';

export const usesGrasp = item => !!item && !item.physical && item.grasp?.version === 1;
/** A home target identifies its object, even when the other hand is occupied. */
export function putItem(items, target) {
  if (target && Object.hasOwn(target, 'itemId')) return items[target.itemId]?.held ? items[target.itemId] : null;
  return Object.values(items).find(i => i.held === 'right') || Object.values(items).find(i => i.held) || null;
}
export function worldPose(node) {
  node.updateWorldMatrix(true, false);
  return { position: node.getWorldPosition(new THREE.Vector3()).toArray(),
    quaternion: node.getWorldQuaternion(new THREE.Quaternion()).toArray() };
}
export function setWorldPose(node, pose) {
  const p = normalizePose(pose), pos = new THREE.Vector3(...p.position), q = new THREE.Quaternion(...p.quaternion);
  if (node.parent) {
    node.parent.updateWorldMatrix(true, false);
    node.parent.worldToLocal(pos);
    q.premultiply(node.parent.getWorldQuaternion(new THREE.Quaternion()).invert());
  }
  node.position.copy(pos); node.quaternion.copy(q); node.updateWorldMatrix(false, true);
}

/** +Z is the physical palm normal, +Y the projected wrist-to-middle direction. */
export function palmPose(L, side) {
  const hand = L.nodes[`${side}Hand`], mid = L.vrm.humanoid.getNormalizedBoneNode(`${side}MiddleProximal`);
  if (!hand || !mid) throw new Error('抓握需要掌骨和中指根骨');
  hand.updateWorldMatrix(true, true);
  const wrist = L._world(hand), middle = L._world(mid), hq = hand.getWorldQuaternion(new THREE.Quaternion());
  const z = new THREE.Vector3(0, -1, 0).applyQuaternion(hq).normalize();
  const y = middle.clone().sub(wrist); y.addScaledVector(z, -y.dot(z));
  if (y.lengthSq() < 1e-12) throw new Error('抓握掌心坐标退化');
  y.normalize();
  const x = y.clone().cross(z).normalize(); y.copy(z).cross(x).normalize();
  const q = new THREE.Quaternion().setFromRotationMatrix(new THREE.Matrix4().makeBasis(x, y, z));
  return { position: wrist.lerp(middle, 0.6).toArray(), quaternion: q.toArray() };
}

/** Desired wrist/hand pose for an actual palm frame, without modifying bones. */
export function palmAim(L, side, desired) {
  const p = normalizePose(desired), actual = palmPose(L, side), hand = L.nodes[`${side}Hand`];
  const hq = hand.getWorldQuaternion(new THREE.Quaternion()), inv = hq.clone().invert();
  const offset = new THREE.Vector3(...actual.position).sub(L._world(hand)).applyQuaternion(inv);
  const localQ = inv.multiply(new THREE.Quaternion(...actual.quaternion));
  const q = new THREE.Quaternion(...p.quaternion).multiply(localQ.invert());
  return { wrist: new THREE.Vector3(...p.position).sub(offset.applyQuaternion(q)), quaternion: q };
}
export function aimPalm(L, side, desired, weight = 1) {
  const w = Math.max(0, Math.min(1, weight)), aim = palmAim(L, side, desired), hand = L.nodes[`${side}Hand`];
  L._armTo(side, aim.wrist, w);
  const q = hand.getWorldQuaternion(new THREE.Quaternion()).slerp(aim.quaternion, w);
  const parentQ = hand.parent?.getWorldQuaternion(new THREE.Quaternion()) || new THREE.Quaternion();
  hand.quaternion.copy(parentQ.invert().multiply(q)); hand.updateWorldMatrix(false, true);
  L._anchor(side, w);
}

export function taskPalm(task) {
  return task.kind === 'put' ? palmForObject(task.targetPose, task.binding)
    : targetPalm(task.targetPose, task.grasp);
}
export function syncGrasp(L, item) {
  if (!usesGrasp(item) || !item.held || !item.graspBinding) return false;
  if (item.held !== item.graspBinding.side) throw new Error('抓握绑定不能直接换手');
  setWorldPose(item.mesh, poseFromPalm(palmPose(L, item.held), item.graspBinding.pose));
  return true;
}

/** These two transitions intentionally DO NOT set object position/rotation. */
export function bindGrasp(L, item, side) {
  if (!usesGrasp(item) || item.held || item.graspBinding) throw new Error('物件不能重复绑定');
  const pose = worldPose(item.mesh), palm = palmPose(L, side), g = validateGrasp(item.grasp);
  const e = graspErrors(pose, g, palm);
  if (headContact(L).depth > 0) throw new Error('最终头部姿态与家具重叠，不能绑定');
  if (side !== g.preferredHand || Object.values(L.items).some(i => i !== item && i.held === side)
      || e.position > g.positionTolerance || e.angle > g.angleTolerance) throw new Error('掌心还没有接触抓握点');
  L._grabGap(item, side, new THREE.Vector3(...pose.position), true); // legacy centre-gap metric, unchanged
  item.graspOrigin = { on: item.on, pose };
  item.origin = { on: item.on, pos: new THREE.Vector3(...pose.position) };
  item.graspBinding = { version: 1, side, pose: bind(pose, palm) };
  item.held = side; item.on = null;
}
export function releaseGrasp(L, item, target, on) {
  if (!usesGrasp(item) || !item.held || item.graspBinding?.side !== item.held) throw new Error('没有可释放的抓握绑定');
  if (headContact(L).depth > 0) throw new Error('最终头部姿态与家具重叠，不能释放');
  target = normalizePose(target);
  const current = worldPose(item.mesh), g = validateGrasp(item.grasp);
  const angle = new THREE.Quaternion(...current.quaternion).angleTo(new THREE.Quaternion(...target.quaternion));
  const gap = supportGap(item, current, supportGap(item, target, 0));
  if (new THREE.Vector3(...current.position).distanceTo(new THREE.Vector3(...target.position)) > g.positionTolerance
      || angle > (item.grasp.putAngleTolerance ?? Math.PI / 60) || Math.abs(gap) > (item.grasp.supportTolerance ?? 0.003))
    throw new Error('物件尚未到达支撑目标，不能释放');
  L._grabGap(item, item.held, new THREE.Vector3(...target.position), true);
  item.held = null; item.on = on; item.graspBinding = null;
}

/** Signed distance of the lowest object-box corner to the requested support plane. */
export function supportGap(item, pose, surfaceY) {
  const half = item.graspHalfExtents;
  if (!Array.isArray(half) || half.length !== 3 || !half.every(v => Number.isFinite(v) && v > 0)) return Infinity;
  const q = new THREE.Quaternion(...pose.quaternion);
  const extent = half.reduce((sum, h, i) => {
    const axis = new THREE.Vector3(); axis.setComponent(i, 1).applyQuaternion(q);
    return sum + h * Math.abs(axis.y);
  }, 0);
  return pose.position[1] - extent - surfaceY;
}
/** Place an oriented centre-frame box on a horizontal support without reorienting it. */
export function poseOnSupport(item, place, quaternion) {
  const pose = normalizePose({ position: place, quaternion });
  const bottom = supportGap(item, pose, 0);
  if (!Number.isFinite(bottom)) throw new Error('缺少物件支撑几何');
  pose.position[1] += place[1] - bottom;
  return pose;
}

/** Same rotated head sphere and soft allowance as room-body.tick. This is a
 * final contact gate, not evidence that the approach/turn path stayed clear. */
export function headContact(L) {
  const node = L.nodes.head, s = L.hipHeight / 0.78;
  if (!node || !(s > 0)) return { depth: Infinity, source: 'missing-head' };
  const p = L._world(node).add(new THREE.Vector3(0, 0.09 * s, 0)
    .applyQuaternion(node.getWorldQuaternion(new THREE.Quaternion()))), radius = 0.1 * s;
  let depth = 0, source = 'scene', obstacle = null;
  let boxes;
  if (L.graspHeadBoxes) {
    source = 'room-body';
    boxes = L.graspHeadBoxes().map(b => ({ id: b.id, soft: b.soft ?? 0.015,
      min: [b.center[0] - b.size[0] / 2, b.y0, b.center[1] - b.size[1] / 2],
      max: [b.center[0] + b.size[0] / 2, b.y1, b.center[1] + b.size[1] / 2] }));
  } else {
    boxes = furnitureBoxes([...(L.scene?.objects || []), ...(L.scene?.decor || [])]);
  }
  for (const box of boxes) {
    const d = boxDepth(p.toArray(), radius, box) - (box.soft ?? 0.015);
    if (d > depth) { depth = d; obstacle = box.id; }
  }
  return { depth, position: p.toArray(), radius, source, obstacle };
}

export function contactReport(L, task) {
  const object = worldPose(task.item.mesh), palm = palmPose(L, task.side);
  const e = task.kind === 'pick' ? graspErrors(object, task.grasp, palm)
    : { position: new THREE.Vector3(...object.position).distanceTo(new THREE.Vector3(...task.targetPose.position)),
      angle: new THREE.Quaternion(...object.quaternion).angleTo(new THREE.Quaternion(...task.targetPose.quaternion)) };
  const support = task.kind === 'put' ? supportGap(task.item, object, task.surfaceY) : null;
  const positionTolerance = task.grasp.positionTolerance;
  const angleTolerance = task.kind === 'put' ? (task.item.grasp.putAngleTolerance ?? Math.PI / 60) : task.grasp.angleTolerance;
  const supportTolerance = task.item.grasp.supportTolerance ?? 0.003;
  const head = headContact(L);
  const ok = head.depth <= 0 && e.position <= positionTolerance && e.angle <= angleTolerance
    && (task.kind !== 'put' || Math.abs(support) <= supportTolerance);
  const desiredPalm = taskPalm(task);
  const palmPosition = new THREE.Vector3(...palm.position).distanceTo(new THREE.Vector3(...desiredPalm.position));
  const palmAngle = new THREE.Quaternion(...palm.quaternion).angleTo(new THREE.Quaternion(...desiredPalm.quaternion));
  return { kind: task.kind, item: task.item.id, side: task.side, ...e, support, palmPosition, palmAngle, headDepth: head.depth, head,
    positionTolerance, angleTolerance, supportTolerance, ok, object, palm, target: task.targetPose };
}

/** Cancel the operation, not the held object: an established attachment remains valid.
 * Reset discards callbacks silently; user/timeout interruption settles the operation once. */
export function cancelGrasp(L, reason = '抓握动作被取消', notify = true) {
  const task = L._graspTask;
  if (!task) return false;
  L._graspTask = null;
  if (L.reaching?.hold?.graspTask === task) L.reaching = null;
  if (!task.finished) {
    task.ready = false; task.error = reason; task.succeeded = false;
    if (notify) task.finish?.();
    task.finished = true;
  }
  return true;
}

/** Called only after pose filters, grounding and carrySteady; never moves the hand. */
export function finalizeGrasp(L, dt) {
  let changed = false;
  for (const item of Object.values(L.items)) if (syncGrasp(L, item)) changed = true;
  const task = L._graspTask, r = L.reaching;
  if (!task || task.finished || !task.ready || r?.hold?.graspTask !== task || r.fired) return changed;
  task.wait += Math.max(0, dt);
  const report = contactReport(L, task); task.errors = report; L.lastGraspContact = report;
  if (report.ok) {
    try {
      if (task.kind === 'pick') {
        L._graspBind(task.item, task.side);
        (L._pickHand ??= {})[task.side] = { from: L._handPos(task.side), t: 0 };
      } else L._graspRelease(task.item, task.targetPose, task.on);
      r.fired = true; task.succeeded = true; report.outcome = 'contact'; changed = true;
    } catch (e) {
      r.fired = true; task.error = e.message; report.outcome = 'unreachable';
    }
  } else if (task.wait >= 1.2) {
    r.fired = true; task.error = report.headDepth > 0 ? '最终头部姿态与家具重叠，不能完成抓握'
      : task.kind === 'pick' ? '抓握点够不到或掌心朝向未对齐' : '未能在支撑面上放稳';
    report.outcome = 'unreachable';
  }
  return changed; // avatar calls _items once, including its visible-prop follow wrapper
}
