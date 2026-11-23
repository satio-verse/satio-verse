// Shared browser preferences for the workbench and settings iframe.
// Only presentation and interaction options belong here; no account or model credentials.
export const STORAGE_KEY = 'wm.ui.preferences.v1';
export const WALLPAPER_KEY = 'wm.ui.wallpaper.v1';
export const DEFAULT_PREFERENCES = Object.freeze({
  theme: 'system', themePreset: 'lilac', uiFontSize: 14, contentFontSize: 14,
  codeFont: 'system', reducedMotion: 'system', sidebarTranslucent: false,
  contentWidth: 'standard', sendKey: 'enter', showSessionTimes: true,
  expandToolDetails: false, notifyDone: 'background', notifyApproval: true,
  notificationSound: false,
  material: 'glass', glassBlur: 24, glassOpacity: 82, wallpaperDim: 18,
  uiFont: 'system', customFont: '', colorSource: 'preset', seedColor: '#8171c8',
});
const OPTIONS = {
  theme: ['system', 'light', 'dark'], themePreset: ['neutral', 'lilac', 'blue', 'forest'],
  uiFontSize: [13, 14, 15, 16], contentFontSize: [14, 15, 16, 18],
  codeFont: ['system', 'consolas'], reducedMotion: ['system', 'on', 'off'],
  contentWidth: ['compact', 'standard', 'wide'], sendKey: ['enter', 'ctrlEnter'],
  notifyDone: ['all', 'background', 'off'],
  material: ['glass', 'solid'], uiFont: ['system', 'yahei', 'segoe', 'custom'],
  colorSource: ['preset', 'wallpaper', 'custom'],
};
const WIDTHS = { compact: 680, standard: 760, wide: 960 };
let current = { ...DEFAULT_PREFERENCES };
let dispose = null;

function normalize(input, base = DEFAULT_PREFERENCES) {
  const next = { ...base };
  if (!input || typeof input !== 'object' || Array.isArray(input)) return next;
  for (const key of Object.keys(DEFAULT_PREFERENCES)) {
    const value = input[key];
    if (typeof DEFAULT_PREFERENCES[key] === 'boolean') {
      if (typeof value === 'boolean') next[key] = value;
    } else if (['glassBlur', 'glassOpacity', 'wallpaperDim'].includes(key)) {
      const [min, max] = key === 'glassBlur' ? [0, 48] : key === 'glassOpacity' ? [62, 100] : [0, 70];
      if (typeof value === 'number' && Number.isFinite(value)) next[key] = Math.round(Math.max(min, Math.min(max, value)));
    } else if (key === 'customFont' && typeof value === 'string') next[key] = value.replace(/[\x00-\x1f]/g, '').trim().slice(0, 80);
    else if (key === 'seedColor' && typeof value === 'string' && /^#[\da-f]{6}$/i.test(value)) next[key] = value.toLowerCase();
    else if (OPTIONS[key]?.includes(value)) next[key] = value;
  }
  return next;
}

function readStored() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? normalize(JSON.parse(raw)) : { ...DEFAULT_PREFERENCES };
  } catch { return { ...current }; }
}

const matches = (query) => typeof matchMedia === 'function' && matchMedia(query).matches;
export function getPreferences() { return { ...readStored() }; }

function apply(preferences, source, persisted = true) {
  current = preferences;
  const root = document.documentElement;
  const resolvedTheme = preferences.theme === 'system'
    ? (matches('(prefers-color-scheme: dark)') ? 'dark' : 'light') : preferences.theme;
  const reducedMotion = preferences.reducedMotion === 'on'
    || (preferences.reducedMotion === 'system' && matches('(prefers-reduced-motion: reduce)'));
  root.dataset.wmTheme = resolvedTheme;
  root.dataset.wmThemeChoice = preferences.theme;
  root.dataset.wmPreset = preferences.themePreset;
  root.dataset.wmMotion = reducedMotion ? 'reduce' : 'full';
  root.dataset.wmTranslucent = String(preferences.sidebarTranslucent);
  root.dataset.wmSessionTimes = String(preferences.showSessionTimes);
  root.dataset.wmCodeFont = preferences.codeFont;
  root.dataset.wmMaterial = preferences.material;
  root.dataset.wmColorSource = preferences.colorSource;
  root.style.setProperty('--wm-glass-blur', `${preferences.glassBlur}px`);
  root.style.setProperty('--wm-glass-opacity', `${preferences.glassOpacity}%`);
  root.style.setProperty('--wm-wallpaper-dim', String(preferences.wallpaperDim / 100));
  const terminalSystem = typeof document === 'undefined' || !document.body?.classList.contains('wm-pet-surface') ? '"Bahnschrift", ' : '';  // 终端和设置页的"系统"字体先用 Bahnschrift (没有的机器回落到后面), 桌宠窗口不变
  const fonts = {system:terminalSystem + '"Segoe UI Variable Text", "Segoe UI", "Microsoft YaHei UI", system-ui, sans-serif', yahei:'"Microsoft YaHei UI", "Microsoft YaHei", sans-serif',segoe:'"Segoe UI Variable Text", "Segoe UI", sans-serif'};
  root.style.setProperty('--wm-ui-font', preferences.uiFont === 'custom' && preferences.customFont ? `${JSON.stringify(preferences.customFont)}, ${fonts.system}` : fonts[preferences.uiFont] || fonts.system);
  // Keep light and dark text contrast independent of the seed's luminance.
  const palette = seededPalette(preferences.seedColor, resolvedTheme === 'dark');
  for (const [key, value] of Object.entries(palette)) {
    if (preferences.colorSource === 'preset') root.style.removeProperty(key);
    else root.style.setProperty(key, value);
  }
  applyWallpaper();
  root.style.setProperty('--wm-ui-font-size', `${preferences.uiFontSize}px`);
  root.style.setProperty('--wm-ui-small', `${Math.max(12, preferences.uiFontSize - 1)}px`);
  root.style.setProperty('--wm-ui-caption', `${Math.max(11, preferences.uiFontSize - 2)}px`);
  root.style.setProperty('--wm-content-font-size', `${preferences.contentFontSize}px`);
  root.style.setProperty('--wm-content-width', `${WIDTHS[preferences.contentWidth]}px`);
  window.dispatchEvent(new CustomEvent('wm-preferences-changed', {
    detail: { preferences: { ...preferences }, source, persisted, resolvedTheme, reducedMotion },
  }));
}

/** Save a validated patch, apply it immediately, and notify local subscribers.
 * Other same-origin windows/iframes receive the browser's storage event.
 * Throws on storage failure so the caller never claims an unsaved change was saved.
 */
export function updatePreferences(patch) {
  const next = normalize(patch, readStored());
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(next)); }
  catch { throw new Error('无法保存偏好设置，请确认此窗口允许使用本地存储。'); }
  apply(next, 'local');
  return { ...next };
}

/** Safe to call more than once in the same document. Returns a cleanup function. */
export function initPreferences() {
  if (dispose) return dispose;
  const onStorage = (event) => {
    if (event.key === STORAGE_KEY || event.key === WALLPAPER_KEY || event.key === null) apply(readStored(), 'storage');
  };
  const onSystem = () => apply(readStored(), 'system');
  // The page itself never zooms: text size comes from uiFontSize / contentFontSize. Ctrl+wheel, touchpad pinch
  // (Chromium reports it as ctrl+wheel) and Ctrl +/- are swallowed here because the released host still lets
  // WebView2 zoom (the zoom lock in webview_host.cpp is not shipped yet). Ctrl+0 stays free so an already
  // zoomed window can be reset; elements that zoom their own content (the room camera) still get the wheel.
  const onWheel = (event) => { if (event.ctrlKey) event.preventDefault(); };
  const onKey = (event) => {
    if ((event.ctrlKey || event.metaKey) && ['+', '=', '-', '_'].includes(event.key)) event.preventDefault();
  };
  const media = typeof matchMedia === 'function'
    ? [matchMedia('(prefers-color-scheme: dark)'), matchMedia('(prefers-reduced-motion: reduce)')] : [];
  window.addEventListener('storage', onStorage);
  window.addEventListener('wheel', onWheel, { passive: false });
  window.addEventListener('keydown', onKey);
  for (const item of media) item.addEventListener('change', onSystem);
  dispose = () => {
    window.removeEventListener('storage', onStorage);
    window.removeEventListener('wheel', onWheel);
    window.removeEventListener('keydown', onKey);
    for (const item of media) item.removeEventListener('change', onSystem);
    dispose = null;
  };
  apply(readStored(), 'init');
  return dispose;
}

function seededPalette(hex, dark) {
  const r=parseInt(hex.slice(1,3),16)/255,g=parseInt(hex.slice(3,5),16)/255,b=parseInt(hex.slice(5,7),16)/255;
  const max=Math.max(r,g,b),min=Math.min(r,g,b),delta=max-min;
  let hue=0;if(delta)hue=(max===r?(g-b)/delta+(g<b?6:0):max===g?(b-r)/delta+2:(r-g)/delta+4)*60;
  const sat=Math.max(26,Math.min(58,Math.round(delta*100+28)));
  const c=(s,l)=>{
    // A bright yellow/green seed must not produce white-on-bright action labels.
    const whiteContrast=(light)=>{
      const h=hue/30,saturation=s/100,L=light/100,a=saturation*Math.min(L,1-L);
      const channel=n=>{const k=(n+h)%12;const v=L-a*Math.max(-1,Math.min(k-3,9-k,1));return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;};
      return 1.05/(.2126*channel(0)+.7152*channel(8)+.0722*channel(4)+.05);
    };
    if(!dark&&l<50)while(l>20&&whiteContrast(l)<5)l--;
    return `hsl(${Math.round(hue)} ${s}% ${l}%)`;
  };
  return {'--accent':c(sat,dark?76:43),'--accent-ink':c(sat,dark?84:36),'--accent-soft':c(28,dark?21:94),'--b1':c(45,dark?54:76),'--b2':`hsl(${Math.round((hue+35)%360)} 42% ${dark?60:81}%)`};
}

export function getWallpaper() {
  try { const value=localStorage.getItem(WALLPAPER_KEY)||'';return /^data:image\/jpeg;base64,[A-Za-z0-9+/=]+$/.test(value)?value:''; } catch { return ''; }
}
function applyWallpaper() {
  const image=getWallpaper(),root=document.documentElement;
  root.dataset.wmWallpaper=String(!!image);
  if(image)root.style.setProperty('--wm-wallpaper',`url("${image}")`);else root.style.removeProperty('--wm-wallpaper');
}
/** Local raster only; downsample before persistence. No image leaves the device. */
export async function setWallpaperFile(file) {
  if(!file || !['image/jpeg','image/png','image/webp'].includes(file.type))throw Error('请选择 JPG、PNG 或 WebP 图片。');
  if(file.size>20*1024*1024)throw Error('请选择不超过 20 MB 的图片。');
  const objectUrl=URL.createObjectURL(file),image=new Image();
  try {
    image.src=objectUrl;await image.decode();
    const scale=Math.min(1,1600/Math.max(image.naturalWidth,image.naturalHeight));
    const canvas=document.createElement('canvas');canvas.width=Math.max(1,Math.round(image.naturalWidth*scale));canvas.height=Math.max(1,Math.round(image.naturalHeight*scale));
    const ctx=canvas.getContext('2d');ctx.fillStyle='#eeeeee';ctx.fillRect(0,0,canvas.width,canvas.height);ctx.drawImage(image,0,0,canvas.width,canvas.height);
    const encoded=canvas.toDataURL('image/jpeg',.82);
    const sample=document.createElement('canvas');sample.width=48;sample.height=48;
    const sampleCtx=sample.getContext('2d',{willReadFrequently:true});sampleCtx.drawImage(canvas,0,0,48,48);
    const pixels=sampleCtx.getImageData(0,0,48,48).data,buckets=new Map();
    for(let i=0;i<pixels.length;i+=4){const r=pixels[i],g=pixels[i+1],b=pixels[i+2],v=Math.max(r,g,b),m=Math.min(r,g,b);if(v<35||m>230)continue;const key=[r>>5,g>>5,b>>5].join(',');const item=buckets.get(key)||{count:0,r:0,g:0,b:0};const weight=1+(v-m)/60;item.count+=weight;item.r+=r*weight;item.g+=g*weight;item.b+=b*weight;buckets.set(key,item);}
    const main=[...buckets.values()].sort((a,b)=>b.count-a.count)[0];
    const seed=main?'#'+['r','g','b'].map(key=>Math.round(main[key]/main.count).toString(16).padStart(2,'0')).join(''):'#8171c8';
    const previous=getWallpaper();
    try {localStorage.setItem(WALLPAPER_KEY,encoded);updatePreferences({seedColor:seed,colorSource:'wallpaper',material:'glass'});}
    catch(error){try{if(previous)localStorage.setItem(WALLPAPER_KEY,previous);else localStorage.removeItem(WALLPAPER_KEY);}catch{}throw Error('壁纸无法保存，本机存储空间可能不足。');}
    return {seedColor:seed,bytes:encoded.length};
  } finally {URL.revokeObjectURL(objectUrl);}
}
export function clearWallpaper() {
  try{localStorage.removeItem(WALLPAPER_KEY);}catch{throw Error('暂时无法移除壁纸。');}
  apply(readStored(),'wallpaper');
}

// ---- 桌宠大小 (2026-11-16 pet-scale) ----
// 存的是相对"默认窗口高 PET_SCALE_REF_H"的比例 k (1 = 现在的样子). 桌宠页面 (pet.js) 和设置页 (panel-pet.js) 共用这一份存储,
// 两边同源、共用 WebView2 环境, 设置页改了桌宠马上收到 storage 事件. 读写都不抛: 存储不可用就当 1.
export const PET_SCALE_KEY = 'wm.pet.scale.v1';
export const PET_SCALE_MIN = 0.35;
export const PET_SCALE_REF_H = 800;  // 宿主默认窗口高 (CSS 像素, desktop/host/src/app.h 的 height)
export const PET_SCALE_WHEEL = 0.0008;  // 滚轮一格 (100 像素) 约 8%

/** 夹进 [PET_SCALE_MIN, max]; 不是有限数字就当 1 (再夹). */
export function clampPetScale(k, max = 1) {
  const n = typeof k === 'number' ? k : typeof k === 'string' && k.trim() ? Number(k) : NaN;
  const v = Number.isFinite(n) ? n : 1;
  return Math.max(PET_SCALE_MIN, Math.min(Math.max(PET_SCALE_MIN, max), v));
}

/** 窗口 CSS 高度放得下的最大比例: 默认窗口 = 1; 宿主以后把窗口放大了才会大于 1. 取到 0.01, 免得 799.8/800 这类取整误差让"100%"差一点. */
export function petScaleMax(innerH) {
  if (!(innerH > 0)) return 1;
  return Math.max(PET_SCALE_MIN, Math.min(1.5, Math.round((innerH / PET_SCALE_REF_H) * 100) / 100));
}

/** 滚轮 (ctrl+滚轮、触控板捏合) 一次事件后的新比例; deltaY<0 (向上/张开) 变大. 单次事件最多当 300 像素用. */
export function wheelPetScale(k, deltaY, deltaMode = 0, max = 1) {
  const px = (deltaMode === 1 ? deltaY * 33 : deltaMode === 2 ? deltaY * 800 : deltaY) || 0;
  const next = k * Math.exp(-Math.max(-300, Math.min(300, px)) * PET_SCALE_WHEEL);
  return clampPetScale(next, max);  // 不取整: 捏合的小增量每次不到 0.1%, 取整会把它们吃掉或者攒成偏差 (存的时候 writePetScale 才取到 0.001)
}

/** 比例 -> 传给门面的"占满窗口的比例" r (<= 1). */
export function petScaleFit(k, innerH) {
  if (!(innerH > 0)) return 1;
  return Math.min(1, clampPetScale(k, 1.5) * PET_SCALE_REF_H / innerH);
}

export function readPetScale() {
  try {
    const raw = localStorage.getItem(PET_SCALE_KEY);
    return raw === null ? 1 : clampPetScale(Number(raw), 1.5);
  } catch { return 1; }
}

/** 存下来; 存储不可用时返回 false (调用方照样用内存里的值, 只是重开不记得). */
export function writePetScale(k) {
  const v = Math.round(clampPetScale(k, 1.5) * 1000) / 1000;
  try { localStorage.setItem(PET_SCALE_KEY, String(v)); return true; } catch { return false; }
}

// ---- 终端启动画面 (2026-11-17 term-step1) ----
// 设置页 (panel-pet.js) 的"启动画面"开关: 默认开, 存 '0' 才关; 新的键 (旧的 wm.pet.splash.v1 是桌宠窗口里那个已经清掉的启动动画, 不沿用: 用户可能手动关过).
// workbench.html 的启动脚本读它; TERM_SPLASH_LAST 记"上次放过的冷启动令牌", 同一个令牌 (同一次启动里关了终端又打开) 不再放.
export const TERM_SPLASH_KEY = 'wm.term.splash.v1';
export const TERM_SPLASH_LAST = 'wm.term.splash.last';
export function readTermSplash() {
  try { return localStorage.getItem(TERM_SPLASH_KEY) !== '0'; } catch { return true; }
}
/** 存下来; 存储不可用时返回 false. */
export function writeTermSplash(on) {
  try { localStorage.setItem(TERM_SPLASH_KEY, on ? '1' : '0'); return true; } catch { return false; }
}
