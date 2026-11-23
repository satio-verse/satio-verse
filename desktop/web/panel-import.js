// Local MCP JSON import. Selected files stay in memory; credentials never enter markup or logs.
export const MCP_IMPORT_MAX_BYTES = 1024 * 1024;
const MAX_SERVERS = 100;
const NAME_RE = /^[A-Za-z0-9_-]{1,32}$/;
const isObject = (value) => value && typeof value === 'object' && !Array.isArray(value);
const forbidden = new Set(['__proto__', 'prototype', 'constructor']);
const state = { file: '', rows: [], selected: new Set(), error: '', busy: false, staged: false, submitted: null, success: null };

/** Revalidate an entry immediately before committing. Returns only whitelisted settings. */
export function validateImportedEntry(config, name = config?.name) {
  const errors = [], warnings = [];
  if (!isObject(config)) return { entry: null, errors: ['服务器配置必须是 JSON 对象。'], warnings };
  if (typeof name !== 'string' || !NAME_RE.test(name)) errors.push('名称需为 1–32 个字母、数字、下划线或连字符。');
  const declared = config.transport ?? config.type;
  const rawTransport = declared ?? (typeof config.url === 'string' && config.url.trim() ? 'http' : 'stdio');
  const transport = ['http', 'streamable-http', 'streamable_http'].includes(rawTransport) ? 'http' : rawTransport;
  if (!['stdio', 'http'].includes(transport)) errors.push('只支持 stdio 或 Streamable HTTP；不支持旧式 SSE。');
  const text = (key) => {
    if (config[key] === undefined) return '';
    if (typeof config[key] !== 'string' || config[key].includes('\0')) { errors.push(`${key} 必须是不含空字符的文字。`); return ''; }
    return config[key].trim();
  };
  const command = text('command'), cwd = text('cwd'), url = text('url');
  let args = [];
  if (config.args !== undefined) {
    if (!Array.isArray(config.args) || config.args.length > 128 || config.args.some((value) => typeof value !== 'string' || value.includes('\0'))) errors.push('args 必须是最多 128 项的文字数组。');
    else args = [...config.args];
  }
  if (command && url) errors.push('同时含有启动命令和服务器地址，请保留一种连接方式。');
  if (transport === 'stdio' && !command) errors.push('本机程序需要 command 启动命令。');
  if (transport === 'http') {
    try {
      const address = new URL(url);
      if (!['http:', 'https:'].includes(address.protocol) || !address.hostname) throw new Error();
      if (address.username || address.password || [...address.searchParams.keys()].some((key) => /^(?:api[_-]?key|access[_-]?token|token|password|secret|authorization)$/i.test(key))) errors.push('地址中含有认证字段，请改为 headers 请求头认证后再导入。');
    } catch { errors.push('HTTP 连接需要完整的 http 或 https 地址。'); }
  }
  const secrets = (part) => {
    const out = Object.create(null), input = config[part];
    if (input === undefined) return out;
    if (!isObject(input) || Object.keys(input).length > 128) { errors.push(`${part} 必须是最多 128 项的名称与文字值对象。`); return out; }
    for (const [key, value] of Object.entries(input)) {
      const validName = part === 'env' ? /^[A-Za-z_][A-Za-z0-9_]*$/.test(key) : /^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(key);
      if (forbidden.has(key) || !validName || typeof value !== 'string' || value.includes('\0') || (part === 'headers' && /[\r\n]/.test(value))) {
        errors.push(`${part} 中有无效名称或值；值必须是文字，请检查原文件。`);
        continue;
      }
      out[key] = value;
    }
    return out;
  };
  const env = secrets('env'), headers = secrets('headers');
  if ([command, cwd, url, ...args, ...Object.values(env), ...Object.values(headers)].some((value) => /\$\{[^}]+\}|%[A-Za-z_][A-Za-z0-9_]*%/.test(value))) warnings.push('含变量占位符。本次不会展开变量，启用前请在管理页检查实际值。');
  return { entry: errors.length ? null : { name, transport, command, args, cwd, url, env, headers }, errors: [...new Set(errors)], warnings };
}

/** Pure parser for supported JSON shapes; JSON parser errors never expose source text. */
export function normalizeMcpImport(text, existingNames = []) {
  if (typeof text !== 'string' || new TextEncoder().encode(text).byteLength > MCP_IMPORT_MAX_BYTES) throw new Error('文件不能超过 1 MB。');
  let json;
  try { json = JSON.parse(text.replace(/^\uFEFF/, '')); }
  catch { throw new Error('JSON 格式不正确，请检查括号、逗号和引号。'); }
  if (!isObject(json)) throw new Error('请使用包含 mcpServers 对象或 servers 列表的 JSON 文件。');
  let pairs;
  if (Object.hasOwn(json, 'mcpServers') && isObject(json.mcpServers)) pairs = Object.entries(json.mcpServers);
  else if (Array.isArray(json.servers)) pairs = json.servers.map((item) => [item?.name, item]);
  else if (isObject(json.servers)) pairs = Object.entries(json.servers);
  else throw new Error('没有找到 mcpServers 对象或 servers 列表；此页不导入聊天记录或账号。');
  if (!pairs.length) throw new Error('文件中没有 MCP 服务器。');
  if (pairs.length > MAX_SERVERS) throw new Error('一次最多导入 100 个服务器，请拆分文件。');
  const counts = new Map(), existing = new Set(existingNames);
  pairs.forEach(([name]) => counts.set(name, (counts.get(name) || 0) + 1));
  return pairs.map(([name, value], index) => ({ index, name: typeof name === 'string' ? name : `第 ${index + 1} 项`,
    ...validateImportedEntry(value, name), duplicate: existing.has(name) ? '已有同名服务器' : counts.get(name) > 1 ? '文件中名称重复' : '' }));
}

function currentRows(ctx) {
  const existing = new Set(state.staged ? [] : ctx.getMcpNames?.() || []);
  return state.rows.map((row) => ({ ...row, duplicate: row.duplicate || (existing.has(row.name) ? '已有同名服务器' : '') }));
}

function redactedError(message, entries) {
  let text = typeof message === 'string' ? message : '导入没有完成，请重试。';
  const values = entries.flatMap((entry) => [...Object.values(entry.env || {}), ...Object.values(entry.headers || {})]).filter(Boolean).sort((a, b) => b.length - a.length);
  for (const value of values) text = text.split(value).join('••••');
  return text.slice(0, 400);
}

/** ctx: {esc,render,isTab,show,getMcpNames,importMcp(entries),openLocalImport}.
 * importMcp must revalidate, stage once, persist with the panel save queue, and return
 * {ok,count,error?,staged?}. Retrying passes the same submitted entries for idempotency.
 */
export function renderImport(main, ctx) {
  const esc = ctx.esc, rows = currentRows(ctx);
  const valid = rows.filter((row) => row.entry && !row.duplicate);
  const invalid = rows.filter((row) => !row.entry).length, duplicates = rows.filter((row) => row.duplicate).length;
  const selected = valid.filter((row) => state.selected.has(row.index));
  const repaint = () => { if (ctx.isTab?.() !== false) ctx.render ? ctx.render() : renderImport(main, ctx); };
  main.innerHTML = `<h1>导入配置</h1><p class="lead">从本地文件添加 MCP 连接，或导入 WorldModel 人设与技能内容包。</p>
    <section class="card settings-group"><h2>MCP JSON</h2><p class="hint">支持 <code>mcpServers</code> 对象、<code>servers</code> 对象或列表。每个文件最多 1 MB、100 项。</p>
      <div class="row"><span>配置文件</span><div class="import-file-picker"><input id="mcpImportFile" type="file" accept=".json,application/json" aria-label="选择 MCP 配置文件" hidden ${state.busy || state.staged ? 'disabled' : ''}><button type="button" class="btn" id="mcpImportChoose" ${state.busy || state.staged ? 'disabled' : ''}>${state.file ? '重新选择文件' : '选择 JSON 文件'}</button><span class="hint" role="status">${state.file ? esc(state.file) : '尚未选择文件'}</span></div></div>
      <p class="hint">文件仅在本机解析。环境变量与请求头的值不显示，导入时通过桌宠现有加密存储保存。</p>
      ${state.busy ? '<p class="hint" role="status">正在处理，请稍候…</p>' : ''}
      ${state.error ? `<p class="note err" role="alert">${esc(state.error)}</p>` : ''}
      ${state.success ? `<div class="note" role="status">已导入 ${state.success.count} 个 MCP 连接，均保持停用。</div><div class="actions"><button type="button" class="btn" id="mcpImportManage">管理 MCP 服务器</button></div>` : ''}
    </section>
    ${rows.length ? `<section class="card settings-group"><div class="section-toolbar"><h2>导入预览</h2><span class="hint">可导入 ${valid.length} · 无效 ${invalid} · 重名 ${duplicates}</span></div>
      <p class="hint">重名项不会覆盖已有配置。请修改原文件后重新选择。下方仅显示连接类型和认证项数量。</p>
      <div class="settings-list">${rows.map((row) => `<label class="settings-list-row"><span class="setting-copy"><span class="setting-label">${esc(row.name)}</span>
        <span class="setting-description">${row.entry ? `${row.entry.transport === 'http' ? 'Streamable HTTP' : '本机程序 · stdio'} · ${row.entry.args.length} 项参数 · ${Object.keys(row.entry.env).length} 个环境变量 · ${Object.keys(row.entry.headers).length} 个请求头` : '配置无效'}</span>
        ${[...row.errors, ...(row.duplicate ? [row.duplicate] : []), ...row.warnings].map((message) => `<span class="setting-description ${row.errors.length || row.duplicate ? 'err-text' : ''}">${esc(message)}</span>`).join('')}</span>
        <input type="checkbox" data-import-index="${row.index}" aria-label="导入 ${esc(row.name)}" ${state.selected.has(row.index) && row.entry && !row.duplicate ? 'checked' : ''} ${!row.entry || row.duplicate || state.busy || state.staged ? 'disabled' : ''}></label>`).join('')}</div>
      <p class="hint">确认后仅保存连接配置：默认停用，不加入任何人设，不启动程序、不测试连接。你可以随后在 MCP 管理页启用。</p>
      <div class="actions"><button type="button" class="btn primary" id="mcpImportConfirm" ${state.busy || (!state.staged && !selected.length) ? 'disabled' : ''}>${state.staged ? '重试保存' : `确认导入 ${selected.length} 项`}</button><button type="button" class="btn" id="mcpImportClear" ${state.busy || state.staged ? 'disabled' : ''}>清除预览</button></div>
    </section>` : ''}
    <section class="card settings-group"><h2>WorldModel 内容包</h2><p class="hint">导入 <code>.wmext</code> 中的人设与技能。现有导入流程会先检查文件、列出内容和冲突，再由你确认安装。</p>
      <div class="actions"><button type="button" class="btn" id="openWmextImport">前往内容包导入</button></div></section>`;
  main.querySelector('#mcpImportChoose').addEventListener('click', () => main.querySelector('#mcpImportFile').click());
  main.querySelector('#mcpImportFile').addEventListener('change', async (event) => {
    const file = event.target.files?.[0]; if (!file || state.busy || state.staged) return;
    state.rows = []; state.selected.clear(); state.success = null; state.error = ''; state.file = file.name; state.busy = true; repaint();
    try {
      if (file.size > MCP_IMPORT_MAX_BYTES) throw new Error('文件不能超过 1 MB。');
      state.rows = normalizeMcpImport(await file.text(), ctx.getMcpNames?.() || []);
      state.selected = new Set(state.rows.filter((row) => row.entry && !row.duplicate).map((row) => row.index));
    } catch (error) { state.error = error.name === 'NotReadableError' ? '无法读取此文件，请重新选择。' : error.message || '无法读取此文件。'; }
    state.busy = false; repaint();
  });
  main.querySelectorAll('[data-import-index]').forEach((input) => input.addEventListener('change', () => {
    const index = Number(input.dataset.importIndex); input.checked ? state.selected.add(index) : state.selected.delete(index); repaint();
  }));
  main.querySelector('#mcpImportClear')?.addEventListener('click', () => {
    state.rows = []; state.selected.clear(); state.error = ''; state.file = ''; state.submitted = null; repaint();
  });
  main.querySelector('#mcpImportConfirm')?.addEventListener('click', async () => {
    if (state.busy) return;
    const entries = state.staged ? state.submitted : currentRows(ctx).filter((row) => row.entry && !row.duplicate && state.selected.has(row.index)).map((row) => row.entry);
    if (!entries?.length) { state.error = '没有可导入的项；请检查选择与重名配置。'; repaint(); return; }
    state.submitted = entries; state.busy = true; state.error = ''; repaint();
    try {
      const result = await ctx.importMcp(entries);
      if (!result?.ok) {
        state.staged = !!result?.staged;
        state.error = redactedError(result?.error || '导入未完成，请重试。', entries);
      } else {
        state.success = { count: Number.isInteger(result.count) ? result.count : entries.length };
        state.rows = []; state.selected.clear(); state.submitted = null; state.staged = false; state.file = '';
      }
    } catch (error) { state.error = redactedError(error.message, entries); }
    state.busy = false; repaint();
  });
  main.querySelector('#mcpImportManage')?.addEventListener('click', () => ctx.show('mcp'));
  main.querySelector('#openWmextImport').addEventListener('click', () => ctx.openLocalImport());
}
