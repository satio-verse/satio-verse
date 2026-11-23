// 工作台页面 (桌宠的工作台窗口 workbench.cpp, 浏览器打开服务端的 /agent 也是这一份). 活在服务端 (wm/agent): /api/agent 和 /agent/ws.
// 左边: 她的名片 + 工作区和会话; 中间: 你说的、她说的 (带头像)、她每一步用了什么工具 (一行一条, 点开看结果和改动)、要你点头的卡片;
// 底下: 输入框 (工作区、权限菜单、智能搜索、模型 + 做法 + 思考强度的菜单), 状态条. 模型和工具结果里的字一律当不可信的文字: 经 esc 再放进页面.
import { getPreferences, initPreferences, updatePreferences } from '/pet/ui-preferences.js';
import { initStudio } from '/pet/workbench-studio.js';
import { initNavigation } from '/pet/workbench-navigation.js';
import { canvasIcon, messageActions, renderMarkdown, showCitation } from '/pet/workbench-canvas.js';
import { initPanel } from '/pet/workbench-panel.js';
import { collectChanges, turnRanges, extractLinks, relativePath } from '/pet/workbench-diff.js';

const $ = (s, r = document) => r.querySelector(s);
// 菜单里每一项下面的一行说明 (学 DSH 的模式菜单、ChatGPT 的"应如何批准")
const DESC = {
  standard: '读写文件、搜、跑命令, 按权限来', plan: '先只看、列计划, 你说"按计划做"再动手',
  read: '只看文件、搜、读网页; 不改文件、不跑命令', workspace: '工作区里直接改; 跑命令先问你 (只读的、名单里的不问)',
  full: '哪里都能改、命令直接跑; 删东西、装软件、联网下载照样问你',
  unrestricted: '按你的任务直接执行，黑名单仍然生效；可随时停止或收回授权',
};
// 思考强度 (2026-10-28 用户: "把预设和模型合并到一起, 学习图一图二的设计" — ChatGPT 的模型菜单和推理强度滑条)
const EFFORT_KEYS = ['off', 'low', 'high', 'max'];
const EFFORT_DESC = { off: '不思考: 最快、最省', low: '想一点: 简单的事够用', high: '想清楚再动手: DeepSeek 默认这一档',
  max: '想透再动手: 最深的一档, 慢一些、多花一点' };
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const host = window.chrome?.webview;
const tell = (op, args = {}) => host?.postMessage(`workbench ${JSON.stringify({ op, ...args })}`);
const AVATAR = '/pet/brand/avatar-128.png';  // 绫音的个人头像 (2026-11-02 用户给的); 应用图标还是 brand/icon*.png
const S = { state: null, sid: null, view: null, ws: null, typing: '', thinkLive: '', running: false, closed: new Set(), slashSel: 0, filter: '',
  wallet: null, desk: null, deskOn: true, deskLoading: false, deskWant: null,
  pick: {} };  // 还没开会话时在菜单里选的 (权限、模型、做法、思考强度、智能搜索), 开会话时带上
let preferences = getPreferences();
const detailStates = new Map();
const notices = { queued: new Map(), seen: new Set(), audio: null };
let settingsCloseRequest = null, settingsCloseSequence = 0;
let studio = null, pendingSend = null, pendingSteer = null, creatingSession = null;
let navigation = null;
const revealRequests = new Map();
function revealWorkspaceFile(item) {
  if (!host) {flash('在桌面工作台中可打开系统文件夹');return;}
  if (!S.hostCaps?.reveal_file) throw new Error('现在这版桌宠宿主还不能打开文件位置（要等新版宿主）；可以先复制路径');  // 正式宿主 v2 没有, 不再干等 10 秒
  const workspace = item.workspace || S.view?.workspace?.path || S.view?.workspace;  // 项目右键"在文件夹中显示"带着那个项目自己的路径
  if ((!item.workspace && item.id !== S.view?.id) || !workspace) throw new Error('工作区已切换，请重新打开文件');
  return new Promise((resolve,reject) => {
    const rid=crypto.randomUUID(),timer=setTimeout(()=>{revealRequests.delete(rid);reject(new Error('打开文件位置超时，请重试'));},10000);
    revealRequests.set(rid,{resolve,reject,timer});tell('reveal_file',{rid,workspace,path:item.absolutePath});
  });
}
function attachWorkspaceText(item) {
  return studio.addTextMaterial({name:`${item.name || '文件'}${item.selection ? ' · 选段' : ' · 文本'}`,text:item.text,source:item.absolutePath || item.path,targetId:item.id});
}
let panel = null;  // 右侧面板 (workbench-panel.js): 网页、文件、变更、终端、协作都是它的标签
const expandedBranches = new Set();
const wsExpanded = new Set(), WS_LIMIT = 5;  // 左栏每个项目先列几个会话; 点"再显示"的项目全列
// 左栏怎么排 (照 Claude / Codex 的"分组依据 / 排序方式 / 显示"菜单), 记在本机: group folder | none, sort updated | created | title
const SIDE_VIEW = { group: 'folder', sort: 'updated', recent: true };
const readJSON = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } };
const writeJSON = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* 存不下就算了 */ } };
const sideView = () => ({ ...SIDE_VIEW, ...readJSON('wm.workbench.sideView', {}) });
function sortSessions(list) {
  const how = sideView().sort;
  const key = how === 'title' ? (a, b) => a.title.localeCompare(b.title, 'zh') : how === 'created' ? (a, b) => String(b.id).localeCompare(String(a.id)) : (a, b) => (b.updated || 0) - (a.updated || 0);
  return [...list].sort((a, b) => (!!b.pinned - !!a.pinned) || key(a, b));  // 置顶的永远在前
}
function orderedWorkspaces() {  // 项目顺序: 右键"上移 / 下移"排过的照排, 新加的在后
  const order = readJSON('wm.workbench.wsOrder', []), at = (w) => { const i = order.indexOf(w.path); return i < 0 ? 1e9 : i; };
  return [...(S.state?.workspaces || [])].sort((a, b) => at(a) - at(b));
}
function moveWorkspace(path, step) {
  const paths = orderedWorkspaces().map((w) => w.path), i = paths.indexOf(path), j = i + step;
  if (i < 0 || j < 0 || j >= paths.length) return;
  [paths[i], paths[j]] = [paths[j], paths[i]];
  writeJSON('wm.workbench.wsOrder', paths);
  renderSide();
}
let sessionGeneration = 0, stateGeneration = 0, canvasReloadGeneration = 0;
const settingRequests = new Map();
const isCurrentSession = (id, generation) => S.sid === id && sessionGeneration === generation;
let welcomeScene = '';
const entranceAnimations = new Set();
const motionAllowed = () => !document.hidden && preferences.reducedMotion !== 'on'
  && (preferences.reducedMotion === 'off' || !matchMedia('(prefers-reduced-motion: reduce)').matches);
function enterOnce(node, delay = 0) {
  if (!node || !motionAllowed()) return;
  const animation = node.animate([{ opacity: .25, transform: 'translateY(6px)' }, { opacity: 1, transform: 'none' }],
    { duration: 220, delay, easing: 'cubic-bezier(.2,.7,.2,1)', fill: 'backwards' });
  entranceAnimations.add(animation);
  animation.finished.catch(() => {}).finally(() => entranceAnimations.delete(animation));
}
function welcomeEntrance(root, key) {
  if (welcomeScene === key) return;
  welcomeScene = key;
  root.querySelectorAll('.wb-welcome-av, .welcome h2, .welcome > p, .wb-starters button')
    .forEach((node, index) => enterOnce(node, index * 18));
}
function incomingEntrance(type) {
  const selector = { user: '.msg-user', assistant: '.her-msg.response', error: '.error', done: '.wb-edits, .wb-src-btn' }[type];
  if (selector) enterOnce([...$('#chat').querySelectorAll(selector)].at(-1));
}
const SLASH = [['/plan', '计划模式: 她先看、列计划, 你点头再做'], ['/compact', '压缩: 把较早的对话写成一份检查点 (原文还能回查); 后面可以跟要留住的重点']];
// 检查点卡 (2026-11-07, wm/agent/compaction.py 的 compact 事件): 为什么压、压前压后大约多少字
const CKPT_WHY = { manual: '你让她压的', budget: '上下文快满了', overflow: '模型说装不下了' };
const fmtChars = (n) => { n = Number(n) || 0; return n >= 10000 ? `${(n / 10000).toFixed(1)} 万` : n.toLocaleString(); };
const STARTERS = [['了解项目', '看看这个工作区里有什么，介绍一下结构和主要用途。', 'list_dir', '从文件结构开始'], ['写个脚本', '写一个脚本统计这个文件夹里每种文件有几个，然后跑一下。', 'run_command', '把重复的事交给我'],
  ['整理文件', '把这个文件夹里的文件按类型整理进子文件夹，先列计划给我看。', 'edit_file', '先计划，再整理'], ['查找问题', '在这个工作区的代码里搜一下 TODO，列出来并按重要程度排一排。', 'grep', '一起找到下一步']];
const ICON = {  // 单色小图标 (和桌宠侧菜单一个画法)
  read_file: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 12h6M9 16h6"/>',
  list_dir: '<path d="M3 6.5h6l2 2h10V19H3z"/>',
  glob: '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5"/>',
  grep: '<circle cx="11" cy="11" r="6"/><path d="M20 20l-4.5-4.5M8.5 11h5"/>',
  write_file: '<path d="M6 3h8l4 4v14H6z"/><path d="M12 10v6M9 13h6"/>',
  edit_file: '<path d="M4 20h4L19 9l-4-4L4 16z"/><path d="M13.5 6.5l4 4"/>',
  run_command: '<rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="M7 10l3 2.5L7 15M12.5 15h4"/>',
  her_skill: '<path d="M12 20s-7-4.4-7-10a4 4 0 017-2.6A4 4 0 0119 10c0 5.6-7 10-7 10z"/>',
  web_search: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z"/>',
  web_fetch: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/><circle cx="12" cy="14" r="3"/>',
};
const TOG_ICON = { web: ICON.web_search };  // 输入框下面的开关: 地球 (智能搜索)
const MODE_ICON = {  // 权限三档: 眼睛 (只看)、举手 (先问你)、带叹号的盾 (完全权限)
  read: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/>',
  workspace: '<path d="M8 11.5V5.8a1.4 1.4 0 0 1 2.8 0v5M10.8 10.5V4.6a1.4 1.4 0 0 1 2.8 0v5.9M13.6 10.5V5.6a1.4 1.4 0 0 1 2.8 0v6.4M16.4 10.5V8.2a1.4 1.4 0 0 1 2.8 0v5.3c0 4-2.8 7-6.8 7-2.7 0-4.3-1.3-5.8-3.5L4 13.6a1.5 1.5 0 0 1 2.4-1.8L8 13.6"/>',
  full: '<path d="M12 3l7.5 3v5.5c0 4.5-3.2 8.2-7.5 9.5-4.3-1.3-7.5-5-7.5-9.5V6z"/><path d="M12 8v4.5M12 15.8v.2"/>',
  unrestricted: '<path d="M12 3l7.5 3v5.5c0 4.5-3.2 8.2-7.5 9.5-4.3-1.3-7.5-5-7.5-9.5V6z"/><path d="m8 12 3 3 5-6"/>',
};
const CHEV_DOWN = '<path d="M7 10l5 5 5-5"/>';
const CHECK = '<path d="M5 12.5l4.5 4.5L19 7.5"/>';
const svg = (p, cls = '') => `<svg class="${cls}" viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">${p}</svg>`;
const SEND = svg('<path d="M12 19V5M5.5 11.5L12 5l6.5 6.5"/>');
const STOP = '<svg viewBox="0 0 24 24" width="14" height="14"><rect x="5" y="5" width="14" height="14" rx="2.5" fill="currentColor"/></svg>';

const ago = (t) => {
  const s = Date.now() / 1000 - t;
  return s < 60 ? '刚刚' : s < 3600 ? `${Math.floor(s / 60)} 分钟` : s < 86400 ? `${Math.floor(s / 3600)} 小时` : `${Math.floor(s / 86400)} 天`;
};
const tok = (n) => (n >= 1e4 ? `${(n / 1e4).toFixed(1)} 万` : String(n || 0));
const money = (x) => `¥${(x || 0) >= 0.01 || !x ? (x || 0).toFixed(2) : x.toFixed(4)}`;
const herName = () => S.state?.her?.name || '她';
// 右边的功能键 (2026-10-26 用户: "工作台打开的时候桌宠的小屋等都最小化关闭, 右侧的功能键会放到工作台上, 还包括设置也是"):
// 桌宠侧菜单的那几样搬过来, 状态由宿主发 {t: 'dock'} (麦克风、静音、工作台置顶、人设预设); 点了发 workbench {op: dock, act}
const RAIL_ICON = {
  mic: '<path d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z"/><path d="M19 11a7 7 0 0 1-14 0M12 18v3"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.6 3 2.6 15 0 18M12 3c-2.6 3-2.6 15 0 18"/>',
  micOff: '<path d="M15 9.3V6a3 3 0 0 0-5.7-1.3M9 9v3a3 3 0 0 0 5.1 2.1M19 11a7 7 0 0 1-1.2 3.9M5 11a7 7 0 0 0 10.6 6M12 18v3M3 3l18 18"/>',
  volume: '<path d="M4 9.5v5h3.5L12 18V6L7.5 9.5H4z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/>',
  volumeOff: '<path d="M4 9.5v5h3.5L12 18V6L7.5 9.5H4z"/><path d="M16 9.5l5 5M21 9.5l-5 5"/>',
  persona: '<circle cx="12" cy="8" r="3.6"/><path d="M4.5 20.5a7.5 7.5 0 0 1 15 0"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',  // 齿轮 (和桌宠侧菜单同一个); 以前那个像太阳, 会被当成切换明暗主题
  pin: '<path d="M8.5 3.5h7M10 3.5l-.6 6L6.5 13v1.5h11V13l-2.9-3.5-.6-6M12 14.5V21"/>',
  room: '<path d="M3.5 11 12 4l8.5 7"/><path d="M5.5 9.5V20h13V9.5"/><path d="M10 20v-5.5h4V20"/>',
  desktop: '<rect x="3" y="4" width="18" height="12" rx="1.8"/><path d="M8.5 20h7M12 16v4"/>',
};

async function api(body) {
  const r = await fetch('/api/agent', body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {});
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.error || `HTTP ${r.status}`);
  return d;
}
async function getView(id) {
  const r = await fetch(`/api/agent?id=${encodeURIComponent(id)}`);
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

// ---- 左边 ----
async function loadState() {
  const generation = ++stateGeneration;
  const state = await api();
  if (generation !== stateGeneration) return;
  S.state = state;
  renderSide();
  renderTools();
}

// 左栏照 Codex (2026-11-01): 新聊天、绫音、插件、自动化任务, 然后"项目"(工作区下挂会话) 和"最近"(跨工作区按时间)
function renderRecent(f) {
  const box = $('#recent');
  const all = (S.state?.workspaces || []).flatMap((w) => w.sessions.map((s) => ({ ...s, wsName: w.name })));
  const view = sideView();
  const rows = f || !view.recent || view.group === 'none' ? [] : all.filter((s) => !s.parent_id).sort((a, b) => (b.updated || 0) - (a.updated || 0)).slice(0, 12);
  $('#recentHead').hidden = !rows.length;
  box.innerHTML = rows.map((s) => `<button type="button" class="wb-recent-row ${s.id === S.sid ? 'sel' : ''}" data-recent="${esc(s.id)}" title="${esc(s.title)} · ${esc(s.wsName)}">
      ${sessionState(s)}<span class="t">${esc(s.title)}</span><span class="when">${ago(s.updated)}</span></button>`).join('');
  box.querySelectorAll('[data-recent]').forEach((el) => el.addEventListener('click', () => { openSession(el.dataset.recent); if (innerWidth <= 860) setSidebar(false); }));
}

// 会话的状态: 固定在标题前面一格 (第二批, 学 Linear: 一眼分得清) —— 在做转圈、等你橙点、待核查菱形、有新消息小圆点; 空着也占位, 标题对齐
const sessionState = (s) => s.status !== 'idle'
  ? (s.status === 'waiting' ? '<span class="ss ss-wait" title="等你点头"></span>' : '<span class="ss ss-run" title="在做"></span>')
  : s.check === 'needs_check' ? '<span class="ss ss-check" title="说做完了，但待核查"></span>' : s.unread ? '<span class="ss ss-unread" title="有新消息"></span>' : '<span class="ss"></span>';
function sessionRowHtml(s) {
  return `<div class="session-row ${s.id === S.sid ? 'selected' : ''} ${s.unread ? 'unread' : ''}" data-session-row="${esc(s.id)}"><button type="button" class="sess ${s.id === S.sid ? 'sel' : ''}" data-sid="${esc(s.id)}" ${s.id === S.sid ? 'aria-current="page"' : ''} title="${esc(s.title)}">
          ${sessionState(s)}${s.pinned ? svg(RAIL_ICON.pin, 'session-pin') : ''}<span class="t">${esc(s.title)}</span><span class="when">${ago(s.updated)}</span></button><button type="button" class="session-more" data-session-more="${esc(s.id)}" aria-label="${esc(s.title)}的更多操作" aria-haspopup="menu" aria-expanded="false" title="更多操作（也可右键会话）">${svg('<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>')}</button></div>`;
}

function renderBadges() {  // 统一状态 (wm/agent/activity.py) 的计数: 等你的标在"绫音"上, 待核查的标在"任务与成果"上
  const a = S.state?.activity || {};
  const you = Number(a.you) || 0, check = Number(a.needs_check) || 0;
  $('#herBadge').hidden = !you;
  $('#herBadge').textContent = you;
  $('#herBadge').title = you ? `有 ${you} 件事在等你` : '';
  $('#checkBadge').hidden = !check;
  $('#checkBadge').textContent = check;
  $('#checkBadge').title = check ? `${check} 件说做完了，但待核查` : '';
}

function renderSide() {
  const tree = $('#tree');
  const focusedBranch = tree.contains(document.activeElement) ? document.activeElement?.dataset.branch : null;
  const f = S.filter.trim().toLowerCase();
  renderRecent(f);
  renderBadges();
  const pinned = f ? [] : (S.state?.workspaces || []).flatMap((w) => w.sessions).filter((s) => s.pinned && !s.parent_id)
    .sort((a, b) => (b.updated || 0) - (a.updated || 0));  // 置顶的单独一组放最上面, 不分项目 (第二批, 学 Notion 的收藏); 搜的时候不另列
  $('#pinnedHead').hidden = !pinned.length;
  $('#pinned').innerHTML = pinned.map(sessionRowHtml).join('');
  $('#pinned').querySelectorAll('[data-sid]').forEach((el) => el.addEventListener('click', () => { openSession(el.dataset.sid); if (innerWidth <= 860) setSidebar(false); }));
  const flat = sideView().group === 'none';  // 不分组: 所有会话排成一列, 不显示项目那一行
  const groups = flat ? [{ path: '', name: '全部会话', sessions: (S.state?.workspaces || []).flatMap((w) => w.sessions) }] : orderedWorkspaces();
  tree.innerHTML = groups.map((w) => {
    const byId = new Map(w.sessions.map(s=>[s.id,s]));
    const visible = new Set(w.sessions.filter(s=>!f || s.title.toLowerCase().includes(f)).map(s=>s.id));
    if(f) for(const id of [...visible]) {let item=byId.get(id), seen=new Set();while(item?.parent_id&&!seen.has(item.parent_id)){seen.add(item.parent_id);visible.add(item.parent_id);item=byId.get(item.parent_id);}}
    const list = sortSessions(w.sessions.filter(s=>visible.has(s.id)));
    const roots = list.filter(s=>!s.parent_id||!byId.has(s.parent_id));
    const renderSession = (s,seen=new Set()) => {
      if(seen.has(s.id))return '';const branchSeen=new Set([...seen,s.id]);
      const children=list.filter(child=>child.parent_id===s.id);
      const expanded=!!f||expandedBranches.has(s.id);
      return `${sessionRowHtml(s)}${children.length?`<button type="button" class="session-branch-toggle" data-branch="${esc(s.id)}" aria-expanded="${expanded}">${svg('<path d="m9 6 6 6-6 6"/>','branch-chevron')}${canvasIcon('agents')} ${children.length} 个嵌套会话</button>${expanded?`<div class="session-branch">${children.map(child=>renderSession(child,branchSeen)).join('')}</div>`:''}`:''}`;
    };
    const closed = !flat && S.closed.has(w.path) && !f;
    // 每个项目先列最近 5 个 (照 Codex, 免得左栏拉得很长); 正打开的那个总在; 搜的时候全列
    const showAll = !!f || flat || wsExpanded.has(w.path);
    const shown = showAll ? roots : roots.slice(0, WS_LIMIT);
    const holding = roots.find((s) => s.id === S.sid || list.some((c) => c.id === S.sid && c.parent_id === s.id));
    if (!showAll && holding && !shown.includes(holding)) shown.push(holding);
    const more = roots.length - shown.length;
    return (flat ? '' : `<div class="ws-row ${closed ? 'closed' : ''}" data-ws="${esc(w.path)}" title="${esc(w.path)}">
        <button type="button" class="ws-toggle" aria-expanded="${!closed}" aria-label="${esc(w.name)}">${svg('<path d="M6 9l6 6 6-6"/>', 'chev')}${svg(ICON.list_dir)}<span class="name">${esc(w.name)}</span></button>
        <button type="button" class="icon ws-menu" data-ws-menu="${esc(w.path)}" title="项目操作（也可以右键项目）" aria-label="${esc(w.name)}的项目操作" aria-haspopup="menu">${svg('<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>')}</button>
        <button type="button" class="icon add" data-new="${esc(w.path)}" title="在这个项目开新会话">＋</button></div>`)
      + `${closed ? '' : list.length ? shown.map(s=>renderSession(s)).join('')
        + (more > 0 ? `<button type="button" class="ws-more" data-ws-more="${esc(w.path)}">再显示 ${more} 个</button>` : showAll && !f && roots.length > WS_LIMIT ? `<button type="button" class="ws-more" data-ws-less="${esc(w.path)}">收起</button>` : '')
        : `<div class="empty-ws">${f ? '没搜到' : '还没有会话'}</div>`}`;
  }).join('');
  $('#archivedCount').textContent = S.state?.archived_sessions?.length || '';
  tree.querySelectorAll('[data-branch]').forEach(button=>button.addEventListener('click',()=>{const id=button.dataset.branch;expandedBranches.has(id)?expandedBranches.delete(id):expandedBranches.add(id);renderSide();}));
  tree.querySelectorAll('[data-ws-more], [data-ws-less]').forEach((button) => button.addEventListener('click', () => {
    if (button.dataset.wsMore) wsExpanded.add(button.dataset.wsMore); else wsExpanded.delete(button.dataset.wsLess);
    renderSide();
  }));
  tree.querySelectorAll('[data-sid]').forEach((el) => el.addEventListener('click', () => { openSession(el.dataset.sid); if (innerWidth <= 860) setSidebar(false); }));
  tree.querySelectorAll('[data-new]').forEach((el) => el.addEventListener('click', (e) => { e.stopPropagation(); newSession(el.dataset.new); }));
  tree.querySelectorAll('.ws-toggle').forEach((el) => el.addEventListener('click', () => {
    const p = el.closest('.ws-row').dataset.ws;
    S.closed.has(p) ? S.closed.delete(p) : S.closed.add(p);
    renderSide();
  }));
  if (focusedBranch) [...tree.querySelectorAll('[data-branch]')].find(button => button.dataset.branch === focusedBranch)?.focus({preventScroll:true});
  renderHer();
}

function expandSessionAncestors(id, view = null) {
  const workspaces = S.state?.workspaces || [];
  const byId = new Map(workspaces.flatMap(workspace => workspace.sessions).map(session => [session.id, session]));
  const seen = new Set([id]);
  let parentId = view?.parent_id || byId.get(id)?.parent_id;
  while (parentId && !seen.has(parentId)) {
    seen.add(parentId);
    expandedBranches.add(parentId);
    parentId = byId.get(parentId)?.parent_id;
  }
  const workspace = workspaces.find(item => item.sessions.some(session => session.id === id));
  if (workspace) S.closed.delete(workspace.path);
}

function renderHer() {  // 她的名片: 在做什么、钱包 (账本 wm/ledger.py); 左栏"绫音"一行是它的缩略 (照 Codex 的"你的 dot")
  const name = herName();
  $('#herSub').textContent = S.state?.her?.name ? `${name}工作台` : '工作台';
  const busy = S.state?.her?.busy || [];
  const doing = busy.find((b) => b.status === 'waiting') ? `等你点头: 「${busy.find((b) => b.status === 'waiting').title}」`
    : busy.length ? `正在做「${busy[0].title}」` : S.dock?.hides ? '在工作台里陪着你' : '在桌面上陪着你';
  $('#herName').textContent = name;
  $('#input').placeholder = `给${name}发消息，/ 看指令`;  // 第六轮: 提示语缩短 (拖文件、粘图片照样能用, 写在"＋"按钮的提示里)
  $('#herStatus').textContent = doing;
  $('#herRow').classList.toggle('busy', !!busy.length);
  $('#herRow').classList.toggle('waiting', busy.some((b) => b.status === 'waiting'));
  const w = S.wallet;
  const t = w?.today || {}, n = w?.now, b = w?.balance || {};
  $('#herRow').title = w ? `余额 ${b.total != null ? money(b.total) : '—'} · 今天花了 ${money(t.cost)} · 点一下看她的卡片` : '点一下看她的卡片';
  const next = n?.next_in_s != null ? `${Math.max(1, Math.round(n.next_in_s / 3600))} 小时后${n.next_tier === 'peak' ? '高峰' : '空闲'}` : '';
  $('#her').classList.toggle('has-desk', !!S.desk);
  $('#herInfo').innerHTML = `<div class="row">${S.desk ? '' : `<img src="${AVATAR}" alt="">`}<div style="min-width:0"><div class="who">${esc(name)}</div>
      <div class="doing">${esc(doing)}</div></div></div>
    ${w ? `<div class="wallet-line"><span>余额 <b>${b.total != null ? money(b.total) : '—'}</b></span><span title="今日花费 ${money(t.cost)}${t.by_who?.agent ? `，工作台 ${money(t.by_who.agent.cost)}` : ''} · 缓存命中 ${t.hit_rate != null ? Math.round(t.hit_rate * 100) + '%' : '—'}${next ? ` · ${next}` : ''}">今天 ${money(t.cost)}${n ? `<span class="peak ${n.tier === 'peak' ? 'hi' : ''}">${n.tier === 'peak' ? '高峰' : '空闲'}</span>` : ''}</span></div>` : ''}`;
}

async function loadWallet() {
  try { S.wallet = await (await fetch('/api/billing')).json(); } catch { S.wallet = null; }
  renderHer();
}

// ---- 会话 ----
// 新会话的联网搜索 (第 5 块, 能力与插件 → 内置能力里设): "默认开 / 默认关"; 没设 (跟着当前会话) 返回 undefined
const webDefault = () => { try { return { on: true, off: false }[localStorage.getItem('wm.workbench.webDefault')]; } catch { return undefined; } };
async function newSession(workspace) {
  // 权限照"新会话默认的那一档" (服务端记着你上次选的); 模型、思考强度接着用现在这个会话的 (或者开会话前在菜单里选的);
  // 联网搜索: 菜单里选过的 > 插件页设的新会话默认 > 现在这个会话的
  const want = { model: S.pick.model ?? S.view?.model, effort: S.pick.effort ?? S.view?.effort, web: S.pick.web ?? webDefault() ?? S.view?.web,
    preset: S.pick.preset };
  let v = await api({ action: 'new', workspace: workspace || S.view?.workspace || undefined, mode: S.pick.mode || '', model: want.model || '' });
  const extra = Object.fromEntries(['effort', 'web', 'preset'].filter((k) => want[k] !== undefined && want[k] !== v[k]).map((k) => [k, want[k]]));
  if (Object.keys(extra).length) v = await api({ action: 'settings', id: v.id, ...extra });
  S.pick = {};
  await loadState();
  await openSession(v.id);
  if (innerWidth <= 860) setSidebar(false);
  $('#input').focus();
}

async function openSession(id) {
  const generation = ++sessionGeneration;
  pendingSend = null;
  if (S.ws) { S.ws.onmessage = null; S.ws.onclose = null; S.ws.close(); S.ws = null; }
  S.sid = id;
  S.typing = '';
  S.thinkLive = '';
  try { localStorage.setItem('wm.workbench.sid', id); } catch { /* 无所谓 */ }
  const view = await getView(id);
  if (!isCurrentSession(id, generation)) return false;
  if (view.unread) {
    await api({action:'unread',id,unread:false});
    if (!isCurrentSession(id, generation)) return false;
    view.unread = false;
    const brief = S.state?.workspaces.flatMap(w => w.sessions).find(s => s.id === id);
    if (brief) brief.unread = false;
  }
  S.view = view;
  S.running = S.view.status !== 'idle';
  expandSessionAncestors(id, view);
  renderSide();
  renderTools();
  renderChat(true);
  connect(id, generation);
  panel?.sessionChanged();
  loadNotebook();
  return true;
}

// ---- 资料本 (wm/agent/notebook.py): 这个会话挂着哪本 —— 回答里的出处 [S3-17] 点了查它, 输入框底下显示它 ----
async function loadNotebook() {
  const sid = S.sid;
  let nb = null;
  if (sid) {
    try {
      const data = await (await fetch(`/api/agent/notebooks?${new URLSearchParams({ session: sid })}`)).json();
      const book = (data.notebooks || []).find((b) => b.id === data.attached);
      nb = book ? { id: book.id, title: book.title } : null;
    } catch { nb = null; }  // 服务端还没有资料本接口 (没重启): 不显示
  }
  if (S.sid !== sid) return;
  S.notebook = nb;
  const chip = $('#nbChip');
  if (!chip) return;
  chip.hidden = !nb;
  chip.innerHTML = nb ? `${svg('<path d="M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z"/><path d="M5 17a3 3 0 0 1 3-3h11"/>')}<span>${esc(nb.title)}</span>` : '';
  chip.title = nb ? `这个会话挂着资料本「${nb.title}」：她回答会先查它、标出处（点开看）` : '';
}

function connect(id, generation = sessionGeneration) {
  if (!isCurrentSession(id, generation)) return;
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/agent/ws?id=${encodeURIComponent(id)}`);
  S.ws = ws;
  ws.onmessage = (e) => {
    if (S.ws === ws && isCurrentSession(id, generation)) onEvent(JSON.parse(e.data));
  };
  ws.onclose = () => {
    if (S.ws !== ws || !isCurrentSession(id, generation)) return;
    S.ws = null;
    if (pendingSend?.sid === id) {
      if (!$('#input').value) $('#input').value = pendingSend.text;
      pendingSend = null; autosize(); updateSend();
      flash('连接中断，已保留草稿；重连后先检查任务是否已经开始。');
    }
    setTimeout(() => { if (!S.ws && isCurrentSession(id, generation)) connect(id, generation); }, 2000);
  };
}

function onEvent(ev) {
  if (ev.type === 'hello') {
    if (ev.session?.id !== S.sid) return;
    S.view = ev.session; S.running = S.view.status !== 'idle'; renderChat(); renderTools(); deskResume(); return;
  }
  if (ev.session && ev.session !== S.sid) return;
  if (ev.type === 'queued') {  // 排队发送: 这一轮做完她再接着做
    S.view.queue = ev.queue || [];
    updateSend();
    if (!ev.queue?.length) flash('队列空了，都发完了');
    return;
  }
  if (ev.type === 'queued_ack') return;
  if (ev.type === 'steered') { S.view.steer = ev.steer || []; updateSend(); return; }  // 补充的要求里还没交给她的几条
  if (ev.type === 'steer_ack') { pendingSteer = null; if (ev.mode === 'queued') flash('她刚好做完这一轮，按新的一件事接着做'); return; }
  if (ev.type === 'accepted') { if (pendingSend?.sid === S.sid) { if (!pendingSend.text.startsWith('/')) studio?.sent(); pendingSend = null; } return; }
  if (ev.type === 'refused') {
    if (ev.steer) {  // 补充的那句没收下: 放回输入框 (你又打了字就不盖掉)
      if (pendingSteer?.sid === S.sid && !$('#input').value.trim()) { $('#input').value = pendingSteer.text; autosize(); updateSend(); }
      pendingSteer = null;
    } else if (pendingSend?.sid === S.sid) { $('#input').value = pendingSend.text; pendingSend = null; autosize(); updateSend(); }
    flash(ev.text); return;
  }
  if (ev.type === 'delta') { S.typing += ev.text; renderTyping(); deskPhase('write'); return; }
  if (ev.type === 'think_delta') { S.thinkLive += ev.text; renderThinkLive(); deskPhase('think', '在想…'); return; }
  if (ev.type === 'stream_reset') { S.typing = ''; S.thinkLive = ''; renderChat(); return; }  // 一步说到一半断流、要重来 (不存盘): 半句话清掉
  if (ev.type === 'step') return;
  const hadStreamingReply = !!S.typing;
  deskEvent(ev);
  S.view.events.push(ev);
  if (['assistant', 'tool', 'done', 'error', 'thinking'].includes(ev.type)) { S.typing = ''; S.thinkLive = ''; }
  if (ev.type === 'user') { S.running = true; if (pendingSend?.sid === S.sid) { studio?.sent(); pendingSend = null; } }
  if (ev.type === 'job_done' && ev.delivered === 'continue') { S.running = true; loadState().catch(() => {}); }  // 作业做完, 她自己接着做
  if (ev.type === 'ask' || ev.type === 'ask_done' || ev.type === 'user') loadState().catch(() => {});
  if (ev.type === 'artifact') studio?.refresh(true);
  if (ev.type === 'artifact' || ev.type === 'tool_result' && ev.change || ev.type === 'note') panel?.homeChanged();  // 新标签页的"推荐"、打开着的变更
  if (ev.type === 'subagent') { loadState().catch(()=>{}); panel?.refreshTasks(); }
  if (ev.type === 'done') {  // 一轮做完: 标题、左边、钱包都刷新
    S.running = false;
    S.view.stats = ev.stats;
    if (ev.run) S.view.run = ev.run;
    const sid = S.sid, generation = sessionGeneration;
    getView(sid).then((v) => { if (isCurrentSession(sid, generation) && S.view?.id === v.id) { S.view = {...v, events: S.view.events}; renderTop(); renderTools(); studio?.refresh(true); } }).catch(() => {});
    loadState().catch(() => {});
    loadWallet();
  }
  renderChat();
  if (ev.type !== 'assistant' || !hadStreamingReply) incomingEntrance(ev.type);
  renderTools();
  notifyEvent(ev);
}

// 工作台内提醒，不申请系统通知权限。后台收到的提醒在返回窗口后显示。
const inBackground = () => document.hidden || !document.hasFocus();
const noticeAllowed = (item) => item.kind === 'ask' ? preferences.notifyApproval : preferences.notifyDone !== 'off';
// 三层验收 (wm/agent/acceptance.py): 她说做完了, 但程序核对一次以后还对不上 —— 不说"执行结束", 说"待核查"
const needsCheck = (run) => run?.state === 'completed' && run?.check?.verdict === 'needs_check';
const runOutcome = (state, check) => (state === 'completed' && check?.verdict === 'needs_check' ? '说做完了，但待核查'
  : {completed: '本轮执行结束', cancelled: '任务已停止', budget_exhausted: '任务达到预算上限', failed: '任务执行失败', interrupted: '任务已中断'}[state] || '本轮执行结束');

function forgetAskNotices(session, askId) {
  for (const [key, item] of notices.queued) {
    if (item.kind === 'ask' && item.session === session && (!askId || item.askId === askId)) notices.queued.delete(key);
  }
  $('#notifications')?.querySelectorAll('.wb-notice[data-kind="ask"]').forEach((node) => {
    if (node.dataset.session === session && (!askId || node.dataset.askId === askId)) node.remove();
  });
}

function notifyEvent(ev) {
  if (ev.type === 'ask_done') { forgetAskNotices(S.sid, ev.id); return; }
  if (ev.type !== 'done' && ev.type !== 'ask') return;
  if (ev.type === 'done') forgetAskNotices(S.sid);
  const kind = ev.type === 'ask' ? 'ask' : 'done';
  const item = { kind, session: S.sid, title: S.view?.title || '当前会话', askId: kind === 'ask' ? ev.id : '',
    label: kind === 'ask' ? (ev.label || '操作') : '', outcome: runOutcome(ev.run?.state, ev.run?.check), background: inBackground() };
  if (!noticeAllowed(item) || (kind === 'done' && preferences.notifyDone === 'background' && !item.background)) return;
  const eventId = kind === 'ask' ? ev.id : (ev.t ?? ev.stats?.turns ?? S.view?.events.filter((event) => event.type === 'user').length);
  const id = `${item.session}:${kind}:${eventId}`;
  if (notices.seen.has(id)) return;
  notices.seen.add(id);
  if (notices.seen.size > 200) notices.seen.delete(notices.seen.values().next().value);
  item.key = `${item.session}:${kind}:${item.askId}`;
  if (item.background) {
    notices.queued.set(item.key, item);
    if (notices.queued.size > 20) notices.queued.delete(notices.queued.keys().next().value);
  } else showNotice(item);
}

function showNotice(item) {
  const root = $('#notifications');
  if (!root || !noticeAllowed(item)) return;
  for (const node of root.querySelectorAll('.wb-notice')) if (node.dataset.key === item.key) node.remove();
  const toast = document.createElement('div');
  toast.className = 'wb-notice';
  toast.dataset.key = item.key;
  toast.dataset.kind = item.kind;
  toast.dataset.session = item.session;
  toast.dataset.askId = item.askId;
  toast.innerHTML = `<button type="button" class="wb-notice-open"><strong>${item.kind === 'ask' ? '等待你的确认' : esc(item.outcome)}</strong><span>${esc(item.title)}${item.kind === 'ask' ? ` · ${esc(item.label)}` : ''}</span><small>${item.kind === 'ask' ? '查看待确认操作' : '查看会话与执行结果'}</small></button><button type="button" class="wb-notice-close" aria-label="关闭提醒" title="关闭提醒">${svg('<path d="M6 6l12 12M18 6L6 18"/>')}</button>`;
  toast.querySelector('.wb-notice-close').addEventListener('click', () => toast.remove());
  toast.querySelector('.wb-notice-open').addEventListener('click', async () => {
    try {
      if (S.settingsOpen && !await closeSettings()) return;
      if (S.sid !== item.session) await openSession(item.session);
      if (innerWidth <= 860) setSidebar(false);
      const ask = [...$('#chat').querySelectorAll('.ask[data-ask-id]')].find((node) => node.dataset.askId === item.askId);
      if (item.kind === 'ask' && ask) { ask.scrollIntoView({ block: 'center', behavior: 'auto' }); ask.focus({ preventScroll: true }); }
      else { $('#chat').scrollTop = $('#chat').scrollHeight; $('#input').focus(); }
      toast.remove();
    } catch (error) { flash(`暂时无法打开会话：${error.message}`); }
  });
  root.append(toast);
  while (root.children.length > 3) root.firstElementChild.remove();
  playNoticeSound();
}

function flushNotices() {
  if (inBackground()) return;
  const queued = [...notices.queued.values()];
  notices.queued.clear();
  queued.forEach(showNotice);
}

function unlockNoticeSound(event) {
  if (!event.isTrusted || !preferences.notificationSound || S.dock?.muted) return;
  const Audio = window.AudioContext || window.webkitAudioContext;
  if (!Audio) return;
  try {
    notices.audio ||= new Audio();
    if (notices.audio.state === 'suspended') notices.audio.resume().catch(() => {});
  } catch { /* 声音不可用时，文字提醒仍然可用。 */ }
}

function playNoticeSound() {
  const ctx = notices.audio;
  if (!preferences.notificationSound || S.dock?.muted || ctx?.state !== 'running') return;
  try {
    const tone = ctx.createOscillator(), gain = ctx.createGain(), now = ctx.currentTime;
    tone.type = 'sine';
    tone.frequency.setValueAtTime(660, now);
    tone.frequency.exponentialRampToValueAtTime(880, now + .12);
    gain.gain.setValueAtTime(.0001, now);
    gain.gain.exponentialRampToValueAtTime(.035, now + .025);
    gain.gain.exponentialRampToValueAtTime(.0001, now + .2);
    tone.connect(gain); gain.connect(ctx.destination);
    tone.start(now); tone.stop(now + .21);
    tone.onended = () => { tone.disconnect(); gain.disconnect(); };
  } catch { /* 不让音频问题影响会话。 */ }
}

// ---- 中间 ----
function md(text) {  // 很简单的 markdown: 代码块、行内代码、粗体、换行 (先转义)
  const parts = String(text).split(/```[a-zA-Z0-9_+-]*\n?/);
  return parts.map((p, i) => (i % 2 ? `<pre>${esc(p.replace(/\n$/, ''))}</pre>`
    : esc(p).replace(/`([^`\n]+)`/g, '<code>$1</code>').replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
      .split(/\n{2,}/).map((x) => (x.trim() ? `<p>${x.replace(/\n/g, '<br>')}</p>` : '')).join(''))).join('');
}

function lineDiff(before, after) {  // 按行: 前后相同的跳过, 中间改了的整段列出来; 返回 {html, add, del}
  const a = before.split('\n'), b = after.split('\n');
  let s = 0;
  while (s < a.length && s < b.length && a[s] === b[s]) s++;
  let ea = a.length - 1, eb = b.length - 1;
  while (ea >= s && eb >= s && a[ea] === b[eb]) { ea--; eb--; }
  const dels = a.slice(s, ea + 1), adds = b.slice(s, eb + 1);
  const rows = [...dels.map((l) => `<div class="l d">- ${esc(l)}</div>`), ...adds.map((l) => `<div class="l a">+ ${esc(l)}</div>`)];
  return { add: adds.length, del: dels.length,
    html: rows.length ? `<div class="diff"><div class="h">第 ${s + 1} 行起</div>${rows.join('')}</div>` : '<div class="note">内容没变</div>' };
}

function herMsg(inner, cls = '') {  // 正文全宽 (照 Codex); 头像和名字在每轮开头那一行 (照 Lexora, 见 herHead)
  return `<div class="her-msg msg-her ${cls}"><div class="body">${inner}</div></div>`;
}
function herHead(meta = '') {  // 一轮开头: 她的小头像、名字, 右边是这一轮的状态 (在想 / 做完了 · 几步)
  return `<div class="msg-head"><img class="msg-av" src="${AVATAR}" alt=""><span class="msg-name">${esc(herName())}</span>${meta ? `<span class="msg-meta">${meta}</span>` : ''}</div>`;
}

// ---- 一轮做完的结果卡片 (照 Codex: "已编辑 17 个文件 +570 −62 · 撤销 · 查看变更"、链接 / 成果"打开方式") ----
const hostOf = (url) => { try { return new URL(url).hostname; } catch { return url; } };
const fileBase = (path) => String(path || '').replace(/\\/g, '/').split('/').filter(Boolean).pop() || path;
const OPEN_CHEV = svg('<path d="m7 10 5 5 5-5"/>', 'chev');
// 一轮里提到的网址 (第六轮, 学 ChatGPT 的"来源"): 以前每个网址一张大卡片; 现在正文里是站点名小胶囊, 一轮末尾一个"来源 N"按钮, 点了在右侧面板列出来 ("打开方式"在那里)
function sourcesRow(urls) {
  if (!urls.length) return '';
  const marks = urls.slice(0, 3).map((url) => `<i>${esc((hostOf(url).replace(/^www\./, '')[0] || '·').toUpperCase())}</i>`).join('');
  return `<div class="wb-sources"><button type="button" class="wb-src-btn" data-sources="${esc(JSON.stringify(urls))}" title="在右侧列出这一轮提到的 ${urls.length} 个网址"><span class="wb-src-marks">${marks}</span>来源 · ${urls.length}</button></div>`;
}
function turnCards(events, v) {
  const changes = collectChanges(events, v.workspace);
  const reverted = new Set((v.events || []).filter((e) => e.type === 'note' && Array.isArray(e.reverted)).flatMap((e) => e.reverted));  // 撤销接口记下的
  const artifacts = [];
  const norm = (path) => String(path || '').replace(/\\/g, '/').toLowerCase();
  const changed = new Set(changes.map((f) => norm(f.path)));  // 改过的文件已经在"已编辑"卡片里, 成果卡片只列别的 (命令生成的、图片…)
  for (const e of events) if (e.type === 'artifact' && e.artifact?.path && !artifacts.some((a) => a.path === e.artifact.path) && !changed.has(norm(e.artifact.path))) artifacts.push(e.artifact);
  let out = '';
  if (changes.length) {
    const add = changes.reduce((n, f) => n + f.add, 0), del = changes.reduce((n, f) => n + f.del, 0);
    const calls = changes.flatMap((f) => f.calls);
    const row = (f) => `<button type="button" class="wb-edit-row" data-diff="${esc(JSON.stringify(f.calls))}" data-diff-title="${esc(fileBase(f.name))}" title="看 ${esc(f.name)} 改了什么">
        <span class="p">${esc(f.name)}</span>${f.created ? '<span class="wb-tag">新建</span>' : ''}<span class="wb-add">+${f.add}</span><span class="wb-del">−${f.del}</span></button>`;
    const undone = calls.every((id) => reverted.has(id));
    out += `<div class="wb-card wb-edits ${undone ? 'undone' : ''}" data-calls="${esc(JSON.stringify(calls))}"><header><span class="wb-card-title">已编辑 ${changes.length} 个文件</span><span class="wb-add">+${add}</span><span class="wb-del">−${del}</span><span class="grow"></span>
        <button type="button" class="wb-card-btn" data-undo ${undone ? 'disabled' : ''} title="${undone ? '已经撤销了' : '把这一轮改的文件放回改之前（之后又改过的不动）'}">撤销${svg('<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>')}</button>
        <button type="button" class="wb-card-btn" data-view-changes>查看变更</button></header>
      <div class="wb-edits-list">${changes.slice(0, 5).map(row).join('')}${changes.length > 5 ? `<details class="wb-edits-more"><summary>再显示 ${changes.length - 5} 个文件${OPEN_CHEV}</summary>${changes.slice(5).map(row).join('')}</details>` : ''}</div></div>`;
  }
  for (const a of artifacts.slice(0, 4)) {
    const rel = relativePath(a.path, v.workspace);
    out += `<div class="wb-card wb-link wb-artifact"><button type="button" class="wb-link-main" data-open-file="${esc(rel)}" title="在右侧打开">
        <span class="wb-link-icon">${svg(ICON.read_file)}</span><span class="wb-link-copy"><strong>${esc(a.label || fileBase(a.path))}</strong><small>成果 · ${esc(rel)}</small></span></button>
      <button type="button" class="wb-open-with" data-open-menu="file" data-target="${esc(rel)}" aria-haspopup="menu">打开方式${OPEN_CHEV}</button></div>`;
  }
  return out;
}

function renderChat(jump = false) {
  const chat = $('#chat');
  const v = S.view;
  if (chat.dataset.session) chat.querySelectorAll('details[data-id]').forEach((detail) => {
    detailStates.set(`${chat.dataset.session}:${detail.dataset.id}`, detail.open);
  });
  chat.dataset.session = v?.id || '';
  const empty = !v || (!(v.events || []).length && !S.typing && !S.running);
  $('#mainView').classList.toggle('is-empty', empty);
  renderEmptyExtras(empty);
  renderTop();
  if (!v) {
    chat.innerHTML = welcome('从一个想法、一段代码，或一件待办开始。');
    welcomeEntrance($('#mainView'), 'fresh');
    return;
  }
  const evs = v.events || [];
  if (!evs.length && !S.typing && !S.running) {
    chat.innerHTML = welcome(`在「${esc(S.state?.workspaces?.find((w) => w.path === v.workspace)?.name || v.workspace.split(/[\\/]/).pop())}」里，一起把事情做好。`);
    welcomeEntrance($('#mainView'), v.id);
    return;
  }
  welcomeScene = '';
  const tools = new Map(), asks = new Map(), children = new Map();
  for (const e of evs) {
    if (e.type === 'tool') tools.set(e.id, { ...e });
    if (e.type === 'tool_result' && tools.has(e.id)) Object.assign(tools.get(e.id), { result: e });
    if (e.type === 'ask') asks.set(e.id, { ...e });
    if (e.type === 'ask_done' && asks.has(e.id)) asks.get(e.id).how = e.how;
    if (e.type === 'subagent' && e.child?.id) children.set(e.child.id,e);
  }
  const html = [];
  let steps = [], needHead = false, headMeta = '';
  const head = () => { if (needHead) { html.push(herHead(headMeta)); needHead = false; } };  // 这一轮她第一次出声前放头像和名字
  const flush = () => { if (steps.length) { head(); html.push(actHtml(steps)); steps = []; } };
  // 一轮收尾 (第六轮, 学 ChatGPT): 顺序固定为 过程摘要 → 正文 → 改动卡 → 来源 → 操作行。回复的按钮先空着 (ACTS),
  // 这一轮结束时: 最后一条回复后面没别的了就把改动卡、来源、按钮都放进它里面 (悬停整条才出按钮); 后面又跑了工具的, 卡片单独放在最后
  const ACTS = '\u0000acts\u0000';
  let pending = null, turnLinks = [];
  const settle = (cards = '') => {
    const foot = sourcesRow(turnLinks);
    if (pending && pending.idx === html.length - 1) html[pending.idx] = html[pending.idx].replace(ACTS, cards + foot + messageActions(pending.e));
    else {
      if (pending) html[pending.idx] = html[pending.idx].replace(ACTS, messageActions(pending.e));
      if (cards || foot) html.push(`<div class="turn-foot">${cards}${foot}</div>`);
    }
    pending = null; turnLinks = [];
  };
  const ranges = turnRanges(evs);
  const turnAt = new Map(ranges.filter((r) => r.end >= 0).map((r) => [r.end, r]));  // done 的位置 -> 这一轮从哪开始
  // 一轮开头那一行右边: 做完了几秒 · 几步 · 花了多少 (第六轮学 Linear: 以前另起一行的"本轮执行结束 · 本会话 N 轮 · N 步"并到这里, 悬停看全会话的)
  let lastStats = { steps: 0, cost: 0 };  // done 带的 stats 是全会话累计的, 这一轮的 = 减去上一轮的
  const turnMeta = new Map(ranges.map((r) => {
    const done = r.end >= 0 ? evs[r.end] : null, began = Number(evs[r.start]?.t);
    if (!done) return [r.start, S.running && r === ranges.at(-1) ? '<span class="spin"></span>在做' : ''];
    const took = Number(done.t) > began ? ` · ${secs(Number(done.t) - began)}` : '';
    const word = needsCheck(done.run) ? '待核查' : ({ completed: '已完成', cancelled: '已停止', budget_exhausted: '到预算了', failed: '没成', interrupted: '中断了' })[done.run?.state] || '已完成';
    const st = done.stats;
    let extra = '', tip = runOutcome(done.run?.state, done.run?.check);
    if (st) {
      const steps = lastStats ? st.steps - lastStats.steps : 0, cost = lastStats ? st.cost - lastStats.cost : 0;
      extra = (steps > 0 ? ` · ${steps} 步` : '') + (cost >= 0.005 ? ` · ${money(cost)}` : '');
      tip += ` · 本会话共 ${st.turns} 轮 · ${st.steps} 步 · ${money(st.cost)}`;
      lastStats = st;
    } else lastStats = null;  // 老会话没记 stats: 下一轮算不出这一轮的, 就不写
    return [r.start, `<span class="mm" title="${esc(tip)}"><span class="${needsCheck(done.run) || ['failed', 'interrupted'].includes(done.run?.state) ? 'warn' : ''}">${word}</span>${took}${extra}</span>`];
  }));
  for (let index = 0; index < evs.length; index++) {
    const e = evs[index];
    if (e.type === 'tool') {
      const ask = asks.get(e.id);
      if (ask && !ask.how) { flush(); head(); html.push(askCard(ask)); }
      steps.push({ t: tools.get(e.id), ask });
      continue;
    }
    if (e.type === 'thinking') { steps.push({ think: e }); continue; }  // 想了什么: 和这一段的工具收在一起
    if (['user', 'assistant', 'note', 'error', 'done', 'job_done', 'check'].includes(e.type)) flush();
    if (e.type === 'user' && pending && !e.steer) settle();  // 上一轮没有 done (中断了 / 老会话): 按钮照样补上
    if (e.type === 'user') {
      // 只发了附件没打字时, 对话里直接把图片显示出来 (缩略图, 点开看原图)
      const pics = (e.materials || []).filter(m => m.kind === 'file' &&
        /\.(jpe?g|png|gif|webp|bmp)$/i.test(String(m.name || m.file || ''))).slice(0, 4);
      const thumbs = pics.length ? `<div class="msg-attachments">${pics.map(m => `<img class="msg-attach" src="/api/agent/context/image?id=${encodeURIComponent(v.id)}&item=${encodeURIComponent(m.id)}" alt="${esc(m.name || '图片')}" loading="lazy">`).join('')}</div>` : '';
      const steer = e.steer ? '<div class="msg-steer" title="她做事的中途补充的：手头那一步做完就交给她，后面照它来">中途补充</div>' : '';
      html.push(`<div class="user-message${e.steer ? ' steer' : ''}">${steer}<div class="msg-user">${esc(e.text)}</div>${thumbs}${messageActions(e)}</div>`);
      needHead = true;
      headMeta = e.steer ? '' : turnMeta.get(index) || '';
    }
    else if (e.type === 'assistant') {
      head();
      if (pending) html[pending.idx] = html[pending.idx].replace(ACTS, messageActions(pending.e));  // 同一轮前面那条回复: 按钮照旧 (悬停才出)
      for (const url of extractLinks(e.text, 8)) if (!turnLinks.includes(url)) turnLinks.push(url);
      html.push(herMsg(`<div class="message-text">${renderMarkdown(e.text, { cite: true })}</div>${ACTS}`, 'response'));
      pending = { idx: html.length - 1, e };
    }
    else if (e.type === 'subagent_result') html.push(`<details class="nested-result" data-id="child:${esc(e.child_id||e.id)}"><summary>${canvasIcon('agents')} 子任务结果 · ${esc(e.title||'嵌套任务')}</summary><div>${md(e.text||e.result||'')}</div><button type="button" data-child-session="${esc(e.child_id||e.id)}">查看子会话 ↗</button></details>`);
    // 子任务转去等你点头 (2026-11-07 起服务端也告诉父会话): 卡上写"等你点头"、标橙、自己展开 (换一个 data-id, 收起来不影响平时那张), 按钮直接去子会话批
    else if (e.type === 'subagent' && children.get(e.child?.id)===e) {flush();const wait=e.child.status==='waiting';html.push(`<details class="nested-result${wait?' wait':''}" data-id="child:${esc(e.child.id)}${wait?':wait':''}"${wait?' data-open-default':''}><summary>${canvasIcon('agents')} ${esc(e.child.title||'子任务')} <span>${esc(({running:'运行中',queued:'排队中',preparing:'准备中',completed:'已完成',done:'已完成',failed:'失败',cancelled:'已停止',cancelling:'正在停下',interrupted:'中断了',budget_exhausted:'到预算了',waiting:'等你点头'})[e.child.status]||e.child.status)}</span></summary><div>${md(e.child.result||e.child.error||(wait?'子任务有一步要你点头：到子会话里批准或拒绝，它才接着做。':'子任务正在独立会话中处理。'))}</div><button type="button" data-child-session="${esc(e.child.id)}">${wait?'去子会话点头 ↗':'查看子会话 ↗'}</button></details>`);}
    // 较早的对话写成了检查点 (存盘): 可展开的卡片, 里面是检查点正文; 前后那两条 note ("…写成一份检查点…""…约 X → Y 字") 照常
    else if (e.type === 'compact') { flush(); html.push(`<details class="nested-result ckpt" data-id="ckpt:${esc(e.message_id || '')}"><summary>${canvasIcon('document')} 检查点 · 较早的 ${Number(e.archived) || 0} 条写成了摘要 <span>${esc([CKPT_WHY[e.why], e.before_chars ? `约 ${fmtChars(e.before_chars)} → ${fmtChars(e.after_chars)} 字` : ''].filter(Boolean).join(' · '))}</span></summary><div class="message-text">${renderMarkdown(e.text || '')}</div></details>`); }  // 正文是模型写的, 常带标题、列表: 用回复那套渲染 (先转义)
    else if (e.type === 'note') html.push(`<div class="note">${esc(e.text)}</div>`);
    // 她起的后台作业做完了 (wm/agent/jobwatch.py): 交给正在做的这一轮 / 她接着看结果 / 那一轮停了只记一笔
    else if (e.type === 'job_done') html.push(`<div class="note job-done">${esc(e.text)}${({ steer: ' · 交给她了', continue: ' · 她接着看结果', note: ` · ${esc(e.why || '')}，没有接着做` })[e.delivered] || ''}</div>`);
    else if (e.type === 'error') html.push(`<div class="error">${esc(e.text)}</div>`);
    else if (e.type === 'check') html.push(`<div class="note check-note${e.stage === 'final' ? ' warn' : ''}">${e.stage === 'final' ? '程序核对没过，待核查：' : '程序核对：她说做完了，但'}${esc((e.check?.reasons || []).join('；'))}${e.stage === 'final' ? '' : '。让她再核对一次'}</div>`);
    else if (e.type === 'done') {
      const turn = turnAt.get(index);
      settle(turn ? turnCards(evs.slice(turn.start, index + 1), v) : '');
    }
  }
  flush();
  if (pending && S.running) html[pending.idx] = html[pending.idx].replace(ACTS, messageActions(pending.e));  // 还在做的这一轮: 先放按钮, 来源等做完再列
  else if (pending) settle();
  if ((S.thinkLive && S.running) || S.typing || S.running) head();
  if (S.thinkLive && S.running) html.push(thinkLiveHtml());  // 正在想 (边想边看)
  if (S.typing) html.push(herMsg(`<div class="message-text">${renderMarkdown(S.typing)}</div>`, 'typing'));
  else if (S.running && !S.thinkLive && !Object.values(Object.fromEntries(asks)).some((a) => !a.how)) {
    const last = evs[evs.length - 1];
    if (!(last?.type === 'tool' && !tools.get(last.id)?.result)) html.push(herMsg('<span class="thinking"><i></i><i></i><i></i></span>'));
  }
  const atBottom = jump || chat.scrollHeight - chat.scrollTop - chat.clientHeight < 120;
  chat.innerHTML = `<div class="col">${html.join('')}</div>`;
  [...chat.querySelectorAll('.her-msg.response')].at(-1)?.classList.add('latest');  // 最后一条回复的按钮一直露着, 别的悬停才出来
  chat.querySelectorAll('details[data-id]').forEach((detail) => {
    const key = `${v.id}:${detail.dataset.id}`;
    detail.open = detailStates.has(key) ? detailStates.get(key) : detail.hasAttribute('data-open-default') || preferences.expandToolDetails;
    detailStates.set(key, detail.open);
    detail.addEventListener('toggle', () => { if (detail.isConnected) detailStates.set(key, detail.open); });
  });
  while (detailStates.size > 2000) detailStates.delete(detailStates.keys().next().value);
  chat.querySelectorAll('[data-answer]').forEach((b) => b.addEventListener('click', () => {
    S.ws?.send(JSON.stringify({ type: 'answer', id: b.dataset.id, how: b.dataset.answer }));
    b.closest('.btns').innerHTML = '<span class="note">好的…</span>';
  }));
  chat.querySelectorAll('[data-child-session]').forEach(button=>button.addEventListener('click',()=>openSession(button.dataset.childSession)));
  if (atBottom) chat.scrollTop = chat.scrollHeight;
}

function welcome(sub) {  // 空会话照 Codex / ChatGPT: 问候在上, 输入框紧跟着; 建议和"接着上次"在输入框下面 (renderEmptyExtras)
  return `<div class="col"><div class="welcome wb-welcome"><img class="wb-welcome-av" src="/pet/brand/avatar.png" alt="">
    <h2>今天想和${esc(herName())}一起做点什么？</h2><p>${sub}</p></div></div>`;
}
// 空会话输入框下面 (第六轮): 一排建议小胶囊 (学 ChatGPT, 点了是填进输入框、不直接发) + "接着上次" (学 Notion 的最近访问: 这个项目里最近的会话和看过的文件)
const CHAT_GLYPH = '<path d="M5 5.5h14v10H10l-5 4z"/>';
function renderEmptyExtras(on) {
  const box = $('#emptyExtras');
  if (!box) return;
  box.hidden = !on;
  if (!on) { box.innerHTML = ''; return; }
  const ws = S.view?.workspace || S.state?.workspaces?.[0]?.path || '';
  const sessions = (S.state?.workspaces?.find((w) => w.path === ws)?.sessions || [])
    .filter((s) => s.id !== S.sid && s.title && s.title !== '新会话' && !s.parent_id)
    .sort((a, b) => (b.updated || 0) - (a.updated || 0)).slice(0, 3);
  const files = readJSON('wm.panel.files.' + ws, []).filter((r) => r?.path).slice(0, Math.max(1, 4 - sessions.length));
  const recent = [
    ...sessions.map((s) => `<button type="button" class="wb-recent-item" data-sid-open="${esc(s.id)}" title="${esc(s.title)}">${svg(CHAT_GLYPH)}<span>${esc(s.title)}</span><em>会话</em><time>${esc(ago(s.updated))}</time></button>`),
    ...files.map((f) => `<button type="button" class="wb-recent-item" data-file-open="${esc(f.path)}" title="${esc(f.path)}">${svg(ICON.read_file)}<span>${esc(fileBase(f.path))}</span><em>文件</em><time>${f.at ? esc(ago(f.at / 1000)) : ''}</time></button>`),
  ];
  box.innerHTML = `<div class="wb-starters">${STARTERS.map(([t, say, icon, desc]) => `<button type="button" data-say="${esc(say)}" title="${esc(desc)}：${esc(say)}">${svg(ICON[icon])}<span>${esc(t)}</span></button>`).join('')}</div>`
    + (recent.length ? `<div class="wb-recent-block"><div class="wb-recent-head">接着上次</div>${recent.join('')}</div>` : '');
}

function renderTyping() {
  const el = $('.her-msg.typing .body');
  if (!el) { renderChat(); return; }
  el.innerHTML = `<div class="message-text">${renderMarkdown(S.typing)}</div>`;
  const chat = $('#chat');
  if (chat.scrollHeight - chat.scrollTop - chat.clientHeight < 200) chat.scrollTop = chat.scrollHeight;
}

// 一段连着的工具和思考收成一行灰字 (学 Codex: "已读取 10 个文件, 已搜索 8 次  +75 −0 ›"), 点开是一条条的小行,
// 每条再点开看命令、改动、输出 (2026-10-28 用户: 把工作台那一片"跑命令""读文件"的方框行改成 Codex 那样).
const DONE_WORD = { read_file: ['已读取', '个文件'], list_dir: ['已查看', '个目录'], glob: ['已查找', '次文件'], grep: ['已搜索', '次'],
  write_file: ['已写入', '个文件'], edit_file: ['已编辑', '个文件'], run_command: ['已运行', '个命令'], her_skill: ['用了', '次本事'],
  web_search: ['已上网搜索', '次'], web_fetch: ['已阅读', '个网页'] };
const NOW_WORD = { read_file: '正在读取', list_dir: '正在查看', glob: '正在查找', grep: '正在搜索', write_file: '正在写入', edit_file: '正在编辑',
  run_command: '正在运行', her_skill: '正在', web_search: '正在上网搜索', web_fetch: '正在阅读' };
const CHEV = '<svg class="chev" viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6l6 6-6 6"/></svg>';
const base = (p) => String(p || '').split('  (')[0].split(/[\\/]/).filter(Boolean).pop() || p;
const secs = (n) => (n >= 60 ? `${Math.floor(n / 60)} 分 ${Math.round(n % 60)} 秒` : `${Math.max(1, Math.round(n))} 秒`);

/** 一条工具在行里怎么说: 读取 core.py / 搜索 "watch_bvid" / 运行 (命令的说明) / 阅读 某网址 */
function stepText(t) {
  const arg = String(t.args || '');
  const what = { read_file: base(arg), list_dir: base(arg), glob: arg.split('  (')[1]?.replace(/\)$/, '') || arg,
    grep: arg.split('  (')[1]?.replace(/\)$/, '') || arg, write_file: base(arg), edit_file: base(arg), run_command: t.desc || arg,
    web_search: arg, web_fetch: arg, her_skill: arg }[t.name] ?? arg;
  return what;
}

function actItem({ t, ask, think }) {
  if (think) {
    return `<details class="act-item" data-id="k-${esc(String(think.t))}"><summary>${CHEV}<span class="t">思考了 ${secs(think.secs || 0)}</span></summary>
      <div class="act-body think-text">${esc(think.text)}</div></details>`;
  }
  const r = t.result;
  const st = r ? (r.ok ? 'ok' : (t.state === 'denied' ? 'denied' : 'err')) : (ask && !ask.how ? 'asking' : t.state);
  const d = r?.change ? lineDiff(r.change.before, r.change.after) : null;
  const verb = r ? (t.name === 'write_file' && r.change && !r.change.before ? '已创建' : DONE_WORD[t.name]?.[0] || t.label) : NOW_WORD[t.name] || t.label;
  const how = ask?.how ? `<div class="note" style="text-align:left">${{ once: '你允许了一次', turn: '你允许了这一轮', always: '你允许了, 以后这类命令不再问', deny: '你拒绝了', stopped: '停掉了' }[ask.how] || ''}</div>` : '';
  const cmd = t.name === 'run_command' ? `<pre class="cmd">${esc(t.args)}</pre>` : '';
  const body = r ? `${how}${cmd}${d ? d.html : ''}<pre>${esc(r.text)}</pre>` : t.why && t.state === 'denied' ? `${cmd}<pre>${esc(t.why)}</pre>` : how || cmd || '<div class="note">在做…</div>';
  const mark = st === 'running' || st === 'asking' ? '<span class="spin"></span>' : st === 'ok' ? '' : `<span class="bad">${st === 'denied' ? '不让做' : '没成'}</span>`;
  return `<details class="act-item ${st}" data-id="${esc(t.id)}"><summary>${CHEV}<span class="ic">${svg(ICON[t.name] || ICON.run_command)}</span>
    <span class="t"><b>${esc(verb)}</b> <span class="arg" title="${esc(t.args)}">${esc(stepText(t))}</span></span>
    ${d ? `<span class="cnt"><span class="a">+${d.add}</span> <span class="d">−${d.del}</span></span>` : ''}${mark}</summary><div class="act-body">${body}</div></details>`;
}

/** 一段: 一行灰字说做了什么 (正在做时说正在做的那一步), 点开是每一条 */
function actHtml(steps) {
  const tools = steps.filter((s) => s.t), thinks = steps.filter((s) => s.think);
  const running = tools.find((s) => !s.t.result);
  const order = [], count = {};
  let add = 0, del = 0, bad = 0;
  for (const { t } of tools) {
    if (!(t.name in count)) order.push(t.name);
    count[t.name] = (count[t.name] || 0) + 1;
    if (t.result?.change) { const d = lineDiff(t.result.change.before, t.result.change.after); add += d.add; del += d.del; }
    if (t.result && !t.result.ok) bad += 1;
  }
  const parts = thinks.length ? [`已思考 ${secs(thinks.reduce((n, s) => n + (s.think.secs || 0), 0))}`] : [];
  for (const k of order) {
    const one = tools.find((s) => s.t.name === k).t;
    const [verb, unit] = DONE_WORD[k] || [one.label, '次'];
    // 只有一个的写出是哪个 (已创建 说明.md、已运行 跑一遍单测), 好几个的写个数
    parts.push(count[k] === 1 && ['write_file', 'edit_file', 'run_command', 'web_fetch', 'her_skill'].includes(k)
      ? `${k === 'write_file' && one.result?.change && !one.result.change.before ? '已创建' : verb} ${stepText(one)}` : `${verb} ${count[k]} ${unit}`);
  }
  const head = running ? `<span class="spin"></span><span class="now">${esc(NOW_WORD[running.t.name] || running.t.label)} ${esc(stepText(running.t))}…</span>`
    : `<span class="sum">${esc(parts.join(', '))}</span>`;
  const extra = `${add || del ? `<span class="cnt"><span class="a">+${add}</span> <span class="d">−${del}</span></span>` : ''}${bad ? `<span class="bad">${bad} 个没成</span>` : ''}`;
  const id = steps[0].t ? steps[0].t.id : `k${steps[0].think.t}`;
  return `<details class="act" data-id="a-${esc(id)}"><summary>${head}${extra}${CHEV}</summary>
    <div class="act-list">${steps.map(actItem).join('')}</div></details>`;
}

/** 正在想: 一行"正在思考…", 下面灰字是想到哪了 (只露最后几百字) */
function thinkLiveHtml() {
  return `<div class="act thinking-live"><div class="now-row"><span class="spin"></span><span class="now">正在思考…</span></div>
    <div class="think-text live">${esc(S.thinkLive.slice(-700))}</div></div>`;
}
function renderThinkLive() {
  const el = $('.thinking-live .think-text');
  if (!el) { renderChat(); return; }
  el.textContent = S.thinkLive.slice(-700);
  const chat = $('#chat');
  if (chat.scrollHeight - chat.scrollTop - chat.clientHeight < 200) chat.scrollTop = chat.scrollHeight;
}

function askCard(a) {
  return `<div class="ask ${a.risk === 'power' ? 'power' : ''}" data-ask-id="${esc(a.id)}" tabindex="-1"><div class="h">${svg(MODE_ICON.workspace, 'ask-ic')}<span>${esc(herName())}想${esc(a.label)}${a.risk === 'power' ? ' · 高风险' : ''}</span></div>
    <pre>${esc(a.text)}</pre><div class="why">${esc(a.why)} · 15 秒没点会在 QQ / 微信上问你, 那边回"好"也算</div>
    <div class="btns"><button type="button" class="btn primary" data-answer="once" data-id="${esc(a.id)}">允许一次</button>
      ${a.turn_ok ? `<button type="button" class="btn" data-answer="turn" data-id="${esc(a.id)}">这一轮都允许</button>` : ''}
      ${a.always ? `<button type="button" class="btn" data-answer="always" data-id="${esc(a.id)}"
        title="记进名单: 以后以「${esc(a.always)}」开头的单条命令不再问 (在权限菜单里能移出)">以后都允许 <code>${esc(a.always)} …</code></button>` : ''}
      <button type="button" class="btn danger" data-answer="deny" data-id="${esc(a.id)}">拒绝</button></div></div>`;
}

const sidebarExpanded = () => (innerWidth <= 860 ? document.querySelector('.app').classList.contains('sidebar-open') : !document.querySelector('.app').classList.contains('sidebar-collapsed'));
function renderTop() {  // 照 Codex: 侧栏收起时左上角才有展开按钮; 标题 + 工作区; 右边 花费、⋯、开关右侧面板
  const v = S.view;
  const expanded = sidebarExpanded(), panelOn = !!panel?.isOpen();
  $('#top').innerHTML = (expanded ? '' : `<button type="button" class="icon sidebar-toggle" id="sidebarToggle" aria-label="展开会话栏" aria-controls="sidebar" aria-expanded="false" title="会话栏 (Ctrl B)">${svg('<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M9 4v16"/>')}</button>`)
    + (v ? `<div class="ttl"><h1 title="${esc(v.title)}">${esc(v.title)}</h1><div class="path" title="${esc(v.workspace)}">${svg(ICON.list_dir)}<span>${esc(S.state?.workspaces?.find((w) => w.path === v.workspace)?.name || String(v.workspace).split(/[\\/]/).pop())}</span></div></div><span class="grow"></span>
    ${v.parent_id ? `<button type="button" class="session-parent-link" data-parent-session="${esc(v.parent_id)}">↑ 主会话</button>` : ''}
    ${v.preset !== 'standard' ? `<span class="chip">${esc(S.state?.presets?.[v.preset]||v.preset)}</span>` : ''}
    <span class="session-cost" title="本会话花费">${money(v.stats?.cost)}</span>
    <button type="button" class="icon" id="sessionMore" title="会话操作" aria-label="当前会话的更多操作" aria-haspopup="menu" aria-expanded="false">${svg('<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>')}</button>` : `<div class="ttl"><h1>新聊天</h1></div><span class="grow"></span>`)
    + (S.pageStale ? `<button type="button" class="wb-update" id="pageReload" title="工作台的页面文件更新了，刷新后用新的（会话在服务端，输入框里没发的字会带过去）">有新版本 · 刷新</button>` : '')
    + `<button type="button" class="icon wb-panel-toggle ${panelOn ? 'on' : ''}" id="panelToggle" title="${panelOn ? '收起' : '打开'}右侧面板 (Ctrl J)" aria-label="${panelOn ? '收起' : '打开'}右侧面板" aria-pressed="${panelOn}">${svg('<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M15 4v16"/>')}</button>`;
  $('#sidebarToggle')?.addEventListener('click', () => setSidebar(true));
  $('#pageReload')?.addEventListener('click', () => {  // 输入框里没发的字带过去 (sessionStorage 刷新后还在)
    try { sessionStorage.setItem('wm.workbench.reloadDraft', JSON.stringify({ sid: S.sid, text: $('#input').value })); } catch { /* 存不下就算了 */ }
    location.reload();
  });
  $('#panelToggle').addEventListener('click', () => panel?.toggle());
  $('#sessionMore')?.addEventListener('click', event => navigation?.openSessionMenu(S.sid,event.currentTarget));
  $('[data-parent-session]')?.addEventListener('click',event=>openSession(event.currentTarget.dataset.parentSession));
}

function setSidebar(open) {
  const app = document.querySelector('.app');
  if (innerWidth <= 860) app.classList.toggle('sidebar-open', open);
  else {
    app.classList.toggle('sidebar-collapsed', !open);
    try { localStorage.setItem('wm.workbench.sidebar', open ? '1' : '0'); } catch { /* optional preference */ }
  }
  $('#sideScrim').hidden = !(innerWidth <= 860 && open);
  renderTop();
}

function updateSend() {
  const send = $('#send');
  const sendKey = preferences.sendKey === 'ctrlEnter' ? 'Ctrl + Enter' : 'Enter';
  const typed = $('#input').value.trim();
  const busy = !!S.running;
  const attached = (studio?.contextIds() || []).length > 0;
  send.innerHTML = busy && !typed && !attached ? STOP : SEND;
  const otherKey = preferences.sendKey === 'ctrlEnter' ? 'Enter' : 'Ctrl + Enter';
  const steering = S.state?.steer && typed && !attached && !typed.startsWith('/');  // 带附件的、指令先排队 (这一轮中途不收材料); 旧服务端不认补充也排队
  send.title = busy ? (typed || attached ? (steering ? '加到这一轮 (她做完手头这一步就看到，后面照它来)' : '排队发送 (她做完这一轮再发)') : '停止生成')
    : `发送 · ${preferences.sendKey === 'ctrlEnter' ? 'Enter' : 'Shift + Enter'} 换行 (${sendKey})`;
  send.setAttribute('aria-label', busy ? (typed || attached ? (steering ? '加到这一轮' : '排队发送') : '停止生成') : '发送消息');
  send.classList.toggle('stop', busy && !typed && !attached);
  send.disabled = !busy && !typed && !attached;
  const queue = queueHint();
  // 第六轮 (学 Codex): 底栏平时不挂"Enter 发送 · Shift + Enter 换行" (写在发送键的提示里); 她在做事时才说怎么插话、排队
  $('#composerHint').innerHTML = busy
    ? (S.state?.steer ? `${sendKey} 加到这一轮<span class="hint-extra"> · ${otherKey} 做完再发 · 空着按停止</span>${queue}`
      : `${sendKey} 排队发送<span class="hint-extra"> · 空着按停止</span>${queue}`)
    : queue.replace(/^<span class="hint-extra"> · /, '<span class="hint-extra">');
}

function queueHint() {
  const n = S.view?.queue?.length || 0, m = S.view?.steer?.length || 0;
  return (m ? `<span class="hint-extra"> · 待交给她 ${m} 条</span>` : '') + (n ? `<span class="hint-extra"> · 排队 ${n} 条</span>` : '');
}

// ---- 底下 ----
function renderTools() {
  const st = S.state, v = S.view;
  if (!st) return;
  studio?.refresh();
  const c = cur();
  const mb = $('#modeBtn');  // 权限 (学 ChatGPT 的"应如何批准"): 图标 + 这一档的名字
  mb.innerHTML = `${svg(MODE_ICON[c.mode] || MODE_ICON.workspace)}<span>${esc(st.modes[c.mode] || c.mode)}</span>${svg(CHEV_DOWN, 'chev')}`;
  mb.classList.toggle('warn', c.mode === 'unrestricted');  // 第六轮: 只有"完全授权"用提醒色 (以前"自动执行"也是橙红, 像报错)
  // 模型 + 思考强度 (学 ChatGPT 的 "GPT-6 Astra Ultra"), 计划模式也标在这里
  const hasModels = !!st.models?.length;
  $('#modelBtn').innerHTML = hasModels ? `${c.preset === 'plan' ? '<span class="plan">计划</span>' : ''}<span class="m">${esc(modelName(c.model))}</span>`
    + `<span class="eff ${c.effort === 'off' ? 'off' : ''}">${esc(st.efforts?.[c.effort] || c.effort)}</span>${svg(CHEV_DOWN, 'chev')}`
    : `${svg(RAIL_ICON.settings)}<span class="m">配置模型</span>`;
  $('#modelBtn').title = hasModels ? '模型、做法和思考强度' : '打开模型设置，连接一个模型后开始对话';
  $('#modelBtn').setAttribute('aria-haspopup', hasModels ? 'menu' : 'false');
  const web = $('#webTog');  // 智能搜索 (学 DeepSeek 网页版的开关)
  web.innerHTML = `${svg(TOG_ICON.web)}<span>联网搜索</span>`;
  web.classList.toggle('on', c.web !== false);
  web.setAttribute('aria-pressed', c.web !== false ? 'true' : 'false');
  web.title = c.web !== false ? '联网搜索开着：她能上网搜索、读网页（只读公网）。点一下关掉' : '联网搜索关着：她只用工作区里的资料。点一下打开';
  if (!$('#menu').hidden) renderMenu();
  const ws = v?.workspace || st.workspaces[0]?.path || '';
  const name = st.workspaces.find(item => item.path === ws)?.name || ws.split(/[\\/]/).pop() || '选择工作区';
  $('#wsName').innerHTML = `${svg(ICON.list_dir)}<span>${esc(name)}</span>${svg(CHEV_DOWN)}`;
  $('#wsName').title = ws;
  updateSend();
  const s = v?.stats;
  const total = s ? s.hit + s.miss : 0;
  const contextChars = Number(v?.context_chars) || 0;
  const ctx = v?.fold_chars > 0 ? Math.round(contextChars / v.fold_chars * 100) : 0;
  const contextHint = (v?.context_unit === 'normalized_json_chars' ? '完整请求（含思考、工具参数和背景）的字符体积' : '历史消息的字符体积')
    + '，用于决定什么时候整理较早的内容（折起工具结果、写成检查点，原文都还能回查）；不代表模型窗口占用或实际 token 数。'
    + (v?.compacted_count ? `已经写过 ${v.compacted_count} 次检查点。` : '');
  const statsOpen = !!$('#status details')?.open;
  // 第六轮 (学 Codex): 上下文用量是一个小圆环, 悬停看数字, 点开看明细
  const ringLen = 2 * Math.PI * 7;
  const ring = `<svg class="ctx-ring" viewBox="0 0 18 18" width="16" height="16" aria-hidden="true"><circle cx="9" cy="9" r="7" fill="none" stroke="currentColor" stroke-opacity=".2" stroke-width="2.2"/><circle cx="9" cy="9" r="7" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-dasharray="${Math.max(1.4, ringLen * Math.min(100, ctx) / 100).toFixed(1)} ${ringLen.toFixed(1)}" transform="rotate(-90 9 9)"/></svg>`;
  $('#status').innerHTML = s && (s.turns || s.steps || total || s.out) ? `<details class="usage" ${statsOpen ? 'open' : ''}><summary title="上下文 ${tok(contextChars)} 字（到整理线 ${ctx}%）· ${s.turns || 0} 轮 · 点开看用量" aria-label="上下文用量 ${ctx}%">${ring}<span class="ctx-pct">${ctx}%</span></summary><div class="usage-pop"><strong>会话用量</strong><div>工具调用 <b>${s.steps || 0} 步</b></div><div>输入 / 输出 <b>${tok(total)} / ${tok(s.out)}</b></div><div>缓存命中 <b>${total ? Math.round(s.hit / total * 100) + '%' : '—'}</b></div><div>费用 <b>${money(s.cost)}${s.unpriced ? ` (${s.unpriced} 次待计价)` : ''}</b></div><div title="${esc(contextHint)}">整理阈值 <span><span class="bar"><i style="width:${Math.min(100, ctx)}%"></i></span>${ctx}%</span></div></div></details>` : '';
}

async function changeSetting(key, value) {
  if (!S.view) {  // 还没开会话: 先记着, 开会话时带上
    S.pick[key] = value;
    renderTools();
    return;
  }
  const sid = S.sid, generation = sessionGeneration;
  if (S.view.id !== sid) return false;
  const requestKey = `${sid}:${key}`, request = Symbol();
  settingRequests.set(requestKey, request);
  let view;
  try {
    view = await api({ action: 'settings', id: sid, [key]: value });
    if (!isCurrentSession(sid, generation) || S.view?.id !== sid || settingRequests.get(requestKey) !== request) return false;
    // 只合并本次设置，保留期间收到的消息和其他设置响应。
    const patch = { [key]: view[key] };
    if (key === 'think' || key === 'effort') patch.effort = view.effort;
    S.view = { ...S.view, ...patch };
  } finally {
    if (settingRequests.get(requestKey) === request) settingRequests.delete(requestKey);
  }
  if (key === 'mode' && S.state) S.state.default_mode = view.mode;  // 服务端也记成新会话的默认
  renderTop();
  renderTools();
  return true;
}

// 现在的设置: 开着的会话的; 没开会话时是菜单里选的, 再不然是默认
function cur() {
  const st = S.state || {}, v = S.view || {}, p = S.pick;
  return { mode: v.mode || p.mode || st.default_mode || 'workspace', preset: v.preset || p.preset || 'standard',
    model: v.model || p.model || st.models?.[0]?.id || '', effort: v.effort || p.effort || 'max', web: v.web ?? p.web ?? true };
}
const modelName = (id) => {
  const m = S.state?.models?.find((x) => x.id === id);
  return m ? m.label.split(' · ').pop() : (id ? id.split('|').pop() : '没有能用的模型');
};
// 本机模型 (2026-11-02): 服务端给的名字是"本机模型 · …" (selfhost, 或者自定义接口填了本机 / 局域网地址又没填 key 的, 服务端都按本机算)
const isLocalModel = (id) => (S.state?.models?.find((x) => x.id === id)?.label || '').startsWith('本机模型') || /^selfhost\|/.test(id || '');

// ---- 底下的两个菜单: 权限 (学 ChatGPT 的"应如何批准") 和 模型 + 做法 + 思考强度 (学 ChatGPT 的模型菜单和推理强度滑条) ----
function openMenu(kind) {
  const m = $('#menu');
  if (!m.hidden && m.dataset.kind === kind) { closeMenu(); return; }
  m.dataset.kind = kind;
  m.hidden = false;
  renderMenu();
  $('#modeBtn').classList.toggle('open', kind === 'mode');
  $('#modelBtn').classList.toggle('open', kind === 'model');
  $('#modeBtn').setAttribute('aria-expanded', String(kind === 'mode'));
  $('#modelBtn').setAttribute('aria-expanded', String(kind === 'model'));
}
function closeMenu() {
  $('#menu').hidden = true;
  $('#modeBtn').classList.remove('open');
  $('#modelBtn').classList.remove('open');
  $('#modeBtn').setAttribute('aria-expanded', 'false');
  $('#modelBtn').setAttribute('aria-expanded', 'false');
}
function renderMenu() {
  const m = $('#menu'), st = S.state, c = cur();
  if (!st) return;
  const item = (key, val, icon, title, desc, sel, cls = '') => `<button type="button" class="mi ${sel ? 'sel' : ''} ${cls}" role="menuitemradio"
    aria-checked="${sel}" data-set="${key}" data-val="${esc(val)}">${icon ? svg(icon, 'ic') : ''}<span class="t"><b>${esc(title)}</b>${desc ? `<small>${esc(desc)}</small>` : ''}</span>${sel ? svg(CHECK, 'ck') : ''}</button>`;
  if (m.dataset.kind === 'mode') {
    const allow = st.allow || [];
    // 第六轮 (学 Linear 的菜单): 一档一行 (图标 + 名字 + 勾), 说明不再每行两三行地摊开, 放在下面一格: 平时写选中的那档, 悬停哪档写哪档
    m.innerHTML = `<div class="mh">她改东西、跑命令前怎么问你?</div>
      ${Object.entries(st.modes).map(([k, label]) => item('mode', k, MODE_ICON[k], label, '', k === c.mode, k === 'unrestricted' ? 'warn' : '')).join('')}
      <div class="mdesc" id="modeDesc">${esc(DESC[c.mode] || '')}</div>
      <div class="mnote">选了哪一档, 以后新开的会话也照这一档</div>
      <div class="msec"><span>以后都允许的命令</span><small>${allow.length} 条</small></div>
      ${allow.length ? `<div class="rules">${allow.map((r) => `<div class="rule" title="${esc(r.example || '')}"><code>${esc(r.rule)} …</code>
        <button type="button" class="x" data-unallow="${esc(r.rule)}" title="移出名单 (以后又会问)" aria-label="移出名单">${svg('<path d="M6 6l12 12M18 6L6 18"/>')}</button></div>`).join('')}</div>`
        : '<div class="mnote">还没有. 申请卡片上点"以后都允许", 那一类命令记在这里, 以后在"工作区内修改"档不再问</div>'}
      <div class="mnote">名单只管单条命令; 删东西、装软件、联网下载这些照样每次问, 守门拦的照样拦</div>`;
    const desc = m.querySelector('#modeDesc');
    m.querySelectorAll('[data-set="mode"]').forEach((b) => {
      const show = () => { desc.textContent = DESC[b.dataset.val] || ''; desc.classList.toggle('warn', b.dataset.val === 'unrestricted'); };
      b.addEventListener('pointerenter', show);
      b.addEventListener('focus', show);
    });
    m.onpointerleave = () => { desc.textContent = DESC[c.mode] || ''; desc.classList.toggle('warn', c.mode === 'unrestricted'); };
    desc.classList.toggle('warn', c.mode === 'unrestricted');
  } else {
    m.onpointerleave = null;
    const i = Math.max(0, EFFORT_KEYS.indexOf(c.effort));
    m.innerHTML = `<div class="eff-card">
        <div class="eff-top"><span class="eff-name ${c.effort === 'off' ? 'off' : ''}">${esc(st.efforts?.[c.effort] || c.effort)}</span>
          <span class="eff-model">${esc(modelName(c.model))}</span></div>
        <input type="range" id="effRange" min="0" max="${EFFORT_KEYS.length - 1}" step="1" value="${i}" aria-label="思考强度" style="--p:${i / (EFFORT_KEYS.length - 1) * 100}%">
        <div class="eff-ticks">${EFFORT_KEYS.map((k) => `<button type="button" class="${k === c.effort ? 'on' : ''}" data-set="effort" data-val="${k}">${esc(st.efforts?.[k] || k)}</button>`).join('')}</div>
        <div class="eff-desc">${esc(EFFORT_DESC[c.effort] || '')}</div>
        ${isLocalModel(c.model) ? '<div class="eff-local">本机模型：思考只有开 / 关两档有区别（低、高、最高都等于开），开了会慢很多</div>' : ''}</div>
      <div class="msec"><span>做法</span></div>
      ${Object.entries(st.presets).map(([k, label]) => item('preset', k, '', label, DESC[k], k === c.preset)).join('')}
      <div class="msec"><span>选择模型</span></div>
      ${st.models.length ? st.models.map((x) => item('model', x.id, '', x.label.split(' · ').pop(), x.label.split(' · ').slice(0, -1).join(' · '), x.id === c.model)).join('')
        : '<div class="mnote">没有能用的模型: 在设置里填一个云端模型的 key、接一个本机模型, 或者设环境变量 DEEPSEEK_API_KEY</div>'}`;
  }
  // 放在按钮正上方: 权限菜单左对齐、模型菜单右对齐
  const btn = m.dataset.kind === 'mode' ? $('#modeBtn') : $('#modelBtn');
  const box = $('.composer').getBoundingClientRect(), b = btn.getBoundingClientRect();
  m.style.bottom = `${box.bottom - b.top + 8}px`;
  m.style.maxHeight = `${Math.min(520, Math.max(220, b.top - 16))}px`;  // 按钮上面放得下多高就多高, 其余在菜单里滚 (新聊天页输入框在中间时, 以前顶上会被窗口切掉)
  if (m.dataset.kind === 'mode') { m.style.left = `${Math.max(8, b.left - box.left)}px`; m.style.right = 'auto'; }
  else { m.style.right = `${Math.max(8, box.right - b.right)}px`; m.style.left = 'auto'; }
}
async function onMenuClick(e) {
  const un = e.target.closest('[data-unallow]');
  if (un) {
    const r = await api({ action: 'allow', remove: un.dataset.unallow });
    S.state.allow = r.allow || [];
    renderMenu();
    return;
  }
  const it = e.target.closest('[data-set]');
  if (!it) return;
  const key = it.dataset.set, val = it.dataset.val;
  const wasLocal = key === 'model' && isLocalModel(cur().model);
  await changeSetting(key, val);
  // 从云端换成本机模型 (2026-11-02 本机大模型会话实测: Qwen3.5-2B 开着思考一个任务 43 s → 432 s、步数翻倍、对的没多):
  // 思考强度先切到不思考, 想开在这里再开 (之后在本机模型之间换不再动它)
  if (key === 'model' && !wasLocal && isLocalModel(val) && cur().effort !== 'off') {
    await changeSetting('effort', 'off');
    flash('换成本机模型：思考先关了（开着会慢很多），要思考在模型菜单里再开');
  }
  if (key !== 'effort') closeMenu();  // 思考强度在菜单里多试几下, 不关
}

function flash(text) {
  $('#status').textContent = text;
  setTimeout(renderTools, 3000);
}

async function submit(later = false) {
  const input = $('#input');
  const typed = input.value.trim();
  const attached = (studio?.contextIds() || []).length;
  if (S.running) {  // 她正忙: 打了字就加到这一轮 (另一个键 = 做完再发; 带附件的、指令也排队), 什么都没有按就是停
    if (!typed && !attached) { S.ws?.send(JSON.stringify({ type: 'stop' })); return; }
    return later || attached || typed.startsWith('/') || !S.state?.steer ? queueSend(typed || '（看看我发来的附件）') : steerSend(typed);
  }
  if (!typed && !attached) return;
  if (pendingSend) return;
  const draftSid = S.sid;
  try { await studio?.ready(); } catch (err) { flash(err.message); return; }
  if (draftSid && draftSid !== S.sid) { flash('会话已切换，请在当前会话重新检查后发送'); return; }
  const ids = studio?.contextIds() || [];
  const body = typed || (ids.length ? '（看看我发来的附件）' : '');  // 只拖了附件没打字, 也要能发出去
  if (!body) return;
  if (!S.view) await newSession();
  for (let i = 0; i < 20 && S.ws?.readyState !== 1; i++) await new Promise((r) => setTimeout(r, 100));
  if (S.ws?.readyState !== 1) { flash('还没连上服务端, 稍等再发'); return; }
  input.value = '';
  autosize();
  updateSend();
  hideSlash();
  pendingSend = {sid: S.sid, text: body};
  S.ws.send(JSON.stringify({ type: 'send', text: body, context_ids: ids }));
}

// 她正忙的时候: 一句话排进队列 (这一轮做完, 当新的一件事接着做)
function queueSend(text) {
  if (S.ws?.readyState !== 1) { flash('还没连上服务端, 稍等再发'); return; }
  const input = $('#input');
  input.value = ''; autosize(); updateSend(); hideSlash();
  S.ws.send(JSON.stringify({ type: 'enqueue', text, context_ids: studio?.contextIds() || [] }));
  flash('已排队，这一轮做完就发');
}

// 她正忙的时候补充一句 (2026-11-01 "运行中加要求"): 不停下这一轮, 她做完手头这一步就看到, 后面的步骤照它来
function steerSend(text) {
  if (S.ws?.readyState !== 1) { flash('还没连上服务端, 稍等再发'); return; }
  const input = $('#input');
  input.value = ''; autosize(); updateSend(); hideSlash();
  pendingSteer = { sid: S.sid, text };
  S.ws.send(JSON.stringify({ type: 'steer', text }));
  flash('加到这一轮了，她做完手头这一步就看到');
}

function autosize() {
  const t = $('#input');
  t.style.height = 'auto';
  t.style.height = `${Math.min(240, t.scrollHeight)}px`;
}

function showSlash() {
  const v = $('#input').value;
  const items = v.startsWith('/') && !v.includes(' ') ? SLASH.filter(([c]) => c.startsWith(v)) : [];
  const box = $('#slash');
  if (!items.length) { hideSlash(); return; }
  S.slashSel = Math.min(S.slashSel, items.length - 1);
  box.innerHTML = items.map(([c, d], i) => `<div class="${i === S.slashSel ? 'sel' : ''}" data-cmd="${c}"><b>${c}</b><span>${esc(d)}</span></div>`).join('');
  box.hidden = false;
  box.querySelectorAll('[data-cmd]').forEach((el) => el.addEventListener('click', () => { $('#input').value = el.dataset.cmd; hideSlash(); submit(); }));
}
function hideSlash() { $('#slash').hidden = true; S.slashSel = 0; }

async function addWorkspace() {
  navigation?.addWorkspace();
}

// ---- 桌前的她 (workbench-pet.js): 同一个角色只露上半身, 坐在键盘后面, 跟着这里的进度换样子 ----
function deskPhase(name, text) {
  if (S.deskPin) name = S.deskPin;  // 自检截图: ?deskphase=write 定住一个样子
  if (S.desk && (S.desk.phaseName !== name || text !== undefined)) S.desk.phase(name, text);
}
function deskEvent(ev) {
  if (ev.type === 'user') deskPhase('think');
  else if (ev.type === 'tool') deskPhase(ev.state === 'denied' ? 'error' : 'tool', ev.state === 'denied' ? '不让做…' : `${ev.label || ev.name}…`);
  else if (ev.type === 'tool_result' || ev.type === 'ask_done') deskPhase('think');
  else if (ev.type === 'ask') deskPhase('ask');
  else if (ev.type === 'assistant') deskPhase('write');
  else if (ev.type === 'done') deskPhase('done');
  else if (ev.type === 'error') deskPhase('error');
}
function deskResume() {  // 打开会话、重连时: 按会话现在的状态摆
  const st = S.view?.status;
  deskPhase(st === 'waiting' ? 'ask' : st && st !== 'idle' ? 'think' : 'idle');
}
// 桌前的她用 3D 还是 2D: 默认跟着桌宠现在的门面 (宿主的 dock 消息带 live2d); 名片右上角的"2D / 3D"换成另一个,
// 换到和桌宠一样的那个就回到"跟着桌宠" (2026-10-27 用户: "工作台左侧的桌宠还不能切换成2d的")
const FACE_KEY = 'wm.workbench.desk.face';
const deskFacePick = () => { try { return localStorage.getItem(FACE_KEY) || 'auto'; } catch { return 'auto'; } };
const deskFace = () => { const p = deskFacePick(); return p === 'vrm' || p === 'live2d' ? p : (S.dock?.live2d ? 'live2d' : 'vrm'); };
function renderFaceSw() {
  const b = $('#deskFace');
  if (!b) return;
  const now = S.deskWant || deskFace(), other = now === 'live2d' ? 'vrm' : 'live2d';
  b.hidden = !S.deskOn;
  b.textContent = other === 'live2d' ? '2D' : '3D';
  b.title = `换成${other === 'live2d' ? ' 2D (Live2D)' : ' 3D'}${deskFacePick() === 'auto' ? ' (现在跟着桌宠的门面)' : ''}`;
}
async function switchFace() {
  const want = (S.deskWant || deskFace()) === 'live2d' ? 'vrm' : 'live2d';
  try { localStorage.setItem(FACE_KEY, want === (S.dock?.live2d ? 'live2d' : 'vrm') ? 'auto' : want); } catch { /* 无所谓 */ }
  await remountDesk();
}
async function remountDesk() {
  if (!S.deskOn || S.deskLoading) return;
  S.desk?.dispose();
  S.desk = null;
  await setDesk(true);
}
async function setDesk(on) {
  S.deskOn = on;
  try { localStorage.setItem('wm.workbench.desk', on ? '1' : '0'); } catch { /* 无所谓 */ }
  const el = $('#desk');
  $('#deskFold').innerHTML = svg(on ? '<path d="M6 15l6-6 6 6"/>' : '<path d="M6 9l6 6 6-6"/>');
  $('#deskFold').classList.toggle('off', !on);
  if (!on) { S.desk?.dispose(); S.desk = null; el.hidden = true; renderHer(); renderFaceSw(); tell('desk', { ok: false }); return; }
  if (S.desk || S.deskLoading) return;
  S.deskLoading = true;
  el.hidden = false;
  el.classList.add('loading');
  const face = S.deskWant = deskFace();
  try {
    const { mountDesk } = await import('/pet/workbench-pet.js');  // 分开载入: 3D 这块出错不影响工作台本身
    S.desk = S.deskOn ? await mountDesk(el, { name: herName(), face }) : null;
    if (!S.desk && S.deskOn && face === 'live2d') {  // 2D 没加载成 (没有 Live2D 的文件、浏览器里打开的): 先用 3D
      tell('log', { text: '桌前的她 (2D) 没加载成, 先用 3D' });
      S.desk = await mountDesk(el, { name: herName(), face: 'vrm' });
    }
  } catch (err) {
    tell('log', { text: `桌前的她没加载成: ${err.message}` });
    S.desk = null;
  }
  S.deskLoading = false;
  el.classList.remove('loading');
  if (!S.desk) el.hidden = true;
  else if (!S.deskOn) { S.desk.dispose(); S.desk = null; el.hidden = true; }
  else { deskResume(); if ($('#her').hidden) S.desk.pause(true); }  // 名片收着: 先不画
  renderHer();
  renderFaceSw();
  tell('desk', { ok: !!S.desk, face: S.desk?.face || face });
  if (S.desk && S.deskWant !== deskFace()) remountDesk();  // 加载的时候桌宠换了门面
}

// ---- 桌宠的功能键 (2026-11-01 照 Codex 改版: 原来右边那一列挪到左栏底下一排小图标; 状态照旧由宿主发 {t: 'dock'}) ----
function renderDock() {
  const d = S.dock || {};
  const key = (act, label, glyph, { on = false, off = !host } = {}) => `<button type="button" data-act="${act}" class="${on ? 'on' : ''}" title="${label}" aria-label="${label}" ${act === 'mic' || act === 'mute' || act === 'pin' ? `aria-pressed="${on}"` : ''} ${off ? 'disabled' : ''}>${svg(RAIL_ICON[glyph])}</button>`;
  $('#dockKeys').innerHTML = key('mic', d.mic ? '关闭麦克风' : '打开麦克风', d.mic ? 'mic' : 'micOff', { on: !!d.mic })
    + key('mute', d.muted ? '恢复声音' : '静音', d.muted ? 'volumeOff' : 'volume', { on: !!d.muted })
    + key('persona', '切换人设', 'persona')
    + key('pin', d.top ? '取消工作台置顶' : '工作台置顶', 'pin', { on: !!d.top })
    + (d.room && !d.live2d ? key('room', '回到实验室小屋', 'room') : '')  // 实验室 (小屋) 是研发场: 只在已经开着时给回去的入口 (2026-11-01)
    + key('desktop', '回到桌宠', 'desktop')
    + '<span class="grow"></span>'
    + key('settings', '设置 (Ctrl ,)', 'settings', { on: !!S.settingsOpen });
}
function dockClick(e) {
  const btn = e.target.closest('button[data-act]');
  if (!btn || btn.disabled) return;
  const act = btn.dataset.act;
  if (act === 'settings') { S.settingsOpen ? closeSettings() : openSettings(); return; }
  if (act === 'persona') { togglePersonas(btn); return; }
  tell('dock', { act });
}

function togglePersonas(btn) {
  const pop = $('#personaPop');
  if (!pop.hidden) { pop.hidden = true; return; }
  const d = S.dock || {};
  pop.innerHTML = `<div class="pp-title">人设预设</div>${(d.personas || []).map((p) => `<button type="button" data-id="${esc(p.id)}" class="${p.id === d.active ? 'sel' : ''}">
    ${esc(p.name)}${p.id === d.active ? '<span class="ck">✓</span>' : ''}</button>`).join('') || '<div class="pp-empty">还没有人设预设</div>'}`;
  const r = btn.getBoundingClientRect();
  pop.hidden = false;
  pop.style.top = `${Math.max(10, r.top - pop.offsetHeight - 8)}px`;  // 功能键在左栏底下: 往上弹
  pop.style.left = `${Math.max(10, Math.min(r.left, innerWidth - pop.offsetWidth - 10))}px`;
  pop.querySelectorAll('[data-id]').forEach((el) => el.addEventListener('click', () => { tell('dock', { act: 'persona', id: el.dataset.id }); pop.hidden = true; }));
}

// 网页都在右侧面板里开: 桌面工作台是独立的原生网页 (宿主 v2 一个、v3 每个标签一个), 浏览器里打开的工作台用文字阅读
function openBrowser(url) {
  panel?.openWeb(url || '', { newTab: !!url });
}

// ---- 对话里的卡片: 撤销、查看变更、打开方式 ----
function closeOpenWith() { $('#openWithMenu')?.remove(); }
function showOpenWith(btn) {
  closeOpenWith();
  const kind = btn.dataset.openMenu, target = btn.dataset.target;
  const menu = document.createElement('div');
  menu.id = 'openWithMenu';
  menu.className = 'wb-pmenu wb-open-menu';
  menu.setAttribute('role', 'menu');
  const item = (act, label, glyph) => `<button type="button" role="menuitem" data-open-act="${act}">${svg(glyph)}<span>${label}</span></button>`;
  menu.innerHTML = kind === 'url'
    ? item('panel', '在右侧打开', RAIL_ICON.globe) + item('external', '在系统浏览器打开', '<path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6"/>') + item('attach', '加入对话材料', ICON.read_file) + item('copy', '复制链接', '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V4H4v12h4"/>')
    : item('panel', '在右侧打开', ICON.read_file) + (host ? item('reveal', '在文件夹中显示', ICON.list_dir) : '') + item('copy', '复制路径', '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V4H4v12h4"/>');
  document.body.append(menu);
  const r = btn.getBoundingClientRect();
  menu.style.top = `${Math.min(r.bottom + 6, innerHeight - menu.offsetHeight - 10)}px`;
  menu.style.left = `${Math.max(10, Math.min(r.right - menu.offsetWidth, innerWidth - menu.offsetWidth - 10))}px`;
  menu.querySelector('button')?.focus({ preventScroll: true });
  menu.addEventListener('click', async (event) => {
    const act = event.target.closest('[data-open-act]')?.dataset.openAct;
    if (!act) return;
    closeOpenWith();
    try {
      if (act === 'panel') kind === 'url' ? openBrowser(target) : panel?.openFile(target);
      else if (act === 'external') { if (host) tell('browser', { action: 'external', url: target, tab_id: 'main' }); else window.open(target, '_blank', 'noopener,noreferrer'); }
      else if (act === 'attach') { await studio?.addReference(target); flash('链接已加入对话材料'); }
      else if (act === 'reveal') await revealWorkspaceFile({ id: S.view?.id, path: target, absolutePath: `${String(S.view?.workspace || '').replace(/[\\/]+$/, '')}\\${target.replace(/\//g, '\\')}` });
      else if (act === 'copy') { await navigator.clipboard.writeText(kind === 'url' ? target : `${String(S.view?.workspace || '').replace(/[\\/]+$/, '')}\\${target.replace(/\//g, '\\')}`); flash('已复制'); }
    } catch (error) { flash(error.message); }
  });
}
async function chatCardClick(event) {
  const link = event.target.closest('.message-text a[href^="http"]');
  if (link) { event.preventDefault(); openBrowser(link.href); return; }
  const b = event.target.closest('button');
  if (!b || b.disabled) return;
  try {
    if (b.dataset.nbKey) {  // 资料本的出处小卡片: 弹出那一块的原文
      const hit = /^S(\d+)-\d+$/.exec(b.dataset.nbKey);  // 这本的阅读标签开着: 直接翻到那一页, 不弹卡片
      if (hit && S.notebook && panel?.hasReading(S.notebook.id)) { panel.openReading(S.notebook.id, Number(hit[1]), b.dataset.nbKey); return; }
      showCitation(b, { notebookId: S.notebook?.id, key: b.dataset.nbKey, openDoc: (url, title) => panel?.openDoc(url, title),
        openReading: (nid, no, key, name) => panel?.openReading(nid, no, key, name),
        openWeb: (url) => openBrowser(url), openNotebook: (id) => panel?.openNotebook(id) });
      return;
    }
    if (b.dataset.sources) panel?.openSources(JSON.parse(b.dataset.sources));
    else if (b.dataset.openUrl) openBrowser(b.dataset.openUrl);
    else if (b.dataset.openFile !== undefined && b.closest('.wb-card')) panel?.openFile(b.dataset.openFile);
    else if (b.dataset.openMenu) showOpenWith(b);
    else if (b.dataset.diff) panel?.openDiff(JSON.parse(b.dataset.diff), `变更 · ${b.dataset.diffTitle || ''}`);
    else if (b.hasAttribute('data-view-changes')) { const card = b.closest('.wb-edits'); panel?.openDiff(JSON.parse(card.dataset.calls), `变更 · ${card.querySelectorAll('.wb-edit-row').length} 个文件`); }
    else if (b.hasAttribute('data-undo')) {
      if (S.running) { flash('她还在做这一轮，等做完再撤销'); return; }
      b.disabled = true;
      const result = await panel?.undo(JSON.parse(b.closest('.wb-edits').dataset.calls));
      if (result?.ok) b.closest('.wb-edits').classList.add('undone');
      b.disabled = false;
    }
  } catch (error) { b.disabled = false; flash(error.message); }
}
document.addEventListener('pointerdown', (event) => { if (!event.target.closest('#openWithMenu, [data-open-menu]')) closeOpenWith(); });
document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && $('#openWithMenu')) { event.preventDefault(); closeOpenWith(); } }, true);

// ---- 设置 (panel.html?embed=1 放在 iframe 里; 它和宿主说的话经这里转) ----
function openSettings(tab) {
  if (!host) { flash('设置在桌宠的工作台窗口里才有 (浏览器里打开的工作台连不上桌宠)'); return; }
  panel?.suspend(true);
  let frame = $('#settingsFrame iframe');
  if (!frame) {
    frame = document.createElement('iframe');
    frame.title = '设置';
    frame.src = `/pet/panel.html?embed=1${tab ? `&tab=${encodeURIComponent(tab)}` : ''}`;
    frame.addEventListener('load', () => { frame.dataset.ready = '1'; });
    $('#settingsFrame').append(frame);
  } else if (tab) {
    frame.contentWindow?.postMessage({ wmHost: { t: 'panel', op: 'show', tab } }, location.origin);
  }
  S.settingsOpen = true;
  closeMenu();
  $('#personaPop').hidden = true;
  $('#sideScrim').hidden = true;
  document.querySelector('.app').classList.remove('sidebar-open');
  $('#settings').hidden = false;  // 弹窗盖在上面, 两边侧栏不动 (不再给中间加 show-settings 把对话藏掉)
  // 设置要宽: 左边的会话栏收起来 (顶上有"← 回到对话"), 桌前的她跟着停画
  document.querySelector('.app').classList.add('settings-mode');
  S.desk?.pause(true);
  renderDock();
  frame.addEventListener('load', () => frame.contentWindow?.focus(), { once: true });
}
function closeSettings() {
  if (!S.settingsOpen) return Promise.resolve(true);
  if (settingsCloseRequest) return settingsCloseRequest.promise;
  const frame = $('#settingsFrame iframe');
  if (!frame) { completeSettingsClose(); return Promise.resolve(true); }
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  const request = { id: ++settingsCloseSequence, promise, resolve };
  settingsCloseRequest = request;
  $('#settingsBack').disabled = true;
  $('#settingsBack').setAttribute('aria-busy', 'true');
  // 保持设置可见，保存失败时用户可以直接查看错误并重试。
  const send = () => {
    if (settingsCloseRequest === request) frame.contentWindow?.postMessage({ wmHost: { t: 'panel', op: 'closing', requestId: request.id } }, location.origin);
  };
  if (frame.dataset.ready === '1') send();
  else frame.addEventListener('load', send, { once: true });
  setTimeout(() => {  // 设置页没加载成 / 没回话: 4 秒后直接关上, 不让人困在弹窗里
    if (settingsCloseRequest !== request) return;
    completeSettingsClose();
    flash('设置页没有回应，已直接关上（改过的项目重新打开设置看一眼）');
  }, 4000);
  return promise;
}
function settleSettingsClose(ok) {
  const pending = settingsCloseRequest;
  settingsCloseRequest = null;
  $('#settingsBack').disabled = false;
  $('#settingsBack').removeAttribute('aria-busy');
  pending?.resolve(ok);
}
function completeSettingsClose() {
  // 仅在设置 iframe 明确确认 flush 成功后调用，不再向它重复发送 closing。
  S.settingsOpen = false;
  panel?.suspend(false);
  $('#settings').hidden = true;
  $('#mainView').classList.remove('show-settings');
  document.querySelector('.app').classList.remove('settings-mode');
  S.desk?.pause($('#her').hidden);
  renderDock();
  $('#input').focus();
  settleSettingsClose(true);
}

function onHost(e) {
  const m = e.data;
  if (m?.t === 'workbench-capabilities') S.hostCaps = m;  // 不拦: 浏览器、导航也要看
  if(m?.t==='workbench-file-revealed') {const request=revealRequests.get(m.rid);if(request){clearTimeout(request.timer);revealRequests.delete(m.rid);m.ok?request.resolve():request.reject(new Error(m.error||'无法打开文件位置'));}return;}
  if (panel?.onHost(m)) return;
  if (navigation?.onHost(m)) return;
  if (m?.t === 'autonomy-host' && m.version === 1 && m.scope === 'native-tools') {
    S.nativeAuthority = true;
    studio?.refresh(true);
    return;
  }
  if (m?.t === 'dock') {
    S.dock = m;
    renderDock(); renderHer(); renderFaceSw();
    if (S.desk && S.deskWant !== deskFace()) remountDesk();  // 桌宠换了门面 (人设预设 3D <-> 2D), 跟着桌宠时一起换
    return;
  }
  if (m?.t === 'wb' && m.op === 'settings') { openSettings(m.tab || ''); return; }
  if (m?.t === 'panel') $('#settingsFrame iframe')?.contentWindow?.postMessage({ wmHost: m }, location.origin);
}

// 页面文件改了 (宿主每次从磁盘读, 但开着的窗口不会自己换): 顶栏出一个"有新版本 · 刷新", 不自动刷 (免得打断打字)
// 2026-11-01: 改版后用户一直看着早上加载的旧页面, 以为没改好
const PAGE_FILES = ['workbench.html', 'workbench.js', 'workbench-shell.css', 'workbench-panel.js', 'workbench-browser.js', 'workbench-files.js',
  'workbench-canvas.js', 'workbench-studio.js', 'workbench-resident.js', 'workbench-platform.js'];  // 第六轮: 工具页这几个改了也提示刷新
async function pageStamp() {
  let hash = 0;
  for (const name of PAGE_FILES) {
    const text = await (await fetch(`/pet/${name}`, { cache: 'no-store' })).text();
    for (let i = 0; i < text.length; i++) hash = (Math.imul(hash, 31) + text.charCodeAt(i)) | 0;
  }
  return hash;
}
function watchPageFiles() {
  let first = null;
  const look = async () => {
    if (document.hidden || S.pageStale) return;
    try {
      const now = await pageStamp();
      if (first === null) first = now;
      else if (now !== first) { S.pageStale = true; renderTop(); }
    } catch { /* 读不到就下次再看 */ }
  };
  look();
  setInterval(look, 60000);
  document.addEventListener('visibilitychange', look);
}

// 输入框上方飘一句提示 (开关之类点了要有回应), 两秒多淡掉
function composerToast(text) {
  let toast = $('#composerToast');
  if (!toast) { toast = document.createElement('div'); toast.id = 'composerToast'; toast.className = 'wb-toast'; toast.setAttribute('role', 'status'); $('.wb-composer').append(toast); }
  toast.textContent = text;
  toast.classList.remove('show'); void toast.offsetWidth; toast.classList.add('show');
  clearTimeout(composerToast.timer);
  composerToast.timer = setTimeout(() => toast.classList.remove('show'), 2400);
}

// 左栏"项目"旁边的 ≡: 分组依据 / 排序方式 / 显示 / 添加项目 / 查看全部 / 恢复默认 (照 Claude 桌面版那个菜单)
function closeSideView() { $('#sideViewMenu')?.remove(); $('#sideViewBtn')?.setAttribute('aria-expanded', 'false'); }
function toggleSideView(anchor) {
  if ($('#sideViewMenu')) { closeSideView(); return; }
  const menu = document.createElement('div');
  menu.id = 'sideViewMenu';
  menu.className = 'wb-ctx';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', '左栏怎么排');
  document.body.append(menu);
  renderSideView();
  const r = anchor.getBoundingClientRect();
  menu.style.top = `${Math.max(8, Math.min(r.bottom + 6, innerHeight - menu.offsetHeight - 8))}px`;
  menu.style.left = `${Math.max(8, Math.min(r.left - 8, innerWidth - menu.offsetWidth - 200))}px`;
  anchor.setAttribute('aria-expanded', 'true');
  menu.querySelector('[role^="menuitem"]')?.focus({ preventScroll: true });
}
function renderSideView() {
  const menu = $('#sideViewMenu');
  if (!menu) return;
  const v = sideView(), times = preferences.showSessionTimes !== false;
  const isDefault = v.group === SIDE_VIEW.group && v.sort === SIDE_VIEW.sort && v.recent === SIDE_VIEW.recent && times && !readJSON('wm.workbench.wsOrder', []).length;
  const radio = (key, value, label) => `<button type="button" role="menuitemradio" aria-checked="${v[key] === value}" data-view-set="${key}:${value}"><span>${label}</span>${v[key] === value ? svg(CHECK, 'ck') : ''}</button>`;
  const check = (act, on, label) => `<button type="button" role="menuitemcheckbox" aria-checked="${on}" data-view-act="${act}"><span>${label}</span>${on ? svg(CHECK, 'ck') : ''}</button>`;
  const sub = (label, value, inner) => `<div class="wb-ctx-item has-sub" tabindex="0" role="menuitem" aria-haspopup="menu"><span>${label}</span><em>${value}</em>${svg('<path d="m9 6 6 6-6 6"/>', 'chev')}<div class="wb-ctx wb-ctx-sub" role="menu">${inner}</div></div>`;
  menu.innerHTML = sub('分组依据', v.group === 'none' ? '不分组' : '项目', radio('group', 'folder', '按项目（文件夹）') + radio('group', 'none', '不分组'))
    + sub('排序方式', { updated: '上次更新', created: '创建时间', title: '名称' }[v.sort], radio('sort', 'updated', '上次更新') + radio('sort', 'created', '创建时间') + radio('sort', 'title', '名称'))
    + sub('显示', '', check('times', times, '会话时间') + check('recent', v.recent, '“最近”一栏'))
    + `<hr><button type="button" role="menuitem" data-view-act="add"><span>添加项目（文件夹）…</span></button>
      <button type="button" role="menuitem" data-view-act="all"><span>查看全部会话</span></button>
      <button type="button" role="menuitem" data-view-act="reset" ${isDefault ? 'disabled' : ''}><span>恢复默认设置</span></button>`;
}
function sideViewClick(event) {
  const sub = event.target.closest('.has-sub');
  const b = event.target.closest('button');
  if (!b) { sub?.classList.toggle('open'); return; }
  if (b.disabled) return;
  if (b.dataset.viewSet) {
    const [key, value] = b.dataset.viewSet.split(':');
    writeJSON('wm.workbench.sideView', { ...sideView(), [key]: value });
  } else if (b.dataset.viewAct === 'times') updatePreferences({ showSessionTimes: preferences.showSessionTimes === false });
  else if (b.dataset.viewAct === 'recent') writeJSON('wm.workbench.sideView', { ...sideView(), recent: !sideView().recent });
  else if (b.dataset.viewAct === 'add') { closeSideView(); navigation?.addWorkspace(); return; }
  else if (b.dataset.viewAct === 'all') { (S.state?.workspaces || []).forEach((w) => wsExpanded.add(w.path)); S.closed.clear(); closeSideView(); renderSide(); return; }
  else if (b.dataset.viewAct === 'reset') {
    try { localStorage.removeItem('wm.workbench.sideView'); localStorage.removeItem('wm.workbench.wsOrder'); } catch { /* 无所谓 */ }
    updatePreferences({ showSessionTimes: true }); wsExpanded.clear(); S.closed.clear();
  }
  renderSide();
  const keep = [...$('#sideViewMenu').querySelectorAll('.has-sub.open, .has-sub:hover')].map((el) => el.querySelector('span')?.textContent);
  renderSideView();  // 菜单不关, 打勾跟着变; 刚才展开的那一层还展开着
  $('#sideViewMenu').querySelectorAll('.has-sub').forEach((el) => { if (keep.includes(el.querySelector('span')?.textContent)) el.classList.add('open'); });
}
document.addEventListener('click', (event) => {
  if (!$('#sideViewMenu')) return;
  if (event.target.closest('#sideViewMenu')) sideViewClick(event);
  else if (!event.target.closest('#sideViewBtn')) closeSideView();
});
document.addEventListener('keydown', (event) => { if (event.key === 'Escape' && $('#sideViewMenu')) { event.preventDefault(); closeSideView(); $('#sideViewBtn')?.focus(); } }, true);

// 项目右键菜单里只动页面的那几样 (顺序、折叠、显示多少、打开文件夹); 归档 / 删除在 workbench-navigation.js 里带确认
function workspaceAction(action, path) {
  if (action === 'up' || action === 'down') moveWorkspace(path, action === 'up' ? -1 : 1);
  else if (action === 'collapse_all') { (S.state?.workspaces || []).forEach((w) => S.closed.add(w.path)); renderSide(); }
  else if (action === 'expand_all') { S.closed.clear(); renderSide(); }
  else if (action === 'toggle_all') { wsExpanded.has(path) ? wsExpanded.delete(path) : wsExpanded.add(path); S.closed.delete(path); renderSide(); }
  else if (action === 'reveal') return revealWorkspaceFile({ workspace: path, path: '', absolutePath: path });
  else if (action === 'expanded') return wsExpanded.has(path);
  else if (action === 'order') return orderedWorkspaces().map((w) => w.path);
}

// 左栏的搜索框: 平时收着 (照 Codex 只有一个放大镜), 点了或 Ctrl K 才出来; 清空再收起
function toggleSideSearch(open = $('#sideSearch').hidden) {
  $('#sideSearch').hidden = !open;
  if (open) { $('#filter').focus(); $('#filter').select(); return; }
  if ($('#filter').value) { $('#filter').value = ''; S.filter = ''; renderSide(); }
}
// "绫音"一行点开是她的名片: 桌前的她 + 钱包 (收起时桌前的她照旧按自己的开关, 不在这里卸掉)
function toggleHerCard(open = $('#her').hidden) {
  $('#her').hidden = !open;
  $('#herRow').setAttribute('aria-expanded', String(open));
  try { localStorage.setItem('wm.workbench.herCard', open ? '1' : '0'); } catch { /* 无所谓 */ }
  S.desk?.pause(!open);
}

// Ctrl+P 里能直接做的事 (第二批, 学 Linear 的命令面板; 输入 > 只搜这些): 标题写清做什么, 右边是快捷键
function paletteCommands() {
  const st = S.state || {}, c = cur(), list = [];
  const add = (title, run, extra = {}) => list.push({ title, run, ...extra });
  add('新聊天', () => newSession(), { kbd: 'Ctrl+N', icon: 'plus', keywords: 'new chat 新会话' });
  add(c.web !== false ? '关掉联网搜索' : '打开联网搜索', () => $('#webTog').click(), { icon: 'globe', keywords: 'web search 联网 上网' });
  add('打开终端', () => panel.openTool('terminal'), { kbd: 'Ctrl+`', icon: 'terminal', keywords: 'terminal 命令行' });
  add('新标签页', () => panel.openWeb('', { newTab: true }), { kbd: 'Ctrl+T', icon: 'globe', keywords: 'tab 网页' });
  add(panel.isOpen() ? '收起右侧面板' : '打开右侧面板', () => panel.toggle(), { kbd: 'Ctrl+J', icon: 'hide', keywords: 'panel 面板' });
  add(sidebarExpanded() ? '收起左栏' : '展开左栏', () => setSidebar(!sidebarExpanded()), { kbd: 'Ctrl+B', icon: 'hide', keywords: 'sidebar 侧栏' });
  add('搜索会话', () => { setSidebar(true); toggleSideSearch(true); }, { kbd: 'Ctrl+K', icon: 'search', keywords: 'search 找会话' });
  for (const [k, label] of Object.entries(st.modes || {})) if (k !== c.mode) add(`权限改成「${label}」`, () => changeSetting('mode', k), { icon: 'tools', sub: DESC[k] || '', keywords: `mode permission 权限 ${DESC[k] || ''}` });
  for (const m of st.models || []) if (m.id !== c.model) add(`模型换成 ${m.label.split(' · ').pop()}`, () => changeSetting('model', m.id), { icon: 'box', sub: m.label.split(' · ').slice(0, -1).join(' · '), keywords: `model 模型 ${m.label}` });
  add('打开设置', () => openSettings(), { kbd: 'Ctrl+,', icon: 'grid', keywords: 'settings 偏好' });
  add('资料本', () => panel.openNotebook(S.notebook?.id || ''), { icon: 'book', keywords: 'notebook 资料 学习' });
  add('任务与成果', () => studio?.open(), { icon: 'agents', keywords: 'tasks 成果' });
  add('归档与回收站', () => navigation?.archived(), { icon: 'box', keywords: 'archive 归档 回收站' });
  return list;
}

// 悬停提示 (第六轮, 学 Linear): 按钮悬停 0.4 秒出一个小黑条, 写名字和快捷键。把 title 挪成 data-tip, 免得和浏览器自带的提示叠在一起
function initTips() {
  let tip = null, timer = 0, target = null;
  const hide = () => { clearTimeout(timer); tip?.remove(); tip = null; target = null; };
  const show = (el) => {
    const text = el.dataset.tip || '';
    if (!text || !el.isConnected || !el.matches(':hover')) return;
    const m = /^(.*?)\s*\(((?:Ctrl|Alt|Shift|Enter|Esc|F\d)[^()]*)\)\s*$/.exec(text);  // "新标签页 (Ctrl T)" -> 名字 + 键
    tip = document.createElement('div');
    tip.className = 'wb-tip';
    tip.setAttribute('role', 'tooltip');
    tip.innerHTML = m ? `<span>${esc(m[1])}</span><kbd>${esc(m[2])}</kbd>` : `<span>${esc(text)}</span>`;
    document.body.append(tip);
    const r = el.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.top = `${r.bottom + 8 + h < innerHeight ? r.bottom + 6 : r.top - h - 6}px`;
    tip.style.left = `${Math.max(6, Math.min(innerWidth - w - 6, r.left + r.width / 2 - w / 2))}px`;
  };
  document.addEventListener('pointerover', (event) => {
    const el = event.target.closest?.('button[title], [role="tab"][title], summary[title], a.wb-cite[title], [data-tip]');
    if (el === target) return;
    hide();
    if (!el) return;
    if (el.hasAttribute('title')) {
      const title = el.getAttribute('title');
      el.removeAttribute('title');
      if (!title) return;
      el.dataset.tip = title;
      if (!el.hasAttribute('aria-label') && !el.textContent.trim()) el.setAttribute('aria-label', title);
    }
    target = el;
    timer = setTimeout(() => show(el), 420);
  });
  for (const type of ['pointerdown', 'keydown', 'scroll', 'blur']) addEventListener(type, hide, true);
}

async function main() {
  navigation = initNavigation({api,getState:()=>S.state,getView,getSession:()=>S.view,openSession,newSession,notify:flash,host,tell,
    workspaceAction,getHostCaps:()=>S.hostCaps||{},
    reload:async()=>{await loadState();renderTop();renderChat();},
    clearSession:()=>{
      sessionGeneration++;
      if(S.ws){S.ws.onmessage=null;S.ws.onclose=null;S.ws.close();S.ws=null;}
      S.sid=null;S.view=null;S.running=false;S.typing='';S.thinkLive='';pendingSend=null;
      try{localStorage.removeItem('wm.workbench.sid');}catch{}
      renderChat();renderTools();panel?.sessionChanged();
    }
  });
  $('#archivedSessions').addEventListener('click',()=>navigation.archived().catch(error=>flash(error.message)));
  $('#wsName').addEventListener('click',event=>navigation.workspaceMenu(event.currentTarget));
  studio = initStudio({api, getSession: () => S.view,
    ensureSession: async () => {
      if (!S.view) { creatingSession ||= newSession().finally(() => { creatingSession = null; }); await creatingSession; }
      return S.view;
    }, openSession, notify: flash, getNativeAuthority: () => S.nativeAuthority === true,
    authorityChanged: () => tell('autonomy_changed'), onPlan: () => changeSetting('preset','plan'),
    setDraft: (text) => { $('#input').value = text; autosize(); updateSend(); $('#input').focus(); }
  });
  const panelReload = async () => {
    const sid = S.sid, generation = sessionGeneration, request = ++canvasReloadGeneration;
    await loadState();
    if (!isCurrentSession(sid, generation) || request !== canvasReloadGeneration) return;
    if (sid) { const view = await getView(sid); if (!isCurrentSession(sid, generation) || request !== canvasReloadGeneration) return; S.view = view; }
    renderChat(); renderTools();
  };
  panel = initPanel({ app: document.querySelector('.app'), getSession: () => S.view, getState: () => S.state, api, tell, notify: flash, openSession,
    ensureSession: async () => { if (!S.view) await newSession(); return S.view; }, reload: panelReload, openStudio: (tab) => studio.open(tab), openSettings,
    setDraft: (text) => { $('#input').value = text; autosize(); updateSend(); $('#input').focus(); }, setPreset: (value) => changeSetting('preset', value), changeSetting, renderChat,
    attachReference: (value) => studio.addReference(value), attachText: (item) => studio.addTextMaterial(item),
    ask: async (text) => {  // 阅读面板的"问她": 和输入框同一条发送路径 (她正忙就按现有的排队 / 并入); 输入框里原来的草稿发完放回去
      const input = $('#input'), keep = input.value;
      input.value = text; autosize(); updateSend();
      await submit();
      if (!input.value && keep) { input.value = keep; autosize(); updateSend(); }
    },
    revealFile: host ? revealWorkspaceFile : null, onLayout: () => renderTop(), openWith: showOpenWith, getCommands: paletteCommands,
    onStudioTab: (ref) => studio.display(ref) });
  // 任务与成果开在右侧面板的标签里 (第六轮第三批, 以前是挡住整个工作台的大弹窗)
  studio.setHost?.({ show: (next) => panel.openStudioTab(next), shown: () => panel.studioShown(), moved: (ref) => panel.studioTab(ref) });
  addEventListener('wm-open-browser', (event) => openBrowser(event.detail?.url));
  addEventListener('wm-panel-tool', (event) => panel.openTool(event.detail?.name));
  addEventListener('wm-preferences-changed', (event) => {
    preferences = event.detail.preferences;
    if (!motionAllowed()) entranceAnimations.forEach((animation) => animation.cancel());
    updateSend();
    for (const [key, item] of notices.queued) if (!noticeAllowed(item)) notices.queued.delete(key);
    $('#notifications')?.querySelectorAll('.wb-notice').forEach((node) => {
      if (!noticeAllowed({ kind: node.dataset.kind })) node.remove();
    });
  });
  initPreferences();
  document.addEventListener('visibilitychange', flushNotices);
  addEventListener('focus', flushNotices);
  document.addEventListener('pointerdown', unlockNoticeSound, true);
  document.addEventListener('keydown', unlockNoticeSound, true);
  try { if (localStorage.getItem('wm.workbench.sidebar') === '0') document.querySelector('.app').classList.add('sidebar-collapsed'); } catch { /* optional preference */ }
  host?.addEventListener('message', onHost);
  addEventListener('message', (e) => {  // 设置页 (iframe) 要和宿主说的话
    const frame = $('#settingsFrame iframe');
    if (e.origin !== location.origin || !frame || e.source !== frame.contentWindow) return;
    if (e.data?.wmPanelUI === 'open-tool' && ['capabilities', 'presets'].includes(e.data.tool)) {  // 设置里的"内置插件 / Agent 预设": 关设置, 开面板那一页
      closeSettings().then((ok) => { if (ok) panel?.openTool(e.data.tool); });
      return;
    }
    if (['close-ready', 'close-failed'].includes(e.data?.wmPanelUI)) {
      const { requestId, wmPanelUI } = e.data;
      if (requestId !== undefined && requestId !== settingsCloseRequest?.id) return;
      if (wmPanelUI === 'close-ready') completeSettingsClose();
      else { settleSettingsClose(false); frame.contentWindow?.focus(); }
      return;
    }
    if (frame && e.source === frame.contentWindow && typeof e.data?.wmPanel === 'string' && e.data.wmPanel.startsWith('panel ')) host?.postMessage(e.data.wmPanel);
  });
  addEventListener('storage', (e) => {  // 设置里开关了"桌前的她" (同源, localStorage 共用)
    if (e.key === 'wm.workbench.desk' && (e.newValue !== '0') !== S.deskOn) setDesk(e.newValue !== '0');
  });
  renderDock();
  $('#dockKeys').addEventListener('click', dockClick);
  $('#settingsBack').addEventListener('click', closeSettings);
  $('#sideScrim').addEventListener('click', () => setSidebar(false));
  document.addEventListener('pointerdown', (e) => { const pop = $('#personaPop'); if (!pop.hidden && !pop.contains(e.target) && !e.target.closest('[data-act="persona"]')) pop.hidden = true; });
  $('#newSession').addEventListener('click', () => newSession());
  $('#sideSearchBtn').addEventListener('click', () => toggleSideSearch());
  $('#sideCollapse').addEventListener('click', () => setSidebar(false));
  $('#herRow').addEventListener('click', () => toggleHerCard());
  $('#sideScroll').addEventListener('keydown', (event) => {  // 左栏的会话: ↑↓ 换行, Enter 打开 (按钮本来就会)
    if (!['ArrowDown', 'ArrowUp'].includes(event.key) || !event.target.matches('.sess, .wb-recent-row')) return;
    const rows = [...$('#sideScroll').querySelectorAll('.sess, .wb-recent-row')].filter((el) => el.offsetParent);
    const i = rows.indexOf(event.target);
    if (i < 0) return;
    event.preventDefault();
    rows[(i + (event.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length]?.focus();
  });
  $('#emptyExtras').addEventListener('click', (event) => {  // 空会话输入框下面: 建议胶囊填进输入框; "接着上次"打开会话 / 文件
    const b = event.target.closest('button');
    if (!b) return;
    if (b.dataset.say) { $('#input').value = b.dataset.say; autosize(); updateSend(); $('#input').focus(); }
    else if (b.dataset.sidOpen) openSession(b.dataset.sidOpen);
    else if (b.dataset.fileOpen) panel?.openFile(b.dataset.fileOpen);
  });
  initTips();
  $('#pluginsOpen').addEventListener('click', () => panel.openTool('capabilities'));
  $('#automationOpen').addEventListener('click', () => panel.openTool('automation'));
  $('#mailOpen').addEventListener('click', () => panel.openTool('mail'));
  $('#notebookOpen').addEventListener('click', () => panel.openNotebook(S.notebook?.id || ''));  // 资料本: 挂着哪本就直接开到那本
  $('#nbChip').addEventListener('click', () => panel.openNotebook(S.notebook?.id || ''));
  addEventListener('wm-notebook-changed', () => loadNotebook());  // 服务端的 /mail (连接邮箱), 开在右侧面板的标签里
  $('#chat').addEventListener('click', chatCardClick);
  // 输入框里的引用卡片: 从文件几行、网页一节引来的, 点名字回到原处 (照 Lexora 的"快照 + 定位"); 别的照旧打开预览
  $('#contextChips').addEventListener('click', (event) => {
    const chip = event.target.closest('.context-chip');
    if (!chip || !event.target.closest('.context-chip-name')) return;
    if (panel?.revealQuote(chip.title || '')) { event.preventDefault(); event.stopPropagation(); }
  }, true);
  $('#filter').addEventListener('keydown', (e) => { if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); toggleSideSearch(false); } });
  $('#addWs').addEventListener('click', addWorkspace);
  $('#send').addEventListener('click', submit);
  $('#filter').addEventListener('input', (e) => { S.filter = e.target.value; renderSide(); });
  $('#modeBtn').addEventListener('click', (e) => { openMenu('mode'); if (!e.detail && !$('#menu').hidden) $('#menu button')?.focus(); });
  $('#modelBtn').addEventListener('click', (e) => {
    if(!S.state?.models?.length){closeMenu();openSettings('models');return;}
    openMenu('model'); if (!e.detail && !$('#menu').hidden) $('#menu input')?.focus();
  });
  $('#webTog').addEventListener('click', async () => {  // 点了要看得出变了 (2026-11-01 用户: "联网搜索点击后没有反馈")
    const next = cur().web === false;
    try {
      await changeSetting('web', next);
      composerToast(next ? '联网搜索已开：她能上网搜索、读网页（只读公网）' : '联网搜索已关：她只用工作区里的资料');
    } catch (error) { composerToast(`没改成：${error.message}`); }
  });
  $('#sideViewBtn').addEventListener('click', (event) => { event.stopPropagation(); toggleSideView(event.currentTarget); });
  $('#settings').addEventListener('click', (event) => {  // 点弹窗外面的暗处 = 关设置
    if (event.target !== event.currentTarget) return;
    const r = event.currentTarget.getBoundingClientRect();
    if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) closeSettings();
  });
  $('#menu').addEventListener('click', onMenuClick);
  $('#menu').addEventListener('input', (e) => {  // 拖滑条: 边拖边显示是哪一档, 松手再存
    if (e.target.id !== 'effRange') return;
    const k = EFFORT_KEYS[+e.target.value];
    e.target.style.setProperty('--p', `${+e.target.value / (EFFORT_KEYS.length - 1) * 100}%`);
    $('#menu .eff-name').textContent = S.state?.efforts?.[k] || k;
    $('#menu .eff-name').classList.toggle('off', k === 'off');
    $('#menu .eff-desc').textContent = EFFORT_DESC[k] || '';
    document.querySelectorAll('#menu .eff-ticks button').forEach((b) => b.classList.toggle('on', b.dataset.val === k));
  });
  $('#menu').addEventListener('change', (e) => { if (e.target.id === 'effRange') changeSetting('effort', EFFORT_KEYS[+e.target.value]); });
  document.addEventListener('pointerdown', (e) => {
    if (!$('#menu').hidden && !e.target.closest('#menu, #modeBtn, #modelBtn')) closeMenu();
    if (!e.target.closest('.usage')) $('#status details')?.removeAttribute('open');
  });
  addEventListener('resize', () => { if (!$('#menu').hidden) renderMenu(); $('#sideScrim').hidden = !(innerWidth <= 860 && document.querySelector('.app').classList.contains('sidebar-open') && !S.settingsOpen); renderTop(); });
  document.addEventListener('keydown', async (e) => {
    if (e.defaultPrevented || document.querySelector('dialog[open]')) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'n') { e.preventDefault(); if (S.settingsOpen && !await closeSettings()) return; newSession(); return; }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b' && !S.settingsOpen) { e.preventDefault(); setSidebar(!sidebarExpanded()); }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k' && !S.settingsOpen) { e.preventDefault(); setSidebar(true); toggleSideSearch(true); }
    if ((e.ctrlKey || e.metaKey) && e.key === ',') { e.preventDefault(); S.settingsOpen ? closeSettings() : openSettings(); }
    if (e.key === 'Escape' && !$('#menu').hidden) { const kind = $('#menu').dataset.kind; closeMenu(); $(kind === 'mode' ? '#modeBtn' : '#modelBtn').focus(); return; }
    if (!$('#menu').hidden && $('#menu').contains(e.target) && e.target.tagName !== 'INPUT' && ['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(e.key)) {
      e.preventDefault();
      const items = [...$('#menu').querySelectorAll('button:not(:disabled), input:not(:disabled)')];
      const index = items.indexOf(document.activeElement);
      const next = e.key === 'Home' ? 0 : e.key === 'End' ? items.length - 1 : (index + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      items[next]?.focus();
    }
    if (e.key === 'Escape' && S.settingsOpen) closeSettings();
    if (e.key === 'Escape' && document.querySelector('.app').classList.contains('sidebar-open')) setSidebar(false);
  });
  const input = $('#input');
  input.addEventListener('input', () => { autosize(); showSlash(); updateSend(); });
  input.addEventListener('keydown', (e) => {
    if (!e.ctrlKey && !e.altKey && !e.metaKey && (e.key.length === 1 || ['Process', 'Backspace', 'Enter'].includes(e.key))) S.desk?.typed(e.key);
    const open = !$('#slash').hidden;
    if (open && (e.key === 'ArrowDown' || e.key === 'ArrowUp')) {
      e.preventDefault();
      S.slashSel = Math.max(0, S.slashSel + (e.key === 'ArrowDown' ? 1 : -1));
      showSlash();
      return;
    }
    const sendChord = preferences.sendKey === 'ctrlEnter' ? (e.ctrlKey || e.metaKey) : (!e.ctrlKey && !e.metaKey);
    const otherChord = preferences.sendKey === 'ctrlEnter' ? (!e.ctrlKey && !e.metaKey) : (e.ctrlKey || e.metaKey);
    if (e.key === 'Enter' && !e.shiftKey && !e.altKey && !e.isComposing && (sendChord || (S.running && otherChord))) {
      e.preventDefault();
      if (open) { const sel = $('#slash .sel'); if (sel) input.value = sel.dataset.cmd; }
      submit(S.running && otherChord);  // 她忙的时候: 主键加到这一轮, 另一个键做完再发
    }
    if (e.key === 'Escape') hideSlash();
  });
  $('#deskFold').addEventListener('click', () => setDesk(!S.deskOn));
  $('#deskFace').addEventListener('click', switchFace);
  await loadState();
  await loadWallet();
  let herCard = false;
  try { herCard = localStorage.getItem('wm.workbench.herCard') === '1'; } catch { herCard = false; }  // 第二批: 默认收着, 余额在"绫音"一行的提示里
  toggleHerCard(herCard);
  let deskOn = new URLSearchParams(location.search).get('desk') !== '0';
  S.deskPin = new URLSearchParams(location.search).get('deskphase') || '';
  try { deskOn = deskOn && localStorage.getItem('wm.workbench.desk') !== '0'; } catch { /* 无所谓 */ }
  setDesk(deskOn);  // 不等它: 角色加载要一两秒, 会话先出来
  if (new URLSearchParams(location.search).has('settings')) openSettings(new URLSearchParams(location.search).get('settings'));  // 自检截图
  setInterval(loadWallet, 30000);
  setInterval(() => { if (!S.running) loadState().catch(() => {}); }, 15000);
  const params = new URLSearchParams(location.search);
  let want = params.get('s');
  try { want = want || localStorage.getItem('wm.workbench.sid'); } catch { /* 无所谓 */ }
  const all = S.state.workspaces.flatMap((w) => w.sessions);
  if (want && all.some((s) => s.id === want)) await openSession(want);
  else if (all.length && params.get('fresh') !== '1') await openSession(all[0].id);
  else { renderChat(); renderTools(); }
  try {  // 点"有新版本 · 刷新"之前输入框里的字放回来
    const kept = JSON.parse(sessionStorage.getItem('wm.workbench.reloadDraft') || 'null');
    sessionStorage.removeItem('wm.workbench.reloadDraft');
    if (kept?.text && kept.sid === S.sid && !$('#input').value) { $('#input').value = kept.text; autosize(); updateSend(); }
  } catch { /* 无所谓 */ }
  watchPageFiles();
  tell('ready');
}

window.addEventListener('error', (e) => tell('log', { text: `错误 ${e.message} @ ${e.filename}:${e.lineno}` }));
main().catch((err) => {
  $('#chat').innerHTML = `<div class="col"><div class="error" style="margin:0">工作台打不开: ${esc(err.message)}. 服务端开着吗? (重启服务端以后才有工作台)</div></div>`;
  tell('log', { text: `打不开: ${err.message}` });
  tell('ready');
});

// 对话里的图片缩略图: 点一下在页面里放大看原图 (WebView2 里开新窗口不稳, 用浮层)
document.addEventListener('click', (event) => {
  const img = event.target.closest('.msg-attach');
  if (!img) return;
  let box = document.getElementById('imgPreview');
  if (!box) {
    box = document.createElement('div');
    box.id = 'imgPreview';
    box.className = 'img-preview';
    const inner = document.createElement('img');
    box.appendChild(inner);
    box.addEventListener('click', () => { box.hidden = true; });
    document.body.appendChild(box);
  }
  box.querySelector('img').src = img.src;
  box.hidden = false;
});
