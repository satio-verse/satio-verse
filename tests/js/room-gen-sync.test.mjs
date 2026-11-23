// Execute the real pet.js sync block in two isolated VM pages sharing mock storage.
// No browser, real reset endpoint or user's storage is touched.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { GEN_KEY, roomGenPlan, roomGenKeys } from '../../desktop/web/room-gen.js';

const source = readFileSync(new URL('../../desktop/web/pet.js', import.meta.url), 'utf8');
const start = source.indexOf('// Capture before enterRoom loads Known/Practice.');
const end = source.indexOf('\n/**', start);
assert.ok(start >= 0 && end > start, 'production synchronization block must be present');
const syncSource = source.slice(start, end);
const nextTick = () => new Promise((resolve) => setImmediate(resolve));
const response = (info) => ({ ok: true, json: async () => info });
const info = (generation, scopes = ['experience']) => ({ generation,
  history: Array.from({ length: generation }, (_, n) => ({ gen: n + 1, scope: scopes })) });

function storage(values = []) {
  const data = new Map(values), faults = {};
  const api = {
    get length() { if (faults.read) throw Error('storage unavailable'); return data.size; },
    key(i) { return [...data.keys()][i] ?? null; },
    getItem(key) { if (faults.read) throw Error('storage unavailable'); return data.get(key) ?? null; },
    setItem(key, value) { if (faults.write) throw Error('quota'); data.set(key, String(value)); },
    removeItem(key) { if (faults.remove === key) throw Error('remove failed'); data.delete(key); },
  };
  return { api, data, faults };
}

function page(shared, { fetcher = async () => response(info(1)), room = true } = {}) {
  const listeners = {}, logs = [], requests = [];
  let reloads = 0, reloadFails = false, memory = { known: 'old in-memory room discovery' };
  const context = vm.createContext({ GEN_KEY, roomGenPlan, roomGenKeys, ROOM: room,
    localStorage: shared.api,
    fetch: async (...args) => { requests.push(args); return fetcher(...args); },
    log: (...args) => logs.push(args),
    location: { reload() { if (reloadFails) throw Error('navigation failed'); reloads++; memory = null; } },
    window: { addEventListener(name, handler) { listeners[name] = handler; } },
  });
  vm.runInContext(syncSource + '\nglobalThis.run = syncRoomGen; globalThis.state = () => ({ applied: roomGenApplied, pending: roomGenPending, busy: roomGenBusy });', context);
  return { run: context.run, state: () => JSON.parse(JSON.stringify(context.state())), logs, requests,
    get reloads() { return reloads; }, get memory() { return memory; },
    failReload(value) { reloadFails = value; },
    storageEvent(gen, storageArea = shared.api) { listeners.storage({ key: GEN_KEY, newValue: String(gen), storageArea }); },
  };
}

test('both loaded rooms reload even after the first updated the shared marker', async () => {
  const shared = storage([[GEN_KEY, '0'], ['pet.room.known', 'legacy'],
    ['pet.room.scene.casa-luce-v8.known', 'new'], ['pet.room.practice', 'practice'],
    ['pet.room.scene.casa-luce-v8.liquid', 'water'], ['pet.room.needs', 'needs']]);
  const first = page(shared), second = page(shared);
  await first.run(1);
  assert.equal(shared.data.get(GEN_KEY), '1');
  assert.equal(second.state().applied, 0, 'shared disk marker cannot acknowledge another page memory');
  assert.ok(second.memory);
  await second.run(1);
  assert.equal(first.reloads, 1);
  assert.equal(second.reloads, 1);
  assert.equal(second.memory, null);
  assert.equal(second.state().applied, 1);
  assert.equal(shared.data.has('pet.room.known'), false);
  assert.equal(shared.data.has('pet.room.scene.casa-luce-v8.known'), false);
  assert.equal(shared.data.get('pet.room.practice'), 'practice');
  assert.equal(shared.data.get('pet.room.scene.casa-luce-v8.liquid'), 'water');
  assert.equal(shared.data.get('pet.room.needs'), 'needs');
});

test('cross-window storage event reloads stale local memory without a websocket notice', async () => {
  const shared = storage([[GEN_KEY, '0'], ['pet.room.scene.casa-luce-v8.known', 'old']]);
  const first = page(shared), second = page(shared);
  await first.run(1);
  second.storageEvent(1);
  await nextTick();
  assert.equal(second.reloads, 1);
  assert.equal(second.state().applied, 1);
  assert.equal(second.memory, null);
});

test('fresh page captures the pre-load marker and preserves knowledge learned after it', async () => {
  const shared = storage([[GEN_KEY, '3'], ['pet.room.scene.casa-luce-v8.known', 'new knowledge']]);
  const fresh = page(shared);
  await fresh.run(3);
  assert.equal(fresh.requests.length, 0);
  assert.equal(fresh.reloads, 0);
  assert.equal(shared.data.get('pet.room.scene.casa-luce-v8.known'), 'new knowledge');
});

test('newer generation received while busy is fetched and applied rather than lost', async () => {
  const shared = storage([[GEN_KEY, '0'], ['pet.room.known', 'known'], ['pet.room.practice', 'practice']]);
  let release, calls = 0;
  const pending = new Promise((resolve) => { release = resolve; });
  const p = page(shared, { fetcher: async () => ++calls === 1 ? pending : response(info(2, ['experience', 'page'])) });
  const first = p.run(1);
  await p.run(2);
  assert.equal(p.state().pending, 2);
  release(response(info(1)));
  await first;
  assert.equal(calls, 2);
  assert.deepEqual(p.state(), { applied: 2, pending: 2, busy: false });
  assert.equal(p.reloads, 1);
  assert.equal(shared.data.has('pet.room.practice'), false);
});

test('new busy request survives failure of the preceding fetch', async () => {
  const shared = storage([[GEN_KEY, '0']]);
  let reject, calls = 0;
  const pending = new Promise((_, no) => { reject = no; });
  const p = page(shared, { fetcher: async () => ++calls === 1 ? pending : response(info(2)) });
  const first = p.run(1);
  await p.run(2);
  reject(Error('offline'));
  await first;
  assert.equal(calls, 2);
  assert.equal(p.state().applied, 2);
  assert.equal(p.reloads, 1);
});

test('offline scope is not acknowledged and the same generation can retry', async () => {
  const shared = storage([[GEN_KEY, '0'], ['pet.room.known', 'old']]);
  let online = false;
  const p = page(shared, { fetcher: async () => { if (!online) throw Error('offline'); return response(info(1)); } });
  await p.run(1);
  assert.equal(p.state().applied, 0);
  assert.equal(shared.data.get('pet.room.known'), 'old');
  assert.equal(p.reloads, 0);
  online = true;
  await p.run(1);
  assert.equal(p.state().applied, 1);
  assert.equal(p.reloads, 1);
});

for (const fault of ['read', 'write', 'remove']) test(`storage ${fault} failure never reports application success`, async () => {
  const shared = storage([[GEN_KEY, '0'], ['pet.room.known', 'old'], ['pet.room.scene.casa-luce-v8.known', 'old']]);
  const p = page(shared);
  shared.faults[fault] = fault === 'remove' ? 'pet.room.scene.casa-luce-v8.known' : true;
  await p.run(1);
  assert.equal(p.state().applied, 0);
  assert.equal(shared.data.get(GEN_KEY), '0');
  assert.equal(p.reloads, 0);
  assert.equal(p.logs.some((parts) => String(parts[1]).includes(' -> ')), false);
  delete shared.faults[fault];
  await p.run(1);
  assert.equal(p.state().applied, 1);
  assert.equal(p.reloads, 1);
});

test('reload failure preserves local retry even though the shared marker was written', async () => {
  const shared = storage([[GEN_KEY, '0'], ['pet.room.known', 'old']]);
  const p = page(shared);
  p.failReload(true);
  await p.run(1);
  assert.equal(shared.data.get(GEN_KEY), '1');
  assert.equal(p.state().applied, 0);
  assert.ok(p.memory);
  p.failReload(false);
  await p.run(1);
  assert.equal(p.reloads, 1);
  assert.equal(p.memory, null);
});

test('a later shared marker cannot be lowered or falsely acknowledge local scope', async () => {
  const shared = storage([[GEN_KEY, '0'], ['pet.room.known', 'old'], ['pet.room.practice', 'old']]);
  const p = page(shared, { room: false, fetcher: async () => response(info(2, ['experience', 'page'])) });
  shared.data.set(GEN_KEY, '2');
  await p.run(1);
  assert.equal(shared.data.get(GEN_KEY), '2');
  assert.equal(p.state().applied, 1);
  await p.run(2);
  assert.equal(p.state().applied, 2);
  assert.equal(p.reloads, 0);
});

test('unrelated storage areas and invalid generations do not clear anything', async () => {
  const shared = storage([[GEN_KEY, '0'], ['pet.room.known', 'old']]);
  const p = page(shared);
  p.storageEvent(1, {});
  for (const value of [0, -1, null, '1', 1.2, Infinity, Number.MAX_SAFE_INTEGER + 1]) await p.run(value);
  await nextTick();
  assert.equal(p.requests.length, 0);
  assert.equal(shared.data.get('pet.room.known'), 'old');
});
