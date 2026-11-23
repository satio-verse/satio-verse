#!/usr/bin/env node
// 运动层执行评估的无界面跑法: 用本机已装的 Chrome / Edge (无界面模式, 单独的临时用户目录, 不碰平时用的浏览器)
// 打开 WorldModel 页面, 调 web/motion-eval.js 跑场景, 结果打印成 JSON. 页面要连一个模拟服务
// (python -m wm --llm mock --asr none --tts mock --rvc mock --port 8797 --set memory.vault="").
//
//   node tools/motion_eval.mjs --url http://localhost:8797 [--avatar miomio.vrm] [--scene room] [--scenarios idle,walk] [--seeds 1,2]
//                              [--params params.json] [--checks]
//       --scenarios sweep: 体检用的全套 (走到每个目标、坐每个座位、每件东西放到每个台面, 见 motion-eval.js);
//       --checks: 另外给出场景和模型的静态检查
//   node tools/motion_eval.mjs --url ... --serve [--tabs 4]
//       逐行从标准输入读任务 {"id", "params", "scenarios", "seeds", "avatar", "scene", "checks"}, 逐行输出 {"id", ...结果};
//       调优 (tools/tune_motion.py)、体检 (tools/motion_checkup.py) 用.
//       每个标签页在单独的浏览器上下文里 (单独的渲染进程), 几个任务可以并行.
//   --probe motion-growth-probe.js: 载入评估模块以后再载入 web/ 下的这个探针 (它的 install(state, 评估模块) 返回包过的评估模块),
//       动作成长 (tools/motion_growth.py) 用它在每个场景里跑和小屋同一套失败检测, 结果多一段 growth. 不给就和原来一样.
//   --query perfoff=proxy: 页面地址多带的参数 (同一份代码开关两边做 A/B); 不给时取环境变量 WM_EVAL_QUERY,
//       调优、端咖啡评测台 (tools/bench_carry.py) 这些经 tune_motion.py 起的也就带上了.
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createInterface } from 'node:readline';

const BROWSERS = [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser',
];

function args(argv) {
  const out = { url: 'http://localhost:8797', tabs: 1, avatar: null, scene: null, scenarios: null, seeds: null, params: null,
    serve: false, checks: false, browser: null, timeout: 180, probe: null };
  for (let i = 2; i < argv.length; i++) {
    const k = argv[i].replace(/^--/, '');
    if (k === 'serve' || k === 'checks') out[k] = true;
    else out[k] = argv[++i];
  }
  out.tabs = Number(out.tabs) || 1;
  out.timeout = Number(out.timeout) || 180;
  out.coverageMode = out['coverage-mode'] ?? 'normal';
  if (!['normal', 'diagnostic'].includes(out.coverageMode)) throw new Error('--coverage-mode 只接受 normal 或 diagnostic');
  if (out.probe && !/^[\w.-]+\.js$/.test(out.probe)) throw new Error(`--probe 只能是 web/ 下的文件名: ${out.probe}`);
  PROBE = out.probe;
  QUERY = String(out.query ?? process.env.WM_EVAL_QUERY ?? '').replace(/^[?&]+/, '');
  return out;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let PROBE = null;  // --probe
let QUERY = '';  // --query

/** 起一个无界面浏览器, 返回 {ws, send, close}: send(method, params, sessionId) 发 CDP 命令. */
async function launch(browserPath) {
  const exe = browserPath || BROWSERS.find((p) => existsSync(p));
  if (!exe) throw new Error('没找到 Chrome 或 Edge, 用 --browser 指定路径');
  const dir = mkdtempSync(join(tmpdir(), 'wm-eval-'));
  const proc = spawn(exe, ['--headless=new', '--remote-debugging-port=0', `--user-data-dir=${dir}`, '--no-first-run',
    '--no-default-browser-check', '--disable-extensions', '--mute-audio', '--disable-background-timer-throttling',
    '--disable-renderer-backgrounding', '--disable-backgrounding-occluded-windows', '--use-angle=swiftshader',
    '--enable-unsafe-swiftshader', 'about:blank'], { stdio: 'ignore' });
  let port = null;
  for (let i = 0; i < 200 && !port; i++) {
    await sleep(100);
    const f = join(dir, 'DevToolsActivePort');
    // 浏览器还在写这个文件时读会报 EBUSY (同时起好几个评估进程时碰到过), 下一轮再读
    try { if (existsSync(f)) port = readFileSync(f, 'utf8').split('\n')[0].trim() || null; } catch { /* 下一轮再读 */ }
  }
  if (!port) { proc.kill(); throw new Error('浏览器没起来 (没有 DevToolsActivePort)'); }
  const { webSocketDebuggerUrl } = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
  const ws = new WebSocket(webSocketDebuggerUrl);
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
    ws.close();
    proc.kill();
    await sleep(300);
    try { rmSync(dir, { recursive: true, force: true }); } catch { /* 浏览器还占着文件, 留给系统清理 */ }
  };
  return { send, close };
}

/** 在页面里求值 (等 Promise), 返回值; 页面里抛错就抛出来. */
async function evaluate(b, session, expression) {
  const r = await b.send('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true }, session);
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result.value;
}

/** 新开一个标签页 (单独的浏览器上下文), 等行动层和待机库载入, 停掉页面自己的渲染循环, 载入评估模块. */
async function openTab(b, url, avatar, scene, timeout) {
  const { browserContextId } = await b.send('Target.createBrowserContext');
  const page = `${url.replace(/\/$/, '')}/?body=none${avatar ? `&avatar=${encodeURIComponent(avatar)}` : ''}`
    + `${scene ? `&scene=${encodeURIComponent(scene)}` : ''}${QUERY ? `&${QUERY}` : ''}`;
  const { targetId } = await b.send('Target.createTarget', { url: 'about:blank', browserContextId });
  const { sessionId } = await b.send('Target.attachToTarget', { targetId, flatten: true });
  // 评估时手动推进帧, 不需要页面自己的渲染循环: 无界面模式下是软件渲染, 带阴影的房间画一帧要很久, 会把载入卡住;
  // 它还会在评估之间推进角色. 页面一开始就让 requestAnimationFrame 什么都不做
  await b.send('Page.enable', {}, sessionId);
  await b.send('Page.addScriptToEvaluateOnNewDocument', { source: 'window.requestAnimationFrame = () => 0;' }, sessionId);
  await b.send('Page.navigate', { url: page }, sessionId);
  const t0 = Date.now();
  for (;;) {
    const ok = await evaluate(b, sessionId, '!!(window.__wm?.vrm?.loco?.idleDB && window.__wm.vrm.kin)').catch(() => false);
    if (ok) break;
    if (Date.now() - t0 > timeout * 1000) throw new Error(`页面 ${page} ${timeout} 秒内没载入行动层`);
    await sleep(500);
  }
  await evaluate(b, sessionId, '(() => { window.__wm.vrm.renderer.render = () => {}; return true; })()');
  await evaluate(b, sessionId, "import('/static/motion-eval.js').then((m) => { window.__me = m; return true; })");
  if (PROBE) {
    await evaluate(b, sessionId, `import('/static/${PROBE}').then((p) => p.install(window.__wm, window.__me))`
      + '.then((m) => { if (m) window.__me = m; return true; })');
  }
  return { sessionId, targetId, browserContextId, avatar, key: `${avatar || ''}|${scene || ''}` };
}

async function runJob(b, tab, job) {
  if (job.coverageMode !== undefined && !['normal', 'diagnostic'].includes(job.coverageMode)) throw new Error('coverageMode 只接受 normal 或 diagnostic');
  const spec = JSON.stringify({ params: job.params || {}, ...(job.scenarios ? { scenarios: job.scenarios } : {}),
    ...(job.seeds ? { seeds: job.seeds } : {}), ...(job.checks ? { checks: true } : {}),
    ...(job.coverageMode !== undefined ? { coverageMode: job.coverageMode } : {}) });
  return evaluate(b, tab.sessionId, `window.__me.runEval(${spec})`);
}

async function main() {
  const a = args(process.argv);
  const b = await launch(a.browser);
  try {
    if (!a.serve) {
      const tab = await openTab(b, a.url, a.avatar, a.scene, a.timeout);
      const job = { params: a.params ? JSON.parse(readFileSync(a.params, 'utf8')).values ?? {} : {},
        scenarios: a.scenarios === 'sweep' ? 'sweep' : a.scenarios ? a.scenarios.split(',') : null,
        seeds: a.seeds ? a.seeds.split(',').map(Number) : null, checks: a.checks, coverageMode: a.coverageMode };
      const t0 = Date.now();
      const res = await runJob(b, tab, job);
      res.wallSeconds = +((Date.now() - t0) / 1000).toFixed(1);
      res.avatar = a.avatar;
      process.stdout.write(`${JSON.stringify(res)}\n`);
      return;
    }
    // 服务模式: 按 (形象, 场景) 分组的标签页池, 任务排队, 空闲的标签页取任务
    const tabs = new Map();  // "形象|场景" -> [tab]
    const free = [], queue = [];
    const pending = new Set();
    const keyOf = (j) => `${j.avatar || a.avatar || ''}|${j.scene || a.scene || ''}`;
    const getTabs = async (job) => {
      const key = keyOf(job);
      if (!tabs.has(key)) {
        const avatar = job.avatar || a.avatar, scene = job.scene || a.scene;
        const list = await Promise.all(Array.from({ length: a.tabs }, () => openTab(b, a.url, avatar, scene, a.timeout)));
        tabs.set(key, list);
        free.push(...list);
      }
      return tabs.get(key);
    };
    const pump = async () => {
      for (;;) {
        const i = queue.findIndex((j) => free.some((t) => t.key === keyOf(j)));
        if (i < 0) return;
        const job = queue.splice(i, 1)[0];
        const ti = free.findIndex((t) => t.key === keyOf(job));
        const tab = free.splice(ti, 1)[0];
        const p = (async () => {
          const t0 = Date.now();
          let out;
          try { out = { id: job.id, ...(await runJob(b, tab, job)) }; } catch (err) { out = { id: job.id, error: String(err.message || err) }; }
          out.wallSeconds = +((Date.now() - t0) / 1000).toFixed(1);
          out.avatar = tab.avatar;
          process.stdout.write(`${JSON.stringify(out)}\n`);
          free.push(tab);
        })();
        pending.add(p);
        p.finally(() => { pending.delete(p); pump(); });
      }
    };
    const rl = createInterface({ input: process.stdin });
    for await (const line of rl) {
      if (!line.trim()) continue;
      const job = JSON.parse(line);
      job.coverageMode ??= a.coverageMode;
      await getTabs(job);
      queue.push(job);
      pump();
    }
    while (pending.size || queue.length) { await sleep(200); pump(); }
  } finally {
    await b.close();
  }
}

main().catch((err) => { process.stderr.write(`${err.stack || err}\n`); process.exit(1); });
