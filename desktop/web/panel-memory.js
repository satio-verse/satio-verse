// 设置里的"记忆"页 (2026-11-01 用户要"记忆看得见"; 研究里评论最常抱怨的就是"不知道它记了什么"):
// 她记得关于你的每一条 —— 哪来的 (桌宠上说的 / QQ·微信私聊里说的、确认过 / 每晚整理的 / 你自己写的)、哪一类、哪天说的、
// 上次什么时候想起、改过 / 忘掉没有、QQ / 微信和你私聊时看不看得到;
// 等你确认的候选、最近几轮想起了什么、她自己的理解、关系概况、各处怎么用记忆. 数据是 Obsidian 库里那几份文件 (GET /api/memory,
// wm/app.py api_memory). 能做的只有两件: 忘掉一条程序记下的 (在文件里划掉, 想恢复去掉划线) 和确认 / 不要候选.

const ORIGIN = { chat: '桌宠上你直接说的', private: 'QQ / 微信私聊里说的', digest: '每晚整理聊天时记下的', you: '你自己写的' };
const STATUS = { corrected: '改过', forgotten: '忘掉了', struck: '划掉了' };
const FILTERS = [['all', '记得的'],['pref', '喜好'], ['fact', '事实'], ['event', '事件'], ['remote', 'QQ / 微信看得到'],
  ['changed', '改过的'], ['forgotten', '忘掉的']];
const state = { filter: 'all', q: '', busy: false, error: '' };

const isPref = (f) => f.tags.includes('偏好') || /喜欢|讨厌|害怕/.test(f.text);
const match = (f) => ({
  all: f.status === 'active', pref: f.status === 'active' && isPref(f), fact: f.status === 'active' && !isPref(f) && f.kind === 'long',
  event: f.status === 'active' && f.kind === 'event', remote: f.remote, changed: f.status === 'corrected',
  forgotten: f.status === 'forgotten' || f.status === 'struck',
}[state.filter]) && (!state.q || f.text.includes(state.q) || (f.note || '').includes(state.q));

function daysAgo(day) {
  if (!day) return '';
  const d = Math.round((Date.now() - new Date(`${day}T12:00:00`).getTime()) / 86400000);
  return d <= 0 ? '今天' : d === 1 ? '昨天' : d < 31 ? `${d} 天前` : day;
}

export async function renderMemory(main, { esc }) {
  const token = Symbol('memory');
  main._memoryRender = token;
  const alive = () => main.isConnected && main._memoryRender === token;
  let data;
  const load = async () => {
    const r = await fetch('/api/memory', { cache: 'no-store' });
    if (r.status === 404) throw new Error('服务端是加"记忆"页之前起的: 重启服务端以后才看得到');
    if (!r.ok) throw new Error(`服务端返回 ${r.status}`);
    return r.json();
  };
  const post = async (body) => {
    const r = await fetch('/api/memory', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const out = await r.json().catch(() => ({}));
    if (!r.ok || out.error) throw new Error(out.error || `服务端返回 ${r.status}`);
    return out;
  };

  const draw = () => {
    if (!alive()) return;
    if (!data.enabled) {
      main.innerHTML = `<h1>记忆</h1><p class="empty">${esc(data.reason || '没开长期记忆库')}</p>`;
      return;
    }
    const active = data.facts.filter((f) => f.status === 'active');
    const changed = data.facts.filter((f) => f.status !== 'active');
    const rows = data.facts.filter(match);
    main.innerHTML = `
      <h1>记忆</h1>
      <p class="lead">她记得关于你的事、从哪来的、谁能看到。数据就是 Obsidian 里 <code>${esc(data.dir)}</code> 下那几份文件；
        在这里忘掉的会在文件里划掉，想恢复把划线去掉就行。</p>
      <div class="mem-stats">
        <span><b>${active.length}</b> 条记得的</span><span><b>${data.candidates.length}</b> 条等你确认</span>
        <span><b>${changed.length}</b> 条改过 / 忘掉</span><span><b>${data.understanding.length}</b> 句她自己的理解</span>
      </div>
      ${state.error ? `<p class="note warn">${esc(state.error)}</p>` : ''}
      <div class="card"><h2>谁能看到、记不记</h2>
        <div class="mem-vis">${data.visibility.map((v) => `<div class="mem-vis-row"><b>${esc(v.where)}</b>
          <span class="mem-how">${esc(v.how)}</span><span class="hint">${esc(v.sees)}${v.writes ? `<br>记：${esc(v.writes)}` : ''}</span></div>`).join('')}</div></div>
      ${data.candidates.length ? `<div class="card"><h2>等你确认的</h2>
        <p class="hint">每天晚上整理聊天记录时找出来的，和你在 QQ / 微信私聊里说的"我叫…""我喜欢…"。你点"记住"才会进记忆，点"不要"以后同样的事不再提。</p>
        ${data.candidates.map((c) => `<div class="mem-row"><div class="mem-main"><span class="mem-text">${esc(c.text)}</span>
          <span class="mem-meta">${c.source ? `来源 ${esc(c.source)}` : ''}${c.days > 1 ? ` · 在 ${c.days} 天里提到过` : ''}</span></div>
          <span class="mem-actions"><button type="button" class="btn primary" data-review="${esc(c.id)}" data-ok="1" ${state.busy ? 'disabled' : ''}>记住</button>
          <button type="button" class="btn" data-review="${esc(c.id)}" data-ok="0" ${state.busy ? 'disabled' : ''}>不要</button></span></div>`).join('')}
      </div>` : ''}
      <div class="card"><h2>她记得关于你的</h2>
        <div class="mem-tools"><div class="mem-filters">${FILTERS.map(([id, label]) => `<button type="button" class="chip ${state.filter === id ? 'on' : ''}" data-filter="${id}">${label}</button>`).join('')}</div>
          <input type="search" class="mem-search" placeholder="搜一搜" value="${esc(state.q)}" aria-label="搜记忆"></div>
        ${rows.length ? rows.map((f) => `<div class="mem-row ${f.status !== 'active' ? 'gone' : ''}"><div class="mem-main">
            <span class="mem-text">${esc(f.text)}</span>
            <span class="mem-meta"><span class="mem-tag">${esc(ORIGIN[f.origin] || f.origin)}</span>
              <span class="mem-tag">${f.kind === 'long' ? '长期' : '事件'}</span>
              ${f.tags.includes('常提') ? '<span class="mem-tag">常提</span>' : ''}
              ${f.remote ? '<span class="mem-tag remote" title="QQ / 微信和你私聊、工作台里她也知道这一条">QQ / 微信看得到</span>' : ''}
              ${f.status !== 'active' ? `<span class="mem-tag gone">${STATUS[f.status]}</span>` : ''}
              ${f.said ? `<span>${esc(daysAgo(f.said))}说的</span>` : ''}
              ${f.kind === 'event' ? `<span>${f.last ? `上次想起 ${esc(daysAgo(f.last))}` : '还没想起过'}</span>` : ''}
              ${f.days > 1 ? `<span>有 ${f.days} 天说起或想起过</span>` : ''}
              ${f.source ? `<span>来源 ${esc(f.source)}</span>` : ''}</span>
            ${f.note ? `<span class="mem-note">${esc(f.note)}</span>` : ''}</div>
          <span class="mem-actions">${f.status === 'active' && f.origin !== 'you'
            ? `<button type="button" class="btn danger" data-forget="${esc(f.text)}" ${state.busy ? 'disabled' : ''}>忘掉</button>`
            : f.status === 'active' ? '<span class="hint">在 Obsidian 里改</span>' : ''}</span></div>`).join('')
          : `<p class="empty">${state.filter === 'all' && !state.q ? '还没有记下关于你的事' : '没有这一类的'}</p>`}
      </div>
      <div class="card"><h2>最近想起了什么</h2>
        <p class="hint">桌宠每一轮按你说的话去找、带进去的几条；服务端这次启动以后的，重启就清。QQ / 微信私聊不按句找，每次带上面"谁能看到"第二行那些。</p>
        ${data.recalls.length ? data.recalls.map((r) => `<div class="mem-recall"><div class="mem-meta"><span>${esc(r.at.slice(5, 16))}</span>
            <span class="mem-said">你说：${esc(r.q)}</span>${r.first ? '<span class="mem-tag">这次见面第一句</span>' : ''}</div>
          ${r.hits.length ? `<ul>${r.hits.map((h) => `<li>${esc(h.text)} <span class="hint">· ${esc(h.file.split('/').pop().replace('.md', ''))} · 分数 ${h.score}${h.fresh != null ? ` · 鲜活度 ${h.fresh}` : ''}</span></li>`).join('')}</ul>`
            : `<p class="hint">没找到相关的${r.answered ? '（这一句由记忆直接回答）' : ''}</p>`}
          ${(r.faded || []).length ? `<p class="hint">太久没想起来，这次没带上：${r.faded.map((f) => esc(f.text || f)).join('；')}</p>` : ''}
          ${(r.promoted || []).length ? `<p class="hint">常提够了天数，升成长期：${r.promoted.map(esc).join('；')}</p>` : ''}</div>`).join('')
          : '<p class="empty">这次启动以后桌宠还没聊过天</p>'}
      </div>
      ${data.understanding.length ? `<div class="card"><h2>她自己的理解</h2>
        <p class="hint">她看视频、过日子慢慢想通、攒下来的，聊到相关的事会自然说起；在 Obsidian 的"日记/我的理解.md"里删一行就是收回。</p>
        ${data.understanding.slice().reverse().map((u) => `<div class="mem-row"><div class="mem-main"><span class="mem-text">${esc(u.text)}</span>
          <span class="mem-meta"><span>${esc(u.day)}</span>${u.source ? `<span>${esc(u.source)}</span>` : ''}</span></div></div>`).join('')}</div>` : ''}
      ${Object.keys(data.relation).length ? `<div class="card"><h2>你们的关系</h2><div class="mem-rel">${[
        ['user_name', '她叫你'], ['first_met', '第一次见面'], ['last_seen', '上次见面'], ['sessions', '见面次数'], ['turns', '聊了几轮'],
        ['familiarity', '熟悉程度'], ['mood', '上次你的心情'], ['cared', '摸头投喂']].filter(([k]) => data.relation[k]).map(([k, label]) =>
        `<span><span class="hint">${label}</span><b>${esc(data.relation[k])}</b></span>`).join('')}</div></div>` : ''}`;
    main.querySelectorAll('[data-filter]').forEach((b) => b.addEventListener('click', () => { state.filter = b.dataset.filter; draw(); }));
    const search = main.querySelector('.mem-search');
    search?.addEventListener('input', () => {
      state.q = search.value.trim();
      const at = search.selectionStart;
      draw();
      const again = main.querySelector('.mem-search');
      again?.focus();
      again?.setSelectionRange(at, at);
    });
    main.querySelectorAll('[data-forget]').forEach((b) => b.addEventListener('click', async () => {
      if (!confirm(`让她忘掉"${b.dataset.forget}"？\n会在 Obsidian 文件里划掉这一行，想恢复把划线去掉就行。`)) return;
      await act({ action: 'forget', text: b.dataset.forget });
    }));
    main.querySelectorAll('[data-review]').forEach((b) => b.addEventListener('click',
      () => act({ action: 'review', id: b.dataset.review, ok: b.dataset.ok === '1' })));
  };

  const act = async (body) => {
    state.busy = true;
    state.error = '';
    draw();
    try {
      await post(body);
      data = await load();
    } catch (err) {
      state.error = `没做成：${err.message}`;
    }
    state.busy = false;
    draw();
  };

  main.innerHTML = '<h1>记忆</h1><p class="empty">读取中…</p>';
  try {
    data = await load();
  } catch (err) {
    if (alive()) main.innerHTML = `<h1>记忆</h1><p class="empty">${esc(err.message)}</p>`;
    return;
  }
  draw();
}
