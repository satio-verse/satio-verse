import test from 'node:test';
import assert from 'node:assert/strict';
import { declaredCollisionParts } from '../../web/scene-colliders.js';

const table = () => ({ id: 'table', min: [-1, 0, -1], max: [1, 1, 1], parts: [
  { part: 'top', min: [-1, .9, -1], max: [1, 1, 1] },
  { part: 'leg', min: [-1, 0, -1], max: [-.9, .9, -.9] },
] });
test('ordinary scenes retain mesh-derived collisions; explicit empty list remains empty', () => {
  assert.equal(declaredCollisionParts({}), null);
  assert.deepEqual(declaredCollisionParts({ collisionParts: [] }), []);
});
test('imported parts preserve empty space below tabletop and are independent copies', () => {
  const g = table(), out = declaredCollisionParts({ collisionParts: [g] });
  assert.equal(out[0].parts.length, 2);
  assert.equal(out[0].parts.some(p => [0, .5, 0].every((v,k) => v >= p.min[k] && v <= p.max[k])), false);
  out[0].parts[0].min[0] = -99;
  assert.equal(g.parts[0].min[0], -1);
});
test('malformed imported bounds cannot silently fall back to empty collisions', () => {
  for (const patch of [g => g.parts[0].max[0] = NaN, g => g.parts = [], g => g.max[0] = 20,
    g => g.parts[0].min[0] = 1]) {
    const g = table(); patch(g);
    assert.throws(() => declaredCollisionParts({ collisionParts: [g] }));
  }
  assert.throws(() => declaredCollisionParts({ collisionParts: [table(), table()] }));
});
