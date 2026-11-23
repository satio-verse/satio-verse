#!/usr/bin/env node
// R1a 动作网络 (web/lmm.js 读 tools/train_lmm.py 导出的 lmmneutral.bin / lmmidle.bin) 在 Node 里的核对:
//   1. 和 PyTorch 对得上: 文件头里存的样本 (解压、步进、投影) 用 JS 重算, 看最大误差;
//   2. 耗时: 每个网络算一次多久; 模拟播放 (每 1/60 秒推进 0.625 帧, 每 6 帧搜一次) 每帧多久;
//   3. 离线误差: 拿原来的动作库当真值. 中性库: 从某一帧的匹配特征投影过来, 播 2 秒, 每帧的骨骼指向和真值差几度;
//      待机库: 每一帧直接比.
//   node tools/lmm_check.mjs [--dir models/motion] [--json 结果.json]
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { LMMIdle, LMMMatch, LMMNeutral, Net, parseLMM } from '../web/lmm.js';
import { loadActions, loadDB, samplePose, search } from '../web/mm.js';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const a = { dir: join(ROOT, 'models/motion'), json: null };
for (let i = 2; i < process.argv.length; i++) a[process.argv[i].replace(/^--/, '')] = process.argv[++i];
const buf = (p) => { const b = readFileSync(p); return b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength); };
const db = (name, loader) => loader(JSON.parse(readFileSync(join(ROOT, 'models/motion', `${name}.json`), 'utf8')), buf(join(ROOT, 'models/motion', `${name}.bin`)));
const maxDiff = (x, y) => x.reduce((m, v, i) => Math.max(m, Math.abs(v - y[i])), 0);
const qrot = (q, v) => {
  const [x, y, z, w] = q, tx = 2 * (y * v[2] - z * v[1]), ty = 2 * (z * v[0] - x * v[2]), tz = 2 * (x * v[1] - y * v[0]);
  return [v[0] + w * tx + y * tz - z * ty, v[1] + w * ty + z * tx - x * tz, v[2] + w * tz + x * ty - y * tx];
};
/** 两个姿态 (每根骨骼一个四元数) 的骨骼指向平均差几度. */
function dirErr(pa, pb, canon) {
  let s = 0;
  for (let b = 0; b < canon.length; b++) {
    const u = qrot(pa[b], canon[b]), v = qrot(pb[b], canon[b]);
    s += Math.acos(Math.max(-1, Math.min(1, u[0] * v[0] + u[1] * v[1] + u[2] * v[2]))) * 180 / Math.PI;
  }
  return s / canon.length;
}
const q = (arr, p) => { const s = [...arr].sort((x, y) => x - y); return s.length ? +s[Math.min(s.length - 1, Math.floor(s.length * p))].toFixed(4) : null; };
const mean = (v) => (v.length ? +(v.reduce((s, x) => s + x, 0) / v.length).toFixed(4) : null);
function bench(fn, n = 2000) { for (let i = 0; i < 200; i++) fn(); const t0 = performance.now(); for (let i = 0; i < n; i++) fn(); return +((performance.now() - t0) / n).toFixed(4); }

const out = {};
// -- 中性库 --
{
  const file = parseLMM(buf(join(a.dir, 'lmmneutral.bin'))), h = file.head, ck = h.check;
  const lmm = new LMMNeutral(file), NZ = h.nz;
  const zr = Float32Array.from(ck.xz.map((v, i) => (i < 27 ? v : v * lmm.zStd[i - 27] + lmm.zMean[i - 27])));
  const dec = lmm.D.run(zr), st = lmm.S.run(Float32Array.from(ck.xz)), pr = lmm.P.run(Float32Array.from(ck.query));
  const step = Array.from(st, (d, k) => ck.xz[k] + d * lmm.dStd[k] + lmm.dMean[k]);
  const kpar = lmm.K && ck.contact ? +maxDiff(Array.from(lmm.K.run(Float32Array.from(ck.xz))), ck.contact).toExponential(2) : null;
  out.neutral = { file: file.bytes, params: h.params, hidden: h.hidden, parity: { contact: kpar, decompressor: +maxDiff(Array.from(dec), ck.decoded).toExponential(2),
    stepper: +maxDiff(step, ck.step).toExponential(2), projector: +maxDiff(Array.from(pr), ck.projected).toExponential(2) },
  ms: { decompressor: bench(() => lmm.D.run(zr, lmm._out)), stepper: bench(() => lmm.S.run(Float32Array.from(ck.xz), lmm._out)),
    projector: bench(() => lmm.P.run(Float32Array.from(ck.query), lmm._out)) }, bytes: lmm.bytes() };
  // 离线误差: 从原库某帧的特征投影过来, 播 60 帧 (2 秒), 和原库对应帧比
  const src = db('neutral', loadDB), canon = src.bones.map((n) => src.meta.canon[n]);
  const horizon = Math.max(...src.meta.horizon), KS = [0, 15, 30, 60, 120, 240], errs = Object.fromEntries(KS.map((k) => [k, []]));
  const hipsErr = [], contactOk = [], long = new LMMNeutral(file, { slotLen: 512 });
  for (const c of src.clips) {
    for (let f = c.start; f < c.end - horizon - 61; f += 97) {
      const r = long.search(src.feats.subarray(f * 27, f * 27 + 27));
      for (const k of KS) {
        if (f + k >= c.end - 1) continue;  // 往后推 k 帧还在同一片段里才比
        const pa = [], pb = [];
        const sa = long.sample(r.frame + k, pa), sb = samplePose(src, f + k, pb);
        errs[k].push(dirErr(pa, pb, canon));
        if (k === 30) { hipsErr.push(Math.hypot(...sa.hips.map((v, i) => v - sb.hips[i]))); contactOk.push(+(sa.contacts[0] === sb.contacts[0]) + +(sa.contacts[1] === sb.contacts[1])); }
      }
    }
  }
  out.neutral.rollout = { starts: errs[0].length, dirDeg: Object.fromEntries(Object.entries(errs).map(([k, v]) => [`${k}f`, { n: v.length, mean: mean(v), p95: q(v, 0.95) }])),
    hips30f: mean(hipsErr), contactAcc30f: +(contactOk.reduce((s, x) => s + x, 0) / (2 * contactOk.length)).toFixed(4) };
  // 触地的时刻: 从原库某帧投影过来播 60 帧, 逐帧取触地标记, 和原库比"着地 (0->1)"、"离地 (1->0)"的时刻.
  // 真事件在 ±6 帧里找最近的预测事件; 报找到的比例、时刻差 (帧, 正 = 晚)、多出来的事件 (每秒几次). 脚锁跟着这些时刻走
  const timing = { on: { n: 0, hit: 0, d: [], extra: 0 }, off: { n: 0, hit: 0, d: [], extra: 0 } };
  let seconds = 0;
  const evts = (s) => { const on = [], off = []; for (let k = 1; k < s.length; k++) { if (!s[k - 1] && s[k]) on.push(k); if (s[k - 1] && !s[k]) off.push(k); } return { on, off }; };
  for (const c of src.clips) {
    for (let f = c.start; f < c.end - horizon - 61; f += 97) {
      const r = long.search(src.feats.subarray(f * 27, f * 27 + 27)), pa = [];
      const pred = [[], []], tru = [[], []];
      for (let k = 0; k <= 60; k++) {
        const s = long.sample(r.frame + k, pa);
        for (let i = 0; i < 2; i++) { pred[i].push(s.contacts[i]); tru[i].push(src.contacts[(f + k) * 2 + i]); }
      }
      seconds += 2;
      for (let i = 0; i < 2; i++) {
        const ep = evts(pred[i]), et = evts(tru[i]);
        for (const kind of ['on', 'off']) {
          const used = new Set();
          for (const t of et[kind]) {
            timing[kind].n++;
            let best = null;
            for (const p of ep[kind]) if (!used.has(p) && Math.abs(p - t) <= 6 && (best === null || Math.abs(p - t) < Math.abs(best - t))) best = p;
            if (best !== null) { used.add(best); timing[kind].hit++; timing[kind].d.push(best - t); }
          }
          timing[kind].extra += ep[kind].length - used.size;
        }
      }
    }
  }
  out.neutral.contactTiming = Object.fromEntries(Object.entries(timing).map(([k, t]) => [k, { events: t.n, found: +(t.hit / Math.max(1, t.n)).toFixed(4),
    // 多出来的事件是两只脚加在一起数的: 每只脚每秒要再除以 2 (2026-10-27 以前的结果里 extraPerSec 是两只脚合计, 标错成"每只脚")
    absFrames: mean(t.d.map(Math.abs)), signedFrames: mean(t.d), extraBothFeetPerSec: +(t.extra / Math.max(1, seconds)).toFixed(4),
    extraPerFootSec: +(t.extra / Math.max(1, 2 * seconds)).toFixed(4) }]));
  // 模拟播放的每帧耗时: 每 1/60 秒推进 0.625 帧 (步频 1.25), 每 6 帧搜一次 (查询取原库随机帧)
  const fresh = new LMMNeutral(file), pose = [], times = [], searchTimes = [];
  let frame = fresh.search(src.feats.subarray(0, 27)).frame, seed = 7;
  const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
  for (let tick = 0; tick < 6000; tick++) {
    const t0 = performance.now();
    if (tick % 6 === 0) {
      const f = Math.floor(rnd() * (src.F - 40));
      const r = fresh.search(src.feats.subarray(f * 27, f * 27 + 27));
      if (rnd() < 0.3) frame = r.frame;  // 运动匹配大约三成的搜索会换
      searchTimes.push(performance.now() - t0);
    }
    const end = fresh.clips[fresh.clipOf[Math.floor(frame)]].end - 1;
    frame = Math.min(frame + 0.625, end);
    if (frame >= end) frame = fresh.search(src.feats.subarray(0, 27)).frame;
    fresh.sample(frame, pose);
    times.push(performance.now() - t0);
  }
  out.neutral.tick = { ms: { mean: mean(times), p50: q(times, 0.5), p95: q(times, 0.95), p99: q(times, 0.99), max: q(times, 1) },
    searchMs: { mean: mean(searchTimes), p95: q(searchTimes, 0.95) }, stats: fresh.stats };
}
// -- 待机库 (只重训中性库时目录里没有) --
if (existsSync(join(a.dir, 'lmmidle.bin'))) {
  const file = parseLMM(buf(join(a.dir, 'lmmidle.bin'))), h = file.head, ck = h.check;
  const lmm = new LMMIdle(file);
  const dec = lmm.D.run(Float32Array.from(ck.z));
  const src = db('idle', loadActions), canon = src.bones.map((n) => src.meta.canon[n]);
  const errs = [], hipsErr = [], times = [];
  for (let f = 0; f < src.F; f += 7) {
    const pa = [], pb = [];
    const t0 = performance.now();
    const sa = lmm.sample(f + 0.5, pa);
    times.push(performance.now() - t0);
    const sb = samplePose(src, f + 0.5, pb);
    errs.push(dirErr(pa, pb, canon));
    hipsErr.push(Math.hypot(...sa.hips.map((v, i) => v - sb.hips[i])));
  }
  out.idle = { file: file.bytes, params: h.params, hidden: h.hidden, key: h.meta.key, parity: { decompressor: +maxDiff(Array.from(dec), ck.decoded).toExponential(2) },
    dirDeg: { mean: mean(errs), p95: q(errs, 0.95), max: q(errs, 1) }, hips: mean(hipsErr),
    sampleMs: { mean: mean(times), p95: q(times, 0.95) }, bytes: lmm.bytes() };
}
// -- 第三版 (压缩的匹配库): 解压和 PyTorch 对不对得上; 搜索结果和原库 (mm.js) 的搜索是不是同一帧 --
if (existsSync(join(a.dir, 'lmmmatch.bin'))) {
  const file = parseLMM(buf(join(a.dir, 'lmmmatch.bin'))), h = file.head, ck = h.check;
  const lmm = new LMMMatch(file, { slotLen: 64 });
  const dec = lmm.D.run(Float32Array.from([...ck.x, ...ck.z]));
  const src = db('neutral', loadDB);
  let same = 0, n = 0, costRatio = [];
  const ts = [], tb = [];
  let seed = 11;
  const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
  for (let i = 0; i < 400; i++) {
    // 查询: 原库某帧的特征, 一半换成另一帧的轨迹, 加一点噪声 (像行动层的查询)
    const f = Math.floor(rnd() * (src.F - 1)), g = Math.floor(rnd() * (src.F - 1)), qv = Float32Array.from(src.feats.subarray(f * 27, f * 27 + 27));
    if (rnd() < 0.5) qv.set(src.feats.subarray(g * 27, g * 27 + 12));
    for (let k = 0; k < 27; k++) qv[k] += (rnd() - 0.5) * 0.2;
    lmm.playing = -1;
    let t0 = performance.now();
    const r = lmm.search(qv);
    ts.push(performance.now() - t0);
    t0 = performance.now();
    const rb = search(src, qv);
    tb.push(performance.now() - t0);
    n++;
    if (lmm.origin[lmm.clipOf[r.frame]] === rb.frame) same++;
    costRatio.push(r.cost / Math.max(1e-9, rb.cost));
  }
  out.match = { file: file.bytes, params: h.params, key: h.meta.key, parity: { decompressor: +maxDiff(Array.from(dec), ck.decoded).toExponential(2) },
    sameFrameAsMM: +(same / n).toFixed(4), costRatio: { mean: mean(costRatio), p95: q(costRatio, 0.95) },
    searchMs: { match: mean(ts), mmOriginal: mean(tb) }, bytes: lmm.bytes() };
}
out.originalBytes ={ neutral: readFileSync(join(ROOT, 'models/motion/neutral.bin')).length, idle: readFileSync(join(ROOT, 'models/motion/idle.bin')).length };
if (a.json) writeFileSync(a.json, JSON.stringify(out, null, 1));
console.log(JSON.stringify(out, null, 1));
void Net;
