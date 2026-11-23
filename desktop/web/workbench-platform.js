// Explicit user contributions, Creator community and staged application updates.
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {queued:'待上传',sent:'已上传',withdrawn:'已撤回',deleted:'已删除',idle:'尚未检查',staged:'已验证并暂存',applied:'已应用',rolled_back:'已回滚'};
const time = value => value ? new Date(value * 1000).toLocaleString() : '';

export function initPlatform({container, getSession = () => null, notify = () => {}} = {}) {
  let page = 'community', state = null, preview = null, exported = null, generation = 0;
  const drafts = {};
  const root = typeof container === 'string' ? document.querySelector(container) : container;
  if (!root) throw new Error('Platform needs a container');
  const request = async body => {
    const response = await fetch('/api/agent/platform', body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {cache:'no-store'});
    const value = await response.json();
    if (!response.ok || value.error) throw new Error(value.error || `HTTP ${response.status}`);
    return value;
  };
  function saveDrafts() {
    root.querySelectorAll('[data-draft]').forEach(input => { if(input.type !== 'password') drafts[input.dataset.draft] = input.type === 'checkbox' ? input.checked : input.value; });
  }
  const field = (id, label, placeholder = '', type = 'text') => `<label class="platform-field">${esc(label)}<input data-draft="${id}" id="${id}" type="${type}" placeholder="${esc(placeholder)}" value="${esc(drafts[id] || '')}" ${type === 'password' ? 'autocomplete="off"' : ''}></label>`;
  const button = (action, label, data = '') => `<button type="button" class="platform-button" data-action="${action}" ${data}>${esc(label)}</button>`;
  const note = text => `<p class="platform-note">${esc(text)}</p>`;
  const sessionId = () => { const session = getSession(); return typeof session === 'string' ? session : session?.id; };
  function download(name, value) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));
    const a = document.createElement('a'); a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),5000);
  }
  function render() {
    if (!state) return;
    const titles = {data:'数据贡献',community:'创作与社区',updates:'软件更新'};
    let html = `<section class="platform-page" data-platform-page="${page}"><header><div><h2>${titles[page]}</h2></div>${button('refresh','刷新')}</header><p class="platform-status" role="status" id="platformStatus"></p>`;
    if (page === 'data') {
      const settings = state.data.settings;
      html += `<p class="platform-lead">由你决定哪些内容帮助改进产品。贡献到 WorldModel 与调用模型提供方的 API 是两件事。</p><div class="platform-card">
        <label class="platform-option"><span><strong>贡献诊断统计</strong><small>仅包含轮次、步数和用量统计，不含聊天正文。</small></span><input type="checkbox" id="platformDiagnostics" data-draft="platformDiagnostics" ${(drafts.platformDiagnostics??settings.diagnostics)?'checked':''}></label>
        <label class="platform-option"><span><strong>贡献模型训练对话</strong><small>先预览脱敏后的用户与助手消息，再手动提交；不会自动上传历史记录。</small></span><input type="checkbox" id="platformTraining" data-draft="platformTraining" ${(drafts.platformTraining??settings.training)?'checked':''}></label>
        ${button('data-save','保存选择')}<details><summary>数据接收服务</summary>${field('collectorEndpoint','服务地址','https://community.example.com')}${note(settings.endpoint ? `当前接收方：${settings.endpoint}` : '未配置接收服务。预览和待上传内容保留在本机。')}</details></div>
        <div class="platform-card"><h3>当前会话</h3><div class="platform-actions">${button('preview-training','预览训练对话')}${button('preview-diagnostics','预览诊断统计')}</div>
        ${preview ? `<p>${esc(preview.notice)}</p><pre class="platform-preview">${esc(JSON.stringify(preview.payload,null,2))}</pre>${button('queue','确认加入待上传')}` : note('预览不会上传任何内容。附件、工具输出和系统提示不参与贡献。')}</div>
        <h3>贡献记录</h3>${state.data.items.length ? state.data.items.slice().reverse().map(item => `<article class="platform-card"><div class="platform-row"><strong>${item.kind==='training'?'训练对话':'诊断统计'}</strong><span class="platform-badge">${labels[item.state]||esc(item.state)}</span></div><small>${time(item.created)}</small><div class="platform-actions">${item.state==='queued'?button('send','上传',`data-id="${item.id}"`)+button('withdraw','撤回',`data-id="${item.id}"`):item.state==='sent'?button('delete-remote','删除远程数据',`data-id="${item.id}"`):''}</div></article>`).join(''):note('还没有贡献记录。')}
        ${note('关闭贡献会撤回对应的本机待上传数据。已上传数据需点击远程删除并取得回执；已经用于训练的模型不能据此声称已被“反训练”。')}`;
    } else if (page === 'community') {
      const community = state.community;
      const creator = Object.values(community.creator || {});
      html += `<p class="platform-lead">把和绫音一起制作的技能、工具和界面打包分享，也可以从社区索引安装。</p>
        <div class="platform-card"><h3>发布我的创作</h3>${creator.length ? `<label class="platform-field">Creator 扩展<select id="communityPackage" data-draft="communityPackage">${creator.map(item=>`<option value="${esc(item.id)}" ${drafts.communityPackage===item.id?'selected':''}>${esc(item.name||item.id)}</option>`).join('')}</select></label>${field('communityAuthor','作者名')}${field('communityVersion','版本','1.0.0')}${button('export','预览并生成发布包')}`:note('还没有 Creator 扩展。可以在聊天中请绫音制作技能、工具或界面。')}
        ${exported ? `<div class="platform-publication"><strong>${esc(exported.entry.name)} · ${esc(exported.entry.version)}</strong><p>${esc(exported.entry.description)}</p><small>${Object.keys(exported.package.spec.files).length} 个文件 · ${exported.entry.bytes} 字节</small><ul>${Object.keys(exported.package.spec.files).map(name=>`<li>${esc(name)}</li>`).join('')}</ul><div class="platform-actions">${button('download-package','下载发布包')}${button('download-index','下载索引')}</div><details><summary>发布到公共社区</summary>${field('communityEndpoint','社区服务地址','https://community.example.com')}${field('communityToken','作者令牌','仅用于这次发布','password')}${note('发布后包内所有文件会公开。请检查是否包含私人资料或凭据；令牌不会保存到配置。')}${button('publish','确认公开发布')}</details></div>` : ''}</div>
        <div class="platform-card"><h3>社区索引</h3>${field('communitySource','添加索引','https://community.example.com/index.json')}${button('source','添加索引')}${note('支持 WorldModel 索引 URL 或本机 index.json 绝对路径。导出的本地发布包不会自动公开。')}${community.sources.map(source=>`<div class="platform-source"><code>${esc(source.source)}</code>${button('source-refresh','刷新',`data-id="${source.id}"`)}</div>`).join('')}</div>
        <h3>发现插件</h3>${community.entries.length ? community.entries.map(entry=>`<article class="platform-card"><div class="platform-row"><strong>${esc(entry.name||entry.id)}</strong><span class="platform-badge">${esc(entry.version)}</span></div><p>${esc(entry.description)}</p><small>${esc(entry.author)} · Creator</small><div class="platform-actions">${button('install','导入扩展',`data-id="${esc(entry.id)}" data-source="${entry.source_id}" data-version="${esc(entry.version)}"`)}</div></article>`).join('') : note('添加社区索引后，可在这里浏览真实的已发布插件。目前没有预设公共服务器。')}`;
    } else {
      const update = state.updates;
      html += `<p class="platform-lead">界面更新可在任务空闲时应用。后端和桌面程序先暂存，退出程序后更新，保留上一版本用于回滚。</p><div class="platform-card"><div class="platform-row"><h3>${labels[update.state]||esc(update.state)}</h3>${update.version?`<span class="platform-badge">${esc(update.version)}</span>`:''}</div>${note(update.requires_restart?'此版本包含后端或桌面程序，需要退出后更新。':'界面更新应用后，请重新打开工作台。')}${update.files?`<details><summary>${update.files.length} 个更新文件</summary><ul>${update.files.map(file=>`<li>${esc(file.path)}</li>`).join('')}</ul></details>`:''}<div class="platform-actions">${button('check-update','检查并暂存')}${update.state==='staged'?button('apply-update','应用更新'):''}${update.history?.length?button('rollback-update','回滚上个版本'):''}</div></div>
        <details class="platform-card"><summary>更新来源与信任</summary>${field('updateSource','更新包 URL 或本机绝对路径',update.trust.source||'https://releases.example.com/release.json')}${field('updateDigest','发布方提供的清单 SHA256',update.trust.manifest_sha256||'64 位校验值')}${field('updatePublicKey','或 Ed25519 公钥（Base64）',update.trust.public_key||'选填')}${note('校验值或公钥需从发布方独立取得。不会从下载包中自动接受它自己的信任声明。')}${button('save-update-source','保存更新来源')}</details>
        ${note('大型语音、模型等组件仍通过组件页独立下载；本更新通道用于应用代码。当前未启用后台自动下载或自动重启。')}`;
    }
    root.innerHTML = html + '</section>';
    if (page === 'community' && exported) {
      const details=document.createElement('details'), summary=document.createElement('summary'), content=document.createElement('pre');
      summary.textContent='检查发布包的全部内容';content.className='platform-preview';content.textContent=JSON.stringify(exported.package,null,2);
      details.append(summary,content);root.querySelector('.platform-publication')?.append(details);
    }
    root.querySelectorAll('[data-action]').forEach(element => element.addEventListener('click', () => run(element)));
    root.querySelector('#platformDiagnostics')?.addEventListener('change', saveDrafts);
    root.querySelector('#platformTraining')?.addEventListener('change', saveDrafts);
  }
  async function run(element) {
    saveDrafts();
    const action=element.dataset.action, current=generation;
    const value=id=>root.querySelector('#'+id)?.value?.trim() || '';
    let body;
    if(action==='download-package'){download(exported.entry.file,exported.package);return;}
    if(action==='download-index'){download('index.json',exported.index);return;}
    if(action==='refresh'){await refresh();return;}
    if(action==='data-save') body={action:'data_settings',diagnostics:root.querySelector('#platformDiagnostics').checked,training:root.querySelector('#platformTraining').checked,endpoint:value('collectorEndpoint')};
    else if(action.startsWith('preview-')) body={action:'data_preview',session:sessionId(),kind:action.slice(8)};
    else if(action==='queue') body={action:'data_queue',session:preview.session,kind:preview.payload.kind,sha256:preview.sha256,event_id:preview.event_id};
    else if(action==='send') body={action:'data_send',id:element.dataset.id};
    else if(action==='withdraw') body={action:'data_withdraw',id:element.dataset.id};
    else if(action==='delete-remote') body={action:'data_delete',id:element.dataset.id};
    else if(action==='export') body={action:'community_export',id:value('communityPackage'),author:value('communityAuthor'),version:value('communityVersion')||'1.0.0'};
    else if(action==='publish') body={action:'community_publish',id:exported.entry.id,version:exported.entry.version,endpoint:value('communityEndpoint'),token:value('communityToken')};
    else if(action==='source') body={action:'community_source',source:value('communitySource')};
    else if(action==='source-refresh') body={action:'community_refresh',source_id:element.dataset.id};
    else if(action==='install') body={action:'community_install',source_id:element.dataset.source,id:element.dataset.id,version:element.dataset.version};
    else if(action==='save-update-source') body={action:'update_trust',source:value('updateSource'),sha256:value('updateDigest'),public_key:value('updatePublicKey')};
    else if(action==='check-update') body={action:'update_check'};
    else if(action==='apply-update') body={action:'update_apply'};
    else if(action==='rollback-update') body={action:'update_rollback'};
    if (!body) return;
    element.disabled=true;
    try {
      const result=await request(body);
      state=result;
      if(action.startsWith('preview-')) preview={...result.result,session:body.session};
      if(action==='export') exported=result.result;
      if(action==='queue') preview=null;
      if(action==='publish'){delete drafts.communityToken;const input=root.querySelector('#communityToken');if(input)input.value='';}
      const message=result.result?.notice || (action==='publish'?'已发布，社区索引现在可发现此版本。':action.startsWith('preview-')?'请检查预览内容。':'已完成');
      if(current===generation){render();root.querySelector('#platformStatus').textContent=message;}
      notify(message);
    }catch(error){if(current===generation){root.querySelector('#platformStatus').textContent=error.message;element.disabled=false;}notify(error.message);}
  }
  async function refresh() {
    saveDrafts();const current=++generation;
    try{const result=await request();if(current===generation){state=result;if(drafts.collectorEndpoint===undefined)drafts.collectorEndpoint=state.data.settings.endpoint;for(const [field,key] of [['updateSource','source'],['updateDigest','manifest_sha256'],['updatePublicKey','public_key']])if(drafts[field]===undefined)drafts[field]=state.updates.trust[key]||'';render();}}
    catch(error){if(current===generation)root.innerHTML=`<div class="platform-page"><p role="alert">${esc(error.message)}</p><button type="button" id="platformRetry">重试</button></div>`;root.querySelector('#platformRetry')?.addEventListener('click',refresh);}
  }
  function open(next='community') {saveDrafts();page=['data','community','updates'].includes(next)?next:'community';root.innerHTML='<p class="platform-note">正在读取…</p>';return refresh();}
  return {open,refresh,close(){saveDrafts();generation++;},previewContribution:async(eventId)=>{const sid=sessionId();await open('data');const current=generation;try{const result=await request({action:'data_preview',session:sid,kind:'training',event_id:eventId});if(current===generation){preview={...result.result,session:sid};render();}}catch(error){notify(error.message);if(current===generation)root.querySelector('#platformStatus').textContent=error.message;}}};
}
