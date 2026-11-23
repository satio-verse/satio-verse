// 右侧面板里的文件标签 (2026-11-01 照 Codex 改版): 一行头部 —— 面包屑、显示更改、查看源代码 / 预览、打开 ▾ —— 右边文件树
// (带筛选), 正文按行号显示; 选中几行能引用到对话 (带"第几行", Lexora 那种快照 + 来源). 标签条在 workbench-panel.js:
// 一个文件一个标签, path 为空的标签只看文件树. 文件只读 (/api/agent/workspace?action=files), 不在这里改.
import { diffLines, hunks } from '/pet/workbench-diff.js';

const E = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const G = {
  folder: '<path d="M3 6.5h6l2 2h10V19H3z"/>', file: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/>', chevron: '<path d="m9 6 6 6-6 6"/>',
  tree: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><path d="M15 4v16"/>', diff: '<path d="M8 4v10M3 9h10M14 15h7M7 20l10-16"/>',
  code: '<path d="m8 8-4 4 4 4M16 8l4 4-4 4M13.5 5l-3 14"/>', eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/>',
  open: '<path d="M14 4h6v6M20 4l-9 9M18 14v6H4V6h6"/>', down: '<path d="m7 10 5 5 5-5"/>', search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 4.5 4.5"/>',
  refresh: '<path d="M20 7v5h-5M20 12a8 8 0 1 0-2 6"/>', quote: '<path d="M5 5h14v12H9l-4 4zM8 9h8M8 13h5"/>', minus: '<path d="M6 12h12"/>',
};
const icon = (name, size = 16) => `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${G[name] || G.file}</svg>`;
const bytes = n => n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`;
const base = path => String(path || '').replace(/\\/g, '/').replace(/\/$/, '').split('/').pop() || '工作区';
const kind = path => /\.(md|markdown)$/i.test(path) ? 'md' : /\.docx$/i.test(path) ? 'docx' : /\.pptx$/i.test(path) ? 'pptx' : /\.pdf$/i.test(path) ? 'pdf' : /\.(js|mjs|ts|jsx|tsx|json|py|css|html|cpp|h|rs|toml|ya?ml|sh|ps1|bat|cmd|c|go|java|cs)$/i.test(path) ? 'code' : /\.(png|jpe?g|gif|webp|svg|bmp)$/i.test(path) ? 'img' : /\.(mp4|webm|mov|mkv|avi|m4v)$/i.test(path) ? 'video' : /\.(mp3|wav|ogg|m4a|flac|aac|opus)$/i.test(path) ? 'audio' : 'text';
// 直接放的图片 / 视频 / 音频 (2026-11-07, 用户 11-05: "为什么工作台右侧功能栏的文件列表点击后看不了图片、视频、文档呢？"): 只认服务端 raw 肯给的后缀
// (wm/agent/workspace_http.py 的 RAW_TYPES; SVG、HTML 这类同源打开能跑代码的不给, 照旧当文字看)
const MEDIA = path => /\.(png|jpe?g|gif|webp|bmp)$/i.test(path) ? 'image' : /\.(mp4|webm)$/i.test(path) ? 'video' : /\.(mp3|wav|ogg|m4a|flac)$/i.test(path) ? 'audio' : '';
const badge = path => `<span class="wb-ftype t-${kind(path)}">${{ md: 'M↓', code: '</>', img: 'IMG', video: 'VID', audio: 'AUD', text: 'TXT', docx: 'DOC', pptx: 'PPT', pdf: 'PDF' }[kind(path)]}</span>`;

// ---- 代码着色 (2026-11-02 第二批, 学 Codex 的代码视图; 自己写的小分词, 不下载库) ----
// 一行一行着色; 跨行的块注释、三引号字符串、HTML 注释、模板字符串、Markdown 代码块记着状态。认不出的语言、太大的文件照原样显示
const KEYWORDS = {
  py: 'and|as|assert|async|await|break|class|continue|def|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|or|pass|raise|return|try|while|with|yield|None|True|False|self|match|case',
  js: 'async|await|break|case|catch|class|const|continue|debugger|default|delete|do|else|export|extends|finally|for|from|function|if|import|in|instanceof|let|new|of|return|static|super|switch|this|throw|try|typeof|var|void|while|with|yield|null|undefined|true|false|interface|type|enum|implements|public|private|protected|readonly|as',
  c: 'auto|bool|break|case|catch|char|class|const|continue|default|delete|do|double|else|enum|extern|false|float|fn|for|func|go|if|impl|import|inline|int|let|long|match|mod|mut|namespace|new|nullptr|override|package|private|protected|pub|public|return|self|short|signed|sizeof|static|string|struct|switch|template|this|throw|trait|true|try|type|typedef|typename|union|unsigned|use|using|var|virtual|void|volatile|while',
  sh: 'if|then|else|elif|fi|for|in|do|done|while|until|case|esac|function|return|local|export|echo|param|foreach|switch|try|catch|finally|begin|process|end|true|false|null',
};
const LANG_OF = { py: 'py', pyw: 'py', js: 'js', mjs: 'js', cjs: 'js', ts: 'js', tsx: 'js', jsx: 'js', json: 'json', css: 'css', html: 'html', htm: 'html', xml: 'html', svg: 'html', vue: 'html',
  md: 'md', markdown: 'md', sh: 'sh', bash: 'sh', ps1: 'sh', bat: 'sh', cmd: 'sh', yml: 'yaml', yaml: 'yaml', toml: 'yaml', ini: 'yaml', cfg: 'yaml', c: 'c', h: 'c', cpp: 'c', hpp: 'c', cs: 'c', java: 'c', go: 'c', rs: 'c' };
const STR = String.raw`"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'`;
const NUM = String.raw`\b(?:0x[\da-fA-F]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\b`;
const rule = pairs => ({ re: new RegExp(pairs.map(([, source]) => `(${source})`).join('|'), 'g'), cls: pairs.map(([cls]) => cls) });
const RULES = {
  py: rule([['s', String.raw`[rbfuRBFU]{0,2}"""[\s\S]*?(?:"""|$)`], ['s', String.raw`[rbfuRBFU]{0,2}'''[\s\S]*?(?:'''|$)`], ['c', '#.*'], ['s', `[rbfuRBFU]{0,2}(?:${STR})`],
    ['d', String.raw`@[\w.]+`], ['n', NUM], ['k', String.raw`\b(?:${KEYWORDS.py})\b`], ['f', String.raw`\b[A-Za-z_]\w*(?=\()`]]),
  js: rule([['c', String.raw`\/\*[\s\S]*?(?:\*\/|$)`], ['c', String.raw`\/\/.*`], ['s', String.raw`\`(?:\\.|[^\`\\])*(?:\`|$)`], ['s', STR], ['n', NUM],
    ['k', String.raw`\b(?:${KEYWORDS.js})\b`], ['f', String.raw`\b[A-Za-z_$][\w$]*(?=\()`]]),
  json: rule([['p', String.raw`"(?:\\.|[^"\\])*"(?=\s*:)`], ['s', String.raw`"(?:\\.|[^"\\])*"`], ['n', '-?' + NUM], ['k', String.raw`\b(?:true|false|null)\b`]]),
  css: rule([['c', String.raw`\/\*[\s\S]*?(?:\*\/|$)`], ['s', STR], ['k', String.raw`@[\w-]+|!important`], ['p', String.raw`[\w-]+(?=\s*:(?!:))`],
    ['n', String.raw`#[\da-fA-F]{3,8}\b|-?\d*\.?\d+(?:px|em|rem|%|s|ms|vh|vw|deg|fr|ch)?\b`]]),
  html: rule([['c', String.raw`<!--[\s\S]*?(?:-->|$)`], ['g', String.raw`<\/?[\w:-]+|\/?>`], ['a', String.raw`\s[\w:@.-]+(?==)`], ['s', STR]]),
  md: rule([['h', String.raw`^#{1,6}\s.*`], ['c', String.raw`^\s*>.*`], ['k', String.raw`^\s*(?:[-*+]|\d+[.)])\s`], ['s', '`[^`]+`'], ['a', String.raw`\[[^\]]+\]\([^)]+\)`], ['k', String.raw`\*\*[^*]+\*\*`]]),
  sh: rule([['c', String.raw`#.*|^\s*(?:rem|::)\b.*`], ['s', STR], ['a', String.raw`\$\{?[\w:]+\}?|%\w+%`], ['n', NUM], ['k', String.raw`\b(?:${KEYWORDS.sh})\b`]]),
  yaml: rule([['c', String.raw`^\s*[#;].*|\s#.*`], ['k', String.raw`^\s*\[.*\]`], ['p', String.raw`^\s*-?\s*[\w."-]+(?=\s*[:=])`], ['s', STR], ['n', NUM], ['k', String.raw`\b(?:true|false|null|yes|no|on|off)\b`]]),
  c: rule([['c', String.raw`\/\*[\s\S]*?(?:\*\/|$)`], ['c', String.raw`\/\/.*`], ['k', String.raw`^\s*#\s*\w+`], ['s', STR], ['n', NUM], ['k', String.raw`\b(?:${KEYWORDS.c})\b`], ['f', String.raw`\b[A-Za-z_]\w*(?=\()`]]),
};
const OPENERS = [['/*', '*/'], ['"""', '"""'], ["'''", "'''"], ['<!--', '-->'], ['`', '`']];
export function highlightLines(lines, path) {
  const lang = LANG_OF[String(path || '').toLowerCase().split('.').pop()];
  if (!lang || lines.length > 6000 || lines.reduce((n, line) => n + line.length, 0) > 300000) return null;
  const { re, cls } = RULES[lang];
  let open = '', fence = false;  // open: 跨行还没收尾的 (块注释 / 三引号 / HTML 注释 / 模板字符串) 的收尾记号
  const span = (k, text) => (text ? `<span class="tk-${k}">${E(text)}</span>` : '');
  const kindOf = closer => (closer === '-->' || closer === '*/' ? 'c' : 's');
  return lines.map(line => {
    if (lang === 'md' && /^\s*```/.test(line)) { fence = !fence; return span('s', line); }
    if (fence) return span('s', line);
    let out = '', from = 0;
    if (open) {
      const end = line.indexOf(open);
      if (end < 0) return span(kindOf(open), line);
      out = span(kindOf(open), line.slice(0, end + open.length));
      from = end + open.length;
      open = '';
    }
    re.lastIndex = from;
    let last = from, m;
    while ((m = re.exec(line))) {
      if (!m[0]) { re.lastIndex++; continue; }
      const k = cls[m.slice(1).findIndex(x => x !== undefined)];
      out += E(line.slice(last, m.index)) + span(k, m[0]);
      last = m.index + m[0].length;
      if (k === 'c' || k === 's') {  // 这一段到行尾都没收尾: 下一行接着是注释 / 字符串
        const body = m[0].replace(/^[rbfuRBFU]{0,2}/, ''), opener = OPENERS.find(([a]) => body.startsWith(a));
        if (opener && (body.length < opener[0].length + opener[1].length || !body.endsWith(opener[1]))) open = opener[1];
      }
    }
    return out + E(line.slice(last));
  });
}


/** Markdown → HTML, 每块带它在原文的起止行 (data-line / data-line-end), 选中后能说出"第几行" */
export function markdownWithLines(text) {
  const inline = raw => E(raw).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" data-file-link>$1</a>');
  const lines = String(text || '').replace(/\r\n?/g, '\n').split('\n'), out = [];
  const cells = line => line.trim().replace(/^\||\|$/g, '').split('|').map(cell => cell.trim());
  let para = [], paraStart = 0, list = '', code = null;
  const flush = end => { if (para.length) { out.push(`<p data-line="${paraStart}" data-line-end="${end}">${para.map(inline).join('<br>')}</p>`); para = []; } };
  const endList = () => { if (list) { out.push(`</${list}>`); list = ''; } };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i], n = i + 1;
    if (code) {
      if (/^\s*```/.test(line)) { out.push(`<pre data-line="${code.start}" data-line-end="${n}"><code>${code.rows.map(E).join('\n')}</code></pre>`); code = null; }
      else code.rows.push(line);
      continue;
    }
    if (/^\s*```/.test(line)) { flush(n - 1); endList(); code = { start: n, rows: [] }; continue; }
    const h = /^(#{1,6})\s+(.+)$/.exec(line), li = /^\s*(?:[-*+]|\d+[.)])\s+(.+)$/.exec(line);
    if (li) {
      flush(n - 1);
      const next = /^\s*\d/.test(line) ? 'ol' : 'ul';
      if (list !== next) { endList(); out.push(`<${next}>`); list = next; }
      const task = /^\[([ xX])\]\s+(.*)$/.exec(li[1]);  // 待办: - [ ] / - [x] (第二批, 学 Notion; 只读)
      out.push(task ? `<li class="task" data-line="${n}" data-line-end="${n}"><input type="checkbox" disabled ${task[1] === ' ' ? '' : 'checked'} aria-label="${task[1] === ' ' ? '没做' : '做完了'}">${inline(task[2])}</li>`
        : `<li data-line="${n}" data-line-end="${n}">${inline(li[1])}</li>`);
      continue;
    }
    endList();
    if (line.includes('|') && i + 1 < lines.length && cells(lines[i + 1]).every(cell => /^:?-{3,}:?$/.test(cell))) {
      flush(n - 1);
      const head = cells(line), start = n;
      const rows = [];
      i++;
      while (i + 1 < lines.length && lines[i + 1].trim() && lines[i + 1].includes('|')) rows.push(cells(lines[++i]));
      out.push(`<div class="markdown-table-scroll" data-line="${start}" data-line-end="${i + 1}"><table><thead><tr>${head.map(c => `<th>${inline(c)}</th>`).join('')}</tr></thead><tbody>${rows.map(r => `<tr>${head.map((_, j) => `<td>${inline(r[j] || '')}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`);
    } else if (h) { flush(n - 1); out.push(`<h${h[1].length} data-line="${n}" data-line-end="${n}">${inline(h[2])}</h${h[1].length}>`); }
    else if (/^>\s?/.test(line)) { flush(n - 1); out.push(`<blockquote data-line="${n}" data-line-end="${n}">${inline(line.replace(/^>\s?/, ''))}</blockquote>`); }
    else if (/^\s*(?:---+|\*\*\*+)\s*$/.test(line)) { flush(n - 1); out.push('<hr>'); }
    else if (!line.trim()) flush(n - 1);
    else { if (!para.length) paraStart = n; para.push(line); }
  }
  if (code) out.push(`<pre data-line="${code.start}" data-line-end="${lines.length}"><code>${code.rows.map(E).join('\n')}</code></pre>`);
  flush(lines.length); endList();
  return out.join('');
}

export function initFileView(container, { getSession, request, notify, revealFile, attachText, changesFor, onDocsChanged, onOpen, onQuoted, openDoc }) {
  const states = new Map();
  let visible = false, current = null, filter = '', finding = '', findOpen = false, treeHidden = false, selection = null, menuOpen = false, pendingReveal = null;
  container.classList.add('wb-files');
  const doc = () => current?.docs.get(current.active);
  const absolute = path => String(current?.root || '').replace(/[\\/]+$/, '') + (path ? '\\' + path.replace(/\//g, '\\') : '');
  function stateFor() {
    const session = getSession();
    if (!session?.id) return null;
    const key = session.id + '\n' + session.workspace;
    if (!states.has(key)) states.set(key, { key, id: session.id, root: session.workspace, dirs: new Map(), expanded: new Set(['.']), docs: new Map(), active: '', loading: new Set() });
    return states.get(key);
  }
  const api = (state, path, extra = {}) => request('/api/agent/workspace?' + new URLSearchParams({ action: 'files', id: state.id, path, ...extra }));
  const live = state => state === current && visible;

  async function directory(path = '.', more = false) {
    const state = current;
    if (!state || state.loading.has(path)) return;
    const old = state.dirs.get(path);
    state.loading.add(path);
    if (live(state)) renderTree();
    try {
      const data = await api(state, path === '.' ? '' : path, more ? { offset: old?.next_offset || 0 } : {});
      state.dirs.set(path, { ...data, entries: more ? [...(old?.entries || []), ...data.entries] : data.entries });
    } catch (error) { state.dirs.set(path, { ...old, error: error.message, entries: old?.entries || [] }); }
    finally { state.loading.delete(path); if (live(state)) { renderTree(); const d = doc(); if (d && MEDIA(d.path) && !d.fallback) renderMediaProps(d); } }
  }
  async function load(path, force = false) {
    const state = current;
    if (!state || !path) return;
    let item = state.docs.get(path);
    if (!item) { item = { path, name: base(path), mode: ['md', 'docx'].includes(kind(path)) ? 'preview' : 'source', limit: 120000 }; state.docs.set(path, item); }
    if (MEDIA(path)) {  // 图片 / 视频 / 音频: 不读文字, renderContent 直接放 raw 地址
      if (force) { item.mediaEl?.pause?.(); Object.assign(item, { mediaEl: null, info: null, fallback: false, unplayable: false, text: undefined }); }
      if (!item.fallback) { item.loading = false; item.error = ''; onDocsChanged?.(); if (live(state) && state.active === path) { renderHead(); renderContent(); } return; }
    }
    if (item.text !== undefined && !force) return;
    if (item.fetching) return;
    item.fetching = true; item.loading = true; item.error = '';
    if (live(state) && state.active === path) renderContent();
    try {
      // 第 7 块: Word 由服务端转成网页 (每段带段号); PPT 不直接放 (给"导出 PDF")
      const data = kind(path) === 'docx' ? await request('/api/agent/workspace?' + new URLSearchParams({ action: 'docx_preview', id: state.id, path }))
        : ['pptx', 'pdf'].includes(kind(path)) ? { kind: kind(path), text: '' } : await api(state, path);
      if (data.html !== undefined) data.text = textOf(data.html);
      Object.assign(item, data, { loading: false, error: '' });
    }
    catch (error) { item.loading = false; item.error = error.message; }
    finally { item.fetching = false; onDocsChanged?.(); if (live(state) && state.active === path) { renderHead(); renderContent(); } }
  }

  function textOf(html) {  // Word 预览的纯文字 (复制全文、加入对话用): 一段一行
    const box = document.createElement('div');
    box.innerHTML = html;
    box.querySelectorAll('p, h1, h2, h3, h4, h5, h6, li, tr').forEach(el => el.append('\n'));
    return box.textContent.replace(/\n{3,}/g, '\n\n').trim();
  }
  function crumbs(path) {
    const parts = String(path || '').split('/').filter(Boolean);
    return `<button type="button" data-crumb="." title="${E(current.root)}">${icon('folder', 14)}<span>${E(base(current.root))}</span></button>`
      + parts.map((part, i) => `<span class="wb-crumb-sep">›</span>${i === parts.length - 1 ? `<strong title="${E(path)}">${E(part)}</strong>` : `<button type="button" data-crumb="${E(parts.slice(0, i + 1).join('/'))}">${E(part)}</button>`}`).join('');
  }
  function render() {
    selection = null;
    if (!current) { container.innerHTML = '<div class="wb-empty-note wb-files-empty">先打开一个会话，再看它工作区里的文件。</div>'; return; }
    container.classList.toggle('tree-hidden', treeHidden);
    container.innerHTML = `<header class="wb-file-head"></header>
      <div class="wb-file-find" hidden>${icon('search', 14)}<input type="search" aria-label="在文件中查找" placeholder="在文件中查找"><span class="wb-find-count"></span><button type="button" data-f="find-close" aria-label="关闭查找">×</button></div>
      <div class="wb-file-sel" hidden><span></span><button type="button" data-f="quote">${icon('quote', 14)}引用到对话</button></div>
      <div class="wb-file-layout"><section class="wb-file-content" tabindex="0" aria-label="文件内容"></section>
        <aside class="wb-file-tree" aria-label="工作区文件"><header><span title="${E(current.root)}">${icon('folder', 14)}${E(base(current.root))}</span><button type="button" data-f="collapse" title="全部收起" aria-label="全部收起">${icon('minus', 14)}</button><button type="button" data-f="reload-tree" title="刷新" aria-label="刷新目录">${icon('refresh', 14)}</button></header>
          <label class="wb-file-filter">${icon('search', 14)}<input type="search" placeholder="筛选文件…" aria-label="筛选已展开的文件" value="${E(filter)}"></label><div class="wb-file-treebody" role="tree"></div><footer>Ctrl+P 按名字找整个工作区</footer></aside></div>`;
    renderHead(); renderTree(); renderContent();
  }
  function renderHead() {
    const head = container.querySelector('.wb-file-head');
    if (!head) return;
    const d = doc(), changes = d ? changesFor?.(d.path) || [] : [];
    const previewable = d && kind(d.path) === 'md';
    head.innerHTML = `<nav class="wb-crumbs" aria-label="文件路径">${crumbs(d?.path || current.browse || '')}</nav><span class="grow"></span>
      ${d && changes.length ? `<button type="button" class="wb-fbtn ${d.mode === 'changes' ? 'on' : ''}" data-f="changes" aria-pressed="${d.mode === 'changes'}" title="这个会话里她对这个文件的改动">${icon('diff', 15)}<span>显示更改</span><em class="wb-add">+${changes[0].add}</em><em class="wb-del">−${changes[0].del}</em></button>` : ''}
      ${d ? `${previewable ? `<button type="button" class="wb-fbtn ${d.mode === 'source' ? 'on' : ''}" data-f="${d.mode === 'source' ? 'preview' : 'source'}">${icon(d.mode === 'source' ? 'eye' : 'code', 15)}<span>${d.mode === 'source' ? '预览' : '查看源代码'}</span></button>` : ''}
        ${['docx', 'pptx'].includes(kind(d.path)) ? `<button type="button" class="wb-fbtn" data-f="export-pdf" title="经本机 Word / PowerPoint 另存成同名 PDF，开在面板里">${icon('file', 15)}<span>导出 PDF</span></button>` : ''}
        <div class="wb-fmenu-wrap"><button type="button" class="wb-fbtn" data-f="menu" aria-haspopup="menu" aria-expanded="${menuOpen}">${icon('open', 15)}<span>打开</span>${icon('down', 13)}</button>
          <div class="wb-pmenu wb-fmenu" role="menu" ${menuOpen ? '' : 'hidden'}>
            <button type="button" role="menuitem" data-f="reveal" ${revealFile ? '' : 'disabled'}>${icon('folder', 15)}<span>在文件夹中显示</span>${revealFile ? '' : '<small>只在桌面工作台里</small>'}</button>
            <button type="button" role="menuitem" data-f="editor" disabled title="需要新版桌宠宿主">${icon('code', 15)}<span>用编辑器打开</span><small>需要新版桌宠宿主</small></button>
            <hr><button type="button" role="menuitem" data-f="attach" ${d.text === undefined ? 'disabled' : ''}>${icon('quote', 15)}<span>整个文件加入对话</span>${d.text === undefined && MEDIA(d.path) ? '<small>图片和音视频不能当文字加入</small>' : ''}</button>
            <button type="button" role="menuitem" data-f="copy-path">${icon('file', 15)}<span>复制文件路径</span></button>
            <button type="button" role="menuitem" data-f="copy" ${d.text === undefined ? 'disabled' : ''}>${icon('file', 15)}<span>复制全文</span></button>
            <button type="button" role="menuitem" data-f="refresh">${icon('refresh', 15)}<span>重新读取</span></button></div></div>` : ''}
      <button type="button" class="wb-fbtn icon ${treeHidden ? '' : 'on'}" data-f="tree" title="${treeHidden ? '显示' : '收起'}文件树" aria-label="${treeHidden ? '显示' : '收起'}文件树" aria-pressed="${!treeHidden}">${icon('tree', 15)}</button>`;
  }
  function renderTree() {
    const target = container.querySelector('.wb-file-treebody');
    if (!target || !current) return;
    const search = filter.trim().toLowerCase();
    let shown = 0;
    const deep = (path, seen = new Set()) => { if (seen.has(path)) return false; seen.add(path); return current.dirs.get(path)?.entries.some(e => e.path.toLowerCase().includes(search) || e.directory && deep(e.path, seen)); };
    const rows = (path, depth) => {
      const data = current.dirs.get(path), loading = current.loading.has(path);
      if (!data) return `<div class="wb-tree-note" style="--d:${depth}">${loading ? '正在读取…' : ''}</div>`;
      return data.entries.map(entry => {
        const children = entry.directory && current.dirs.get(entry.path);
        if (search && !entry.path.toLowerCase().includes(search) && !(children && deep(entry.path))) return '';
        shown++;
        const expanded = current.expanded.has(entry.path) || (!!search && !!children);
        return `<div role="none"><button type="button" role="treeitem" style="--d:${Math.min(depth, 14)}" ${entry.directory ? `aria-expanded="${expanded}" data-folder="${E(entry.path)}"` : `data-open="${E(entry.path)}" aria-selected="${current.active === entry.path}"`} title="${E(entry.path)}${entry.directory ? '' : ' · ' + bytes(entry.size)}">
          <span class="wb-tree-chev ${expanded ? 'open' : ''}">${entry.directory ? icon('chevron', 12) : ''}</span>${entry.directory ? icon('folder', 15) : badge(entry.path)}<span class="wb-tree-name">${E(entry.name)}</span></button>${entry.directory && expanded ? `<div role="group">${rows(entry.path, depth + 1)}</div>` : ''}</div>`;
      }).join('') + (data.error ? `<div class="wb-tree-note err" style="--d:${depth}">${E(data.error)} <button type="button" data-retry="${E(path)}">重试</button></div>` : '')
        + (data.next_offset != null ? `<button type="button" class="wb-tree-more" style="--d:${depth}" data-more="${E(path)}">${loading ? '正在读取…' : `再列一些 · 共 ${data.total} 项`}</button>` : '')
        + (!data.entries.length && !data.error ? `<div class="wb-tree-note" style="--d:${depth}">空文件夹</div>` : '');
    };
    target.innerHTML = rows('.', 0) + (search && !shown ? '<p class="wb-tree-note">已展开的文件夹里没有，Ctrl+P 找整个工作区</p>' : '');
  }
  function sourceHtml(text, limit, path) {
    const lines = text.slice(0, limit).replace(/\r\n?/g, '\n').split('\n');
    if (lines.at(-1) === '' && lines.length > 1) lines.pop();
    const colored = highlightLines(lines, path);  // null: 认不出的语言或太大, 照原样
    // 每行一块 (行号一格、正文一格, 折行时正文对齐); 块和块之间不再夹换行, 不然会空一行
    return `<pre class="wb-src${colored ? ' hl' : ''}"><code>${lines.map((line, i) => `<span class="ln" data-line="${i + 1}" data-line-end="${i + 1}"><i>${i + 1}</i><span class="lc">${(colored ? colored[i] : E(line)) || ' '}</span></span>`).join('')}</code></pre>`;
  }
  function changesHtml(d) {
    const [change] = changesFor?.(d.path) || [];
    if (!change) return '<p class="wb-empty-note">这个会话里她没改过这个文件。</p>';
    const rows = hunks(diffLines(change.before, change.after).rows, 4);
    return `<div class="wb-diff wb-diff-inline"><p class="wb-diff-note">这个会话里的改动（${change.calls.length} 次）· <span class="wb-add">+${change.add}</span> <span class="wb-del">−${change.del}</span>${change.created ? ' · 这个文件是她新建的' : ''}</p>
      <div class="wb-diff-rows">${rows.map(r => r.t === 'gap' ? `<div class="wb-diff-gap">⋯ ${r.count} 行没有改动</div>` : `<div class="wb-diff-row ${r.t === '+' ? 'add' : r.t === '-' ? 'del' : ''}"><span class="n">${r.a ?? ''}</span><span class="n">${r.b ?? ''}</span><code>${E(r.text) || ' '}</code></div>`).join('')}</div></div>`;
  }
  // ---- 图片 / 视频 / 音频 (2026-11-07): 直接用 raw 地址放 (按后缀定类型、nosniff、视频能拖进度). 元素每个文件留一个, 重画时挂回去 (视频不会从头放);
  // 放不出来先探一下 raw: 不给 (服务端还是旧的) 就退回读文字那条路 (原来那句"这是二进制文件…"), 给了却放不了 (编码不支持、文件坏了) 说清楚、给"在文件夹中显示"
  const rawURL = (state, path) => '/api/agent/workspace?' + new URLSearchParams({ action: 'raw', id: state.id, path });
  function mediaEl(state, d) {
    if (d.mediaEl) return d.mediaEl;
    const type = MEDIA(d.path), el = document.createElement(type === 'image' ? 'img' : type);
    el.className = 'wb-media-el';
    if (type === 'image') {  // 点一下看原始大小 / 缩回窗口大小
      el.alt = d.name; el.decoding = 'async'; el.title = '点一下看原始大小';
      el.addEventListener('click', () => { const actual = el.classList.toggle('actual'); el.title = actual ? '点一下缩回窗口大小' : '点一下看原始大小'; });
    } else { el.controls = true; el.preload = 'metadata'; }
    el.addEventListener(type === 'image' ? 'load' : 'loadedmetadata', () => {
      if (d.mediaEl !== el) return;
      d.info = type === 'image' ? { w: el.naturalWidth, h: el.naturalHeight } : { w: el.videoWidth || 0, h: el.videoHeight || 0, duration: el.duration };
      if (live(state) && state.active === d.path) renderMediaProps(d);
    });
    el.addEventListener('error', () => mediaFailed(state, d, el));
    el.src = rawURL(state, d.path);
    return (d.mediaEl = el);
  }
  async function mediaFailed(state, d, el) {
    if (d.mediaEl !== el) return;
    d.loading = true;
    if (live(state) && state.active === d.path) renderContent();
    let status = 0;
    try {  // 只要头一个字节, 拿到状态就断开 (视频不整个下)
      const stop = new AbortController(), res = await fetch(rawURL(state, d.path), { headers: { Range: 'bytes=0-0' }, cache: 'no-store', signal: stop.signal });
      status = res.status; stop.abort();
    } catch { /* 连不上: 当作不给 */ }
    if (d.mediaEl !== el) return;
    if (status === 200 || status === 206) d.unplayable = true;
    else { d.fallback = true; d.mediaEl = null; try { Object.assign(d, await api(state, d.path), { error: '' }); } catch (error) { d.error = error.message; } }
    d.loading = false;
    if (live(state) && state.active === d.path) { renderHead(); renderContent(); }
  }
  const clock = s => { const t = Math.round(s), h = Math.floor(t / 3600), m = Math.floor(t / 60) % 60, sec = String(t % 60).padStart(2, '0'); return h ? `${h}:${String(m).padStart(2, '0')}:${sec}` : `${m}:${sec}`; };
  function mediaPropsHtml(d) {  // PNG 图片 · 大小 · 宽 × 高 · 时长 · 改动时间 · 只读 (大小、时间是文件树里读到的; 宽高、时长等元素读出来再补)
    const entry = [...current.dirs.values()].flatMap(dir => dir.entries || []).find(e => e.path === d.path), info = d.info || {};
    const when = entry?.modified ? new Date(entry.modified * 1000) : null;
    const parts = [`${(String(d.path).split('.').pop() || '').toUpperCase()} ${{ image: '图片', video: '视频', audio: '音频' }[MEDIA(d.path)]}`, entry ? bytes(entry.size || 0) : '',
      info.w && info.h ? `${info.w} × ${info.h}` : '', Number.isFinite(info.duration) && info.duration > 0 ? clock(info.duration) : '',
      when ? `改于 ${when.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}` : '', '只读'].filter(Boolean);
    return `<div class="wb-file-props">${parts.map(x => `<span>${E(x)}</span>`).join('')}</div>`;
  }
  function renderMediaProps(d) { const box = container.querySelector('.wb-file-content > .wb-file-props'); if (box && doc() === d) box.outerHTML = mediaPropsHtml(d); }
  const fileActions = () => `<div class="wb-file-actions"><button type="button" data-f="refresh">重新读取</button>${revealFile ? '<button type="button" data-f="reveal">在文件夹中显示</button>' : ''}</div>`;
  function renderContent() {
    const target = container.querySelector('.wb-file-content');
    if (!target) return;
    clearSelection();
    const d = doc();
    if (!d) {
      const recent = [...current.docs.values()].slice(-6).reverse();
      target.innerHTML = `<div class="wb-file-placeholder">${icon('file', 28)}<h3>选一个文件</h3><p>在右边的文件树里点开，或按 Ctrl+P 按名字找。</p>${recent.length ? `<div class="wb-file-recent">${recent.map(r => `<button type="button" data-open="${E(r.path)}">${badge(r.path)}<span>${E(r.path)}</span></button>`).join('')}</div>` : ''}</div>`;
      return;
    }
    if (d.loading) { target.innerHTML = '<p class="wb-empty-note">正在读取…</p>'; return; }
    if (kind(d.path) === 'pdf') { target.innerHTML = `<div class="wb-file-placeholder">${icon('file', 28)}<h3>PDF 在"原文"标签里看</h3><p>用浏览器内核自带的 PDF 阅读器打开，能翻页、缩放、查找。</p><button type="button" data-f="open-pdf">打开</button></div>`; return; }
    if (kind(d.path) === 'pptx') { target.innerHTML = `<div class="wb-file-placeholder">${icon('file', 28)}<h3>PPT 在这里不直接放</h3><p>导出成 PDF 就能在面板里一页一页看（用本机 PowerPoint 另存，样子和打开时一样）。</p><button type="button" data-f="export-pdf">导出 PDF 并打开</button></div>`; return; }
    if (MEDIA(d.path) && !d.fallback) {
      if (d.unplayable) { target.innerHTML = `<div class="wb-file-placeholder">${icon('file', 28)}<h3>放不出来</h3><p>文件读到了，但这里的浏览器内核放不了它（比如 H.265 编码的视频），也可能是文件坏了。可以在文件夹里用别的程序打开。</p><code>${E(d.path)}</code>${fileActions()}</div>`; return; }
      target.innerHTML = mediaPropsHtml(d) + `<div class="wb-media wb-media-${MEDIA(d.path)}"></div>`;
      target.querySelector('.wb-media').append(mediaEl(current, d));
      return;
    }
    if (d.error) { target.innerHTML = `<div class="wb-file-placeholder">${icon('file', 28)}<h3>暂时看不了</h3><p>${E(d.error)}</p><code>${E(d.path)}</code>${fileActions()}</div>`; return; }
    const text = d.text || '';
    if (d.mode === 'changes') target.innerHTML = changesHtml(d);
    else if (!text) target.innerHTML = '<p class="wb-empty-note">这个文件是空的。</p>';
    else if (d.html !== undefined) target.innerHTML = `<article class="wb-md wb-docx">${d.html}</article>`;  // 服务端只留了排版用的标签
    else target.innerHTML = d.mode === 'preview' ? `<article class="wb-md">${markdownWithLines(text.slice(0, d.limit))}</article>` : sourceHtml(text, d.limit, d.path);
    if (d.mode !== 'changes') target.insertAdjacentHTML('afterbegin', propsHtml(d, text));
    if (text.length > d.limit && d.mode !== 'changes') target.insertAdjacentHTML('beforeend', `<div class="wb-file-more">只显示了前 ${d.limit.toLocaleString()} / ${text.length.toLocaleString()} 字 <button type="button" data-f="more">接着显示</button></div>`);

    if (finding) highlight();
    if (pendingReveal?.path === d.path && d.mode !== 'changes') { (pendingReveal.unit === 'p' ? flashParas : flashLines)(pendingReveal.start, pendingReveal.end); pendingReveal = null; }
  }
  // 文件顶上一行属性 (第二批, 学 Notion 的属性行): 类型 · 大小 · 行数 · 编码 · 改动时间 (文件树里读到过才有); 她改了几行写在头部的"显示更改"上
  function propsHtml(d, text) {
    const entry = [...current.dirs.values()].flatMap(dir => dir.entries || []).find(e => e.path === d.path);
    const modified = entry?.modified || d.modified, when = modified ? new Date(modified * 1000) : null, docx = d.html !== undefined;
    const parts = [docx ? 'Word 文档' : d.mode === 'preview' ? 'Markdown' : kind(d.path) === 'code' ? (String(d.path).split('.').pop() || '代码').toUpperCase() : '文本', bytes(d.size || 0),
      docx ? `${Number(d.paragraphs || 0).toLocaleString()} 段` : `${text.split('\n').length.toLocaleString()} 行`, d.encoding || '', when ? `改于 ${when.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })}` : '', '只读'].filter(Boolean);
    return `<div class="wb-file-props">${parts.map(x => `<span>${E(x)}</span>`).join('')}</div>`;
  }
  // 回到引用的地方: 那几行闪一下、滚到中间 (Markdown 预览里是包含那几行的段落)
  function flashLines(start, end = start) {
    const hits = [...container.querySelectorAll('.wb-file-content [data-line]')].filter(el => Number(el.dataset.line) <= end && Number(el.dataset.lineEnd || el.dataset.line) >= start);
    hits.forEach(el => { el.classList.remove('wb-flash'); void el.offsetWidth; el.classList.add('wb-flash'); });
    hits[0]?.scrollIntoView({ block: 'center' });
  }
  function flashParas(start, end = start) {  // Word 预览: 第 start–end 段闪一下、滚到中间
    const hits = [...container.querySelectorAll('.wb-file-content [data-p]')].filter(el => Number(el.dataset.p) >= start && Number(el.dataset.p) <= end);
    hits.forEach(el => { el.classList.remove('wb-flash'); void el.offsetWidth; el.classList.add('wb-flash'); });
    hits[0]?.scrollIntoView({ block: 'center' });
  }
  function highlight() {
    const content = container.querySelector('.wb-file-content'), counter = container.querySelector('.wb-file-find .wb-find-count');
    if (!content) return;
    content.querySelectorAll('mark.wb-hit').forEach(mark => mark.replaceWith(document.createTextNode(mark.textContent)));
    content.normalize();
    const needle = finding.trim().toLowerCase();
    let count = 0, first = null;
    if (needle) {
      const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT), nodes = [];
      while (walker.nextNode()) if (!walker.currentNode.parentElement.closest('.wb-file-more, .wb-file-status, i')) nodes.push(walker.currentNode);
      for (const node of nodes) {
        const value = node.textContent, lower = value.toLowerCase();
        let at = lower.indexOf(needle), from = 0;
        if (at < 0) continue;
        const fragment = document.createDocumentFragment();
        while (at >= 0 && count < 500) {
          fragment.append(value.slice(from, at));
          const mark = document.createElement('mark');
          mark.className = 'wb-hit';
          mark.textContent = value.slice(at, at + needle.length);
          first ||= mark;
          fragment.append(mark);
          count++;
          from = at + needle.length;
          at = lower.indexOf(needle, from);
        }
        fragment.append(value.slice(from));
        node.replaceWith(fragment);
      }
    }
    if (counter) counter.textContent = needle ? `${count >= 500 ? '500+' : count} 处` : '';
    first?.scrollIntoView({ block: 'center' });
  }

  // ---- 选中几行 → 引用 (带行号) ----
  function lineOf(node, end = false) {
    const el = (node?.nodeType === 1 ? node : node?.parentElement)?.closest?.('[data-line]');
    return el ? Number(end ? el.dataset.lineEnd || el.dataset.line : el.dataset.line) : 0;
  }
  const paraOf = node => Number((node?.nodeType === 1 ? node : node?.parentElement)?.closest?.('[data-p]')?.dataset.p) || 0;  // Word 预览: 第几段
  function clearSelection() { selection = null; const bar = container.querySelector('.wb-file-sel'); if (bar) bar.hidden = true; }
  function capture() {
    const d = doc(), content = container.querySelector('.wb-file-content');
    if (!visible || !d || d.text === undefined || !content || d.mode === 'changes') { clearSelection(); return; }
    const sel = getSelection();
    if (!sel?.rangeCount || sel.isCollapsed) { clearSelection(); return; }
    const range = sel.getRangeAt(0);
    if (!content.contains(range.startContainer) || !content.contains(range.endContainer)) { clearSelection(); return; }
    const text = sel.toString().trim();
    if (!text) { clearSelection(); return; }
    const unit = d.html !== undefined ? 'p' : 'line';  // Word 预览按段 (和她 docx_read / docx_edit 的段号一致), 别的按行
    let start = unit === 'p' ? paraOf(range.startContainer) : lineOf(range.startContainer), end = unit === 'p' ? paraOf(range.endContainer) : lineOf(range.endContainer, true);
    if (start && end && end < start) [start, end] = [end, start];
    selection = { key: current.key, path: d.path, text: d.mode === 'source' ? sourceText(range) : text, start, end, unit };
    const bar = container.querySelector('.wb-file-sel');
    bar.hidden = false;
    bar.querySelector('span').textContent = start ? `已选第 ${start}${end && end !== start ? `–${end}` : ''} ${unit === 'p' ? '段' : '行'}` : `已选 ${text.length} 字`;
  }
  function sourceText(range) {  // 源码模式: 引用不带左边的行号; 跨行的一行一行接上换行 (行和行之间页面里没有换行字符)
    const box = document.createElement('div');
    box.append(range.cloneContents());
    box.querySelectorAll('i').forEach(i => i.remove());
    const rows = [...box.querySelectorAll('.ln')];
    return (rows.length > 1 ? rows.map(row => row.textContent).join('\n') : box.textContent).trim();
  }
  async function quote() {
    const d = doc();
    if (!selection || selection.key !== current?.key || selection.path !== d?.path) {
      if (d?.text !== undefined) { notify('先在文件里选中要引用的几行'); }
      return;
    }
    const where = selection.start ? `第 ${selection.start}${selection.end && selection.end !== selection.start ? `–${selection.end}` : ''} ${selection.unit === 'p' ? '段' : '行'}` : '选段';
    const picked = { name: `${d.name} · ${where}`, path: d.path, start: selection.start, end: selection.end || selection.start, unit: selection.unit };
    await attachText({ name: picked.name, text: selection.text, source: `${absolute(d.path)}（${where}）`, targetId: current.id });
    if (picked.start) onQuoted?.(picked);
    getSelection()?.removeAllRanges();
    clearSelection();
    notify(`${d.name} 的${where}已引用到对话`);
  }
  document.addEventListener('selectionchange', () => { if (visible) capture(); });
  // 面板窄了 (拖窄、窗口变小): 文件树先收起来, 点按钮时浮在正文上面, 不挤正文
  new ResizeObserver(() => {
    const narrow = container.clientWidth > 0 && container.clientWidth < 560;
    if (narrow === container.classList.contains('narrow')) return;
    container.classList.toggle('narrow', narrow);
    if (narrow && !treeHidden) { treeHidden = true; container.classList.add('tree-hidden'); renderHead(); }
  }).observe(container);
  container.addEventListener('pointerdown', event => { if (event.target.closest('[data-f="quote"]')) event.preventDefault(); });

  function closeMenu() { if (!menuOpen) return; menuOpen = false; container.querySelector('.wb-fmenu')?.setAttribute('hidden', ''); container.querySelector('[data-f="menu"]')?.setAttribute('aria-expanded', 'false'); }
  container.addEventListener('input', event => {
    if (event.target.matches('.wb-file-filter input')) { filter = event.target.value; renderTree(); }
    if (event.target.matches('.wb-file-find input')) { finding = event.target.value; highlight(); }
  });
  container.addEventListener('click', async event => {
    const link = event.target.closest('a[data-file-link]');
    if (link) { event.preventDefault(); dispatchEvent(new CustomEvent('wm-open-browser', { detail: { url: link.href } })); return; }
    const b = event.target.closest('button');
    if (!b || b.disabled || !current) return;
    const d = doc(), f = b.dataset.f;
    if (f !== 'menu' && b.closest('.wb-fmenu') === null) closeMenu();
    try {
      if (b.dataset.open) { if (onOpen) onOpen(b.dataset.open); else openPath(b.dataset.open); }  // 一个文件一个标签: 交给面板开
      else if (b.dataset.folder) { const p = b.dataset.folder; if (current.expanded.has(p)) current.expanded.delete(p); else { current.expanded.add(p); if (!current.dirs.has(p)) directory(p); } renderTree(); }
      else if (b.dataset.crumb) { const p = b.dataset.crumb; treeHidden = false; container.classList.remove('tree-hidden'); if (p !== '.') { const parts = p.split('/'); for (let i = 1; i <= parts.length; i++) current.expanded.add(parts.slice(0, i).join('/')); } filter = ''; render(); if (!current.dirs.has(p)) await directory(p); }
      else if (b.dataset.retry) await directory(b.dataset.retry);
      else if (b.dataset.more) await directory(b.dataset.more, true);
      else if (f === 'menu') { menuOpen = !menuOpen; renderHead(); }
      else if (f === 'tree') { treeHidden = !treeHidden; container.classList.toggle('tree-hidden', treeHidden); renderHead(); }
      else if (f === 'collapse') { current.expanded = new Set(['.']); renderTree(); }
      else if (f === 'reload-tree') { current.dirs.clear(); await directory('.'); }
      else if (f === 'changes' && d) { d.mode = d.mode === 'changes' ? (kind(d.path) === 'md' ? 'preview' : 'source') : 'changes'; renderHead(); renderContent(); }
      else if ((f === 'source' || f === 'preview') && d) { d.mode = f; renderHead(); renderContent(); }
      else if (f === 'refresh' && d) { closeMenu(); await load(d.path, true); }
      else if (f === 'more' && d) { d.limit += 120000; const box = container.querySelector('.wb-file-content'), top = box.scrollTop; renderContent(); box.scrollTop = top; }
      else if (f === 'copy' && d) { closeMenu(); await navigator.clipboard.writeText(d.text || ''); notify('已复制全文'); }
      else if (f === 'copy-path' && d) { closeMenu(); await navigator.clipboard.writeText(absolute(d.path)); notify('已复制文件路径'); }
      else if (f === 'attach' && d) { closeMenu(); await attachText({ name: `${d.name} · 全文`, text: d.text || '', source: absolute(d.path), targetId: current.id }); notify('文件已加入对话'); }
      else if (f === 'reveal' && d) { closeMenu(); await revealFile?.({ id: current.id, path: d.path, absolutePath: absolute(d.path) }); }
      else if (f === 'quote') await quote();
      else if (f === 'export-pdf' && d) {  // 第 7 块: 本机 Word / PowerPoint 另存同名 PDF, 开在面板的"原文"标签里
        notify('正在用本机 Office 导出 PDF…');
        const out = await request('/api/agent/workspace', { action: 'office_export', id: current.id, path: d.path });
        notify(`导出了 ${out.path}`);
        openDoc?.('/api/agent/workspace?' + new URLSearchParams({ action: 'raw', id: current.id, path: out.path }), base(out.path));
      }
      else if (f === 'open-pdf' && d) openDoc?.('/api/agent/workspace?' + new URLSearchParams({ action: 'raw', id: current.id, path: d.path }), d.name);
      else if (f === 'find-close') showFind(false);
    } catch (error) { notify(error.message); }
  });
  document.addEventListener('pointerdown', event => { if (menuOpen && !event.target.closest('.wb-fmenu-wrap')) closeMenu(); }, true);
  container.addEventListener('keydown', event => {
    if (event.key === 'Escape') { if (menuOpen) closeMenu(); else if (findOpen) showFind(false); else clearSelection(); }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'f' && doc()) { event.preventDefault(); showFind(true); }
  });
  function showFind(open) {
    findOpen = open;
    const bar = container.querySelector('.wb-file-find');
    if (!bar) return;
    bar.hidden = !open;
    if (open) bar.querySelector('input').focus();
    else { finding = ''; bar.querySelector('input').value = ''; highlight(); }
  }
  function openPath(path) {
    if (!current) return;
    current.active = path;
    if (!path) { render(); return; }
    render();
    load(path);
    if (container.clientWidth < 560) { treeHidden = true; container.classList.add('tree-hidden'); renderHead(); }
  }
  function ensure() {
    const next = stateFor();
    if (next !== current) { current = next; filter = ''; finding = ''; findOpen = false; }
    if (current && !current.dirs.has('.')) directory('.');
  }
  return {
    show(path) {
      visible = true;
      ensure();
      if (!current) { render(); return; }
      const parts = String(path || '').split('/');
      for (let i = 1; i < parts.length; i++) current.expanded.add(parts.slice(0, i).join('/'));
      for (let i = 1; i < parts.length; i++) { const dir = parts.slice(0, i).join('/'); if (!current.dirs.has(dir)) directory(dir); }
      openPath(path || '');
    },
    remove(path) { if (!current) return; current.docs.get(path)?.mediaEl?.pause?.(); current.docs.delete(path); if (current.active === path) current.active = ''; },
    info(path) { return current?.docs.get(path); },
    setVisible(value) { visible = value; if (!value) { clearSelection(); closeMenu(); for (const d of current?.docs.values() || []) d.mediaEl?.pause?.(); } },
    sessionChanged() { if (visible) ensure(); },
    refreshOpen() {  // 撤销了她的改动: 打开过的文件都重读 (图片 / 音视频丢掉留着的播放器, 切过去时重新读)
      if (!current) return;
      for (const d of current.docs.values()) { d.text = undefined; if (d.mediaEl) { d.mediaEl.pause?.(); Object.assign(d, { mediaEl: null, info: null, fallback: false, unplayable: false }); } }
      current.dirs.clear(); if (visible) { directory('.'); if (current.active) load(current.active, true); else render(); } },
    quote,
    reveal(path, start, end, unit = 'line') {  // 面板随后会打开这个文件的标签 (show → openPath 整页重画, 那时再闪); 正看着"显示更改"的先换回正文 (unit: Word 是 'p' 段)
      pendingReveal = { path, start, end, unit };  // 这里先画一遍会把它用掉, 紧接着的重画又把闪的那一下抹了 (文件正开着时)
      const d = current?.docs.get(path);
      if (d?.mode === 'changes') d.mode = ['md', 'docx'].includes(kind(path)) ? 'preview' : 'source';
    },
  };
}
