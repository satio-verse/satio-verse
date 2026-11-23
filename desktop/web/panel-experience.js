// 设置面板"桌宠与工作台"页里的"她在小窝里攒的经历" (服务端 /api/experience, wm/cognition/store.py; 2026-10-27 用户贴的评审:
// "设置页应提供导出小窝经历和重置小窝学习, 同时展示范围. 导出先在本地预览并选择目的地").
// - 先列出有哪些、各多少条、存在哪 (预览);
// - 导出: 服务端打成 zip, 宿主弹系统的保存对话框让你选位置 (op save-as), 存好在资源管理器里选中;
// - 删除: 只删这几个经历文件 (服务端里记着的也清掉, 删除代次加一, 桌宠页面连上时把本机那份发现簿也清掉).
// - 完整重置小窝学习: POST {action: "reset", confirm: true} (scope 不给 = 全部: 经历、动作成长退回出厂、来源标着"小窝"的「我的理解」、
//   页面里的练习参数); 回包 {generation, cleared, kept} 照原样列出来. 页面那一半见 room-gen.js.
// 卡片和确认框上写清楚删什么、不删什么 (按认知会话定的覆盖范围, 2026-10-28).
const st = { data: null, error: '', busy: false, note: '', result: null, history: false };  // history: 卡片上"连训练历史一起清"勾着没

// 服务端 store.FILES 里的几种 (不认识的照文件名显示)
const KINDS = {
  'room-events.jsonl': '她以为东西在哪、找到过哪里',
  'carry-events.jsonl': '端东西的经验',
  'afford-events.jsonl': '小窝里看见过什么、试过什么、喜不喜欢',
};
// "删掉这些经历"不包括的 (前三样要用"完整重置小窝学习")
const NOT_HERE = ['小屋里的练习参数', '她自己练出来的动作参数 (动作成长)', '来源标着"小窝"的「我的理解」', '聊天记忆和日记', '身体状态、房间摆放'];
// "完整重置"会清的 (确认框里列; 清完照服务端的回包再列一遍)
const RESET_CLEARS = ['上面那几样经历 (连桌宠页面里那份发现簿)', '小屋里的练习参数 (存在桌宠页面里, 它下次连上时清)',
  '她练出来的动作参数, 退回出厂 ("空闲时自己练"的开关留着)', '来源标着"小窝"的「我的理解」'];
// 服务端回包的 kept 没拿到时用这份 (和认知会话 2026-10-28 定的一样)
const RESET_KEEPS = ['聊天记忆和日记', '没标来源的「我的理解」', '身体状态', '房间摆放、东西倒下停住的样子', '液体和水渍', '需求', '工具授权', 'QQ',
  '"空闲时自己练"开关', '仓库里的开发实验数据'];
// 动作成长的训练历史 (毛病记录、换上过的每一版、练习记录、问题报告) 默认留着: 评审 "退回出厂参数不等于删除历史训练记录";
// 要清得在卡片上另外勾 (scope 加 growth-history)
const HISTORY = '动作成长的训练历史 (毛病记录、她换上过的每一版、练习记录、问题报告)';
const SCOPE = ['experience', 'growth', 'understanding', 'page'];
const SCOPE_NAMES = { experience: '经历', growth: '动作成长', 'growth-history': '动作成长的训练历史', understanding: '「我的理解」', page: '桌宠页面里的' };

const human = (n) => (n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} 字节`);

async function load(paint) {
  try {
    const r = await fetch('/api/experience', { cache: 'no-store' });
    if (r.status === 404) throw new Error('服务端是加这个接口之前起的: 重启服务端以后才能用');
    if (!r.ok) throw new Error(`服务端回了 ${r.status}`);
    st.data = await r.json();
    st.error = '';
  } catch (err) {
    st.data = null;
    st.error = err.message.includes('重启') || err.message.includes('回了') ? err.message : '连不上服务端: 服务端开着才能看、导出、删除';
  }
  paint();
}

const blobBase64 = (blob) => new Promise((resolve, reject) => {
  const fr = new FileReader();
  fr.onload = () => resolve(String(fr.result).split(',', 2)[1] || '');
  fr.onerror = () => reject(fr.error);
  fr.readAsDataURL(blob);
});

async function exportZip(ctx, paint) {
  st.busy = true;
  st.note = '';
  paint();
  try {
    const r = await fetch('/api/experience?export=1', { cache: 'no-store' });
    if (!r.ok) throw new Error(`服务端回了 ${r.status}`);
    const blob = await r.blob();
    const t = new Date(), two = (n) => String(n).padStart(2, '0');  // 本机时间 (不是 UTC)
    const stamp = `${t.getFullYear()}${two(t.getMonth() + 1)}${two(t.getDate())}-${two(t.getHours())}${two(t.getMinutes())}`;
    const out = await ctx.call('save-as', { name: `她在小窝的经历-${stamp}.zip`, ext: 'zip', data: await blobBase64(blob) });
    if (out?.ok) st.note = `存好了: ${out.path} (${human(out.bytes || blob.size)})`;
    else if (out?.cancelled) st.note = '没存 (取消了)';
    else st.note = `没存成: ${out?.error || '宿主没回 (旧版桌宠?)'}`;
  } catch (err) {
    st.note = `没导出成: ${err.message}`;
  }
  st.busy = false;
  paint();
}

async function remove(paint) {
  const d = st.data, n = Object.values(d.files || {}).reduce((s, f) => s + f.lines, 0);
  const ok = confirm(`删掉她在小窝里攒的经历 (${n} 条)?\n\n会删: ${Object.keys(d.files).map((k) => KINDS[k] || k).join('; ')}.\n`
    + `不会删: ${NOT_HERE.join('、')}.\n\n删了找不回来 (想留一份就先点"导出"). 删完她会重新去看、重新学.`);
  if (!ok) return;
  st.busy = true;
  st.note = '';
  paint();
  try {
    const r = await fetch('/api/experience', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'delete', confirm: true }) });
    if (!r.ok) throw new Error(await r.text() || `服务端回了 ${r.status}`);
    const out = await r.json();
    st.note = out.deleted?.length ? `删掉了 ${out.deleted.length} 个文件` : '本来就没有';
  } catch (err) {
    st.note = `没删成: ${err.message}`;
  }
  st.busy = false;
  await load(paint);
}

// 回包里一项清了什么 -> 一句话 (服务端各项的格式不完全一样: 字符串、{files, legacy}、动作成长自己的结果)
function describe(v) {
  if (v == null) return '';
  if (typeof v === 'string') return v;
  if (Array.isArray(v)) return v.length ? v.join('、') : '没有';
  if (Array.isArray(v.files) || Array.isArray(v.legacy)) {
    const n = (v.files || []).length, old = (v.legacy || []).length;
    return `删了 ${n} 个文件${old ? `, 旧位置的 ${old} 个` : ''}`;
  }
  if (Array.isArray(v.cleared)) {  // 动作成长 (wm/motion_growth.py 的 reset): {cleared, kept, history}
    const done = v.cleared.length ? `清了 ${v.cleared.join('、')}` : '本来就是出厂参数, 没有要清的';
    return `${done}${v.history ? '; 训练历史也清了' : '; 训练历史留着'}`;
  }
  return v.text || v.summary || v.message || JSON.stringify(v).slice(0, 160);
}

async function reset(paint) {
  const clears = st.history ? [...RESET_CLEARS, HISTORY] : RESET_CLEARS;
  const keeps = st.history ? RESET_KEEPS : [HISTORY, ...RESET_KEEPS];
  const ok = confirm(`完整重置她在小窝里学到的?\n\n会清:\n- ${clears.join('\n- ')}\n\n不会清:\n- ${keeps.join('\n- ')}\n\n`
    + '清了找不回来 (想留一份经历就先点"导出"). 之后她在小窝里会重新去看、重新学.');
  if (!ok) return;
  st.busy = true;
  st.note = '';
  st.result = null;
  paint();
  try {
    const r = await fetch('/api/experience', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'reset', confirm: true, scope: st.history ? [...SCOPE, 'growth-history'] : SCOPE }) });
    if (r.status === 400) throw new Error('服务端还不认识完整重置 (重启服务端以后才有)');
    if (!r.ok) throw new Error(await r.text() || `服务端回了 ${r.status}`);
    st.result = await r.json();
    st.note = `重置好了 (第 ${st.result.generation} 代)`;
  } catch (err) {
    st.note = `没重置成: ${err.message}`;
  }
  st.busy = false;
  await load(paint);
}

/** el: 放这一块的元素; ctx: 面板的 { esc, call }. */
export function renderExperience(el, ctx) {
  const { esc } = ctx;
  const paint = () => renderExperience(el, ctx);
  const d = st.data;
  if (!d) {
    el.innerHTML = `<h2>小屋经历</h2><p class="empty">${esc(st.error || '读取中…')}</p>`;
    if (!st.error) load(paint);
    return;
  }
  const files = Object.entries(d.files || {});
  const total = files.reduce((s, [, f]) => s + f.lines, 0);
  el.innerHTML = `
    <div class="card-head"><h2>小屋经历 <span class="badge off">${total} 条</span></h2>
      <button type="button" class="btn" id="expExport" ${st.busy || !total ? 'disabled' : ''}>${st.busy ? '处理中…' : '导出数据…'}</button></div>
    <p class="hint">她在小屋生活时积累的本机数据。导出前可以选择保存位置。</p>
    ${files.length ? files.map(([k, f]) => `<div class="row top"><label>${esc(KINDS[k] || k)}</label><span class="tr-text">${f.lines} 条, ${esc(human(f.bytes))}
      <span class="hint">最后一条 ${esc((f.modified || '').replace('T', ' '))}</span></span></div>`).join('') : '<p class="empty">还没有攒下什么</p>'}
    <details class="setting-details"><summary>存储位置</summary><p class="hint"><code>${esc(d.dir)}</code>。卸载应用不会删除这些数据。</p></details>
    <details class="setting-details danger-zone"><summary>删除与重置</summary>
    <p class="hint">删除经历只清除上面的数据，不包括：${esc(NOT_HERE.join('、'))}。前三项需要使用完整重置。</p>
    <div class="actions">
      <button type="button" class="btn ghost danger" id="expDelete" ${st.busy || !total ? 'disabled' : ''}>删掉这些经历</button>
      <button type="button" class="btn ghost danger" id="expReset" ${st.busy ? 'disabled' : ''}>完整重置小窝学习…</button>
    </div>
    <label class="checks"><input type="checkbox" id="expHistory" ${st.history ? 'checked' : ''} ${st.busy ? 'disabled' : ''}>
      完整重置时连${esc(HISTORY)}一起清 <span class="hint">(默认留着: 退回出厂参数不等于删掉她练过的记录)</span></label></details>
    ${st.note ? `<p class="hint">${esc(st.note)}</p>` : ''}
    ${st.result ? `<div class="row top"><label>清了</label><span class="tr-text">${Object.entries(st.result.cleared || {}).map(([k, v]) =>
      `${esc(SCOPE_NAMES[k] || k)}: ${esc(describe(v))}`).join('<br>') || '没有'}</span></div>
      <div class="row top"><label>留着</label><span class="tr-text">${esc((st.result.kept || RESET_KEEPS).join('、'))}</span></div>` : ''}`;
  el.querySelector('#expExport').addEventListener('click', () => exportZip(ctx, paint));
  el.querySelector('#expDelete').addEventListener('click', () => remove(paint));
  el.querySelector('#expReset').addEventListener('click', () => reset(paint));
  el.querySelector('#expHistory').addEventListener('change', (e) => { st.history = e.target.checked; });
}

/** 换页回来时重新读. */
export function experienceForget() { st.data = null; st.error = ''; st.note = ''; st.result = null; }
