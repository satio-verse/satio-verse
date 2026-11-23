// 操作面板的"账号与余额"页 (panel.js 引进来用; 2026-10-26 用户: "账本、DeepSeek 余额、峰谷, 加设置里的账号与余额页",
// "还有命中率、缓存、kv cache、未命中率、token 消耗量等等"):
//   DeepSeek 余额 (服务端去查, key 不经过面板)、现在是高峰还是空闲 (北京时间的 24 小时时间条) 和价格表、
//   今天和这个月花了多少 (token、缓存命中 / 没命中、谁花的、本机模型的 KV cache 复用)、最近 14 天、最近的记录.
// 数据全在服务端的账本 (wm/ledger.py, GET /api/billing). 页面开着时每 5 秒刷新; 余额服务端每分钟最多查一次, "刷新"马上查.
// 按渠道的额度 (2026-10-27 用户: "per-channel token budgets (QQ / B站 / desk pet / other, warn at 80%, stop at 100%)"):
//   每个渠道和总额今天用了多少、上限 (0 = 不限)、进度条; 改了点保存 (POST /api/billing/budget). 正在填的时候不自动刷新, 填的数先留着.
const bill = { data: null, error: '', timer: 0, refreshing: false, table: false, pricing: false, draft: null, editing: false, saving: false, saveError: '', section: 'overview' };
const LEVEL = { off: '不限', ok: '', warn: '⚠ 快到了', stop: '⛔ 用完了, 先停' };
const WHO = { pet: '桌宠', channels: '外部渠道', workbench: '她的研究 (dsh)', agent: '工作台' };
const TIER = { peak: '高峰', off: '空闲' };

const money = (x) => (x == null ? '—' : `¥${x >= 0.01 || x === 0 ? x.toFixed(2) : x.toFixed(4)}`);
const fine = (x) => (x == null ? '—' : `¥${x.toFixed(4)}`);
const tok = (n) => (n >= 1e8 ? `${(n / 1e8).toFixed(2)} 亿` : n >= 1e4 ? `${(n / 1e4).toFixed(1)} 万` : String(n ?? 0));
const pct = (r) => (r == null ? '—' : `${(r * 100).toFixed(r < 0.1 && r > 0 ? 1 : 0)}%`);
const span = (s) => {
  const m = Math.max(1, Math.round(s / 60)), h = Math.floor(m / 60);
  return h ? `${h} 小时${m % 60 ? ` ${m % 60} 分钟` : ''}` : `${m} 分钟`;
};
const clock = (t) => new Date(t * 1000).toLocaleTimeString('zh-CN', { hour12: false });

async function load(ctx, refresh = false) {
  try {
    const r = await fetch(refresh ? '/api/billing/refresh' : '/api/billing', { method: refresh ? 'POST' : 'GET' });
    if (r.status === 404) throw Object.assign(new Error('old'), { old: true });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    bill.data = await r.json();
    bill.error = '';
  } catch (err) {
    bill.error = err.old ? '服务端还没有账本 (是加账本之前起的): 重启服务端以后才有'
      : `连不上服务端 (${err.message}): 服务端开着才有账本`;
  }
  if (ctx.isTab() && !bill.editing) ctx.render();
}

async function saveBudget(ctx) {
  const d = bill.draft;
  bill.saving = true;
  ctx.render();
  try {
    const r = await fetch('/api/billing/budget', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ unit: d.unit, limits: d.limits }) });
    if (r.status === 404) throw new Error('服务端是加额度之前起的: 重启服务端以后才能设');
    if (!r.ok) throw new Error(await r.text() || `HTTP ${r.status}`);
    bill.data = await r.json();
    bill.draft = null;
    bill.saveError = '';
  } catch (err) {
    bill.saveError = err.message;
  }
  bill.saving = false;
  if (ctx.isTab()) ctx.render();
}

/** 按渠道的额度: 一行一个渠道 (最后是总额), 今天用了多少 (token 和元都写)、上限输入框、进度条和档. */
function budgetCard(b, esc) {
  if (!b) return '';
  const d = bill.draft || { unit: b.unit, limits: Object.fromEntries(b.rows.map((r) => [r.id, r.limit])) };
  const yuan = d.unit === 'yuan';
  const rows = b.rows.map((r) => {
    const w = r.ratio == null ? 0 : Math.min(100, Math.round(r.ratio * 100));
    const state = r.level === 'off' ? LEVEL.off : `${r.level === 'ok' ? '' : `${LEVEL[r.level]} · `}${pct(r.ratio)}`;
    return `<tr class="lv-${r.level}${r.id === 'total' ? ' total' : ''}">
      <td>${esc(r.id === 'total' ? '总额 (加起来)' : r.name)}</td>
      <td>${tok(r.tokens)} token · ${money(r.cost)}</td>
      <td><input type="number" min="0" step="${yuan ? '0.01' : '1000'}" data-limit="${esc(r.id)}" value="${d.limits[r.id] ?? 0}"
        aria-label="${esc(r.name)}每天的上限"></td>
      <td class="bud"><div class="bar ${r.level}"><i style="width:${w}%"></i></div><span class="hint">${esc(state)}</span></td></tr>`;
  }).join('');
  const tight = b.rows.filter((r) => r.level === 'warn' || r.level === 'stop');
  return `<div class="card" id="bBudget" data-billing="budget">
    <h2>每日使用额度 ${tight.length ? `<span class="badge warn-badge">${esc(tight.map((r) => r.name).join('、'))}</span>` : ''}</h2>
    <p class="hint">每天每个渠道最多用多少云端模型. 到 ${pct(b.warn)} 在桌宠的对话栏提醒一次, 到 100% 这个渠道先不用云端模型 (QQ 先不回、B站先不看);
      0 是不限. 本机模型不花钱, 不算. 每天零点重新算 (这台电脑的零点, 北京时间 ${esc(b.reset_bj)}).</p>
    <div class="inline budget-unit">单位 <select id="bUnit"><option value="tokens" ${yuan ? '' : 'selected'}>token</option>
      <option value="yuan" ${yuan ? 'selected' : ''}>元 (价格表里有的模型)</option></select></div>
    <table class="num budget"><thead><tr><th>渠道</th><th>今天用了</th><th>上限 (${yuan ? '元' : 'token'})</th><th>进度</th></tr></thead>
      <tbody>${rows}</tbody></table>
    <div class="actions"><button type="button" class="btn primary" id="bBudgetSave" ${bill.draft && !bill.saving ? '' : 'disabled'}>
      ${bill.saving ? '保存中…' : '保存'}</button>${bill.draft ? ' <span class="hint">改了还没保存</span>' : ''}</div>
    ${bill.saveError ? `<div class="hint err-text">${esc(bill.saveError)}</div>` : ''}
    <div class="hint">外部渠道页的"每天上限"是外部渠道自己的 token 上限 (群里最多用其中一部分), 这里是所有渠道的总闸门, 两个哪个先到就听哪个.</div>
  </div>`;
}

function bindBudget(main, ctx) {
  const card = ctx.$('#bBudget');
  if (!card) return;
  const b = bill.data.budget;
  const draft = () => (bill.draft ||= { unit: b.unit, limits: Object.fromEntries(b.rows.map((r) => [r.id, r.limit])) });
  card.addEventListener('focusin', () => { bill.editing = true; });
  card.addEventListener('focusout', () => setTimeout(() => { bill.editing = card.contains(document.activeElement); }, 0));
  ctx.$$('[data-limit]', card).forEach((el) => el.addEventListener('input', () => {
    draft().limits[el.dataset.limit] = el.value === '' ? 0 : Number(el.value);
    ctx.$('#bBudgetSave').disabled = false;
  }));
  ctx.$('#bUnit').addEventListener('change', (e) => {
    draft().unit = e.target.value;
    bill.editing = false;
    ctx.render();
  });
  ctx.$('#bBudgetSave').addEventListener('click', () => { bill.editing = false; saveBudget(ctx); });
}

/** ctx: { esc, $, $$, render, isTab() } */
export function renderBilling(main, ctx) {
  const { esc } = ctx;
  clearInterval(bill.timer);
  bill.timer = setInterval(() => (ctx.isTab() ? load(ctx) : clearInterval(bill.timer)), 5000);
  if (!bill.data) {
    main.innerHTML = `<h1>账号与余额</h1><p class="empty">${esc(bill.error || '读取中…')}</p>`;
    if (!bill.error) load(ctx);
    return;
  }
  const d = bill.data, b = d.balance || {}, now = d.now, t = d.today, mo = d.month;
  const y = main.scrollTop;
  main.innerHTML = `
    <h1>账号与余额</h1>
    <p class="lead">查看余额和模型用量，为每个渠道设置每日额度。</p>
    <div class="subtabs" role="tablist" aria-label="账单分类">${[['overview', '概览'], ['usage', '用量明细'], ['budget', '额度']].map(([id, label]) => `<button type="button" role="tab" data-billing-tab="${id}" aria-selected="${bill.section === id}" class="${bill.section === id ? 'on' : ''}">${label}${id === 'budget' && bill.draft ? ' · 未保存' : ''}</button>`).join('')}</div>
    ${bill.error ? `<div class="note err">${esc(bill.error)}</div>` : ''}
    <div class="card" data-billing="overview">
      <h2>DeepSeek 余额 ${b.low ? '<span class="badge warn-badge">⚠ 不多了</span>' : ''}</h2>
      ${b.total != null ? `
        <div class="hero">${money(b.total)}</div>
        <div class="hint">充值 ${money(b.topped_up)} · 赠金 ${money(b.granted)}${b.available === false ? ' · <b>DeepSeek 说现在不能用</b>' : ''}</div>
        <div class="hint">今天余额少了 ${money(b.today_drop ?? 0)}${b.today_topup ? ` (今天充值 / 赠金 ${money(b.today_topup)})` : ''},
          包括别的程序 (比如你自己用的 dsh) 花的</div>
        <div class="hint">${esc(b.source || '')} · ${clock(b.at)} 查的</div>` : '<p class="empty">还没查到余额</p>'}
      ${b.error ? `<div class="hint err-text">${esc(b.error)}</div>` : ''}
      <div class="actions"><button type="button" class="btn" id="bRefresh" ${bill.refreshing ? 'disabled' : ''}>${bill.refreshing ? '查询中…' : '刷新余额'}</button></div>
    </div>
    ${budgetCard(d.budget, esc)}
    <div class="card" data-billing="overview usage">
      <h2>今日用量</h2>
      ${tiles(t)}
      ${t.hit + t.miss ? hitBar(t) : '<p class="empty">今天还没用过云端模型</p>'}
      <div class="hint">本机模型：${t.local.turns} 次 · 不计费</div>
      ${Object.keys(t.by_who).length || t.local.turns ? `<details class="setting-details" id="bUsageSources"><summary>按使用来源查看</summary>${Object.keys(t.by_who).length ? whoTable(t, esc) : ''}
        ${t.local.turns ? `<div class="hint">本机模型 KV cache 复用 ${pct(t.local.hit_rate)}
          (复用 ${tok(t.local.hit)} · 新算 ${tok(t.local.miss)} · 生成 ${tok(t.local.out)} token)</div>` : ''}</details>` : ''}
      ${t.unpriced ? `<div class="hint">有 ${t.unpriced} 次用的模型不在价格表里, 只记了 token, 没算钱</div>` : ''}
    </div>
    ${now ? peakCard(now, d.prices, esc) : ''}
    <div class="card" data-billing="usage">
      <h2>本月用量</h2>
      ${tiles(mo)}
    </div>
    <div class="card" data-billing="overview usage">
      <h2>近 14 天花费</h2>
      ${daysChart(d.days)}
      <details id="bDaysTable" ${bill.table ? 'open' : ''}><summary>看表格</summary>${daysTable(d.days)}</details>
    </div>
    <div class="card" data-billing="usage">
      <h2>最近的记录</h2>
      ${recentTable(d.recent, esc)}
      <div class="hint">账本在服务端日志目录的 <code>billing\\</code> 里, 一天一个文件. 本机模型每次复用的 KV cache 也记在里面.</div>
    </div>
    <div class="tip" id="bTip" hidden></div>`;
  main.scrollTop = y;
  ctx.$$('[data-billing]', main).forEach((section) => { section.hidden = !section.dataset.billing.split(' ').includes(bill.section); });
  ctx.$$('[data-billing-tab]', main).forEach((button) => button.addEventListener('click', () => {
    bill.section = button.dataset.billingTab;
    bill.editing = false;
    ctx.render();
    main.scrollTop = 0;
    main.querySelector(`[data-billing-tab="${bill.section}"]`)?.focus();
  }));
  ctx.$('#bRefresh').addEventListener('click', async () => {
    bill.refreshing = true;
    ctx.render();
    await load(ctx, true);
    bill.refreshing = false;
    if (ctx.isTab()) ctx.render();
  });
  ctx.$('#bDaysTable').addEventListener('toggle', (e) => { bill.table = e.target.open; });
  ctx.$('#bPricing')?.addEventListener('toggle', (e) => { bill.pricing = e.target.open; });
  bindBudget(main, ctx);
  bindTips(main, ctx);
}

function tiles(a) {
  const rate = a.hit_rate == null ? '—' : pct(a.hit_rate);
  return `<div class="tiles">
    <div class="tile"><div class="tl">花费</div><div class="tv">${money(a.cost)}</div>
      <div class="ts">缓存省了 ${money(a.saved)}</div></div>
    <div class="tile"><div class="tl">token</div><div class="tv">${tok(a.tokens)}</div>
      <div class="ts">输入 ${tok(a.hit + a.miss)} · 输出 ${tok(a.out)}${a.think ? ` (思考 ${tok(a.think)})` : ''}</div></div>
    <div class="tile"><div class="tl">缓存命中率</div><div class="tv">${rate}</div>
      <div class="ts">没命中 ${pct(a.miss_rate)}</div></div>
    <div class="tile"><div class="tl">请求</div><div class="tv">${a.turns} 次</div>
      <div class="ts">高峰时段 ${a.peak_turns} 次</div></div>
  </div>`;
}

/** 输入 token 里命中缓存 (服务器复用了算好的 KV cache, 价钱是没命中的 1/50) 和没命中的比例: 一根横条, 中间 2px 空隙. */
function hitBar(a) {
  const total = a.hit + a.miss, W = 720, H = 18, gap = 2;
  const hw = total ? Math.round((a.hit / total) * W) : 0;
  const segs = [];
  if (a.hit) segs.push({ x: 0, w: a.miss ? Math.max(1, hw - gap / 2) : W, cls: 's1', label: '命中缓存', n: a.hit, r: a.hit_rate });
  if (a.miss) segs.push({ x: a.hit ? hw + gap / 2 : 0, w: a.hit ? W - hw - gap / 2 : W, cls: 's2', label: '没命中', n: a.miss, r: a.miss_rate });
  const rect = (s, i) => {  // 两头 4px 圆角, 中间方的
    const r = 4, L = i === 0, R = i === segs.length - 1, x0 = s.x, x1 = s.x + s.w;
    const d = `M${x0 + (L ? r : 0)},0 H${x1 - (R ? r : 0)} ${R ? `Q${x1},0 ${x1},${r} V${H - r} Q${x1},${H} ${x1 - r},${H}` : `V${H}`} H${x0 + (L ? r : 0)} ${L ? `Q${x0},${H} ${x0},${H - r} V${r} Q${x0},0 ${x0 + r},0` : `V0`} Z`;
    return `<path class="${s.cls}" d="${d}" data-tip="${s.label} ${tok(s.n)} token (${pct(s.r)})" tabindex="0"></path>`;
  };
  return `<div class="viz-label">输入 token 里命中缓存的比例</div>
    <svg class="hitbar" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="命中缓存 ${pct(a.hit_rate)}">${segs.map(rect).join('')}</svg>
    <div class="legend"><span><i class="key s1"></i>命中缓存 <b>${tok(a.hit)}</b> (${pct(a.hit_rate)})</span>
      <span><i class="key s2"></i>没命中 <b>${tok(a.miss)}</b> (${pct(a.miss_rate)})</span></div>`;
}

function whoTable(a, esc) {
  const rows = Object.entries(a.by_who).sort((x, y) => y[1].cost - x[1].cost || y[1].tokens - x[1].tokens);
  return `<table class="num"><thead><tr><th>谁花的</th><th>次数</th><th>token</th><th>花费</th></tr></thead><tbody>
    ${rows.map(([w, v]) => `<tr><td>${esc(WHO[w] || w)}</td><td>${v.turns}</td><td>${tok(v.tokens)}</td><td>${money(v.cost)}</td></tr>`).join('')}
  </tbody></table>`;
}

/** 北京时间今天的 24 小时: 高峰段填色、写"高峰", 竖线是现在. */
function peakCard(now, prices, esc) {
  const W = 720, x = (m) => (m / 1440) * W;
  const segs = now.segments.map(([a, b]) => `<rect class="s2" x="${x(a)}" y="10" width="${x(b) - x(a)}" height="16" rx="4"></rect>
    <text class="in" x="${(x(a) + x(b)) / 2}" y="22">高峰</text>`).join('');
  const ticks = [0, 6, 9, 12, 14, 18, 24].map((h) => `<text class="tick" x="${Math.min(W - 8, Math.max(8, x(h * 60)))}" y="44">${h}:00</text>`).join('');
  const nx = x(now.minute);
  const next = now.next_in_s == null ? '' : `, ${span(now.next_in_s)}后${now.next_tier === 'peak' ? '进入高峰' : '回到空闲'}`;
  const offNote = now.tier === 'off' ? ` (价钱是高峰的 ${now.off_ratio})` : '';
  return `<div class="card" data-billing="usage">
    <h2>计费时段 <span class="badge ${now.tier === 'peak' ? 'peak-badge' : 'off'}">${now.tier === 'peak' ? '高峰时段' : '空闲时段'}</span></h2>
    <p class="hint">现在是${TIER[now.tier]}时段${offNote}${next}。</p>
    <details class="setting-details" id="bPricing" ${bill.pricing ? 'open' : ''}><summary>查看计费规则</summary>
    <svg class="timeline" viewBox="0 0 ${W} 48" role="img" aria-label="北京时间 ${esc(now.date)} 的高峰时段">
      <rect class="track" x="0" y="10" width="${W}" height="16" rx="4"></rect>${segs}
      ${now.segments.length ? '' : `<text class="in dim" x="${W / 2}" y="22">今天全天空闲 (周末或节假日)</text>`}
      <line class="now" x1="${nx}" x2="${nx}" y1="4" y2="32"></line><circle class="now" cx="${nx}" cy="4" r="4"></circle>${ticks}
    </svg>
    <div class="hint">北京时间 ${esc(now.date)}. DeepSeek 的规则: 周一到周五 (不含法定节假日) 9:00–12:00、14:00–18:00 是高峰, 其余时间 (包括周末、节假日全天) 空闲, 空闲五折.</div>
    <table class="num"><thead><tr><th>模型 (元 / 百万 token, 空闲 / 高峰)</th><th>命中缓存</th><th>没命中</th><th>输出</th></tr></thead><tbody>
      ${prices.map((p) => `<tr><td>${esc(p.name)}</td>${['hit', 'miss', 'out'].map((k) => `<td>${p[k][0]} / ${p[k][1]}</td>`).join('')}</tr>`).join('')}
    </tbody></table>
    <div class="hint">价格和节假日在服务端配置的 <code>billing</code> 段 (2026-10-26 照 DeepSeek 官方价格页填的), 变了改那里.</div>
  </details></div>`;
}

/** 最近 14 天每天的花费: 柱子 (≤ 24px, 顶上 4px 圆角), 横线是刻度, 只在今天的柱子顶上写数; 其余悬停看. */
function daysChart(days) {
  const W = 720, H = 170, L = 44, R = 8, T = 18, B = 26, pw = W - L - R, ph = H - T - B;
  const max = Math.max(...days.map((d) => d.cost), 0);
  if (!max) return '<p class="empty">最近 14 天还没花过钱</p>';
  const step = niceStep(max / 2), top = step * Math.ceil(max / step), band = pw / days.length, bw = Math.min(24, band * 0.6);
  const yv = (v) => T + ph - (v / top) * ph;
  const grid = [0, top / 2, top].map((v) => `<line class="grid" x1="${L}" x2="${W - R}" y1="${yv(v)}" y2="${yv(v)}"></line>
    <text class="tick" x="${L - 6}" y="${yv(v) + 4}" text-anchor="end">${v ? `¥${fmtTick(v)}` : '0'}</text>`).join('');
  const bars = days.map((d, i) => {
    const cx = L + band * (i + 0.5), h = (d.cost / top) * ph, x0 = cx - bw / 2, y0 = T + ph - h, r = Math.min(4, h);
    const path = h > 0 ? `<path class="s1" d="M${x0},${T + ph} V${y0 + r} Q${x0},${y0} ${x0 + r},${y0} H${x0 + bw - r} Q${x0 + bw},${y0} ${x0 + bw},${y0 + r} V${T + ph} Z"></path>` : '';
    const last = i === days.length - 1;
    const label = last && h > 0 ? `<text class="val" x="${cx}" y="${y0 - 5}">${money(d.cost)}</text>` : '';
    const xl = i % 2 === days.length % 2 || last ? `<text class="tick" x="${cx}" y="${H - 8}">${d.day.slice(5)}</text>` : '';
    const tip = `${d.day} · ${money(d.cost)} · ${d.turns} 次 · 命中 ${pct(d.hit_rate)}`;
    // 悬停层垫在柱子下面 (柱子和字不接鼠标, 事件落到它上面), 高亮不压暗柱子
    return `<rect class="hit" x="${L + band * i}" y="${T}" width="${band}" height="${ph}" data-tip="${tip}" tabindex="0"></rect>${path}${label}${xl}`;
  }).join('');
  return `<svg class="days" viewBox="0 0 ${W} ${H}" role="img" aria-label="最近 14 天每天的花费">${grid}
    <line class="base" x1="${L}" x2="${W - R}" y1="${T + ph}" y2="${T + ph}"></line>${bars}</svg>`;
}

function niceStep(x) {
  const p = 10 ** Math.floor(Math.log10(x)), f = x / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}
const fmtTick = (v) => (v >= 1 ? v.toFixed(v % 1 ? 1 : 0) : String(+v.toPrecision(2)));

function daysTable(days) {
  return `<table class="num"><thead><tr><th>日期</th><th>花费</th><th>请求</th><th>命中缓存</th><th>没命中</th><th>输出</th><th>命中率</th></tr></thead><tbody>
    ${[...days].reverse().map((d) => `<tr><td>${d.day}</td><td>${money(d.cost)}</td><td>${d.turns}</td><td>${tok(d.hit)}</td><td>${tok(d.miss)}</td>
      <td>${tok(d.out)}</td><td>${pct(d.hit_rate)}</td></tr>`).join('')}</tbody></table>`;
}

function recentTable(recent, esc) {
  if (!recent.length) return '<p class="empty">今天还没有记录</p>';
  return `<div class="scroll"><table class="num"><thead><tr><th>时间</th><th>谁</th><th>模型</th><th>命中</th><th>没命中</th><th>输出</th><th>时段</th><th>花费</th></tr></thead><tbody>
    ${recent.map((r) => `<tr><td>${clock(r.t)}</td><td>${esc(WHO[r.who] || r.who)}</td>
      <td>${r.provider === 'local' ? '本机 · ' : ''}${esc(r.model)}</td><td>${tok(r.hit)}</td><td>${tok(r.miss)}</td><td>${tok(r.out)}</td>
      <td>${r.tier ? TIER[r.tier] : '—'}</td><td>${r.provider === 'local' ? '不花钱' : fine(r.cost)}</td></tr>`).join('')}
  </tbody></table></div>`;
}

/** 柱子、横条的悬停 / 键盘焦点: 一个提示框, 文字用 textContent. */
function bindTips(main, ctx) {
  const tip = ctx.$('#bTip');
  const show = (el, x, y) => {
    tip.textContent = el.dataset.tip;
    tip.hidden = false;
    const box = main.getBoundingClientRect();
    tip.style.left = `${Math.min(x - box.left + 12, main.clientWidth - tip.offsetWidth - 8)}px`;
    tip.style.top = `${y - box.top + main.scrollTop + 14}px`;
  };
  ctx.$$('[data-tip]', main).forEach((el) => {
    el.addEventListener('pointermove', (e) => show(el, e.clientX, e.clientY));
    el.addEventListener('pointerleave', () => { tip.hidden = true; });
    el.addEventListener('focus', () => { const r = el.getBoundingClientRect(); show(el, r.left + r.width / 2, r.top); });
    el.addEventListener('blur', () => { tip.hidden = true; });
  });
}

/** 面板自检 (panel.js 的 selftest 调): 账本接口通、这一页画得出来. 返回 [{name, ok, ...}]. */
export async function billingSelftest(show, $) {
  const out = [];
  let d = null;
  try { d = await (await fetch('/api/billing')).json(); } catch { /* 下面记失败 */ }
  out.push({ name: '账本接口 /api/billing 通, 有峰谷和 14 天', ok: !!d?.now?.tier && d.days?.length === 14 });
  bill.data = null;
  show('billing');
  for (let i = 0; i < 100 && !bill.data; i++) await new Promise((r) => setTimeout(r, 100));  // 机器忙时服务端慢, 最多等 10 秒
  const svg = $('#main svg.timeline');
  out.push({ name: '账号与余额页画出来了 (峰谷时间条、今天、本月)', ok: !!svg && $('#main').textContent.includes('本月用量') });
  const rows = document.querySelectorAll('#bBudget [data-limit]');
  out.push({ name: '按渠道的额度: QQ / B站 / 桌宠 / 其他 / 总额 五个上限', ok: rows.length === 5 && !!d?.budget?.rows });
  return out;
}
