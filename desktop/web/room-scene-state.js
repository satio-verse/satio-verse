// Scene identity and local spatial storage. Character needs/practice/body remain
// global; callers use this adapter only for spatial state and scene experience.
export const LEGACY_ROOM_SCENES = new Set(['petal-oak', 'petal-oak-life-v1']);
export const LEGACY_COGNITION_SCENE = 'petal-oak-life-v1';
export const DEFAULT_ROOM_SCENE = 'casa-luce-v8';

export function checkedSceneId(value) {
  if (typeof value !== 'string' || !/^[a-z0-9][a-z0-9-]{0,63}$/.test(value)) throw new Error('小屋场景编号无效');
  return value;
}

// The two old visual profiles share the historical cognition/experience store.
export function roomProtocolSceneId(sceneId) {
  checkedSceneId(sceneId);
  return LEGACY_ROOM_SCENES.has(sceneId) ? LEGACY_COGNITION_SCENE : sceneId;
}

export function roomStorageKey(sceneId, key) {
  checkedSceneId(sceneId);
  if (typeof key !== 'string' || !key.startsWith('pet.room.')) throw new Error('不是小屋存储键');
  return LEGACY_ROOM_SCENES.has(sceneId) ? key : `pet.room.scene.${sceneId}.${key.slice(9)}`;
}

function localStore() { try { return globalThis.localStorage ?? null; } catch { return null; } }

export function createRoomStorage(sceneId, storage = localStore()) {
  checkedSceneId(sceneId);
  return {
    sceneId,
    getItem(key) { try { return storage?.getItem(roomStorageKey(sceneId, key)) ?? null; } catch { return null; } },
    setItem(key, value) { storage?.setItem(roomStorageKey(sceneId, key), value); },
    removeItem(key) { storage?.removeItem(roomStorageKey(sceneId, key)); },
  };
}

const SCOPED_REPLIES = new Set(['room_observe', 'room_search', 'room_known', 'room_afford_done', 'room_carry', 'room_carry_done']);
export function withRoomSceneServer(sceneId, server) {
  const protocolScene = roomProtocolSceneId(sceneId);
  if (!server) return server;
  const scoped = (payload) => ({ ...(payload || {}), scene_id: protocolScene });
  return {
    ...server,
    tell(type, payload) { return server.tell?.(type, scoped(payload)); },
    async ask(type, payload, ...rest) {
      const response = await server.ask?.(type, scoped(payload), ...rest);
      if (response?.scene_id !== undefined && response.scene_id !== protocolScene) return null;
      if (!LEGACY_ROOM_SCENES.has(sceneId) && SCOPED_REPLIES.has(type) && response?.scene_id !== protocolScene) return null;
      return response ?? null;
    },
  };
}

export function withRoomSceneThink(sceneId, think) {
  const protocolScene = roomProtocolSceneId(sceneId);
  if (!think) return think;
  return (payload, ...rest) => {
    const pending = think({ ...(payload || {}), scene_id: protocolScene }, ...rest);
    // Disconnected roomThink returns null synchronously: Life uses that to avoid
    // starting its question cooldown when no question was actually sent.
    if (!pending) return pending;
    return Promise.resolve(pending).then((response) => {
      if (response?.scene_id !== undefined && response.scene_id !== protocolScene) return null;
      if (!LEGACY_ROOM_SCENES.has(sceneId) && response?.scene_id !== protocolScene) return null;
      return response ?? null;
    });
  };
}
