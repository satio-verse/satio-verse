// 桌宠对话通道 (desktop/web/talk.js) 换一条连接以后: 服务端每条连接是新的会话 (wm/app.py 先发 hello 再开 Session), 轮号从 1 重新数 ——
// 上一条连接里打断到第几轮、每句的段、放声音的那一轮都不能带过来 (2026-11-08 陪看第四次真机试: 重启前第 9–11 轮被打断过,
// 页面的 dropUpTo 停在 11, 重启后她的第 1–3 轮全被当成旧轮扔了 —— 没显示、没出声, audio_end 照样报了)
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';

const STATIC_URL = new URL('../../web/', import.meta.url).href;
register(`data:text/javascript,${encodeURIComponent(`export async function resolve(s,c,n) {
  if(s.startsWith('/static/')) return {url:new URL(s.slice(8),${JSON.stringify(STATIC_URL)}).href,shortCircuit:true};
  return n(s,c); }`)}`);
const { Talk } = await import('../../desktop/web/talk.js');
const { AudioOut } = await import('/static/audio.js');
globalThis.WebSocket ??= { OPEN: 1 };

// 假的 AudioContext: 排上的每段声音记在 live 里, finish() = 都放完了 (AudioOut 靠每段的 onended 算"放完了")
class FakeCtx {
  constructor() { this.currentTime = 0; this.outputLatency = 0; this.destination = {}; this.live = new Set(); }
  createGain() { return { gain: { value: 1 }, connect() {} }; }
  createBuffer(_channels, length, sampleRate) { return { duration: length / sampleRate, copyToChannel() {} }; }
  createBufferSource() {
    const live = this.live, src = { connect() {}, start() { live.add(src); }, stop() { live.delete(src); } };
    return src;
  }
  finish() { for (const src of [...this.live]) { this.live.delete(src); src.onended?.(); } }
}

// 服务端下发的一段音频: <4sIIII (WMA1, 轮, 段, 采样率, 序号) + int16 PCM
function frame(turn, seg, n = 1600, rate = 16000) {
  const buf = new ArrayBuffer(20 + n * 2), dv = new DataView(buf);
  [...'WMA1'].forEach((c, i) => dv.setUint8(i, c.charCodeAt(0)));
  dv.setUint32(4, turn, true); dv.setUint32(8, seg, true); dv.setUint32(12, rate, true); dv.setUint32(16, 0, true);
  const pcm = new Int16Array(buf, 20, n);
  for (let i = 0; i < n; i++) pcm[i] = Math.round(8000 * Math.sin(i / 6));
  return buf;
}

function pet() {
  const replies = [], done = [], ctx = new FakeCtx();
  const talk = new Talk(null, { onReply: (turn, text) => replies.push([turn, text]), onDone: (turn) => done.push(turn) });
  talk.out = new AudioOut(ctx);
  // 连上一条 (新的) 连接: 换掉 ws, 服务端先发 hello. 返回这条连接上页面发出去的消息
  const connect = () => {
    const sent = [];
    talk.ws = { readyState: 1, send: (s) => sent.push(JSON.parse(s)) };
    talk.handle({ type: 'hello', status: {} });
    return sent;
  };
  const say = (turn, text) => {
    talk.handle({ type: 'turn_start', turn, text: '', source: 'watch' });
    talk.handle({ type: 'reply_delta', turn, text });
    talk.handle({ type: 'audio_seg', turn, seg: 0, text });
    talk.onAudio(frame(turn, 0));
  };
  return { talk, ctx, replies, done, connect, say };
}
const marks = (sent, name) => sent.filter((m) => m.type === 'mark' && m.name === name).map((m) => m.turn);

test('after a reconnect her first turns are shown and heard, though turn 11 was interrupted on the old connection', () => {
  const p = pet();
  const old = p.connect();
  p.say(11, '上一条连接的话');
  p.talk.handle({ type: 'interrupted', turn: 11, reason: 'barge_in' });
  p.talk.handle({ type: 'reply_delta', turn: 11, text: '打断以后迟到的字' });
  p.talk.onAudio(frame(11, 1));
  assert.deepEqual(p.replies, [[11, '上一条连接的话']], 'on the same connection the interrupted turn stays dropped');
  assert.deepEqual(marks(old, 'client_audio_start'), [11]);

  const sent = p.connect();  // 服务端重启了: 新会话, 轮号从 1 数
  for (const [turn, text] of [[1, '好～我先把视频打开'], [2, '你现在看到几分几秒啦？'], [3, '你没说看到哪了，那我先不跟啦']]) {
    p.say(turn, text);
    p.talk.handle({ type: 'audio_end', turn });
    p.ctx.finish();
  }
  assert.deepEqual(p.replies.slice(1).map(([turn]) => turn), [1, 2, 3], 'the new turns 1–3 reach the speech bubble');
  assert.deepEqual(marks(sent, 'client_audio_start'), [1, 2, 3], 'and their sound is played');
  assert.deepEqual(marks(sent, 'client_audio_end'), [1, 2, 3]);
  assert.deepEqual(p.done, [1, 2, 3]);
});

test('a new turn with the same number as the last one of the old connection starts its sound from scratch', () => {
  const p = pet();
  p.connect();
  p.say(1, '上一条连接的第 1 轮');
  p.talk.handle({ type: 'audio_end', turn: 1 });
  p.ctx.finish();
  const sent = p.connect();
  p.say(1, '新连接的第 1 轮');
  assert.deepEqual(marks(sent, 'client_audio_start'), [1], 'not appended to the old turn 1 (no "started" mark then)');
  assert.equal(p.talk.turn.audioAt > 0, true);
});

test('sound of the old connection still playing when the new one says hello: its end is settled then, and not reported to the new session', () => {
  const p = pet();
  p.connect();
  p.say(1, '一大段还没念完的话');
  p.talk.handle({ type: 'audio_end', turn: 1 });  // 服务端发完了, 页面还在放 (没 finish)
  const sent = p.connect();
  assert.deepEqual(p.done, [1], 'the old turn ends now, before the new session reuses turn number 1');
  assert.deepEqual(marks(sent, 'client_audio_end'), [], 'its end mark would land on the new session\'s turn 1');
  p.say(1, '新连接的第 1 轮');
  p.talk.handle({ type: 'audio_end', turn: 1 });
  p.ctx.finish();
  assert.deepEqual(p.done, [1, 1]);
  assert.deepEqual(marks(sent, 'client_audio_start'), [1]);
  assert.deepEqual(marks(sent, 'client_audio_end'), [1]);
});

test('AudioOut.forgetTurn (the web page uses it too): a pending "played to the end" fires right away, once; the next turn starts fresh even with the same number', () => {
  const ctx = new FakeCtx(), out = new AudioOut(ctx), ended = [], pcm = new Int16Array(1600);
  out.play(1, 16000, pcm, 0);
  out.endTurn(1, (t) => ended.push(t));
  assert.equal(ended.length, 0, 'still playing');
  out.forgetTurn();
  assert.equal(ended.length, 1);
  assert.equal(out.play(1, 16000, pcm, 0).first, true);
  ctx.finish();
  assert.equal(ended.length, 1, 'the old callback does not fire again');
});
