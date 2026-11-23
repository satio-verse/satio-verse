// 操作面板的"IM 机器人"页 (panel.js 引进来用; 2026-10-26, 学 dsh-im 的扫码接入, own-harness.md 的 H1):
//   左边一列平台 (微信能扫码接入; QQ 在"外部渠道"页; 飞书、钉钉、企业微信以后做), 右边是选中平台的卡片.
//   微信: 扫码把你微信里的 ClawBot 联系人绑到她身上 (服务端 wm/channels/weixin.py, 接口 /api/weixin; 二维码服务端画成 SVG).
// 页面开着时扫码中每 1.5 秒、平时每 5 秒问一次服务端. token 只在服务端 (DPAPI 加密存盘), 面板拿不到.
const im = { sel: 'weixin', data: null, error: '', timer: 0, busy: false, code: '', infoOpen: false };
const PLATFORMS = [
  { id: 'weixin', name: '微信', icon: '💬', note: '扫码接入' },
  { id: 'qq', name: 'QQ', icon: '🐧', note: '在"外部渠道"页' },
  { id: 'feishu', name: '飞书', icon: '🪶', note: '暂不支持', unavailable: true },
  { id: 'dingtalk', name: '钉钉', icon: '📌', note: '暂不支持', unavailable: true },
  { id: 'wecom', name: '企业微信', icon: '🏢', note: '暂不支持', unavailable: true },
];
const STATE = { online: ['ok', '运行正常'], connecting: ['wait', '连接中'], expired: ['err', 'token 过期了'],
  error: ['err', '出错了'], off: ['off', '没在收'] };
const LOGIN = { fetching: '正在拿二维码…', wait: '用微信扫一扫下面的二维码', scanned: '扫好了, 在手机上点"确认"',
  need_code: '微信要验证码: 填你手机上看到的数字', checking_code: '在核对验证码…', confirmed: '绑好了',
  expired: '二维码过期了', error: '出错了' };
const clock = (t) => new Date(t * 1000).toLocaleString('zh-CN', { hour12: false });

async function load(ctx, body) {
  try {
    const r = await fetch('/api/weixin', body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {});
    if (r.status === 404) throw Object.assign(new Error('old'), { old: true });
    const d = await r.json().catch(() => null);
    if (!r.ok && !d) throw new Error(`HTTP ${r.status}`);
    im.data = d;
    im.error = r.ok ? '' : (d?.error || `HTTP ${r.status}`);
  } catch (err) {
    im.error = err.old ? '服务端还没有微信接入 (是加这个功能之前起的): 重启服务端以后才有' : `连不上服务端 (${err.message})`;
  }
  if (ctx.isTab() && document.activeElement?.id !== 'wxCode') ctx.render();  // 正在填验证码时不重画 (不然输入框丢焦点)
  else if (ctx.isTab()) im.timer = setTimeout(() => load(ctx), 1500);
}

const scanning = (d) => ['fetching', 'wait', 'scanned', 'need_code', 'checking_code'].includes(d?.login?.state);

/** ctx: { esc, $, $$, render, isTab(), show(tab) } */
export function renderIm(main, ctx) {
  const { esc } = ctx;
  clearTimeout(im.timer);
  im.timer = setTimeout(() => (ctx.isTab() ? load(ctx) : null), scanning(im.data) ? 1500 : 5000);
  if (!im.data && !im.error) load(ctx);
  const y = main.scrollTop;
  main.innerHTML = `
    <h1>IM 机器人</h1>
    <p class="lead">连接你常用的聊天软件，在手机上继续和她交流。</p>
    ${im.error ? `<div class="note err">${esc(im.error)}</div>` : ''}
    <div class="split">
      <div class="list channel-platforms">${PLATFORMS.map((p) => `<button type="button" class="item ${p.id === im.sel ? 'sel' : ''}" data-im="${p.id}" ${p.id === im.sel ? 'aria-current="page"' : ''} ${p.unavailable ? 'disabled' : ''}>
        <span>${p.name}</span><span class="badge ${p.id === 'weixin' && im.data?.bound ? '' : 'off'}">${
          p.id === 'weixin' && im.data?.bound ? '已接入' : p.note}</span></button>`).join('')}</div>
      <div>${im.sel === 'weixin' ? weixinCard(im.data, esc) : im.sel === 'qq' ? qqCard() : laterCard(PLATFORMS.find((p) => p.id === im.sel))}</div>
    </div>`;
  main.scrollTop = y;
  ctx.$$('[data-im]', main).forEach((b) => b.addEventListener('click', () => { im.sel = b.dataset.im; ctx.render(); }));
  const act = async (body) => { im.busy = true; ctx.render(); await load(ctx, body); im.busy = false; if (ctx.isTab()) ctx.render(); };
  ctx.$('#wxLogin')?.addEventListener('click', () => act({ action: 'login' }));
  ctx.$('#wxCancel')?.addEventListener('click', () => act({ action: 'cancel' }));
  ctx.$('#wxLogout')?.addEventListener('click', () => {
    if (confirm('解绑以后她就收不到你的微信了 (微信里的 ClawBot 联系人你自己删). 解绑吗?')) act({ action: 'logout' });
  });
  const code = ctx.$('#wxCode');
  code?.addEventListener('input', () => { im.code = code.value; });
  ctx.$('#wxCodeOk')?.addEventListener('click', () => act({ action: 'code', code: im.code }));
  ctx.$('#wxGoQQ')?.addEventListener('click', () => ctx.show('qq'));
  ctx.$('#wxAbout')?.addEventListener('toggle', (e) => { im.infoOpen = e.target.open; });
}

function weixinCard(d, esc) {
  const intro = `<p class="hint">通过 ClawBot 接入，仅与你一对一交流。当前支持文字消息。</p>
    <details class="setting-details" id="wxAbout" ${im.infoOpen ? 'open' : ''}><summary>连接方式与消息限制</summary><p class="hint">用的是腾讯给 AI 助手开的官方通道 (ClawBot): 扫码以后你微信里多一个联系人, 跟它说话就是跟她说话.
    平台的限制: 只能你一个人和她一对一 (进不了群), 她<b>不能主动</b>找你, 只能接着你的话回. 她要跟你说的事 (学好了、操作电脑的结果、申请)
    在你半小时内在微信上找过她时发微信, 否则还是发 QQ. 这一版只收发文字 (图片、语音先写成 ［图片］［语音］).</p></details>`;
  if (!d) return `<div class="card"><h2>微信</h2>${intro}<p class="empty">读取中…</p></div>`;
  const lg = d.login || {};
  const [cls, label] = STATE[d.state] || ['off', d.state];
  const bound = d.bound ? `
    <div class="row"><label>状态</label><div><span class="badge ${cls === 'ok' ? '' : 'off'} ${cls === 'err' ? 'warn-badge' : ''}">${
      cls === 'ok' ? '● ' : cls === 'err' ? '⚠ ' : ''}${label}</span>${d.error ? ` <span class="hint err-text">${esc(d.error)}</span>` : ''}</div></div>
    <div class="row"><label>绑的微信</label><div><code>${esc(d.user)}</code> · ${esc(d.bound_at || '')} 绑的</div></div>
    <div class="row"><label>今天</label><div>收到 ${d.received} 条、回了 ${d.sent} 条${d.ignored ? `, 不理的 ${d.ignored} 条 (不是你发的或回显)` : ''}${
      d.last_in ? ` · 最近收到消息 ${clock(d.last_in)}` : ''}</div></div>` : !scanning(d) ? '<ol class="setup-steps"><li>点击“扫码连接”获取二维码。</li><li>用你的微信扫描，并在手机上确认。</li><li>向 ClawBot 发送消息开始聊天。</li></ol>' : '';
  const qr = scanning(d) || ['expired', 'error'].includes(lg.state) ? `
    <div class="qr-box">
      ${lg.svg && ['wait', 'scanned', 'need_code', 'checking_code'].includes(lg.state) ? `<div class="qr ${lg.state !== 'wait' ? 'dim' : ''}">${lg.svg}</div>` : ''}
      <div class="qr-side">
        <div class="qr-state">${esc(LOGIN[lg.state] || lg.state)}</div>
        ${lg.n ? `<div class="hint">第 ${lg.n} 张二维码, 5 分钟内有效, 过期了自动换 (最多 3 张)</div>` : ''}
        ${lg.error ? `<div class="hint err-text">${esc(lg.error)}</div>` : ''}
        ${lg.state === 'need_code' ? `<div class="actions"><input type="text" id="wxCode" class="mono" maxlength="12" placeholder="验证码" value="${esc(im.code)}" style="width:9em">
          <button type="button" class="btn primary" id="wxCodeOk">提交</button></div>` : ''}
        <div class="actions">${scanning(d) ? '<button type="button" class="btn" id="wxCancel">不扫了</button>'
          : '<button type="button" class="btn primary" id="wxLogin">再扫一次</button>'}</div>
      </div>
    </div>` : lg.state === 'confirmed' ? '<div class="note">绑好了 ✓ 在微信里给 ClawBot 发一句试试.</div>' : '';
  const actions = scanning(d) ? '' : `<div class="actions">
    <button type="button" class="btn ${d.bound ? '' : 'primary'}" id="wxLogin" ${im.busy ? 'disabled' : ''}>${im.busy ? '连接中…' : d.bound ? '重新扫码' : '扫码连接'}</button>
    ${d.bound ? `<button type="button" class="btn danger" id="wxLogout" ${im.busy ? 'disabled' : ''}>解绑</button>` : ''}</div>`;
  return `<div class="card"><h2>微信 <span class="badge ${d.bound ? '' : 'off'}">${d.bound ? '已连接' : scanning(d) ? '等待扫码' : '未连接'}</span></h2>${bound}${qr}${scanning(d) || ['expired', 'error'].includes(lg.state) ? '' : actions}${intro}</div>`;
}

function qqCard() {
  return `<div class="card"><h2>QQ</h2>
    <p>QQ 是她自己的小号 (经你电脑上的 SnowLuma 登录), 能进群当群友、发说说, 设置在"外部渠道"页.</p>
    <p class="hint">腾讯也有官方的 QQ 机器人扫码接入 (带"机器人"标, 群里只能被 @ 才说话), 当不了群友, 这里先不做.</p>
    <div class="actions"><button type="button" class="btn primary" id="wxGoQQ">配置 QQ 渠道</button></div></div>`;
}

function laterCard(p) {
  return `<div class="card"><h2>${p.name}</h2><p class="empty">当前版本暂不支持这个平台。</p></div>`;
}

/** 面板自检 (panel.js 的 selftest 调): 接口通、这一页画得出来、扫码能拿到二维码 (paneltest 里是假的 iLink). */
export async function imSelftest(show, $) {
  const out = [];
  let d = null;
  try { d = await (await fetch('/api/weixin')).json(); } catch { /* 下面记失败 */ }
  out.push({ name: '微信接口 /api/weixin 通', ok: !!d && typeof d.bound === 'boolean' });
  im.data = null;
  show('im');
  for (let i = 0; i < 40 && !im.data; i++) await new Promise((r) => setTimeout(r, 100));
  out.push({ name: 'IM 机器人页画出来了 (微信卡片、扫码按钮)', ok: !!$('#main .list') && !!$('#wxLogin') });
  await fetch('/api/weixin', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'login' }) });
  let svg = false;
  for (let i = 0; i < 60 && !svg; i++) {
    await new Promise((r) => setTimeout(r, 200));
    svg = !!(await (await fetch('/api/weixin')).json()).login?.svg;
  }
  out.push({ name: '点扫码接入拿到了二维码 (假的 iLink)', ok: svg });
  return out;
}
