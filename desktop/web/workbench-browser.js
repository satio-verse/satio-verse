// 右侧面板里的网页标签 (2026-11-01 照 Codex 改版; 标签条归 workbench-panel.js, 这里只管一个个网页).
// 宿主 v3: 每个标签一个独立 WebView2 (tab_id); 宿主 v2 (正式版 10-31): 只有一个原生网页, 切标签时重新导航;
// 没有宿主 (浏览器里打开 /agent): 服务端的文字阅读 (/api/webview, 不执行网页脚本), 在我们自己的页面里排版,
// 能页面内查找、缩放、选取段落 / 整节、引用选中的字 (Lexora 那种"快照 + 来源"). 和宿主说的话照旧:
// workbench {op:'browser', action, tab_id, ...}; 宿主回 workbench-browser / workbench-browser-selection (/ 以后的 -pick).
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const P = {
  back: 'm14 6-6 6 6 6', forward: 'm10 6 6 6-6 6', reload: 'M20 7v5h-5M20 12a8 8 0 1 0-2 6', stop: 'm6 6 12 12M18 6 6 18',
  globe: 'M3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0', lock: 'M6 11h12v9H6zM8.5 11V8a3.5 3.5 0 0 1 7 0v3',
  quote: 'M5 5h14v12H9l-4 4zM8 9h8M8 13h5', more: 'M5 12h.01M12 12h.01M19 12h.01', star: 'm12 3 2.8 5.8 6.2.9-4.5 4.4 1.1 6.2L12 17.4 6.4 20.3l1.1-6.2L3 9.7l6.2-.9z',
  pick: 'M4 4l6.5 16 2.3-6.7L19.5 11zM13 13l6 6', search: 'M10.5 17a6.5 6.5 0 1 1 0-13 6.5 6.5 0 0 1 0 13zM16 16l4.5 4.5', reader: 'M5 4h10l4 4v12H5zM8 10h8M8 14h8M8 18h5',
  external: 'M14 4h6v6M20 4l-9 9M18 14v6H4V6h6', history: 'M4 12a8 8 0 1 0 2.4-5.7M4 4v4h4M12 8v4l3 2', clear: 'M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13',
  settings: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM4 12h2M18 12h2M12 4v2M12 18v2M6.3 6.3l1.4 1.4M16.3 16.3l1.4 1.4M6.3 17.7l1.4-1.4M16.3 7.7l1.4-1.4',
  print: 'M7 9V4h10v5M7 17H5v-7h14v7h-2M8 14h8v6H8z', device: 'M8 3h8v18H8zM11 18h2', camera: 'M4 8h4l2-2h4l2 2h4v11H4zM12 17a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z',
  download: 'M12 4v11M7 10l5 5 5-5M5 20h14', link: 'M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1',
};
const icon = name => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${P[name] || P.globe}"/></svg>`;
const read = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } };
const write = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* 存不下就算了 */ } };
const ENGINES = { bing: ['必应', 'https://www.bing.com/search?q='], baidu: ['百度', 'https://www.baidu.com/s?wd='], ddg: ['DuckDuckGo', 'https://duckduckgo.com/?q='] };
export const browserSettings = () => ({ engine: 'bing', autoReader: true, ...read('wm.browser.settings', {}) });
const host = url => { try { return new URL(url).hostname; } catch { return url; } };

export function browserURL(value, engine = browserSettings().engine) {
  const text = String(value || '').trim();
  if (!text) return '';
  if (/^BV[0-9A-Za-z]{10}$/.test(text)) return 'https://www.bilibili.com/video/' + text;
  if (/^[a-z][\w+.-]*:/i.test(text) && !/^https?:\/\//i.test(text) && !/^localhost:\d+/i.test(text) && !/^([\w-]+\.)+[\w-]+:\d+(\/|\?|#|$)/i.test(text)) throw Error('请输入 http 或 https 网页地址。');
  const url = /^https?:\/\//i.test(text) ? text : /^(localhost|127\.0\.0\.1)(:\d+)?(\/|$)/i.test(text) ? 'http://' + text
    : /^[\w-]+(\.[\w-]+)+(:\d+)?(\/|\?|#|$)/.test(text) ? 'https://' + text : (ENGINES[engine] || ENGINES.bing)[1] + encodeURIComponent(text);
  const parsed = new URL(url);
  if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password) throw Error('网址格式不正确，请勿在网址中填写账号密码。');
  return parsed.href;
}

/** 文字阅读的正文 (服务端 web_fetch 的格式): 第一行标题、第二行网址、"(全文 N 字; 小标题: 名字 @位置 · …)", 其余一行一段 */
// 网页引用回到原处 (第 7 块): 网址后面接 #:~:text=… —— 浏览器内核自己滚到那几句、标黄 (Chromium 的 text fragment).
// 短的整句当一个词; 长的取开头一小句和结尾一小句 (起止 + 中间随便多少字都算上); 文字里的 - , & 要转义 (语法里有用)
export function textFragmentURL(url, text) {
  const words = String(text || '').replace(/\s+/g, ' ').trim();
  const page = String(url || '').split('#')[0];
  if (!words || !page) return page || url;
  const enc = s => encodeURIComponent(s.trim()).replace(/-/g, '%2D');
  if (words.length <= 80) return `${page}#:~:text=${enc(words)}`;
  const clause = s => s.split(/(?<=[。！？；.!?;])\s*/).filter(Boolean);
  const first = clause(words.slice(0, 60))[0] || words.slice(0, 30), last = clause(words.slice(-60)).pop() || words.slice(-30);
  return `${page}#:~:text=${enc(first)},${enc(last)}`;
}

export function parseReader(text) {
  const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n');
  const title = lines[0]?.trim() || '', url = lines[1]?.trim() || '';
  let index = 2, total = 0, headings = [];
  while (index < lines.length && !lines[index].trim()) index++;
  const meta = /^\(全文 (\d+) 字(?:; 小标题: (.*))?\)\s*$/.exec(lines[index] || '');
  if (meta) {
    total = Number(meta[1]);
    headings = (meta[2] || '').split(' · ').map(item => item.replace(/\s*@\d+$/, '').trim()).filter(Boolean);
    index++;
  }
  const names = new Set(headings.map(h => h.toLowerCase()));
  const blocks = [];
  for (const raw of lines.slice(index)) {
    const line = raw.trim();
    if (!line || line === '-' || line === '|') continue;
    const bare = line.replace(/¶$/, '').trim();
    if (names.has(bare.toLowerCase())) blocks.push({ kind: 'h', text: bare });
    else if (/^- /.test(line)) blocks.push({ kind: 'li', text: line.slice(2) });
    else blocks.push({ kind: 'p', text: line });
  }
  return { title, url, total, headings, blocks };
}

export function initWorkbenchBrowser(container, { tell, notify, attachReference, attachSelection, getSessionId, renderHome, onTabsChanged }) {
  let capable = false, nativeTabs = false, nativeZoom = false, nativeSelection = false, nativePick = false;
  let visible = false, suspended = false, active = '', raf = 0, lastBounds = '', disposed = false, picking = false, findText = '', pendingHeading = '', pendingText = '';
  const legacy = () => capable && !nativeTabs;
  const tabs = new Map(), selections = new Map();
  const current = () => tabs.get(active);
  const native = (message, tab = current()) => tell('browser', { ...message, ...(tab ? { tab_id: tab.id } : {}) });
  const validRows = key => { const rows = read(key, []); return Array.isArray(rows) ? rows.filter(x => x && typeof x.url === 'string' && /^https?:\/\//i.test(x.url)) : []; };
  container.classList.add('wb-web');
  container.innerHTML = `<div class="wb-web-bar"><button type="button" data-web="back" title="后退" aria-label="后退">${icon('back')}</button><button type="button" data-web="forward" title="前进" aria-label="前进">${icon('forward')}</button><button type="button" data-web="reload" title="刷新" aria-label="刷新">${icon('reload')}</button>
    <form class="wb-address" autocomplete="off"><span class="wb-address-icon">${icon('globe')}</span><input id="browserUrl" aria-label="网址或搜索词" placeholder="搜索或输入网址" spellcheck="false"></form>
    <button type="button" data-web="quote" title="引用到对话：选中的文字，没选时引用这一页的链接" aria-label="引用到对话">${icon('quote')}</button><button type="button" data-web="more" title="更多" aria-label="网页菜单" aria-haspopup="menu" aria-expanded="false">${icon('more')}</button></div>
    <div class="wb-loadbar"></div>
    <div class="wb-find" hidden><input type="search" aria-label="在页面中查找" placeholder="在页面中查找"><span class="wb-find-count"></span><button type="button" data-web="find-prev" aria-label="上一处">↑</button><button type="button" data-web="find-next" aria-label="下一处">↓</button><button type="button" data-web="find-close" aria-label="关闭查找">×</button></div>
    <div class="wb-pickbar" hidden>${icon('pick')}<span>点一段或一个小标题引用到对话（小标题 = 整节）· Esc 取消</span><button type="button" data-web="pick-stop">取消</button></div>
    <div class="wb-web-stage"><div class="wb-web-home"></div><div class="wb-web-native" hidden></div><article class="wb-reader" hidden tabindex="0"></article>
      <div class="wb-web-error" role="alert" hidden></div></div>
    <div class="wb-pmenu wb-web-menu" role="menu" hidden></div>`;
  const $ = q => container.querySelector(q), address = $('#browserUrl'), menu = $('.wb-web-menu'), reader = $('.wb-reader');
  const changed = () => onTabsChanged?.();

  function makeTab(saved = {}) {
    const id = saved.id || 'web-' + crypto.randomUUID().replaceAll('-', '').slice(0, 24);
    const tab = { id, url: saved.url || '', title: saved.title || '', mode: capable && !saved.reader ? 'native' : 'reader', readerChosen: !!saved.reader,
      state: saved.url ? 'unloaded' : 'home', error: '', zoom: 1, readerZoom: 1, canBack: false, canForward: false, history: [], index: -1,
      ticket: 0, timer: 0, abort: null, doc: null, offset: 0, nativeLoaded: false };
    tabs.set(id, tab);
    return tab;
  }
  function remember(tab) {
    if (!tab.url) return;
    const rows = validRows('wm.browser.history').filter(x => x.url !== tab.url);
    rows.unshift({ url: tab.url, title: tab.title || host(tab.url), at: Date.now() });
    write('wm.browser.history', rows.slice(0, 120));
  }
  const busy = tab => ['loading', 'starting'].includes(tab?.state);

  function update() {
    const tab = current();
    if (!tab) return;
    if (document.activeElement !== address) address.value = ['home', 'history', 'settings'].includes(tab.state) ? '' : tab.url;
    address.placeholder = { history: '历史记录', settings: '浏览器设置' }[tab.state] || '搜索或输入网址';
    $('.wb-address-icon').innerHTML = icon(tab.url?.startsWith('https://') && !['home', 'history', 'settings'].includes(tab.state) ? 'lock' : 'globe');
    const nav = tab.mode === 'native' ? [!tab.canBack, !tab.canForward] : [tab.index <= 0, tab.index >= tab.history.length - 1];
    $('[data-web="back"]').disabled = nav[0];
    $('[data-web="forward"]').disabled = nav[1];
    const reload = $('[data-web="reload"]');
    reload.innerHTML = icon(busy(tab) ? 'stop' : 'reload');
    reload.title = reload.ariaLabel = busy(tab) ? '停止加载' : '刷新';
    reload.disabled = !tab.url || ['home', 'history', 'settings'].includes(tab.state);
    $('[data-web="quote"]').disabled = !tab.url || ['home', 'history', 'settings'].includes(tab.state);
    $('.wb-loadbar').classList.toggle('active', busy(tab));
    const page = ['home', 'history', 'settings'].includes(tab.state);
    $('.wb-web-home').hidden = !page;
    $('.wb-web-native').hidden = page || tab.mode !== 'native';
    reader.hidden = page || tab.mode !== 'reader';
    const error = $('.wb-web-error');
    error.hidden = !tab.error || page;
    if (tab.error && !page) {
      error.innerHTML = `<strong>${esc(tab.state === 'error' ? '这个网页没打开' : '网页还在加载')}</strong><p>${esc(tab.error)}</p><div>${tab.state === 'error' || busy(tab) ? '<button type="button" data-web="retry">重试</button>' : ''}${tab.mode === 'native' ? '<button type="button" data-web="to-reader">切到文字阅读</button>' : capable ? '<button type="button" data-web="to-native">打开完整网页</button>' : ''}<button type="button" data-web="external">在系统浏览器打开</button>${busy(tab) ? '<button type="button" data-web="dismiss">继续等</button>' : ''}</div>`;
    }
    if (picking && (tab.mode !== 'reader' || page)) stopPick();
    scheduleBounds();
  }
  function renderPage() {
    const tab = current();
    if (!tab) return;
    const target = $('.wb-web-home');
    if (tab.state === 'history') target.innerHTML = historyHtml();
    else if (tab.state === 'settings') target.innerHTML = settingsHtml();
    else if (tab.state === 'home') renderHome?.(target, tab);
  }
  function historyHtml() {
    const rows = validRows('wm.browser.history');
    return `<section class="wb-web-page"><header><h2>历史记录</h2><button type="button" data-web="clear-data">清除浏览数据</button></header>
      ${rows.length ? `<div class="wb-history">${rows.map(x => `<button type="button" data-url="${esc(x.url)}">${icon('globe')}<span><strong>${esc(x.title || x.url)}</strong><small>${esc(host(x.url))}</small></span><time>${esc(new Date(x.at || 0).toLocaleString([], { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }))}</time></button>`).join('')}</div>` : '<p class="wb-empty-note">还没有浏览记录。</p>'}
      <p class="wb-empty-note">记录只存在这个工作台里${capable ? '；网页自己的登录和缓存在桌宠浏览器的资料目录，清它要新版宿主' : ''}。</p></section>`;
  }
  function settingsHtml() {
    const s = browserSettings();
    return `<section class="wb-web-page"><header><h2>浏览器设置</h2></header><form class="wb-web-settings">
      <label><span><strong>搜索引擎</strong><small>地址栏里输入的不是网址时用它搜</small></span><select name="engine">${Object.entries(ENGINES).map(([key, [name]]) => `<option value="${key}" ${key === s.engine ? 'selected' : ''}>${name}</option>`).join('')}</select></label>
      <label><span><strong>打不开时自动切到文字阅读</strong><small>原生网页报错、10 秒没响应时，改用服务端读出的正文</small></span><input type="checkbox" name="autoReader" ${s.autoReader ? 'checked' : ''}></label>
      </form><p class="wb-empty-note">桌宠的浏览器不管理账号密码，也不导入别的浏览器的 Cookie 和密码。</p></section>`;
  }
  function showMenu() {
    const t = current(), page = !t.url || ['home', 'history', 'settings'].includes(t.state), r = t.mode === 'reader' && !page;
    const bookmarked = validRows('wm.browser.bookmarks').some(x => x.url === t.url);
    const item = (action, glyph, label, { off = false, why = '', kbd = '' } = {}) => `<button type="button" role="menuitem" data-web="${action}" ${off ? 'disabled' : ''} ${why ? `title="${esc(why)}"` : ''}>${icon(glyph)}<span>${label}</span>${kbd ? `<kbd>${kbd}</kbd>` : ''}${off && why ? `<small>${esc(why)}</small>` : ''}</button>`;
    const needHost = '需要新版桌宠宿主';
    const zoomable = r || (nativeZoom && t.mode === 'native' && !page), zoom = Math.round((r ? t.readerZoom : t.zoom) * 100);
    menu.innerHTML = `${item('find', 'search', '在页面中查找', { off: page || !r, why: !page && !r ? '完整网页里查找' + needHost + '；文字阅读里可以找' : '', kbd: 'Ctrl F' })}
      ${item('print', 'print', '打印', { off: true, why: needHost })}
      <div class="wb-menu-zoom"><span>缩放</span><button type="button" data-web="zoom-out" aria-label="缩小" ${zoomable ? '' : 'disabled'}>−</button><button type="button" data-web="zoom-reset" title="恢复 100%" ${zoomable ? '' : 'disabled'}>${zoom}%</button><button type="button" data-web="zoom-in" aria-label="放大" ${zoomable ? '' : 'disabled'}>＋</button></div>
      <hr>${item('pick', 'pick', '选取页面内容引用', { off: page || !(r || nativePick && t.mode === 'native'), why: !page && !r && !nativePick ? '完整网页里选取' + needHost + '；可先切到文字阅读' : '' })}
      ${item('attach', 'link', '把链接加入对话', { off: page })}${item('bookmark', 'star', bookmarked ? '取消收藏' : '收藏网页', { off: page })}
      <hr>${item('device', 'device', '设备工具栏', { off: true, why: needHost })}${item('capture', 'camera', '截取屏幕截图', { off: true, why: needHost })}${item('download', 'download', '下载', { off: true, why: '下载请用系统浏览器' })}
      ${item('history', 'history', '历史记录')}${item('clear-data', 'clear', '清除浏览数据')}
      <hr>${capable ? item('mode', 'reader', t.mode === 'native' ? '切到文字阅读' : '打开完整网页', { off: page }) : ''}${item('external', 'external', '在系统浏览器打开', { off: page })}${item('settings', 'settings', '浏览器设置')}`;
    menu.hidden = false;
    $('[data-web="more"]').setAttribute('aria-expanded', 'true');
    menu.querySelector('button:not(:disabled)')?.focus({ preventScroll: true });
    scheduleBounds();
  }
  function closeMenu(focus = false) {
    if (menu.hidden) return;
    menu.hidden = true;
    $('[data-web="more"]').setAttribute('aria-expanded', 'false');
    if (focus) $('[data-web="more"]').focus();
    scheduleBounds();
  }

  // ---- 原生网页的位置: 面板那一块没被别的浮层盖住时才露出来 (宿主的 WebView2 总在页面上面) ----
  const OVERLAYS = 'dialog[open],.wb-palette:not([hidden]),.wb-pmenu:not([hidden]),.navigation-menu:not([hidden]),.context-add-menu:not([hidden]),#personaPop:not([hidden]),#menu:not([hidden]),.wb-notice,#imgPreview:not([hidden]),.rail-more-popover:not([hidden])';
  function occluded(slot) {
    if (!visible || suspended || document.hidden || document.querySelector('.app.settings-mode')) return true;
    return [...document.querySelectorAll(OVERLAYS)].some(el => {
      const r = el.getBoundingClientRect();
      return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden'
        && r.left < slot.right && r.right > slot.left && r.top < slot.bottom && r.bottom > slot.top;
    });
  }
  function bounds() {
    raf = 0;
    const t = current(), slot = $('.wb-web-native').getBoundingClientRect();
    const show = t && capable && t.mode === 'native' && !['home', 'history', 'settings', 'unloaded', 'closed'].includes(t.state) && !t.error
      && slot.width > 10 && slot.height > 10 && !occluded(slot);
    const payload = show ? { action: 'bounds', tab_id: t.id, visible: true, bounds: { x: slot.x, y: slot.y, width: slot.width, height: slot.height, scale: devicePixelRatio } } : { action: 'hide' };
    const serialized = JSON.stringify(payload);
    if (serialized !== lastBounds) { lastBounds = serialized; native(payload, show ? t : null); }
  }
  function scheduleBounds() { if (!disposed && !raf) raf = requestAnimationFrame(bounds); }

  // ---- 加载 ----
  function fail(tab, message, state = 'error') {
    tab.error = message;
    tab.state = state;
    if (tab === current()) update();
    changed();
  }
  async function readerLoad(tab, target, offset = 0) {
    const ticket = ++tab.ticket;
    tab.abort?.abort();
    tab.abort = new AbortController();
    try {
      const response = await fetch('/api/webview?' + new URLSearchParams({ url: target, offset: String(offset) }), { signal: tab.abort.signal });
      const html = await response.text();
      if (ticket !== tab.ticket || tab.mode !== 'reader' || !tabs.has(tab.id)) return;
      if (!response.ok) throw Error(html.replace(/<[^>]+>/g, '').trim().slice(0, 200) || '网页文字读取失败');
      const text = new DOMParser().parseFromString(html, 'text/html').querySelector('pre')?.textContent || '';
      const doc = parseReader(text);
      tab.doc = { ...doc, offset, chunk: text.length };
      tab.offset = offset;
      tab.title = doc.title || host(target);
      tab.state = 'ready';
      tab.error = '';
      remember(tab);
      if (tab === current()) { renderReader(); update(); }
      changed();
    } catch (e) {
      if (ticket !== tab.ticket || e.name === 'AbortError' || !tabs.has(tab.id)) return;
      fail(tab, e.message + '。可以重试，或在系统浏览器打开。');
    }
  }
  function renderReader() {
    const tab = current(), doc = tab?.doc;
    if (!tab || tab.mode !== 'reader') return;
    if (!doc) { reader.innerHTML = '<p class="wb-empty-note">正在读取正文…</p>'; return; }
    let section = 0;
    const body = doc.blocks.map((block, index) => {
      if (block.kind === 'h') section = index;
      const tag = block.kind === 'h' ? 'h3' : block.kind === 'li' ? 'li' : 'p';
      return `<${tag} class="rb" data-block="${index}" data-section="${section}">${esc(block.text)}</${tag}>`;
    }).join('');
    const more = doc.total && doc.offset + doc.chunk < doc.total + 200;
    reader.style.setProperty('--reader-zoom', tab.readerZoom);
    reader.innerHTML = `<header class="wb-reader-head"><small>文字阅读 · 不执行网页脚本${doc.total ? ` · 全文 ${doc.total.toLocaleString()} 字` : ''}</small><h2>${esc(doc.title || host(tab.url))}</h2><a href="${esc(tab.url)}" data-web-external>${esc(tab.url)}</a>
      ${doc.headings.length ? `<nav class="wb-reader-toc">${doc.headings.slice(0, 24).map(h => `<button type="button" data-heading="${esc(h)}">${esc(h)}</button>`).join('')}</nav>` : ''}</header>
      <div class="wb-reader-body">${body || '<p class="wb-empty-note">这一页没读出正文。</p>'}</div>
      <footer class="wb-reader-foot">${doc.offset > 0 ? `<button type="button" data-web="reader-prev">上一段</button>` : ''}${more ? `<button type="button" data-web="reader-next">下一段</button>` : ''}<span>${doc.offset ? `第 ${doc.offset.toLocaleString()} 字起` : ''}</span></footer>`;
    if (findText) highlight();
    if (pendingText && revealText(pendingText)) pendingHeading = '';
    if (pendingHeading) revealHeading(pendingHeading);
  }
  function revealText(text) {  // 回到引用的那几句 (文字阅读里): 滚到含开头那几个字的那一段, 闪一下
    const head = String(text || '').replace(/\s+/g, ' ').trim().slice(0, 24);
    const target = head && [...reader.querySelectorAll('[data-block]')].find(el => el.textContent.replace(/\s+/g, ' ').includes(head));
    if (!target) return false;
    pendingText = '';
    target.scrollIntoView({ block: 'center' });
    target.classList.remove('wb-flash'); void target.offsetWidth; target.classList.add('wb-flash');
    return true;
  }
  function revealHeading(text) {  // 回到引用的那一节 (文字阅读里)
    const target = [...reader.querySelectorAll('h3[data-block]')].find(h => h.textContent === text);
    if (!target) return false;
    pendingHeading = '';
    target.scrollIntoView({ block: 'start' });
    target.classList.remove('wb-flash'); void target.offsetWidth; target.classList.add('wb-flash');
    return true;
  }
  function load(value, { history = true, tab = current() } = {}) {
    let next;
    try { next = browserURL(value); } catch (e) { notify(e.message); return; }
    if (!next) { goHome(tab); return; }
    clearTimeout(tab.timer);
    tab.url = next; tab.title = ''; tab.state = 'loading'; tab.error = ''; tab.doc = null;
    if (tab === current()) address.value = next;
    if (tab.mode === 'native' && capable) {
      tab.abort?.abort(); tab.ticket++; tab.nativeLoaded = true;
      native({ action: 'navigate', url: next }, tab);
      tab.timer = setTimeout(() => {
        if (!tabs.has(tab.id) || !busy(tab)) return;
        if (browserSettings().autoReader) toReader(tab, '原生网页 10 秒没响应，先给你看服务端读出的正文。');
        else fail(tab, '网站资源加载较慢，可以继续等、重试，或切到文字阅读。', tab.state);
      }, 10000);
    } else {
      if (history) { tab.history = tab.history.slice(0, tab.index + 1); tab.history.push(next); tab.index = tab.history.length - 1; }
      if (tab === current()) renderReader();
      readerLoad(tab, next);
    }
    update();
    changed();
  }
  function toReader(tab, why = '') {
    clearTimeout(tab.timer);
    if (tab === current()) native({ action: 'hide' }, null);
    tab.mode = 'reader'; tab.readerChosen = true; tab.error = '';
    if (why) notify(why);
    if (tab.url) load(tab.url, { tab });
    else update();
  }
  function goHome(tab = current()) {
    tab.abort?.abort(); tab.ticket++; clearTimeout(tab.timer);
    if (tab.nativeLoaded && !legacy()) native({ action: 'close' }, tab);
    Object.assign(tab, { url: '', title: '', state: 'home', error: '', canBack: false, canForward: false, history: [], index: -1, doc: null, nativeLoaded: false });
    if (tab === current()) { renderPage(); update(); }
    changed();
  }
  function special(tab, state) {
    tab.abort?.abort(); tab.ticket++; clearTimeout(tab.timer);
    if (tab.nativeLoaded && !legacy()) native({ action: 'close' }, tab);
    Object.assign(tab, { url: '', title: state === 'history' ? '历史记录' : '浏览器设置', state, error: '', doc: null, nativeLoaded: false });
    renderPage(); update(); changed();
  }

  // ---- 引用: 选中的字 / 段落 / 整节 / 链接 ----
  function readerSelection() {
    const selection = getSelection();
    if (!selection?.rangeCount || selection.isCollapsed) return null;
    const range = selection.getRangeAt(0);
    if (!reader.contains(range.commonAncestorContainer)) return null;
    const text = selection.toString().trim();
    if (!text) return null;
    const block = (range.startContainer.nodeType === 1 ? range.startContainer : range.startContainer.parentElement)?.closest('[data-block]');
    return { text, heading: headingOf(block) };
  }
  function headingOf(block) {
    const doc = current()?.doc, index = Number(block?.dataset.section);
    return doc && Number.isFinite(index) && doc.blocks[index]?.kind === 'h' ? doc.blocks[index].text : '';
  }
  async function quote(payload) {
    const tab = current();
    await attachSelection?.({ url: tab.url, title: tab.title || host(tab.url), text: payload.text.slice(0, 16000), heading: payload.heading || '', targetId: getSessionId?.() });
    notify(payload.heading ? `已引用「${payload.heading}」里的内容` : '已引用到对话');
  }
  async function quoteAction() {
    const tab = current();
    if (!tab?.url) return;
    if (tab.mode === 'reader') {
      const picked = readerSelection();
      if (picked) { await quote(picked); getSelection()?.removeAllRanges(); return; }
      await attachReference?.(tab.url);
      notify('这一页的链接已加入对话材料（选中文字再点就引用那几句）');
      return;
    }
    if (!nativeSelection) { await attachReference?.(tab.url); notify('链接已加入对话材料'); return; }
    const request_id = crypto.randomUUID(), targetId = getSessionId?.();
    const timer = setTimeout(() => {
      if (!selections.delete(request_id)) return;
      attachReference?.(tab.url).then(() => notify('没读到选中的字，先把链接加入了对话')).catch(e => notify(e.message));
    }, 8000);
    selections.set(request_id, { tab_id: tab.id, targetId, timer, fallbackUrl: tab.url });
    native({ action: 'selection', request_id });
  }
  function startPick() {
    const tab = current();
    if (!tab || ['home', 'history', 'settings'].includes(tab.state)) return;
    if (tab.mode === 'native') {
      if (!nativePick) return;
      const request_id = crypto.randomUUID();
      selections.set(request_id, { tab_id: tab.id, targetId: getSessionId?.(), timer: setTimeout(() => selections.delete(request_id), 60000), pick: true });
      native({ action: 'pick', request_id });
      notify('在网页里点一下要引用的地方');
      return;
    }
    picking = true;
    container.classList.add('picking');
    $('.wb-pickbar').hidden = false;
    reader.focus({ preventScroll: true });
  }
  function stopPick() {
    picking = false;
    container.classList.remove('picking');
    $('.wb-pickbar').hidden = true;
    reader.querySelectorAll('.pick-hover').forEach(el => el.classList.remove('pick-hover'));
  }
  function pickTarget(el) {
    const block = el?.closest?.('[data-block]');
    if (!block) return [];
    if (block.tagName !== 'H3') return [block];
    const all = [...reader.querySelectorAll('[data-block]')], start = all.indexOf(block), out = [block];
    for (let i = start + 1; i < all.length && all[i].tagName !== 'H3'; i++) out.push(all[i]);
    return out;
  }

  // ---- 查找 (文字阅读) ----
  function highlight() {
    reader.querySelectorAll('mark.wb-hit').forEach(mark => mark.replaceWith(document.createTextNode(mark.textContent)));
    reader.normalize();
    const needle = findText.trim().toLowerCase();
    let count = 0;
    if (needle) {
      const walker = document.createTreeWalker(reader.querySelector('.wb-reader-body') || reader, NodeFilter.SHOW_TEXT), nodes = [];
      while (walker.nextNode()) nodes.push(walker.currentNode);
      for (const node of nodes) {
        const text = node.textContent, lower = text.toLowerCase();
        let at = lower.indexOf(needle), from = 0;
        if (at < 0) continue;
        const fragment = document.createDocumentFragment();
        while (at >= 0 && count < 999) {
          fragment.append(text.slice(from, at));
          const mark = document.createElement('mark');
          mark.className = 'wb-hit';
          mark.textContent = text.slice(at, at + needle.length);
          fragment.append(mark);
          count++;
          from = at + needle.length;
          at = lower.indexOf(needle, from);
        }
        fragment.append(text.slice(from));
        node.replaceWith(fragment);
      }
    }
    $('.wb-find-count').textContent = needle ? `${count} 处` : '';
    focusHit(0);
  }
  let hitIndex = 0;
  function focusHit(step) {
    const hits = [...reader.querySelectorAll('mark.wb-hit')];
    if (!hits.length) return;
    hitIndex = step === 0 ? 0 : (hitIndex + step + hits.length) % hits.length;
    hits.forEach((hit, i) => hit.classList.toggle('current', i === hitIndex));
    hits[hitIndex].scrollIntoView({ block: 'center' });
  }
  function showFind(open = true) {
    const bar = $('.wb-find');
    bar.hidden = !open;
    if (open) { bar.querySelector('input').focus(); bar.querySelector('input').select(); }
    else { findText = ''; bar.querySelector('input').value = ''; highlight(); }
  }

  async function action(name) {
    const tab = current();
    if (!tab) return;
    if (!['more', 'zoom-in', 'zoom-out', 'zoom-reset'].includes(name)) closeMenu();
    if (name === 'more') { menu.hidden ? showMenu() : closeMenu(); return; }
    if (name === 'history') { special(tab, 'history'); return; }
    if (name === 'settings') { special(tab, 'settings'); return; }
    if (name === 'clear-data') { write('wm.browser.history', []); write('wm.browser.bookmarks', []); notify('已清除工作台里的浏览记录和收藏'); if (tab.state === 'history') renderPage(); changed(); return; }
    if (name === 'find') { if (tab.mode === 'reader' && tab.doc) showFind(true); return; }
    if (name === 'find-close') { showFind(false); return; }
    if (name === 'find-next' || name === 'find-prev') { focusHit(name === 'find-next' ? 1 : -1); return; }
    if (name === 'pick') { startPick(); return; }
    if (name === 'pick-stop') { stopPick(); return; }
    if (name === 'dismiss') { tab.error = ''; update(); return; }
    if (name === 'to-reader' || (name === 'mode' && tab.mode === 'native')) { toReader(tab); return; }
    if (name === 'to-native' || name === 'mode') { tab.mode = 'native'; tab.readerChosen = false; tab.doc = null; if (tab.url) load(tab.url); else update(); return; }
    if (name.startsWith('zoom-')) {
      if (tab.mode === 'reader') {
        tab.readerZoom = Math.min(2, Math.max(0.6, name === 'zoom-reset' ? 1 : Math.round((tab.readerZoom + (name === 'zoom-in' ? 0.1 : -0.1)) * 10) / 10));
        reader.style.setProperty('--reader-zoom', tab.readerZoom);
      } else if (nativeZoom) {
        tab.zoom = Math.min(2, Math.max(0.5, name === 'zoom-reset' ? 1 : Math.round((tab.zoom + (name === 'zoom-in' ? 0.1 : -0.1)) * 10) / 10));
        native({ action: 'zoom', factor: tab.zoom });
      }
      showMenu();
      return;
    }
    if (name === 'back' || name === 'forward') {
      if (tab.mode === 'native') native({ action: name });
      else { const index = tab.index + (name === 'back' ? -1 : 1); if (index >= 0 && index < tab.history.length) { tab.index = index; load(tab.history[index], { history: false }); } }
      return;
    }
    if (name === 'reload' || name === 'retry') {
      if (name === 'reload' && busy(tab)) {
        clearTimeout(tab.timer); tab.abort?.abort(); tab.ticket++;
        if (tab.mode === 'native') native({ action: 'stop' });
        tab.state = 'ready'; tab.error = tab.mode === 'reader' && !tab.doc ? '已停止读取，点重试再读一次。' : '';
        update(); changed();
      } else if (tab.url) {
        if (tab.mode === 'native' && name === 'reload' && tab.nativeLoaded) { native({ action: 'reload' }); tab.state = 'loading'; tab.error = ''; update(); }
        else load(tab.url, { history: false });
      }
      return;
    }
    if (name === 'reader-next' || name === 'reader-prev') {
      const offset = Math.max(0, (tab.doc?.offset || 0) + (name === 'reader-next' ? 20000 : -20000));
      tab.state = 'loading'; update(); readerLoad(tab, tab.url, offset); reader.scrollTop = 0;
      return;
    }
    if (!tab.url) return;
    if (name === 'external') { if (capable) native({ action: 'external', url: browserURL(tab.url) }); else window.open(browserURL(tab.url), '_blank', 'noopener,noreferrer'); }
    else if (name === 'attach') { await attachReference?.(tab.url); notify('网页链接已加入对话材料'); }
    else if (name === 'bookmark') {
      const rows = validRows('wm.browser.bookmarks'), i = rows.findIndex(x => x.url === tab.url);
      if (i < 0) rows.unshift({ url: tab.url, title: tab.title || host(tab.url) }); else rows.splice(i, 1);
      write('wm.browser.bookmarks', rows);
      notify(i < 0 ? '已收藏网页' : '已取消收藏');
      changed();
    } else if (name === 'quote') await quoteAction();
  }

  container.addEventListener('click', event => {
    const external = event.target.closest('[data-web-external]');
    if (external) { event.preventDefault(); action('external').catch(e => notify(e.message)); return; }
    if (picking) {
      const blocks = pickTarget(event.target);
      if (blocks.length) {
        event.preventDefault();
        const heading = blocks[0].tagName === 'H3' ? blocks[0].textContent : headingOf(blocks[0]);
        quote({ text: blocks.map(b => b.textContent).join('\n'), heading }).catch(e => notify(e.message));
        stopPick();
        return;
      }
    }
    const heading = event.target.closest('[data-heading]');
    if (heading) { [...reader.querySelectorAll('h3[data-block]')].find(h => h.textContent === heading.dataset.heading)?.scrollIntoView({ block: 'start' }); return; }
    const b = event.target.closest('button');
    if (!b || b.disabled) return;
    if (b.dataset.url) { load(b.dataset.url); return; }
    if (b.dataset.web) action(b.dataset.web).catch(e => notify(e.message));
  });
  container.addEventListener('pointerover', event => {
    if (!picking) return;
    reader.querySelectorAll('.pick-hover').forEach(el => el.classList.remove('pick-hover'));
    pickTarget(event.target).forEach(el => el.classList.add('pick-hover'));
  });
  container.addEventListener('change', event => {
    const form = event.target.closest('.wb-web-settings');
    if (!form) return;
    write('wm.browser.settings', { engine: form.elements.engine.value, autoReader: form.elements.autoReader.checked });
    notify('浏览器设置已保存');
  });
  $('.wb-address').addEventListener('submit', event => { event.preventDefault(); load(address.value); address.blur(); });
  address.addEventListener('focus', () => address.select());
  $('.wb-find input').addEventListener('input', event => { findText = event.target.value; highlight(); });
  $('.wb-find input').addEventListener('keydown', event => { if (event.key === 'Enter') { event.preventDefault(); focusHit(event.shiftKey ? -1 : 1); } });
  function pointer(event) { if (!menu.hidden && !menu.contains(event.target) && !event.target.closest('[data-web="more"]')) closeMenu(); }
  function key(event) {
    if (!visible || suspended || event.defaultPrevented || document.querySelector('dialog[open]')) return;
    const modifier = event.ctrlKey || event.metaKey;
    if (event.key === 'Escape' && picking) { event.preventDefault(); stopPick(); return; }
    if (event.key === 'Escape' && !menu.hidden) { event.preventDefault(); event.stopPropagation(); closeMenu(true); return; }
    if (event.key === 'Escape' && !$('.wb-find').hidden && container.contains(document.activeElement)) { event.preventDefault(); showFind(false); return; }
    if (modifier && !event.altKey && event.key.toLowerCase() === 'l') { event.preventDefault(); address.focus(); address.select(); return; }
    const here = container.contains(document.activeElement) || document.activeElement === document.body || !document.activeElement;  // 焦点在网页里, 或者哪儿都没焦点
    if (modifier && !event.altKey && event.key.toLowerCase() === 'f' && here && current()?.mode === 'reader' && current()?.doc) { event.preventDefault(); showFind(true); return; }
    if (!menu.hidden && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const list = [...menu.querySelectorAll('button:not(:disabled)')], i = list.indexOf(document.activeElement);
      list[event.key === 'Home' ? 0 : event.key === 'End' ? list.length - 1 : (i + (event.key === 'ArrowDown' ? 1 : -1) + list.length) % list.length]?.focus();
    }
  }
  const resize = new ResizeObserver(scheduleBounds);
  resize.observe(container);
  const overlays = new MutationObserver(scheduleBounds);
  overlays.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['hidden', 'open', 'class', 'style'] });
  addEventListener('resize', scheduleBounds);
  document.addEventListener('visibilitychange', scheduleBounds);
  document.addEventListener('pointerdown', pointer, true);
  document.addEventListener('keydown', key, true);

  function activate(id) {
    if (!tabs.has(id)) return;
    closeMenu();
    if (picking) stopPick();
    if (!$('.wb-find').hidden) showFind(false);
    const previous = current();
    active = id;
    lastBounds = '';
    const tab = current();
    if (legacy() && previous && previous !== tab && previous.mode === 'native') previous.nativeLoaded = false;  // 宿主 v2 只有一个网页: 换标签就重新导航
    if (tab.state === 'unloaded' || (legacy() && tab.mode === 'native' && tab.url && !tab.nativeLoaded && !['home', 'history', 'settings'].includes(tab.state))) load(tab.url, { history: false, tab });
    else { renderPage(); if (tab.mode === 'reader') renderReader(); update(); }
  }
  return {
    create(saved = {}) { const tab = makeTab(saved); return tab.id; },
    show(id) { visible = true; activate(id); },
    hide() { visible = false; closeMenu(); if (picking) stopPick(); scheduleBounds(); },
    remove(id) {
      const tab = tabs.get(id);
      if (!tab) return;
      clearTimeout(tab.timer); tab.abort?.abort(); tab.ticket++;
      if (tab.nativeLoaded && (!legacy() || id === active)) native({ action: legacy() ? 'hide' : 'close' }, legacy() ? null : tab);
      tabs.delete(id);
      if (active === id) { active = ''; lastBounds = ''; scheduleBounds(); }
    },
    info(id) { const t = tabs.get(id); return t && { id: t.id, url: t.url, title: t.title, state: t.state, mode: t.mode, loading: busy(t) }; },
    open(url, id = active) { const tab = tabs.get(id); if (tab) load(url, { tab }); },
    serialize(id) { const t = tabs.get(id); return t && (t.url ? { url: t.url, title: t.title, reader: t.readerChosen } : {}); },
    focusAddress() { address.focus(); address.select(); },
    revealHeading(text) { pendingHeading = text || ''; if (pendingHeading && current()?.doc) revealHeading(pendingHeading); },
    // 回到引用的那几句 (第 7 块): 原生网页跳到 网址#:~:text=那几句; 文字阅读里滚到那一段, 找不到就回到那一节 (还没读完就等读完)
    revealQuote({ text = '', heading = '' } = {}) {
      const tab = current();
      if (!tab) return;
      if (tab.mode === 'native' && capable && text) { load(textFragmentURL(tab.url, text), { history: false, tab }); return; }
      pendingText = text; pendingHeading = heading;
      if (!tab.doc) return;
      if (pendingText && revealText(pendingText)) pendingHeading = '';
      if (pendingHeading) revealHeading(pendingHeading);
    },
    quote: () => quoteAction(),
    homeChanged() { if (visible && current()?.state === 'home') renderPage(); },
    suspend(value) { suspended = value; scheduleBounds(); },
    capabilities: () => ({ capable, nativeTabs, nativeZoom, nativeSelection, nativePick }),
    onHost(message) {
      if (message?.t === 'workbench-capabilities') {
        const first = !capable;
        capable = message.native_browser === true;
        nativeTabs = capable && message.browser_tabs === true;
        nativeZoom = capable && message.browser_zoom === true;
        nativeSelection = capable && message.browser_selection === true;
        nativePick = capable && message.browser_pick === true;
        if (first && capable) {  // 晚到的握手: 没主动选文字阅读的标签升级成原生网页, 取消旧的阅读请求
          for (const t of tabs.values()) if (!t.readerChosen) { t.abort?.abort(); t.ticket++; t.mode = 'native'; t.doc = null; if (t.url) t.state = 'unloaded'; }
          if (visible && current()?.url && !current().readerChosen) load(current().url, { history: false });
        }
        update();
        return false;
      }
      if (message?.t === 'workbench-browser-selection' || message?.t === 'workbench-browser-pick') {
        const pending = selections.get(message.request_id);
        if (!pending || pending.tab_id !== message.tab_id) return true;
        clearTimeout(pending.timer);
        selections.delete(message.request_id);
        if (!message.text?.trim()) { notify(message.t === 'workbench-browser-pick' ? '没有选到内容' : '请先在网页里选中文字，再点引用'); return true; }
        Promise.resolve(attachSelection?.({ url: message.url, title: message.title, text: message.text.slice(0, 16000), heading: message.heading || '', targetId: pending.targetId }))
          .then(() => notify('网页内容已引用到对话')).catch(e => notify(e.message));
        return true;
      }
      if (message?.t !== 'workbench-browser') return false;
      const tab = message.tab_id && tabs.has(message.tab_id) ? tabs.get(message.tab_id) : (legacy() ? current() : null);
      if (!tab || tab.mode !== 'native' || ['home', 'history', 'settings'].includes(tab.state)) return true;
      const before = tab.state + tab.url + tab.title;
      if (!['loading', 'starting'].includes(message.state)) clearTimeout(tab.timer);
      if (message.url) { try { tab.url = browserURL(message.url); } catch { return true; } }
      tab.title = message.title || tab.title;
      tab.state = message.state || tab.state;
      tab.canBack = !!message.can_back;
      tab.canForward = !!message.can_forward;
      if (Number.isFinite(message.zoom_factor)) tab.zoom = message.zoom_factor;
      const httpError = Number(message.http_status) >= 400 ? `网站回了 HTTP ${message.http_status}` : '';
      tab.error = message.error || (tab.state === 'error' ? httpError || '网页没有打开' : '');
      if (tab.error && tab.state === 'error' && browserSettings().autoReader) { toReader(tab, `原生网页没打开（${tab.error}），先给你看服务端读出的正文。`); return true; }
      if (tab.state === 'ready' && before !== tab.state + tab.url + tab.title) remember(tab);
      if (tab === current()) update();
      changed();
      return true;
    },
    dispose() {
      disposed = true; cancelAnimationFrame(raf);
      for (const t of tabs.values()) { clearTimeout(t.timer); t.abort?.abort(); }
      for (const p of selections.values()) clearTimeout(p.timer);
      resize.disconnect(); overlays.disconnect();
      removeEventListener('resize', scheduleBounds);
      document.removeEventListener('visibilitychange', scheduleBounds);
      document.removeEventListener('pointerdown', pointer, true);
      document.removeEventListener('keydown', key, true);
      native({ action: 'hide' }, null);
    },
  };
}
