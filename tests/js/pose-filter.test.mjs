import { test } from 'node:test';
import assert from 'node:assert/strict';
import { PoseFilter, groupOf, qangle, qslerp } from '../../web/pose-filter.js';

const node = (q = [0, 0, 0, 1], p = [0, 1, 0]) => ({
  quaternion: { x: q[0], y: q[1], z: q[2], w: q[3] }, position: { x: p[0], y: p[1], z: p[2] } });
const aboutX = (deg) => { const h = (deg * Math.PI) / 360; return [Math.sin(h), 0, 0, Math.cos(h)]; };
const set = (n, q) => Object.assign(n.quaternion, { x: q[0], y: q[1], z: q[2], w: q[3] });
const get = (n) => [n.quaternion.x, n.quaternion.y, n.quaternion.z, n.quaternion.w];
const deg = (r) => (r * 180) / Math.PI;

test('四元数: 夹角和短弧插值', () => {
  assert.ok(Math.abs(deg(qangle(aboutX(0), aboutX(90))) - 90) < 1e-6);
  assert.ok(Math.abs(deg(qangle(aboutX(0), qslerp(aboutX(0), aboutX(90), 0.5))) - 45) < 1e-6);
  const neg = aboutX(30).map((x) => -x);  // 同一个旋转的另一种表示
  assert.ok(qangle(aboutX(30), neg) < 1e-6);
  assert.equal(groupOf('leftLowerLeg'), 'leg');
  assert.equal(groupOf('leftIndexProximal'), 'finger');
  assert.equal(groupOf('hips'), 'hips');
});

test('跳变被抑制成有限加速度的连续运动, 几帧后追上', () => {
  const leg = node();
  const f = new PoseFilter({ leftLowerLeg: leg });
  const dt = 1 / 30;
  f.apply(dt);  // 第一帧建立状态
  set(leg, aboutX(120));  // 来源突然跳到 120 度
  const seen = [];
  for (let i = 0; i < 30; i++) {
    set(leg, aboutX(120));
    f.apply(dt);
    seen.push(deg(qangle(aboutX(0), get(leg))));
  }
  assert.ok(seen[0] < 10, `第一帧只能走一小步, 实际 ${seen[0].toFixed(1)} 度`);
  for (let i = 1; i < seen.length; i++) assert.ok(seen[i] >= seen[i - 1] - 1e-6, '单调靠近目标');
  assert.ok(Math.abs(seen.at(-1) - 120) < 0.5, `一秒内追上, 实际 ${seen.at(-1).toFixed(1)}`);
  assert.ok(f.stats.suppressed > 0 && f.stats.byBone.leftLowerLeg > 0);
});

test('逐渐加速的真实动作不受影响 (腿不做平滑, 没有滞后)', () => {
  const leg = node();
  const f = new PoseFilter({ leftUpperLeg: leg });
  const dt = 1 / 30;
  let maxErr = 0;
  for (let i = 0; i < 90; i++) {  // 从静止开始的 2 Hz、0-30 度摆动 (峰值角加速度约 41 rad/s^2, 和快走时的膝关节同量级)
    const target = aboutX(15 * (1 - Math.cos(2 * Math.PI * 2 * i * dt)));
    set(leg, target);
    f.apply(dt);
    maxErr = Math.max(maxErr, deg(qangle(target, get(leg))));
  }
  assert.ok(maxErr < 0.5, `误差 ${maxErr.toFixed(2)} 度`);
  assert.equal(f.stats.suppressed, 0);
});

test('胯骨旋转不做平滑 (腿部 IK 按平滑前的胯骨解, 胯骨滞后会让踩住的脚滑开)', () => {
  const hips = node(), chest = node();
  const f = new PoseFilter({ hips, chest });
  const dt = 1 / 60;
  let hipsErr = 0, chestErr = 0;
  for (let i = 0; i < 60; i++) {  // 3 Hz、±2 度的骨盆晃动 (耸肩时的量级)
    const target = aboutX(2 * Math.sin(2 * Math.PI * 3 * i * dt));
    set(hips, target); set(chest, target);
    f.apply(dt);
    if (i > 10) { hipsErr = Math.max(hipsErr, deg(qangle(target, get(hips)))); chestErr = Math.max(chestErr, deg(qangle(target, get(chest)))); }
  }
  assert.ok(hipsErr < 0.01, `胯骨误差 ${hipsErr.toFixed(3)} 度`);
  assert.ok(chestErr > 0.05, '胸部照常平滑');
});

test('真人挥手 (上臂内外旋 ±35 度、2.5 Hz, 动捕手势的量级) 不被当成跳变', () => {
  const arm = node();
  const f = new PoseFilter({ rightUpperArm: arm });
  const dt = 1 / 60;
  for (let i = 0; i < 120; i++) {  // 从静止开始, 峰值角加速度约 150 rad/s^2
    set(arm, aboutX(35 * Math.sin(2 * Math.PI * 2.5 * i * dt) * Math.min(1, i / 10)));
    f.apply(dt);
  }
  assert.equal(f.stats.suppressed, 0);
  const flip = node();  // IK 翻转: 一帧转 90 度, 仍然被抑制
  const g = new PoseFilter({ rightLowerArm: flip });
  g.apply(dt);
  set(flip, aboutX(90));
  g.apply(dt);
  assert.ok(deg(qangle(aboutX(0), get(flip))) < 10 && g.stats.suppressed === 1);
});

test('上半身的细小抖动被平滑掉, 胯骨位置跳变被抑制', () => {
  const arm = node(), hips = node([0, 0, 0, 1], [0, 0.8, 0]);
  const f = new PoseFilter({ leftUpperArm: arm, hips });
  const dt = 1 / 30;
  let rawJitter = 0, outJitter = 0, prevRaw = aboutX(0), prevOut = aboutX(0);
  for (let i = 0; i < 60; i++) {
    const raw = aboutX(i % 2 ? 1.5 : -1.5);  // 每帧来回 3 度的求解器抖动
    set(arm, raw);
    f.apply(dt);
    if (i > 10) { rawJitter += qangle(prevRaw, raw); outJitter += qangle(prevOut, get(arm)); }
    prevRaw = raw; prevOut = get(arm);
  }
  assert.ok(outJitter < rawJitter * 0.6, `平滑后抖动 ${deg(outJitter).toFixed(1)} vs ${deg(rawJitter).toFixed(1)}`);
  hips.position.y = 0.5;  // 一帧下沉 30 cm
  f.apply(dt);
  assert.ok(hips.position.y > 0.75, `第一帧只下沉一点, 实际 ${hips.position.y.toFixed(3)}`);
  for (let i = 0; i < 30; i++) { hips.position.y = 0.5; f.apply(dt); }
  assert.ok(Math.abs(hips.position.y - 0.5) < 0.005);
});

test('关掉时原样通过', () => {
  const leg = node();
  const f = new PoseFilter({ leftLowerLeg: leg }, { enabled: false });
  set(leg, aboutX(120));
  f.apply(1 / 30);
  assert.ok(Math.abs(deg(qangle(aboutX(120), get(leg)))) < 1e-6);
});
