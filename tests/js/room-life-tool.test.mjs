import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
const src = readFileSync(new URL('../../tools/room_life.mjs', import.meta.url), 'utf8');

test('room_life default fast remains 1 and explicit mode rejects unsupported values before launch', () => {
  const start = src.indexOf('const A = '), end = src.indexOf('// 活动归哪一类', start);
  const code = src.slice(start, end) + '\nglobalThis.options=A;';
  const context = argv => { const c = vm.createContext({ process: { argv: ['node', 'room_life.mjs', ...argv] } }); vm.runInContext(code, c); return c.options; };
  assert.equal(context([]).fast, 1);
  assert.equal(context(['--fast', '0']).fast, '0');
  assert.throws(() => context(['--fast', '2']), /--fast/);
});

test('real navigation code emits exactly one authoritative fast value even with duplicate query values', async () => {
  const start = src.indexOf('const extraQuery = '), end = src.indexOf('const t0 = Date.now();', start);
  assert.ok(start >= 0 && end > start);
  for (const fast of ['0', '1']) {
    let navigated;
    const c = vm.createContext({ URLSearchParams, base: 'http://127.0.0.1:8893', sid: 'test',
      A: { fast, server: 'offline-mock', avatar: 'silvercat.vrm', query: 'ws=0&fast=1&fast=0&roomscene=casa-luce-v8' },
      send: async (method, params) => { assert.equal(method, 'Page.navigate'); navigated = params.url; } });
    await vm.runInContext(`(async()=>{${src.slice(start, end)}})()`, c);
    const u = new URL(navigated);
    assert.deepEqual(u.searchParams.getAll('fast'), [fast]);
    assert.equal(u.searchParams.get('ws'), '0');
    assert.equal(u.searchParams.get('roomscene'), 'casa-luce-v8');
  }
});
