import { test } from 'node:test';
import assert from 'node:assert/strict';
import { causeAt, Excess, FAULTS, FaultDetector, familyOf, gripEvents, Sensor, signalValue, spillEvents } from '../../web/motion-faults.js';
import { PAIN_TERMS } from '../../web/pain.js';

const DT = 1 / 30;
const sig = (t, { pain = {}, f = {}, ctx = {} } = {}) => ({ t, dt: DT, pain, f: { handJump: 0, handDrop: 0, holdGap: 0, spillMl: 0, dropped: 0, ...f },
  ctx: { mode: 'idle', action: null, held: [], ...ctx } });
const carrying = { mode: 'go', held: [{ id: 'cup', side: 'right', physical: true }], goT0: 0 };

test('动作成长: 种类表读的痛觉项都在 pain.js 里, motion 类都有阈值', () => {
  for (const [k, d] of Object.entries(FAULTS)) {
    for (const s of d.src) if (s.startsWith('pain.')) assert.ok(PAIN_TERMS[s.slice(5)], `${k} 读的 ${s} 不在 pain.js`);
    assert.ok(d.thr > 0 && d.label, k);
    if (!d.consequence) assert.equal(d.cls, 'motion', k);
  }
});

test('动作成长: 信号值按帧率换成每秒, 缺的返回 null', () => {
  assert.equal(signalValue(FAULTS['arm.push'], sig(0, { pain: { pushed: 0.01 } })), 0.01 / DT);
  assert.equal(signalValue(FAULTS['hold.gap'], { dt: DT, f: {} }), null);
  assert.equal(familyOf({ held: [{ id: 'a' }] }), 'carry');
  assert.equal(familyOf({ action: { kind: 'sit' } }), 'sit');
  assert.equal(familyOf({ mode: 'go' }), 'walk');
  assert.equal(familyOf({ mode: 'idle' }), 'stand');
});

test('动作成长: 连续超过 hold 秒才记, 同一次动作只记一次, 冷却内不重复', () => {
  const d = new FaultDetector();
  const out = [];
  // 握着时掌心离杯子 10 cm (阈值 6 cm, 要连续 0.3 秒): 第 9 帧 (0.3 秒) 才记
  for (let i = 0; i < 30; i++) out.push(...d.push(sig(i * DT, { f: { holdGap: 0.1 }, ctx: carrying })));
  assert.equal(out.length, 1);
  assert.equal(out[0].kind, 'hold.gap');
  assert.equal(out[0].cls, 'motion');
  assert.ok(Math.abs(out[0].t - 8 * DT) < 1e-3, `第 9 帧记, 实际 ${out[0].t}`);
  assert.ok(out[0].ring.length > 0 && out[0].ring.length <= 45);
  // 通用的毛病 (手臂被推开) 按当时在做什么归类: 坐下时是 sit, 端着东西时是 carry
  const ds = new FaultDetector();
  const sitting = { mode: 'act', action: { kind: 'sit', clip: 'sit3', f: 20, t0: 0 }, held: [] };
  const fam = [];
  for (let i = 0; i < 12; i++) fam.push(...ds.push(sig(i * DT, { pain: { pushed: 0.02 }, ctx: sitting })).map((r) => r.family));
  for (let i = 300; i < 312; i++) fam.push(...ds.push(sig(i * DT, { pain: { pushed: 0.02 }, ctx: carrying })).map((r) => r.family));
  assert.deepEqual(fam, ['sit', 'carry']);
  // 不拿东西时同一个信号不算 (only: carry)
  const d2 = new FaultDetector();
  for (let i = 0; i < 30; i++) assert.equal(d2.push(sig(i * DT, { f: { holdGap: 0.1 } })).length, 0);
  // 一帧就算的 (跳变): 隔 1 秒两次, 冷却 8 秒内只记一次
  const d3 = new FaultDetector();
  const n = [0, 30].flatMap((i) => d3.push(sig(i * DT, { f: { handJump: 0.05 }, ctx: carrying }))).length;
  assert.equal(n, 1);
});

test('动作成长: 洒了 —— 前面手砸下去算动作缺陷, 平平稳稳端着洒了算环境', () => {
  // 1) 放下时手往下 2 m/s 甩了 0.2 秒, 接着洒了 10 ml
  const d = new FaultDetector(), out = [];
  const put = { mode: 'act', action: { kind: 'put', clip: 'put_low', f: 3, t0: 0 }, held: [{ id: 'cup', side: 'right', physical: true }] };
  for (let i = 0; i < 6; i++) out.push(...d.push(sig(i * DT, { f: { handDrop: 2 }, ctx: put })));
  out.push(...d.push(sig(6 * DT, { f: { spillMl: 10 }, ctx: put })));
  for (let i = 7; i < 30; i++) out.push(...d.push(sig(i * DT, { ctx: put })));
  const spill = out.find((r) => r.kind === 'spill');
  assert.ok(out.some((r) => r.kind === 'put.slam'), JSON.stringify(out.map((r) => r.kind)));
  assert.equal(spill.cls, 'motion');
  assert.equal(spill.cause, 'put.slam');
  // 2) 满杯慢慢端着走, 没有任何动作缺陷, 洒了 5 ml: 环境约束
  const e = new FaultDetector(), out2 = [];
  for (let i = 0; i < 30; i++) out2.push(...e.push(sig(i * DT, { f: { spillMl: i === 10 ? 5 : 0, handDrop: 0.2 }, ctx: carrying })));
  assert.deepEqual(out2.map((r) => [r.kind, r.cls, r.cause]), [['spill', 'env', null]]);
  // 3) 洒得少 (1 秒里不到 3 ml) 不记
  const g = new FaultDetector();
  assert.equal([0, 1, 2].flatMap((i) => g.push(sig(i, { f: { spillMl: 1 }, ctx: carrying }))).length, 0);
});

test('动作成长: 评估里事后给洒分类, 和小屋里一样', () => {
  const marks = [{ t: 2.0, kind: 'put.slam', r: 1.6 }];
  const ev = spillEvents([[2.1, 2], [2.3, 4], [9, 1], [15, 6]], marks);
  assert.deepEqual(ev.map((e) => [e.t, e.cls, e.cause]), [[2.3, 'motion', 'put.slam'], [15, 'env', null]]);
  assert.equal(causeAt(marks, 3.5), null, '超过 lookback 不算原因');
  // 滑、脱手 (carry.phys.events): 放下时手一顿接着滑了算动作缺陷, 隔得远的算环境
  const g = gripEvents([['put', 'slip', 2.2], ['put', 'release', 2.4], ['walk', 'slip', 20], ['walk', 'drop', 20.1]], marks);
  assert.deepEqual(g.map((e) => [e.kind, e.cls, e.cause]), [['slip', 'motion', 'put.slam'], ['slip', 'env', null], ['drop', 'env', null]]);
  // 小屋里每帧的滑: 和洒一样等 0.3 秒再分类
  const d = new FaultDetector(), out = [];
  for (let i = 0; i < 20; i++) out.push(...d.push(sig(i * DT, { f: { slipped: i === 5 ? 1 : 0 }, ctx: carrying })));
  assert.deepEqual(out.map((r) => [r.kind, r.cls]), [['slip', 'env']]);
});

test('动作成长: 平滑量越轻越小, 没过阈值一半就是 0; 曝光按动作次数', () => {
  const ex = new Excess(), d = new FaultDetector();
  for (let i = 0; i < 30; i++) ex.push(sig(i * DT, { f: { handDrop: 0.5 }, ctx: carrying }));
  assert.equal(ex.values['put.slam'], undefined, '0.5 m/s 没过阈值 1.2 的一半');
  for (let i = 0; i < 30; i++) ex.push(sig(i * DT, { f: { handDrop: 1.8 }, ctx: carrying }));
  assert.ok(Math.abs(ex.values['put.slam'] - (1.8 / 1.2 - 0.5) * 1) < 1e-3);
  for (const k of [0, 0, 1, 1, 2]) d.push(sig(k, { ctx: { mode: 'act', action: { kind: 'pick', clip: 'c', t0: k } } }));
  assert.equal(d.exposure.carry, 3);
});

test('动作成长: 传感器从行动层读掌心跳变、下落、握着的距离、脱手', () => {
  const pos = { left: [0, 1, 0], right: [0.3, 1, 0] };
  const item = { id: 'cup', held: 'right', physical: true, mesh: { position: { x: 0.3, y: 1, z: 0.1 } }, target: { p: [0.3, 1, 0] } };
  const loco = { mode: 'go', items: { cup: item }, action: null, seated: null,
    _palmHold: (_, side) => ({ x: pos[side][0], y: pos[side][1], z: pos[side][2] }) };
  const s = new Sensor(loco);
  let r;
  for (let i = 0; i < 3; i++) { pos.right[0] += 0.005; r = s.read(i * DT, DT); }
  pos.right[0] += 0.06;  // 一帧跳 6 cm
  r = s.read(3 * DT, DT);
  pos.right[0] += 0.005;
  r = s.read(4 * DT, DT);
  assert.ok(r.f.handJump > 0.04, `跳变 ${r.f.handJump}`);
  assert.ok(Math.abs(r.f.holdGap - 0.1) < 1e-9, '杯子在掌心目标后面 10 cm');
  pos.right[1] -= 0.06;
  r = s.read(5 * DT, DT);
  assert.ok(Math.abs(r.f.handDrop - 0.06 / DT) < 1e-6);
  Object.assign(item, { held: null, dropped: true });
  r = s.read(6 * DT, DT);
  assert.equal(r.f.dropped, 1);
  assert.equal(r.ctx.held.length, 0);
});
