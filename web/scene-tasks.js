// 场景能做的事 (执行评估、体检用): 从场景 JSON 推出走路、坐下、拿放的目标, 场景数据和房间模型对不对得上的静态检查.
// 换场景不用改评估代码. 纯函数, 不依赖 three.js, 可以在 Node 里单测.
import { toCell, walkable } from './nav.js';
import { furnitureBoxes } from './pain.js';
import { handActionBand } from './reach-safety.js';

/** 场景 JSON -> 能走到的目标、能坐的座位、能拿的东西 (和它原来在哪)、能放东西的台面. */
export function sceneTasks(scene) {
  const objs = scene.objects || [];
  return {
    walk: objs.filter((o) => o.approach && !o.item).map((o) => o.id),
    seats: objs.filter((o) => o.seat).map((o) => o.id),
    items: objs.filter((o) => o.item).map((o) => ({ id: o.id, on: o.item.on })),
    places: objs.filter((o) => o.place).map((o) => ({ id: o.id, y: o.place[1] })),
  };
}

/** 默认评估里走路、坐下、拿放用哪些目标. 场景 JSON 的 eval 段指定了就用它 (room.json 指定的就是最早手写的那几个,
 *  以前的结果还能直接比); 没指定就挑: 离出生点最远的目标再到离它最远的目标; 第一个座位; 放在地上的东西 (要蹲下)
 *  优先, 放到最矮的台面 (膝盖最容易碰到). 做不了的一类为 null. */
export function scenePicks(scene) {
  const t = sceneTasks(scene), ev = scene.eval || {};
  const at = (id) => scene.objects.find((o) => o.id === id)?.approach;
  const far = (from, ids) => ids.reduce((b, id) => {
    const p = at(id), d = Math.hypot(p[0] - from[0], p[1] - from[1]);
    return !b || d > b.d ? { id, d } : b;
  }, null)?.id;
  let walk = ev.walk || null;
  if (!walk && t.walk.length >= 2) {
    const a = far(scene.spawn?.pos || [0, 0], t.walk);
    walk = [a, far(at(a), t.walk.filter((id) => id !== a))];
  }
  let pickput = ev.pickput || null;
  if (!pickput && t.items.length && t.places.length) {
    const item = t.items.find((i) => i.on === 'floor') || t.items[0];
    const places = t.places.filter((p) => p.id !== item.on).sort((a, b) => a.y - b.y);
    if (places.length) pickput = [item.id, places[0].id];
  }
  return { walk, sit: ev.sit || t.seats[0] || null, pickput };
}

/** 默认的一组场景 (调优用): 待机、走路、坐下站起、拿放、手势、说话, 场景里做不了的那类不跑. */
export function defaultScenarios(scene, loco = null) {
  const p = scenePicks(scene);
  return ['idle', p.walk && 'walk', p.sit && 'sit', p.pickput && 'pickput', (!loco || loco.gestDB) && 'gesture', 'talk'].filter(Boolean);
}

/** 体检用的全套: 默认那几个场景之外, 伸手邀请牵手 (场景里有用户这个目标时)、表情动作 (有行动层时), 走到每个目标、坐每个座位、
 *  每件东西放到每个别的台面. */
export function sweepScenarios(scene, loco = null) {
  const t = sceneTasks(scene), user = (scene.objects || []).some((o) => o.id === 'user');
  return [...defaultScenarios(scene, loco), ...(user ? ['hand'] : []), ...(loco ? ['emote'] : []),
    ...t.walk.map((id) => `walk:${id}`), ...t.seats.map((id) => `sit:${id}`),
    ...t.items.flatMap((i) => t.places.filter((p) => p.id !== i.on).map((p) => `pickput:${i.id}>${p.id}`))];
}

/** 场景名 -> [类, 目标列表或 undefined]: 'pickput:ball>coffee_table' -> ['pickput', ['ball', 'coffee_table']]. */
export function parseScenario(name) {
  const i = name.indexOf(':');
  return i < 0 ? [name, undefined] : [name.slice(0, i), name.slice(i + 1).split('>')];
}

/** 场景检查: 换了场景 (或改了家具) 时先看这些, 很多"碰家具""物品穿模"其实是场景数据和模型对不上.
 *  parts: 房间的碰撞体 (scene.js 的 collisionParts). 返回 [{level: 'warn' | 'info', what, msg}]. 只看数据, 不跑动作. */
export function sceneChecks(loco, parts) {
  const out = [], sc = loco.scene;
  const warn = (what, msg) => out.push({ level: 'warn', what, msg }), info = (what, msg) => out.push({ level: 'info', what, msg });
  const cm = (m) => `${(m * 100).toFixed(0)} cm`;
  const byId = new Map((parts || []).map((g) => [g.id, g]));
  // 站在哪: 目标的站位要在能走的地方
  for (const o of sc.objects) {
    if (o.approach && !walkable(loco.grid, ...toCell(loco.grid, ...o.approach))) warn(o.id, '站位 approach 不在能走的地方 (会就近找一个)');
  }
  // 碰撞体: 每件家具要有房间里的网格部件, 占地和场景 JSON 的包围盒 (导航按它绕开) 对得上. 只比水平方向:
  // 高度上模型常常更高 (书桌上的显示器), 那是对的
  for (const b of furnitureBoxes(sc.objects)) {
    const g = byId.get(b.id);
    if (!g) { warn(b.id, '房间里没有它的网格 (scene.js 没画), 碰撞只能用 JSON 包围盒'); continue; }
    const d = Math.max(...[0, 2].flatMap((k) => [Math.abs(g.min[k] - b.min[k]), Math.abs(g.max[k] - b.max[k])]));
    if (d > 0.1) warn(b.id, `占地和 JSON 包围盒差 ${cm(d)} (导航按 JSON 绕开, 量痛觉按模型)`);
    if (g.parts.every((p) => p.part === 'solid') && sc.objects.find((o) => o.id === b.id)?.seat)
      warn(b.id, '能坐但没标部件角色 (scene.js 的 part), 坐着时座面也会算碰家具');
  }
  // 台面高度: 坐的座面、放东西的点、东西原来的位置, 都要和模型表面对得上. 取点周围 r 范围里、不高于 y + above 的
  // 最高表面 (两块坐垫中间有条缝, 座位点正好在缝上)
  const topUnder = (x, z, y, r, id = null, above = 0.05) => {
    let top = null;
    for (const g of parts || []) {
      if (id && g.id !== id) continue;
      for (const p of g.parts) {
        if (x + r < p.min[0] || x - r > p.max[0] || z + r < p.min[2] || z - r > p.max[2] || p.max[1] > y + above) continue;
        if (top === null || p.max[1] > top) top = p.max[1];
      }
    }
    return top;
  };
  for (const o of sc.objects) {
    if (o.seat && byId.has(o.id)) {
      const h = o.seat.height, top = topUnder(o.seat.pos[0], o.seat.pos[1], h + 0.1, 0.1, o.id);
      // 坐垫是软的, 座位可以比坐垫面低几厘米 (坐下去会陷), 不能悬在上面
      if (top === null) warn(o.id, '座位下面找不到座面');
      else if (h > top + 0.02 || h < top - 0.07) warn(o.id, `座位高度 ${h} 和座面 (${top.toFixed(2)}) 差 ${cm(h - top)}`);
    }
    if (o.place && parts) {
      const [x, y, z] = o.place, top = topUnder(x, z, y, 0.03);
      if (top === null) warn(o.id, `放置点 (${o.place.join(', ')}) 下面没有台面, 东西会悬空或插进别的东西`);
      else if (Math.abs(top - y) > 0.03) warn(o.id, `放置点高度 ${y} 和台面 (${top.toFixed(2)}) 差 ${cm(y - top)}`);
    }
    if (o.item && parts) {
      const it = loco.items[o.id], [x, y, z] = o.item.pos, bottom = y - (it?.half ?? 0);
      const top = o.item.on === 'floor' ? 0 : topUnder(x, z, bottom, 0.03, o.item.on, 0.1);  // 陷进台面的也要找到台面
      if (top === null) warn(o.id, `原来放在 ${o.item.on} 上, 但下面没有它的台面`);
      else if (Math.abs(top - bottom) > 0.03) warn(o.id, `底面 ${bottom.toFixed(2)} 和下面的台面 (${top.toFixed(2)}) 差 ${cm(bottom - top)}`);
    }
  }
  // 够不够得着: 低于 0.6 m 用蹲下/弯腰的拿放片段, 0.6-1.0 m 站着伸手 (只靠 IK), 再高用前倾伸手片段 (见 locomotion.js
  // 的 _handAction). 片段里手的高度 (按这个模型的髋高换算) 离目标太远时, 要靠 IK 硬拉手臂
  const range = (kinds) => {
    const hs = kinds.flatMap((k) => (loco.acts?.groups[k] || []).map((g) => g.hand[1] * loco.hipHeight));
    return hs.length ? [Math.min(...hs), Math.max(...hs)] : null;
  };
  const low = range(['pick', 'put']), high = range(['reach']);
  for (const o of sc.objects) {
    const y = o.place?.[1] ?? o.item?.pos[1];
    if (y === undefined) continue;
    const band = handActionBand(y);
    if (band === 'middle') continue;
    const rg = band === 'low' ? low : high;
    if (!rg) { warn(o.id, `高度 ${y} m: 动作库里没有${band === 'low' ? '蹲下拿放' : '前倾伸手'}的片段`); continue; }
    const gap = y < rg[0] ? rg[0] - y : y > rg[1] ? y - rg[1] : 0;
    if (gap > 0.2) info(o.id, `高度 ${y} m: ${band === 'low' ? '蹲下拿放' : '前倾伸手'}片段里手在 ${rg[0].toFixed(2)}-${rg[1].toFixed(2)} m, 要靠 IK 拉 ${cm(gap)}`);
  }
  return out;
}
