// 由 tools/build_soullink.mjs 从 vendor/soullink/utils/easing.ts 生成, 不要手改. 来源: SoullinkEmotion3D (Apache-2.0), 内含 soullink-emotion-sdk @ 02cd75ac.
import { clamp01 } from "./clamp.js";
export function ease(name, t) {
    const x = clamp01(t);
    if (name === "easeIn") return x * x;
    if (name === "easeOut") return 1 - (1 - x) * (1 - x);
    if (name === "easeInOut") return x < 0.5 ? 2 * x * x : 1 - Math.pow(-2 * x + 2, 2) / 2;
    return x;
}
