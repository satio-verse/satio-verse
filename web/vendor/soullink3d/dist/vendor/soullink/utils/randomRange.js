// 由 tools/build_soullink.mjs 从 vendor/soullink/utils/randomRange.ts 生成, 不要手改. 来源: SoullinkEmotion3D (Apache-2.0), 内含 soullink-emotion-sdk @ 02cd75ac.
export function randomRange(range, random) {
    return range[0] + (range[1] - range[0]) * random();
}
export function pickOne(items, random) {
    return items[Math.floor(random() * items.length) % items.length];
}
