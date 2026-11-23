// Real Needs/panel source in an isolated VM; never touches browser/user storage.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import * as consumption from '../../desktop/web/room-consumption.js';

const KEY = 'pet.room.needs', HOUR = 3600, NOON = 12 * HOUR;
const source = readFileSync(new URL('../../desktop/web/room-needs.js', import.meta.url), 'utf8');
assert.equal((source.match(/^import /gm) || []).length, 2, 'review new module dependencies before extending the harness');
const executable = source.replace(/^import .* from '\.\/room-consumption\.js';$/m, '').replace(/^import \{ herDate \} from '\/pet\/tz\.js';$/m, '')
  .replace(/^export (const|class|function) /gm, '$1 ') + '\nglobalThis.api = { Needs, needsPanel, FEED };';
const plain = (value) => JSON.parse(JSON.stringify(value));
const close = (actual, expected) => assert.ok(Math.abs(actual - expected) < 1e-9, `${actual} != ${expected}`);

function environment(saved = null, { document } = {}) {
  let time = NOON, writes = 0;
  const data = new Map(saved ? [[KEY, JSON.stringify(saved)]] : []);
  const localStorage = {
    getItem: (key) => data.get(key) ?? null,
    setItem: (key, value) => { writes++; data.set(key, String(value)); },
  };
  const context = vm.createContext({ ...consumption, localStorage, document, setTimeout: (fn) => fn(),
    herDate: (ms) => ({ getHours: () => new Date(ms).getUTCHours() }) });
  vm.runInContext(executable, context);
  return { ...context.api, now: () => time, at(value) { time = value; },
    get saved() { return JSON.parse(data.get(KEY) || 'null'); },
    get writes() { return writes; },
    create(options = {}) { return new context.api.Needs({ now: () => time, ...options }); } };
}

const initial = (extra = {}) => ({ v: { energy: 80, food: 70, water: 60, fun: 80, social: 80 },
  stock: { drinks: 6, beans: 3, snacks: 2, milk: 1 }, fed: 4, load: 23, at: NOON, ...extra });

test('legacy constructor and old saves keep enabled nutrition and their original hourly decay', () => {
  const env = environment(initial());
  env.at(NOON + HOUR);
  const needs = env.create();
  assert.equal(needs.nutritionEnabled, true);
  assert.equal(needs.feedAllowed, true);
  close(needs.v.food, 61); close(needs.v.water, 48);
  assert.equal(env.writes, 0, 'unchanged legacy mode does not add a constructor write');
  env.at(NOON + HOUR + 60); needs.tick();
  close(needs.v.food, 60.85); close(needs.v.water, 47.8);
  assert.equal(env.saved.nutritionEnabled, true);
});

test('paused online time preserves nutrition and inventory while other needs still decay', () => {
  const env = environment(initial()), needs = env.create({ nutritionEnabled: false });
  const stock = plain(needs.stock);
  for (let n = 1; n <= 60; n++) { env.at(NOON + n * 60); needs.tick(); }
  close(needs.v.food, 70); close(needs.v.water, 60);
  close(needs.v.energy, 73); close(needs.v.fun, 71); close(needs.v.social, 74);
  assert.deepEqual(plain(needs.stock), stock);
  assert.equal(needs.fed, 4); assert.equal(needs.load, 23);
  assert.equal(env.saved.nutritionEnabled, false);
});

test('sleep still restores energy without changing paused food or water', () => {
  const env = environment(initial()), needs = env.create({ nutritionEnabled: false });
  needs.sleeping = true;
  env.at(NOON + 60); needs.tick();
  close(needs.v.energy, 80.75); close(needs.v.food, 70); close(needs.v.water, 60);
  close(needs.v.fun, 80 - 9 * 0.35 / 60);
});

test('entering pause applies previously enabled offline decay, then saves the exact mode boundary', () => {
  const env = environment(initial());
  env.at(NOON + HOUR);
  const needs = env.create({ nutritionEnabled: false });
  close(needs.v.food, 61); close(needs.v.water, 48);
  assert.equal(env.saved.at, NOON + HOUR);
  assert.equal(env.saved.nutritionEnabled, false);
  assert.equal(env.writes, 1);
  assert.deepEqual(env.saved.stock, initial().stock);
});

test('paused offline reload preserves old values, then resuming only decays future time', () => {
  const env = environment(initial({ nutritionEnabled: false }));
  env.at(NOON + 2 * HOUR);
  const needs = env.create();
  close(needs.v.food, 70); close(needs.v.water, 60);
  close(needs.v.fun, 62);
  assert.equal(env.saved.nutritionEnabled, true);
  assert.equal(env.saved.at, NOON + 2 * HOUR);
  env.at(NOON + 2 * HOUR + 60); needs.tick();
  close(needs.v.food, 69.85); close(needs.v.water, 59.8);
});

test('pause then resume works even when each room closes before its first tick', () => {
  const env = environment(initial());
  const paused = env.create({ nutritionEnabled: false });
  assert.equal(env.saved.nutritionEnabled, false);
  env.at(NOON + HOUR);
  const enabled = env.create();
  close(enabled.v.food, paused.v.food); close(enabled.v.water, paused.v.water);
  assert.equal(env.saved.nutritionEnabled, true);
  env.at(NOON + 2 * HOUR);
  const pausedAgain = env.create({ nutritionEnabled: false });
  close(pausedAgain.v.food, 61); close(pausedAgain.v.water, 48);
  assert.equal(env.saved.nutritionEnabled, false);
});

test('new paused room saves its state immediately, without refilling existing low values', () => {
  const fresh = environment(), needs = fresh.create({ nutritionEnabled: false });
  assert.equal(fresh.writes, 1);
  assert.equal(fresh.saved.nutritionEnabled, false);
  assert.equal(needs.v.food, 75); assert.equal(needs.v.water, 70);
  const low = environment(initial({ v: { energy: 80, food: 0, water: 2, fun: 80, social: 80 } }));
  const paused = low.create({ nutritionEnabled: false });
  assert.equal(paused.v.food, 0); assert.equal(paused.v.water, 2);
  assert.equal(low.saved.v.food, 0); assert.equal(low.saved.v.water, 2);
});

test('paused offline catch-up still caps other needs at twelve hours and leaves nutrition untouched', () => {
  const env = environment(initial({ nutritionEnabled: false }));
  env.at(NOON + 30 * HOUR);
  const needs = env.create({ nutritionEnabled: false });
  close(needs.v.food, 70); close(needs.v.water, 60);
  close(needs.v.social, 8);
  assert.deepEqual(plain(needs.stock), initial().stock);
});

test('paused nutrition is excluded from worst, mood and reported deficits, but keeps original values', () => {
  const env = environment(initial({ v: { energy: 100, food: 0, water: 1, fun: 100, social: 100 } }));
  const needs = env.create({ nutritionEnabled: false });
  assert.deepEqual(plain(needs.worst()), { key: null, gap: 0 });
  close(needs.mood.score, 100);
  assert.equal(needs.mood.label, '开心');
  const summary = plain(needs.summary());
  assert.equal(summary.mood, 'happy'); assert.equal(summary.score, 100);
  assert.deepEqual(summary.needs, []);
  assert.equal(summary.values.food, 0); assert.equal(summary.values.water, 1);
  assert.equal(summary.nutritionEnabled, false);
  needs.v.energy = 20;
  assert.equal(needs.worst().key, 'energy');
  assert.deepEqual(plain(needs.summary().needs), ['sleepy']);
  assert.equal(needs.summary().mood, 'sleepy');
});

test('enabled low nutrition still controls the original mood and summary', () => {
  const needs = environment(initial({ v: { energy: 100, food: 0, water: 10, fun: 100, social: 100 } })).create();
  assert.equal(needs.worst().key, 'food'); assert.equal(needs.mood.label, '饿了');
  assert.equal(needs.summary().mood, 'hungry');
  assert.deepEqual(plain(needs.summary().needs), ['hungry', 'thirsty']);
  close(needs.mood.score, 62);
});

test('all paused feeds and inventory uses have zero state, save or event side effects', () => {
  const env = environment(initial()), needs = env.create({ nutritionEnabled: false });
  const before = plain(needs.summary()), fed = needs.fed, saved = env.saved, writes = env.writes;
  let events = 0; needs.onChange(() => events++);
  for (const kind of [...Object.keys(env.FEED), 'unknown']) assert.equal(needs.feed(kind), null);
  for (const stock of [...Object.keys(needs.stock), 'unknown']) assert.equal(needs.use(stock), false);
  assert.deepEqual(plain(needs.summary()), before); assert.equal(needs.fed, fed);
  assert.deepEqual(env.saved, saved); assert.equal(env.writes, writes); assert.equal(events, 0);
});

test('nutrition rewards cannot pretend to restore unsupported nutrition while other rewards still work', () => {
  const needs = environment(initial()).create({ nutritionEnabled: false });
  needs.add({ food: 50, water: -30, fun: 5, social: 3 });
  close(needs.v.food, 70); close(needs.v.water, 60);
  close(needs.v.fun, 85); close(needs.v.social, 83);
});

test('enabled feed and use retain inventory, social reward and save behavior', () => {
  const env = environment(initial()), needs = env.create();
  const answer = needs.feed('beans');
  assert.ok(env.FEED.beans.thanks.includes(answer));
  assert.equal(needs.stock.beans, 7); assert.equal(needs.fed, 5);
  close(needs.v.social, 88); close(needs.v.fun, 83);
  assert.equal(needs.use('beans'), true); assert.equal(needs.stock.beans, 6);
  assert.equal(env.saved.stock.beans, 6);
});

// Small DOM stand-in for the public panel contract, including direct synthetic clicks.
function node() {
  const classes = new Set(), handlers = {};
  return { style: {}, dataset: {}, hidden: false, disabled: false, title: '', textContent: '', innerHTML: '',
    classList: { add: (...items) => items.forEach((item) => classes.add(item)),
      remove: (...items) => items.forEach((item) => classes.delete(item)),
      toggle: (item, on) => on ? classes.add(item) : classes.delete(item), contains: (item) => classes.has(item) },
    addEventListener: (event, fn) => { handlers[event] = fn; }, click: () => handlers.click?.(),
  };
}

function panelDocument() {
  const el = node(), chip = node(), card = node(), bars = node(), stock = node(), extra = node();
  const rows = Object.fromEntries(['energy', 'food', 'water', 'fun', 'social'].map((k) => {
    const row = node(), bar = node(), note = ['food', 'water'].includes(k) ? node() : null;
    row.querySelector = (selector) => selector === '[data-nutrition-note]' ? note : null;
    bar.parentElement = { parentElement: row };
    return [k, { row, bar, note }];
  }));
  const buttons = ['drink', 'beans', 'snack', 'milk'].map((kind) => ({ ...node(), dataset: { feed: kind } }));
  bars.querySelector = (selector) => rows[selector.match(/data-need="([^"]+)"/)[1]].bar;
  const selectors = { '.mood-chip': chip, '.mood-card': card, '.mood-bars': bars, '.mood-stock': stock,
    '.mood-extra': extra, '.mood-icon': node(), '.mood-label': node() };
  el.querySelector = (selector) => selectors[selector];
  el.querySelectorAll = (selector) => selector === '[data-feed]' ? buttons : [];
  return { document: { createElement: () => el }, el, chip, card, bars, extra, rows, buttons };
}

test('paused panel identifies both frozen bars and blocks all four feed buttons, including synthetic clicks', () => {
  const dom = panelDocument(), env = environment(initial(), { document: dom.document });
  const needs = env.create({ nutritionEnabled: false }), extra = ['已有练习记录'];
  let feeds = 0;
  env.needsPanel(needs, { feed: () => feeds++, extra: () => extra });
  assert.equal((dom.bars.innerHTML.match(/饮食适配中，暂停变化/g) || []).length, 2);
  for (const kind of ['food', 'water']) assert.equal(dom.rows[kind].note.hidden, false);
  for (const button of dom.buttons) {
    assert.equal(button.disabled, true); assert.match(button.title, /饮食适配中/); button.click();
    assert.equal(button.classList.contains('fed'), false);
  }
  assert.equal(feeds, 0);
  assert.match(dom.extra.innerHTML, /暂不可投喂/);
  assert.deepEqual(extra, ['已有练习记录'], 'render does not mutate caller-owned extra lines');
  needs.v.food = 0; needs.add({ fun: 1 });
  assert.equal(dom.rows.food.row.classList.contains('low'), false);
  assert.equal(dom.rows.food.bar.style.width, '0%');
});

test('legacy enabled panel keeps all feed controls usable and hides paused explanations', () => {
  const dom = panelDocument(), env = environment(initial(), { document: dom.document }), calls = [];
  env.needsPanel(env.create(), { feed: (kind) => calls.push(kind) });
  for (const kind of ['food', 'water']) assert.equal(dom.rows[kind].note.hidden, true);
  for (const button of dom.buttons) { assert.equal(button.disabled, false); button.click(); }
  assert.deepEqual(calls, ['drink', 'beans', 'snack', 'milk']);
  assert.doesNotMatch(dom.extra.innerHTML, /饮食适配中/);
});

test('water-only panel freezes food alone, enables only beans, and exposes temporary coffee feedback', () => {
  const dom = panelDocument(), env = environment(initial(), { document: dom.document });
  const needs = env.create({ nutritionEnabled: false, nutritionCapabilities: { food: false, water: true }, actualConsumption: true, feedKinds: ['beans'] });
  const calls = []; env.needsPanel(needs, { feed: (kind) => calls.push(kind), extra: () => [] });
  assert.equal(dom.rows.food.note.hidden, false); assert.equal(dom.rows.water.note.hidden, true);
  for (const button of dom.buttons) { assert.equal(button.disabled, button.dataset.feed !== 'beans'); button.click(); }
  assert.deepEqual(calls, ['beans']); assert.match(dom.extra.innerHTML, /饮水按实际入口结算/);
  needs.consumeLiquid({ version: 1, sourceId: 'panel', sequence: 1, sceneId: 'room', cup: 'cup', kind: 'coffee', ml: 10, temp: 30, at: NOON });
  assert.match(dom.extra.innerHTML, /暂时精神一点/); assert.equal(needs.v.food, 70); assert.equal(needs.v.water, 61.2);
});
