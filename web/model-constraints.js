// Model-owned limits and collision groups are not replaced by host defaults.
// These optional corrective targets must be authored with the matching geometry.
export const CORRECTIVE_RULES = Object.freeze([
  ['corr_eyeBlinkWideLeft', 'eyeBlinkLeft', 'eyeWideLeft'],
  ['corr_eyeBlinkWideRight', 'eyeBlinkRight', 'eyeWideRight'],
  ['corr_jawOpenMouthClose', 'jawOpen', 'mouthClose'],
]);
const unit = (v) => Number.isFinite(v) ? Math.max(0, Math.min(1, v)) : 0;

/** Run after all base morphs are written, including VRM presets and procedural blink. */
export function applyModelCorrectives(meshes) {
  const values = {};
  for (const mesh of meshes) {
    const d = mesh.morphTargetDictionary, w = mesh.morphTargetInfluences;
    if (!d || !w) continue;
    for (const [target, a, b] of CORRECTIVE_RULES) {
      if (d[target] === undefined) continue;
      const value = unit(w[d[a]]) * unit(w[d[b]]);
      w[d[target]] = value;
      values[target] = Math.max(values[target] ?? 0, value);
    }
  }
  return values; // Diagnostics only; never feed these aggregate values back to meshes.
}

/** Only chains without usable authored collision assignments may receive fallback. */
export function springFallbackKind(vrm, joint) {
  if (joint.colliderGroups?.some((g) => g.colliders?.length)) return null;
  const h = vrm.humanoid;
  if (!h) return null;
  const named = new Map(Object.keys(h.humanBones ?? {}).map((n) => [h.getRawBoneNode(n), n]).filter(([b]) => b));
  for (let p = joint.bone; p; p = p.parent) {
    const human = named.get(p);
    if (!human) continue;
    if (human === 'head' || human === 'neck') return 'hair';
    if (/^(hips|leftUpperLeg|rightUpperLeg|leftLowerLeg|rightLowerLeg)$/.test(human)) return 'skirt';
    return null; // Unknown/chest/accessory chains need model-specific calibration.
  }
  return null;
}
