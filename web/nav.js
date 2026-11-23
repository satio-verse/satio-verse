// 可行走区域与寻路 (纯函数, 不依赖 three.js, 方便在 Node 里测试).
//
// 场景坐标: 米, 地面是 x-z 平面. 可行走区域 = 地面多边形内, 离墙和障碍物 (按身体半径外扩) 都够远的格子.
// 寻路: 8 邻接 A* (不允许斜穿墙角), 再沿视线把折线拉直.

const SQRT2 = Math.SQRT2;

function pointInPolygon(x, z, poly) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, zi] = poly[i], [xj, zj] = poly[j];
    if ((zi > z) !== (zj > z) && x < ((xj - xi) * (z - zi)) / (zj - zi) + xi) inside = !inside;
  }
  return inside;
}

function distToSegment(x, z, [ax, az], [bx, bz]) {
  const dx = bx - ax, dz = bz - az;
  const t = Math.max(0, Math.min(1, ((x - ax) * dx + (z - az) * dz) / (dx * dx + dz * dz || 1)));
  return Math.hypot(x - (ax + t * dx), z - (az + t * dz));
}

/** 障碍物脚印 (轴对齐矩形) 到点的距离, 点在里面时为 0. */
function distToBox(x, z, { center: [cx, cz], size: [sx, sz] }) {
  const dx = Math.max(0, Math.abs(x - cx) - sx / 2), dz = Math.max(0, Math.abs(z - cz) - sz / 2);
  return Math.hypot(dx, dz);
}

/** 挡路的物体: 有 box, 不是虚拟点、不是挂在墙上、没标记可走 (放在桌上的台灯). */
export function blockers(scene) {
  return [...scene.objects, ...(scene.decor || [])].filter((o) => o.box && !o.virtual && !o.wall && !o.walkable);
}

export function buildGrid(scene, cell = 0.1) {
  const xs = scene.floor.map((p) => p[0]), zs = scene.floor.map((p) => p[1]);
  const x0 = Math.min(...xs), z0 = Math.min(...zs);
  const w = Math.ceil((Math.max(...xs) - x0) / cell), h = Math.ceil((Math.max(...zs) - z0) / cell);
  const r = scene.agentRadius ?? 0.3;
  const walls = scene.floor.map((p, i) => [p, scene.floor[(i + 1) % scene.floor.length]]);
  const boxes = blockers(scene).map((o) => o.box);
  const open = new Uint8Array(w * h);
  for (let j = 0; j < h; j++) {
    for (let i = 0; i < w; i++) {
      const x = x0 + (i + 0.5) * cell, z = z0 + (j + 0.5) * cell;
      if (!pointInPolygon(x, z, scene.floor)) continue;
      if (walls.some(([a, b]) => distToSegment(x, z, a, b) < r)) continue;
      if (boxes.some((b) => distToBox(x, z, b) < r)) continue;
      open[j * w + i] = 1;
    }
  }
  return { x0, z0, w, h, cell, open };
}

export const toCell = (g, x, z) => [Math.floor((x - g.x0) / g.cell), Math.floor((z - g.z0) / g.cell)];
export const toWorld = (g, i, j) => [g.x0 + (i + 0.5) * g.cell, g.z0 + (j + 0.5) * g.cell];
export const walkable = (g, i, j) => i >= 0 && j >= 0 && i < g.w && j < g.h && g.open[j * g.w + i] === 1;

/** 离 (x, z) 最近的可走格子中心; 本身可走就原样返回. */
export function nearestWalkable(g, x, z) {
  const [ci, cj] = toCell(g, x, z);
  if (walkable(g, ci, cj)) return [x, z];
  let best = null, bestD = Infinity;
  for (let rad = 1; rad < Math.max(g.w, g.h); rad++) {
    for (let j = cj - rad; j <= cj + rad; j++) {
      for (let i = ci - rad; i <= ci + rad; i++) {
        if (Math.max(Math.abs(i - ci), Math.abs(j - cj)) !== rad || !walkable(g, i, j)) continue;
        const [wx, wz] = toWorld(g, i, j);
        const d = Math.hypot(wx - x, wz - z);
        if (d < bestD) { bestD = d; best = [wx, wz]; }
      }
    }
    if (best && bestD < rad * g.cell) return best;  // 再往外的环不会更近
  }
  return best;
}

/** 两点之间的线段是否全程可走 (按半格步长采样). */
export function lineOfSight(g, [ax, az], [bx, bz]) {
  const n = Math.ceil(Math.hypot(bx - ax, bz - az) / (g.cell * 0.5));
  for (let k = 0; k <= n; k++) {
    const t = n ? k / n : 0;
    const [i, j] = toCell(g, ax + (bx - ax) * t, az + (bz - az) * t);
    if (!walkable(g, i, j)) return false;
  }
  return true;
}

class Heap {
  constructor() { this.a = []; }
  push(item, key) {
    const a = this.a; a.push([key, item]);
    let i = a.length - 1;
    while (i > 0) { const p = (i - 1) >> 1; if (a[p][0] <= a[i][0]) break; [a[p], a[i]] = [a[i], a[p]]; i = p; }
  }
  pop() {
    const a = this.a, top = a[0], last = a.pop();
    if (a.length) {
      a[0] = last;
      let i = 0;
      for (;;) {
        const l = 2 * i + 1, r = l + 1;
        let m = i;
        if (l < a.length && a[l][0] < a[m][0]) m = l;
        if (r < a.length && a[r][0] < a[m][0]) m = r;
        if (m === i) break;
        [a[m], a[i]] = [a[i], a[m]]; i = m;
      }
    }
    return top[1];
  }
  get size() { return this.a.length; }
}

/** A* 寻路, 返回拉直后的折线 [[x, z], ...] (含起点终点); 不可达时返回 null. 起终点不可走时先吸附到最近可走处. */
export function findPath(g, from, to) {
  const start = nearestWalkable(g, ...from), goal = nearestWalkable(g, ...to);
  if (!start || !goal) return null;
  const [si, sj] = toCell(g, ...start), [gi, gj] = toCell(g, ...goal);
  const N = g.w * g.h, cost = new Float32Array(N).fill(Infinity), prev = new Int32Array(N).fill(-1);
  const heur = (i, j) => { const dx = Math.abs(i - gi), dz = Math.abs(j - gj); return Math.max(dx, dz) + (SQRT2 - 1) * Math.min(dx, dz); };
  const s = sj * g.w + si, goalIdx = gj * g.w + gi;
  cost[s] = 0;
  const heap = new Heap();
  heap.push(s, heur(si, sj));
  const closed = new Uint8Array(N);
  while (heap.size) {
    const cur = heap.pop();
    if (closed[cur]) continue;
    closed[cur] = 1;
    if (cur === goalIdx) break;
    const ci = cur % g.w, cj = (cur / g.w) | 0;
    for (let dj = -1; dj <= 1; dj++) {
      for (let di = -1; di <= 1; di++) {
        if (!di && !dj) continue;
        const ni = ci + di, nj = cj + dj;
        if (!walkable(g, ni, nj)) continue;
        if (di && dj && (!walkable(g, ci + di, cj) || !walkable(g, ci, cj + dj))) continue;  // 不斜穿墙角
        const n = nj * g.w + ni, c = cost[cur] + (di && dj ? SQRT2 : 1);
        if (c < cost[n]) { cost[n] = c; prev[n] = cur; heap.push(n, c + heur(ni, nj)); }
      }
    }
  }
  if (!closed[goalIdx]) return null;
  const cells = [];
  for (let c = goalIdx; c !== -1; c = prev[c]) cells.push(toWorld(g, c % g.w, (c / g.w) | 0));
  cells.reverse();
  cells[0] = start;
  cells[cells.length - 1] = goal;
  return smoothPath(g, cells);
}

/** 拉直: 从当前点出发, 跳到视线能直达的最远点. */
export function smoothPath(g, pts) {
  if (pts.length <= 2) return pts.slice();
  const out = [pts[0]];
  let i = 0;
  while (i < pts.length - 1) {
    let j = pts.length - 1;
    while (j > i + 1 && !lineOfSight(g, pts[i], pts[j])) j--;
    out.push(pts[j]);
    i = j;
  }
  return out;
}

export function pathLength(path) {
  let s = 0;
  for (let i = 1; i < path.length; i++) s += Math.hypot(path[i][0] - path[i - 1][0], path[i][1] - path[i - 1][1]);
  return s;
}

/** 点到折线的最近投影, 返回沿线距离 s. */
export function projectOnPath(path, [x, z]) {
  let best = 0, bestD = Infinity, acc = 0;
  for (let i = 1; i < path.length; i++) {
    const a = path[i - 1], b = path[i];
    const len = Math.hypot(b[0] - a[0], b[1] - a[1]);
    const dx = b[0] - a[0], dz = b[1] - a[1];
    const t = len ? Math.max(0, Math.min(1, ((x - a[0]) * dx + (z - a[1]) * dz) / (len * len))) : 0;
    const d = Math.hypot(x - (a[0] + t * dx), z - (a[1] + t * dz));
    if (d < bestD) { bestD = d; best = acc + t * len; }
    acc += len;
  }
  return best;
}

/** 沿折线距离 s 处的点. */
export function pointOnPath(path, s) {
  let acc = 0;
  for (let i = 1; i < path.length; i++) {
    const a = path[i - 1], b = path[i];
    const len = Math.hypot(b[0] - a[0], b[1] - a[1]);
    if (acc + len >= s && len > 0) {
      const t = (s - acc) / len;
      return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
    }
    acc += len;
  }
  return path[path.length - 1].slice();
}
