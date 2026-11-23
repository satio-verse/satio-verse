// 终端启动画面 (term-step1, 2026-11-17) 的测试. 默认测 desktop/web 里的真文件; 草稿期用环境变量 SPLASH_WEB 指到别的 web 目录:
//   SPLASH_WEB=G:/RunProgram/WorldModel_v1/backups/term-step1/run-new/desktop/web node --test tests/js/splash.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { pathToFileURL, fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const web = process.env.SPLASH_WEB || path.join(here, '..', '..', 'desktop', 'web');
const S = await import(pathToFileURL(path.join(web, 'splash.js')).href);
const P = await import(pathToFileURL(path.join(web, 'ui-preferences.js')).href);
const read = (name) => readFileSync(path.join(web, name), 'utf8');

// ---- 最小的 DOM / canvas / 时钟替身 (计数监听器、rAF、定时器、被移除的画布) ----
function ctxProxy() {
  const store = {};
  return new Proxy(store, {
    get: (t, k) => {
      if (k in t) return t[k];
      if (k === 'measureText') return (ch) => ({ width: 55 * [...String(ch)].length });
      return k.startsWith('create') ? () => ({ addColorStop() {} }) : () => {};
    },
    set: (t, k, v) => { t[k] = v; return true; },
  });
}
function listeners() {
  const m = new Map();
  return { m, add(t, f) { if (!m.has(t)) m.set(t, new Set()); m.get(t).add(f); }, del(t, f) { m.get(t)?.delete(f); }, count() { let n = 0; for (const s of m.values()) n += s.size; return n; },
    fire(t, e = {}) { for (const f of [...(m.get(t) || [])]) f(e); } };
}
function env({ w = 420, h = 800, dpr = 1.25 } = {}) {
  const clock = { now: 0, q: new Map(), id: 0, rafCalls: 0, timers: new Map(), tid: 0 };
  const created = [];
  const dl = listeners(), wl = listeners();
  const body = { dataset: {}, children: [], append(c) { this.children.push(c); } };
  const doc = {
    body,
    createElement() {
      const cl = listeners();
      const c = { width: 0, height: 0, style: {}, removed: false, cl, ctx: ctxProxy(), attrs: {}, setAttribute(k, v) { this.attrs[k] = v; },
        addEventListener: (t, f) => cl.add(t, f), removeEventListener: (t, f) => cl.del(t, f),
        getContext() { return this.ctx; }, remove() { this.removed = true; const i = body.children.indexOf(this); if (i >= 0) body.children.splice(i, 1); } };
      created.push(c);
      return c;
    },
    addEventListener: (t, f) => dl.add(t, f), removeEventListener: (t, f) => dl.del(t, f),
  };
  const win = { innerWidth: w, innerHeight: h, devicePixelRatio: dpr, Event: class { constructor(type) { this.type = type; } },
    addEventListener: (t, f) => wl.add(t, f), removeEventListener: (t, f) => wl.del(t, f), dispatchEvent(e) { wl.fire(e.type, e); } };
  const raf = (f) => { clock.id++; clock.rafCalls++; clock.q.set(clock.id, f); return clock.id; };
  const caf = (id) => { clock.q.delete(id); };
  const setT = (f, ms) => { clock.tid++; clock.timers.set(clock.tid, { due: clock.now + ms, f }); return clock.tid; };
  const clearT = (id) => { clock.timers.delete(id); };
  const advance = (sec, step = 1 / 60) => {
    for (let t = 0; t < sec - 1e-9; t += step) {
      clock.now += step * 1000;
      for (const [id, tm] of [...clock.timers.entries()]) if (tm.due <= clock.now) { clock.timers.delete(id); tm.f(); }
      const run = [...clock.q.entries()]; clock.q.clear(); for (const [, f] of run) f();
    }
  };
  return { clock, doc, win, dl, wl, body, created, raf, caf, advance,
    ready: () => win.dispatchEvent(new win.Event('wm-term-ready')),
    opts: (extra = {}) => ({ doc, win, now: () => clock.now, raf, caf, setTimeout: setT, clearTimeout: clearT, ...extra }) };
}
const endOf = (c) => c.log.find((x) => x.name === 'end').t;
const at = (c, name) => c.log.find((x) => x.name === name)?.t;

test('字样常量: 一个字符不改; 状态文案', () => {
  assert.equal(S.SPLASH_TEXT, 'S.A.T.I.O Verse');
  assert.equal(S.SPLASH_TEXT.length, 15);
  assert.equal(S.STATUS_TEXT, '正在准备终端…');
  assert.ok(S.MIN_TOTAL > 2.8 && S.MIN_TOTAL < 3.0, `最短 ${S.MIN_TOTAL}`);
});

test('coldToken / splashEnabled: 只有冷启动令牌 (不是上次放过的) 才放; 没有令牌 (浏览器、测试、截图、自启) 不放; splash=0 不放', () => {
  assert.equal(S.coldToken('?splash=123456'), '123456');
  assert.equal(S.coldToken('?splash=0123'), '0123');     // 以 0 开头的令牌也是令牌 (%RANDOM%%RANDOM% 拼出来可能这样)
  assert.equal(S.coldToken('?splash=0'), null);
  assert.equal(S.coldToken('?splash='), null);
  assert.equal(S.coldToken(''), null);
  assert.equal(S.coldToken('?desk=0&s=abc'), null);
  assert.equal(S.splashEnabled({ search: '?splash=111' }), true);
  assert.equal(S.splashEnabled({ search: '?splash=111', last: '111' }), false);      // 同一次启动里再开终端: 不放
  assert.equal(S.splashEnabled({ search: '?splash=222', last: '111' }), true);        // 新的一次启动
  assert.equal(S.splashEnabled({ search: '?splash=111', pref: false }), false);       // 设置里关了
  assert.equal(S.splashEnabled({ search: '?splash=0' }), false);
  assert.equal(S.splashEnabled({ search: '' }), false);                                // 浏览器里打开 / 开机自启 / 界面实点测试 / 截图脚本: 没有令牌
  assert.equal(S.splashEnabled({ search: '?desk=0&s=abc' }), false);
  assert.equal(S.splashEnabled({ search: '?splashtest=life', pref: false, last: 'x' }), true);   // 自检照放
  assert.equal(S.splashEnabled({ search: '?splashtest=life&splash=0' }), false);
  assert.equal(S.splashEnabled(), false);
});

test('splashReduced: 偏好 on 或 (偏好不是 off 且系统要求)', () => {
  assert.equal(S.splashReduced('on', false), true);
  assert.equal(S.splashReduced('system', true), true);
  assert.equal(S.splashReduced('system', false), false);
  assert.equal(S.splashReduced('off', true), false);
});

test('layoutText: 窄窗口占 84%、宽窗口占 62%; 短字样更窄; 都放得进窗口; 字距放宽; 位置递增', () => {
  const m = (ch, px) => px * 0.55;
  const n = S.layoutText(420, 800, S.SPLASH_TEXT, m), w = S.layoutText(1280, 800, S.SPLASH_TEXT, m), sh = S.layoutText(1280, 800, 'S.A.T.I.O', m);
  assert.ok(Math.abs(n.width - 420 * 0.84) < 1, `窄窗口宽 ${n.width}`);
  assert.ok(Math.abs(w.width - 1280 * 0.62) < 1 || w.fs === 800 * 0.2, `宽窗口宽 ${w.width}`);
  assert.ok(sh.width < w.width);
  for (const L of [n, w, sh]) {
    assert.equal(L.chars.join(''), L.chars.join(''));
    for (let i = 1; i < L.xs.length; i++) assert.ok(L.xs[i] > L.xs[i - 1]);
    assert.ok(L.tracking > L.fs * 0.3);
  }
  assert.equal(n.chars.join(''), 'S.A.T.I.O Verse');
  const tiny = S.layoutText(120, 120, S.SPLASH_TEXT, m);
  assert.ok(tiny.width <= 120);
});

test('lightAt: 亮点 -> 7% 的一小段光 -> 先快后慢展开到字样宽度 -> 变细淡出', () => {
  const W = 1280, th = 380;
  const a = (t) => S.lightAt(t, W, th);
  assert.equal(a(0.1).alpha, 0); assert.equal(a(0.1).half, 0);                 // 开头全黑
  assert.ok(a(0.40).alpha > 0.5 && a(0.40).half < 2);                           // 亮点
  const h0 = W * 0.035;
  assert.ok(Math.abs(a(0.64).half - h0) < 1, `短光半长 ${a(0.64).half}`);       // 宽度的 7%
  assert.ok(Math.abs(a(0.64).half * 2 / W - 0.07) < 0.002);
  assert.ok(a(0.64).alpha === 1 && a(0.64).thick === 1);
  let prev = a(0.64).half;
  for (let t = 0.7; t <= 1.56; t += 0.05) { const h = a(t).half; assert.ok(h >= prev - 1e-9, `展开要单调 ${t}`); prev = h; }
  assert.ok(a(1.56).half >= th * 1.1, '展开到字样的宽度');
  assert.ok(a(0.9).half - a(0.8).half > (a(1.5).half - a(1.4).half) * 3, '先快后慢');
  assert.ok(a(1.9).thick < 0.3 && a(1.9).alpha < 0.05 && a(2.0).alpha === 0, '光变细、淡出');
  assert.ok(S.revealHalf(300, W) < 300 && S.revealHalf(300, W) > 250, '字比光略落后');
  assert.equal(S.revealHalf(3, W), 0);
});

test('phaseAt: 0.2/0.6/1.2/2.0 秒在播放里, 之后保持, 终端早好 -> 2.42 秒起淡出 0.5 秒, 约 2.92 秒结束', () => {
  const p = S.phaseAt;
  for (const t of [0.2, 0.6, 1.2, 2.0]) assert.equal(p(t, { readyAt: 0.4 }).phase, 'play', `${t}`);
  assert.equal(p(2.5, { readyAt: 0.4 }).phase, 'fade');
  assert.equal(p(2.42 + 0.25, { readyAt: 0.4 }).u.toFixed(2), '0.50');
  assert.equal(p(S.MIN_TOTAL - 0.01, { readyAt: 0.4 }).phase, 'fade');
  assert.equal(p(S.MIN_TOTAL + 0.01, { readyAt: 0.4 }).phase, 'done');
  assert.equal(p(3.0, {}).phase, 'hold');                                             // 终端没好: 停在字加细线那一帧
  assert.equal(p(3.0, {}).tl, S.T.fade0);
  assert.ok(Math.abs(p(3.5, {}).holdT - (3.5 - S.T.fade0)) < 1e-9);
});

test('phaseAt: 终端晚到 -> 到了才收尾; 一直不到 -> 20 秒放行; 跳过 -> 停在当时那一帧快速淡出; 减少动态效果只淡入淡出', () => {
  const p = S.phaseAt;
  assert.equal(p(5.0, { readyAt: 6 }).phase, 'hold');
  assert.equal(p(6.1, { readyAt: 6 }).phase, 'fade');
  assert.equal(p(6.55, { readyAt: 6 }).phase, 'done');
  assert.equal(p(19.9, {}).phase, 'hold');
  assert.equal(p(20.1, {}).phase, 'fade');
  assert.equal(p(20.55, {}).phase, 'done');
  assert.equal(p(100, { readyAt: 50 }).phase, 'done');                                // 上限之后才好: 以上限为准
  const s = p(1.1, { skipAt: 1.0, readyAt: 0 });
  assert.equal(s.phase, 'fade'); assert.equal(s.tl, 1.0); assert.equal(s.fadeDur, S.T.skipFade);
  assert.equal(p(1.0 + S.T.skipFade + 0.01, { skipAt: 1.0 }).phase, 'done');
  assert.equal(p(3.2, { skipAt: 3.0 }).phase, 'fade');                                // 保持里点一下
  assert.equal(p(2.6, { skipAt: 2.5, readyAt: 0 }).fadeStart, 2.42);                  // 已经在淡出里再点: 无效
  const r = (t, o = {}) => p(t, { reduced: true, ...o });
  assert.equal(r(0.2).phase, 'play'); assert.equal(r(0.85, { readyAt: 0 }).phase, 'play');
  assert.equal(r(0.95, { readyAt: 0 }).phase, 'fade');
  assert.equal(r(0.9 + S.T.rOut + 0.01, { readyAt: 0 }).phase, 'done');
  assert.equal(r(3, {}).phase, 'hold');
  assert.ok(![0.2, 1, 2, 3, 20].some((t) => p(t, { reduced: true }).phase === 'flip'));
});

test('statusAlpha: 保持里 4 秒起才出灰色小字; 淡出里不出', () => {
  const ph = (t) => S.phaseAt(t, {});
  assert.equal(S.statusAlpha(3.0, ph(3.0)), 0);
  assert.equal(S.statusAlpha(4.0, ph(4.0)), 0);
  assert.ok(S.statusAlpha(4.3, ph(4.3)) > 0 && S.statusAlpha(5.0, ph(5.0)) === 1);
  assert.equal(S.statusAlpha(2.0, S.phaseAt(2.0, {})), 0);
  assert.equal(S.statusAlpha(20.2, S.phaseAt(20.2, {})), 0);
});

test('drawFrame: 各个阶段、各种窗口大小、两种字样、减少动态都画得出来 (不抛), 返回的不透明度只在淡出里小于 1', () => {
  for (const [w, h] of [[420, 800], [1280, 800], [300, 300]]) for (const text of [S.SPLASH_TEXT, 'S.A.T.I.O']) {
    const e = env({ w, h });
    const sc = S.buildScene(e.doc, w, h, text, 1.25), ctx = ctxProxy();
    for (const t of [0, 0.2, 0.35, 0.5, 0.64, 1.0, 1.4, 1.9, 2.4, 3.0, 4.5, 5.0, 20.1]) {
      const ph = S.phaseAt(t, { readyAt: t > 20 ? 0 : null });
      const op = S.drawFrame(ctx, sc, t, ph, { reduced: false });
      assert.ok(op >= 0 && op <= 1); assert.equal(op < 1, ph.phase === 'fade' && ph.u > 0);
    }
    for (const t of [0, 0.2, 0.6, 0.95, 1.1]) S.drawFrame(ctx, sc, t, S.phaseAt(t, { reduced: true, readyAt: 0 }), { reduced: true });
    assert.equal(S.renderFrameAt(e.doc, w, h, 1.0, {}, 1, text).width, w);
  }
});

test('控制器全程 (终端 0.5 秒好): 约 2.92 秒结束, 画布移除、监听全拆、没有排着的 rAF 和定时器、done 已 resolve', async () => {
  const e = env();
  const c = S.startSplash(e.opts());
  assert.equal(e.body.children.length, 1);
  assert.equal(e.body.children[0].attrs['aria-label'], 'S.A.T.I.O Verse');
  assert.ok(e.dl.count() >= 1 && e.wl.count() >= 1 && e.created[0].cl.count() >= 1);
  e.advance(0.5);
  e.ready();
  e.advance(S.MIN_TOTAL + 0.3);
  assert.equal(c.active(), false);
  await c.done;
  assert.equal(e.body.children.length, 0);
  assert.ok(e.created[0].removed);
  for (const cv of e.created) { assert.equal(cv.width, 0); assert.equal(cv.height, 0); }
  assert.equal(e.dl.count(), 0); assert.equal(e.wl.count(), 0); assert.equal(e.created[0].cl.count(), 0);
  assert.equal(e.clock.q.size, 0); assert.equal(e.clock.timers.size, 0);
  const end = endOf(c);
  assert.ok(end >= 2.85 && end <= 3.1, `结束 ${end}`);
  assert.ok(at(c, 'fade') >= 2.4 && at(c, 'fade') <= 2.55);
  c.destroy(); c.skip(); c.ready();
});

test('终端晚到 (3.5 秒): 停在保持里, 4 秒前没有小字, 到了才收尾; 淡出时不透明度降到 0', async () => {
  const e = env();
  const c = S.startSplash(e.opts());
  e.advance(3.4);
  assert.equal(c.active(), true);
  assert.ok(at(c, 'hold') >= 2.4 && at(c, 'hold') <= 2.6, '进入保持');
  const cv = e.created[0];
  e.ready();
  e.advance(0.25);
  assert.ok(Number(cv.style.opacity) < 1 && Number(cv.style.opacity) > 0, `淡出中 ${cv.style.opacity}`);
  e.advance(0.4);
  assert.equal(c.active(), false);
  const end = endOf(c);
  assert.ok(end >= 3.9 && end <= 4.2, `结束 ${end}`);
  await c.done;
});

test('一直不好: 20 秒到了不管怎样放行 (不抛、不留东西)', async () => {
  const e = env();
  const c = S.startSplash(e.opts());
  e.advance(19.5, 1 / 30);
  assert.equal(c.active(), true);
  e.advance(1.2, 1 / 30);
  assert.equal(c.active(), false);
  const end = endOf(c);
  assert.ok(end >= 20.4 && end <= 20.8, `结束 ${end}`);
  assert.equal(e.body.children.length, 0); assert.equal(e.clock.timers.size, 0); assert.equal(e.clock.q.size, 0);
  await c.done;
});

test('跳过: 点一下 / 按键 -> 画面停住 0.3 秒快速淡出后结束; 淡出里再点无效; 保持里点也行', async () => {
  for (const how of ['pointerdown', 'keydown']) {
    const e = env();
    const c = S.startSplash(e.opts());
    e.advance(0.8);
    if (how === 'pointerdown') e.created[0].cl.fire('pointerdown'); else e.dl.fire('keydown');
    e.advance(S.T.skipFade + 0.15);
    assert.equal(c.active(), false, how);
    const end = endOf(c);
    assert.ok(end >= 0.8 + S.T.skipFade - 0.03 && end <= 0.8 + S.T.skipFade + 0.12, `${how} 结束 ${end}`);
    assert.equal(e.body.children.length, 0);
  }
  const e = env();
  const c = S.startSplash(e.opts());
  e.ready();
  e.advance(2.6);                                   // 已经在淡出里
  c.skip();
  assert.equal(c.log.filter((x) => x.name === 'skip').length, 0);
  c.destroy();
  const e2 = env();
  const c2 = S.startSplash(e2.opts());
  e2.advance(3.2);                                  // 保持里
  e2.created[0].cl.fire('pointerdown');
  e2.advance(0.5);
  assert.equal(c2.active(), false);
  assert.ok(endOf(c2) < 3.9);
});

test('页面藏了一阵 (rAF 不跑) 再显示: 该结束就直接收尾, 不补放过程; 看门狗兜底', async () => {
  const e = env();
  const c = S.startSplash(e.opts());
  e.advance(0.5);
  e.ready();
  const before = c.frames;
  e.clock.now += 20000;
  e.advance(1 / 60);
  assert.equal(c.active(), false);
  assert.equal(c.frames, before, '结束时不再画');
  assert.equal(e.body.children.length, 0);
  assert.equal(e.clock.q.size, 0); assert.equal(e.clock.timers.size, 0);
  await c.done;
});

test('减少动态效果: 只有字的淡入淡出 (淡入 0.4 + 至少停 0.5 + 淡出 0.4), ~1.3 秒结束; 终端晚到就等', async () => {
  const e = env();
  const c = S.startSplash(e.opts({ reduced: true }));
  e.ready();
  e.advance(1.6);
  assert.equal(c.active(), false);
  const end = endOf(c);
  assert.ok(end >= 1.25 && end <= 1.45, `结束 ${end}`);
  const e2 = env();
  const c2 = S.startSplash(e2.opts({ reduced: true }));
  e2.advance(2.0);
  assert.equal(c2.active(), true);
  e2.ready(); e2.advance(0.6);
  assert.equal(c2.active(), false);
});

test('window.__wmTermReady 已经置位 (宿主的消息比启动画面先到): 一开始就算好了', async () => {
  const e = env();
  e.win.__wmTermReady = true;
  const c = S.startSplash(e.opts());
  e.advance(S.MIN_TOTAL + 0.3);
  assert.equal(c.active(), false);
  assert.ok(endOf(c) <= 3.1);
  assert.equal(at(c, 'ready'), undefined);
});

test('enabled: false: 什么都不建、不挂监听; 字样可以换 (开场卡复用)', async () => {
  const e = env();
  const c = S.startSplash(e.opts({ enabled: false }));
  assert.equal(c.active(), false);
  assert.equal(e.created.length, 0); assert.equal(e.dl.count() + e.wl.count(), 0);
  await c.done; c.skip(); c.ready(); c.destroy();
  const e2 = env();
  const c2 = S.startSplash(e2.opts({ text: 'S.A.T.I.O' }));
  assert.equal(e2.body.children[0].attrs['aria-label'], 'S.A.T.I.O');
  c2.destroy();
});

test('限帧: 终端加载期间约 30 帧/秒 (rAF 也只要约 30 次), 好了以后约 60 帧/秒', () => {
  const e = env();
  const c = S.startSplash(e.opts());
  e.advance(1.0, 1 / 144);
  assert.ok(c.frames >= 26 && c.frames <= 36, `加载期间 1 秒画了 ${c.frames} 帧`);
  assert.ok(e.clock.rafCalls <= 40, `rAF 次数 ${e.clock.rafCalls}`);
  e.ready();
  const before = c.frames;
  e.advance(0.5, 1 / 144);
  const after = c.frames - before;
  assert.ok(after >= 25 && after <= 36, `好了以后 0.5 秒画了 ${after} 帧`);
  c.destroy();
});

test('splash.js: 无 import、不联网、不用外部素材; 控制器清理不依赖 DOM 以外的东西', () => {
  const src = read('splash.js');
  assert.ok(!/^\s*import\s/m.test(src) && !/\bimport\(/.test(src));
  assert.ok(!/fetch\(|XMLHttpRequest|WebSocket|https?:\/\//.test(src.replace(/\/\/.*$/gm, '')));
  assert.ok(!/@font-face|FontFace|\.woff|\.ttf/.test(src));
});

test('ui-preferences: 终端启动画面开关默认开、存 0 才关、存储不可用不抛; 令牌读写; 旧的桌宠启动动画开关已清掉', () => {
  assert.equal(P.TERM_SPLASH_KEY, 'wm.term.splash.v1');
  assert.notEqual(P.TERM_SPLASH_KEY, 'wm.pet.splash.v1');        // 新键, 不沿用旧的 (用户可能手动关过旧的)
  assert.equal(P.TERM_SPLASH_LAST, 'wm.term.splash.last');
  assert.ok(!('PET_SPLASH_KEY' in P) && !('readPetSplash' in P) && !('writePetSplash' in P));
  const real = globalThis.localStorage;
  try {
    const mem = new Map();
    globalThis.localStorage = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, String(v)), removeItem: (k) => mem.delete(k) };
    assert.equal(P.readTermSplash(), true);
    mem.set('wm.pet.splash.v1', '0'); assert.equal(P.readTermSplash(), true);   // 旧键存过 0 也不影响新键
    assert.equal(P.writeTermSplash(false), true); assert.equal(mem.get('wm.term.splash.v1'), '0'); assert.equal(P.readTermSplash(), false);
    assert.equal(P.writeTermSplash(true), true); assert.equal(P.readTermSplash(), true);
    mem.set('wm.term.splash.v1', 'garbage'); assert.equal(P.readTermSplash(), true);
    globalThis.localStorage = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
    assert.equal(P.readTermSplash(), true); assert.equal(P.writeTermSplash(false), false);
    delete globalThis.localStorage;
    assert.equal(P.readTermSplash(), true); assert.equal(P.writeTermSplash(false), false);
  } finally { if (real) globalThis.localStorage = real; }
  assert.ok('readPetScale' in P && 'STORAGE_KEY' in P && 'updatePreferences' in P);   // 原来的导出还在
});

test('workbench.html: 冷启动才写黑底、启动脚本在 workbench.js 之前、动态 import 并用 try 包住、只读监听 workbench-capabilities、标题 S.A.T.I.O', () => {
  const html = read('workbench.html');
  const cold = html.indexOf('wm-cold'), boot = html.indexOf("import('/pet/splash.js')"), main = html.indexOf('src="/pet/workbench.js"');
  assert.ok(cold > 0 && cold < html.indexOf('<link rel="stylesheet"'), '黑底在样式表之前');
  assert.ok(boot > 0 && main > boot, '启动脚本在 workbench.js 之前');
  assert.ok(/<title>S\.A\.T\.I\.O<\/title>/.test(html));
  assert.ok(html.includes('try {') && html.includes('catch (err)'));
  assert.ok(!/<script[^>]*src="\/pet\/splash\.js"/.test(html), '不用静态标签挡着');
  assert.ok(html.includes("e.data.t === 'workbench-capabilities'") && html.includes("host.addEventListener('message'"));
  assert.ok(!/stopPropagation|stopImmediatePropagation|preventDefault/.test(html), '只读监听: 不拦截、不改 workbench.js 收到的消息');
  assert.ok(html.includes('readTermSplash') && html.includes('TERM_SPLASH_LAST') && html.includes('splashEnabled'));
  assert.ok(html.includes('splashtest'));
  // 浏览器里 (没有令牌) 的判断用的正则和 splash.js 的 coldToken 一致: 令牌 0123 算令牌, 0 不算
  const re = /[?&]splash=(?!0(?:&|$))[^&]/;
  assert.ok(re.test('?splash=0123') && !re.test('?splash=0') && !re.test('?splash=0&x=1') && !re.test('?desk=0'));
});

test('桌宠窗口里的旧启动动画已清掉: pet.html / pet.js / panel-pet.js 里没有它; 设置页有新开关', () => {
  const petHtml = read('pet.html'), petJs = read('pet.js'), panel = read('panel-pet.js');
  assert.ok(!petHtml.includes('splash.js') && !petHtml.includes('splashEnabled') && !petHtml.includes('wm.splash.shown'));
  assert.ok(petHtml.includes('class="pet-splash"'), '原来的品牌图启动画面还在 (不是这次的东西)');
  for (const k of ['wm-pet-ready', 'wm-splash-change', 'wmSplash', '__wmMaskInfo']) assert.ok(!petJs.includes(k), `pet.js 里还有 ${k}`);
  assert.ok(!panel.includes('readPetSplash') && !panel.includes('writePetSplash') && !panel.includes("'启动动画'"));
  assert.ok(panel.includes('readTermSplash') && panel.includes('writeTermSplash') && panel.includes("'termsplash'") && panel.includes('启动画面'));
});

test('终端的色和字: 只在 workbench-shell.css 末尾追加深色变量 (中性近黑), 没有 ID 选择器和布局属性; 字体来自"系统"默认字体 (用户选了就照用户的)', () => {
  const css = read('workbench-shell.css');
  const i = css.indexOf('/* ---- 终端的色和字');
  assert.ok(i > 0);
  const block = css.slice(i);
  assert.ok(/html\[data-wm-theme="dark"\]\s*\{/.test(block));
  for (const v of ['--bg', '--side', '--panel', '--line', '--text', '--muted', '--faint']) assert.ok(block.includes(`${v}:`), v);
  assert.ok(!/#[A-Za-z]/.test(block.replace(/#[0-9a-fA-F]{3,6}\b/g, '').replace(/\/\*[\s\S]*?\*\//g, '')), '没有 ID 选择器');
  assert.ok(!/(width|height|margin|padding|display|position|grid|flex)\s*:/.test(block.replace(/\/\*[\s\S]*?\*\//g, '')), '不动布局');
  // 深色变量是中性的 (R≈G≈B, 不偏蓝)
  for (const m of block.matchAll(/--(bg|side|panel|field|line|line-2):\s*#([0-9a-f]{6})/g)) {
    const [r, g, b] = [0, 2, 4].map((k) => parseInt(m[2].slice(k, k + 2), 16));
    assert.ok(Math.max(r, g, b) - Math.min(r, g, b) <= 3, `${m[1]} 不够中性 #${m[2]}`);
  }
  const pref = read('ui-preferences.js');
  assert.ok(pref.includes('"Bahnschrift"') && pref.includes("wm-pet-surface"));
  assert.ok(pref.includes("yahei:'\"Microsoft YaHei UI\"") && pref.includes('customFont'));   // 用户选的字体还是用户的
});

test('"系统"字体: 终端和设置页先用 Bahnschrift, 桌宠窗口 (wm-pet-surface) 不变, 用户选了字体就照用户的', async () => {
  const mk = (cls) => {
    const style = new Map(), root = { dataset: {}, style: { setProperty: (k, v) => style.set(k, v), removeProperty: (k) => style.delete(k) } };
    return { style, doc: { documentElement: root, body: { classList: { contains: (c) => c === cls } } } };
  };
  const font = async (cls, prefs) => {
    const { style, doc } = mk(cls);
    const real = { document: globalThis.document, localStorage: globalThis.localStorage, window: globalThis.window, matchMedia: globalThis.matchMedia };
    globalThis.document = doc;
    globalThis.window = { dispatchEvent() {}, addEventListener() {}, removeEventListener() {} };
    globalThis.CustomEvent = class { constructor(t, o) { this.type = t; this.detail = o?.detail; } };
    globalThis.localStorage = { getItem: (k) => (k === P.STORAGE_KEY ? JSON.stringify(prefs) : null), setItem() {}, removeItem() {} };
    try { P.getPreferences(); P.updatePreferences({}); } finally { Object.assign(globalThis, real); if (!real.document) delete globalThis.document; if (!real.window) delete globalThis.window; }
    return style.get('--wm-ui-font');
  };
  assert.match(await font('wb2', {}), /^"Bahnschrift", "Segoe UI Variable Text"/);                      // 终端: 默认 ("系统")
  assert.match(await font('', { uiFont: 'system' }), /^"Bahnschrift"/);                                  // 设置页 (没有 wb2 也不是桌宠)
  assert.doesNotMatch(await font('wm-pet-surface', {}), /Bahnschrift/);                                  // 桌宠窗口不变
  assert.doesNotMatch(await font('wb2', { uiFont: 'yahei' }), /Bahnschrift/);                            // 用户选了雅黑
  assert.doesNotMatch(await font('wb2', { uiFont: 'segoe' }), /Bahnschrift/);
  assert.match(await font('wb2', { uiFont: 'custom', customFont: 'Consolas' }), /^"Consolas", "Bahnschrift"/);   // 自定义字体排在最前
});

test('start-pet.cmd: 加了 --workbench 和每次启动不同的令牌 (--workbench-query "splash=%RANDOM%%RANDOM%"), 原来的参数还在', () => {
  const cmd = readFileSync(process.env.SPLASH_CMD || path.join(here, '..', '..', 'desktop', 'start-pet.cmd'), 'utf8');   // 草稿期: SPLASH_CMD 指到草稿的 start-pet.cmd
  const line = cmd.split('\n').find((l) => l.startsWith('start ""'));
  assert.ok(line.includes('--server http://127.0.0.1:8790') && line.includes('--services') && line.includes('--workbench ') && line.includes('--workbench-query "splash=%RANDOM%%RANDOM%"'));
});
