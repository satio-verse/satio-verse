// 数据与存储：只读取三个已有接口，分别展示实测文件大小、会话数量和组件清单估计值。
const SOURCES = { experience: '/api/experience', agent: '/api/agent', components: '/api/components' };
const storage = Object.fromEntries(Object.keys(SOURCES).map((key) => [key, { data: null, error: '', loading: false, attempted: false, updated: '' }]));
const count = (n) => Number.isFinite(Number(n)) ? Math.max(0, Number(n)) : 0;
const bytes = (n) => {
  n = count(n);
  return n >= 1e9 ? `${(n / 1e9).toFixed(1)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : n >= 1000 ? `${(n / 1000).toFixed(1)} KB` : `${n} 字节`;
};

async function load(ctx, key, repaint = true) {
  const source = storage[key];
  if (source.loading) return;
  source.loading = true;
  source.attempted = true;
  source.error = '';
  if (repaint && ctx.isTab()) ctx.render();
  try {
    const response = await fetch(SOURCES[key], { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    const valid = key === 'experience' ? data.files && typeof data.files === 'object' : Array.isArray(data[key === 'agent' ? 'workspaces' : 'components']);
    if (!valid) throw new Error('返回的数据格式不完整');
    source.data = data;
    source.updated = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  } catch (err) {
    source.error = err.message || '无法连接服务端';
  }
  source.loading = false;
  if (ctx.isTab()) ctx.render();
}

function experienceBody(data, esc) {
  const files = Object.entries(data.files || {});
  const totalBytes = files.reduce((sum, [, file]) => sum + count(file.bytes), 0);
  const totalLines = files.reduce((sum, [, file]) => sum + count(file.lines), 0);
  return `<div class="storage-metric"><strong>${bytes(totalBytes)}</strong><span class="hint">经历文件实测大小 · ${totalBytes.toLocaleString('zh-CN')} 字节</span></div>
    <p class="hint">${files.length} 个文件 · ${totalLines.toLocaleString('zh-CN')} 条记录</p>
    ${files.length ? `<div class="storage-files">${files.map(([name, file]) => `<div class="row"><div class="setting-copy"><span class="setting-label">${esc(name)}</span><span class="setting-description">${count(file.lines).toLocaleString('zh-CN')} 条记录${file.modified ? ` · ${esc(String(file.modified).replace('T', ' '))}` : ''}</span></div><span>${bytes(file.bytes)}</span></div>`).join('')}</div>` : '<p class="empty">还没有积累小屋经历。</p>'}
    ${data.dir ? `<details class="setting-details"><summary>存储位置</summary><p class="hint"><code>${esc(data.dir)}</code></p></details>` : ''}
    <div class="actions"><button type="button" class="btn" data-storage-manage="experience">导出与管理经历</button></div>`;
}

function agentBody(data, esc) {
  const workspaces = data.workspaces || [];
  const total = workspaces.reduce((sum, workspace) => sum + (workspace.sessions || []).length, 0);
  return `<div class="storage-metric"><strong>${total.toLocaleString('zh-CN')} 个会话</strong><span class="hint">${workspaces.length} 个工作区</span></div>
    <p class="hint">根据工作台会话列表统计。会话文件的磁盘占用尚未测量。</p>
    ${workspaces.length ? `<div class="storage-workspaces">${workspaces.map((workspace) => `<div class="row"><div class="setting-copy"><span class="setting-label">${esc(workspace.name || '未命名工作区')}</span><span class="setting-description">${esc(workspace.path || '')}</span></div><span>${(workspace.sessions || []).length} 个会话</span></div>`).join('')}</div>` : '<p class="empty">还没有工作区。</p>'}`;
}

function componentsBody(data, esc) {
  const managed = data.components.filter((component) => !!component.installed);
  const external = data.components.filter((component) => component.present && !component.installed);
  const known = managed.filter((component) => component.unpacked != null && Number.isFinite(Number(component.unpacked)));
  const total = known.reduce((sum, component) => sum + count(component.unpacked), 0);
  const unknown = managed.length - known.length;
  return `<div class="storage-metric"><strong>${bytes(total)}</strong><span class="hint">${unknown ? '已知组件安装空间估计' : '组件安装空间估计'} · 根据组件清单计算</span></div>
    <p class="hint">通过组件库安装 ${managed.length} 项${unknown ? `，其中 ${unknown} 项尚未提供安装大小` : ''}。实际文件占用可能不同。</p>
    ${external.length ? `<p class="hint">本机另有 ${external.length} 项外部安装资源，未计入上方估计。</p>` : ''}
    ${managed.length ? `<details class="setting-details"><summary>查看已安装组件</summary><div class="storage-files">${managed.map((component) => `<div class="row"><span class="setting-label">${esc(component.name || component.id)}</span><span>${known.includes(component) ? `约 ${bytes(component.unpacked)}` : '大小未知'}</span></div>`).join('')}</div></details>` : '<p class="empty">还没有通过组件库安装的资源。</p>'}
    <div class="actions"><button type="button" class="btn" data-storage-manage="components">管理组件下载</button></div>`;
}

/** ctx: { esc, $, $$, render, isTab(), show(tab, options) } */
export function renderStorage(main, ctx) {
  const { esc } = ctx;
  const titles = { experience: '小屋经历', agent: '工作台会话', components: '组件资源' };
  const bodies = { experience: experienceBody, agent: agentBody, components: componentsBody };
  const scroll = main.scrollTop;
  main.innerHTML = `<h1>数据与存储</h1><p class="lead">查看经历文件、工作台会话与组件资源。各项按实际可读取的范围统计。</p>
    ${Object.keys(SOURCES).map((key) => {
      const source = storage[key];
      return `<section class="card storage-card" data-storage-source="${key}">
        <div class="card-head"><h2>${titles[key]}</h2><button type="button" class="btn small" data-storage-refresh="${key}" ${source.loading ? 'disabled' : ''}>${source.loading ? '读取中…' : source.error ? '重试' : '刷新'}</button></div>
        ${source.error ? `<p class="note err" role="alert">${source.data ? '刷新失败，以下为上次读取的结果' : '暂时无法读取'}：${esc(source.error)}</p>` : ''}
        ${source.data ? bodies[key](source.data, esc) : source.error ? '' : '<p class="empty" role="status">正在读取…</p>'}
        ${source.updated ? `<p class="hint storage-updated">上次读取 ${esc(source.updated)}</p>` : ''}
      </section>`;
    }).join('')}
    <p class="hint">经历文件显示实测大小，组件显示安装空间估计；会话只统计数量。这里尚未统计应用的全部磁盘占用。</p>`;
  main.scrollTop = scroll;
  ctx.$$('[data-storage-refresh]', main).forEach((button) => button.addEventListener('click', () => load(ctx, button.dataset.storageRefresh)));
  ctx.$$('[data-storage-manage]', main).forEach((button) => button.addEventListener('click', () => {
    if (button.dataset.storageManage === 'experience') ctx.show('pet', { section: 'experience', focus: '#experienceCard' });
    else ctx.show('components');
  }));
  for (const key of Object.keys(SOURCES)) if (!storage[key].attempted) load(ctx, key, false);
}
