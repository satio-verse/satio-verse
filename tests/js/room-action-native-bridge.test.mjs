import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

// Execute the actual room-do branch and Promise callback; no host, DOM or physical action.
const source = await readFile(new URL('../../desktop/web/pet.js', import.meta.url), 'utf8');
const marker = "  } else if (m?.t === 'room-do') {";
const start = source.indexOf(marker), end = source.indexOf("  } else if (m?.t === 'remind')", start);
assert.ok(start >= 0 && end > start, 'the actual native room-do branch must be found');
const body = source.slice(source.indexOf('\n', start) + 1, end);
const bridge = new Function('state', 'm', 'post', body);
const flush = () => new Promise(resolve => setImmediate(resolve));
async function send(receipt) {
  const posts = [], calls = [];
  bridge({ room: { requestAct(act, options) { calls.push({ act, options }); return Promise.resolve(receipt); } } },
    { t:'room-do', call_id:'native-123', act:'coffee' }, value => posts.push(value));
  await flush();
  assert.deepEqual(calls, [{ act:'coffee', options:{ requestId:'tool:native-123' } }]);
  assert.equal(posts.length, 1);
  const match = /^builtin-done native-123 ([01]) (.+)$/.exec(posts[0]); assert.ok(match);
  return { flag:Number(match[1]), result:JSON.parse(match[2]) };
}
for (const [name, receipt, flag] of [
  ['completed', {ok:true, started:true, completed:true, outcome:'completed', cleanupConfirmed:true, cancelled:false}, 1],
  ['cancelled', {ok:false, started:true, completed:false, outcome:'cancelled', cleanupConfirmed:true, cancelled:true}, 0],
  ['unconfirmed cleanup', {ok:false, started:true, completed:false, outcome:'cancel_unconfirmed', cleanupConfirmed:false, cancelled:true}, 0],
  ['failed cleanup', {ok:false, started:true, completed:false, outcome:'failed', cleanupConfirmed:false, cancelled:false}, 0],
]) {
  test(`native room-do forwards ${name} receipt and correct success flag`, async () => {
    const input = { act:'coffee', reason:name, detail:{consumed:false}, ...receipt };
    const actual = await send(input);
    assert.equal(actual.flag, flag); assert.deepEqual(actual.result, input);
  });
}
test('native bridge retains an explicit whitelist rather than spreading diagnostic fields', async () => {
  const input = {act:'coffee',ok:false,started:true,completed:false,outcome:'cancel_unconfirmed',cleanupConfirmed:false,cancelled:true,
    reason:'尚未确认',detail:{consumed:false},temperatureC:69.12,liquidMl:72.3,bodyTrace:{hidden:true},worldState:{private:true}};
  const {result} = await send(input);
  assert.deepEqual(Object.keys(result).sort(), ['act','cancelled','cleanupConfirmed','completed','detail','ok','outcome','reason','started'].sort());
  assert.equal(result.temperatureC,undefined);assert.equal(result.liquidMl,undefined);assert.equal(result.bodyTrace,undefined);
});
test('native bridge does not reply before the actual request promise has settled', async () => {
  let resolve; const pending = new Promise(r => { resolve=r; }), posts=[];
  bridge({room:{requestAct:()=>pending}}, {call_id:'pending',act:'coffee'}, x=>posts.push(x));
  await flush();assert.deepEqual(posts,[]);
  resolve({ok:false,started:true,completed:false,outcome:'cancelled',cleanupConfirmed:true,cancelled:true});
  await flush();assert.equal(posts.length,1);assert.match(posts[0],/^builtin-done pending 0 /);
});
test('native bridge preserves legacy receipt and rejection behavior', async () => {
  const old = await send({ok:false,act:'coffee',started:false,completed:false});
  assert.deepEqual(old.result,{ok:false,act:'coffee',started:false,completed:false,reason:'',detail:null});
  const posts=[];
  bridge({room:{requestAct:()=>Promise.reject(new Error('synthetic failure'))}}, {call_id:'error',act:'coffee'},x=>posts.push(x));
  await flush();assert.deepEqual(posts,['builtin-done error 0 synthetic failure']);
});
