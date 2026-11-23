// Imported rooms may have merged render meshes. Their authored collision parts
// stay separate from navigation boxes, so a table is not a single solid block.
export function declaredCollisionParts(scene) {
  if (scene?.collisionParts === undefined) return null;
  const groups = scene.collisionParts;
  if (!Array.isArray(groups) || groups.length > 4096) throw new Error('场景碰撞分组无效');
  const ids = new Set();
  let total = 0;
  const point = (p) => Array.isArray(p) && p.length === 3 && p.every((x) => Number.isFinite(x) && Math.abs(x) < 10000);
  return groups.map((g) => {
    if (!g || typeof g.id !== 'string' || !g.id || ids.has(g.id) || !Array.isArray(g.parts) || !g.parts.length)
      throw new Error('场景碰撞分组缺少部件或编号重复');
    ids.add(g.id);
    const parts = g.parts.map((p) => {
      if (++total > 16384 || !point(p?.min) || !point(p?.max) || p.min.some((v, k) => v >= p.max[k]))
        throw new Error('场景碰撞部件范围无效');
      if (p.part !== undefined && (typeof p.part !== 'string' || !p.part)) throw new Error('场景碰撞部件角色无效');
      return { part: p.part || 'solid', min: p.min.slice(), max: p.max.slice() };
    });
    const min = [0, 1, 2].map((k) => Math.min(...parts.map((p) => p.min[k])));
    const max = [0, 1, 2].map((k) => Math.max(...parts.map((p) => p.max[k])));
    if (!point(g.min) || !point(g.max) || min.some((v, k) => Math.abs(v - g.min[k]) > 1e-5) || max.some((v, k) => Math.abs(v - g.max[k]) > 1e-5))
      throw new Error('场景碰撞粗筛范围与部件不一致');
    return { id: g.id, min, max, parts };
  });
}
