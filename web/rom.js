// 关节活动范围 (Range of Motion, ROM) 与解剖学角度分解. 纯计算, 不依赖 three.js, Node 里可单测.
//
// 数值按临床"中立零度法": 解剖学站姿为 0 度, 各方向分别计角度. 来自健康成年人的平均参考范围
// (年龄、性别、主动/被动测量和个体差异都会让它变化), 这里取表中区间的中间值, 过伸按允许的上限.
// VRM 的标准化骨骼以 T 姿态为零位 (角色面朝 +Z, 左边 +X, 上 +Y), 解剖学角度从骨骼方向换算, 不直接用骨骼的欧拉角.
export const ROM = {
  shoulder: { flex: 180, ext: 60, abd: 180, add: 45, intRot: 70, extRot: 90 },  // 内外旋暂不限制 (零位要另外定义)
  elbow: { flex: 145, hyper: 10 },
  forearm: { pron: 80, sup: 85 },
  wrist: { flex: 75, ext: 75, radial: 20, ulnar: 35 },
  hip: { flex: 120, ext: 25, abd: 40, add: 30, intRot: 35, extRot: 50 },        // 内外旋暂不限制
  knee: { flex: 140, hyper: 10 },
  ankle: { dorsi: 25, plantar: 45, inv: 30, ev: 30 },
  neck: { flex: 50, ext: 55, rot: 80, lat: 45 },
  lumbar: { flex: 50, ext: 25 },
};

const RAD = Math.PI / 180, DEG = 180 / Math.PI;
export const v = {
  add: (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]],
  sub: (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]],
  mul: (a, s) => [a[0] * s, a[1] * s, a[2] * s],
  dot: (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2],
  cross: (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]],
  norm: (a) => { const n = Math.hypot(a[0], a[1], a[2]) || 1; return [a[0] / n, a[1] / n, a[2] / n]; },
};
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));

/** 方向 d 在坐标系 (前 f, 上 u, 外侧 s) 里: 抬起角 (离开"向下"多少度) 和抬起平面里朝前/朝外的分量 (度). */
export function elevation(d, { f, u, s }) {
  const dn = v.norm(d);
  return {
    fwd: Math.atan2(v.dot(dn, f), -v.dot(dn, u)) * DEG,   // 矢状面: 0 = 向下, 90 = 向前平举, 负 = 向后
    side: Math.asin(clamp(v.dot(dn, s), -1, 1)) * DEG,     // 偏离矢状面的角度: 0 = 在矢状面里, 90 = 侧平举, 负 = 越过中线
                                                           // (屈髋时也成立; 用冠状面夹角的话, 坐着时大腿水平向前会被量成外展 90 度)
  };
}

/**
 * 把肢体方向限制在关节活动范围内. 方向分解成 下/前/外 三个分量, 在矢状面 (下-前) 和冠状面 (下-外) 里分别看角度:
 *   后伸 (back)、越过中线 (cross): 只在肢体朝下时检查 (举过头顶稍微往后不算后伸);
 *   前屈上限 (fwdMax)、外展上限 (sideMax): 朝前/朝外时检查, 抬过水平也算 (髋前屈 120 度).
 * 越界的平面按极限角重算该平面里的两个分量 (平面内长度不变). 返回修正后的方向和越界的项.
 */
export function limitDirection(d, frame, { back, cross, fwdMax = 180, sideMax = 180 }) {
  const { f, u, s } = frame;
  const dn = v.norm(d);
  let df = v.dot(dn, f), du = -v.dot(dn, u), ds = v.dot(dn, s);
  const hit = [];
  const rs = Math.hypot(df, du), as = Math.atan2(df, du) * DEG;
  if (du > 0 && as < -back) { hit.push('ext'); df = -rs * Math.sin(back * RAD); du = rs * Math.cos(back * RAD); }
  else if (fwdMax < 180 && df > 0 && as > fwdMax) { hit.push('flex'); df = rs * Math.sin(fwdMax * RAD); du = rs * Math.cos(fwdMax * RAD); }
  // 外展/内收: 偏离矢状面的角度. 内收只在肢体明显朝下时检查 (手臂水平交叉抱胸、坐着翘腿是另一种动作, 不拦)
  const af = Math.asin(clamp(ds, -1, 1)) * DEG;
  const setSide = (deg) => {
    const nds = Math.sin(deg * RAD), k = Math.sqrt(Math.max(0, 1 - nds * nds)) / (Math.hypot(df, du) || 1e-9);
    df *= k; du *= k; ds = nds;
  };
  if (du > 0.3 && af < -cross) { hit.push('add'); setSide(-cross); }
  else if (sideMax < 180 && af > sideMax) { hit.push('abd'); setSide(sideMax); }
  if (!hit.length) return { d: dn, hit };
  return { d: v.norm(v.add(v.add(v.mul(f, df), v.mul(u, -du)), v.mul(s, ds))), hit };
}

/** 反着折超过这么多度 (肘、膝最多过伸 10 度左右) 不是过伸, 是上一段绕自身转了半圈、铰链轴跟着反了: 手举到头后面
 *  (肘屈 150 度以上, 前臂几乎折回到上臂上) 时动捕的上臂常常这样. 以前当成过伸 160 度, 调姿层把前臂转 150 度"纠正",
 *  手臂一帧里翻过去. 同理, 弯曲平面和铰链轴差得多 (上一段扭了九十度上下, 看上去往侧面弯) 也不算过伸:
 *  弯曲方向和"反着折"的方向夹角在 SIDEWAYS 度以内才算. 骨骼位置是对的, 扭的只是上一段自己的朝向. */
export const TWISTED = 60;
export const SIDEWAYS = 60;
const REVERSED = -Math.cos((SIDEWAYS * Math.PI) / 180);  // 弯曲平面法线和铰链轴的点积小于它才算反着折

/** a x b 的方向和铰链轴: 1 正常屈曲, -1 反着折, 0 往侧面弯 (见上). */
function bendSide(an, bn, axis) {
  const c = v.cross(an, bn), n = Math.hypot(...c);
  if (n < 1e-9) return 1;
  const d = v.dot(c, axis) / (n * Math.hypot(...axis));
  return d >= 0 ? 1 : d < REVERSED ? -1 : 0;
}

/** 铰链关节 (肘、膝): 上一段 a、下一段 b 的方向, 铰链轴 axis (正常屈曲时 a x b 与 axis 同向). 返回屈曲角 (过伸为负;
 *  反着折超过 TWISTED 度按屈曲算, 见上). */
export function hingeAngle(a, b, axis) {
  const an = v.norm(a), bn = v.norm(b);
  const ang = Math.acos(clamp(v.dot(an, bn), -1, 1)) * DEG;
  return bendSide(an, bn, axis) < 0 && ang > 0.5 && ang <= TWISTED ? -ang : ang;
}

/** 铰链关节越界时需要补转的角度 (度, 正 = 继续屈曲方向). 过伸绕铰链轴 axis 补; 屈曲过头绕实际的弯曲平面法线
 *  (a x b) 补, 正常时它和 axis 同向, 上一段扭转时 axis 不准 (见 TWISTED). */
export function hingeCorrection(a, b, axis, { flex, hyper }) {
  const ang = hingeAngle(a, b, axis);
  if (ang > flex) return { angle: ang, fix: -(ang - flex) };
  if (ang < -hyper) return { angle: ang, fix: -hyper - ang };
  return { angle: ang, fix: 0 };
}

/** 四元数 [x, y, z, w] 绕单位轴 axis 的扭转角 (度, 摆动-扭转分解). */
export function twistAngle(q, axis) {
  const p = v.dot([q[0], q[1], q[2]], axis);
  let ang = 2 * Math.atan2(p, q[3]) * DEG;
  if (ang > 180) ang -= 360;
  if (ang < -180) ang += 360;
  return ang;
}

/** 欧拉角 (YXZ 顺序: 先绕 Y 转头, 再绕 X 点头, 最后绕 Z 歪头), 度. 颈部和腰部用. */
export function eulerYXZ(q) {
  const [x, y, z, w] = q;
  const m11 = 1 - 2 * (y * y + z * z), m12 = 2 * (x * y - z * w), m13 = 2 * (x * z + y * w);
  const m22 = 1 - 2 * (x * x + z * z), m23 = 2 * (y * z - x * w);
  const m21 = 2 * (x * y + z * w), m33 = 1 - 2 * (x * x + y * y), m31 = 2 * (x * z - y * w);
  const ex = Math.asin(clamp(-m23, -1, 1));
  const [ey, ez] = Math.abs(m23) < 0.9999999 ? [Math.atan2(m13, m33), Math.atan2(m21, m22)] : [Math.atan2(-m31, m11), 0];
  return { yaw: ey * DEG, pitch: ex * DEG, roll: ez * DEG, m12 };
}

/** 按极限缩放转动: 每个轴超出多少, 就把整个转动按比例往零位收 (返回 0..1 的保留比例). */
export function keepRatio(angles, limits) {
  let k = 1;
  for (const [name, val] of Object.entries(angles)) {
    const lim = limits[name];
    if (!lim) continue;
    const [lo, hi] = Array.isArray(lim) ? lim : [-lim, lim];
    if (val > hi && val > 1e-6) k = Math.min(k, hi / val);
    if (val < lo && val < -1e-6) k = Math.min(k, lo / val);
  }
  return clamp(k, 0, 1);
}

/** 向量绕单位轴转 deg 度 (罗德里格斯公式). */
export function rotateAround(p, axis, deg) {
  const a = v.norm(axis), t = deg * RAD, c = Math.cos(t), sn = Math.sin(t);
  return v.add(v.add(v.mul(p, c), v.mul(v.cross(a, p), sn)), v.mul(a, v.dot(a, p) * (1 - c)));
}
