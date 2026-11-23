// 身体比例研究的评估探针 (docs/roadmap/motion-growth.md 9.5, 只在评估页面里用, 不进产品): 在动作成长的探针
// (motion-growth-probe.js, 每个场景的失败检测) 之外, 每帧在拿放交接的那些帧 (拿着东西、或者在拿放) 记几个中介量:
//   - 肘弯多少 (拿着东西那只手, 没拿就是在拿放那只手; 上臂和小臂方向的夹角, 0 = 伸直), 接近伸直 (< 15 度) 的帧占多少;
//   - 手一顿一甩 (痛觉 heldAcc / dt, 超出加速度上限的部分) 在接近伸直和不伸直时各多少;
//   - 手臂被防穿模层推开 (痛觉 pushed / dt).
// 单因素改变: 参数里带 morph.* 的不进参数表, 由这里在场景开始前改、跑完改回去:
//   morph.radiusScale  碰撞胶囊 (接触层量出来的身体各段半径, 含两端) 统一乘这个数, 只改碰撞几何, 别的都不动.
// 结果附在每条结果上: results[i].morph = {frames, straightFrac, elbowMean, heldAcc: {straight, bent}, push}.
// tools/motion_eval.mjs --probe motion-morph-probe.js
import { install as growthInstall } from './motion-growth-probe.js';

const STRAIGHT = 15;  // 肘弯小于这么多度算接近伸直 (两骨 IK 的奇异区附近)

export function install(state, me) {
  const inner = growthInstall(state, me) || me;
  const vrm = state.vrm, loco = vrm.loco, kin = vrm.kin, h = vrm.vrm.humanoid;
  let cur = null;
  const done = {};
  const begin = kin.begin.bind(kin), end = kin.end.bind(kin), step = state.step;
  kin.begin = (label) => {
    cur = { label, frames: 0, straight: 0, elbow: 0, acc: { straight: 0, bent: 0 }, accFrames: { straight: 0, bent: 0 }, push: 0 };
    return begin(label);
  };
  kin.end = (result) => {
    if (cur) done[cur.label] = cur;
    cur = null;
    return end(result);
  };
  const W = (n) => { const b = h.getNormalizedBoneNode(n); const e = b.matrixWorld.elements; return [e[12], e[13], e[14]]; };
  const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
  const ang = (u, v) => {
    const d = Math.hypot(...u) * Math.hypot(...v);
    return d > 1e-9 ? Math.acos(Math.max(-1, Math.min(1, (u[0] * v[0] + u[1] * v[1] + u[2] * v[2]) / d))) * 180 / Math.PI : 0;
  };
  state.step = (seconds, dt) => {
    step(seconds, dt);
    if (!cur) return;
    const held = Object.values(loco.items || {}).find((i) => i.held);
    const side = held?.held || loco.action?.grab?.side || loco.reaching?.side || null;
    if (!side) return;
    const S = side === 'left' ? 'left' : 'right';
    const u = W(`${S}UpperArm`), l = W(`${S}LowerArm`), w = W(`${S}Hand`);
    const elbow = ang(sub(l, u), sub(w, l));  // 0 = 伸直
    const p = kin.painNow || {};
    const acc = (p.heldAcc || 0) / Math.max(1e-4, seconds), push = (p.pushed || 0) / Math.max(1e-4, seconds);
    const k = elbow < STRAIGHT ? 'straight' : 'bent';
    cur.frames++;
    cur.elbow += elbow;
    if (k === 'straight') cur.straight++;
    cur.acc[k] += acc * seconds;
    cur.accFrames[k]++;
    cur.push += push * seconds;
  };
  const run = inner.runEval;
  const runEval = async (spec) => {
    for (const key of Object.keys(done)) delete done[key];
    const params = { ...(spec.params || {}) }, morph = {};
    for (const key of Object.keys(params)) if (key.startsWith('morph.')) { morph[key.slice(6)] = params[key]; delete params[key]; }
    const c = vrm.contact, saved = c ? { radius: { ...c.radius }, endR: { ...c.endR }, hang: { ...c.hang } } : null;
    if (c && Number.isFinite(morph.radiusScale)) {
      for (const key of Object.keys(c.radius)) c.radius[key] *= morph.radiusScale;
      for (const key of Object.keys(c.endR)) c.endR[key] *= morph.radiusScale;
      // 手臂下垂不碰身体的外展角是载入时按半径算的: 跟着重算, 不然改的就不只是一样东西
      try { c.hang = c._hangAngles(); } catch { /* 算不了就保持 */ }
    }
    try {
      const res = await run({ ...spec, params });
      for (const r of res.results) {
        const m = done[`${r.scenario}#${r.seed}`];
        if (!m) continue;
        r.morph = { frames: m.frames, straightFrac: m.frames ? +(m.straight / m.frames).toFixed(3) : null,
          elbowMean: m.frames ? +(m.elbow / m.frames).toFixed(1) : null,
          heldAcc: { straight: +m.acc.straight.toFixed(3), bent: +m.acc.bent.toFixed(3) },
          heldAccFrames: { ...m.accFrames }, push: +m.push.toFixed(4), morph };
      }
      return res;
    } finally {
      if (c && saved) { Object.assign(c.radius, saved.radius); Object.assign(c.endR, saved.endR); c.hang = saved.hang; }
    }
  };
  return { ...inner, runEval };
}
