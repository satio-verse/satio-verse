// CPU-only imports of the same bundled dependencies used by the browser importmap.
export async function resolve(specifier, context, nextResolve) {
  let relative;
  if (specifier === 'three') relative = '../../web/vendor/three/build/three.module.js';
  else if (specifier === '@pixiv/three-vrm') relative = '../../web/vendor/three-vrm/three-vrm.module.min.js';
  else if (specifier.startsWith('three/addons/')) relative = '../../web/vendor/three/examples/jsm/' + specifier.slice(13);
  else if (specifier.startsWith('/static/')) relative = '../../web/' + specifier.slice(8);
  if (relative) return { url: new URL(relative, import.meta.url).href, shortCircuit: true };
  return nextResolve(specifier, context);
}
