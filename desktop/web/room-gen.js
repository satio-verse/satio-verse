// 删除代次 (服务端 ~/.worldmodel/experience/generation.json, wm/cognition/store.py): 你在设置里删了她的经历、或完整重置了
// 小窝学习, 服务端代次加一. 页面记着自己清到第几代 (GEN_KEY); 连上时 (hello.room_gen, 或者服务端推的 room_gen) 见到更大的,
// 按那几次的范围清掉本机存的 (2026-10-28 用户贴的评审: "确认重启和重新打开页面不会从旧数据重放回来").
// 这里只算"该清哪些", 不碰存储, 好单测; 读写和重载在 pet.js.
export const GEN_KEY = 'pet.room.gen';
// 每种范围要清的本机键. experience: 服务端发现簿在本机的那份; page: 小屋练习参数.
// 不清: pet.room.world (环境本身: 书好不好看、土湿不湿、灯开没开)、layout、rigid.poses、liquid、body、needs.
export const CLEAR = { experience: ['pet.room.known'], page: ['pet.room.practice'] };

/** A global deletion generation clears matching spatial copies in every saved
 * scene; personal needs and physical world state are deliberately untouched. */
export function roomGenKeys(keys, storedKeys = []) {
  const out = new Set(keys);
  const suffixes = new Set(keys.filter((k) => k.startsWith('pet.room.')).map((k) => k.slice(9)));
  for (const key of storedKeys) {
    if (typeof key !== 'string') continue;
    const m = /^pet\.room\.scene\.[a-z0-9][a-z0-9-]{0,63}\.(.+)$/.exec(key);
    if (m && suffixes.has(m[1])) out.add(key);
  }
  return [...out];
}

/**
 * gen: 服务端现在的代次; mine: 本机清到的代次; info: GET /api/experience 的回包 ({generation, last_reset: {gen, scope},
 * history?: [{gen, scope}]}), 拿不到时 null.
 * 返回 null: 不用管; {retry: true}: 要清但不知道范围 (回包没拿到), 这次先不动、本机代次也不改, 下次连上再看;
 * {gen, keys, scopes, guessed}: 清这些键, 本机代次记成 gen. guessed: 中间有几次的范围不知道 (只记了最后一次), 按全部范围清
 * (宁可多清一样她自己能重新练回来的, 也不把你删过的留着).
 */
export function roomGenPlan(gen, mine, info) {
  if (!Number.isInteger(gen) || gen <= 0) return null;
  const done = Number.isInteger(mine) && mine > 0 ? mine : 0;
  if (gen <= done) return null;
  if (!info) return { retry: true };
  const hist = (Array.isArray(info.history) ? info.history : info.last_reset ? [info.last_reset] : [])
    .filter((h) => Number.isInteger(h?.gen) && h.gen > done && h.gen <= gen);
  const seen = new Set(hist.map((h) => h.gen));
  let guessed = false;
  for (let g = done + 1; g <= gen; g++) if (!seen.has(g)) guessed = true;
  const scopes = new Set();
  for (const h of hist) for (const s of Array.isArray(h.scope) ? h.scope : Object.keys(CLEAR)) scopes.add(s);
  if (guessed) for (const s of Object.keys(CLEAR)) scopes.add(s);
  const keys = [...new Set([...scopes].flatMap((s) => CLEAR[s] || []))];
  return { gen, keys, scopes: [...scopes], guessed };
}
