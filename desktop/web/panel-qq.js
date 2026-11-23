// 操作面板的"外部渠道"页 (panel.js 引进来用, 标签 id 还是 qq):
//   - QQ: 连接、白名单、人设和模型、发送方式、暂停, 和服务端那边的状态.
//     设置存在 pet-settings.json 的 qq 段 (宿主 settings.h); 协议端 token 和 API key 一样, 页面只能填新的或清掉 (DPAPI 加密存).
//     宿主把它转成 channel_config 交给桌宠页面, 再经 /ws 发给服务端 (wm/channels). 状态由桌宠页面报给宿主, 这里用 op "qq-status" 取.
//   - 第三方框架 (AstrBot 这类, 经本机接口的 wm-guest): 怎么填、今天的计数、日志在哪 (状态里的 api 段, 宿主的 guest 计数).
//     人设、模型和每日额度跟 QQ 那一套共用. 访客 token 不经过这一页 (只能在资源管理器里看文件).
import { archiveOptions, setArchiveChoice, setGroupArchive, retentionSummary, retentionDifference, retentionCard } from './panel-qq-retention.js';

const SEND = { draft: '草稿: 你点了才发', auto: '直接发' };
const TRIGGER = { mention: '被 @、回复、叫名字才说', always: '每条都看 (她自己决定说不说)' };
const STATES = {
  off: ['关着', 'off'], connecting: ['连接中…', 'off'], online: ['在线', ''], offline: ['她的号离线了', 'off'],
  wrong_account: ['挂错了号', 'off'], auth_failed: ['token 不对', 'off'], token_error: ['读不到 token', 'off'],
  paused: ['暂停了', 'off'], error: ['出错了', 'off'],
};
const qqUI = { section: 'connection' };

const digits = (v) => String(v ?? '').replace(/\D/g, '');
const ids = (text) => String(text || '').split(/[\s,，、]+/).map(digits).filter(Boolean).map(Number);

export function defaultQQ() {
  return { access_scope: 'follow', blocked_groups: [], enabled: false, snowluma_dir: '', snowluma_autostart: true, self_id: 0, owner_id: 0, names: ['绫音'], persona: '', convs: [], block: [],
    group_send: 'draft', private_send: 'draft', owner_send: 'auto', daily_tokens: 200000, paused: false };
}

/** ctx: { S, esc, $, $$, changed, save, setKey, call, render } */
export function renderQQ(main, ctx) {
  const { S, esc, $, $$ } = ctx;
  S.qq ??= defaultQQ();
  const q = S.qq;
  q.proactive ??= {};
  q.qzone ??= {};
  const pro = q.proactive, qz = q.qzone;
  const personaOpts = [{ id: '', name: '默认人设 (绫音, config/personas/绫音.md)' }, ...S.personas.filter((p) => p.id !== 'default')]
    .map((p) => `<option value="${esc(p.id)}" ${p.id === (q.persona || '') ? 'selected' : ''}>${esc(p.name || '(没有名字)')}${p.provider && p.provider !== 'local' ? ` · ${esc(S.providers[p.provider]?.label || p.provider)}` : ''}</option>`).join('');
  const sendOpts = (v, extra = {}) => Object.entries({ ...extra, ...SEND }).map(([k, label]) => `<option value="${k}" ${k === (v || '') ? 'selected' : ''}>${esc(label)}</option>`).join('');
  main.innerHTML = `
    <h1>外部渠道</h1>
    <p class="lead">连接 QQ 或第三方聊天框架，设置她如何与其他人交流。</p>
    <div class="card" id="qqStatus"><span class="hint">读状态…</span></div>
    <div class="subtabs" role="tablist" aria-label="外部渠道分类">${[['connection', '连接与模型'], ['interaction', '互动方式'], ['access', '访问权限'], ['retention', '聊天记录'], ['bridge', '第三方框架']].map(([id, label]) => `<button type="button" role="tab" data-qq-tab="${id}" aria-selected="${qqUI.section === id}" class="${qqUI.section === id ? 'on' : ''}">${label}</button>`).join('')}</div>
    <section class="settings-section" data-qq-section="connection">
    <div class="card">
      <h2>人设与模型</h2><p class="hint">QQ 和第三方框架共用以下设置。</p>
      <div class="row"><label for="qPersona">用哪个预设</label><div><select id="qPersona">${personaOpts}</select>
        <div class="hint">用这个预设的人设文字和模型. 它勾的 MCP 服务器和 skill 在这里不生效 (外部渠道没有工具). 选云端模型时, 别人的话会发给那家.</div></div></div>
      ${q.persona ? '' : `<div class="row"><label for="qProvider">用哪个模型</label><div><select id="qProvider">${['local', ...Object.keys(S.providers || {})].map((id) =>
        `<option value="${esc(id)}" ${id === (q.provider || 'local') ? 'selected' : ''}>${id === 'local' ? '内置小模型 (0.8B, 长人设容易照抄)' : esc(S.providers[id]?.label || id)}${!['local', 'selfhost', 'custom'].includes(id) && !S.providers[id]?.has_key ? ' (还没填 key)' : ''}</option>`).join('')}</select>
        <input type="text" class="mono" id="qModel" value="${esc(q.model || '')}" placeholder="模型名, 空着用这家的默认" size="22" ${(q.provider || 'local') === 'local' ? 'hidden' : ''}>
        <div class="hint">内置小模型适合简单回复。云端模型先在“模型”页配置账号；本机模型在“模型”页填地址、点“检测”。</div></div></div>`}
      <details class="setting-details"><summary>云端用量预算</summary><div class="row"><label for="qTokens">每天上限</label><div><input type="text" class="mono" id="qTokens" value="${q.daily_tokens ?? 200000}" inputmode="numeric">
        <div class="hint">云端模型每天最多用这么多 token, QQ 和第三方框架加起来算, 用完就不说话 (0 是不限). 内置小模型和本机模型都不算。</div></div></div></details>
    </div>
    <div class="card">
      <h2>QQ 连接</h2>
      <p class="hint">先在 SnowLuma 登录她的 QQ 小号，再填写目录与账号并启用。非官方协议可能导致封号，请勿使用主号。</p>
      <div class="row"><label for="qEnabled">启用 QQ</label><div class="checks"><label><input type="checkbox" id="qEnabled" ${q.enabled ? 'checked' : ''}>让她在 QQ 上收发消息</label>
        <label><input type="checkbox" id="qPaused" ${q.paused ? 'checked' : ''}>暂停 (不收不发, 托盘里也能切)</label></div></div>
      <div class="row"><label for="qDir">SnowLuma 目录</label><div><input type="text" class="mono" id="qDir" value="${esc(q.snowluma_dir)}" placeholder="例如 D:/SnowLuma">
        <div class="hint">填了就每次连接时从它的 config/onebot_&lt;她的号&gt;.json 读 ws-default 的 token, 下面的 token 可以不填. 记得在 SnowLuma 里关掉 #sl 状态命令.</div></div></div>
      <div class="row"><label for="qSnowAuto">随桌宠启动</label><div class="checks"><label><input type="checkbox" id="qSnowAuto" ${q.snowluma_autostart !== false ? 'checked' : ''}>自动启动已安装的 SnowLuma</label><div class="hint">已运行时直接复用，关闭桌宠后保持运行。首次设置、QQ 登录与挂接仍由 SnowLuma 完成。</div></div></div>
      <details class="setting-details"><summary>手动配置连接凭证</summary>
      <div class="row"><label for="qToken">token</label><div><input type="password" class="mono" id="qToken" autocomplete="off"
        placeholder="${q.has_token ? '已保存 (不显示); 填新的会替换' : '不填就用 SnowLuma 目录里的'}">
        ${q.has_token ? '<button type="button" class="btn danger" id="qClearToken">清除 token</button>' : ''}</div></div></details>
      <div class="row"><label for="qSelf">她的 QQ 号</label><div><input type="text" class="mono" id="qSelf" value="${q.self_id || ''}" inputmode="numeric">
        <div class="hint">连上后会核对协议端挂的是不是这个号, 不是就不收不发.</div></div></div>
      <div class="row"><label for="qOwner">你的 QQ 号</label><div><input type="text" class="mono" id="qOwner" value="${q.owner_id || ''}" inputmode="numeric">
        <div class="hint">你私聊她时总会回, 不用加进白名单.</div></div></div>
      <div class="row"><label for="qNames">她的名字</label><div><input type="text" id="qNames" value="${esc((q.names || []).join(', '))}">
        <div class="hint">群里有人叫这些名字时她会接话 (逗号分开).</div></div></div>
    </div>
    </section><section class="settings-section" data-qq-section="interaction">
    <div class="card">
      <h2>交流方式 <span class="badge" id="qPermissionBadge">读取权限…</span></h2>
      <p class="hint" id="qPermissionHint">完全权限下直接回复，不再按每日次数或未回复条数拦截。暂停、指定交流范围和黑名单始终有效。</p>
      <div class="row"><label for="qCross">跨会话记忆</label><div class="checks"><label><input type="checkbox" id="qCross" ${q.cross_session ? 'checked' : ''}>私聊时记得群里刚聊过什么（群里不会说出你私聊的话）</label></div></div>
      <div class="row"><label for="qProOn">主动联系</label><div class="checks"><label><input type="checkbox" id="qProOn" ${pro.enabled ? 'checked' : ''}>空闲时自主决定是否找你聊聊</label></div></div>
      <div class="row"><label for="qProLife">独处活动</label><div class="checks"><label><input type="checkbox" id="qProLife" ${pro.life !== false ? 'checked' : ''}>安排学习、浏览和自己的项目</label><div class="hint">实际完成的活动与文字设想会分别记录。</div></div></div>
      <div class="row"><label for="qProQuiet">免打扰时间</label><div><input type="text" class="mono" id="qProQuiet" value="${esc(pro.quiet ?? '')}" placeholder="不限，或 23:30-08:30"><div class="hint">这是你的安静时间；随时可以调整。</div></div></div>
      <details class="setting-details"><summary>生活节奏与可选次数设置</summary>
        <div class="row"><label for="qProIdle">联系间隔（小时）</label><input type="number" class="mono" id="qProIdle" min="0.001" step="0.1" value="${pro.idle_h ?? 3}"></div>
        <p class="hint">以下次数仅在普通权限下使用；完全权限下不限制次数。云端用量仍计入账本和你设置的预算。</p>
        <div class="row"><label for="qProDay">每天主动联系</label><input type="number" id="qProDay" value="${pro.max_per_day ?? 2}" min="0"></div>
        <div class="row"><label for="qProUn">连续未回复</label><input type="number" id="qProUn" value="${pro.max_unanswered ?? 1}" min="1"></div>
        <div class="row"><label for="qProShare">每天主动分享</label><input type="number" id="qProShare" value="${pro.share_per_day ?? 3}" min="0"></div>
      </details>
      <details class="setting-details"><summary>普通权限下的发送方式</summary>
        <div class="row"><label for="qGroupSend">群聊</label><select id="qGroupSend">${sendOpts(q.group_send)}</select></div>
        <div class="row"><label for="qPrivSend">好友私聊</label><select id="qPrivSend">${sendOpts(q.private_send)}</select></div>
        <div class="row"><label for="qOwnerSend">与你的私聊</label><select id="qOwnerSend">${sendOpts(q.owner_send)}</select></div>
        <p class="hint">完全权限下这些草稿设置不生效；恢复普通权限后继续按这里的选择发送。</p>
      </details>
      <p class="hint" id="qOwn"></p>
    </div>
    <div class="card">
      <h2>QQ 空间</h2>
      <div class="row"><label for="qQzOn">自主动态</label><div class="checks"><label><input type="checkbox" id="qQzOn" ${qz.enabled ? 'checked' : ''}>把真实经历写成动态</label></div></div>
      <div class="row"><label for="qQzRight">谁能看</label><select id="qQzRight">${[[4, '好友'], [1, '所有人'], [64, '只有她自己']].map(([v, l]) => `<option value="${v}" ${v === (qz.right ?? 4) ? 'selected' : ''}>${l}</option>`).join('')}</select></div>
      <details class="setting-details"><summary>动态安排与普通权限发送方式</summary>
        <div class="row"><label for="qQzWin">活动时间</label><input type="text" class="mono" id="qQzWin" value="${esc(qz.window ?? '19:00-23:00')}" placeholder="19:00-23:00"></div>
        <div class="row"><label for="qQzDay">每天安排（条）</label><div><input type="number" id="qQzDay" min="0" value="${qz.per_day ?? 1}"><div class="hint">这是定时机会，不是手动或任务发布的次数上限。</div></div></div>
        <div class="row"><label for="qQzSend">普通权限发送</label><select id="qQzSend">${sendOpts(qz.send ?? 'draft')}</select></div>
      </details>
    </div>
    </section><section class="settings-section" data-qq-section="access">
    <div class="card">
      <h2>交流范围</h2>
      <div class="row"><label for="qScope">与谁交流</label><select id="qScope">${[['follow','跟随权限（完全权限时全部）'],['all','全部群与好友'],['selected','仅下方选定的群与好友'],['core','仅标记为核心的群与好友']].map(([id,name]) => `<option value="${id}" ${id === (q.access_scope || 'follow') ? 'selected' : ''}>${name}</option>`).join('')}</select></div>
      <p class="hint">核心标记用于集中关注；选择全部时，列表是常用联系人。你的私聊始终保留，黑名单优先。「汇报给我」关掉只停这一行的重要消息汇报，她照常聊天。</p>
      <div class="table-wrap"><table class="qq-convs"><colgroup><col class="c-core"><col class="c-who"><col class="c-when"><col class="c-send"><col class="c-log"><col class="c-notify"><col class="c-del"></colgroup><thead><tr><th>核心</th><th>群 / 好友</th><th>回复时机</th><th>发送方式</th><th>原文归档</th><th title="把这里的重要消息整理汇报给你；关掉只停汇报，她照常聊天">汇报给我</th><th></th></tr></thead><tbody>
        ${(q.convs || []).map((c, i) => `<tr data-i="${i}">
          <td><input type="checkbox" data-c="core" aria-label="核心联系人" ${c.core ? 'checked' : ''}></td>
          <td><div class="qq-who"><span class="qq-kind${c.type === 'group' ? '' : ' p'}">${c.type === 'group' ? '群' : '好友'}</span><input type="text" data-c="name" value="${esc(c.name)}" placeholder="${c.type === 'group' ? '群名' : '备注'}" aria-label="备注"><input type="text" class="mono" data-c="id" value="${c.id || ''}" inputmode="numeric" placeholder="${c.type === 'group' ? '群号' : 'QQ 号'}" aria-label="${c.type === 'group' ? '群号' : 'QQ 号'}"></div></td>
          <td>${c.type === 'group' ? `<select data-c="trigger">${Object.entries(TRIGGER).map(([k, l]) => `<option value="${k}" ${k === (c.trigger || 'mention') ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>` : '<span class="hint">有消息就说</span>'}</td>
          <td><select data-c="send">${sendOpts(c.send, { '': '按上面的' })}</select></td>
          <td>${c.type === 'group' ? `<select data-c="log_raw" aria-label="此群原文归档">${archiveOptions(c.log_raw, c.enabled === false ? '停用时不归档' : '按群聊默认')}</select>` : '<span class="hint">沿用私聊机制</span>'}</td>
          <td><input type="checkbox" data-c="notify" aria-label="把重要消息汇报给我" title="把这里的重要消息整理汇报给你；关掉只停汇报，她照常聊天" ${c.notify === false ? '' : 'checked'}></td>
          <td><button type="button" class="btn danger" data-c="remove">删</button></td></tr>`).join('')}
      </tbody></table></div>
      ${q.convs?.length ? '' : '<p class="empty">尚未添加群或好友。她仍可回复你的私聊。</p>'}
      <div class="actions"><button type="button" class="btn" id="qAddGroup">添加群</button><button type="button" class="btn" id="qAddFriend">添加好友</button></div>
      <details class="setting-details"><summary>黑名单（始终生效）</summary><div class="row"><label for="qBlock">屏蔽账号</label><div><input type="text" class="mono" id="qBlock" value="${esc((q.block || []).join(', '))}">
        <div class="hint">这些账号的消息不读取、不回复，逗号分开。</div></div></div><div class="row"><label for="qBlockGroups">屏蔽群聊</label><input type="text" class="mono" id="qBlockGroups" value="${esc((q.blocked_groups || []).join(', '))}" placeholder="群号，逗号分开"></div></details>
    </div>
    </section><section class="settings-section" data-qq-section="retention">
    ${retentionCard(q)}
    </section><section class="settings-section" data-qq-section="bridge">
    <h2 class="section">第三方框架</h2>
    <p class="hint">它们从别的平台 (微信 ClawBot、飞书、Telegram…) 转来别人的话. 只给它们 wm-guest 和访客 token, 别接 wm-pet / wm-text
      (那两个是你自己的对话, 会改你的记忆). 它们直接回, 没有草稿门, 所以白名单要在它们那边管好. 方案见 docs/roadmap/astrbot-wm-guest.md.</p>
    <div class="card" id="qqGuest"><span class="hint">读取连接状态…</span></div></section>
    <details class="setting-details"><summary>外部会话的隐私与权限</summary><p class="hint">每个群、私聊和第三方会话彼此独立，无法使用电脑工具，也不会读取或改写你的记忆。你在 QQ 私聊她时，她只知道你的名字和喜好。使用云端模型时，消息会发送到对应的模型提供方。</p></details>`;
  $$('[data-qq-section]', main).forEach((section) => { section.hidden = section.dataset.qqSection !== qqUI.section; });
  $$('[data-qq-tab]', main).forEach((button) => button.addEventListener('click', () => {
    qqUI.section = button.dataset.qqTab;
    ctx.render();
    main.scrollTop = 0;
    main.querySelector(`[data-qq-tab="${qqUI.section}"]`)?.focus();
  }));
  const bind = (sel, fn, ev = 'input') => $(sel, main).addEventListener(ev, (e) => { fn(e.target); ctx.changed(); });
  bind('#qEnabled', (t) => { q.enabled = t.checked; }, 'change');
  bind('#qPaused', (t) => { q.paused = t.checked; }, 'change');
  bind('#qDir', (t) => { q.snowluma_dir = t.value.trim(); });
  bind('#qSnowAuto', (t) => { q.snowluma_autostart = t.checked; }, 'change');
  bind('#qSelf', (t) => { q.self_id = Number(digits(t.value)) || 0; });
  bind('#qOwner', (t) => { q.owner_id = Number(digits(t.value)) || 0; });
  bind('#qNames', (t) => { q.names = t.value.split(/[,，、]/).map((s) => s.trim()).filter(Boolean); });
  bind('#qPersona', (t) => { q.persona = t.value; ctx.render(); }, 'change');
  if (!q.persona) {
    bind('#qProvider', (t) => { q.provider = t.value; ctx.render(); }, 'change');
    bind('#qModel', (t) => { q.model = t.value.trim(); });
  }
  bind('#qTokens', (t) => { q.daily_tokens = Number(digits(t.value)) || 0; });
  bind('#qGroupSend', (t) => { q.group_send = t.value; }, 'change');
  bind('#qPrivSend', (t) => { q.private_send = t.value; }, 'change');
  bind('#qOwnerSend', (t) => { q.owner_send = t.value; }, 'change');
  bind('#qGroupLogRaw', (t) => { setGroupArchive(q, t.value); }, 'change');
  bind('#qApiLogRaw', (t) => { setArchiveChoice(q, 'api_log_raw', t.value); }, 'change');
  bind('#qBlock', (t) => { q.block = ids(t.value); });
  bind('#qBlockGroups', (t) => { q.blocked_groups = ids(t.value); });
  bind('#qScope', (t) => { q.access_scope = t.value; }, 'change');
  const num = (v, fallback) => {  // 空着或填错就用默认值
    const s = String(v).trim(), n = Number(s);
    return s && Number.isFinite(n) && n >= 0 ? n : fallback;
  };
  bind('#qCross', (t) => { q.cross_session = t.checked; }, 'change');
  bind('#qProOn', (t) => { pro.enabled = t.checked; }, 'change');
  bind('#qProIdle', (t) => { pro.idle_h = num(t.value, 3); });
  bind('#qProDay', (t) => { pro.max_per_day = Math.round(num(t.value, 2)); });
  bind('#qProUn', (t) => { pro.max_unanswered = Math.max(1, Math.round(num(t.value, 1))); });
  bind('#qProQuiet', (t) => { pro.quiet = t.value.trim(); });
  bind('#qProLife', (t) => { pro.life = t.checked; }, 'change');
  bind('#qProShare', (t) => { pro.share_per_day = Math.round(num(t.value, 3)); });
  bind('#qQzOn', (t) => { qz.enabled = t.checked; }, 'change');
  bind('#qQzWin', (t) => { qz.window = t.value.trim(); });
  bind('#qQzDay', (t) => { qz.per_day = Math.round(num(t.value, 1)); });
  bind('#qQzRight', (t) => { qz.right = Number(t.value); }, 'change');
  bind('#qQzSend', (t) => { qz.send = t.value; }, 'change');
  $('#qToken', main).addEventListener('change', async (e) => {
    const v = e.target.value.trim();
    if (!v) return;
    ctx.setKey('qq', v);
    e.target.value = '';
    if (await ctx.save()) ctx.render();
  });
  $('#qClearToken', main)?.addEventListener('click', async () => {
    ctx.setKey('qq', '');
    if (await ctx.save()) ctx.render();
  });
  const add = (type) => {
    q.convs = [...(q.convs || []), { type, id: 0, name: '', ...(type === 'group' ? { trigger: 'mention' } : {}), send: '' }];
    ctx.changed();
    ctx.render();
    main.querySelector('.qq-convs tr:last-child input[data-c="id"]')?.focus();
  };
  $('#qAddGroup', main).addEventListener('click', () => add('group'));
  $('#qAddFriend', main).addEventListener('click', () => add('private'));
  $$('.qq-convs tr[data-i]', main).forEach((tr) => {
    const c = q.convs[Number(tr.dataset.i)];
    tr.addEventListener('input', (e) => {
      const f = e.target.dataset.c;
      if (f === 'id') c.id = Number(digits(e.target.value)) || 0;
      else if (f === 'name') c.name = e.target.value;
      else return;
      ctx.changed();
    });
    tr.addEventListener('change', (e) => {
      const f = e.target.dataset.c;
      if (f === 'core') { c.core = e.target.checked; ctx.changed(); }
      else if (f === 'notify') { if (e.target.checked) delete c.notify; else c.notify = false; ctx.changed(); }
      else if (f === 'trigger' || f === 'send') { c[f] = e.target.value; ctx.changed(); }
      else if (f === 'log_raw' && c.type === 'group') { setArchiveChoice(c, 'log_raw', e.target.value); ctx.changed(); }
    });
    $('[data-c="remove"]', tr).addEventListener('click', () => {
      q.convs = q.convs.filter((x) => x !== c);
      ctx.changed();
      ctx.render();
    });
  });
  showStatus(main, ctx);
  // 停在这一页时每 3 秒刷新一次状态; 换了页 (#qqStatus 不在了) 就停
  clearInterval(renderQQ.timer);
  renderQQ.timer = setInterval(() => {
    if (!document.getElementById('qqStatus')) clearInterval(renderQQ.timer);
    else showStatus(main, ctx);
  }, 3000);
}

/** 服务端那边的状态 (桌宠页面报给宿主的最后一份 channel_status). */
// 服务端认定"只聊天、不汇报"的会话 (2026-11-07, 状态里的 own.digest): 面板上关了"汇报给我"的, 和 config 里 digest.mute_groups 兜底的
// (面板上看不到, 那一行照样显示"开"; 以前看不出真假)
function mutedLine(ctx, s) {
  const d = s.own?.digest || {}, convs = ctx.S.qq?.convs || [], panel = d.muted_panel || [];
  const name = (key) => {
    const [type, id] = String(key).split(':'), c = convs.find((x) => x.type === type && String(x.id) === id), what = type === 'group' ? '群' : 'QQ';
    return c?.name ? `${c.name}（${what} ${id}）` : `${what} ${id}`;
  };
  const parts = [...panel.map(name), ...(d.muted_config || []).filter((k) => !panel.includes(k)).map((k) => `${name(k)}，配置里屏蔽的，面板开关管不到`)];
  return parts.length ? `<p class="hint">不汇报给你的：${ctx.esc(parts.join('；'))}</p>` : '';
}

export async function showStatus(main, ctx) {
  const box = ctx.$('#qqStatus', main);
  if (!box) return;
  const r = await ctx.call('qq-status');
  if (!box.isConnected) return;
  const s = r.status;
  const archiveStatus = ctx.$('#qRetentionStatus', main);
  if (archiveStatus) archiveStatus.textContent = retentionSummary(s);
  const archiveDifference = ctx.$('#qRetentionDifference', main);
  if (archiveDifference) archiveDifference.textContent = retentionDifference(ctx.S.qq, s);
  showGuest(main, ctx, r);
  if (!s) {
    box.innerHTML = '<span class="hint">还没有状态: 桌宠没连上服务, 或者服务端还没收到外部渠道的设置.</span>';
    return;
  }
  const [label, cls] = STATES[s.state] || [s.state, 'off'];
  box.innerHTML = `<h2>QQ 状态 <span class="badge ${cls}">${ctx.esc(label)}</span></h2>
    ${s.snowluma?.detail ? `<p class="hint">SnowLuma：${ctx.esc(s.snowluma.detail)}</p>` : ''}
    ${s.error ? `<p class="hint" style="color:var(--danger)">${ctx.esc(s.error)}</p>` : ''}
    <p class="hint">${s.nick ? `她的号: ${ctx.esc(s.nick)} (${s.self_id}) · ` : ''}${s.access_scope === 'all' ? '全部群与好友' : s.access_scope === 'core' ? '仅核心联系人' : `已选 ${s.convs ?? 0} 个会话`} · 今天用了 ${s.today_tokens ?? 0} token (和第三方框架一起算) · 待处理草稿 ${s.drafts ?? 0}
      · 模型 ${s.model?.engine === 'cloud' ? ctx.esc(`${s.model.provider}:${s.model.model}`) : '本地'}</p>
    <p class="hint">${ctx.esc(retentionSummary(s))}</p>
    ${mutedLine(ctx, s)}
    ${(s.warnings || []).map((w) => `<p class="hint">提醒: ${ctx.esc(w)}</p>`).join('')}`;
  const permission = ctx.$('#qPermissionBadge', main);
  if (permission) permission.textContent = s.unrestricted ? '完全权限 · 直接交流' : '普通权限 · 按配置交流';
  const own = ctx.$('#qOwn', main);
  if (own && s.own) {
    own.textContent = `今天主动找了你 ${s.own.proactive_today ?? 0} 次${s.own.unanswered ? `, ${s.own.unanswered} 条你还没回` : ''}`
      + (s.own.life_today ? ` · 自己做了 ${s.own.life_today} 件事, 分享了 ${s.own.share_today ?? 0} 次` : '')
      + (s.own.qzone_next ? ` · 下一条说说大约 ${s.own.qzone_next}` : '');
  }
}

/** 第三方框架那一块: 怎么填 (本机接口开没开、端口、访客 token 文件)、今天的计数 (服务端状态的 api)、带了工具的提醒 (宿主的 guest). */
function showGuest(main, ctx, r) {
  const box = ctx.$('#qqGuest', main);
  if (!box) return;
  const esc = ctx.esc, api = r.api || {}, g = r.guest, a = r.status?.api;
  const how = api.on
    ? `<div class="row"><label>地址</label><code class="mono">http://127.0.0.1:${api.port}/v1</code></div>
       <div class="row"><label>模型</label><code class="mono">wm-guest</code></div>
       <div class="row"><label>key</label><div>${api.token_exists ? `访客 token, 在 <code class="mono">${esc(api.token_path)}</code>
         <button type="button" class="btn" id="qGuestToken">在资源管理器里显示</button>` : '访客 token 还没生成 (本机接口第一次打开时生成)'}
         <div class="hint">别填你自己的 token (pet-api-token.txt): 那个能用 wm-pet 和 MCP.</div></div></div>`
    : '<p class="hint">本机接口没开: 托盘里勾上"本机接口"之后, 这里会显示地址和访客 token 在哪.</p>';
  const today = a ? `<p class="hint">今天: ${a.requests ?? 0} 句 · 说了 ${a.sent ?? 0} · 不说 ${a.silent ?? 0} · 拦下 ${a.blocked ?? 0}
      · 限频或额度 ${a.limited ?? 0} · 出错 ${a.errors ?? 0} · ${a.tokens ?? 0} token · ${a.sessions ?? 0} 个访客会话</p>
    ${a.last_error ? `<p class="hint" style="color:var(--danger)">最近的问题: ${esc(a.last_error)}</p>` : ''}
    <p class="hint">${a.log_dir ? `日志在 <code class="mono">${esc(a.log_dir)}</code> (按服务配置的保留期处理)` : esc(a.note || '日志没落盘')}</p>` : '';
  const tools = g?.with_tools ? `<p class="hint" style="color:var(--danger)">这次启动以来有 ${g.with_tools} 次请求带了工具 (已经忽略, 她不会调用).
    建议在 AstrBot 的人格里把工具都关掉.</p>` : '';
  box.innerHTML = `<h2>怎么接</h2>${how}${today ? `<h2>今天</h2>${today}` : ''}${tools}`;
  ctx.$('#qGuestToken', box)?.addEventListener('click', () => ctx.call('show-guest-token'));
}
