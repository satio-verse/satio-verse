// 行级 diff 和"一轮改了哪些文件" (2026-11-01 工作台照 Codex 改版): 对话里"已编辑 N 个文件 +x −y"、右侧"变更"标签、
// 文件查看的"显示更改"共用这一份. 纯函数, 不碰页面 (tests/js/workbench-diff.test.mjs).

const MAX_CELLS = 3_000_000;  // 中间那段 LCS 表最多这么多格 (约 1700 × 1700 行), 再大就整段算替换

export function splitLines(text) {
  const value = String(text ?? '').replace(/\r\n?/g, '\n');
  if (!value) return [];
  const lines = value.split('\n');
  if (lines.at(-1) === '') lines.pop();
  return lines;
}

function middle(a, b) {
  const n = a.length, m = b.length;
  if (!n) return b.map(text => ({ t: '+', text }));
  if (!m) return a.map(text => ({ t: '-', text }));
  if ((n + 1) * (m + 1) > MAX_CELLS) return [...a.map(text => ({ t: '-', text })), ...b.map(text => ({ t: '+', text }))];
  const w = m + 1, table = new Uint16Array((n + 1) * w);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      table[i * w + j] = a[i] === b[j] ? table[(i + 1) * w + j + 1] + 1 : Math.max(table[(i + 1) * w + j], table[i * w + j + 1]);
    }
  }
  const ops = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { ops.push({ t: '=', text: a[i] }); i++; j++; }
    else if (table[(i + 1) * w + j] >= table[i * w + j + 1]) ops.push({ t: '-', text: a[i++] });  // 同样长时先删后加 (和 git 一样)
    else ops.push({ t: '+', text: b[j++] });
  }
  while (i < n) ops.push({ t: '-', text: a[i++] });
  while (j < m) ops.push({ t: '+', text: b[j++] });
  return ops;
}

/** rows: 每行 {t: ' ' | '-' | '+', a: 改前行号或 null, b: 改后行号或 null, text} */
export function diffLines(before = '', after = '') {
  const a = splitLines(before), b = splitLines(after);
  let start = 0;
  while (start < a.length && start < b.length && a[start] === b[start]) start++;
  let endA = a.length, endB = b.length;
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB--; }
  const rows = [];
  for (let i = 0; i < start; i++) rows.push({ t: ' ', a: i + 1, b: i + 1, text: a[i] });
  let ia = start, ib = start, add = 0, del = 0;
  for (const op of middle(a.slice(start, endA), b.slice(start, endB))) {
    if (op.t === '=') rows.push({ t: ' ', a: ++ia, b: ++ib, text: op.text });
    else if (op.t === '-') { rows.push({ t: '-', a: ++ia, b: null, text: op.text }); del++; }
    else { rows.push({ t: '+', a: null, b: ++ib, text: op.text }); add++; }
  }
  for (let i = endA, j = endB; i < a.length; i++, j++) rows.push({ t: ' ', a: i + 1, b: j + 1, text: a[i] });
  return { rows, add, del };
}

/** 改动附近留 context 行, 其余收成 {t: 'gap', count}. 没有改动时返回 [] */
export function hunks(rows, context = 3) {
  const keep = new Uint8Array(rows.length);
  rows.forEach((row, index) => {
    if (row.t === ' ') return;
    for (let k = Math.max(0, index - context); k <= Math.min(rows.length - 1, index + context); k++) keep[k] = 1;
  });
  if (!keep.some(Boolean)) return [];
  const out = [];
  for (let index = 0; index < rows.length;) {
    if (keep[index]) { out.push(rows[index++]); continue; }
    let end = index;
    while (end < rows.length && !keep[end]) end++;
    out.push({ t: 'gap', count: end - index, a: rows[index].a, b: rows[index].b });
    index = end;
  }
  return out;
}

const fileKey = path => String(path || '').replace(/\\/g, '/').toLowerCase();

/** 显示用的相对路径: 在工作区里就去掉工作区前缀 (不分大小写, 两种斜杠都认) */
export function relativePath(path, workspace = '') {
  const value = String(path || '').replace(/\\/g, '/');
  const root = String(workspace || '').replace(/\\/g, '/').replace(/\/+$/, '');
  return root && value.toLowerCase().startsWith(root.toLowerCase() + '/') ? value.slice(root.length + 1) : value;
}

/** 一段事件里改过的文件: 同一个文件改了几次用第一次的改前、最后一次的改后; calls 是这些工具调用的 id (撤销用) */
export function collectChanges(events, workspace = '') {
  const files = new Map();
  for (const event of events || []) {
    if (event?.type !== 'tool_result' || !event.change || typeof event.change.after !== 'string') continue;
    const key = fileKey(event.change.path);
    const entry = files.get(key) || { path: event.change.path, before: event.change.before ?? '', calls: [] };
    entry.after = event.change.after;
    entry.calls.push(event.id);
    files.set(key, entry);
  }
  return [...files.values()].map(entry => {
    const { add, del } = diffLines(entry.before, entry.after);
    return { ...entry, name: relativePath(entry.path, workspace), created: entry.before === '', add, del };
  });
}

/** 把会话事件切成一轮一轮: 从一条你说的话 (中途补充不算新一轮) 到 done; 还没做完的那轮 end = -1 */
export function turnRanges(events) {
  const turns = [];
  let current = null;
  (events || []).forEach((event, index) => {
    if (event?.type === 'user' && !event.steer) {
      if (current) current.end = index - 1;
      current = { start: index, end: -1 };
      turns.push(current);
    } else if (event?.type === 'done' && current) {
      current.end = index;
      current = null;
    }
  });
  return turns;
}

/** 回复里的网址 (去掉结尾的标点, 去重, 最多 n 个) */
export function extractLinks(text, n = 4) {
  const seen = new Set(), out = [];
  // 网址后面紧跟中文 (没空格) 很常见: 碰到中文字、全角标点就算结束
  for (const match of String(text || '').matchAll(/https?:\/\/[^\s<>"'`)\]⺀-鿿　-〿＀-￯]+/g)) {
    const url = match[0].replace(/[.,;:!?。，；：！？、]+$/, '');
    if (seen.has(url)) continue;
    seen.add(url);
    out.push(url);
    if (out.length >= n) break;
  }
  return out;
}
