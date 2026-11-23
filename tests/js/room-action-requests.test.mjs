import { test } from 'node:test';
import assert from 'node:assert/strict';
import { RoomActionRequests } from '../../desktop/web/room-action-requests.js';
function fixture(enqueue = () => true, cancel) {
  const timers = [], cancels = [];
  const q = new RoomActionRequests({ enqueue, cancel: cancel || (a => cancels.push(a)),
    schedule: (fn, ms) => { const t = { fn, ms }; timers.push(t); return t; }, unschedule: t => { if (t) t.cleared = true; } });
  return { q, timers, cancels };
}
const flush = async () => { await Promise.resolve(); await Promise.resolve(); };

test('queued is not completed; terminal result is retained for duplicate request id', async () => {
  let n = 0; const { q } = fixture(() => (++n, true));
  const p = q.request('coffee', { requestId: 'one' });
  assert.equal(q.request('coffee', { requestId: 'one' }), p);
  let resolved = false; p.then(() => { resolved = true; }); await flush();
  assert.equal(resolved, false);
  const entry = q.begin('coffee'); assert.equal(q.begin('coffee'), null); q.finish(entry, { ok: true });
  assert.deepEqual(await p, { act:'coffee', requestId:'one', started:true, completed:true, ok:true,
    cancelled:false, cleanupConfirmed:true, outcome:'completed' });
  assert.equal(await q.request('coffee', { requestId:'one' }), await p); assert.equal(n, 1);
});
test('failure, unavailable and concurrent requests cannot claim completion', async () => {
  const { q } = fixture(); const p = q.request('phone');
  assert.equal((await q.request('computer')).ok, false);
  q.finish(q.begin('phone'), { ok:false, reason:'够不到' });
  assert.equal((await p).completed, false);
  assert.equal((await fixture(() => false).q.request('coffee')).started, false);
});
test('running stop and timeout retain occupancy until cleanup and override late success', async () => {
  for (const mode of ['timeout','stop']) {
    const { q, timers, cancels } = fixture(); const p = q.request('coffee'); const entry = q.begin('coffee');
    let result; p.then(x => { result = x; });
    mode === 'timeout' ? timers[0].fn() : q.stop(); await flush();
    assert.equal(result, undefined); assert.equal(q.active, entry); assert.equal(entry.phase, 'cancelling');
    assert.equal(q.blocked, true); assert.equal(q.begin('coffee'), null);
    assert.equal((await q.request('coffee', { requestId:'second' })).started, false);
    q.finish(entry, { ok:true });
    assert.equal((await p).ok, false); assert.equal((await p).cleanupConfirmed, true);
    assert.equal((await p).outcome, 'cancelled'); assert.equal(q.active, null); assert.deepEqual(cancels, ['coffee']);
  }
});
test('queued cancellation removes work immediately without starting it', async () => {
  const { q } = fixture(); const p = q.request('coffee'); q.stop('取消排队');
  const r = await p; assert.equal(r.started, false); assert.equal(r.completed, false);
  assert.equal(r.cleanupConfirmed, true); assert.equal(q.active, null); assert.equal(q.begin('coffee'), null);
});
test('cleanup timeout returns unconfirmed but blocks new work; late confirmation never rewrites reply', async () => {
  const { q, timers } = fixture(); const p = q.request('coffee', { requestId:'A' }); const entry = q.begin('coffee');
  timers[0].fn(); assert.equal(timers[1].ms, 45000); q.stop('再次停止'); assert.equal(timers.length, 2);
  timers[1].fn(); const out = await p;
  assert.equal(out.outcome, 'cancel_unconfirmed'); assert.equal(out.cleanupConfirmed, false); assert.equal(out.completed, false);
  assert.equal(q.active, entry); assert.equal(q.blocked, true);
  assert.equal((await q.request('coffee', { requestId:'B' })).started, false);
  q.finish(entry, { ok:true, cleanupConfirmed:true });
  assert.equal(q.active, null); assert.equal(await p, out); assert.equal((await p).outcome, 'cancel_unconfirmed');
  assert.equal(await q.request('coffee', { requestId:'A' }), out);
});
test('old finish and queued timer callback cannot cancel or complete a later same-name request', async () => {
  const { q, timers, cancels } = fixture(); const a = q.request('coffee', { requestId:'A' }); const old = q.begin('coffee');
  q.finish(old, { ok:true }); await a;
  const b = q.request('coffee', { requestId:'B' }); const current = q.begin('coffee');
  timers[0].fn(); q.finish(old, { ok:true });
  assert.equal(q.active, current); assert.equal(current.cancelled, undefined); assert.deepEqual(cancels, []);
  q.finish(current, { ok:true }); assert.equal((await b).ok, true);
});
test('cancel adapter exception replies unconfirmed without freeing queued or running work', async () => {
  for (const started of [false, true]) {
    const { q } = fixture(() => true, () => { throw new Error('adapter failed'); });
    const p = q.request('coffee'); const entry = started ? q.begin('coffee') : q.active;
    assert.doesNotThrow(() => q.stop('停止')); const out = await p;
    assert.equal(out.started, started); assert.equal(out.cleanupConfirmed, false); assert.match(out.reason, /adapter failed/);
    assert.equal(q.active, entry); assert.equal(q.blocked, true); assert.equal(q.begin('coffee'), null);
    q.finish(entry, { ok:false, cleanupConfirmed:true }); assert.equal(q.active, null); assert.equal(await p, out);
  }
});
test('failed cleanup retains lease even when the activity script returned success', async () => {
  const { q } = fixture(); const p = q.request('phone'); const entry = q.begin('phone');
  q.finish(entry, { ok:true, reason:'归还失败', cleanupConfirmed:false });
  const out = await p; assert.equal(out.completed, false); assert.equal(out.outcome, 'failed');
  assert.equal(q.blocked, true); assert.equal(q.active, entry);
  q.finish(entry, { ok:false, cleanupConfirmed:true }); assert.equal(q.active, null); assert.equal(await p, out);
});
test('request id cannot be reused for another activity; busy-cache eviction preserves current id', async () => {
  const { q } = fixture(); const p = q.request('coffee', { requestId:'x' });
  assert.equal((await q.request('phone', { requestId:'x' })).ok, false);
  for (let i=0;i<80;i++) await q.request('coffee', { requestId:`busy-${i}` });
  assert.equal(q.request('coffee', { requestId:'x' }), p); assert.equal(q.cache.size, 64);
  q.stop(); await p;
});


test('canonical receipt identity and completion cannot be overwritten by contradictory result fields', async () => {
  const { q } = fixture(); const p = q.request('coffee', {requestId:'original'}); const entry = q.begin('coffee');
  q.finish(entry, {ok:false, completed:true, started:false, act:'phone', requestId:'forged'});
  const r = await p;
  assert.equal(r.completed, false); assert.equal(r.started, true); assert.equal(r.act, 'coffee'); assert.equal(r.requestId, 'original');
});

test('cancelled failed cleanup is unconfirmed; non-boolean confirmation fails closed', async () => {
  for (const confirmation of [false, 'true', 1, null]) {
    const { q } = fixture(); const p = q.request('coffee'); const entry = q.begin('coffee');q.stop('停止');
    q.finish(entry, {ok:true, completed:true, cleanupConfirmed:confirmation, reason:'归还未证实'});
    const r = await p; assert.equal(r.outcome, 'cancel_unconfirmed'); assert.equal(r.cleanupConfirmed, false);
    assert.equal(r.completed, false); assert.equal(q.blocked, true);
    q.finish(entry, {ok:false, cleanupConfirmed:true});
  }
});
