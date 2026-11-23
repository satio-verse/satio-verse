// 操作面板的"诊断"页 (2026-10-27 用户: "copy-diagnostics + environment self-check button"; 照 dsh-bili-scroll 2.0 的
// "复制诊断信息 / 环境自检"): 出问题时点一下.
//   - 环境自检: GET /api/selfcheck, 一项一行 (✓ 通 / ! 要注意 / ✕ 不通 / – 没用到), 有问题的写怎么办. 打开这页就查一遍;
//   - 复制诊断信息: GET /api/diagnose 拿一份去过隐私的文字 (版本、系统、显卡、配置摘要、服务、组件、渠道、额度、最近的报错),
//     复制到剪贴板, 也摊开给你看 (复制不了时自己选中复制). 页面补一句自己的情况 (WebView2 版本).
// 数据都在服务端 (wm/diagnose.py).
const diag = { checks: null, at: 0, running: false, checkError: '', text: '', copying: false, copied: '', copyError: '', open: false, expanded: new Map() };
const MARK = { ok: '✓', warn: '!', fail: '✕', skip: '–' };
const WORD = { ok: '正常', warn: '需关注', fail: '异常', skip: '未启用' };

function pageInfo() {
  const m = navigator.userAgent.match(/Edg\/([\d.]+)/);
  return m ? `WebView2 (Edge ${m[1]})` : navigator.userAgent.slice(0, 120);
}

async function fetchJson(url, what) {
  const r = await fetch(url);
  if (r.status === 404) throw new Error(`服务端是加${what}之前起的: 重启服务端以后才有`);
  if (!r.ok) throw new Error(await r.text() || `HTTP ${r.status}`);
  return r.json();
}

async function runCheck(ctx) {
  diag.running = true;
  if (ctx.isTab()) ctx.render();
  try {
    const d = await fetchJson('/api/selfcheck', '自检');
    diag.checks = d.checks;
    diag.at = d.at;
    diag.checkError = '';
  } catch (err) {
    diag.checkError = `查不了 (${err.message}): 服务端开着才能查`;
  }
  diag.running = false;
  if (ctx.isTab()) ctx.render();
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch { /* 剪贴板不让用 (工作台里的面板、没有用户手势): 用老办法 */ }
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;opacity:0;left:-9999px';
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  ta.remove();
  return ok;
}

async function copyReport(ctx) {
  diag.copying = true;
  diag.copied = '';
  ctx.render();
  try {
    diag.text = (await fetchJson(`/api/diagnose?page=${encodeURIComponent(pageInfo())}`, '诊断')).text;
    diag.copied = await copyText(diag.text) ? '复制好了, 直接粘贴给别人' : '复制不了 (剪贴板不让用): 在下面的框里全选复制';
    diag.copyError = '';
  } catch (err) {
    diag.copyError = `拿不到诊断信息 (${err.message}): 服务端开着才有`;
  }
  diag.copying = false;
  if (ctx.isTab()) ctx.render();
}

/** ctx: { esc, $, $$, render, isTab() } */
export function renderDiagnose(main, ctx) {
  const { esc } = ctx;
  if (!diag.checks && !diag.running && !diag.checkError) runCheck(ctx);
  const c = [...(diag.checks || [])].sort((a, b) => ({ fail: 0, warn: 1, ok: 2, skip: 3 }[a.level] ?? 4) - ({ fail: 0, warn: 1, ok: 2, skip: 3 }[b.level] ?? 4));
  const n = (lv) => c.filter((x) => x.level === lv).length;
  const badge = !c.length ? '' : n('fail') ? `<span class="badge warn-badge">${n('fail')} 项不通</span>`
    : n('warn') ? `<span class="badge warn-badge">${n('warn')} 项要注意</span>` : '<span class="badge">都通</span>';
  main.innerHTML = `
    <h1>诊断</h1>
    <p class="lead">检查运行环境，定位问题，或复制诊断信息寻求帮助。</p>
    <div class="card">
      <div class="card-head"><h2>环境自检 ${badge}</h2><button type="button" class="btn" id="dCheck" ${diag.running ? 'disabled' : ''}>${diag.running ? '检查中…' : '重新检查'}</button></div>
      ${diag.at ? `<p class="hint">最近检查：${new Date(diag.at * 1000).toLocaleTimeString('zh-CN', { hour12: false })}</p>` : ''}
      ${diag.checkError ? `<div class="note err">${esc(diag.checkError)}</div>` : ''}
      <div class="selfcheck-list">${c.map((x) => `<details class="check setting-details ${esc(x.level)}" data-check="${esc(x.name)}" ${(diag.expanded.get(x.name) ?? ['fail', 'warn'].includes(x.level)) ? 'open' : ''}>
        <summary><span class="mark" aria-hidden="true">${MARK[x.level] || '?'}</span><b>${esc(x.name)}</b><span class="badge off">${WORD[x.level] || esc(x.level)}</span></summary>
        <div class="body"><p class="hint">${esc(x.detail)}</p>${x.fix ? `<div class="fix">建议：${esc(x.fix)}</div>` : ''}</div></details>`).join('')
        || `<p class="empty">${diag.running ? '正在检查运行环境…' : '尚未检查'}</p>`}</div>
    </div>
    <div class="card">
      <h2>诊断信息</h2>
      <p class="hint">包含设备、服务和最近的错误。自动去除密钥、token、cookie 和余额，并遮盖账号等长数字。</p>
      <div class="actions"><button type="button" class="btn primary" id="dCopy" ${diag.copying ? 'disabled' : ''}>${diag.copying ? '整理中…' : '复制诊断信息'}</button>
        ${diag.copied ? `<span class="hint">${esc(diag.copied)}</span>` : ''}</div>
      ${diag.copyError ? `<div class="hint err-text">${esc(diag.copyError)}</div>` : ''}
      ${diag.text ? `<details id="dText" ${diag.open ? 'open' : ''}><summary>看复制了什么</summary><pre class="diag">${esc(diag.text)}</pre></details>` : ''}
    </div>`;
  ctx.$('#dCheck').addEventListener('click', () => runCheck(ctx));
  ctx.$('#dCopy').addEventListener('click', () => copyReport(ctx));
  ctx.$('#dText')?.addEventListener('toggle', (e) => { diag.open = e.target.open; });
  main.querySelectorAll('[data-check]').forEach((detail) => detail.addEventListener('toggle', () => diag.expanded.set(detail.dataset.check, detail.open)));
}

/** 面板自检 (panel.js 的 selftest 调): 自检接口通、每项都有档; 诊断信息拿得到、不带假 key. 返回 [{name, ok}]. */
export async function diagnoseSelftest(show, $) {
  const out = [];
  diag.checks = null;
  diag.checkError = '';
  show('diagnose');
  for (let i = 0; i < 100 && !diag.checks && !diag.checkError; i++) await new Promise((r) => setTimeout(r, 100));
  const rows = document.querySelectorAll('#main .check');
  out.push({ name: '诊断页: 环境自检查完了, 每项一行', ok: !!diag.checks && rows.length === diag.checks.length && rows.length >= 8 });
  $('#dCopy').click();
  for (let i = 0; i < 100 && (diag.copying || !diag.text) && !diag.copyError; i++) await new Promise((r) => setTimeout(r, 100));
  out.push({ name: '诊断页: 复制诊断信息拿到了文字, 不带假 key',
    ok: diag.text.startsWith('WorldModel 诊断信息') && !diag.text.includes('sk-selftest-not-a-real-key') && !!diag.copied });
  return out;
}
