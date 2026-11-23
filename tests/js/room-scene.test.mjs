import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createSight, REGIONS, zoneAt } from '../../desktop/web/room-sight.js';
import { Known } from '../../desktop/web/room-known.js';
import { loadRoomScene, loadRoomAfford } from '../../desktop/web/room-scene-config.js';
import { createRoomStorage, roomStorageKey, roomProtocolSceneId, withRoomSceneServer, withRoomSceneThink } from '../../desktop/web/room-scene-state.js';

const ID = 'casa-luce-v8';
const copy = (x) => JSON.parse(JSON.stringify(x));
const sight = () => ({ version: 1, zones: { desk: [10, 20], nook: [15, 20] }, walk: {}, also: {},
  regions: { table: { zone: 'desk', at: [10, 1, 20], r: 0.5 }, drawer: { zone: 'nook', at: [15, 1, 20], r: 0.5, box: true } },
  items: ['cup'], names: { desk: '桌边', nook: '角落', table: '桌面', drawer: '抽屉', cup: '杯子' },
  kinds: { cup: 'cup' }, kindNames: { cup: '杯子' }, favorite: 'cup' });
const profile = () => ({ sceneId: ID, glb: `/pet/rooms/${ID}/room.glb`, sight: sight() });
function fakeFetch(files) {
  const calls = [];
  const fetcher = async (url) => { calls.push(url); return Object.hasOwn(files, url)
    ? { ok: true, json: async () => copy(files[url]) } : { ok: false, status: 404 }; };
  return { fetcher, calls };
}
function store() { const m = new Map(); return { m, getItem: (k) => m.get(k) ?? null, setItem: (k, v) => m.set(k, String(v)), removeItem: (k) => m.delete(k) }; }

test('each sight instance owns its map, without changing its input or legacy coordinates', () => {
  const definition = sight(), a = createSight(definition), b = createSight(definition), legacy = createSight();
  const old = copy(REGIONS);
  a.setPlace('table', [11, 2, 20]); a.ZONES.desk[0] = 11;
  assert.deepEqual(b.REGIONS.table.at, [10, 1, 20]);
  assert.deepEqual(definition.regions.table.at, [10, 1, 20]);
  assert.deepEqual(b.ZONES.desk, [10, 20]);
  assert.deepEqual(REGIONS, old);
  legacy.setPlace('surf_desk', [99, 99, 99]);
  assert.deepEqual(REGIONS, old);
  assert.equal(zoneAt(2.15, -1.24), 'coffee');
  assert.equal(a.zoneAt(11, 20), 'desk');
  assert.equal(b.zoneAt(10, 20), 'desk');
});

test('new sight still gates hidden contents and ignores unobserved liquid truth', () => {
  const map = createSight(sight()), me = { x: 10, z: 20 };
  const hidden = map.observe({ ...me, items: { cup: [15, 1, 20] } });
  assert.deepEqual(hidden, map.observe({ ...me, items: { cup: [99, 1, 99] }, levels: { cup: 100 } }));
  assert.deepEqual(hidden.seen, { table: [] });
  assert.deepEqual(map.observe({ x: 15, z: 20, items: { cup: [15, 1, 20] } }).seen, {});
  assert.deepEqual(map.observe({ x: 15, z: 20, items: { cup: [15, 1, 20] }, open: ['drawer'] }).seen, { drawer: ['cup'] });
  assert.deepEqual(map.standFor('desk', 'table').xz, [10, 20]);
  assert.throws(() => map.standFor('missing'), /站位/);
});

test('malformed new sight never falls back to a legacy map', () => {
  for (const mutate of [s => { delete s.walk; }, s => { s.version = 2; }, s => { delete s.names.cup; },
    s => { s.regions.table.zone = 'absent'; }, s => { s.zones.desk[0] = NaN; }, s => { s.items.push('cup'); },
    s => { s.favorite = 'absent'; }, s => { s.kinds.cup = 'unknown'; }, s => { delete s.favorite; },
    s => { s.items = []; s.favorite = null; }, s => { s.regions = {}; }]) {
    const s = sight(); mutate(s); assert.throws(() => createSight(s), /sight/);
  }
  assert.throws(() => createSight(null), /sight/);
});

test('explicit scene missing, invalid or mismatched rejects without requesting old resources', async () => {
  const missing = fakeFetch({});
  await assert.rejects(loadRoomScene(`?roomscene=${ID}`, missing), /404/);
  assert.deepEqual(missing.calls, [`/pet/room-scenes/${ID}.json`]);
  await assert.rejects(loadRoomScene('?roomscene=../secret', missing), /编号/);
  assert.equal(missing.calls.length, 1);
  for (const mutate of [p => { delete p.sceneId; }, p => { p.sceneId = 'other'; }, p => { delete p.sight; }]) {
    const p = profile(); mutate(p); const f = fakeFetch({ [`/pet/room-scenes/${ID}.json`]: p });
    await assert.rejects(loadRoomScene(`?roomscene=${ID}`, f));
    assert.equal(f.calls.length, 1);
  }
});

test('implicit legacy default may fall back and reports actual loaded scene identity', async () => {
  const f = fakeFetch({ '/pet/room-scenes/petal-oak.json': { glb: '/pet/rooms/petal-oak/room.glb' } }), logs = [];
  const loaded = await loadRoomScene('', { ...f, log: s => logs.push(s), defaultScene: 'petal-oak-life-v1' });
  assert.equal(loaded.sceneId, 'petal-oak');
  assert.equal(loaded.scene.sceneId, 'petal-oak');
  assert.equal(loaded.sightMap.zoneAt(2.15, -1.24), 'coffee');
  assert.equal(logs.length, 1);
  assert.deepEqual(f.calls, ['/pet/room-scenes/petal-oak-life-v1.json', '/pet/room-scenes/petal-oak.json']);
});

test('a future nonlegacy default fails explicitly; legacy cognition alias stays legacy', async () => {
  const f = fakeFetch({ '/pet/room-scenes/petal-oak.json': { glb: '/pet/rooms/petal-oak/room.glb' } });
  await assert.rejects(loadRoomScene('', { ...f, defaultScene: ID }), /404/);
  assert.deepEqual(f.calls, [`/pet/room-scenes/${ID}.json`]);
  assert.equal(roomProtocolSceneId('petal-oak'), 'petal-oak-life-v1');
  assert.equal(roomProtocolSceneId('petal-oak-life-v1'), 'petal-oak-life-v1');
  assert.equal(roomProtocolSceneId(ID), ID);
});

test('new afford uses only matching scene and requires authored outcomes', async () => {
  const key = `/pet/room-scenes/${ID}`, spec = { sceneId: ID, things: {}, acts: [{ id: 'explore' }] };
  for (const files of [{}, { [`${key}.afford.json`]: spec },
    { [`${key}.afford.json`]: spec, [`${key}.afford-world.json`]: { sceneId: ID, out: {} } },
    { [`${key}.afford.json`]: { ...spec, sceneId: 'other' } }]) {
    const f = fakeFetch(files); await assert.rejects(loadRoomAfford(ID, f));
    assert.ok(f.calls.every(url => url.startsWith(key)));
  }
  const f = fakeFetch({ [`${key}.afford.json`]: spec, [`${key}.afford-world.json`]: { sceneId: ID, out: { explore: { fun: 1 } } } });
  assert.deepEqual((await loadRoomAfford(ID, f)).acts[0].out, { fun: 1 });
  const legacy = fakeFetch({ '/pet/room-scenes/petal-oak.afford.json': { things: {}, acts: [{ id: 'explore' }] } });
  assert.deepEqual((await loadRoomAfford('petal-oak-life-v1', legacy)).acts[0].out, {});
});

test('spatial stores and Known persistence are isolated; old keys are retained', () => {
  const raw = store(), old = createRoomStorage('petal-oak', raw), oldVersion = createRoomStorage('petal-oak-life-v1', raw);
  const a = createRoomStorage(ID, raw), b = createRoomStorage('other-room', raw);
  for (const key of ['known', 'world', 'layout', 'liquid', 'rigid.poses']) {
    old.setItem(`pet.room.${key}`, 'legacy'); a.setItem(`pet.room.${key}`, 'new');
    assert.equal(oldVersion.getItem(`pet.room.${key}`), 'legacy');
    assert.equal(b.getItem(`pet.room.${key}`), null);
    a.removeItem(`pet.room.${key}`); assert.equal(old.getItem(`pet.room.${key}`), 'legacy');
  }
  const ka = new Known({ storage: a, now: () => 1000 }); ka.saw('books'); ka.tried('explore', 'stale', { felt: { fun: 2 } });
  assert.equal(new Known({ storage: a }).knows('books'), true);
  assert.equal(new Known({ storage: b }).knows('books'), false);
  assert.equal(raw.getItem('pet.room.known'), 'legacy');
  assert.equal(roomStorageKey(ID, 'pet.room.world'), 'pet.room.scene.casa-luce-v8.world');
  assert.throws(() => roomStorageKey('../bad', 'pet.room.world'));
  assert.throws(() => roomStorageKey(ID, 'unrelated'));
});

test('room requests retain their initiating scene and reject late or missing reply scope', async () => {
  const calls = []; let reply = { scene_id: ID, ok: true };
  const original = { connected: () => true, tell(type, p) { assert.equal(this, original); calls.push([type, p]); },
    async ask(type, p) { assert.equal(this, original); calls.push([type, p]); return reply; } };
  const a = withRoomSceneServer(ID, original), legacy = withRoomSceneServer('petal-oak', original);
  a.tell('room_carry_done', { delivered: true, scene_id: 'spoof' });
  assert.equal(calls.at(-1)[1].scene_id, ID);
  assert.equal((await a.ask('room_known', {})).ok, true);
  reply = { scene_id: 'petal-oak', ok: true }; assert.equal(await a.ask('room_known', {}), null);
  reply = { ok: true }; assert.equal(await a.ask('room_known', {}), null);
  assert.deepEqual(await legacy.ask('room_known', {}), { ok: true });
  assert.equal(calls.at(-1)[1].scene_id, 'petal-oak-life-v1');
  reply = { scene_id: 'petal-oak-life-v1', ok: true };
  assert.equal((await legacy.ask('room_known', {})).ok, true);
  let finish; original.ask = (_t, p) => new Promise(resolve => { calls.push(['delayed', p]); finish = resolve; });
  const pending = a.ask('room_carry', {}); const other = withRoomSceneServer('other-room', original);
  other.tell('room_observe', {}); finish({ scene_id: 'other-room', ok: true });
  assert.equal(await pending, null); assert.equal(calls.at(-2)[1].scene_id, ID);
});

test('think requests carry scope and reject plans from another scene', async () => {
  assert.equal(withRoomSceneThink(ID, () => null)({}), null);
  let got, result = { scene_id: ID, plan: 'explore' };
  const think = withRoomSceneThink(ID, async p => { got = p; return result; });
  assert.equal((await think({ scene_id: 'spoof' })).plan, 'explore'); assert.equal(got.scene_id, ID);
  result = { scene_id: 'petal-oak', plan: 'coffee' }; assert.equal(await think({}), null);
  result = { plan: 'coffee' }; assert.equal(await think({}), null);
  assert.deepEqual(await withRoomSceneThink('petal-oak', async () => result)({}), result);
});

test('real V8 profile and afford table satisfy the independent loader contract', async () => {
  const fetcher = async url => ({ ok: true, json: async () => JSON.parse(readFileSync(new URL(`../../desktop/web/${url.slice('/pet/'.length)}`, import.meta.url), 'utf8')) });
  const loaded = await loadRoomScene(`?roomscene=${ID}`, { fetcher });
  assert.equal(loaded.sceneId, ID); assert.equal(loaded.scene.glb, `/pet/rooms/${ID}/room.glb`);
  assert.ok(loaded.sightMap.ITEMS.length > 0);
  for (const id of loaded.sightMap.ITEMS) assert.equal(loaded.sightMap.KINDS[id], 'cup');
  const afford = await loadRoomAfford(ID, { fetcher });
  assert.deepEqual(afford.acts.map(a => a.id), ['explore']);
  assert.equal(afford.acts[0].out.fun, 1);
});
