import { test } from 'node:test';
import assert from 'node:assert/strict';
import { GRIP, Grip } from '../../web/physics/grip.js';
import { RigidWorld } from '../../web/physics/rigid.js';

// 一张桌子 (台面 0.75 m 高)、地面; 一只马克杯 (圆柱: 半高 5 cm、半径 4.5 cm, 0.7 kg)
async function table() {
  const w = await RigidWorld.create();
  w.addFixedBox('floor', [-5, -0.1, -5], [5, 0, 5]);
  w.addFixedBox('table', [-0.5, 0.7, -0.5], [0.5, 0.75, 0.5]);
  return w;
}
const run = (w, secs, before = null) => { for (let i = 0; i < secs * 60; i++) w.step(1 / 60, before); };

test('放在桌上的杯子落稳、睡着; 推出桌沿会掉到地上, 带着速度', async () => {
  const w = await table();
  w.addDynamic('cup', { shape: { cylinder: [0.05, 0.045] }, mass: 0.7, p: [0, 0.81, 0] });
  run(w, 2);
  const a = w.pose('cup');
  assert.ok(Math.abs(a.p[1] - 0.8) < 0.005, `落在台面上 ${a.p[1].toFixed(3)}`);
  assert.ok(a.sleeping, '不动了就睡着');
  w.setPose('cup', [0.48, 0.8, 0], null, [1.2, 0, 0]);  // 推一下, 往桌沿外
  run(w, 2);
  const b = w.pose('cup');
  assert.ok(b.p[1] < 0.1 && b.p[0] > 0.5, `掉到地上 ${b.p.map((x) => x.toFixed(2))}`);
});

test('抓持: 从桌上拿起来 (握住时差 3 cm)、端着走, 杯子跟得上、稳住; 每帧耗时', async () => {
  const w = await table();
  w.addDynamic('cup', { shape: { cylinder: [0.05, 0.045] }, mass: 0.7, p: [0, 0.81, 0] });
  run(w, 1);
  const g = new Grip(w, 'cup', 'right');
  let t = 0, stable = 0, n = 0;
  for (let i = 0; i < 240; i++) {
    w.step(1 / 60, (h) => {
      t += h;
      // 手先在杯子旁边 3 cm, 0.8 秒抬到 0.95 m, 之后起步 0.6 秒、0.8 m/s 往前
      const lift = Math.min(1, t / 0.8), y = 0.83 + 0.12 * lift * lift * (3 - 2 * lift);
      const tw = Math.max(0, t - 0.8), v = 0.8 * Math.min(1, tw / 0.6), x = 0.03 + (tw < 0.6 ? 0.8 * tw * tw / 1.2 : 0.24 + 0.8 * (tw - 0.6));
      g.step(h, { p: [x, y, 0], q: [0, 0, 0, 1], v: [v, 0, 0] });
    });
    n++;
    if (t > 1 && g.stable) stable++;
  }
  assert.equal(g.state, 'hold');
  assert.ok(stable / (n - 60) > 0.8, `大部分时间稳住了 (${(stable / (n - 60)).toFixed(2)})`);
  assert.ok(g.stats.maxErr < GRIP.slip, `离掌心最多 ${g.stats.maxErr.toFixed(3)} m`);
  assert.ok(w.stats.ms / w.stats.frames < 5, `每帧 ${(w.stats.ms / w.stats.frames).toFixed(3)} ms`);
});

test('抓持: 手往下穿进桌子 (杯子被台面挡住) 时先滑、再脱手, 受阻时力到上限', async () => {
  const w = await table();
  w.addDynamic('cup', { shape: { cylinder: [0.05, 0.045] }, mass: 0.7, p: [0, 0.81, 0] });
  run(w, 1);
  const g = new Grip(w, 'cup', 'right');
  const states = new Set();
  let t = 0;
  for (let i = 0; i < 240 && g.state !== 'drop'; i++) {
    w.step(1 / 60, (h) => {
      t += h;
      g.step(h, { p: [0, 0.81 - 0.3 * t, 0], q: [0, 0, 0, 1], v: [0, -0.3, 0] });  // 手往下按进桌面
      states.add(g.state);
    });
  }
  assert.ok(states.has('slip'), '先滑');
  assert.equal(g.state, 'drop', '最后脱手');
  assert.ok(g.stats.strainedSteps > 0, '受阻时力到上限');
  assert.ok(w.pose('cup').p[1] > 0.79, '杯子停在台面上, 没被按进去');
});

test('力气不够: 握不紧 (strength 小) 时端不住一杯满的, 脱手往下掉', async () => {
  const w = await table();
  w.addDynamic('cup', { shape: { cylinder: [0.05, 0.045] }, mass: 0.7, p: [1, 0.95, 0] });
  const g = new Grip(w, 'cup', 'right', { strength: 0.15 });  // 最多 3.75 N, 托不住 6.9 N 的重量
  for (let i = 0; i < 120 && g.state !== 'drop'; i++) w.step(1 / 60, (h) => g.step(h, { p: [1, 0.95, 0], q: [0, 0, 0, 1], v: [0, 0, 0] }));
  assert.equal(g.state, 'drop');
  run(w, 1);
  assert.ok(w.pose('cup').p[1] < 0.1, '掉到地上');
});
