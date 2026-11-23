// 弹簧骨中心节点的逆矩阵缓存 (web/spring-center.js, 2026-10-27, 知识库「小屋性能实验记录」) 和 three-vrm 原来的 Proxy 缓存逐位相同.
// 评审 (「绫音动作成长与自主生活阶段评审」第五节) 要的对拍都在这里: 同一串输入一边走原来的 setter (装 Proxy)、一边走新的, 逐位比:
//   - 装上以后设 center 不再给中心节点的 matrixWorld.elements 装 Proxy (原来的 setter 装);
//   - 随机的矩阵变化 (含写回同样的值、只换零的正负), 随时读;
//   - 矩阵改了以后反复读、只改平移或右下角、奇异矩阵 (缩放 0、两列相同: three.js 求逆得全 0)、NaN / ±Infinity / 次正规数 / 极大值;
//   - 第一次读时矩阵恰好是全 0、全 NaN (缓存初值碰巧相同也得算一次);
//   - 换中心节点 (A → B → A → 不要 → B), 经 three-vrm 自己读逆矩阵的 _getMatrixWorldToCenter;
//   - 弹簧骨链跟着会动的中心跑, 中途换模型 (卸掉一套骨架、再建一套结构不同的), 每根骨头每一步逐位相同.
// 页面里真的换 VRM 的对拍见知识库记录. 以后换 three-vrm 版本时先跑这个 (center 的写法变了 CENTER_CACHE 会是 false).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';
import { types } from 'node:util';

// 'three'、'@pixiv/three-vrm' 指到仓库里的 (页面里是 import map), spring-center.js 要读 location.search
const VENDOR = new URL('../../web/vendor/', import.meta.url).href;
const MAP = { three: `${VENDOR}three/build/three.module.js`, '@pixiv/three-vrm': `${VENDOR}three-vrm/three-vrm.module.min.js` };
register(`data:text/javascript,${encodeURIComponent(`const M = ${JSON.stringify(MAP)};
export async function resolve(s, c, n) { return M[s] ? { url: M[s], shortCircuit: true } : n(s, c); }`)}`);
globalThis.location ??= { search: '' };
const THREE = await import('three');
const { VRMSpringBoneJoint, VRMSpringBoneManager } = await import('@pixiv/three-vrm');
const ORIG = Object.getOwnPropertyDescriptor(VRMSpringBoneJoint.prototype, 'center');  // 装上之前的
const { CENTER_CACHE, InverseCache } = await import('../../web/spring-center.js');

const rng = (seed) => () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; };
/** 逐位相同 (Object.is: 0 和 -0 不同, NaN 和 NaN 相同). */
const same = (a, b) => a.length === b.length && Array.prototype.every.call(a, (x, i) => Object.is(x, b[i]));
/** 原来的 setter 设 center (装 Proxy). */
const origCenter = (joint, node) => ORIG.set.call(joint, node);
const joint = () => new VRMSpringBoneJoint(new THREE.Bone(), null);
/** 两个中心节点: a 用原来的 (Proxy), b 用新的. */
function pair() {
  const a = new THREE.Object3D(), b = new THREE.Object3D();
  origCenter(joint(), a);
  joint().center = b;
  return { a, b, inv: () => [a.userData.inverseCacheProxy.inverse.elements, b.userData.inverseCacheProxy.inverse.elements] };
}

test('装上以后设 center 不装 Proxy; 原来的装', () => {
  assert.equal(CENTER_CACHE, true);
  const { a, b } = pair();
  assert.ok(types.isProxy(a.matrixWorld.elements));
  assert.ok(!types.isProxy(b.matrixWorld.elements));
  assert.ok(b.userData.inverseCacheProxy instanceof InverseCache);
  // 几根弹簧骨共用一个中心、换走再换回来: 都不装
  const j = joint();
  j.center = b; j.center = null; j.center = b;
  assert.ok(!types.isProxy(b.matrixWorld.elements));
});

test('随机的矩阵变化, 随时读逆矩阵都和原来的逐位相同 (2 万次)', () => {
  const rnd = rng(11), { a, b, inv } = pair();
  const q = new THREE.Quaternion(), p = new THREE.Vector3(), s = new THREE.Vector3(), m = new THREE.Matrix4(), e = new THREE.Euler();
  let reads = 0, zeros = 0;
  for (let i = 0; i < 20000; i++) {
    const k = Math.floor(rnd() * 6);
    q.setFromEuler(e.set(rnd() * 6.28, rnd() * 6.28, rnd() * 6.28));
    p.set(rnd() - 0.5, rnd() * 1.6, rnd() - 0.5);
    s.setScalar(0.5 + rnd());
    m.compose(p, q, s);
    const z = Math.floor(rnd() * 16);
    for (const o of [a, b]) {
      const w = o.matrixWorld;
      if (k === 0) w.compose(p, q, s);
      else if (k === 1) w.copy(w);                // 写回同样的值
      else if (k === 2) w.multiplyMatrices(m, w);
      else if (k === 3) w.identity();
      else if (k === 4 && w.elements[z] === 0) w.elements[z] = Object.is(w.elements[z], 0) ? -0 : 0;  // 只换零的正负
    }
    if (k === 4 && b.matrixWorld.elements[z] === 0) zeros++;
    if (rnd() < 0.6) {
      reads++;
      const [ia, ib] = inv();
      assert.ok(same(ia, ib), `第 ${i} 步读到的逆矩阵不同`);
      assert.ok(same(ib, m.copy(b.matrixWorld).invert().elements));
    }
  }
  assert.ok(reads > 10000 && zeros > 100);
});

test('改了以后反复读、奇异矩阵、NaN / ±Infinity / 次正规数 / 极大值: 和原来的逐位相同', () => {
  const { a, b, inv } = pair(), m = new THREE.Matrix4();
  const R = new THREE.Matrix4().makeRotationFromEuler(new THREE.Euler(0.3, -1.1, 0.7)).setPosition(0.1, 0.9, -0.2);
  const steps = [
    ['单位阵', (w) => w.identity()],
    ['正常', (w) => w.copy(R)],
    ['缩放 0 (奇异)', (w) => w.makeScale(0, 1, 1)],
    ['全缩放 0 (奇异)', (w) => w.makeScale(0, 0, 0)],
    ['两列相同 (奇异)', (w) => { w.copy(R); for (let i = 0; i < 4; i++) w.elements[4 + i] = w.elements[i]; }],
    ['回到正常', (w) => w.copy(R)],
    ['一个 NaN', (w) => { w.elements[5] = NaN; }],
    ['同一个 NaN 再写一次', (w) => { w.elements[5] = NaN; }],
    ['再一个 NaN', (w) => { w.elements[12] = NaN; }],
    ['回到正常', (w) => w.copy(R)],
    ['+Infinity', (w) => { w.elements[0] = Infinity; }],
    ['同一个 Infinity 再写一次', (w) => { w.elements[0] = Infinity; }],
    ['-Infinity', (w) => { w.elements[10] = -Infinity; }],
    ['回到正常', (w) => w.copy(R)],
    ['次正规数', (w) => { w.makeScale(5e-324, 1, 1); }],
    ['负的次正规数', (w) => { w.elements[0] = -5e-324; }],
    ['极大值', (w) => { w.makeScale(1e308, 1e-308, 1); }],
    ['-0 换掉 0', (w) => { w.identity(); w.elements[1] = -0; w.elements[14] = -0; }],
    ['写回同样的值', (w) => w.copy(w)],
    ['回到正常', (w) => w.copy(R)],
    ['只改平移', (w) => w.setPosition(0.4, 1.2, 0.3)],
    ['只改右下角', (w) => { w.elements[15] = 2; }],
  ];
  for (const [name, f] of steps) {
    f(a.matrixWorld); f(b.matrixWorld);
    assert.ok(same(a.matrixWorld.elements, b.matrixWorld.elements), `${name}: 两边输入不同`);
    for (let r = 0; r < 3; r++) {  // 改了以后连读三次
      const [ia, ib] = inv();
      assert.ok(same(ia, ib), `${name}: 第 ${r + 1} 次读到的逆矩阵不同 ${[...ia]} / ${[...ib]}`);
      assert.ok(same(ib, m.copy(b.matrixWorld).invert().elements), `${name}: 不是此刻的逆`);
    }
    if (name.includes('奇异')) assert.ok(inv()[1].every((x) => Object.is(x, 0)), `${name}: 应该是全 0`);
  }
});

test('第一次读时矩阵是全 0、全 NaN: 和原来的一样 (原来装上就标"要重算")', () => {
  for (const v of [0, -0, NaN]) {
    const a = new THREE.Object3D(), b = new THREE.Object3D();
    a.matrixWorld.elements.fill(v); b.matrixWorld.elements.fill(v);
    origCenter(joint(), a);
    joint().center = b;
    const ia = a.userData.inverseCacheProxy.inverse.elements, ib = b.userData.inverseCacheProxy.inverse.elements;
    assert.ok(same(ia, ib), `全 ${v}: ${[...ia]} / ${[...ib]}`);
  }
});

test('换中心节点 (A → B → A → 不要 → B): 弹簧骨读到的逆矩阵和原来的逐位相同', () => {
  const rnd = rng(3), side = (orig) => {
    const A = new THREE.Object3D(), B = new THREE.Object3D(), j = joint();
    return { A, B, j, set: (n) => (orig ? origCenter(j, n) : (j.center = n)) };
  };
  const o = side(true), n = side(false), m = new THREE.Matrix4();
  for (const c of ['A', 'B', 'A', null, 'B']) {
    o.set(c && o[c]); n.set(c && n[c]);
    for (let i = 0; i < 50; i++) {
      m.makeRotationY(rnd() * 6.28).setPosition(rnd(), rnd(), rnd());
      for (const s of [o, n]) { s.A.matrixWorld.copy(m); s.B.matrixWorld.copy(m).multiply(m); }
      assert.ok(same(o.j._getMatrixWorldToCenter().elements, n.j._getMatrixWorldToCenter().elements), `中心 ${c} 第 ${i} 次`);
    }
    // 换走的那个: 原来的把 Proxy 撤掉、缓存删掉; 新的也删掉, 一直没有 Proxy
    for (const k of ['A', 'B']) {
      if (k === c) continue;
      assert.equal(!!o[k].userData.inverseCacheProxy, !!n[k].userData.inverseCacheProxy);
      assert.ok(!types.isProxy(n[k].matrixWorld.elements));
    }
  }
});

/** 一个会动的 Hips 下面挂几条"头发", 弹簧骨的中心 = Hips; useOrig 时用原来的 setter. */
function rig(useOrig, { strands = 2, len = 3, seg = 0.12 } = {}) {
  const root = new THREE.Object3D(), hips = new THREE.Object3D(), mgr = new VRMSpringBoneManager();
  root.add(hips);
  for (let k = 0; k < strands; k++) {
    let parent = hips;
    const bones = [];
    for (let i = 0; i <= len; i++) {
      const bone = new THREE.Bone();
      bone.position.set(i ? 0 : (k - (strands - 1) / 2) * 0.08, i ? -seg : 0.5, i ? 0.02 : -0.05);
      parent.add(bone);
      bones.push(bone);
      parent = bone;
    }
    for (let i = 0; i < len; i++) {
      const j = new VRMSpringBoneJoint(bones[i], bones[i + 1], { stiffness: 0.6, dragForce: 0.3, gravityPower: 0.4, hitRadius: 0.02 });
      if (useOrig) origCenter(j, hips); else j.center = hips;
      mgr.addJoint(j);
    }
  }
  root.updateMatrixWorld(true);
  mgr.setInitState();
  return { root, hips, mgr };
}

/** 两套 (原来的 / 新的) 一样地跑 frames 帧, 每帧逐根比. */
function runPair(A, B, frames, seed) {
  const rnd = rng(seed), bonesOf = (r) => { const out = []; r.root.traverse((o) => o.isBone && out.push(o)); return out; };
  const ba = bonesOf(A), bb = bonesOf(B);
  for (let f = 0; f < frames; f++) {
    const yaw = Math.sin(f * 0.05) * 1.2 + rnd() * 0.05, x = Math.sin(f * 0.031) * 0.4, still = f % 50 > 40;  // 有时停住不动
    for (const r of [A, B]) {
      if (!still) { r.hips.rotation.set(0, yaw, Math.sin(f * 0.11) * 0.2); r.hips.position.set(x, 0.9 + Math.sin(f * 0.2) * 0.03, 0); }
      r.root.updateMatrixWorld(true);
      r.mgr.update(1 / 60);
    }
    for (let i = 0; i < ba.length; i++) {
      assert.ok(same(ba[i].quaternion.toArray(), bb[i].quaternion.toArray()), `第 ${f} 帧第 ${i} 根骨头姿态不同`);
      assert.ok(same(ba[i].matrixWorld.elements, bb[i].matrixWorld.elements));
    }
  }
  assert.ok(ba.some((b) => Math.abs(b.quaternion.x) > 0.01), '头发要真的在动');
}

test('弹簧骨链跟着会动的中心跑, 中途换模型: 原来的和新的每根骨头逐位相同', () => {
  const A = rig(true), B = rig(false);
  assert.ok(types.isProxy(A.hips.matrixWorld.elements) && !types.isProxy(B.hips.matrixWorld.elements));
  runPair(A, B, 600, 5);
  // 换模型: 卸掉这一套, 载入结构不同的另一套 (新的中心节点也走新缓存)
  const shape = { strands: 4, len: 5, seg: 0.07 }, C = rig(true, shape), D = rig(false, shape);
  assert.ok(types.isProxy(C.hips.matrixWorld.elements) && !types.isProxy(D.hips.matrixWorld.elements));
  assert.ok(D.hips.userData.inverseCacheProxy instanceof InverseCache);
  runPair(C, D, 400, 9);
});

test('perfoff=proxy、perfoff=all 不装', async () => {
  const keep = globalThis.location;
  try {
    for (const q of ['?perfoff=proxy', '?perfoff=matrix,all']) {
      globalThis.location = { search: q };
      const m = await import(`../../web/spring-center.js${q}`);  // 换个地址 = 重新执行一遍模块
      assert.equal(m.CENTER_CACHE, false);
    }
  } finally {
    globalThis.location = keep;
  }
});
