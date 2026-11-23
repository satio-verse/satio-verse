#!/usr/bin/env node
// 小屋 (桌宠的「抹茶樱花」小筑居) 逐帧诊断: 她躺床、坐床边、做某件事的时候身体晃不晃、是哪一层在晃.
// 本地起一个小代理 (/pet/* 读 desktop/web 和 desktop/third_party, 和宿主一样; 其余转给模拟服务), 用无界面 Chrome 打开小屋页面.
// 不改仓库文件: 代理把 /pet/pet.js 末尾加一行, 把模块里的 state 挂到 window.__pet; 在运动仲裁表 (web/arbiter.js) 每层 mark 之后
// 和姿势层 (room-poses.js) 之后各拍一份骨骼快照, 每帧算:
//   - 最终姿态各部位 (头颈、躯干、左右臂、左右腿) 的角速度 (度/秒), 每根骨骼超过 5 度/秒的;
//   - 每一层写完时的累计姿态帧间转了多少 (stage): 晃动在哪一层出现、到哪一层被压住;
//   - 画出来的骨盆、头、手、脚的世界速度 (cm/s), 头发 / 裙子弹簧骨相对骨盆的速度; 根 (vrm.scene) 的位置和朝向;
//   - 防穿模层这一帧推了几次、推了多远, 体感层的疼、自己穿模.
// 2026-10-27 查"睡觉、坐在床边乱晃"时写的 (见知识库「床上姿势与坐姿待机实验记录」). 页面帧率按宿主的空闲帧率 (默认 10).
//
//   node tools/room_diag.mjs --plan sleep --hold 30 --out sleep.json [--shots 目录]     睡一整觉: 上床、躺好、睡 hold 秒、叫醒、起床
//   node tools/room_diag.mjs --plan do:rest --hold 45 --out rest.json                   做某件事 (life 的 id: rest 坐在床边发呆、read…), 量 hold 秒
//   node tools/room_diag.mjs --report sleep.json[,旧.json]                              按阶段 (姿势名 / 坐在哪) 出表, 几份并排比
// 选项: --server http://127.0.0.1:端口 (不给就自己起一个模拟服务, --tts mock, 用完关掉); --fps 10 (宿主的空闲帧率);
//   --wake 0 (睡着以后不叫醒); --patch 文件.js (载入后在页面里执行, 比如改 TUNE、给 Math.random 定种子);
//   --ovr "static/locomotion.js=旧文件,pet/room-poses.js=旧文件" (A/B: 这几个文件换成别的版本; 不带开头的斜杠, Git Bash 会把它当路径改掉);
//   --probe 文件.js (躺着时每 2 秒在页面里执行一次, 结果记进 log); --gpu 0 (软件渲染).
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
const A = { server: '', plan: 'sleep', hold: 30, out: null, fps: 10, gpu: 1, query: '', shots: '', patch: null, probe: null, wake: 1, ovr: '', report: '', avatar: '' };
for (let i = 2; i < process.argv.length; i++) { const k = process.argv[i].replace(/^--/, ''); A[k] = process.argv[++i]; }
A.hold = Number(A.hold);

// -- 报告 -----------------------------------------------------------------------------------------
if (A.report) {
  const pct = (a, p) => { if (!a.length) return 0; const s = a.slice().sort((x, y) => x - y); return s[Math.min(s.length - 1, Math.floor(s.length * p))]; };
  const runs = A.report.split(',').map((f) => ({ f, d: JSON.parse(readFileSync(f, 'utf8')) }));
  const phases = [];
  for (const { d } of runs) for (const fr of d.frames) if (!phases.includes(fr.ph)) phases.push(fr.ph);
  const stat = (frames) => {
    const g = (k) => pct(frames.filter((x) => x.g).map((x) => x.g[k] || 0), 0.95);
    const w = (i) => pct(frames.filter((x) => x.wv).map((x) => x.wv[i]), 0.95);
    const s = (k) => pct(frames.filter((x) => x.spr?.[k]).map((x) => x.spr[k][1]), 0.95);
    const rt = frames.map((x) => x.root).filter(Boolean);
    const T = frames.reduce((a, x) => a + x.dt, 0);
    return { n: frames.length, s: T, head: g('head'), arm: Math.max(g('lArm'), g('rArm')), leg: Math.max(g('lLeg'), g('rLeg')),
      hipsW: w(0), headW: w(1), handW: Math.max(w(2), w(3)), hair: s('hair'), skirt: s('skirt'),
      push: frames.reduce((a, x) => a + (x.push || 0), 0) / Math.max(1, frames.length),
      yaw: rt.length ? (Math.max(...rt.map((r) => r[2])) - Math.min(...rt.map((r) => r[2]))) * 57.3 : 0 };
  };
  const cols = [['秒', 's', 0], ['头', 'head', 0], ['手臂', 'arm', 0], ['腿', 'leg', 0], ['骨盆W', 'hipsW', 1], ['头W', 'headW', 1], ['手W', 'handW', 1],
    ['头发', 'hair', 0], ['裙子', 'skirt', 0], ['推开/帧', 'push', 2], ['根转(度)', 'yaw', 1]];
  console.log(`p95: 角速度 度/秒 (头、手臂、腿), 世界速度 cm/s (W), 弹簧骨 cm/s. 每格按 ${runs.map((r) => basename(r.f)).join(' / ')} 排`);
  console.log(['阶段'.padEnd(26), ...cols.map(([n]) => n.padStart(16))].join(''));
  for (const p of phases) {
    const st = runs.map(({ d }) => { const fr = d.frames.filter((x) => x.ph === p); return fr.length ? stat(fr) : null; });
    console.log([p.slice(0, 26).padEnd(26), ...cols.map(([, k, dp]) => st.map((x) => (x ? x[k].toFixed(dp) : '-')).join(' / ').padStart(16))].join(''));
  }
  for (const { f, d } of runs) console.log(basename(f), '体感', JSON.stringify(d.body), '防穿模推开', d.contact?.pushes);
  process.exit(0);
}

// -- 模拟服务 (没给 --server 时自己起) -----------------------------------------------------------------
let server = null, serverUrl = A.server;
if (!serverUrl) {
  const port = await new Promise((r) => { const s = netServer().listen(0, '127.0.0.1', () => { const p = s.address().port; s.close(() => r(p)); }); });
  const logDir = mkdtempSync(join(tmpdir(), 'wm-roomdiag-log-'));
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
  // 宿主里页面和服务端同源: 转发时 Origin 也换成服务端的, 不然查来源的接口 (POST /api/motion/faults 这类) 回 403
  const headers = { ...req.headers, host: up.host, ...(req.headers.origin ? { origin: up.origin } : {}) };
  const p = httpRequest({ host: up.hostname, port: up.port, path: req.url, method: req.method, headers }, (r) => {
    res.writeHead(r.statusCode, r.headers);
    r.pipe(res);
  });
  p.on('error', () => { res.writeHead(502); res.end(); });
  req.pipe(p);
});
await new Promise((r) => proxy.listen(0, '127.0.0.1', r));
const base = `http://127.0.0.1:${proxy.address().port}`;

// -- 浏览器 ---------------------------------------------------------------------------------------
const exe = BROWSERS.find((p) => existsSync(p));
if (!exe) throw new Error('没找到 Chrome 或 Edge');
const profile = mkdtempSync(join(tmpdir(), 'wm-roomdiag-'));
const gpu = Number(A.gpu) ? ['--use-angle=d3d11', '--enable-gpu', '--ignore-gpu-blocklist'] : ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'];
const proc = spawn(exe, ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${profile}`, '--no-first-run', '--no-default-browser-check',
  '--disable-extensions', '--mute-audio', '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
  '--disable-backgrounding-occluded-windows', ...gpu, 'about:blank'], { stdio: 'ignore' });
let port = null;
for (let i = 0; i < 200 && !port; i++) {
  await sleep(100);
  try { const f = join(profile, 'DevToolsActivePort'); if (existsSync(f)) port = readFileSync(f, 'utf8').split('\n')[0].trim() || null; } catch { /* 浏览器还在写, 下一轮再读 */ }
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
  if (consoleTail.length > 60) consoleTail.shift();
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
  try { rmSync(profile, { recursive: true, force: true }); } catch { /* 浏览器还占着, 留给系统清 */ }
};

const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
const { sessionId: sid } = await send('Target.attachToTarget', { targetId, flatten: true });
await send('Page.enable', {}, sid);
await send('Runtime.enable', {}, sid);
await send('Emulation.setDeviceMetricsOverride', { width: 560, height: 760, deviceScaleFactor: 1, mobile: false }, sid);
// 不连 WebSocket (ws=0): 不和服务端对话; roomreset: 心情、摆法、练习从头来
await send('Page.navigate', { url: `${base}/pet/pet.html?room=1&ws=0&roomreset=1${A.avatar ? `&avatar=${A.avatar}` : ''}${A.query ? `&${A.query}` : ''}` }, sid);
const t0 = Date.now();
while (!(await evaluate(sid, '!!(window.__pet?.state?.room && window.__pet.state.ready)').catch(() => false))) {
  if (Date.now() - t0 > 180000) { console.error(consoleTail.join('\n')); await close(); throw new Error('小屋 180 秒没载入'); }
  await sleep(500);
}
if (A.patch) await evaluate(sid, readFileSync(A.patch, 'utf8'));

// -- 页面里的量法 ---------------------------------------------------------------------------------
await evaluate(sid, String.raw`(() => {
  const st = window.__pet.state, v = st.avatar, room = st.room, arb = v.arbiter, h = v.vrm.humanoid;
  st.hostFps = ${Number(A.fps)};
  const names = Object.keys(h.humanBones).filter((n) => !/Thumb|Index|Middle|Ring|Little|Eye|Jaw/.test(n) && h.getNormalizedBoneNode(n));
  const nodes = names.map((n) => h.getNormalizedBoneNode(n)), N = names.length, hipsN = h.getNormalizedBoneNode('hips');
  const GROUP = { head: ['neck', 'head'], torso: ['hips', 'spine', 'chest', 'upperChest'],
    lArm: ['leftShoulder', 'leftUpperArm', 'leftLowerArm', 'leftHand'], rArm: ['rightShoulder', 'rightUpperArm', 'rightLowerArm', 'rightHand'],
    lLeg: ['leftUpperLeg', 'leftLowerLeg', 'leftFoot', 'leftToes'], rLeg: ['rightUpperLeg', 'rightLowerLeg', 'rightFoot', 'rightToes'] };
  const gOf = names.map((n) => Object.keys(GROUP).find((g) => GROUP[g].includes(n)) || null);
  const snap = () => {
    const a = new Float64Array(N * 4 + 3);
    for (let i = 0; i < N; i++) { const q = nodes[i].quaternion; a[i * 4] = q.x; a[i * 4 + 1] = q.y; a[i * 4 + 2] = q.z; a[i * 4 + 3] = q.w; }
    const p = hipsN.position; a[N * 4] = p.x; a[N * 4 + 1] = p.y; a[N * 4 + 2] = p.z;
    return a;
  };
  const ang = (a, i, b, j) => 2 * Math.acos(Math.min(1, Math.abs(a[i] * b[j] + a[i + 1] * b[j + 1] + a[i + 2] * b[j + 2] + a[i + 3] * b[j + 3]))) * 57.29578;
  const D = window.__diag = { frames: [] };
  const joints = [...(v.vrm.springBoneManager?.joints || [])].map((j) => j.bone).filter(Boolean);
  const kind = (b) => { for (let o = b, i = 0; i < 8 && o; i++, o = o.parent) { if (/skirt|スカート/i.test(o.name)) return 'skirt'; if (/hair|髪/i.test(o.name)) return 'hair'; } return 'other'; };
  const jk = joints.map(kind);
  const W = (o) => [o.matrixWorld.elements[12], o.matrixWorld.elements[13], o.matrixWorld.elements[14]];
  const WB = ['hips', 'head', 'leftHand', 'rightHand', 'leftFoot', 'rightFoot'].map((n) => h.getNormalizedBoneNode(n));
  const ob = arb.begin.bind(arb), om = arb.mark.bind(arb), oe = arb.end.bind(arb), pu = room.poses.update.bind(room.poses);
  let seq = [], drawn = null, lastDrawn = null, prevFinal = null, prevStage = {}, push0 = v.contact?.stats?.pushes ?? 0;
  const byPrev = { ...(v.contact?.stats?.by || {}) };
  arb.begin = () => {  // 上一帧画出来的样子 (渲染时更新过 matrixWorld)
    ob();
    const hp = W(hipsN);
    drawn = { w: WB.map(W), s: joints.map((j) => { const p = W(j); return [p[0] - hp[0], p[1] - hp[1], p[2] - hp[2]]; }) };
    seq = [['start', snap()]];
  };
  arb.mark = (layer, o) => { om(layer, o); seq.push([layer, snap()]); };
  room.poses.update = (dt) => { pu(dt); seq.push(['room', snap()]); };
  arb.end = (t, src, mix) => {
    oe(t, src, mix);
    if (seq.length < 2) return;
    const dt = v.loco?._dt || 0.1, fin = seq[seq.length - 1][1], L = room.layer, P = room.poses;
    const ph = (P.name || (L.seated ? 'seat:' + (L.seated.seat?.id || '?') + ':' + L.mode : L.mode)) + (room.life.sleeping ? '(睡)' : '');
    const f = { t: +t.toFixed(3), dt: +dt.toFixed(4), ph, act: room.life.current?.id || '-', src, W: +P.W.toFixed(3), g: {}, stage: {} };
    if (prevFinal) {
      f.bone = {};
      for (let i = 0; i < N; i++) {
        const a = ang(fin, i * 4, prevFinal, i * 4) / dt, g = gOf[i];
        if (g && a > (f.g[g] || 0)) f.g[g] = +a.toFixed(1);
        if (a > 5) f.bone[names[i]] = +a.toFixed(1);
      }
    }
    const occ = {}, now = {};
    for (let s = 1; s < seq.length; s++) {
      const [lab, A2] = seq[s];
      occ[lab] = (occ[lab] || 0) + 1;
      const key = occ[lab] > 1 ? lab + '#' + occ[lab] : lab, pa = prevStage[key];
      now[key] = A2;
      if (!pa) continue;
      const gg = {};
      for (let i = 0; i < N; i++) { const g = gOf[i]; if (g) gg[g] = Math.max(gg[g] || 0, +(ang(A2, i * 4, pa, i * 4) / dt).toFixed(1)); }
      gg.hip = +(Math.hypot(A2[N * 4] - pa[N * 4], A2[N * 4 + 1] - pa[N * 4 + 1], A2[N * 4 + 2] - pa[N * 4 + 2]) * 100 / dt).toFixed(2);
      f.stage[key] = gg;
    }
    prevStage = now;
    prevFinal = fin.slice();
    if (drawn && lastDrawn) {
      const sp = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]) * 100 / dt;
      f.wv = drawn.w.map((p, i) => +sp(p, lastDrawn.w[i]).toFixed(1));
      const by = { hair: [], skirt: [], other: [] };
      drawn.s.forEach((p, i) => by[jk[i]].push(sp(p, lastDrawn.s[i])));
      f.spr = {};
      for (const k in by) if (by[k].length) { const a = by[k].sort((x, y) => x - y); f.spr[k] = [+(a.reduce((x, y) => x + y, 0) / a.length).toFixed(1), +a[Math.floor(a.length * 0.95)].toFixed(1)]; }
    }
    lastDrawn = drawn;
    const sc = v.vrm.scene;
    f.root = [+sc.position.x.toFixed(4), +sc.position.z.toFixed(4), +sc.rotation.y.toFixed(4)];
    const cs = v.contact?.stats;
    if (cs) {
      f.push = cs.pushes - push0;
      push0 = cs.pushes;
      f.pushM = +((cs.lastPush || 0) * 100).toFixed(2);
      const by = {};  // 这一帧哪一段身体推了手臂的哪个取样点 (cm, 防穿模层第一轮的穿透深度)
      for (const [k, x] of Object.entries(cs.by || {})) { if (x - (byPrev[k] || 0) > 1e-4) by[k] = +((x - (byPrev[k] || 0)) * 100).toFixed(1); byPrev[k] = x; }
      if (Object.keys(by).length) f.by = by;
    }
    f.pain = +(room.body.pain || 0).toFixed(3);
    if (room.body.self) f.self = room.body.self.pair + ':' + room.body.self.cm;
    f.seatAmt = +(L.seatAmount || 0).toFixed(2);
    D.frames.push(f);
    if (D.frames.length > 40000) D.frames.shift();
  };
  return true;
})()`);

const status = () => evaluate(sid, `(() => { const r = window.__pet.state.room; return { act: r.life.current?.id || null, pose: r.poses.name, mode: r.layer.mode,
  seated: r.layer.seated?.seat?.id || null, sleeping: r.life.sleeping, status: document.querySelector('.room-status')?.textContent || '' }; })()`);
const shot = async (name) => {
  if (!A.shots) return;
  const r = await send('Page.captureScreenshot', { format: 'png' }, sid);
  writeFileSync(join(A.shots, `${name}.png`), Buffer.from(r.data, 'base64'));
};
const log = [];
const tick = async (label) => { const s = await status(); log.push({ at: +((Date.now() - t0) / 1000).toFixed(1), label, ...s }); return s; };
const waitFor = async (pred, ms, label) => { for (const end = Date.now() + ms; ;) { const s = await tick(label); if (pred(s)) return s; if (Date.now() > end) return null; await sleep(1000); } };

// -- 剧本 -----------------------------------------------------------------------------------------
await sleep(3000);
await evaluate(sid, 'window.__diag.frames.length = 0, true');
if (A.plan === 'sleep') {
  await evaluate(sid, `window.__pet.state.room.life.goDo('sleep')`);
  await waitFor((s) => s.act === 'sleep', 30000, '去睡');
  await waitFor((s) => s.seated || s.pose, 60000, '走到床边');
  await shot('01-床边');
  await waitFor((s) => s.pose === 'lieRest', 60000, '上床、躺下');
  await shot('02-躺好');
  for (const t = Date.now(); Date.now() - t < A.hold * 1000;) {
    await tick('躺着');
    if (A.probe) log.push({ probe: await evaluate(sid, readFileSync(A.probe, 'utf8')).catch((e) => String(e)) });
    await sleep(2000);
  }
  await shot('03-睡着');
  if (Number(A.wake)) {
    await evaluate(sid, `window.__pet.state.room.life.hurry = true`);
    await waitFor((s) => s.act !== 'sleep', 90000, '叫醒、起床');
    await shot('04-起来');
  }
} else if (A.plan.startsWith('do:')) {
  const id = A.plan.slice(3);
  // 她自己已经在做了就不再叫 (叫一次会把正在做的那次催完)
  await evaluate(sid, `(() => { const l = window.__pet.state.room.life; return l.current?.id === ${JSON.stringify(id)} || l.goDo(${JSON.stringify(id)}); })()`);
  await waitFor((s) => s.act === id, 30000, '去做');
  await waitFor((s) => s.mode === 'seated' || s.pose, 60000, '走过去');
  await shot('01-开始');
  for (const t = Date.now(); Date.now() - t < A.hold * 1000;) { await tick('做着'); await sleep(2000); }
  await shot('02-之后');
}
const data = JSON.parse(await evaluate(sid, `JSON.stringify({ frames: window.__diag.frames, body: window.__pet.state.room.body.totals,
  bodyLog: window.__pet.state.room.body.log.slice(-20), selfStats: window.__pet.state.room.body.selfStats, contact: window.__pet.state.avatar.contact?.stats })`));
const out = { args: A, log, consoleTail, ...data };
if (A.out) writeFileSync(A.out, JSON.stringify(out));
console.error(`${out.frames.length} 帧${A.out ? ` -> ${A.out}` : ''}`);
await close();
process.exit(0);
