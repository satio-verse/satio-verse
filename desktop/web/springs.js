// 头发、裙子的物理 (VRM 的弹簧骨骼) 按真实经过的时间、拆成小步算.
// web/avatar-vrm.js 每帧调 vrm.update(min(dt, 0.05)): 桌宠空闲 10 帧时一帧 0.1 秒, 弹簧骨骼只走 0.05 秒 —— 头发按半速慢动作飘,
// 而且一大步一大步地跳, 看着不自然又卡 (用户说的"头发卡卡的"). 这里接管弹簧骨骼的 update: 不管传进来多少, 按两次调用之间真实
// 过了多久 (最多 0.25 秒, 后台回来不暴冲) 算, 每步不超过 1/60 秒. 不改 web/ 的文件.
// 一帧最多拆几步 (2026-10-27, 见知识库「小屋性能实验记录」): 当时一步约 1.1 ms (88 根弹簧骨 x 21 个碰撞体; 去掉 three-vrm 的 Proxy 以后
// 约 0.15 ms). 以前不封顶, 一帧卡了 0.25 秒就要补 15 步, 越卡越慢; 封顶 8 步: 菜单里的空闲帧率 (10/15/30/60) 一帧用不到 8 步, 和原来逐位相同, 只有卡了 0.13 秒以上
// 的那一帧每步长一点. 页面参数 springmax=、springhz=、springbudget=、springmin= 做 A/B,
// perfoff=spring 回到原来的 (不封顶、不降级、每步都算祖先).
import * as THREE from 'three';
import { VRMSpringBoneCollider, VRMSpringBoneColliderShapeCapsule, VRMSpringBoneColliderShapeSphere } from '@pixiv/three-vrm';
import { perfOn } from './perf-flags.js';
// 弹簧骨"中心节点"的逆矩阵缓存不用 Proxy (InverseCache, 2026-10-27, 见知识库「小屋性能实验记录」): 挪到 web/spring-center.js,
// 网页版、桌宠共用一份 (/static/avatar-vrm.js 一导入就装上; 这里再导入一次是同一个模块, 保证载入模型之前装好). perfoff=proxy 照旧关掉.
import '/static/spring-center.js';
import { springFallbackKind } from '/static/model-constraints.js';

const Q = new URLSearchParams(location.search);
const STEP = 1 / (Number(Q.get('springhz')) || 60);
const MAX_S = 0.25;
const MAX_N = perfOn('spring') ? Number(Q.get('springmax')) || 8 : Infinity;
// 电脑慢的时候自动少拆几步 (perfoff=spring 关): 每一步实际花多少毫秒 (指数平均), 一帧弹簧骨最多花 BUDGET 毫秒, 但不少于 MIN_N 步;
// 这台 i7-12700H 一步约 0.15 ms (CPU 放慢 4 倍时 0.8-1.1 ms), 预算够 8 步, 不受影响; 一步要 1.3 ms 以上才开始少拆,
// 3.3 ms 以上 (比这台慢 20 倍上下) 一帧 2 步 (30 帧时每步 1/60 秒, 12 帧时 1/24 秒). 新做法比第一版多花的 (交替两对): 空闲时
// 每秒 6-10 -> 8-10 ms, CPU 放慢 4 倍时每帧 2.4-2.6 -> 3.4-4.7 ms, 整帧耗时差在噪声里.
// 封顶和降级会改数值模拟 (逐位不同), 2026-10-27 晚按阶段评审单独验证过 (知识库「小屋性能实验记录」第 9 项: 同一份录下的输入, 逐位重现
// 真实页面, 只换每帧几步回放, silvercat、wendy): 最先的上限 4 步 + 预算 5 ms 在 10 帧 (省电) 时就少拆, 卡顿那一帧头发插进身体最深
// 1.3 -> 2.2 cm; 上限 8 步 + 预算 10 ms 时菜单帧率全部逐位不变, 卡顿那一帧 1.3 -> 1.4 cm. 每帧只走 1 步时头发和 60 帧参考的差距是原来的
// 2.4-3 倍、插进身体更深, 至多 2 步时差距和原来差不多, 所以下限是 2. 页面参数 springmin=1 退回下限 1 步.
const BUDGET = Number(Q.get('springbudget')) || 10;
const MIN_N = Number(Q.get('springmin')) || 2;

/** 给角色装上. 返回统计 (自检看: 每帧平均拆成几步). */
export function realtimeSprings(v) {
  const sb = v.vrm?.springBoneManager;
  if (!sb || sb.__realtime) return null;
  const update = sb.update.bind(sb);
  const stats = { frames: 0, steps: 0 };
  let last = performance.now();
  sb.update = (delta) => {
    const now = performance.now();
    const real = Math.min(MAX_S, (now - last) / 1000);
    last = now;
    if (!delta) return update(delta);  // 加载、重置时的 update(0): 只摆正, 不模拟
    let n = Math.max(1, Math.min(MAX_N, Math.ceil(real / STEP)));
    if (FAST && stats.stepMs > 0) n = Math.max(Math.min(MIN_N, n), Math.min(n, Math.floor(BUDGET / stats.stepMs)));
    const t0 = performance.now();
    for (let i = 0; i < n; i++) (FAST ? step : update)(real / n, i === 0);
    const per = (performance.now() - t0) / n;
    stats.stepMs = stats.stepMs ? stats.stepMs + (per - stats.stepMs) * 0.05 : per;
    stats.frames++;
    stats.steps += n;
  };
  // 一帧拆成几步时, 第二步起不再重算"弹簧骨的祖先" (three-vrm 每一步先把所有弹簧骨的祖先: 身体骨架、碰撞体… 91 个节点的世界矩阵重算一遍):
  // 同一帧的几步之间它们没动 (弹簧骨只改自己, 祖先里不含弹簧骨). 页面里对拍 (整个角色存下、还原, 原版 / 这里各走两步) 逐位相同;
  // 一帧两步 2.21 -> 2.09 ms. 下面照抄 three-vrm 3.x 的 VRMSpringBoneManager.update (vendor/three-vrm 换版本时要对一下).
  // (试过再按"串"粗筛碰撞体: 结果也逐位相同, 但只再省 4%, 代码复杂, 没留; 见知识库「小屋性能实验记录」)
  const walk = (o, fn) => { o.children.forEach((c) => { fn(c) || walk(c, fn); }); };
  const step = (dt, first) => {
    sb._sortJoints();
    if (first) { const A = sb._ancestors; for (let e = 0; e < A.length; e++) A[e].updateWorldMatrix(e === 0, false); }
    const J = sb._sortedJoints;
    for (let e = 0; e < J.length; e++) {
      const j = J[e];
      j.bone.updateMatrix();
      j.bone.updateWorldMatrix(false, false);
      j.update(dt);
      walk(j.bone, sb._relevantChildrenUpdated);
    }
  };
  const FAST = perfOn('spring') && typeof sb._sortJoints === 'function' && Array.isArray(sb._ancestors) && typeof sb._relevantChildrenUpdated === 'function';
  stats.step = step;  // 对拍用 (页面里直接调一步)
  sb.__realtime = stats;
  return stats;
}

/**
 * Only unconfigured hair/skirt chains receive fallback body colliders. Nonempty
 * imported groups are preserved, including intentional arm collisions on skirts.
 * Fallback skirts use torso/legs; hair may also use arms. This is deliberately not
 * a replacement for calibrating the collider groups inside an existing VRM.
 */
export function bodyColliders(v) {
  const h = v.vrm?.humanoid, sb = v.vrm?.springBoneManager;
  if (!h || !sb || !sb.joints || sb.__body !== undefined) return 0;
  const targets = Array.from(sb.joints, (joint) => ({ joint, kind: springFallbackKind(v.vrm, joint) })).filter((x) => x.kind);
  sb.__body = 0;
  if (!targets.length) return 0;
  v.vrm.scene.updateMatrixWorld(true);
  const raw = (n) => h.getRawBoneNode(n);
  const at = (n) => raw(n)?.getWorldPosition(new THREE.Vector3());
  const feet = Math.min(at('leftFoot')?.y ?? 0, at('rightFoot')?.y ?? 0);
  const s = Math.max(0.6, Math.min(1.4, ((at('head')?.y ?? 1.4) - feet + 0.12) / 1.5));  // 身高相对 1.5 m 的比例
  const colliders = [], armColliders = [];
  const capsule = (bone, tailBone, radius) => {
    const b = raw(bone), tail = at(tailBone);
    if (!b || !tail) return;
    const c = new VRMSpringBoneCollider(new VRMSpringBoneColliderShapeCapsule({
      offset: new THREE.Vector3(), tail: b.worldToLocal(tail.clone()), radius: radius * s }));
    b.add(c);
    colliders.push(c);
    if (/Arm/.test(bone)) armColliders.push(c);
  };
  const sphere = (bone, up, forward, radius) => {
    const b = raw(bone);
    if (!b) return;
    const p = at(bone).add(new THREE.Vector3(0, up * s, forward * s));  // 角色朝 +z
    const c = new VRMSpringBoneCollider(new VRMSpringBoneColliderShapeSphere({ offset: b.worldToLocal(p), radius: radius * s }));
    b.add(c);
    colliders.push(c);
  };
  sphere('head', 0.075, 0.01, 0.072);  // 比头骨小一点: 太大会把刘海顶得飘起来
  capsule(raw('chest') ? 'chest' : 'spine', raw('neck') ? 'neck' : 'head', 0.095);
  capsule('spine', raw('chest') ? 'chest' : 'neck', 0.1);
  sphere('hips', 0.02, 0, 0.12);
  for (const side of ['left', 'right']) {
    capsule(`${side}UpperLeg`, `${side}LowerLeg`, 0.07);
    capsule(`${side}LowerLeg`, `${side}Foot`, 0.048);
    capsule(`${side}UpperArm`, `${side}LowerArm`, 0.042);
    capsule(`${side}LowerArm`, `${side}Hand`, 0.034);
  }
  const hair = { name: 'pet-body-fallback-hair', colliders };
  const skirt = { name: 'pet-body-fallback-skirt', colliders: colliders.filter((c) => !armColliders.includes(c)) };
  for (const { joint, kind } of targets) joint.colliderGroups = [...(joint.colliderGroups || []), kind === 'hair' ? hair : skirt];
  sb.__body = colliders.length;
  sb.__bodyGroups = { hair, skirt };
  return colliders.length;
}

/**
 * 床面、座面 (小屋里躺着、坐着时): 一个埋在座面下面的大球, 球顶正好是座面 —— 头发、裙子落在垫子上, 不穿进沙发和床里.
 * 球顶附近接近平的, 往外沿慢慢低下去, 裙摆还能从座位前沿垂下来. 返回 { set(球顶中心, 半径) / set(null) 收起 }.
 */
export function surfaceCollider(v) {
  const sb = v.vrm?.springBoneManager;
  if (!sb?.joints) return null;
  if (sb.__surface) return sb.__surface;
  // Environment contact is separate from body augmentation. Authored models still
  // receive bed/chair contact, without acquiring a generic all-body collider group.
  const group = { name: 'pet-surface', colliders: [] };
  for (const joint of sb.joints) joint.colliderGroups = [...(joint.colliderGroups || []), group];
  sb.__surfaceGroup = group;
  const shape = new VRMSpringBoneColliderShapeSphere({ offset: new THREE.Vector3(), radius: 1 });
  const c = new VRMSpringBoneCollider(shape);
  c.name = 'pet-surface';
  (v.scene ?? v.vrm.scene).add(c);
  return sb.__surface = {
    set(top, radius = 0.6) {
      const i = group.colliders.indexOf(c);
      if (!top) {
        if (i >= 0) group.colliders.splice(i, 1);
        return;
      }
      shape.radius = radius;
      c.position.set(top.x, top.y - radius, top.z);
      c.updateWorldMatrix(true, false);
      if (i < 0) group.colliders.push(c);
    },
  };
}
