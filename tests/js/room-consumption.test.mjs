import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import * as C from '../../desktop/web/room-consumption.js';
import { capacityMl } from '../../web/physics/liquid.js';

const raw = readFileSync(new URL('../../desktop/web/room-needs.js', import.meta.url), 'utf8');
const source = raw.replace(/^import .*;\r?$/gm, '').replace(/^export /gm, '') + '\nglobalThis.Needs = Needs;';
const copy = (o) => JSON.parse(JSON.stringify(o));
function env(saved = null) {
  let time = 100000, fail = false; const store = new Map(saved ? [['pet.room.needs', JSON.stringify(saved)]] : []);
  const localStorage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => { if (fail) throw Error('disk'); store.set(k, v); } };
  const ctx = vm.createContext({ ...C, localStorage, herDate: () => ({ getHours: () => 12 }) }); vm.runInContext(source, ctx);
  const options = { now: () => time, nutritionEnabled: false, nutritionCapabilities: { food: false, water: true }, actualConsumption: true, feedKinds: ['beans'] };
  return { make: (extra = {}) => new ctx.Needs({ ...options, ...extra }), tick: (s) => { time += s; }, fail: (b) => { fail = b; },
    saved: () => JSON.parse(store.get('pet.room.needs')), now: () => time };
}
const receipt = (sequence = 1, extra = {}) => ({ version: 1, sourceId: 'test-world', sequence, sceneId: 'casa-luce-v8', cup: 'cup', kind: 'coffee', ml: 10, temp: 40, at: 100000, ...extra });

test('only actual volume restores water; effects, unique IDs and high-water marks survive reload', () => {
  const e = env(), n = e.make(); const energy = n.v.energy, load = n.load, food = n.v.food;
  assert.equal(n.consumeLiquid(receipt()).ok, true); assert.equal(n.v.water, 71.2);
  assert.equal(n.coffeeBuff, .4); assert.equal(n.v.energy, energy); assert.equal(n.load, load); assert.equal(n.v.food, food);
  assert.equal(n.consumeLiquid(receipt()).duplicate, true); assert.equal(n.v.water, 71.2);
  const other = e.make(); assert.equal(other.consumeLiquid(receipt()).duplicate, true); assert.equal(other.v.water, 71.2);
  assert.equal(other.consumeLiquid(receipt(3)).reason, 'sequence-gap'); assert.equal(other.v.water, 71.2);
  assert.equal(other.consumeLiquid(receipt(2)).ok, true); assert.equal(other.v.water, 72.4);
  assert.equal(n.consumeLiquid(receipt(2)).duplicate, true, 'stale instance refreshes the saved settlement');
});
test('invalid, zero and non-liquid records give no effects', () => {
  const e = env(), n = e.make(), before = copy(n._record());
  for (const extra of [{ ml: 0 }, { ml: -1 }, { ml: NaN }, { kind: 'snack' }, { temp: Infinity }, { sourceId: '__proto__' }, { sequence: 1.5 }])
    assert.equal(n.consumeLiquid(receipt(1, extra)).ok, false);
  assert.deepEqual(copy(n._record()), before);
  n.add({ water: 30, food: 20, fun: 1 }); assert.equal(n.v.water, 70); assert.equal(n.v.food, 75); assert.equal(n.v.fun, 66);
});
test('coffee is capped, decays offline, and never erases sleep load or permanently adds energy', () => {
  const e = env(), n = e.make(); n.load = 3000;
  n.consumeLiquid(receipt(1, { ml: 500 })); assert.equal(n.v.water, 100); assert.equal(n.coffeeBuff, 6);
  e.tick(1800); assert.equal(n.coffeeBuff, 3); const reload = e.make(); assert.equal(reload.coffeeBuff, 3); assert.equal(reload.load, 3000);
  e.tick(1800); assert.equal(reload.coffeeBuff, 1.5);
});
test('paused food has no penalty while enabled water decays; feed whitelist and script inventory debit are enforced', () => {
  const e = env(), n = e.make(); e.tick(60); n.tick();
  assert.equal(n.v.food, 75); assert.equal(n.v.water, 69.8);
  assert.equal(n.canFeed('beans'), true); assert.equal(n.canFeed('drink'), false); assert.equal(n.canFeed('snack'), false);
  assert.equal(n.feed('drink'), null); assert.equal(n.use('beans'), false);
  n.save(); e.tick(3600); const reload = e.make(); assert.equal(reload.v.food, 75); assert.equal(reload.v.water, 57.8);
});
test('paused receipts are acknowledged without retroactive rewards when capability is enabled', () => {
  const e = env(), n = e.make({ nutritionCapabilities: { food: false, water: false } });
  assert.equal(n.consumeLiquid(receipt()).appliedMl, 0); assert.equal(n.v.water, 70);
  const reload = e.make(); assert.equal(reload.consumeLiquid(receipt()).duplicate, true); assert.equal(reload.v.water, 70);
});
test('failed Needs persistence changes neither effects nor sequence; retry commits once', () => {
  const e = env(), n = e.make(); e.fail(true);
  assert.equal(n.consumeLiquid(receipt()).reason, 'storage-unavailable'); assert.equal(n.v.water, 70); assert.equal(n.consumption.totalMl, 0);
  e.fail(false); assert.equal(n.consumeLiquid(receipt()).ok, true); assert.equal(n.v.water, 71.2);
});
test('physical preparation debits exactly once; empty/missing stock and ID conflicts fail', () => {
  const e = env(), n = e.make();
  assert.equal(n.prepareStock({ id: 'brew1', stock: 'beans', preparedMl: 0 }).ok, false); assert.equal(n.stock.beans, 3);
  assert.equal(n.prepareStock({ id: 'brew1', stock: 'beans', preparedMl: .1 }).ok, true); assert.equal(n.stock.beans, 2);
  assert.equal(e.make().prepareStock({ id: 'brew1', stock: 'beans', preparedMl: 60 }).duplicate, true);
  assert.equal(n.prepareStock({ id: 'brew1', stock: 'milk', preparedMl: 60 }).reason, 'preparation-conflict');
  n.stock.beans = 0; assert.equal(n.prepareStock({ id: 'brew2', stock: 'beans', preparedMl: .1 }).ok, false);
});
test('outbox is persisted before settlement, retries world-save failure, and survives crash before acknowledgment', () => {
  const e = env(), n = e.make(), box = new C.ConsumptionOutbox(null, () => 'world'); let disk = null, fail = true;
  const event = { type: 'drink', cup: 'cup', ml: 10, temp: 40 };
  box.drink(event, 'coffee', 'room', e.now()); box.drink(event, 'coffee', 'room', e.now());
  assert.equal(box.state.pending.length, 1); assert.equal(box.drink({ type: 'spill' }), null);
  const write = (s) => { if (fail) return false; disk = copy(s); return true; };
  assert.equal(box.flush(write, n), false); assert.equal(n.v.water, 70);
  fail = false; assert.equal(box.flush(write, n), true); assert.equal(n.v.water, 71.2); assert.equal(disk.pending.length, 1);
  const reload = new C.ConsumptionOutbox(disk); assert.equal(reload.flush(write, e.make()), true); assert.equal(e.saved().v.water, 71.2);
  const next = reload.drink({ type: 'drink', cup: 'cup', ml: 1, temp: 20 }, 'water', 'room', e.now()); assert.equal(next.sequence, 2);
});
test('failed preparation prevents pending consumption from settling, then retries using unchanged IDs', () => {
  const e = env(), n = e.make(), box = new C.ConsumptionOutbox(null, () => 'world');
  box.state.preparations.push({ id: 'p', stock: 'beans', preparedMl: 1 });
  box.drink({ type: 'drink', cup: 'cup', ml: 1, temp: 20 }, 'coffee', 'room', e.now());
  e.fail(true); assert.equal(box.flush(() => true, n), false); assert.equal(n.stock.beans, 3); assert.equal(n.v.water, 70);
  e.fail(false); assert.equal(box.flush(() => true, n), true); assert.equal(n.stock.beans, 2); assert.equal(n.v.water, 70.12);
});
test('migration starts an empty stream, never manufactures receipts from historical drunk totals', () => {
  const box = new C.ConsumptionOutbox(undefined, () => 'new'); assert.equal(box.state.sequence, 0); assert.deepEqual(box.state.pending, []);
});
test('low liquid solves clipped capacity; unreachable wrist budget and empty cup do not force a drink', () => {
  const cup = { R: .02677500043, H: .06050008261, ml: 30.008 };
  const target = C.sipTarget(cup, capacityMl); assert.equal(target.reachable, true);
  assert.ok(target.critical > 1.14 && target.critical < 1.15); assert.ok(capacityMl(cup.R, cup.H, Math.tan(target.angle)) < cup.ml);
  assert.equal(C.sipTarget({ ...cup, ml: 15 }, capacityMl).reason, 'wrist-budget');
  assert.equal(C.sipTarget({ ...cup, ml: 0 }, capacityMl).angle, 0);
  const full = C.sipTarget({ ...cup, ml: Math.PI * cup.R ** 2 * cup.H * 1e6 }, capacityMl);
  assert.equal(full.reachable, true); assert.ok(full.angle < .03);
});

test('effective alertness uses a temporary bounded bonus, without hiding fatigue or urgent base energy', () => {
  const e = env(), n = e.make(); n.v.energy = 20; n.load = 5700;
  n.consumeLiquid(receipt(1, { ml: 100 })); assert.equal(n.coffeeBuff, 4); assert.equal(n.effectiveEnergy, 24);
  assert.equal(n.summary().effectiveEnergy, 24); assert.equal(n.v.energy, 20); assert.equal(n.fatigue, .95);
  assert.equal(n.mood.label, '困得不行'); assert.equal(n.worst().key, 'energy');
  e.tick(1800); assert.equal(n.effectiveEnergy, 22); assert.equal(n.v.energy, 20);
  n.v.energy = 99; assert.equal(n.effectiveEnergy, 100);
});
