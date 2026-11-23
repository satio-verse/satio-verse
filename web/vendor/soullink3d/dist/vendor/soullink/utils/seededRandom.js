// 由 tools/build_soullink.mjs 从 vendor/soullink/utils/seededRandom.ts 生成, 不要手改. 来源: SoullinkEmotion3D (Apache-2.0), 内含 soullink-emotion-sdk @ 02cd75ac.
export function seededRandom(seed) {
    let value = Math.abs(Math.floor(seed)) || 1;
    return ()=>{
        value = (value * 9301 + 49297) % 233280;
        return value / 233280;
    };
}
