import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { jointChecks } from '../../web/kinematics-core.js';
import { accExcess, boxDepth, floorRect, furnitureBoxes, HELD_ACC_LIMIT, jointExcess, PAIN_TERMS, painScore, quatAngle, roomDepth } from '../../web/pain.js';
import { applyParams, PARAM_SPACE, readParams } from '../../web/motion-params.js';
import { LIMITS } from '../../web/pose-filter.js';

test('痛觉: 球插进盒子、出了墙的深度', () => {
  const b = { min: [0, 0, 0], max: [1, 0.5, 1] };
  assert.equal(boxDepth([2, 0.2, 0.5], 0.1, b), 0, '离得远不疼');
  assert.ok(Math.abs(boxDepth([1.05, 0.2, 0.5], 0.1, b) - 0.05) < 1e-9, '贴着面插进 5 cm');
  assert.ok(Math.abs(boxDepth([0.5, 0.45, 0.5], 0.1, b) - 0.15) < 1e-9, '在里面: 到最近一面的距离 + 半径');
  assert.ok(boxDepth([0.5, 0.6, 0.5], 0.1, b) < 1e-9, '球刚好放在台面上 (中心高 = 台面 + 半径) 不算插进去');
  const rect = floorRect([[-3.5, -3], [3.5, -3], [3.5, 2.5], [-3.5, 2.5]]);
  assert.ok(Math.abs(roomDepth([3.45, 1, 0], 0.1, rect) - 0.05) < 1e-9);
  assert.equal(roomDepth([0, 1, 2.6], 0.1, rect), 0, '前方没有墙 (镜头那一侧)');
});

test('痛觉: 场景里哪些算家具', () => {
  const boxes = furnitureBoxes([
    { id: 'sofa', box: { center: [-3, -0.4], size: [0.9, 2.1], height: 0.85 } },
    { id: 'window', wall: 'back', box: { center: [1.3, -3], size: [1.7, 0.04], height: 1.25, base: 0.95 } },
    { id: 'rug', box: { center: [0, 0], size: [2, 2], height: 0.01 } },
    { id: 'ball', item: {}, box: { center: [0, 0], size: [0.2, 0.2], height: 0.2 } },
    { id: 'user', virtual: true },
    { id: 'lamp', box: { center: [-0.8, -2.75], size: [0.22, 0.22], height: 0.5, base: 0.75 } },
  ]);
  assert.deepEqual(boxes.map((b) => b.id), ['sofa', 'lamp']);
  assert.deepEqual(boxes[1].min.map((x) => +x.toFixed(2)), [-0.91, 0.75, -2.86]);
});

test('痛觉: 加权求和、关节超出的度数、四元数夹角', () => {
  assert.equal(painScore({}), 0);
  assert.equal(painScore({ furniture: 0.01, joint: 10 }), 0.01 * PAIN_TERMS.furniture.w + 10 * PAIN_TERMS.joint.w);
  const body = { fwd: [0, 0, 1], up: [0, 1, 0], right: [-1, 0, 0] };
  const knee = (foot) => jointChecks({ leftUpperLeg: [0.1, 0.9, 0], leftLowerLeg: [0.1, 0.5, 0], leftFoot: foot },
    { leftLowerLeg: [1, 0, 0] }, body).find((j) => j.joint === 'leftLowerLeg');
  assert.equal(knee([0.1, 0.12, -0.15]).excess, 0, '正常屈膝不超');
  const hyper = knee([0.1, 0.12, 0.15]);
  assert.ok(hyper.violated && hyper.excess > 5, `过伸超出 ${hyper.excess} 度`);
  assert.equal(jointExcess([{ excess: 3 }, { excess: 0 }, {}]), 3);
  const h = Math.PI / 8;  // 绕 X 转 45 度
  assert.ok(Math.abs(quatAngle([0, 0, 0, 1], [Math.sin(h), 0, 0, Math.cos(h)]) - Math.PI / 4) < 1e-9);
  assert.ok(quatAngle([0, 0, 0, 1], [0, 0, 0, -1]) < 1e-9, '同一个旋转的两种表示');
});

test('痛觉: 拿着东西时手的线加速度 (heldAcc)、洒出来的量 (spill)', () => {
  const dt = 1 / 60;
  // 匀速移动 (每帧 2 cm) 不疼; 静止后一帧突然挪 5 cm (拿放起步那一帧跳) 约 180 m/s², 超出阈值的部分乘 dt
  assert.equal(accExcess([0, 0, 0], [0.02, 0, 0], [0.04, 0, 0], dt), 0);
  const jump = accExcess([0, 1, 0], [0, 1, 0], [0.05, 1, 0], dt);
  assert.ok(Math.abs(jump - (0.05 / (dt * dt) - HELD_ACC_LIMIT) * dt) < 1e-9, `${jump}`);
  // 一步一颠 (竖直 10 m/s²) 在阈值以下
  const bob = 10 * dt * dt;
  assert.equal(accExcess([0, 1, 0], [0, 1 + bob / 2, 0], [0, 1 + 2 * bob, 0], dt), 0);
  assert.ok(PAIN_TERMS.heldAcc && PAIN_TERMS.spill && PAIN_TERMS.grab, '拿放的三项都在痛觉里 (调优按名字读)');
  assert.equal(painScore({ spill: 100 }), 100 * PAIN_TERMS.spill.w);
});

test('调优参数: 按名字读写平滑层和行动层的参数, 名字都对得上', () => {
  const tune = { lockIn: 0.04 };
  const filter = JSON.parse(JSON.stringify(LIMITS));
  const t = { tune, filter };
  const before = applyParams(t, { 'tune.lockIn': 0.08, 'filter.arm.acc': 500, 'filter.core.euro.0': 9 });
  assert.deepEqual(before, { 'tune.lockIn': 0.04, 'filter.arm.acc': LIMITS.arm.acc, 'filter.core.euro.0': LIMITS.core.euro[0] });
  assert.equal(tune.lockIn, 0.08);
  assert.equal(filter.arm.acc, 500);
  assert.equal(filter.core.euro[0], 9);
  assert.throws(() => applyParams(t, { 'tune.nope': 1 }), /没有这个参数/);
  assert.throws(() => applyParams(t, { 'tune.lockIn': NaN }), /不是数/);
  // 搜索空间里的每个名字都在 LIMITS / locomotion.js 的 TUNE 里 (TUNE 依赖 three.js, 按源码文本查), 默认值在范围内
  const src = readFileSync(new URL('../../web/locomotion.js', import.meta.url), 'utf8');
  const tuneBlock = src.slice(src.indexOf('export const TUNE = {'), src.indexOf('};', src.indexOf('export const TUNE = {')));
  const contactSrc = readFileSync(new URL('../../web/contact.js', import.meta.url), 'utf8');
  for (const p of PARAM_SPACE) {
    const [root, ...path] = p.key.split('.');
    if (root === 'filter') {
      const v = readParams({ filter: LIMITS }, [p.key])[p.key];
      assert.ok(v >= p.lo && v <= p.hi, `${p.key} 默认 ${v} 不在 [${p.lo}, ${p.hi}]`);
    } else if (root === 'contact') {  // contact.js 也依赖 three.js, 同样按源码文本查
      const m = contactSrc.match(new RegExp(`export const CONTACT = \\{[^}]*\\b${path[0]}: ([\\d.]+)`));
      assert.ok(m, `CONTACT 里没有 ${path[0]}`);
      assert.ok(+m[1] >= p.lo && +m[1] <= p.hi, `${p.key} 默认 ${m[1]} 不在 [${p.lo}, ${p.hi}]`);
    } else {
      const m = tuneBlock.match(new RegExp(`\\b${path[0]}: ([\\d.]+)`));
      assert.ok(m, `TUNE 里没有 ${path[0]}`);
      assert.ok(+m[1] >= p.lo && +m[1] <= p.hi, `${p.key} 默认 ${m[1]} 不在 [${p.lo}, ${p.hi}]`);
    }
  }
});
