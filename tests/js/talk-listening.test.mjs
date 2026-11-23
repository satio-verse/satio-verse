// 桌宠对话通道 (desktop/web/talk.js): 服务端说"那一段声音没认出话 / 是外放的回声" (listening_end, 2026-11-08 外放回声那次加的)
// 时收起"在听"的姿态、告诉对话栏; 以前只有下一轮 turn_start 才收, 外放时会一直挂着"在听…".
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { register } from 'node:module';

const STATIC_URL = new URL('../../web/', import.meta.url).href;
register(`data:text/javascript,${encodeURIComponent(`export async function resolve(s,c,n) {
  if(s.startsWith('/static/')) return {url:new URL(s.slice(8),${JSON.stringify(STATIC_URL)}).href,shortCircuit:true};
  return n(s,c); }`)}`);
const { Talk } = await import('../../desktop/web/talk.js');

test('listening_end puts the listening pose away and tells the dialog bar', () => {
  const calls = [];
  const talk = new Talk({ backchannel: () => {} }, {
    onListening: () => calls.push('listening'),
    onListeningEnd: () => calls.push('end'),
  });
  talk.handle({ type: 'listening' });
  assert.equal(talk.performer.listening, true);
  talk.handle({ type: 'listening_end' });
  assert.equal(talk.performer.listening, false);
  assert.deepEqual(calls, ['listening', 'end']);
});

test('a page without the new hook just ignores listening_end', () => {
  const talk = new Talk({ backchannel: () => {} }, {});
  talk.handle({ type: 'listening' });
  assert.doesNotThrow(() => talk.handle({ type: 'listening_end' }));
  assert.equal(talk.performer.listening, false);
});
