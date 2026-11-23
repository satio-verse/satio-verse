// 运动学约束的纯计算部分 (不依赖 three.js, Node 里可单测): 质心、支撑多边形与稳定裕度、关节可行域.
import { ROM, hingeAngle } from './rom.js';
// 位置都是 [x, y, z] (y 向上), 地面是 y = 0 平面上的 [x, z].

// 节段质量占比 (Winter, Biomechanics and Motor Control of Human Movement): [近端骨骼, 远端骨骼, 质量比例]
export const SEGMENTS = [
  ['head', null, 0.081], ['hips', 'chest', 0.497],
  ['leftUpperArm', 'leftLowerArm', 0.028], ['rightUpperArm', 'rightLowerArm', 0.028],
  ['leftLowerArm', 'leftHand', 0.016], ['rightLowerArm', 'rightHand', 0.016],
  ['leftHand', null, 0.006], ['rightHand', null, 0.006],
  ['leftUpperLeg', 'leftLowerLeg', 0.1], ['rightUpperLeg', 'rightLowerLeg', 0.1],
  ['leftLowerLeg', 'leftFoot', 0.0465], ['rightLowerLeg', 'rightFoot', 0.0465],
  ['leftFoot', 'leftToes', 0.0145], ['rightFoot', 'rightToes', 0.0145],
];

/** 质心: 每个节段取两端骨骼位置的中点, 按质量比例加权. pos: {骨骼名: [x,y,z]}. */
export function centerOfMass(pos) {
  let m = 0;
  const c = [0, 0, 0];
  for (const [a, b, w] of SEGMENTS) {
    const pa = pos[a];
    if (!pa) continue;
    const pb = (b && pos[b]) || pa;
    for (let k = 0; k < 3; k++) c[k] += ((pa[k] + pb[k]) / 2) * w;
    m += w;
  }
  return m ? c.map((v) => v / m) : null;
}

/** 二维凸包 (Andrew 单调链), 逆时针. pts: [[x, z], ...]. */
export function convexHull(pts) {
  const p = [...pts].sort((a, b) => a[0] - b[0] || a[1] - b[1]);
  if (p.length < 3) return p;
  const cross = (o, a, b) => (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
  const lower = [], upper = [];
  for (const q of p) { while (lower.length >= 2 && cross(lower.at(-2), lower.at(-1), q) <= 0) lower.pop(); lower.push(q); }
  for (const q of p.reverse()) { while (upper.length >= 2 && cross(upper.at(-2), upper.at(-1), q) <= 0) upper.pop(); upper.push(q); }
  return lower.slice(0, -1).concat(upper.slice(0, -1));
}

/** 点到凸多边形边界的有符号距离 (在里面为正). 少于 3 个点时退化为到线段/点的距离取负. */
export function signedMargin(hull, q) {
  if (!hull.length) return -Infinity;
  const segDist = (a, b) => {
    const dx = b[0] - a[0], dz = b[1] - a[1], L = dx * dx + dz * dz;
    const t = L ? Math.max(0, Math.min(1, ((q[0] - a[0]) * dx + (q[1] - a[1]) * dz) / L)) : 0;
    return Math.hypot(q[0] - (a[0] + t * dx), q[1] - (a[1] + t * dz));
  };
  if (hull.length < 3) return -(hull.length === 1 ? segDist(hull[0], hull[0]) : segDist(hull[0], hull[1]));
  let inside = true, d = Infinity;
  for (let i = 0; i < hull.length; i++) {
    const a = hull[i], b = hull[(i + 1) % hull.length];
    if ((b[0] - a[0]) * (q[1] - a[1]) - (b[1] - a[1]) * (q[0] - a[0]) < 0) inside = false;
    d = Math.min(d, segDist(a, b));
  }
  return inside ? d : -d;
}

/** 两条线段 p1-q1、p2-q2 (三维数组) 的最近距离 d 和最近点的参数 s、t (0..1). */
export function segmentDistance(p1, q1, p2, q2) {
  const d1 = [q1[0] - p1[0], q1[1] - p1[1], q1[2] - p1[2]], d2 = [q2[0] - p2[0], q2[1] - p2[1], q2[2] - p2[2]];
  const r = [p1[0] - p2[0], p1[1] - p2[1], p1[2] - p2[2]];
  const dot3 = (u, w) => u[0] * w[0] + u[1] * w[1] + u[2] * w[2];
  const a = dot3(d1, d1), e = dot3(d2, d2), f = dot3(d2, r), c = dot3(d1, r), b = dot3(d1, d2), den = a * e - b * b;
  const clamp01 = (x) => Math.max(0, Math.min(1, x));
  let s = den > 1e-12 ? clamp01((b * f - c * e) / den) : 0;
  let t = e > 1e-12 ? (b * s + f) / e : 0;
  if (t < 0) { t = 0; s = a > 1e-12 ? clamp01(-c / a) : 0; } else if (t > 1) { t = 1; s = a > 1e-12 ? clamp01((b - c) / a) : 0; }
  const x = [p1[0] + d1[0] * s - p2[0] - d2[0] * t, p1[1] + d1[1] * s - p2[1] - d2[1] * t, p1[2] + d1[2] * s - p2[2] - d2[2] * t];
  return { d: Math.hypot(...x), s, t };
}

/** 两条腿互相插进去的深度 (米, 取最深的一对): 小腿和小腿、脚和另一条小腿、脚和脚, 都按胶囊算.
 *  caps: {left, right}, 每条 {a, b, r, foot: {a, b, r} | null} (见 locomotion.legCapsules: 小腿按网格中心挪开、半径按网格量). */
export function legPenetration(caps) {
  const L = caps?.left, R = caps?.right;
  if (!L || !R) return 0;
  const pen = (x, y) => x.r + y.r - segmentDistance(x.a, x.b, y.a, y.b).d;
  let deep = pen(L, R);
  if (L.foot) deep = Math.max(deep, pen(L.foot, R));
  if (R.foot) deep = Math.max(deep, pen(L, R.foot));
  if (L.foot && R.foot) deep = Math.max(deep, pen(L.foot, R.foot));
  return Math.max(0, deep);
}

/** 鞋底的着地部分:鞋底取样点 [[x, y, z], ...] 里比这只脚最低点高不到 tol 的, 投影到地面 [[x, z], ...].
 *  脚放平时是整个鞋底 (含脚跟、脚尖), 踮脚时只剩前掌. 比 footPoints 的"脚踝到脚尖"准: 那个长方形没有脚跟和脚尖,
 *  脚踝正在髋部下方的模型 (silvercat) 重心稍往后就算出了支撑面; 穿高跟的 (miomio) 着地的是前掌, 长方形却在它后面. */
export function soleContact(points, tol = 0.015) {
  let low = Infinity;
  for (const p of points) if (p[1] < low) low = p[1];
  return points.filter((p) => p[1] < low + tol).map((p) => [p[0], p[2]]);
}

/** 一只脚着地时贡献的支撑点: 脚踝和脚尖在地面上的投影, 向两侧各扩半个脚宽 (没有鞋底取样时用). */
export function footPoints(ankle, toe, halfWidth = 0.04) {
  const dx = toe[0] - ankle[0], dz = toe[2] - ankle[2], L = Math.hypot(dx, dz) || 1;
  const sx = (-dz / L) * halfWidth, sz = (dx / L) * halfWidth;
  return [[ankle[0] + sx, ankle[2] + sz], [ankle[0] - sx, ankle[2] - sz], [toe[0] + sx, toe[2] + sz], [toe[0] - sx, toe[2] - sz]];
}

const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const crossv = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const norm = (a) => { const n = Math.hypot(...a) || 1; return a.map((x) => x / n); };
const angle = (a, b) => (Math.acos(Math.max(-1, Math.min(1, dot(norm(a), norm(b))))) * 180) / Math.PI;

// 关节可行域 (度): 用 rom.js 的关节活动范围 (临床中立零度法的成人参考值). bend: 铰链关节的屈曲 (0 = 伸直),
// 反向弯曲 (过伸) 超过 hyper 度算违反.
export const LIMITS = {
  knee: { min: 0, max: ROM.knee.flex, hyper: ROM.knee.hyper },
  elbow: { min: 0, max: ROM.elbow.flex, hyper: ROM.elbow.hyper },
  hipFlex: { min: -ROM.hip.ext, max: ROM.hip.flex },
  hipAbd: { min: -ROM.hip.add, max: ROM.hip.abd },
  neck: { max: Math.max(ROM.neck.flex, ROM.neck.ext) },
};

/** 余量: 0 = 在极限上, 1 = 在可行域中间; 越界为负. */
export const margin = (v, lo, hi) => Math.min(v - lo, hi - v) / ((hi - lo) / 2);

/** 铰链关节的转轴 (标准化骨骼在 T 姿态下的局部轴, 乘上一节骨骼的世界旋转): 正常弯曲时 dot(u x l, 轴) > 0.
 *  膝盖: 大腿的局部 +X (小腿往后折); 左肘: 上臂局部 -Y, 右肘: 上臂局部 +Y (前臂往前折). */
export const HINGE = {
  leftLowerLeg: ['leftUpperLeg', [1, 0, 0]], rightLowerLeg: ['rightUpperLeg', [1, 0, 0]],
  leftLowerArm: ['leftUpperArm', [0, -1, 0]], rightLowerArm: ['rightUpperArm', [0, 1, 0]],
};

/**
 * 关节检查. pos: 骨骼世界位置; axes: {中间骨骼名: 世界坐标下的铰链轴 (见 HINGE)}; body: 胯骨的前/上/右方向.
 * 返回 [{joint, value, margin, violated, excess}]; joint 是中间那根骨骼, 叠加层在它上面画; excess: 超出活动范围的度数.
 */
export function jointChecks(pos, axes, { fwd, up, right }) {
  const out = [];
  const hinge = (a, b, c, lim) => {
    if (!pos[a] || !pos[b] || !pos[c]) return;
    const u = sub(pos[b], pos[a]), l = sub(pos[c], pos[b]);
    const flex = angle(u, l);
    const axis = axes[b];
    // 反着折 = 过伸; 折得很深或往侧面弯是上一段扭转让铰链轴不对了, 按屈曲算 (和调姿层同一个判断, 见 rom.js 的 hingeAngle)
    const wrong = axis ? hingeAngle(u, l, axis) < 0 && flex > 3 : false;
    const v = wrong ? -flex : flex;
    // 伸直是铰链关节正常的休息位, 不算接近极限: 余量只看离最大屈曲多远; 反着折才为负
    const m = wrong ? -flex / lim.hyper : Math.min(1, (lim.max - v) / (lim.max / 2));
    const excess = wrong ? Math.max(0, flex - lim.hyper) : Math.max(0, v - lim.max);
    out.push({ joint: b, value: +v.toFixed(1), margin: +m.toFixed(2), violated: excess > 0, excess });
  };
  hinge('leftUpperLeg', 'leftLowerLeg', 'leftFoot', LIMITS.knee);
  hinge('rightUpperLeg', 'rightLowerLeg', 'rightFoot', LIMITS.knee);
  hinge('leftUpperArm', 'leftLowerArm', 'leftHand', LIMITS.elbow);
  hinge('rightUpperArm', 'rightLowerArm', 'rightHand', LIMITS.elbow);
  for (const [s, sign] of [['left', 1], ['right', -1]]) {  // 髋: 大腿方向在身体坐标里的屈伸和外展
    const a = pos[`${s}UpperLeg`], b = pos[`${s}LowerLeg`];
    if (!a || !b) continue;
    const t = norm(sub(b, a)), down = up.map((x) => -x);
    const flex = (Math.atan2(dot(t, fwd), dot(t, down)) * 180) / Math.PI;
    const abd = (Math.asin(Math.max(-1, Math.min(1, dot(t, right) * -sign))) * 180) / Math.PI;  // 偏离矢状面 (屈髋时也成立)
    const mf = margin(flex, LIMITS.hipFlex.min, LIMITS.hipFlex.max), ma = margin(abd, LIMITS.hipAbd.min, LIMITS.hipAbd.max);
    const over = (v, lo, hi) => Math.max(0, lo - v, v - hi);
    out.push({ joint: `${s}UpperLeg`, value: +flex.toFixed(1), margin: +Math.min(mf, ma).toFixed(2), violated: mf < 0 || ma < 0,
      excess: over(flex, LIMITS.hipFlex.min, LIMITS.hipFlex.max) + over(abd, LIMITS.hipAbd.min, LIMITS.hipAbd.max) });
  }
  if (pos.neck && pos.head && pos.chest) {  // 颈: 头相对胸的偏转
    const v = angle(sub(pos.neck, pos.chest), sub(pos.head, pos.neck));
    out.push({ joint: 'neck', value: +v.toFixed(1), margin: +(1 - v / LIMITS.neck.max).toFixed(2), violated: v > LIMITS.neck.max,
      excess: Math.max(0, v - LIMITS.neck.max) });
  }
  return out;
}
