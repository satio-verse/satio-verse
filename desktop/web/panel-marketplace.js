// 随应用分发的本地内容包目录。读取目录不联网；安装仍由宿主 ext-inspect/ext-install 校验与执行。
// ctx.call 必须使用 panel.js 的 callExtension，保留保存队列与安装时的编辑锁。
const CATALOG_URL = '/pet/plugin-catalog.json';
const PACK_ROOT = '/pet/plugin-packs/';
const ID_RE = /^[a-z0-9][a-z0-9._-]{0,63}$/;
const state = {
  catalog: null, catalogError: '', installed: null, stateError: '', loading: false,
  selected: null, query: '', category: 'all', status: 'all', records: new Map(), installing: '', notice: '',
};
const ICONS = {
  workspace: '<path d="M3 7h7l2 2h9v10H3z"/><path d="M3 7V5h7l2 2"/>',
  code: '<path d="m8 7-5 5 5 5M16 7l5 5-5 5M14 4l-4 16"/>',
  writing: '<path d="m15 4 5 5-10 10-6 1 1-6zM13 6l5 5"/>',
};
const repaint = (ctx) => { if (ctx.isTab()) ctx.render(); };
const recordFor = (id) => {
  if (!state.records.has(id)) state.records.set(id, { preview: null, text: '', error: '', loading: false });
  return state.records.get(id);
};
const installedFor = (id) => state.installed?.find((item) => item.id === id);
const byteSize = (n) => n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`;

function catalogFrom(data) {
  if (data?.version !== 1 || data?.source !== 'bundled' || !Array.isArray(data.entries) || !Array.isArray(data.categories)
    || data.entries.length > 64 || data.categories.some((item) => !ID_RE.test(item.id) || typeof item.name !== 'string')) throw new Error('本地插件目录格式不正确。');
  const ids = new Set();
  for (const item of data.entries) {
    if (!ID_RE.test(item.id) || ids.has(item.id) || item.file !== `${item.id}.wmext`
      || !/^[a-f0-9]{64}$/.test(item.sha256) || !Number.isInteger(item.size_bytes) || item.size_bytes < 1 || item.size_bytes > 2 * 1024 * 1024
      || ['name', 'description', 'author', 'license', 'version', 'example', 'category'].some((key) => typeof item[key] !== 'string' || !item[key].trim())
      || ![item.skills, item.features, item.tags].every((list) => Array.isArray(list) && list.every((value) => typeof value === 'string'))
      || !data.categories.some((category) => category.id === item.category)) throw new Error('本地插件目录包含无效条目。');
    ids.add(item.id);
  }
  return data;
}

async function load(ctx, refreshCatalog = false) {
  if (state.loading) return;
  state.loading = true;
  state.stateError = '';
  if (refreshCatalog) state.catalogError = '';
  const catalogRequest = state.catalog && !refreshCatalog ? Promise.resolve(state.catalog) : fetch(CATALOG_URL, { cache: 'no-store' })
    .then((response) => { if (!response.ok) throw new Error(`无法读取随应用提供的目录（${response.status}）。`); return response.json(); }).then(catalogFrom);
  const results = await Promise.allSettled([catalogRequest, ctx.call('ext-state')]);
  if (results[0].status === 'fulfilled') { state.catalog = results[0].value; state.catalogError = ''; }
  else state.catalogError = results[0].reason?.message || '无法读取本地插件目录。';
  if (results[1].status === 'fulfilled' && results[1].value.ok !== false && Array.isArray(results[1].value.extensions)) {
    state.installed = results[1].value.extensions;
    state.stateError = '';
  } else {
    state.installed = null;
    state.stateError = results[1].status === 'fulfilled' ? results[1].value.error || '无法读取安装状态。' : results[1].reason?.message || '无法读取安装状态。';
  }
  state.loading = false;
  repaint(ctx);
}

/** Root navigation can select a catalogue entry without creating a separate page route. */
export function setMarketplaceSelection(id = null) {
  state.selected = id;
  state.notice = '';
}

/** ctx: { esc, $, $$, call, flush, setSettings, isTab, render, show, setStatus, onInstalled?, openLocalImport? } */
export function renderMarketplace(main, ctx) {
  const { esc, $, $$ } = ctx;
  const focusedSearch = document.activeElement?.id === 'marketSearch' ? { start: document.activeElement.selectionStart, end: document.activeElement.selectionEnd } : null;
  const entering = !main.querySelector('[data-marketplace-page]');
  if (entering && !state.loading) load(ctx);
  const entries = state.catalog?.entries || [];
  const selected = entries.find((item) => item.id === state.selected);
  const status = state.catalogError || state.stateError;
  const shell = `<div data-marketplace-page><h1>插件市场</h1><p class="lead">发现适合她的做事方法。当前目录随应用提供，不连接在线商店。</p>
    ${status ? `<div class="note err" role="alert">${esc(status)} <button type="button" class="btn small" id="marketRetry">重试</button></div>` : ''}
    ${state.notice ? `<p class="market-notice" role="status">${esc(state.notice)}</p>` : ''}`;
  if (!state.catalog) {
    main.innerHTML = shell + `<div class="market-empty"><h2>${state.catalogError ? '目录暂时无法打开' : '正在读取插件目录…'}</h2><p>也可以从本机导入已有的 .wmext 内容包。</p><button type="button" class="btn" id="marketImport">从本地导入</button></div></div>`;
  } else if (selected) {
    main.innerHTML = shell + detailMarkup(selected, ctx) + '</div>';
    $('#marketBack', main).addEventListener('click', () => { state.selected = null; state.notice = ''; ctx.render(); main.scrollTop = 0; });
    $('#marketPreview', main)?.addEventListener('click', () => preview(selected, ctx));
    $('#marketInstall', main)?.addEventListener('click', () => install(selected, ctx));
    $('#marketManage', main)?.addEventListener('click', () => openImport(ctx));
    $('#marketSkills', main)?.addEventListener('click', () => ctx.show('skills'));
  } else {
    const categories = state.catalog.categories || [];
    main.innerHTML = shell + `<div class="market-toolbar"><label class="market-search"><input type="search" id="marketSearch" placeholder="搜索名称、用途或技能" aria-label="搜索插件市场" value="${esc(state.query)}"></label>
      <select id="marketStatus" aria-label="筛选安装状态"><option value="all" ${state.status === 'all' ? 'selected' : ''}>全部状态</option><option value="installed" ${state.status === 'installed' ? 'selected' : ''}>已安装</option><option value="available" ${state.status === 'available' ? 'selected' : ''}>未安装</option></select>
      <button type="button" class="btn" id="marketImport">从本地导入</button></div>
      <div class="market-categories" aria-label="内容类别">${[['all', '全部'], ...categories.map((category) => [category.id, category.name])].map(([id, name]) => `<button type="button" data-market-category="${esc(id)}" class="${state.category === id ? 'on' : ''}" aria-pressed="${state.category === id}">${esc(name)}</button>`).join('')}</div>
      <p class="market-results-count hint" id="marketCount" role="status"></p><div class="market-grid" id="marketResults"></div>
      <div class="market-source-note"><b>本地内容包</b><p>这些技能由本项目编写，只包含文字内容。安装不会新增程序、工具权限，也不会自动启用技能。</p></div></div>`;
    const paint = () => paintEntries(main, ctx);
    $('#marketSearch', main).addEventListener('input', (event) => { state.query = event.target.value; paint(); });
    $('#marketStatus', main).addEventListener('change', (event) => { state.status = event.target.value; paint(); });
    $$('[data-market-category]', main).forEach((button) => button.addEventListener('click', () => {
      state.category = button.dataset.marketCategory;
      $$('[data-market-category]', main).forEach((item) => { const on = item.dataset.marketCategory === state.category; item.classList.toggle('on', on); item.setAttribute('aria-pressed', String(on)); });
      paint();
    }));
    paint();
  }
  $('#marketRetry', main)?.addEventListener('click', () => { load(ctx, true); ctx.render(); });
  $('#marketImport', main)?.addEventListener('click', () => openImport(ctx));
  if (focusedSearch && $('#marketSearch', main)) {
    const input = $('#marketSearch', main);
    input.focus({ preventScroll: true });
    if (focusedSearch.start !== null) input.setSelectionRange(focusedSearch.start, focusedSearch.end);
  }
}

function openImport(ctx) {
  if (ctx.openLocalImport) ctx.openLocalImport(); else ctx.show('ext');
}

function paintEntries(main, ctx) {
  const { esc, $, $$ } = ctx;
  const query = state.query.trim().toLowerCase();
  const entries = state.catalog.entries.filter((entry) => (!query || `${entry.name} ${entry.description} ${entry.tags.join(' ')} ${entry.skills.join(' ')}`.toLowerCase().includes(query))
    && (state.category === 'all' || entry.category === state.category)
    && (state.status === 'all' || (state.installed !== null && (state.status === 'installed' ? !!installedFor(entry.id) : !installedFor(entry.id)))));
  $('#marketCount', main).textContent = state.loading ? '正在核对安装状态…' : `${entries.length} 个内容包 · 随应用提供`;
  $('#marketResults', main).innerHTML = entries.map((entry) => `<article class="market-card"><button type="button" class="market-card-open" data-market-open="${esc(entry.id)}">
    <span class="market-icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round">${ICONS[entry.category] || ICONS.workspace}</svg></span><span class="market-card-copy"><b>${esc(entry.name)}</b><span>${esc(entry.description)}</span></span></button>
    <div class="market-card-meta"><span>${entry.skills.length} 项技能 · ${byteSize(entry.size_bytes)}</span><span class="badge ${installedFor(entry.id) ? '' : 'off'}">${installedFor(entry.id) ? '已安装' : state.installed === null ? '状态待确认' : '可安装'}</span></div></article>`).join('')
    || `<div class="market-empty"><h2>${state.installed === null && state.status !== 'all' ? '暂时无法筛选安装状态' : '没有匹配的内容包'}</h2><p>${state.installed === null && state.status !== 'all' ? '请先重试读取安装状态，或查看全部状态。' : '试试其他关键词，或切换内容类别。'}</p><button type="button" class="btn" id="marketClear">重置筛选</button></div>`;
  $$('[data-market-open]', main).forEach((button) => button.addEventListener('click', () => { state.selected = button.dataset.marketOpen; state.notice = ''; ctx.render(); main.scrollTop = 0; }));
  $('#marketClear', main)?.addEventListener('click', () => { state.query = ''; state.category = 'all'; state.status = 'all'; ctx.render(); });
}

function detailMarkup(entry, ctx) {
  const { esc } = ctx, record = recordFor(entry.id), installed = installedFor(entry.id), checking = record.loading, installing = state.installing === entry.id;
  const pv = record.preview, conflicts = pv?.conflicts || [];
  const installable = pv && !conflicts.length && !installed && state.installed !== null && !state.loading && !checking && !state.installing;
  return `<button type="button" class="detail-back" id="marketBack">← 返回市场</button>
    <section class="market-detail-header"><span class="market-icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round">${ICONS[entry.category] || ICONS.workspace}</svg></span><div><h2>${esc(entry.name)}</h2><p>${esc(entry.description)}</p><div class="market-detail-meta">${esc(entry.author)} · v${esc(entry.version)} · ${esc(entry.license)} · ${byteSize(entry.size_bytes)}</div></div></section>
    <div class="market-detail-actions"><button type="button" class="btn ${pv || installed ? '' : 'primary'}" id="marketPreview" ${checking || state.installing ? 'disabled' : ''}>${checking ? '正在校验内容…' : record.error ? '重新预览' : pv ? '重新校验' : '预览安装内容'}</button>
      ${installed ? '<span class="badge">已安装</span><button type="button" class="btn" id="marketSkills">前往技能页</button><button type="button" class="btn ghost" id="marketManage">管理内容包</button>' : pv ? `<button type="button" class="btn primary" id="marketInstall" ${installable ? '' : 'disabled'}>${installing ? '正在安装…' : '安装内容包'}</button>` : ''}</div>
    ${installed ? `<p class="hint">已安装版本 ${esc(installed.version)}。技能是否启用，由你在当前人设中选择。</p>` : '<p class="hint">先查看将安装的完整内容，再确认安装。安装不会切换当前人设。</p>'}
    ${record.error ? `<div class="note err" role="alert">${esc(record.error)}</div>` : ''}
    <section class="market-section"><h2>适合用来</h2><ul class="market-features">${entry.features.map((feature) => `<li>${esc(feature)}</li>`).join('')}</ul><div class="market-example"><span>可以这样开始</span><p>${esc(entry.example)}</p></div></section>
    <section class="market-section"><h2>包含内容与权限</h2><div class="market-permission-row"><span class="market-permission-copy"><b>${entry.skills.length} 项技能</b><span>${entry.skills.map(esc).join('、')}</span></span><span class="badge off">仅文字</span></div><p class="hint">内容包不包含程序或 MCP 服务器，不授予新权限。使用已有工具时，仍遵循工作台现有的权限设置。安装后需要手动启用。</p></section>
    ${pv ? `<section class="market-section market-preview"><h2>安装预览</h2><p class="hint">宿主已核对文件格式与完整性 · ${pv.items.length} 项 · ${byteSize(pv.size)}</p>
      ${conflicts.length ? `<div class="note err" role="alert">${conflicts.map(esc).join('<br>')}</div>` : ''}
      ${pv.warnings?.length ? `<div class="note market-warning"><b>这些内容需要留意</b><p>${pv.warnings.map(esc).join('<br>')}</p></div>` : ''}
      ${pv.items.map((item) => `<details class="market-file" open><summary>${esc(item.name)} <span>${esc((item.files || [item.file]).filter(Boolean).join('、'))}</span></summary><pre>${esc(item.text || '')}</pre></details>`).join('')}
      <p class="hint">来源：随应用分发的本地目录。当前内容包未安装任何自动启用的人设。</p></section>` : ''}`;
}

async function preview(entry, ctx) {
  const record = recordFor(entry.id);
  if (record.loading || state.installing) return;
  record.loading = true;
  record.error = '';
  record.preview = null;
  record.text = '';
  repaint(ctx);
  try {
    const response = await fetch(PACK_ROOT + entry.file, { cache: 'no-store' });
    if (!response.ok) throw new Error(`无法读取内容包（${response.status}）。请重试。`);
    const text = await response.text();
    if (new TextEncoder().encode(text).length !== entry.size_bytes) throw new Error('内容包大小与本地目录不一致，请更新或修复应用文件后重试。');
    const result = await ctx.call('ext-inspect', { text });
    if (!result.ok) throw new Error(result.error || '宿主无法校验这个内容包。');
    const pv = result.preview;
    if (pv?.manifest?.id !== entry.id || pv.manifest.type !== 'content' || pv.sha256 !== entry.sha256 || !Array.isArray(pv.items)) throw new Error('内容包与目录记录不一致，已停止安装。');
    record.preview = pv;
    record.text = text;
  } catch (error) { record.error = error.message || '无法预览内容包，请重试。'; }
  record.loading = false;
  repaint(ctx);
}

async function install(entry, ctx) {
  const record = recordFor(entry.id);
  if (!record.preview || record.preview.conflicts?.length || state.installing || state.loading || state.installed === null || installedFor(entry.id)) return;
  state.installing = entry.id;
  record.error = '';
  repaint(ctx);
  try {
    if (!await ctx.flush()) throw new Error('设置尚未保存成功，请先重试保存，再安装内容包。');
    const result = await ctx.call('ext-install', { text: record.text });
    if (!result.ok) throw new Error(result.error || '安装失败，请重试。');
    ctx.setSettings(result.settings);
    // 安装成功与启用技能是两步，不修改 active、personas[].skills 或工具权限。
    state.installed = [...(state.installed || []).filter((item) => item.id !== entry.id), { ...record.preview.manifest }];
    state.notice = `「${entry.name}」已安装。前往技能页，为需要的人设启用。`;
    ctx.setStatus?.('内容包已安装');
    if (ctx.onInstalled) {
      try { await ctx.onInstalled(); }
      catch { state.notice += ' 技能列表暂未刷新，请重新打开技能页。'; }
    }
    await load(ctx);
  } catch (error) {
    record.error = error.message || '安装失败，请重试。';
    ctx.setStatus?.(record.error, true);
  } finally {
    state.installing = '';
    repaint(ctx);
  }
}
