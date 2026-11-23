const esc = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels = {waiting:'等你操作',running:'进行中',completed:'已完成',failed:'上一条命令出错',closed:'已关闭'};
const stateLabel=row=>row.state==='closed'&&row.exit_code!=null?`已退出 · 代码 ${row.exit_code}`:(labels[row.state]||row.state);
const stateDetail=row=>[row.cwd,row.command&&`命令：${row.command}`,row.last_exit_code!=null&&`上一条命令退出码：${row.last_exit_code}`,row.notice].filter(Boolean).join('\n');
const themes = {
  midnight:{background:'#171a22',foreground:'#e1e5ef',cursor:'#b9a3ff',selectionBackground:'#74629966',black:'#303340',red:'#ef8a91',green:'#8ed2ae',yellow:'#e5c383',blue:'#97b9ed',magenta:'#c6a6ef',cyan:'#8dcbd2',white:'#e3e7f0',brightBlack:'#858a9b',brightRed:'#ffa6ae',brightGreen:'#b0edc9',brightYellow:'#f3dca4',brightBlue:'#c0d5ff',brightMagenta:'#dfc4ff',brightCyan:'#b2eef1',brightWhite:'#ffffff'},
  forest:{background:'#14211e',foreground:'#dce7df',cursor:'#9bd9ab',selectionBackground:'#7eaa8b55',black:'#283b31',red:'#e69a91',green:'#a3ce9f',yellow:'#d5c38f',blue:'#9abdd0',magenta:'#c0abd2',cyan:'#87c9b9',white:'#e1e8df'},
  paper:{background:'#f7f5ef',foreground:'#343c46',cursor:'#68569a',selectionBackground:'#b9a3dd55',black:'#414751',red:'#a83c4e',green:'#35704c',yellow:'#8b6517',blue:'#345f9c',magenta:'#805299',cyan:'#277777',white:'#b9b7b0'},
};
let loaded;
function script(url){return new Promise((resolve,reject)=>{const el=document.createElement('script');el.src=url;el.onload=resolve;el.onerror=()=>reject(Error('终端组件加载失败，请刷新工作台'));document.head.append(el);});}
function dependencies(){return loaded ||= script('/pet/vendor/xterm/xterm.js').then(()=>Promise.all(['/pet/vendor/xterm/addon-fit.js','/pet/vendor/xterm/addon-search.js'].map(script))).catch(error=>{loaded=null;throw error;});}
async function request(sid, action, values={}){
  const options=action?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id:sid,action,...values})}:{};
  const response=await fetch('/api/agent/terminal'+(action?'':'?'+new URLSearchParams({id:sid,...values})),options);
  const data=await response.json();if(!response.ok||data.ok===false)throw Error(data.error||'终端暂时不可用');return data;
}
export function initTerminalPanel(container,{getSession,ensureSession,notify,openPanel,openSession}){
  let visible=false,alive=true,active='',sid='',creating=false,outputTimer=0,listTimer=0,resizeTimer=0,refreshing=false,refreshAgain=false,theme='midnight';
  const instances=new Map(),rows=new Map(),selected=new Map(),seenStates=new Map(),knownSessions=new Set(),epochs=new Map(),noticeTimes=new Map();
  try{theme=localStorage.getItem('wm.terminal.theme')||theme;}catch{}
  if(!themes[theme])theme='midnight';
  container.className='workbench-terminal';container.hidden=true;
  container.innerHTML=`<div class="terminal-top"><div class="terminal-tabs" role="tablist" aria-label="终端会话"></div><span class="terminal-engine" title="Windows 原生交互终端">ConPTY</span><button type="button" data-term-new title="新建终端">＋ 新建</button></div><div class="terminal-tools"><div class="terminal-search"><input type="search" aria-label="搜索终端输出" placeholder="搜索输出"><button type="button" data-term-find="previous" title="上一个">↑</button><button type="button" data-term-find="next" title="下一个">↓</button></div><button type="button" data-term-copy title="复制选中内容">复制</button><button type="button" data-term-clear title="清空显示，保留 Shell">清屏</button><details class="terminal-options"><summary title="终端选项">•••</summary><div><label>主题<select data-term-theme><option value="midnight">午夜紫</option><option value="forest">松林绿</option><option value="paper">暖纸白</option></select></label><label>会话名称<input data-term-title maxlength="80" placeholder="例如：开发服务"></label><button type="button" data-term-rename>重命名</button><button type="button" data-term-notifications>开启后台桌面通知</button></div></details></div><div class="terminal-stage"><div class="terminal-placeholder"><span>⌘</span><h3>让命令留在同一个上下文</h3><p>目录、变量与交互程序持续保留。收起面板后仍在运行。</p><button type="button" data-term-new>新建终端</button></div><div class="terminal-surfaces"></div></div><div class="terminal-status"><span data-term-state>尚未启动</span><span data-term-cwd></span><button type="button" data-term-interrupt title="中断当前命令，保留终端 (Ctrl+C)">中断</button><button type="button" data-term-close>关闭会话</button></div><p class="terminal-error" role="status" hidden></p><div class="terminal-keyboard-hint">Ctrl + Shift + ↑ / ↓ 跳转命令 · Ctrl + Shift + F 搜索</div>`;
  const find=sel=>container.querySelector(sel), error=message=>{find('.terminal-error').textContent=message;find('.terminal-error').hidden=!message;};
  const current=()=>instances.get(active);
  find('[data-term-theme]').value=theme;
  function applyTheme(item){item.term.options.theme=themes[theme];container.style.setProperty('--terminal-bg',themes[theme].background);container.style.setProperty('--terminal-fg',themes[theme].foreground);}
  container.style.setProperty('--terminal-bg',themes[theme].background);container.style.setProperty('--terminal-fg',themes[theme].foreground);
  function redraw(){
    const own=[...rows.values()].filter(row=>row._sid===sid);
    find('.terminal-tabs').innerHTML=own.map(row=>`<button type="button" role="tab" aria-selected="${row.id===active}" data-term-select="${esc(row.id)}" title="${esc(stateLabel(row)+'\n'+stateDetail(row))}"><span class="terminal-session-state ${esc(row.state)}"></span><span>${esc(row.title||'PowerShell')}</span>${row.last_exit_code||row.exit_code?'<span class="terminal-failure">!</span>':''}${row.progress!=null?`<small>${Math.round(row.progress)}%</small>`:''}</button>`).join('');
    for(const [id,item] of instances)item.node.hidden=id!==active;
    find('.terminal-placeholder').hidden=!!active;
    const row=rows.get(active);
    find('[data-term-state]').textContent=row?stateLabel(row)+(row.progress!=null?` · ${Math.round(row.progress)}%`:''):'尚未启动';
    find('[data-term-state]').title=row?stateDetail(row):'';
    find('[data-term-state]').dataset.state=row?.state||'';
    find('[data-term-cwd]').textContent=row?.cwd||getSession()?.workspace||'';
    find('[data-term-cwd]').title=row?.cwd||'';
    find('[data-term-interrupt]').disabled=!row||row.state==='closed';
    find('[data-term-close]').disabled=!row||row.state==='closed';
    if(!find('.terminal-options').open)find('[data-term-title]').value=row?.title||'';
  }
  async function attach(row){
    if(!row)return;
    if(instances.has(row.id))return instances.get(row.id);
    await dependencies();if(!alive)return;
    if(instances.has(row.id))return instances.get(row.id);
    const node=document.createElement('div');node.className='terminal-surface';node.hidden=row.id!==active;find('.terminal-surfaces').append(node);
    const term=new window.Terminal({theme:themes[theme],fontFamily:'Cascadia Mono, Consolas, monospace',fontSize:13,lineHeight:1.25,cursorBlink:true,scrollback:6000,allowProposedApi:true,allowTransparency:false,convertEol:false,screenReaderMode:true});
    const fit=new window.FitAddon.FitAddon(),search=new window.SearchAddon.SearchAddon();term.loadAddon(fit);term.loadAddon(search);term.open(node);
    const item={term,node,fit,search,sid:row._sid,cursor:0,markers:[],queue:Promise.resolve(),reading:false,pending:'',inputTimer:0,disposed:false};instances.set(row.id,item);
    term.parser.registerOscHandler(52,()=>true); // A process cannot change the system clipboard through terminal output.
    term.parser.registerOscHandler(133,value=>{if(value==='A'){const marker=term.registerMarker(0);if(marker)item.markers.push(marker);item.markers=item.markers.filter(x=>!x.isDisposed).slice(-100);}return true;});
    function flushInput(){
      clearTimeout(item.inputTimer);item.inputTimer=0;
      if(!item.pending||item.disposed)return;
      const all=item.pending;item.pending='';
      for(let offset=0;offset<all.length;){
        let end=Math.min(offset+16000,all.length);
        if(end<all.length&&/[\uD800-\uDBFF]/.test(all[end-1]))end--;
        const data=all.slice(offset,end);offset=end;
        item.queue=item.queue.then(()=>{if(!item.disposed)return request(item.sid,'input',{terminal_id:row.id,data});}).catch(e=>{if(active===row.id)error(e.message);});
      }
    }
    item.flushInput=flushInput;
    term.onData(data=>{item.pending+=data;if(/[\r\n\x03]/.test(data)||item.pending.length>=16000)flushInput();else if(!item.inputTimer)item.inputTimer=setTimeout(flushInput,24);});
    term.attachCustomKeyEventHandler(event=>{
      if(event.type!=='keydown')return true;
      if((event.ctrlKey||event.metaKey)&&event.shiftKey&&event.key.toLowerCase()==='f'){find('.terminal-search input').focus();event.preventDefault();return false;}
      if(event.ctrlKey&&event.shiftKey&&['ArrowUp','ArrowDown'].includes(event.key)){jump(event.key==='ArrowUp'?-1:1);event.preventDefault();return false;}
      if(event.ctrlKey&&event.shiftKey&&event.key.toLowerCase()==='c'){copySelection();return false;}
      return true;
    });
    return item;
  }
  function jump(direction){const item=current();if(!item)return;const line=item.term.buffer.active.viewportY;const positions=item.markers.filter(x=>!x.isDisposed).map(x=>x.line);const target=direction<0?positions.filter(n=>n<line).at(-1):positions.find(n=>n>line);if(target!==undefined)item.term.scrollToLine(target);else if(direction>0)item.term.scrollToBottom();}
  async function select(id){const row=rows.get(id);if(!row||row._sid!==sid)return;active=id;selected.set(sid,id);redraw();const item=await attach(row);if(!item||active!==id||sid!==row._sid)return;redraw();resize();await readOutput(id);if(visible&&active===id)item.term.focus();}
  function resize(){clearTimeout(resizeTimer);resizeTimer=setTimeout(async()=>{const id=active,item=instances.get(id);if(!visible||!item||item.node.hidden)return;try{item.fit.fit();const cols=Math.max(20,Math.min(500,item.term.cols)),count=Math.max(5,Math.min(200,item.term.rows));if(cols!==item.term.cols||count!==item.term.rows)item.term.resize(cols,count);if(rows.get(id)?.state!=='closed')await request(item.sid,'resize',{terminal_id:id,cols,rows:count});}catch(e){if(active===id)error(e.message);}},100);}
  const observer=new ResizeObserver(resize);observer.observe(container);
  async function readOutput(id){
    const item=instances.get(id);if(!item||item.reading)return;item.reading=true;
    try{const epoch=epochs.get(item.sid)||0;const data=await request(item.sid,null,{terminal_id:id,after:item.cursor});if(!alive||item.disposed)return;const row=data.terminal||data;
      if(row.id&&epoch===(epochs.get(item.sid)||0))rows.set(id,{...rows.get(id),...row,_sid:item.sid});
      if(data.reset){item.term.reset();item.markers=[];}
      if(data.output)await new Promise(resolve=>{item.resolveWrite=resolve;item.term.write(data.output,()=>{item.resolveWrite=null;resolve();});});
      item.cursor=data.next_cursor; if(active===id)redraw();
    }catch(e){if(active===id)error(e.message);}finally{item.reading=false;}
  }
  // 多久问一次 (2026-11-07 复查: 以前不管面板开没开都 1.2 秒问一次终端列表, 11-07 一天 16,272 次, 服务端访问日志 14 MB 里约 5 MB 是它):
  // 面板开着且窗口在前台, 或者有命令在跑 (做完要及时提醒) 才 1.2 秒; 其余 15 秒一次 (她新开的终端、等你输入的提醒晚十几秒也够).
  // 面板一打开、窗口回到前台马上刷一次; 输出也只在看得见时读 (以前窗口最小化了、面板开着照样 0.18 秒读一次)
  const looking=()=>visible&&!document.hidden;
  const listDelay=()=>looking()||[...rows.values()].some(row=>row.state==='running')?1200:15000;
  async function outputLoop(){if(!alive)return;if(looking()&&active)await readOutput(active);if(alive)outputTimer=setTimeout(outputLoop,looking()?180:1200);}
  function announce(row){const previous=seenStates.get(row.id);const stamp=[row.state,row.prompt_seq,row.notice].join(':');seenStates.set(row.id,stamp);
    if(previous===undefined||stamp===previous||!(['completed','failed','waiting'].includes(row.state)||row.state==='closed'&&(row.exit_code!=null||row.notice))||visible&&row.id===active&&!document.hidden)return;
    const now=performance.now();if(now-(noticeTimes.get(row.id)||-4000)<3000)return;noticeTimes.set(row.id,now);
    const text=`${row.title}：${row.notice||stateLabel(row)}`;notify(text);
    let allowed=false;try{allowed=localStorage.getItem('wm.terminal.notifications')==='1';}catch{}
    if(allowed&&window.Notification?.permission==='granted'){const n=new Notification('绫音工作台 · 终端',{body:text,tag:'terminal-'+row.id});n.onclick=async()=>{n.close();if(!alive)return;window.focus();try{if(getSession()?.id!==row._sid&&openSession)await openSession(row._sid);await openPanel();sessionChanged();if(row._sid===sid)await select(row.id);}catch(e){error(e.message);}};}
  }
  async function refresh(){
    if(!alive)return;
    clearTimeout(listTimer);
    if(refreshing){refreshAgain=true;return;}
    refreshing=true;
    try{
    for(const owner of knownSessions){
      if(owner!==sid&&![...rows.values()].some(row=>row._sid===owner&&row.state!=='closed')){knownSessions.delete(owner);continue;}
      try{const epoch=epochs.get(owner)||0;const data=await request(owner);if(!alive)return;if(creating||epoch!==(epochs.get(owner)||0))continue;
        if(owner===sid&&data.supported===false){error(data.reason||'交互终端组件尚不可用');}
        for(const item of data.terminals||[]){const row={...item,_sid:owner};rows.set(row.id,row);announce(row);}
        const ids=new Set((data.terminals||[]).map(x=>x.id));
        for(const [id,row] of rows){if(row._sid!==owner||ids.has(id))continue;rows.delete(id);const item=instances.get(id);if(item){item.disposed=true;clearTimeout(item.inputTimer);item.resolveWrite?.();item.term.dispose();item.node.remove();instances.delete(id);}if(active===id){active='';selected.delete(owner);}}
        if(owner===sid&&!active){const own=(data.terminals||[]);const first=own.find(x=>x.id===selected.get(sid))||own.find(x=>x.state!=='closed')||own.at(-1);if(first)await select(first.id);}
      }catch(e){if(owner===sid&&visible)error(e.message);}
    }
    if(visible)redraw();
    }finally{refreshing=false;if(alive){const delay=refreshAgain?0:listDelay();refreshAgain=false;listTimer=setTimeout(refresh,delay);}}
  }
  async function create(){if(creating)return;creating=true;error('');container.querySelectorAll('[data-term-new]').forEach(b=>b.disabled=true);
    try{let session=getSession();if(!session)session=await ensureSession();if(!session?.id)throw Error('请先选择一个工作区');sid=session.id;const owner=sid;knownSessions.add(owner);const number=Math.max(0,...[...rows.values()].filter(row=>row._sid===owner).map(row=>{const match=/^PowerShell\s*(\d*)$/.exec(row.title||'');return match?Number(match[1]||1):0;}))+1;const data=await request(owner,'create',{cols:100,rows:28,title:'PowerShell'+number});epochs.set(owner,(epochs.get(owner)||0)+1);if(!alive)return;const row={...data.terminal,_sid:owner};rows.set(row.id,row);if(sid===owner)await select(row.id);}
    catch(e){error(e.message);}finally{creating=false;container.querySelectorAll('[data-term-new]').forEach(b=>b.disabled=false);}
  }
  async function copySelection(){try{const text=current()?.term.getSelection();if(text)await navigator.clipboard.writeText(text);else error('先选中需要复制的终端文本。');}catch(e){error(e.message);}}
  container.addEventListener('click',async event=>{
    const button=event.target.closest('button');if(!button)return;
    try{
      if(button.hasAttribute('data-term-new'))return create();
      if(button.dataset.termSelect)return select(button.dataset.termSelect);
      if(button.hasAttribute('data-term-copy'))return copySelection();
      if(button.hasAttribute('data-term-clear'))return current()?.term.clear();
      if(button.dataset.termFind){const q=find('.terminal-search input').value;current()?.search[button.dataset.termFind==='next'?'findNext':'findPrevious'](q);return;}
      if(button.hasAttribute('data-term-notifications')){if(!window.Notification)throw Error('当前环境未提供系统通知，仍会在工作台提醒。');if(await Notification.requestPermission()==='granted'){localStorage.setItem('wm.terminal.notifications','1');notify('已开启终端后台通知');}return;}
      if(!active)return;
      const action=button.hasAttribute('data-term-close')?'close':button.hasAttribute('data-term-interrupt')?'interrupt':button.hasAttribute('data-term-rename')?'rename':'';
      if(action){const id=active,owner=rows.get(id)?._sid||sid,title=find('[data-term-title]').value;const item=instances.get(id);item?.flushInput();await item?.queue;const data=await request(owner,action,{terminal_id:id,...(action==='rename'?{title}:{})});epochs.set(owner,(epochs.get(owner)||0)+1);rows.set(id,{...rows.get(id),...data.terminal,_sid:owner});if(alive){find('.terminal-options').open=false;redraw();if(active===id)current()?.term.focus();}}
    }catch(e){error(e.message);}
  });
  find('[data-term-theme]').addEventListener('change',event=>{theme=event.target.value;try{localStorage.setItem('wm.terminal.theme',theme);}catch{}for(const item of instances.values())applyTheme(item);});
  find('.terminal-search input').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();current()?.search[event.shiftKey?'findPrevious':'findNext'](event.target.value);}if(event.key==='Escape')current()?.term.focus();});
  function sessionChanged(){const next=getSession()?.id||'';if(next!==sid){sid=next;active=selected.get(sid)||'';if(active&&!rows.has(active))active='';}if(sid)knownSessions.add(sid);redraw();if(active)select(active);clearTimeout(listTimer);refresh();}
  document.addEventListener('visibilitychange',()=>{if(alive&&looking()){clearTimeout(listTimer);refresh();}});
  // 看板上"去看看" (2026-11-07): 面板随后打开, 选中这个终端; 别的会话的先记着 (换过去时 sessionChanged 按它选), 还没列出来的下一次刷新时选它
  async function show(id,owner){const own=owner||sid;if(!id||!own)return;selected.set(own,id);knownSessions.add(own);if(own!==sid)return;if(rows.has(id))await select(id);else{active='';clearTimeout(listTimer);refresh();}}
  outputLoop();
  return {setVisible(value){visible=value;container.hidden=!value;if(value){sessionChanged();resize();}else redraw();},sessionChanged,show,
    dispose(){alive=false;clearTimeout(outputTimer);clearTimeout(listTimer);clearTimeout(resizeTimer);observer.disconnect();for(const item of instances.values()){item.disposed=true;clearTimeout(item.inputTimer);item.resolveWrite?.();item.term.dispose();}}};
}
