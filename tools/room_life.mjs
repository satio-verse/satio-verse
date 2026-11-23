#!/usr/bin/env node
// 小屋生活快进 (2026-10-27 "她在小窝里只会健身、冲咖啡、上床、坐凳子" 时写的): 无界面 Chrome 打开小屋, 不点、不说话, 让她自己过
// 一段时间, 记她做了哪些事、每件多久、做的时候疼不疼、被防穿模推了几次; 需求按 --speed 倍快进 (40 分钟 × 12 倍 ≈ 8 小时的需求变化),
// 页面带 fast=1 (等待最多 2 秒, 两件事之间不歇). 改前改后比"活动的种类和分布".
// 代理、浏览器的接法和 tools/room_diag.mjs 一样 (/pet/* 读 desktop/web 和 desktop/third_party; 不改仓库文件; --ovr 换文件做 A/B).
//
//   node tools/room_life.mjs --minutes 40 --speed 12 --seed 1 --out after.json [--shots 目录]
//   node tools/room_life.mjs --report before.json,after.json [--csv 分布.csv]
// 选项: --server http://127.0.0.1:端口 (给了就连上它, 她隔几分钟问服务端; 不给是 ws=0, 只按本机规则);
//   --ovr "pet/room-acts.js=旧文件,..." (不带开头的斜杠, Git Bash 会把它当路径改掉); --query 额外的页面参数; --fps 10; --gpu 0 软件渲染;
//   --known 0 (开始时她什么都没见过: 和 roomreset 一样; 1 是先让她把房间走一圈再开始计); --fast 0|1 (默认 1, 0 保留活动等待原时长).
import { spawn } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs';
import { createServer, request as httpRequest } from 'node:http';
import { connect as netConnect, createServer as createNetServer } from 'node:net';
import { tmpdir } from 'node:os';
import { basename, dirname, extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const BROWSERS = ['C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', 'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  '/usr/bin/google-chrome', '/usr/bin/chromium'];
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const A = { server: '', minutes: 30, speed: 12, seed: 1, out: null, fps: 10, gpu: 1, query: '', shots: '', ovr: '', report: '', csv: '', avatar: '', fast: 1 };
for (let i = 2; i < process.argv.length; i++) { const k = process.argv[i].replace(/^--/, ''); A[k] = process.argv[++i]; }
if (!['0', '1'].includes(String(A.fast))) throw new Error('--fast 必须是 0 或 1');

// 活动归哪一类 (用户说的"只会健身、冲咖啡、上床、坐凳子" 那几样算老一套)
const HABIT = { yoga: '健身', train: '健身', coffee: '冲咖啡', sleep: '上床', rest: '上床', read: '坐凳子', computer: '坐凳子' };

// -- 报告 -----------------------------------------------------------------------------------------
if (A.report) {
  const runs = A.report.split(',').map((f) => ({ f, d: JSON.parse(readFileSync(f, 'utf8')) }));
  const ids = [...new Set(runs.flatMap(({ d }) => d.done.map((x) => x.id)))];
  const rows = [];
  for (const { f, d } of runs) {
    const ok = d.done.filter((x) => x.ok), n = ok.length || 1, count = {}, secs = {};
    for (const x of ok) { count[x.id] = (count[x.id] || 0) + 1; secs[x.id] = (secs[x.id] || 0) + x.s; }
    const p = Object.values(count).map((c) => c / n), H = -p.reduce((s, x) => s + x * Math.log2(x), 0);
    const habit = ok.filter((x) => HABIT[x.id]).length / n;
    const top4 = Object.values(count).sort((a, b) => b - a).slice(0, 4).reduce((s, c) => s + c, 0) / n;
    rows.push({ f: basename(f), done: d.done.length, ok: ok.length, failed: d.done.length - ok.length, kinds: Object.keys(count).length,
      entropy: +H.toFixed(2), habit: +habit.toFixed(2), top4: +top4.toFixed(2), simHours: d.simHours, count, secs,
      pain: d.body?.pain ?? null, falls: d.body?.falls ?? null, pushes: d.pushes ?? null, painP95: d.painP95 ?? null });
  }
  console.log('文件'.padEnd(28), ['做了', '成了', '没成', '种类', '熵(bit)', '老一套占比', '前4占比', '快进小时', '摔', '推开'].map((s) => s.padStart(9)).join(''));
  for (const r of rows) console.log(r.f.slice(0, 28).padEnd(28), [r.done, r.ok, r.failed, r.kinds, r.entropy, r.habit, r.top4, r.simHours, r.falls, r.pushes].map((x) => String(x).padStart(9)).join(''));
  console.log('\n每件事做了几次 (成了的):');
  for (const id of ids) console.log(`  ${id.padEnd(14)}${(HABIT[id] || '').padEnd(6)}`, rows.map((r) => String(r.count[id] || 0).padStart(6)).join(''));
  if (A.csv) writeFileSync(A.csv, ['id,类,' + rows.map((r) => r.f).join(','), ...ids.map((id) => `${id},${HABIT[id] || '新'},${rows.map((r) => r.count[id] || 0).join(',')}`)].join('\n'));
  for (const { f, d } of runs) {
    const fails = d.done.filter((x) => !x.ok);
    if (fails.length) console.log(`\n${basename(f)} 没成的:`, fails.map((x) => `${x.id}: ${x.error}`).join(' | '));
  }
  process.exit(0);
}

// -- 模拟服务 / 代理 / 浏览器 (同 room_diag.mjs) --------------------------------------------------------
// 没给 --server 时自己起一个模拟服务 (--tts mock, 日志在临时目录), 页面 ws=0 不和它对话 (只用它的静态文件和动作库);
// 给了 --server 时页面连上它 (她隔几分钟问服务端接下来做什么)
let server = null, serverUrl = A.server;
if (!serverUrl) {
  const port = await new Promise((r) => { const s = createNetServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => r(p)); }); });
  const logDir = mkdtempSync(join(tmpdir(), 'wm-roomlife-log-'));
  server = spawn('python', ['-m', 'wm', '--llm', 'mock', '--asr', 'none', '--tts', 'mock', '--rvc', 'mock', '--port', String(port),
    '--set', 'memory.vault=', '--set', `trace.log_dir=${logDir.replace(/\\/g, '/')}`], { cwd: ROOT, stdio: 'ignore' });
  serverUrl = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 300; i++) {
    if (await fetch(`${serverUrl}/api/status`).then((r) => r.ok).catch(() => false)) break;
    await sleep(1000);
  }
}
const TYPES = { '.js': 'text/javascript', '.mjs': 'text/javascript', '.html': 'text/html', '.css': 'text/css', '.json': 'application/json',
  '.png': 'image/png', '.jpg': 'image/jpeg', '.svg': 'image/svg+xml', '.glb': 'model/gltf-binary', '.wasm': 'application/wasm', '.ktx2': 'image/ktx2' };
const EXPOSE = Buffer.from('\n;window.__pet = { state, PARAMS };\n');
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
// /ws (给了 --server 时页面连它): 升级请求原样转过去, Host、Origin 换成服务端的
proxy.on('upgrade', (req, socket, head) => {
  const s = netConnect(Number(up.port), up.hostname, () => {
    const headers = { ...req.headers, host: up.host, ...(req.headers.origin ? { origin: up.origin } : {}) };
    s.write(`${req.method} ${req.url} HTTP/1.1\r\n${Object.entries(headers).map(([k, v]) => `${k}: ${v}`).join('\r\n')}\r\n\r\n`);
    s.write(head);
    socket.pipe(s).pipe(socket);
  });
  s.on('error', () => socket.destroy());
  socket.on('error', () => s.destroy());
});
await new Promise((r) => proxy.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${proxy.address().port}`;

const exe = BROWSERS.find((p) => existsSync(p));
if (!exe) throw new Error('没找到 Chrome 或 Edge');
const profile = mkdtempSync(join(tmpdir(), 'wm-roomlife-'));
const gpu = Number(A.gpu) ? ['--use-angle=d3d11', '--enable-gpu', '--ignore-gpu-blocklist'] : ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'];
const proc = spawn(exe, ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--no-first-run', '--no-default-browser-check',
  '--disable-extensions', '--mute-audio', '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
  '--disable-backgrounding-occluded-windows', ...gpu, 'about:blank'], { stdio: 'ignore' });
let port = null;
for (let i = 0; i < 200 && !port; i++) {
  await sleep(100);
  try { const f = join(profile, 'DevToolsActivePort'); if (existsSync(f)) port = readFileSync(f, 'utf8').split('\n')[0].trim() || null; } catch { /* 浏览器还在写 */ }
}
if (!port) throw new Error('浏览器没起来');
const { webSocketDebuggerUrl } = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
const ws = new WebSocket(webSocketDebuggerUrl);
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
let next = 1;
const waiting = new Map(), consoleTail = [];
ws.onmessage = (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && waiting.has(m.id)) { const { res, rej } = waiting.get(m.id); waiting.delete(m.id); if (m.error) rej(new Error(m.error.message)); else res(m.result); }
  else if (m.method === 'Runtime.consoleAPICalled') consoleTail.push(m.params.args.map((a) => a.value ?? a.description ?? '').join(' ').slice(0, 300));
  else if (m.method === 'Runtime.exceptionThrown') consoleTail.push(`异常 ${m.params.exceptionDetails?.exception?.description?.slice(0, 400)}`);
  if (consoleTail.length > 200) consoleTail.shift();
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
  try { await send('Browser.close'); } catch { /* 已经关了 */ }
  ws.close(); proc.kill(); proxy.close(); server?.kill();
  await sleep(300);
  try { rmSync(profile, { recursive: true, force: true }); } catch { /* 浏览器还占着 */ }
};

const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
const { sessionId: sid } = await send('Target.attachToTarget', { targetId, flatten: true });
await send('Page.enable', {}, sid);
await send('Runtime.enable', {}, sid);
await send('Emulation.setDeviceMetricsOverride', { width: 560, height: 760, deviceScaleFactor: 1, mobile: false }, sid);
// Math.random 定种子 (页面脚本跑之前): 同样的种子挑同样的事 (走路的快慢还是会让两次之间有出入)
await send('Page.addScriptToEvaluateOnNewDocument', { source: `(() => { let s = ${Number(A.seed) >>> 0 || 1}; Math.random = () => { s = (s + 0x6D2B79F5) | 0;
  let t = Math.imul(s ^ (s >>> 15), 1 | s); t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; })();` }, sid);
const extraQuery = new URLSearchParams(A.query);
extraQuery.delete('fast');  // --fast is authoritative; URL must contain exactly one value.
await send('Page.navigate', { url: `${base}/pet/pet.html?room=1&fast=${A.fast}&roomreset=1${A.server ? '' : '&ws=0'}${A.avatar ? `&avatar=${A.avatar}` : ''}${extraQuery.size ? `&${extraQuery}` : ''}` }, sid);
const t0 = Date.now();
while (!(await evaluate(sid, '!!(window.__pet?.state?.room && window.__pet.state.ready)').catch(() => false))) {
  if (Date.now() - t0 > 180000) { console.error(consoleTail.join('\n')); await close(); throw new Error('小屋 180 秒没载入'); }
  await sleep(500);
}
// 需求快进: Needs 按 now() 算掉了多少 (每秒一次, 每次最多算 60 秒)
await evaluate(sid, `(() => { const st = window.__pet.state, n = st.room.needs, k = ${Number(A.speed)}, w0 = Date.now() / 1000, s0 = n.now();
  st.hostFps = ${Number(A.fps)}; n.now = () => s0 + (Date.now() / 1000 - w0) * k; n.at = n.now(); return true; })()`);

// --eval 文件.js: 载入后在页面里执行一段 (查问题用), 打印结果; --minutes 0 时打印完就退出
if (A.eval) console.log(JSON.stringify(await evaluate(sid, readFileSync(A.eval, 'utf8')).catch((e) => String(e)), null, 1));
if (A.shots) mkdirSync(A.shots, { recursive: true });
const shot = async (name) => {
  if (!A.shots) return;
  const r = await send('Page.captureScreenshot', { format: 'png' }, sid);
  writeFileSync(join(A.shots, `${name}.png`), Buffer.from(r.data, 'base64'));
};
// 每秒: 在做哪件、姿势、在哪、疼不疼、防穿模这一秒推了几次
const sample = () => evaluate(sid, `(() => { const st = window.__pet.state, r = st.room, c = st.avatar.contact?.stats;
  return { act: r.life.current?.id || null, pose: r.poses.name || r.layer.mode, x: +r.layer.char.x.toFixed(2), z: +r.layer.char.z.toFixed(2),
    pain: +(r.body.pain || 0).toFixed(3), pushes: c?.pushes ?? 0, fps: st.fps, status: document.querySelector('.room-status')?.textContent || '',
    needs: r.needs.summary().values, done: r.life.done.length }; })()`);
const samples = [], firstShot = new Set(), poseShot = new Set();
let lastAct = null, actStart = 0;
const end = Date.now() + Number(A.minutes) * 60000;
while (Date.now() < end) {
  const s = await sample().catch(() => null);
  if (s) {
    samples.push({ at: +((Date.now() - t0) / 1000).toFixed(1), ...s });
    if (s.act !== lastAct) { lastAct = s.act; actStart = Date.now(); }
    // 每件事第一次做: 开始 8 秒后拍一张 (走过去、坐下、拿起来以后)
    if (s.act && !firstShot.has(s.act) && Date.now() - actStart > 8000) { firstShot.add(s.act); await shot(`${String(firstShot.size).padStart(2, '0')}-${s.act}`); }
    // 第一次摆出这件事的姿势 (捧着看、蹲下、坐着...) 再拍一张: 做的样子
    if (s.act && !poseShot.has(s.act) && !['idle', 'go', 'act'].includes(s.pose) && Date.now() - actStart > 2500) {
      poseShot.add(s.act);
      await shot(`${String(firstShot.size + 1).padStart(2, '0')}-${s.act}-${s.pose}`);
    }
  }
  await sleep(1000);
}
const data = JSON.parse(await evaluate(sid, `JSON.stringify((() => { const r = window.__pet.state.room;
  return { done: r.life.done, needs: r.needs.summary(), body: r.body.totals, bodyLog: r.body.log.slice(-30), practice: r.practice.summary(),
    afford: r.afford?.summary?.() ?? null, pushes: window.__pet.state.avatar.contact?.stats?.pushes ?? null }; })())`));
const pains = samples.map((s) => s.pain).sort((a, b) => a - b);
const out = { args: A, simHours: +(Number(A.minutes) * Number(A.speed) / 60).toFixed(1), painP95: pains[Math.floor(pains.length * 0.95)] ?? 0,
  ...data, samples, consoleTail };
if (A.out) writeFileSync(A.out, JSON.stringify(out));
console.error(`${out.done.length} 件事 (${out.done.filter((x) => x.ok).length} 件成了)${A.out ? ` -> ${A.out}` : ''}`);
await close();
process.exit(0);
