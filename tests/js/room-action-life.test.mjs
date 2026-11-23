import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { register } from 'node:module';

const THREE_URL = new URL('../../web/vendor/three/build/three.module.js', import.meta.url).href;
const STATIC_URL = new URL('../../web/', import.meta.url).href;
const PET_URL = new URL('../../desktop/web/', import.meta.url).href;
register(`data:text/javascript,${encodeURIComponent(`export async function resolve(s,c,n) {
  if (s === 'three') return {url:${JSON.stringify(THREE_URL)},shortCircuit:true};
  if (s.startsWith('/static/')) return {url:new URL(s.slice(8),${JSON.stringify(STATIC_URL)}).href,shortCircuit:true};
  if (s.startsWith('/pet/')) return {url:new URL(s.slice(5),${JSON.stringify(PET_URL)}).href,shortCircuit:true};
  return n(s,c); }`)}`);
globalThis.location = { search: '' };
const source = await readFile(new URL('../../desktop/web/room-acts.js', import.meta.url), 'utf8');
// The real Life and Ctx are used; only test activity bodies and the room's physical layer are mock.
const { Life, SCRIPTS } = await import(`data:text/javascript;base64,${Buffer.from(source + '\nexport { SCRIPTS };').toString('base64')}`);
const roomSource = await readFile(new URL('../../desktop/web/room-life.js', import.meta.url), 'utf8');
function api(life) {
  const start = roomSource.indexOf('    requestAct(id, options)'), end = roomSource.indexOf('    doAct(id)', start);
  assert.ok(start >= 0 && end > start);
  // Execute all actual returned-object methods in this span, including any accidental duplicate key.
  return Function('life', 'needs', `return ({${roomSource.slice(start, end)}});`)(life, life.r.needs);
}
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; };
const flush = () => new Promise(resolve => setImmediate(resolve));
function fixture(name, script) {
  const steps = [], events = [];
  SCRIPTS[name] = script;
  const r = {
    scene: { life: [{ id: 'coffee', name: '测试活动', script: name }] }, busy: () => false,
    props: { fed: [] }, needs: { stock: { beans: 1 }, v: { energy: 70 }, fatigue: 0, worst: () => ({ gap: 0 }), save() { events.push('saved'); } },
    practice: { history: [], skills: {} }, body: { injured: false, log: [] },
    status() {}, log() {}, layer: { run(step, done) { steps.push(step.do); done(); }, stop() { events.push('layer-stop'); } },
  };
  const life = new Life(r); life.fast = true; life.start = () => {}; life._ask = async () => null;
  return { life, api: api(life), steps, events };
}

test('public availability shares real Life eligibility and preserves legacy stock limits', () => {
  const f = fixture('__capability_legacy', async () => {});
  f.life.base = [
    { id: 'coffee', script: 'coffee', need: 'beans' }, { id: 'phone' },
    { id: 'snack', need: 'snacks' }, { id: 'known', script: 'afford', ctx: null }, { id: 'known-empty-stock', script: 'afford', ctx: {}, need: 'snacks' },
  ];
  f.life.r.needs.stock = { beans: 0, snacks: 0 };
  f.life.asking = true; // Inspect actual goDo queue eligibility without starting the activity body.
  const caps = f.api.capabilities();
  assert.deepEqual(caps.map(x => [x.id, x.enabled]), [['coffee', false], ['phone', true], ['snack', false], ['known', false], ['known-empty-stock', false]]);
  assert.ok(caps.every(x => typeof x.enabled === 'boolean'));
  assert.equal(f.life.goDo('coffee'), false);
  assert.equal(f.life.goDo('phone'), true);
  assert.deepEqual(f.life.order, ['phone']);
  f.life.r.needs.stock.beans = 1;
  assert.equal(f.api.capabilities()[0].enabled, true);
  assert.equal(f.life.goDo('coffee'), true);
});

test('actual coffee may be requested with no beans to approach and inspect existing liquid', () => {
  const f = fixture('__capability_actual', async () => {});
  f.life.base = [{ id: 'coffee', script: 'coffee', need: 'beans' }, { id: 'snack', need: 'snacks' }];
  Object.assign(f.life.r.needs, { actualConsumption: true, stock: { beans: 0, snacks: 0 } });
  f.life.r.scene.coffee = { cups: ['cup'], stand: [0, 0] };
  // Being out of view is not proof of an empty cup and must not permanently block inspection.
  f.life.r.liquid = { inspect: () => ({ visible: false, known: false, level: 'unknown' }) };
  f.life.asking = true;
  assert.deepEqual(f.api.capabilities().map(x => x.enabled), [true, false]);
  assert.equal(f.life.goDo('coffee'), true);
  assert.deepEqual(f.life.order, ['coffee']);
  // Eligibility does not pretend the observation/consumption path exists in an incomplete room.
  delete f.life.r.liquid;
  assert.equal(f.api.capabilities()[0].enabled, false);
  assert.equal(f.life.goDo('coffee'), false);
  f.life.recoveryError = '杯子尚未归还';
  assert.ok(f.api.capabilities().every(x => x.enabled === false));
});

test('Life completes a request only after its cleanup and caches that terminal result', async () => {
  const gate = deferred(); let attempts = 0;
  const f = fixture('__request_cleanup_success', async ctx => { attempts++; ctx.later(() => gate.promise); });
  const p = f.api.requestAct('coffee', { requestId: 'complete' });
  let resolved = false; p.then(() => { resolved = true; });
  await flush(); assert.equal(resolved, false); assert.ok(f.life.current);
  gate.resolve(); const out = await p; await flush();
  assert.equal(out.ok, true); assert.equal(out.started, true); assert.equal(out.completed, true);
  assert.equal(f.life.current, null);
  assert.equal(await f.api.requestAct('coffee', { requestId: 'complete' }), out); assert.equal(attempts, 1);
});

test('Life cleanup failure returns failed terminal even after the main script succeeds', async () => {
  const f = fixture('__request_cleanup_failure', async ctx => { ctx.later(() => { throw new Error('归还失败'); }); });
  const out = await f.api.requestAct('coffee', { requestId: 'cleanup-fail' }); await flush();
  assert.equal(out.ok, false); assert.equal(out.started, true); assert.equal(out.completed, false);
  assert.match(out.reason, /归还失败/); assert.equal(f.life.current, null);
  assert.ok(f.life.recoveryError);
  assert.ok(f.api.capabilities().every(x => x.enabled === false));
  assert.equal((await f.api.requestAct('coffee', { requestId: 'after-broken-cleanup' })).started, false);
  assert.equal(f.life.goDo('coffee'), false);
});

test('cancellation during cleanup rejects same-name retries and cannot enter successful recent history', async () => {
  const gate = deferred(); let attempts = 0;
  const f = fixture('__request_queued_recovery', async ctx => {
    attempts++;
    ctx.later(async () => { await gate.promise; });
  });
  const first = f.api.requestAct('coffee', { requestId: 'recovering' }); await flush();
  assert.deepEqual(f.life.done, [], 'script completion alone is not an observed outcome');
  f.api.cancelAct('停止'); let resolved = false; first.then(() => { resolved = true; }); await flush();
  assert.equal(resolved, false); assert.equal(f.life.requests.active.phase, 'cancelling');
  const rejected = await f.api.requestAct('coffee', { requestId: 'during-recovery' });
  assert.equal(rejected.started, false); assert.match(rejected.reason, /收尾/);
  gate.resolve(); const out = await first; await flush();
  assert.equal(out.ok, false); assert.equal(out.cleanupConfirmed, true); assert.equal(out.outcome, 'cancelled');
  assert.equal(f.life.done[0].ok, false); assert.match(f.life.done[0].error, /停止/);
  assert.equal(f.life.requests.active, null); assert.deepEqual(f.life.order, []);
  assert.equal(attempts, 1);
});

test('stopping an active Life request prevents new forward steps but retains cleanup', async () => {
  const gate = deferred();
  const f = fixture('__request_stop_forward', async ctx => {
    ctx.later(() => { f.events.push('cleanup'); });
    await gate.promise;
    await ctx.run({ do: 'pick', to: 'cup' });
    f.events.push('consumption');
  });
  const p = f.api.requestAct('coffee', { requestId: 'stopped' }); await flush();
  f.api.cancelAct('用户停止');
  gate.resolve(); const out = await p; await flush();
  assert.equal(out.ok, false); assert.equal(out.completed, false); assert.equal(out.cleanupConfirmed, true);
  assert.deepEqual(f.steps, [], 'cancelled request must not launch a new pick after stop');
  assert.ok(!f.events.includes('consumption'), 'cancelled request must not consume later');
  assert.ok(f.events.includes('cleanup'), 'safety cleanup is still required');
  assert.equal(f.life.current, null);
});


test('an early busy rejection cannot later launch an activity under the same request id', async () => {
  const gate = deferred(); let attempts = 0;
  const f = fixture('__request_busy_duplicate', async () => { attempts++; await gate.promise; });
  const first = f.api.requestAct('coffee', { requestId: 'first' }); await flush();
  const rejected = await f.api.requestAct('coffee', { requestId: 'busy-id' });
  assert.equal(rejected.ok, false);
  gate.resolve(); await first; await flush();
  const again = await f.api.requestAct('coffee', { requestId: 'busy-id' }); await flush();
  assert.equal(again.ok, false, 'replay of a rejected id stays rejected');
  assert.equal(attempts, 1, 'replay must not start an activity after caller already received failure');
  assert.equal(rejected.started, false); assert.equal(rejected.completed, false);
});


test('unconfirmed cleanup blocks both explicit and autonomous work until actual late confirmation', async () => {
  const gate = deferred(), timers = []; let attempts = 0;
  const f = fixture('__request_unconfirmed', async ctx => { attempts++; ctx.later(() => gate.promise); });
  f.life.requests.schedule = (fn, ms) => { const timer = { fn, ms }; timers.push(timer); return timer; };
  f.life.requests.unschedule = () => {};
  const first = f.api.requestAct('coffee', { requestId: 'stall' }); await flush();
  f.api.cancelAct('停止'); timers[1].fn(); const out = await first;
  assert.equal(out.cleanupConfirmed, false); assert.equal(f.life.requests.blocked, true);
  await f.life.next(); assert.equal(attempts, 1);
  assert.equal((await f.api.requestAct('coffee', { requestId: 'blocked' })).started, false);
  gate.resolve(); await flush(); await flush();
  assert.equal(f.life.current, null); assert.equal(f.life.requests.active, null);
  assert.equal(f.life.done[0].ok, false); assert.equal(await first, out);
});

test('queued cancel adapter exception cannot revive residual order as an autonomous action', async () => {
  let attempts = 0; const f = fixture('__request_cancel_error', async () => { attempts++; });
  f.life.asking = true;
  f.life.requests.cancel = () => { throw new Error('撤队失败'); };
  const p = f.api.requestAct('coffee', { requestId: 'queued' });
  f.api.cancelAct('停止'); const out = await p;
  assert.equal(out.started, false); assert.equal(out.cleanupConfirmed, false);
  assert.deepEqual(f.life.order, ['coffee']);
  f.life.asking = false; await f.life.next();
  assert.doesNotThrow(() => f.life._idleMoment(), 'queued idle gestures must also stay blocked');
  assert.ok(f.api.capabilities().every(a => !a.enabled));
  assert.equal(f.life.goDo('fetch'), false, 'legacy fetch entrance cannot bypass the lease');
  assert.equal(attempts, 0); assert.equal(f.life.requests.blocked, true); assert.deepEqual(f.life.done, []);
  f.life.requests.finish(f.life.requests.active, { ok:false, cleanupConfirmed:true });
});

test('async plan returning after an unconfirmed cancellation cannot start residual work', async () => {
  let attempts = 0; const plan = deferred();
  const f = fixture('__request_stale_plan', async () => { attempts++; });
  f.life._ask = () => { f.life.asking = true; return plan.promise; };
  const next = f.life.next(); await flush();
  f.life.requests.cancel = () => { throw new Error('撤队失败'); };
  const p = f.api.requestAct('coffee', { requestId:'during-plan' }); f.api.cancelAct('停止'); await p;
  plan.resolve(null); await next; await flush();
  assert.equal(attempts, 0); assert.equal(f.life.current, null); assert.equal(f.life.requests.blocked, true);
  f.life.requests.finish(f.life.requests.active, { ok:false, cleanupConfirmed:true });
});


test('cancelled stand-up continuation cannot start its old walking target', async () => {
  let standDone, walks = 0;
  const f = fixture('__request_deferred_walk', async ctx => { await ctx._walk([0.5, 0.5]); f.events.push('arrived'); });
  Object.assign(f.life.r.layer, {
    grid: {x0:0,z0:0,w:2,h:2,cell:1,open:new Uint8Array([1,1,1,1])}, seated:{},
    standUp(done) { standDone = done; }, _walkTo() { walks++; return true; },
  });
  const p = f.api.requestAct('coffee'); await flush(); assert.equal(typeof standDone, 'function');
  f.api.cancelAct('用户停止'); standDone(); const out = await p; await flush();
  assert.equal(walks, 0); assert.equal(out.ok, false); assert.equal(out.cleanupConfirmed, true);
  assert.ok(!f.events.includes('arrived'));
});

test('late successful action callback cannot run forward script code after cancellation', async () => {
  let done;
  const f = fixture('__request_late_action', async ctx => { await ctx.run({do:'pick',to:'cup'}); f.events.push('forward-mutation'); });
  f.life.r.layer.run = (_step, callback) => { done = callback; };
  const p = f.api.requestAct('coffee'); await flush(); f.api.cancelAct('停止'); done(null);
  const out = await p; await flush();
  assert.equal(out.ok, false); assert.ok(!f.events.includes('forward-mutation'));
});

test('cancelled reach suppresses its event and its successful continuation while cleanup is still allowed', async () => {
  let event, done;
  const f = fixture('__request_late_reach', async ctx => {
    ctx.later(() => { f.events.push('cleanup'); });
    await ctx.reach('right', {clone(){return this;}}, () => { f.events.push('reach-event'); });
    f.events.push('forward-mutation');
  });
  f.life.r.layer._reachStart = (_side, _point, onEvent, onDone) => { event=onEvent;done=onDone; };
  const p = f.api.requestAct('coffee'); await flush(); f.api.cancelAct('停止'); event();done();
  const out = await p; await flush();
  assert.equal(out.ok, false); assert.deepEqual(f.events, ['cleanup', 'saved']);
});
