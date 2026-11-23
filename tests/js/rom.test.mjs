import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ROM, SIDEWAYS, TWISTED, elevation, eulerYXZ, hingeAngle, hingeCorrection, keepRatio, limitDirection, rotateAround, twistAngle, v } from '../../web/rom.js';

const near = (a, b, eps = 0.5) => Math.abs(a - b) < eps;
const frame = { f: [0, 0, 1], u: [0, 1, 0], s: [1, 0, 0] };  // 左侧肢体: 外侧 = +X

test('抬起角: 向下 0, 前平举 90, 侧平举 90, 向后为负', () => {
  assert.ok(near(elevation([0, -1, 0], frame).fwd, 0));
  assert.ok(near(elevation([0, 0, 1], frame).fwd, 90));
  assert.ok(near(elevation([1, 0, 0], frame).side, 90));
  assert.ok(near(elevation([0, -1, -1], frame).fwd, -45));
});

test('肩后伸超过 60 度拉回 60, 越过中线超过 45 度拉回 45, 范围内不动', () => {
  const back = rotateAround([0, -1, 0], [1, 0, 0], 80);  // 手臂往后摆 80 度 (向下的向量绕 +X 正转是往后)
  const r = limitDirection(back, frame, { back: ROM.shoulder.ext, cross: ROM.shoulder.add });
  assert.deepEqual(r.hit, ['ext']);
  assert.ok(near(elevation(r.d, frame).fwd, -60), `后伸被拉回 ${elevation(r.d, frame).fwd}`);
  const across = rotateAround([0, -1, 0], [0, 0, 1], -70);  // 越过中线 70 度 (往 -X)
  const r2 = limitDirection(across, frame, { back: 60, cross: 45 });
  assert.ok(r2.hit.includes('add') && near(elevation(r2.d, frame).side, -45));
  const ok = limitDirection([0.3, -1, 0.4], frame, { back: 60, cross: 45 });
  assert.equal(ok.hit.length, 0);
});

test('坐着时大腿水平向前不算外展; 髋前屈上限 120 度, 外展 40 度', () => {
  const seated = [0.12, -0.05, 1];  // 大腿水平向前, 稍微朝外
  assert.equal(limitDirection(seated, frame, { back: 25, cross: 30, fwdMax: 120, sideMax: 40 }).hit.length, 0);
  assert.ok(Math.abs(elevation(seated, frame).side) < 10);
  const up = rotateAround([0, -1, 0], [1, 0, 0], -130);  // 大腿往前抬 130 度
  const r = limitDirection(up, frame, { back: 25, cross: 30, fwdMax: 120, sideMax: 40 });
  assert.ok(r.hit.length > 0);
  const abd = rotateAround([0, -1, 0], [0, 0, 1], 60);  // 外展 60 度
  const r2 = limitDirection(abd, frame, { back: 25, cross: 30, fwdMax: 120, sideMax: 40 });
  assert.ok(r2.hit.includes('abd') && near(elevation(r2.d, frame).side, 40));
});

test('铰链: 膝过伸 20 度修正到允许的 10 度, 屈曲 150 修正到 140', () => {
  const axis = [1, 0, 0], a = [0, -1, 0];  // 大腿竖直, 正常屈膝小腿往后 (-Z)
  const bent = rotateAround(a, axis, 60);
  assert.ok(near(hingeAngle(a, bent, axis), 60), `正常屈膝 ${hingeAngle(a, bent, axis)}`);
  const hyper = rotateAround(a, axis, -20);
  const c = hingeCorrection(a, hyper, axis, ROM.knee);
  assert.ok(near(c.angle, -20) && near(c.fix, 10));
  assert.ok(near(hingeAngle(a, rotateAround(hyper, axis, c.fix), axis), -10));
  const deep = rotateAround(a, axis, 150);
  const c2 = hingeCorrection(a, deep, axis, ROM.knee);
  assert.ok(near(hingeAngle(a, rotateAround(deep, axis, c2.fix), axis), 140));
});

test('铰链: 反着折很深、往侧面弯是上一段扭了 (铰链轴不准), 按屈曲算, 不当过伸去"纠正"', () => {
  const axis = [1, 0, 0], a = [0, -1, 0];
  // 反着折 163 度: 肘、膝不可能过伸这么多, 是上一段转了半圈 (手举到头后面时动捕的上臂常这样)
  const twisted = rotateAround(a, axis, -163);
  assert.ok(near(hingeAngle(a, twisted, axis), 163));
  const c = hingeCorrection(a, twisted, axis, ROM.elbow);
  assert.ok(near(c.fix, -(163 - ROM.elbow.flex)), `只把屈曲收回到上限, 不是转 150 度: ${c.fix}`);
  // 往侧面弯 35 度 (弯曲平面和铰链轴差 90 度): 不算过伸
  const sideways = rotateAround(a, [0, 0, 1], 35);
  assert.ok(near(hingeAngle(a, sideways, axis), 35));
  assert.equal(hingeCorrection(a, sideways, axis, ROM.elbow).fix, 0);
  // 真的过伸 (反着折 20 度, 平面对得上) 照样算
  assert.ok(near(hingeAngle(a, rotateAround(a, axis, -20), axis), -20));
  assert.ok(TWISTED >= 30 && SIDEWAYS > 0 && SIDEWAYS < 90);
});

test('扭转角、欧拉角和按比例收回', () => {
  const h = (30 * Math.PI) / 360;
  assert.ok(near(twistAngle([Math.sin(h), 0, 0, Math.cos(h)], [1, 0, 0]), 30));
  const yaw = (40 * Math.PI) / 360;
  assert.ok(near(eulerYXZ([0, Math.sin(yaw), 0, Math.cos(yaw)]).yaw, 40));
  assert.ok(near(keepRatio({ pitch: 100 }, { pitch: [-55, 50] }), 0.5, 1e-6));
  assert.equal(keepRatio({ pitch: 20, yaw: 10 }, { pitch: [-55, 50], yaw: 80 }), 1);
  assert.ok(v.dot(rotateAround([0, 0, 1], [0, 1, 0], 90), [1, 0, 0]) > 0.999);
});
