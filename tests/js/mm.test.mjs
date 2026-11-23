import { test } from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';
import { buildQuery, frameCost, Inertializer, loadActions, loadDB, predict, qexp, qfromTo, qinv, qlog, qmul, qrot, qyaw,
  samplePose, search, springVelocity, springYaw } from '../../web/mm.js';

const close = (a, b, eps = 1e-5) => a.every((v, i) => Math.abs(v - b[i]) < eps);

test('四元数: 旋转向量往返, 最小旋转, 绕 Y 转', () => {
  const v = [0.3, -0.2, 0.9];
  assert.ok(close(qlog(qexp(v)), v));
  const q = qfromTo([0, 1, 0], [1, 0, 0]);
  assert.ok(close(qrot(q, [0, 1, 0]), [1, 0, 0]));
  assert.ok(close(qrot(qyaw(Math.PI / 2), [0, 0, 1]), [1, 0, 0]), 'yaw=90° 时 +Z 应转到 +X');
  const r = qmul(q, qinv(q));
  assert.ok(close(r, [0, 0, 0, 1]));
});

test('弹簧: 速度收敛到期望值, 朝向走最短弧', () => {
  let s = { x: 0, z: 0, vx: 0, vz: 0, ax: 0, az: 0 };
  for (let i = 0; i < 120; i++) s = springVelocity(s, 1, 0, 0.27, 1 / 60);
  assert.ok(Math.abs(s.vx - 1) < 0.01 && Math.abs(s.vz) < 1e-6);
  assert.ok(s.x > 1.4 && s.x < 2, `两秒走了 ${s.x}`);
  let r = { yaw: 3.0, w: 0 };
  for (let i = 0; i < 120; i++) r = springYaw(r, -3.0, 0.2, 1 / 60);
  assert.ok(Math.abs(Math.atan2(Math.sin(r.yaw + 3.0), Math.cos(r.yaw + 3.0))) < 0.01);
  const fut = predict({ x: 0, z: 0, vx: 0, vz: 0, ax: 0, az: 0 }, { yaw: 0, w: 0 }, 0, 1, 0, [1 / 3, 2 / 3, 1], 0.27, 0.2);
  assert.ok(fut[0].z < fut[1].z && fut[1].z < fut[2].z);
});

test('惯性化: 切换瞬间姿态不跳, 之后衰减到新动画', () => {
  const inert = new Inertializer(1, 0.1);
  const prev = [qexp([0, 0.8, 0])], next = [qexp([0, -0.4, 0])];
  inert.transition(prev, next, [0, 1, 0], [0, 0.9, 0]);
  const shown = [next[0].slice()], hips = [0, 0.9, 0];
  inert.apply(shown, hips);
  assert.ok(close(shown[0], prev[0], 1e-4) && Math.abs(hips[1] - 1) < 1e-6);
  for (let i = 0; i < 60; i++) inert.update(1 / 60);
  const later = [next[0].slice()];
  inert.apply(later, [0, 0, 0]);
  assert.ok(close(later[0], next[0], 1e-3));
});

const DB = new URL('../../models/motion/neutral.json', import.meta.url);
test('真实数据库: 自身特征能搜回来, 站立和前进的查询各找到对应的片段', { skip: !existsSync(DB) && '没有生成运动数据库' }, () => {
  const meta = JSON.parse(readFileSync(DB, 'utf8'));
  const bin = readFileSync(new URL('../../models/motion/neutral.bin', import.meta.url));
  const db = loadDB(meta, bin.buffer.slice(bin.byteOffset, bin.byteOffset + bin.byteLength));
  const fw = meta.clips.find((c) => c.name === 'Neutral_FW');
  const f = fw.start + 200;
  const own = search(db, db.feats.subarray(f * 27, f * 27 + 27));
  assert.ok(own.cost < 1e-6 && frameCost(db, db.feats.subarray(f * 27, f * 27 + 27), own.frame) < 1e-6);

  const idle = meta.clips.find((c) => c.name === 'Neutral_ID');
  const hip = 1;
  const root = { x: 0, z: 0, yaw: 0 };
  const still = [1, 2, 3].map(() => ({ x: 0, z: 0, yaw: 0 }));
  const qs = buildQuery(db, idle.start + 50, root, still, hip);
  const hitStill = search(db, qs);
  const traj = (k) => db.feats[hitStill.frame * 27 + k] * db.scale[k] + db.mean[k];
  assert.ok(Math.abs(traj(4)) < 0.1 && Math.abs(traj(5)) < 0.15, '站立查询应该找到几乎不动的帧');

  const v = meta.speeds.FW;
  const walk = [1, 2, 3].map((k) => ({ x: 0, z: (v * k) / 3, yaw: 0 }));
  const qw = buildQuery(db, f, root, walk, hip);
  const hitWalk = search(db, qw);
  const z1 = db.feats[hitWalk.frame * 27 + 5] * db.scale[5] + db.mean[5];
  assert.ok(z1 > v * 0.6, `前进查询找到的帧 1 秒后只走了 ${z1}`);

  const pose = [];
  const s = samplePose(db, f + 0.5, pose);
  assert.equal(pose.length, meta.bones.length);
  assert.ok(s.hips[1] > 0.8 && s.hips[1] < 1.2, `髋高 ${s.hips[1]}`);
});

const ACTIONS = new URL('../../models/motion/actions.json', import.meta.url);
test('动作库: 每组的片段编号和关键帧都在范围内, 坐姿髋高接近沙发', { skip: !existsSync(ACTIONS) && '没有生成动作库' }, () => {
  const meta = JSON.parse(readFileSync(ACTIONS, 'utf8'));
  const bin = readFileSync(new URL('../../models/motion/actions.bin', import.meta.url));
  const db = loadActions(meta, bin.buffer.slice(bin.byteOffset, bin.byteOffset + bin.byteLength));
  const len = (c) => db.clips[c].end - db.clips[c].start;
  assert.ok(db.groups.sit.length >= 8 && db.groups.pick.length >= 8 && db.groups.put.length >= 8 && db.groups.reach.length >= 8);
  for (const g of db.groups.sit) {
    for (const k of ['down', 'idle', 'up']) if (g[k] !== null) assert.equal(db.clips[g[k]].kind, `sit_${k}`);
    assert.ok(g.seatHips > 0.45 && g.seatHips < 0.7, `${g.source} 坐姿髋高 ${g.seatHips}`);
    assert.ok(len(g.idle) >= 45, '坐着的段至少 1.5 秒');
  }
  for (const kind of ['pick', 'put', 'reach']) {
    for (const g of db.groups[kind]) {
      assert.equal(db.clips[g.clip].kind, kind);
      assert.ok(g.eventOffset > 0 && g.eventOffset < len(g.clip), `${g.source} 关键帧越界`);
      assert.ok(['left', 'right'].includes(g.side));
    }
  }
  // 捡起的关键帧: 手在离地 0.2 髋高以内
  for (const g of db.groups.pick) assert.ok(g.hand[1] < 0.2, `${g.source} 抓取时手高 ${g.hand[1]}`);
  const pose = [];
  const s = samplePose(db, db.clips[db.groups.sit[0].idle].start + 5, pose);
  assert.ok(s.hips[1] < 0.7, '坐着时髋部低于站立');
});
