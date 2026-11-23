// 操作面板 (设置窗口, 宿主那边见 desktop/host/src/panel.h): 人设预设、模型、MCP 服务器、skill.
// 设置的读写都经宿主 ("panel <json>"), 改了就自动保存. 密钥 (API key、MCP 服务器的环境变量和请求头) 页面只能填新的或清掉,
// 宿主用 DPAPI 加密后存, 回来的设置里只有"有没有" (has_key / secret_names).
import { renderQQ } from '/pet/panel-qq.js';
import { renderExt, extSelftest, extForget, BUILTIN_GROUPS } from '/pet/panel-ext.js';
import { renderBilling, billingSelftest } from '/pet/panel-billing.js';
import { renderServices } from '/pet/panel-services.js';
import { renderComponents } from '/pet/panel-components.js';
import { renderMarketplace } from '/pet/panel-marketplace.js';
import { renderImport, validateImportedEntry } from '/pet/panel-import.js';
import { renderStorage } from '/pet/panel-storage.js';
import { renderDiagnose, diagnoseSelftest } from '/pet/panel-diagnose.js';
import { renderIm, imSelftest } from '/pet/panel-im.js';
import { autoEnhance } from '/pet/ui-kit.js';
import { renderPet, petForget } from '/pet/panel-pet.js';
import { renderPreferences, PREFERENCE_SEARCH } from '/pet/panel-preferences.js';
import { initPreferences } from '/pet/ui-preferences.js';
import { uiIcon } from '/pet/ui-icons.js';
import { initPlatform } from '/pet/workbench-platform.js';
import { renderLife } from '/pet/panel-life.js';
import { renderMemory } from '/pet/panel-memory.js';

const PARAMS = new URLSearchParams(location.search);
// 嵌在工作台里 (workbench.html 的设置页, iframe; 工作台 + 桌宠一份设置): iframe 里不用 chrome.webview,
// 经工作台页面转给宿主 (宿主在工作台那边收 "panel ..."), 宿主的回话由工作台页面转下来
const EMBED = PARAMS.get('embed') === '1' && window.parent !== window;
const host = EMBED ? {
  postMessage: (text) => parent.postMessage({ wmPanel: text }, location.origin),
  addEventListener: (type, fn) => addEventListener('message', (e) => { if (e.source === parent && e.data?.wmHost) fn({ data: e.data.wmHost }); }),
} : window.chrome?.webview;
const TABS = ['general', 'appearance', 'notifications', 'shortcuts', 'billing', 'services', 'components', 'marketplace', 'import', 'storage', 'diagnose', 'pet', 'personas', 'models', 'plugins', 'mcp', 'skills', 'qq', 'im', 'ext','community','data','updates','life','memory'];
let platformPanel=null;
const PLUGIN_TABS = [['ext', '工具与内容包'], ['mcp', 'MCP 服务器'], ['skills', '技能']];
const PREF_TABS = ['general', 'appearance', 'notifications', 'shortcuts'];
const PROVIDER_IDS = ['deepseek', 'qwen', 'kimi', 'custom', 'selfhost'];
// 本机模型 (2026-11-02, 服务端 cloud.py 的 selfhost): 本机 / 局域网里兼容 OpenAI 的服务, key 可以不填. 宿主只自动建前四家,
// 设置里没有时面板补一份 (宿主存的时候原样留着), 常见服务的端口做成按钮
const SELFHOST = { label: '本机模型 (OpenAI 兼容)', base_url: 'http://127.0.0.1:8080/v1', model: '' };
const LOCAL_SERVERS = [['llama-server', 8080], ['Ollama', 11434], ['LM Studio', 1234], ['vLLM', 8000], ['自带的', 8788]];
const localUrl = (port) => `http://127.0.0.1:${port}/v1`;
const needsKey = (id) => !['local', 'selfhost', 'custom'].includes(id);  // 内置小模型、本机模型、自定义 (常接本机服务) 不提"还没填 key"
function ensureProviders() { S.providers ||= {}; S.providers.selfhost ||= { ...SELFHOST }; }
const PERMS = { auto: '自动', ask: '每次确认', off: '禁用' };
// 操控电脑的服务器 (kind) 的几档 (宿主的 guard.h 定): 读屏、动手、高风险只有每次确认和禁用 (高风险的永远每次问, 没有"这一轮都允许"), 锁定的改不了
const TIERS = { read: '只读', screen: '读屏', act: '动手', power: '高风险', locked: '锁定' };
const CONTROL_PERMS = { ask: '每次确认', off: '禁用' };
const NAME_RE = /^[A-Za-z0-9_-]{1,32}$/;

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const uid = (prefix) => prefix + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);

// ---------------------------------------------------------------------------
// 和宿主说话
let seq = 0;
const waiting = new Map();
function call(op, args = {}) {
  return new Promise((resolve) => {
    const id = ++seq;
    waiting.set(id, resolve);
    host?.postMessage(`panel ${JSON.stringify({ op, id, ...args })}`);
  });
}
const tell = (op, args = {}) => host?.postMessage(`panel ${JSON.stringify({ op, ...args })}`);
host?.addEventListener('message', (e) => {
  const m = e.data;
  if (m?.t !== 'panel') return;
  if (m.op === 'show') { show(m.tab); return; }
  if (m.op === 'changed') { reload(); return; }  // 侧菜单换了人设: 没有没存的改动时重新读一遍
  if (m.op === 'closing') { confirmPanelClose(m.requestId); return; }
  const done = waiting.get(m.id);
  if (done) { waiting.delete(m.id); done(m); }
});

// ---------------------------------------------------------------------------
// 状态
const ui = { tab: TABS.includes(PARAMS.get('tab')) ? PARAMS.get('tab') : 'general', persona: 'default', server: null, faces: [],
  models: {}, tests: {}, skills: [], skillsDir: '', personaDetail: false, provider: null, pluginTab: 'skills', search: '', localTest: null };
let S = null;                 // 宿主给的设置 (不含密钥)
let keys = {};                // 新填的 API key: { provider: "sk-..." | "" (清掉) }
let secrets = {};             // 新填的密钥: { serverId: { env: {}, headers: {}, remove: { env: [], headers: [] } } }
let saveTimer = 0, saving = null, revision = 0, savedRevision = 0, settingsOperation = null;
const hasPendingSettings = () => revision > savedRevision || Object.keys(keys).length > 0 || Object.keys(secrets).length > 0;

function setStatus(text, err = false) {
  const el = $('#status');
  el.textContent = text;
  el.classList.toggle('err', err);
  el.dataset.state = err ? 'error' : text === '保存中…' ? 'busy' : text === '有改动…' ? 'pending' : 'ready';
}

/** 改了就排一次保存 (停手 0.6 秒后存). flush 时马上存, 并等它存完. */
function changed() {
  revision++;
  setStatus('有改动…');
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => save(false), 600);
}

async function save(markDirty = true) {
  // 显式保存也覆盖未经过 changed 的调用者（密钥、人设自检等）。
  if (markDirty) revision++;
  clearTimeout(saveTimer);
  saveTimer = 0;
  if (settingsOperation) await settingsOperation;
  if (saving) return saving;
  if (!hasPendingSettings()) return true;
  saving = (async () => {
    // 一个 drain 串行写入所有新版本；多个 save/flush 等待同一个完整队列。
    while (hasPendingSettings()) {
      clearTimeout(saveTimer);
      saveTimer = 0;
      const sentRevision = revision, sentKeys = keys, sentSecrets = secrets;
      keys = {};
      secrets = {};
      setStatus('保存中…');
      let r;
      try { r = await call('save', { settings: S, keys: sentKeys, secrets: sentSecrets }); }
      catch (error) { r = { ok: false, error: error.message || '保存失败' }; }
      if (!r.ok) {
        restoreCredentials(sentKeys, sentSecrets);
        // 不推进 savedRevision；普通设置失败也必须继续视为 dirty。
        clearTimeout(saveTimer);
        saveTimer = 0;
        setStatus(r.error || '保存失败，请重试', true);
        return false;
      }
      savedRevision = sentRevision;
      // 只有当前版本与请求一致时才采纳宿主规范化结果，绝不覆盖在途新输入。
      if (revision === sentRevision && !Object.keys(keys).length && !Object.keys(secrets).length) { adopt(S, r.settings); ensureProviders(); }
    }
    setStatus('设置已保存');
    return true;
  })();
  try { return await saving; } finally { saving = null; }
}

function restoreCredentials(sentKeys, sentSecrets) {
  keys = { ...sentKeys, ...keys };
  for (const [id, previous] of Object.entries(sentSecrets)) {
    const newer = secretChange(id);
    for (const part of ['env', 'headers']) {
      const sets = { ...previous[part], ...newer[part] };
      const removes = new Set([...(previous.remove?.[part] || []), ...newer.remove[part]]);
      // 新写入覆盖旧删除；新删除覆盖旧写入，绝不把同一个名字同时发给宿主设置和删除。
      for (const name of Object.keys(newer[part])) removes.delete(name);
      for (const name of newer.remove[part]) delete sets[name];
      newer[part] = sets;
      newer.remove[part] = [...removes];
    }
  }
}

/** 把 from 的值并进 into, 同一个对象改值、不换引用 (长度一样的数组按位置并, 不一样就整个换). */
function adopt(into, from) {
  const isObj = (v) => v && typeof v === 'object' && !Array.isArray(v);
  for (const k of Object.keys(into)) if (!(k in from)) delete into[k];
  for (const [k, v] of Object.entries(from)) {
    const cur = into[k];
    if (Array.isArray(v) && Array.isArray(cur) && [...v, ...cur].every((item) => isObj(item) && typeof item.id === 'string')) {
      const existing = new Map(cur.map((item) => [item.id, item]));
      const next = v.map((item) => existing.has(item.id) ? adopt(existing.get(item.id), item) : item);
      cur.splice(0, cur.length, ...next);
    } else if (Array.isArray(v) && Array.isArray(cur) && cur.length === v.length) {
      v.forEach((x, i) => { if (isObj(x) && isObj(cur[i])) adopt(cur[i], x); else cur[i] = x; });
    } else if (isObj(v) && isObj(cur)) adopt(cur, v);
    else into[k] = v;
  }
  return into;
}

async function flush() {
  clearTimeout(saveTimer);
  saveTimer = 0;
  if (settingsOperation) await settingsOperation;
  if (saving && !(await saving)) return false;
  if (hasPendingSettings()) return save(false);
  return true;
}

async function confirmPanelClose(requestId) {
  let ok = false;
  try { if (importInFlight) await importInFlight; ok = await flush(); }
  catch (error) { setStatus(error.message || '保存失败，请重试', true); }
  if (EMBED) parent.postMessage({ wmPanelUI: ok ? 'close-ready' : 'close-failed', requestId }, location.origin);
  else if (ok) tell('close-ok'); // 独立宿主另有自己的关闭超时。
  return ok;
}

function secretChange(id) {
  secrets[id] ??= { env: {}, headers: {}, remove: { env: [], headers: [] } };
  return secrets[id];
}

function queueSecretSet(id, part, name, value) {
  const pending = secretChange(id);
  pending[part][name] = value;
  pending.remove[part] = pending.remove[part].filter((item) => item !== name);
}

function queueSecretRemove(id, part, name) {
  const pending = secretChange(id);
  delete pending[part][name];
  pending.remove[part] = [...new Set([...pending.remove[part], name])];
}

// 内容包操作会在宿主修改整份配置。短暂锁住编辑，再就地采纳结果，避免覆盖在途编辑或换掉闭包引用。
let configMutationTail = Promise.resolve();
function queueConfigMutation(operation) {
  const result = configMutationTail.then(operation);
  configMutationTail = result.catch(() => {});
  return result;
}
async function callExtension(op, args = {}) {
  if (!['ext-install', 'ext-uninstall'].includes(op)) return call(op, args);
  return queueConfigMutation(async () => {
  if (settingsOperation) await settingsOperation;
  if (!await flush()) return { ok: false, error: '设置尚未保存成功，请重试后再操作内容包。' };
  let release;
  settingsOperation = new Promise((resolve) => { release = resolve; });
  const app = $('.app'), wasInert = app.inert, opener = document.activeElement;
  const progress = document.createElement('dialog');
  progress.className = 'settings-dialog settings-operation';
  progress.tabIndex = -1;
  progress.setAttribute('aria-labelledby', 'settingsOperationTitle');
  progress.innerHTML = `<div class="settings-operation-card"><h2 id="settingsOperationTitle">${op === 'ext-install' ? '正在导入内容包' : '正在卸载内容包'}</h2><p role="status">正在更新人设、技能与配置。完成后即可继续调整设置。</p></div>`;
  progress.addEventListener('cancel', (e) => e.preventDefault());
  progress.addEventListener('keydown', (e) => { if (e.key === 'Tab') { e.preventDefault(); progress.focus(); } });
  document.body.append(progress);
  app.inert = true;
  try {
    progress.showModal();
    progress.focus();
    const result = await call(op, args);
    if (result.ok && result.settings) {
      adopt(S, result.settings);
      setStatus('内容包配置已更新');
      // setSettings 的后续调用拿到同一对象，不会用旧响应再次覆盖配置。
      return { ...result, settings: S };
    }
    return result;
  } finally {
    if (progress.open) progress.close();
    progress.remove();
    app.inert = wasInert;
    settingsOperation = null;
    release();
    requestAnimationFrame(() => { if (!$('dialog[open]')) (opener?.isConnected && opener !== document.body ? opener : $('#main'))?.focus?.({ preventScroll: true }); });
  }
  });
}

function acceptExtensionSettings(settings) {
  // callExtension 已原子采纳成功结果；兼容自检或旧调用者时仍保留 S 的身份。
  if (settings && settings !== S) adopt(S, settings);
}

const openLocalImport = () => show('ext', { section: 'packs', focus: '#xFile' });
async function refreshInstalledSkills() {
  extForget();
  const result = await call('skills');
  if (result.ok === false) throw new Error('技能列表暂时无法刷新');
  ui.skills = result.skills || ui.skills;
  ui.faces = result.faces || ui.faces;
  ui.skillsDir = result.skills_dir || ui.skillsDir;
}

// 导入与普通设置共用保存队列。失败保留同一事务，重试只保存，不重复添加服务器。
let importTransaction = null, importInFlight = null;
async function importMcp(entries) {
  if (importInFlight) return importInFlight;
  importInFlight = queueConfigMutation(async () => {
    if (importTransaction) {
      if (importTransaction.entries !== entries || !importTransaction.ids.every((id) => S.mcp.some((server) => server.id === id))) {
        return { ok: false, staged: true, error: '导入配置已发生变化，请先保存当前设置，再重新打开设置窗口。' };
      }
    } else {
      if (!Array.isArray(entries) || !entries.length || entries.length > 100) return { ok: false, error: '请选择有效的 MCP 配置。' };
      if (!await flush()) return { ok: false, error: '现有设置尚未保存，请先重试保存。' };
      const names = new Set(S.mcp.map((server) => server.name.toLowerCase()));
      const checked = [];
      for (const raw of entries) {
        const { entry, errors } = validateImportedEntry(raw);
        if (!entry || errors.length || names.has(entry.name.toLowerCase())) return { ok: false, error: '配置无效或名称已存在，请重新检查导入文件。' };
        names.add(entry.name.toLowerCase());
        checked.push(entry);
      }
      const ids = [];
      for (const entry of checked) {
        const id = uid('s');
        ids.push(id);
        S.mcp.push({ id, name: entry.name, transport: entry.transport, command: entry.command, args: entry.args,
          cwd: entry.cwd, url: entry.url, enabled: false, tools: {}, secret_names: { env: [], headers: [] } });
        for (const part of ['env', 'headers']) for (const [name, value] of Object.entries(entry[part])) queueSecretSet(id, part, name, value);
      }
      importTransaction = { entries, ids };
      changed();
    }
    if (!await flush()) return { ok: false, staged: true, error: '配置尚未保存成功。点击重试保存即可，无需重新导入。' };
    const count = importTransaction.ids.length;
    importTransaction = null;
    return { ok: true, count };
  });
  try { return await importInFlight; }
  catch { return { ok: false, staged: !!importTransaction, error: '保存未完成，请重试。' }; }
  finally { importInFlight = null; }
}

// ---------------------------------------------------------------------------
// 页面
function revealCurrentNavigation() {
  const list = $('.nav-items'), active = $('.nav-items [aria-current="page"]');
  if (!list || !active || active.hidden || !list.clientHeight) return;
  const viewport = list.getBoundingClientRect(), item = active.getBoundingClientRect();
  const inset = 12;
  // Scroll only the navigation pane; preserve the content position and keyboard focus.
  if (item.top < viewport.top + inset) list.scrollTop += item.top - viewport.top - inset;
  else if (item.bottom > viewport.bottom - inset) list.scrollTop += item.bottom - viewport.bottom + inset;
}
const navigationSize = new ResizeObserver(revealCurrentNavigation);
if ($('.nav-items')) navigationSize.observe($('.nav-items'));
function show(tab, options = {}) {
  if (tab === 'plugins') tab = ui.pluginTab;
  if (tab === 'pet') petForget();  // 托盘里可能改过: 每次进来重新读
  const previous = ui.tab;
  ui.tab = TABS.includes(tab) ? tab : 'general';
  if (PLUGIN_TABS.some(([id]) => id === ui.tab)) ui.pluginTab = ui.tab;
  if (options.persona) { ui.persona = options.persona; ui.personaDetail = true; }
  if (options.provider) ui.provider = options.provider;
  if (options.server) ui.server = options.server;
  ui.search = '';
  if ($('#navSearch')) $('#navSearch').value = '';
  $$('.nav button[data-tab]').forEach((b) => {
    b.hidden = false;
    const section = PLUGIN_TABS.some(([id]) => id === ui.tab) && $('.nav [data-tab="plugins"]') ? 'plugins' : ui.tab;
    const active = b.dataset.tab === section;
    b.classList.toggle('active', active);
    if (active) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');
  });
  $$('.nav .nav-group').forEach((g) => { g.hidden = false; });
  if ($('#navEmpty')) $('#navEmpty').hidden = true;
  ui.focusTarget = options.focus || '';
  ui.focusSection = options.section || '';
  ui.focusReveals = new Set();
  render();
  requestAnimationFrame(revealCurrentNavigation);
  if (previous !== ui.tab) {
    $('#main').scrollTop = 0;
    const pending = hasPendingSettings() || saving || settingsOperation || ui.skillEdit;
    if (!pending && !$('#status').classList.contains('err')) setStatus('设置已同步');
  }
}

let renderedRoute = '', pageEntrances = [];
const PAGE_ICONS = { general: 'sliders', appearance: 'palette', notifications: 'bell', shortcuts: 'keyboard', pet: 'heart', personas: 'person', models: 'chip', marketplace: 'store', components: 'download', plugins: 'blocks', ext: 'blocks', mcp: 'plug', skills: 'star', qq: 'chat', im: 'chat', billing: 'wallet', services: 'server', import: 'download', storage: 'folder', diagnose: 'pulse' };
function finishPage(main) {
  const title = $('h1', main);
  if (title && !$('.page-emblem', title)) title.insertAdjacentHTML('afterbegin', `<span class="page-emblem" aria-hidden="true">${uiIcon(ui.search ? 'search' : PAGE_ICONS[ui.tab])}</span>`);
  $$('.component-heading', main).forEach((heading) => {
    if ($('.resource-icon', heading)) return;
    const id = heading.closest('[data-component-id]').dataset.componentId;
    const icon = /kokoro|sovits|speech|tts/i.test(id) ? 'mic' : /embed|rerank|model/i.test(id) ? 'chip' : 'box';
    heading.insertAdjacentHTML('afterbegin', `<span class="resource-icon" aria-hidden="true">${uiIcon(icon)}</span>`);
  });
  $$('.storage-card .card-head h2', main).forEach((heading) => {
    const source = heading.closest('[data-storage-source]').dataset.storageSource;
    heading.insertAdjacentHTML('afterbegin', uiIcon({ experience: 'heart', agent: 'chat', components: 'box' }[source], 'section-icon'));
  });
  animateRoute(main);
}
function animateRoute(main) {
  const route = JSON.stringify([ui.search ? 'search' : ui.tab, ui.provider, ui.personaDetail, ui.server, !!ui.skillEdit]);
  if (renderedRoute === route) return;
  renderedRoute = route;
  pageEntrances.forEach((animation) => animation.cancel());
  pageEntrances = [];
  if (document.documentElement.dataset.wmMotion === 'reduce') return;
  // Animate only route changes, never polling, search typing or a new progress tick.
  const content = $('[data-marketplace-page]', main) || main;
  const blocks = [...content.children].filter((el) => !el.hidden).slice(0, 7);
  pageEntrances = blocks.map((el, index) => el.animate([{ opacity: 0, translate: '0 7px' }, { opacity: 1, translate: '0 0' }],
    { duration: 220, delay: Math.min(index * 24, 100), easing: 'cubic-bezier(.2,.7,.2,1)', fill: 'backwards' }));
}
addEventListener('wm-preferences-changed', (event) => { if (event.detail.reducedMotion) pageEntrances.forEach((animation) => animation.cancel()); });

function render() {
  const main = $('#main');
  main.dataset.tab = ui.tab;
  main.dataset.section = PLUGIN_TABS.some(([id]) => id === ui.tab) ? 'plugins' : ui.tab;
  platformPanel?.close();main._lifeRender=null;
  if (!S) { main.innerHTML = '<p class="empty">读取设置…</p>'; return; }
  if (ui.search) { renderSearch(main); finishPage(main); return; }
  if (PREF_TABS.includes(ui.tab)) {
    renderPreferences(main, { tab: ui.tab, esc, $, $$, setStatus });
    locateSetting(main);
    finishPage(main);
    return;
  }
  if(['community','data','updates'].includes(ui.tab)) {
    platformPanel ||= initPlatform({container:main,notify:text=>setStatus(text)});
    platformPanel.open(ui.tab);return;
  }
  if(ui.tab==='life'){renderLife(main);return;}
  if (ui.tab === 'memory') { renderMemory(main, { esc }); return; }  // 记忆看得见 (panel-memory.js)
  ({ billing: (m) => renderBilling(m, { esc, $, $$, render, isTab: () => ui.tab === 'billing' }),
     services: (m) => renderServices(m, { esc, $, $$, render, isTab: () => ui.tab === 'services' }),
     components: (m) => renderComponents(m, { esc, $, $$, render, isTab: () => ui.tab === 'components' }),
     marketplace: (m) => renderMarketplace(m, { esc, $, $$, call: callExtension, flush, setSettings: acceptExtensionSettings, render, show, setStatus,
       isTab: () => ui.tab === 'marketplace', onInstalled: refreshInstalledSkills, openLocalImport }),
     import: (m) => renderImport(m, { esc, $, $$, render, show, importMcp, getMcpNames: () => S.mcp.map((server) => server.name), openLocalImport, isTab: () => ui.tab === 'import' }),
     storage: (m) => renderStorage(m, { esc, $, $$, render, show, isTab: () => ui.tab === 'storage' }),
     diagnose: (m) => renderDiagnose(m, { esc, $, $$, render, isTab: () => ui.tab === 'diagnose' }),
     pet: (m) => renderPet(m, { esc, $, $$, call, render, isTab: () => ui.tab === 'pet' }),
     im: (m) => renderIm(m, { esc, $, $$, render, show, isTab: () => ui.tab === 'im' }),
     personas: renderPersonas, models: renderModels, mcp: renderMcp, skills: renderSkills,
     qq: (m) => renderQQ(m, { S, esc, $, $$, changed, save, call, render, setKey: (k, v) => { keys[k] = v; } }),
     ext: (m) => renderExt(m, { S, esc, $, $$, changed, call: callExtension, render, flush, setSettings: acceptExtensionSettings, skills: ui.skills,
       isTab: () => ui.tab === 'ext' }) })[ui.tab]?.(main);
  if (PLUGIN_TABS.some(([id]) => id === ui.tab)) decoratePlugins(main);
  locateSetting(main);
  finishPage(main);
}

function decoratePlugins(main) {
  let title = $('h1', main), lead = $('.lead', main);
  if (!title) { title = document.createElement('h1'); main.prepend(title); }
  title.textContent = '我的插件';
  if (!lead) { lead = document.createElement('p'); lead.className = 'lead'; title.after(lead); }
  lead.textContent = '连接工具、管理扩展，为她添加适合当前工作的能力。';
  const tabs = document.createElement('div');
  tabs.className = 'settings-subtabs';
  tabs.setAttribute('role', 'tablist');
  tabs.setAttribute('aria-label', '我的插件');
  tabs.innerHTML = PLUGIN_TABS.map(([id, label]) => `<button type="button" role="tab" aria-selected="${id === ui.tab}" tabindex="${id === ui.tab ? 0 : -1}" data-plugin-tab="${id}" class="${id === ui.tab ? 'on' : ''}">${label}</button>`).join('');
  lead.after(tabs);
  $$('[data-plugin-tab]', tabs).forEach((button) => button.addEventListener('click', () => show(button.dataset.pluginTab)));
  tabs.addEventListener('keydown', (e) => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
    e.preventDefault();
    const at = PLUGIN_TABS.findIndex(([id]) => id === ui.tab);
    const next = e.key === 'Home' ? 0 : e.key === 'End' ? PLUGIN_TABS.length - 1 : (at + (e.key === 'ArrowRight' ? 1 : -1) + PLUGIN_TABS.length) % PLUGIN_TABS.length;
    const id = PLUGIN_TABS[next][0];
    show(id);
    $(`[data-plugin-tab="${id}"]`)?.focus();
  });
}

function locateSetting(main) {
  if (!ui.focusTarget || ui.revealingSection) return;
  const tabAttribute = { pet: 'data-pet-section', billing: 'data-billing-tab', qq: 'data-qq-tab', ext: 'data-sub-tab' }[ui.tab];
  if (ui.focusSection && tabAttribute) {
    const button = $(`[${tabAttribute}="${CSS.escape(ui.focusSection)}"]`, main);
    if (button && button.getAttribute('aria-selected') !== 'true' && !button.classList.contains('on') && revealSearchTab(button, tabAttribute, ui.focusSection, main)) return;
  }
  const el = $(ui.focusTarget, main);
  if (!el) return; // 异步设置页完成读取并重绘后再定位。
  const sections = [
    ['[data-section]', 'section', 'data-pet-section'],
    ['[data-billing]', 'billing', 'data-billing-tab'],
    ['[data-qq-section]', 'qqSection', 'data-qq-tab'],
    ['[data-sub]', 'sub', 'data-sub-tab'],
  ];
  for (const [selector, dataKey, buttonAttribute] of sections) {
    const section = el.closest(selector);
    if (!section || (!section.hidden && getComputedStyle(section).display !== 'none')) continue;
    const value = section.dataset[dataKey].split(' ')[0];
    const button = $(`[${buttonAttribute}="${CSS.escape(value)}"]`, main);
    if (button && revealSearchTab(button, buttonAttribute, value, main)) return;
  }
  if (el.closest('[hidden]')) return;
  const row = el.closest('.toggle-row, .row, .settings-list-row, .card') || el;
  for (let parent = el.parentElement; parent && parent !== main; parent = parent.parentElement) if (parent.tagName === 'DETAILS') parent.open = true;
  row.classList.add('search-target');
  row.scrollIntoView({ block: 'center', behavior: 'smooth' });
  const control = el.matches('input, select, textarea, button, a') ? el : $('input, select, textarea, button, a', el) || el;
  const target = control._dd || control;
  target.focus?.({ preventScroll: true });
  ui.focusTarget = '';
  setTimeout(() => row.classList.remove('search-target'), 2400);
}

function revealSearchTab(button, attribute, value, main) {
  const attempt = `${ui.focusTarget}|${attribute}|${value}`;
  if (ui.focusReveals?.has(attempt)) return false;
  (ui.focusReveals ??= new Set()).add(attempt);
  // 子页点击会同步重绘并把焦点还给标签，等其完成后再定位真实控件。
  ui.revealingSection = true;
  try { button.click(); } finally { ui.revealingSection = false; }
  requestAnimationFrame(() => locateSetting(main));
  return true;
}

/** 原生 modal 提供背景隔离；补上首尾键盘循环与关闭后的焦点恢复。 */
function wireDialog(dialog, fallbackFocus) {
  const opener = document.activeElement;
  dialog.addEventListener('keydown', (e) => {
    if (e.key !== 'Tab') return;
    const items = $$('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], summary, [tabindex]:not([tabindex="-1"])', dialog)
      .filter((item) => item.getClientRects().length && !item.closest('[hidden]'));
    const first = items[0], last = items[items.length - 1];
    if (!first) { e.preventDefault(); dialog.focus(); return; }
    if (e.shiftKey && (document.activeElement === first || !dialog.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (document.activeElement === last || !dialog.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
  });
  dialog.addEventListener('close', () => {
    dialog.remove();
    requestAnimationFrame(() => {
      if ($('dialog[open]')) return;
      const target = opener?.isConnected && opener !== document.body ? opener : fallbackFocus?.() || $('#main');
      target?.focus?.({ preventScroll: true });
    });
  });
}

// 搜索的是实际设置项（含已配置的人设、服务与技能），不读取或索引密钥。
const SEARCH_FIELDS = [
  ['pet', '打开工作台时收起桌宠', '窗口 最小化 桌面 陪伴', '[data-key="workbench_hides"]'],
  ['pet', '在侧栏显示她', '工作台 角色 显存', '[data-key="desk"]'],
  ['pet', '始终置顶', '窗口 桌面', '[data-key="topmost"]'],
  ['pet', '眼睛跟着鼠标', '视线 跟随', '[data-key="follow_mouse"]'],
  ['pet', '小屋模式', '房间 生活', '[data-key="room"]'],
  ['pet', '感知当前活动', '前台 程序 隐私', '[data-key="activity"]'],
  ['pet', '空闲时的帧率', '性能 省电 fps', '#petFps'],
  ['pet', '时区', '时间 昼夜 时钟', '#petTz'],
  ['pet', '本机接口', 'OpenAI API MCP 端口', '[data-key="api"]'],
  ['pet', '动作成长', '自动 学习 训练', '#growthCard'],
  ['pet', '她的经历', '记忆 导出 删除', '#experienceCard'],
  ['billing', '账号余额与费用', '消费 账单 用量 缓存', '#bRefresh'],
  ['billing', '使用额度', '预算 限额 token', '#bBudget'],
  ['qq', '她的 QQ 账号', '外部渠道 机器人 连接', '#qSelf'],
  ['qq', '你的 QQ 账号', '主人 绑定', '#qOwner'],
  ['qq', '主动联系', '主动 找你 消息 提醒', '#qProOn'],
  ['qq', 'QQ 空间', '动态 发布', '#qQzOn'],
  ['im', '微信接入', '扫码 机器人 登录', '#wxLogin'],
  ['diagnose', '运行检查', '诊断 故障 错误', '#dCheck'],
  ['diagnose', '复制诊断信息', '日志 报告', '#dCopy'],
  ['skills', '技能文件夹', '导入 扫描 skill SKILL.md', '#kOpen'],
  ['ext', '内置工具权限', '插件 提醒 小屋 打开网址', '[data-bperm]'],
  ['marketplace', '插件市场', '下载 内容包 技能 发现 安装', '#marketSearch'],
  ['components', '组件', '下载 安装 大小 进度 版本 语音 模型', '#componentSearch'],
  ['import', '导入 MCP 配置', 'JSON 文件 环境变量 迁移', '#mcpImportChoose'],
  ['storage', '数据与存储', '文件大小 磁盘 空间 经历 会话 导出', 'h1'],
  ['services', '本地服务', '语音 声音 麦克风 TTS 进程', 'h1'],
];
function searchEntries() {
  const entries = SEARCH_FIELDS.map(([tab, title, detail, focus]) => ({ tab, title, detail, focus,
    section: tab === 'pet' ? ({ '#growthCard': 'growth', '#experienceCard': 'experience' }[focus] || 'companion')
      : tab === 'billing' ? (focus === '#bBudget' ? 'budget' : 'overview')
        : tab === 'qq' ? (['#qProOn', '#qQzOn'].includes(focus) ? 'interaction' : 'connection')
          : tab === 'ext' ? 'tools' : '',
  }));
  entries.push(...PREFERENCE_SEARCH.map(({ tab, label, description, selector }) => ({ tab, title: label, detail: description, focus: selector })));
  for (const p of S.personas) for (const [title, focus] of [['人设文字', '#pPrompt'], ['角色形象', '#pFace'], ['默认模型', '#pProvider'], ['可用工具与技能', '.tool-block']]) {
    entries.push({ tab: 'personas', title: `${p.name || '未命名预设'} · ${title}`, detail: '人设预设', focus, persona: p.id });
  }
  for (const id of PROVIDER_IDS) {
    const p = S.providers[id];
    for (const [title, field] of [['接口地址', 'base_url'], ['API key', 'key'], ['默认模型', 'model']]) {
      entries.push({ tab: 'models', title: `${p.label || id} · ${title}`, detail: `${id} ${title === '默认模型' ? p.model || '' : id === 'selfhost' ? '本机模型配置' : '云端模型配置'}`, focus: `[data-f="${field}"]`, provider: id });
    }
  }
  for (const s of S.mcp) entries.push({ tab: 'mcp', title: s.name, detail: `MCP 服务器 ${s.transport} ${s.kind || ''}`, focus: '#sName', server: s.id });
  for (const k of ui.skills) entries.push({ tab: 'skills', title: k.name, detail: k.description || '技能', focus: `[data-kenabled="${CSS.escape(k.name)}"]` });
  return entries;
}
function renderSearch(main) {
  const query = ui.search.trim().toLowerCase();
  const hits = searchEntries().filter((entry) => `${entry.title} ${entry.detail}`.toLowerCase().includes(query));
  const names = { pet: '桌宠与工作台', personas: '人设预设', models: '模型', skills: '我的插件 / 技能', mcp: '我的插件 / MCP', ext: '我的插件', marketplace: '插件市场', import: '导入', storage: '数据与存储', billing: '用量与余额', qq: '外部渠道', im: 'IM 机器人', diagnose: '诊断', services: '本地服务', components: '组件', general: '常规', appearance: '外观', notifications: '通知', shortcuts: '快捷键' };
  main.innerHTML = `<h1>搜索设置</h1><p class="lead" role="status">${hits.length ? `找到 ${hits.length} 项与「${esc(ui.search)}」相关的设置` : `没有找到与「${esc(ui.search)}」匹配的设置`}</p>
    <div class="settings-search-results settings-list">${hits.map((hit, i) => `<button type="button" class="search-result settings-list-row" data-search-result="${i}">
      <span class="setting-copy"><span class="search-result-title setting-label">${esc(hit.title)}</span><span class="search-result-meta setting-description">${esc(names[hit.tab] || hit.tab)} · ${esc(hit.detail).slice(0, 140)}</span></span><span aria-hidden="true">›</span></button>`).join('') || '<div class="empty-state"><p>试试设置名称、模型名、技能名，或「时区」「置顶」「API key」。</p></div>'}</div>
    <div class="actions"><button type="button" class="btn" id="searchBack">返回设置</button></div>`;
  $$('[data-search-result]', main).forEach((button) => button.addEventListener('click', () => {
    const hit = hits[Number(button.dataset.searchResult)];
    if (hit.tab === 'skills') ui.skillQuery = '';
    show(hit.tab, hit);
  }));
  $('#searchBack').addEventListener('click', () => show(ui.tab));
}

/** 放大编辑一段长文字 (人设文字): 一个大的对话框, 存了才写回去 */
function bigEdit(title, value, onSave) {
  const d = document.createElement('dialog');
  d.className = 'big-edit';
  d.innerHTML = `<form method="dialog"><div class="be-head"><b>${esc(title)}</b><span class="hint" id="beCount"></span></div>
    <textarea id="beText" spellcheck="false"></textarea>
    <div class="actions"><button type="submit" class="btn primary" value="ok">好了</button><button type="submit" class="btn" value="cancel">不改了</button></div></form>`;
  document.body.append(d);
  const ta = d.querySelector('#beText'), count = () => { d.querySelector('#beCount').textContent = `${ta.value.length} 字`; };
  ta.value = value;
  ta.addEventListener('input', count);
  count();
  d.addEventListener('close', () => { if (d.returnValue === 'ok' && ta.value !== value) onSave(ta.value); });
  wireDialog(d, () => $('#pPromptMax'));
  d.showModal();
  ta.focus();
}

const providerLabel = (id) => (id === 'local' ? '内置小模型 (0.8B)' : S.providers[id]?.label || id);

function renderPersonas(main) {
  if (!ui.personaDetail) {
    main.innerHTML = `<h1>人设预设</h1><p class="lead">选择她的性格、形象与做事方式。每个预设可以使用不同的模型和工具。</p>
      <div class="section-toolbar"><h2>我的预设 <span class="section-count">${S.personas.length}</span></h2><button type="button" class="btn primary" id="addPersona">＋ 新建预设</button></div>
      <label class="settings-filter"><input type="search" id="personaSearch" placeholder="搜索预设" aria-label="搜索人设预设"></label>
      <div class="settings-list">${S.personas.map((p) => `<button type="button" class="settings-list-row" data-persona="${esc(p.id)}" data-filter-text="${esc(p.name || '').toLowerCase()}">
        <span class="settings-row-icon" aria-hidden="true">${uiIcon('person')}</span><span class="setting-copy"><span class="setting-label">${esc(p.name || '未命名预设')} ${p.id === S.active ? '<span class="badge">当前使用</span>' : ''}</span>
        <span class="setting-description">${esc(providerLabel(p.provider))}${p.model ? ` · ${esc(p.model)}` : ''} · ${(p.skills || []).length} 项技能</span></span><span class="row-chevron" aria-hidden="true">›</span></button>`).join('')}
        <p class="empty" id="personaEmpty" hidden>没有匹配的预设。</p></div>
      <p class="hint">切换当前预设后，从下一句话起生效。</p>`;
    $$('[data-persona]', main).forEach((button) => button.addEventListener('click', () => { ui.persona = button.dataset.persona; ui.personaDetail = true; render(); $('#main').scrollTop = 0; }));
    $('#addPersona').addEventListener('click', createPersona);
    bindFilter('#personaSearch', '[data-filter-text]', '#personaEmpty');
    return;
  }
  const p = S.personas.find((x) => x.id === ui.persona) || S.personas[0];
  ui.persona = p.id;
  const prov = S.providers[p.provider];
  const modelHint = p.provider === 'local' ? '内置小模型由服务端的 config 决定'
    : p.provider === 'selfhost' && !prov?.model ? '本机服务里的模型名; 空着用服务加载的那个, 在<a href="#" data-goto="models">"模型"页</a>点"检测"能看到有哪些' : (prov?.model ? `留空就用<a href="#" data-goto="models">"模型"页</a>的默认: ${esc(prov.model)}`
    : '例如 deepseek-chat; 可以在<a href="#" data-goto="models">"模型"页</a>取列表');
  // 内置小模型不调工具: 下面三样先记着, 换成云端或本机模型才交给她 (本机大模型能调工具, 不淡掉; 就地说清楚, 不用回头读顶上的提示)
  const local = p.provider === 'local';
  const localHint = local ? '<div class="hint warn-text">现在用的是内置小模型, 不调工具: 这里勾的先记着, 换成云端或本机模型才交给她</div>' : '';
  main.innerHTML = `
    <button type="button" class="detail-back" id="personaBack">← 人设预设</button>
    <h1>${esc(p.name || '未命名预设')}</h1>
    <p class="lead">更改自动保存。${p.id === S.active ? '这是她当前使用的预设。' : '设为当前后，从下一句话起生效。'}</p>
      <div class="card settings-group">
        <div class="card-head"><h2>基本信息${p.id === S.active ? ' <span class="badge">当前使用</span>' : ''}</h2>
          <div class="actions">
            <button type="button" class="btn primary" id="pActive" ${p.id === S.active ? 'disabled' : ''}>${p.id === S.active ? '正在用' : '设为当前'}</button>
            <button type="button" class="btn" id="pCopy" title="照这个复制一份新的预设">复制</button>
            ${p.id === 'default' ? '' : '<button type="button" class="btn danger" id="pDelete">删除</button>'}
          </div></div>
        <div class="row"><label for="pName">名字</label><input type="text" id="pName" value="${esc(p.name)}"></div>
        <div class="row top"><label for="pPrompt">人设文字</label><div>
          <div class="ta-wrap"><textarea id="pPrompt" class="tall" rows="12" placeholder="空着就用服务端的默认人设">${esc(p.prompt)}</textarea>
            <button type="button" class="ta-max" id="pPromptMax" title="放大编辑">⤢</button></div>
          <div class="hint">她是谁、怎么说话，以及与你的关系。${(p.prompt || '').length} 字</div></div></div>
      </div>
      <div class="card settings-group"><h2>形象与模型</h2>
        <div class="row"><label for="pFace">角色形象</label><div><select id="pFace">
          ${(ui.faces.length ? ui.faces : [{ id: 'vrm', name: '3D (VRM)', ok: true }]).map((f) => `<option value="${esc(f.id)}" ${f.id === (p.face || 'vrm') ? 'selected' : ''}
            ${f.ok ? '' : 'disabled'}>${esc(f.name)}${f.ok ? '' : ' (先跑 prepare_live2d.py)'}</option>`).join('')}
          </select><div class="hint">换门面时桌宠会重新加载一次. 2D 的 Eustia 手是交握的, 挥手、举手、指东西做不了, 她会照实说.</div></div></div>
        <div class="row"><label for="pProvider">模型</label><select id="pProvider">
          ${['local', ...PROVIDER_IDS].map((id) => `<option value="${id}" ${id === p.provider ? 'selected' : ''}>${esc(providerLabel(id))}${needsKey(id) && !S.providers[id]?.has_key ? ' (还没填 key)' : ''}</option>`).join('')}
        </select></div>
        <div class="row"><label for="pModel">模型名</label><div>
          <input type="text" id="pModel" class="mono" list="pModels" value="${esc(p.model)}" ${p.provider === 'local' ? 'disabled' : ''}>
          <datalist id="pModels">${(ui.models[p.provider] || []).map((m) => `<option value="${esc(m)}">`).join('')}</datalist>
          <div class="hint">${modelHint}</div></div></div>
      </div>
      <div class="card settings-group tool-block ${local ? 'dim' : ''}"><h2>可用工具与技能</h2>${localHint}
        <div class="row top"><label>MCP 服务器</label><div class="checks">
          ${S.mcp.length ? S.mcp.map((s) => `<label><input type="checkbox" data-mcp="${esc(s.id)}" ${p.mcp?.includes(s.id) ? 'checked' : ''}>${esc(s.name)}${s.enabled === false ? ' (停用)' : ''}</label>`).join('') : '<span class="hint">还没有, 在<a href="#" data-goto="mcp">"MCP 服务器"页</a>添加</span>'}
        </div></div>
        <div class="row top"><label>技能</label><div class="checks">
          ${ui.skills.length ? ui.skills.map((k) => `<label title="${esc(k.description)}"><input type="checkbox" data-skill="${esc(k.name)}" ${p.skills?.includes(k.name) ? 'checked' : ''}>${esc(k.name)}</label>`).join('') : '<span class="hint">还没有, 见<a href="#" data-goto="skills">"Skill"页</a></span>'}
        </div></div>
        <div class="row top"><label>内置工具</label><div class="checks">
          ${BUILTIN_GROUPS.map(([id, label]) => `<label><input type="checkbox" data-builtin="${id}" ${!p.builtins || p.builtins.includes(id) ? 'checked' : ''}>${esc(label)}</label>`).join('')}
          <span class="hint">桌宠自带的, 不用下载; 权限在<a href="#" data-goto="ext">"扩展"页</a>改</span>
        </div></div>
      </div>`;
  $('#personaBack').addEventListener('click', () => { ui.personaDetail = false; render(); $('#main').scrollTop = 0; });
  $('#pName').addEventListener('input', (e) => { p.name = e.target.value; changed(); $('h1', main).textContent = p.name || '未命名预设'; });
  $('#pPrompt').addEventListener('input', (e) => { p.prompt = e.target.value; changed(); });
  $('#pProvider').addEventListener('change', (e) => { p.provider = e.target.value; changed(); render(); });
  $('#pFace').addEventListener('change', (e) => { p.face = e.target.value; changed(); });
  $('#pModel').addEventListener('input', (e) => { p.model = e.target.value.trim(); changed(); });
  $$('[data-mcp]', main).forEach((c) => c.addEventListener('change', () => {
    p.mcp = $$('[data-mcp]', main).filter((x) => x.checked).map((x) => x.dataset.mcp);
    changed();
  }));
  $$('[data-skill]', main).forEach((c) => c.addEventListener('change', () => {
    p.skills = $$('[data-skill]', main).filter((x) => x.checked).map((x) => x.dataset.skill);
    changed();
  }));
  $$('[data-builtin]', main).forEach((c) => c.addEventListener('change', () => {
    p.builtins = $$('[data-builtin]', main).filter((x) => x.checked).map((x) => x.dataset.builtin);
    changed();
  }));
  $('#pActive').addEventListener('click', () => { S.active = p.id; changed(); render(); });
  $('#pCopy').addEventListener('click', () => {
    const np = { ...JSON.parse(JSON.stringify(p)), id: uid('p'), name: `${p.name || '预设'} 副本` };
    S.personas.push(np);
    ui.persona = np.id;
    changed();
    render();
  });
  $('#pPromptMax').addEventListener('click', () => bigEdit('人设文字', p.prompt || '', (v) => { p.prompt = v; changed(); render(); }));
  $('#pDelete')?.addEventListener('click', () => {
    S.personas = S.personas.filter((x) => x.id !== p.id);
    if (S.active === p.id) S.active = 'default';
    ui.persona = 'default';
    ui.personaDetail = false;
    changed();
    render();
  });
}

function createPersona() {
  const p = { id: uid('p'), name: '新预设', prompt: '', provider: 'local', model: '', mcp: [], skills: [] };
  S.personas.push(p);
  ui.persona = p.id;
  ui.personaDetail = true;
  changed();
  render();
  $('#pName').select();
}

function bindFilter(input, items, empty) {
  $(input)?.addEventListener('input', (e) => {
    const query = e.target.value.trim().toLowerCase();
    const rows = $$(items, $('#main'));
    rows.forEach((row) => { row.hidden = !!query && !row.dataset.filterText.includes(query); });
    $(empty).hidden = rows.some((row) => !row.hidden);
  });
}

function renderModels(main) {
  if (!ui.provider) {
    const sh = S.providers.selfhost || SELFHOST, t = ui.localTest;
    main.innerHTML = `<h1>模型</h1><p class="lead">管理模型连接。为不同的人设选择合适的提供方与默认模型。</p>
      <h2 class="section">云端模型</h2><div class="settings-list">${PROVIDER_IDS.filter((id) => id !== 'selfhost').map((id) => {
        const provider = S.providers[id];
        return `<button type="button" class="settings-list-row" data-open-provider="${id}"><span class="settings-row-icon" aria-hidden="true">${uiIcon('cloud')}</span><span class="setting-copy"><span class="setting-label">${esc(provider.label || id)}</span>
          <span class="setting-description">${esc(provider.model || '尚未选择默认模型')}</span></span><span class="setting-controls"><span class="badge ${provider.has_key ? '' : 'off'}">${provider.has_key ? '已配置' : '待配置'}</span><span class="row-chevron" aria-hidden="true">›</span></span></button>`
      }).join('')}</div>
      <h2 class="section">本机模型</h2><div class="settings-list">
        <button type="button" class="settings-list-row" data-open-provider="selfhost"><span class="settings-row-icon" aria-hidden="true">${uiIcon('server')}</span><span class="setting-copy"><span class="setting-label">${esc(sh.label || '本机模型')}</span>
          <span class="setting-description">${esc(sh.base_url || '还没填地址')} · ${esc(sh.model || '用服务加载的那个')}</span></span><span class="setting-controls"><span class="badge ${t?.item?.ok ? '' : 'off'}">${t?.item ? (t.item.ok ? '连得上' : '连不上') : '没检测'}</span><span class="row-chevron" aria-hidden="true">›</span></span></button>
        <button type="button" class="settings-list-row" data-goto="services"><span class="settings-row-icon" aria-hidden="true">${uiIcon('server')}</span><span class="setting-copy"><span class="setting-label">内置小模型 (0.8B)</span><span class="setting-description">服务端自带的小模型：启动、停止和查看运行状态</span></span><span class="row-chevron" aria-hidden="true">›</span></button></div>
      <p class="hint">API key 仅在本机加密保存。连接配置完成后，在<a href="#" data-goto="personas">人设预设</a>中选用；本机模型在工作台的模型菜单里也能选。</p>`;
    $$('[data-open-provider]', main).forEach((button) => button.addEventListener('click', () => { ui.provider = button.dataset.openProvider; render(); $('#main').scrollTop = 0; }));
    return;
  }
  const id = ui.provider, p = S.providers[id], local = id === 'selfhost', models = ui.models[id];
  main.innerHTML = `
    <button type="button" class="detail-back" id="modelBack">← 模型</button>
    <h1>${esc(providerLabel(id))}</h1>
    <p class="lead">${local ? '接本机或局域网里兼容 OpenAI 的服务（llama-server、Ollama、LM Studio、vLLM），多数不用 key。更改自动保存。' : '配置兼容 OpenAI 的接口。更改自动保存，已保存的密钥不会显示。'}</p>
    <div class="card settings-group" data-provider="${id}">
      <h2>连接配置 ${local ? '' : p.has_key ? '<span class="badge">密钥已保存</span>' : '<span class="badge off">待配置</span>'}</h2>
      <div class="row"><label for="providerUrl">接口地址</label><div><input type="url" class="mono" id="providerUrl" data-f="base_url" value="${esc(p.base_url)}" placeholder="${local ? SELFHOST.base_url : 'https://.../v1'}">
        ${local ? `<div class="chips" aria-label="常见的本机服务">${LOCAL_SERVERS.map(([name, port]) => `<button type="button" class="chip ${p.base_url === localUrl(port) ? 'on' : ''}" data-local-url="${localUrl(port)}">${esc(name)} :${port}</button>`).join('')}</div>` : ''}</div></div>
      <div class="row"><label for="providerKey">API key</label><input type="password" class="mono" id="providerKey" data-f="key" autocomplete="off" placeholder="${p.has_key ? '已保存；填入新密钥以替换' : local ? '多数本机服务不用，空着就行' : '粘贴 API key'}"></div>
      <div class="row"><label for="providerModel">默认模型</label><input type="text" class="mono" id="providerModel" data-f="model" value="${esc(p.model)}" placeholder="${local ? '空着用服务加载的那个' : '例如 deepseek-chat'}"></div>
      <div class="actions">
        ${local ? '<button type="button" class="btn primary" data-act="detect">检测</button>' : '<button type="button" class="btn" data-act="models">获取可用模型</button>'}
        ${p.has_key ? '<button type="button" class="btn danger" data-act="clear">清除密钥</button>' : ''}
      </div>
      ${local ? `<div class="local-test" id="localTest" role="status">${localTestHtml(ui.localTest)}</div>`
        : `<div class="chips">${models === undefined ? '' : models.error ? `<span class="hint" style="color:var(--danger)">${esc(models.error)}</span>`
          : models.map((m) => `<button type="button" class="chip" data-model="${esc(m)}">${esc(m)}</button>`).join('') || '<span class="hint">对方没有返回模型</span>'}</div>`}
    </div>`;
  $('#modelBack').addEventListener('click', () => { ui.provider = null; render(); $('#main').scrollTop = 0; });
  const card = $('[data-provider]', main);
  $('[data-f="base_url"]', card).addEventListener('input', (e) => {
    p.base_url = e.target.value.trim();
    changed();
    if (ui.localTest) { ui.localTest = null; const box = $('#localTest'); if (box) box.innerHTML = localTestHtml(null); }  // 地址改了, 上次的结果不算数
  });
  $('[data-f="model"]', card).addEventListener('input', (e) => { p.model = e.target.value.trim(); changed(); });
  $('[data-f="key"]', card).addEventListener('change', async (e) => {
    const v = e.target.value.trim();
    if (!v) return;
    keys[id] = v;
    e.target.value = '';
    if (await save()) render();
  });
  $('[data-act="clear"]', card)?.addEventListener('click', async () => {
    keys[id] = '';
    if (await save()) render();
  });
  $('[data-act="models"]', card)?.addEventListener('click', async (e) => {
    e.target.disabled = true;
    e.target.textContent = '正在获取…';
    if (!await flush()) { render(); return; }
    const r = await call('models', { provider: id });
    ui.models[id] = r.ok ? r.models : Object.assign([], { error: r.error || '取不到' });
    render();
  });
  $('[data-act="detect"]', card)?.addEventListener('click', () => detectLocal(p));
  $$('[data-local-url]', card).forEach((b) => b.addEventListener('click', () => { p.base_url = b.dataset.localUrl; changed(); ui.localTest = null; render(); }));
  $$('[data-model]', card).forEach((c) => c.addEventListener('click', () => { p.model = c.dataset.model; changed(); render(); }));
}

// 本机模型的"检测": 问服务端 /api/llm/local (它去问那个地址: 连不连得上、是哪家的服务、有哪些模型、上下文多长、能不能看图和调工具;
// 只问本机和局域网里的地址, 云端的它直接拒绝). 结果只在这一页留着, 不存
const LOCAL_SERVER_NAMES = { 'llama.cpp': 'llama-server (llama.cpp)', ollama: 'Ollama', lmstudio: 'LM Studio', vllm: 'vLLM' };
const kTokens = (n) => (n >= 1024 ? `${Math.round(n / 1024)}K` : String(n));
async function detectLocal(p) {
  ui.localTest = { busy: true };
  render();
  try {
    const r = await fetch(`/api/llm/local?${new URLSearchParams({ base_url: p.base_url || '', model: p.model || '' })}`, { cache: 'no-store' });
    const d = await r.json().catch(() => ({}));
    ui.localTest = r.ok && d.items?.[0] ? { item: d.items[0] } : { error: d.error || `检测失败 (HTTP ${r.status})` };
  } catch {
    ui.localTest = { error: '连不上服务端：检测要服务端（8790）开着' };
  }
  if (ui.tab === 'models') render();
}
function localTestHtml(t) {
  if (!t) return '<p class="hint">填好地址点"检测"：看连不连得上、有哪些模型、上下文多长、能不能看图和调工具（要服务端开着）。</p>';
  if (t.busy) return '<p class="hint">正在检测…</p>';
  if (t.error) return `<p class="hint err-text">${esc(t.error)}</p>`;
  const it = t.item, can = (v) => (v === true ? '能' : v === false ? '不能' : '—'), chosen = S.providers.selfhost?.model || it.model;
  const facts = [['连接', it.ok ? '连得上' : '连不上'], ['服务', LOCAL_SERVER_NAMES[it.server] || it.server || '—'],
    ['上下文', it.ctx ? `${kTokens(it.ctx)} token${it.ctx_train > it.ctx ? `（模型最多 ${kTokens(it.ctx_train)}）` : ''}` : '—'],
    ['看图', can(it.vision)], ['调工具', can(it.tools)], ['同时处理', it.slots ? `${it.slots} 路` : '—']];
  const notes = it.notes?.length ? `<ul class="local-notes">${it.notes.map((n) => `<li>${esc(n)}</li>`).join('')}</ul>` : '';
  if (!it.ok) return `<p class="hint err-text">连不上 ${esc(it.base_url || '这个地址')}</p>${notes}`;  // 连不上: 别的几格都是空的, 只写一句和提醒
  return `<dl class="local-facts">${facts.map(([k, v]) => `<div><dt>${k}</dt><dd>${esc(v)}</dd></div>`).join('')}</dl>${notes}`
    + (it.models?.length ? `<div class="hint">服务里的模型（点一下设为默认）：</div><div class="chips">${it.models.map((m) => `<button type="button" class="chip ${m === chosen ? 'on' : ''}" data-model="${esc(m)}">${esc(m)}</button>`).join('')}</div>` : '');
}

function renderMcp(main) {
  const s = S.mcp.find((x) => x.id === ui.server);
  if (s) {
    main.innerHTML = `<h1>MCP 服务器</h1><p class="lead">管理已连接的工具与权限。</p><button type="button" class="detail-back" id="serverBack">← 所有 MCP 服务器</button>${mcpEditor(s)}`;
    $('#serverBack').addEventListener('click', () => { ui.server = null; render(); $('#main').scrollTop = 0; });
    bindMcpEditor(main, s);
    return;
  }
  ui.server = null;
  main.innerHTML = `
    <h1>MCP 服务器</h1>
    <p class="lead">连接外部工具，让她能查资料、看日程或操作电脑。</p>
    <div class="section-toolbar"><h2>已添加的服务器 <span class="section-count">${S.mcp.length}</span></h2><button type="button" class="btn primary" id="addServer">＋ 添加服务器</button></div>
    <label class="settings-filter"><input type="search" id="mcpSearch" placeholder="搜索服务器" aria-label="搜索 MCP 服务器"></label>
    <div class="settings-list">${S.mcp.map((server) => `<div class="settings-list-row" data-filter-text="${esc(`${server.name} ${server.transport} ${server.kind || ''}`).toLowerCase()}">
      <button type="button" class="setting-copy setting-open" data-server="${esc(server.id)}"><span class="setting-label">${esc(server.name || '未命名服务器')}</span><span class="setting-description">${server.transport === 'http' ? 'Streamable HTTP' : '本机程序 · stdio'}${server.kind ? ' · 电脑操控' : ''}</span></button>
      <div class="setting-controls"><label class="switch-control"><input type="checkbox" class="switch" data-senabled="${esc(server.id)}" ${server.enabled !== false ? 'checked' : ''} aria-label="启用 ${esc(server.name)}"></label><button type="button" class="btn ghost" data-server="${esc(server.id)}" aria-label="配置 ${esc(server.name)}">配置 ›</button></div></div>`).join('')}
      <div class="empty-state" id="mcpEmpty" ${S.mcp.length ? 'hidden' : ''}><h3>${S.mcp.length ? '没有匹配的服务器' : '连接你的第一个工具'}</h3><p>添加一个本机程序或 HTTP 地址，连接文件、浏览器、日历等服务。</p></div></div>
    <div class="card settings-group"><div class="settings-list-row"><span class="setting-copy"><span class="setting-label">Windows 电脑操控</span><span class="setting-description">使用随附的 Windows-MCP 配置，查看屏幕、点击和输入。</span></span><button type="button" class="btn" id="addComputer">添加配置</button></div></div>
    <p class="hint">在人设预设中选择服务器后，她才会使用其中的工具。需要确认的操作会在对话中询问。</p>`;
  $$('[data-server]', main).forEach((b) => b.addEventListener('click', () => { ui.server = b.dataset.server; render(); }));
  $$('[data-senabled]', main).forEach((input) => input.addEventListener('change', () => { const server = S.mcp.find((item) => item.id === input.dataset.senabled); server.enabled = input.checked; changed(); }));
  bindFilter('#mcpSearch', '[data-filter-text]', '#mcpEmpty');
  $('#addServer').addEventListener('click', () => addMcpDialog());
  $('#addComputer').addEventListener('click', async (e) => {
    e.currentTarget.disabled = true;
    const r = await call('computer-preset');
    $('#addComputer')?.removeAttribute('disabled');
    if (!r.ok) { setStatus(`没有找到 Windows-MCP (${r.command || ''})`, true); return; }
    if (ui.tab !== 'mcp') return;
    let name = 'computer', n = 2;
    while (S.mcp.some((x) => x.name === name)) name = `computer${n++}`;
    addMcpDialog({ name, kind: 'windows-mcp', command: r.command, args: r.args, timeout_s: 60, stop_on_input: true });
  });
}

function addMcpDialog(preset = {}) {
  const dialog = document.createElement('dialog');
  dialog.className = 'settings-dialog';
  dialog.setAttribute('aria-labelledby', 'mcpDialogTitle');
  dialog.innerHTML = `<form id="newMcpForm" novalidate><div class="dialog-head"><h2 id="mcpDialogTitle">${preset.kind ? '添加电脑操控' : '添加 MCP 服务器'}</h2><button type="button" class="dialog-close" aria-label="关闭">×</button></div>
    <div class="dialog-body"><p class="hint">填写服务提供方给出的连接信息。创建后可以继续配置权限与调用超时。</p>
      <div class="row"><label for="nsName">名称</label><input type="text" id="nsName" class="mono" required maxlength="32" pattern="[A-Za-z0-9_-]+" placeholder="例如 filesystem" value="${esc(preset.name || '')}"></div>
      <div class="row"><label>连接方式</label><div class="checks"><label><input type="radio" name="nsTransport" value="stdio" checked>本机程序 (stdio)</label><label><input type="radio" name="nsTransport" value="http" ${preset.kind ? 'disabled' : ''}>HTTP</label></div></div>
      <div id="nsStdio"><div class="row"><label for="nsCommand">启动命令</label><input type="text" id="nsCommand" class="mono" placeholder="例如 npx 或 C:/tools/server.exe" value="${esc(preset.command || '')}"></div>
        <div class="row top"><label for="nsArgs">参数</label><textarea id="nsArgs" class="mono" rows="3" placeholder="一行一个参数">${esc((preset.args || []).join('\n'))}</textarea></div>
        <div class="row"><label for="nsCwd">工作目录</label><input type="text" id="nsCwd" class="mono" placeholder="可选"></div></div>
      <div class="row" id="nsHttp" hidden><label for="nsUrl">服务器地址</label><input type="url" id="nsUrl" class="mono" placeholder="https://example.com/mcp"></div>
      <details class="dialog-advanced"><summary>环境变量或请求头（可选）</summary><p class="hint" id="nsSecretHint">变量值在本机加密保存。</p><div id="nsSecrets" class="secret-list"></div><button type="button" class="btn small" id="nsAddSecret">＋ 添加一项</button></details>
      <p id="nsError" class="err-text" role="alert"></p></div>
    <div class="dialog-actions"><button type="button" class="btn" id="nsCancel">取消</button><button type="submit" class="btn primary">创建</button></div></form>`;
  document.body.append(dialog);
  const close = () => dialog.close();
  $('.dialog-close', dialog).addEventListener('click', close);
  $('#nsCancel', dialog).addEventListener('click', close);
  wireDialog(dialog, () => $('#sName') || $('#addServer'));
  $('#newMcpForm', dialog).addEventListener('input', () => { $('#nsError', dialog).textContent = ''; });
  const transport = () => $('[name="nsTransport"]:checked', dialog).value;
  $$('[name="nsTransport"]', dialog).forEach((radio) => radio.addEventListener('change', () => {
    $('#nsStdio', dialog).hidden = transport() === 'http';
    $('#nsHttp', dialog).hidden = transport() !== 'http';
    $('#nsSecretHint', dialog).textContent = transport() === 'http' ? '作为请求头发送。值在本机加密保存。' : '作为环境变量传给本机程序。值在本机加密保存。';
  }));
  $('#nsAddSecret', dialog).addEventListener('click', () => {
    const row = document.createElement('div'); row.className = 'secret';
    row.innerHTML = '<input type="text" data-secret-name aria-label="变量或请求头名称" placeholder="名称"><input type="password" data-secret-value aria-label="变量或请求头值" placeholder="值" autocomplete="off"><button type="button" class="btn ghost" aria-label="移除这一项">×</button>';
    $('button', row).addEventListener('click', () => row.remove());
    $('#nsSecrets', dialog).append(row);
    $('input', row).focus();
  });
  $('#newMcpForm', dialog).addEventListener('submit', (e) => {
    e.preventDefault();
    const name = $('#nsName', dialog).value.trim(), kind = transport(), command = $('#nsCommand', dialog).value.trim(), url = $('#nsUrl', dialog).value.trim();
    const error = $('#nsError', dialog);
    if (!NAME_RE.test(name)) { error.textContent = '名称只能包含字母、数字、下划线和连字符，最多 32 个字符。'; return; }
    if (S.mcp.some((server) => server.name === name)) { error.textContent = '这个名称已被使用，请换一个名称。'; return; }
    if (kind === 'stdio' && !command) { error.textContent = '请填写启动命令。'; $('#nsCommand', dialog).focus(); return; }
    if (kind === 'http') {
      let valid = false;
      try { const address = new URL(url); valid = ['http:', 'https:'].includes(address.protocol) && !!address.hostname; } catch { /* 留在表单内修正 */ }
      if (!valid) { error.textContent = '请填写完整的 http 或 https 地址。'; $('#nsUrl', dialog).focus(); return; }
    }
    const pairs = {};
    for (const row of $$('#nsSecrets .secret', dialog)) {
      const key = $('[data-secret-name]', row).value.trim(), value = $('[data-secret-value]', row).value;
      if (!key && !value) continue;
      if (!key || !value) { error.textContent = '请完整填写每一项的名称和值，或移除空项。'; return; }
      pairs[key] = value;
    }
    const server = { id: uid('s'), ...preset, name, transport: kind, command, args: $('#nsArgs', dialog).value.split('\n').map((x) => x.trim()).filter(Boolean), cwd: $('#nsCwd', dialog).value.trim(), url, enabled: true, tools: {}, secret_names: { env: [], headers: [] } };
    S.mcp.push(server);
    for (const [name, value] of Object.entries(pairs)) queueSecretSet(server.id, kind === 'http' ? 'headers' : 'env', name, value);
    ui.server = server.id;
    changed();
    close();
    render();
    $('#main').scrollTop = 0;
  });
  dialog.showModal();
  $('#nsName', dialog).focus();
}

function mcpEditor(s) {
  const part = s.transport === 'http' ? 'headers' : 'env';
  const names = s.secret_names?.[part] || [];
  const pending = secrets[s.id]?.[part] || {};
  const test = ui.tests[s.id];
  return `<div class="card settings-group">
    <div class="card-head"><h2>${esc(s.name || '(没有名字)')}${s.enabled === false ? ' <span class="badge off">停用</span>' : ''}</h2>
      <div class="actions"><button type="button" class="btn primary" id="sTest">测试连接</button>
        <button type="button" class="btn danger" id="sDelete">删除</button></div></div>
    <div class="row"><label for="sName">名称</label><div><input type="text" class="mono" id="sName" value="${esc(s.name)}">
      <div class="hint" id="sNameHint">工具在模型那里叫 mcp__名字__工具名. 只能用字母、数字、_ 和 -, 最多 32 个字.</div></div></div>
    <label class="toggle-row"><span class="tr-text"><b>启用此服务器</b><span class="hint">启用后，可在人设预设中选择它。</span></span><input type="checkbox" class="switch" id="sEnabled" ${s.enabled !== false ? 'checked' : ''}></label>
    <div class="row"><label>方式</label><div class="checks">
      <label><input type="radio" name="sTransport" value="stdio" ${s.transport !== 'http' ? 'checked' : ''}> 本机程序 (stdio)</label>
      <label><input type="radio" name="sTransport" value="http" ${s.transport === 'http' ? 'checked' : ''}> 网址 (Streamable HTTP)</label></div></div>
    ${s.transport === 'http' ? `
      <div class="row"><label>地址</label><input type="url" class="mono" id="sUrl" value="${esc(s.url)}" placeholder="https://example.com/mcp"></div>` : `
      <div class="row"><label>命令</label><input type="text" class="mono" id="sCommand" value="${esc(s.command)}" placeholder="例如 npx 或 C:/tools/server.exe"></div>
      <div class="row top"><label>参数</label><div><textarea class="mono" id="sArgs" rows="6" wrap="off" placeholder="一行一个">${esc((s.args || []).join('\n'))}</textarea>
        <div class="hint">一行一个, 不自动折行 (长的左右拖着看)</div></div></div>
      <div class="row"><label>工作目录</label><input type="text" class="mono" id="sCwd" value="${esc(s.cwd)}" placeholder="可以不填"></div>`}
    </div><div class="card settings-group"><h2>权限与调用</h2>
    ${s.kind ? `<div class="sub-card"><h3>操控电脑: 她能做什么、要不要问你</h3>
      <div class="rules">
        <div class="rule ok"><b>读屏、动手</b><span>每次在对话栏里问你, 可以选"这一轮都允许"; 你在 QQ 上亲口让她做的事, 算你点过头了</span></div>
        <div class="rule warn"><b>高风险: 命令、文件、注册表、进程、剪贴板、抓网页、通知</b><span><b>每次</b>都弹卡片问你 (写着要做什么), 没有"这一轮都允许"; 你不在电脑前就上 QQ 问你</span></div>
        <div class="rule bad"><b>她碰不了</b><span>付款、发消息、删文件、改系统设置、输密码的地方, 要你自己点; 聊天和邮件软件不看也不碰</span></div>
        <div class="rule info"><b>隐私</b><span>截图只留在本机, 不发给云端; 屏幕上的文字会发给当前预设的云端模型; 这个服务器的匿名统计已关掉</span></div>
      </div>
      <label class="checks"><input type="checkbox" id="sPower" ${S.guard?.power !== false ? 'checked' : ''}> 允许她用命令、文件、注册表、进程、剪贴板、抓网页、通知 (每次都问你)</label>
      <div class="hint">删文件、装软件、下载来运行、改注册表和系统设置、关机、系统目录和密钥令牌这类会直接被拦下, 不会弹卡片. 取消勾选就整档不交给她.</div>
      <label class="checks"><input type="checkbox" id="sStopInput" ${s.stop_on_input !== false ? 'checked' : ''}> 她操作时你一点鼠标、一按键盘, 她就停</label>
      <div class="hint">随时可以按 Ctrl+Alt+Shift+Backspace 或点对话栏上的「停」; 托盘里有"断开电脑操控".</div>
      <div class="hint">另外不碰的程序 (一行一个程序名, 比如 Alipay.exe):</div>
      <textarea class="mono" id="sGuardProcs" placeholder="一行一个">${esc((S.guard?.processes || []).join('\n'))}</textarea>
    </div>` : ''}
    <div class="row"><label>调用超时</label><div><input type="number" class="mono" id="sTimeout" min="5" max="600" step="5" value="${esc(s.timeout_s || 60)}"> 秒
      <div class="hint">一次工具调用等这么久没回就放弃 (5 到 600). 在调的时候也可以在对话栏里点"停".</div></div></div>
    </div><div class="card settings-group"><h2>${part === 'env' ? '环境变量' : '请求头'}</h2>
    <div class="row top"><label>认证信息</label><div class="secret-list">
      ${names.map((n) => `<div class="secret"><code>${esc(n)}</code><span class="saved">${(secrets[s.id]?.remove?.[part] || []).includes(n) ? '保存后删掉' : '已保存 (不显示)'}</span>
        <button type="button" class="btn danger" data-del="${esc(n)}">删</button></div>`).join('')}
      ${Object.keys(pending).map((n) => `<div class="secret"><code>${esc(n)}</code><span class="saved">新填的, 保存中</span><span></span></div>`).join('')}
      <div class="secret"><input type="text" class="mono" id="sKey" placeholder="${part === 'env' ? '名字, 例如 GITHUB_TOKEN' : '名字, 例如 Authorization'}">
        <input type="password" class="mono" id="sVal" placeholder="值" autocomplete="off"><button type="button" class="btn" id="sAdd">加上</button></div>
      <div class="hint">这里的值会加密保存, 和 API key 一样只能换、不能看.</div>
    </div></div>
  </div>${test ? `<div class="card settings-group"><h2>连接状态与工具</h2>${testResult(s, test)}</div>` : ''}`;
}

function testResult(s, t) {
  if (t.pending) return '<div class="result">连接中… (第一次用 npx 起的服务器要下载, 可能要等一会儿)</div>';
  if (!t.ok) return `<div class="result"><span class="err">连不上</span>: ${esc(t.error)}</div>`;
  const info = t.server_info || {};
  return `<div class="result"><span class="ok">连上了</span> · 协议 ${esc(t.version)}${t.modern ? ' (新版)' : ' (旧版)'} ·
    ${esc(info.title || info.name || '')} ${esc(info.version || '')} · ${t.tools.length} 个工具 · ${Math.round(t.ms)} ms
    ${t.instructions ? `<div class="hint">服务器说明: ${esc(t.instructions)}</div>` : ''}
    <table><thead><tr><th>工具</th><th>说明</th><th>权限</th></tr></thead><tbody>
    ${t.tools.map((tool) => {
      const perm = s.tools?.[tool.name] || defaultPerm(tool);
      const control = tool.tier === 'screen' || tool.tier === 'act' || tool.tier === 'power';
      const options = control ? CONTROL_PERMS : PERMS;
      const cell = tool.tier === 'locked' ? '<span class="badge off">锁定</span>'
        : `<select data-perm="${esc(tool.name)}">${Object.entries(options).map(([k, v]) => `<option value="${k}" ${k === perm ? 'selected' : ''}>${v}</option>`).join('')}</select>`;
      return `<tr><td><code>${esc(tool.name)}</code>${tool.tier ? `<div class="hint">${TIERS[tool.tier] || ''}</div>` : ''}</td><td>${esc(tool.description)}</td><td>${cell}</td></tr>`;
    }).join('')}
    </tbody></table>
    <div class="hint">${s.kind ? '读屏、动手每次在对话栏里问你 ("允许一次 / 这一轮都允许 / 拒绝"); 锁定的不交给她.'
      : '只读的工具默认自动调用; 其余默认每次在对话栏里问你 ("允许一次 / 总是允许 / 拒绝"). 删文件、付款、发消息这类最好禁用.'}</div></div>`;
}

const defaultPerm = (tool) => ({ read: 'auto', screen: 'ask', act: 'ask', power: 'ask', locked: 'off' })[tool.tier] || (tool.annotations?.readOnlyHint ? 'auto' : 'ask');

function bindMcpEditor(main, s) {
  const on = (sel, ev, fn) => $(sel, main)?.addEventListener(ev, fn);
  on('#sName', 'input', (e) => {
    const v = e.target.value.trim();
    const ok = NAME_RE.test(v) && !S.mcp.some((x) => x !== s && x.name === v);
    $('#sNameHint').style.color = ok ? '' : 'var(--danger)';
    if (!ok) return;
    s.name = v;
    changed();
    const title = $('.card-head h2', main);
    if (title) title.firstChild.textContent = v;
  });
  on('#sEnabled', 'change', (e) => { s.enabled = e.target.checked; changed(); });
  on('#sStopInput', 'change', (e) => { s.stop_on_input = e.target.checked; changed(); });
  on('#sPower', 'change', (e) => {
    S.guard = { ...(S.guard || {}), power: e.target.checked };
    changed();
  });
  on('#sGuardProcs', 'input', (e) => {
    S.guard = { ...(S.guard || {}), processes: e.target.value.split('\n').map((x) => x.trim()).filter(Boolean) };
    changed();
  });
  $$('[name="sTransport"]', main).forEach((r) => r.addEventListener('change', () => { s.transport = r.value; changed(); render(); }));
  on('#sUrl', 'input', (e) => { s.url = e.target.value.trim(); changed(); });
  on('#sCommand', 'input', (e) => { s.command = e.target.value.trim(); changed(); });
  on('#sArgs', 'input', (e) => { s.args = e.target.value.split('\n').map((x) => x.trim()).filter(Boolean); changed(); });
  on('#sCwd', 'input', (e) => { s.cwd = e.target.value.trim(); changed(); });
  on('#sTimeout', 'input', (e) => {
    const v = Math.round(Number(e.target.value));
    if (v >= 5 && v <= 600) { s.timeout_s = v; changed(); }
  });
  const part = s.transport === 'http' ? 'headers' : 'env';
  on('#sAdd', 'click', async () => {
    const k = $('#sKey').value.trim(), v = $('#sVal').value;
    if (!k || !v) return;
    queueSecretSet(s.id, part, k, v);
    if (await save()) render();
  });
  $$('[data-del]', main).forEach((b) => b.addEventListener('click', async () => {
    queueSecretRemove(s.id, part, b.dataset.del);
    if (await save()) render();
  }));
  on('#sTest', 'click', () => testServer(s.id));
  on('#sDelete', 'click', () => {
    S.mcp = S.mcp.filter((x) => x.id !== s.id);
    S.personas.forEach((p) => { p.mcp = (p.mcp || []).filter((id) => id !== s.id); });
    ui.server = null;
    changed();
    render();
  });
  $$('[data-perm]', main).forEach((sel) => sel.addEventListener('change', () => {
    s.tools = { ...(s.tools || {}), [sel.dataset.perm]: sel.value };
    changed();
  }));
}

async function testServer(id) {
  ui.tests[id] = { pending: true };
  render();
  await flush();
  const r = await call('mcp-test', { server: id });
  if (settingsOperation) await settingsOperation;
  ui.tests[id] = r;
  // 第一次测通: 没定过权限的工具按默认值记下来
  const s = S.mcp.find((x) => x.id === id);
  if (r.ok && s) {
    s.tools = { ...(s.tools || {}) };
    for (const t of r.tools) s.tools[t.name] ??= defaultPerm(t);
    changed();
  }
  if (ui.tab === 'mcp') render();
  return r;
}

function renderSkills(main) {
  const ed = ui.skillEdit;  // 正在新建或改的那个: { name, description, body, isNew, bundled }
  const persona = S.personas.find((p) => p.id === S.active) || S.personas[0];
  const usesComputer = S.mcp.some((server) => server.kind && server.enabled !== false && persona?.mcp?.includes(server.id));
  main.innerHTML = `
    <h1>技能</h1>
    <p class="lead">把常用的做事方法教给她。在人设预设中启用，需要时自动读取。</p>
    ${ed ? `<div class="card" id="kEditor">
      <h2>${ed.isNew ? '新建技能' : `编辑技能 · ${esc(ed.name)}`}${ed.bundled ? ' <span class="badge off">保存为自定义副本</span>' : ''}</h2>
      <div class="row"><label for="kName">名字</label><div><input type="text" class="mono" id="kName" value="${esc(ed.name)}" placeholder="比如 bedtime-story" ${ed.isNew ? '' : 'readonly'}>
        <div class="hint">小写英文、数字和 -. 人设预设里按这个名字勾选.</div></div></div>
      <div class="row"><label for="kDesc">什么时候用</label><div><textarea id="kDesc" rows="2" placeholder="比如: 用户要听睡前故事时用">${esc(ed.description)}</textarea>
        <div class="hint">模型平时只看到名字和这句, 靠它决定要不要读全文. 写清楚什么情况下用.</div></div></div>
      <div class="row"><label for="kBody">怎么做</label><div><textarea id="kBody" class="mono" rows="14" placeholder="# 标题&#10;1. 第一步…">${esc(ed.body)}</textarea>
        <div class="hint">SKILL.md 的正文 (Markdown). 第一版只读文字, 不执行里面的脚本.</div></div></div>
      <div class="actions"><button type="button" class="btn primary" id="kSave">保存</button><button type="button" class="btn" id="kCancel">取消</button>
        <span class="hint" id="kMsg"></span></div>
    </div>` : ''}
    <div class="skill-library" ${ed ? 'hidden' : ''}>
    <div class="card settings-group skill-preset"><div class="settings-list-row"><span class="setting-copy"><span class="setting-label">为当前预设启用技能</span><span class="setting-description">${esc(persona?.name || '默认预设')}${persona?.provider === 'local' ? ' · 内置小模型暂不调用工具，选择会保留。' : ' · 更改会自动保存，下次对话时使用。'}</span></span><button type="button" class="btn" id="kPersona">管理预设</button></div></div>
    <section class="card skills-section settings-group">
      <div class="section-toolbar"><h2>已安装 <span class="section-count">${ui.skills.length}</span></h2>
        <button type="button" class="btn primary" id="kNew">＋ 新建技能</button></div>
      <label class="skill-search"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/></svg>
        <input type="search" id="kSearch" placeholder="搜索名称或描述" aria-label="搜索已安装技能" value="${esc(ui.skillQuery || '')}"></label>
      <div class="skill-list">
      ${ui.skills.length ? ui.skills.map((k) => `<article class="skill" data-skill-search="${esc(`${k.name} ${k.description || ''}`.toLowerCase())}">
        <div class="skill-icon" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9Z"/></svg></div>
        <div class="skill-content"><div class="name">${esc(k.name)}${k.bundled ? ' <span class="badge off">内置</span>' : ''}</div>
          <p class="desc">${esc((k.description || '尚未添加描述').slice(0, 110))}${k.description?.length > 110 ? '…' : ''}</p>
          <details class="skill-details"><summary>查看详情</summary>${k.description?.length > 110 ? `<p>${esc(k.description)}</p>` : ''}<div class="dir">${esc(k.dir)}</div></details>
        </div>
        <div class="skill-actions"><label class="switch-control" title="${k.name === 'computer-use' && usesComputer ? '随电脑操控服务器自动启用' : '在当前预设中启用'}"><input type="checkbox" class="switch" data-kenabled="${esc(k.name)}" aria-label="启用技能 ${esc(k.name)}" ${persona?.skills?.includes(k.name) || (k.name === 'computer-use' && usesComputer) ? 'checked' : ''} ${k.name === 'computer-use' && usesComputer ? 'disabled' : ''}></label><button type="button" class="btn" data-kedit="${esc(k.name)}">${k.bundled ? '自定义' : '编辑'}</button>
          ${k.bundled ? '' : `<button type="button" class="btn danger ghost" data-kdel="${esc(k.name)}" aria-label="删除 ${esc(k.name)}">删除</button>`}</div>
      </article>`).join('') : ''}
      <p class="empty skill-empty" id="kEmpty" ${ui.skills.length ? 'hidden' : ''}>${ui.skills.length ? '没有匹配的技能，试试其他关键词。' : '还没有技能。新建一个，或将现有 SKILL.md 放入技能文件夹。'}</p>
      </div>
      <p class="hint section-foot">开关只影响当前预设。其他预设的技能可以在<a href="#" data-goto="personas">人设预设</a>中分别设置。</p>
    </section>
    <section class="card skill-folder">
      <div><h2>技能文件夹</h2><p class="hint">放入现有技能后，重新扫描即可载入。</p><div class="hint mono folder-path">${esc(ui.skillsDir)}</div></div>
      <div class="actions"><button type="button" class="btn" id="kOpen">打开文件夹</button><button type="button" class="btn" id="kScan">重新扫描</button></div>
    </section>
    <details class="skill-help">
      <summary>技能如何工作</summary>
      <p>每个技能是一个包含 <code>SKILL.md</code> 的文件夹，用来描述某类任务的做法。模型先查看名称和描述，使用时才读取全文。目前只读取文字，不执行技能中的脚本。</p>
      <p>启用操控电脑的服务器时，会自动带上内置 computer-use 技能。自定义同名技能会覆盖内置版本。删除的技能移入文件夹内的 <code>.trash</code>，可以手动移回。</p>
      <h3>SKILL.md 示例</h3>
      <pre>---
name: bedtime-story
description: 用户要听睡前故事时用: 语速放慢, 故事温和, 结尾道晚安
---

# 睡前故事
1. 先问想听什么主题 ...</pre>
    </details></div>`;
  $('#kPersona').addEventListener('click', () => show('personas', { persona: persona.id }));
  $$('[data-kenabled]', main).forEach((input) => input.addEventListener('change', () => {
    const skills = new Set(persona.skills || []);
    if (input.checked) skills.add(input.dataset.kenabled); else skills.delete(input.dataset.kenabled);
    persona.skills = [...skills];
    changed();
  }));
  const filterSkills = () => {
    const query = $('#kSearch').value.trim().toLowerCase();
    ui.skillQuery = $('#kSearch').value;
    const items = $$('[data-skill-search]', main);
    items.forEach((item) => { item.hidden = !!query && !item.dataset.skillSearch.includes(query); });
    $('#kEmpty').hidden = items.some((item) => !item.hidden);
  };
  $('#kSearch').addEventListener('input', filterSkills);
  filterSkills();
  $('#kOpen').addEventListener('click', () => tell('open-skills'));
  $('#kScan').addEventListener('click', async () => {
    const r = await call('skills');
    ui.skills = r.skills || [];
    ui.faces = r.faces || ui.faces;
    ui.skillsDir = r.skills_dir || ui.skillsDir;
    render();
  });
  $('#kNew').addEventListener('click', () => {
    ui.skillEdit = { name: '', description: '', body: '', isNew: true };
    render();
    $('#kName').focus();
    $('#main').scrollTop = 0;
  });
  $$('[data-kedit]').forEach((b) => b.addEventListener('click', async () => {
    const r = await call('skill-read', { name: b.dataset.kedit });
    if (ui.tab !== 'skills') return;
    if (!r.ok) { setStatus(r.error || '读不到这个 skill', true); return; }
    const k = r.skill;
    ui.skillEdit = { name: k.name, description: k.description || '', body: k.body || '', isNew: false, bundled: !!k.bundled };
    render();
    $('#kDesc').focus();
    $('#main').scrollTop = 0;
  }));
  $$('[data-kdel]').forEach((b) => b.addEventListener('click', async () => {
    if (!confirm(`删掉 skill「${b.dataset.kdel}」? (挪进 .trash, 还能找回)`)) return;
    const r = await call('skill-delete', { name: b.dataset.kdel });
    if (r.ok) ui.skills = r.skills || ui.skills;
    setStatus(r.ok ? (ui.tab === 'skills' ? '技能已删除' : '设置已同步') : (r.error || '无法删除技能'), !r.ok);
    render();
  }));
  if (!ed) return;
  $('#kCancel').addEventListener('click', () => { ui.skillEdit = null; render(); });
  $('#kSave').addEventListener('click', () => saveSkill());
  for (const [selector, field] of [['#kName', 'name'], ['#kDesc', 'description'], ['#kBody', 'body']]) $(selector).addEventListener('input', (e) => { ed[field] = e.target.value; });
}

/** Skill 页的"保存": 写成 SKILL.md (宿主 settings::save_skill), 成功就关掉编辑框、刷新列表. */
async function saveSkill() {
  const ed = ui.skillEdit;
  if (!ed) return false;
  ed.name = $('#kName').value.trim();
  ed.description = $('#kDesc').value;
  ed.body = $('#kBody').value;
  const r = await call('skill-save', { name: ed.name, description: ed.description, body: ed.body });
  if (!r.ok) {
    if ($('#kMsg') && ui.skillEdit === ed) $('#kMsg').textContent = r.error || '无法保存技能';
    else setStatus(r.error || '无法保存技能', true);
    return false;
  }
  ui.skills = r.skills || ui.skills;
  if (ui.skillEdit === ed) ui.skillEdit = null;
  setStatus(ui.tab === 'skills' ? '技能已保存' : '设置已同步');
  render();
  return true;
}

// ---------------------------------------------------------------------------
/**
 * 自检 (面板地址带 ?selftest=1 时): 不动用户的设置 (宿主用 --settings-dir 指到临时目录). 依次:
 * 读默认设置 -> 加一个人设、给 DeepSeek 填一个假 key、加一个 MCP 服务器 (连桌宠自己的 --mcp-stdio) 和一个假的环境变量 -> 保存
 * -> 确认回来的设置里没有密钥明文 -> 设为当前 -> 测试连接 -> 取"自定义"提供方的模型列表 (没填地址, 应当报错). 结果交给宿主写进 --out.
 */
async function selftest() {
  const checks = [];
  const check = (name, ok, info = {}) => checks.push({ name, ok: !!ok, ...info });
  check('默认设置: 有默认人设和 5 个提供方 (本机模型面板自己补)', S.personas.some((p) => p.id === 'default') && PROVIDER_IDS.every((id) => S.providers[id]));
  const persona = { id: 'p-selftest', name: '自检人设', prompt: '你是自检用的人设.', provider: 'deepseek', model: 'deepseek-selftest', mcp: [], skills: [] };
  const server = { id: 's-selftest', name: 'self', transport: 'stdio', command: PARAMS.get('mcpcmd') || '', args: (PARAMS.get('mcpargs') || '').split(' ').filter(Boolean),
    cwd: '', url: '', enabled: true, tools: {} };
  S.personas.push(persona);
  S.mcp.push(server);
  persona.mcp = [server.id];
  keys.deepseek = 'sk-selftest-not-a-real-key';
  queueSecretSet(server.id, 'env', 'WM_SELFTEST_SECRET', 'selftest-secret-value');
  const saved = await save();
  const text = JSON.stringify(S);
  check('保存', saved);
  check('回来的设置里没有密钥明文', !text.includes('sk-selftest-not-a-real-key') && !text.includes('selftest-secret-value'));
  check('DeepSeek 显示 key 已保存', S.providers.deepseek.has_key === true && !('key' in S.providers.deepseek));
  check('MCP 服务器显示环境变量名', S.mcp.find((x) => x.id === server.id)?.secret_names?.env?.includes('WM_SELFTEST_SECRET'));
  S.active = persona.id;
  check('设为当前并保存', await save());
  const t = await testServer(server.id);
  check('测试连接 (桌宠自己的 MCP 服务端)', t.ok && t.tools.some((x) => x.name === 'pet_state'), { version: t.version, tools: t.tools?.length, error: t.error, ms: t.ms });
  check('工具权限按默认记下 (pet_state 只读 -> 自动, pet_chat -> 每次确认)', S.mcp.find((x) => x.id === server.id)?.tools?.pet_state === 'auto'
    && S.mcp.find((x) => x.id === server.id)?.tools?.pet_chat === 'ask');
  await flush();
  // 连着改、每次都存: 以前存完把 S 整个换掉, 页面手里的对象 (人设、外部渠道页的 q = S.qq) 还是旧的, 第二次起的改动就丢了
  const typeInto = (sel, v) => { const el = $(sel); el.value = v; el.dispatchEvent(new Event('input')); };
  ui.persona = persona.id;
  ui.personaDetail = true;
  show('personas');
  typeInto('#pName', '自检人设改名一');
  await flush();
  typeInto('#pName', '自检人设改名二');
  await flush();
  const names = (await call('get')).settings.personas.map((p) => p.name);
  check('人设页连着改两次名字、各存一次: 存下来的是第二次的', names.includes('自检人设改名二'), { names });
  typeInto('#pName', '自检人设');  // 改回来: paneltest 后面按这个名字查 pet_state
  await flush();
  show('qq');
  typeInto('#qSelf', '3123456789');
  await flush();
  typeInto('#qOwner', '2987654321');
  await flush();
  const qq = (await call('get')).settings.qq || {};
  check('外部渠道页先填她的号、再填你的号, 各存一次: 两个 10 位号都在', qq.self_id === 3123456789 && qq.owner_id === 2987654321,
    { self_id: qq.self_id, owner_id: qq.owner_id });
  const tick = (sel) => { const el = $(sel); el.checked = !el.checked; el.dispatchEvent(new Event('change')); };
  tick('#qProOn');
  tick('#qQzOn');
  typeInto('#qNames', '绫音');
  await flush();
  const own = (await call('get')).settings.qq || {};
  check('"她自己开口": 勾上主动找你和 QQ 空间、名字改成绫音, 都存下来了', own.proactive?.enabled === true && own.qzone?.enabled === true
    && own.names?.[0] === '绫音', { proactive: own.proactive, qzone: own.qzone, names: own.names });
  // Skill 页: 新建、读回来、名字不对的拒绝、删 (挪进 .trash)
  show('skills');
  $('#kNew').click();
  $('#kName').value = 'selftest-skill';
  $('#kDesc').value = '自检用: 测试\n新建 skill';
  $('#kBody').value = '# 自检\n1. 什么也不做';
  check('Skill 页新建并保存, 列表里有了', await saveSkill() && ui.skills.some((k) => k.name === 'selftest-skill'));
  const rd = await call('skill-read', { name: 'selftest-skill' });
  check('  读回来: 描述折成一行, 正文一致', rd.ok && rd.skill.description === '自检用: 测试 新建 skill' && rd.skill.body === '# 自检\n1. 什么也不做',
    { skill: rd.skill });
  const bad = await call('skill-save', { name: 'Bad Name', description: '随便', body: '' });
  check('  名字不对的拒绝', !bad.ok && !!bad.error, { error: bad.error });
  const del = await call('skill-delete', { name: 'selftest-skill' });
  check('  删了 (挪进 .trash), 列表里没了', del.ok && !del.skills.some((k) => k.name === 'selftest-skill'), { error: del.error });
  // 外部渠道页: 默认人设 (没选预设) 配云端模型
  show('qq');
  const pv = $('#qProvider');
  pv.value = 'deepseek';
  pv.dispatchEvent(new Event('change'));
  await flush();
  const qm = (await call('get')).settings.qq || {};
  check('外部渠道页: 默认人设配 DeepSeek, 存下来了', qm.provider === 'deepseek' && !qm.persona && !!$('#qModel') && !$('#qModel').hidden,
    { provider: qm.provider, persona: qm.persona });
  show('personas');
  const m = await call('models', { provider: 'custom' });
  check('没填地址时取模型列表报错', !m.ok && m.error, { error: m.error });
  const reread = await call('get');
  check('重新读出来的设置一致', reread.settings.active === persona.id && reread.settings.providers.deepseek.has_key === true);
  for (const c of await billingSelftest(show, $)) check(c.name, c.ok);
  for (const c of await imSelftest(show, $)) check(c.name, c.ok);
  for (const c of await diagnoseSelftest(show, $)) check(c.name, c.ok);
  show('personas');
  tell('selftest', { result: { panel: 'selftest', checks, failed: checks.filter((c) => !c.ok).map((c) => c.name) } });
}

async function reload() {
  if (hasPendingSettings() || saving || settingsOperation) return;
  const readRevision = revision;
  const r = await call('get');
  if (revision !== readRevision || hasPendingSettings() || saving || settingsOperation) return;
  adopt(S, r.settings);
  ensureProviders();
  ui.skills = r.skills || ui.skills;
  ui.persona = S.active;
  setStatus('设置已同步');
  render();
}

/**
 * 门面列表: 宿主给的 (3D 用服务端默认的角色、2D), 再加上服务端 models/vrm 里的每个 3D 模型 —— 选了就用那个模型
 * (存成 vrm:文件名, 宿主打开页面时带 avatar=文件名). 服务端没开时只有宿主给的.
 */
async function loadFaces(fromHost = []) {
  let items = [];
  try { items = (await (await fetch('/api/avatars')).json()).items || []; } catch { /* 服务端没开 */ }
  const base = fromHost.map((f) => (f.id === 'vrm' ? { ...f, name: '3D (服务端默认的角色)' } : f));
  return [...base, ...items.map((i) => ({ id: `vrm:${i.name}`, name: `3D · ${i.name.replace(/\.vrm$/i, '')}`, ok: true }))];
}

async function main() {
  initPreferences();
  if (EMBED) document.body.classList.add('embed');
  if (EMBED && $('.nav-items')) {  // 工作台里 (第 5 块): "扩展与下载"前面多一组"工作台" —— 内置插件、Agent 预设在工作台的面板里, 点了关设置、开那一页
    const icons = { capabilities: '<rect x="4" y="4" width="7" height="7" rx="1.6"/><rect x="13" y="4" width="7" height="7" rx="1.6"/><rect x="4" y="13" width="7" height="7" rx="1.6"/><path d="M16.5 13v7M13 16.5h7"/>',
      presets: '<rect x="5" y="4" width="14" height="16" rx="2"/><path d="M9 9h6M9 13h6M9 17h3"/>' };
    const group = $$('.nav-items .nav-group').find((g) => g.textContent.trim() === '扩展与下载');
    const html = `<div class="nav-group">工作台</div>${[['capabilities', '内置插件'], ['presets', 'Agent 预设']].map(([tool, label]) => `<button type="button" data-workbench-tool="${tool}"><svg viewBox="0 0 24 24">${icons[tool]}</svg><span>${label}</span></button>`).join('')}`;
    if (group) group.insertAdjacentHTML('beforebegin', html); else $('.nav-items').insertAdjacentHTML('beforeend', html);
    $('.nav-items').addEventListener('click', (e) => {
      const b = e.target.closest('[data-workbench-tool]');
      if (b) parent.postMessage({ wmPanelUI: 'open-tool', tool: b.dataset.workbenchTool }, location.origin);
    });
  }
  $('#main').addEventListener('click', (e) => {
    const a = e.target.closest('[data-goto]');
    if (!a) return;
    e.preventDefault();
    show(a.dataset.goto);
    $('#main').scrollTop = 0;
  });  // 工作台里: 透明底 (透出工作台的柔光), 不要品牌行和"工作台 ↗"
  $$('.nav button[data-tab]').forEach((b) => b.addEventListener('click', () => show(b.dataset.tab)));
  $('#navSearch')?.addEventListener('input', (e) => {
    ui.search = e.target.value.trim();
    render();
    $('#main').scrollTop = 0;
  });
  $('#navSearch')?.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.target.value = '';
      e.target.dispatchEvent(new Event('input', { bubbles: true }));
    } else if (e.key === 'Enter') {
      const first = $('[data-search-result]');
      if (first) { e.preventDefault(); first.click(); }
    }
  });
  $('#openWorkbench')?.addEventListener('click', () => tell('open-workbench'));
  if (EMBED) addEventListener('keydown', async (e) => {
    const closeShortcut = e.key === 'Escape' || ((e.ctrlKey || e.metaKey) && e.key === ',');
    // 让弹窗、下拉、搜索清除和未保存的技能编辑器优先处理 Escape。
    if (!closeShortcut || e.defaultPrevented || $('dialog[open], .dd.open') || ui.skillEdit) return;
    if (e.key === 'Escape' && e.target.matches?.('input[type="search"]') && e.target.value) return;
    e.preventDefault();
    await confirmPanelClose();
  }, true);
  autoEnhance($('#main'));  // 每一页的下拉都换成磨砂玻璃的 (ui-kit.js), 原生的留着管值和 change 事件
  const r = await call('get');
  S = r.settings;
  ensureProviders();
  ui.skills = r.skills || [];
  ui.skillsDir = r.skills_dir || '';
  ui.faces = await loadFaces(r.faces || []);
  ui.persona = S.active;
  setStatus('设置已同步');
  show(ui.tab);
  if (PARAMS.get('mcptest')) {  // 截图用: 先测一下这个服务器, MCP 服务器那页才有工具表 (操控电脑的四档)
    ui.server = PARAMS.get('mcptest');
    await testServer(ui.server);
  }
  tell('ready', { tabs: TABS });
  if (PARAMS.get('selftest') === '1') selftest().catch((err) => tell('selftest', { result: { panel: 'selftest', error: String(err?.stack || err) } }));
  if (PARAMS.get('exttest') === '1') {  // 扩展页的自检 (exttest.ps1): 内容包的预览、装、卸、坏包、导出
    extSelftest({ S: () => S, call: callExtension, flush, render: () => {}, setSettings: acceptExtensionSettings, isTab: () => false }, tell)
      .catch((err) => tell('selftest', { result: { panel: 'exttest', error: String(err?.stack || err) } }));
  }
}

window.addEventListener('error', (e) => tell('log', { text: `错误 ${e.message} @ ${e.filename}:${e.lineno}` }));
main();
