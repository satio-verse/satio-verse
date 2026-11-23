import { test } from 'node:test';
import assert from 'node:assert/strict';
import { handActionBand } from '../../web/reach-safety.js';

test('GLB 台面相减的舍入误差不把放回分到另一种动作, 毫米差仍保留', () => {
  assert.equal(handActionBand(0.71930330991745 - 0.11930331587791443), 'middle');
  for (const h of [0.6, 0.599999994, 0.999]) assert.equal(handActionBand(h), 'middle');
  for (const h of [0, 0.599, 0.5999]) assert.equal(handActionBand(h), 'low');
  for (const h of [0.999999999, 1, 1.001]) assert.equal(handActionBand(h), 'high');
});
