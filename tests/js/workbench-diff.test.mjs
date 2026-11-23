import test from 'node:test';
import assert from 'node:assert/strict';
import { splitLines, diffLines, hunks, collectChanges, relativePath, turnRanges, extractLinks } from '../../desktop/web/workbench-diff.js';

test('splitLines normalises CRLF and drops only the final empty line', () => {
  assert.deepEqual(splitLines('a\r\nb\n'), ['a', 'b']);
  assert.deepEqual(splitLines('a\n\n'), ['a', '']);
  assert.deepEqual(splitLines(''), []);
  assert.deepEqual(splitLines(null), []);
});

test('diffLines counts real line changes with line numbers on both sides', () => {
  const { rows, add, del } = diffLines('a\nb\nc\nd\n', 'a\nB\nc\nd\ne\n');
  assert.equal(add, 2);
  assert.equal(del, 1);
  assert.deepEqual(rows.map(r => r.t + r.text), [' a', '-b', '+B', ' c', ' d', '+e']);
  assert.deepEqual(rows.find(r => r.text === 'B'), { t: '+', a: null, b: 2, text: 'B' });
  assert.deepEqual(rows.find(r => r.text === 'd'), { t: ' ', a: 4, b: 4, text: 'd' });
  // A moved block is not counted as "everything changed" (the old prefix/suffix trim did that).
  const moved = diffLines('x\n1\n2\n3\ny\n', 'x\ny\n1\n2\n3\n');
  assert.ok(moved.add + moved.del <= 2, `moved block cost ${moved.add + moved.del}`);
  assert.deepEqual(diffLines('', 'new\nfile\n'), { rows: [{ t: '+', a: null, b: 1, text: 'new' }, { t: '+', a: null, b: 2, text: 'file' }], add: 2, del: 0 });
  assert.equal(diffLines('same\n', 'same\r\n').add, 0);
});

test('huge middles fall back to block replacement instead of a giant table', () => {
  const a = Array.from({ length: 2500 }, (_, i) => 'a' + i).join('\n');
  const b = Array.from({ length: 2500 }, (_, i) => 'b' + i).join('\n');
  const { add, del } = diffLines(a, b);
  assert.equal(add, 2500);
  assert.equal(del, 2500);
});

test('hunks keep context around changes and collapse the rest', () => {
  const before = Array.from({ length: 20 }, (_, i) => 'l' + i).join('\n');
  const after = before.replace('l10', 'L10');
  const out = hunks(diffLines(before, after).rows, 2);
  assert.deepEqual(out.map(r => r.t === 'gap' ? `gap${r.count}` : r.t + r.text), ['gap8', ' l8', ' l9', '-l10', '+L10', ' l11', ' l12', 'gap7']);
  assert.deepEqual(hunks(diffLines('a', 'a').rows), []);
});

test('collectChanges merges several edits of one file and keeps undo ids', () => {
  const events = [
    { type: 'tool', id: 't1' },
    { type: 'tool_result', id: 't1', ok: true, change: { path: 'G:\\ws\\src\\a.py', before: 'v1\n', after: 'v2\n' } },
    { type: 'tool_result', id: 't2', ok: true, change: { path: 'G:\\ws\\notes.md', before: '', after: '# hi\n' } },
    { type: 'tool_result', id: 't3', ok: true, change: { path: 'g:/ws/src/A.py', before: 'v2\n', after: 'v3\nmore\n' } },
    { type: 'tool_result', id: 't4', ok: true, text: 'no change recorded' },
  ];
  const files = collectChanges(events, 'G:\\ws');
  assert.equal(files.length, 2);
  assert.deepEqual(files[0].calls, ['t1', 't3']);
  assert.equal(files[0].name, 'src/a.py');
  assert.equal(files[0].before, 'v1\n');
  assert.equal(files[0].after, 'v3\nmore\n');
  assert.deepEqual([files[0].add, files[0].del], [2, 1]);
  assert.equal(files[1].created, true);
  assert.equal(relativePath('D:/other/x.txt', 'G:\\ws'), 'D:/other/x.txt');
});

test('turnRanges treats steer messages as part of the running turn', () => {
  const events = [{ type: 'user' }, { type: 'tool' }, { type: 'user', steer: true }, { type: 'assistant' }, { type: 'done' },
    { type: 'user' }, { type: 'assistant' }];
  assert.deepEqual(turnRanges(events), [{ start: 0, end: 4 }, { start: 5, end: -1 }]);
});

test('extractLinks trims punctuation and de-duplicates', () => {
  assert.deepEqual(extractLinks('看 https://a.com/x。还有 (https://b.org/y), https://a.com/x'), ['https://a.com/x', 'https://b.org/y']);
});
