// 痛觉: 把"姿态不对"量成负反馈. 每帧 (所有运动层、接触层、平滑层之后) 按物理单位记下各项, 约束面板显示加权后的
// 痛觉值, 执行评估和自动调优 (tools/tune_motion.py) 用各项的累计. 以后做物理仿真加强化学习时, 这些项可以直接当奖励里的惩罚项.
//
// 只有纯函数, 不依赖 three.js, 可以在 Node 里单测.

// 各项: 单位, 显示用的权重 (换算成大致可比的"疼"), 说明
export const PAIN_TERMS = {
  selfPen: { unit: 'm', w: 100, note: '接触层处理后手臂、手里的东西仍插在身体里的深度' },
  legPen: { unit: 'm', w: 100, note: '两条腿互相插进去的深度: 小腿和小腿、脚和另一条小腿, 按胶囊 (小腿半径按模型网格量)' },
  pushed: { unit: 'm', w: 30, note: '接触层这一帧把手臂推出身体的距离 (动作本身穿了模)' },
  furniture: { unit: 'm', w: 100, note: '手、脚、膝盖插进家具或墙里的深度, 坐着时臀部陷进座面的深度' },
  itemPen: { unit: 'm', w: 100, note: '物品插进家具、地面的深度' },
  floorPen: { unit: 'm', w: 100, note: '鞋底踩进地面的深度 (超过 0.5 cm 的部分)' },
  floating: { unit: 'm', w: 50, note: '站着时两只脚都离地的高度 (超过 1 cm 的部分)' },
  slide: { unit: 'm', w: 100, note: '锁定的支撑脚这一帧的水平滑动 (超过 5 mm 的部分)' },
  joint: { unit: 'deg', w: 0.2, note: '关节超出活动范围的度数之和' },
  balance: { unit: 'm', w: 100, note: '站定时质心在支撑面外的距离 (要摔的姿势)' },
  jump: { unit: 'count', w: 0.2, note: '平滑层抑制的跳变数 (骨骼数; 随平滑层上限变, 只作参考, 调优看下面的 jerk)' },
  jerk: { unit: 'rad/s', w: 0.5, note: '骨骼 (世界朝向) 的角加速度超过 JERK_LIMIT 的部分乘 dt: 突然的甩动、跳变. 阈值固定, 不随可调参数变' },
  grab: { unit: 'm', w: 100, note: '拿起、放下的那一刻物品和手 / 落点之间的距离 (隔空取物、瞬移)' },
  heldAcc: { unit: 'm/s', w: 2, note: '拿着东西时掌心持物点的线加速度超过 HELD_ACC_LIMIT 的部分乘 dt: 拿放起步一帧跳、放下时往下甩再猛停. 阈值固定, 不随可调参数变' },
  spill: { unit: 'ml', w: 0.25, note: '评估场景挂了液体 (端咖啡, motion-eval.js 的 carry) 时洒出来的量' },
};

/** jerk 的阈值 (rad/s^2): 真人挥手时上臂约 150. */
export const JERK_LIMIT = 200;

/** heldAcc 的阈值 (m/s^2): 端着东西走路一步一颠约 10 (不端稳) / 1 (端稳); 拿放起步那一帧跳 5 cm 约 200. */
export const HELD_ACC_LIMIT = 20;

/** 三个点 (前前帧、前帧、这一帧, 每帧 dt 秒) 的线加速度超过 limit 的部分乘 dt. */
export function accExcess(p2, p1, p, dt, limit = HELD_ACC_LIMIT) {
  const a = Math.hypot(p[0] - 2 * p1[0] + p2[0], p[1] - 2 * p1[1] + p2[1], p[2] - 2 * p1[2] + p2[2]) / (dt * dt);
  return Math.max(0, a - limit) * dt;
}

/** 两个单位四元数 [x,y,z,w] 之间的转角 (弧度). */
export function quatAngle(a, b) {
  const d = Math.abs(a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3]);
  return 2 * Math.acos(Math.min(1, d));
}

/** 加权求和成一个痛觉值 (terms 里缺的项按 0). */
export function painScore(terms) {
  let s = 0;
  for (const [k, def] of Object.entries(PAIN_TERMS)) s += (terms[k] || 0) * def.w;
  return s;
}

/** 球 (中心 p, 半径 r) 插进轴对齐盒子 b = {min: [x,y,z], max: [x,y,z]} 的深度; 没插进去为 0. */
export function boxDepth(p, r, b) {
  let inside = true, out2 = 0, minFace = Infinity;
  for (let k = 0; k < 3; k++) {
    const lo = b.min[k], hi = b.max[k], v = p[k];
    if (v < lo) { inside = false; out2 += (lo - v) ** 2; } else if (v > hi) { inside = false; out2 += (v - hi) ** 2; } else {
      minFace = Math.min(minFace, v - lo, hi - v);
    }
  }
  return inside ? minFace + r : Math.max(0, r - Math.sqrt(out2));
}

/** 球出了地面范围 (矩形 rect = {minX, maxX, minZ, maxZ}, 即墙) 的深度. open: 不算的一边 (比如前方没有墙的 maxZ). */
export function roomDepth(p, r, rect, open = ['maxZ']) {
  const d = [];
  if (!open.includes('minX')) d.push(rect.minX - (p[0] - r));
  if (!open.includes('maxX')) d.push(p[0] + r - rect.maxX);
  if (!open.includes('minZ')) d.push(rect.minZ - (p[2] - r));
  if (!open.includes('maxZ')) d.push(p[2] + r - rect.maxZ);
  return Math.max(0, ...d);
}

/** 场景物体 -> 碰撞盒: 有体积的家具 (墙上的、地毯这类薄的、能拿的东西、虚拟目标都不算). */
export function furnitureBoxes(objects) {
  const out = [];
  for (const o of objects || []) {
    const b = o.box;
    if (!b || o.virtual || o.wall || o.item || b.height < 0.05) continue;
    const [cx, cz] = b.center, [sx, sz] = b.size, base = b.base || 0;
    out.push({ id: o.id, min: [cx - sx / 2, base, cz - sz / 2], max: [cx + sx / 2, base + b.height, cz + sz / 2] });
  }
  return out;
}

/** 场景 JSON 的包围盒换成按物体分组的格式 (没有房间部件时的退路, 每件家具一个实心部件). */
export function boxGroups(boxes) {
  return boxes.map((b) => ({ id: b.id, min: b.min, max: b.max, parts: [{ part: 'solid', min: b.min, max: b.max }] }));
}

/** 球插进碰撞体 (见 scene.js 的 collisionParts) 的深度: 各部件里最深的. skip(物体, 部件, p) 为真的部件不算 (比如坐着的座面). */
export function partsDepth(groups, p, r, skip = null) {
  let worst = 0;
  for (const g of groups) {
    if (p[0] + r < g.min[0] || p[0] - r > g.max[0] || p[1] + r < g.min[1] || p[1] - r > g.max[1]
      || p[2] + r < g.min[2] || p[2] - r > g.max[2]) continue;
    for (const part of g.parts) {
      if (skip && skip(g, part, p)) continue;
      const d = boxDepth(p, r, part);
      if (d > worst) worst = d;
    }
  }
  return worst;
}

/** 地面范围 (多边形的包围矩形). */
export function floorRect(floor) {
  const xs = floor.map((p) => p[0]), zs = floor.map((p) => p[1]);
  return { minX: Math.min(...xs), maxX: Math.max(...xs), minZ: Math.min(...zs), maxZ: Math.max(...zs) };
}

/** 关节检查结果 (kinematics-core.js 的 jointChecks) 里超出活动范围的度数之和. */
export function jointExcess(joints) {
  return joints.reduce((s, j) => s + (j.excess || 0), 0);
}
