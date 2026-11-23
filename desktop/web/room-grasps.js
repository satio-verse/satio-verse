// Explicitly reviewed object metadata. Do not infer a spine from every object's longest axis.
export function roomGrasp(asset, id, localSize) {
  if (asset !== '/pet/rooms/petal-oak-life-v1/room.glb' || id !== 'IR_Item_Library_01') return null;
  const expected = [0.298, 0.238606632, 0.05480194];
  if (!Array.isArray(localSize) || localSize.length !== 3
      || !Array.from(localSize).every((v, i) => Number.isFinite(v) && Math.abs(v - expected[i]) < 0.002)) return null;
  return {
    // The M022 body face at node x=.1409998089, minus the object's bbox centre (-1.3411045e-7).
    // Decorative cover strips extend to x=.1489999145 but do not cover this Y/Z location.
    version: 1, point: [0.1409999430179596, 0, 0], normal: [1, 0, 0], up: [0, 1, 0],
    preferredHand: 'left', positionTolerance: 0.01, angleTolerance: Math.PI / 18,
  };
}
