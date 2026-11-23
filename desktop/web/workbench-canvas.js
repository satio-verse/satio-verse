import { initPlatform } from '/pet/workbench-platform.js';

export const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const E = escapeHTML;
const paths = {
  files:'<path d="M3 7h7l2 2h9v11H3zM3 7V4h7l2 3"/>',
  browser:'<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c5 5 5 13 0 18-5-5-5-13 0-18"/>',
  agents:'<circle cx="12" cy="5" r="3"/><circle cx="5" cy="19" r="3"/><circle cx="19" cy="19" r="3"/><path d="M12 8v4H5v4m7-4h7v4"/>',
  tools:'<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><path d="M17.5 14v7M14 17.5h7"/>',
  community:'<path d="m3 9 3-5h12l3 5M4 12v9h16v-9M9 21v-6h6v6M3 9a3 3 0 0 0 6 0 3 3 0 0 0 6 0 3 3 0 0 0 6 0"/>',
  data:'<path d="M4 5c0-4 16-4 16 0s-16 4-16 0v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/>',
  updates:'<path d="M20 7V2m0 5h-5M4 17v5m0-5h5M20 7a9 9 0 0 0-16 3m0 7a9 9 0 0 0 16-3"/>',
  copy:'<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M15 8V3H3v13h5"/>',
  good:'<path d="M8 10v11H3V10zm0 1 5-8c3-1 2 5 1 7h5c2 0 2 2 1 4l-2 7H8"/>',
  bad:'<path d="M8 14V3H3v11zm0-1 5 8c3 1 2-5 1-7h5c2 0 2-2 1-4l-2-7H8"/>',
  fork:'<circle cx="6" cy="4" r="2"/><circle cx="6" cy="20" r="2"/><circle cx="18" cy="4" r="2"/><path d="M6 6v12m0-6c9 0 12-1 12-6"/>',
  document:'<path d="M5 3h9l5 5v13H5zM14 3v5h5M8 12h8M8 16h8"/>',
  terminal:'<rect x="3" y="4" width="18" height="16" rx="2"/><path d="m7 9 3 3-3 3m6 0h4"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>',
  expand:'<path d="M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5"/>',
  // 第六轮: 工具页的列表图标 (按能力类别分开, 不再一律"四格")
  plus:'<path d="M12 5v14M5 12h14"/>',
  refresh:'<path d="M20 11a8 8 0 1 0-2.4 5.7M20 4v7h-7"/>',
  search:'<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
  chev:'<path d="m9 6 6 6-6 6"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  person:'<circle cx="12" cy="8" r="4"/><path d="M4 21c1-4 4.5-6 8-6s7 2 8 6"/>',
  rules:'<path d="M6 3h12v18H6zM9 8h6M9 12h6M9 16h4"/>',
  bulb:'<path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-3.6 10.8c.8.6 1.1 1.3 1.1 2.2h5c0-.9.3-1.6 1.1-2.2A6 6 0 0 0 12 3z"/>',
  todo:'<path d="m4 7 2 2 4-4M4 17l2 2 4-4M13 7h7M13 17h7"/>',
  box:'<path d="m3 7 9-4 9 4v10l-9 4-9-4zM3 7l9 4 9-4M12 11v10"/>',
  heart:'<path d="M12 20s-8-4.6-8-10.2A4.3 4.3 0 0 1 12 7a4.3 4.3 0 0 1 8 2.8C20 15.4 12 20 12 20z"/>',
  shield:'<path d="M12 3 4 6v6c0 5 3.5 8 8 9 4.5-1 8-4 8-9V6z"/>',
  chip:'<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4m6-4v4M9 18v4m6-4v4M2 9h4m-4 6h4m12-6h4m-4 6h4"/>',
  plug:'<path d="M9 3v5m6-5v5M6 8h12v3a6 6 0 0 1-12 0zM12 17v4"/>',
  edit:'<path d="M4 20h4L19 9l-4-4L4 16zM14 6l4 4"/>',
  trash:'<path d="M4 7h16M10 11v6m4-6v6M6 7l1 13h10l1-13M9 7V4h6v3"/>',
  book:'<path d="M5 4h11a3 3 0 0 1 3 3v13H8a3 3 0 0 1-3-3z"/><path d="M5 17a3 3 0 0 1 3-3h11"/>',
};
// 能力 id -> 图标 (wm/agent/capabilities.py 里登记的那些)
const CAP_ICON={persona:'person','agent-instructions':'rules','time-context':'clock','tool-fs':'files','tool-pwsh':'terminal','tool-web':'browser','tool-skill':'plug',present:'box',
  'tool-subagent':'agents','tool-todo':'todo','tool-plugin-manager':'tools',context:'document',life:'heart',resident:'clock','other-tools':'tools',sessions:'data',authorization:'shield',models:'chip',platform:'community'};
export const canvasIcon = name => `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.document}</svg>`;
const labels = {start:'工作空间',files:'工作区文件',terminal:'终端',browser:'浏览器',agents:'Agent 与嵌套任务',tools:'工具与上下文',community:'插件社区',data:'数据贡献',updates:'软件更新',life:'生活与成长'};
export function messageActions(event) {
  const id = event.message_id || '';
  const stamp = event.t || event.time || event.created;
  const date = stamp ? new Date(Number(stamp) * (Number(stamp) < 1e12 ? 1000 : 1)) : null;
  const valid = date && !Number.isNaN(date.getTime());
  const rawVote = typeof event.feedback === 'object' ? event.feedback?.value : event.feedback;
  const vote = rawVote==='issue'?'bad':rawVote;
  const button = (action,title) => `<button type="button" data-message-action="${action}" data-message-id="${E(id)}" title="${title}" aria-label="${title}" ${action==='good'||action==='bad'?`aria-pressed="${vote===action}"`:''}>${canvasIcon(action)}</button>`;
  return `<div class="message-actions" data-message-id="${E(id)}">${button('copy','复制消息')}${event.type==='assistant'?button('good','好的回答')+button('bad','有问题的回答'):''}${id?button('fork','从这里分支到新聊天'):''}${valid?`<time datetime="${date.toISOString()}" title="${E(date.toLocaleString())}">${E(date.toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}))}</time>`:'<span class="message-time-unknown">历史时间未记录</span>'}</div>`;
}

// Read-only Markdown: source HTML remains text and links only accept HTTP(S).
// cite: 对话里用 (第六轮, 学 ChatGPT 的来源胶囊) —— 正文里光秃秃的网址换成站点名小胶囊, 悬停看完整网址; 文件预览不换
const CITE_RE = /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<>"'`，。；：、！？）」』】]+)/g;
const citeHost = url => { try { return new URL(url).hostname.replace(/^www\./, ''); } catch { return url; } };
// 资料本的出处编号 [S3-17]、【S1-2】、[S3-17、S2-4] —— 规则照 wm/agent/notebook.py 的 cited_keys(): 方括号里找 S数字-数字
const NB_BRACKET = /[\[［【]([^\[\]［］【】\n]{1,160})[\]］】]/g;
const NB_KEYS = /S(\d{1,4})-(\d{1,5})/g;
const NB_SPLIT = /(S\d{1,4}-\d{1,5})/;
const nbKey = raw => raw.replace(/S0*(\d+)-0*(\d+)/, 'S$1-$2');  // S03-007 -> S3-7 (同 cited_keys 里的 int())
const nbPill = raw => `<button type="button" class="wb-nb-cite" data-nb-key="${nbKey(raw)}" title="看出处 ${nbKey(raw)} 的原文">${nbKey(raw)}</button>`;
export function citedKeys(text) {
  const out = [];
  for (const m of String(text || '').matchAll(NB_BRACKET)) for (const k of m[1].matchAll(NB_KEYS)) { const key = `S${+k[1]}-${+k[2]}`; if (!out.includes(key)) out.push(key); }
  return out;
}
export function renderMarkdown(text, { cite = false } = {}) {
  const words = raw => E(raw).replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  // cite 时方括号里的出处编号换成可点的小卡片: 只有编号的整个方括号换掉, 夹着别的字的只换编号
  const plain = raw => {
    if (!cite) return words(raw);
    let out = '', last = 0;
    for (const m of raw.matchAll(NB_BRACKET)) {
      if (!NB_SPLIT.test(m[1])) continue;
      const bare = !m[1].replace(NB_KEYS, '').replace(/[\s、,，;；和及与]+/g, '');
      out += words(raw.slice(last, m.index)) + (bare ? `<span class="wb-nb-cites">${[...m[1].matchAll(NB_KEYS)].map(k => nbPill(k[0])).join('')}</span>`
        : E(m[0][0]) + m[1].split(NB_SPLIT).map((part, i) => i % 2 ? nbPill(part) : words(part)).join('') + E(m[0].slice(-1)));
      last = m.index + m[0].length;
    }
    return out + words(raw.slice(last));
  };
  const linked = raw => {
    let out = '', last = 0;
    for (const m of raw.matchAll(CITE_RE)) {
      out += plain(raw.slice(last, m.index));
      if (m[1]) out += `<a href="${E(m[2])}" target="_blank" rel="noopener noreferrer">${plain(m[1])}</a>`;
      else if (!cite) out += E(m[0]);
      else {
        const trail = /[.,;:!?)\]]+$/.exec(m[3]) ?.[0] || '', url = trail ? m[3].slice(0, -trail.length) : m[3];
        out += `<a class="wb-cite" href="${E(url)}" target="_blank" rel="noopener noreferrer" title="${E(url)}">${E(citeHost(url))}</a>${E(trail)}`;
      }
      last = m.index + m[0].length;
    }
    return out + plain(raw.slice(last));
  };
  const inline = raw => String(raw).split(/(`[^`]+`)/g).map((part, i) => i % 2 ? `<code>${E(part.slice(1, -1))}</code>` : linked(part)).join('');
  const lines=String(text||'').replace(/\r\n?/g,'\n').split('\n');let code=false,buffer=[],result=[],list='';
  const flush=()=>{if(buffer.length){result.push(`<p>${buffer.map(inline).join('<br>')}</p>`);buffer=[];}};
  const endList=()=>{if(list){result.push(`</${list}>`);list='';}};
  const cells=line=>line.trim().replace(/^\||\|$/g,'').split('|').map(cell=>cell.trim());
  for(let i=0;i<lines.length;i++){
    const line=lines[i];
    if(/^\s*```/.test(line)){flush();endList();code=!code;result.push(code?'<pre><code>':'</code></pre>');continue;}
    if(code){result.push(E(line)+'\n');continue;}
    const h=/^(#{1,6})\s+(.+)$/.exec(line),li=/^\s*(?:([-*+])|([0-9]+)[.)])\s+(.+)$/.exec(line);
    if(li){flush();const next=li[1]?'ul':'ol';if(list!==next){endList();result.push(`<${next}>`);list=next;}result.push(`<li>${inline(li[3])}</li>`);continue;}
    endList();
    if(line.includes('|')&&i+1<lines.length&&cells(lines[i+1]).every(cell=>/^:?-{3,}:?$/.test(cell))){
      flush();const headers=cells(line);result.push('<div class="markdown-table-scroll"><table><thead><tr>'+headers.map(cell=>`<th>${inline(cell)}</th>`).join('')+'</tr></thead><tbody>');i++;
      while(i+1<lines.length&&lines[i+1].trim()&&lines[i+1].includes('|')){const row=cells(lines[++i]);result.push('<tr>'+headers.map((_,j)=>`<td>${inline(row[j]||'')}</td>`).join('')+'</tr>');}
      result.push('</tbody></table></div>');
    }else if(h){flush();result.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`);}
    else if(/^>\s?/.test(line)){flush();result.push(`<blockquote>${inline(line.replace(/^>\s?/,''))}</blockquote>`);}
    else if(/^\s*(?:---+|\*\*\*+)\s*$/.test(line)){flush();result.push('<hr>');}
    else if(!line.trim())flush();else buffer.push(line);
  }
  flush();endList();if(code)result.push('</code></pre>');return result.join('');
}

export async function canvasRequest(path,body) {
  const response = await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
  const data=await response.json().catch(()=>({}));if(!response.ok)throw new Error(data.error||`请求失败 (${response.status})`);return data;
}

// 出处卡片: 回答 (或笔记、检索结果) 里的 [S3-17] 点了, 弹一张卡片写那一块的原文、位置 (第 12 页 / 段落 3–8 / 「标题 › 小标题」第 40–58 行)
// 和前后各一块; PDF 给"打开原文" (面板里开 …&file=1#page=页码), 网页给"打开网页". 接口: GET /api/agent/notebooks?id=&key=&around=1
let citePop = null;
export function closeCitation() { citePop?.remove(); citePop = null; }
export async function showCitation(anchor, { notebookId, key, openDoc, openWeb, openNotebook, openReading } = {}) {
  closeCitation();
  const pop = citePop = document.createElement('div');
  pop.className = 'nb-pop';
  pop.setAttribute('role', 'dialog');
  pop.setAttribute('aria-label', `出处 ${key}`);
  const place = () => {
    const r = anchor.getBoundingClientRect(), w = Math.min(460, innerWidth - 16);
    pop.style.width = `${w}px`;
    const h = pop.offsetHeight, below = r.bottom + 10 + h < innerHeight || r.top < h + 16;
    pop.style.top = `${Math.max(8, below ? r.bottom + 6 : r.top - h - 6)}px`;
    pop.style.left = `${Math.max(8, Math.min(innerWidth - w - 8, r.left - 16))}px`;
  };
  const paint = (head, inner = '', foot = '') => {
    pop.innerHTML = `<header><span class="nb-key">${E(key)}</span><div class="nb-src">${head}</div><button type="button" class="wb-btn icon small" data-nb-pop-close aria-label="关闭">${canvasIcon('close')}</button></header>${inner}${foot ? `<footer>${foot}</footer>` : ''}`;
    place();
  };
  pop.addEventListener('click', event => {
    const b = event.target.closest('button');
    if (!b) return;
    if (b.hasAttribute('data-nb-pop-close')) closeCitation();
    else if (b.dataset.nbPopDoc) { closeCitation(); openDoc?.(b.dataset.nbPopDoc, b.dataset.title); }
    else if (b.dataset.nbPopWeb) { closeCitation(); openWeb?.(b.dataset.nbPopWeb); }
    else if (b.dataset.nbPopRead) { closeCitation(); openReading?.(notebookId, b.dataset.nbPopRead, b.dataset.key, b.dataset.title); }
    else if (b.hasAttribute('data-nb-pop-books')) { closeCitation(); openNotebook?.(notebookId || ''); }
    else if (b.hasAttribute('data-nb-pop-copy')) navigator.clipboard?.writeText(pop.dataset.text || '').then(() => { b.textContent = '复制好了'; }).catch(() => {});
  });
  document.body.append(pop);
  paint('<strong>正在读原文…</strong>');
  if (!notebookId) {
    paint('<strong>这个会话没挂资料本</strong>', '<p class="nb-pop-note">出处编号要对着挂着的那本资料本看；挂上资料本再点。</p>',
      openNotebook ? '<button type="button" class="wb-btn small" data-nb-pop-books>打开资料本</button>' : '');
    return;
  }
  try {
    const data = await canvasRequest('/api/agent/notebooks?' + new URLSearchParams({ id: notebookId, key, around: '1' }));
    if (citePop !== pop) return;
    const src = data.source || {}, blocks = data.blocks || [], me = blocks.find(b => b.this) || blocks[0] || {};
    const page = me.loc?.page, pdf = src.kind === 'file' && String(src.format || '').toLowerCase() === 'pdf';
    const doc = `/api/agent/notebooks?${new URLSearchParams({ id: notebookId, source: String(src.no), file: '1' })}${page ? `#page=${page}` : ''}`;
    pop.dataset.text = me.text || '';
    paint(`<strong>${E(src.name || '资料')}</strong><small>${E(me.label || '')}</small>`,
      `<div class="nb-blocks">${blocks.map(b => `<section class="nb-block ${b.this ? 'this' : ''}"><small>${E(b.key)}${b.label ? ` · ${E(b.label)}` : ''}</small><p>${E(b.text)}</p></section>`).join('')}</div>`,
      (pdf && openDoc ? `<button type="button" class="wb-btn small primary" data-nb-pop-doc="${E(doc)}" data-title="${E(src.name || '原文')}${page ? ` · 第 ${page} 页` : ''}">打开原文${page ? ` · 第 ${page} 页` : ''}</button>` : '')
      + (openReading && src.no ? `<button type="button" class="wb-btn small" data-nb-pop-read="${E(String(src.no))}" data-key="${E(key)}" data-title="${E(src.name || '资料')}">在阅读面板里看</button>` : '')
      + (src.kind === 'web' && src.url && openWeb ? `<button type="button" class="wb-btn small" data-nb-pop-web="${E(src.url)}">打开网页</button>` : '')
      + '<button type="button" class="wb-btn small text" data-nb-pop-copy>复制这段</button>'
      + (openNotebook ? '<button type="button" class="wb-btn small text" data-nb-pop-books>在资料本里看</button>' : '')
      + `<span class="grow"></span><small class="nb-pop-meta">${E(data.notebook?.title || '')} · 第 ${E(String(me.key || key).split('-')[1] || '?')} / ${E(String(data.total ?? '?'))} 块</small>`);
  } catch (error) {
    if (citePop !== pop) return;
    paint('<strong>没找到这处原文</strong>', `<p class="nb-pop-note">${E(/\(404\)/.test(error.message) ? '资料本要等服务端重启后才有（重启前会先问你）。' : error.message)}</p>`);
  }
}
document.addEventListener('pointerdown', event => { if (citePop && !event.target.closest('.nb-pop, .wb-nb-cite, [data-nb-cite-open]')) closeCitation(); });
document.addEventListener('keydown', event => { if (citePop && event.key === 'Escape') { event.preventDefault(); closeCitation(); } }, true);

export function initCanvas({container,getSession,getState,ensureSession,api,notify,openSession,openTerminal,reload,openStudio,openSettings,setDraft,setPreset,changeSetting,renderChat,openDoc,openWeb,openReading}) {
  // 2026-11-01 照 Codex 改版: 协作、能力这几页装在右侧面板的一个标签里 (workbench-panel.js); 停靠框、宽度、终端、文件、快捷键都归面板
  const dock=container;dock.classList.add('canvas-pages');
  dock.innerHTML=`<nav class="canvas-tabs" id="canvasTabs"></nav><div id="canvasBody" class="canvas-body"></div><p id="canvasNotice" class="canvas-notice" role="status"></p>`;
  const body=dock.querySelector('#canvasBody'), tabs=dock.querySelector('#canvasTabs'), notice=dock.querySelector('#canvasNotice');
  const platform=initPlatform({container:body,getSession,notify});
  let page='',tab='',generation=0,poll=0,activeSession='',actionLock=false,presetCatalog=[],historyQuery='',historyGeneration=0,treeSequence=0,capabilityPreset='';
  const RESEARCH_TOOLS = new Set(['read_file','list_dir','glob','grep','web_search','web_fetch','extensions_list','skill_read','context_inspect','context_read','artifact_list','artifact_read','delegate_status','worktree_status','growth_status','history_search','history_read']);
  const runtime=(view,extra={})=>canvasRequest('/api/agent/runtime?'+new URLSearchParams({view,id:getSession()?.id||'',...extra}));
  const command=(action,extra={})=>canvasRequest('/api/agent/runtime',{action,id:getSession()?.id||'',...extra});
  // 第六轮 (学 Linear): 每页一个页头 (标题 + 一句说明 + 右边的按钮)、列表一行一项、按钮只有 主 / 次 / 文字 / 图标 四种样子 (.wb-btn, 样式在 workbench-shell.css)
  const empty=(title,description,action='')=>`<div class="cv-empty">${canvasIcon(page==='tools'?(tab==='experiences'?'bulb':'tools'):page)}<h3>${E(title)}</h3><p>${E(description)}</p>${action}</div>`;
  const head=(title,sub='',actions='')=>`<header class="cv-head"><div class="cv-title"><h2>${E(title)}</h2>${sub?`<p>${sub}</p>`:''}</div>${actions?`<div class="cv-head-actions">${actions}</div>`:''}</header>`;
  const btn=(attrs,label,kind='')=>`<button type="button" class="wb-btn ${kind}" ${attrs}>${label}</button>`;
  const iconBtn=(attrs,glyph,label)=>`<button type="button" class="wb-btn icon" ${attrs} title="${E(label)}" aria-label="${E(label)}">${canvasIcon(glyph)}</button>`;
  const chev=`<span class="cv-chev">${canvasIcon('chev')}</span>`;
  const wan=n=>{n=Number(n)||0;return n>=1e4?`${(n/1e4).toFixed(1).replace(/\.0$/,'')} 万`:n.toLocaleString('zh-CN');};
  let capFilter='all';
  const announce=text=>{notice.textContent=text;};
  function setTabs(items){tabs.innerHTML=items.map(([key,label])=>`<button type="button" data-canvas-tab="${key}" aria-pressed="${tab===key}">${E(label)}</button>`).join('');tabs.hidden=!items.length;}
  function viewChanged(){dispatchEvent(new CustomEvent('wm-canvas-view',{detail:{page,tab,visible:!dock.hidden}}));}
  function close(){generation++;platform.close();body._lifeRender=null;body._residentCleanup?.();clearTimeout(poll);page='';viewChanged();}
  async function open(next,sub=''){
    if(next==='notebooks'&&!sub)sub=nbLast;  // 资料本: 换到别的标签再回来, 还停在刚才看的那本
    const same=page===next&&tab===(sub||({agents:'tasks',tools:'builtins'}[next]||''));
    page=next;dock.dataset.page=page;tab=sub||({agents:'tasks',tools:'builtins'}[page]||'');
    if(!same)announce('');await render();
  }
  async function render(){
    const version=++generation;clearTimeout(poll);platform.close();body._lifeRender=null;body._residentCleanup?.();
    const sid=getSession()?.id||'';if(activeSession!==sid){capabilityPreset='';activeSession=sid;}
    body.hidden=false;viewChanged();
    // 第六轮: 每个入口一页 (左栏、新标签页、"更多工具"各开各的标签), 页里不再套一排子标签 (以前自动化和协作两个入口进去是同一组)
    setTabs([]);
    if(['community','data','updates'].includes(page)||page==='tools'&&['data','updates'].includes(tab)){await platform.open(['data','updates'].includes(tab)?tab:page);return;}
    if(page==='agents'&&tab==='resident'){const module=await import('/pet/workbench-resident.js');if(version===generation)await module.renderResident(body,{getSession,openSession});return;}
    if(page==='agents'&&tab==='life'){const module=await import('/pet/panel-life.js');if(version===generation)await module.renderLife(body);return;}
    if(page==='tools'&&tab==='extensions'){
      body.innerHTML=head('我的插件','技能、MCP 工具和界面都能在聊天里做出来，先预览，再启用。')+empty('扩展绫音的能力','让她在对话里帮你做；做好的先给你看，再决定用不用。',`<div class="cv-actions">${btn('data-studio="extensions"','管理创作与版本','primary')}${btn('data-settings="plugins"','已安装插件')}${btn('data-settings="components"','下载组件')}</div>`);return;
    }
    if(page==='agents'&&tab==='presets'){await renderPresets(version);return;}
    if(page==='tools'&&tab==='experiences'){await renderExperiences(version);return;}
    if(page==='notebooks'){await renderNotebooks(version);return;}
    if(!sid&&!(page==='tools'&&tab==='builtins')){body.innerHTML=empty('先选择一个会话','这一页跟着当前会话和工作区走。');return;}
    body.innerHTML='<p class="cv-loading">正在读取…</p>';
    try{
      if(page==='agents'){
        const data=await runtime('tree');if(version!==generation)return;
        const rows=flattenTree(data.tree||data.session_tree||data.children||[]);
        // 列表在前 (学 Linear): 新建的表单收在"新建子任务"后面, 点了才出来
        body.innerHTML=head('协作任务',`子任务各自记录过程，结果汇回主会话${rows.length?` · ${rows.length} 个`:''}`,iconBtn('data-canvas-refresh','refresh','刷新')+btn('data-new-task',`${canvasIcon('plus')}新建子任务`,'primary'))
          +`<form id="canvasDelegate" class="cv-pop" hidden><strong class="cv-pop-title">新建子任务</strong><label for="canvasGoals">每行一个子任务</label><textarea id="canvasGoals" rows="3" placeholder="检查实现中的问题&#10;核对测试覆盖&#10;整理使用说明" required></textarea><label class="cv-inline">工作方式<select name="workspace_mode"><option value="read">读取同一工作区</option><option value="shared">协作修改同一工作区</option><option value="worktree">独立 Git 工作树</option></select></label><p class="cv-muted" id="canvasIsolationNote">上下文相互独立。要同时改同一份代码，优先用工作树。</p><div class="cv-pop-row"><span class="grow"></span><button type="button" class="wb-btn text" data-pop-cancel>取消</button><button class="wb-btn primary" type="submit">创建并执行</button></div></form>`
          +`<div class="cv-toolbar"><div class="cv-seg" role="group" aria-label="怎么看">${[['board','看板'],['list','列表']].map(([k,l])=>`<button type="button" data-task-view="${k}" aria-pressed="${taskView===k}">${l}</button>`).join('')}</div><div class="cv-seg" role="group" aria-label="看哪些">${[['session','这个会话的子任务'],['all','她手上所有的事']].map(([k,l])=>`<button type="button" data-task-scope="${k}" aria-pressed="${taskScope===k}">${l}</button>`).join('')}</div></div>`
          +`<div id="cvTeam"></div><div class="canvas-task-list cv-list"><p class="cv-loading">正在读取…</p></div><div id="cvWorktree"></div>`
          +`<div class="cv-foot">${btn('data-studio="tasks"','查看任务详情与停止操作 ↗','text')}</div>`
          +`<p class="cv-foot cv-muted">同时跑几个、最多套几层，在${btn('data-start-page="capabilities"','能力与插件 · 内置能力','text small')}里改。</p>`;
        body._taskRows=rows;await paintTasks(rows);
      }else if(page==='tools'&&tab==='builtins'){
        await renderCapabilities(version);
      }else if(page==='tools'&&tab==='context'){
        const data=await runtime('context');if(version!==generation)return;
        const info=data.context||data;
        body.innerHTML=head('上下文与记录','压缩只影响发给模型的内容，原始记录都留着，随时能查。',btn('data-compact','压缩当前上下文'))+contextHtml(info)
          +`<h3 class="cv-sub">查原始记录</h3><form id="canvasHistorySearch" class="cv-search-form"><label class="cv-search">${canvasIcon('search')}<input name="query" type="search" placeholder="搜索历史消息与工具结果" required aria-label="搜索历史消息与工具结果"></label><button type="submit" class="wb-btn">搜索</button></form><div id="canvasHistoryResults"></div>`;
      }
    }catch(error){if(version===generation)body.innerHTML=empty('暂时无法读取',error.message);}
  }
  // ---- 能力与插件 (第 5 块, 学 DSH 的插件页): 内置能力 (真开关) → 已安装 (扩展能启停; 技能、MCP 只列, 在设置里管) → 全部能力 (原来的列表) ----
  // 只放真能改到运行时的: 子智能体走 team_config (服务端存盘); 联网搜索 = 这个会话的设置 + 新会话默认 (工作台偏好, 新建会话时带上);
  // 终端的两个数写死在程序里 (wm/agent/tools.py), 照实写成说明, 不做假开关
  const ADD_MENU=[['data-create-plugin','在聊天里让她做','说想要的技能、工具或界面；做好先给你预览，再决定启用'],
    ['data-settings="mcp"','添加 MCP 服务器','在设置里填命令，或导入 JSON 配置'],['data-settings="skills"','新建技能','在设置里写一份技能说明'],
    ['data-settings="ext"','从本地装工具与内容包','在设置里选 .wmext 文件'],['data-settings="marketplace"','插件市场','在设置里装官方和社区的包'],
    ['data-start-page="community"','插件社区','看别人分享的插件']];
  const addMenu=()=>`<details class="cv-add"><summary class="wb-btn primary">${canvasIcon('plus')}添加插件</summary><div class="cv-add-menu" role="menu">${ADD_MENU.map(([attrs,label,sub])=>`<button type="button" role="menuitem" ${attrs}><strong>${label}</strong><small>${sub}</small></button>`).join('')}</div></details>`;
  const webDefault=()=>{try{return localStorage.getItem('wm.workbench.webDefault')||'follow';}catch{return 'follow';}};
  function builtinHtml(config,session){
    const card=(icon,title,sub,inner)=>`<section class="cv-bcard"><header><span class="cv-ic">${canvasIcon(icon)}</span><div><strong>${title}</strong><small>${sub}</small></div></header>${inner}</section>`;
    const web=webDefault(),on=session?.web!==false;
    return `<h3 class="cv-sub">内置能力</h3><div class="cv-bcards">`
      +card('agents','子智能体','复杂的事拆开并行做，各自记录过程',`<form id="canvasTeamConfig"><label class="cv-prop"><span>同时跑</span><input name="max_parallel" type="number" min="1" max="32" value="${Number(config.max_parallel)||3}" required></label><label class="cv-prop"><span>最多套几层</span><input name="max_depth" type="number" min="1" max="8" value="${Number(config.max_depth)||1}" required></label><div class="cv-bfoot"><small>满了自动排队；有子任务在跑时改不了</small><button type="submit" class="wb-btn small">保存</button></div></form>`)
      +card('browser','联网搜索','上网搜索、读网页（只读公网）',`<div class="cv-prop"><span>这个会话</span>${session?.id?`<button type="button" class="studio-switch" role="switch" aria-checked="${on}" aria-label="这个会话联网搜索" data-web-now><span></span></button>`:'<small>先开一个会话</small>'}</div><label class="cv-prop"><span>新会话</span><select id="canvasWebDefault">${[['follow','跟着当前会话'],['on','默认开'],['off','默认关']].map(([key,label])=>`<option value="${key}" ${web===key?'selected':''}>${label}</option>`).join('')}</select></label>`)
      +card('terminal','终端','她跑的命令，和你自己开的终端',`<p class="cv-bnote">她跑的每条命令默认等 60 秒，按需要最多 600 秒，到时间就停掉；交给她看的输出最多 2 万字（留开头和结尾）。这两个数写在程序里，这里改不了；要不要每次问你，看会话权限。</p><div class="cv-bfoot"><span></span>${btn('data-start-page="terminal"','打开终端','small')}</div>`)
      +'</div>';
  }
  // 已安装: 扩展包能启停 (POST /api/agent/studio extension_enable, 和"扩展与版本"同一个接口; 版本回退在那一页), 技能和 MCP 工具服务只列 (在设置里管)
  async function installedHtml(session){
    const head=`<div class="cv-subhead"><h3 class="cv-sub">已安装</h3>${btn('data-studio="extensions"','版本与回退','text small')}${btn('data-settings="plugins"','在设置里管理','text small')}</div>`;
    if(!session?.id)return head+'<p class="cv-muted">先开一个会话再管扩展（启停要记在哪个会话名下）。</p>';
    const ext=(await canvasRequest(`/api/agent/studio?id=${encodeURIComponent(session.id)}`)).extensions||{};
    const packs=ext.extensions||[],skills=ext.skills||[],servers=ext.servers||[],child=!!session.parent_id;
    if(!packs.length&&!skills.length&&!servers.length)return head+`<div class="cv-list">${empty('还没装插件','可以在聊天里让她做一个，或者从右上角"添加插件"装。')}</div>`;
    const group=(title,rows)=>rows.length?`<p class="cv-group-title">${title}<em>${rows.length}</em></p><div class="cv-list">${rows.join('')}</div>`:'';
    const pack=p=>`<div class="cv-row cv-pack" data-pack="${E(p.id)}"><span class="cv-ic">${canvasIcon('box')}</span><strong>${E(p.name||p.id)}</strong><span class="cv-desc" title="${E(p.description||'')}">${E(p.description||'')}</span>${p.has_skill?'<span class="cv-tag">技能</span>':''}${p.has_mcp?'<span class="cv-tag">MCP</span>':''}${p.has_ui?'<span class="cv-tag">界面</span>':''}${p.error?'<span class="cv-tag bad">校验没过</span>':''}<span class="cv-meta">${p.enabled?'用着 ':'草稿 '}${E(String(p.active_revision||p.latest_revision||'').slice(0,6))}</span><button type="button" class="studio-switch" role="switch" aria-checked="${!!p.enabled}" aria-label="${p.enabled?'停用':'启用'} ${E(p.name||p.id)}" data-ext-toggle="${E(p.id)}" ${child||p.error?'disabled':''}><span></span></button></div>`;
    const skill=s=>`<div class="cv-row"><span class="cv-ic">${canvasIcon('plug')}</span><strong>${E(s.name)}</strong><span class="cv-desc" title="${E(s.description||'')}">${E(s.description||'')}</span><span class="cv-tag">${s.source==='creator'?'扩展里的':'设置里的'}</span></div>`;
    const server=s=>`<div class="cv-row"><span class="cv-ic">${canvasIcon('tools')}</span><strong>${E(s.name||s.id)}</strong><span class="cv-desc">${E(s.transport||'stdio')}</span>${s.has_credentials?'<span class="cv-tag">带凭据</span>':''}<span class="cv-tag">${s.source==='desktop'?'设置里的':'扩展里的'}</span></div>`;
    return head+(child?'<p class="cv-muted">这是子任务的会话：扩展的启停回主会话里改。</p>':'')+group('扩展',packs.map(pack))+group('技能',skills.map(skill))+group('MCP 工具服务',servers.map(server));
  }
  // 插件宿主 (第 6 块): 装了哪个、给了什么权限档、有哪几个动作、最近几次调用成没成。
  // 服务端开关关着时不下发 data.plugins → 这里返回空串, 页面上不会多出任何元素
  function pluginHostHtml(data){
    const host=data.plugins;
    if(!host||host.ok!==true||!Array.isArray(host.plugins)||!host.plugins.length)return '';
    const TIER={text:'只读档',panel:'能动手',code:'能跑代码'};
    const recent=p=>{const r=p.recent||{},last=Array.isArray(r.last)?r.last.slice(-3):[];
      if(!Number(r.calls))return '还没调用过';
      const marks=last.map(x=>x.ok?'✓':`✗${x.error_code?` ${x.error_code}`:''}`).join(' ');
      return `${Number(r.calls)} 次调用 · ${Number(r.ok)||0} 成 ${Number(r.failed)||0} 败${marks?` · 最近 ${marks}`:''}`;};
    const actions=p=>{const rows=Array.isArray(p.actions)?p.actions:[];
      return rows.length?`动作：${rows.map(a=>E(a.name)+(a.kind==='act'?'(动手)':'(查询)')).join('、')}`:'没有声明动作';};
    return `<div class="cv-subhead"><h3 class="cv-sub">插件宿主</h3><span class="cv-muted">装了什么、给了什么权限，也记着最近几次调用成没成</span></div><div class="cv-list">`
      +host.plugins.map(p=>`<div class="cv-row"><span class="cv-ic">${canvasIcon('box')}</span><strong>${E(p.name||p.id)}</strong><span class="cv-desc">${E(p.id)} · ${E(p.version||'版本没标')} · ${E(TIER[p.granted_tier]||p.granted_tier||'未提权')} · ${E(actions(p))}</span>${p.enabled?'<span class="cv-state ready"><i></i>用着</span>':'<span class="cv-state inactive"><i></i>停着</span>'}<span class="cv-meta">${E(recent(p))}</span></div>`).join('')
      +`</div>${host.verify?'<p class="cv-muted">宿主核对开关也开着：这些调用里，宿主自己也去世界核对过对得上、对不上还是看不懂。</p>':''}`;
  }
  // Agent 预设 (第 5 块): 一张卡一种工作方式 —— 说明、怎么用、"用这个"; 最后一张"让她帮我设计一个". 切换照旧走 setPreset (会话设置)
  const PRESET_ICON={standard:'todo',plan:'rules',creator:'tools',research:'search','security-review':'shield'};
  const PRESET_HOW={standard:'直接说要做的事；改文件、跑命令照会话权限来。',plan:'先说目标；她只调查、写计划，你说"开始"才动手。',
    creator:'说想要的技能、工具或界面；做好先给你预览，再决定启用。',research:'说要查的问题；她拆开并行查，结论都带出处。',
    'security-review':'指给她要查的代码、依赖或样本；她复现问题，给修复和回归证据。'};
  function presetCard(item,current){
    const now=item.id===current;
    return `<article class="cv-pcard ${now?'on':''}" data-preset-card="${E(item.id)}"><header><span class="cv-ic">${canvasIcon(PRESET_ICON[item.id]||'person')}</span><strong>${E(item.name||item.label||item.id)}</strong>${now?'<span class="cv-state ready"><i></i>正在用</span>':`<span class="cv-tag">${item.builtin?'内置':'自定义'}</span>`}</header>`
      +`<p>${E(item.description||'')}</p><p class="cv-pcard-how"><b>怎么用</b>${E(PRESET_HOW[item.id]||'照你写的工作指令做事。')}</p>`
      +`<footer>${now?'<span class="cv-muted">这个会话正在用</span>':btn(`data-preset-use="${E(item.id)}"`,'用这个','small primary')}<span class="grow"></span>${!item.builtin?iconBtn(`data-preset-edit="${E(item.id)}"`,'edit','编辑')+iconBtn(`data-preset-delete="${E(item.id)}"`,'trash','删除'):''}</footer></article>`;
  }
  const designCard=()=>`<article class="cv-pcard design"><header><span class="cv-ic">${canvasIcon('bulb')}</span><strong>让她帮我设计一个</strong></header><p>说说你想让她怎么干活：她先和你确认用途、工作指令和工具范围，再建成新的预设。</p><footer>${btn('data-create-preset','在聊天里开始','small')}${btn('data-new-preset','自己填','small text')}</footer></article>`;
  async function renderCapabilities(version){
    const session=getSession();
    const [data,tree,installed]=await Promise.all([runtime('capabilities',capabilityPreset?{preset:capabilityPreset}:{}),runtime('tree').catch(()=>({})),
      installedHtml(session).catch(error=>`<h3 class="cv-sub">已安装</h3><p class="cv-muted">暂时读不到：${E(error.message)}</p>`)]);
    if(version!==generation)return;
    const status={ready:'已就绪',partial:'部分可用',inactive:'没启用',missing:'没接上'};
    const groups=data.groups||[];
    const counts={all:groups.length,ready:groups.filter(g=>g.status==='ready').length};counts.other=counts.all-counts.ready;
    // 一行一项 (36px): 图标按类别分、名字、一行灰字说明、右边状态点; 点开在原地看详情和它带的工具
    const itemHtml=item=>`<details class="cv-cap" data-capability-search="${E([item.name,item.id,item.description,item.detail,...item.tools.map(t=>t.name+' '+t.description)].join(' ').toLowerCase())}" data-state="${item.status==='ready'?'ready':'other'}"><summary class="cv-row"><span class="cv-ic">${canvasIcon(item.scope==='planned'?'document':CAP_ICON[item.id]||'tools')}</span><strong>${E(item.name)}</strong><span class="cv-desc">${E(item.description)}</span><span class="cv-state ${E(item.status)}"><i></i>${E(status[item.status]||item.status)}</span>${chev}</summary><div class="cv-detail"><p>${E(item.detail||'通过当前会话的工具和预设使用。')}</p><code>${E(item.id)}</code>${item.tools.length?`<div class="cv-tools">${item.tools.map(tool=>`<details class="cv-tool"><summary>${chev}<strong>${E(tool.label||tool.name)}</strong><small>${E(tool.name)} · ${E(tool.source_label||"内置")} · ${tool.enabled?'当前预设可用':'当前预设或权限没开'}</small></summary><p>${E(tool.description)}</p><pre>${E(JSON.stringify(tool.parameters,null,2))}</pre></details>`).join('')}</div>`:''}</div></details>`;
    body.innerHTML=head('能力与插件',`${data.enabled_count} / ${data.tool_count} 个工具可用 · 按实际运行时列出，你做的扩展也在这里`,addMenu())
      +`<p class="cv-links">也在这里：${btn('data-start-page="extensions"','我的插件','text small')}${btn('data-start-page="experiences"','工作经验','text small')}${btn('data-studio="permissions"','权限与规则','text small')}${btn('data-start-page="presets"','Agent 预设','text small')}</p>`
      +builtinHtml(tree.config||{},session)+installed+pluginHostHtml(data)+'<h3 class="cv-sub">全部能力</h3>'
      +`<div class="cv-toolbar"><label class="cv-search">${canvasIcon('search')}<input id="canvasToolSearch" type="search" placeholder="搜索能力、插件或工具" aria-label="搜索工具"></label><div class="cv-seg" role="group" aria-label="按状态筛选">${[['all','全部'],['ready','已就绪'],['other','未就绪']].map(([key,label])=>`<button type="button" data-cap-filter="${key}" aria-pressed="${capFilter===key}">${label}<span>${counts[key]}</span></button>`).join('')}</div><label class="cv-select" title="按哪个 Agent 预设看">预设<select id="canvasCapabilityPreset" ${!data.session_id?'disabled':''}>${(data.presets||[]).map(p=>`<option value="${E(p.id)}" ${p.id===data.preset_id?'selected':''}>${E(p.name)}</option>`).join('')}</select></label></div>`
      +`<p class="cv-note">${data.preview?'正在按这个预设预览，会话的预设没改。':'工具最后能不能用，还要看当前权限和调用参数。'}</p><p id="canvasCapabilityEmpty" class="cv-muted" hidden>没有匹配的能力或工具。</p>`
      +[['session','会话能力',true],['global','全局服务',true],['planned','待接入能力',false]].map(([scope,title,open])=>{const list=groups.filter(g=>g.scope===scope);return list.length?`<details class="cv-group" data-capability-group ${open?'open':''}><summary>${chev}<span>${E(title)}</span><em>${list.length}</em></summary><div class="cv-list">${list.map(itemHtml).join('')}</div></details>`:'';}).join('');
    filterCapabilities();
  }
  // 搜索框 + 状态筛选一起作用; 搜索时把有结果的组临时展开, 清空后恢复原来的开合
  function filterCapabilities(){
    const input=body.querySelector('#canvasToolSearch');if(!input)return;
    const q=input.value.trim().toLowerCase();let matches=0;
    body.querySelectorAll('[data-capability-search]').forEach(el=>{el.hidden=!(el.dataset.capabilitySearch.includes(q)&&(capFilter==='all'||el.dataset.state===capFilter));if(!el.hidden)matches++;});
    body.querySelectorAll('[data-capability-group]').forEach(el=>{const narrowed=!!q||capFilter!=='all';if(narrowed&&!el.dataset.searching){el.dataset.wasOpen=String(el.open);el.dataset.searching='1';}el.hidden=![...el.querySelectorAll('[data-capability-search]')].some(row=>!row.hidden);if(narrowed&&!el.hidden)el.open=true;else if(!narrowed&&el.dataset.searching){el.open=el.dataset.wasOpen==='true';delete el.dataset.searching;}});
    body.querySelector('#canvasCapabilityEmpty').hidden=matches>0;
  }
  // 上下文: 字段翻成人话 (以前直接露出 normalized_json_chars、fold_chars 这些名字)
  function contextHtml(info){
    const total=Number(info.chars??info.context_chars)||0,fold=Number(info.fold_chars)||0,parts=info.parts&&typeof info.parts==='object'?info.parts:null;
    const unit=info.unit==='normalized_json_chars'?'完整请求的字数，含思考、工具参数和背景':'历史消息的字数';
    const prop=(label,value,note='')=>`<div class="cv-prop"><span>${E(label)}</span><b>${value}</b>${note?`<small>${E(note)}</small>`:''}</div>`;
    const PART={content:'对话',reasoning:'思考',tool_calls:'工具调用',tools:'工具说明',system:'系统提示',structure:'格式开销'};
    const known=new Set(['chars','context_chars','unit','parts','background_chars','fold_chars','over_budget','folded_count','messages','message_count']);
    const extra=Object.entries(info).filter(([k,v])=>!known.has(k)&&(typeof v==='number'||typeof v==='string'));
    return `<div class="cv-props">${prop('现在的上下文',`${wan(total)} 字`,unit)}${fold?prop('自动整理的线',`${wan(fold)} 字`,info.over_budget?'已经超过：较早的工具结果会被整理':`到这个量就整理较早的工具结果 · 现在 ${Math.min(100,Math.round(total/fold*100))}%`):''}${info.background_chars!==undefined?prop('背景资料',`${wan(info.background_chars)} 字`,'算在对话里，不另外加'):''}${info.folded_count!==undefined?prop('已经整理过',`${wan(info.folded_count)} 次`,'原始记录都还在，下面能查'):''}${info.messages??info.message_count?prop('消息',`${wan(info.messages??info.message_count)} 条`):''}${extra.map(([k,v])=>prop(contextLabel(k),E(typeof v==='number'?wan(v):v))).join('')}</div>`
      +(parts&&total?`<h3 class="cv-sub">上下文里都是什么</h3><div class="cv-parts">${Object.entries(PART).filter(([k])=>Number(parts[k])>0).map(([k,label])=>{const pct=Math.max(1,Math.round(Number(parts[k])/total*100));return `<div class="cv-part"><span>${E(label)}</span><i style="--w:${pct}%"></i><b>${wan(parts[k])} 字</b></div>`;}).join('')}</div>`:'');
  }
  async function renderPresets(version){
    try{const data=await runtime('presets');if(version!==generation)return;
      const presets=data.presets||data.preset_catalog||[];presetCatalog=presets;
      const current=getSession()?.preset;
      body.innerHTML=head('Agent 预设','选一种工作方式；权限还是看会话设置。',btn('data-new-preset',`${canvasIcon('plus')}新建预设`,'primary'))
        +`<form id="canvasPresetForm" class="cv-pop" hidden><strong class="cv-pop-title">新建预设</strong><input type="hidden" name="preset_id"><label>名称<input name="name" required maxlength="60"></label><label>用途<input name="description" required maxlength="300"></label><label>工作指令<textarea name="instructions" rows="5" required placeholder="例如：先核对资料来源，再给出带出处的研究结果。"></textarea></label><label>工具范围<select name="tool_policy"><option value="all">全部可用工具</option><option value="read">读取与检索工具</option><option value="custom">自定义工具清单</option></select></label><label id="canvasPresetTools" hidden>工具名称 · 每行一个<textarea name="tool_names" rows="4" placeholder="read_file&#10;history_search"></textarea></label><div class="cv-pop-row"><span class="grow"></span><button type="button" class="wb-btn text" data-preset-reset>取消</button><button class="wb-btn primary" type="submit">保存预设</button></div></form>`
        +`<div class="cv-pcards">${presets.map(item=>presetCard(item,current)).join('')}${designCard()}</div>`;
    }catch(error){if(version===generation)body.innerHTML=empty('预设暂时不可用',error.message);}
  }
  // ---- 协作看板 (第六轮第三批, 学 Linear 的看板): 排队 / 在做 / 等你 / 做完 · 待验收 —— 这个会话的子任务 (任务树) 或她手上所有的事 (统一状态) ----
  const BOARD_COLS=[['queued','排队'],['running','在做'],['waiting','等你'],['done','做完 · 待验收']];
  const boardCol=state=>state==='queued'?'queued':['running','preparing','cancelling'].includes(state)?'running':state==='waiting'?'waiting':'done';
  const KIND_LABEL={session:'会话',check:'待核查',child:'子任务',job:'后台作业',terminal:'终端',command:'命令',computer:'操作电脑',self_improve:'自己学',ask:'等你点头'};
  let taskView='board',taskScope='session';
  try{taskView=localStorage.getItem('wm.tasks.view')==='list'?'list':'board';taskScope=localStorage.getItem('wm.tasks.scope')==='all'?'all':'session';}catch{/* 默认看板、这个会话 */}
  const since=t=>{const s=Date.now()/1000-Number(t||0);return !t?'':s<60?'刚刚':s<3600?`${Math.floor(s/60)} 分钟前`:s<86400?`${Math.floor(s/3600)} 小时前`:`${Math.floor(s/86400)} 天前`;};
  // 统一成一种卡片: {id, title, state, detail, meta[], session, worktree}
  const treeCards=rows=>rows.map(row=>({id:row.id,title:row.title||row.goal||'子任务',state:row.status,detail:row.error||row.result||'',session:row.id,worktree:row.worktree_id||'',
    meta:[WAIT_TEXT[row.status]||stateName(row.status),row.depth?`第 ${row.depth+1} 层`:'',Number(row.steps)||Number(row.cost)?`¥${(Number(row.cost)||0).toFixed(2)} · ${Number(row.steps)||0} 步`:''].filter(Boolean)}));
  // 统一状态里同一个子任务会有两条 (它自己的会话一条、"子任务"一条): 并成一张, 说明取会话那条 (写着具体在等哪一步), 种类写子任务.
  // 工作台要你点头的一步, QQ 那边还会再来一条 (kind ask; 2026-11-07 起带 session 和 ask_id): 并进那个会话的卡, 写"QQ 上也能批"
  // (问出去了带上 QQ 上的编号), 不再多出一张; 那个会话没有卡就单独一张, 点了进那个会话. 别的申请 (学本事、改进自己……) 照旧
  function mergeActivity(items){
    const bySession=new Map(),out=[],asks=[];
    for(const item of items){
      if(item.kind==='ask'&&item.session){asks.push(item);continue;}
      const key=['session','child'].includes(item.kind)&&item.session?item.session:'',prev=key&&bySession.get(key);
      if(prev){if(item.kind==='child')prev.kind='child';else if(item.detail)prev.detail=item.detail;continue;}
      const copy={...item};if(key)bySession.set(key,copy);out.push(copy);
    }
    for(const ask of asks){
      const code=String(ask.detail||'').match(/#[A-Za-z0-9]+/)?.[0],card=bySession.get(ask.session);
      if(card){card.qqAsk=[...(card.qqAsk||[]),...(code?[code]:[])];continue;}
      const copy={...ask,qqAsk:code?[code]:[]};bySession.set(ask.session,copy);out.push(copy);
    }
    return out;
  }
  // 终端"在跑"却半小时以上没有新输出 (2026-11-07: 服务端给 quiet_s, 说明末尾也带一句): 说明里去掉那句, 卡片上单独一行弱提示; 终端卡都有"去看看" (打开那个终端)
  const QUIET_S=1800,quietText=s=>`${s>=5400?`${Math.round(s/3600)} 小时`:`${Math.round(s/60)} 分钟`}没有新输出，可能停在提示符或在等输入`;
  const activityCards=items=>mergeActivity(items).map(item=>{const quiet=item.kind==='terminal'&&Number(item.quiet_s)>=QUIET_S?Number(item.quiet_s):0;
    return {id:item.id,title:item.title||'事情',state:item.state==='needs_check'?'done':item.state,detail:quiet?String(item.detail||'').replace(/（[^（）]*没有新输出[^（）]*）\s*$/,''):item.detail||'',session:item.session||'',worktree:'',
      terminal:item.kind==='terminal'?item.id:'',quiet,
      meta:[KIND_LABEL[item.kind]||'事情',item.qqAsk?['QQ 上也能批',...item.qqAsk].join(' '):item.via==='qq'?(item.kind==='ask'?'QQ 上也能批':'QQ 上接的'):'',since(item.since)].filter(Boolean),check:item.state==='needs_check'};});
  async function taskCards(rows){
    if(taskScope!=='all')return treeCards(rows);
    const data=await runtime('activity');
    return activityCards(data.items||[]);
  }
  function cardHtml(card){
    return `<div class="cv-card" data-state="${E(card.state)}"><button type="button" class="cv-card-main" ${card.session?`data-open-session="${E(card.session)}"`:'disabled'} title="${E(card.title)}"><span class="cv-state ${E(card.check?'partial':card.state)}"><i></i></span><strong>${E(card.title)}</strong></button>`
      +(card.detail?`<p>${E(String(card.detail).slice(0,160))}</p>`:'')+(card.quiet?`<p class="cv-card-quiet">${E(quietText(card.quiet))}</p>`:'')+`<div class="cv-card-meta">${card.meta.map(m=>`<span>${E(m)}</span>`).join('')}</div>`
      +(card.terminal?`<div class="cv-card-actions">${btn(`data-open-terminal="${E(card.terminal)}" data-terminal-session="${E(card.session)}"`,'去看看','small')}</div>`:'')
      +(card.worktree?`<div class="cv-card-actions">${btn(`data-wt-diff="${E(card.worktree)}"`,'查看差异','small')}${btn(`data-wt-export="${E(card.worktree)}"`,'导出补丁','small text')}</div>`:'')+'</div>';
  }
  function boardHtml(cards){
    if(!cards.length)return empty(taskScope==='all'?'她手上现在没事':'还没有子任务',taskScope==='all'?'在做的、等你点头的、排队的、说做完了待核查的，都会出现在这里。':'可以自己拆，也可以让绫音在对话里安排。',taskScope==='all'?'':btn('data-new-task',`${canvasIcon('plus')}新建子任务`));
    const colHead=(key,label,count)=>`<span class="cv-state ${key==='done'?'completed':key}"><i></i></span><strong>${label}</strong><em>${count}</em>`;
    // 列表: 和看板同一个顺序分组, 组头一行 (空的组不出)
    if(taskView==='list')return BOARD_COLS.map(([key,label])=>{const list=cards.filter(card=>boardCol(card.state)===key);return list.length?`<div class="cv-list-group" data-col="${key}">${colHead(key,label,list.length)}</div>`+list.map(card=>`<button type="button" class="cv-row cv-task" ${card.terminal?`data-open-terminal="${E(card.terminal)}" data-terminal-session="${E(card.session)}" title="打开这个终端"`:card.session?`data-open-session="${E(card.session)}"`:''}><span class="cv-state ${E(card.check?'partial':card.state)}"><i></i></span><strong>${E(card.title)}</strong><span class="cv-desc">${E(card.detail)}${card.quiet?` · ${E(quietText(card.quiet))}`:''}</span><span class="cv-state-text">${E(card.meta[0]||'')}</span></button>`).join(''):'';}).join('');
    return BOARD_COLS.map(([key,label])=>{const list=cards.filter(card=>boardCol(card.state)===key);return `<section class="cv-col" data-col="${key}"><header>${colHead(key,label,list.length)}</header><div class="cv-col-body">${list.map(cardHtml).join('')||'<p class="cv-col-empty">没有</p>'}</div></section>`;}).join('');
  }
  // 成员 (学 DSH 的智能体团队, 第七轮): 她 (这个会话) + 每个子任务, 状态灯 + 在等什么; 点了进那个会话. 只在"这个会话的子任务"下出
  const WAIT_TEXT={queued:'排队等空位',preparing:'准备中',running:'在做',waiting:'等你点头',cancelling:'正在停下',completed:'做完了',failed:'没做成',cancelled:'停了',interrupted:'中断了',budget_exhausted:'到预算了'};
  async function teamHtml(rows){
    const session=getSession()||{},name=getState()?.her?.name||'她';
    let mine=null;  // 她这会儿在等什么 (统一状态里这个会话那一条, 比如"等子任务 1 个做完再汇总")
    if(session.status&&session.status!=='idle'){try{mine=((await runtime('activity')).items||[]).find(item=>item.session===session.id&&['session','check'].includes(item.kind));}catch{/* 读不到就只写状态 */}}
    const member=(id,avatar,title,text,state,cls='')=>`<button type="button" class="cv-member ${cls}" data-open-session="${E(id)}" title="${E(title)}"><span class="cv-avatar">${E(avatar)}</span><span class="cv-member-text"><strong>${E(title)}</strong><small>${E(text)}</small></span><span class="cv-state ${E(state)}"><i></i></span></button>`;
    const herState=mine?.state||(session.status&&session.status!=='idle'?session.status:'idle');
    return `<div class="cv-team" aria-label="成员">${member(session.id||'',name.slice(0,1),name,mine?.detail||(herState==='idle'?'空闲':WAIT_TEXT[herState]||herState),herState,'me')}`
      +rows.map((row,i)=>member(row.id,String(i+1),row.title||row.goal||'子任务',WAIT_TEXT[row.status]||row.status||'',row.status)).join('')+'</div>';
  }
  async function paintTasks(rows){
    const target=body.querySelector('.canvas-task-list');if(!target)return;
    const cards=await taskCards(rows);
    const team=taskScope==='session'&&rows.length?await teamHtml(rows):'';
    if(!body.contains(target))return;
    // 页头那句跟着范围换 (以前切到"她手上所有的事"还写"子任务 · N 个")
    const sub=body.querySelector('.cv-head .cv-title p');
    if(sub)sub.textContent=taskScope==='all'?`她手上现在的事：在做的、等你点头的、排队的、说做完了待核查的${cards.length?` · ${cards.length} 件`:''}`:`子任务各自记录过程，结果汇回主会话${rows.length?` · ${rows.length} 个`:''}`;
    const box=body.querySelector('#cvTeam');if(box)box.innerHTML=team;
    target.className=`canvas-task-list ${taskView==='board'&&cards.length?'cv-board':'cv-list'}`;
    target.innerHTML=boardHtml(cards);
  }
  // ---- 工作经验 (第七轮; 后端 wm/agent/experience.py, 读 view=experiences、改 action=experience): 她跑命令报了错、换了做法又成了,
  // 程序整理成一条; 下次同一类命令报类似的错时附给她参考. 程序整理的是最低一档, 你在这里确认 (推的时候写"主人确认过")、停用、删 ----
  const EXP_STATE={supported:['有支持','ready'],candidate:['候选','partial'],contradicted:['被反驳','failed'],retired:['退役','missing']};
  const EXP_MODE={on:'都推给她',confirmed:'只推你确认过的',shadow:'只记不推（对照）',off:'关着'};
  let expFilter='all';
  const pct=v=>v===null||v===undefined?'—':`${Math.round(Number(v)*100)}%`;
  const day=t=>t?new Date(Number(t)*1000).toLocaleDateString('zh-CN',{month:'numeric',day:'numeric'}):'—';
  const when=t=>t?new Date(Number(t)*1000).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'';
  const codeText=text=>E(text).replace(/`([^`]+)`/g,'<code>$1</code>');
  const stat=(label,value,note='')=>`<div><dt>${E(label)}</dt><dd>${value}</dd>${note?`<small>${E(note)}</small>`:''}</div>`;
  function expRow(e,open){
    const [name,dot]=EXP_STATE[e.state]||[e.state,''],mine=Number(e.trust)<=2,id=E(e.id);
    const titles=new Map((getState()?.workspaces||[]).flatMap(w=>w.sessions||[]).map(s=>[s.id,s.title])),seen=new Set();
    const sources=(e.sources||[]).filter(s=>s.session&&!seen.has(s.session)&&seen.add(s.session));  // 一个会话只写一次
    return `<details class="cv-cap cv-exp" data-exp-id="${id}" data-exp-state="${E(e.state)}" ${open?'open':''}><summary class="cv-row"><span class="cv-state ${dot}"><i></i></span><code class="cv-exp-head">${E(e.head)}</code><strong>${codeText(e.action)}</strong><span class="cv-desc">${E(e.sig)}</span>${mine?'<span class="cv-tag mine">你确认过</span>':''}${e.enabled?'':'<span class="cv-tag">停用了</span>'}<span class="cv-state-text">${E(name)} · ${pct(e.confidence)}</span></summary>`
      +`<div class="cv-detail"><p>同一类命令 <code>${E(e.head)}</code> 报这样的错时：</p><pre class="cv-exp-sig">${E(e.sig)}</pre>`
      +`<p>当时${codeText(e.action)}，同一类命令再跑就成了${Number(e.steps)?`（从报错到成了隔 ${Number(e.steps)} 步）`:''}。</p>`
      +`<dl class="cv-stats small">${stat('成过',`${Number(e.support)||0} 次`)}${stat('被反驳',`${Number(e.contradict)||0} 次`)}${stat('推给她',`${Number(e.pushed)||0} 次`)}${stat('照着做又成了',`${Number(e.hits)||0} 次`)}${stat('置信度',pct(e.confidence),'支持和反驳算的倾向，不是对错')}${stat('最近验证',day(e.verified))}</dl>`
      +(sources.length?`<p class="cv-muted">从哪几个会话整理的（只存在本机，不进提示词）：${sources.map(s=>btn(`data-open-session="${E(s.session)}"`,`${E(titles.get(s.session)||'那个会话')} · ${when(s.at)}`,'small text')).join('')}</p>`:'')
      +`<div class="cv-actions">${mine?'':btn(`data-exp-do="approve" data-exp-id="${id}" title="确认后，推给她时写&quot;主人确认过&quot;"`,'确认这条','small primary')}${btn(`data-exp-do="${e.enabled?'disable':'enable'}" data-exp-id="${id}"`,e.enabled?'停用':'启用','small')}${btn(`data-exp-ask="${id}"`,'删除','small text')}</div>`
      +`<div class="nb-confirm" data-exp-confirm="${id}" hidden><span>删掉这条？程序以后碰到同样的情况还会重新记。</span>${btn(`data-exp-do="delete" data-exp-id="${id}"`,'删掉','small danger')}${btn('data-exp-cancel','算了','small text')}</div></div></details>`;
  }
  async function renderExperiences(version){
    const opened=new Set([...body.querySelectorAll('.cv-exp[open]')].map(el=>el.dataset.expId));
    const data=await runtime('experiences');if(version!==generation)return;
    const items=data.items||[],on=(data.stats||{})[data.mode==='shadow'?'shadow':'on']||{};
    const counts={all:items.length};for(const key of Object.keys(EXP_STATE))counts[key]=items.filter(e=>e.state===key).length;
    const rate=(v,word)=>v===null||v===undefined?'':`${word} ${pct(v)}`;
    body.innerHTML=head('工作经验','她跑命令报了错、换了做法又成了，程序就记成一条；下次同一类命令报类似的错时附给她参考（不是指令）。你确认过的，推的时候写"主人确认过"。',iconBtn('data-exp-refresh','refresh','刷新'))
      +`<dl class="cv-stats">${stat('现在怎么用',E(EXP_MODE[data.mode]||data.mode||'—'))}${stat('推给她',`${Number(on.pushes)||0} 次`)}${stat('她照着做',`${Number(on.adopted)||0} 次`,rate(on.adopt_rate,'采纳率'))}${stat('照着做又成了',`${Number(on.hits)||0} 次`,rate(on.hit_rate,'命中率'))}</dl>`
      +(items.length?`<div class="cv-toolbar"><div class="cv-seg" role="group" aria-label="按状态筛选">${[['all','全部'],...Object.entries(EXP_STATE).map(([key,[label]])=>[key,label])].map(([key,label])=>`<button type="button" data-exp-filter="${key}" aria-pressed="${expFilter===key}">${label}<span>${counts[key]}</span></button>`).join('')}</div></div>`
        +`<div class="cv-list">${items.map(e=>expRow(e,opened.has(e.id))).join('')}</div><p id="cvExpEmpty" class="cv-muted" hidden>这一类现在没有。</p>`
        :`<div class="cv-list">${empty('还没整理出工作经验','她在工作台跑命令报了错、换了做法又成了，这一轮结束后程序会自动记一条。')}</div>`);
    filterExperiences();
  }
  function filterExperiences(){
    let shown=0;
    body.querySelectorAll('.cv-exp').forEach(el=>{el.hidden=expFilter!=='all'&&el.dataset.expState!==expFilter;if(!el.hidden)shown++;});
    const none=body.querySelector('#cvExpEmpty');if(none)none.hidden=shown>0;
  }
  // ---- 资料本 (后端 wm/agent/notebook.py, 接口 /api/agent/notebooks): 列表 → 一本的详情 (资料、加资料、检索、笔记、挂到这个会话) ----
  const NB_STATUS={ready:['已读好','ready'],partial:['读了一部分','partial'],needs_ocr:['要 OCR','partial'],garbled:['乱码','failed'],empty:['没有文字','missing'],
    image:['是图片','missing'],unsupported:['格式不支持','missing'],unavailable:['缺解析组件','missing'],error:['没读成','failed']};
  const NB_KIND={file:['document','文件'],web:['browser','网页'],text:['rules','贴的文字'],note:['edit','笔记']};
  const nbApi=params=>canvasRequest('/api/agent/notebooks'+(params?'?'+new URLSearchParams(params):''));
  const nbPost=data=>canvasRequest('/api/agent/notebooks',data);
  const nbChanged=()=>dispatchEvent(new CustomEvent('wm-notebook-changed'));  // 工作台据此刷新"这个会话挂着哪本"
  const ago=t=>{const s=Date.now()/1000-Number(t||0);return s<60?'刚刚':s<3600?`${Math.floor(s/60)} 分钟前`:s<86400?`${Math.floor(s/3600)} 小时前`:`${Math.floor(s/86400)} 天前`;};
  const nbFailed=error=>/\(404\)/.test(error.message)?empty('资料本还打不开','资料本要等服务端重启后才有（重启前会先问你）。'):empty('资料本暂时读不了',error.message);
  const nbAdded=out=>{const a=out.added||{};return a.duplicate?`「${a.name}」已经在里面了（S${a.no}）`:`加好了：S${a.no} · ${a.chunks||0} 块${a.reader?`；${a.reader}`:''}`;};
  let nbSource='',nbMessage='',nbLast='';  // nbSource: "只在这份里查"; nbMessage: 下一次画页面时顶上那句 (加好了、删掉了……)
  async function renderNotebooks(version){
    const sid=getSession()?.id||'';
    try{
      if(tab){await renderNotebook(version,tab,sid);return;}
      const data=await nbApi(sid?{session:sid}:null);if(version!==generation)return;
      const rows=data.notebooks||[],attached=data.attached||'';
      body.innerHTML=head('资料本',`跨会话保留的学习资料；挂到会话后，她只按资料回答，句句带出处${rows.length?` · ${rows.length} 本`:''}`,iconBtn('data-nb-refresh','refresh','刷新')+btn('data-new-nb',`${canvasIcon('plus')}新建资料本`,'primary'))
        +`<form id="nbCreate" class="cv-pop" hidden><strong class="cv-pop-title">新建资料本</strong><label>名字<input name="title" maxlength="80" required placeholder="比如：线代复习、论文资料"></label><div class="cv-pop-row"><span class="grow"></span><button type="button" class="wb-btn text" data-pop-cancel>取消</button><button class="wb-btn primary" type="submit">新建</button></div></form>`
        +`<p id="nbStatus" class="cv-notice" role="status"></p>`
        +(rows.length?`<div class="cv-list nb-books">${rows.map(b=>`<button type="button" class="cv-row" data-nb-open="${E(b.id)}"><span class="cv-ic">${canvasIcon('book')}</span><strong>${E(b.title)}</strong><span class="cv-desc">${b.sources} 份资料 · ${wan(b.chunks)} 块${b.notes?` · ${b.notes} 条笔记`:''}</span>${b.id===attached?'<span class="cv-tag on">挂在这个会话</span>':''}<span class="cv-meta">${ago(b.updated)}</span>${chev}</button>`).join('')}</div>`
          :empty('还没有资料本','建一本，把文件、网页、文字放进去；挂到会话后，她回答会标出处。',`<div class="cv-actions">${btn('data-new-nb',`${canvasIcon('plus')}新建资料本`)}</div>`));
      body.dataset.nb='';
      flushNbMessage();
    }catch(error){if(version===generation)body.innerHTML=head('资料本')+nbFailed(error);}
  }
  function flushNbMessage(){const s=body.querySelector('#nbStatus');if(s&&nbMessage){s.textContent=nbMessage;nbMessage='';}}
  async function renderNotebook(version,nid,sid){
    const [d,list]=await Promise.all([nbApi({id:nid}),sid?nbApi({session:sid}):Promise.resolve({})]);
    if(version!==generation)return;
    const attached=list.attached===nid,sources=d.sources||[],notes=d.notes||[];
    const srcRow=s=>{
      const [glyph,kind]=NB_KIND[s.kind]||['document','资料'],[label,cls]=NB_STATUS[s.status]||[s.status||'没读成','missing'],pdf=s.kind==='file'&&String(s.format||'').toLowerCase()==='pdf';
      const props=[['类型',kind+(s.kind==='file'&&s.format?` · ${String(s.format).toUpperCase()}`:'')],['块',`${s.chunks||0} 块 · ${wan(s.chars)} 字`],
        ['向量',s.vectors?'算好了，按意思也能找到':d.embedder?'还在后台算，先按关键词找':'没载嵌入模型，按关键词找'],['加入',s.added?new Date(s.added*1000).toLocaleString('zh-CN'):'—'],
        ...(s.reader?[['照实的说明',s.reader]]:[]),...(s.url?[['网址',s.url]]:[]),...((s.warnings||[]).length?[['提醒',s.warnings.join('；')]]:[])];
      return `<details class="cv-cap nb-source" data-nb-src="${s.no}"><summary class="cv-row"><span class="cv-ic">${canvasIcon(glyph)}</span><strong>${E(s.name)}</strong><span class="cv-desc" title="${E(s.reader||'')}">${E(s.reader||`${s.chunks||0} 块 · ${wan(s.chars)} 字`)}</span><span class="cv-tag">S${s.no}</span><span class="cv-state ${cls}"><i></i>${E(label)}</span>${chev}</summary>`
        +`<div class="cv-detail"><div class="cv-props nb-props">${props.map(([k,v])=>`<div class="cv-prop"><span>${E(k)}</span><b>${E(v)}</b></div>`).join('')}</div>`
        +`<div class="cv-actions">${openReading?btn(`data-nb-read="${s.no}" data-title="${E(s.name)}"`,'阅读','small'):''}${pdf?btn(`data-nb-doc="${s.no}" data-title="${E(s.name)}"`,'打开原文','small'):''}${s.kind==='web'&&s.url?btn(`data-nb-web="${E(s.url)}"`,'打开网页','small'):''}${s.chunks?btn(`data-nb-search-in="${s.no}"`,'只在这份里查','small'):''}<span class="grow"></span>${btn(`data-nb-remove="${s.no}"`,'拿掉','small text')}</div>`
        +`<div class="nb-confirm" data-nb-remove-box="${s.no}" hidden><span>拿掉「${E(s.name)}」？它的块和向量会删掉（原文件留着），以前的出处编号不会指到别的资料上。</span><button type="button" class="wb-btn small text" data-nb-remove-cancel>取消</button><button type="button" class="wb-btn small danger" data-nb-remove-ok="${s.no}">拿掉</button></div></div></details>`;
    };
    const cancel='<button type="button" class="wb-btn text" data-pop-cancel>取消</button>';
    body.innerHTML=`<button type="button" class="wb-btn text small cv-back" data-nb-back>← 全部资料本</button>`
      +head(d.title,`${sources.length} 份资料 · ${wan(d.chunks)} 块${notes.length?` · ${notes.length} 条笔记`:''} · ${d.embedder?'按关键词和意思一起找':'按关键词找（嵌入模型没载）'}`,
        iconBtn('data-nb-rename','edit','改名')+iconBtn('data-nb-delete','trash','删掉这本')+(sid?(attached?btn('data-nb-detach',`${canvasIcon('book')}挂在这个会话 · 解开`):btn('data-nb-attach',`${canvasIcon('book')}挂到这个会话`,'primary')):''))
      +`<form id="nbRename" class="cv-pop" hidden><strong class="cv-pop-title">改名</strong><label>名字<input name="title" maxlength="80" required value="${E(d.title)}"></label><div class="cv-pop-row"><span class="grow"></span>${cancel}<button class="wb-btn primary" type="submit">保存</button></div></form>`
      +`<div id="nbDelete" class="cv-pop" hidden><strong class="cv-pop-title">删掉「${E(d.title)}」？</strong><p class="cv-muted">会挪进资料本目录下的回收站（.trash），原文件、块、笔记都还在，能找回；挂着它的会话会解开。</p><div class="cv-pop-row"><span class="grow"></span>${cancel}<button type="button" class="wb-btn danger" data-nb-delete-ok>删掉</button></div></div>`
      +(sid&&!attached?'<p class="cv-note">挂到这个会话后，她回答这个会话里的问题会先查这本，句子后面标出处，比如 [S3-17]。</p>':'')
      +`<p id="nbStatus" class="cv-notice" role="status"></p>`
      +`<h3 class="cv-sub">加资料</h3><div class="nb-add">${btn('data-nb-add="file"',`${canvasIcon('document')}上传文件`)}${btn('data-nb-add="url"',`${canvasIcon('browser')}网址`)}${btn('data-nb-add="text"',`${canvasIcon('rules')}贴文字`)}<input type="file" id="nbFile" multiple hidden></div>`
      +`<form id="nbUrl" class="cv-pop" hidden><strong class="cv-pop-title">加网页</strong><label>网址<input name="url" type="url" required placeholder="https://…"></label><p class="cv-muted">只读公网；加的时候存一份正文，以后不再联网。</p><div class="cv-pop-row"><span class="grow"></span>${cancel}<button class="wb-btn primary" type="submit">加进来</button></div></form>`
      +`<form id="nbText" class="cv-pop" hidden><strong class="cv-pop-title">贴一段文字</strong><label>标题<input name="title" maxlength="120" placeholder="不写就用第一行"></label><label>文字<textarea name="text" rows="6" required placeholder="把要当资料的文字贴在这里；Markdown 的标题会记成位置"></textarea></label><div class="cv-pop-row"><span class="grow"></span>${cancel}<button class="wb-btn primary" type="submit">加进来</button></div></form>`
      +`<h3 class="cv-sub">资料${sources.length?` · ${sources.length}`:''}</h3>`
      +(sources.length?`<div class="cv-list nb-sources">${sources.map(srcRow).join('')}</div>`:'<p class="cv-muted">还没有资料。上传文件（PDF、Word、PPT、Excel、Markdown、电子书……单个最多 32 MB）、加网页，或者贴一段文字。</p>')
      +`<h3 class="cv-sub">在这本里查</h3><form id="nbSearch" class="cv-search-form"><label class="cv-search">${canvasIcon('search')}<input name="query" type="search" required placeholder="${nbSource?`只在 S${E(nbSource)} 里查`:'问一句，看她会查到哪几块'}" aria-label="在资料本里查"></label>${nbSource?btn('data-nb-search-all','查全部资料','small text'):''}<button type="submit" class="wb-btn">查</button></form><div id="nbHits"></div>`
      +`<h3 class="cv-sub">笔记${notes.length?` · ${notes.length}`:''}</h3><form id="nbNote" class="nb-note-form"><textarea name="text" rows="2" required maxlength="20000" placeholder="记一条笔记；可以写出处编号，比如 [S1-2]"></textarea><div class="cv-pop-row"><label class="cv-inline"><input type="checkbox" name="as_source"> 也当一份资料（能被搜到、被引用）</label><span class="grow"></span><button class="wb-btn" type="submit">记下</button></div></form>`
      +(notes.length?`<div class="nb-notes">${notes.slice().reverse().map(n=>`<article class="nb-note"><div class="nb-note-text">${renderMarkdown(n.text,{cite:true})}</div><small>${n.by==='user'?'你记的':'她记的'} · ${ago(n.created)}${n.source?` · 也是资料 S${n.source}`:''}</small></article>`).join('')}</div>`:'');
    body.dataset.nb=nid;nbLast=nid;
    flushNbMessage();
  }
  function nbHits(found){
    const target=body.querySelector('#nbHits');if(!target)return;
    const rows=found.results||[];
    target.innerHTML=(found.weak?`<p class="nb-weak">相关度低：资料里多半没有这个。${E(found.why||'')}</p>`:`<p class="cv-muted">${E(found.why||'')}</p>`)
      +(rows.length?`<div class="cv-list nb-hits">${rows.map(h=>`<details class="cv-cap nb-hit"><summary class="cv-row"><span class="cv-tag">${E(h.key)}</span><strong>${E(h.name)}</strong><span class="cv-desc">${E(h.label)}${h.label?' · ':''}${E(String(h.text||'').replace(/\s+/g,' ').slice(0,90))}</span>${chev}</summary><div class="cv-detail"><p class="nb-text">${E(h.text)}</p><div class="cv-actions">${btn(`data-nb-cite-open="${E(h.key)}"`,'看前后文','small')}</div></div></details>`).join('')}</div>`:'');
  }
  async function nbUpload(files){
    const nid=body.dataset.nb,list=[...files||[]];if(!nid||!list.length)return;
    const said=[];let added=0;
    for(const [i,file] of list.entries()){
      if(file.size>32*1024*1024){said.push(`${file.name}：超过 32 MB`);continue;}
      const status=body.querySelector('#nbStatus');if(status)status.textContent=`正在加「${file.name}」（${i+1} / ${list.length}）…`;
      try{
        const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(String(reader.result).split(',')[1]||'');reader.onerror=()=>reject(new Error('读不了这个文件'));reader.readAsDataURL(file);});
        const out=await nbPost({action:'add',id:nid,name:file.name,data});
        if(!out.added?.duplicate)added++;
        if(out.added?.duplicate||out.added?.reader||out.added?.status!=='ready')said.push(`${file.name}：${nbAdded(out)}`);
      }catch(error){said.push(`${file.name}：${error.message}`);}
    }
    nbMessage=`加了 ${added} 份${said.length?`。${said.join('；')}`:''}`;
    nbChanged();
    if(body.dataset.nb===nid)await render();
  }
  async function refreshTasks(){
    if(dock.hidden||page!=='agents'||tab!=='tasks')return;
    const version=generation,sequence=++treeSequence;
    try{const data=await runtime('tree');if(version!==generation||sequence!==treeSequence)return;body._taskRows=flattenTree(data.tree||[]);await paintTasks(body._taskRows);}catch(error){if(version===generation)announce(error.message);}
  }
  async function searchHistory(offset=0,append=false){
    const version=generation,sequence=++historyGeneration;
    const data=await runtime('history',{query:historyQuery,offset});
    if(version!==generation||sequence!==historyGeneration)return;
    const target=body.querySelector('#canvasHistoryResults');if(!target)return;
    const items=(data.items||[]).map(item=>`<article class="cv-hit"><small>${E(item.role||'记录')}</small><p>${E(item.excerpt||'')}</p><button type="button" class="wb-btn text small" data-history-id="${E(item.message_id)}">读取原始记录</button></article>`).join('');
    target.querySelector('[data-history-more]')?.remove();
    if(append)target.insertAdjacentHTML('beforeend',items);else target.innerHTML=`<p class="cv-muted">${data.total} 条匹配</p>`+items;
    if(data.next_offset!==null)target.insertAdjacentHTML('beforeend',`<button type="button" class="wb-btn" data-history-more="${data.next_offset}">显示更多匹配</button>`);
  }
  tabs.addEventListener('click',event=>{const button=event.target.closest('[data-canvas-tab]');if(button){tab=button.dataset.canvasTab;render();}});
  body.addEventListener('change',event=>{if(event.target.id==='nbFile'){nbUpload(event.target.files);return;}
    if(event.target.id==='canvasWebDefault'){try{localStorage.setItem('wm.workbench.webDefault',event.target.value);}catch{/* 存不下就只管这一次 */}announce({follow:'新会话的联网搜索跟着当前会话',on:'新会话默认开联网搜索',off:'新会话默认关联网搜索'}[event.target.value]||'');return;}if(event.target.id==='canvasCapabilityPreset'){capabilityPreset=event.target.value;render();return;}if(event.target.name==='tool_policy')body.querySelector('#canvasPresetTools').hidden=event.target.value!=='custom';});
  body.addEventListener('input',event=>{if(event.target.id==='canvasToolSearch')filterCapabilities();});
  // 列表里 ↑↓ 换行、Esc 收起弹出的新建卡 (学 Linear 的键盘操作)
  body.addEventListener('keydown',event=>{
    if(event.key==='Escape'){const add=event.target.closest('.cv-add[open]');if(add){event.preventDefault();add.open=false;add.querySelector('summary').focus();return;}const pop=event.target.closest('.cv-pop');if(pop){event.preventDefault();event.stopPropagation();hidePop(pop);}return;}
    if(!['ArrowDown','ArrowUp'].includes(event.key)||!event.target.matches('.cv-list .cv-row, .cv-list summary.cv-row'))return;
    const rows=[...body.querySelectorAll('.cv-list .cv-row')].filter(el=>el.offsetParent&&(el.matches('summary')||el.matches('button')||el.querySelector('button')));
    const focusable=rows.map(el=>el.matches('summary, button')?el:el.querySelector('button'));
    const i=focusable.indexOf(event.target);if(i<0)return;
    event.preventDefault();focusable[(i+(event.key==='ArrowDown'?1:-1)+focusable.length)%focusable.length]?.focus();
  });
  document.addEventListener('click',event=>{body.querySelectorAll('.cv-add[open]').forEach(menu=>{if(!menu.contains(event.target))menu.open=false;});});
  function showPop(form,title=''){if(!form)return;form.hidden=false;const t=form.querySelector('.cv-pop-title');if(t&&title)t.textContent=title;form.querySelector('textarea, input:not([type=hidden]), select')?.focus();}
  function hidePop(form){if(!form)return;form.reset();form.hidden=true;if(form.id==='canvasPresetForm'){form.elements.preset_id.value='';body.querySelector('#canvasPresetTools').hidden=true;}}
  body.addEventListener('click',async event=>{
    const link=event.target.closest('a.wb-cite');  // 笔记里的网址小胶囊: 在面板的网页标签里开
    if(link){event.preventDefault();openWeb?.(link.href);return;}
    const button=event.target.closest('button');if(!button||button.disabled)return;
    if(button.dataset.nbKey||button.dataset.nbCiteOpen){showCitation(button,{notebookId:body.dataset.nb,key:button.dataset.nbKey||button.dataset.nbCiteOpen,openDoc,openWeb,openReading});return;}
    const add=button.closest('.cv-add');if(add)add.open=false;  // 添加插件的菜单: 点了哪一项都先收起
    const version=generation;button.disabled=true;
    try{
      if(button.hasAttribute('data-create-plugin'))setDraft('帮我做一个工作台插件：（说说想要它做什么）。先和我确认用途和验收标准，再做成能预览的版本；做好先给我看，再决定启用。');
      else if(button.hasAttribute('data-web-now')){
        const next=button.getAttribute('aria-checked')!=='true';
        if(await changeSetting?.('web',next)===false)return;
        button.setAttribute('aria-checked',String(next));announce(next?'这个会话的联网搜索开了':'这个会话的联网搜索关了');
      }
      else if(button.dataset.extToggle){
        const next=button.getAttribute('aria-checked')!=='true';
        await canvasRequest('/api/agent/studio',{action:'extension_enable',id:getSession()?.id||'',package_id:button.dataset.extToggle,enabled:next});
        if(version===generation){await render();announce(next?'启用了：她接下来就能用':'停用了：她不再用它');}
      }
      else if(button.dataset.startPage){dispatchEvent(new CustomEvent('wm-panel-tool',{detail:{name:button.dataset.startPage}}));}
      else if(button.hasAttribute('data-new-nb'))showPop(body.querySelector('#nbCreate'));
      else if(button.dataset.nbOpen){nbSource='';await open('notebooks',button.dataset.nbOpen);}
      else if(button.hasAttribute('data-nb-back')){nbSource='';nbLast='';await open('notebooks','');}
      else if(button.hasAttribute('data-nb-refresh'))await render();
      else if(button.hasAttribute('data-nb-rename'))showPop(body.querySelector('#nbRename'));
      else if(button.hasAttribute('data-nb-delete'))showPop(body.querySelector('#nbDelete'));
      else if(button.hasAttribute('data-nb-delete-ok')){await nbPost({action:'delete',id:body.dataset.nb});nbChanged();nbMessage='删掉了：挪进了回收站，能找回';nbSource='';nbLast='';await open('notebooks','');}
      else if(button.hasAttribute('data-nb-attach')||button.hasAttribute('data-nb-detach')){
        const sid=getSession()?.id,on=button.hasAttribute('data-nb-attach');if(!sid)return;
        await nbPost({action:on?'attach':'detach',id:body.dataset.nb,session_id:sid});nbChanged();nbMessage=on?'挂好了：这个会话里她会先查这本，回答标出处':'解开了：这个会话不再查这本';await render();
      }
      else if(button.dataset.nbAdd==='file')body.querySelector('#nbFile')?.click();
      else if(button.dataset.nbAdd)showPop(body.querySelector(button.dataset.nbAdd==='url'?'#nbUrl':'#nbText'));
      else if(button.dataset.nbRemove){const box=body.querySelector(`[data-nb-remove-box="${button.dataset.nbRemove}"]`);if(box)box.hidden=false;}
      else if(button.hasAttribute('data-nb-remove-cancel'))button.closest('.nb-confirm').hidden=true;
      else if(button.dataset.nbRemoveOk){await nbPost({action:'remove',id:body.dataset.nb,source:Number(button.dataset.nbRemoveOk)});nbChanged();nbMessage=`拿掉了 S${button.dataset.nbRemoveOk}`;await render();}
      else if(button.dataset.nbRead)openReading?.(body.dataset.nb,button.dataset.nbRead,'',button.dataset.title);
      else if(button.dataset.nbDoc)openDoc?.(`/api/agent/notebooks?${new URLSearchParams({id:body.dataset.nb,source:button.dataset.nbDoc,file:'1'})}`,button.dataset.title||'原文');
      else if(button.dataset.nbWeb)openWeb?.(button.dataset.nbWeb);
      else if(button.dataset.nbSearchIn){nbSource=button.dataset.nbSearchIn;await render();body.querySelector('#nbSearch input')?.focus();}
      else if(button.hasAttribute('data-nb-search-all')){nbSource='';await render();}
      else if(button.dataset.capFilter){capFilter=button.dataset.capFilter;body.querySelectorAll('[data-cap-filter]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));filterCapabilities();}
      else if(button.hasAttribute('data-new-task'))showPop(body.querySelector('#canvasDelegate'));
      else if(button.dataset.expFilter){expFilter=button.dataset.expFilter;body.querySelectorAll('[data-exp-filter]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));filterExperiences();}
      else if(button.dataset.expAsk){const box=body.querySelector(`[data-exp-confirm="${CSS.escape(button.dataset.expAsk)}"]`);if(box)box.hidden=false;}
      else if(button.hasAttribute('data-exp-cancel'))button.closest('.nb-confirm').hidden=true;
      else if(button.dataset.expDo){
        await command('experience',{exp_id:button.dataset.expId,do:button.dataset.expDo});
        if(version===generation){announce({approve:'确认了：以后推给她时写"主人确认过"',disable:'停用了：不再推给她',enable:'启用了：同类报错时又会推给她',delete:'删掉了这条经验'}[button.dataset.expDo]||'');await renderExperiences(version);}
      }
      else if(button.hasAttribute('data-exp-refresh'))await renderExperiences(version);
      else if(button.dataset.taskView||button.dataset.taskScope){
        if(button.dataset.taskView)taskView=button.dataset.taskView;else taskScope=button.dataset.taskScope;
        try{localStorage.setItem('wm.tasks.view',taskView);localStorage.setItem('wm.tasks.scope',taskScope);}catch{/* 下次照默认 */}
        body.querySelectorAll('[data-task-view]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.taskView===taskView)));
        body.querySelectorAll('[data-task-scope]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.taskScope===taskScope)));
        await paintTasks(body._taskRows||[]);
      }
      else if(button.dataset.wtDiff){const out=await canvasRequest('/api/agent/studio',{action:'worktree_diff',id:getSession()?.id||'',worktree_id:button.dataset.wtDiff});const box=body.querySelector('#cvWorktree');if(box)box.innerHTML=`<details class="cv-settings" open><summary>${chev}工作副本差异${out.truncated?'（已截断）':''}</summary><pre class="cv-diff">${E(out.text||'没有发现文本差异')}</pre></details>`;}
      else if(button.dataset.wtExport){const out=await canvasRequest('/api/agent/studio',{action:'worktree_export',id:getSession()?.id||'',worktree_id:button.dataset.wtExport});announce(`补丁导出了：${out.path||out.note||''}。还没合进原来的工作区。`);}
      else if(button.hasAttribute('data-new-preset'))showPop(body.querySelector('#canvasPresetForm'),'新建预设');
      else if(button.hasAttribute('data-pop-cancel'))hidePop(button.closest('.cv-pop'));
      else if(button.dataset.openTerminal){await openTerminal?.(button.dataset.terminalSession,button.dataset.openTerminal);}
      else if(button.dataset.openSession){await openSession(button.dataset.openSession);render();}
      else if(button.dataset.studio)openStudio(button.dataset.studio);
      else if(button.dataset.settings)openSettings(button.dataset.settings);
      else if(button.hasAttribute('data-canvas-refresh'))await refreshTasks();
      else if(button.dataset.presetUse){await setPreset(button.dataset.presetUse);await reload();if(version===generation){render();announce('Agent 预设已应用');}}
      else if(button.dataset.presetEdit){const item=presetCatalog.find(p=>p.id===button.dataset.presetEdit);if(!item)return;const form=body.querySelector('#canvasPresetForm');showPop(form,'编辑预设 · '+item.name);form.elements.preset_id.value=item.id;for(const key of ['name','description','instructions'])form.elements[key].value=item[key]||'';form.elements.tool_policy.value=item.tools.includes('*')?'all':'custom';form.elements.tool_names.value=item.tools.join('\n');body.querySelector('#canvasPresetTools').hidden=form.elements.tool_policy.value!=='custom';form.scrollIntoView({block:'nearest'});form.elements.name.focus();}
      else if(button.hasAttribute('data-preset-reset'))hidePop(body.querySelector('#canvasPresetForm'));
      else if(button.dataset.presetDelete){await command('preset_delete',{preset_id:button.dataset.presetDelete});await reload();if(version===generation)render();}
      else if(button.hasAttribute('data-create-preset')){setDraft('帮我设计一个可复用的 Agent 预设。先和我确认用途、工作指令和工具范围，再创建。');}
      else if(button.hasAttribute('data-life-draft')){setDraft('看看你当前生活参数和实际运行状态，结合最近的经历提出你想尝试的节奏调整。说明原因、预期效果与恢复方法；保持原样也可以。');}
      else if(button.hasAttribute('data-compact')){await command('compact');announce('上下文已压缩，原始记录保留');render();}
      else if(button.hasAttribute('data-history-more'))await searchHistory(Number(button.dataset.historyMore),true);
      else if(button.dataset.historyId){const data=await runtime('message',{message_id:button.dataset.historyId,offset:button.dataset.historyOffset||'0'});if(version===generation){const pre=document.createElement('pre');pre.className='canvas-code';pre.textContent=data.text||'';button.before(pre);if(data.next_offset!==null){button.dataset.historyOffset=data.next_offset;button.textContent='继续读取原文';}else button.remove();}}
    }catch(error){if(version===generation)announce(error.message);}finally{button.disabled=false;}
  });
  body.addEventListener('submit',async event=>{
    const form=event.target;if(!['canvasDelegate','canvasPresetForm','canvasHistorySearch','canvasTeamConfig','nbCreate','nbRename','nbUrl','nbText','nbSearch','nbNote'].includes(form.id))return;
    event.preventDefault();const version=generation,button=event.submitter;button.disabled=true;
    try{
      if(form.id==='canvasDelegate'){const goals=form.querySelector('textarea').value.split('\n').map(x=>x.trim()).filter(Boolean);await command('delegate',{tasks:goals.map(goal=>({goal,workspace_mode:form.elements.workspace_mode.value}))});await reload();if(version===generation){announce('子任务已创建');render();}}
      else if(form.id==='canvasPresetForm'){const values=Object.fromEntries(new FormData(form));values.id=values.preset_id||'custom-'+crypto.randomUUID().split('-')[0];values.tools=values.tool_policy==='read'?(await runtime('tools')).tools.filter(tool=>RESEARCH_TOOLS.has(tool.name)).map(tool=>tool.name):values.tool_policy==='custom'?values.tool_names.split(/[,\s]+/).filter(Boolean):['*'];delete values.preset_id;delete values.tool_policy;delete values.tool_names;await command('preset_save',{preset:values});await reload();if(version===generation){announce('预设已保存');render();}}
      else if(form.id==='canvasTeamConfig'){const out=await command('team_config',{config:{max_parallel:Number(form.elements.max_parallel.value),max_depth:Number(form.elements.max_depth.value)}});if(version===generation)announce(`存好了：同时跑 ${out.config?.max_parallel} 个，最多套 ${out.config?.max_depth} 层`);}
      else if(form.id==='nbCreate'){const out=await nbPost({action:'create',title:form.elements.title.value});nbChanged();nbMessage='建好了：加点资料，再挂到会话';await open('notebooks',out.notebook.id);}
      else if(form.id==='nbRename'){await nbPost({action:'rename',id:body.dataset.nb,title:form.elements.title.value});nbChanged();nbMessage='改好了';await render();}
      else if(form.id==='nbUrl'||form.id==='nbText'){
        const status=body.querySelector('#nbStatus');if(status)status.textContent=form.id==='nbUrl'?'正在读这个网页…':'正在切块…';
        const out=await nbPost(form.id==='nbUrl'?{action:'add',id:body.dataset.nb,url:form.elements.url.value}:{action:'add',id:body.dataset.nb,title:form.elements.title.value,text:form.elements.text.value});
        nbChanged();nbMessage=nbAdded(out);await render();
      }
      else if(form.id==='nbSearch'){const found=await nbPost({action:'search',id:body.dataset.nb,query:form.elements.query.value,...(nbSource?{source:Number(nbSource)}:{})});if(version===generation)nbHits(found);}
      else if(form.id==='nbNote'){const text=form.elements.text.value;await nbPost({action:'note',id:body.dataset.nb,text,cites:citedKeys(text),as_source:form.elements.as_source.checked});nbChanged();nbMessage='记下了';await render();}
      else {historyQuery=String(new FormData(form).get('query'));await searchHistory();}
    }catch(error){if(version===generation)announce(error.message);}finally{button.disabled=false;}
  });
  document.getElementById('chat').addEventListener('click',async event=>{
    const button=event.target.closest('[data-message-action]');if(!button||actionLock)return;
    const sid=getSession()?.id, id=button.dataset.messageId, action=button.dataset.messageAction;
    const item=(getSession()?.events||[]).find(e=>e.message_id===id);
    actionLock=true;button.disabled=true;
    try{
      if(action==='copy'){const text=item?.text||button.closest('.her-msg,.user-message')?.querySelector('.message-text,.msg-user')?.textContent||'';await navigator.clipboard.writeText(text);notify('已复制消息');}
      else if(action==='fork'){const result=await api({action:'fork',id:sid,message_id:id});await reload();if(getSession()?.id===sid)await openSession(result.id);notify('已从这条消息建立分支');}
      else {await command('feedback',{message_id:id,value:button.getAttribute('aria-pressed')==='true'?'':action});await reload();renderChat();notify('反馈已保存');}
    }catch(error){notify(error.message);}finally{actionLock=false;button.disabled=false;}
  });
  return {open,close,render,refreshTasks,sessionChanged(){if(page)render();}};
}
function stateName(state){return {running:'运行中',queued:'排队中',waiting:'等待回复',completed:'已完成',done:'已完成',idle:'就绪',cancelled:'已停止',failed:'失败',preparing:'准备中',cancelling:'停止中'}[state]||state||'就绪';}
function flattenTree(rows,depth=0){return (Array.isArray(rows)?rows:rows?[rows]:[]).flatMap(row=>[{...row,depth},...flattenTree(row.children,depth+1)]);}
function contextLabel(key){return {messages:'消息数',message_count:'消息数',events:'事件数',context_chars:'当前上下文字数',original_chars:'原始记录字数',estimated_tokens:'估算 Token',folded:'已压缩记录',compressed_messages:'已压缩记录',history_messages:'历史消息',tool_results:'工具结果',chars:'当前上下文字数'}[key]||key;}
