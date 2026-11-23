// 小屋性能优化的开关 (2026-10-27, 见知识库「小屋性能实验记录」): 默认都开; 页面参数 perfoff=matrix,ray,... 关掉某几项, perfoff=all 全关.
// 只为 A/B 对比 (tools/room_perf.mjs 同一份代码开关两边各跑一次), 用户版不用管.
//   matrix   角色骨架 updateMatrixWorld 的快速版 (fast-matrix.js)
//   ray      液体落点往下照房间的快速版 (room-ray.js)
//   spring   弹簧骨一帧最多拆几步 (springs.js)
//   warm     载入后预编译会挪进活动层的东西的着色器 (room-life.js)
//   node     props.node 记住找到的节点 (room-props.js)
//   proxy    弹簧骨中心节点的逆矩阵缓存不用 Proxy (web/spring-center.js, 和网页版共用; 那边自己读这个参数)
//   puddles  水渍、印子用两张实例化网格画 (room-puddles.js, room-liquid.js)
const off = new Set(String(new URLSearchParams(location.search).get('perfoff') || '').split(',').filter(Boolean));

export const perfOn = (name) => !off.has('all') && !off.has(name);
