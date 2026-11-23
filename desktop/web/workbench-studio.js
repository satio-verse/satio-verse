// The studio uses the same task records as the agent. It never invents success or runs a draft.
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icons = {
  tasks:'<path d="m4 6 2 2 4-4M13 6h7M4 13h2m7 0h7M4 20h2m7 0h7"/>',
  deliveries:'<path d="M4 7h16v14H4ZM3 3h18v4H3Zm6 8h6m-3 0v5m-2-2 2 2 2-2"/>',
  extensions:'<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><path d="M14 17h7m-3.5-3.5v7"/>',
  permissions:'<path d="M12 3 4 6v6c0 5 8 9 8 9s8-4 8-9V6Z"/><path d="m8 12 3 3 5-6"/>',
  growth:'<path d="M4 20V4m0 16h17M8 15l4-5 4 2 5-7"/>',
  file:'<path d="M14 3H5v18h14V8Zm0 0v5h5M8 12h8m-8 4h6"/>',
  link:'<path d="m9 15 6-6m-7 3-2 2a4 4 0 0 0 6 6l3-3m-6-6 3-3a4 4 0 0 1 6 6l-2 2"/>',
  pin:'<path d="M9 3h6l-1 6 4 4H6l4-4Zm3 10v8"/>',
  spark:'<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z"/>',
  team:'<circle cx="9" cy="8" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3m1-16a3 3 0 0 1 0 6m2 4a5 5 0 0 1 3 4v2"/>',
};
const icon = name => `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.file}</svg>`;
const money = value => `¥${(Number(value) || 0).toFixed(2)}`;
const bytes = value => Number(value) >= 1048576 ? `${(Number(value) / 1048576).toFixed(1)} MB` : `${Math.ceil((Number(value) || 0) / 1024)} KB`;
// 输入框上方附件卡写的类型 (引用来的文字段落按"引用"算)
const chipType = item => {
  if (item.kind === 'bilibili') return 'B 站视频';
  if (item.kind !== 'file') return '网页';
  const name = String(item.name || ''), ext = String(item.format || name.split('.').pop() || '').toLowerCase();
  if (/· 引用|第 \d+(–\d+)? 行/.test(name)) return '引用';
  if (/^(png|jpe?g|gif|webp|bmp)$/.test(ext)) return '图片';
  if (ext === 'pdf') return 'PDF';
  if (/^(docx?|rtf|odt)$/.test(ext)) return '文档';
  if (/^(xlsx?|csv|tsv)$/.test(ext)) return '表格';
  if (/^(pptx?)$/.test(ext)) return '演示文稿';
  if (/^(md|txt|log)$/.test(ext)) return '文字';
  if (/^(py|js|mjs|ts|tsx|jsx|json|css|html?|c|cpp|h|cs|java|go|rs|ps1|sh|ya?ml|toml|ini)$/.test(ext)) return '代码';
  return ext ? ext.toUpperCase() : '文件';
};
const date = value => value ? new Date(Number(value) * 1000).toLocaleString('zh-CN', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit'}) : '';
const labels = {running:'进行中',waiting:'等待回应',queued:'待执行',completed:'执行结束',done:'执行结束',idle:'空闲',failed:'执行失败',error:'执行失败',interrupted:'已中断',stopped:'已停止',cancelled:'已取消',budget:'达到预算',budget_exhausted:'达到预算',accepted:'验收通过',rejected:'需要改进',unreviewed:'待验收'};
const status = value => labels[value] || value || '尚未开始';
const modes = {read:'仅可查看',workspace:'工作区内修改',full:'自动执行',unrestricted:'完全授权'};
const kinds = {instruction:'自然语言约束',tool:'工具',path:'文件与目录',command:'命令片段',domain:'网站域名'};
const tag = (text, tone='') => `<span class="studio-tag ${tone}">${escapeHTML(text)}</span>`;
const empty = (title, text='') => `<div class="studio-empty">${icon('spark')}<strong>${escapeHTML(title)}</strong>${text ? `<p>${escapeHTML(text)}</p>` : ''}</div>`;
const button = (action, text, attrs='', cls='') => `<button type="button" class="studio-button ${cls}" data-action="${action}" ${attrs}>${text}</button>`;
const title = (text, sub='', action='') => `<div class="studio-section-head"><div><h3>${text}</h3>${sub ? `<p>${sub}</p>` : ''}</div>${action}</div>`;
const array = value => Array.isArray(value) ? value : [];

export function initStudio({api, getSession, ensureSession, openSession, notify, setDraft, onPlan, getNativeAuthority=() => false, authorityChanged}) {
  const $ = id => document.getElementById(id);
  const dialog = $('studioDialog'), body = $('studioBody'), contextDialog = $('contextDialog');
  if (!dialog || !body || !contextDialog) return {refresh(){},contextIds:()=>[],sent(){},open(){},ready:async()=>{},pending:()=>false};
  let sid = null, data = null, tab = 'tasks', loadedAt = 0, loadSequence = 0, loading = null, loadError = '', ruleEditId = '', mutating = 0;
  const selected = new Map(), initializedSelections = new Set(), drafts = new Map(), scrolls = new Map(), disclosures = new Map(), uploads = new Set(), references = new Set(), uploadFailures = [];
  let contexts = [], diffResult = null, previewKey = '', previewHTML = '', dragDepth = 0, sessionPromise = null, growthReport = null, deliveryRunId = '', deliveryFresh = false;
  const readers = {context:null,delivery:null};
  let readerSequence = 0, ruleQuery = '', ruleKind = '';
  // 第六轮第三批: 开在右侧面板的标签里 (host 由工作台给: show 开标签、shown 现在看得见没有、moved 换到任务 / 成果哪一页); 没有 host 时照旧弹窗
  let host = null, deliveryView = 'list';
  try { deliveryView = localStorage.getItem('wm.studio.deliveryView') === 'grid' ? 'grid' : 'list'; } catch { /* 默认列表 */ }
  const shown = () => dialog.open || !!host?.shown?.();
  const contextMenu = document.createElement('div');
  contextMenu.id = 'contextAddMenu';contextMenu.className = 'context-add-menu';contextMenu.hidden = true;
  contextMenu.setAttribute('role','menu');contextMenu.setAttribute('aria-label','添加任务内容');
  document.body.append(contextMenu);
  $('contextAdd').setAttribute('aria-haspopup','menu');$('contextAdd').setAttribute('aria-controls',contextMenu.id);$('contextAdd').setAttribute('aria-expanded','false');
  const sessionId = () => getSession()?.id || '';
  const reduceMotion = () => document.documentElement.dataset.wmMotion === 'reduce' || matchMedia('(prefers-reduced-motion: reduce)').matches;
  const picks = () => {if (!selected.has(sid)) selected.set(sid, new Set());return selected.get(sid);};
  const announce = message => {if ($('studioFeedback')) $('studioFeedback').textContent = message;if (contextDialog.open && $('contextFeedback')) $('contextFeedback').textContent = message;notify?.(message);};
  const formKey = () => `${sid}:${tab}`;
  const disclosureKey = (element, seen) => {
    if (element.dataset.disclosureKey) return element.dataset.disclosureKey;
    const label = element.querySelector('summary')?.textContent.trim() || '';
    const index = seen.get(label) || 0;seen.set(label,index+1);return `${label}:${index}`;
  };
  const stash = () => {
    const values = {};
    body.querySelectorAll('[data-draft]').forEach(el => values[el.dataset.draft] = el.type === 'checkbox' ? el.checked : el.value);
    if (Object.keys(values).length) drafts.set(formKey(), values);
    const opened = {}, seen = new Map();body.querySelectorAll('details').forEach(el => {opened[disclosureKey(el,seen)] = el.open;});
    disclosures.set(formKey(),opened);
  };
  const restore = () => {
    const values = drafts.get(formKey()) || {};
    body.querySelectorAll('[data-draft]').forEach(el => {if (el.dataset.draft in values) {if (el.type === 'checkbox') el.checked = values[el.dataset.draft];else el.value = values[el.dataset.draft];}});
    const opened = disclosures.get(formKey()) || {}, seen = new Map();
    body.querySelectorAll('details').forEach(el => {const key = disclosureKey(el,seen);if (opened[key] !== undefined) el.open = opened[key];});
  };
  const request = async (url, options) => {
    const response = await fetch(url, options);
    const result = await response.json();
    if (!response.ok || result.ok === false || result.error && !result.blacklist) {const error = new Error(result.error || `请求失败 (${response.status})`);error.status = response.status;throw error;}
    return result;
  };
  const syncContexts = items => {
    contexts = array(items);
    if (sid && !initializedSelections.has(sid)) {
      const session = getSession(), available = new Set(contexts.map(item => item.id));
      // A new fork inherits its last task's exact material snapshots, not its whole library.
      if (session?.id === sid && session.forked_from && !session.run?.id) {
        array(session.materials).filter(item => item.owner === sid && available.has(item.id)).forEach(item => picks().add(item.id));
      }
      initializedSelections.add(sid);
    }
    contexts.filter(x => x.pinned).forEach(x => picks().add(x.id));
    renderContexts();
  };
  async function load(force=false, preserve=true) {
    const current = sessionId();
    if (sid !== current) {
      closeContextMenu(false);stash();sid = current;data = null;contexts = [];loadedAt = 0;loading = null;ruleEditId = '';diffResult = null;previewKey = '';previewHTML = '';growthReport = null;deliveryRunId = '';deliveryFresh = false;readers.context = null;readers.delivery = null;readerSequence++;
      loadSequence++;renderContexts();
    }
    if (!current) return;
    if (!force && loading) return loading;
    if (!force && Date.now() - loadedAt < 2500) return;
    if (!force && data && !shown() && !contextDialog.open) return;
    const sequence = ++loadSequence;
    const refreshDeliveries = tab === 'deliveries';
    const work = (async () => {
      try {
        const result = await request(`/api/agent/studio?id=${encodeURIComponent(current)}${deliveryRunId ? `&run_id=${encodeURIComponent(deliveryRunId)}` : ''}${refreshDeliveries ? '&deliveries=1' : ''}`);
        if (sequence !== loadSequence || current !== sessionId()) return;
        if (preserve) stash();data = result;deliveryFresh = refreshDeliveries;loadedAt = Date.now();loadError = '';syncContexts(result.contexts);
        if (refreshDeliveries && readers.delivery?.item?.current?.sha256) {
          const latest = array(result.deliveries?.artifacts).find(item => item.id === readers.delivery.id);
          if (!latest || latest.current?.sha256 !== readers.delivery.item.current.sha256) readers.delivery = {...readers.delivery,item:latest || readers.delivery.item,text:undefined,nextOffset:null,error:'文件状态已变化，请重新读取并核对当前内容。'};
        }
        if (growthReport) growthReport = array(result.growth?.reports).find(report => report.id === growthReport.id) || growthReport;
        // Polling must not move the caret, close a disclosure, or reload a preview.
        const editing = body.contains(document.activeElement) && document.activeElement.matches('input,textarea,select,iframe');
        if (shown() && !editing && !mutating) render();
      } catch (error) {
        if (sequence !== loadSequence) return;
        loadError = error.message;if (shown() && !data) render();
        $('contextComposerStatus').textContent = '内容未同步';
      } finally {if (sequence === loadSequence) loading = null;}
    })();
    loading = work;
    return work;
  }
  async function post(action, extra={}, requestedId=sessionId()) {
    return request('/api/agent/studio', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action,id:requestedId,...extra,...(['rule_add','rule_update','rule_remove'].includes(action) ? {expected_revision:data?.authority?.revision} : {})})});
  }
  async function requireSession() {
    if (!getSession()?.id && !sessionPromise) sessionPromise = Promise.resolve(ensureSession()).finally(() => {sessionPromise = null;});
    const session = getSession()?.id ? getSession() : await sessionPromise;
    if (!session?.id) throw new Error('请先选择一个会话');
    if (sid !== session.id) await load(true);
    return session;
  }
  function showDialog(target) {if (!target.open) target.showModal();}
  async function open(next='tasks') {
    closeContextMenu(false);
    if (!['tasks','deliveries','extensions','permissions','growth'].includes(next)) next = 'tasks';
    try {await requireSession();} catch (error) {announce(error.message);return;}
    if (host) {host.show(next);return;}  // 右侧面板: 面板把标签切到前面以后回头调 display()
    stash();scrolls.set(formKey(),body.scrollTop);tab = next;if (!mutating) $('studioFeedback').textContent = '进度和结果来自实际任务记录';showDialog(dialog);render();body.scrollTop = scrolls.get(formKey()) || 0;load(true);
  }
  function display(next = tab) {  // 面板里的"任务与成果"标签切到前面时调: 画这一页、拉一次最新的
    if (!['tasks','deliveries','extensions','permissions','growth'].includes(next)) next = 'tasks';
    stash();scrolls.set(formKey(),body.scrollTop);tab = next;if (!mutating) $('studioFeedback').textContent = '进度和结果来自实际任务记录';render();body.scrollTop = scrolls.get(formKey()) || 0;load(true);
  }
  function renderTabs() {
    $('studioTabs').innerHTML = Object.entries({tasks:'任务',deliveries:'成果',extensions:'扩展',permissions:'权限',growth:'成长'}).map(([id,label]) => `<button type="button" role="tab" id="studioTab-${id}" aria-controls="studioBody" aria-selected="${tab === id}" tabindex="${tab === id ? 0 : -1}" data-tab="${id}">${icon(id)}${label}</button>`).join('');
    body.setAttribute('aria-labelledby',`studioTab-${tab}`);
  }
  function render() {
    renderTabs();
    $('studioSubtitle').textContent = getSession()?.title || '一起把想法变成成果';
    if (!data) {body.innerHTML = loadError ? empty('暂时无法同步', loadError) + button('reload','重试') : '<div class="studio-loading" role="status">正在同步任务与能力…</div>';return;}
    const top = loadError ? `<div class="studio-alert">上次同步失败：${escapeHTML(loadError)} ${button('reload','重试')}</div>` : '';
    body.innerHTML = top + ({tasks:renderTasks,deliveries:renderDeliveries,extensions:renderExtensions,permissions:renderPermissions,growth:renderGrowth}[tab])();
    restore();
    if (tab === 'permissions') updateRuleHint();
    if (tab === 'extensions' && previewKey) mountPreview();
    if (tab === 'growth') updateEvalRevisions();
  }
  // 任务页 (第六轮第三批, 学 Linear 的事项详情): 标题 + 一行属性 (状态、开始、用时、花费、步数、改了几个文件) + 这一轮的经过 (时间线);
  // 分工协作并进了协作任务的看板, 这里只写几个、给入口 (有独立工作副本的子任务照旧能看差异、导出补丁)
  function runFlow(session) {
    const events = array(session?.events), items = [], files = new Set();
    let start = -1;
    for (let i = events.length - 1; i >= 0; i--) if (events[i].type === 'user' && !events[i].steer) { start = i; break; }
    if (start < 0) return { items, files: [], took: '' };
    const clock = t => (t ? new Date(Number(t) * (Number(t) < 1e12 ? 1000 : 1)).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : '');
    const WORD = { read_file: ['读了', '个文件'], list_dir: ['看了', '个目录'], glob: ['找了', '次文件'], grep: ['搜了', '次'], write_file: ['写了', '个文件'], edit_file: ['改了', '个文件'],
      run_command: ['跑了', '条命令'], web_search: ['上网搜了', '次'], web_fetch: ['读了', '个网页'], notebook_search: ['查了', '次资料本'], notebook_read: ['读了', '块资料'] };
    const user = events[start];
    items.push({ text: '你交代', sub: String(user.text || '').replace(/\s+/g, ' ').slice(0, 90), at: clock(user.t) });
    let group = null, done = null;
    const flush = () => {
      if (!group) return;
      const parts = Object.entries(group.counts).map(([name, n]) => WORD[name] ? `${WORD[name][0]} ${n} ${WORD[name][1]}` : `用了 ${n} 次${name}`);
      items.push({ text: parts.join('，'), sub: group.what.slice(0, 3).join('、'), at: clock(group.t) });
      group = null;
    };
    for (const e of events.slice(start + 1)) {
      if (e.type === 'tool') {
        group ||= { counts: {}, what: [], t: e.t };
        group.counts[e.name] = (group.counts[e.name] || 0) + 1;
        const what = String(e.desc || e.args || '').split('  (')[0].replace(/\\/g, '/').split('/').pop();
        if (what && !group.what.includes(what)) group.what.push(what);
      } else if (e.type === 'tool_result' && e.change?.path) files.add(String(e.change.path).replace(/\\/g, '/').toLowerCase());
      else if (e.type === 'assistant') { flush(); items.push({ text: '她回复', sub: String(e.text || '').replace(/\s+/g, ' ').slice(0, 90), at: clock(e.t) }); }
      else if (e.type === 'check') { flush(); items.push({ text: e.stage === 'final' ? '程序核对没过' : '程序核对', sub: array(e.check?.reasons).join('；').slice(0, 90), at: clock(e.t), cls: 'warn' }); }
      else if (e.type === 'done') {
        flush(); done = e;
        const verdict = e.run?.check?.verdict === 'needs_check' ? '说做完了，但待核查' : ({ completed: '做完了', cancelled: '停下了', budget_exhausted: '到预算了', failed: '没做成', interrupted: '中断了' })[e.run?.state] || '这一轮结束';
        items.push({ text: verdict, at: clock(e.t), cls: e.run?.check?.verdict === 'needs_check' || ['failed', 'interrupted'].includes(e.run?.state) ? 'warn' : 'ok' });
      }
    }
    flush();
    const span = done && Number(done.t) > Number(user.t) ? Number(done.t) - Number(user.t) : 0;
    const took = span ? (span >= 60 ? `${Math.floor(span / 60)} 分 ${Math.round(span % 60)} 秒` : `${Math.max(1, Math.round(span))} 秒`) : '';
    return { items: items.slice(-14), files: [...files], took };
  }
  function renderTasks() {
    const run = data.run || {}, session = getSession() || {}, team = array(data.team?.children || data.team), plan = data.plan || {}, queue = array(data.queue);
    const active = ['running','waiting','queued'].includes(session.status);
    const resume = run.id && !active && !['completed','done'].includes(run.state);
    const progress = array(plan.steps), done = progress.filter(x => x.status === 'done').length;
    const flow = runFlow(session), state = run.state || session.status;
    const warn = run.check?.verdict === 'needs_check' || ['failed', 'interrupted'].includes(state);
    const props = [['状态', tag(status(state), active ? 'active' : warn ? 'warn' : '')], ['开始', run.started ? escapeHTML(date(run.started)) : '—'],
      ['用时', escapeHTML(flow.took || (active ? '在做' : '—'))], ['花费', run.id ? escapeHTML(money(run.cost ?? session.stats?.cost)) : '—'],
      ['步数', run.id ? `${Number(run.steps ?? session.stats?.steps) || 0} 步` : '—'], ['改了', flow.files.length ? `${flow.files.length} 个文件` : '没改文件']];
    const counts = { running: 0, waiting: 0, done: 0 };
    for (const child of team) counts[['running', 'queued', 'preparing'].includes(child.status) ? 'running' : child.status === 'waiting' ? 'waiting' : 'done']++;
    const worktrees = team.filter(child => child.worktree_id);
    return `<section class="st-issue"><div class="st-issue-top"><span class="studio-eyebrow">当前任务</span><span class="st-grow"></span>${resume ? button('resume','继续任务','','primary') : ''}</div>
        <h3>${escapeHTML(run.goal || '把下一件事交给她')}</h3>${run.reason ? `<p class="st-why">${escapeHTML(run.reason)}</p>` : ''}
        <dl class="st-props">${props.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join('')}</dl></section>
      ${flow.items.length ? `<section>${title('经过', '从你这一轮交代起')}<ol class="st-timeline">${flow.items.map(item => `<li class="${item.cls || ''}"><span class="st-dot" aria-hidden="true"></span><div><strong>${escapeHTML(item.text)}</strong>${item.sub ? `<small>${escapeHTML(item.sub)}</small>` : ''}</div><time>${escapeHTML(item.at)}</time></li>`).join('')}</ol></section>` : ''}
      ${progress.length ? `<section>${title('执行计划',`${done} / ${progress.length} 项完成`)}<ol class="studio-plan">${progress.map(step => `<li class="${step.status === 'done' ? 'done' : ''}"><span class="studio-plan-mark">${step.status === 'done' ? '✓' : step.status === 'doing' ? '●' : '○'}</span><span>${escapeHTML(step.title)}</span>${step.status === 'doing' ? tag(['running','waiting','queued'].includes(run.state || session.status) ? '正在做' : '中断于此','active') : ''}</li>`).join('')}</ol>${plan.acceptance ? `<p class="studio-note">验收标准：${escapeHTML(plan.acceptance)}</p>` : ''}</section>` : ''}
      <div class="studio-delivery-entry"><span class="studio-row-icon">${icon('deliveries')}</span><div class="studio-flex-copy"><strong>任务成果</strong><small>${(data.delivery_run?.id || run.id) === run.id && array(data.deliveries?.artifacts).length ? `${array(data.deliveries.artifacts).length} 个文件 · 可查看内容与交付时的记录` : '查看交付文件、执行证据和验收记录'}</small></div>${button('deliveries-current','查看成果')}</div>
      <section>${title('分工协作', team.length ? `${team.length} 个子任务 · 在做 ${counts.running} · 等你 ${counts.waiting} · 做完 ${counts.done}` : '复杂任务可以拆成几个子任务，各自记录过程，做完汇回这里。', button('board', '在协作看板里看') + button('delegate-draft', `${icon('team')} 拆分任务`))}
        ${worktrees.length ? `<div class="studio-list">${worktrees.map(child => `<article class="studio-row"><div class="studio-row-icon">${icon('team')}</div><div class="studio-flex-copy"><strong>${escapeHTML(child.title || child.goal || '子任务')}</strong><div class="studio-meta">${tag(status(child.status))}${tag('独立工作副本')}</div><div class="studio-actions">${button('worktree-diff','查看差异',`data-id="${escapeHTML(child.worktree_id)}"`)}${button('worktree-export','导出补丁',`data-id="${escapeHTML(child.worktree_id)}"`)}<small>尚未合入原工作区</small></div></div>${button('open-child','打开',`data-id="${escapeHTML(child.id)}"`)}</article>`).join('')}</div>` : ''}
        ${diffResult ? `<details class="studio-diff" open><summary>工作副本差异 ${diffResult.truncated ? '（已截断）' : ''}</summary><pre>${escapeHTML(diffResult.text || '没有发现文本差异')}</pre></details>` : ''}</section>
      <section>${title('接下来做','提交后会排队；当前空闲时立即开始。')}<div class="studio-list">${queue.map(item => `<article class="studio-row"><span class="studio-queue-dot"></span><div class="studio-flex-copy"><strong>${escapeHTML(item.text)}</strong><small>${date(item.created)}</small></div>${button('queue-remove','移除',`data-id="${escapeHTML(item.id)}"`)}</article>`).join('')}</div><form id="studioQueueForm" class="studio-form studio-queue-form"><label class="sr-only" for="studioQueueText">下一项任务</label><textarea id="studioQueueText" data-draft="queue" rows="2" required maxlength="60000" placeholder="补充一项任务，比如：完成后再检查边界情况"></textarea><div class="studio-form-end"><small>附带当前选中的 ${contextIds().length} 份内容</small><button class="studio-button" type="submit">加入队列</button></div></form></section>
      <details class="studio-budget"><summary>${icon('permissions')} 任务预算 <span>按需要设置</span></summary><form id="studioBudgetForm" class="studio-form"><div class="studio-form-grid"><label>费用上限（元）<input data-draft="budget" name="budget_cny" type="number" min="0" max="100000" step="0.01" value="${Number(session.budget_cny)||0}" required></label><label>时间上限（秒）<input data-draft="time" name="time_budget_s" type="number" min="0" max="86400" step="1" value="${Number(session.time_budget_s)||0}" required></label><label>工具步数<input data-draft="steps" name="max_steps" type="number" min="0" max="10000" step="1" value="${Number(session.max_steps)||0}" required></label></div><div class="studio-form-end"><small>0 表示不另设上限。子任务仍共享父任务预算。</small><button class="studio-button" ${active || session.parent_id ? 'disabled' : ''}>保存预算</button></div></form></details>`;
  }
  function renderDeliveries() {
    const run = data.delivery_run || data.run || {}, allRuns = array(data.delivery_runs), delivery = data.deliveries || {};
    const artifacts = array(delivery.artifacts), evidence = array(delivery.evidence), reviews = array(delivery.reviews);
    const lastReview = [...reviews].sort((a,b) => Number(b.created || b.reviewed || 0) - Number(a.created || a.reviewed || 0))[0];
    const artifactVersions = Object.fromEntries(artifacts.map(item => [item.id,item.current?.sha256 || null]));
    const unavailableFiles = artifacts.some(item => !item.current?.sha256);
    const active = run.id === data.run?.id && ['running','waiting','queued'].includes(getSession()?.status);
    const runOptions = allRuns.length ? allRuns : run.id ? [run] : [];
    if (!deliveryFresh || deliveryRunId && run.id !== deliveryRunId) return '<div class="studio-loading" role="status">正在读取并核对所选任务的成果…</div>';
    return `<section class="studio-delivery-heading"><div><span class="studio-eyebrow">可检查的交付</span><h3>成果与验收</h3><p>文件、执行记录与验收反馈都对应具体任务。</p></div><label>任务<select id="studioDeliveryRun" aria-label="选择成果所属任务">${runOptions.map(item => `<option value="${escapeHTML(item.id)}" ${item.id === run.id ? 'selected' : ''}>${escapeHTML(item.goal || item.title || '任务')} · ${date(item.started)}</option>`).join('')}</select></label></section>
      ${run.id ? `<section class="studio-delivery-summary"><div class="studio-meta">${tag(status(run.state))}${tag(lastReview?.stale ? '验收对应旧快照' : status(run.verification || 'unreviewed'),lastReview?.stale ? 'warn' : run.verification === 'accepted' ? 'ok' : '')}<span>${date(run.started)}</span></div><h3>${escapeHTML(run.goal || run.title || '任务')}</h3>${lastReview?.stale ? '<p class="studio-reader-warning">验收后的文件发生变化或无法核对，请检查当前内容后重新验收。</p>' : ''}${run.summary || run.result ? `<details class="studio-details"><summary>交付说明</summary><p>${escapeHTML(run.summary || run.result)}</p></details>` : ''}</section>` : ''}
      <section>${title('交付文件',`${artifacts.length} 个文件 · 状态按当前文件核对`,artifacts.length ? `<span class="st-seg" role="group" aria-label="交付文件怎么排">${[['list','列表'],['grid','网格']].map(([v,l]) => `<button type="button" data-action="delivery-view" data-view="${v}" aria-pressed="${deliveryView === v}">${l}</button>`).join('')}</span>` : '')}<div class="studio-list studio-delivery-files ${deliveryView === 'grid' ? 'grid' : ''}">${artifacts.length ? artifacts.map(item => {
        const snapshot = array(lastReview?.artifact_snapshots).find(saved => saved.id === item.id);
        const sinceReview = !!(snapshot?.sha256 && item.current?.sha256 && snapshot.sha256 !== item.current.sha256);
        return `<article class="studio-delivery-file"><div class="studio-row"><span class="studio-row-icon">${icon('file')}</span><div class="studio-flex-copy"><button type="button" class="studio-file-name" data-action="delivery-preview" data-id="${escapeHTML(item.id)}" aria-expanded="${readers.delivery?.id === item.id}" aria-controls="studioReader-delivery">${escapeHTML(item.label || item.name || '文件')}</button><div class="studio-meta">${tag(({current:'与登记时一致',changed:'登记后已变化',missing:'文件已丢失',unavailable:'暂时无法核对'})[item.status] || '尚未核对',item.status === 'current' ? 'ok' : item.status === 'changed' || item.status === 'missing' ? 'warn' : '')}<span>${escapeHTML((item.kind || 'file').toUpperCase())} · ${bytes(item.current?.size ?? item.registered?.size)}</span>${sinceReview ? tag('验收后已变化','warn') : ''}</div><span class="studio-file-path">${escapeHTML(item.path)}</span>${item.updated ? `<small>登记更新于 ${date(item.updated)}${array(item.versions).length ? ` · 保留最近 ${array(item.versions).length + 1} 个登记版本` : ''}</small>` : ''}${item.error ? `<p class="studio-reader-error">${escapeHTML(item.error)}</p>` : ''}</div>${button('delivery-copy','复制路径',`data-id="${escapeHTML(item.id)}"`)}</div>${readers.delivery?.id === item.id ? renderReader('delivery') : ''}</article>`;
      }).join('') : empty('这项任务尚未登记交付文件','有实际产物时，她会将文件登记到所属任务中。')}</div></section>
      <section>${title('执行证据',`${evidence.length} 条记录 · 命令退出结果不等于人工验收通过`)}<div class="studio-list">${evidence.length ? evidence.map(record => `<details class="studio-delivery-evidence"><summary><span class="studio-evidence-mark ${record.status === 'failed' ? 'failed' : ''}">${record.status === 'succeeded' ? '✓' : record.status === 'failed' ? '×' : '○'}</span><span class="studio-flex-copy"><strong>${escapeHTML(record.command || record.tool || '工具执行')}</strong><small>${record.status === 'succeeded' ? `命令执行结束 · 退出码 ${Number.isInteger(record.exit_code) ? record.exit_code : '未提供'}` : record.status === 'failed' ? `执行失败${Number.isInteger(record.exit_code) ? ` · 退出码 ${record.exit_code}` : ''}` : '执行未完成'} · ${date(record.created)}</small></span></summary><pre>${escapeHTML(record.output || '没有输出记录')}</pre></details>`).join('') : empty('尚无可核对的执行记录','这里仅展示系统记录的工具结果。')}</div></section>
      ${run.id ? `<section>${title('验收这次交付','反馈只绑定上方选中的任务，以及验收时的文件快照。')}<form id="studioDeliveryReviewForm" class="studio-form" data-run-id="${escapeHTML(run.id)}"><input type="hidden" name="artifact_versions" value="${escapeHTML(JSON.stringify(artifactVersions))}"><label>反馈<textarea name="note" rows="3" maxlength="2000" data-draft="delivery-review-${escapeHTML(run.id)}" placeholder="检查文件和证据后，记录哪些达标、哪些需要改进。">${escapeHTML(run.review_note || lastReview?.note || '')}</textarea></label><div class="studio-form-end"><small>${active ? '任务执行结束后可验收' : unavailableFiles ? '部分文件缺失或无法核对，恢复并刷新后可验收通过' : '文件后续变化时，会显示变化状态'}</small><div class="studio-actions"><button type="submit" name="accepted" value="false" class="studio-button" ${active ? 'disabled' : ''}>需要改进</button><button type="submit" name="accepted" value="true" class="studio-button primary" ${active || unavailableFiles ? 'disabled' : ''}>验收通过</button></div></div></form></section>` : ''}
      ${reviews.length ? `<section>${title('验收记录')}<div class="studio-list">${[...reviews].reverse().map(review => `<article class="studio-row"><span class="studio-record-mark ${review.accepted && !review.stale ? 'ok' : ''}">${review.accepted ? '✓' : '↺'}</span><div class="studio-flex-copy"><strong>${review.accepted ? '用户验收通过' : '用户反馈需要改进'}${review.stale ? ' · 对应旧快照' : ''}</strong><small>${date(review.created || review.reviewed)} · ${array(review.artifact_snapshots).length} 份文件快照${review.stale ? ' · 当前文件需重新核对' : ''}</small>${review.note ? `<p>${escapeHTML(review.note)}</p>` : ''}</div></article>`).join('')}</div></section>` : ''}`;
  }
  function materialState(item) {
    const state = item.parse_status || (item.readable ? 'ready' : 'stored');
    return {state,label:({stored:'已保存 · 尚未解析',ready:'文本已提取',partial:'部分文本已提取',image:'图片 · 会直接看图',needs_ocr:'需要文字识别',unavailable:'解析组件不可用',unsupported:'暂不支持解析',error:'解析失败'})[state] || '尚未解析',tone:state === 'ready' || state === 'image' ? 'ok' : ['partial','needs_ocr','unavailable','error'].includes(state) ? 'warn' : ''};
  }
  function renderReader(scope) {
    const state = readers[scope];if (!state) return '';
    const item = state.item || {}, info = materialState(item), busy = state.loading;
    const pages = [...new Set(array(item.sections).map(section => section.page).filter(value => Number.isInteger(value)))];
    const sheets = array(item.sheets), choices = pages.map(page => ({value:`page:${page}`,label:`第 ${page} 页`})).concat(sheets.map(sheet => ({value:`sheet:${sheet.name}`,label:`工作表 · ${sheet.name}`})));
    const selection = state.page ? `page:${state.page}` : state.sheet ? `sheet:${state.sheet}` : '';
    const metadataOnly = scope === 'delivery' && item.kind !== 'text';
    const path = scope === 'delivery' ? item.path : '';
    const flags = [...array(item.warnings),...(item.preview?.encoding_note ? [item.preview.encoding_note] : [])].map(warning => `<p class="studio-reader-warning">${escapeHTML(warning)}</p>`).join('');
    const readError = state.error || item.error;
    return `<section id="studioReader-${scope}" class="studio-reader" aria-label="${escapeHTML(item.name || '内容')}只读预览" aria-busy="${!!busy}"><header><div class="studio-flex-copy"><strong>${escapeHTML(item.label || item.name || state.name || '内容预览')}</strong><div class="studio-meta">${tag('只读预览')}${scope === 'context' && item.kind === 'file' ? tag(info.label,info.tone) : ''}${item.pages ? `<span>${pages.length && pages.length < item.pages ? `已提取 ${pages.length} / ${item.pages} 页` : `${item.pages} 页`}</span>` : ''}${sheets.length ? `<span>${item.sheet_count && sheets.length < item.sheet_count ? `已提取 ${sheets.length} / ${item.sheet_count} 个工作表` : `${sheets.length} 个工作表`}</span>` : ''}${item.chars !== undefined ? `<span>${Number(item.chars)||0} 字符已提取</span>` : ''}</div></div><button type="button" class="studio-icon-button" data-reader-action="close" data-scope="${scope}" aria-label="关闭内容预览">×</button></header>
      ${choices.length ? `<label class="studio-reader-source">来源<select data-reader-action="source" data-scope="${scope}" aria-label="选择来源页或工作表" ${busy ? 'disabled' : ''}><option value="">全部已提取内容</option>${choices.map(choice => `<option value="${escapeHTML(choice.value)}" ${choice.value === selection ? 'selected' : ''}>${escapeHTML(choice.label)}</option>`).join('')}</select></label>` : ''}
      ${busy ? '<p class="studio-reader-status" role="status">正在读取内容…</p>' : ''}${readError ? `<div class="studio-reader-error" role="alert">${escapeHTML(readError)} <button type="button" class="studio-button" data-reader-action="reload" data-scope="${scope}" ${busy ? 'disabled' : ''}>重新读取</button></div>` : ''}${flags}${item.truncated ? '<p class="studio-reader-warning">仅提取了部分内容，预览不代表完整文件。</p>' : ''}
      ${metadataOnly ? `<div class="studio-reader-metadata"><p>${escapeHTML((item.kind || 'file').toUpperCase())} 文件 · ${escapeHTML(item.mime || '二进制内容')}</p><p>此处展示文件状态与路径，请使用对应查看器核对原文件。</p><code>${escapeHTML(path)}</code></div>` : item.kind && item.kind !== 'file' && scope === 'context' ? `<div class="studio-reader-metadata"><p>${item.kind === 'bilibili' ? 'B 站视频引用' : '网页引用'} · 内容尚需读取</p><code>${escapeHTML(item.url || '')}</code><p>锁定引用不代表网页或视频已经解析。</p></div>` : state.text !== undefined ? `<pre class="studio-reader-text" tabindex="0" aria-label="提取的只读文本">${escapeHTML(state.text || '没有可显示的文本')}</pre>` : !busy && !readError ? '<p class="studio-reader-status">没有可预览的文本，请查看解析状态。</p>' : ''}
      ${state.text !== undefined && !metadataOnly ? `<footer><span>${state.offset ? `从第 ${state.offset + 1} 个字符开始` : '从内容开头开始'}${state.nextOffset != null ? ' · 后续还有内容' : ' · 本段结束'}</span><div class="studio-actions"><button type="button" class="studio-button" data-reader-action="previous" data-scope="${scope}" ${busy || !state.history?.length ? 'disabled' : ''}>上一段</button><button type="button" class="studio-button" data-reader-action="next" data-scope="${scope}" ${busy || state.nextOffset == null ? 'disabled' : ''}>下一段</button></div></footer>` : ''}</section>`;
  }
  function renderReaderHost(scope) {
    if (scope === 'context') renderContexts();else if (tab === 'deliveries') render();
  }
  async function readContent(scope, id, {offset=0,page=null,sheet=null,history=[],focus=false}={}) {
    const targetSession = sessionId(), targetRun = data?.delivery_run?.id || data?.run?.id || '';
    const token = ++readerSequence, previous = readers[scope];
    const existing = previous?.id === id ? previous.item : scope === 'context' ? contexts.find(item => item.id === id) : array(data?.deliveries?.artifacts).find(item => item.id === id);
    readers[scope] = {id,item:existing || {},loading:true,offset,page,sheet,history,token,runId:targetRun};renderReaderHost(scope);
    try {
      let response;
      if (scope === 'context') {
        const inspected = await post('context_inspect',{context_id:id},targetSession);
        if (readers[scope]?.token !== token || targetSession !== sessionId()) return;
        readers[scope].item = inspected.item;
        const canRead = inspected.item?.kind === 'file' && (['ready','partial'].includes(inspected.item.parse_status) || inspected.item.readable);
        response = canRead ? await post('context_read',{context_id:id,offset,limit:6000,...(page ? {page} : {}),...(sheet ? {sheet} : {})},targetSession) : {item:inspected.item};
      } else response = await post('delivery_read',{artifact_id:id,run_id:targetRun,offset,limit:6000},targetSession);
      if (readers[scope]?.token !== token || targetSession !== sessionId() || scope === 'delivery' && targetRun !== (data?.delivery_run?.id || data?.run?.id || '')) return;
      const item = scope === 'context' ? response.item : response.artifact, preview = scope === 'context' ? response : item?.preview;
      readers[scope] = {...readers[scope],item:item || {},loading:false,text:preview?.text,offset:Number(preview?.offset ?? offset),nextOffset:preview?.next_offset == null ? null : Number(preview.next_offset)};
      if (scope === 'context') contexts = contexts.map(candidate => candidate.id === id ? {...candidate,...item} : candidate);
      else if (data.deliveries?.artifacts) data.deliveries.artifacts = data.deliveries.artifacts.map(candidate => candidate.id === id ? {...candidate,...item} : candidate);
    } catch (error) {
      if (readers[scope]?.token !== token || targetSession !== sessionId()) return;
      readers[scope] = {...readers[scope],loading:false,error:error.message};
    }
    renderReaderHost(scope);
    if (focus) {
      const panel = $(`studioReader-${scope}`);panel?.scrollIntoView({block:'nearest',behavior:reduceMotion() ? 'auto' : 'smooth'});
      panel?.querySelector('.studio-reader-text, [data-reader-action=close]')?.focus({preventScroll:true});
    }
  }
  async function readerAction(event) {
    const target = event.target.closest('[data-reader-action]');if (!target || target.disabled) return;
    if (target.tagName === 'SELECT' && event.type !== 'change') return;
    const scope = target.dataset.scope, state = readers[scope];if (!state) return;
    const action = target.dataset.readerAction;
    if (action === 'close') {const id = state.id;readers[scope] = null;renderReaderHost(scope);(scope === 'context' ? $('contextLibrary') : body).querySelector(`[data-id="${CSS.escape(id)}"][data-${scope === 'context' ? 'context-action' : 'action'}="${scope === 'context' ? 'preview' : 'delivery-preview'}"]`)?.focus({preventScroll:true});return;}
    let offset = 0, page = state.page, sheet = state.sheet, history = [...(state.history || [])];
    if (action === 'next') {if (state.nextOffset == null) return;offset = state.nextOffset;history.push(state.offset);}
    else if (action === 'previous') {if (!history.length) return;offset = history.pop();}
    else if (action === 'reload') offset = state.offset || 0;
    else if (action === 'source') {page = target.value.startsWith('page:') ? Number(target.value.slice(5)) : null;sheet = target.value.startsWith('sheet:') ? target.value.slice(6) : null;history = [];}
    await readContent(scope,state.id,{offset,page,sheet,history,focus:action !== 'source'});
    if (action === 'source') $(`studioReader-${scope}`)?.querySelector('[data-reader-action=source]')?.focus({preventScroll:true});
  }
  function closeContextMenu(restoreFocus=true) {
    contextMenu.hidden = true;$('contextAdd').setAttribute('aria-expanded','false');
    if (restoreFocus) $('contextAdd').focus({preventScroll:true});
  }
  function positionContextMenu() {
    const rect = $('contextAdd').getBoundingClientRect(), gap = 8, margin = 12;
    contextMenu.style.maxHeight = `${Math.max(120,innerHeight-margin*2)}px`;
    const width = contextMenu.offsetWidth, height = contextMenu.offsetHeight;
    const top = rect.top-gap-height >= margin ? rect.top-gap-height : Math.min(rect.bottom+gap,innerHeight-height-margin);
    contextMenu.style.left = `${Math.max(margin,Math.min(rect.left,innerWidth-width-margin))}px`;
    contextMenu.style.top = `${Math.max(margin,top)}px`;
  }
  function showContextMenu(last=false) {
    const session = getSession() || {}, active = ['running','waiting','queued'].includes(session.status);
    contextMenu.innerHTML = `<div class="context-menu-label">添加</div><button type="button" role="menuitem" data-add-action="files">${icon('file')}<span><strong>文件</strong><small>从电脑选择，支持多选</small></span></button><button type="button" role="menuitem" data-add-action="reference">${icon('link')}<span><strong>链接或 BV 号</strong><small>添加网页与视频引用</small></span></button><button type="button" role="menuitem" data-add-action="manage">${icon('pin')}<span><strong>管理已添加内容</strong><small>${contexts.length ? `${contexts.length} 份内容 · 预览、锁定或移除` : '预览、锁定或移除任务材料'}</small></span></button><div class="context-menu-divider"></div><button type="button" role="menuitem" data-add-action="plan" ${!onPlan || active || session.preset === 'plan' ? 'disabled' : ''}>${icon('tasks')}<span><strong>${session.preset === 'plan' ? '已开启计划模式' : '计划模式'}</strong><small>${active ? '当前任务结束后可切换' : '先讨论方案，再开始执行'}</small></span></button>`;
    contextMenu.hidden = false;$('contextAdd').setAttribute('aria-expanded','true');positionContextMenu();
    const items = [...contextMenu.querySelectorAll('[role=menuitem]:not(:disabled)')];(last ? items.at(-1) : items[0])?.focus({preventScroll:true});
  }
  function openContextLibrary(reference=false) {
    $('contextFeedback').textContent = '';showDialog(contextDialog);renderContexts();load(true);
    if (reference) {$('contextReference').focus({preventScroll:true});$('contextReferenceForm').scrollIntoView({block:'nearest',behavior:reduceMotion() ? 'auto' : 'smooth'});}
  }
  function renderExtensions() {
    const catalogue = data.extensions || {}, packages = array(catalogue.extensions);
    return `<section class="studio-create"><div class="studio-create-icon">${icon('spark')}</div><div><span class="studio-eyebrow">CREATOR</span><h3>把常做的事，变成自己的能力</h3><p>用一句话描述工具、技能或小界面，她会在对话中创建草稿。</p><form id="studioCreatorForm" class="studio-inline"><input name="idea" data-draft="creator" aria-label="想创建的能力" placeholder="例如：把选中的资料整理成带来源的简报" required maxlength="3000"><button class="studio-button primary">通过聊天创建</button></form></div></section>
      <section>${title('我的扩展',`${packages.length} 个扩展 · 可以查看版本、启停或回退`)}<div class="studio-list">${packages.length ? packages.map(pkg => {
        const id = escapeHTML(pkg.id), revision = pkg.active_revision || pkg.latest_revision || array(pkg.revisions).at(-1) || '';
        return `<article class="studio-package"><div class="studio-row"><span class="studio-row-icon">${icon('extensions')}</span><div class="studio-flex-copy"><strong>${escapeHTML(pkg.name || pkg.id)}</strong><p>${escapeHTML(pkg.description || '个人创建的扩展')}</p><div class="studio-meta">${tag(pkg.enabled ? '已启用' : '已停用',pkg.enabled ? 'ok' : '')}${pkg.has_skill ? tag('技能') : ''}${pkg.has_mcp ? tag('工具') : ''}${pkg.has_ui ? tag('界面') : ''}${pkg.error ? tag('校验失败','danger') : tag('静态校验通过')}</div></div><button type="button" class="studio-switch" role="switch" aria-checked="${!!pkg.enabled}" aria-label="${pkg.enabled ? '停用' : '启用'} ${escapeHTML(pkg.name || pkg.id)}" data-action="extension-enable" data-id="${id}" data-enabled="${!pkg.enabled}" ${pkg.error && !pkg.enabled ? 'disabled' : ''}><span></span></button></div><div class="studio-package-bottom"><label>版本 <select aria-label="${escapeHTML(pkg.name || pkg.id)}的版本" data-package="${id}" data-draft="revision-${id}">${array(pkg.revisions).map(rev => `<option value="${escapeHTML(rev)}" ${rev === revision ? 'selected' : ''}>${escapeHTML(rev)}${rev === pkg.active_revision ? ' · 当前' : rev === pkg.latest_revision ? ' · 最新' : ''}</option>`).join('')}</select></label><div class="studio-actions">${button('extension-rollback','切换版本',`data-id="${id}"`)}${pkg.has_ui ? button('preview','预览界面',`data-id="${id}"`) : ''}</div></div><p class="studio-note">${escapeHTML(pkg.error || '静态校验只检查文件和语法；实际功能仍需运行验证。')}</p>${previewKey.split(':')[0] === pkg.id ? `<div class="studio-preview"><div class="studio-form-end"><small>隔离预览 · 无法访问工作台</small>${button('preview-close','关闭预览')}</div><iframe id="studioPreview" title="${escapeHTML(pkg.name || pkg.id)}界面预览" sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe></div>` : ''}</article>`;
      }).join('') : empty('还没有自己的扩展','从上方描述一个想法，创建结果会记录在这里。')}</div></section>
      <div class="studio-two-col"><section>${title('可用技能',`${array(catalogue.skills).length} 项`)}<div class="studio-list">${array(catalogue.skills).map(item => `<div class="studio-row"><span class="studio-row-icon">${icon('file')}</span><div class="studio-flex-copy"><strong>${escapeHTML(item.name || item.id)}</strong><p>${escapeHTML(item.description || '')}</p></div></div>`).join('') || '<p class="studio-note">尚未安装技能</p>'}</div></section><section>${title('工具服务',`${array(catalogue.servers).length} 项`)}<div class="studio-list">${array(catalogue.servers).map(item => `<div class="studio-row"><span class="studio-row-icon">${icon('extensions')}</span><div class="studio-flex-copy"><strong>${escapeHTML(item.name || item.id)}</strong><p>${escapeHTML(item.source || '')}</p></div></div>`).join('') || '<p class="studio-note">尚未连接工具服务</p>'}</div></section></div>`;
  }
  function matchingRules() {
    const query = ruleQuery.trim().toLocaleLowerCase();
    return array(data.blacklist?.rules || data.blacklist).filter(rule => (!ruleKind || rule.kind === ruleKind)
      && (!query || [rule.value,rule.reason,rule.evidence,kinds[rule.kind],rule.source === 'assistant' ? '她归纳的' : rule.origin === 'default' ? '默认规则' : '你设置的'].join(' ').toLocaleLowerCase().includes(query)));
  }
  function ruleRows() {
    const rules = matchingRules();
    return rules.length ? rules.map(rule => `<article class="studio-rule"><div class="studio-row"><div class="studio-flex-copy"><div class="studio-meta">${tag(kinds[rule.kind] || rule.kind)}${tag(rule.source === 'assistant' ? '她归纳的' : rule.origin === 'default' ? '默认规则' : '你设置的')}${tag(rule.kind === 'instruction' ? '对话提示' : '参数拦截',rule.kind === 'instruction' ? '' : 'ok')}</div><strong>${escapeHTML(rule.value)}</strong>${rule.reason ? `<p>${escapeHTML(rule.reason)}</p>` : ''}${rule.kind === 'path' ? `<small>${rule.operations === 'write' ? '限制修改' : rule.operations === 'read' ? '限制读取' : '限制读取和修改'}</small>` : ''}${rule.evidence ? `<details class="studio-details"><summary>查看依据</summary><p>${escapeHTML(rule.evidence)}</p></details>` : ''}</div><button type="button" class="studio-switch" role="switch" aria-checked="${!!rule.enabled}" aria-label="${rule.enabled ? '停用' : '启用'}规则 ${escapeHTML(rule.value)}" data-action="rule-toggle" data-id="${escapeHTML(rule.id)}" data-enabled="${!rule.enabled}"><span></span></button></div><div class="studio-rule-bottom"><small>${date(rule.created)}</small><div class="studio-actions">${button('rule-edit','编辑',`data-id="${escapeHTML(rule.id)}"`)}${button('rule-remove','删除',`data-id="${escapeHTML(rule.id)}"`,'danger')}</div></div></article>`).join('') : empty('没有匹配的约束','换一个关键词或类型试试。');
  }
  function updateRuleRows() {
    if (!$('studioRuleRows')) return;
    $('studioRuleRows').innerHTML = ruleRows();
    $('studioRuleMatchCount').textContent = `${matchingRules().length} 条匹配`;
  }
  function renderPermissions() {
    const rules = array(data.blacklist?.rules || data.blacklist), editing = rules.find(rule => rule.id === ruleEditId);
    const currentMode = getSession()?.mode || 'workspace', active = ['running','waiting','queued'].includes(getSession()?.status);
    const nativeReady = getNativeAuthority() === true, globalEnabled = data.authority?.global_unrestricted === true;
    return `<section class="studio-overview"><div class="studio-overview-icon">${icon('permissions')}</div><div class="studio-flex-copy"><h3>由你决定，她能走多远</h3><p>完全授权允许已接入工具直接行动；已启用的黑名单继续生效。</p></div></section><div class="studio-list studio-native-authority"><div class="studio-row"><div class="studio-flex-copy"><strong>桌宠原生工具自主授权</strong><p>${nativeReady ? '控制已接入的桌宠原生工具，与当前会话权限分开设置。' : '当前宿主未声明全局权限覆盖'}</p><small>${nativeReady ? '黑名单和明确停用的工具仍优先生效。' : '此开关暂不可用；下方仍可调整工作台会话权限。'}</small></div><button type="button" class="studio-switch" role="switch" aria-label="桌宠原生工具自主授权" aria-checked="${globalEnabled}" data-action="global-authority" data-enabled="${!globalEnabled}" ${nativeReady ? '' : 'disabled'}><span></span></button></div></div><form id="studioModeForm" class="studio-form studio-mode-form"><label for="studioMode">当前会话的权限</label><div class="studio-inline"><select id="studioMode" name="mode" data-draft="mode">${Object.entries(modes).map(([mode,label]) => `<option value="${mode}" ${mode === currentMode ? 'selected' : ''}>${label}</option>`).join('')}</select><button class="studio-button" ${active || getSession()?.parent_id ? 'disabled' : ''}>应用</button></div><p class="studio-note">这是应用的执行策略，不是操作系统沙箱。命令和脚本内部的行为无法仅凭参数黑名单完整约束。</p></form>
      <details id="studioRuleEditor" class="studio-rule-editor" data-disclosure-key="rule-editor" ${editing ? 'open' : ''}><summary>${icon('permissions')}<strong>${editing ? '编辑约束' : '添加约束'}</strong><span>${editing ? '修改后保存' : '记录偏好或设置拦截规则'}</span></summary><form id="studioRuleForm" class="studio-form"><div class="studio-form-grid rule-grid"><label>类型<select id="studioRuleKind" name="kind" data-draft="rule-kind">${Object.entries(kinds).map(([kind,label]) => `<option value="${kind}" ${kind === (editing?.kind || 'instruction') ? 'selected' : ''}>${label}</option>`).join('')}</select></label><label id="studioRuleOperations">作用于<select name="operations" data-draft="rule-operations"><option value="all">读取和修改</option><option value="read" ${editing?.operations === 'read' ? 'selected' : ''}>读取</option><option value="write" ${editing?.operations === 'write' ? 'selected' : ''}>修改</option></select></label></div><label>内容<input id="studioRuleValue" name="value" data-draft="rule-value" value="${escapeHTML(editing?.value || '')}" required maxlength="2000" placeholder="例如：整理文件前先保留原始文件"></label><p class="studio-note" id="studioRuleHint"></p><label>原因（可选）<input name="reason" data-draft="rule-reason" value="${escapeHTML(editing?.reason || '')}" maxlength="2000" placeholder="记录为什么设置这条约束"></label><div class="studio-form-end">${editing ? button('rule-cancel','取消编辑') : '<span></span>'}<button class="studio-button primary">${editing ? '保存修改' : '添加约束'}</button></div></form></details>
      <details id="studioRuleList" class="studio-rule-list" data-disclosure-key="rule-list"><summary>${icon('permissions')}<strong>黑名单与约束</strong><span>${rules.length} 条 · ${rules.filter(x => x.enabled).length} 条已启用</span></summary><div class="studio-rule-controls"><label class="sr-only" for="studioRuleSearch">搜索约束</label><input id="studioRuleSearch" type="search" value="${escapeHTML(ruleQuery)}" placeholder="搜索内容、原因或来源" autocomplete="off"><label class="sr-only" for="studioRuleFilter">筛选约束类型</label><select id="studioRuleFilter"><option value="">全部类型</option>${Object.entries(kinds).map(([kind,label]) => `<option value="${kind}" ${kind === ruleKind ? 'selected' : ''}>${label}</option>`).join('')}</select></div><div class="studio-rule-list-caption"><span id="studioRuleMatchCount" role="status">${matchingRules().length} 条匹配</span><span>她可以新增，只有你可以修改或解除</span></div><div id="studioRuleRows" class="studio-list studio-rule-scroll">${rules.length ? ruleRows() : empty('没有已保存的约束','在上方添加明确的拦截规则，或记录自然语言偏好。')}</div></details>
      <details class="studio-audit"><summary>最近的约束变更</summary>${array(data.audit).slice(0,12).map(item => `<div class="studio-audit-row"><span>${escapeHTML(({add:'新增',update:'修改',remove:'删除',deny:'拦截',initialize:'初始化'})[item.action] || item.action || item.event || '变更')}</span><span>${escapeHTML(item.by === 'assistant' ? '她' : '你')}</span><small>${date(item.at || item.time || item.created)}</small></div>`).join('') || '<p class="studio-note">暂无变更记录</p>'}</details>`;
  }
  function updateRuleHint() {
    const kind = $('studioRuleKind')?.value;if (!kind) return;
    $('studioRuleOperations').hidden = kind !== 'path';
    $('studioRuleValue').maxLength = kind === 'instruction' ? 2000 : kind === 'tool' ? 200 : 1024;
    body.querySelector('#studioRuleForm [name=reason]').maxLength = 1000;
    $('studioRuleValue').placeholder = {instruction:'例如：整理文件前先保留原始文件',tool:'例如：mcp.browser.*',path:'例如：G:\\Documents\\Private',command:'例如：Remove-Item',domain:'例如：example.com'}[kind];
    $('studioRuleHint').textContent = {instruction:'记录在任务指令中，由她遵守；不会进行参数匹配。',tool:'匹配工具名称，支持 * 通配符。例如 mcp.browser.*。',path:'填写完整路径，匹配指定文件或目录及其后代。',command:'按命令的字面片段匹配，不执行正则表达式。',domain:'匹配指定域名及其子域名。例如 example.com。'}[kind];
  }
  function renderGrowth() {
    const growth = data.growth || {}, run = data.run || {}, records = array(growth.records), active = ['running','waiting','queued'].includes(getSession()?.status);
    return `<section class="studio-growth-intro"><span class="studio-overview-icon">${icon('growth')}</span><div><h3>每一次交付，都留下依据</h3><p>记录实际任务和你的验收反馈，帮助她在下一次做得更稳。</p></div></section><div class="studio-metrics">${[['已记录任务',growth.runs],['验收通过',growth.accepted],['需要改进',growth.rejected],['待验收',growth.unreviewed]].map(([label,value]) => `<div><strong>${Number(value)||0}</strong><span>${label}</span></div>`).join('')}</div><p class="studio-note">${growth.scope === 'current_session' ? '本会话已结束任务' : '已记录任务'}费用 ${money(growth.cost)}${growth.truncated ? '（最近 500 项）' : ''}。完成次数不等于能力提升，当前展示可核对的记录。</p>
      ${run.id ? data.deliveries !== undefined ? `<section>${title('验收当前任务','先核对成果与执行证据，再记录这次交付的反馈。')}<div class="studio-delivery-entry"><span class="studio-row-icon">${icon('deliveries')}</span><div class="studio-flex-copy"><strong>${escapeHTML(run.goal || '当前任务')}</strong><small>验收会记录这次任务与文件版本</small></div>${button('deliveries-current','前往成果验收')}</div></section>` : `<section>${title('验收当前任务',`当前状态：${escapeHTML(status(run.verification || 'unreviewed'))}`)}<form id="studioReviewForm" class="studio-form"><label>反馈<textarea name="note" data-draft="review" rows="3" maxlength="2000" placeholder="哪些符合预期，哪些下次需要改进？">${escapeHTML(run.review_note || '')}</textarea></label><div class="studio-form-end"><small>${active ? '任务执行结束后可验收' : '以交付结果为准，随时补充反馈'}</small><div class="studio-actions"><button type="submit" name="accepted" value="false" class="studio-button" ${active ? 'disabled' : ''}>需要改进</button><button type="submit" name="accepted" value="true" class="studio-button primary" ${active ? 'disabled' : ''}>验收通过</button></div></div></form></section>` : ''}
      <section>${title('交付记录')}<div class="studio-list">${records.length ? records.map(record => `<article class="studio-row"><span class="studio-record-mark ${record.verification === 'accepted' ? 'ok' : ''}">${record.verification === 'accepted' ? '✓' : '•'}</span><div class="studio-flex-copy"><strong>${escapeHTML(record.goal || record.title || '任务')}</strong><div class="studio-meta">${tag(status(record.verification || 'unreviewed'),record.verification === 'accepted' ? 'ok' : '')}<span>${date(record.ended || record.started)}</span><span>${money(record.cost)}</span></div>${record.review_note ? `<p>${escapeHTML(record.review_note)}</p>` : ''}</div>${record.session_id && record.session_id !== sid ? button('open-child','查看',`data-id="${escapeHTML(record.session_id)}"`) : ''}</article>`).join('') : empty('从下一次交付开始积累','执行任务后，回来记录结果是否达到你的要求。')}</div></section>${renderRegression(growth)}`;
  }
  function renderRegression(growth) {
    const packages = array(data.extensions?.extensions).filter(x => x.has_mcp), suites = array(growth.suites), reports = array(growth.reports);
    const adopted = !!growthReport && packages.some(pkg => pkg.id === growthReport.package_id && pkg.enabled && pkg.active_revision === growthReport.candidate?.revision);
    const comparisons = {improved_on_suite:'固定题集通过数提高',regression_passed:'回归通过',candidate_passed_no_baseline:'候选通过 · 尚无基线',candidate_failed_no_baseline:'候选未通过 · 尚无基线',regression_failed:'发现回归',incomplete:'比较未完成',not_improved:'未发现提升'};
    const example = {id:'my-regression',name:'我的回归题集',package_id:packages[0]?.id || '扩展ID',cases:[{name:'基本输入',tool:'工具名',arguments:{text:'测试输入'},expect:{contains:'预期内容'},timeout_s:15}]};
    return `<section>${title('能力回归','用固定题集比较版本。题集由你维护，结果保留证据。')}<div class="studio-list">${suites.map(suite => `<article class="studio-row"><span class="studio-row-icon">${icon('file')}</span><div class="studio-flex-copy"><strong>${escapeHTML(suite.name || suite.id)}</strong><small>${escapeHTML(suite.package_id)} · ${array(suite.cases).length} 个用例 · v${escapeHTML(suite.version || 1)}</small></div>${button('suite-edit','编辑题集',`data-id="${escapeHTML(suite.id)}"`)}</article>`).join('') || empty('还没有固定题集','为带工具的扩展保存真实输入与预期结果。')}</div><details class="studio-suite-editor"><summary>维护回归题集</summary><form id="studioSuiteForm" class="studio-form"><label for="studioSuiteSpec">题集 JSON<textarea id="studioSuiteSpec" name="spec" data-draft="suite-spec" rows="10" spellcheck="false" required>${escapeHTML(JSON.stringify(example,null,2))}</textarea></label><p class="studio-note">expect 支持 contains、not_contains 和 equals。每个用例会调用指定工具。</p><div class="studio-form-end"><small>修改后会生成新的题集版本</small><button class="studio-button">保存题集</button></div></form></details>
      <form id="studioEvaluateForm" class="studio-form studio-evaluate"><div class="studio-form-grid"><label>扩展<select name="package_id" id="studioEvalPackage" data-draft="eval-package" required><option value="">选择扩展</option>${packages.map(pkg => `<option value="${escapeHTML(pkg.id)}">${escapeHTML(pkg.name || pkg.id)}</option>`).join('')}</select></label><label>候选版本<select name="revision" id="studioEvalRevision" data-draft="eval-revision" required><option value="">选择版本</option></select></label><label>固定题集<select name="suite_id" data-draft="eval-suite" required><option value="">选择题集</option>${suites.map(suite => `<option value="${escapeHTML(suite.id)}">${escapeHTML(suite.name || suite.id)}</option>`).join('')}</select></label></div><p class="studio-note">评测会实际运行基线与候选工具。请使用测试数据；临时副本不能隔离脚本对本机的访问。</p><div class="studio-form-end"><small>${getSession()?.mode === 'read' || getSession()?.preset === 'plan' ? '当前模式只查看报告，切换权限后可运行评测。' : '结果只代表这套题集覆盖的范围'}</small><button class="studio-button primary" ${!packages.length || !suites.length || getSession()?.mode === 'read' || getSession()?.preset === 'plan' ? 'disabled' : ''}>运行版本比较</button></div></form>
      ${reports.length ? `<div class="studio-section-head studio-report-title"><h3>比较报告</h3></div><div class="studio-list">${reports.map(report => `<article class="studio-row"><div class="studio-flex-copy"><strong>${escapeHTML(comparisons[report.comparison] || report.comparison || status(report.status))}</strong><small>${escapeHTML(report.package_id)} · ${escapeHTML(report.suite_id)}</small></div>${button('growth-report','查看证据',`data-id="${escapeHTML(report.id)}"`)}</article>`).join('')}</div>` : ''}
      ${growthReport ? `<article class="studio-report"><div class="studio-section-head"><div><h3>${escapeHTML(comparisons[growthReport.comparison] || growthReport.comparison || '版本比较报告')}</h3><p>${escapeHTML(growthReport.scope || '仅代表固定题集的测试结果')}</p></div>${button('growth-report-close','收起')}</div><div class="studio-two-col">${['baseline','candidate'].map(phase => {const result = growthReport[phase];return `<div class="studio-report-phase"><h4>${phase === 'baseline' ? '基线' : '候选'}</h4>${result ? `<p>${escapeHTML(result.revision || '')}</p><strong>${Number(result.passed)||0} 通过 / ${Number(result.failed)||0} 失败</strong>${array(result.cases).map(test => `<details class="studio-case"><summary>${test.passed ? '✓' : '×'} ${escapeHTML(test.name || test.tool)}</summary><pre>${escapeHTML(test.error || test.output_preview || JSON.stringify(test.assertions || {},null,2))}</pre></details>`).join('')}` : '<p>没有已启用基线</p>'}</div>`;}).join('')}</div><div class="studio-form-end"><small>${escapeHTML(adopted ? '当前已启用这个版本，可在扩展页回退。' : array(growthReport.adoption?.reasons).join('；') || '采用后将启用该候选版本，后续仍可回退。')}</small>${button('growth-adopt',adopted ? '当前已启用' : '采用此版本',`data-id="${escapeHTML(growthReport.id)}" ${adopted || !growthReport.adoption?.eligible ? 'disabled' : ''}`,'primary')}</div></article>` : ''}</section>`;
  }
  function updateEvalRevisions() {
    const picker = $('studioEvalPackage'), revisions = $('studioEvalRevision');if (!picker || !revisions) return;
    const pkg = array(data.extensions?.extensions).find(x => x.id === picker.value), previous = drafts.get(formKey())?.['eval-revision'] || revisions.value;
    revisions.innerHTML = '<option value="">选择版本</option>' + array(pkg?.revisions).map(revision => `<option value="${escapeHTML(revision)}">${escapeHTML(revision)}</option>`).join('');
    if (array(pkg?.revisions).includes(previous)) revisions.value = previous;
  }
  function renderContexts() {
    const picked = contexts.filter(item => picks().has(item.id) || item.pinned);
    $('contextChips').hidden = !picked.length;
    // 第六轮 (学 ChatGPT 的附件卡): 带类型的小卡片, 图片出缩略图; 名字下面一行写类型和大小
    const sid = getSession()?.id || '';
    $('contextChips').innerHTML = picked.map(item => `<span class="context-chip card ${item.pinned ? 'pinned' : ''}" data-type="${escapeHTML(chipType(item))}" title="${escapeHTML(item.name)}"><span class="context-chip-thumb">${chipType(item) === '图片' && sid ? `<img src="/api/agent/context/image?id=${encodeURIComponent(sid)}&amp;item=${encodeURIComponent(item.id)}" alt="" loading="lazy">` : icon(item.kind === 'file' ? 'file' : 'link')}</span><button type="button" class="context-chip-name" data-context-action="preview" data-id="${escapeHTML(item.id)}" title="预览内容" aria-label="预览 ${escapeHTML(item.name)}"><b>${escapeHTML(item.name)}</b><small>${escapeHTML(chipType(item))}${item.kind === 'file' && item.size ? ` · ${bytes(item.size)}` : ''}</small></button><button type="button" class="context-chip-pin" data-context-action="pin" data-id="${escapeHTML(item.id)}" aria-pressed="${!!item.pinned}" title="${item.pinned ? '取消锁定' : '锁定到后续任务'}" aria-label="${item.pinned ? '取消锁定' : '锁定'} ${escapeHTML(item.name)}">${icon('pin')}</button><button type="button" data-context-action="deselect" data-id="${escapeHTML(item.id)}" aria-label="${item.pinned ? '取消锁定并移除本轮引用' : '移除本轮引用'} ${escapeHTML(item.name)}" title="${item.pinned ? '取消锁定并移除本轮引用' : '移除本轮引用'}">×</button></span>`).join('');
    $('contextComposerStatus').textContent = uploads.size ? `正在上传 ${uploads.size} 个文件…` : picked.length ? `${picked.length} 份内容${picked.some(x => x.pinned) ? ' · 已锁定内容会持续引用' : ''}` : '';
    $('contextLibrary').innerHTML = contexts.length ? contexts.map(item => {
      const info = materialState(item);
      return `<article class="context-library-item"><div class="context-library-row"><input type="checkbox" aria-label="本轮引用 ${escapeHTML(item.name)}" data-context-action="select" data-id="${escapeHTML(item.id)}" ${picked.some(x => x.id === item.id) ? 'checked' : ''} ${item.pinned ? 'disabled' : ''}><span class="studio-row-icon">${icon(item.kind === 'file' ? 'file' : 'link')}</span><div class="studio-flex-copy"><button type="button" class="studio-file-name" data-context-action="preview" data-id="${escapeHTML(item.id)}" aria-expanded="${readers.context?.id === item.id}" aria-controls="studioReader-context">${escapeHTML(item.name)}</button><div class="studio-meta">${item.kind === 'file' ? `${tag(info.label,info.tone)}<span>${bytes(item.size)}${item.format ? ` · ${escapeHTML(item.format.toUpperCase())}` : ''}</span>` : tag(item.kind === 'bilibili' ? 'B 站视频链接 · 尚需读取' : '网页链接 · 尚需读取')}</div>${item.error ? `<small class="studio-reader-error">${escapeHTML(item.error)}</small>` : ''}</div><button type="button" class="studio-icon-button ${item.pinned ? 'selected' : ''}" data-context-action="pin" data-id="${escapeHTML(item.id)}" aria-pressed="${!!item.pinned}" aria-label="${item.pinned ? '取消锁定' : '锁定'} ${escapeHTML(item.name)}">${icon('pin')}</button><button type="button" class="studio-icon-button" data-context-action="remove" data-id="${escapeHTML(item.id)}" aria-label="移除内容 ${escapeHTML(item.name)}">×</button></div>${readers.context?.id === item.id ? renderReader('context') : ''}</article>`;
    }).join('') : empty('让她知道要参考什么','先选择文件，或粘贴一个链接。');
  }
  async function addReference(value) {
    const session = await requireSession(), targetId = session.id;
    const result = await post('context_reference',{value},targetId);
    if (sessionId() === targetId) {syncContexts(result.contexts || [...contexts, result.item].filter(Boolean));if (result.item?.id) picks().add(result.item.id);renderContexts();}
    return result;
  }
  async function addTextMaterial({name,text,source='',targetId}={}) {
    if (!String(text || '').trim()) throw new Error('没有可引用的文字');
    if (targetId && sessionId() !== targetId) throw new Error('对话已切换，请回到原对话后重新引用');
    const work = (async () => {
      const session = await requireSession(), id = targetId || session.id;
      if (session.id !== id) throw new Error('对话已切换，请重新引用');
      const file = new Blob([`${source ? `引用来源：${source}\n\n` : ''}${text}`],{type:'text/plain;charset=utf-8'});
      if (file.size > 8 * 1024 * 1024) throw new Error('引用超过 8 MB，请只选择需要的段落');
      const encoded = await new Promise((resolve,reject) => {const reader = new FileReader();reader.onload = () => resolve(String(reader.result).split(',')[1]);reader.onerror = () => reject(new Error('引用内容读取失败'));reader.readAsDataURL(file);});
      const result = await request(`/api/agent/context?id=${encodeURIComponent(id)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:String(name || '引用内容').replace(/[\\/:*?"<>|]/g,'_').slice(0,170)+'.txt',data:encoded})});
      if (sessionId() === id) {syncContexts(result.contexts);if (result.item?.id) picks().add(result.item.id);renderContexts();}
      return result;
    })();
    uploads.add(work);renderContexts();
    try {return await work;} catch (error) {uploadFailures.push(error.message);throw error;} finally {uploads.delete(work);renderContexts();}
  }
  function uploadFiles(files) {
    for (const file of Array.from(files)) {
      if (file.size > 8 * 1024 * 1024) {announce(`${file.name} 超过 8 MB，请选择更小的文件`);continue;}
      const work = (async () => {
        const session = await requireSession(), targetId = session.id;
        const encoded = await new Promise((resolve,reject) => {const reader = new FileReader();reader.onload = () => resolve(String(reader.result).split(',')[1]);reader.onerror = () => reject(new Error(`无法读取 ${file.name}`));reader.readAsDataURL(file);});
        const result = await request(`/api/agent/context?id=${encodeURIComponent(targetId)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:file.name,data:encoded})});
        if (sessionId() === targetId) {syncContexts(result.contexts);if (result.item?.id) picks().add(result.item.id);}
      })();
      uploads.add(work);renderContexts();
      work.catch(error => {uploadFailures.push(error.message);announce(`添加文件失败：${error.message}`);}).finally(() => {uploads.delete(work);renderContexts();});
    }
  }
  async function ready() {
    const session = getSession();
    if (session?.forked_from && !session.run?.id && !initializedSelections.has(session.id)) {
      await (sid === session.id && loading ? loading : load(true));
      while (sessionId() === session.id && loading) await loading;
      if (sessionId() === session.id && !initializedSelections.has(session.id)) throw new Error('分叉材料尚未同步，请稍后重试发送');
    }
    while (uploads.size || references.size) await Promise.allSettled([...uploads,...references]);
    if (uploadFailures.length) throw new Error(uploadFailures.splice(0).join('；'));
  }
  function contextIds() {if (sid !== sessionId()) return [];return contexts.filter(x => x.pinned || picks().has(x.id)).map(x => x.id);}
  function sent() {initializedSelections.add(sid);picks().clear();contexts.filter(x => x.pinned).forEach(x => picks().add(x.id));renderContexts();}
  async function contextAction(event) {
    const target = event.target.closest('[data-context-action]');if (!target || target.disabled) return;
    const item = contexts.find(x => x.id === target.dataset.id), targetSession = sessionId();if (!item) return;
    try {
      target.disabled = true;
      const action = target.dataset.contextAction;
      if (action === 'preview') {const fromChip = !!target.closest('#contextChips');showDialog(contextDialog);if (readers.context?.id === item.id && !fromChip) {readers.context = null;renderContexts();} else await readContent('context',item.id,{focus:true});return;}
      if (action === 'select') {target.checked ? picks().add(item.id) : picks().delete(item.id);renderContexts();return;}
      if (action === 'deselect' && !item.pinned) {picks().delete(item.id);renderContexts();return;}
      const result = await post(action === 'remove' ? 'context_remove' : 'context_pin',action === 'remove' ? {context_id:item.id} : {context_id:item.id,pinned:action === 'deselect' ? false : !item.pinned},targetSession);
      if (targetSession !== sessionId()) return;
      if (action === 'deselect' || action === 'remove') picks().delete(item.id);
      if (action === 'remove' && readers.context?.id === item.id) readers.context = null;
      syncContexts(result.contexts || contexts.map(x => x.id === item.id ? {...x,pinned:!x.pinned} : x).filter(x => action !== 'remove' || x.id !== item.id));
    } catch (error) {announce(error.message);target.disabled = false;}
  }
  function extractReferences(text) {
    const result = new Set();
    for (const match of text.matchAll(/https?:\/\/[^\s<>"'\u3000-\u303f\uff00-\uffef]+|\bBV[0-9a-zA-Z]{10}\b/g)) {
      const value = match[0].replace(/[.,;!?)\]}]+$/,'');
      // A BV inside an already matched URL is naturally consumed by matchAll.
      result.add(value);
    }
    return [...result].slice(0,24);
  }
  function pastedReferences(text) {
    const work = (async () => {for (const value of extractReferences(text)) {try {await addReference(value);} catch (error) {uploadFailures.push(error.message);announce(`引用未添加：${error.message}`);}}})();
    references.add(work);work.finally(() => references.delete(work));return work;
  }
  async function perform(action, target) {
    const id = target.dataset.id;
    if (action === 'reload') {await load(true);return;}
    if (action === 'delivery-view') {deliveryView = target.dataset.view === 'grid' ? 'grid' : 'list';try {localStorage.setItem('wm.studio.deliveryView',deliveryView);} catch { /* 下次还是列表 */ }render();return;}
    if (action === 'board') {dispatchEvent(new CustomEvent('wm-panel-tool',{detail:{name:'agents'}}));return;}  // 分工协作 -> 协作任务的看板
    if (action === 'deliveries-current') {deliveryRunId = '';deliveryFresh = false;readers.delivery = null;await open('deliveries');return;}
    if (action === 'delivery-preview') {if (readers.delivery?.id === id) {readers.delivery = null;render();} else await readContent('delivery',id,{focus:true});return;}
    if (action === 'delivery-copy') {const path = array(data.deliveries?.artifacts).find(item => item.id === id)?.path;if (!path) throw new Error('没有可复制的文件路径');if (!navigator.clipboard?.writeText) throw new Error('当前环境无法自动复制，请选中文件路径复制');await navigator.clipboard.writeText(path);announce('文件路径已复制');return;}
    if (action === 'global-authority') {
      if (getNativeAuthority() !== true) throw new Error('当前宿主未声明全局权限覆盖');
      const result = await request('/api/agent/permissions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action:'global',enabled:target.dataset.enabled === 'true',expected_revision:data?.authority?.revision})});
      data.authority = result;await authorityChanged?.();await load(true,false);render();announce(result.global_unrestricted ? '已开启桌宠原生工具自主授权，会话权限保持独立' : '已关闭桌宠原生工具自主授权');return;
    }
    if (action === 'open-child') {dialog.close();await openSession(id);return;}
    if (action === 'delegate-draft') {dialog.close();setDraft('请把当前任务拆成互不冲突的子任务，使用子代理分担上下文。调研任务使用只读权限；需要修改代码的任务使用独立工作副本。给出每项验收标准，汇总结果并检查后再交付。');return;}
    if (action === 'rule-edit') {ruleEditId = id;drafts.delete(formKey());render();$('studioRuleEditor').open = true;$('studioRuleEditor').scrollIntoView({block:'nearest',behavior:reduceMotion() ? 'auto' : 'smooth'});$('studioRuleValue').focus();return;}
    if (action === 'rule-cancel') {ruleEditId = '';drafts.delete(formKey());render();return;}
    if (action === 'preview-close') {previewKey = '';previewHTML = '';render();return;}
    if (action === 'suite-edit') {const suite = array(data.growth?.suites).find(x => x.id === id);if (suite) {const spec = {id:suite.id,name:suite.name,package_id:suite.package_id,cases:suite.cases};$('studioSuiteSpec').value = JSON.stringify(spec,null,2);$('studioSuiteSpec').closest('details').open = true;$('studioSuiteSpec').focus();stash();}return;}
    if (action === 'growth-report-close') {growthReport = null;render();return;}
    if (action === 'growth-report') {const result = await post('growth_report',{report_id:id});growthReport = {...result.report,adoption:result.adoption || result.report?.adoption};render();return;}
    if (action === 'growth-adopt') {await post('growth_adopt',{report_id:id,authorized:true});const result = await post('growth_report',{report_id:id});growthReport = {...result.report,adoption:result.adoption || result.report?.adoption};await load(true,false);render();announce('已采用候选版本，可在扩展中回退');return;}
    if (action === 'preview') {
      const revision = body.querySelector(`select[data-package="${CSS.escape(id)}"]`)?.value || '';
      const result = await request(`/api/agent/studio?id=${encodeURIComponent(sid)}&preview=${encodeURIComponent(id)}&revision=${encodeURIComponent(revision)}`);
      previewKey = `${id}:${revision}`;previewHTML = String(result.html || '');stash();render();return;
    }
    if (action === 'worktree-diff') {diffResult = await post('worktree_diff',{worktree_id:id});stash();render();return;}
    if (action === 'worktree-export') {const result = await post('worktree_export',{worktree_id:id});announce(`补丁已导出：${result.path || result.note || ''}。尚未合入原工作区。`);return;}
    if (action === 'resume') await post('resume');
    else if (action === 'queue-remove') await post('queue_remove',{queue_id:id});
    else if (action === 'rule-toggle') await post('rule_update',{rule_id:id,changes:{enabled:target.dataset.enabled === 'true'}});
    else if (action === 'rule-remove') {await post('rule_remove',{rule_id:id});if (ruleEditId === id) {ruleEditId = '';drafts.delete(formKey());}}
    else if (action === 'extension-enable' || action === 'extension-rollback') {
      const revision = body.querySelector(`select[data-package="${CSS.escape(id)}"]`)?.value || '';
      await post(action === 'extension-enable' ? 'extension_enable' : 'extension_rollback',{package_id:id,revision,enabled:target.dataset.enabled === 'true'});
      previewKey = '';previewHTML = '';drafts.delete(formKey());
    }
    await load(true,false);render();
  }
  function mountPreview() {
    const iframe = $('studioPreview');if (!iframe) return;
    // The server also supplies CSP. Keep a network-free fallback even if a malformed response omits it.
    iframe.srcdoc = `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; font-src data:; connect-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'">${previewHTML}`;
  }
  async function submit(form, submitter) {
    const values = Object.fromEntries(new FormData(form));
    if (form.id === 'studioCreatorForm') {
      dialog.close();
      setDraft(`请为我创建一个工作台扩展：${values.idea.trim()}\n请先明确用途与验收标准，再通过 Creator 能力创建可追溯版本。按需要包含技能、扩展工具或界面；先保存草稿，检查静态校验和隔离预览，并报告哪些功能已经实际验证、哪些尚未运行。`);
      announce('创建要求已放入输入框，发送后开始');return;
    }
    const session = await requireSession();
    if (form.id === 'studioQueueForm') {await ready();await post('queue',{text:values.queue || $('studioQueueText').value,context_ids:contextIds()},session.id);drafts.get(formKey()) && delete drafts.get(formKey()).queue;}
    else if (form.id === 'studioBudgetForm') {await post('budget',Object.fromEntries(Object.entries(values).map(([key,value]) => [key,Number(value)])));await openSession(session.id);drafts.delete(formKey());}
    else if (form.id === 'studioModeForm') {await api({action:'settings',id:session.id,mode:values.mode});await openSession(session.id);drafts.delete(formKey());}
    else if (form.id === 'studioRuleForm') {
      const rule = {kind:values.kind,value:values.value.trim(),reason:values.reason.trim(),operations:values.kind === 'path' ? values.operations : 'all'};
      await post(ruleEditId ? 'rule_update' : 'rule_add',ruleEditId ? {rule_id:ruleEditId,changes:rule} : {rule});ruleEditId = '';drafts.delete(formKey());
    } else if (form.id === 'studioReviewForm') {await post('review',{run_id:data.run?.id,accepted:submitter?.value === 'true',note:values.note});drafts.delete(formKey());}
    else if (form.id === 'studioDeliveryReviewForm') {const accepted = submitter?.value === 'true', versions = JSON.parse(values.artifact_versions);if (accepted && Object.values(versions).some(value => !value)) throw new Error('部分文件无法核对，请恢复文件并刷新后验收');await post('review',{run_id:form.dataset.runId,accepted,note:values.note,...(accepted ? {artifact_versions:versions} : {})});drafts.get(formKey()) && delete drafts.get(formKey())[`delivery-review-${form.dataset.runId}`];}
    else if (form.id === 'studioSuiteForm') {let spec;try {spec = JSON.parse(values.spec);} catch {throw new Error('题集 JSON 格式有误，请检查后保存');}await post('suite_save',{spec});}
    else if (form.id === 'studioEvaluateForm') {announce('正在运行基线与候选工具，请等待报告…');const result = await post('growth_evaluate',{...values,authorized:true});growthReport = {...result.report,adoption:result.adoption || result.report?.adoption};}
    await load(true,false);render();announce(form.id === 'studioEvaluateForm' ? '比较结束，请查看用例证据' : '已保存');
  }
  $('studioOpen').addEventListener('click',() => open());
  $('studioQuick')?.addEventListener('click',() => open());
  $('studioClose').addEventListener('click',() => dialog.close());
  $('studioRefresh').addEventListener('click',() => {stash();load(true);});
  dialog.addEventListener('close',stash);
  $('studioTabs').addEventListener('click',event => {const target = event.target.closest('[data-tab]');if (!target) return;stash();scrolls.set(formKey(),body.scrollTop);tab = target.dataset.tab;host?.moved?.(tab);if (!mutating) $('studioFeedback').textContent = '进度和结果来自实际任务记录';render();body.scrollTop = scrolls.get(formKey()) || 0;if (tab === 'deliveries') load(true);});
  $('studioTabs').addEventListener('keydown',event => {
    if (!['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) return;
    event.preventDefault();const tabs = [...$('studioTabs').querySelectorAll('[data-tab]')].filter(x => x.offsetParent), index = tabs.findIndex(x => x.dataset.tab === tab);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length-1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
    tabs[next].click();$('studioTabs').querySelector(`[data-tab="${tab}"]`).focus();
  });
  body.addEventListener('click',async event => {
    const target = event.target.closest('[data-action]');if (!target || target.disabled) return;
    const position = body.scrollTop;stash();target.disabled = true;mutating++;
    try {await perform(target.dataset.action,target);} catch (error) {if (error.status === 409) {await load(true);render();}announce(error.message);} finally {mutating--;if (target.isConnected) target.disabled = false;if (!['rule-edit','suite-edit','open-child','delivery-preview','deliveries-current'].includes(target.dataset.action)) body.scrollTop = position;}
  });
  body.addEventListener('submit',async event => {
    event.preventDefault();const form = event.target, submitter = event.submitter;stash();mutating++;
    if (submitter) submitter.disabled = true;
    try {await submit(form,submitter);} catch (error) {if (error.status === 409) {await load(true);render();}announce(error.message);} finally {mutating--;if (submitter?.isConnected) submitter.disabled = false;}
  });
  body.addEventListener('input',event => {if (event.target.id === 'studioRuleSearch') {ruleQuery = event.target.value;updateRuleRows();}});
  body.addEventListener('change',async event => {stash();if (event.target.id === 'studioRuleFilter') {ruleKind = event.target.value;updateRuleRows();}if (event.target.id === 'studioRuleKind') updateRuleHint();if (event.target.id === 'studioEvalPackage') updateEvalRevisions();if (event.target.id === 'studioDeliveryRun') {deliveryRunId = event.target.value;readers.delivery = null;render();await load(true);if (tab === 'deliveries') render();}});
  body.addEventListener('click',readerAction);
  body.addEventListener('change',readerAction);
  $('contextAdd').addEventListener('click',() => contextMenu.hidden ? showContextMenu() : closeContextMenu());
  $('contextAdd').addEventListener('keydown',event => {if (['ArrowDown','ArrowUp'].includes(event.key)) {event.preventDefault();showContextMenu(event.key === 'ArrowUp');}});
  contextMenu.addEventListener('click',event => {
    const item = event.target.closest('[data-add-action]');if (!item || item.disabled) return;
    const action = item.dataset.addAction;closeContextMenu(false);
    // Keep the native file chooser inside the original trusted click gesture.
    if (action === 'files') {$('contextFiles').click();return;}
    if (action === 'reference' || action === 'manage') {openContextLibrary(action === 'reference');return;}
    if (action === 'plan') Promise.resolve().then(() => onPlan?.()).then(() => {announce('已开启计划模式，先讨论方案再开始执行');$('input').focus();}).catch(error => announce(error.message));
  });
  contextMenu.addEventListener('keydown',event => {
    const items = [...contextMenu.querySelectorAll('[role=menuitem]:not(:disabled)')];
    if (event.key === 'Escape') {event.preventDefault();event.stopPropagation();closeContextMenu();return;}
    if (event.key === 'Tab') {closeContextMenu();return;}
    if (!['ArrowDown','ArrowUp','Home','End'].includes(event.key)) return;
    event.preventDefault();const index = items.indexOf(document.activeElement);
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? items.length-1 : (index+(event.key === 'ArrowDown' ? 1 : -1)+items.length)%items.length;
    items[next]?.focus();
  });
  document.addEventListener('pointerdown',event => {if (!contextMenu.hidden && !contextMenu.contains(event.target) && !$('contextAdd').contains(event.target)) closeContextMenu(false);},true);
  document.addEventListener('keydown',event => {if (!contextMenu.hidden && event.key === 'Escape') {event.preventDefault();closeContextMenu();}},true);
  window.addEventListener('resize',() => {if (!contextMenu.hidden) positionContextMenu();});
  window.addEventListener('scroll',event => {if (!contextMenu.hidden && !contextMenu.contains(event.target)) closeContextMenu(false);},true);
  $('contextClose').addEventListener('click',() => contextDialog.close());
  $('contextUpload').addEventListener('click',() => $('contextFiles').click());
  $('contextFiles').addEventListener('change',event => {uploadFiles(event.target.files);event.target.value = '';});
  $('contextReferenceForm').addEventListener('submit',async event => {
    event.preventDefault();const submitter = event.submitter;submitter.disabled = true;
    try {await addReference($('contextReference').value.trim());$('contextReference').value = '';} catch (error) {announce(error.message);} finally {submitter.disabled = false;}
  });
  $('contextLibrary').addEventListener('click',contextAction);
  $('contextLibrary').addEventListener('click',readerAction);
  $('contextLibrary').addEventListener('change',readerAction);
  $('contextChips').addEventListener('click',contextAction);
  $('input').addEventListener('paste',event => {
    const files = event.clipboardData?.files;if (files?.length) {event.preventDefault();uploadFiles(files);return;}
    const text = event.clipboardData?.getData('text/plain');if (text) pastedReferences(text);
  });
  const main = $('mainView');
  const isContentDrag = event => [...(event.dataTransfer?.types || [])].some(x => ['Files','text/uri-list'].includes(x));
  main.addEventListener('dragenter',event => {if (!isContentDrag(event)) return;event.preventDefault();dragDepth++;main.classList.add('context-dragging');});
  main.addEventListener('dragover',event => {if (!isContentDrag(event)) return;event.preventDefault();event.dataTransfer.dropEffect = 'copy';});
  main.addEventListener('dragleave',event => {if (!isContentDrag(event)) return;if (--dragDepth <= 0) main.classList.remove('context-dragging');});
  main.addEventListener('drop',event => {if (!isContentDrag(event)) return;event.preventDefault();dragDepth = 0;main.classList.remove('context-dragging');if (event.dataTransfer.files.length) uploadFiles(event.dataTransfer.files);else pastedReferences(event.dataTransfer.getData('text/uri-list'));});
  window.setInterval(() => {if (dialog.open && !document.hidden && !mutating) load();},3500);
  renderContexts();
  return {refresh:(force=false) => load(force),contextIds,sent,open,display,setHost:value => {host = value;},ready,addReference,addTextMaterial,pending:() => uploads.size + references.size > 0};
}
