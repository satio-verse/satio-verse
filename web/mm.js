// 运动匹配核心 (纯函数和类型数组, 不依赖 three.js). 思路参考 Holden 等人的 Learned Motion Matching (2020)
// 与 orangeduck/Motion-Matching (MIT) 的公开实现, 代码是重新写的:
//   - 模拟骨: 用临界阻尼弹簧把角色位置/朝向推向期望速度/朝向, 同一组公式预测未来 1/3, 2/3, 1 秒的轨迹;
//   - 查询向量 = 当前播放帧的姿态特征 + 预测轨迹; 在数据库里找最近的帧 (按 16 帧分块的包围盒剪枝);
//   - 切换时用惯性化 (offset 按弹簧衰减) 消除跳变.
// 数据库由 tools/build_motion_db.py 生成, 格式见那里的说明. 四元数用 [x, y, z, w].

export const BLOCK = 16;
const LN2 = Math.log(2);

// -- 四元数 --------------------------------------------------------------------------------
export function qmul(a, b, out = [0, 0, 0, 1]) {
  const [ax, ay, az, aw] = a, [bx, by, bz, bw] = b;
  out[0] = aw * bx + ax * bw + ay * bz - az * by;
  out[1] = aw * by - ax * bz + ay * bw + az * bx;
  out[2] = aw * bz + ax * by - ay * bx + az * bw;
  out[3] = aw * bw - ax * bx - ay * by - az * bz;
  return out;
}
export const qinv = (q) => [-q[0], -q[1], -q[2], q[3]];
export function qnorm(q) {
  const n = Math.hypot(q[0], q[1], q[2], q[3]) || 1;
  q[0] /= n; q[1] /= n; q[2] /= n; q[3] /= n;
  return q;
}
export function qrot(q, v) {
  const [x, y, z, w] = q;
  const tx = 2 * (y * v[2] - z * v[1]), ty = 2 * (z * v[0] - x * v[2]), tz = 2 * (x * v[1] - y * v[0]);
  return [v[0] + w * tx + y * tz - z * ty, v[1] + w * ty + z * tx - x * tz, v[2] + w * tz + x * ty - y * tx];
}
export const qyaw = (yaw) => [0, Math.sin(yaw / 2), 0, Math.cos(yaw / 2)];
export function qnlerp(a, b, t, out = [0, 0, 0, 1]) {
  const s = a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3] < 0 ? -1 : 1;
  for (let i = 0; i < 4; i++) out[i] = a[i] * (1 - t) + b[i] * s * t;
  return qnorm(out);
}
/** 旋转向量 (轴 * 角度) <-> 四元数. */
export function qlog(q) {
  const s = q[3] < 0 ? -1 : 1;
  const x = q[0] * s, y = q[1] * s, z = q[2] * s, w = q[3] * s;
  const len = Math.hypot(x, y, z);
  if (len < 1e-8) return [2 * x, 2 * y, 2 * z];
  const k = (2 * Math.atan2(len, w)) / len;
  return [x * k, y * k, z * k];
}
export function qexp(v) {
  const a = Math.hypot(v[0], v[1], v[2]);
  if (a < 1e-8) return qnorm([v[0] / 2, v[1] / 2, v[2] / 2, 1]);
  const s = Math.sin(a / 2) / a;
  return [v[0] * s, v[1] * s, v[2] * s, Math.cos(a / 2)];
}
/** 把方向 a 转到方向 b 的最小旋转. */
export function qfromTo(a, b) {
  const la = Math.hypot(...a) || 1, lb = Math.hypot(...b) || 1;
  const ax = a[0] / la, ay = a[1] / la, az = a[2] / la, bx = b[0] / lb, by = b[1] / lb, bz = b[2] / lb;
  const d = ax * bx + ay * by + az * bz;
  if (d < -0.999999) {
    const axis = Math.abs(ax) < 0.9 ? [0, az, -ay] : [-az, 0, ax];
    const n = Math.hypot(...axis);
    return [axis[0] / n, axis[1] / n, axis[2] / n, 0];
  }
  return qnorm([ay * bz - az * by, az * bx - ax * bz, ax * by - ay * bx, 1 + d]);
}
export const wrapAngle = (a) => Math.atan2(Math.sin(a), Math.cos(a));

// -- 数据库 --------------------------------------------------------------------------------
export function loadDB(meta, buffer) {
  const F = meta.frames, B = meta.bones.length, off = meta.offsets;
  const db = {
    meta, F, B, bones: meta.bones, fps: meta.fps,
    feats: new Float32Array(buffer, off.features, F * 27),
    hips: new Float32Array(buffer, off.hips, F * 3),
    root: new Float32Array(buffer, off.root, F * 3),
    quats: new Int16Array(buffer, off.quats, F * B * 4),
    contacts: new Uint8Array(buffer, off.contacts, F * 2),
    mean: Float32Array.from(meta.featureMean), scale: Float32Array.from(meta.featureScale),
    clipOf: new Int32Array(F), clips: meta.clips,
  };
  meta.clips.forEach((c, i) => db.clipOf.fill(i, c.start, c.end));
  // 可搜索的帧: 未来 1 秒的轨迹还在同一段里; 按 16 帧分块记录包围盒, 搜索时整块剪枝
  const horizon = Math.max(...meta.horizon);
  const blocks = [];
  for (const c of meta.clips) {
    for (let s = c.start; s < c.end - horizon - 1; s += BLOCK) {
      const e = Math.min(s + BLOCK, c.end - horizon - 1);
      const lo = new Float32Array(27).fill(Infinity), hi = new Float32Array(27).fill(-Infinity);
      for (let f = s; f < e; f++) {
        for (let k = 0; k < 27; k++) { const v = db.feats[f * 27 + k]; if (v < lo[k]) lo[k] = v; if (v > hi[k]) hi[k] = v; }
      }
      blocks.push({ s, e, lo, hi });
    }
  }
  db.blocks = blocks;
  return db;
}

/** 物体交互动作库 (坐下/站起/捡/放/伸手): 和运动匹配库同样的姿态格式, 没有匹配特征, 按片段编号直接播放. */
export function loadActions(meta, buffer) {
  const F = meta.frames, B = meta.bones.length, off = meta.offsets;
  const db = {
    meta, F, B, bones: meta.bones, fps: meta.fps, clips: meta.clips, groups: meta.groups,
    hips: new Float32Array(buffer, off.hips, F * 3),
    root: new Float32Array(buffer, off.root, F * 3),
    quats: new Int16Array(buffer, off.quats, F * B * 4),
    contacts: new Uint8Array(buffer, off.contacts, F * 2),
    clipOf: new Int32Array(F),
  };
  meta.clips.forEach((c, i) => db.clipOf.fill(i, c.start, c.end));
  return db;
}

/** 第 f 帧 (可带小数, 同一段内插值) 的姿态: 骨骼旋转 (根朝向坐标系) 写进 out, 返回髋部位置和根部运动. */
export function samplePose(db, f, out) {
  if (db.sample) return db.sample(f, out);  // 学出来的动作来源 (web/lmm.js) 自己算
  const clip = db.clips[db.clipOf[Math.floor(f)]];
  const i0 = Math.max(clip.start, Math.min(clip.end - 1, Math.floor(f)));
  const i1 = Math.min(clip.end - 1, i0 + 1), t = Math.min(1, Math.max(0, f - i0));
  const q0 = [0, 0, 0, 1], q1 = [0, 0, 0, 1];
  for (let b = 0; b < db.B; b++) {
    const a = (i0 * db.B + b) * 4, c = (i1 * db.B + b) * 4;
    for (let k = 0; k < 4; k++) { q0[k] = db.quats[a + k] / 32767; q1[k] = db.quats[c + k] / 32767; }
    out[b] = qnlerp(q0, q1, t, out[b] || [0, 0, 0, 1]);
  }
  const lerp3 = (arr) => [0, 1, 2].map((k) => arr[i0 * 3 + k] * (1 - t) + arr[i1 * 3 + k] * t);
  const n = Math.round(f) <= clip.end - 1 ? Math.round(f) : clip.end - 1;
  return { hips: lerp3(db.hips), root: lerp3(db.root), contacts: [db.contacts[n * 2], db.contacts[n * 2 + 1]] };
}

// -- 模拟骨 (临界阻尼弹簧) ------------------------------------------------------------------
const halflifeToDamping = (h) => (4 * LN2) / (h + 1e-5);

/** 位置弹簧: 状态 {x, z, vx, vz, ax, az} 朝期望速度 (dvx, dvz) 推进 dt 秒, 返回新状态. */
export function springVelocity(s, dvx, dvz, halflife, dt) {
  const y = halflifeToDamping(halflife) / 2, eydt = Math.exp(-y * dt);
  const one = (x, v, a, dv) => {
    const j0 = v - dv, j1 = a + j0 * y;
    return [eydt * ((-j1 / (y * y)) + ((-j0 - j1 * dt) / y)) + j1 / (y * y) + j0 / y + dv * dt + x,
      eydt * (j0 + j1 * dt) + dv, eydt * (a - j1 * y * dt)];
  };
  const [x, vx, ax] = one(s.x, s.vx, s.ax, dvx), [z, vz, az] = one(s.z, s.vz, s.az, dvz);
  return { x, z, vx, vz, ax, az };
}

/** 朝向弹簧: 状态 {yaw, w} 朝期望朝向推进 dt 秒. */
export function springYaw(s, goal, halflife, dt) {
  const y = halflifeToDamping(halflife) / 2, eydt = Math.exp(-y * dt);
  const j0 = wrapAngle(s.yaw - goal), j1 = s.w + j0 * y;
  return { yaw: wrapAngle(eydt * (j0 + j1 * dt) + goal), w: eydt * (s.w - j1 * y * dt) };
}

/** 从模拟状态预测未来若干秒的位置和朝向. */
export function predict(pos, rot, dvx, dvz, dyaw, times, hv, hr) {
  return times.map((t) => {
    const p = springVelocity(pos, dvx, dvz, hv, t), r = springYaw(rot, dyaw, hr, t);
    return { x: p.x, z: p.z, yaw: r.yaw };
  });
}

// -- 查询与搜索 ------------------------------------------------------------------------------
/**
 * 查询向量: 姿态部分 (12..26) 取当前播放帧的特征; 轨迹部分 (0..11) 把预测轨迹换到当前根坐标系,
 * 位置除以角色髋高 (数据库是髋高单位), 再按数据库的均值和尺度归一化.
 * src: 当前播放帧所在的数据库 (切换情绪风格时和要搜的库不同, 姿态特征要先还原再按新库归一化).
 */
export function buildQuery(db, frame, root, future, hipHeight, out = new Float32Array(27), src = db) {
  for (let k = 12; k < 27; k++) {
    const raw = src.feats[frame * 27 + k] * src.scale[k] + src.mean[k];
    out[k] = (raw - db.mean[k]) / db.scale[k];
  }
  const c = Math.cos(-root.yaw), s = Math.sin(-root.yaw);
  future.forEach((p, k) => {
    const dx = p.x - root.x, dz = p.z - root.z;
    const lx = (c * dx + s * dz) / hipHeight, lz = (-s * dx + c * dz) / hipHeight;
    const dy = p.yaw - root.yaw;
    const raw = [lx, lz, Math.sin(dy), Math.cos(dy)];
    const idx = [2 * k, 2 * k + 1, 6 + 2 * k, 7 + 2 * k];
    idx.forEach((d, n) => { out[d] = (raw[n] - db.mean[d]) / db.scale[d]; });
  });
  return out;
}

/** 在数据库里找离查询最近的帧. skip(f) 为真的帧不参与. 返回 {frame, cost}. */
export function search(db, query, skip = null) {
  if (db.search) return db.search(query, skip);  // 学出来的动作来源 (web/lmm.js): 投影器
  let best = -1, bestCost = Infinity;
  for (const blk of db.blocks) {
    let bound = 0;
    for (let k = 0; k < 27 && bound < bestCost; k++) {
      const q = query[k];
      const d = q < blk.lo[k] ? blk.lo[k] - q : q > blk.hi[k] ? q - blk.hi[k] : 0;
      bound += d * d;
    }
    if (bound >= bestCost) continue;
    for (let f = blk.s; f < blk.e; f++) {
      if (skip && skip(f)) continue;
      let cost = 0;
      const o = f * 27;
      for (let k = 0; k < 27 && cost < bestCost; k++) { const d = query[k] - db.feats[o + k]; cost += d * d; }
      if (cost < bestCost) { bestCost = cost; best = f; }
    }
  }
  return { frame: best, cost: bestCost };
}

export function frameCost(db, query, f) {
  let cost = 0;
  for (let k = 0; k < 27; k++) { const d = query[k] - db.feats[f * 27 + k]; cost += d * d; }
  return cost;
}

// -- 惯性化 --------------------------------------------------------------------------------
/** 衰减中的偏移: 每根骨骼一个旋转向量 (和角速度), 外加髋部位置偏移. 切换动画时吸收跳变, 再按弹簧回零. */
export class Inertializer {
  constructor(bones, halflife = 0.1) {
    this.B = bones;
    this.halflife = halflife;
    this.rot = new Float32Array(bones * 3);
    this.vel = new Float32Array(bones * 3);
    this.pos = new Float32Array(3);
    this.posVel = new Float32Array(3);
  }

  /** prev/next: 切换前输出的姿态和切换后的新动画姿态 (每根骨骼一个四元数); 速度项可省略. */
  transition(prev, next, prevHips, nextHips) {
    for (let b = 0; b < this.B; b++) {
      // 当前已有的偏移叠加到 prev 上, 等于切换前真正显示的姿态
      const shown = qmul(qexp(this.offset(b)), prev[b]);
      const d = qlog(qmul(shown, qinv(next[b])));
      this.rot.set(d, b * 3);
    }
    this.vel.fill(0);  // 偏移从静止开始衰减, 不带上一段的速度
    for (let k = 0; k < 3; k++) this.pos[k] = prevHips[k] + this.pos[k] - nextHips[k];
    this.posVel.fill(0);
  }

  offset(b) { return [this.rot[b * 3], this.rot[b * 3 + 1], this.rot[b * 3 + 2]]; }

  update(dt) {
    const y = halflifeToDamping(this.halflife) / 2, eydt = Math.exp(-y * dt);
    const decay = (x, v, i) => {
      const j1 = v[i] + x[i] * y;
      x[i] = eydt * (x[i] + j1 * dt);
      v[i] = eydt * (v[i] - j1 * y * dt);
    };
    for (let i = 0; i < this.rot.length; i++) decay(this.rot, this.vel, i);
    for (let i = 0; i < 3; i++) decay(this.pos, this.posVel, i);
  }

  /** 把偏移叠加到动画姿态上 (就地修改). */
  apply(quats, hips) {
    for (let b = 0; b < this.B; b++) quats[b] = qnorm(qmul(qexp(this.offset(b)), quats[b]));
    for (let k = 0; k < 3; k++) hips[k] += this.pos[k];
  }
}
