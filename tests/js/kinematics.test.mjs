import { test } from 'node:test';
import assert from 'node:assert/strict';
import { centerOfMass, convexHull, footPoints, jointChecks, legPenetration, segmentDistance, signedMargin, soleContact } from '../../web/kinematics-core.js';

test('凸包与有符号距离: 里面为正, 外面为负', () => {
  const hull = convexHull([[0, 0], [1, 0], [1, 1], [0, 1], [0.5, 0.5]]);
  assert.equal(hull.length, 4);
  assert.ok(Math.abs(signedMargin(hull, [0.5, 0.5]) - 0.5) < 1e-9);
  assert.ok(Math.abs(signedMargin(hull, [1.2, 0.5]) + 0.2) < 1e-9);
  assert.ok(signedMargin(convexHull([[0, 0], [1, 0]]), [0.5, 0.3]) < 0, '只有一条线段时总是不稳');
});

test('脚的支撑点和质心', () => {
  const pts = footPoints([0, 0.1, 0], [0, 0.02, 0.15], 0.04);
  assert.equal(pts.length, 4);
  assert.ok(pts.some((p) => Math.abs(p[0] - 0.04) < 1e-9));
  const com = centerOfMass({ hips: [0, 1, 0], chest: [0, 1.3, 0], head: [0, 1.6, 0], leftUpperLeg: [0.1, 0.9, 0],
    leftLowerLeg: [0.1, 0.5, 0], leftFoot: [0.1, 0.1, 0], rightUpperLeg: [-0.1, 0.9, 0], rightLowerLeg: [-0.1, 0.5, 0], rightFoot: [-0.1, 0.1, 0] });
  assert.ok(Math.abs(com[0]) < 1e-9 && com[1] > 0.8 && com[1] < 1.2);
});

test('线段最近距离和两腿穿插: 平行、交叉、脚踩到另一条小腿', () => {
  // 两条平行竖直线段, 相距 10 cm
  let r = segmentDistance([0, 0.5, 0], [0, 0.1, 0], [0.1, 0.5, 0], [0.1, 0.1, 0]);
  assert.ok(Math.abs(r.d - 0.1) < 1e-9);
  // 交叉 (X 形): 中点相交, 参数都是 0.5
  r = segmentDistance([-0.1, 0.5, 0], [0.1, 0.1, 0], [0.1, 0.5, 0], [-0.1, 0.1, 0]);
  assert.ok(r.d < 1e-9 && Math.abs(r.s - 0.5) < 1e-9 && Math.abs(r.t - 0.5) < 1e-9);
  const leg = (x, dx = 0) => ({ a: [x, 0.5, 0], b: [x + dx, 0.1, 0], r: 0.04, foot: { a: [x + dx, 0.1, 0], b: [x + dx, 0.05, 0.12], r: 0.035 } });
  assert.equal(legPenetration({ left: leg(0.08), right: leg(-0.08) }), 0, '两腿分开站, 不穿');
  assert.ok(Math.abs(legPenetration({ left: leg(0.03), right: leg(-0.03) }) - 0.02) < 1e-9, '小腿中心线隔 6 cm, 半径各 4 cm: 穿 2 cm');
  assert.ok(legPenetration({ left: leg(0.08, -0.16), right: leg(-0.08, 0.16) }) > 0.07, '两条小腿交叉');
  assert.equal(legPenetration({ left: leg(0.08) }), 0, '少一条腿时不算');
});

test('鞋底的着地部分: 脚放平时含脚跟和脚尖, 踮脚时只剩前掌; 重心在脚跟上方也算站稳', () => {
  // 一只 20 cm 长、8 cm 宽的鞋底, 脚踝在 z = 0, 脚跟到 z = -0.05, 脚尖到 z = 0.15
  const sole = [];
  for (const z of [-0.05, 0, 0.05, 0.1, 0.15]) for (const x of [-0.04, 0.04]) sole.push([x, 0.002, z]);
  const flat = soleContact(sole);
  assert.equal(flat.length, 10);
  const hull = convexHull(flat);
  assert.ok(signedMargin(hull, [0, -0.03]) > 0, '重心在脚跟上方: 在支撑面里');
  assert.ok(signedMargin(convexHull(footPoints([0, 0.1, 0], [0, 0.05, 0.1], 0.04)), [0, -0.03]) < 0, '脚踝到脚尖的长方形没有脚跟');
  // 踮脚: 脚跟抬起 6 cm (高度随 z 线性变化), 只有前掌在 1.5 cm 以内
  const tiptoe = sole.map(([x, , z]) => [x, 0.06 * (0.15 - z) / 0.2, z]);
  const front = soleContact(tiptoe);
  assert.ok(front.length > 0 && front.every(([, z]) => z >= 0.1), JSON.stringify(front));
});

test('关节可行域: 膝盖正常弯曲 vs 反向弯曲 (铰链轴取大腿自己的局部轴, 坐着时也成立)', () => {
  const body = { fwd: [0, 0, 1], up: [0, 1, 0], right: [-1, 0, 0] };
  const axes = { leftLowerLeg: [1, 0, 0] };  // 大腿没有转动时局部 +X 就是世界 +X
  const knee = (foot, hip = [0.1, 0.9, 0], kneeP = [0.1, 0.5, 0]) =>
    jointChecks({ leftUpperLeg: hip, leftLowerLeg: kneeP, leftFoot: foot }, axes, body).find((j) => j.joint === 'leftLowerLeg');
  const bent = knee([0.1, 0.12, -0.15]);
  assert.ok(bent.value > 20 && !bent.violated, `正常屈膝 (脚往后) ${bent.value}`);
  const hyper = knee([0.1, 0.12, 0.15]);
  assert.ok(hyper.value < 0 && hyper.violated, `大腿竖直时脚往前折应判为过伸 ${hyper.value}`);
  const seated = knee([0.1, 0.1, 0.4], [0.1, 0.5, 0], [0.1, 0.5, 0.4]);  // 坐着: 大腿水平向前, 小腿垂下
  assert.ok(seated.value > 80 && !seated.violated, `坐姿屈膝 ${seated.value}`);
  const hip = jointChecks({ leftUpperLeg: [0.1, 0.9, 0], leftLowerLeg: [0.1, 0.5, 0] }, axes, body).find((j) => j.joint === 'leftUpperLeg');
  assert.ok(Math.abs(hip.value) < 1 && !hip.violated, '大腿竖直时髋屈伸约 0 度');
});
