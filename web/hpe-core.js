// 人体姿态估计的归纳部分 (纯计算, Node 里可单测): MediaPipe Pose Landmarker 的 33 个关键点 (图像归一化坐标,
// x 向右、y 向下, 带可见度) -> 几个固定取值的字段, 并进角色状态发给服务端. 关键点的"左右"是被拍的人自己的左右.
export const LM = { nose: 0, lShoulder: 11, rShoulder: 12, lElbow: 13, rElbow: 14, lWrist: 15, rWrist: 16,
  lHip: 23, rHip: 24, lKnee: 25, rKnee: 26 };

const vis = (p) => (p ? (p.visibility ?? 1) : 0);

/** 挥手检测: 举着的手腕在最近 windowS 秒里左右往返的次数 (每次幅度超过 minAmp, 以肩宽为单位). */
export function countSwings(track, now, { windowS = 1.5, minAmp = 0.25 } = {}) {
  const pts = track.filter((p) => now - p.t <= windowS);
  let swings = 0, dir = 0, ext = pts[0]?.x ?? 0;
  for (const p of pts) {
    const d = p.x - ext;
    if (dir >= 0 && d < -minAmp) { if (dir > 0) swings++; dir = -1; ext = p.x; }
    else if (dir <= 0 && d > minAmp) { if (dir < 0) swings++; dir = 1; ext = p.x; }
    else if ((dir > 0 && p.x > ext) || (dir < 0 && p.x < ext) || dir === 0) ext = dir === 0 ? ext : p.x;
  }
  return swings;
}

/**
 * 一帧的关键点 -> {present, posture, raised, waving, facing}. track: 每只手腕的历史 (由调用方保存, 这里追加).
 * posture: 看得见膝盖时按大腿倾角判断 (大腿偏离竖直 50 度以上算坐着), 看不见 (桌前只拍到上半身) 为 unknown.
 */
export function summarize(lm, now, track = { left: [], right: [] }) {
  if (!lm || lm.length < 27) return { present: false };
  const ls = lm[LM.lShoulder], rs = lm[LM.rShoulder];
  if (vis(ls) < 0.5 || vis(rs) < 0.5) return { present: false };
  const sw = Math.hypot(ls.x - rs.x, ls.y - rs.y) || 1e-3;  // 肩宽 (图像单位), 其他距离都按它归一化
  // 是否面对镜头: 侧过身时肩宽相对躯干长度会缩小 (正面约 0.6-0.8, 侧身 0.2 左右). 不用关键点的深度 z:
  // 对二次元角色和戴眼镜、长发遮脸的人都不可靠
  const lh = lm[LM.lHip], rh = lm[LM.rHip];
  const torso = vis(lh) > 0.5 && vis(rh) > 0.5 ? Math.hypot((ls.x + rs.x - lh.x - rh.x) / 2, (ls.y + rs.y - lh.y - rh.y) / 2) : sw / 0.7;
  const out = { present: true, raised: 'none', waving: false, facing: sw > 0.35 * torso };
  const up = { left: false, right: false };
  for (const [side, w, s] of [['left', LM.lWrist, ls], ['right', LM.rWrist, rs]]) {
    const p = lm[w];
    if (vis(p) < 0.5) continue;
    up[side] = s.y - p.y > 0.15 * sw;  // 手腕高过肩
    const tr = (track[side] ||= []);
    tr.push({ t: now, x: p.x / sw });
    while (tr.length && now - tr[0].t > 2) tr.shift();
    if (up[side] && countSwings(tr, now) >= 3) out.waving = true;
  }
  out.raised = up.left && up.right ? 'both' : up.left ? 'left' : up.right ? 'right' : 'none';
  const thigh = [[LM.lHip, LM.lKnee], [LM.rHip, LM.rKnee]].filter(([h, k]) => vis(lm[h]) > 0.5 && vis(lm[k]) > 0.5);
  if (!thigh.length) out.posture = 'unknown';
  else {
    const tilt = Math.max(...thigh.map(([h, k]) => Math.atan2(Math.abs(lm[k].x - lm[h].x), Math.max(1e-6, lm[k].y - lm[h].y)) * 180 / Math.PI));
    out.posture = tilt > 50 ? 'sitting' : 'standing';
  }
  return out;
}

/** 给人看的一句话 (界面上显示). */
export function describe(s) {
  if (!s?.present) return '镜头里没有人';
  const parts = [{ standing: '站着', sitting: '坐着' }[s.posture] || '只拍到上半身'];
  if (s.waving) parts.push('在挥手');
  else if (s.raised !== 'none') parts.push({ left: '举着左手', right: '举着右手', both: '举着双手' }[s.raised]);
  if (!s.facing) parts.push('没有正对镜头');
  return parts.join(' · ');
}
