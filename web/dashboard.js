// 延迟仪表: 最近一轮的时间线瀑布图 + 本次会话的 P50/P95.
const KEY_METRICS = ['reaction_ms', 'intent_ms', 'motion_ms', 'voice_ms'];

const fmt = (ms) => (ms == null ? '—' : `${Math.round(ms)}`);

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k === 'style') node.style.cssText = v;
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c);
  return node;
}

export class Dashboard {
  constructor(root) {
    this.root = root;
    this.marks = [];
    this.metricLabels = {};
    this.kpis = root.querySelector('[data-kpis]');
    this.waterfall = root.querySelector('[data-waterfall]');
    this.table = root.querySelector('[data-stats]');
    this.meta = root.querySelector('[data-meta]');
  }

  init(marks, metricLabels, stats) {
    this.marks = marks;
    this.metricLabels = metricLabels;
    this.renderStats(stats);
  }

  renderTrace(trace) {
    const m = trace.metrics || {};
    this.kpis.replaceChildren(...KEY_METRICS.map((key) => el('div', { class: 'kpi' }, [
      el('div', { class: 'kpi-value', text: fmt(m[key]) + (m[key] == null ? '' : ' ms') }),
      el('div', { class: 'kpi-label', text: (this.metricLabels[key] || key).replace('★ ', '') }),
    ])));

    const marks = trace.marks || {};
    const names = this.marks.map(([name]) => name).filter((name) => name in marks);
    const label = Object.fromEntries(this.marks);
    const lo = Math.min(0, ...names.map((n) => marks[n]));
    const hi = Math.max(1, ...names.map((n) => marks[n]));
    const span = hi - lo;
    const pos = (t) => `${((t - lo) / span) * 100}%`;
    const zero = pos(0);
    const rows = names.sort((a, b) => marks[a] - marks[b]).map((name) => {
      const t = marks[name];
      const left = Math.min(t, 0), right = Math.max(t, 0);
      const bar = el('div', { class: `wf-bar${name.startsWith('client_') ? ' client' : ''}`,
        style: `left:${pos(left)};width:max(2px, calc(${pos(right)} - ${pos(left)}))` });
      return el('div', { class: 'wf-row' }, [
        el('div', { class: 'wf-label', text: label[name] || name, title: name }),
        el('div', { class: 'wf-track' }, [el('div', { class: 'wf-zero', style: `left:${zero}` }), bar]),
        el('div', { class: 'wf-value', text: `${t > 0 ? '+' : ''}${fmt(t)}` }),
      ]);
    });
    this.waterfall.replaceChildren(...rows);

    const llm = (trace.meta && trace.meta.llm) || {};
    const parts = [`第 ${trace.turn} 轮`, trace.source === 'voice' ? '语音' : '文字'];
    if (llm.completion_tokens != null) parts.push(`${llm.completion_tokens} tokens`);
    if (llm.timings && llm.timings.predicted_per_second) parts.push(`${Math.round(llm.timings.predicted_per_second)} tok/s`);
    if (trace.meta && trace.meta.speculative) parts.push('投机命中');
    if (trace.meta && trace.meta.merged_from) parts.push('合并了切早的半句');
    if (trace.interrupted) parts.push('被打断');
    if (trace.meta && trace.meta.parse_error) parts.push('输出解析失败');
    this.meta.textContent = parts.join(' · ') + ' · 时间相对"说完"';
  }

  renderStats(stats) {
    const rows = Object.entries((stats && stats.metrics) || {});
    if (!rows.length) {
      this.table.replaceChildren(el('tr', {}, [el('td', { colspan: '4', class: 'muted', text: '还没有数据, 说一句话或发一条消息.' })]));
      return;
    }
    this.table.replaceChildren(...rows.map(([key, s]) => el('tr', { class: s.label.startsWith('★') ? 'key' : '' }, [
      el('td', { text: s.label.replace('★ ', ''), title: key }),
      el('td', { class: 'num', text: fmt(s.p50) }),
      el('td', { class: 'num', text: fmt(s.p95) }),
      el('td', { class: 'num', text: String(s.n) }),
    ])));
  }
}
