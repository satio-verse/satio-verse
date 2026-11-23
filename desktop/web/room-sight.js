// 她看得到什么 (认知阶段 2, 见 wm/cognition/ 和根目录 README "小窝认知"). 小屋页面是环境, 只报她这一刻看得到的:
// 站在哪一处、那一处的台面上有什么、打开着的冰箱里有什么、手里拿着什么. 她以为东西在哪、怎么找, 由服务端按这些算
// (上行 room_observe / room_search). 页面不再替她"知道"没看到的事 (以前冲咖啡人还没过去就知道杯子不在).
// 和 wm/cognition/layout.py 是同一张表 (编号、站位, tests/test_room_sight.py 核对); 桌宠小屋还没有抽屉, 这里只有台面和冰箱.
// 不 import three: 东西在哪由调用方给 ([x, y, z] / 'hand' / null 看不见), 这里只算归到哪一处、看不看得到 (node --test 能测).

// 能站的几处: 编号 -> 站位 [x, z]
export const ZONES = {
  coffee: [2.15, -1.24], fridge: [1.78, -1.12], desk: [0.95, -1.15], bed: [-0.62, -0.95],
  reading: [-1.6, 2.12], yoga: [1.65, 1.0], window: [-2.3, 0.2],
};
const ZONE_R = 0.9;  // 离站位多远以内算在这一处 (坐在阅读椅上离站位 0.54 m)
// 站位在窄地方、平常寻路走不到的几处: 找东西时走到这里, 站在这里也算在那一处. 阅读椅前面只有 0.34-0.42 m 的缝
// (坐下时侧着身子挤进去), 站到小边桌这边 (场景表 surf_table 的 approach), 小边桌和阅读椅都看得到
export const WALK = { reading: [-0.95, 1.2] };
// 也算在这一处的点 (座位离站位远): 坐在床上 (休息) 的座位离窗边站位只有 0.4 m、离床边站位 1.64 m,
// 不加的话坐在床上算"窗边", 看不到床上和床头柜 (2026-10-27 用户的桌宠上看到的)
const ALSO = { bed: [[-2.0, -0.06]] };
// 放东西的地方: 在哪一处、放置点 [x, y, z]、多远以内算放在这里 (米)、要不要打开才看得到 (box).
// 台面的放置点取自场景表 (targets 的 place); 咖啡机上是杯子原来的位置, 载入时按房间里杯子的位置补 (setPlace)
export const REGIONS = {
  coffee_machine: { zone: 'coffee', at: [2.3, 1.0, -1.95], r: 0.35 },
  water_caddy: { zone: 'coffee', at: [2.68, 0.65, -0.95], r: 0.3 },
  fridge: { zone: 'fridge', at: [1.92, 0.72, -1.78], r: 0.45, box: true },
  surf_desk: { zone: 'desk', at: [0.95, 0.77, -1.78], r: 0.6 },
  surf_bed: { zone: 'bed', at: [-1.45, 0.72, -0.42], r: 0.9 },
  nightstand: { zone: 'bed', at: [-0.89, 0.5, -1.94], r: 0.3 },
  surf_table: { zone: 'reading', at: [-1.05, 0.51, 1.7], r: 0.35 },
  surf_chair: { zone: 'reading', at: [-1.76, 0.5, 1.6], r: 0.35 },
  surf_yoga: { zone: 'yoga', at: [2.05, 0.0, 1.95], r: 0.9 },
};
// 记着的东西 (和 layout.py 的 ITEMS 一样): 杯子
export const ITEMS = ['IR_Item_Espresso_Pink', 'IR_Item_Espresso_White', 'IR_Item_Mug_0', 'IR_Item_Mug_1'];
export const FAVORITE = 'IR_Item_Espresso_Pink';
export const NAMES = {
  IR_Item_Espresso_Pink: '粉色浓缩咖啡杯', IR_Item_Espresso_White: '白色浓缩咖啡杯', IR_Item_Mug_0: '奶油白马克杯', IR_Item_Mug_1: '雾粉马克杯',
  coffee_machine: '咖啡机上', water_caddy: '推车上', fridge: '冰箱里', surf_desk: '电脑桌上', surf_bed: '床上', nightstand: '床头柜上',
  surf_table: '小边桌上', surf_chair: '阅读椅上', surf_yoga: '瑜伽区的地上',
};

const clone = (value) => JSON.parse(JSON.stringify(value));
const legacyDefinition = clone({ zones: ZONES, walk: WALK, also: ALSO, regions: REGIONS, items: ITEMS,
  names: NAMES, favorite: FAVORITE, kinds: Object.fromEntries(ITEMS.map((id) => [id, 'cup'])), kindNames: { cup: '杯子' } });
const record = (x) => x !== null && typeof x === 'object' && !Array.isArray(x);
const point = (x, n) => Array.isArray(x) && x.length === n && x.every(Number.isFinite);
const safeId = (id) => typeof id === 'string' && /^[A-Za-z][A-Za-z0-9_-]{0,95}$/.test(id)
  && !['constructor', 'prototype', '__proto__'].includes(id);

/** Profile maps are validated and copied; changing a table in one room never changes another room or the legacy exports. */
export function createSight(spec) {
  if (spec === undefined) return buildSight(clone(legacyDefinition));
  const bad = () => { throw new Error('小屋 sight 地图无效或缺少字段'); };
  if (!record(spec) || spec.version !== 1) bad();
  for (const key of ['zones', 'walk', 'also', 'regions', 'names', 'kinds', 'kindNames']) if (!record(spec[key])) bad();
  const zoneIds = Object.keys(spec.zones), regionIds = Object.keys(spec.regions);
  if (!zoneIds.length || !regionIds.length || !zoneIds.every(safeId) || !regionIds.every(safeId)) bad();
  for (const p of Object.values(spec.zones)) if (!point(p, 2)) bad();
  for (const [id, p] of Object.entries(spec.walk)) if (!zoneIds.includes(id) || !point(p, 2)) bad();
  for (const [id, points] of Object.entries(spec.also)) if (!zoneIds.includes(id) || !Array.isArray(points) || !points.every((p) => point(p, 2))) bad();
  for (const region of Object.values(spec.regions)) {
    if (!record(region) || !zoneIds.includes(region.zone) || !point(region.at, 3) || !Number.isFinite(region.r) || region.r <= 0
      || (region.box !== undefined && typeof region.box !== 'boolean')) bad();
  }
  if (!Array.isArray(spec.items) || !spec.items.length || !spec.items.every(safeId) || new Set(spec.items).size !== spec.items.length) bad();
  if (!spec.items.includes(spec.favorite)) bad();
  for (const id of [...zoneIds, ...regionIds, ...spec.items]) if (typeof spec.names[id] !== 'string' || !spec.names[id].trim()) bad();
  for (const id of spec.items) if (!safeId(spec.kinds[id]) || typeof spec.kindNames[spec.kinds[id]] !== 'string' || !spec.kindNames[spec.kinds[id]].trim()) bad();
  return buildSight(clone(spec));
}

function buildSight(def) {
  const { zones, walk, also, regions, items, names, favorite, kinds, kindNames } = def;
  const map = { ZONES: zones, WALK: walk, REGIONS: regions, ITEMS: items, NAMES: names, FAVORITE: favorite ?? null,
    KINDS: kinds, KIND_NAMES: kindNames, PET_REGIONS: Object.keys(regions) };
  map.setPlace = (region, at) => { if (regions[region] && point(at, 3)) regions[region].at = at.slice(); };
  map.zoneAt = (x, z) => {
    let best = null, bestD = ZONE_R;
    for (const [id, p] of Object.entries(zones)) for (const [zx, zz] of [p, walk[id], ...(also[id] || [])].filter(Boolean)) {
      const d = Math.hypot(x - zx, z - zz);
      if (d < bestD) { best = id; bestD = d; }
    }
    return best;
  };
  map.regionAt = (pos) => {
    if (!pos) return null;
    let best = null, bestD = Infinity;
    for (const [id, g] of Object.entries(regions)) {
      const d = Math.hypot(pos[0] - g.at[0], (pos[1] - g.at[1]) * 0.5, pos[2] - g.at[2]);
      if (d < g.r && d < bestD) { best = id; bestD = d; }
    }
    return best;
  };
  map.visible = (at, open = []) => Object.entries(regions).filter(([id, g]) => g.zone === at && (!g.box || open.includes(id))).map(([id]) => id);
  map.observe = ({ x, z, items: positions = {}, where = {}, open = [] }) => {
    const at = map.zoneAt(x, z);
    if (!at) return null;
    const seen = Object.fromEntries(map.visible(at, open).map((id) => [id, []])), hand = [];
    for (const id of items) {
      const p = positions[id];
      if (p === 'hand') { hand.push(id); continue; }
      const region = where[id] ?? map.regionAt(p);
      if (region && seen[region]) seen[region].push(id);
    }
    for (const list of Object.values(seen)) list.sort();
    return { at, seen, hand: hand.sort(), open: open.filter((id) => regions[id]?.zone === at).sort() };
  };
  map.witnessed = (at, id, from, to) => {
    const nearFrom = !!from && regions[from]?.zone === at, nearTo = !!to && regions[to]?.zone === at;
    return !at || !(nearFrom || nearTo) ? null : { kind: 'moved', item: id, from: nearFrom ? from : null, to: nearTo ? to : null };
  };
  map.standFor = (zone, region = null) => {
    const p = walk[zone] || zones[zone];
    if (!p) throw new Error(`小屋没有这个站位: ${zone}`);
    const [x, z] = p, g = region ? regions[region] : Object.values(regions).find((v) => v.zone === zone);
    return { xz: [x, z], yaw: g ? Math.atan2(g.at[0] - x, g.at[2] - z) : null };
  };
  return map;
}

// Backward-compatible exports for existing callers/tests; new rooms use their own map.
const legacySight = buildSight({ ...legacyDefinition, zones: ZONES, walk: WALK, also: ALSO, regions: REGIONS, items: ITEMS, names: NAMES });
export const PET_REGIONS = legacySight.PET_REGIONS;
export const { setPlace, zoneAt, regionAt, visible, observe, witnessed, standFor } = legacySight;
