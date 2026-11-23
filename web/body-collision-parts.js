import { declaredCollisionParts } from './scene-colliders.js';

/** Compile immutable imported geometry once. Only activity exemptions vary per frame.
 * No declaration returns null so callers retain their exact legacy collision path.
 * Navigation's walkable flag and coarse furniture slabs do not erase authored solids.
 */
export function createBodyPartBoxes(scene, softByKind = {}) {
  const declared = declaredCollisionParts(scene);
  if (declared === null) return null;
  const owners = new Map([...(scene.objects || []), ...(scene.decor || [])].map(o => [o.id, o]));
  const groups = declared.map(group => {
    const owner = owners.get(group.id), kind = owner?.kind;
    const center = [(group.min[0] + group.max[0]) / 2, (group.min[2] + group.max[2]) / 2];
    const soft = softByKind[kind];
    const boxes = group.parts.map(part => ({
      center: [(part.min[0] + part.max[0]) / 2, (part.min[2] + part.max[2]) / 2],
      size: [part.max[0] - part.min[0], part.max[2] - part.min[2]],
      y0: part.min[1], y1: part.max[1], id: group.id, name: owner?.name || group.id, soft,
    }));
    const squeezing = boxes.map(box => ({ ...box, soft: Math.max(.05, soft ?? 0) }));
    return { id: group.id, owner, kind, center, boxes, squeezing };
  });
  return ({ seat = null, lying = false, pushing = null, squeezing = false } = {}) => {
    const out = [];
    for (const group of groups) {
      const { owner, kind, center } = group;
      if (pushing?.has(group.id)) continue;
      if (seat && owner?.seat && Math.hypot(owner.seat.pos[0] - seat.pos[0], owner.seat.pos[1] - seat.pos[1]) < .05) continue;
      const bedCenter = owner?.box?.center || center;
      if ((lying || seat) && kind === 'bed' && (lying || Math.hypot(seat.pos[0] - bedCenter[0], seat.pos[1] - bedCenter[1]) < 1.6)) continue;
      out.push(...(squeezing ? group.squeezing : group.boxes));
    }
    return out;
  };
}
