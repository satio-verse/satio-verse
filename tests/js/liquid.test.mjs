import { test } from 'node:test';
import assert from 'node:assert/strict';
import { capacityMl, heatEffect, LIQUID, LiquidWorld, rotate, surfaceAt } from '../../web/physics/liquid.js';

// 合成的手部轨迹 (杯子在腰前 0.95 m 高, 竖着): 走路有上下颠、左右晃; 急转是走着拐 90 度 (半径 0.35 m); 急停是 0.25 秒停下
const DT = 1 / 60;
const UP = [0, 0, 0, 1];
function walk({ speed = 0.6, secs = 4, bob = 0.015, sway = 0.01, turnAt = null, stopAt = null, R = 0.35 } = {}) {
  const out = [];
  let x = 0, z = 0, yaw = 0, v = 0, turned = 0, phase = 0;
  for (let i = 0; i < secs / DT; i++) {
    const t = i * DT;
    v = Math.min(speed, v + (speed / 0.6) * DT);  // 0.6 秒起步
    if (stopAt !== null && t >= stopAt) v = Math.max(0, v - (speed / 0.25) * DT - (speed / 0.6) * DT);
    if (turnAt !== null && t >= turnAt && turned < Math.PI / 2) { const d = (v / R) * DT; yaw += d; turned += d; }
    x += Math.sin(yaw) * v * DT;
    z += Math.cos(yaw) * v * DT;
    phase += 2 * Math.PI * 1.8 * Math.sqrt(v / 0.6) * DT;  // 步频随速度, 相位按积分走 (不然一减速相位跳)
    const s = Math.sin(phase), c = Math.sin(phase / 2);
    const k = v / Math.max(speed, 1e-6);
    out.push({ p: [x + Math.cos(yaw) * sway * c * k, 0.95 + bob * s * k, z - Math.sin(yaw) * sway * c * k], q: UP, hold: 'right' });
  }
  return out;
}
const R = 0.04, H = 0.09, CAP = Math.PI * R * R * H * 1e6;  // 马克杯 (内半径 4 cm, 内高 9 cm) 约 452 ml
const FULL = CAP - Math.PI * R * R * 0.008 * 1e6;            // 满杯: 离杯沿 8 mm
const HALF = CAP / 2;

/** 端着一杯走完轨迹, 再放着等 3 秒 (落下的都落地). 返回洒了多少、事件、世界. */
function run(path, { ml = FULL, temp = 80, kind = 'coffee', hit = null } = {}) {
  const w = new LiquidWorld();
  w.cup('mug', { R, H });
  w.fill('mug', ml, temp, kind);
  const ev = [];
  for (const pose of path) ev.push(...w.step(DT, { poses: { mug: pose }, hit }));
  for (let i = 0; i < 180; i++) ev.push(...w.step(DT, {}));
  return { spilled: w.ledger.spilled, ev, w };
}

test('记账: 倒进去的 = 杯里 + 在落的 + 每一摊 + 身上 + 喝掉 + 干掉, 任何时候都成立', () => {
  const w = new LiquidWorld();
  w.cup('mug', { R, H });
  w.fill('mug', FULL, 85);
  const hand = (p0, p1) => (p1[1] < 0.9 && p0[1] >= 0.9 ? { part: 'rightHand' } : null);
  for (const pose of walk({ speed: 1.3, stopAt: 1.5 })) {
    w.step(DT, { poses: { mug: pose }, hit: hand });
    assert.ok(Math.abs(w.balance().error) < 1e-6, JSON.stringify(w.balance()));
  }
  w.advance(3600 * 5);
  const b = w.balance();
  assert.ok(Math.abs(b.error) < 1e-6, JSON.stringify(b));
  assert.ok(b.dried > 0, '几个小时以后洒出来的都干了');
  assert.equal(w.drops.length, 0);
});

test('空杯不洒, 倒不进超过杯子能装的', () => {
  const r = run(walk({ speed: 1.4, turnAt: 1, stopAt: 2.5 }), { ml: 0 });
  assert.equal(r.spilled, 0);
  assert.equal(r.ev.length, 0);
  const w = new LiquidWorld();
  w.cup('mug', { R, H });
  assert.ok(Math.abs(w.fill('mug', 1000, 80) - CAP) < 1e-6);
  assert.equal(w.fill('mug', 10, 80), 0);
});

test('同样的走法: 半杯比满杯稳; 急转、急停比慢走洒得多', () => {
  const slow = walk({ speed: 0.6 }), turn = walk({ speed: 1.3, turnAt: 1 }), stop = walk({ speed: 1.3, stopAt: 1.5 });
  const full = { slow: run(slow).spilled, turn: run(turn).spilled, stop: run(stop).spilled };
  const half = { slow: run(slow, { ml: HALF }).spilled, turn: run(turn, { ml: HALF }).spilled, stop: run(stop, { ml: HALF }).spilled };
  assert.equal(half.slow, 0);
  assert.ok(half.turn <= full.turn && half.stop <= full.stop, JSON.stringify({ half, full }));
  assert.ok(full.turn > full.slow && full.stop > full.slow, JSON.stringify(full));
  assert.ok(full.stop > 0.5, `满杯急停应该洒出来 (${full.stop.toFixed(2)} ml)`);
  assert.ok(full.slow < 0.5, `满杯慢走基本不洒 (${full.slow.toFixed(2)} ml)`);
});

test('冷水和热咖啡洒的一样多, 只有热的会烫', () => {
  const path = walk({ speed: 1.3, stopAt: 1.5 });
  const hand = (p0, p1) => (p1[1] < 0.8 && p0[1] >= 0.8 ? { part: 'rightThigh' } : null);
  const hot = run(path, { temp: 80, kind: 'coffee', hit: hand }), cold = run(path, { temp: 20, kind: 'water', hit: hand });
  assert.ok(hot.spilled > 0);
  assert.ok(Math.abs(hot.spilled - cold.spilled) < 1e-9);
  const burns = (r) => r.ev.filter((e) => e.type === 'contact' && e.effect.level !== 'wet').length;
  assert.ok(burns(hot) > 0, '热咖啡溢出来顺着杯身流到手上, 烫');
  assert.equal(burns(cold), 0, '冷水只是湿');
  assert.ok(cold.ev.some((e) => e.type === 'contact' && e.effect.level === 'wet'));
});

test('倾斜: 静态液面、装得下多少; 倒过来就倒光', () => {
  assert.ok(Math.abs(surfaceAt(R, H, HALF / 1e6, 0) - H / 2) < 1e-9);
  assert.ok(Math.abs(capacityMl(R, H, 0) - CAP) < 1e-6);
  assert.ok(Math.abs(capacityMl(R, H, Math.tan(Math.PI / 6)) - Math.PI * R * R * (H - R * Math.tan(Math.PI / 6)) * 1e6) < 0.5);
  assert.ok(capacityMl(R, H, 5) > 0 && capacityMl(R, H, 5) < capacityMl(R, H, 2));  // 倾得很厉害时液面碰到杯底, 还剩一点
  const tilt = (deg) => { const a = (deg * Math.PI) / 360; return [0, 0, Math.sin(a), Math.cos(a)]; };  // 绕 z 轴倾
  const w = new LiquidWorld();
  w.cup('mug', { R, H });
  w.fill('mug', FULL, 60);
  for (let i = 0; i < 240; i++) w.step(DT, { poses: { mug: { p: [0, 0.9, 0], q: tilt(30) } } });
  const left = w.cups.mug.ml, should = capacityMl(R, H, Math.tan(Math.PI / 6));
  assert.ok(Math.abs(left - should) < 0.08 * should, `倾 30 度剩 ${left.toFixed(1)}, 应该约 ${should.toFixed(1)}`);
  for (let i = 0; i < 60; i++) w.step(DT, { poses: { mug: { p: [0, 0.9, 0], q: tilt(180) } } });
  assert.ok(w.cups.mug.ml < 0.01, '倒过来倒光');
});

test('失重 (刚体的杯子): 正着的杯子往下拽得比重力还快, 冒出杯沿的那截甩出去, 不是一下倒光; 自由落体不洒', () => {
  // 刚体给的是位置、速度 (physical: true), 加速度按速度差算. 满杯 (离杯沿 8 mm) 先静止, 再以 15 m/s² 往下拽 0.1 秒, 然后匀速
  const yank = (a, secs = 0.1) => {
    const w = new LiquidWorld();
    w.cup('mug', { R, H });
    w.fill('mug', FULL, 80);
    let y = 0.9, v = 0;
    for (let i = 0; i < 120; i++) {
      if (i >= 30 && i < 30 + secs / DT) v -= a * DT;
      y += v * DT;
      w.step(DT, { poses: { mug: { p: [0, y, 0], q: UP, v: [0, v, 0], physical: true } } });
    }
    return w.ledger.spilled;
  };
  const hard = yank(15), soft = yank(5), fall = yank(9.81, 0.3);
  assert.ok(hard > 2 && hard < FULL * 0.6, `往下拽 15 m/s² 0.1 秒洒 ${hard.toFixed(1)} ml (以前当成倒过来, 一下倒光)`);
  assert.ok(soft < 0.5, `5 m/s² 不失重, 不洒 (${soft.toFixed(2)})`);
  assert.ok(fall < 0.5, `自由落体 0.3 秒: 液体跟着杯子一起落, 不洒 (${fall.toFixed(2)})`);
});

test('喝: 杯沿凑在嘴边慢慢倾过来, 流出来的咽下去, 不洒; 一下子倒太猛咽不下, 照样洒', () => {
  const tiltTo = (deg) => { const a = (deg * Math.PI) / 360; return [0, 0, Math.sin(a), Math.cos(a)]; };  // 绕 z 轴: 杯口 (+Y) 转向 -X
  const sip = (deg, ramp) => {
    const w = new LiquidWorld();
    w.cup('cup', { R: 0.03, H: 0.06 });
    w.fill('cup', 60, 65);
    const lip = rotate(tiltTo(deg), [-0.03, 0.06, 0]), mouth = [lip[0], 1.2 + lip[1] + 0.01, 0];  // 嘴贴在液面高的那侧杯沿
    let drunk = 0;
    for (let i = 0; i < 120; i++) {
      const q = tiltTo(deg * Math.min(1, (i + 1) / ramp));
      for (const e of w.step(DT, { poses: { cup: { p: [0, 1.2, 0], q, hold: 'right' } }, mouth })) if (e.type === 'drink') drunk += e.ml;
    }
    for (let i = 0; i < 120; i++) w.step(DT, {});
    assert.ok(Math.abs(w.balance().error) < 1e-6);
    return { drunk, spilled: w.ledger.spilled };
  };
  const slow = sip(58, 60);  // 一秒倾到 58 度: 液面刚过杯沿
  assert.ok(slow.drunk > 3, `喝到 ${slow.drunk.toFixed(1)} ml`);
  assert.ok(slow.spilled < 0.5, `洒了 ${slow.spilled.toFixed(2)} ml`);
  const gulp = sip(100, 3);  // 一下子倒过来: 每秒只咽得下 25 ml
  assert.ok(gulp.spilled > 5, `猛倒也该洒 (${gulp.spilled.toFixed(1)} ml)`);
});

test('喝只看几何, 不看她想不想喝: 杯子离嘴远、手被挡住送不到、脱手、动作中断了 drinking 没清, 都不算喝掉', () => {
  // 浓缩杯 60 ml, 倾到 70 度 (过了杯沿, 会流出来). 嘴的位置固定在 [0, 1.5, 0]; 每种情况杯子在哪、谁握着、宿主说没说在喝
  const tiltTo = (deg) => { const a = (deg * Math.PI) / 360; return [0, 0, Math.sin(a), Math.cos(a)]; };  // 杯口 (+Y) 转向 -X
  const MOUTH = [0, 1.5, 0], R0 = 0.03, H0 = 0.06;
  const pour = ({ at, hold = 'right', drinking = false, fall = false }) => {
    const w = new LiquidWorld();
    w.cup('cup', { R: R0, H: H0 });
    w.fill('cup', 60, 65);
    let drunk = 0, y = at[1], v = 0;
    for (let i = 0; i < 90; i++) {
      if (fall) { v -= 9.81 * DT; y += v * DT; }
      const pose = { p: [at[0], y, at[2]], q: tiltTo(70 * Math.min(1, (i + 1) / 30)), hold, drinking, ...(fall ? { physical: true, v: [0, v, 0] } : {}) };
      for (const e of w.step(DT, { poses: { cup: pose }, mouth: MOUTH })) if (e.type === 'drink') drunk += e.ml;
    }
    for (let i = 0; i < 120; i++) w.step(DT, {});
    assert.ok(Math.abs(w.balance().error) < 1e-6);
    return { drunk, out: w.ledger.spilled + drunk };
  };
  // 杯底放在让倾过来的杯沿正好贴着嘴的地方 (杯沿在杯口中心 -X 那侧 R0 处)
  const lip = rotate(tiltTo(70), [-R0, H0, 0]), atMouth = [MOUTH[0] - lip[0] + 0.005, MOUTH[1] - lip[1] - 0.01, 0];
  const ok = pour({ at: atMouth });
  assert.ok(ok.drunk > 10 && ok.drunk > 0.8 * ok.out, `杯沿贴着嘴: 喝到 ${ok.drunk.toFixed(1)} / 流出 ${ok.out.toFixed(1)} ml`);
  const cases = {
    杯子离嘴远: pour({ at: [0.3, 1.0, 0.2], drinking: true }),
    手被挡住送不到嘴边: pour({ at: [atMouth[0] + 0.12, atMouth[1] - 0.04, 0], drinking: true }),
    杯子脱手: pour({ at: atMouth, hold: null, drinking: true }),
    杯子在嘴边往下掉: pour({ at: atMouth, hold: null, drinking: true, fall: true }),
    动作中断了还标着在喝: pour({ at: [0.05, 1.0, 0.25], drinking: true }),
    杯沿背着嘴: pour({ at: [MOUTH[0] + lip[0] - 0.005, atMouth[1], 0], drinking: true }),
  };
  for (const [k, r] of Object.entries(cases)) {
    if (k !== '杯子在嘴边往下掉') assert.ok(r.out > 5, `${k}: 该流出来 (${r.out.toFixed(1)} ml)`);  // 自由落体时液体失重, 本来就不往外流
    assert.equal(r.drunk, 0, `${k}: 不该算喝掉 (${r.drunk.toFixed(2)} ml)`);
  }
});

test('存档读回来接着算: 离线冷却按解析解, 咖啡干了留下印子, 水不留', () => {
  const w = new LiquidWorld();
  w.cup('cup', { R: 0.03, H: 0.06 });
  w.fill('cup', 60, 70);
  const back = LiquidWorld.from(JSON.parse(JSON.stringify(w.toJSON())));
  back.advance(600);
  const T = back.cups.cup.temp;
  assert.ok(T > 45 && T < 55, `70 ℃ 的浓缩咖啡放十分钟 ${T.toFixed(1)} ℃`);
  back.puddles.push({ id: 'p9', surface: 'desk', at: [0, 0.76, 0], ml: 3, temp: 60, kind: 'coffee', r: 0.035, stain: false, born: 0 },
    { id: 'p10', surface: 'floor', at: [1, 0, 0], ml: 3, temp: 20, kind: 'water', r: 0.035, stain: false, born: 0 });
  back.ledger.poured += 6;
  back.advance(3600 * 24);
  assert.deepEqual(back.puddles.map((p) => [p.id, p.ml, p.stain]), [['p9', 0, true]]);
  assert.ok(Math.abs(back.balance().error) < 1e-6);
});

test('干了的印子只按写明的规则变: 挨着的同种印子并成一块 (面积、干掉的量守恒), 不按条数删; 擦掉才没', () => {
  const w = new LiquidWorld();
  // 80 滩散开的牛奶 (相隔 0.5 m, 各成一块); 再在一处洒一排 6 滩咖啡: 相隔 6 cm, 湿的时候各是一滩, 干了以后印子边挨边
  for (let i = 0; i < 80; i++) w.pour([-3 - (i % 10) * 0.5, 0.3, Math.floor(i / 10) * 0.5], 1, 40, 'milk');
  for (let i = 0; i < 60; i++) w.step(1 / 60, {});
  for (let i = 0; i < 6; i++) w.pour([0.06 * i, 0.3, 0], 2, 60, 'coffee');
  for (let i = 0; i < 60; i++) w.step(1 / 60, {});
  const wet = w.puddles.filter((p) => p.kind === 'coffee').length;
  const area0 = w.puddles.filter((p) => p.kind === 'coffee').reduce((s, p) => s + p.r * p.r, 0);
  const ev = [];
  for (let k = 0; k < 400; k++) ev.push(...w.advance(60));
  const stains = w.puddles.filter((p) => p.stain), coffee = stains.filter((p) => p.kind === 'coffee');
  assert.equal(wet, 6, '湿的时候是 6 滩');
  assert.ok(coffee.length >= 1 && coffee.length < wet, `挨着的 6 滩咖啡干了并成 ${coffee.length} 块`);
  assert.ok(Math.abs(coffee.reduce((s, p) => s + p.r * p.r, 0) - area0) < 1e-9, '并了面积守恒');
  assert.ok(Math.abs(coffee.reduce((s, p) => s + p.dried, 0) - 12) < 1e-6, '干掉的量守恒 (6 x 2 ml)');
  assert.equal(stains.filter((p) => p.kind === 'milk').length, 80, '散开的牛奶印子一块不少 (不按条数删)');
  assert.equal(ev.filter((e) => e.type === 'dried').length, 86, '每一摊干了都有事件');
  assert.equal(ev.filter((e) => e.type === 'merged').length, wet - coffee.length, '每次合并都有事件');
  // 擦掉才没
  const id = coffee[0].id;
  w.wipe(id);
  assert.ok(!w.puddles.some((p) => p.id === id));
  assert.ok(Math.abs(w.balance().error) < 1e-6);
});

test('碰到液体的后果分档: 湿、烫一下、烫伤 (越烫越多越重)', () => {
  assert.equal(heatEffect(30, 5).level, 'wet');
  assert.equal(heatEffect(45, 5).level, 'hot');
  assert.equal(heatEffect(80, 0.1).level, 'hot', '几滴热咖啡烫一下, 不算伤');
  const a = heatEffect(55, 3), b = heatEffect(80, 3), c = heatEffect(80, 10);
  assert.equal(a.level, 'burn');
  assert.ok(a.severity < b.severity && b.severity < c.severity);
  assert.ok(c.sore > a.sore);
});
