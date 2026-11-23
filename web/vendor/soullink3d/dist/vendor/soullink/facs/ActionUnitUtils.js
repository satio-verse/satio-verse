// 由 tools/build_soullink.mjs 从 vendor/soullink/facs/ActionUnitUtils.ts 生成, 不要手改. 来源: SoullinkEmotion3D (Apache-2.0), 内含 soullink-emotion-sdk @ 02cd75ac.
import { clamp } from "../utils/clamp.js";
import { actionUnitDefinitions, actionUnitKeys } from "./FACSActionUnitState.js";
import { createDefaultActionUnitState } from "./defaultActionUnitState.js";
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
const rangeByKey = Object.fromEntries(actionUnitDefinitions.map((definition)=>[
        definition.key,
        [
            definition.min,
            definition.max
        ]
    ]));
export function actionUnitRangeForKey(key) {
    return rangeByKey[key] ?? (directionalKeys.has(key) ? [
        -1,
        1
    ] : [
        0,
        1
    ]);
}
export function clampActionUnitValue(key, value) {
    const [min, max] = actionUnitRangeForKey(key);
    return clamp(value, min, max);
}
export function clampActionUnitState(state) {
    const result = {
        ...state
    };
    for (const key of Object.keys(result)){
        const value = result[key];
        if (typeof value === "number") {
            result[key] = clampActionUnitValue(key, value);
        }
    }
    return result;
}
export function normalizeActionUnits(partial) {
    const result = createDefaultActionUnitState();
    for (const key of actionUnitKeys){
        const value = partial[key];
        result[key] = clampActionUnitValue(key, value ?? result[key]);
    }
    return result;
}
export function addActionUnits(base, overlay, weight = 1) {
    const result = {
        ...base
    };
    for (const key of Object.keys(overlay)){
        const value = overlay[key];
        if (typeof value === "number") {
            result[key] = (result[key] ?? 0) + value * weight;
        }
    }
    return clampActionUnitState(result);
}
