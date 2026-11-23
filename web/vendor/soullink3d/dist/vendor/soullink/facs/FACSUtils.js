// 由 tools/build_soullink.mjs 从 vendor/soullink/facs/FACSUtils.ts 生成, 不要手改. 来源: SoullinkEmotion3D (Apache-2.0), 内含 soullink-emotion-sdk @ 02cd75ac.
import { clamp } from "../utils/clamp.js";
import { lerp } from "../utils/lerp.js";
import { createDefaultFACSState, defaultFACSState } from "./defaultFACSState.js";
const directionalKeys = new Set([
    "gazeX",
    "gazeY",
    "headX",
    "headY",
    "headZ",
    "bodyX",
    "bodyY",
    "bodyZ"
]);
export const facsKeys = Object.keys(createDefaultFACSState());
export function facsRangeForKey(key) {
    if (directionalKeys.has(key)) return [
        -1,
        1
    ];
    if (key === "eyeOpen") return [
        0,
        1.25
    ];
    return [
        0,
        1
    ];
}
export function clampFACSValue(key, value) {
    const [min, max] = facsRangeForKey(key);
    return clamp(value, min, max);
}
export function normalizeFACS(partial) {
    const result = createDefaultFACSState();
    for (const key of facsKeys){
        const value = partial[key];
        result[key] = clampFACSValue(key, value ?? result[key]);
    }
    return result;
}
export function clampFACSState(state) {
    const result = {
        ...state
    };
    for (const key of Object.keys(result)){
        const value = result[key];
        if (typeof value === "number") {
            result[key] = clampFACSValue(key, value);
        }
    }
    return result;
}
export function mergeFACS(base, overlay) {
    return clampFACSState({
        ...base,
        ...overlay
    });
}
export function addFACS(base, overlay, weight = 1) {
    const result = {
        ...base
    };
    for (const key of Object.keys(overlay)){
        const value = overlay[key];
        if (typeof value === "number") {
            result[key] = (result[key] ?? 0) + value * weight;
        }
    }
    return clampFACSState(result);
}
export function scaleFACS(state, scale) {
    const result = {};
    for (const key of Object.keys(state)){
        const value = state[key];
        if (typeof value === "number") result[key] = value * scale;
    }
    return clampFACSState(result);
}
export function scaleFACSFromNeutral(state, scale) {
    const result = {};
    for (const key of Object.keys(state)){
        const value = state[key];
        if (typeof value === "number") {
            result[key] = lerp(defaultFACSState[key], value, scale);
        }
    }
    return clampFACSState(result);
}
export function interpolateFACS(from, to, t) {
    const result = {};
    const keys = new Set([
        ...Object.keys(from),
        ...Object.keys(to)
    ]);
    for (const key of keys){
        result[key] = lerp(from[key] ?? 0, to[key] ?? 0, t);
    }
    return clampFACSState(result);
}
