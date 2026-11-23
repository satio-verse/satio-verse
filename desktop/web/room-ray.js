// 往下照房间模型 (液体的落点: room-liquid.js 的 _grounder) 的快速版: 结果和 three.js 的 Raycaster.intersectObject(房间, true)
// 再 .find(accept) 一样 (同一批三角形、同样的求交算式和顺序, 逐位相同), 只是不再每次把一路上所有网格的三角形都试一遍.
// 2026-10-27 量的 (知识库「小屋性能实验记录」): 房间 107 万个三角形, 一次没命中缓存的照射 15-17 ms; 端咖啡洒的时候一帧 3-20 次,
// 一帧 70-314 ms, 洒一股 (30 滴) 一帧 500-640 ms, 页面掉到 5 帧.
//
// 做法: 先按包围球在世界坐标里筛掉这一段竖线碰不到的网格; 剩下的网格第一次被照到时, 在它自己的局部坐标里按"射线方向的主轴"
// 把三角形分进二维格子 (另外两根轴上的包围范围落在哪几格). 一次照射: 把这一段射线 (起点到 far) 换到网格局部坐标, 投到格子平面上,
// 只试它扫过的那几格里的三角形. 页面里和原来对拍 3000 次逐位相同; 一次 16.7 ms -> 0.26 ms (第一次建格子时 0.7 ms).
// 斜的网格、射线和主轴偏得多的 (超过约 8 度)、实例化 / 蒙皮 / 有形变的网格照旧交给 three.js 自己的 raycast.
import * as THREE from 'three';

const _inv = new THREE.Matrix4(), _ray = new THREE.Ray(), _a = new THREE.Vector3(), _b = new THREE.Vector3(), _c = new THREE.Vector3();
const _p = new THREE.Vector3(), _pw = new THREE.Vector3(), _o = new THREE.Vector3(), _e = new THREE.Vector3(), _s = new THREE.Sphere();
const AXES = [[1, 2], [0, 2], [0, 1]];  // 主轴 x / y / z 时格子用的两根轴

/** 这个网格能不能用格子 (普通网格; 实例化、蒙皮、批量、正在变形的交给 three.js). */
function plain(o) {
  return o.isMesh && !o.isInstancedMesh && !o.isSkinnedMesh && !o.isBatchedMesh && !(o.geometry?.morphAttributes?.position && o.morphTargetInfluences);
}

/** 按 three.js Mesh._computeIntersections 的顺序列出每个三角形 (起点 j、用哪个材质): 有分组的按组, 再按三角形. */
function triangles(mesh, fn) {
  const g = mesh.geometry, index = g.index, pos = g.attributes.position, dr = g.drawRange, mats = Array.isArray(mesh.material);
  const n = index ? index.count : pos.count;
  if (mats) {
    for (const grp of g.groups) {
      const start = Math.max(grp.start, dr.start), end = Math.min(n, Math.min(grp.start + grp.count, dr.start + dr.count));
      for (let j = start; j < end; j += 3) fn(j, grp.materialIndex);
    }
  } else {
    const start = Math.max(0, dr.start), end = Math.min(n, dr.start + dr.count);
    for (let j = start; j < end; j += 3) fn(j, -1);
  }
}

/** 第 j 个三角形的三个顶点 (局部坐标). */
function verts(g, j) {
  const index = g.index, pos = g.attributes.position;
  const ia = index ? index.getX(j) : j, ib = index ? index.getX(j + 1) : j + 1, ic = index ? index.getX(j + 2) : j + 2;
  _a.fromBufferAttribute(pos, ia); _b.fromBufferAttribute(pos, ib); _c.fromBufferAttribute(pos, ic);
}

/** 一个网格在主轴 axis 上的格子: 三角形按另外两根轴的包围范围分格 (CSR: 每格在 list 里的起止). */
function buildGrid(mesh, axis) {
  const g = mesh.geometry;
  if (!g.boundingBox) g.computeBoundingBox();
  const [u, v] = AXES[axis], bb = g.boundingBox, lo = [bb.min.x, bb.min.y, bb.min.z], hi = [bb.max.x, bb.max.y, bb.max.z];
  let count = 0;
  triangles(mesh, () => { count++; });
  const side = Math.max(1, Math.min(128, Math.round(Math.sqrt(count / 4))));  // 一格平均四个三角形左右
  const nu = side, nv = side, su = Math.max(1e-9, (hi[u] - lo[u]) / nu), sv = Math.max(1e-9, (hi[v] - lo[v]) / nv);
  const cell = (x, s, l, n) => Math.max(0, Math.min(n - 1, Math.floor((x - l) / s)));
  const range = new Int32Array(count * 4);
  let k = 0;
  triangles(mesh, (j) => {
    verts(g, j);
    const A = [_a.x, _a.y, _a.z], B = [_b.x, _b.y, _b.z], C = [_c.x, _c.y, _c.z];
    // 往外放一点 (浮点误差), 保证是超集: 少放一个三角形结果就不一样了, 多放只是多试一次
    const pu = 1e-7 * Math.max(1, Math.abs(lo[u]), Math.abs(hi[u])), pv = 1e-7 * Math.max(1, Math.abs(lo[v]), Math.abs(hi[v]));
    range[k++] = cell(Math.min(A[u], B[u], C[u]) - pu, su, lo[u], nu);
    range[k++] = cell(Math.max(A[u], B[u], C[u]) + pu, su, lo[u], nu);
    range[k++] = cell(Math.min(A[v], B[v], C[v]) - pv, sv, lo[v], nv);
    range[k++] = cell(Math.max(A[v], B[v], C[v]) + pv, sv, lo[v], nv);
  });
  const start = new Int32Array(nu * nv + 1);
  for (let t = 0; t < count; t++) for (let a = range[t * 4]; a <= range[t * 4 + 1]; a++) for (let b = range[t * 4 + 2]; b <= range[t * 4 + 3]; b++) start[a * nv + b + 1]++;
  for (let i = 0; i < nu * nv; i++) start[i + 1] += start[i];
  const fill = start.slice(0, nu * nv), list = new Int32Array(start[nu * nv]), mat = Array.isArray(mesh.material) ? new Int16Array(list.length) : null;
  let t = 0;
  triangles(mesh, (j, m) => {
    for (let a = range[t * 4]; a <= range[t * 4 + 1]; a++) {
      for (let b = range[t * 4 + 2]; b <= range[t * 4 + 3]; b++) {
        const at = fill[a * nv + b]++;
        list[at] = j;
        if (mat) mat[at] = m;
      }
    }
    t++;
  });
  return { axis, u, v, nu, nv, su, sv, lo: [lo[u], lo[v]], start, list, mat, geometry: g, version: g.attributes.position.version, index: g.index?.version ?? -1, count };
}

export class DownRay {
  constructor() {
    this.grids = new WeakMap();
    this.stats = { queries: 0, tested: 0, fallback: 0, built: 0, buildMs: 0, tris: 0 };
    this.raycaster = new THREE.Raycaster();
  }

  _grid(mesh, axis) {
    let gr = this.grids.get(mesh);
    const g = mesh.geometry;
    if (gr && gr.axis === axis && gr.geometry === g && gr.version === g.attributes.position.version && gr.index === (g.index?.version ?? -1)) return gr;
    const t0 = performance.now();
    gr = buildGrid(mesh, axis);
    this.stats.built++;
    this.stats.tris += gr.count;
    this.stats.buildMs += performance.now() - t0;
    this.grids.set(mesh, gr);
    return gr;
  }

  /**
   * 从 from 往下 (世界 -y) 照, far 以内, root 和它下面所有的东西里 accept(object) 为真的最近一处.
   * 返回 {distance, point, object} (同 three.js 的交点, 不带 uv、面), 没有返回 null.
   */
  hit(root, from, far, accept) {
    this.stats.queries++;
    const world = this.raycaster.ray;
    world.origin.copy(from);
    world.direction.set(0, -1, 0);
    this.raycaster.near = 0;
    this.raycaster.far = far;
    let best = null;
    const consider = (h) => { if (h && (!best || h.distance < best.distance)) best = h; };
    const top = from.y, bottom = from.y - far;
    root.traverse((o) => {
      if (!o.isMesh || !o.layers.test(this.raycaster.layers) || o.material === undefined) return;
      // 先按包围球粗筛 (世界坐标): 这一段竖线碰不到包围球的网格不用看 (包围球包住了全部三角形, 筛掉的不可能有交点)
      const g = o.geometry;
      if (g && !o.isInstancedMesh && !o.isSkinnedMesh && !o.isBatchedMesh) {
        if (g.boundingSphere === null) g.computeBoundingSphere();
        _s.copy(g.boundingSphere).applyMatrix4(o.matrixWorld);
        const r = _s.radius * 1.000001 + 1e-6;
        if (Math.hypot(from.x - _s.center.x, from.z - _s.center.z) > r || _s.center.y - r > top || _s.center.y + r < bottom) return;
      }
      if (!accept(o)) return;
      if (!plain(o)) {  // 交给 three.js (结果同样按距离取最近)
        this.stats.fallback++;
        const out = [];
        o.raycast(this.raycaster, out);
        for (const h of out) consider(h);
        return;
      }
      consider(this._mesh(o, far, best ? best.distance : Infinity));
    });
    return best;
  }

  /** 一个网格里最近的交点 (要比 limit 近才算; 同样远时取先列出来的, 和 three.js 排序后的顺序一样). */
  _mesh(mesh, far, limit) {
    const g = mesh.geometry;
    _inv.copy(mesh.matrixWorld).invert();
    _ray.copy(this.raycaster.ray).applyMatrix4(_inv);
    const d = _ray.direction, ad = [Math.abs(d.x), Math.abs(d.y), Math.abs(d.z)];
    const axis = ad[1] >= ad[0] && ad[1] >= ad[2] ? 1 : ad[0] >= ad[2] ? 0 : 2;
    if (ad[axis] < 0.99) {  // 射线在这个网格里斜着走: 交给 three.js
      this.stats.fallback++;
      const out = [];
      mesh.raycast(this.raycaster, out);
      let b = null;
      for (const h of out) if (!b || h.distance < b.distance) b = h;
      return b && b.distance < limit ? b : null;
    }
    const gr = this._grid(mesh, axis);
    // 这一段射线 (起点到 far) 在局部坐标里投到格子平面上扫过的范围
    _o.copy(this.raycaster.ray.origin).applyMatrix4(_inv);
    _e.copy(this.raycaster.ray.origin).addScaledVector(this.raycaster.ray.direction, far).applyMatrix4(_inv);
    const O = [_o.x, _o.y, _o.z], E = [_e.x, _e.y, _e.z];
    const cu = (x) => Math.floor((x - gr.lo[0]) / gr.su), cv = (x) => Math.floor((x - gr.lo[1]) / gr.sv);
    const pu = 1e-7 * Math.max(1, Math.abs(O[gr.u]), Math.abs(E[gr.u])), pv = 1e-7 * Math.max(1, Math.abs(O[gr.v]), Math.abs(E[gr.v]));
    const a0 = Math.max(0, cu(Math.min(O[gr.u], E[gr.u]) - pu)), a1 = Math.min(gr.nu - 1, cu(Math.max(O[gr.u], E[gr.u]) + pu));
    const b0 = Math.max(0, cv(Math.min(O[gr.v], E[gr.v]) - pv)), b1 = Math.min(gr.nv - 1, cv(Math.max(O[gr.v], E[gr.v]) + pv));
    if (a0 > a1 || b0 > b1) return null;
    // 一个三角形可能在好几格里: 按三角形在原来列表里的顺序试 (跨格子去重), 同样远的取先列出来的
    const cand = [];
    for (let a = a0; a <= a1; a++) {
      for (let b = b0; b <= b1; b++) {
        const s = gr.start[a * gr.nv + b], e = gr.start[a * gr.nv + b + 1];
        for (let i = s; i < e; i++) cand.push(i);
      }
    }
    if (a0 !== a1 || b0 !== b1) {
      cand.sort((x, y) => gr.list[x] - gr.list[y] || (gr.mat ? gr.mat[x] - gr.mat[y] : 0));
    }
    let best = null, lastJ = -1, lastM = -2;
    const mats = gr.mat ? mesh.material : null;
    for (const i of cand) {
      const j = gr.list[i], m = gr.mat ? gr.mat[i] : -1;
      if (j === lastJ && m === lastM) continue;  // 同一个三角形在相邻两格里
      lastJ = j; lastM = m;
      const material = mats ? mats[m] : mesh.material;
      verts(g, j);
      this.stats.tested++;
      const hitL = material.side === THREE.BackSide ? _ray.intersectTriangle(_c, _b, _a, true, _p) : _ray.intersectTriangle(_a, _b, _c, material.side === THREE.FrontSide, _p);
      if (hitL === null) continue;
      _pw.copy(_p).applyMatrix4(mesh.matrixWorld);
      const distance = this.raycaster.ray.origin.distanceTo(_pw);
      if (distance < this.raycaster.near || distance > this.raycaster.far) continue;
      if (distance < limit && (!best || distance < best.distance)) best = { distance, point: _pw.clone(), object: mesh };
    }
    return best;
  }
}
