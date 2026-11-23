// Local workspace and conversation navigation. All names/content are rendered as text.
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths = {
  folder:'<path d="M3 7h7l2 2h9v11H3z"/><path d="M3 7V4h7l2 3"/>',
  edit:'<path d="m4 16-1 5 5-1L21 7l-4-4zM14 6l4 4"/>',
  pin:'<path d="M8 3h8M9 3v6l-3 4v2h12v-2l-3-4V3M12 15v7"/>',
  unread:'<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/>',
  move:'<path d="M3 7h7l2 2h9v4M3 7v13h8m3-3h8m-3-3 3 3-3 3"/>',
  fork:'<circle cx="6" cy="4" r="2"/><circle cx="6" cy="20" r="2"/><circle cx="18" cy="4" r="2"/><path d="M6 6v12m0-6c9 0 12-1 12-6"/>',
  copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V3H3v13h5"/>',
  export:'<path d="M12 3v12m-4-4 4 4 4-4M4 16v5h16v-5"/>',
  external:'<path d="M14 3h7v7m0-7L10 14M10 4H3v17h17v-7"/>',
  archive:'<path d="M4 8h16v13H4zM3 3h18v5H3zM9 12h6"/>',
  restore:'<path d="M4 8h16v13H4zM3 3h18v5H3zM12 17v-6m-3 3 3-3 3 3"/>',
  trash:'<path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/>',
  plus:'<path d="M12 5v14M5 12h14"/>',
  chevron:'<path d="m9 5 7 7-7 7"/>',
};
const icon = name => `<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.folder}</svg>`;
const workspaceName = path => String(path || '').replace(/[\\/]+$/, '').split(/[\\/]/).pop() || path;

export function initNavigation({api,getState,getView,getSession,reload,openSession,newSession,clearSession,notify,host,tell,workspaceAction=()=>{},getHostCaps=()=>({})}) {
  const menu = document.createElement('div');
  menu.id = 'conversationMenu';menu.className = 'navigation-menu';menu.hidden = true;menu.setAttribute('role','menu');
  const dialog = document.createElement('dialog');
  dialog.id = 'navigationDialog';dialog.className = 'navigation-dialog';dialog.setAttribute('aria-labelledby','navigationTitle');
  document.body.append(menu,dialog);
  let menuSession = '', anchor = null, capabilities = {}, folderPath = '', sequence = 0, modalGeneration = 0;
  const currentDialog = generation => generation === modalGeneration && dialog.open;
  const pending = new Map();
  const rows = () => [...(getState()?.workspaces || []).flatMap(w => w.sessions || []),...(getState()?.archived_sessions || [])];
  const brief = id => getSession()?.id === id ? {...rows().find(s => s.id === id),...getSession()} : rows().find(s => s.id === id);
  function closeMenu(focus=false) {
    menu.hidden=true;anchor?.setAttribute?.('aria-expanded','false');
    if (focus && anchor?.isConnected) anchor.focus();
  }
  function placeMenu(point) {
    menu.hidden=false;menu.style.left='0px';menu.style.top='0px';
    const bounds = point || anchor?.getBoundingClientRect() || {left:16,bottom:80};
    const x = bounds.x ?? bounds.left, y = point ? bounds.y : bounds.bottom + 5;
    const rect=menu.getBoundingClientRect();
    menu.style.left=`${Math.max(8,Math.min(x,innerWidth-rect.width-8))}px`;
    menu.style.top=`${Math.max(8,Math.min(y,innerHeight-rect.height-8))}px`;
    menu.querySelector('button:not(:disabled)')?.focus();
  }
  const menuItem = (action,label,glyph,key='',disabled=false,danger=false) => `<button type="button" role="menuitem" data-nav-action="${action}" ${disabled?'disabled':''} class="${danger?'danger':''}">${icon(glyph)}<span>${label}</span>${key?`<kbd>${key}</kbd>`:''}</button>`;
  function openSessionMenu(id, target, point) {
    const item=brief(id);if(!item)return;
    closeMenu();menuSession=id;anchor=target;anchor?.setAttribute?.('aria-expanded','true');
    const busy=item.status && item.status!=='idle';
    menu.setAttribute('aria-label',`${item.title}的会话操作`);
    menu.innerHTML=`<div class="navigation-menu-title">${escapeHTML(item.title)}</div>
      ${menuItem('rename','重命名','edit','Alt Ctrl R')}
      ${menuItem('pin',item.pinned?'取消置顶':'置顶','pin','Alt Ctrl P')}
      ${menuItem('unread',item.unread?'标记为已读':'标记为未读','unread','Ctrl Shift U')}
      ${menuItem('move_workspace','移至工作区','move','',busy)}
      <div class="navigation-separator" role="separator"></div>
      ${menuItem('fork','分叉会话','fork','',busy)}
      ${menuItem('copy','复制对话','copy')}
      ${menuItem('export','导出 Markdown','export')}
      ${menuItem('open_browser','在浏览器打开','external')}
      <div class="navigation-separator" role="separator"></div>
      ${menuItem(item.archived?'restore':'archive',item.archived?'恢复会话':'归档',item.archived?'restore':'archive','Ctrl Shift A',busy)}
      ${menuItem('delete','移到回收站','trash','',busy,true)}
      ${busy?'<p class="navigation-menu-note">任务执行中，结束后可移动、分叉或归档。</p>':''}`;
    placeMenu(point);
  }
  // 项目 (工作区) 的右键菜单 (2026-11-01 用户: "项目右键没有反馈"; 照 DSH / Claude 桌面版的分组菜单)
  let menuWorkspace = '';
  function openWorkspaceMenu(path, target, point) {
    const w=(getState()?.workspaces || []).find(item=>item.path===path);if(!w)return;
    closeMenu();menuSession='';menuWorkspace=path;anchor=target;anchor?.setAttribute?.('aria-expanded','true');
    const order=workspaceAction('order') || [], index=order.indexOf(path), count=w.sessions.length;
    const expanded=workspaceAction('expanded',path), canReveal=!!host && !!getHostCaps().reveal_file;
    const item=(act,label,glyph,{disabled=false,danger=false,note=''}={})=>`<button type="button" role="menuitem" data-ws-action="${act}" ${disabled?'disabled':''} class="${danger?'danger':''}" ${note?`title="${escapeHTML(note)}"`:''}>${icon(glyph)}<span>${label}</span></button>`;
    menu.setAttribute('aria-label',`${w.name}的项目操作`);
    menu.innerHTML=`<div class="navigation-menu-title" title="${escapeHTML(path)}">${escapeHTML(w.name)}</div>
      ${item('new','新建会话','plus')}
      ${item('reveal','在文件夹中显示','folder',{disabled:!canReveal,note:canReveal?'':host?'要等新版桌宠宿主':'只在桌面工作台里'})}
      ${item('copy','复制文件夹路径','copy')}
      <div class="navigation-separator" role="separator"></div>
      ${item('up','上移','move',{disabled:index<=0})}
      ${item('down','下移','move',{disabled:index<0 || index>=order.length-1})}
      ${item('toggle_all',expanded?'只显示最近 5 个':`显示全部会话（${count}）`,'unread',{disabled:count<=5 && !expanded})}
      ${item('collapse_all','全部折叠','archive')}
      ${item('expand_all','全部展开','restore')}
      <div class="navigation-separator" role="separator"></div>
      ${item('archive_all',`全部归档（${count}）`,'archive',{disabled:!count})}
      ${item('delete_all',`删除所有会话（${count}）`,'trash',{disabled:!count,danger:true})}
      ${canReveal?'':'<p class="navigation-menu-note">“在文件夹中显示”要等新版桌宠宿主；可以先复制路径。</p>'}`;
    placeMenu(point);
  }
  async function workspacePerform(action, path) {
    closeMenu();
    const w=(getState()?.workspaces || []).find(item=>item.path===path);if(!w)return;
    if(action==='new'){await newSession(path);return;}
    if(action==='copy'){await navigator.clipboard.writeText(path);notify('已复制文件夹路径');return;}
    if(action==='archive_all' || action==='delete_all'){bulk(w,action==='delete_all');return;}
    await workspaceAction(action,path);
  }
  // 整个项目的会话一起归档 / 移到回收站: 先说清楚几个、能不能恢复, 正在做事的不动
  function bulk(w, remove) {
    const idle=w.sessions.filter(item=>!item.status || item.status==='idle'), busy=w.sessions.length-idle.length;
    const title=remove?`删除「${escapeHTML(w.name)}」里的所有会话？`:`归档「${escapeHTML(w.name)}」里的所有会话？`;
    const body=remove?`${idle.length} 个会话会移到回收站，可以在“归档与回收站”里恢复；项目文件夹里的文件不动。`:`${idle.length} 个会话会归档，可以在“归档与回收站”里恢复；项目文件夹里的文件不动。`;
    const generation=showDialog(title,`<form><p class="navigation-description">${body}${busy?`正在做事的 ${busy} 个先不动。`:''}</p>${footer(remove?`删除 ${idle.length} 个会话`:`归档 ${idle.length} 个会话`,remove)}</form>`);
    if(!idle.length)dialog.querySelector('[type="submit"]').disabled=true;
    dialog.querySelector('form').addEventListener('submit',async event=>{
      event.preventDefault();const button=event.submitter;button.disabled=true;
      let done=0,failed=0;
      for(const item of idle){
        if(!currentDialog(generation))break;
        try{await api({action:remove?'delete':'archive',id:item.id,...(remove?{confirm:true}:{})});done++;if(getSession()?.id===item.id)clearSession();}
        catch{failed++;}
        status(`${remove?'删除':'归档'}中… ${done} / ${idle.length}`);
      }
      await reload();
      if(currentDialog(generation))dialog.close();
      notify(`${remove?'已移到回收站':'已归档'} ${done} 个会话${failed?`，${failed} 个没成（可能是子任务，要从主会话处理）`:''}`);
    });
  }
  function showDialog(title,content,wide=false) {
    closeMenu();modalGeneration++;dialog.classList.toggle('wide',wide);
    dialog.innerHTML=`<header><h2 id="navigationTitle">${title}</h2><button type="button" data-nav-close class="navigation-icon" aria-label="关闭">×</button></header>${content}`;
    dialog.querySelectorAll('[data-nav-close]').forEach(button=>button.addEventListener('click',()=>dialog.close()));
    if(!dialog.open)dialog.showModal();
    return modalGeneration;
  }
  function status(message) {const el=dialog.querySelector('.navigation-feedback');if(el)el.textContent=message;}
  const footer = (label,danger=false) => `<p class="navigation-feedback" role="status"></p><footer><button type="button" data-nav-close>取消</button><button type="submit" class="${danger?'danger-solid':'primary'}">${label}</button></footer>`;
  function nativeRequest(op,args={}) {
    return new Promise((resolve,reject)=>{
      const rid=`navigation-${Date.now()}-${++sequence}`;
      const timer=setTimeout(()=>{pending.delete(rid);reject(new Error('系统对话框未返回，请关闭后重试。'));},600000);
      pending.set(rid,{resolve,reject,timer});tell(op,{rid,...args});
    });
  }
  function updateFolder(path,name='') {
    const input=dialog.querySelector('[name="name"]'), old=workspaceName(folderPath);
    if(input && (!input.value.trim() || input.value===old))input.value=name || workspaceName(path);
    folderPath=path;
    const shown=dialog.querySelector('#workspaceFolderValue');if(shown){shown.textContent=path || '选择此电脑上的项目文件夹';shown.title=path;}
    dialog.querySelector('#workspaceChoose').classList.toggle('selected',!!path);
    dialog.querySelector('#workspaceCreate').disabled=!path;
  }
  function addWorkspace() {
    folderPath='';
    const generation=showDialog('添加工作区',`<form id="workspaceForm"><label for="workspaceTitle">工作区名称</label><div class="navigation-name-field">${icon('folder')}<input id="workspaceTitle" name="name" maxlength="64" placeholder="例如：桌宠计划" autocomplete="off"></div>
      <label>源文件夹</label><div class="workspace-folder-box"><button type="button" id="workspaceChoose">${icon('folder')}<strong>选择文件夹</strong><span id="workspaceFolderValue">选择此电脑上的项目文件夹</span></button><small>已有文件保留在原位置。</small></div>
      <details class="workspace-path-fallback"><summary>粘贴文件夹路径</summary><label for="workspacePath">文件夹路径</label><input id="workspacePath" placeholder="G:\\Projects\\…" autocomplete="off"></details>
      <p class="navigation-feedback" role="status"></p><footer><button type="button" data-nav-close>取消</button><button id="workspaceCreate" type="submit" class="primary" disabled>添加工作区</button></footer></form>`);
    dialog.querySelector('#workspaceChoose').addEventListener('click',async event=>{
      if(!host || !capabilities.folder_picker){
        dialog.querySelector('.workspace-path-fallback').open=true;
        status(host?'当前宿主尚未支持文件夹选择，请更新桌宠；也可以粘贴路径。':'浏览器无法取得本机完整路径。可在桌宠工作台选择文件夹，或在这里粘贴路径。');
        dialog.querySelector('#workspacePath').focus();return;
      }
      const button=event.currentTarget;button.disabled=true;button.setAttribute('aria-busy','true');status('等待选择文件夹…');
      try {const result=await nativeRequest('choose_workspace_folder');if(generation!==modalGeneration || !dialog.open)return;
        if(result.error)throw new Error(result.error);
        if(!result.cancelled && result.path)updateFolder(result.path,result.name);
        status(result.cancelled?'已取消选择，可重新选择文件夹。':'');
      }catch(error){if(generation===modalGeneration)status(error.message);}finally{button.disabled=false;button.removeAttribute('aria-busy');}
    });
    dialog.querySelector('#workspacePath').addEventListener('input',event=>updateFolder(event.target.value.trim()));
    dialog.querySelector('form').addEventListener('submit',async event=>{
      event.preventDefault();const button=dialog.querySelector('#workspaceCreate');button.disabled=true;
      try {await api({action:'workspace',path:folderPath,name:dialog.querySelector('[name="name"]').value.trim()});await reload();if(currentDialog(generation))dialog.close();notify('工作区已添加');}
      catch(error){if(currentDialog(generation))status(error.message);}finally{button.disabled=!folderPath;}
    });
    dialog.querySelector('#workspaceTitle').focus();
  }
  function workspaceMenu(target) {
    closeMenu();anchor=target;menuSession='';menu.setAttribute('aria-label','选择工作区');
    menu.innerHTML='<div class="navigation-menu-title">在工作区中新建会话</div>'+(getState()?.workspaces || []).map(w=>`<button role="menuitem" type="button" data-nav-workspace="${escapeHTML(w.path)}" title="${escapeHTML(w.path)}">${icon('folder')}<span>${escapeHTML(w.name)}</span></button>`).join('')+`<div class="navigation-separator" role="separator"></div>${menuItem('add_workspace','添加工作区','plus')}`;
    placeMenu();
  }
  async function changed(id,action,extra={}) {
    const result=await api({action,id,...extra});
    if(['archive','delete'].includes(action) && getSession()?.id===id)clearSession();
    if(result.session && getSession()?.id===id)Object.assign(getSession(),result.session);
    await reload();return result;
  }
  function rename(id) {
    const item=brief(id);if(!item)return;
    const generation=showDialog('重命名会话',`<form><label for="conversationTitle">会话名称</label><input id="conversationTitle" name="title" maxlength="80" required value="${escapeHTML(item.title)}">${footer('保存')}</form>`);
    dialog.querySelector('form').addEventListener('submit',async event=>{
      event.preventDefault();const button=event.submitter;button.disabled=true;
      try {await changed(id,'rename',{title:dialog.querySelector('[name="title"]').value.trim()});if(currentDialog(generation))dialog.close();notify('会话已重命名');}catch(error){if(currentDialog(generation))status(error.message);}finally{button.disabled=false;}
    });
    dialog.querySelector('input').select();
  }
  function move(id) {
    const item=brief(id);if(!item)return;
    const choices=(getState()?.workspaces || []).filter(w=>w.path!==item.workspace);
    const generation=showDialog('移至工作区',`<form><p class="navigation-description">后续任务将在所选文件夹中执行。已有消息、附件与成果保留原记录，文件不会随会话搬动。</p><label for="conversationWorkspace">目标工作区</label><select id="conversationWorkspace" required ${choices.length?'':'disabled'}>${choices.map(w=>`<option value="${escapeHTML(w.path)}">${escapeHTML(w.name)}</option>`).join('')}</select>${choices.length?'':'<p class="navigation-description">请先添加另一个工作区。</p>'}${footer('移动会话')}</form>`);
    if(!choices.length)dialog.querySelector('[type="submit"]').disabled=true;
    dialog.querySelector('form').addEventListener('submit',async event=>{
      event.preventDefault();const button=event.submitter;button.disabled=true;
      try{await changed(id,'move_workspace',{workspace:dialog.querySelector('select').value});if(currentDialog(generation))dialog.close();notify('会话工作区已更新');}catch(error){if(currentDialog(generation))status(error.message);}finally{button.disabled=false;}
    });
  }
  function trash(id) {
    const item=brief(id);if(!item)return;
    const generation=showDialog('将会话移到回收站？',`<form><p class="navigation-description"><strong>${escapeHTML(item.title)}</strong></p><p class="navigation-description">会话将从列表移除，可在“归档与回收站”中恢复。工作区里的文件保留。</p>${footer('移到回收站',true)}</form>`);
    dialog.querySelector('form').addEventListener('submit',async event=>{
      event.preventDefault();const button=event.submitter;button.disabled=true;
      try{await changed(id,'delete',{confirm:true});if(currentDialog(generation))dialog.close();notify('已移到回收站');}catch(error){if(currentDialog(generation))status(error.message);}finally{button.disabled=false;}
    });
  }
  async function conversationText(id) {
    const view=await getView(id);
    const messages=(view.events || []).filter(e=>e.type==='user' || e.type==='assistant');
    return {title:view.title || '会话',text:`# ${view.title || '会话'}\n\n`+messages.map(e=>`## ${e.type==='user'?'你':'助手'}\n\n${e.text || ''}`).join('\n\n')};
  }
  async function perform(action,id) {
    closeMenu();const item=brief(id);
    if(action==='add_workspace'){addWorkspace();return;}
    if(!item)return;
    if(action==='rename'){rename(id);return;}
    if(action==='move_workspace'){move(id);return;}
    if(action==='delete'){trash(id);return;}
    if(action==='pin' || action==='unread'){await changed(id,action,{[action==='pin'?'pinned':'unread']:!item[action==='pin'?'pinned':'unread']});return;}
    if(action==='archive' || action==='restore'){await changed(id,action);notify(action==='archive'?'会话已归档，可在归档与回收站中恢复':'会话已恢复');return;}
    if(action==='fork'){const view=await api({action:'fork',id});await reload();await openSession(view.id);notify('已从当前对话建立分叉，文件仍使用原工作区');return;}
    if(action==='copy' || action==='export'){
      const result=await conversationText(id);
      if(action==='copy'){await navigator.clipboard.writeText(result.text);notify('对话文字已复制');}
      else {const url=URL.createObjectURL(new Blob([result.text],{type:'text/markdown;charset=utf-8'}));const link=document.createElement('a');link.href=url;link.download=`${result.title.replace(/[<>:"/\\|?*\u0000-\u001f]/g,'_').slice(0,80)}.md`;link.click();setTimeout(()=>URL.revokeObjectURL(url),10000);notify('已导出可见对话文字');}return;
    }
    if(action==='open_browser'){
      if(host){if(!capabilities.open_session_browser)throw new Error('当前宿主尚未支持，请更新桌宠后使用。');const result=await nativeRequest('open_session_browser',{id});if(result.error || result.ok===false)throw new Error(result.error || '无法打开浏览器');}
      else window.open(`/agent?s=${encodeURIComponent(id)}`,'_blank','noopener');
    }
  }
  async function archived() {
    await reload();const generation=showDialog('归档与回收站',`<div class="navigation-archive"><input id="archiveSearch" type="search" placeholder="搜索已归档的会话" aria-label="搜索归档与回收站"><div id="archiveList"></div><p class="navigation-feedback" role="status"></p></div><footer><button type="button" data-nav-close>完成</button></footer>`,true);
    let shown=40;
    function render(){
      const query=dialog.querySelector('#archiveSearch').value.trim().toLowerCase();
      const items=(getState()?.archived_sessions || []).filter(item=>!query || item.title.toLowerCase().includes(query));
      dialog.querySelector('#archiveList').innerHTML=items.length?items.slice(0,shown).map(item=>`<div class="navigation-archive-row">${icon('archive')}<div><strong>${escapeHTML(item.title)}</strong><small>${escapeHTML(workspaceName(item.workspace))}${item.trashed?' · 回收站':''}</small></div><button type="button" data-restore="${escapeHTML(item.id)}">恢复</button></div>`).join('')+(items.length>shown?'<button type="button" class="navigation-more" data-more>显示更多</button>':''):'<p class="navigation-empty">'+(query?'没有匹配的会话':'还没有归档或移到回收站的会话')+'</p>';
    }
    dialog.querySelector('#archiveSearch').addEventListener('input',()=>{shown=40;render();});
    dialog.querySelector('#archiveList').addEventListener('click',async event=>{
      const button=event.target.closest('button');if(!button)return;
      if(button.hasAttribute('data-more')){shown+=40;render();return;}
      button.disabled=true;
      try{await changed(button.dataset.restore,'restore');if(currentDialog(generation)){render();status('会话已恢复到原工作区');}}catch(error){if(currentDialog(generation))status(error.message);button.disabled=false;}
    });render();dialog.querySelector('#archiveSearch').focus();
  }
  menu.addEventListener('click',event=>{
    const button=event.target.closest('button');if(!button || button.disabled)return;
    if(button.dataset.navWorkspace){closeMenu();Promise.resolve(newSession(button.dataset.navWorkspace)).catch(error=>notify(error.message));return;}
    if(button.dataset.wsAction){Promise.resolve(workspacePerform(button.dataset.wsAction,menuWorkspace)).catch(error=>notify(error.message));return;}
    perform(button.dataset.navAction,menuSession).catch(error=>notify(error.message));
  });
  const sideScroll=document.getElementById('sideScroll')||document.getElementById('tree');  // 置顶分组 (#pinned) 和项目树 (#tree) 都在里面
  sideScroll.addEventListener('click',event=>{
    const button=event.target.closest('[data-session-more]');if(button){event.preventDefault();event.stopPropagation();openSessionMenu(button.dataset.sessionMore,button);return;}
    const wsButton=event.target.closest('[data-ws-menu]');if(wsButton){event.preventDefault();event.stopPropagation();openWorkspaceMenu(wsButton.dataset.wsMenu,wsButton);}
  });
  sideScroll.addEventListener('contextmenu',event=>{
    const row=event.target.closest('[data-session-row]');if(row){event.preventDefault();openSessionMenu(row.dataset.sessionRow,row.querySelector('[data-session-more]'),{x:event.clientX,y:event.clientY});return;}
    const ws=event.target.closest('.ws-row[data-ws]');if(ws){event.preventDefault();openWorkspaceMenu(ws.dataset.ws,ws.querySelector('[data-ws-menu]'),{x:event.clientX,y:event.clientY});}
  });
  document.addEventListener('pointerdown',event=>{if(!menu.hidden && !menu.contains(event.target) && !event.target.closest('[data-session-more],[data-ws-menu],#sessionMore,#wsName'))closeMenu();});
  document.addEventListener('keydown',event=>{
    if(event.defaultPrevented)return;
    if(!menu.hidden && event.key==='Escape'){event.preventDefault();event.stopImmediatePropagation();closeMenu(true);return;}
    if(!menu.hidden && ['ArrowDown','ArrowUp','Home','End'].includes(event.key)){
      event.preventDefault();const buttons=[...menu.querySelectorAll('button:not(:disabled)')],index=buttons.indexOf(document.activeElement);
      buttons[event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length]?.focus();return;
    }
    if(!menu.hidden && event.key==='Tab'){closeMenu();return;}
    const row=event.target.closest?.('[data-session-row]');
    if(row && (event.key==='ContextMenu' || event.shiftKey && event.key==='F10')){event.preventDefault();openSessionMenu(row.dataset.sessionRow,row.querySelector('[data-session-more]'));return;}
    if(document.querySelector('dialog[open]') || document.querySelector('.settings-mode') || event.target.closest?.('input,textarea,select,[contenteditable="true"]'))return;
    const key=event.key.toLowerCase();let action='';
    if(event.ctrlKey && event.altKey && !event.shiftKey)action=key==='r'?'rename':key==='p'?'pin':'';
    if(event.ctrlKey && event.shiftKey && !event.altKey)action=key==='u'?'unread':key==='a'?'archive':'';
    if(action && getSession()?.id){event.preventDefault();perform(action,getSession().id).catch(error=>notify(error.message));}
  });
  addEventListener('resize',()=>closeMenu());
  document.getElementById('tree').addEventListener('scroll',()=>closeMenu(),{passive:true});  // 照旧只绑项目树 (绑到滚动区的话, 右键前把行滚进视野会把刚开的菜单关掉)
  return {addWorkspace,workspaceMenu,archived,openSessionMenu,openWorkspaceMenu,closeMenu,
    onHost(message){
      if(message?.t==='workbench-capabilities' && Number(message.version)>=1){capabilities=message;return false;}
      if(['workbench-folder','workbench-opened'].includes(message?.t)){
        const request=pending.get(message.rid);if(request){clearTimeout(request.timer);pending.delete(message.rid);request.resolve(message);}return true;
      }return false;
    }};
}
