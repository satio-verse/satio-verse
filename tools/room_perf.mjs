#!/usr/bin/env node
// 小屋 (桌宠的「抹茶樱花」小筑居) 每帧耗时逐层计时: 她站着、走路、冲咖啡端着喝、杯子掉下来、洒一大股时, 一帧的时间花在哪一层.
// 2026-10-27 查"小窝有物理交互时很卡"时写的 (见知识库「小屋性能实验记录」). 代理、无界面 Chrome 的做法同 tools/room_diag.mjs, 不改仓库文件:
// 代理在 /pet/pet.js 末尾加一行把 state、talk、motion、shell 挂到 window.__pet; 载入后在页面里给各层的函数包一层计时 (只量不改):
//   帧 = talk.frame 开始到 shell.tick 结束 (talk / motion / 小屋 before (props、life、房间缓存重画) / 角色 apply (表情引擎、
//   行动层按运动仲裁表的每一步、姿势层、手势、关节范围、接触层 (外展、肘转角、裙子、握) 、手部、平滑、落地、运动学监测、vrm.update
//   (弹簧骨)、渲染) / 小屋 after (刚体: Rapier 的 step; 液体: 子步、落点射线、画液面; 体感) / 掩码);
//   次数: vrm.scene.updateMatrixWorld、接触层 _elbowPen / _armPen 试探、props.node 查找、液体落点射线 (缓存没中的)、localStorage 写、
//   WebSocket 发; 帧间隔 (卡顿)、长任务 (PerformanceObserver longtask)、JS 堆; GC 停顿用 CDP Tracing (devtools.timeline 的 MinorGC / MajorGC).
//
//   node tools/room_perf.mjs --plan stand:20,walk:30,coffee,spill:12 --rigid 0 --out a.json
//   node tools/room_perf.mjs --plan coffee,drop --rigid 1 --out b.json
//   node tools/room_perf.mjs --report a.json[,b.json]     每一段: 帧耗时 P50 / P95 / 最差、各层平均和 P95、次数、GC、存储
// 剧本 (逗号分隔, 按顺序): stand[:秒] 站着不动 (生活暂停); walk[:秒] 在两处之间来回走; coffee 冲一杯咖啡、端着喝三口、放回去 (按活动的阶段分段);
//   drop[:秒] (刚体开着才有) 冲咖啡端起来以后手松掉, 杯子连咖啡掉下去, 量之后的秒数; spill[:秒] 从 1 米高倒 60 ml 下来 (液体落点、一摊);
//   idle[:秒] 生活照常 (她自己挑事做).
// 选项: --rigid 0|1 (页面 rigid=1); --fps 10 (宿主的空闲帧率; 做动作时页面自己升到 30); --gpusync 1 (每次渲染后读 1 个像素, 把 GPU 的时间算进来);
//   --throttle 4 (CDP 把 CPU 放慢几倍, 模拟慢电脑); --profile 段名 (这一段录 CPU 采样, 出自身耗时最多的函数); --ws 1 (连模拟服务, 量上行消息);
//   --server、--ovr、--patch、--gpu、--avatar 同 room_diag.mjs; --seed 7 (Math.random 定种子, A/B 用);
//   --alloc 段名 (这一段做分配采样, 出分配最多的函数);
//   --light 1 (轻量: 只量帧耗时、堆、GC, 不包各层; 量分配速率和 GC 用这个);
//   --probe 文件.js (剧本跑完后在页面里执行, 返回值记进结果的 probe); --jsflags "..." (传给 V8 的参数, 比如 --allow-natives-syntax).
// 坑: 机器被别的会话占满时整帧计时两轮能差一倍, A/B 要交替跑; 小改动用页面内微基准 (同一状态原版 / 快速版轮流跑) 下结论.
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { createServer, request as httpRequest } from 'node:http';
import { createServer as netServer } from 'node:net';
import { tmpdir } from 'node:os';
import { basename, dirname, extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const BROWSERS = ['C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', 'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  '/usr/bin/google-chrome', '/usr/bin/chromium'];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const A = { server: '', plan: 'stand:20,walk:30,coffee', rigid: 0, out: null, fps: 10, gpu: 1, gpusync: 0, throttle: 1, profile: '', ws: 0,
  alloc: '', query: '', patch: null, probe: null, ovr: '', report: '', avatar: '', seed: 7, keep: 0, light: 0, jsflags: '', shot: '' };
for (let i = 2; i < process.argv.length; i++) { const k = process.argv[i].replace(/^--/, ''); A[k] = process.argv[++i]; }

const pct = (a, p) => { if (!a.length) return 0; const s = a.slice().sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(s.length * p))]; };
const mean = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0);

// -- 报告 -----------------------------------------------------------------------------------------
/** 一段的统计: frames 是这一段的帧. */
export function segStats(frames, gc = []) {
  const work = frames.map((f) => f.work), gap = frames.map((f) => f.gap).filter((x) => x > 0);
  const keys = new Set();
  for (const f of frames) for (const k of Object.keys(f.ms || {})) keys.add(k);
  const layers = {};
  for (const k of keys) {
    const a = frames.map((f) => f.ms?.[k] || 0);
    layers[k] = { mean: +mean(a).toFixed(3), p95: +pct(a, 0.95).toFixed(3), max: +Math.max(...a).toFixed(2) };
  }
  const cnt = {};
  for (const f of frames) for (const [k, n] of Object.entries(f.n || {})) cnt[k] = (cnt[k] || 0) + n;
  for (const k of Object.keys(cnt)) cnt[k] = +(cnt[k] / Math.max(1, frames.length)).toFixed(2);
  const secs = frames.length ? (frames[frames.length - 1].t - frames[0].t) / 1000 : 0;
  const heap = frames.map((f) => f.heap).filter(Boolean);
  let alloc = 0;
  for (let i = 1; i < heap.length; i++) if (heap[i] > heap[i - 1]) alloc += heap[i] - heap[i - 1];
  return {
    frames: frames.length, secs: +secs.toFixed(1), fps: +(frames.length / Math.max(0.001, secs)).toFixed(1),
    work: { p50: +pct(work, 0.5).toFixed(2), p95: +pct(work, 0.95).toFixed(2), p99: +pct(work, 0.99).toFixed(2), max: +Math.max(0, ...work).toFixed(1), mean: +mean(work).toFixed(2) },
    gap: { p50: +pct(gap, 0.5).toFixed(1), p95: +pct(gap, 0.95).toFixed(1), max: +Math.max(0, ...gap).toFixed(1), over50: gap.filter((x) => x > 50).length, over100: gap.filter((x) => x > 100).length },
    layers, counts: cnt,
    heapMB: heap.length ? { start: +(heap[0] / 1048576).toFixed(1), end: +(heap[heap.length - 1] / 1048576).toFixed(1), allocMBps: +(alloc / 1048576 / Math.max(0.001, secs)).toFixed(2) } : null,
    gc: { n: gc.length, ms: +gc.reduce((s, g) => s + g.ms, 0).toFixed(1), max: +Math.max(0, ...gc.map((g) => g.ms)).toFixed(2),
      major: gc.filter((g) => g.major).length },
  };
}

if (A.report) {
  const runs = A.report.split(',').map((f) => ({ f, d: JSON.parse(readFileSync(f, 'utf8')) }));
  const segs = [];
  for (const { d } of runs) for (const s of Object.keys(d.stats)) if (!segs.includes(s)) segs.push(s);
  const show = (x) => (x === undefined || x === null ? '-' : typeof x === 'number' ? String(x) : x);
  console.log(`每格按 ${runs.map((r) => basename(r.f)).join(' / ')} 排`);
  for (const s of segs) {
    const st = runs.map(({ d }) => d.stats[s] || null);
    console.log(`\n== ${s}  (帧数 ${st.map((x) => show(x?.frames)).join(' / ')}, 帧率 ${st.map((x) => show(x?.fps)).join(' / ')})`);
    const line = (name, get) => console.log(`  ${name.padEnd(22)}${st.map((x) => show(x ? get(x) : null).padStart(10)).join('')}`);
    line('帧耗时 P50 ms', (x) => x.work.p50);
    line('帧耗时 P95 ms', (x) => x.work.p95);
    line('帧耗时 P99 ms', (x) => x.work.p99);
    line('帧耗时 最差 ms', (x) => x.work.max);
    line('帧间隔 P95 ms', (x) => x.gap.p95);
    line('帧间隔 最差 ms', (x) => x.gap.max);
    line('帧间隔 >100ms 次', (x) => x.gap.over100);
    line('GC 次 / 总 ms', (x) => `${x.gc.n}/${x.gc.ms}`);
    line('GC 最长 ms', (x) => x.gc.max);
    line('堆 MB 起-止', (x) => (x.heapMB ? `${x.heapMB.start}-${x.heapMB.end}` : '-'));
    line('分配 MB/s', (x) => x.heapMB?.allocMBps);
    const keys = new Set();
    for (const x of st) if (x) for (const k of Object.keys(x.layers)) keys.add(k);
    const top = [...keys].sort((a, b) => Math.max(...st.map((x) => x?.layers[b]?.mean || 0)) - Math.max(...st.map((x) => x?.layers[a]?.mean || 0)));
    for (const k of top) {
      if (Math.max(...st.map((x) => x?.layers[k]?.mean || 0)) < 0.02) continue;
      line(`${k} 均/P95/最差`, (x) => (x.layers[k] ? `${x.layers[k].mean}/${x.layers[k].p95}/${x.layers[k].max}` : '-'));
    }
    const ck = new Set();
    for (const x of st) if (x) for (const k of Object.keys(x.counts)) ck.add(k);
    for (const k of [...ck].sort()) line(`#${k} /帧`, (x) => x.counts[k]);
  }
  for (const { f, d } of runs) {
    console.log(`\n${basename(f)}: 存储写 ${JSON.stringify(d.storage)}; 长任务 ${d.longtasks?.length || 0} 个, 最长 ${Math.max(0, ...(d.longtasks || []).map((x) => x.ms)).toFixed(0)} ms; 帧外 ${JSON.stringify(d.outside)}`);
    if (d.allocs) console.log('分配采样 (这一段里分配最多的函数):\n' + d.allocs.map((p) => `  ${p.mb.toFixed(1).padStart(8)} MB  ${p.pct.toFixed(1).padStart(5)}%  ${p.fn}  ${p.at}`).join('\n'));
    if (d.profile) console.log('CPU 采样 (自身耗时最多的函数):\n' + d.profile.map((p) => `  ${p.ms.toFixed(1).padStart(8)} ms  ${p.pct.toFixed(1).padStart(5)}%  ${p.fn}  ${p.at}`).join('\n'));
  }
  process.exit(0);
}

// -- 模拟服务 (没给 --server 时自己起) -----------------------------------------------------------------
let server = null, serverUrl = A.server;
if (!serverUrl) {
  const port = await new Promise((r) => { const s = netServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => r(p)); }); });
  const logDir = mkdtempSync(join(tmpdir(), 'wm-roomperf-log-'));
  server = spawn('python', ['-m', 'wm', '--llm', 'mock', '--asr', 'none', '--tts', 'mock', '--rvc', 'mock', '--port', String(port),
    '--set', 'memory.vault=', '--set', `trace.log_dir=${logDir.replace(/\\/g, '/')}`], { cwd: ROOT, stdio: 'ignore' });
  serverUrl = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 300; i++) {
    if (await fetch(`${serverUrl}/api/status`).then((r) => r.ok).catch(() => false)) break;
    await sleep(1000);
  }
}

// -- 代理 -----------------------------------------------------------------------------------------
const TYPES = { '.js': 'text/javascript', '.mjs': 'text/javascript', '.html': 'text/html', '.css': 'text/css', '.json': 'application/json',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.svg': 'image/svg+xml', '.glb': 'model/gltf-binary', '.wasm': 'application/wasm', '.ktx2': 'image/ktx2' };
const EXPOSE = Buffer.from('\n;window.__pet = { state, PARAMS, talk, motion, shell };\n');
const up = new URL(serverUrl);
const OVR = Object.fromEntries(A.ovr.split(',').filter(Boolean).map((kv) => kv.split('='))
  .map(([k, v]) => [`/${k.replace(/^.*?(static|pet)\//, '$1/')}`, v]));
const proxy = createServer((req, res) => {
  const u = new URL(req.url, 'http://x');
  let file = OVR[u.pathname] || null;
  if (!file && u.pathname.startsWith('/pet/')) {
    const rel = decodeURIComponent(u.pathname.slice(5)) || 'pet.html';
    const dir = normalize(join(ROOT, /^(live2d\/|live2d-models\/|rooms\/)/.test(rel) ? 'desktop/third_party' : 'desktop/web'));
    const f = normalize(join(dir, rel));
    if (!f.startsWith(dir) || !existsSync(f) || !statSync(f).isFile()) { res.writeHead(404); res.end(); return; }
    file = f;
  }
  if (file) {
    let body = readFileSync(file);
    if (u.pathname === '/pet/pet.js') body = Buffer.concat([body, EXPOSE]);
    res.writeHead(200, { 'Content-Type': TYPES[extname(u.pathname)] || 'application/octet-stream', 'Cache-Control': 'no-store' });
    res.end(body);
    return;
  }
  const headers = { ...req.headers, host: up.host, ...(req.headers.origin ? { origin: up.origin } : {}) };
  const p = httpRequest({ host: up.hostname, port: up.port, path: req.url, method: req.method, headers }, (r) => {
    res.writeHead(r.statusCode, r.headers);
    r.pipe(res);
  });
  p.on('error', () => { res.writeHead(502); res.end(); });
  req.pipe(p);
});
// WebSocket (ws=1 时): 代理把升级请求原样转给服务端
proxy.on('upgrade', (req, sock, head) => {
  const p = httpRequest({ host: up.hostname, port: up.port, path: req.url, method: req.method,
    headers: { ...req.headers, host: up.host, ...(req.headers.origin ? { origin: up.origin } : {}) } });
  p.on('upgrade', (r, s2, h2) => {
    sock.write(`HTTP/1.1 101 Switching Protocols\r\n${Object.entries(r.headers).map(([k, v]) => `${k}: ${v}`).join('\r\n')}\r\n\r\n`);
    if (h2?.length) sock.write(h2);
    if (head?.length) s2.write(head);
    s2.pipe(sock).pipe(s2);
  });
  p.on('error', () => sock.destroy());
  p.end();
});
await new Promise((r) => proxy.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${proxy.address().port}`;

// -- 浏览器 ---------------------------------------------------------------------------------------
const exe = BROWSERS.find((p) => existsSync(p));
if (!exe) throw new Error('没找到 Chrome 或 Edge');
const profileDir = mkdtempSync(join(tmpdir(), 'wm-roomperf-'));
const gpu = Number(A.gpu) ? ['--use-angle=d3d11', '--enable-gpu', '--ignore-gpu-blocklist'] : ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'];
const proc = spawn(exe, ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profileDir}`, '--no-first-run', '--no-default-browser-check',
  '--disable-extensions', '--mute-audio', '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
  '--disable-backgrounding-occluded-windows', '--enable-precise-memory-info', ...(A.jsflags ? [`--js-flags=${A.jsflags}`] : []), ...gpu, 'about:blank'], { stdio: 'ignore' });
let port = null;
for (let i = 0; i < 200 && !port; i++) {
  await sleep(100);
  try { const f = join(profileDir, 'DevToolsActivePort'); if (existsSync(f)) port = readFileSync(f, 'utf8').split('\n')[0].trim() || null; } catch { /* 浏览器还在写 */ }
}
if (!port) throw new Error('浏览器没起来');
const { webSocketDebuggerUrl } = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
const ws = new WebSocket(webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let next = 1;
const waiting = new Map(), consoleTail = [], trace = [];
let traceDone = null;
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && waiting.has(m.id)) { const { res, rej } = waiting.get(m.id); waiting.delete(m.id); if (m.error) rej(new Error(m.error.message)); else res(m.result); }
  else if (m.method === 'Runtime.consoleAPICalled') consoleTail.push(m.params.args.map((a) => a.value ?? a.description ?? '').join(' ').slice(0, 300));
  else if (m.method === 'Runtime.exceptionThrown') consoleTail.push(`异常 ${m.params.exceptionDetails?.exception?.description?.slice(0, 400)}`);
  else if (m.method === 'Tracing.dataCollected') {
    for (const e of m.params.value) {
      if (e.name === 'MinorGC' || e.name === 'MajorGC' || e.name === 'V8.GCFinalizeMC' || e.name === 'V8.GCCompactor' || e.name === 'BlinkGC.AtomicPauseMarkEpilogue') {
        if (e.ph === 'X') trace.push({ name: e.name, ts: e.ts / 1000, ms: (e.dur || 0) / 1000 });
      } else if (e.name === 'TimeStamp' && e.args?.data?.message?.startsWith('perf:')) trace.push({ name: 'mark', ts: e.ts / 1000, msg: e.args.data.message });
    }
  } else if (m.method === 'Tracing.tracingComplete') traceDone?.();
  if (consoleTail.length > 80) consoleTail.shift();
};
const send = (method, params = {}, sessionId) => new Promise((res, rej) => {
  const id = next++;
  waiting.set(id, { res, rej });
  ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
});
const evaluate = async (sid, expression) => {
  const r = await send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, sid);
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
};
const close = async () => {
  // 浏览器先断开时 Browser.close 等不到回复: 最多等 3 秒
  try { await Promise.race([send('Browser.close'), sleep(3000)]); } catch { /* 已经关了 */ }
  ws.close(); proc.kill(); proxy.close(); server?.kill();
  await sleep(300);
  try { rmSync(profileDir, { recursive: true, force: true }); } catch { /* 浏览器还占着 */ }
};

const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
const { sessionId: sid } = await send('Target.attachToTarget', { targetId, flatten: true });
await send('Page.enable', {}, sid);
await send('Runtime.enable', {}, sid);
await send('Emulation.setDeviceMetricsOverride', { width: 560, height: 760, deviceScaleFactor: 1, mobile: false }, sid);
// 定种子 (A/B 时两边挑同样的待机片段、同样的事): 页面脚本跑之前就换掉 Math.random
if (Number(A.seed)) {
  await send('Page.addScriptToEvaluateOnNewDocument', { source: `(() => { let s = ${Number(A.seed)} >>> 0; Math.random = () => { s = (s + 0x6D2B79F5) >>> 0;
    let t = s; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; })()` }, sid);
}
const q = [`room=1`, `roomreset=1`, Number(A.ws) ? '' : 'ws=0', Number(A.rigid) ? 'rigid=1' : '', A.avatar ? `avatar=${A.avatar}` : '', A.query].filter(Boolean).join('&');
await send('Page.navigate', { url: `${base}/pet/pet.html?${q}` }, sid);
const t0 = Date.now();
while (!(await evaluate(sid, '!!(window.__pet?.state?.room && window.__pet.state.ready)').catch(() => false))) {
  if (Date.now() - t0 > 180000) { console.error(consoleTail.join('\n')); await close(); throw new Error('小屋 180 秒没载入'); }
  await sleep(500);
}
const loadS = (Date.now() - t0) / 1000;
if (Number(A.rigid)) await evaluate(sid, 'window.__pet.state.room.rigid?.ready');
if (A.patch) await evaluate(sid, readFileSync(A.patch, 'utf8'));
if (Number(A.throttle) > 1) await send('Emulation.setCPUThrottlingRate', { rate: Number(A.throttle) }, sid);

// -- 页面里的计时 ---------------------------------------------------------------------------------
await evaluate(sid, String.raw`(() => {
  const st = window.__pet.state, v = st.avatar, room = st.room, P = window.__perf = { frames: [], seg: '-', outside: {}, storage: { n: 0, bytes: 0, ms: 0, keys: {} },
    ws: { n: 0, bytes: 0, kinds: {} }, longtasks: [] };
  const now = performance.now.bind(performance), GPUSYNC = ${Number(A.gpusync) ? 1 : 0}, LIGHT = ${Number(A.light) ? 1 : 0};
  st.hostFps = ${Number(A.fps)};
  let cur = null, last = 0, inCache = 0;
  const add = (k, ms) => { if (cur) cur.ms[k] = (cur.ms[k] || 0) + ms; else P.outside[k] = +((P.outside[k] || 0) + ms).toFixed(3); };
  const cnt = (k, n = 1) => { if (cur) cur.n[k] = (cur.n[k] || 0) + n; else P.outside['#' + k] = (P.outside['#' + k] || 0) + n; };
  const wrap = (obj, name, key, count = false) => {
    const f = obj?.[name];
    if (typeof f !== 'function' || f.__perf) return false;
    const g = function () { const t = now(); try { return f.apply(this, arguments); } finally { add(key, now() - t); if (count) cnt(key); } };  // arguments: 不分配数组
    g.__perf = true;
    obj[name] = g;
    return true;
  };
  const counter = (obj, name, key) => {
    const f = obj?.[name];
    if (typeof f !== 'function' || f.__perf) return false;
    const g = function () { cnt(key); return f.apply(this, arguments); };
    g.__perf = true;
    obj[name] = g;
    return true;
  };
  // 帧边界: talk.frame 是 frame() 里第一件事, shell.tick 在画完、掩码之后
  const tf = window.__pet.talk.frame.bind(window.__pet.talk);
  window.__pet.talk.frame = (t, dt) => {
    const s = now();
    cur = { t: s, gap: last ? s - last : 0, seg: P.seg, ph: '', ms: {}, n: {}, heap: performance.memory?.usedJSHeapSize || 0 };
    last = s;
    const out = tf(t, dt);
    add('talk', now() - s);
    return out;
  };
  const sh = window.__pet.shell.tick.bind(window.__pet.shell);
  window.__pet.shell.tick = (...a) => {
    const out = sh(...a);
    if (cur) {
      cur.work = now() - cur.t;
      const L = room.layer, held = Object.values(L.items).some((i) => i.held);
      cur.ph = (room.life.current?.id || '-') + ':' + (room.sight?.act || room.poses.name || L.mode) + (room.liquid?.brewing ? '+brew' : '') + (held ? '+held' : '');
      for (const k in cur.ms) cur.ms[k] = +cur.ms[k].toFixed(3);
      P.frames.push(cur);
      if (P.frames.length > 60000) P.frames.shift();
      cur = null;
    }
    return out;
  };
  P.mark = (seg) => { P.seg = seg; console.timeStamp('perf:' + seg); return now(); };
  // 轻量模式: 只量帧边界、堆和 GC (各层的包装本身每次调用都分配, 会把分配速率和 GC 次数量多)
  if (LIGHT) return true;
  wrap(window.__pet.motion, 'update', 'motion');
  // 小屋 before / after
  wrap(room, 'before', 'before');
  wrap(room, 'after', 'after');
  wrap(room.props, 'tick', 'props.tick');
  wrap(room.life, 'tick', 'life.tick');
  wrap(room.props, 'follow', 'props.follow');
  wrap(room.props, 'node', 'props.node', true);
  const cu = room.cache.update.bind(room.cache);
  room.cache.update = () => { const t = now(); inCache++; let drew; try { drew = cu(); } finally { inCache--; } add('cache', now() - t); if (drew) { cnt('cacheDraw'); if (GPUSYNC) { const t1 = now(); syncGpu(); add('cacheGpu', now() - t1); } } return drew; };
  // 角色 apply: 各层按运动仲裁表的 mark 分段 (每一步写完到下一步写完)
  wrap(v, 'apply', 'apply');
  wrap(v.engine, 'update', 'face');
  const arb = v.arbiter;
  if (arb) {
    const ob = arb.begin.bind(arb), om = arb.mark.bind(arb);
    let lastMark = 0, occ = {};
    arb.begin = (...a) => { ob(...a); lastMark = now(); occ = {}; };
    const names = new Map();  // 'm.层' / 'm.层#2' 记住, 不每次拼字符串
    arb.mark = (layer, o) => { om(layer, o); const t = now(); const n = occ[layer] = (occ[layer] || 0) + 1;
      let row = names.get(layer); if (!row) names.set(layer, row = []);
      const k = row[n] ??= 'm.' + layer + (n > 1 ? '#' + n : ''); add(k, t - lastMark); lastMark = t; };
  }
  wrap(v.loco, 'update', 'loco');
  wrap(room.poses, 'update', 'poses');
  wrap(v.loco, '_items', 'loco.items', true);
  wrap(v.loco, '_ground', 'loco.ground');
  wrap(v.loco, '_carrySteady', 'loco.steady');
  wrap(v, '_gestures', 'gestures');
  wrap(v.posture, 'limbs', 'rom');
  wrap(v.posture, 'axial', 'rom');
  wrap(v.contact, 'update', 'contact');
  wrap(v.contact, '_spread', 'contact.spread');
  wrap(v.contact, '_swivel', 'contact.swivel');
  wrap(v.contact, '_skirt', 'contact.skirt');
  wrap(v.contact, '_skirtNow', 'contact.skirtNow');
  wrap(v.contact, '_grasp', 'contact.grasp');
  wrap(v.contact, '_bodyCaps', 'contact.caps', true);
  counter(v.contact, '_elbowPen', 'elbowPen');
  counter(v.contact, '_armPen', 'armPen');
  wrap(v.hands, 'update', 'hands');
  wrap(v.filter, 'apply', 'filter');
  wrap(v.kin, 'update', 'kin');
  wrap(v.vrm, 'update', 'vrm');
  wrap(v.vrm.springBoneManager, 'update', 'vrm.spring');
  wrap(v.vrm.expressionManager, 'update', 'vrm.expr');
  wrap(v.vrm.scene, 'updateMatrixWorld', 'umw', true);
  const gl = v.renderer.getContext(), px = new Uint8Array(4);
  const syncGpu = () => gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px);
  const rr = v.renderer.render.bind(v.renderer), progs = () => v.renderer.info.programs?.length || 0;
  let nProg = progs();
  v.renderer.render = (s, c) => {
    if (inCache) return rr(s, c);
    const t = now();
    rr(s, c);
    add('render', now() - t);
    const np = progs();
    if (np > nProg) cnt('newProgram', np - nProg);  // 第一次画某种材质: 编译着色器
    nProg = np;
    const info = v.renderer.info.render;
    cnt('drawCalls', info.calls); cnt('tris', info.triangles);
    if (GPUSYNC) { const t1 = now(); syncGpu(); add('gpu', now() - t1); }
  };
  // 液体、刚体、体感
  wrap(room.body, 'tick', 'body');
  const L = room.liquid;
  if (L) {
    wrap(L, 'tick', 'liquid');
    wrap(L.world, 'step', 'liq.step');
    wrap(L.world, '_drops', 'liq.drops');
    wrap(L, '_draw', 'liq.draw');
    wrap(L, '_tiltStep', 'liq.tilt');
    wrap(L, '_mouth', 'liq.mouth');
    const gr = L.ground;
    L.ground = (x, z, y) => { const t = now(), n0 = L.groundCache?.size ?? 0; const out = gr(x, z, y); const ms = now() - t;
      if ((L.groundCache?.size ?? 0) > n0) { cnt('liqRay'); add('liq.ray', ms); } else cnt('liqGroundHit'); return out; };
    if (L.world.drops) Object.defineProperty(P, 'drops', { get: () => L.world.drops.length });
  }
  const R = room.rigid;
  if (R?.layer) {
    wrap(R, 'tick', 'rigid');
    wrap(R.layer, 'tick', 'rigid.layer');
    wrap(R.world, 'step', 'rigid.step');
    wrap(R.world.world, 'step', 'rapier', true);
    wrap(R, '_touch', 'rigid.touch');
  }
  // 存储、上行消息
  const si = Storage.prototype.setItem;
  Storage.prototype.setItem = function (k, val) { const t = now(); try { return si.call(this, k, val); } finally { const ms = now() - t; P.storage.n++; P.storage.bytes += String(val).length * 2; P.storage.ms += ms;
    const e = (P.storage.keys[k] ??= { n: 0, bytes: 0, ms: 0, max: 0 }); e.n++; e.bytes = String(val).length * 2; e.ms = +(e.ms + ms).toFixed(3); e.max = Math.max(e.max, +ms.toFixed(3)); cnt('store'); add('store', ms); } };
  const wsSend = WebSocket.prototype.send;
  WebSocket.prototype.send = function (d) { const s = typeof d === 'string' ? d : ''; P.ws.n++; P.ws.bytes += s.length || d?.byteLength || 0;
    const kind = (s.match(/"type"\s*:\s*"([^"]+)"/) || [])[1] || '?'; P.ws.kinds[kind] = (P.ws.kinds[kind] || 0) + 1; cnt('ws'); return wsSend.call(this, d); };
  if (room.sight) wrap(room.sight, 'tick', 'sight.tick');
  wrap(room.needs, 'tick', 'needs.tick');
  try { new PerformanceObserver((l) => { for (const e of l.getEntries()) P.longtasks.push({ t: +e.startTime.toFixed(0), ms: +e.duration.toFixed(0), seg: P.seg }); }).observe({ type: 'longtask', buffered: false }); } catch { /* 不支持 */ }
  P.mark = (seg) => { P.seg = seg; console.timeStamp('perf:' + seg); return now(); };
  return true;
})()`);

// -- 剧本 -----------------------------------------------------------------------------------------
const tracing = send('Tracing.start', { categories: 'devtools.timeline,v8,disabled-by-default-v8.gc', transferMode: 'ReportEvents' }).catch(() => null);
await tracing;
const marks = [];  // [段名, 页面里的 performance.now()]
const mark = async (seg) => { marks.push([seg, await evaluate(sid, `window.__perf.mark(${JSON.stringify(seg)})`)]); };
const R = 'window.__pet.state.room';
const status = () => evaluate(sid, `(() => { const r = ${R}; return { act: r.life.current?.id || null, pose: r.poses.name, mode: r.layer.mode,
  sact: r.sight?.act || null, held: Object.values(r.layer.items).some((i) => i.held), status: document.querySelector('.room-status')?.textContent || '' }; })()`);
const waitFor = async (pred, ms) => { for (const end = Date.now() + ms; ;) { const s = await status(); if (pred(s)) return s; if (Date.now() > end) return null; await sleep(300); } };
const log = [];
let profiled = null, allocs = null;
const profStart = async (seg) => {
  if (A.alloc === seg) {  // 分配采样: 这一段里谁分配的内存最多 (按函数, 自身)
    await send('HeapProfiler.enable', {}, sid);
    await send('HeapProfiler.startSampling', { samplingInterval: 16384, includeObjectsCollectedByMajorGC: true, includeObjectsCollectedByMinorGC: true }, sid);
  }
  if (A.profile !== seg) return;
  await send('Profiler.enable', {}, sid);
  await send('Profiler.setSamplingInterval', { interval: 200 }, sid);
  await send('Profiler.start', {}, sid);
};
const profStop = async (seg) => {
  if (A.alloc === seg) {
    const { profile } = await send('HeapProfiler.stopSampling', {}, sid);
    const by = new Map();
    const walk = (n) => {
      const cf = n.callFrame, key = `${cf.functionName || '(匿名)'}|${cf.url.split('/').slice(-1)[0]}:${cf.lineNumber + 1}`;
      by.set(key, (by.get(key) || 0) + n.selfSize);
      for (const c of n.children || []) walk(c);
    };
    walk(profile.head);
    const total = [...by.values()].reduce((a, b) => a + b, 0);
    allocs = [...by.entries()].sort((a, b) => b[1] - a[1]).slice(0, 40).map(([k, b]) => ({ fn: k.split('|')[0], at: k.split('|')[1], mb: b / 1048576, pct: (100 * b) / total }));
  }
  if (A.profile !== seg) return;
  const { profile } = await send('Profiler.stop', {}, sid);
  const byId = new Map(profile.nodes.map((n) => [n.id, n]));
  const self = new Map();
  const dts = profile.timeDeltas;
  for (let i = 0; i < profile.samples.length; i++) {
    const n = byId.get(profile.samples[i]), cf = n.callFrame;
    const key = `${cf.functionName || '(匿名)'}|${cf.url.split('/').slice(-1)[0]}:${cf.lineNumber + 1}`;
    self.set(key, (self.get(key) || 0) + (dts[i] || 0) / 1000);
  }
  const total = [...self.values()].reduce((a, b) => a + b, 0);
  profiled = [...self.entries()].sort((a, b) => b[1] - a[1]).slice(0, 40).map(([k, ms]) => ({ fn: k.split('|')[0], at: k.split('|')[1], ms, pct: (100 * ms) / total }));
  if (A.out) writeFileSync(A.out.replace(/\.json$/, '') + `-${seg}.cpuprofile`, JSON.stringify(profile));
};
// 生活暂停: 一件做完以后不再自己挑下一件 (goDo 照常, 剧本叫她做什么就做什么)
await evaluate(sid, `(() => { const l = ${R}.life; l.start = () => {}; l.hurry = !!l.current; return true; })()`);
await waitFor((s) => !s.act, 30000);
await sleep(2000);

for (const step of A.plan.split(',').filter(Boolean)) {
  const [name, arg] = step.split(':');
  const secs = Number(arg) || 0;
  log.push({ at: +((Date.now() - t0) / 1000).toFixed(1), step });
  if (name === 'stand' || name === 'idle') {
    if (name === 'idle') await evaluate(sid, `(() => { const l = ${R}.life; delete l.start; l.start(0.5); return true; })()`);
    await mark(name); await profStart(name);
    await sleep((secs || 20) * 1000);
    await profStop(name);
    if (name === 'idle') await evaluate(sid, `(() => { const l = ${R}.life; l.start = () => {}; l.hurry = !!l.current; return true; })()`);
    await waitFor((s) => !s.act, 30000);
  } else if (name === 'walk') {
    // 在两处之间来回走 (行动层的 goTo; 离得最远的两个目标)
    await mark('walk'); await profStart('walk');
    const end = Date.now() + (secs || 30) * 1000;
    const ids = await evaluate(sid, `(() => { const L = ${R}.layer; const t = Object.values(L.targets).filter((x) => x.approach && x.id !== 'user');
      let best = null, d = 0; for (const a of t) for (const b of t) { const dd = Math.hypot(a.approach[0] - b.approach[0], a.approach[1] - b.approach[1]); if (dd > d) { d = dd; best = [a.id, b.id]; } } return best; })()`);
    let k = 0;
    while (Date.now() < end && ids) {
      await evaluate(sid, `${R}.layer.goTo(${JSON.stringify(ids[k++ % 2])})`);
      await sleep(500);
      while (Date.now() < end && (await status()).mode === 'go') await sleep(300);
      await sleep(800);
    }
    await profStop('walk');
  } else if (name === 'coffee' || name === 'drop') {
    // 冲咖啡: 要不太精神 (精力 85 以上不想喝)、有咖啡豆
    await evaluate(sid, `(() => { const r = ${R}; r.needs.v.energy = 40; r.needs.stock.beans = Math.max(3, r.needs.stock.beans || 0); return r.life.goDo('coffee'); })()`);
    await mark(name); await profStart(name);
    if (name === 'coffee') {
      await waitFor((s) => s.act === 'coffee', 20000);
      const done = await waitFor((s) => s.act !== 'coffee', 150000);
      log.push({ step, done: !!done });
    } else {
      // 端起来 (拿起、送到嘴边以前): 手松掉 (握力 0), 杯子连咖啡掉下去
      const held = await waitFor((s) => s.act === 'coffee' && s.held && s.sact === 'lift', 120000);
      await sleep(600);
      await mark('drop+');
      await evaluate(sid, `(() => { const R = ${R}.rigid; R._strength = () => 0; return true; })()`);
      await sleep((secs || 10) * 1000);
      await evaluate(sid, `(() => { const r = ${R}; delete r.rigid._strength; r.life.hurry = true; return true; })()`);
      log.push({ step, held: !!held });
      await waitFor((s) => !s.act, 60000);
    }
    await profStop(name);
    await waitFor((s) => !s.act, 30000);
  } else if (name === 'spill') {
    // 从 1 米高倒 60 ml 下来: 分 30 股、每股 2 ml (像杯子洒出来一样一串一串), 落在她身边
    await mark('spill'); await profStart('spill');
    await evaluate(sid, `(() => { const r = ${R}, c = r.layer.char; let n = 0; const id = setInterval(() => {
      r.liquid.world.pour([c.x + 0.35, 1.0, c.z + 0.2], 2, 60, 'coffee', [(Math.random() - 0.5) * 0.6, 0, (Math.random() - 0.5) * 0.6]);
      if (++n >= 30) clearInterval(id); }, 50); return true; })()`);
    await sleep((secs || 12) * 1000);
    await profStop('spill');
  }
  await sleep(1500);
}
await mark('end');

// -- 收尾: GC 停顿按段归 --------------------------------------------------------------------------
await send('Tracing.end');
await Promise.race([new Promise((r) => { traceDone = r; }), sleep(15000)]);
const data = JSON.parse(await evaluate(sid, `JSON.stringify({ frames: window.__perf.frames, storage: window.__perf.storage, ws: window.__perf.ws,
  outside: window.__perf.outside, longtasks: window.__perf.longtasks, rigid: ${R}.rigid?.snapshot?.() || null, liquid: ${R}.liquid?.snapshot?.() || null,
  cacheDraws: ${R}.cache.draws, cacheWhy: ${R}.cache.why, groundCache: ${R}.liquid?.groundCache?.size ?? null })`));
// 页面时钟和 trace 时钟的对齐: 每个段开头 console.timeStamp('perf:段') 在 trace 里也有一条
const traceMarks = trace.filter((e) => e.name === 'mark');
let off = null;
for (const tm of traceMarks) { const m = marks.find(([s]) => `perf:${s}` === tm.msg); if (m) { off = tm.ts - m[1]; break; } }
const gcs = trace.filter((e) => e.name === 'MinorGC' || e.name === 'MajorGC').map((e) => ({ t: off === null ? null : e.ts - off, ms: e.ms, major: e.name === 'MajorGC' }));
const segOf = (t) => { let s = '-'; for (const [seg, at] of marks) if (t >= at) s = seg; return s; };
const segs = {};
for (const f of data.frames) (segs[f.seg] ??= []).push(f);
// coffee、drop 再按活动的阶段细分 (冲 / 拿起 / 端着 / 喝 / 放下)
for (const f of data.frames) if (f.seg === 'coffee' || f.seg === 'drop' || f.seg === 'drop+') (segs[`${f.seg}>${f.ph.replace(/^[^:]*:/, '')}`] ??= []).push(f);
const stats = {};
for (const [s, fr] of Object.entries(segs)) {
  const base = s.split('>')[0];
  stats[s] = segStats(fr, s.includes('>') ? [] : gcs.filter((g) => g.t !== null && segOf(g.t) === base));
}
const probe = A.probe ? await evaluate(sid, readFileSync(A.probe, 'utf8')).catch((e) => String(e)) : null;
if (A.shot) { const r = await send('Page.captureScreenshot', { format: 'png' }, sid); writeFileSync(A.shot, Buffer.from(r.data, 'base64')); }  // --shot 文件.png: 跑完截一张
const out = { args: A, loadS, marks, log, consoleTail: consoleTail.slice(-30), stats, gcs, probe, frames: data.frames, storage: data.storage, ws: data.ws,
  outside: data.outside, longtasks: data.longtasks, rigid: data.rigid, liquid: data.liquid && { cups: data.liquid.cups, puddles: data.liquid.puddles.length, ledger: data.liquid.ledger },
  cacheDraws: data.cacheDraws, cacheWhy: data.cacheWhy, groundCache: data.groundCache, profile: profiled, allocs };
if (A.out) writeFileSync(A.out, JSON.stringify(out));
console.error(`${data.frames.length} 帧, GC ${gcs.length} 次${A.out ? ` -> ${A.out}` : ''}`);
for (const [s, x] of Object.entries(stats)) if (!s.includes('>')) console.error(`  ${s.padEnd(10)} 帧 ${x.frames} 帧率 ${x.fps}  耗时 P50 ${x.work.p50} P95 ${x.work.p95} 最差 ${x.work.max} ms  间隔最差 ${x.gap.max} ms  GC ${x.gc.n} 次 ${x.gc.ms} ms`);
if (!Number(A.keep)) await close();
process.exit(0);
