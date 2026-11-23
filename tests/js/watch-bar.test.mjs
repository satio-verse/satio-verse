// 桌宠的陪看提示条 (desktop/web/watch-bar.js): 陪看写"她在陪你看 《标题》", 屏幕陪看 (2026-11-08, mode: "screen") 写
// "她在看你的屏幕 《标题》 · 程序" 加一行小字; 她自己决定不看了 (切走好一阵、看太久) 先写为什么, 停一会儿再收.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { watchView, watchEndNote, WatchBar, WATCH_END_HOLD_MS } from '../../desktop/web/watch-bar.js';

test('screen watching: "她在看你的屏幕", the title once it comes, the app and "只看前台那个窗口" in the small second line', () => {
  assert.deepEqual(watchView({ active: true, mode: 'screen', source: 'screen' }),
    { kind: 'screen', lead: '她在看你的屏幕', what: '', hint: '只看前台那个窗口' });
  assert.deepEqual(watchView({ active: true, mode: 'screen', source: 'screen', title: '胖猫跳舞', app: 'Chrome' }),
    { kind: 'screen', lead: '她在看你的屏幕', what: '《胖猫跳舞》', hint: 'Chrome · 只看前台那个窗口' });
  // B 站客户端的标题一直是"哔哩哔哩", 服务端给空串: 第一行写程序, 第二行不重复
  assert.deepEqual(watchView({ active: true, mode: 'screen', title: '', app: '哔哩哔哩' }),
    { kind: 'screen', lead: '她在看你的屏幕', what: '哔哩哔哩', hint: '只看前台那个窗口' });
  assert.deepEqual(watchView({ active: true, mode: 'screen', title: '只有标题' }),
    { kind: 'screen', lead: '她在看你的屏幕', what: '《只有标题》', hint: '只看前台那个窗口' });
});

test('video watching is written as before (title, BV, short link without https://, file name of a local video)', () => {
  const what = (m) => watchView({ active: true, ...m }).what;
  assert.deepEqual(watchView({ active: true, title: '胖猫跳舞' }), { kind: 'video', lead: '她在陪你看', what: '《胖猫跳舞》', hint: '' });
  assert.equal(what({ source: 'BV1GJ411x7h7' }), 'BV1GJ411x7h7');
  assert.equal(what({ source: 'https://b23.tv/abc' }), 'b23.tv/abc');
  assert.equal(what({ source: 'D:\\视频\\第十二集.mp4' }), '第十二集.mp4');
});

test('only an end she decided herself keeps the bar a moment with the reason; the user stopping, a switch, a disconnect hide it at once', () => {
  assert.equal(watchEndNote({ active: false, mode: 'screen', reason: 'away' }), '你切走好一阵了，她先不看了');
  assert.equal(watchEndNote({ active: false, mode: 'screen', reason: 'max_session' }), '看了很久了，她先歇会儿');
  for (const reason of ['user', 'switch', 'disconnect', 'finished', '']) assert.equal(watchEndNote({ active: false, mode: 'screen', reason }), '');
  assert.equal(watchEndNote({ active: false, reason: 'away' }), '', 'video watching keeps its old behaviour');
  assert.equal(watchEndNote({ active: true, mode: 'screen', reason: 'away' }), '');
});

// 假的对话栏和计时器: 记下提示条显示的样子
function fake() {
  const timers = new Map();
  let seq = 0, refreshed = 0;
  const shell = {
    shown: false, ending: '', calls: [],
    watch(on, what = '', opts = {}) { this.shown = on; this.ending = ''; this.calls.push(['watch', on, what, opts.lead || '']); },
    watchEnding(note) { if (!this.shown) return false; this.ending = note; this.calls.push(['ending', note]); return true; },
  };
  const bar = new WatchBar(shell, { refresh: () => { refreshed++; }, holdMs: 6000,
    setTimer: (fn, ms) => { timers.set(++seq, { fn, ms }); return seq; }, clearTimer: (id) => timers.delete(id) });
  const fire = () => { for (const [id, { fn }] of [...timers]) { timers.delete(id); fn(); } };
  return { shell, bar, timers, fire, refreshed: () => refreshed };
}

test('WatchBar: start, a new title, then "away" shows the reason and hides after the hold (and redraws the click mask)', () => {
  const f = fake();
  f.bar.update({ type: 'watch_state', active: true, mode: 'screen', source: 'screen' });
  assert.deepEqual(f.shell.calls.at(-1), ['watch', true, '', '她在看你的屏幕']);
  f.bar.update({ type: 'watch_state', active: true, mode: 'screen', source: 'screen', title: '胖猫跳舞', app: 'Chrome' });
  assert.deepEqual(f.shell.calls.at(-1), ['watch', true, '《胖猫跳舞》', '她在看你的屏幕']);
  f.bar.update({ type: 'watch_state', active: false, mode: 'screen', title: '胖猫跳舞', reason: 'away' });
  assert.equal(f.shell.shown, true, 'still there for a moment');
  assert.equal(f.shell.ending, '你切走好一阵了，她先不看了');
  assert.equal([...f.timers.values()][0].ms, 6000);
  const before = f.refreshed();
  f.fire();
  assert.equal(f.shell.shown, false);
  assert.ok(f.refreshed() > before, 'the click-through mask is redrawn when it goes');
  assert.equal(WATCH_END_HOLD_MS, 6000);
});

test('WatchBar: a new start during the hold cancels the pending hide; the user stopping or a disconnect hides at once', () => {
  const f = fake();
  f.bar.update({ active: true, mode: 'screen' });
  f.bar.update({ active: false, mode: 'screen', reason: 'away' });
  f.bar.update({ active: true, mode: 'screen', title: '又刷到一条', app: 'Chrome' });
  assert.equal(f.timers.size, 0, 'the old hide is gone');
  assert.equal(f.shell.shown, true);
  f.bar.update({ active: false, mode: 'screen', reason: 'user' });
  assert.equal(f.shell.shown, false);
  assert.equal(f.timers.size, 0);
  f.bar.update({ active: true, mode: 'screen' });
  f.bar.update({ active: false, mode: 'screen', reason: 'away' });
  f.bar.hide();  // 断线 / 新连接 / 点了"不看了"
  assert.equal(f.shell.shown, false);
  assert.equal(f.timers.size, 0);
});

test('WatchBar: an "away" end when the bar is not showing does not pop it up', () => {
  const f = fake();
  f.bar.update({ active: false, mode: 'screen', reason: 'away' });
  assert.equal(f.shell.shown, false);
  assert.equal(f.timers.size, 0);
});
