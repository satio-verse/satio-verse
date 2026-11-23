#!/usr/bin/env node
// 动作生成模型研究 (docs/roadmap/motion-model.md) 的基线和 A/B 量法: 运动系统的内存、每帧耗时、响应延迟、
// 走到目标的误差、脚滑, 外加执行评估的痛觉和像真人分. 2026-10-27 R0 写, R1 起换动作来源后用同一个脚本比.
// 启动方式照抄 tools/motion_eval.mjs (无界面 Chrome, 临时用户目录, 不渲染). 只在页面里包计时、加临时场景, 不改任何文件.
// 页面要连一个模拟服务:
//   python -m wm --llm mock --asr none --tts mock --rvc mock --port 8821 --set memory.vault="" --set trace.log_dir=<临时目录>
//   node tools/motion_baseline.mjs --url http://localhost:8821 [--seeds 1,2,3] [--avatar silvercat.vrm] [--scene room]
//                                  [--out 结果.json] [--pin 上次的结果.json] [--scenarios lat_wave,walk] [--trace 1]
//       --scenarios 只跑这几个 (默认全套; 另有 lat_wave:turned 先转到背后再挥); --trace 1 把挥手、比心、风格步态的逐帧特征写进结果
//       --motion base (默认, 原来的库) / lmm (中性风格库和待机库都换成网络, web/lmm.js) / lmm-neutral / lmm-idle (只换一个)
//         / match (中性库换成第三版的压缩匹配库 lmmmatch, 待机换网络) / match-neutral (只换中性库)
//         --handoff 米: 离路径终点不到这么远时临时换回原来的中性库 (看坐下、拿放是不是坏在交接处; 原库留在内存里)
//         --lmm-bias 数: 投影结果的代价加多少再报给行动层 (默认 0; 加了少换段)
//         --lmm-cgate 1: 换段要求候选和当前帧的触地一致 (web/lmm.js 的 contactGate)
//         --lmm-idleslot 0: 不留待机槽 (2026-10-28 以前的做法, 复现旧结果用; 默认 1)
//         --lmm-slot 帧数: 中性库每个槽多长 (默认 256; 播到槽尾强制重新投影, 短一点步进器漂得少)
//         换之前先用原来的库把像真人分的参照集算好, 两边的分数用同一个参照; 另外记每帧花在动作来源上的时间 (source)
//
// 数字的含义 (别当成别的东西):
//   - 内存: 动作库 (运动匹配各风格库、待机、动作、手势) 载入后的 ArrayBuffer 字节数, 是"选中的动作数据"的估计, 不是整个页面.
//   - 耗时: 无界面、不渲染时每帧的同步更新 (state.step 一帧), 不能拿来推真实页面或桌宠的显示帧率. 用页面原来的
//     performance.now (评估期间被换成模拟时钟); 没有跨源隔离时浏览器把它取整到 0.1 ms.
//   - 延迟: 按模拟时钟 (60 fps, 一帧 16.7 ms), 所有阈值都从发令那一帧算起; 只是运动层自己的, 不含意图模型和网络.
//     每项先录 1-1.5 秒不发指令的对照, 阈值取 max(固定值, 2 x 对照里的最大值); 挥手、比心另外要看到动作本身的特征
//     (手举过肩并左右摆、两只手腕在胸前合拢). 超时或对照段就已经满足, 场景记失败 (ok: false, reason).
//   - 每个场景开始前 runEval 自己会复位; 它没管的行动层状态 (表情动作、交叠、仪态、情绪体态、风格层权重) 这里另外复位.
//   - 像真人分是和动捕参照集的距离, 只说明"离参照多远", 不说明原因. 参照集只用中性风格库、待机、动作、手势.
//   - 版本: 页面用到的代码、动作库、形象、场景、本脚本都记 SHA-256, 跑前跑后各算一次 (中途有文件变了会标出来);
//     --pin 给上一次的结果, 列出和它不一样的文件 (版本不同的两次结果不能直接比).
//     别的会话正在改 web/ 时, 从快照跑: 把 wm/ web/ config/ tools/ 拷到一个目录, models/ 用目录联接过去, 在那个目录里起服务、
//     跑那里的这个脚本 (哈希按脚本所在的根目录算, 和服务给页面的文件是同一份).
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { existsSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { cpus, tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const BROWSERS = ['C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', 'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser'];
const a = { url: 'http://localhost:8821', seeds: '1,2,3', avatar: null, scene: null, out: null, pin: null, browser: null, scenarios: null, trace: null,
  motion: 'base', 'lmm-slot': '256', 'lmm-bias': '0', 'lmm-cgate': '0', 'lmm-idleslot': '1', label: null, handoff: '0' };
for (let i = 2; i < process.argv.length; i++) a[process.argv[i].replace(/^--/, '')] = process.argv[++i];
if (!['base', 'lmm', 'lmm-neutral', 'lmm-idle', 'match', 'match-neutral'].includes(a.motion)) {
  process.stderr.write('--motion 只认 base / lmm / lmm-neutral / lmm-idle / match / match-neutral\n');
  process.exit(2);
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
if (a.pin && !existsSync(a.pin)) { process.stderr.write(`--pin 的文件不存在: ${a.pin}\n`); process.exit(2); }

// -- 版本 -------------------------------------------------------------------------------------
function avatarFile() {
  if (a.avatar) return a.avatar;
  for (const f of ['config/local.json', 'config/default.json']) {
    try { const v = JSON.parse(readFileSync(join(ROOT, f), 'utf8')).avatar?.default; if (v) return v; } catch { /* 下一个 */ }
  }
  return null;
}
function versionFiles() {
  const list = (dir, re) => (existsSync(join(ROOT, dir)) ? readdirSync(join(ROOT, dir)).filter((n) => re.test(n)).sort().map((n) => `${dir}/${n}`) : []);
  const av = avatarFile();
  return [...list('web', /\.js$/), ...list('web/physics', /\.js$/), `web/scenes/${a.scene || 'room'}.json`,
    ...list('models/motion', /\.(json|bin)$/), ...(av ? [`models/vrm/${av}`] : []), 'tools/motion_baseline.mjs']
    .filter((f) => existsSync(join(ROOT, f)));
}
function hashFiles() {
  const files = {};
  for (const f of versionFiles()) files[f] = createHash('sha256').update(readFileSync(join(ROOT, f))).digest('hex').slice(0, 16);
  const group = (re) => createHash('sha256').update(Object.entries(files).filter(([f]) => re.test(f)).map(([f, h]) => `${f}:${h}`).join('\n'))
    .digest('hex').slice(0, 16);
  // 像真人分的参照集由 motion-eval.js 从中性库、待机、动作、手势库取样, 算法在 motion-score.js
  return { files, code: group(/^web\/.*\.js$/), data: group(/^models\/motion\//),
    ref: group(/^(models\/motion\/(neutral|idle|actions|gestures)\.|web\/motion-(eval|score)\.js$)/) };
}

// -- 浏览器 (同 motion_eval.mjs) ----------------------------------------------------------------
async function launch() {
  const exe = a.browser || BROWSERS.find((p) => existsSync(p));
  if (!exe) throw new Error('没找到 Chrome 或 Edge, 用 --browser 指定路径');
  const dir = mkdtempSync(join(tmpdir(), 'wm-base-'));
  const proc = spawn(exe, ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${dir}`, '--no-first-run',
    '--no-default-browser-check', '--disable-extensions', '--mute-audio', '--disable-background-timer-throttling',
    '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows', '--use-angle=swiftshader',
    '--enable-unsafe-swiftshader', '--enable-precise-memory-info', 'about:blank'], { stdio: 'ignore' });
  let port = null;
  for (let i = 0; i < 200 && !port; i++) {
    await sleep(100);
    const f = join(dir, 'DevToolsActivePort');
    try { if (existsSync(f)) port = readFileSync(f, 'utf8').split('\n')[0].trim() || null; } catch { /* 下一轮再读 */ }
  }
  if (!port) { proc.kill(); throw new Error('浏览器没起来 (没有 DevToolsActivePort)'); }
  const info = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
  const ws = new WebSocket(info.webSocketDebuggerUrl);
  await new Promise((res, rej) => { ws.onopen = res; ws.onerror = rej; });
  let next = 1;
  const waiting = new Map();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.id && waiting.has(msg.id)) {
      const { res, rej } = waiting.get(msg.id);
      waiting.delete(msg.id);
      if (msg.error) rej(new Error(`${msg.error.message} ${msg.error.data || ''}`)); else res(msg.result);
    }
  };
  const send = (method, params = {}, sessionId) => new Promise((res, rej) => {
    const id = next++;
    waiting.set(id, { res, rej });
    ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
  });
  const close = async () => {
    try { await send('Browser.close'); } catch { /* 已经关了 */ }
    ws.close(); proc.kill(); await sleep(300);
    try { rmSync(dir, { recursive: true, force: true }); } catch { /* 浏览器还占着文件 */ }
  };
  return { send, close, browser: info.Browser };
}

async function evaluate(b, s, expression) {
  const r = await b.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, s);
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
}

// -- 页面里跑的测量 ------------------------------------------------------------------------------
const MEASURE = (seeds, only, trace, motion, slotLen, costBias, handoff, cgate, idleSlot) => `(async () => {
  const ONLY = ${JSON.stringify(only)}, TRACE = ${JSON.stringify(!!trace)}, MOTION = ${JSON.stringify(motion)}, HANDOFF = ${handoff};
  const pn = performance.now.bind(performance);
  const st = window.__wm, vrm = st.vrm, loco = vrm.loco, me = window.__me;
  const { MOOD } = await import('/static/locomotion.js');
  const FPS = 60;
  // -- 动作来源换成网络 (web/lmm.js): 像真人分的参照集先用原来的库算好 (motion-eval 里缓存着), 再换 --
  let lmmInfo = null;
  const ORIG_NEUTRAL = loco.dbs.neutral;
  if (MOTION !== 'base') {
    await me.runEval({ scenarios: [], seeds: [] });
    const { installLMM } = await import('/static/lmm.js');
    const h0 = performance.memory?.usedJSHeapSize ?? null;
    lmmInfo = await installLMM(loco, { neutral: MOTION === 'lmm-idle' ? false : MOTION.startsWith('match') ? 'match' : true,
      idle: !MOTION.endsWith('-neutral'), slotLen: ${slotLen}, costBias: ${costBias}, contactGate: ${!!cgate}, idleSlot: ${!!idleSlot} });
    lmmInfo.heapBefore = h0;
    lmmInfo.heapAfter = performance.memory?.usedJSHeapSize ?? null;
  }
  // --handoff 米: 离路径终点不到这么远时临时换回原来的中性库 (原库留在内存里), 走远了再换回网络. 看坐下、拿放是不是主要坏在交接处.
  // 只在走路 (go) 时判断: 到了以后原地转身对准 (face)、转身 (turn) 也走运动匹配, 要接着用原库, 下一次走远了才换回网络
  // (2026-10-27 第一次跑 face 时没有路径, 被换回了网络)
  if (HANDOFF > 0 && loco.dbs.neutral !== ORIG_NEUTRAL) {
    const net = loco.dbs.neutral, upd = loco.update;
    loco.stats.handoffs = 0;
    loco.update = function (dt) {
      if (loco.style === 'neutral' && loco.mode === 'go' && loco.path?.length) {
        const g = loco.path[loco.path.length - 1];
        const want = Math.hypot(g[0] - loco.char.x, g[1] - loco.char.z) < HANDOFF ? ORIG_NEUTRAL : net;
        if (loco.db !== want && loco._pendingDB !== want) { loco._pendingDB = want; loco.stats.handoffs++; }
      }
      return upd.call(this, dt);
    };
  }
  // -- 内存: 每个库用到的 ArrayBuffer (去重) 和另外分配的数组; 网络自己报 (文件 + 窗口) --
  const dbBytes = (db) => {
    if (!db) return 0;
    if (typeof db.bytes === 'function') return db.bytes().total;
    const bufs = new Set(); let extra = 0;
    for (const v of Object.values(db)) if (ArrayBuffer.isView(v)) { if (v.buffer.byteLength > v.byteLength * 1.5 || v.byteOffset) bufs.add(v.buffer); else extra += v.byteLength; }
    let n = extra; for (const b of bufs) n += b.byteLength;
    if (db.blocks) n += db.blocks.length * (27 * 2 * 4 + 64);
    return n;
  };
  const heap = () => (performance.memory ? { used: performance.memory.usedJSHeapSize, total: performance.memory.totalJSHeapSize } : null);
  const mem = { atLoad: { styles: Object.fromEntries(Object.entries(loco.dbs).map(([k, d]) => [k, dbBytes(d)])),
    idle: dbBytes(loco.idleDB), actions: dbBytes(loco.acts), gestures: dbBytes(loco.gestDB) },
    frames: { neutral: loco.dbs.neutral?.F, idle: loco.idleDB?.F, actions: loco.acts?.F, gestures: loco.gestDB?.F }, heapAtLoad: heap(),
    motion: MOTION, lmm: lmmInfo };
  // 其余风格库先载入 (风格步态场景要用); 启动时的内存已经量完
  const STYLE_EMO = { elated: 'happy', depressed: 'sad', angry: 'angry', shy: 'shy', calm: 'calm' };
  for (const e of Object.values(STYLE_EMO)) await loco.setStyle(e);
  await loco.setStyle('neutral');
  // setStyle 只记"下一次搜索换哪个库": 连着换几次再换回中性时, 中性库本来就在用, 挂着的会是最后一个 (平静) 库
  loco._pendingDB = loco.db === loco.dbs.neutral ? null : loco.dbs.neutral;
  mem.allStyles = Object.fromEntries(Object.entries(loco.dbs).map(([k, d]) => [k, dbBytes(d)]));
  mem.heapAllStyles = heap();
  // -- runEval 的复位没管的行动层状态: 页面刚载入时的样子存一份, 每次复位 (arbiter.clear 只在复位里调) 时放回去 --
  const KEYS = ['emoteSt', 'emoteNext', 'claspW', 'claspPlan', 'poiseW', 'cock', '_calmW', '_calmK', '_walkW'];
  const snap = JSON.parse(JSON.stringify(Object.fromEntries(KEYS.map((k) => [k, loco[k] ?? null]))));
  const arbClear = vrm.arbiter.clear;
  vrm.arbiter.clear = function (...args) {
    for (const k of KEYS) loco[k] = JSON.parse(JSON.stringify(snap[k]));
    loco.mood = { name: 'neutral', at: -1e9, k: { ...MOOD.neutral } };
    // 中性库放回去: 上一个场景停在别的库上 (交接换回的原库、风格库) 的, 直接换回 (待机时不用这个库); 网络的槽轮换也放回去.
    // 2026-10-28 以前是挂成"下一次搜索换", 第一次搜索时从哪个库过渡过来跟前面跑过什么有关
    if (loco.style === 'neutral') { loco.db = loco.dbs.neutral; loco._pendingDB = null; }
    if (loco.db?.reset) loco.frame = loco.db.reset();
    return arbClear.apply(this, args);
  };
  // -- 计时包装 (真时钟) --
  const T = {}; let cur = 'warmup';
  const restore = [() => { vrm.arbiter.clear = arbClear; }];
  const wrap = (obj, name, key) => {
    if (!obj || typeof obj[name] !== 'function') return;
    const own = Object.prototype.hasOwnProperty.call(obj, name), orig = obj[name];
    obj[name] = function (...args) { const t0 = pn(); try { return orig.apply(this, args); } finally { ((T[cur] ??= {})[key] ??= []).push(pn() - t0); } };
    restore.push(() => { if (own) obj[name] = orig; else delete obj[name]; });
  };
  wrap(st, 'step', 'tick');             // 一帧的同步更新 (面部引擎、表演、身体全部层; 不含渲染)
  wrap(vrm, 'apply', 'body');           // 身体全部层
  wrap(loco, 'update', 'loco');         // 行动层 (动作来源 + 行动层 IK)
  wrap(loco, '_match', 'match');        // 运动匹配: 建查询 + 搜索 + 切换
  wrap(loco, '_idleStep', 'idleStep');  // 待机库 / 手势库播放
  wrap(vrm.contact, 'update', 'contact');
  wrap(vrm.filter, 'apply', 'filter');
  wrap(vrm.kin, 'update', 'kin');       // 痛觉和运动学检查
  // 每帧花在动作来源上的时间 (source): 运动匹配 / 投影、待机和手势播放、网络取帧, 只算最外层那一次 (嵌套的不重复算).
  // 原来的库取帧 (mm.js 的 samplePose) 包不到, 它只是插值, 约 0.01 ms
  let srcAcc = 0, srcDepth = 0;
  const srcWrap = (obj, name) => {
    if (!obj || typeof obj[name] !== 'function') return;
    const own = Object.prototype.hasOwnProperty.call(obj, name), orig = obj[name];
    obj[name] = function (...args) {
      if (srcDepth++) { try { return orig.apply(this, args); } finally { srcDepth--; } }
      const t0 = pn();
      try { return orig.apply(this, args); } finally { srcDepth--; srcAcc += pn() - t0; }
    };
    restore.push(() => { if (own) obj[name] = orig; else delete obj[name]; });
  };
  srcWrap(loco, '_match');
  srcWrap(loco, '_idleStep');
  for (const d of new Set([loco.dbs.neutral, loco.idleDB])) if (d?.sample) srcWrap(d, 'sample');
  const stepOrig = st.step;
  // 手离髋部中线多远 (armOut, 厘米, 每 6 帧量一次, 左右手平均): 看手臂约束是不是靠"一律张开"过关的
  const hum = vrm.vrm.humanoid, wpos = (n) => { const b = hum.getNormalizedBoneNode(n); return b.getWorldPosition(b.position.clone()); };
  let tickN = 0;
  st.step = function (...args) {
    srcAcc = 0;
    const r = stepOrig.apply(this, args);
    ((T[cur] ??= {}).source ??= []).push(srcAcc);
    if (++tickN % 6 === 0) {
      const hp = wpos('hips'), lx = Math.cos(loco.char.yaw), lz = -Math.sin(loco.char.yaw);
      const lat = (p) => Math.abs((p.x - hp.x) * lx + (p.z - hp.z) * lz) * 100;
      (T[cur].armOut ??= []).push((lat(wpos('leftHand')) + lat(wpos('rightHand'))) / 2);
    }
    return r;
  };
  restore.push(() => { st.step = stepOrig; });
  // 每个场景里运动匹配搜了几次、换了几次段; 网络版另记投影、步进、解压的次数
  const counts = () => ({ ...loco.stats, ...Object.fromEntries(Object.entries(loco.dbs.neutral?.stats || {}).map(([k, v]) => ['lmm_' + k, v])),
    ...Object.fromEntries(Object.entries(loco.idleDB?.stats || {}).map(([k, v]) => ['idle_' + k, v])) });
  const SC = {};
  let c0 = null;
  const kb = vrm.kin.begin, ke = vrm.kin.end;
  vrm.kin.begin = function (name) { cur = name; c0 = counts(); return kb.call(this, name); };
  vrm.kin.end = function (...args) {
    const c1 = counts();
    SC[cur] = Object.fromEntries(Object.entries(c1).filter(([k, v]) => typeof v === 'number' && k !== 'searchMs' && v !== (c0?.[k] ?? 0))
      .map(([k, v]) => [k, +(v - (c0?.[k] ?? 0)).toFixed(3)]));
    return ke.apply(this, args);
  };
  restore.push(() => { delete vrm.kin.begin; delete vrm.kin.end; });

  // -- 场景用的小工具 --
  const ms = (f) => (f === null || f === undefined ? null : Math.round(f * 1000 / FPS));
  const ang = (x) => Math.atan2(Math.sin(x), Math.cos(x));
  const deg = (x) => +(x * 180 / Math.PI).toFixed(1);
  const boneW = (ctx) => { const h = ctx.vrm.vrm.humanoid; return (n) => { const b = h.getNormalizedBoneNode(n); return b.getWorldPosition(b.position.clone()); }; };
  const axes = (L) => { const f = [Math.sin(L.char.yaw), Math.cos(L.char.yaw)]; return { f, l: [f[1], -f[0]] }; };
  const targetsOf = (L) => Object.entries(L.targets).filter(([id, t]) => t.approach && id !== 'user' && !t.virtual).map(([id]) => id);
  const farthest = (L, ids = targetsOf(L)) => ids.map((id) => [id, Math.hypot(L.targets[id].approach[0] - L.char.x, L.targets[id].approach[1] - L.char.z)])
    .sort((x, y) => y[1] - x[1])[0][0];
  const go = (L, to, done = () => {}) => L.run({ do: 'go', to, side: null, amp: 1, spd: 1, at: 0 }, done);
  // 根 (loco.char) 的速度, 每帧调一次
  const rootSpeed = (L) => { let p = [L.char.x, L.char.z]; return () => { const v = Math.hypot(L.char.x - p[0], L.char.z - p[1]) * FPS; p = [L.char.x, L.char.z]; return v; }; };
  /** 发令后逐帧推进 (第 1 帧起), 每帧先 sample() 再查各条件, 记第一次成立的帧号 (从发令那一帧算). 全部成立或到 max 帧就停. */
  const watch = (ctx, sample, conds, max) => {
    const hit = Object.fromEntries(Object.keys(conds).map((k) => [k, null]));
    for (let i = 1; i <= max; i++) {
      ctx.step(1);
      const s = sample(i);
      for (const [k, c] of Object.entries(conds)) if (hit[k] === null && c(s, i)) hit[k] = i;
      if (Object.values(hit).every((v) => v !== null)) break;
    }
    return hit;
  };
  /** 不发指令推进 n 帧, 每帧 sample(), 返回所有取样. */
  const control = (ctx, sample, n) => { const out = []; for (let i = 0; i < n; i++) { ctx.step(1); out.push(sample(i)); } return out; };
  /** 走稳: 根速度连续 15 帧 (0.25 秒) 在 0.4 m/s 以上, 而且这 15 帧里走的方向变化不到 5 度. 返回从现在起第几帧走稳 (3 秒内没走稳是 null).
   *  起步方式不同的动作来源 (先转向再走 / 边走边转) 都要等走稳了再录对照、发指令, 不然对照段的方向抖动会大到 160 度以上. */
  const steady = (ctx, L) => {
    const pos = [];
    for (let i = 1; i <= FPS * 3; i++) {
      ctx.step(1);
      pos.push([L.char.x, L.char.z]);
      if (pos.length < 17) continue;
      if (pos.length > 17) pos.shift();
      let ok = true, h0 = null;
      for (let k = 1; k < 17 && ok; k++) {
        const dx = pos[k][0] - pos[k - 1][0], dz = pos[k][1] - pos[k - 1][1];
        if (Math.hypot(dx, dz) * FPS < 0.4) ok = false;
        else { const h = Math.atan2(dx, dz); if (h0 === null) h0 = h; else if (Math.abs(ang(h - h0)) > 5 * Math.PI / 180) ok = false; }
      }
      if (ok) return i;
    }
    return null;
  };
  const missing = (out, names, secs) => { const m = names.filter((k) => out[k + 'Ms'] === null); return m.length ? '超时 (' + secs + ' 秒): ' + m.join(', ') : null; };
  const putMs = (out, hit) => { for (const k in hit) out[k + 'Ms'] = ms(hit[k]); };
  const S = me.SCENARIOS;

  // 起步: 走到离出生点最远的目标
  S.lat_go = (ctx) => {
    const L = ctx.loco, W = boneW(ctx), out = { to: farthest(L) };
    const r0 = [L.char.x, L.char.z], h0 = W('hips');
    const c = control(ctx, () => { const hp = W('hips'); return { root: Math.hypot(L.char.x - r0[0], L.char.z - r0[1]), hips: Math.hypot(hp.x - h0.x, hp.z - h0.z) }; }, FPS);
    out.ctrl = { rootM: +Math.max(...c.map((s) => s.root)).toFixed(4), hipsM: +Math.max(...c.map((s) => s.hips)).toFixed(4) };
    const thrRoot = Math.max(0.02, 2 * out.ctrl.rootM), thrHips = Math.max(0.02, 2 * out.ctrl.hipsM);
    const r1 = [L.char.x, L.char.z], h1 = W('hips'), v = rootSpeed(L);
    go(L, out.to);
    const hit = watch(ctx, () => { const hp = W('hips'); return { v: v(), root: Math.hypot(L.char.x - r1[0], L.char.z - r1[1]), hips: Math.hypot(hp.x - h1.x, hp.z - h1.z) }; },
      { rootMove: (s) => s.root > thrRoot, hipsMove: (s) => s.hips > thrHips, speed04: (s) => s.v >= 0.4 }, FPS * 4);
    putMs(out, hit);
    ctx.extra.latency = out;
    return missing(out, ['rootMove', 'hipsMove', 'speed04'], 4);
  };
  // 中途换目标: 走稳以后改去方向差得最多的目标
  S.lat_redirect = (ctx) => {
    const L = ctx.loco, out = {}, far = farthest(L);
    go(L, far);
    out.steadyMs = ms(steady(ctx, L));
    if (out.steadyMs === null) { ctx.extra.latency = out; return '超时 (3 秒): 没走稳'; }
    const hist = [];
    const head = () => { hist.push([L.char.x, L.char.z]); if (hist.length > 4) hist.shift(); const p = hist[0], q = hist[hist.length - 1]; return Math.hypot(q[0] - p[0], q[1] - p[1]) > 0.005 ? Math.atan2(q[0] - p[0], q[1] - p[1]) : null; };
    const c = control(ctx, () => head(), 30).filter((x) => x !== null);
    const dir0 = c[c.length - 1];
    out.ctrlHeadingDevDeg = deg(Math.max(...c.map((x) => Math.abs(ang(x - dir0)))));
    const thr = Math.max(20, 2 * out.ctrlHeadingDevDeg) * Math.PI / 180;
    const cand = targetsOf(L).filter((id) => id !== far).map((id) => [id, Math.abs(ang(Math.atan2(L.targets[id].approach[0] - L.char.x, L.targets[id].approach[1] - L.char.z) - dir0))])
      .sort((x, y) => y[1] - x[1])[0];
    out.to = cand[0]; out.angleDeg = deg(cand[1]);
    let arrived = false;
    go(L, out.to, (r) => { arrived = !r; });
    const hit = watch(ctx, () => head(), { turned: (h) => h !== null && Math.abs(ang(h - dir0)) > thr, arrived: () => arrived }, FPS * 20);
    putMs(out, hit);
    out.endDistM = +Math.hypot(L.char.x - L.targets[out.to].approach[0], L.char.z - L.targets[out.to].approach[1]).toFixed(3);
    ctx.extra.latency = out;
    return missing(out, ['turned', 'arrived'], 20);
  };
  // 停下: 走稳 1 秒后停, 速度降到 0.1 m/s 以下并保持 0.5 秒
  S.lat_stop = (ctx) => {
    const L = ctx.loco, out = {};
    go(L, farthest(L));
    out.steadyMs = ms(steady(ctx, L));
    if (out.steadyMs === null) { ctx.extra.latency = out; return '超时 (3 秒): 没走稳'; }
    const v = rootSpeed(L);
    const c = control(ctx, () => v(), FPS);
    out.ctrlSpeed = { min: +Math.min(...c).toFixed(3), max: +Math.max(...c).toFixed(3) };
    L.stop();
    let cand = null, done = null;
    for (let i = 1; i <= FPS * 5 && done === null; i++) {
      ctx.step(1);
      if (v() < 0.1) { if (cand === null) cand = i; if (i - cand >= 30) done = cand; } else cand = null;
    }
    out.stopMs = ms(done);
    ctx.extra.latency = out;
    return missing(out, ['stop'], 5);
  };
  // 转身到背后
  S.lat_turn = (ctx) => {
    const L = ctx.loco, out = {}, y0 = L.char.yaw;
    const c = control(ctx, () => Math.abs(ang(L.char.yaw - y0)), FPS);
    out.ctrlYawDeg = deg(Math.max(...c));
    const thr = Math.max(3, 2 * out.ctrlYawDeg) * Math.PI / 180, y1 = L.char.yaw;
    let done = false;
    L.run({ do: 'turn', side: 'B', to: null, amp: 1, spd: 1, at: 0 }, (r) => { done = !r; });
    const hit = watch(ctx, () => Math.abs(ang(L.char.yaw - y1)), { start: (d) => d > thr, half: (d) => d > Math.PI / 2, done: () => done }, FPS * 6);
    putMs(out, hit);
    out.endYawDeg = deg(Math.abs(ang(L.char.yaw - y1)));
    ctx.extra.latency = out;
    return missing(out, ['start', 'half', 'done'], 6);
  };
  // 挥右手 (动捕手势库). 特征: 手腕抬起超出对照的晃动、手腕举过肩、举着时左右摆 (来回 2 次, 每次 3 cm 以上)
  const waveTracker = (ctx) => {
    const L = ctx.loco, W = boneW(ctx);
    const y0 = W('rightHand').y;
    let hi = null, lo = null, dir = 0, rev = 0;
    return () => {
      const hand = W('rightHand'), sh = W('rightUpperArm'), ch = W('chest'), el = W('rightLowerArm'), { f, l } = axes(L);
      const above = hand.y - sh.y, lat = (hand.x - ch.x) * l[0] + (hand.z - ch.z) * l[1];
      if (TRACE) (ctx.extra.trace ??= []).push([+above.toFixed(3), +lat.toFixed(3), +((hand.x - el.x) * l[0] + (hand.z - el.z) * l[1]).toFixed(3),
        +((hand.x - ch.x) * f[0] + (hand.z - ch.z) * f[1]).toFixed(3), +(hand.y - el.y).toFixed(3)]);
      // 左右摆的来回: 离上一个极值回头 3 cm 以上算一次 (第一次定方向不算)
      if (above > 0) {
        if (dir === 0) {
          hi = Math.max(hi ?? lat, lat); lo = Math.min(lo ?? lat, lat);
          if (hi - lat > 0.03) { dir = -1; lo = lat; } else if (lat - lo > 0.03) { dir = 1; hi = lat; }
        } else if (dir === 1) {
          if (lat > hi) hi = lat; else if (hi - lat > 0.03) { rev++; dir = -1; lo = lat; }
        } else if (lat < lo) lo = lat; else if (lat - lo > 0.03) { rev++; dir = 1; hi = lat; }
      }
      return { rise: hand.y - y0, above, rev, gesture: L.idle?.kind === 'gesture' };
    };
  };
  // lat_wave:turned 先转到背后、站稳再挥 (R0 第一版在连着测时看到过转身后挥手没动, 用它单独复测)
  S.lat_wave = (ctx, [pre] = []) => {
    const L = ctx.loco, out = {};
    if (pre === 'turned') {
      const r = ctx.act({ do: 'turn', side: 'B' });
      if (r) return r;
      ctx.until(() => L.mode === 'idle' && !L.action && !!L.idle && Math.hypot(L.sim.vx, L.sim.vz) < 0.05, 8);
      ctx.step(30);
    }
    const c = control(ctx, waveTracker(ctx), 90);
    out.ctrl = { riseM: +Math.max(...c.map((s) => s.rise)).toFixed(3), aboveM: +Math.max(...c.map((s) => s.above)).toFixed(3), rev: c[c.length - 1].rev };
    if (out.ctrl.aboveM > 0 || out.ctrl.rev > 0) { ctx.extra.latency = out; return '对照段 (不发指令) 就已经手举过肩或在摆'; }
    const thr = Math.max(0.05, 2 * Math.max(0, out.ctrl.riseM));
    out.accepted = L.gesture({ do: 'wave', side: 'R', spd: 1 });
    const t = waveTracker(ctx);
    let maxAbove = -1;
    const hit = watch(ctx, () => { const s = t(); maxAbove = Math.max(maxAbove, s.above); return s; },
      { clip: (s) => s.gesture, rise: (s) => s.rise > thr, up: (s) => s.above > 0, waving: (s) => s.rev >= 2 }, FPS * 4);
    putMs(out, hit);
    out.maxAboveShoulderM = +maxAbove.toFixed(3);
    ctx.extra.latency = out;
    if (!out.accepted) return '手势请求没接下';
    return missing(out, ['clip', 'rise', 'up', 'waving'], 4);
  };
  // 比心 (行动层 _emote 的 IK). 判据相对不发指令时的对照 (2026-10-27 深夜改): 两手中点比对照往前 >= 3 cm、往上 >= 1 cm,
  // 两手食指远端关节的间距缩到对照最小值的 75% 以下. 原来用绝对阈值 (指尖 < 7 cm、手在胸口骨骼以上), 是按 silvercat 定的:
  // miomio、wendy 比心时手在胸口骨骼下方 5.6 cm (各模型胸口骨骼位置不同), miomio 指尖只合到 9 cm, 连原库都判不过.
  // 实测 (原库, 待机对照 -> 做满): silvercat 指尖 14 -> 5-6 cm、往前 18.6 -> 24 cm; miomio 13.5 -> 9 cm、14 -> 24 cm;
  // wendy 9.5 -> 0.6 cm、15.5 -> 22.6 cm; 高度都往上 2-10 cm
  const heartShape = (ctx) => {
    const L = ctx.loco, W = boneW(ctx), h = ctx.vrm.vrm.humanoid;
    const tip = (s) => (h.getNormalizedBoneNode(s + 'IndexDistal') ? s + 'IndexDistal' : s + 'MiddleProximal');
    return () => {
      const lw = W('leftHand'), rw = W('rightHand'), ch = W('chest'), { f } = axes(L);
      const mid = lw.clone().add(rw).multiplyScalar(0.5);
      const gap = W(tip('left')).distanceTo(W(tip('right'))), front = (mid.x - ch.x) * f[0] + (mid.z - ch.z) * f[1];
      if (TRACE) (ctx.extra.trace ??= []).push([+gap.toFixed(3), +lw.distanceTo(rw).toFixed(3), +front.toFixed(3), +(mid.y - ch.y).toFixed(3), +(L.emoteSt?.w || 0).toFixed(2)]);
      return { gap, front, up: mid.y - ch.y, w: L.emoteSt?.kind === 'heart' ? L.emoteSt.w : 0 };
    };
  };
  S.lat_heart = (ctx) => {
    const L = ctx.loco, out = {};
    const c = control(ctx, heartShape(ctx), FPS);
    const ctrl = { gap: Math.min(...c.map((s) => s.gap)), front: Math.max(...c.map((s) => s.front)), up: Math.max(...c.map((s) => s.up)) };
    out.ctrl = { minGapM: +ctrl.gap.toFixed(3), maxFrontM: +ctrl.front.toFixed(3), maxUpM: +ctrl.up.toFixed(3) };
    const isShape = (s) => s.gap < 0.75 * ctrl.gap && s.front > ctrl.front + 0.03 && s.up > ctrl.up + 0.01;
    out.accepted = L.emote('heart', { side: 'R' });
    const sh = heartShape(ctx);
    let minGap = 9;
    const hit = watch(ctx, () => { const s = sh(); minGap = Math.min(minGap, s.gap); return s; },
      { half: (s) => s.w > 0.5, full: (s) => s.w > 0.95, shape: isShape }, FPS * 4);
    putMs(out, hit);
    out.minGapM = +minGap.toFixed(3);
    ctx.extra.latency = out;
    if (!out.accepted) return '表情动作请求没接下';
    return missing(out, ['half', 'full', 'shape'], 4);
  };
  // 走到一个目标 (reach:目标): 停下 0.5 秒后离站位多远、朝向差几度; 走的过程中不经过脚锁量脚滑
  S.reach = (ctx, [id]) => {
    const L = ctx.loco, W = boneW(ctx);
    const sk = { frames: 0, contact: 0, dist: 0, prev: {} };
    ctx.onFrame = () => {
      sk.frames++;
      for (const s of ['left', 'right']) {
        const p = W(s + 'Foot'), q = sk.prev[s];
        if (q && p.y < L.ankle[s] + 0.025) { sk.contact++; sk.dist += Math.hypot(p.x - q.x, p.z - q.z); }
        sk.prev[s] = p;
      }
    };
    const f0 = ctx.frame;
    const r = ctx.act({ do: 'go', to: id }, 25);
    ctx.step(30);
    ctx.onFrame = null;
    const t = L.targets[id], lp = L._lookPoint(t);
    const want = Math.atan2(lp.x - L.char.x, lp.z - L.char.z);
    ctx.extra.reach = { to: id, distM: +Math.hypot(L.char.x - t.approach[0], L.char.z - t.approach[1]).toFixed(3), yawErrDeg: deg(Math.abs(ang(L.char.yaw - want))),
      secs: +((ctx.frame - f0) / FPS).toFixed(1), skateMps: +(sk.dist * FPS / Math.max(1, sk.contact)).toFixed(4), contactFrac: +(sk.contact / Math.max(1, 2 * sk.frames)).toFixed(3) };
    return r;
  };
  // 带情绪风格走 (walkstyle:happy): 风格库已经预先载入, setStyle 当帧就换; 走法同 walk
  S.walkstyle = (ctx, [emo]) => {
    const L = ctx.loco;
    L.setStyle(emo);
    const style = L.style, db = L.dbs[style];
    // 绕开 setStyle 的一个问题 (2026-10-27 查到, 没改 locomotion.js): 上一个场景停在这个风格库上, 复位时换回中性只挂了
    // "下一次搜索换中性库", 站着时不搜索; 这里再换回这个风格, 因为当前库就是它, setStyle 不更新挂着的库, 第一次搜索就换成了中性
    if (L.db === db) L._pendingDB = null;
    if (TRACE) {
      let last = '';
      ctx.onFrame = () => {
        const s = [L.style, L.db?.meta?.style, L._pendingDB?.meta?.style || '-', L.mode, L.idle?.kind || '-'].join('/');
        if (s !== last) { (ctx.extra.trace ??= []).push([ctx.frame, s]); last = s; }
      };
    }
    const r = S.walk(ctx);
    ctx.onFrame = null;
    ctx.extra.style = { emotion: emo, style, used: L.db === db, styleNow: L.style, dbNow: L.db?.meta?.style, pending: L._pendingDB?.meta?.style || null };
    return r || (L.db === db ? null : '风格库没换上');
  };

  const scenarios = ONLY || ['idle', 'walk', 'gesture', 'emote', 'talk', 'sit', 'pickput',
    'lat_go', 'lat_redirect', 'lat_stop', 'lat_turn', 'lat_wave', 'lat_heart',
    ...targetsOf(loco).map((id) => 'reach:' + id), ...Object.values(STYLE_EMO).map((e) => 'walkstyle:' + e)];
  let res;
  // 包装是一层套一层的, 倒着拆
  try { res = await me.runEval({ scenarios, seeds: ${JSON.stringify(seeds)} }); } finally { restore.reverse().forEach((f) => f()); }
  const q = (arr, p) => { const s = [...arr].sort((x, y) => x - y); return s.length ? +s[Math.min(s.length - 1, Math.floor(s.length * p))].toFixed(3) : null; };
  const sum = (v) => ({ n: v.length, mean: +(v.reduce((s, x) => s + x, 0) / v.length).toFixed(3), p50: q(v, 0.5), p95: q(v, 0.95), p99: q(v, 0.99), max: q(v, 1) });
  const stats = {}, all = {};
  for (const [sc, keys] of Object.entries(T)) {
    stats[sc] = Object.fromEntries(Object.entries(keys).map(([k, v]) => [k, sum(v)]));
    for (const [k, v] of Object.entries(keys)) (all[k] ??= []).push(...v);
  }
  stats.ALL = Object.fromEntries(Object.entries(all).map(([k, v]) => [k, sum(v)]));
  mem.heapAfterEval = heap();
  for (const r of res.results) r.counts = SC[r.scenario + '#' + r.seed] || null;
  return { mem, stats, ref: res.ref, scene: res.scene, avatarName: vrm.vrm.meta?.name || vrm.vrm.meta?.title || null, results: res.results };
})()`;

// -- 汇总 -------------------------------------------------------------------------------------
function summarize(out) {
  const by = {};
  for (const r of out.results) (by[r.scenario.split(':')[0] === 'reach' ? 'reach' : r.scenario] ??= []).push(r);
  const mean = (v) => (v.length ? +(v.reduce((s, x) => s + x, 0) / v.length).toFixed(3) : null);
  const scen = {};
  for (const [k, rs] of Object.entries(by)) {
    scen[k] = { ok: `${rs.filter((r) => r.ok).length}/${rs.length}`, natural: mean(rs.map((r) => r.natural)), pain_mean: mean(rs.map((r) => r.pain_mean)),
      slide: mean(rs.map((r) => r.pain?.slide || 0)), jerk: mean(rs.map((r) => r.pain?.jerk || 0)),
      fails: rs.filter((r) => !r.ok).map((r) => `${r.scenario}#${r.seed}: ${r.reason}`) };
    if (k.startsWith('lat_')) scen[k].latency = rs.map((r) => ({ seed: r.seed, ...r.latency }));
  }
  const reach = (by.reach || []).map((r) => r.reach).filter(Boolean);
  if (reach.length) {
    scen.reach.agg = { distM: mean(reach.map((x) => x.distM)), maxDistM: Math.max(...reach.map((x) => x.distM)), yawErrDeg: mean(reach.map((x) => x.yawErrDeg)),
      skateMps: mean(reach.map((x) => x.skateMps)), contactFrac: mean(reach.map((x) => x.contactFrac)) };
  }
  return scen;
}

// 整机 CPU 忙碌率 (跑的期间): 耗时随机器负载变, 别的会话在跑评估时会高 20-30%; A/B 要交替跑、同一时段比
const cpuTimes = () => cpus().reduce((s, c) => { const t = c.times; s.idle += t.idle; s.all += t.user + t.nice + t.sys + t.idle + t.irq; return s; }, { idle: 0, all: 0 });
const cpu0 = cpuTimes();
const before = hashFiles();
const b = await launch();
let out;
try {
  const { browserContextId } = await b.send('Target.createBrowserContext');
  const page = `${a.url.replace(/\/$/, '')}/?body=none${a.avatar ? `&avatar=${encodeURIComponent(a.avatar)}` : ''}${a.scene ? `&scene=${encodeURIComponent(a.scene)}` : ''}`;
  const { targetId } = await b.send('Target.createTarget', { url: 'about:blank', browserContextId });
  const { sessionId: s } = await b.send('Target.attachToTarget', { targetId, flatten: true });
  await b.send('Page.enable', {}, s);
  await b.send('Page.addScriptToEvaluateOnNewDocument', { source: 'window.requestAnimationFrame = () => 0;' }, s);
  const tNav = Date.now();
  await b.send('Page.navigate', { url: page }, s);
  for (;;) {
    const ok = await evaluate(b, s, '!!(window.__wm?.vrm?.loco?.idleDB && window.__wm.vrm.kin && window.__wm.vrm.loco.dbs?.neutral)').catch(() => false);
    if (ok) break;
    if (Date.now() - tNav > 240000) throw new Error(`页面 ${page} 没载入行动层`);
    await sleep(250);
  }
  const loadSec = (Date.now() - tNav) / 1000;
  await evaluate(b, s, '(() => { window.__wm.vrm.renderer.render = () => {}; return true; })()');
  await evaluate(b, s, "import('/static/motion-eval.js').then((m) => { window.__me = m; return true; })");
  const t0 = Date.now();
  out = await evaluate(b, s, MEASURE(a.seeds.split(',').map(Number), a.scenarios ? a.scenarios.split(',') : null, a.trace === '1', a.motion, Number(a['lmm-slot']) || 256,
    Number(a['lmm-bias']) || 0, Number(a.handoff) || 0, Number(a['lmm-cgate']) || 0, a['lmm-idleslot'] !== '0'));
  out.loadSec = loadSec;
  out.wallSec = (Date.now() - t0) / 1000;
  out.env = { date: new Date().toISOString(), browser: b.browser, node: process.version, cpu: cpus()[0]?.model, url: page, seeds: a.seeds, root: ROOT,
    motion: a.motion, lmmSlot: Number(a['lmm-slot']) || 256, lmmBias: Number(a['lmm-bias']) || 0, lmmCgate: Number(a['lmm-cgate']) || 0, lmmIdleSlot: a['lmm-idleslot'] !== '0', label: a.label, handoff: Number(a.handoff) || 0 };
} finally {
  await b.close();
}
const after = hashFiles();
const cpu1 = cpuTimes();
out.env.cpuBusy = +(1 - (cpu1.idle - cpu0.idle) / Math.max(1, cpu1.all - cpu0.all)).toFixed(3);
out.env.cpuThreads = cpus().length;
// 动作库在磁盘上的大小 (和页面里的占用分开记)
out.mem.fileBytes = Object.fromEntries(readdirSync(join(ROOT, 'models/motion')).filter((n) => /\.(bin|json)$/.test(n))
  .map((n) => [n, readFileSync(join(ROOT, 'models/motion', n)).length]));
// 页面在开跑时载入代码, 所以中途变了的文件以开跑前的哈希为准 (before), 两个都记
out.versions = { ...after, avatar: avatarFile(), changedDuringRun: Object.keys(after.files).filter((f) => before.files[f] !== after.files[f])
  .map((f) => ({ file: f, before: before.files[f] || null, after: after.files[f] })) };
if (a.pin) {
  const prev = JSON.parse(readFileSync(a.pin, 'utf8')).versions?.files || {};
  const files = new Set([...Object.keys(prev), ...Object.keys(after.files)]);
  out.versions.diffFromPin = [...files].filter((f) => prev[f] !== after.files[f]);
  if (out.versions.diffFromPin.length) process.stderr.write(`和 ${a.pin} 版本不同的文件: ${out.versions.diffFromPin.join(', ')}\n`);
}
if (out.versions.changedDuringRun.length) process.stderr.write(`跑的过程中变了的文件: ${out.versions.changedDuringRun.map((c) => c.file).join(', ')}\n`);
out.summary = summarize(out);
if (a.out) writeFileSync(a.out, JSON.stringify(out, null, 1));
process.stdout.write(`${JSON.stringify({ mem: out.mem, timing: out.stats.ALL, summary: out.summary, versions: { code: out.versions.code, data: out.versions.data, ref: out.versions.ref,
  changedDuringRun: out.versions.changedDuringRun, diffFromPin: out.versions.diffFromPin } }, null, 1)}\n`);
