// 小屋性能优化的两个快速版和原来逐位相同 (2026-10-27, 知识库「小屋性能实验记录」):
//   - desktop/web/room-ray.js 的 DownRay: 往下照房间 == three.js Raycaster.intersectObject(房间, true).find(accept);
//   - desktop/web/fast-matrix.js 的 fastMatrices: 角色根节点的 updateMatrixWorld(true) == three.js 原来的.
// 页面里也对拍过 (room_perf.mjs 的 --patch); 这里用随机场景守着, 以后改这两个文件或换 three.js 版本时先跑这个.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';

// 'three' 指到仓库里的 three.js (页面里是 import map), perf-flags.js 要读 location.search
const THREE_URL = new URL('../../web/vendor/three/build/three.module.js', import.meta.url).href;
register(`data:text/javascript,${encodeURIComponent(`export async function resolve(s, c, n) {
  return s === 'three' ? { url: ${JSON.stringify(THREE_URL)}, shortCircuit: true } : n(s, c); }`)}`);
globalThis.location ??= { search: '' };
const THREE = await import('three');
const { DownRay } = await import('../../desktop/web/room-ray.js');
const { fastMatrices } = await import('../../desktop/web/fast-matrix.js');

const rng = (seed) => () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; };

/** 一间随机的"房间": 地板、各种形状、转了的、斜放的、多材质分组的、实例化的、藏起来的. */
function room(rnd) {
  const root = new THREE.Group();
  const sides = [THREE.FrontSide, THREE.BackSide, THREE.DoubleSide];
  const mat = () => new THREE.MeshBasicMaterial({ side: sides[Math.floor(rnd() * 3)] });
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(6, 6, 12, 12), mat());
  floor.rotation.x = -Math.PI / 2;
  root.add(floor);
  const shelf = new THREE.Group();
  shelf.position.set(0.3, 0.4, -0.2);
  shelf.rotation.y = 0.7;
  root.add(shelf);
  for (let i = 0; i < 40; i++) {
    const k = i % 6;
    const geo = k === 0 ? new THREE.BoxGeometry(0.1 + rnd() * 0.5, 0.05 + rnd() * 0.4, 0.1 + rnd() * 0.5, 3, 2, 3)
      : k === 1 ? new THREE.CylinderGeometry(0.05 + rnd() * 0.2, 0.05 + rnd() * 0.2, 0.1 + rnd() * 0.6, 16, 2)
        : k === 2 ? new THREE.SphereGeometry(0.05 + rnd() * 0.3, 12, 8)
          : k === 3 ? new THREE.TorusGeometry(0.2, 0.05, 8, 16) : new THREE.BoxGeometry(0.3, 0.3, 0.3).toNonIndexed();
    const m = k === 0 ? new THREE.Mesh(geo, [mat(), mat(), mat(), mat(), mat(), mat()]) : new THREE.Mesh(geo, mat());
    m.position.set(-2 + rnd() * 4, rnd() * 1.2, -2 + rnd() * 4);
    m.rotation.y = rnd() * 6.28;
    if (k === 3) m.rotation.x = rnd() * 0.5;      // 斜着的 (有的会交给 three.js 自己算)
    if (k === 5) m.rotation.x = Math.PI / 2;      // 主轴换成局部 z 的
    if (i % 7 === 0) m.scale.set(1 + rnd(), 0.5 + rnd(), 1 + rnd());
    if (i % 11 === 0) m.visible = false;
    if (i % 13 === 0) m.name = 'IR_Item_cup';
    (i % 3 ? root : shelf).add(m);
  }
  const inst = new THREE.InstancedMesh(new THREE.BoxGeometry(0.2, 0.2, 0.2), mat(), 5);
  for (let i = 0; i < 5; i++) inst.setMatrixAt(i, new THREE.Matrix4().makeTranslation(-1 + i * 0.5, 0.1, 1));
  root.add(inst);
  root.updateMatrixWorld(true);
  return root;
}

test('DownRay: 往下照的结果和 three.js 逐位相同 (随机房间, 2000 条)', () => {
  const rnd = rng(7), root = room(rnd), dr = new DownRay(), ray = new THREE.Raycaster(), down = new THREE.Vector3(0, -1, 0), from = new THREE.Vector3();
  const visible = (o) => { for (let x = o; x; x = x.parent) if (!x.visible && x !== root) return false; return true; };
  const accept = (o) => o.isMesh && visible(o) && o.name !== 'IR_Item_cup';
  let hits = 0;
  for (let i = 0; i < 2000; i++) {
    const y = rnd() * 1.8;
    from.set(-2.2 + rnd() * 4.4, y + 0.02, -2.2 + rnd() * 4.4);
    ray.set(from, down);
    ray.far = y + 0.5;
    const a = ray.intersectObject(root, true).find((h) => accept(h.object));
    const b = dr.hit(root, from, ray.far, accept);
    if (!a) { assert.equal(b, null, `第 ${i} 条: three.js 没照到, 快速版照到了`); continue; }
    hits++;
    assert.ok(b, `第 ${i} 条: 快速版没照到`);
    assert.equal(b.object, a.object);
    assert.equal(b.distance, a.distance);
    assert.deepEqual(b.point.toArray(), a.point.toArray());
  }
  assert.ok(hits > 1000, '大多数应该照到地板或东西');
  assert.ok(dr.stats.built > 0 && dr.stats.fallback > 0, '格子和交给 three.js 的两条路都走到');
});

test('fastMatrices: 角色根的 updateMatrixWorld(true) 和 three.js 逐位相同 (随机改动 300 轮)', () => {
  const rnd = rng(11);
  // 两棵一样的树: A 装快速版, B 照旧; 每轮对两棵做同样的改动, 再各自整棵更新, 逐个节点比世界矩阵
  const build = () => {
    const scene = new THREE.Scene(), root = new THREE.Group(), nodes = [root];
    scene.add(root);
    for (let i = 1; i < 160; i++) {
      const n = i % 17 === 0 ? new THREE.Mesh(new THREE.BoxGeometry(0.1, 0.1, 0.1), new THREE.MeshBasicMaterial()) : new THREE.Object3D();
      n.position.set(0.1 * (i % 5), 0.05 * (i % 3), 0.02 * (i % 7));
      if (i % 23 === 0) n.matrixAutoUpdate = false;
      nodes[Math.floor((i - 1) * 0.6)].add(n);
      nodes.push(n);
    }
    scene.updateMatrixWorld(true);
    return { scene, root, nodes };
  };
  const A = build(), B = build();
  assert.ok(fastMatrices(A.root));
  for (let round = 0; round < 300; round++) {
    const k = 1 + Math.floor(rnd() * 8);
    for (let j = 0; j < k; j++) {
      const i = Math.floor(rnd() * A.nodes.length), what = rnd();
      for (const T of [A, B]) {
        const n = T.nodes[i];
        if (what < 0.5) n.quaternion.setFromEuler(new THREE.Euler(round * 0.01, i * 0.1, j * 0.2));
        else if (what < 0.7) n.position.x += 0.01;
        else if (what < 0.8) { n.matrix.makeTranslation(0.1, round * 0.001, 0); n.matrixWorldNeedsUpdate = true; }   // 直接改局部矩阵
        else if (what < 0.9) n.getWorldPosition(new THREE.Vector3());   // 别处走 updateWorldMatrix (把 matrixWorldNeedsUpdate 设成真)
        else n.matrixWorld.elements[13] += 0.5;                          // 别处直接改了世界矩阵 (要被改回来)
      }
    }
    if (round % 10 === 0) for (const T of [A, B]) T.root.position.z += 0.05;  // 根动了: 整棵都要重算
    A.root.updateMatrixWorld(true);
    B.root.updateMatrixWorld(true);
    A.nodes.forEach((n, i) => assert.deepEqual(n.matrixWorld.elements, B.nodes[i].matrixWorld.elements, `第 ${round} 轮第 ${i} 个节点`));
  }
  const st = A.root.__fastMatrix;
  assert.ok(st.recomputed < st.nodes * 0.6, `没变的不重算 (${st.recomputed}/${st.nodes})`);
});
