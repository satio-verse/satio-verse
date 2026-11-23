import test from 'node:test';
import assert from 'node:assert/strict';
import { COFFEE_EVIDENCE as C, restoreCoffeeEvidence, tastedCoffee, coffeeThermalView, coffeeDecision } from '../../desktop/web/room-coffee-policy.js';

const start = 1700000000000;
const saved = (thermal = {}, levels = {}, at = start) => ({ version: 1, at, thermal, levels });
const visible = (e, now) => ({ visible: true, level: 'half', ...coffeeThermalView(e, now) });

test('thirty-second expiry is unknown with remembered hot, never automatic cooling or immediate retry', () => {
  const hot = tastedCoffee(null, 'hot', start);
  for (const elapsed of [31000, 60000, 299999]) {
    const o = visible(hot, start + elapsed);
    assert.equal(o.known, false); assert.equal(o.category, 'unknown'); assert.equal(o.pastHot, true);
    assert.equal(o.retry.state, 'wait'); assert.equal(coffeeDecision(o, { beans: 0 }).action, 'wait');
  }
  const o = visible(hot, start + C.retryMs);
  assert.equal(o.category, 'unknown'); assert.equal(o.retry.state, 'due');
  assert.equal(coffeeDecision(o, { beans: 0 }).action, 'reuse');
  assert.match(o.text, /不能认定已经凉/);
});
test('hot events from one sip share one attempt; further attempts back off 300, 600, 1200 seconds without permanent lock', () => {
  let hot = tastedCoffee(null, 'hot', start);
  for (let n = 1; n < 50; n++) hot = tastedCoffee(hot, 'hot', start + n * 10);
  assert.equal(hot.hotStreak, 1); assert.equal(hot.retryAt, start + 300000);
  let now = start + 300000;
  for (const seconds of [600, 1200, 1200]) {
    hot = tastedCoffee(hot, 'hot', now);
    assert.equal(hot.retryAt - now, seconds * 1000);
    assert.equal(coffeeThermalView(hot, now + seconds * 1000 - 1).retry.state, 'wait');
    now += seconds * 1000;
    assert.equal(coffeeDecision(visible(hot, now), { beans: 0 }).action, 'reuse');
  }
});
test('reload preserves coarse evidence and retry deadline, with no exact temperature or volume restored', () => {
  const hot = tastedCoffee(null, 'hot', start);
  const input = saved({ cup: { ...hot, temp: 70.123, ml: 68.4 } }, { cup: { at: start, level: 'half', ml: 68.4 } });
  const out = restoreCoffeeEvidence(JSON.parse(JSON.stringify(input)), start + 35000);
  assert.deepEqual(out.thermal.cup, hot);
  assert.deepEqual(out.levels.cup, { at: start, level: 'half' });
  assert.equal(coffeeThermalView(out.thermal.cup, start + 35000).retry.state, 'wait');
  assert.equal(JSON.stringify(out).includes('70.123'), false); assert.equal(JSON.stringify(out).includes('68.4'), false);
});
test('rollback and future hot timestamps restart bounded caution; future warmth or levels cannot authorize drinking', () => {
  const hot = tastedCoffee(null, 'hot', start);
  const out = restoreCoffeeEvidence(saved({ cup: hot, other: tastedCoffee(null, 'warm', start) }, { cup: { at: start, level: 'half' } }), start - 10000);
  assert.equal(out.thermal.cup.uncertain, true); assert.equal(out.thermal.cup.retryAt, start - 10000 + C.retryMs);
  assert.equal(coffeeThermalView(out.thermal.cup, start - 10000).known, false);
  assert.equal(out.thermal.other, undefined); assert.deepEqual(out.levels, {});
  const corrupt = restoreCoffeeEvidence(saved({ cup: { ...hot, at: Infinity, retryAt: 0 } }), start);
  assert.equal(corrupt.thermal.cup.retryAt, start + C.retryMs);
});
test('corrupted saved deadline cannot shorten a remembered hot cooldown', () => {
  const hot = tastedCoffee(null, 'hot', start);
  const out = restoreCoffeeEvidence(saved({ cup: { ...hot, retryAt: 0 } }), start + 10000);
  assert.equal(out.thermal.cup.retryAt, start + C.retryMs);
});
test('malformed old attemptAt cannot shorten waiting; normal single-sip timestamps remain exact on reload', () => {
  const hot = tastedCoffee(null, 'hot', start);
  for (const distance of [30000, 300001, start]) {
    const restored = restoreCoffeeEvidence(saved({cup:{...hot,attemptAt:start-distance,retryAt:0}}),start+31000).thermal.cup;
    assert.equal(restored.attemptAt,start);assert.equal(restored.retryAt,start+C.retryMs);
    assert.equal(coffeeThermalView(restored,start+31000).retry.state,'wait');
  }
  const normal={...hot,at:start+20000};
  assert.deepEqual(restoreCoffeeEvidence(saved({cup:normal},{},start+20000),start+35000).thermal.cup,normal);
});
test('old saves and malformed schema do not synthesize knowledge from world truth or past intake', () => {
  for (const input of [undefined, {}, { version: 9, thermal: { cup: tastedCoffee(null, 'hot', start) } }, { world: { temp: 80 }, ledger: { drunk: 50 } }])
    assert.deepEqual(restoreCoffeeEvidence(input, start), { thermal: {}, levels: {} });
  assert.deepEqual(restoreCoffeeEvidence(saved({ cup: { at: start, temperature: 70, inferred: false } }), start).thermal, {});
  assert.deepEqual(restoreCoffeeEvidence(saved(JSON.parse('{"__proto__":{"at":1,"temperature":"hot","inferred":false}}')), start).thermal, {});
});
test('observed level memory permits approach with no beans but never authorizes blind filling', () => {
  const o = { visible: false, level: 'unknown', remembered: { level: 'half' }, ...coffeeThermalView(null, start) };
  assert.equal(coffeeDecision(o, { beans: 0 }).autoApproach, true);
  assert.equal(coffeeDecision(o, { beans: 9 }).action, 'observe');
  assert.equal(coffeeDecision({ ...o, remembered: { level: 'empty' } }, { beans: 0 }).autoApproach, false);
  assert.equal(coffeeDecision({ ...o, ...coffeeThermalView(tastedCoffee(null, 'hot', start), start) }, { beans: 0 }).action, 'wait');
  assert.equal(coffeeDecision({ ...o, visible: true, level: 'empty' }, { beans: 0 }).action, 'refuse');
});
test('level memory expires, and only a new real taste can establish warm; a new observed brew starts a new episode', () => {
  assert.deepEqual(restoreCoffeeEvidence(saved({}, { cup: { at: start, level: 'half' } }), start + C.levelMs + 1).levels, {});
  const hot = tastedCoffee(null, 'hot', start), warm = tastedCoffee(hot, 'warm', start + C.retryMs);
  assert.equal(coffeeThermalView(warm, warm.at).category, 'warm'); assert.equal(coffeeThermalView(warm, warm.at).pastHot, false);
  assert.equal(coffeeThermalView(warm, warm.at + C.tasteMs).category, 'unknown');
  const brewed = { at: start + 1000000, temperature: 'hot', inferred: true };
  assert.equal(tastedCoffee(brewed, 'hot', brewed.at + 10000).hotStreak, 1);
});
