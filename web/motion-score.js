// 像不像真人: 拿动捕库 (运动匹配库、待机库、动作库、手势库) 当参照, 量生成的动作离真人数据有多远.
//
// 每帧的特征: 12 段骨骼在角色朝向坐标系里的方向 (单位向量) 和方向的变化速度, 外加髋高. 生成的动作是所有层
// (动捕、IK、接触层、平滑层、落地) 处理后最终的骨骼位置; 参照是动捕库里的帧. 一帧的"离谱程度"是它在参照集里的
// 最近邻距离 (姿态和速度一起比: 姿势像但抖得不像、或者光滑得像机器, 都会离得远).
// 标定: 把每个库里每 5 段留出 1 段不进参照, 留出来的真人帧的平均最近邻距离当"真人水平"; 分数 = 生成动作的平均距离 / 真人水平,
// 1 左右就是和没见过的真人动作一样像, 越大越不像. 以后做强化学习时, 同一套参照集可以当 AMP 判别器的训练数据.
//
// 只有纯函数 (四元数用 [x, y, z, w] 数组), 不依赖 three.js, 可以在 Node 里单测.

// [骨骼, 它指向的下一节]: 动捕库里用骨骼旋转乘标准方向 (meta.canon) 得到方向, 角色身上用两个关节的位置相减
export const FEATURE_BONES = [['hips', 'chest'], ['chest', 'neck'], ['neck', 'head'],
  ['leftUpperArm', 'leftLowerArm'], ['leftLowerArm', 'leftHand'], ['rightUpperArm', 'rightLowerArm'], ['rightLowerArm', 'rightHand'],
  ['leftUpperLeg', 'leftLowerLeg'], ['leftLowerLeg', 'leftFoot'], ['rightUpperLeg', 'rightLowerLeg'], ['rightLowerLeg', 'rightFoot'],
  ['leftFoot', 'leftToes'], ['rightFoot', 'rightToes']];
export const VEL_W = 0.15;   // 方向变化速度 (1/秒) 的权重: 快速挥手时 5-10/秒, 乘上后和方向差 (0-2) 同一量级
export const HIPS_W = 2;     // 髋高 (髋高的倍数) 的权重: 蹲下、坐下时差 0.3-0.5
export const DIM = FEATURE_BONES.length * 6 + 1;

const qrot = (q, v) => {
  const [x, y, z, w] = q, [vx, vy, vz] = v;
  const tx = 2 * (y * vz - z * vy), ty = 2 * (z * vx - x * vz), tz = 2 * (x * vy - y * vx);
  return [vx + w * tx + (y * tz - z * ty), vy + w * ty + (z * tx - x * tz), vz + w * tz + (x * ty - y * tx)];
};

/** 一帧的特征写进 out[o..o+DIM): dirs 是每段骨骼的方向 (角色朝向坐标系), prev 是上一个取样的方向 (没有时速度记 0). */
function write(out, o, dirs, prev, dt, hipsY) {
  let k = o;
  for (let i = 0; i < dirs.length; i++) {
    const d = dirs[i], p = prev ? prev[i] : d;
    out[k++] = d[0]; out[k++] = d[1]; out[k++] = d[2];
    out[k++] = ((d[0] - p[0]) / dt) * VEL_W; out[k++] = ((d[1] - p[1]) / dt) * VEL_W; out[k++] = ((d[2] - p[2]) / dt) * VEL_W;
  }
  out[k] = hipsY * HIPS_W;
}

/** 动捕库的帧 -> 特征. db: mm.js 的 loadDB / loadActions 结果. 每段按 stride 取样; keep(clipIndex) 决定这一段要不要.
 *  速度用相邻帧算 (db.fps), 每段最后一帧不取. 返回 Float32Array (n x DIM). */
export function dbFeatures(db, { stride = 1, keep = () => true } = {}) {
  const canon = db.meta.canon, B = db.B, idx = Object.fromEntries(db.bones.map((n, i) => [n, i]));
  const bones = FEATURE_BONES.filter(([b]) => idx[b] !== undefined && canon[b]);
  const dirsAt = (f) => bones.map(([b]) => {
    const a = (f * B + idx[b]) * 4;
    return qrot([db.quats[a] / 32767, db.quats[a + 1] / 32767, db.quats[a + 2] / 32767, db.quats[a + 3] / 32767], canon[b]);
  });
  const frames = [];
  db.clips.forEach((c, ci) => {
    if (!keep(ci, c)) return;
    for (let f = c.start; f < c.end - 1; f += stride) frames.push(f);
  });
  const out = new Float32Array(frames.length * DIM);
  frames.forEach((f, n) => {
    const d0 = dirsAt(f), d1 = dirsAt(f + 1);
    write(out, n * DIM, d0, null, 1, db.hips[f * 3 + 1]);
    // 速度: 下一帧减这一帧
    for (let i = 0; i < d0.length; i++) for (let j = 0; j < 3; j++) out[n * DIM + i * 6 + 3 + j] = (d1[i][j] - d0[i][j]) * db.fps * VEL_W;
  });
  return out;
}

/** 角色身上的一帧: pos 是骨骼世界位置 ({骨骼: [x,y,z]}), yaw 是角色朝向 (前方 = (sin, cos)), hipsY 是髋高的倍数.
 *  返回各段骨骼的方向 (角色朝向坐标系: +Z 前, +X 角色的左边, 和动捕库一致). */
export function poseDirs(pos, yaw) {
  const c = Math.cos(yaw), s = Math.sin(yaw);
  return FEATURE_BONES.map(([a, b]) => {
    const p = pos[a], q = pos[b];
    if (!p || !q) return [0, 0, 0];
    const x = q[0] - p[0], y = q[1] - p[1], z = q[2] - p[2];
    const rx = x * c - z * s, rz = x * s + z * c, n = Math.hypot(rx, y, rz) || 1;
    return [rx / n, y / n, rz / n];
  });
}

/** 一串取样 [{dirs, hipsY}] (间隔 dt 秒) -> 特征 (n x DIM); 第一帧没有上一帧, 不取. */
export function trackFeatures(samples, dt) {
  const n = Math.max(0, samples.length - 1), out = new Float32Array(n * DIM);
  for (let i = 1; i < samples.length; i++) write(out, (i - 1) * DIM, samples[i].dirs, samples[i - 1].dirs, dt, samples[i].hipsY);
  return out;
}

/** 成对的取样 [{dirs, prev, dt, hipsY}] -> 特征 (n x DIM): 速度用 prev (dt 秒前) 算. 评估时每隔几帧取一对相隔 1/30 秒的,
 *  和动捕库 (30 fps 相邻帧) 的速度口径一致. */
export function pairFeatures(pairs) {
  const out = new Float32Array(pairs.length * DIM);
  pairs.forEach((p, i) => write(out, i * DIM, p.dirs, p.prev, p.dt, p.hipsY));
  return out;
}

/** 每个查询 (n x DIM) 在参照集 (m x DIM) 里的最近邻距离 (欧氏). 暴力搜索, 超过当前最好值就提前放弃这一个. */
export function nnDistances(ref, query) {
  const m = ref.length / DIM, n = query.length / DIM, out = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    const qo = i * DIM;
    let best = Infinity;
    for (let j = 0; j < m; j++) {
      const ro = j * DIM;
      let d = 0;
      for (let k = 0; k < DIM; k++) {
        const t = query[qo + k] - ref[ro + k];
        d += t * t;
        if (d >= best) break;
      }
      if (d < best) best = d;
    }
    out[i] = Math.sqrt(best);
  }
  return out;
}

export const mean = (a) => (a.length ? a.reduce((s, x) => s + x, 0) / a.length : 0);

/** 拼接几个 Float32Array. */
export function concat(parts) {
  const out = new Float32Array(parts.reduce((s, p) => s + p.length, 0));
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
}
