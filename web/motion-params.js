// 运动层的可调参数: 自动调优 (tools/tune_motion.py) 的搜索空间, 以及调好的值 (动作库目录的 params.json) 的读写.
// 参数本身在 locomotion.js 的 TUNE、pose-filter.js 的 LIMITS、contact.js 的 CONTACT 里, 这里按名字读写
// (targets = {tune: TUNE, filter: LIMITS, contact: CONTACT}),
// 不直接 import 它们 (locomotion.js 要 import 这里, 也能在 Node 里单测).
//
// 名字: "tune.lockIn" -> TUNE.lockIn; "filter.arm.acc" -> LIMITS.arm.acc; "filter.arm.euro.0" -> LIMITS.arm.euro[0];
// "contact.spread" -> contact.js 的 CONTACT.spread.
// log: 按对数均匀搜索 (半衰期、加速度这类跨量级的).

export const PARAM_SPACE = [
  // 姿态平滑层 (pose-filter.js): 最大角加速度 (rad/s^2), One Euro 最低截止频率 (Hz)
  { key: 'filter.arm.acc', lo: 100, hi: 1000, log: true },
  { key: 'filter.hand.acc', lo: 150, hi: 1200, log: true },
  { key: 'filter.core.acc', lo: 20, hi: 200, log: true },
  { key: 'filter.hips.acc', lo: 20, hi: 200, log: true },
  { key: 'filter.leg.acc', lo: 60, hi: 600, log: true },
  { key: 'filter.arm.euro.0', lo: 3, hi: 20, log: true },
  { key: 'filter.core.euro.0', lo: 3, hi: 20, log: true },
  // 脚的锁定、骨盆补偿、落地、配平 (locomotion.js 的 TUNE)
  { key: 'tune.lockIn', lo: 0.01, hi: 0.15, log: true },
  { key: 'tune.lockOut', lo: 0.02, hi: 0.2, log: true },
  { key: 'tune.lockSettle', lo: 0.02, hi: 0.2, log: true },
  { key: 'tune.replant', lo: 0.2, hi: 0.5 },
  { key: 'tune.hipDropDown', lo: 0.02, hi: 0.3, log: true },
  { key: 'tune.hipDropUp', lo: 0.05, hi: 0.6, log: true },
  { key: 'tune.groundRaise', lo: 0.003, hi: 0.05, log: true },
  { key: 'tune.groundLower', lo: 0.01, hi: 0.15, log: true },
  { key: 'tune.balanceMargin', lo: 0, hi: 0.04 },
  { key: 'tune.balanceMax', lo: 0.02, hi: 0.1 },
  { key: 'tune.balanceMaxSingle', lo: 0.02, hi: 0.15 },
  { key: 'tune.balanceAnkle', lo: 0, hi: 1 },
  { key: 'tune.balanceIn', lo: 0.08, hi: 0.8, log: true },
  { key: 'tune.balanceOut', lo: 0.1, hi: 1.2, log: true },
  // 惯性化过渡的半衰期 (秒)
  { key: 'tune.blendMatch', lo: 0.05, hi: 0.3, log: true },
  { key: 'tune.blendLeaveIdle', lo: 0.08, hi: 0.5, log: true },
  { key: 'tune.blendToLoco', lo: 0.1, hi: 0.5, log: true },
  { key: 'tune.blendIdle', lo: 0.2, hi: 0.9, log: true },
  { key: 'tune.blendGestureIn', lo: 0.12, hi: 0.6, log: true },
  { key: 'tune.blendAction', lo: 0.06, hi: 0.35, log: true },
  // 坐下站起、手放腿上、拿放的落脚 (locomotion.js 的 TUNE, 2026-10-25 加): 起身时根部追脚的时间常数、手绕开大腿的平滑、
  // 拿放时放开重新落脚的距离. 风格那一组 (calm、stance、gestureSpeed 等) 不放进来, 见 TUNE 里的说明
  { key: 'tune.riseTau', lo: 0.1, hi: 0.6, log: true },
  { key: 'tune.lapClear', lo: 0.03, hi: 0.3, log: true },
  { key: 'tune.grabReplant', lo: 0.25, hi: 0.5 },
  // 拿放的交接时长 (秒, 见 locomotion.js 的 TUNE; 2026-10-27 动作成长会话要的): 拿住以后送到端着的位置、放下前从端着的位置走到放下的点.
  // 0 = 关掉 (对比用), 不在范围里. 放下前的上限 1.5 = 动作库里最长的放下片段, 默认就是它 (都整段走)
  { key: 'tune.handoffPick', lo: 0.3, hi: 1.8, log: true },
  { key: 'tune.handoffPut', lo: 0.2, hi: 1.5, log: true },
  // 按身体比例的手臂重定向修正 (contact.js): 补的外展角乘的系数
  { key: 'contact.spread', lo: 0, hi: 1.6 },
];

function slot(targets, key) {
  const [root, ...path] = key.split('.');
  let obj = targets[root];
  for (const p of path.slice(0, -1)) obj = obj?.[p];
  const last = path[path.length - 1];
  if (!obj || !(last in obj)) throw new Error(`没有这个参数: ${key}`);
  return [obj, last];
}

export function getParam(targets, key) {
  const [obj, k] = slot(targets, key);
  return obj[k];
}

export function setParam(targets, key, value) {
  if (!Number.isFinite(value)) throw new Error(`参数 ${key} 不是数: ${value}`);
  const [obj, k] = slot(targets, key);
  obj[k] = value;
}

/** 当前值 {名字: 值} (默认是整个搜索空间). */
export function readParams(targets, keys = PARAM_SPACE.map((p) => p.key)) {
  return Object.fromEntries(keys.map((k) => [k, getParam(targets, k)]));
}

/** 写入一组值 (不认识的名字报错, 免得调好的参数悄悄没生效). 返回写之前的值, 方便恢复. */
export function applyParams(targets, values = {}) {
  const before = readParams(targets, Object.keys(values));
  for (const [k, v] of Object.entries(values)) setParam(targets, k, v);
  return before;
}
