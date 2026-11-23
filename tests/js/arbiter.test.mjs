import { test } from 'node:test';
import assert from 'node:assert/strict';
import { Arbiter, LAYER_INDEX, LAYERS, PARTS, shares, summarize, topLayers } from '../../web/arbiter.js';

// 假的骨骼: 只要 quaternion / position 和 set (Arbiter 不依赖 three.js)
class Q { constructor() { this.x = 0; this.y = 0; this.z = 0; this.w = 1; } set(x, y, z, w) { Object.assign(this, { x, y, z, w }); return this; } }
class P { constructor() { this.x = 0; this.y = 0; this.z = 0; } set(x, y, z) { Object.assign(this, { x, y, z }); return this; } }
const node = () => ({ quaternion: new Q(), position: new P() });
const rotX = (n, deg) => { const h = (deg * Math.PI) / 360; n.quaternion.set(Math.sin(h), 0, 0, Math.cos(h)); };

function fakeHumanoid() {
  const nodes = {};
  for (const part of PARTS) for (const b of part.bones) nodes[b] = node();
  return { nodes, getNormalizedBoneNode: (b) => nodes[b] };
}

const NP = PARTS.length;
const at = (w, layer, part) => w[LAYER_INDEX[layer] * NP + PARTS.findIndex((p) => p.id === part)];

test('每层写完记一笔: 动作来源按离零位多远, 之后的层按它改了多少, 权重按部位归一', () => {
  const h = fakeHumanoid(), arb = new Arbiter(h, { position: new P(), quaternion: new Q() }, { seconds: 1, fps: 10 });
  arb.begin();
  rotX(h.nodes.leftUpperArm, 30);
  rotX(h.nodes.leftLowerArm, 30);
  arb.mark('base', { absolute: true, all: true });
  rotX(h.nodes.leftLowerArm, 40);   // 防穿模层把前臂再转 10 度
  arb.mark('contact');
  rotX(h.nodes.leftIndexProximal, 20);
  arb.mark('hands', { absolute: true, fingers: true });
  arb.end(1, 'idle:idle_00', 1);
  const w = arb.last.w;
  assert.ok(Math.abs(at(w, 'base', 'leftArm') - 60 / 70) < 1e-3, `动作来源 60 度, 防穿模 10 度: ${at(w, 'base', 'leftArm')}`);
  assert.ok(Math.abs(at(w, 'contact', 'leftArm') - 10 / 70) < 1e-3);
  assert.equal(at(w, 'hands', 'leftFingers'), 1);
  assert.equal(at(w, 'base', 'leftFingers'), 0, '动作来源不算手指');
  assert.equal(at(w, 'contact', 'rightArm'), 0);
  assert.deepEqual(topLayers(w, 'leftArm', 2).map(([l]) => l), ['base', 'contact']);
});

test('行动层盖在 HAST-SEC 上时动作来源的影响按权重分给两边; 骨盆位移按厘米并进躯干', () => {
  const h = fakeHumanoid(), arb = new Arbiter(h, null, { seconds: 1, fps: 10 });
  arb.begin();
  rotX(h.nodes.spine, 20);
  arb.mark('base', { absolute: true, all: true, split: { hast: 0.25 } });
  h.nodes.hips.position.set(0.05, 0, 0);  // 配平挪了 5 cm
  arb.mark('balance');
  arb.end(1, 'mm:neutral', 0.75);
  const w = arb.last.w;
  assert.ok(Math.abs(at(w, 'hast', 'torso') - 5 / 25) < 1e-3, `HAST-SEC ${at(w, 'hast', 'torso')}`);
  assert.ok(Math.abs(at(w, 'base', 'torso') - 15 / 25) < 1e-3);
  assert.ok(Math.abs(at(w, 'balance', 'torso') - 5 / 25) < 1e-3);
});

test('环形缓存: 只留最近的帧, 按时间取、汇总、时间线; 回放把姿态放回去; 导出再载入一样', () => {
  const h = fakeHumanoid(), root = { position: new P(), quaternion: new Q() };
  const arb = new Arbiter(h, root, { seconds: 1, fps: 5 });  // 5 帧
  for (let i = 0; i < 8; i++) {
    arb.begin();
    rotX(h.nodes.head, 10 + i);
    arb.mark('base', { absolute: true, all: true });
    if (i % 2) { rotX(h.nodes.head, 10 + i + 5); arb.mark('look'); }
    root.position.set(i, 0, 0);
    arb.end(i * 0.1, `src${i}`, 1);
  }
  assert.equal(arb.count, 5);
  assert.equal(arb.frame(0).src, 'src3');
  assert.equal(arb.frame(4).src, 'src7');
  assert.equal(arb.framesBetween(0.45, 0.75).length, 3);
  const rep = arb.report(0.3, 0.75, 0.2);
  assert.equal(rep.frames, 5);
  assert.ok(rep.summary.head.base > 0.6 && rep.summary.head.look > 0.1, JSON.stringify(rep.summary));
  assert.deepEqual(rep.timeline.map((r) => r.src), ['src3', 'src5', 'src7']);
  rotX(h.nodes.head, 90);
  root.position.set(99, 0, 0);
  arb.restore(0);
  assert.ok(Math.abs(h.nodes.head.quaternion.x - Math.sin((18 * Math.PI) / 360)) < 1e-5);  // 第 3 帧: 来源 13 度, 看向再转到 18 度
  assert.equal(root.position.x, 3);
  const copy = new Arbiter(h, root, { seconds: 1, fps: 5 });
  copy.load(JSON.parse(JSON.stringify(arb.dump())));
  assert.equal(copy.count, 5);
  assert.deepEqual(Array.from(copy.frame(2).w, (x) => +x.toFixed(3)), Array.from(arb.frame(2).w, (x) => +x.toFixed(3)));
  assert.throws(() => copy.load({ version: 1, poseSize: 3 }), /对不上/);
  arb.clear();
  assert.equal(arb.count, 0);
});

test('汇总只在有层动了这个部位的帧里平均; 每个部位的权重加起来是 1', () => {
  const infl = new Float32Array(LAYERS.length * NP);
  infl[LAYER_INDEX.base * NP] = 30;
  infl[LAYER_INDEX.look * NP] = 10;
  const still = new Float32Array(LAYERS.length * NP);  // 这一帧头没动
  const sum = summarize([infl, still]);
  assert.deepEqual(sum.head, { base: 0.75, look: 0.25 });
  const w = shares(infl);
  let s = 0;
  for (let l = 0; l < LAYERS.length; l++) s += w[l * NP];
  assert.ok(Math.abs(s - 1) < 1e-6);
  assert.equal(shares(still)[0], 0);
});
