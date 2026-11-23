// 设置面板"桌宠与工作台"页里的"她自己练动作" (动作成长, docs/roadmap/motion-growth.md, 服务端 wm/motion_growth.py):
// 开关 (默认关; 打开前确认一次: 会在电脑空闲时占 CPU)、现在用的是出厂参数还是她练出来的第几版、最近换了什么、一键退回上一版、
// 停下正在练的、这 30 天日常里记了多少动作毛病 (哪几种最多)、给开发的问题报告.
// 毛病的中文名在 /static/motion-faults.js (服务端给): 用到时才动态载入, 载不到就显示原始键名. 不能在顶层 import —— 面板是宿主从
// desktop/web 给的, 服务端没开时也要打得开; 顶层 import /static/ 失败会让整条 panel.js 的模块链载不起来, 面板白屏.

const st = { data: null, error: '', busy: false, labels: null };

async function labels(paint) {
  if (st.labels) return;
  st.labels = {};
  try {
    const { FAULTS } = await import('/static/motion-faults.js');
    st.labels = Object.fromEntries(Object.entries(FAULTS).map(([k, d]) => [k, d.label]));
    paint();
  } catch { /* 服务端没开: 显示键名 */ }
}

async function load(paint) {
  try {
    const r = await fetch('/api/motion/growth');
    st.data = r.ok ? await r.json() : null;
    st.error = r.ok ? '' : `服务端回了 ${r.status}`;
  } catch { st.error = '连不上服务端'; }
  paint();
}

async function post(body, paint) {
  st.busy = true;
  paint();
  try {
    const r = await fetch('/api/motion/growth', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) st.error = `没成功: ${await r.text()}`;
  } catch { st.error = '连不上服务端'; }
  st.busy = false;
  await load(paint);
}

const ACTION = { adopt: '换上', rollback: '退回', 'auto-rollback': '自动退回', suspend: '停用', drop: '不再用', switch: '开关' };
const OUTCOME = { adopted: '练好了, 换上了', rejected: '练出来的没过验证, 没换', report: '调参救不了, 写了问题报告', stopped: '你回来了, 先停了',
  idle: '没有要练的', skip: '没开始' };
const verName = (v) => (v === 'factory' ? '出厂参数' : v === 'base' ? '基础参数' : `她练出来的 ${v}`);
const CAND = { pending: '待核对', confirmed: '核对过了, 没换上', rejected: '核对没过', adopted: '已换上' };

/** el: 放这一块的元素; ctx: 面板的 { esc }. */
export function renderGrowth(el, ctx) {
  const { esc } = ctx;
  const paint = () => renderGrowth(el, ctx);
  const d = st.data;
  if (!d) {
    el.innerHTML = `<h2>动作成长</h2><p class="empty">${esc(st.error || '读取中…')}</p>`;
    if (!st.error) load(paint);
    return;
  }
  labels(paint);
  const faults = Object.entries(d.faults_30d || {}).map(([k, n]) => [...k.split('|'), n]);
  const motion = faults.filter(([, cls]) => cls === 'motion').sort((a, b) => b[2] - a[2]);
  const env = faults.filter(([, cls]) => cls === 'env').reduce((s, [, , n]) => s + n, 0);
  const top = motion.slice(0, 4).map(([k, , n]) => `${esc(st.labels?.[k] || k)} ${n} 次`).join('; ');
  const last = d.last_change, run = d.last_run;
  const slots = Object.values(d.slots || {}).filter((x) => x.slot !== 'global');
  const cands = d.candidates || [];
  el.innerHTML = `
    <h2>动作成长</h2>
    <p class="hint">在电脑空闲时练习动作，验证通过后应用。你回来时自动暂停。</p>
    <details class="setting-details"><summary>了解练习方式与资源占用</summary><p class="hint">她在小屋里动作出了毛病 (放东西时手砸下去、隔空拿东西、脚打滑…) 会自己记下来. 打开以后, 电脑空闲时
      (你没在用键盘鼠标、没全屏、没在通话、插着电、她没在和你说话) 她在后台练: 只调和这个毛病有关的参数, 验证过不比原来差才换上,
      换上以后变差会自己退回. 练的时候占 CPU、不占显卡, 你一回来就停. 不打开也照样记.</p></details>
    <label class="toggle-row ${d.can_run ? '' : 'off'}">
      <span class="tr-text"><b>空闲时自动练习</b><span class="hint">${d.can_run ? '默认关闭。练习占用 CPU，不占用显卡。' : `当前设备无法练习：${esc(d.cannot_run || '')}`}</span></span>
      <input type="checkbox" class="switch" id="growthAuto" ${d.auto ? 'checked' : ''} ${st.busy || (!d.can_run && !d.auto) ? 'disabled' : ''}>
    </label>
    <div class="row top"><label>现在用的</label><span class="tr-text">基础 (所有形象): ${esc(verName(d.active))}${
      d.since ? ` (${esc(d.since.replace('T', ' '))} 起)` : ''}
      ${slots.map((x) => `<span>${esc(x.file || x.slot)}: ${esc(verName(x.version))} (${esc((x.since || '').replace('T', ' '))} 起)
        <button type="button" class="btn small ghost" data-slot="${esc(x.slot)}" ${st.busy ? 'disabled' : ''}>退回</button></span>`).join('')}
      <span class="hint">参数按形象各存一份: 一个形象有自己练出来的那一版就用它, 没有就用基础的; 换了模型 (内容变了) 也回到基础的.${
      (d.revalidate || []).length ? ` 等着重新验证: ${esc(d.revalidate.map((x) => x.version).join(', '))}.` : ''}${
      (d.pending_contract || []).length ? ` 旧格式、待补验收: ${esc(d.pending_contract.join(', '))}.` : ''}</span></span></div>
    ${cands.length ? `<div class="row top"><label>候选</label><span class="tr-text">${cands.map((c) => `${esc(c.id)} ${esc(c.problem || '')} → ${
      esc((c.scope?.avatars || []).map((a) => a.file).join(', '))}: ${esc(CAND[c.status] || c.status)}${
      c.stale ? ' (核对以后动作代码改过, 换上前要重核)' : ''}`).join('<br>')}
      <span class="hint">练出来、只对某个形象好的那一版先当候选, 按验收清单核对、再用没参与挑选的样本确认, 你同意了才换上.</span></span></div>` : ''}
    ${last ? `<div class="row top"><label>最近换了什么</label><span class="tr-text">${esc((last.at || '').replace('T', ' '))} ${esc(ACTION[last.action] || last.action)}
      ${esc(verName(last.from))} → ${esc(verName(last.version))}<span class="hint">${esc(last.why || '')}</span></span></div>` : ''}
    ${run ? `<div class="row top"><label>上次练</label><span class="tr-text">${esc((run.at || '').replace('T', ' '))} ${esc(OUTCOME[run.outcome] || run.outcome || '')}
      <span class="hint">${esc(run.problem || run.step || '')}${run.why ? `: ${esc(run.why)}` : ''}</span></span></div>` : ''}
    <div class="row top"><label>这 30 天记下的</label><span class="tr-text">动作的毛病 ${motion.reduce((s, [, , n]) => s + n, 0)} 条, 环境本来就难的 ${env} 条
      <span class="hint">${top ? `最多的: ${top}. ` : ''}环境本来就难的 (满杯容易洒这类) 交给她自己学, 这里不调.
      练好了说的"少了多少"是毛病超出阈值的量 (程度), 次数另算; 次数没少时她只说"轻一点了".${
      d.reports?.length ? ` 调参救不了、写给开发的问题报告 ${d.reports.length} 份.` : ''}</span></span></div>
    <div class="actions">
      ${d.running ? `<button type="button" class="btn" id="growthStop" ${st.busy ? 'disabled' : ''}>停下正在练的</button>` : ''}
      <button type="button" class="btn ghost" id="growthBack" ${st.busy || d.active === 'factory' ? 'disabled' : ''}>基础参数退回上一版</button>
    </div>
    ${st.error ? `<p class="hint">${esc(st.error)}</p>` : ''}`;
  el.querySelector('#growthAuto').addEventListener('change', (e) => {
    if (e.target.checked && !confirm('打开以后, 电脑空闲时她会在后台跑评估、调动作参数 (占 CPU、不占显卡), 你一动键盘鼠标就停; 验证过不比原来差才换上, 随时能退回.\n\n确定打开吗?')) {
      e.target.checked = false;
      return;
    }
    post({ auto: e.target.checked }, paint);
  });
  el.querySelector('#growthStop')?.addEventListener('click', () => post({ stop: true }, paint));
  el.querySelector('#growthBack').addEventListener('click', () => {
    if (confirm(`基础参数退回上一版? 现在用的是${verName(d.active)}. 小屋里她闲下来就换回去.`)) post({ rollback: true }, paint);
  });
  el.querySelectorAll('[data-slot]').forEach((b) => b.addEventListener('click', () => {
    const x = slots.find((y) => y.slot === b.dataset.slot);
    if (x && confirm(`${x.file || x.slot} 退回上一版? 现在用的是${verName(x.version)}.`)) post({ rollback: true, slot: x.slot }, paint);
  }));
}

/** 换页回来时重新读. */
export function growthForget() { st.data = null; st.error = ''; }
