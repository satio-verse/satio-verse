import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { MotionCoverage } from '../../web/motion-growth-coverage.js';
import { install } from '../../web/motion-growth-probe.js';

const node = () => ({ position: { x: 0, y: 1, z: 0 }, quaternion: { x: 0, y: 0, z: 0, w: 1 } });
const loco = (name = 'cmu69_68_pick') => ({
  mode: 'act', char: { x: 0, z: 0, yaw: 0 }, nodes: { hips: node(), rightHand: node() },
  items: { ball: { held: 'right', mesh: node() } },
  action: { c: { name, start: 100, end: 200 }, grab: { kind: 'pick' }, frame: 101 },
});
const record = (L, mode) => { const c = new MotionCoverage(mode); c.observe(L); c.push(L, 1 / 60); return c.result(); };

test('多个种子落到同一实际轨迹, 摘要相同; 片段身份包含源帧范围', () => {
  const a = loco(), b = loco(); a.seed = 3; b.seed = 5;
  assert.equal(record(a).trajectory.id, record(b).trajectory.id);
  assert.deepEqual(record(a).clips[0], { id: '["actions","pick","cmu69_68_pick",100,200]',
    library: 'actions', kind: 'pick', name: 'cmu69_68_pick', start: 100, end: 200 });
  b.action.c.end++;
  assert.notEqual(record(a).clips[0].id, record(b).clips[0].id);
});

test('实际最终骨骼/物件/根部改变会改变轨迹摘要, 不要求候选与基线摘要相等', () => {
  const original = record(loco()).trajectory.id;
  for (const change of [L => { L.nodes.rightHand.quaternion.z = 0.1; }, L => { L.items.ball.mesh.position.x = 0.1; },
    L => { L.char.yaw = 0.1; }]) {
    const L = loco(); change(L); assert.notEqual(record(L).trajectory.id, original);
  }
});

test('无故障的两个实际片段也完整记录, 连续帧不虚增片段数', () => {
  const L = loco(), c = new MotionCoverage();
  for (let i = 0; i < 3; i++) c.push(L, 1 / 60);
  L.action.c = { name: 'cmu69_69_pick', start: 300, end: 400 };
  c.push(L, 1 / 60);
  const r = c.result();
  assert.equal(r.frames, 4); assert.equal(r.clips.length, 2); assert.equal(r.sequence.length, 2);
});

test('step 内结束的片段仍保留, 诊断标记不会改成正常选择', () => {
  const L = loco(), c = new MotionCoverage('diagnostic');
  c.observe(L); L.action = null; c.push(L, 1 / 60);
  assert.equal(c.result().clips[0].name, 'cmu69_68_pick');
  assert.equal(c.result().mode, 'diagnostic');
});

test('idle、mm 与程序化伸手的来源分开, 不伪装成动作库 pick', () => {
  const L = loco(); L.action = null;
  L.reaching = { kind: 'pick' };
  assert.equal(record(L).clips[0].library, 'procedural');
  L.reaching = null; L.idleDB = { clips: [{ name: 'idle', start: 0, end: 10 }] };
  L.idle = { db: L.idleDB, clip: 0, frame: 1, kind: 'idle' };
  assert.equal(record(L).clips[0].library, 'idle');
  L.idle = null; L.db = { clips: [{ name: 'walk', start: 0, end: 10 }], clipOf: [0] }; L.frame = 0;
  assert.equal(record(L).clips[0].library, 'mm:neutral');
});

test('真实 probe 将覆盖挂到每个结果, 不依赖前 20 条故障记录; 运行之间不串 mode', async () => {
  const L = loco(), kin = { begin() {}, end() {}, painNow: {} };
  const state = { vrm: { loco: L, kin }, step() {} };
  const me = install(state, { async runEval() {
    kin.begin('pickput:ball>coffee_table#3');
    state.step(1 / 60, 1 / 60); kin.end();
    return { results: [{ scenario: 'pickput:ball>coffee_table', seed: 3, frames: 1 }] };
  } });
  const diagnostic = (await me.runEval({ coverageMode: 'diagnostic' })).results[0].growth;
  assert.equal(diagnostic.records.length, 0);
  assert.equal(diagnostic.coverage.mode, 'diagnostic');
  assert.equal(diagnostic.coverage.clips[0].name, 'cmu69_68_pick');
  assert.equal((await me.runEval({})).results[0].growth.coverage.mode, 'normal');
});

test('CLI 未知 coverage-mode 在尝试打开浏览器前拒绝', () => {
  const cli = fileURLToPath(new URL('../../tools/motion_eval.mjs', import.meta.url));
  const r = spawnSync(process.execPath, [cli, '--coverage-mode', 'unknown', '--browser', 'must-not-launch'], { encoding: 'utf8' });
  assert.equal(r.status, 1);
  assert.match(r.stderr, /coverage-mode.*normal.*diagnostic/);
});
