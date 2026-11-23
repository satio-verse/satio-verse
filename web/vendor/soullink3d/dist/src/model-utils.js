// 由 tools/build_soullink.mjs 从 src/model-utils.ts 生成, 不要手改. 来源: SoullinkEmotion3D (Apache-2.0), 内含 soullink-emotion-sdk @ 02cd75ac.
export function normalizeLegacyMorphNames(json) {
    for (const mesh of json.meshes ?? [])if (!mesh.extras?.targetNames) {
        const names = mesh.primitives.find((p)=>p.extras?.targetNames?.length === p.targets?.length)?.extras?.targetNames;
        if (names) mesh.extras = {
            ...mesh.extras,
            targetNames: [
                ...names
            ]
        };
    }
}
export function canonicalMorphName(name) {
    const match = name.match(/Fcl_(.+)$/i);
    if (!match) return name;
    const [region, ...rest] = match[1].split('_');
    return `Fcl_${region.toUpperCase()}_${rest.join('_')}`;
}
