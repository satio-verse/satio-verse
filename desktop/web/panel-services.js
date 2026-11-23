// 操作面板的"本地服务"页 (panel.js 引进来用; own-harness.md 第 9 节 A "桌宠管服务", 2026-10-26 用户: "能不能只下载一个桌宠就能实现所有功能呢?"):
// 桌宠带 --services 启动时在后台起一个看守 (wm/services.py), 它按配置开服务端、本机模型、语音合成, 挂了重启.
// 这一页每样服务一张卡片: 在不在、端口、出了什么事, 能单独重启/停; 上面一张是看守本身. 数据 GET /api/services, 页面开着时每 2 秒刷新.
// 服务端没开时这一页也打不开数据 (要靠它转话) —— 那时用托盘的"本地服务"菜单.
const svc = { data: null, error: '', timer: 0, busy: '', expanded: new Set() };
const ICON = {  // 单色小图标, 和工作台一个画法
  server: '<rect x="4" y="4" width="16" height="6.5" rx="1.6"/><rect x="4" y="13.5" width="16" height="6.5" rx="1.6"/><path d="M8 7.2h.01M8 16.8h.01"/>',
  llm: '<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2.5v3.5M15 2.5v3.5M9 18v3.5M15 18v3.5M2.5 9H6M2.5 15H6M18 9h3.5M18 15h3.5"/>',
  embed: '<circle cx="6" cy="12" r="2.2"/><circle cx="18" cy="6" r="2.2"/><circle cx="18" cy="18" r="2.2"/><path d="M8 11l8-4M8 13l8 4"/>',
  escalate: '<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/><path d="M9 12l2 2 4-4"/>',
  tts: '<path d="M4 9.5v5h3.5L12 18V6L7.5 9.5z"/><path d="M15.5 9a4 4 0 010 6M18 6.5a7.5 7.5 0 010 11"/>',
};
const TONE = { up: 'ok', external: 'ok', starting: 'busy', down: 'bad', failed: 'bad', stopped: 'idle', off: 'idle' };
const DESC = {
  server: '提供对话、记忆、聊天渠道和工作台功能。意外退出后会自动重启。',
  llm: '在本机处理简单回复与意图判断，不产生云端模型费用。',
  embed: '在本机检索过往记忆。默认关闭，启用后会占用额外显存。',
  escalate: '为本机小模型的判断提供复核。默认关闭。',
  tts: '将她的回复转换为语音，使用已配置的角色音色。',
};
const ago = (t) => {
  const s = Math.max(0, Date.now() / 1000 - t);
  return s < 60 ? `${Math.round(s)} 秒` : s < 3600 ? `${Math.round(s / 60)} 分钟` : `${(s / 3600).toFixed(1)} 小时`;
};
const svg = (p) => `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;

async function load(ctx) {
  try {
    const r = await fetch('/api/services');
    if (r.status === 404) throw Object.assign(new Error('old'), { old: true });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    svc.data = await r.json();
    svc.error = '';
  } catch (err) {
    svc.error = err.old ? '服务端是加"本地服务"之前起的: 重启服务端以后才有这一页的数据'
      : `连不上服务端 (${err.message}). 服务端没开的话: 托盘 → 本地服务 → 启动本地服务`;
  }
  if (ctx.isTab()) ctx.render();
}

async function act(ctx, action, names = []) {
  if (svc.busy) return;
  svc.busy = `${action}:${names.join(',')}`;
  ctx.render();
  try {
    const r = await fetch('/api/services', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, names }) });
    if (!r.ok) throw new Error(await r.text() || `HTTP ${r.status}`);
    svc.data = await r.json();
    svc.error = '';
  } catch (err) {
    svc.error = `没成: ${err.message}`;
  }
  setTimeout(() => { svc.busy = ''; load(ctx); }, 1200);
}

/** ctx: { esc, $, $$, render, isTab() } */
export function renderServices(main, ctx) {
  const { esc, $$ } = ctx;
  clearInterval(svc.timer);
  svc.timer = setInterval(() => (ctx.isTab() ? load(ctx) : clearInterval(svc.timer)), 2000);
  if (!svc.data) {
    main.innerHTML = `<h1>本地服务</h1><p class="empty">${esc(svc.error || '读取中…')}</p>`;
    if (!svc.error) load(ctx);
    return;
  }
  const d = svc.data, list = d.services || [];
  const up = list.filter((s) => s.state === 'up' || s.state === 'external').length;
  const busy = (k) => svc.busy === k;
  main.innerHTML = `
    <h1>本地服务</h1>
    <p class="lead">管理本机模型、语音和后台服务。受管理的服务意外退出后会自动重启。</p>
    ${svc.error ? `<div class="note err">${esc(svc.error)}</div>` : ''}
    <div class="card svc-head">
      <div class="svc-sum"><span class="svc-dot ${d.running ? 'ok' : 'idle'}"></span>
        <div><b>${d.running ? `${up} / ${list.length} 项服务运行中` : '后台服务管理未启动'}</b>
          <div class="hint">${d.running ? `已运行 ${ago(d.started)} · 退出桌宠后继续运行` : '启动后会自动管理所需的本地服务。'}</div></div></div>
      <div class="actions">
        ${d.running ? `<button type="button" class="btn" data-act="restart" ${svc.busy ? 'disabled' : ''}>${busy('restart:') ? '重启中…' : '全部重启'}</button>
          <button type="button" class="btn ghost" data-act="stop" ${svc.busy ? 'disabled' : ''}>全部停止</button>`
          : `<button type="button" class="btn primary" data-act="up" ${svc.busy ? 'disabled' : ''}>${busy('up:') ? '启动中…' : '启动本地服务'}</button>`}
      </div>
    </div>
    <div class="svc-grid service-list">${list.map((s) => `
      <div class="card svc-card ${TONE[s.state] || 'idle'}">
        <div class="svc-top"><span class="svc-icon">${svg(ICON[s.name] || ICON.server)}</span>
          <div class="svc-name"><b>${esc(s.label)}</b><span class="hint">端口 ${s.port}</span></div>
          <span class="svc-pill ${TONE[s.state] || 'idle'}"><span class="svc-dot ${TONE[s.state] || 'idle'}"></span>${esc(s.word || s.state)}</span></div>
        ${s.error ? `<div class="hint err-text">${esc(s.error)}</div>` : ''}
        <div class="actions">${s.state === 'external' ? '<span class="hint">外部管理</span>' : `
          <button type="button" class="btn" data-act="restart" data-name="${esc(s.name)}" ${svc.busy || !d.running ? 'disabled' : ''}>
            ${busy(`restart:${s.name}`) ? (['up', 'starting'].includes(s.state) ? '重启中…' : '启动中…') : ['up', 'starting'].includes(s.state) ? '重启' : '启动'}</button>
          ${['up', 'starting'].includes(s.state) ? `<button type="button" class="btn ghost" data-act="stop" data-name="${esc(s.name)}" ${svc.busy || !d.running ? 'disabled' : ''}>停止</button>` : ''}`}
        </div>
        <details class="setting-details" data-service-details="${esc(s.name)}" ${svc.expanded.has(s.name) ? 'open' : ''}><summary>运行详情</summary>
          <p class="hint">${esc(DESC[s.name] || '')}</p>
          ${s.state === 'external' ? '<p class="hint">由其他程序启动，当前不受桌宠管理。外部服务停止后，后台服务管理会按配置接手。</p>' : ''}
          ${s.since ? `<p class="hint">当前状态持续 ${ago(s.since)}${s.restarts?.length ? ` · 近 10 分钟重启 ${s.restarts.length} 次` : ''}</p>` : ''}
        </details>
      </div>`).join('')}</div>
    <details class="setting-details" data-service-details="advanced" ${svc.expanded.has('advanced') ? 'open' : ''}><summary>高级信息与日志</summary>
      <p class="hint">服务管理进程：${d.pid || '—'}。日志位于 <code>logs/services/</code>。嵌入、复核模型默认关闭；在 <code>config/local.json</code> 中配置 <code>"services": {"embed": "on"}</code> 后重启服务。</p>
      <p class="hint">退出桌宠不会停止服务。需要完全退出时，使用托盘中的“退出并停掉本地服务”。</p></details>`;
  $$('[data-service-details]', main).forEach((detail) => detail.addEventListener('toggle', () => {
    detail.open ? svc.expanded.add(detail.dataset.serviceDetails) : svc.expanded.delete(detail.dataset.serviceDetails);
  }));
  $$('[data-act]', main).forEach((b) => b.addEventListener('click', () => {
    const name = b.dataset.name, action = b.dataset.act;
    if (action === 'stop' && (!name || name === 'server')
      && !confirm(name ? '停掉服务端? 停了以后面板、工作台、QQ、微信都连不上她, 要从托盘再开.' : '全部停掉? 服务端也会停, 面板会连不上.')) return;
    act(ctx, action, name ? [name] : []);
  }));
}
