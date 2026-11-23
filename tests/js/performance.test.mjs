// node --test tests/js
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { HOP } from '../../web/lipsync.js';
import { SpeechPerformer } from '../../web/performance.js';

function fakeAvatar() {
  const calls = [];
  return {
    calls,
    glance: (yaw, pitch, seconds) => calls.push(['glance', yaw, pitch, seconds]),
    headAction: (name, opts) => calls.push(['head', name, opts.amplitude]),
    lookAtUser: () => calls.push(['look']),
    pulseAU: (key, value) => calls.push(['au', key, value]),
  };
}

// level 序列 -> 口型帧 (表演层只看 level)
const frames = (levels) => levels.map((level) => ({ level }));

function play(p, until, step = 0.01) {
  for (let t = 0; t <= until; t += step) p.update(t, t, step);
}

test('句首视线移开, 短句不移开', () => {
  const avatar = fakeAvatar();
  const p = new SpeechPerformer();
  p.attach(avatar);
  p.beginClause(1.0, '你好');                       // 2 个字
  p.beginClause(2.0, '一次考试说明不了什么，');       // 10 个字
  play(p, 3);
  const glances = avatar.calls.filter((c) => c[0] === 'glance');
  assert.equal(glances.length, 1);
  assert.ok(Math.abs(glances[0][1]) >= 8 && glances[0][2] > 0);
});

test('能量重音处点头, 间隔不小于 0.9 秒', () => {
  const avatar = fakeAvatar();
  const p = new SpeechPerformer();
  p.attach(avatar);
  p.beginClause(1.0, '好');
  // 每 0.3 秒一次 "安静 -> 突然变响", 共 10 次; 时长 3 秒
  const levels = [];
  for (let i = 0; i < 10; i++) levels.push(0.1, 0.1, 0.1, 0.8, 0.8, 0.6, 0.4, 0.2, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1, 0.1);
  p.addAudio(1.0, frames(levels));
  play(p, 5);
  const nods = avatar.calls.filter((c) => c[0] === 'head');
  assert.ok(nods.length >= 3 && nods.length <= 4, `点头 ${nods.length} 次`);
  const beatTimes = p.log.filter((n) => n === 'beat_nod').length;
  assert.equal(beatTimes, nods.length);
  assert.ok(levels.length * HOP <= 3.01);
});

test('问句在句末歪头扬眉, 之后回正; 整轮结束看回用户并点头', () => {
  const avatar = fakeAvatar();
  const p = new SpeechPerformer();
  p.attach(avatar);
  p.beginClause(1.0, '想聊聊吗？');
  p.addAudio(1.0, frames(new Array(50).fill(0.2)));   // 1 秒
  p.endTurn(2.0);
  let maxRoll = 0;
  for (let t = 0; t <= 4; t += 0.01) { p.update(t, t, 0.01); maxRoll = Math.max(maxRoll, p.neck.roll); }
  assert.ok(maxRoll > 5, `最大歪头 ${maxRoll}`);
  assert.ok(Math.abs(p.neck.roll) < 0.5, '歪头应在句末之后回正');
  assert.ok(avatar.calls.some((c) => c[0] === 'au' && c[1] === 'au01InnerBrowRaiser'));
  const order = p.log.filter((n) => ['question_tilt', 'end_nod', 'question_tilt_end'].includes(n));
  assert.deepEqual(order, ['question_tilt', 'end_nod', 'question_tilt_end']);
  assert.ok(avatar.calls.some((c) => c[0] === 'look'));
});

test('打断时清空所有待触发的表演', () => {
  const avatar = fakeAvatar();
  const p = new SpeechPerformer();
  p.attach(avatar);
  p.beginClause(1.0, '这是一句比较长的回复，');
  p.addAudio(1.0, frames([0.1, 0.1, 0.9, 0.9]));
  p.endTurn(2.0);
  p.reset();
  play(p, 4);
  assert.deepEqual(avatar.calls, []);
});

test('没有 VRM 时不做任何表演', () => {
  const p = new SpeechPerformer();
  p.beginClause(1.0, '这是一句比较长的回复？');
  p.addAudio(1.0, frames([0.1, 0.9]));
  p.endTurn(2.0);
  play(p, 3);
  assert.equal(p.events.length, 0);
});

test('倾听时每 2~4 秒轻点一次头', () => {
  const avatar = fakeAvatar();
  const p = new SpeechPerformer();
  p.attach(avatar);
  p.setListening(true, 0);
  play(p, 10, 0.05);
  const nods = avatar.calls.filter((c) => c[0] === 'head').length;
  assert.ok(nods >= 2 && nods <= 5, `倾听点头 ${nods} 次`);
});
