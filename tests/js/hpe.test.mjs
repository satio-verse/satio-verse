import { test } from 'node:test';
import assert from 'node:assert/strict';
import { LM, countSwings, describe, summarize } from '../../web/hpe-core.js';

/** 一个站着、面对镜头的人 (图像归一化坐标, y 向下). */
function person({ raise = null, sit = false, wristX = null } = {}) {
  const lm = Array.from({ length: 33 }, () => ({ x: 0.5, y: 0.5, z: 0, visibility: 0.9 }));
  const set = (i, x, y) => { lm[i] = { x, y, z: 0, visibility: 0.95 }; };
  set(LM.nose, 0.5, 0.2);
  set(LM.lShoulder, 0.6, 0.35); set(LM.rShoulder, 0.4, 0.35);  // 被拍的人的左肩在图像右边
  set(LM.lWrist, 0.65, 0.6); set(LM.rWrist, 0.35, 0.6);
  set(LM.lHip, 0.57, 0.65); set(LM.rHip, 0.43, 0.65);
  if (sit) { set(LM.lKnee, 0.75, 0.68); set(LM.rKnee, 0.25, 0.68); } else { set(LM.lKnee, 0.58, 0.85); set(LM.rKnee, 0.42, 0.85); }
  if (raise === 'right' || raise === 'both') set(LM.rWrist, wristX ?? 0.35, 0.15);
  if (raise === 'left' || raise === 'both') set(LM.lWrist, 0.65, 0.15);
  return lm;
}

test('站着/坐着、举哪只手、看不见人', () => {
  assert.deepEqual(summarize(person(), 0), { present: true, raised: 'none', waving: false, facing: true, posture: 'standing' });
  assert.equal(summarize(person({ sit: true }), 0).posture, 'sitting');
  assert.equal(summarize(person({ raise: 'right' }), 0).raised, 'right');
  assert.equal(summarize(person({ raise: 'both' }), 0).raised, 'both');
  const noKnees = person();
  noKnees[LM.lKnee].visibility = 0.1; noKnees[LM.rKnee].visibility = 0.1;
  assert.equal(summarize(noKnees, 0).posture, 'unknown');
  assert.equal(summarize([], 0).present, false);
  assert.equal(describe(summarize(person({ raise: 'right' }), 0)), '站着 · 举着右手');
  const side = person();  // 侧过身: 两肩在图像里挨得很近
  side[LM.lShoulder] = { x: 0.52, y: 0.35, z: 0, visibility: 0.9 }; side[LM.rShoulder] = { x: 0.48, y: 0.35, z: 0, visibility: 0.9 };
  assert.equal(summarize(side, 0).facing, false);
});

test('挥手: 举着的手腕 1.5 秒里左右往返 3 次以上', () => {
  const track = { left: [], right: [] };
  let s;
  for (let i = 0; i < 40; i++) {  // 30 fps, 每 0.2 秒换一次方向
    const t = i / 30, x = 0.35 + (Math.floor(t / 0.2) % 2 ? 0.08 : -0.08);
    s = summarize(person({ raise: 'right', wristX: x }), t, track);
  }
  assert.ok(s.waving, '应判为挥手');
  const still = { left: [], right: [] };
  for (let i = 0; i < 40; i++) s = summarize(person({ raise: 'right' }), i / 30, still);
  assert.ok(!s.waving && s.raised === 'right', '举着不动不算挥手');
  assert.equal(countSwings([{ t: 0, x: 0 }, { t: 0.1, x: 1 }, { t: 0.2, x: 0 }, { t: 0.3, x: 1 }], 0.3), 2);
});
