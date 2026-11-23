// 由 tools/build_soullink.mjs 从 vendor/soullink/utils/clamp.ts 生成, 不要手改. 来源: SoullinkEmotion3D (Apache-2.0), 内含 soullink-emotion-sdk @ 02cd75ac.
export function clamp(value, min = -Infinity, max = Infinity) {
    return Math.min(max, Math.max(min, value));
}
export function clamp01(value) {
    return clamp(value, 0, 1);
}
