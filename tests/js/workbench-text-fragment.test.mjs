// 网页引用回到原处 (第 7 块): 网址后面接 #:~:text=…, 浏览器内核自己滚到那几句 (desktop/web/workbench-browser.js 的 textFragmentURL)
import test from 'node:test';
import assert from 'node:assert/strict';
import { textFragmentURL } from '../../desktop/web/workbench-browser.js';

test('a short quote is one text directive; the old #anchor goes, and - , & are escaped', () => {
  assert.equal(textFragmentURL('https://a.example/x?q=1#old', 'Task groups - combine,  tasks & more'),
    'https://a.example/x?q=1#:~:text=Task%20groups%20%2D%20combine%2C%20tasks%20%26%20more');
});

test('a long quote uses its first sentence as the start and its tail as the end', () => {
  const text = '矩阵乘法不满足交换律。' + '中间的话很长'.repeat(20) + '最后一句是结论。';
  const url = textFragmentURL('https://a.example/p', text);
  const [start, end] = decodeURIComponent(url.split('#:~:text=')[1]).split(',');
  assert.equal(start, '矩阵乘法不满足交换律。');
  assert.ok(text.endsWith(end) && end.endsWith('最后一句是结论。'));
});

test('nothing quoted keeps just the page', () => {
  assert.equal(textFragmentURL('https://a.example/p#h', '   '), 'https://a.example/p');
});
