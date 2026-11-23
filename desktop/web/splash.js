// 启动画面 (2026-11-17 term-step1; 用户: "打开桌宠就默认打开终端, 一开始是纯黑屏, 中心一束短光, 往两侧展开 S.A.T.I.O Verse 的白色字体").
// 独立模块: 没有 import, 不依赖任何别的文件, 不联网, 不用外部素材 -- 光、字、辉光都是这里的代码在一块 canvas 2D 上画的.
// 终端 (workbench.html) 在所有脚本之前动态 import 它; 以后进区、进世界的开场卡也用这一个 (传不同的 text).
//
// 画面 (秒, 从 startSplash 起算):
//   0.00-0.30  纯黑
//   0.30-0.64  正中亮起一点, 迅速拉成一小段水平的光 (宽约画面的 7%, 1.5 像素高, 白, 带柔和光晕和两端亮斑)
//   0.64-1.56  光向两侧匀滑展开 (先快后慢) 到字样的宽度; 白色细体字被光从中间向两边显出来 (边缘羽化, 字比光略落后)
//   1.56-2.42  字定住 (很淡的余辉), 光变细、淡出
//   2.42-      保持: 终端好了就收尾; 没好: 字下面一条极细的线慢慢呼吸, 4 秒起再加一行灰色小字 (statusText), 最长等 capS 秒
//   收尾 0.5   整块画布淡出 (CSS opacity), 露出终端; 画布移除
// 点一下画布或按任意键: 画面停在当时那一帧, 0.3 秒快速淡出. 减少动态效果: 字淡入 0.4 秒, 停到终端好 (至少 0.5 秒), 淡出 0.4 秒.

/** 字样: 改这一处 (或给 startSplash 传 text) 就全跟着变. 一个字符都不许改. */
export const SPLASH_TEXT = 'S.A.T.I.O Verse';
export const STATUS_TEXT = '正在准备终端…';

/** 时间线和上限都在这里 (用户看了再调). */
export const T = Object.freeze({
  capS: 20,                              // 终端一直没好, 最多等到这时候就放行
  statusAt: 4,                           // 等到这时候还没好: 字下面加一行灰色小字
  hold: 0.30,
  dotIn: 0.30, dotLen: 0.14,
  stretch0: 0.44, stretch1: 0.64,
  expand0: 0.64, expand1: 1.56,
  thin0: 1.32, thin1: 1.92,
  fade0: 2.42, fadeDur: 0.50,
  skipFade: 0.30,
  rIn: 0.40, rHoldMin: 0.50, rOut: 0.40,  // 减少动态效果
  frameMsLoading: 33, frameMsReady: 16,   // 终端还在加载时 30 帧 (主线程留给加载), 好了以后 60 帧
});
export const MIN_TOTAL = T.fade0 + T.fadeDur;  // 2.92 秒

const FONT = '"Segoe UI Variable Display", "Segoe UI Light", "Segoe UI", "SF Pro Display", "Helvetica Neue", "PingFang SC", "Microsoft YaHei UI", system-ui, sans-serif';

const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
const lerp = (a, b, k) => a + (b - a) * k;
export const easeOut = (u, p = 3) => 1 - Math.pow(1 - clamp(u), p);
export const easeInOut = (u) => { const x = clamp(u); return x * x * (3 - 2 * x); };

// ---------------------------------------------------------------------------------------------------------------------
// 纯函数 (node 里能测)

/** 地址里的冷启动令牌: ?splash=<令牌> (start-pet.cmd 每次启动生成); 没有 / 空 / 0 就是 null. */
export function coldToken(search = '') {
  const v = new URLSearchParams(search).get('splash');
  return v && v !== '0' ? v : null;
}

/** 减少动态效果: 偏好 'on' 或 (偏好不是 'off' 且系统要求减少). */
export const splashReduced = (pref, systemReduce) => pref === 'on' || (pref !== 'off' && !!systemReduce);

/**
 * 要不要放: 只有"冷启动令牌"不是上次放过的那个、设置里没关才放; 页面地址 splash=0 不放; 没有令牌 (浏览器里打开、界面实点测试、截图脚本、开机自启) 不放.
 * 自检 (splashtest=…) 照放, 不管令牌和设置.
 */
export function splashEnabled({ search = '', pref = true, last = null } = {}) {
  const q = new URLSearchParams(search);
  if (q.get('splash') === '0') return false;
  if (q.get('splashtest')) return true;
  const token = coldToken(search);
  if (!token) return false;
  return pref !== false && token !== last;
}

/**
 * 字样怎么排: 窗口 W x H (CSS 像素); measure(ch, px) = 一个字符在 px 号字时的宽度 (真画布里是 measureText, 测试里可以给替身).
 * 宽窗口字样占宽度的 62% (短字样 46%), 窄窗口 (竖屏) 占 84% / 76%; 字距放宽, 空格是一个词间距. 返回字号、各字符的左边位置 (相对字样左端)、宽度、半宽.
 */
export function layoutText(W, H, text, measure) {
  const probe = 100, track = 0.34 * probe, gap = 0.42 * probe, chars = [...text];
  const adv = chars.map((ch) => (ch === ' ' ? gap : measure(ch, probe)));
  const natural = adv.reduce((a, b) => a + b, 0) + track * Math.max(0, chars.length - 1);
  const narrow = W / H < 1.1, long = chars.length > 12;
  const target = W * (narrow ? (long ? 0.84 : 0.76) : (long ? 0.62 : 0.46));
  const fs = Math.min(probe * target / natural, H * 0.2), k = fs / probe;
  let x = 0;
  const xs = chars.map((ch, i) => { const at = x; x += adv[i] * k + track * k; return at; });
  const width = natural * k;
  return { fs, width, half: width / 2, xs, chars, tracking: track * k, narrow };
}

/** 光在 t 秒时的样子: { half: 半长 (CSS 像素), alpha, thick (粗细倍数), dot (亮点强度) }; textHalf = 字样半宽. 0.64–1.56 秒展开 (先快后慢). */
export function lightAt(t, W, textHalf) {
  const h0 = W * 0.035, hMax = textHalf * 1.1 + Math.max(18, W * 0.02);
  let half = 0, alpha = 0, thick = 1, dot = 0;
  if (t >= T.dotIn) {
    dot = easeOut((t - T.dotIn) / T.dotLen, 2);
    if (t < T.stretch0) { half = 1.2; alpha = dot; }
    else if (t < T.expand0) { half = lerp(1.2, h0, easeOut((t - T.stretch0) / (T.stretch1 - T.stretch0), 3)); alpha = 1; }
    else { half = lerp(h0, hMax, easeOut((t - T.expand0) / (T.expand1 - T.expand0), 3.4)); alpha = 1; }
  }
  if (t > T.thin0) {
    const u = clamp((t - T.thin0) / (T.thin1 - T.thin0));
    thick = lerp(1, 0.18, easeInOut(u)); alpha *= 1 - easeInOut(u);
  }
  return { half, alpha, thick, dot, h0, hMax };
}

/** 白字露出到离中线多远 (CSS 像素): 比光略落后. */
export const revealHalf = (half, W) => Math.max(0, half - (10 + W * 0.012));

/**
 * 现在是哪一段. t = 启动后的秒数; o = { readyAt (终端好的时刻, 没好 null), skipAt (点击/按键的时刻), reduced, capS }.
 * 返回 { phase: 'play' | 'hold' | 'fade' | 'done', tl (画面用的时间轴时刻, 保持/淡出时停在 fade0), u (淡出进度 0..1), holdT (保持了多久), fadeStart, fadeDur }.
 */
export function phaseAt(t, o = {}) {
  const cap = o.capS ?? T.capS, ready = o.readyAt ?? Infinity;
  const playEnd = o.reduced ? T.rIn + T.rHoldMin : T.fade0;
  let fadeStart = Math.min(cap, Math.max(playEnd, ready)), fadeDur = o.reduced ? T.rOut : T.fadeDur;
  if (o.skipAt != null && o.skipAt < fadeStart) { fadeStart = o.skipAt; fadeDur = T.skipFade; }
  const frozen = Math.min(fadeStart, o.reduced ? T.rIn : T.fade0);
  if (t >= fadeStart + fadeDur) return { phase: 'done', tl: frozen, u: 1, holdT: 0, fadeStart, fadeDur };
  if (t >= fadeStart) return { phase: 'fade', tl: frozen, u: (t - fadeStart) / fadeDur, holdT: Math.max(0, fadeStart - playEnd), fadeStart, fadeDur };
  if (t >= playEnd) return { phase: 'hold', tl: o.reduced ? T.rIn : T.fade0, u: 0, holdT: t - playEnd, fadeStart, fadeDur };
  return { phase: 'play', tl: t, u: 0, holdT: 0, fadeStart, fadeDur };
}

/** 等终端的时候下面那行灰色小字什么时候出 (t >= statusAt 且还在保持里). 返回 0..1 的淡入进度. */
export const statusAlpha = (t, ph) => (ph.phase === 'hold' && t >= T.statusAt ? easeInOut((t - T.statusAt) / 0.6) : 0);

// ---------------------------------------------------------------------------------------------------------------------
// 场景: 字、辉光预先画到离屏画布 (只在窗口大小/字样变化时画一次)
export function buildScene(doc, W, H, text = SPLASH_TEXT, dpr = 1) {
  const m = doc.createElement('canvas').getContext('2d');
  m.font = `300 100px ${FONT}`;
  const L = layoutText(W, H, text, (ch) => m.measureText(ch).width);
  const fs = L.fs, tw = Math.ceil(L.width + fs * 1.2), th = Math.ceil(fs * 2.4);
  const T0 = doc.createElement('canvas'); T0.width = Math.ceil(tw * dpr); T0.height = Math.ceil(th * dpr);
  const g = T0.getContext('2d'); g.scale(dpr, dpr);
  g.font = `300 ${fs}px ${FONT}`; g.textBaseline = 'middle'; g.fillStyle = '#fff';
  const left = (tw - L.width) / 2;
  L.chars.forEach((ch, i) => { if (ch !== ' ') g.fillText(ch, left + L.xs[i], th / 2 + fs * 0.04); });
  const G0 = doc.createElement('canvas'); G0.width = T0.width; G0.height = T0.height;
  const gg = G0.getContext('2d');
  if ('filter' in gg) { gg.filter = `blur(${(fs * 0.16 * dpr).toFixed(1)}px)`; gg.drawImage(T0, 0, 0); gg.filter = 'none'; gg.globalCompositeOperation = 'lighter'; gg.drawImage(G0, 0, 0); }
  const S0 = doc.createElement('canvas'); S0.width = T0.width; S0.height = T0.height;
  return { W, H, dpr, text, L, tw, th, T0, G0, S0, sg: S0.getContext('2d'), cx: W / 2, cy: H / 2 };
}

/** 画一帧 (纯画). ph = phaseAt 的结果, t = 启动后的秒数 (保持里的呼吸、小字用), opts = { reduced, statusText }. 返回整体不透明度 0..1 (淡出时由调用方设给 CSS opacity). */
export function drawFrame(ctx, sc, t, ph, opts = {}) {
  const { dpr, W, H, cx, cy } = sc;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.globalCompositeOperation = 'source-over';
  ctx.globalAlpha = 1;
  ctx.fillStyle = '#000';
  ctx.fillRect(0, 0, W * dpr, H * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const opacity = ph.phase === 'fade' ? 1 - easeInOut(ph.u) : 1;
  const tl = ph.tl;
  if (opts.reduced) {  // 只有字的淡入
    const a = easeInOut(tl / T.rIn);
    ctx.globalAlpha = a * 0.3; ctx.drawImage(sc.G0, 0, 0, sc.G0.width, sc.G0.height, cx - sc.tw / 2, cy - sc.th / 2, sc.tw, sc.th);
    ctx.globalAlpha = a; ctx.drawImage(sc.T0, 0, 0, sc.T0.width, sc.T0.height, cx - sc.tw / 2, cy - sc.th / 2, sc.tw, sc.th);
    ctx.globalAlpha = 1;
    if (statusAlpha(t, ph) > 0) drawStatus(ctx, sc, statusAlpha(t, ph), opts.statusText || STATUS_TEXT);
    return opacity;
  }
  const { half, alpha, thick, dot } = lightAt(tl, W, sc.L.half);
  // 字: 光经过的地方白字从中间向两边显出来
  const reveal = revealHalf(half, W);
  if (reveal > 0.5) {
    const settle = easeOut((tl - T.expand1) / 0.7, 2);
    const glow = tl < T.expand1 ? 0.30 : lerp(0.46, 0.17, settle);
    const { sg, T0, G0, S0 } = sc, sw = S0.width, sh = S0.height;
    sg.setTransform(1, 0, 0, 1, 0, 0);
    sg.globalCompositeOperation = 'source-over'; sg.globalAlpha = 1;
    sg.clearRect(0, 0, sw, sh);
    sg.globalAlpha = glow; sg.drawImage(G0, 0, 0);
    sg.globalAlpha = 1; sg.drawImage(T0, 0, 0);
    const f = clamp(W * 0.03, 14, 40) * dpr, rv = reveal * dpr, mid = sw / 2, x0 = mid - rv, x1 = mid + rv;
    const gr = sg.createLinearGradient(x0, 0, x1, 0), e = clamp(f / Math.max(1, x1 - x0), 0, 0.5);
    gr.addColorStop(0, 'rgba(0,0,0,0)'); gr.addColorStop(e, 'rgba(0,0,0,1)'); gr.addColorStop(1 - e, 'rgba(0,0,0,1)'); gr.addColorStop(1, 'rgba(0,0,0,0)');
    sg.globalCompositeOperation = 'destination-in';
    sg.fillStyle = gr; sg.fillRect(0, 0, sw, sh);
    ctx.drawImage(S0, 0, 0, sw, sh, cx - sc.tw / 2, cy - sc.th / 2, sc.tw, sc.th);
  }
  // 光: 白色核心 (一两个像素高) + 柔和光晕 + 两端亮斑
  if (alpha > 0.002 && half > 0.1) {
    ctx.globalCompositeOperation = 'lighter';
    const halo = (hh, a) => {
      ctx.save(); ctx.translate(cx, cy); ctx.scale(Math.max(half * 1.06, 3), hh);
      const rg = ctx.createRadialGradient(0, 0, 0, 0, 0, 1);
      rg.addColorStop(0, `rgba(255,255,255,${(0.55 * a * alpha).toFixed(3)})`); rg.addColorStop(0.28, `rgba(255,255,255,${(0.20 * a * alpha).toFixed(3)})`); rg.addColorStop(1, 'rgba(255,255,255,0)');
      ctx.fillStyle = rg; ctx.fillRect(-1, -1, 2, 2); ctx.restore();
    };
    halo(34 * (0.45 + 0.55 * thick), 0.35); halo(12 * (0.5 + 0.5 * thick), 0.6); halo(4.5, 0.9 * thick + 0.1);
    const cw = Math.max(2, half * 2), ch = Math.max(1, 1.6 * thick), core = ctx.createLinearGradient(cx - half, 0, cx + half, 0), edge = Math.min(0.3, 14 / cw * 4);
    core.addColorStop(0, 'rgba(255,255,255,0)'); core.addColorStop(edge, `rgba(255,255,255,${alpha.toFixed(3)})`);
    core.addColorStop(1 - edge, `rgba(255,255,255,${alpha.toFixed(3)})`); core.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = core; ctx.fillRect(cx - half, cy - ch / 2, cw, ch);
    const bloom = (x, r, a) => { const rg = ctx.createRadialGradient(x, cy, 0, x, cy, r); rg.addColorStop(0, `rgba(255,255,255,${(a * alpha).toFixed(3)})`); rg.addColorStop(1, 'rgba(255,255,255,0)'); ctx.fillStyle = rg; ctx.fillRect(x - r, cy - r, r * 2, r * 2); };
    if (tl < T.expand0 + 0.1) bloom(cx, 5 + 7 * dot, 0.9);
    if (tl >= T.stretch0) { const a = 0.5 * (tl < T.expand1 ? 1 : 0.5); bloom(cx - half, 12, a); bloom(cx + half, 12, a); }
    ctx.globalCompositeOperation = 'source-over';
  }
  // 保持 (等终端): 字下面一条极细的线慢慢呼吸; 4 秒起再加一行灰色小字
  if (ph.phase === 'hold') {
    const y = cy + sc.L.fs * 1.15, w = sc.L.half * 0.9, br = 0.5 + 0.5 * Math.sin((2 * Math.PI * ph.holdT) / 2.6), a = (0.10 + 0.20 * br) * easeInOut(ph.holdT / 0.5);
    const gl = ctx.createLinearGradient(cx - w, 0, cx + w, 0);
    gl.addColorStop(0, 'rgba(255,255,255,0)'); gl.addColorStop(0.5, `rgba(255,255,255,${a.toFixed(3)})`); gl.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = gl; ctx.fillRect(cx - w, y, w * 2, 1);
    if (statusAlpha(t, ph) > 0) drawStatus(ctx, sc, statusAlpha(t, ph), opts.statusText || STATUS_TEXT);
  }
  return opacity;
}

function drawStatus(ctx, sc, a, text) {
  ctx.globalAlpha = 0.42 * a;
  ctx.fillStyle = '#fff';
  ctx.font = `300 ${Math.max(11, sc.L.fs * 0.3).toFixed(1)}px ${FONT}`;
  ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
  ctx.fillText(text, sc.cx, sc.cy + sc.L.fs * 1.9);
  ctx.globalAlpha = 1; ctx.textAlign = 'left';
}

// ---------------------------------------------------------------------------------------------------------------------
/**
 * 启动画面. opts: { doc, win, text, statusText, enabled, reduced, capS, onEvent, now, raf, caf, setTimeout, clearTimeout }.
 * 返回 { active(), ready(), skip(), destroy(), done (Promise), log, frames }. ready(): 终端好了 (也可以派发 window 事件 wm-term-ready).
 * 不启动 (enabled 假 / 没有 body) 时什么都不建、不挂监听, 返回的方法都是空操作. 结束时清干净 (取消 rAF/定时器、移除画布、拆监听).
 */
export function startSplash(opts = {}) {
  const o = {
    doc: globalThis.document, win: globalThis, text: SPLASH_TEXT, statusText: STATUS_TEXT, enabled: true, reduced: false, capS: T.capS, onEvent: null,
    now: () => performance.now(), raf: (f) => requestAnimationFrame(f), caf: (id) => cancelAnimationFrame(id),
    setTimeout: (f, ms) => setTimeout(f, ms), clearTimeout: (id) => clearTimeout(id), ...opts,
  };
  const log = [];
  const idle = { active: () => false, ready() {}, skip() {}, destroy() {}, done: Promise.resolve(log), log, frames: 0 };
  const doc = o.doc, win = o.win;
  if (!o.enabled || !doc || !doc.body) return idle;

  const W = win.innerWidth || 1280, H = win.innerHeight || 800, dpr = Math.min(win.devicePixelRatio || 1, 2);
  const canvas = doc.createElement('canvas');
  canvas.width = Math.round(W * dpr); canvas.height = Math.round(H * dpr);
  canvas.style.cssText = 'position:fixed;left:0;top:0;width:100%;height:100%;z-index:2147483000;background:#000;touch-action:manipulation;cursor:pointer;will-change:opacity';
  canvas.setAttribute?.('aria-label', o.text);
  canvas.setAttribute?.('role', 'img');
  const ctx = canvas.getContext('2d');
  let scene = buildScene(doc, W, H, o.text, dpr);
  const t0 = o.now();
  const st = { readyAt: win.__wmTermReady ? 0 : null, skipAt: null, reduced: !!o.reduced, capS: o.capS };
  let rafId = 0, timerId = 0, dead = false, lastDraw = -1e9, frames = 0, entered = '', resolveDone;
  const done = new Promise((r) => { resolveDone = r; });
  const ev = (name, t) => { log.push({ name, t: +t.toFixed(3) }); o.onEvent?.(name, t); };

  const onSkip = () => { if (st.skipAt == null && !dead) { const t = (o.now() - t0) / 1000; const ph = phaseAt(t, st); if (ph.phase === 'play' || ph.phase === 'hold') { st.skipAt = t; ev('skip', t); } } };
  const onReady = () => { if (st.readyAt == null) { st.readyAt = (o.now() - t0) / 1000; ev('ready', st.readyAt); } };
  canvas.addEventListener('pointerdown', onSkip);
  doc.addEventListener('keydown', onSkip, true);
  win.addEventListener('wm-term-ready', onReady);
  doc.body.append(canvas);
  ev('start', 0);

  const destroy = () => {
    if (dead) return;
    dead = true;
    if (rafId) o.caf(rafId);
    rafId = 0;
    if (timerId) o.clearTimeout(timerId);
    timerId = 0;
    o.clearTimeout(watchdog);
    canvas.removeEventListener('pointerdown', onSkip);
    doc.removeEventListener('keydown', onSkip, true);
    win.removeEventListener('wm-term-ready', onReady);
    canvas.remove();
    canvas.width = canvas.height = 0;                       // 位图马上还给系统, 不等垃圾回收
    scene.G0.width = scene.G0.height = scene.T0.width = scene.T0.height = scene.S0.width = scene.S0.height = 0;
    scene = null;
    ev('end', (o.now() - t0) / 1000);
    resolveDone(log);
  };
  // 页面一直藏着 (rAF 不跑) 也别永远留着: 比最长时间线多留 3 秒就收掉
  const watchdog = o.setTimeout(destroy, (o.capS + T.fadeDur + 3) * 1000);

  const frame = () => {
    rafId = 0;
    if (dead) return;
    const nowMs = o.now(), t = (nowMs - t0) / 1000;
    const ph = phaseAt(t, st);
    if (ph.phase === 'done') { destroy(); return; }          // 藏了一阵再显示: 该结束就直接收尾, 不补放过程
    if (ph.phase !== entered && (ph.phase === 'hold' || ph.phase === 'fade')) { entered = ph.phase; ev(ph.phase, t); }
    if (nowMs - lastDraw >= (st.readyAt == null ? T.frameMsLoading : T.frameMsReady) - 3 || ph.phase === 'fade') {
      lastDraw = nowMs;
      const op = drawFrame(ctx, scene, t, ph, { reduced: st.reduced, statusText: o.statusText });
      canvas.style.opacity = String(op);
      frames++;
    }
    next();
  };
  // 下一帧: 先用定时器等到该画的时候再要 rAF (30 帧的动画不要让页面每个垂直同步都跑一遍渲染流程, 终端在加载)
  const next = () => {
    const wait = (st.readyAt == null ? T.frameMsLoading : T.frameMsReady) - (o.now() - lastDraw) - 4;
    if (wait > 2) timerId = o.setTimeout(() => { timerId = 0; if (!dead) rafId = o.raf(frame); }, wait);
    else rafId = o.raf(frame);
  };
  rafId = o.raf(frame);
  return { active: () => !dead, ready: onReady, skip: onSkip, destroy, done, log, get frames() { return frames; } };
}

/** 手动渲染某一时刻 (出过程截图、自检用): 返回 canvas (doc 建的). state = { readyAt, skipAt, reduced }; 画面的时间轴时刻 = t. */
export function renderFrameAt(doc, W, H, t, state = {}, dpr = 1, text = SPLASH_TEXT) {
  const sc = buildScene(doc, W, H, text, dpr);
  const c = doc.createElement('canvas');
  c.width = Math.round(W * dpr); c.height = Math.round(H * dpr);
  const ctx = c.getContext('2d');
  const ph = phaseAt(t, state);
  const op = drawFrame(ctx, sc, t, ph, { reduced: !!state.reduced });
  c.style.opacity = String(op);
  return c;
}

/**
 * 页面自检 (终端页面地址带 splashtest=模式 时, workbench.html 的启动脚本调; 开发版宿主里看, 结果写进宿主日志):
 *   life 真实时间跑一遍 (终端好了会收到 wm-term-ready) | cap 忽略"终端好了", 上限 1.5 秒 | skip 0.6 秒时派发真的 pointerdown | reduced 减少动态效果
 *   hold 停在保持里不结束 | frames 手动步进出过程截图 (窄窗口 420x800 和宽窗口 1280x800), 结果 PNG dataURL 放在 window.__splashFrames
 * log(text): 把结果发给宿主日志 (没有宿主就只 console.log).
 */
export function runSplashTest(mode, { log = () => {}, doc = document, win = window, reduced = false } = {}) {
  const t0 = performance.now();
  if (mode === 'frames') {
    const times = [0.15, 0.40, 0.80, 1.30, 1.95, 2.60, 3.60, 4.60, 5.50];
    const out = {};
    for (const [w, h] of [[420, 800], [1280, 800]]) {
      out[`${w}x${h}`] = {};
      for (const t of times) out[`${w}x${h}`][t] = renderFrameAt(doc, w, h, t, { readyAt: t >= 4.6 ? 5.0 : null }, 1).toDataURL('image/png');
    }
    win.__splashFrames = out;
    log(`splashtest frames done ${Object.keys(out).length}`);
    return null;
  }
  const opts = { doc, win, reduced: mode === 'reduced' || reduced, onEvent: (name, t) => log(`splash ${name} ${t.toFixed(3)} perf ${Math.round(performance.now() - t0)}`) };
  if (mode === 'cap') Object.assign(opts, { capS: 1.5 });
  if (mode === 'hold') Object.assign(opts, { capS: 1e6 });
  const c = startSplash(opts);
  win.__splash = c;
  if (mode === 'skip') setTimeout(() => doc.querySelector('canvas[role="img"]')?.dispatchEvent(new win.PointerEvent('pointerdown', { bubbles: true, button: 0 })), 600);
  c.done.then(() => log(`splash done frames ${c.frames} total ${Math.round(performance.now() - t0)}ms`));
  return c;
}
