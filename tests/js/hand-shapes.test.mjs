import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DOF, fingerDrift, flat, SHAPES, targetAngles } from '../../web/hand-shapes.js';

test('手形都在人手的活动范围里, 放松时从食指到小指越来越弯', () => {
  for (const [name, s] of Object.entries(SHAPES)) {
    assert.equal(flat(s).length, DOF, name);
    for (const [mcp, pip, dip, spread] of s.f) {
      assert.ok(mcp >= 0 && mcp <= 90 && pip >= 0 && pip <= 110 && dip >= 0 && dip <= 80, `${name} 弯曲超出范围`);
      assert.ok(Math.abs(spread) <= 20, `${name} 张开太大`);
    }
  }
  const r = SHAPES.relaxed.f;
  for (let i = 1; i < 4; i++) assert.ok(r[i][0] > r[i - 1][0] && r[i][1] > r[i - 1][1], '放松时靠小指一侧弯得多');
  assert.ok(r.every(([m, p, d]) => d < p && d < m + 5), '远节弯得最少');
  const p = SHAPES.point.f;
  assert.ok(p[0][0] < 5 && p[0][1] < 5, '指向时食指伸直');
  assert.ok(p.slice(1).every(([m, pp]) => m >= 60 && pp >= 90), '其余三指收拢');
});

test('混合: 默认放松, 张开 / 指向 / 搭在腿上按权重, 握持压过其他', () => {
  assert.deepEqual(targetAngles({}), flat(SHAPES.relaxed));
  assert.deepEqual(targetAngles({ open: 1 }), flat(SHAPES.open));
  const half = targetAngles({ point: 0.5 });
  flat(SHAPES.relaxed).forEach((v, i) => assert.ok(Math.abs(half[i] - (v + flat(SHAPES.point)[i]) / 2) < 1e-9));
  // 权重加起来超过 1 时按比例缩, 不会弯过头
  const both = targetAngles({ open: 1, lap: 1 });
  flat(SHAPES.open).forEach((v, i) => assert.ok(Math.abs(both[i] - (v + flat(SHAPES.lap)[i]) / 2) < 1e-9));
  assert.deepEqual(targetAngles({ open: 1, point: 1 }, 1, 'book'), flat(SHAPES.book));
  assert.deepEqual(targetAngles({}, 1, 'teapot'), flat(SHAPES.ball), '不认识的东西按球握');
  const custom = { f: [[1, 2, 3, 0], [4, 5, 6, 0], [7, 8, 9, 0], [10, 11, 12, 0]], t: [13, 14, 15] };
  assert.deepEqual(targetAngles({ open: 1 }, 1, custom), flat(custom), '外面指定的握法');
  const g = targetAngles({}, 0.5, 'cup');
  flat(SHAPES.relaxed).forEach((v, i) => assert.ok(Math.abs(g[i] - (v + flat(SHAPES.cup)[i]) / 2) < 1e-9));
});

test('手指的细小起伏: 很小、两只手不同步、随时间变', () => {
  const a = fingerDrift(1.0, 'left'), b = fingerDrift(1.0, 'right'), c = fingerDrift(3.0, 'left');
  assert.equal(a.length, 4);
  for (let t = 0; t < 60; t += 0.37) for (const x of fingerDrift(t, 'right')) assert.ok(Math.abs(x) <= 2.5 + 1e-9);
  assert.notDeepEqual(a, b);
  assert.notDeepEqual(a, c);
});
