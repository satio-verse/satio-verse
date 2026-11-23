// 由 tools/build_soullink.mjs 从 vendor/soullink/facs/defaultFACSState.ts 生成, 不要手改. 来源: SoullinkEmotion3D (Apache-2.0), 内含 soullink-emotion-sdk @ 02cd75ac.
export function createDefaultFACSState(overrides = {}) {
    return {
        browInnerUp: 0,
        browOuterUp: 0,
        browDown: 0,
        eyeOpen: 1,
        eyeSmile: 0,
        eyeSquint: 0,
        eyeBlinkL: 0,
        eyeBlinkR: 0,
        mouthSmile: 0.04,
        mouthFrown: 0,
        mouthOpen: 0,
        mouthPucker: 0,
        gazeX: 0,
        gazeY: 0,
        headX: 0,
        headY: 0,
        headZ: 0,
        bodyX: 0,
        bodyY: 0,
        bodyZ: 0,
        blush: 0,
        tear: 0,
        sweat: 0,
        breath: 0.5,
        ...overrides
    };
}
export const defaultFACSState = createDefaultFACSState();
