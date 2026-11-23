// Life choices are explicit transactions. The consumer read is shown separately.
export async function renderLife(main, { esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])) } = {}) {
  const token = Symbol('life'); main._lifeRender = token;
  let state, ownedNode, previewSequence = 0;
  const current = () => main.isConnected && main._lifeRender === token && (!ownedNode || main.contains(ownedNode));
  const api = async (body) => {
    const response = await fetch('/api/agent/life', body ? {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {});
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || `服务端返回 ${response.status}`);
    return data;
  };
  const display = (value) => value === true ? '开启' : value === false ? '关闭' : value == null || value === '' ? '未设置' : String(value);
  const paint = () => {
    if (!current()) return;
    previewSequence++;
    main.innerHTML = `<style>.life-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(170px,260px);gap:20px;padding:18px 0;border-bottom:1px solid var(--line,#e5e5e8)}.life-row:last-child{border:0}.life-row label{min-width:0}.life-row label b,.life-row label .hint{display:block}.life-row label .hint{margin-top:5px}.life-row input:not([type=checkbox]){width:100%;box-sizing:border-box}.life-reason{position:relative}.life-group>summary{font-weight:600}.life-group .life-row{margin:0 5px}@media(max-width:620px){.life-row{grid-template-columns:1fr;gap:10px;padding:14px 0}}</style><h1>生活与成长</h1><p class="lead">她可以理解、尝试和调整自己的生活节奏。每次选择保留原因，之后可以检查和恢复。</p>
      <div class="card"><h2>当前状态 <span class="badge">${state.paused ? '已暂停' : state.connected ? '配置已连接' : '等待连接'}</span></h2><p class="hint">${esc(state.note)}</p><div class="actions"><button type="button" class="btn" data-life-refresh>刷新采用证据</button><span class="hint">配置版本 ${state.revision}</span></div></div>
      <form id="lifeForm"><div class="card life-reason"><h2>这次为什么调整</h2><textarea id="lifeReason" maxlength="2000" rows="2" required placeholder="例如：最近想减少打扰，尝试更长的联系间隔。"></textarea><p class="hint">这里只记录真实选择。文字设想、实际完成的事情和后续观察分别保存。</p><div class="actions"><button class="btn primary" type="submit" ${state.connected ? '' : 'disabled'}>预览修改</button><span id="lifeStatus" class="hint" role="status"></span></div></div><div class="card"><h2>选择生活节奏</h2>${state.catalog.map((spec, index) => {
        const value = state.values[spec.key], evidence = state.evidence[spec.key];
        const adopted = evidence && evidence.revision === state.revision && JSON.stringify(evidence.value) === JSON.stringify(value);
        const control = spec.type === 'boolean' ? `<input type="checkbox" data-life-key="${esc(spec.key)}" aria-label="${esc(spec.label)}" ${value ? 'checked' : ''}>` : `<input type="${spec.type === 'number' ? 'number' : 'text'}" data-life-key="${esc(spec.key)}" aria-label="${esc(spec.label)}" value="${esc(value ?? '')}" ${spec.type === 'number' ? `min="${spec.min}" max="${spec.max}" step="any"` : 'placeholder="不限，或 23:00-08:00"'}>`;
        return `${index === 0 ? '<details class="setting-details life-group" open><summary>联系与独处</summary>' : index === 6 ? '</details><details class="setting-details life-group"><summary>空间动态</summary>' : index === 10 ? '</details><details class="setting-details life-group"><summary>群消息观察</summary>' : ''}<div class="life-row"><label><b>${esc(spec.label)}${spec.unit ? `（${esc(spec.unit)}）` : ''}</b><span class="hint">${esc(spec.effect)}</span><span class="hint">${adopted ? `调度器已读取 · ${esc(new Date(evidence.time * 1000).toLocaleTimeString())}` : '等待调度器读取'}</span></label><div>${control}<div class="hint">${esc(spec.cost)}</div></div></div>`;
      }).join('')}</details></div></form>
      <div id="lifePreview"></div><details class="setting-details" data-life-history><summary>调整历史与观察（${state.history.length}）</summary><div style="max-height:420px;overflow:auto">${state.history.length ? state.history.map((entry) => `<article class="card"><h3>${esc(entry.reason)}</h3><p class="hint">${entry.actor === 'assistant' ? '绫音' : '用户'} · ${esc(new Date(entry.time * 1000).toLocaleString())}</p>${Object.keys(entry.after).map((key) => `<p class="hint">${esc(state.catalog.find((x) => x.key === key)?.label || key)}：${esc(display(entry.before[key]))} → ${esc(display(entry.after[key]))}</p>`).join('')}${entry.observations.map((o) => `<p class="hint">观察：${esc(o.text)}</p>`).join('')}<div class="actions"><button class="btn" type="button" data-life-restore="${entry.id}">预览恢复</button><button class="btn" type="button" data-life-observe="${entry.id}">记录观察</button></div><div data-life-observation="${entry.id}"></div></article>`).join('') : '<p class="empty">还没有调整记录。</p>'}</div></details>`;
    ownedNode = main.firstElementChild;
    main.querySelector('[data-life-refresh]').onclick = load;
    main.querySelector('#lifeForm').onsubmit = async (event) => {
      event.preventDefault();
      const changes = {};
      main.querySelectorAll('[data-life-key]').forEach((input) => {
        const value = input.type === 'checkbox' ? input.checked : input.type === 'number' ? Number(input.value) : input.value.trim();
        if (JSON.stringify(value) !== JSON.stringify(state.values[input.dataset.lifeKey] ?? '')) changes[input.dataset.lifeKey] = value;
      });
      if (!Object.keys(changes).length) { main.querySelector('#lifeStatus').textContent = '还没有修改设置'; return; }
      const requestBody = {action:'apply',changes,reason:main.querySelector('#lifeReason').value.trim(),expected_revision:state.revision};
      await preparePreview({action:'preview',changes}, requestBody);
    };
    main.querySelectorAll('[data-life-restore]').forEach((button) => button.onclick = async () => {
      const requestBody = {action:'rollback',change_id:button.dataset.lifeRestore,expected_revision:state.revision,reason:'用户恢复之前的生活节奏',preview:false};
      await preparePreview({...requestBody,preview:true}, requestBody);
    });
    main.querySelectorAll('[data-life-observe]').forEach((button) => button.onclick = () => {
      const id = button.dataset.lifeObserve, box = main.querySelector(`[data-life-observation="${id}"]`);
      box.innerHTML = '<textarea maxlength="2000" rows="2" aria-label="后续观察" placeholder="记录实际感受或观察到的变化"></textarea><button class="btn" type="button">保存观察</button>';
      box.querySelector('textarea').focus();
      box.querySelector('button').onclick = async () => {
        try { state = await api({action:'observe',change_id:id,text:box.querySelector('textarea').value}); paint(); } catch(error) { showError(error); }
      };
    });
  };
  const showError = (error) => { if (current()) { const el = main.querySelector('#lifeStatus'); if(el) el.textContent = error.message; } };
  const preparePreview = async (query, requestBody) => {
    const sequence = ++previewSequence;
    try {
      const preview = await api(query);
      if (sequence === previewSequence) showPreview(preview, {...requestBody,expected_revision:preview.revision});
    } catch (error) { if(sequence === previewSequence) showError(error); }
  };
  const showPreview = (preview, requestBody) => {
    if (!current()) return;
    const box = main.querySelector('#lifePreview');
    box.innerHTML = `<div class="card"><h2>${requestBody.action === 'rollback' ? '恢复预览' : '修改预览'}</h2>${preview.changes.map((change) => `<p><b>${esc(change.label)}</b>：${esc(display(change.before))} → ${esc(display(change.after))}</p>`).join('')}<div class="actions"><button class="btn primary" type="button" data-life-apply>应用这次选择</button><button class="btn" type="button" data-life-cancel>取消</button></div><p class="hint" role="status"></p></div>`;
    box.scrollIntoView({block:'nearest'});
    box.querySelector('[data-life-cancel]').onclick = () => { previewSequence++; box.replaceChildren(); };
    box.querySelector('[data-life-apply]').onclick = async (event) => {
      event.currentTarget.disabled = true;
      try { state = await api(requestBody); paint(); } catch (error) { if(current() && box.querySelector('[role="status"]')) { box.querySelector('[role="status"]').textContent = error.message; box.querySelector('[data-life-apply]').disabled = false; } }
    };
  };
  async function load() {
    try { state = await api(); paint(); } catch (error) { if(current()) { main.innerHTML = `<style>.life-row{display:grid;grid-template-columns:minmax(0,1fr) minmax(170px,260px);gap:20px;padding:18px 0;border-bottom:1px solid var(--line,#e5e5e8)}.life-row:last-child{border:0}.life-row label{min-width:0}.life-row label b,.life-row label .hint{display:block}.life-row label .hint{margin-top:5px}.life-row input:not([type=checkbox]){width:100%;box-sizing:border-box}.life-reason{position:relative}.life-group>summary{font-weight:600}.life-group .life-row{margin:0 5px}@media(max-width:620px){.life-row{grid-template-columns:1fr;gap:10px;padding:14px 0}}</style><h1>生活与成长</h1><p class="empty">${esc(error.message)}</p><button class="btn" type="button">重新连接</button>`; ownedNode = main.firstElementChild; main.querySelector('button').onclick = load; } }
  }
  main.innerHTML = '<h1>生活与成长</h1><p class="empty">正在读取生活设置…</p>';
  ownedNode = main.firstElementChild;
  await load();
}
