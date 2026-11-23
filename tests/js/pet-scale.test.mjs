// 桌宠大小 (pet-scale) 的纯函数测试. 默认测 desktop/web 里的真文件; 草稿期用环境变量 PET_SCALE_WEB 指到别的 web 目录:
//   PET_SCALE_WEB=G:/RunProgram/WorldModel_v1/backups/pet-scale/run-new/desktop/web node --test tests/js/pet-scale.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { pathToFileURL, fileURLToPath } from 'node:url';
import path from 'node:path';

const here = path.dirname(fileURLToPath(import.meta.url));
const web = process.env.PET_SCALE_WEB || path.join(here, '..', '..', 'desktop', 'web');
const prefs = await import(pathToFileURL(path.join(web, 'ui-preferences.js')).href);
const avatar = await import(pathToFileURL(path.join(web, 'live2d-avatar.js')).href);

// Eustia 默认姿势下所有图元顶点的范围 (真 Core 量的, backups/pet-scale/measure_bounds.cjs)
const EUSTIA = { minX: -0.3454810678958893, maxX: 0.38390564918518066, minY: -1.319116234779358, maxY: 1.3673681020736694 };

// 改前的摆放算式, 原样抄自 live2d-avatar.js (11-15 急8 换入的版本 8d134685…)
function before({ minX, maxX, minY, maxY }, aspect, bubbleRoom, frame) {
  const low = frame === 'bust' ? maxY - (maxY - minY) * 0.42 : minY;
  const fitH = (2 * (1 - bubbleRoom) * 0.97) / (maxY - low), fitW = (1.92 * aspect) / (maxX - minX);
  const s = frame === 'bust' ? Math.min(fitH, fitW * 1.8) : Math.min(fitH, fitW);
  return { s, tx: -((minX + maxX) / 2) * s, ty: -0.995 - low * s,
    railY: -0.995 + (maxY - (maxY - minY) * 0.27 - low) * s, faceY: -0.995 + (maxY - (maxY - minY) * 0.11 - low) * s };
}

test('clampPetScale: 夹范围, 不是数字当 1', () => {
  const { clampPetScale: c, PET_SCALE_MIN } = prefs;
  assert.equal(PET_SCALE_MIN, 0.35);
  assert.equal(c(0.1), 0.35);
  assert.equal(c(-3), 0.35);
  assert.equal(c(0.6), 0.6);
  assert.equal(c(2), 1);            // 默认上限 1
  assert.equal(c(2, 1.34), 1.34);
  assert.equal(c(NaN), 1);
  assert.equal(c('abc'), 1);
  assert.equal(c(''), 1);
  assert.equal(c(null), 1);
  assert.equal(c(undefined), 1);
  assert.equal(c('0.5'), 0.5);
  assert.equal(c(Infinity), 1);
  assert.equal(c(0.9, 0.2), 0.35);  // 上限比下限还小: 以下限为准
});

test('petScaleMax: 默认窗口 = 1, 取整误差不让 100% 差一点, 窗口放大才大于 1', () => {
  const m = prefs.petScaleMax;
  assert.equal(m(800), 1);
  assert.equal(m(799.8), 1);
  assert.equal(m(1070), 1.34);
  assert.equal(m(4000), 1.5);
  assert.equal(m(500), 0.63);
  assert.equal(m(0), 1);
  assert.equal(m(NaN), 1);
  assert.equal(m(undefined), 1);
  assert.equal(m(100), 0.35);       // 窗口再矮也不低于下限
});

test('wheelPetScale: 向上变大、向下变小, 一格约 8%, 夹范围, 垃圾输入不动', () => {
  const w = prefs.wheelPetScale;
  const up = w(0.5, -100, 0, 1), down = w(0.5, 100, 0, 1);
  assert.ok(Math.abs(up / 0.5 - 1.0833) < 0.002, `up ${up}`);
  assert.ok(Math.abs(down / 0.5 - 0.9231) < 0.002, `down ${down}`);
  assert.equal(w(1, -100, 0, 1), 1);          // 已经最大
  assert.equal(w(0.35, 100, 0, 1), 0.35);     // 已经最小
  assert.ok(Math.abs(w(0.9, -100, 0, 1) - 0.975) < 0.001);   // 夹在上限以内: 0.9 * 1.0833
  assert.equal(w(0.97, -100, 0, 1), 1);       // 超过上限夹到 1
  assert.equal(w(0.5, NaN, 0, 1), 0.5);
  assert.equal(w(0.5, 0, 0, 1), 0.5);
  assert.equal(w(0.5, undefined, 0, 1), 0.5);
  // 行模式 (deltaMode 1) 按一行约 33 像素算; 页模式按 800
  assert.ok(Math.abs(w(0.5, -3, 1, 1) - w(0.5, -99, 0, 1)) < 0.001);
  assert.ok(w(0.5, -1, 2, 1) > w(0.5, -100, 0, 1));
  // 单次事件最多当 300 像素: 一下滚一万也只放大 exp(0.24) = 1.27 倍
  assert.ok(Math.abs(w(0.5, -10000, 0, 2) - 0.5 * Math.exp(300 * prefs.PET_SCALE_WHEEL)) < 1e-12);
  // 触控板捏合的小增量连续累积 (不会被取整卡住)
  let k = 0.5;
  for (let i = 0; i < 100; i++) k = w(k, -2, 0, 1);
  assert.ok(Math.abs(k - 0.5 * Math.exp(100 * 2 * prefs.PET_SCALE_WHEEL)) < 1e-9, `累积 ${k}`);
  // 反方向走回来基本回到原点
  for (let i = 0; i < 100; i++) k = w(k, 2, 0, 1);
  assert.ok(Math.abs(k - 0.5) < 1e-9, `回来 ${k}`);
});

test('petScaleFit: 传给门面的"占满窗口的比例", 最大 1', () => {
  const f = prefs.petScaleFit;
  assert.equal(f(1, 800), 1);
  assert.equal(f(0.5, 800), 0.5);
  assert.equal(f(0.35, 800), 0.35);
  assert.ok(Math.abs(f(1, 1070) - 800 / 1070) < 1e-12);   // 窗口放大了, 100% 还是原来那么大
  assert.equal(f(1.34, 1070), 1);                         // 窗口放得下的最大 = 占满
  assert.equal(f(1, 700), 1);                             // 窗口比默认矮: 撑满为止
  assert.equal(f(0.5, 0), 1);                             // 窗口高度未知: 不缩
  assert.equal(f(NaN, 800), 1);
});

test('readPetScale / writePetScale: 存取、垃圾值、没存过', () => {
  const real = globalThis.localStorage;
  try {
    const mem = new Map();
    globalThis.localStorage = { getItem: (k) => (mem.has(k) ? mem.get(k) : null), setItem: (k, v) => mem.set(k, String(v)), removeItem: (k) => mem.delete(k) };
    assert.equal(prefs.PET_SCALE_KEY, 'wm.pet.scale.v1');
    assert.equal(prefs.readPetScale(), 1);                       // 没存过
    assert.equal(prefs.writePetScale(0.6), true);
    assert.equal(mem.get('wm.pet.scale.v1'), '0.6');
    assert.equal(prefs.readPetScale(), 0.6);
    assert.equal(prefs.writePetScale(0.123456), true);
    assert.equal(mem.get('wm.pet.scale.v1'), '0.35');            // 夹到下限
    mem.set('wm.pet.scale.v1', 'garbage');
    assert.equal(prefs.readPetScale(), 1);
    mem.set('wm.pet.scale.v1', '9');
    assert.equal(prefs.readPetScale(), 1.5);                     // 读出来的夹到 1.5 (窗口放大后才有意义; 画的时候还会再夹)
  } finally { globalThis.localStorage = real; }
});

test('存储不可用 (抛异常或没有) 时读当 1, 写返回 false, 不抛', () => {
  const real = globalThis.localStorage;
  try {
    globalThis.localStorage = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
    assert.equal(prefs.readPetScale(), 1);
    assert.equal(prefs.writePetScale(0.5), false);
    delete globalThis.localStorage;
    assert.equal(prefs.readPetScale(), 1);
    assert.equal(prefs.writePetScale(0.5), false);
  } finally { if (real) globalThis.localStorage = real; }
});

test('追加导出没有碰原来的导出', () => {
  for (const name of ['STORAGE_KEY', 'WALLPAPER_KEY', 'DEFAULT_PREFERENCES', 'getPreferences', 'updatePreferences', 'initPreferences', 'getWallpaper', 'setWallpaperFile', 'clearWallpaper']) {
    assert.ok(name in prefs, `缺 ${name}`);
  }
  assert.equal(prefs.STORAGE_KEY, 'wm.ui.preferences.v1');
  assert.ok(!('petScale' in prefs.DEFAULT_PREFERENCES), '大小不进偏好项 (它是桌宠自己的)');
});

test('placeModel: r=1 时和改前的算式逐位相同 (整身、半身、几种画布比例)', () => {
  assert.equal(typeof avatar.placeModel, 'function');
  for (const frame of ['full', 'bust']) {
    for (const [W, H, room] of [[525, 1000, 0.16], [420, 800, 0.16], [560, 1070, 0.16], [300, 300, 0.02], [480, 640, 0.02]]) {
      const want = before(EUSTIA, W / H, room, frame);
      for (const got of [avatar.placeModel(EUSTIA, W / H, { bubbleRoom: room, frame, r: 1 }), avatar.placeModel(EUSTIA, W / H, { bubbleRoom: room, frame })]) {
        for (const key of ['s', 'tx', 'ty', 'railY', 'faceY']) assert.ok(Object.is(got[key], want[key]), `${frame} ${W}x${H} ${key}: ${got[key]} != ${want[key]}`);
      }
    }
  }
  // 真窗口: 缩放值 0.6066 (和 11-16 之前自检记录里的 fit 一致)
  assert.equal(avatar.placeModel(EUSTIA, 525 / 1000).fit.toFixed(4), '0.6066');
});

test('placeModel: r<1 绕脚底中点缩 (脚底、中线不动, 高度、栏杆、脸按 r 压缩)', () => {
  const aspect = 525 / 1000;
  const one = avatar.placeModel(EUSTIA, aspect);
  const top = (p) => EUSTIA.maxY * p.s + p.ty;
  for (const r of [0.9, 0.75, 0.5, 0.35, 0.1]) {
    const p = avatar.placeModel(EUSTIA, aspect, { r });
    assert.equal(p.fit, one.fit);
    assert.ok(Math.abs(p.s - one.s * r) < 1e-15);
    assert.ok(Math.abs(EUSTIA.minY * p.s + p.ty + 0.995) < 1e-12, `脚底 r=${r}`);                 // 脚底恒在 -0.995
    assert.ok(Math.abs((EUSTIA.minX + EUSTIA.maxX) / 2 * p.s + p.tx) < 1e-12, `中线 r=${r}`);      // 模型中线在画布中线
    assert.ok(Math.abs((top(p) + 0.995) - r * (top(one) + 0.995)) < 1e-12, `头顶 r=${r}`);         // 高度按 r
    assert.ok(Math.abs((p.railY + 0.995) - r * (one.railY + 0.995)) < 1e-12, `栏杆线 r=${r}`);
    assert.ok(Math.abs((p.faceY + 0.995) - r * (one.faceY + 0.995)) < 1e-12, `脸 r=${r}`);
  }
});

test('live2d-avatar.js: scale 默认 1 (工作台"桌前的她"不传它, 摆放不变); 没有新增 import', () => {
  const src = readFileSync(path.join(web, 'live2d-avatar.js'), 'utf8');
  assert.match(src, /export async function loadLive2DAvatar\(stage, dir, \{[^}]*scale = 1[^}]*\} = \{\}\)/);
  const imports = [...src.matchAll(/^import .* from '([^']+)';/gm)].map((m) => m[1]);
  assert.deepEqual(imports.sort(), ['./expression-caps.js', './expression-layer.js']);   // _PET_FILES 白名单里没有新文件
});
