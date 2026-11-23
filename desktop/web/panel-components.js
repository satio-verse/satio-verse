// 可选的大型资源库。GET 提供真实清单与进度；POST 仅使用 install / cancel / remove。
// installed 是管理器留下的安装记录，present 也包括用户自行安装的文件，后者不能在这里移除。
const comp = { data: null, error: '', timer: 0, busy: '', loading: false, request: 0, expanded: new Set(), failures: new Map(), filter: 'all', query: '' };
const ACTIVE = ['downloading', 'verifying', 'extracting'];
const STATE = { downloading: '下载中', verifying: '校验中', extracting: '安装中', done: '已安装', error: '安装失败', cancelled: '已取消' };
const size = (n) => {
  if (!Number.isFinite(Number(n)) || n == null) return '大小未知';
  n = Math.max(0, Number(n));
  return n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n >= 1e6 ? `${Math.round(n / 1e6)} MB` : n >= 1000 ? `${Math.round(n / 1000)} KB` : `${n} B`;
};
const installed = (c) => !!(c.installed || c.present);
const available = (c) => !!c.ready && !installed(c);
const progress = (job) => Math.max(0, Math.min(100, Math.floor(job.state === 'extracting' ? Number(job.unpack) || 0 : job.total ? Number(job.done) / Number(job.total) * 100 : 0)));

async function load(ctx) {
  if (comp.busy || comp.loading) return;
  comp.loading = true;
  const request = ++comp.request;
  try {
    const r = await fetch('/api/components');
    if (r.status === 404) throw new Error('当前服务端不支持组件管理，请重启或更新服务端。');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const data = await r.json();
    if (request === comp.request) { comp.data = data; comp.error = ''; }
  } catch (err) {
    if (request === comp.request) comp.error = `无法读取组件清单：${err.message}`;
  }
  comp.loading = false;
  if (request === comp.request && ctx.isTab()) ctx.render();
}

async function act(ctx, action, id) {
  if (comp.busy) return;
  const request = ++comp.request;
  comp.busy = `${action}:${id}`;
  comp.failures.delete(id);
  if (ctx.isTab()) ctx.render();
  try {
    const r = await fetch('/api/components', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, id }) });
    if (!r.ok) throw new Error((await r.text()).trim() || `HTTP ${r.status}`);
    const data = await r.json();
    if (request === comp.request) { comp.data = data; comp.error = ''; }
  } catch (err) {
    if (request === comp.request) comp.failures.set(id, { action, message: err.message });
  }
  comp.busy = '';
  if (ctx.isTab()) ctx.render();
}

function applyFilter(main) {
  const q = comp.query.trim().toLocaleLowerCase();
  let count = 0;
  for (const row of main.querySelectorAll('[data-component-id]')) {
    const c = comp.data.components.find((item) => item.id === row.dataset.componentId);
    const matches = [c.name, c.desc, c.from, c.license, c.variant].some((text) => String(text || '').toLocaleLowerCase().includes(q));
    row.hidden = !(matches && (comp.filter === 'all' || (comp.filter === 'installed' ? installed(c) : available(c))));
    if (!row.hidden) count++;
  }
  const empty = main.querySelector('#componentEmpty');
  empty.hidden = count > 0;
  empty.textContent = q ? '没有找到匹配的组件，试试名称、用途或来源。' : comp.filter === 'installed' ? '还没有已安装的组件。可以到“全部”浏览。' : comp.filter === 'available' ? '当前没有可下载的组件。已存在的资源可以在“已安装”查看。' : '组件清单暂时为空。';
  main.querySelector('#componentCount').textContent = `${count} 项组件${q ? ` · 搜索“${comp.query.trim()}”` : ''}`;
  for (const button of main.querySelectorAll('[data-component-filter]')) {
    const selected = button.dataset.componentFilter === comp.filter;
    button.classList.toggle('on', selected);
    button.setAttribute('aria-pressed', String(selected));
  }
}

function sourceLink(c, esc) {
  const label = esc(c.from || '暂未提供来源');
  try {
    const url = new URL(c.page);
    if (['https:', 'http:'].includes(url.protocol)) return `<a href="${esc(url.href)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  } catch { /* 缺少来源网址时保留文本 */ }
  return label;
}

function componentRow(c, job, running, esc) {
  const mine = job?.id === c.id, active = mine && running, failure = comp.failures.get(c.id);
  const error = failure?.message || (mine && job.state === 'error' ? job.error || '安装未完成，请重试。' : '');
  const cancelled = mine && job.state === 'cancelled';
  const status = failure ? '操作失败' : active ? STATE[job.state] : error ? '安装失败' : c.installed ? '已安装' : c.present ? '本机已有' : cancelled ? '已取消' : c.ready ? '可下载' : '暂无下载';
  const tone = error ? 'error' : active ? 'active' : installed(c) ? 'installed' : 'available';
  const pending = comp.busy.endsWith(`:${c.id}`);
  const button = (action, label, disabled = false, primary = false) => `<button type="button" class="btn${primary ? ' primary' : ''}" data-cact="${action}" data-cid="${esc(c.id)}" ${disabled ? 'disabled' : ''}>${label}</button>`;
  let actions;
  if (active) actions = button('cancel', pending ? '取消中…' : '取消', !!comp.busy);
  else if (c.installed) actions = button('remove', pending ? '移除中…' : failure?.action === 'remove' ? '重试移除' : '移除', !!comp.busy);
  else if (c.present) actions = '<span class="component-action-note">外部安装</span>';
  else actions = button('install', pending ? '准备中…' : !c.ready ? '暂无下载' : error ? '重试安装' : cancelled ? '继续安装' : '下载并安装', !c.ready || running || !!comp.busy, true);
  const meta = [c.size ? `下载 ${size(c.size)}` : '下载大小待提供', c.unpacked || c.size ? `安装后约 ${size(c.unpacked || c.size)}` : '安装大小待提供', c.variant || c.installed?.variant].filter(Boolean);
  const revision = c.installed && typeof c.installed === 'object' ? c.installed : {};
  return `<article class="component-row" data-component-id="${esc(c.id)}">
    <div class="component-header">
      <div class="component-heading"><h2>${esc(c.name)}</h2><span class="component-status" data-state="${tone}">${status}</span></div>
      <div class="component-actions">${actions}</div>
    </div>
    <p class="component-description">${esc(c.desc || '暂未提供组件说明。')}</p>
    <div class="component-meta">${meta.map((text) => `<span>${esc(text)}</span>`).join('')}</div>
    <p class="component-source hint">${esc(c.from || '暂未提供来源')}</p>
    ${active ? `<div class="component-progress" aria-live="polite">
      <progress max="100" value="${progress(job)}" aria-label="${esc(c.name)}${STATE[job.state]}"></progress>
      <div class="hint">${job.state === 'extracting' ? `正在解压并安装 · ${progress(job)}%` : job.state === 'verifying' ? '正在校验已下载的文件' : `已下载 ${size(job.done || 0)} / ${job.total ? size(job.total) : '大小待确认'} · ${progress(job)}%`}</div>
    </div>` : ''}
    ${error ? `<div class="component-error note err" role="alert">${esc(error)}</div>` : ''}
    ${cancelled && !error ? `<p class="hint component-job-note">${esc(job.error || '下载已取消，已下载的文件会保留，下次继续。')}</p>` : ''}
    ${!c.ready && !installed(c) ? '<p class="hint component-job-note">清单尚未提供下载文件，暂时无法安装。</p>' : ''}
    <details class="component-details setting-details" data-component-details="${esc(c.id)}" ${comp.expanded.has(c.id) ? 'open' : ''}>
      <summary>安装与来源详情</summary>
      ${c.use ? `<p class="hint component-use">${esc(c.use)}</p>` : ''}
      ${c.present && !c.installed ? '<p class="hint">已检测到本机文件。此组件不是通过组件库安装的，请使用原安装方式管理。</p>' : ''}
      <dl class="component-facts">
        <div><dt>来源</dt><dd>${sourceLink(c, esc)}</dd></div>
        ${c.variant || revision.variant ? `<div><dt>安装包</dt><dd>${esc(c.variant || revision.variant)}</dd></div>` : ''}
        ${revision.rev ? `<div><dt>已安装版本</dt><dd><code>${esc(revision.rev)}</code></dd></div>` : ''}
        ${revision.at ? `<div><dt>安装时间</dt><dd>${esc(revision.at)}</dd></div>` : ''}
        <div><dt>许可</dt><dd>${esc(c.license || '暂未提供')}</dd></div>
        ${c.target ? `<div><dt>安装位置</dt><dd><code>${esc(c.target)}</code></dd></div>` : ''}
      </dl>
    </details>
  </article>`;
}

/** ctx: { esc, $, $$, render, isTab() } */
export function renderComponents(main, ctx) {
  const { esc, $$ } = ctx;
  clearTimeout(comp.timer);
  const running = ACTIVE.includes(comp.data?.job?.state);
  if (!comp.busy) comp.timer = setTimeout(() => ctx.isTab() && load(ctx), running ? 1000 : 5000);
  if (!comp.data) {
    main.innerHTML = `<h1>组件</h1><p class="lead">按需下载语音、表情和本机模型等大型资源。</p><p class="empty" role="status">${esc(comp.error || '正在读取组件清单…')}</p>${comp.error ? '<button type="button" class="btn" data-components-retry>重新加载</button>' : ''}`;
    main.querySelector('[data-components-retry]')?.addEventListener('click', () => load(ctx));
    if (!comp.error) load(ctx);
    return;
  }
  const searchFocused = document.activeElement?.id === 'componentSearch';
  const selection = searchFocused ? [document.activeElement.selectionStart, document.activeElement.selectionEnd] : null;
  const y = main.scrollTop, d = comp.data, counts = { all: d.components.length, installed: d.components.filter(installed).length, available: d.components.filter(available).length };
  main.innerHTML = `
    <h1>组件</h1>
    <p class="lead">按需下载语音、表情和本机模型等大型资源。下载前可查看所需空间，中断后可以继续。</p>
    ${comp.error ? `<div class="note err" role="alert">${esc(comp.error)} <button type="button" class="btn small" data-components-retry>重新加载</button></div>` : ''}
    <div class="component-toolbar">
      <div class="subtabs component-filters" role="group" aria-label="组件范围">${[['all', '全部'], ['installed', '已安装'], ['available', '可下载']].map(([id, name]) => `<button type="button" data-component-filter="${id}" aria-pressed="${comp.filter === id}" class="${comp.filter === id ? 'on' : ''}">${name} <span class="component-filter-count">${counts[id]}</span></button>`).join('')}</div>
      <label class="component-search"><input id="componentSearch" type="search" placeholder="搜索名称、用途或来源" value="${esc(comp.query)}" autocomplete="off" aria-label="搜索组件"></label>
    </div>
    <p class="hint component-result-count" id="componentCount" role="status"></p>
    <div class="component-list">${d.components.map((c) => componentRow(c, d.job, running, esc)).join('')}</div>
    <p class="empty" id="componentEmpty" hidden></p>
    <p class="hint component-footer">下载后会自动校验文件。安装完成后按组件说明配置，并在<a href="#" data-goto="services">本地服务</a>中重启对应服务。</p>
    ${d.version ? `<p class="hint">当前版本 · WorldModel ${esc(d.version)}</p>` : ''}`;
  applyFilter(main);
  main.scrollTop = y;
  const search = main.querySelector('#componentSearch');
  if (searchFocused) { search.focus({ preventScroll: true }); search.setSelectionRange(...selection); }
  search.addEventListener('input', () => { comp.query = search.value; applyFilter(main); });
  $$('[data-component-filter]', main).forEach((button) => button.addEventListener('click', () => { comp.filter = button.dataset.componentFilter; applyFilter(main); }));
  main.querySelector('[data-components-retry]')?.addEventListener('click', () => load(ctx));
  $$('[data-component-details]', main).forEach((detail) => detail.addEventListener('toggle', () => {
    detail.open ? comp.expanded.add(detail.dataset.componentDetails) : comp.expanded.delete(detail.dataset.componentDetails);
  }));
  $$('[data-cact]', main).forEach((button) => button.addEventListener('click', () => {
    const { cact, cid } = button.dataset;
    if (cact === 'remove' && !confirm('移除这个组件？之后使用时需要重新下载。你自行添加的文件会保留。')) return;
    act(ctx, cact, cid);
  }));
}
