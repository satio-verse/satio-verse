// 液体 (web/physics/liquid.js) 的两个回调: 落下来的一段碰到她身体哪里 (hit)、落到哪个台面或地上 (ground).
// 身体: 接触层 (contact.js) 的躯干、大腿胶囊, 再按骨骼加小腿、脚、手. 台面: 房间碰撞部件 (scene.js 的 collisionParts) 的顶面.
// 网页版评测台 (motion-eval.js 的 carry) 和小窝 (desktop/web/room-liquid.js) 共用.

/** 部位 id -> 中文 (小窝的体感、状态栏用). */
export const PART_NAMES = {
  rightHand: '右手', leftHand: '左手', pelvis: '肚子下面', belly: '肚子', chest: '胸口', leftThigh: '左腿', rightThigh: '右腿',
  leftShin: '左小腿', rightShin: '右小腿', leftFoot: '左脚', rightFoot: '右脚', mouth: '嘴',
};

/** 这一刻身体各段的胶囊 [{part, A, B, r}] (A、B 是 [x, y, z]). v: avatar-vrm 的角色 (有 vrm.humanoid, 可能有 contact). */
export function bodyCapsules(v) {
  const h = v.vrm.humanoid;
  const W = (n) => { const b = h.getNormalizedBoneNode(n); return b ? b.getWorldPosition(b.position.clone()).toArray() : null; };
  const out = (v.contact?._bodyCaps?.() || []).map((c) => ({ part: c.id, A: c.A.toArray(), B: c.B.toArray(), r: c.r }));
  for (const s of ['left', 'right']) {
    const k = W(`${s}LowerLeg`), f = W(`${s}Foot`), toe = W(`${s}Toes`), hw = W(`${s}Hand`), mid = W(`${s}MiddleProximal`);
    if (k && f) out.push({ part: `${s}Shin`, A: k, B: f, r: 0.05 });
    if (f) out.push({ part: `${s}Foot`, A: f, B: toe || f, r: 0.05 });
    if (hw) out.push({ part: `${s}Hand`, A: hw, B: mid || hw, r: 0.035 });
  }
  return out;
}

/** 点到线段的距离 (数组). */
export function segDist(p, A, B) {
  const ab = [B[0] - A[0], B[1] - A[1], B[2] - A[2]], ap = [p[0] - A[0], p[1] - A[1], p[2] - A[2]];
  const L = ab[0] * ab[0] + ab[1] * ab[1] + ab[2] * ab[2];
  const s = L > 1e-12 ? Math.max(0, Math.min(1, (ap[0] * ab[0] + ap[1] * ab[1] + ap[2] * ab[2]) / L)) : 0;
  return Math.hypot(ap[0] - ab[0] * s, ap[1] - ab[1] * s, ap[2] - ab[2] * s);
}

/** hit 回调: caps() 给这一帧的胶囊 (每帧算一次就行). 从 p0 落到 p1 这一段 (终点或中点) 进了哪个胶囊. */
export function hitter(caps) {
  return (p0, p1) => {
    const m = [(p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2, (p0[2] + p1[2]) / 2];
    for (const c of caps()) if (segDist(p1, c.A, c.B) < c.r || segDist(m, c.A, c.B) < c.r) return { part: c.part };
    return null;
  };
}

/** ground 回调: groups 是房间碰撞部件 ([{id, parts: [{min, max}]}]). (x, z) 处、y (再高 2 cm) 以下最高的顶面; 没有就是地面. */
export function grounder(groups) {
  return (x, z, y) => {
    let best = { y: 0, surface: 'floor' };
    for (const g of groups || []) for (const p of g.parts) {
      if (x < p.min[0] || x > p.max[0] || z < p.min[2] || z > p.max[2] || p.max[1] > y + 0.02 || p.max[1] <= best.y) continue;
      best = { y: p.max[1], surface: g.id };
    }
    return best;
  };
}
