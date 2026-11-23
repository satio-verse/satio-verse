// 右侧面板 (2026-11-01 照 Codex 桌面版改): 一排标签 —— 网页 (含新标签页)、文件、变更、终端、协作与能力 —— 加 "+ 新标签页"、
// 放大、收起. 每个会话记自己的标签 (localStorage wm.panel.<会话 id>), 换会话再回来还在. 网页交给 workbench-browser.js,
// 文件交给 workbench-files.js, 终端交给 workbench-terminal.js, 协作 / 能力那几页交给 workbench-canvas.js; 这里管标签条、
// 新标签页 (工具 / 推荐 / 最近)、变更 (diff)、Ctrl+P 找文件、面板宽度.
import { initWorkbenchBrowser, browserURL } from '/pet/workbench-browser.js';
import { initFileView } from '/pet/workbench-files.js';
import { initReading } from '/pet/workbench-reading.js';
import { initTerminalPanel } from '/pet/workbench-terminal.js';
import { initCanvas, canvasRequest, renderMarkdown } from '/pet/workbench-canvas.js';
import { diffLines, hunks, collectChanges, relativePath } from '/pet/workbench-diff.js';

const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const G = {
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 3 2.6 15 0 18M12 3c-2.6 3-2.6 15 0 18"/>',
  file: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/>', folder: '<path d="M3 6.5h6l2 2h10V19H3z"/>',
  terminal: '<rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="M7 10l3 2.5L7 15M12.5 15h4"/>',
  agents: '<circle cx="8" cy="8" r="3"/><circle cx="17" cy="9" r="2.5"/><path d="M3 19a5 5 0 0 1 10 0M13.5 18.5a4 4 0 0 1 7 0"/>',
  diff: '<path d="M8 4v10M3 9h10M14 15h7M7 20l10-16"/>', clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  grid: '<rect x="3.5" y="3.5" width="7" height="7" rx="2"/><rect x="13.5" y="3.5" width="7" height="7" rx="2"/><rect x="3.5" y="13.5" width="7" height="7" rx="2"/><rect x="13.5" y="13.5" width="7" height="7" rx="2"/>',
  plus: '<path d="M12 5v14M5 12h14"/>', chat: '<path d="M5 5.5h14v10H10l-5 4z"/>', cmd: '<path d="m6 8 4 4-4 4M12 16h6"/>', close: '<path d="m7 7 10 10M17 7 7 17"/>', expand: '<path d="M8 3H3v5M16 3h5v5M3 16v5h5M21 16v5h-5"/>',
  hide: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M15 4v16"/>', star: '<path d="m12 3 2.8 5.8 6.2.9-4.5 4.4 1.1 6.2L12 17.4 6.4 20.3l1.1-6.2L3 9.7l6.2-.9z"/>',
  box: '<path d="M4 8l8-4 8 4v8l-8 4-8-4z"/><path d="M4 8l8 4 8-4M12 12v8"/>', search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  tools: '<path d="M14.5 6.5a4 4 0 0 0-5.3 5.3L4 17l3 3 5.2-5.2a4 4 0 0 0 5.3-5.3l-2.6 2.6-2.4-.6-.6-2.4z"/>',
  life: '<path d="M12 20s-7-4.4-7-10a4 4 0 0 1 7-2.6A4 4 0 0 1 19 10c0 5.6-7 10-7 10z"/>',
  bulb: '<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.6 10.8c.8.6 1.1 1.3 1.1 2.2h5c0-.9.3-1.6 1.1-2.2A6 6 0 0 0 12 3z"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="m4 7 8 6 8-6"/>', book: '<path d="M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z"/><path d="M5 17a3 3 0 0 1 3-3h11"/>',
};
const ic = (name, size = 16) => `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${G[name] || G.file}</svg>`;
const read = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } };
const write = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* 存不下就算了 */ } };
const host = url => { try { return new URL(url).hostname; } catch { return url; } };
const base = path => String(path || '').replace(/\\/g, '/').split('/').filter(Boolean).pop() || path;
const TOOL_PAGES = {
  agents: ['协作任务', 'agents', 'agents:tasks'], automation: ['自动化任务', 'clock', 'agents:resident'], presets: ['Agent 预设', 'agents', 'agents:presets'],
  life: ['生活与成长', 'life', 'agents:life'], capabilities: ['能力与插件', 'tools', 'tools:builtins'], context: ['上下文与记录', 'book', 'tools:context'],
  experiences: ['工作经验', 'bulb', 'tools:experiences'],
  extensions: ['我的插件', 'box', 'tools:extensions'], community: ['插件社区', 'box', 'community'], data: ['数据贡献', 'grid', 'tools:data'], updates: ['更新', 'grid', 'tools:updates'],
  notebooks: ['资料本', 'book', 'notebooks'],
};
// 服务端自己的页面, 开在面板的"页面"标签里 (同源 iframe; 网页面板屏蔽本机服务端端口, 不能用它开)
const PAGES = { mail: { title: '邮箱', url: '/mail', icon: 'mail', what: '连接邮箱的页面' } };
// 任务与成果 (workbench-studio.js) 的几页: 任务 / 成果共用"任务与成果"标签, 扩展 / 权限 / 成长各开各的 (第六轮第三批)
// "原文"标签只开本机服务端给的这两种: 资料本里的原文件、工作区里导出的 PDF (第 7 块)
const docURL = url => /^\/api\/agent\/(notebooks\?|workspace\?action=raw&)/.test(String(url || ''));
const STUDIO_PAGES = { tasks: ['任务与成果', 'agents'], deliveries: ['任务与成果', 'agents'], extensions: ['扩展与版本', 'box'], permissions: ['权限与规则', 'tools'], growth: ['成长与评测', 'life'] };
const studioGroup = ref => (['tasks', 'deliveries'].includes(ref) ? 'task' : ref);
const when = at => {
  const d = new Date(at || 0), now = new Date();
  return d.toDateString() === now.toDateString() ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : d.toLocaleDateString([], { month: 'numeric', day: 'numeric' });
};

export function initPanel({ app, getSession, getState, api, tell, notify, openSession, ensureSession, reload, openStudio, openSettings, setDraft, setPreset, changeSetting,
  renderChat, attachReference, attachText, revealFile, onLayout, openWith, getCommands, onStudioTab, ask }) {
  const panel = document.createElement('aside');
  panel.className = 'wb-panel';
  panel.id = 'panel';
  panel.hidden = true;
  panel.setAttribute('aria-label', '右侧面板');
  panel.innerHTML = `<div class="wb-panel-resizer" role="separator" tabindex="0" aria-orientation="vertical" aria-label="调整面板宽度"></div>
    <header class="wb-ptabs"><div class="wb-ptab-list" role="tablist" aria-label="面板标签"></div>
      <button type="button" class="wb-picon wb-ptab-more" data-tab-more title="全部标签" aria-label="全部标签" aria-haspopup="menu" hidden><svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><path d="m7 10 5 5 5-5"/></svg><b></b></button>
      <button type="button" class="wb-picon" data-panel="new" title="新标签页 (Ctrl T)" aria-label="新标签页">${ic('plus')}</button><span class="grow"></span>
      <button type="button" class="wb-picon" data-panel="wide" title="放大面板" aria-label="放大面板">${ic('expand')}</button>
      <button type="button" class="wb-picon" data-panel="hide" title="收起面板 (Ctrl J)" aria-label="收起面板">${ic('hide')}</button></header>
    <div class="wb-pbody"><section class="wb-pview" data-view="web" hidden></section><section class="wb-pview" data-view="file" hidden></section>
      <section class="wb-pview" data-view="diff" hidden></section><section class="wb-pview" data-view="terminal" hidden></section><section class="wb-pview" data-view="tool" hidden></section>
      <section class="wb-pview" data-view="page" hidden></section><section class="wb-pview" data-view="sources" hidden></section><section class="wb-pview" data-view="doc" hidden></section><section class="wb-pview" data-view="read" hidden></section><section class="wb-pview" data-view="studio" hidden></section></div>`;
  app.append(panel);
  const views = Object.fromEntries([...panel.querySelectorAll('[data-view]')].map(el => [el.dataset.view, el]));
  const list = panel.querySelector('.wb-ptab-list'), more = panel.querySelector('.wb-ptab-more');
  let key = '', tabs = [], active = '', open = !!read('wm.panel.open', false), renderSeq = 0;  // 开着还是收着: 全局记 (换会话不跟着开合); 标签按会话记
  const sid = () => getSession()?.id || '';
  const workspace = () => getSession()?.workspace || '';

  // ---- 各种标签的内容 ----
  const browser = initWorkbenchBrowser(views.web, {
    tell, notify, attachReference, getSessionId: sid,
    attachSelection: async item => {
      const name = `${item.title || '网页'}${item.heading ? ' · ' + item.heading : ''} · 引用`;
      const result = await attachText({ name, text: item.text, source: item.url, targetId: item.targetId });
      remember(name, { kind: 'web', url: item.url, heading: item.heading || '', text: String(item.text || '').slice(0, 400) });  // 引的那几句: 回到原处时滚到这儿
      return result;
    },
    renderHome, onTabsChanged: () => { syncWebTitles(); save(); },
  });
  const files = initFileView(views.file, {
    getSession, request: canvasRequest, renderMarkdown, notify, revealFile, attachText,
    changesFor: path => fileChanges(path), onDocsChanged: () => { syncFileTitles(); save(); }, onOpen: path => openFile(path),
    onQuoted: q => remember(q.name, { kind: 'file', path: q.path, start: q.start, end: q.end, unit: q.unit || 'line' }),
    openDoc: (url, title) => openDoc(url, title),  // Word / PPT 导出的 PDF 开在"原文"标签里
  });
  const terminalBox = views.terminal.appendChild(document.createElement('div'));  // 终端模块会改写自己容器的 class, 套一层免得面板的视图样式被冲掉
  const terminal = initTerminalPanel(terminalBox, { getSession, ensureSession, notify, openSession, openPanel: () => openTool('terminal') });
  const canvas = initCanvas({ container: views.tool, getSession, getState, api, notify, openSession, reload, openStudio, openSettings, setDraft, setPreset, changeSetting, renderChat,
    openTerminal: async (owner, id) => { if (owner && getSession()?.id !== owner) await openSession(owner); openTool('terminal'); await terminal.show(id, owner); },  // 看板终端卡的"去看看"
    ensureSession, openDoc: (url, title) => openDoc(url, title), openWeb: url => openWeb(url, { newTab: true }),
    openReading: (nid, no, key, name) => openReading(nid, no, key, name) });
  // 阅读面板 (workbench-reading.js): 资料本里一份资料的正文, 选中就问, 回答里的出处点了翻回来
  const reading = initReading(views.read, { getSession, ensureSession, notify, ask, openDoc: (url, title) => openDoc(url, title), openNotebook: id => openNotebook(id) });
  const readKey = new Map();  // 标签 id -> 打开后要跳到的出处编号

  // 引用从哪来的 (Lexora 那种"快照 + 定位"): 输入框里点引用卡片时回到原处; 按会话 + 名字记最近 120 条
  function remember(name, where) {
    const rows = read('wm.panel.quotes', []).filter(r => !(r.sid === sid() && r.name === name));
    rows.unshift({ sid: sid(), name, ...where });
    write('wm.panel.quotes', rows.slice(0, 120));
  }
  function revealQuote(name) {
    const plain = String(name || '').replace(/\.txt$/, '');
    const safe = text => text.replace(/[\\/:*?"<>|]/g, '_');  // 上传时文件名里的这些字换成了 _ (workbench-studio.js addTextMaterial)
    const where = read('wm.panel.quotes', []).find(r => r.sid === sid() && safe(r.name) === safe(plain));
    if (!where) return false;
    if (where.kind === 'file') { files.reveal(where.path, where.start, where.end, where.unit); openFile(where.path); return true; }
    const page = url => String(url || '').split('#')[0];  // 跳过 #:~:text= 以后网址后面多了一截, 比的时候去掉
    const tab = tabs.find(t => t.kind === 'web' && page(browser.info(t.ref)?.url) === page(where.url));
    if (tab) activate(tab.id); else openWeb(where.url, { newTab: true });
    browser.revealQuote({ text: where.text || '', heading: where.heading || '' });
    return true;
  }
  function fileChanges(path) {
    const session = getSession();
    if (!session) return [];
    const target = String(path).replace(/\\/g, '/').toLowerCase();
    return collectChanges(session.events, session.workspace).filter(f => relativePath(f.path, session.workspace).toLowerCase() === target);
  }
  function tabLabel(tab) {
    if (tab.kind === 'web') { const info = browser.info(tab.ref); return info?.title || (info?.url ? host(info.url) : '新标签页'); }
    if (tab.kind === 'file') return tab.ref ? base(tab.ref) : '文件';
    if (tab.kind === 'diff') return tab.title || '变更';
    if (tab.kind === 'terminal') return '终端';
    if (tab.kind === 'page') return PAGES[tab.ref]?.title || '页面';
    if (tab.kind === 'sources') return `来源 · ${(tab.ref || []).length}`;
    if (tab.kind === 'doc') return tab.ref?.title || '原文';
    if (tab.kind === 'read') return tab.title ? `阅读 · ${tab.title}` : '阅读';
    if (tab.kind === 'studio') return STUDIO_PAGES[tab.ref]?.[0] || '任务与成果';
    return TOOL_PAGES[tab.ref]?.[0] || '工具';
  }
  function tabIcon(tab) {
    if (tab.kind === 'web') return ic('globe', 14);
    if (tab.kind === 'file') return ic(tab.ref ? 'file' : 'folder', 14);
    if (tab.kind === 'diff') return ic('diff', 14);
    if (tab.kind === 'terminal') return ic('terminal', 14);
    if (tab.kind === 'page') return ic(PAGES[tab.ref]?.icon || 'globe', 14);
    if (tab.kind === 'sources') return ic('globe', 14);
    if (tab.kind === 'doc') return ic('file', 14);
    if (tab.kind === 'read') return ic('book', 14);
    if (tab.kind === 'studio') return ic(STUDIO_PAGES[tab.ref]?.[1] || 'agents', 14);
    return ic(TOOL_PAGES[tab.ref]?.[1] || 'tools', 14);
  }
  function renderTabs() {
    list.innerHTML = tabs.map(tab => {
      const loading = tab.kind === 'web' && browser.info(tab.ref)?.loading;
      const label = tabLabel(tab);
      return `<div class="wb-ptab ${tab.id === active ? 'on' : ''}" data-tab="${tab.id}"><button type="button" role="tab" aria-selected="${tab.id === active}" tabindex="${tab.id === active ? 0 : -1}" data-tab-open="${tab.id}" title="${esc(tab.kind === 'web' ? browser.info(tab.ref)?.url || label : tab.kind === 'file' ? tab.ref || '工作区文件' : label)}">${loading ? '<i class="wb-spin" aria-label="加载中"></i>' : tabIcon(tab)}<span>${esc(label)}</span></button><button type="button" class="wb-ptab-x" data-tab-close="${tab.id}" aria-label="关闭 ${esc(label)}">${ic('close', 12)}</button></div>`;
    }).join('');
    fitTabs();
    list.querySelector('.wb-ptab.on')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
  // 标签多了 (第六轮, 照 Codex / 浏览器): 先一起收窄; 还放不下就只剩图标 (当前的留名字); 再放不下右边出"∨"列出全部
  function fitTabs() {
    list.classList.remove('compact');
    if (panel.hidden || !tabs.length) { more.hidden = true; return; }
    if (list.scrollWidth > list.clientWidth + 1) list.classList.add('compact');
    more.hidden = !(list.scrollWidth > list.clientWidth + 1);
    more.querySelector('b').textContent = more.hidden ? '' : String(tabs.length);
  }
  let tabMenu = null;
  function closeTabMenu() { tabMenu?.remove(); tabMenu = null; more.setAttribute('aria-expanded', 'false'); }
  function toggleTabMenu(anchor) {
    if (tabMenu) { closeTabMenu(); return; }
    tabMenu = document.createElement('div');
    tabMenu.className = 'wb-pmenu wb-tab-menu';
    tabMenu.setAttribute('role', 'menu');
    tabMenu.innerHTML = tabs.map(tab => `<button type="button" role="menuitem" data-pick-tab="${tab.id}" class="${tab.id === active ? 'on' : ''}">${tabIcon(tab)}<span>${esc(tabLabel(tab))}</span></button>`).join('');
    document.body.append(tabMenu);
    const r = anchor.getBoundingClientRect();
    tabMenu.style.top = `${r.bottom + 6}px`;
    tabMenu.style.left = `${Math.max(8, Math.min(r.left, innerWidth - tabMenu.offsetWidth - 8))}px`;
    more.setAttribute('aria-expanded', 'true');
    tabMenu.addEventListener('click', event => { const b = event.target.closest('[data-pick-tab]'); if (b) { closeTabMenu(); activate(b.dataset.pickTab); } });
    tabMenu.querySelector('.on, button')?.focus({ preventScroll: true });
  }
  document.addEventListener('pointerdown', event => { if (tabMenu && !event.target.closest('.wb-tab-menu, .wb-ptab-more')) closeTabMenu(); });
  document.addEventListener('keydown', event => { if (tabMenu && event.key === 'Escape') { event.preventDefault(); closeTabMenu(); } }, true);
  new ResizeObserver(() => fitTabs()).observe(list.parentElement);
  const syncWebTitles = () => renderTabs();
  const syncFileTitles = () => renderTabs();

  function save() {
    write('wm.panel.open', open);
    if (!key) return;
    write(key, { active: tabs.findIndex(t => t.id === active), tabs: tabs.map(t => ({ kind: t.kind, ref: t.kind === 'web' ? browser.serialize(t.ref) || {} : t.ref, title: t.title || '' })) });
  }
  function teardown() {
    for (const tab of tabs) if (tab.kind === 'web') browser.remove(tab.ref);
    tabs = []; active = '';
  }
  function restore() {
    const saved = read(key, null);
    for (const row of Array.isArray(saved?.tabs) ? saved.tabs.slice(0, 16) : []) {
      if (row.kind === 'web') tabs.push(newTab('web', browser.create(row.ref || {})));
      else if (row.kind === 'studio' && STUDIO_PAGES[row.ref]) tabs.push(newTab('studio', row.ref));
      else if (['file', 'diff', 'terminal', 'tool', 'page', 'sources', 'doc', 'read'].includes(row.kind) && (row.kind !== 'page' || PAGES[row.ref]) && (row.kind !== 'sources' || Array.isArray(row.ref))
        && (row.kind !== 'read' || (/^nb-[0-9a-f]{8}$/.test(row.ref?.nid || '') && Number(row.ref?.no) > 0))
        && (row.kind !== 'doc' || docURL(row.ref?.url))) tabs.push(newTab(row.kind, row.ref, row.title));
    }
    // 面板开着、这个会话还没开过标签: 给一个新标签页, 面板不跟着收起 (以前换会话面板忽开忽关, 左右栏来回跳)
    if (open && !tabs.length) tabs.push(newTab('web', browser.create({})));
    active = tabs[Math.max(0, Number(saved?.active) || 0)]?.id || tabs[0]?.id || '';
  }
  function newTab(kind, ref, title = '') { return { id: 'pt-' + Math.random().toString(36).slice(2, 10), kind, ref, title }; }

  async function show() {
    const seq = ++renderSeq;
    const tab = tabs.find(t => t.id === active);
    panel.hidden = !open || !tab;
    app.classList.toggle('panel-open', !panel.hidden);
    for (const [name, el] of Object.entries(views)) el.hidden = panel.hidden || tab?.kind !== name;
    if (panel.hidden || tab?.kind !== 'web') browser.hide();
    files.setVisible(!panel.hidden && tab?.kind === 'file');
    terminal.setVisible(!panel.hidden && tab?.kind === 'terminal');
    renderTabs();
    onLayout?.();
    if (panel.hidden) { save(); return; }
    if (tab.kind === 'web') browser.show(tab.ref);
    else if (tab.kind === 'file') files.show(tab.ref);
    else if (tab.kind === 'diff') renderDiff(tab);
    else if (tab.kind === 'page') renderPageTab(tab);
    else if (tab.kind === 'sources') renderSources(tab);
    else if (tab.kind === 'doc') renderDoc(tab);
    else if (tab.kind === 'read') { const key = readKey.get(tab.id) || ''; readKey.delete(tab.id); reading.show(tab.ref, { key }); }
    else if (tab.kind === 'studio') mountStudio(tab);
    else if (tab.kind === 'tool') {
      const [page, sub] = (TOOL_PAGES[tab.ref]?.[2] || tab.ref).split(':'), want = toolSub.get(tab.ref);
      toolSub.delete(tab.ref);
      if (seq === renderSeq) await canvas.open(page, want || sub || '');
    }
    save();
  }
  function activate(id) { if (!tabs.some(t => t.id === id)) return; active = id; open = true; show(); }
  function add(tab, { focus = true } = {}) {
    tabs.push(tab);
    if (tabs.length > 16) { const drop = tabs.find(t => t.id !== tab.id && t.id !== active); if (drop) close(drop.id, { silent: true }); }
    if (focus) activate(tab.id); else { renderTabs(); save(); }
    return tab;
  }
  function close(id, { silent = false } = {}) {
    const index = tabs.findIndex(t => t.id === id);
    if (index < 0) return;
    const [tab] = tabs.splice(index, 1);
    if (tab.kind === 'web') browser.remove(tab.ref);
    if (tab.kind === 'file' && tab.ref) files.remove(tab.ref);
    if (active === id) active = tabs[Math.min(index, tabs.length - 1)]?.id || '';
    if (!tabs.length) open = false;
    if (!silent) show();
  }

  // ---- 打开各种东西 ----
  function openWeb(url = '', { newTab: fresh = false } = {}) {
    // 第六轮: 已经有一个空的新标签页就切过去 (以前每按一次 Ctrl+T 多一个"新标签页")
    const blank = !url && fresh ? tabs.find(t => t.kind === 'web' && !browser.info(t.ref)?.url) : null;
    if (blank) { activate(blank.id); browser.focusAddress(); return blank; }
    let tab = tabs.find(t => t.id === active && t.kind === 'web');
    if (!tab || fresh || (url && browser.info(tab.ref)?.url)) tab = add(newTab('web', browser.create({})), { focus: false });
    activate(tab.id);
    if (url) browser.open(url, tab.ref); else browser.focusAddress();
    return tab;
  }
  function openFile(path = '') {
    const ref = String(path || '').replace(/\\/g, '/').replace(/^\.\//, '');
    const found = tabs.find(t => t.kind === 'file' && t.ref === ref);
    if (found) { activate(found.id); return; }
    if (ref) {
      const recent = read('wm.panel.files.' + workspace(), []).filter(r => r.path !== ref);
      recent.unshift({ path: ref, at: Date.now() });
      write('wm.panel.files.' + workspace(), recent.slice(0, 30));
    }
    add(newTab('file', ref));
  }
  function openDiff(calls, title = '') {
    const ref = [...new Set(calls)];
    const found = tabs.find(t => t.kind === 'diff' && JSON.stringify(t.ref) === JSON.stringify(ref));
    if (found) { activate(found.id); return; }
    add(newTab('diff', ref, title));
  }
  function openPage(name) {
    if (!PAGES[name]) return;
    const found = tabs.find(t => t.kind === 'page' && t.ref === name);
    found ? activate(found.id) : add(newTab('page', name));
  }
  // 一轮的来源 (第六轮, 学 ChatGPT): 只开一个"来源"标签, 点别的轮的"来源"就换内容, 不越开越多
  function openSources(urls) {
    const ref = [...new Set((urls || []).filter(u => /^https?:\/\//i.test(u)))].slice(0, 30);
    if (!ref.length) return;
    const found = tabs.find(t => t.kind === 'sources');
    if (found) { found.ref = ref; activate(found.id); return; }
    add(newTab('sources', ref));
  }
  function renderSources(tab) {
    const urls = Array.isArray(tab.ref) ? tab.ref : [];
    const chev = '<svg class="chev" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m7 10 5 5 5-5"/></svg>';
    views.sources.innerHTML = `<div class="wb-sources-view"><header><strong>这一轮的来源</strong><span>${urls.length} 个网址 · 点一条在右侧打开</span></header>
      <div class="wb-src-list">${urls.map((url, i) => {
        let path = '';
        try { const u = new URL(url); path = (u.pathname + u.search).replace(/^\/$/, ''); } catch { path = url; }
        return `<div class="wb-src-row"><button type="button" class="wb-src-main" data-src-open="${esc(url)}" title="${esc(url)}"><span class="wb-src-n">${i + 1}</span><span class="wb-src-copy"><strong>${esc(host(url).replace(/^www\./, ''))}</strong><small>${esc(path || url)}</small></span></button><button type="button" class="wb-open-with" data-open-menu="url" data-target="${esc(url)}" aria-haspopup="menu">打开方式${chev}</button></div>`;
      }).join('')}</div></div>`;
  }
  // 任务与成果 (workbench-studio.js) 开在面板的标签里 (第六轮第三批, 以前是挡住整个工作台的大弹窗): 弹窗里的头部、分类条、正文、底下那行
  // 第一次显示时整块搬进来 (元素 id 不变, studio 那边照样找得到); 任务 / 成果共用一个标签, 扩展 / 权限 / 成长各开各的
  function openStudioTab(next = 'tasks') {
    if (!STUDIO_PAGES[next]) next = 'tasks';
    const found = tabs.find(t => t.kind === 'studio' && studioGroup(t.ref) === studioGroup(next));
    if (found) { found.ref = next; activate(found.id); return; }
    add(newTab('studio', next));
  }
  function mountStudio(tab) {
    const view = views.studio, dialog = document.getElementById('studioDialog');
    if (dialog && !view.childElementCount) view.append(...[...dialog.children]);
    view.dataset.group = studioGroup(tab.ref) === 'task' ? 'task' : 'page';
    const heading = view.querySelector('#studioTitle');
    if (heading) heading.textContent = STUDIO_PAGES[tab.ref]?.[0] || '任务与成果';
    onStudioTab?.(tab.ref);
  }
  const studioShown = () => open && !panel.hidden && tabs.find(t => t.id === active)?.kind === 'studio';
  // 标签里点了"任务 | 成果": 记下在哪一页 (切走再切回来、刷新以后还在这一页)
  function studioTab(ref) {
    const tab = tabs.find(t => t.id === active && t.kind === 'studio');
    if (tab && STUDIO_PAGES[ref] && studioGroup(tab.ref) === studioGroup(ref)) { tab.ref = ref; save(); }
  }
  // 资料本: 直接开到某一本 (出处卡片"在资料本里看"、输入框底下的资料本按钮)
  const toolSub = new Map();
  function openNotebook(id = '') {
    if (id) toolSub.set('notebooks', id);
    openTool('notebooks');
  }
  // 资料本里的原文件 (PDF 跳页: …&file=1#page=12): 同源页面开在"原文"标签里 (网页标签挡着本机服务端端口)
  function openDoc(url, title = '原文') {
    if (!docURL(url)) return;
    const found = tabs.find(t => t.kind === 'doc' && t.ref?.url === url);
    if (found) { activate(found.id); return; }
    add(newTab('doc', { url, title }));
  }
  // 阅读面板: 同一份资料已经开着就激活并跳到出处 (key), 没有就新开一个标签
  function openReading(nid, no, key = '', name = '') {
    no = Number(no);
    if (!/^nb-[0-9a-f]{8}$/.test(nid || '') || !(no > 0)) return;
    let tab = tabs.find(t => t.kind === 'read' && t.ref?.nid === nid && t.ref?.no === no);
    if (!tab) tab = add(newTab('read', { nid, no }, name), { focus: false });
    if (key) readKey.set(tab.id, key);
    activate(tab.id);
  }
  const hasReading = nid => tabs.some(t => t.kind === 'read' && t.ref?.nid === nid);
  function renderDoc(tab) {
    const view = views.doc, url = tab.ref?.url || '';
    let frame = view.querySelector('iframe');
    if (!frame) {
      view.innerHTML = `<header class="wb-doc-bar"><span class="wb-doc-title"></span><span class="grow"></span><button type="button" class="wb-doc-link" data-doc-external title="这里显示不出来时，用电脑上的浏览器看">用系统浏览器打开</button></header>`;
      frame = document.createElement('iframe');
      frame.className = 'wb-page-frame';
      view.append(frame);
    }
    view.querySelector('.wb-doc-title').textContent = tab.ref?.title || '原文';
    if (frame.dataset.url !== url) { frame.dataset.url = url; frame.title = tab.ref?.title || '原文'; frame.src = url; }
  }
  function openTool(name) {
    if (PAGES[name]) { openPage(name); return; }
    if (name === 'terminal') { const found = tabs.find(t => t.kind === 'terminal'); found ? activate(found.id) : add(newTab('terminal', 'terminal')); return; }
    if (name === 'files') { openFile(''); return; }
    if (name === 'browser') { openWeb(''); return; }
    if (!TOOL_PAGES[name]) return;
    const found = tabs.find(t => t.kind === 'tool' && t.ref === name);
    found ? activate(found.id) : add(newTab('tool', name));
  }

  // ---- 搜索 (第二批: Ctrl+P 命令面板和新标签页的搜索框共用): 按组列 —— 网页 / 文件 / 最近 / 会话 / 命令 / 工具页 ----
  const TOOL_ENTRIES = () => [
    ['files', 'folder', '文件', 'Ctrl+P', '按名字找整个工作区'], ['terminal', 'terminal', '终端', 'Ctrl+`', ''], ['notebooks', 'book', '资料本', '', '跨会话的学习资料，回答带出处'],
    ['mail', 'mail', '邮箱', '', '连接、汇报、读信记录'],
    ...Object.entries(TOOL_PAGES).filter(([key]) => key !== 'notebooks').map(([key, [title, glyph]]) => [key, glyph, title, '', '']),
    ['studio:tasks', 'agents', '任务与成果', '', '这一轮的经过、计划、成果和验收'],
    ...['extensions', 'permissions', 'growth'].map(key => [`studio:${key}`, STUDIO_PAGES[key][1], STUDIO_PAGES[key][0], '', '']),
  ];
  const runTool = key => (key === 'files' ? openPalette() : key === 'notebooks' ? openNotebook('') : key.startsWith('studio:') ? openStudio?.(key.slice(7)) : openTool(key));
  const matcher = q => { const words = String(q || '').toLowerCase().split(/\s+/).filter(Boolean); return text => words.every(w => String(text || '').toLowerCase().includes(w)); };
  const looksLikeUrl = q => /^(https?:\/\/|www\.)\S+$/i.test(q) || /^[\w-]+(\.[\w-]+)+(:\d+)?(\/\S*)?$/.test(q) || /^BV[0-9A-Za-z]{10}$/.test(q);
  const goWeb = q => { try { openWeb(browserURL(q)); } catch (error) { notify(error.message); } };
  function recentItems() {
    const files = read('wm.panel.files.' + workspace(), []).filter(r => r?.path).map(r => ({ kind: 'file', path: r.path, title: base(r.path), sub: r.path, at: r.at }));
    const web = read('wm.browser.history', []).filter(x => x?.url).map(x => ({ kind: 'web', url: x.url, title: x.title || host(x.url), sub: host(x.url), at: x.at }));
    return [...files, ...web].sort((a, b) => (b.at || 0) - (a.at || 0));
  }
  function quick(q, kinds) {
    const hit = matcher(q), out = [];
    if (kinds.includes('web') && q) out.push(looksLikeUrl(q) ? { kind: 'web', group: '网页', icon: 'globe', title: `打开 ${q}`, sub: '在这个标签里打开', run: () => goWeb(q) }
      : { kind: 'web', group: '网页', icon: 'search', title: `上网搜「${q}」`, sub: '用浏览器设置里选的搜索引擎', run: () => goWeb(q) });
    if (kinds.includes('recent') && q) for (const r of recentItems().filter(r => hit(`${r.title} ${r.sub}`)).slice(0, 5)) out.push({ kind: r.kind, group: '最近', icon: r.kind === 'file' ? 'file' : 'globe', title: r.title, sub: r.sub, run: () => (r.kind === 'file' ? openFile(r.path) : openWeb(r.url, { newTab: true })) });
    if (kinds.includes('sessions') && q) {
      const all = (getState()?.workspaces || []).flatMap(w => (w.sessions || []).map(s => ({ ...s, ws: w.name }))).filter(s => s.title && s.title !== '新会话' && hit(s.title));
      for (const s of all.sort((a, b) => (b.updated || 0) - (a.updated || 0)).slice(0, 6)) out.push({ kind: 'session', group: '会话', icon: 'chat', title: s.title, sub: s.ws || '', run: () => openSession(s.id) });
    }
    if (kinds.includes('commands')) for (const c of getCommands?.() || []) if (!q || hit(`${c.title} ${c.keywords || ''}`)) out.push({ kind: 'command', group: '命令', icon: c.icon || 'cmd', title: c.title, sub: c.sub || '', kbd: c.kbd || '', run: c.run });
    if (kinds.includes('tools')) for (const [key, glyph, title, kbd, sub] of TOOL_ENTRIES()) if (!q || hit(`${title} ${sub}`)) out.push({ kind: 'tool', group: '工具页', icon: glyph, title, sub, kbd, run: () => runTool(key) });
    return out;
  }
  // 结果按组画 (组名一行, 一项一行: 图标、名字、灰字、右边快捷键)
  function paintList(box, items, selected, empty) {
    let group = '';
    box.innerHTML = items.length ? items.map((item, i) => {
      const head = item.group !== group ? `<div class="wb-qgroup" role="presentation">${esc(item.group)}</div>` : '';
      group = item.group;
      return `${head}<button type="button" role="option" aria-selected="${i === selected}" data-pick="${i}" data-kind="${item.kind}">${ic(item.icon, 15)}<strong>${esc(item.title)}</strong>${item.sub ? `<small>${esc(item.sub)}</small>` : ''}${item.kbd ? `<kbd>${esc(item.kbd)}</kbd>` : ''}</button>`;
    }).join('') : `<p class="wb-empty-note">${esc(empty)}</p>`;
    box.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' });
  }

  // ---- 新标签页 (第二批: 学 Notion 的"最近访问"、Linear 的搜索框): 搜索框、工具一行小按钮、最近 (横排小卡)、推荐 (紧凑行) ----
  function renderHome(el) {
    const session = getSession();
    const changes = session ? collectChanges(session.events, session.workspace) : [];
    const artifacts = [];
    for (const event of session?.events || []) if (event.type === 'artifact' && event.artifact && !artifacts.some(a => a.path === event.artifact.path)) artifacts.push(event.artifact);
    const bookmarks = read('wm.browser.bookmarks', []).filter(x => x?.url);
    const recent = recentItems().slice(0, 8);
    const changedNames = new Set(changes.map(f => f.name.toLowerCase()));  // 改过的文件只列一次 (成果里登记的大多就是它们)
    const picks = [
      ...changes.slice(0, 5).map(f => ({ attrs: `data-open-file="${esc(f.name)}"`, glyph: 'file', title: base(f.name), sub: f.name, tag: f.created ? '<em class="wb-tag">新建</em>' : `<em class="wb-add">+${f.add}</em><em class="wb-del">−${f.del}</em>` })),
      ...artifacts.map(a => ({ a, rel: relativePath(a.path || '', workspace()) })).filter(({ rel }) => !changedNames.has(rel.toLowerCase())).slice(0, 4)
        .map(({ a, rel }) => ({ attrs: `data-open-file="${esc(rel)}"`, glyph: 'box', title: a.label || a.name || base(a.path), sub: '这次的成果' })),
      ...bookmarks.slice(0, 4).map(b => ({ attrs: `data-url="${esc(b.url)}"`, glyph: 'star', title: b.title || host(b.url), sub: host(b.url) })),
    ].slice(0, 8);
    const sites = [['哔哩哔哩', 'https://www.bilibili.com/'], ['GitHub', 'https://github.com/'], ['必应', 'https://www.bing.com/']];
    const row = r => `<button type="button" class="wb-home-row" ${r.attrs}>${ic(r.glyph, 15)}<strong>${esc(r.title)}</strong><small>${esc(r.sub)}</small>${r.tag || ''}</button>`;
    const card = r => `<button type="button" class="wb-home-card" ${r.kind === 'file' ? `data-open-file="${esc(r.path)}"` : `data-url="${esc(r.url)}"`} title="${esc(r.kind === 'file' ? r.path : r.url)}"><span class="wb-home-card-ic">${ic(r.kind === 'file' ? 'file' : 'globe', 15)}</span><strong>${esc(r.title)}</strong><small>${esc(r.sub)}</small><time>${esc(when(r.at))}</time></button>`;
    const tools = [['files', 'folder', '文件', 'Ctrl+P'], ['terminal', 'terminal', '终端', 'Ctrl+`'], ['agents', 'agents', '协作任务'], ['automation', 'clock', '自动化任务'], ['notebooks', 'book', '资料本'], ['mail', 'mail', '邮箱']];
    el.innerHTML = `<div class="wb-home">
      <label class="wb-home-search">${ic('search')}<input type="search" placeholder="搜文件、工具、网页，或输入网址" aria-label="搜文件、工具、网页，或输入网址"></label>
      <div class="wb-home-results wb-qlist" role="listbox" hidden></div>
      <div class="wb-home-main">
        <section><h3>工具</h3><div class="wb-home-tools">${tools.map(([key, glyph, title, kbd]) => `<button type="button" data-home="${key}" title="${esc(title)}${kbd ? ` (${kbd})` : ''}">${ic(glyph, 15)}<span>${esc(title)}</span></button>`).join('')}
          <details class="wb-home-more"><summary>${ic('grid', 15)}<span>更多工具…</span></summary><div>${['presets', 'capabilities', 'context', 'experiences', 'life', 'extensions', 'community', 'data', 'updates'].map(name => `<button type="button" data-home="${name}">${ic(TOOL_PAGES[name][1], 15)}<span>${TOOL_PAGES[name][0]}</span></button>`).join('')}${['extensions', 'permissions', 'growth'].map(key => `<button type="button" data-studio-page="${key}">${ic(STUDIO_PAGES[key][1], 15)}<span>${STUDIO_PAGES[key][0]}</span></button>`).join('')}</div></details></div></section>
        <section><h3>最近</h3>${recent.length ? `<div class="wb-home-cards">${recent.map(card).join('')}</div>` : '<p class="wb-empty-note">打开过的文件和网页会出现在这里。</p>'}</section>
        <section><h3>推荐</h3><div class="wb-home-rows">${(picks.length ? picks : sites.map(([title, url]) => ({ attrs: `data-url="${url}"`, glyph: 'globe', title, sub: host(url) }))).map(row).join('')}</div></section>
      </div></div>`;
    bindHomeSearch(el);
  }
  function bindHomeSearch(el) {
    const input = el.querySelector('.wb-home-search input'), box = el.querySelector('.wb-home-results'), main = el.querySelector('.wb-home-main');
    let items = [], selected = 0, timer = 0, seq = 0;
    const update = async () => {
      const q = input.value.trim(), mine = ++seq;
      box.hidden = !q;
      main.hidden = !!q;
      if (!q) return;
      items = quick(q, ['web', 'recent', 'tools']);
      selected = 0;
      paintList(box, items, selected, '');
      if (!getSession()?.id) return;
      try {
        const data = await canvasRequest('/api/agent/workspace?' + new URLSearchParams({ action: 'find', id: sid(), q }));
        if (mine !== seq) return;
        const files = (data.items || []).slice(0, 6).map(f => ({ kind: 'file', group: '文件', icon: 'file', title: f.name, sub: f.path, run: () => openFile(f.path) }));
        items.splice(items[0]?.group === '网页' ? 1 : 0, 0, ...files);
        paintList(box, items, selected, '');
      } catch { /* 旧服务端没有 find: 只列别的 */ }
    };
    input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(update, 120); });
    input.addEventListener('keydown', event => {
      if ((event.key === 'ArrowDown' || event.key === 'ArrowUp') && items.length) { event.preventDefault(); selected = (selected + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length; paintList(box, items, selected, ''); }
      else if (event.key === 'Enter') { event.preventDefault(); const q = input.value.trim(); if (q) (items[selected] || { run: () => goWeb(q) }).run(); }
      else if (event.key === 'Escape' && input.value) { event.preventDefault(); input.value = ''; update(); }
    });
    box.addEventListener('click', event => { const b = event.target.closest('[data-pick]'); if (b) items[Number(b.dataset.pick)]?.run(); });
  }

  // ---- "页面"标签: 服务端自己的页面 (邮箱…) ----
  const pageFrames = new Map();  // 页面名 -> iframe (换标签、换会话都留着, 不重载)
  async function renderPageTab(tab) {
    const spec = PAGES[tab.ref], view = views.page;
    if (!spec) return;
    view.querySelectorAll('.wb-page-note').forEach(el => el.remove());
    for (const frame of view.querySelectorAll('iframe')) frame.hidden = frame.dataset.page !== tab.ref;
    if (pageFrames.get(tab.ref)?.isConnected) return;
    view.insertAdjacentHTML('beforeend', `<div class="wb-page-note">正在打开${esc(spec.title)}…</div>`);
    let status = 0;
    try { status = (await fetch(spec.url, { cache: 'no-store' })).status; } catch { status = 0; }
    if (active !== tab.id || !open) return;
    view.querySelectorAll('.wb-page-note').forEach(el => el.remove());
    if (status < 200 || status >= 300) {
      const why = status === 404 ? `${spec.what}要等服务端重启后才有（重启前会先问你）。` : status ? `服务端回了 ${status}，稍后再试。` : '连不上本机服务端，等它起来再试。';
      view.insertAdjacentHTML('beforeend', `<div class="wb-page-note"><span class="wb-page-mark">${ic(spec.icon, 26)}</span><strong>${esc(spec.title)}还打不开</strong><p>${esc(why)}</p>
        <div><button type="button" data-page-retry="${esc(tab.ref)}">重试</button>${window.chrome?.webview ? '' : `<a href="${esc(spec.url)}" target="_blank" rel="noopener">在新标签页打开</a>`}</div></div>`);
      return;
    }
    const frame = document.createElement('iframe');
    frame.className = 'wb-page-frame';
    frame.dataset.page = tab.ref;
    frame.title = spec.title;
    frame.src = spec.url;
    frame.addEventListener('load', () => hookLinks(frame));
    view.append(frame);
    pageFrames.set(tab.ref, frame);
  }
  // 页面里点到外面的网址 (比如去邮箱网站生成授权码): 开在工作台的网页标签里 (隔离的浏览器资料), 浏览器里打开的工作台就开新标签页
  function hookLinks(frame) {
    let doc = null;
    try { doc = frame.contentDocument; } catch { return; }
    doc?.addEventListener('click', event => {
      const link = event.target.closest?.('a[href]');
      if (!link) return;
      let url;
      try { url = new URL(link.href, frame.contentWindow.location.href); } catch { return; }
      if (url.origin === location.origin || !/^https?:$/.test(url.protocol)) return;
      event.preventDefault();
      if (window.chrome?.webview) openWeb(url.href, { newTab: true });
      else window.open(url.href, '_blank', 'noopener,noreferrer');
    });
  }

  // ---- 变更 (diff): 这些工具调用改了哪些文件; 能整体撤销 ----
  function renderDiff(tab) {
    const session = getSession(), view = views.diff;
    const wanted = new Set(tab.ref || []);
    const filesChanged = session ? collectChanges(session.events.filter(e => e.type === 'tool_result' && wanted.has(e.id)), session.workspace) : [];
    if (!filesChanged.length) { view.innerHTML = '<div class="wb-diff"><p class="wb-empty-note">这些改动不在当前会话里了。</p></div>'; return; }
    const add = filesChanged.reduce((n, f) => n + f.add, 0), del = filesChanged.reduce((n, f) => n + f.del, 0);
    view.innerHTML = `<div class="wb-diff"><header class="wb-diff-head"><strong>${filesChanged.length === 1 ? esc(filesChanged[0].name) : `${filesChanged.length} 个文件`}</strong><span class="wb-add">+${add}</span><span class="wb-del">−${del}</span><span class="grow"></span><button type="button" data-diff-undo>撤销这些改动</button></header>
      ${filesChanged.map(f => {
        const rows = hunks(diffLines(f.before, f.after).rows, 3);
        return `<section class="wb-diff-file"><header><button type="button" class="wb-linkish" data-open-file="${esc(f.name)}">${esc(f.name)}</button>${f.created ? '<span class="wb-tag">新建</span>' : ''}<span class="wb-add">+${f.add}</span><span class="wb-del">−${f.del}</span></header>
          <div class="wb-diff-rows">${rows.map(r => r.t === 'gap' ? `<div class="wb-diff-gap">⋯ ${r.count} 行没有改动</div>`
            : `<div class="wb-diff-row ${r.t === '+' ? 'add' : r.t === '-' ? 'del' : ''}"><span class="n">${r.a ?? ''}</span><span class="n">${r.b ?? ''}</span><code>${esc(r.text) || ' '}</code></div>`).join('')}</div></section>`;
      }).join('')}</div>`;
  }
  async function undo(calls) {
    const session = getSession();
    if (!session) return null;
    const result = await canvasRequest('/api/agent/workspace', { action: 'revert', id: session.id, calls });
    if (!result.ok) {
      notify('没有撤销：' + result.blocked.map(b => `${b.path}（${b.why}）`).join('；'));
      return result;
    }
    const n = result.reverted.length + result.archived.length;
    notify(`已撤销 ${n} 个文件的改动${result.archived.length ? `，新建的 ${result.archived.length} 个挪进了 .archive` : ''}`);
    await reload?.();
    files.refreshOpen();
    return result;
  }

  // ---- Ctrl+P 命令面板 (第二批, 学 Linear): 一个框里分组搜 文件 / 会话 / 命令 / 工具页, 右边写快捷键; 输入 > 只搜命令 ----
  // 文件是服务端 find 找整个工作区 (旧服务端没有就提示); 文件组排在最前, 结果回来时选中第一个文件
  let palette = null, paletteSeq = 0;
  function openPalette() {
    palette?.remove();
    palette = document.createElement('div');
    palette.className = 'wb-palette';
    palette.innerHTML = `<div class="wb-palette-box" role="dialog" aria-label="命令面板"><label>${ic('search')}<input type="search" placeholder="${getSession()?.id ? `找文件（${esc(base(workspace()))}）、会话、命令或工具页` : '找会话、命令或工具页'}" aria-label="命令面板"></label><div class="wb-palette-list wb-qlist" role="listbox"></div><footer>↑↓ 选择 · Enter 打开 · 输入 &gt; 只搜命令 · Esc 关闭</footer></div>`;
    document.body.append(palette);
    const input = palette.querySelector('input'), results = palette.querySelector('.wb-palette-list');
    let items = [], files = [], note = '', selected = 0, timer = 0;
    const build = (keep = false) => {
      const raw = input.value.trim(), commandsOnly = raw.startsWith('>'), q = commandsOnly ? raw.slice(1).trim() : raw;
      const fileItems = commandsOnly ? [] : files.map(f => ({ kind: 'file', group: '文件', icon: 'file', title: f.name, sub: f.path, run: () => openFile(f.path) }));
      items = [...fileItems, ...quick(q, commandsOnly ? ['commands'] : q ? ['sessions', 'commands', 'tools'] : ['commands'])];
      selected = keep ? Math.min(selected, Math.max(0, items.length - 1)) : 0;
      paintList(results, items, selected, commandsOnly ? '没有这个命令' : note || (raw ? '没找到' : '正在列最近改过的文件…'));
      if (note && items.length) results.insertAdjacentHTML('beforeend', `<p class="wb-empty-note">${esc(note)}</p>`);
    };
    const search = async () => {
      const seq = ++paletteSeq, raw = input.value.trim();
      if (!getSession()?.id || raw.startsWith('>')) { files = []; note = ''; build(); return; }
      files = files.filter(f => matcher(raw)(f.path));  // 先按新的字筛一遍旧结果, 服务端回来再换
      build();
      try {
        const data = await canvasRequest('/api/agent/workspace?' + new URLSearchParams({ action: 'find', id: sid(), q: raw }));
        if (seq !== paletteSeq) return;
        files = (data.items || []).slice(0, 8);
        note = data.truncated ? '文件很多，只找了一部分；名字写长一点更准。' : '';
      } catch (error) {
        if (seq !== paletteSeq) return;
        files = [];
        note = /不支持/.test(error.message) ? '全工作区找文件要等服务端重启后才有；先在文件树里浏览。' : error.message;
      }
      build();
    };
    const pick = i => { const item = items[i]; if (!item) return; closePalette(); item.run(); };
    input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(search, 120); });
    input.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); if (!items.length) return; selected = (selected + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length; build(true); }
      if (event.key === 'Enter') { event.preventDefault(); pick(selected); }
      if (event.key === 'Escape') { event.preventDefault(); closePalette(); }
    });
    palette.addEventListener('click', event => {
      if (event.target === palette) { closePalette(); return; }
      const b = event.target.closest('[data-pick]');
      if (b) pick(Number(b.dataset.pick));
    });
    input.focus();
    build();
    search();
  }
  function closePalette() { palette?.remove(); palette = null; }

  // ---- 宽度 ----
  const resizer = panel.querySelector('.wb-panel-resizer');
  let width = Number(read('wm.panel.width', 0)) || Math.round(innerWidth * 0.42);  // 没拖过就占四成多 (Codex 的右侧面板差不多这么宽)
  function fit(next = width) {
    const side = app.classList.contains('sidebar-collapsed') || innerWidth <= 860 ? 0 : 252;
    const room = innerWidth - side - 400;  // 中间对话至少留 400
    // 放不下 (窗口窄、左栏开着): 面板浮在对话上面, 不去收左栏 (以前自动收左栏, 开关面板时左右来回跳)
    app.classList.toggle('panel-overlay', innerWidth > 860 && room < 340);
    const max = Math.max(340, room);
    width = Math.round(Math.max(340, Math.min(max, next)));
    app.style.setProperty('--panel-w', width + 'px');
    resizer.setAttribute('aria-valuenow', String(width));
    return width;
  }
  resizer.addEventListener('pointerdown', event => { if (event.button !== 0) return; event.preventDefault(); app.classList.remove('panel-wide'); resizer.setPointerCapture(event.pointerId); resizer.dataset.dragging = '1'; });
  resizer.addEventListener('pointermove', event => { if (resizer.hasPointerCapture(event.pointerId)) fit(innerWidth - event.clientX); });
  resizer.addEventListener('lostpointercapture', () => { delete resizer.dataset.dragging; write('wm.panel.width', width); });
  resizer.addEventListener('keydown', event => { if (['ArrowLeft', 'ArrowRight'].includes(event.key)) { event.preventDefault(); fit(width + (event.key === 'ArrowLeft' ? 24 : -24)); write('wm.panel.width', width); } });
  addEventListener('resize', () => fit());
  new MutationObserver(() => fit()).observe(app, { attributes: true, attributeFilter: ['class'] });
  fit();

  // ---- 点击和快捷键 ----
  panel.addEventListener('click', async event => {
    const b = event.target.closest('button');
    if (!b || b.disabled) return;
    try {
      if (b.dataset.tabOpen) activate(b.dataset.tabOpen);
      else if (b.dataset.tabClose) close(b.dataset.tabClose);
      else if (b.dataset.panel === 'new') openWeb('', { newTab: true });
      else if (b.dataset.panel === 'wide') app.classList.toggle('panel-wide');
      else if (b.dataset.panel === 'hide') setOpen(false);
      else if (b.dataset.home === 'files') openPalette();
      else if (b.dataset.pageRetry) { pageFrames.delete(b.dataset.pageRetry); const tab = tabs.find(t => t.id === active); if (tab?.kind === 'page') renderPageTab(tab); }
      else if (b.dataset.home) openTool(b.dataset.home);
      else if (b.dataset.studioPage) openStudio?.(b.dataset.studioPage);
      else if (b.dataset.srcOpen) openWeb(b.dataset.srcOpen, { newTab: true });
      else if (b.hasAttribute('data-doc-external')) {  // 资料本原文: 交给宿主用系统浏览器开 (浏览器里打开的工作台就开新标签页)
        const url = new URL(views.doc.querySelector('iframe')?.dataset.url || '/', location.origin).href;
        if (window.chrome?.webview) tell?.('browser', { action: 'external', url, tab_id: 'main' }); else window.open(url, '_blank', 'noopener');
      }
      else if (b.dataset.openMenu) openWith?.(b);
      else if (b.dataset.tabMore !== undefined) toggleTabMenu(b);
      else if (b.dataset.openFile !== undefined) openFile(b.dataset.openFile);
      else if (b.hasAttribute('data-diff-undo')) {
        const tab = tabs.find(t => t.id === active);
        b.disabled = true;
        const result = await undo(tab?.ref || []);
        if (result?.ok) renderDiff(tab);
        b.disabled = false;
      }
    } catch (error) { notify(error.message); b.disabled = false; }
  });
  panel.addEventListener('auxclick', event => { const t = event.target.closest('[data-tab-open]'); if (t && event.button === 1) close(t.dataset.tabOpen); });
  list.addEventListener('keydown', event => {
    if (!['ArrowLeft', 'ArrowRight'].includes(event.key) || !event.target.matches('[role=tab]')) return;
    event.preventDefault();
    const i = tabs.findIndex(t => t.id === active), next = tabs[(i + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length];
    if (next) { activate(next.id); list.querySelector('.wb-ptab.on [role=tab]')?.focus(); }
  });
  document.addEventListener('keydown', event => {
    if (event.defaultPrevented || document.querySelector('dialog[open]') || app.classList.contains('settings-mode')) return;
    const mod = event.ctrlKey || event.metaKey, k = event.key.toLowerCase();
    if (mod && !event.shiftKey && !event.altKey && k === 'p') { event.preventDefault(); openPalette(); }
    else if (mod && !event.shiftKey && !event.altKey && k === 'j') { event.preventDefault(); setOpen(!open || panel.hidden); }
    else if (event.ctrlKey && event.code === 'Backquote') { event.preventDefault(); const t = tabs.find(x => x.id === active); t?.kind === 'terminal' && !panel.hidden ? setOpen(false) : openTool('terminal'); }
    else if (mod && !event.shiftKey && !event.altKey && k === 't') { event.preventDefault(); openWeb('', { newTab: true }); }  // 面板关着也开 (像浏览器)
    else if (mod && !event.shiftKey && !event.altKey && k === 'w' && panel.contains(document.activeElement) && active) { event.preventDefault(); close(active); }
    else if (event.ctrlKey && event.altKey && k === 's' && !panel.hidden) { event.preventDefault(); quoteActive(); }
  });
  async function quoteActive() {
    const tab = tabs.find(t => t.id === active);
    if (tab?.kind === 'web') await browser.quote();
    else if (tab?.kind === 'file') await files.quote();
    document.getElementById('input')?.focus();
  }
  function setOpen(value) {
    if (value && !tabs.length) { openWeb('', { newTab: true }); return; }
    open = value;
    show();
  }

  return {
    browser, files, canvas,
    openWeb, openFile, openDiff, openTool, openPage, openSources, openNotebook, openDoc, openReading, hasReading, openStudioTab, studioShown, studioTab, openPalette, undo, setOpen, revealQuote,
    isOpen: () => open && !panel.hidden,
    toggle() { setOpen(!(open && !panel.hidden)); },
    sessionChanged() {
      const next = 'wm.panel.' + (sid() || 'none');
      if (next === key) { terminal.sessionChanged(); return; }
      save();
      teardown();
      key = next;
      restore();
      terminal.sessionChanged();
      files.sessionChanged();
      canvas.sessionChanged();
      show();
    },
    homeChanged() { browser.homeChanged(); const tab = tabs.find(t => t.id === active); if (tab?.kind === 'diff' && !panel.hidden) renderDiff(tab); },
    refreshTasks: () => canvas.refreshTasks(),
    onHost: message => browser.onHost(message),
    suspend: value => browser.suspend(value),
  };
}
