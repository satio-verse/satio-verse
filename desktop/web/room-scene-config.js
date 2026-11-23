import { checkedSceneId, DEFAULT_ROOM_SCENE, LEGACY_ROOM_SCENES } from './room-scene-state.js';
import { createSight } from './room-sight.js';

async function jsonAt(url, fetcher) {
  let response;
  try { response = await fetcher(url); } catch (error) { throw new Error(`小屋资源读不到: ${url} (${error.message})`); }
  if (!response?.ok) throw new Error(`小屋资源读不到: ${url} (HTTP ${response?.status ?? 'unknown'})`);
  try { return await response.json(); } catch { throw new Error(`小屋资源不是有效 JSON: ${url}`); }
}

export async function loadRoomScene(search, { fetcher = globalThis.fetch, log = () => {}, defaultScene = DEFAULT_ROOM_SCENE } = {}) {
  const params = new URLSearchParams(search), explicit = params.has('roomscene');
  let sceneId = checkedSceneId(explicit ? params.get('roomscene') : defaultScene);
  let url = `/pet/room-scenes/${sceneId}.json`, scene;
  try { scene = await jsonAt(url, fetcher); } catch (error) {
    if (explicit || !LEGACY_ROOM_SCENES.has(sceneId)) throw error;
    sceneId = 'petal-oak'; url = `/pet/room-scenes/${sceneId}.json`;
    log(`小屋: 默认房间读不到, 使用 ${sceneId}`);
    scene = await jsonAt(url, fetcher);
  }
  if (!scene || typeof scene !== 'object' || Array.isArray(scene)) throw new Error('小屋场景配置无效');
  if ((!LEGACY_ROOM_SCENES.has(sceneId) || scene.sceneId !== undefined) && scene.sceneId !== sceneId) throw new Error('小屋场景编号与请求不一致');
  if (!LEGACY_ROOM_SCENES.has(sceneId) && !scene.sight) throw new Error(`小屋 ${sceneId} 缺少独立 sight 地图`);
  if (typeof scene.glb !== 'string' || !scene.glb.startsWith('/pet/rooms/') || !scene.glb.endsWith('.glb')) throw new Error('小屋缺少有效的本地 GLB 地址');
  const sightMap = createSight(scene.sight);
  return { scene: { ...scene, sceneId }, sceneId, sightMap, url };
}

export async function loadRoomAfford(sceneId, { fetcher = globalThis.fetch } = {}) {
  checkedSceneId(sceneId);
  const legacy = LEGACY_ROOM_SCENES.has(sceneId), names = legacy ? [...new Set([sceneId, 'petal-oak'])] : [sceneId];
  for (const name of names) {
    let spec;
    try { spec = await jsonAt(`/pet/room-scenes/${name}.afford.json`, fetcher); } catch (error) {
      if (!legacy) throw error;
      continue;
    }
    if (!spec || typeof spec.things !== 'object' || !spec.things || Array.isArray(spec.things) || !Array.isArray(spec.acts)) throw new Error(`小屋 ${name} 的可供性表无效`);
    if (!legacy && spec.sceneId !== sceneId) throw new Error('小屋可供性表的场景编号与请求不一致');
    let world = null;
    try { world = await jsonAt(`/pet/room-scenes/${name}.afford-world.json`, fetcher); } catch (error) {
      if (!legacy && spec.acts.length) throw error;
    }
    if (world !== null && (!world.out || typeof world.out !== 'object' || Array.isArray(world.out))) throw new Error(`小屋 ${name} 的后果表无效`);
    if (!legacy && world !== null && (world.sceneId !== sceneId || spec.acts.some((act) => !Object.hasOwn(world.out, act.id)))) throw new Error('小屋后果表的场景编号或活动后果不完整');
    return { ...spec, acts: spec.acts.map((act) => ({ ...act, out: world?.out?.[act.id] || {} })) };
  }
  return null;
}
