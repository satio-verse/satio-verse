import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DIM, FEATURE_BONES, dbFeatures, nnDistances, pairFeatures, poseDirs, VEL_W } from '../../web/motion-score.js';

const BONES = ['hips', 'chest', 'neck', 'head', 'leftUpperArm', 'leftLowerArm', 'leftHand', 'rightUpperArm', 'rightLowerArm', 'rightHand',
  'leftUpperLeg', 'leftLowerLeg', 'leftFoot', 'leftToes', 'rightUpperLeg', 'rightLowerLeg', 'rightFoot', 'rightToes'];
const CANON = { hips: [0, 1, 0], chest: [0, 1, 0], neck: [0, 1, 0], leftUpperArm: [1, 0, 0], leftLowerArm: [1, 0, 0],
  rightUpperArm: [-1, 0, 0], rightLowerArm: [-1, 0, 0], leftUpperLeg: [0, -1, 0], leftLowerLeg: [0, -1, 0],
  rightUpperLeg: [0, -1, 0], rightLowerLeg: [0, -1, 0], leftFoot: [0, -0.4190, 0.9080], rightFoot: [0, -0.4190, 0.9080] };  // 脚: 和 build_motion_db.py 的 _FOOT 一样

/** T 姿态的关节位置 (角色朝 +Z, +X 是角色的左边). */
const TPOSE = { hips: [0, 1, 0], chest: [0, 1.3, 0], neck: [0, 1.5, 0], head: [0, 1.6, 0],
  leftUpperArm: [0.2, 1.45, 0], leftLowerArm: [0.45, 1.45, 0], leftHand: [0.7, 1.45, 0],
  rightUpperArm: [-0.2, 1.45, 0], rightLowerArm: [-0.45, 1.45, 0], rightHand: [-0.7, 1.45, 0],
  leftUpperLeg: [0.1, 0.95, 0], leftLowerLeg: [0.1, 0.5, 0], leftFoot: [0.1, 0.08, 0], leftToes: [0.1, 0.038, 0.091],
  rightUpperLeg: [-0.1, 0.95, 0], rightLowerLeg: [-0.1, 0.5, 0], rightFoot: [-0.1, 0.08, 0], rightToes: [-0.1, 0.038, 0.091] };

/** 一个 T 姿态的小动捕库 (所有骨骼旋转是单位四元数), 第 2 段左臂慢慢放下. */
function tinyDB() {
  const F = 6, B = BONES.length, quats = new Int16Array(F * B * 4);
  for (let f = 0; f < F; f++) for (let b = 0; b < B; b++) quats[(f * B + b) * 4 + 3] = 32767;
  const la = BONES.indexOf('leftUpperArm');
  for (let f = 3; f < 6; f++) {  // 绕 Z 轴往下转 (f-3)*20 度
    const h = (-(f - 3) * 20 * Math.PI) / 360;
    quats[(f * B + la) * 4 + 2] = Math.round(Math.sin(h) * 32767);
    quats[(f * B + la) * 4 + 3] = Math.round(Math.cos(h) * 32767);
  }
  return { meta: { canon: CANON }, B, bones: BONES, fps: 30, hips: new Float32Array(F * 3).fill(1),
    quats, clips: [{ start: 0, end: 3 }, { start: 3, end: 6 }] };
}

test('像真人: 动捕库的帧和角色身上的同一个姿势, 特征一样', () => {
  const db = tinyDB();
  const ref = dbFeatures(db);
  assert.equal(ref.length, 4 * DIM, '每段最后一帧不取 (没有下一帧算速度)');
  const dirs = poseDirs(TPOSE, 0);
  const q = pairFeatures([{ dirs, prev: dirs, dt: 1 / 30, hipsY: 1 }]);
  const d = nnDistances(ref, q);
  assert.ok(d[0] < 0.02, `T 姿态应该在参照里找到几乎一样的帧, 距离 ${d[0]}`);
  // 角色转了 90 度: 方向在角色朝向坐标系里算, 特征不变
  const turned = Object.fromEntries(Object.entries(TPOSE).map(([k, [x, y, z]]) => [k, [z, y, -x]]));
  const d2 = nnDistances(ref, pairFeatures([{ dirs: poseDirs(turned, Math.PI / 2), prev: poseDirs(turned, Math.PI / 2), dt: 1 / 30, hipsY: 1 }]));
  assert.ok(d2[0] < 0.02, `转身后距离 ${d2[0]}`);
});

test('像真人: 姿势不对、抖得太快都离参照远', () => {
  const ref = dbFeatures(tinyDB());
  const dirs = poseDirs(TPOSE, 0);
  const bent = { ...TPOSE, leftHand: [0.45, 1.7, 0] };  // 小臂竖起来 (参照里没有)
  const far = nnDistances(ref, pairFeatures([{ dirs: poseDirs(bent, 0), prev: poseDirs(bent, 0), dt: 1 / 30, hipsY: 1 }]))[0];
  assert.ok(far > 0.5, `没见过的姿势距离 ${far}`);
  const jitter = dirs.map((d) => [d[0] + 0.05, d[1], d[2]]);  // 1/30 秒里每段方向都晃 0.05
  const shaky = nnDistances(ref, pairFeatures([{ dirs, prev: jitter, dt: 1 / 30, hipsY: 1 }]))[0];
  const expected = Math.sqrt(FEATURE_BONES.length) * 0.05 * 30 * VEL_W;
  assert.ok(shaky > expected * 0.8, `抖动的距离 ${shaky}, 预期约 ${expected.toFixed(2)}`);
});
