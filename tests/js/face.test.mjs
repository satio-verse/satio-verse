import { test } from 'node:test';
import assert from 'node:assert/strict';
import { AudioOut } from '../../web/audio.js';
import { arkitToVisemes } from '../../web/lipsync.js';
import { SpeechPerformer } from '../../web/performance.js';

// 最小的 AudioContext 替身: 只要时间线和缓冲区时长.
function fakeCtx() {
  return {
    currentTime: 0, outputLatency: 0, destination: {},
    createGain: () => ({ connect() {} }),
    createBuffer: (ch, len, sr) => ({ duration: len / sr, copyToChannel() {} }),
    createBufferSource: () => ({ connect() {}, start() {}, stop() {}, onended: null }),
  };
}

function faceMsg(seg, n, k = 3) {
  // 第 0 个通道 = 帧号 (方便核对时间), 其余为 0
  const w = new Uint8Array(n * k);
  for (let i = 0; i < n; i++) w[i * k] = i;
  return { seg, fps: 30, n, names: ['frame', 'b', 'c'].slice(0, k), w: Buffer.from(w).toString('base64') };
}

test('faceAt: 按此刻在播的句子和句内偏移取帧, 跨块连续, 帧间插值', () => {
  const ctx = fakeCtx();
  const out = new AudioOut(ctx);
  const sr = 16000;
  const chunk = new Int16Array(sr * 0.2);
  const a = out.play(1, sr, chunk, 1);           // 第 1 句: 两块各 0.2 s
  out.play(1, sr, chunk, 1);
  const c = out.play(1, sr, chunk, 2);           // 第 2 句
  assert.equal(out.faceAt(), null, '还没有面部数据');
  assert.ok(out.addFace(1, faceMsg(1, 13)));
  assert.ok(!out.addFace(0, faceMsg(1, 13)), '别的轮次不收');

  ctx.currentTime = a.start + 0.1 - 0.015;       // lookahead 0.015 s 后正好是第 1 句的 0.1 s = 第 3 帧
  let f = out.faceAt();
  assert.ok(Math.abs(f.values[0] * 255 - 3) < 1e-3, `第 3 帧, 实际 ${f.values[0] * 255}`);
  ctx.currentTime = a.start + 0.25 - 0.015;      // 第二块里: 句内 0.25 s = 第 7.5 帧, 插值
  f = out.faceAt();
  assert.ok(Math.abs(f.values[0] * 255 - 7.5) < 1e-3, `第 7.5 帧, 实际 ${f.values[0] * 255}`);
  assert.deepEqual(f.names, ['frame', 'b', 'c']);

  ctx.currentTime = c.start + 0.05;              // 第 2 句的数据还没到: 退回频谱口型
  assert.equal(out.faceAt(), null);
  out.addFace(1, faceMsg(2, 7));
  assert.ok(out.faceAt() !== null);
  ctx.currentTime = c.start + 1;                 // 全部播完: 没有在播的块
  assert.equal(out.faceAt(), null);
});

test('分段到达: 按 start 接在后面, 还没到的帧返回 null, 对不上的段不拼', () => {
  const ctx = fakeCtx();
  const out = new AudioOut(ctx);
  const a = out.play(1, 16000, new Int16Array(16000), 1);  // 1 s = 30 帧
  const part = (start, n, final) => {
    const m = faceMsg(1, start + n);
    const raw = Buffer.from(m.w, 'base64').subarray(start * 3);  // 第 start 帧起的 n 帧, 第 0 通道仍是帧号
    return { ...m, start, n, final, w: Buffer.from(raw).toString('base64') };
  };
  assert.ok(out.addFace(1, part(0, 10, false)));
  ctx.currentTime = a.start + 5 / 30 - 0.015;
  assert.ok(Math.abs(out.faceAt().values[0] * 255 - 5) < 1e-3);
  ctx.currentTime = a.start + 12 / 30 - 0.015;
  assert.equal(out.faceAt(), null, '第 12 帧还没到');
  assert.ok(!out.addFace(1, part(11, 5, false)), '中间缺了第 10 帧, 不拼');
  assert.ok(out.addFace(1, part(10, 21, true)));
  assert.ok(Math.abs(out.faceAt().values[0] * 255 - 12) < 1e-3, '拼上之后能取到');
  ctx.currentTime = a.start + 0.999 - 0.015;
  assert.ok(out.faceAt() !== null, '最后一段到了 (final), 句末按最后一帧取');
});

test('换一轮时清掉上一轮的面部数据', () => {
  const ctx = fakeCtx();
  const out = new AudioOut(ctx);
  out.play(1, 16000, new Int16Array(3200), 1);
  out.addFace(1, faceMsg(1, 7));
  const b = out.play(2, 16000, new Int16Array(3200), 1);
  ctx.currentTime = b.start + 0.05;
  assert.equal(out.faceAt(), null, '第 2 轮第 1 句不能用第 1 轮第 1 句的数据');
});

test('ARKit -> 五个口型: 闭嘴、张大、圆唇、横拉', () => {
  const zero = { jawOpen: 0, mouthClose: 0, mouthFunnel: 0, mouthPucker: 0, mouthStretchLeft: 0.15,
    mouthStretchRight: 0.15, mouthSmileLeft: 0.1, mouthSmileRight: 0.1 };
  const top = (v) => Object.entries(v).sort((x, y) => y[1] - x[1])[0][0];
  const closed = arkitToVisemes(zero);
  assert.ok(Object.values(closed).every((x) => x < 0.01), JSON.stringify(closed));
  assert.equal(top(arkitToVisemes({ ...zero, jawOpen: 0.4 })), 'aa');
  assert.equal(top(arkitToVisemes({ ...zero, jawOpen: 0.05, mouthPucker: 0.45 })), 'ou');
  assert.equal(top(arkitToVisemes({ ...zero, jawOpen: 0.35, mouthFunnel: 0.45 })), 'oh');
  assert.equal(top(arkitToVisemes({ ...zero, jawOpen: 0.07, mouthStretchLeft: 0.45, mouthStretchRight: 0.45 })), 'ih');
  assert.equal(top(arkitToVisemes({ ...zero, jawOpen: 0.35, mouthStretchLeft: 0.45, mouthStretchRight: 0.45 })), 'ee');
  const pressed = arkitToVisemes({ ...zero, jawOpen: 0.1, mouthClose: 0.15 });  // 抿嘴 (b/p/m): 嘴是合上的
  assert.ok(pressed.aa < 0.05, JSON.stringify(pressed));
  for (const v of Object.values(arkitToVisemes({ ...zero, jawOpen: 1, mouthFunnel: 1, mouthStretchLeft: 1 }))) {
    assert.ok(v >= 0 && v <= 1);
  }
});

test('韵律: 句内秒数换算到音频时钟, 重音点头/扬眉、升调歪头、降调低头; 有韵律后不再按响度点头', () => {
  const ctx = fakeCtx();
  const out = new AudioOut(ctx);
  const a = out.play(3, 16000, new Int16Array(8000), 1);   // 第 1 句: 0.5 s + 0.5 s
  const b = out.play(3, 16000, new Int16Array(8000), 1);
  assert.equal(out.segTime(1, 0.25), a.start + 0.25);
  assert.equal(out.segTime(1, 0.75), b.start + 0.25);
  assert.equal(out.segTime(2, 0.1), null, '这一句的音频还没到');
  const calls = [];
  const avatar = { headAction: (n, o) => calls.push(['nod', o.amplitude]), pulseAU: (k) => calls.push(['au', k]),
    glance() {}, lookAtUser() {} };
  const p = new SpeechPerformer();
  p.attach(avatar);
  p.addProsody([{ t: 1.0, k: 'accent', s: 0.8, hi: true }, { t: 1.3, k: 'accent', s: 0.9, hi: false },
    { t: 2.0, k: 'accent', s: 0.4, hi: false }, { t: 2.5, k: 'rise' }, { t: 3.5, k: 'fall' }]);
  const names = p.events.map((e) => e.name);
  assert.deepEqual(names.filter((n) => n === 'accent_nod').length, 2, '相隔 0.3 s 的第二个重音被合并');
  assert.ok(names.includes('accent_brow') && names.includes('rise_tilt') && names.includes('fall_settle'));
  p.update(10, 0, 0.016);
  assert.ok(calls.some(([k, v]) => k === 'nod' && v > 0.2), '强重音点头幅度更大');
  assert.ok(Math.abs(p.neckTarget.roll) < 1e-9 && Math.abs(p.neckTarget.pitch) < 1e-9, '歪头和低头都回正了');
  p.addAudio(20, [{ level: 0 }, { level: 0 }, { level: 0.9 }]);
  assert.ok(!p.events.some((e) => e.name === 'beat_nod'), '有韵律分析后不再按响度点头');
});
