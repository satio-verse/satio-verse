const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
// 第六轮 (学 Linear): 页头 + 列表在前, 新建的表单点了"新建持续任务"才出来; 图标是工作台自己的线条图标
const icon = path => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
const ICONS = {plus:'<path d="M12 5v14M5 12h14"/>', refresh:'<path d="M20 11a8 8 0 1 0-2.4 5.7M20 4v7h-7"/>', clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>', chev:'<path d="m9 6 6 6-6 6"/>'};
const names = {scheduled:'等待执行',running:'正在推进',paused:'已暂停',waiting:'等你补充',blocked:'需要处理',completed:'已完成',interrupted:'需要核对'};
async function request(body) {
  const response = await fetch('/api/agent/resident', body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {});
  const data = await response.json();
  if(!response.ok || data.ok === false) throw new Error(data.error || '暂时无法读取持续任务');
  return data;
}
export async function renderResident(container, {getSession, openSession}) {
  container._residentCleanup?.();
  let active=true,timer=0,busy=false,sequence=0;
  const owner=()=>active && container.querySelector('#residentView');
  container._residentCleanup=()=>{active=false;sequence++;clearTimeout(timer);};
  const newButton = (kind = 'primary') => `<button type="button" class="wb-btn ${kind}" data-resident-new>${icon(ICONS.plus)}新建持续任务</button>`;
  container.innerHTML=`<section id="residentView"><header class="cv-head"><div class="cv-title"><h2>自动化任务</h2><p>交给绫音持续推进：关掉工作台也接着做，结果和要你决定的事留在这里。在本机跑，电脑和桌宠服务要开着。</p></div><div class="cv-head-actions"><button type="button" class="wb-btn icon" data-resident-refresh title="刷新" aria-label="刷新">${icon(ICONS.refresh)}</button>${newButton()}</div></header><form id="residentCreate" class="cv-pop" hidden><strong class="cv-pop-title">新建持续任务</strong><label for="residentObjective">希望她持续完成什么？</label><textarea id="residentObjective" name="objective" rows="3" maxlength="12000" placeholder="例如：整理这个项目的资料，补齐引用并生成可交付的报告。" required></textarea><details class="cv-settings"><summary><span class="cv-chev">${icon(ICONS.chev)}</span>开始时间与预算</summary><label class="cv-prop"><span>开始时间</span><input name="start" type="datetime-local"></label><label class="cv-prop"><span>整个目标的费用预算（元）</span><input name="budget" type="number" min="0" step="0.01" value="0"></label><small class="cv-muted">不填时间就是马上开始；预算 0 表示不另设额度，账本里原来的设置照样管。</small></details><div class="cv-pop-row"><small class="cv-muted">在当前工作区开一个独立的任务会话，沿用现在的权限和预设。</small><span class="grow"></span><button type="button" class="wb-btn text" data-resident-cancel>取消</button><button class="wb-btn primary" type="submit">开始持续任务</button></div></form><p id="residentNotice" class="cv-notice" role="status"></p><div id="residentItems" class="cv-list"></div></section>`;
  const hideForm=()=>{const form=container.querySelector('#residentCreate');if(form){form.reset();form.hidden=true;}};
  const notice=value=>{if(owner())container.querySelector('#residentNotice').textContent=value;};
  async function refresh(){
    const ticket=++sequence;clearTimeout(timer);
    try {
      const data=await request();if(!owner()||ticket!==sequence)return;
      const list=container.querySelector('#residentItems');
      const opened=new Set([...list.querySelectorAll('details[open][data-resident-id]')].map(el=>el.dataset.residentId));  // 5 秒刷一次, 别把点开的收回去
      list.innerHTML=data.items.map(item=>`<details class="cv-cap" data-resident-id="${escape(item.id)}" data-revision="${item.revision}" ${opened.has(item.id)?'open':''}><summary class="cv-row"><span class="cv-state ${escape(item.state)}"><i></i></span><strong>${escape(item.title)}</strong><span class="cv-desc">${escape(item.summary)}</span><span class="cv-meta">${item.rounds} 轮 · ¥${Number(item.spent).toFixed(2)}${item.state==='scheduled'?` · 下次 ${new Date(item.next_at*1000).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'})}`:''}</span><span class="cv-state-text">${escape(names[item.state]||item.state)}</span><span class="cv-chev">${icon(ICONS.chev)}</span></summary><div class="cv-detail"><p>${escape(item.objective)}</p><div class="cv-actions"><button type="button" class="wb-btn" data-resident-session="${escape(item.session_id)}">查看会话 ↗</button>${['running','scheduled'].includes(item.state)?'<button type="button" class="wb-btn" data-resident-action="pause">暂停</button>':''}${['paused','waiting','blocked','interrupted'].includes(item.state)?'<button type="button" class="wb-btn" data-resident-action="resume">继续任务</button>':''}${item.state!=='completed'?'<button type="button" class="wb-btn text" data-resident-action="complete">标记完成</button>':''}</div></div></details>`).join('')||`<div class="cv-empty">${icon(ICONS.clock)}<h3>还没有持续任务</h3><p>给她一个明确目标，进度会保存在这里。</p>${newButton('')}</div>`;
      if(!data.runner_online)notice('后台调度器尚未启动；任务已保存，服务启动后执行。');
    }catch(error){if(owner()&&ticket===sequence)notice(error.message);}
    if(owner())timer=setTimeout(refresh,5000);
  }
  container.querySelector('#residentCreate').addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();event.stopPropagation();hideForm();}});
  container.querySelector('#residentCreate').addEventListener('submit',async event=>{
    event.preventDefault();if(busy)return;
    const session=getSession();if(!session?.id){notice('先选择一个主会话及工作区。');return;}
    const form=event.currentTarget,data=new FormData(form),start=data.get('start');
    busy=true;form.querySelector('button[type=submit]').disabled=true;
    try{await request({action:'create',session_id:session.id,objective:data.get('objective'),budget_cny:Number(data.get('budget')||0),next_at:start?new Date(start).getTime()/1000:0});if(!owner())return;form.reset();hideForm();notice('已建立持续任务。你可以继续当前对话。');await refresh();}
    catch(error){notice(error.message);}finally{busy=false;if(owner())form.querySelector('button[type=submit]').disabled=false;}
  });
  container.querySelector('#residentView').addEventListener('click',async event=>{
    const button=event.target.closest('button');if(!button)return;
    if(button.hasAttribute('data-resident-refresh'))return refresh();
    if(button.hasAttribute('data-resident-new')){const form=container.querySelector('#residentCreate');form.hidden=false;form.querySelector('textarea').focus();return;}
    if(button.hasAttribute('data-resident-cancel'))return hideForm();
    if(button.dataset.residentSession)return openSession(button.dataset.residentSession);
    if(!button.dataset.residentAction||busy)return;
    const card=button.closest('[data-resident-id]');busy=true;button.disabled=true;
    try{await request({action:button.dataset.residentAction,id:card.dataset.residentId,expected_revision:Number(card.dataset.revision)});await refresh();}
    catch(error){notice(error.message);await refresh();}finally{busy=false;}
  });
  await refresh();
}
