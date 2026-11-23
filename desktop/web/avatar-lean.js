// 省内存地加载 VRM (桌宠用). 在第一次渲染、上传显卡之前改资源, 不改 web/ 里的模块:
//   - 贴图边长上限: 桌宠在屏幕上只有约 500x900 像素, 2048 的贴图白占内存;
//   - 变形目标 (表情) 按图元压缩: VRoid 的脸部网格 8 个图元共用一份顶点缓冲, three.js 按几何体建变形纹理,
//     每个图元都会得到一张覆盖全部顶点的 Float32 纹理 (AvatarSample_B: 8 x 8.6 MB). 每个图元只留自己用到的顶点;
//   - 可选去掉变形法线 (纹理再小一半; 卡通着色的脸看不出差别);
//   - 可选关掉 MSAA;
//   - 可选在上传之后释放贴图原图 (显卡里已经有一份; WebGL 上下文丢了就整页重载).
// 网格对象本身不换, 表情绑定 (three-vrm 的 expression、avatar-vrm.js 收集的形变列表) 都照常有效.
import * as THREE from 'three';
import { VRMAvatar } from '/static/avatar-vrm.js';

/** 从页面地址读实验开关 (自检对比用), 没给的用默认值. */
export function leanOptions(params, defaults) {
  const num = (k) => (params.has(k) ? Number(params.get(k)) : defaults[k]);
  const str = (k) => (params.has(k) ? params.get(k) : defaults[k]);
  return { tex: num('tex'), morph: str('morph'), free: num('free'), aa: num('aa'), pr: num('pr'), render: num('render') };
}

function textures(root) {
  const out = new Set();
  root.traverse((o) => {
    for (const m of [o.material].flat()) {
      if (!m) continue;
      for (const v of [...Object.values(m), ...Object.values(m.uniforms || {}).map((u) => u?.value)]) {
        if (v?.isTexture && v.image) out.add(v);
      }
    }
  });
  return out;
}

function capTextures(root, cap) {
  if (!cap) return 0;
  let n = 0;
  for (const tex of textures(root)) {
    const img = tex.image, w = img.width, h = img.height;
    if (!w || !h || Math.max(w, h) <= cap) continue;
    const k = cap / Math.max(w, h);
    const c = new OffscreenCanvas(Math.max(1, Math.round(w * k)), Math.max(1, Math.round(h * k)));
    c.getContext('2d').drawImage(img, 0, 0, c.width, c.height);
    img.close?.();
    tex.image = c;
    tex.needsUpdate = true;
    n++;
  }
  return n;
}

/** 只留 attr 里 order 列出的顶点 (order[i] = 原顶点号). */
function pick(attr, order) {
  const size = attr.itemSize, n = order.length;
  const out = new attr.array.constructor(n * size);
  if (attr.isInterleavedBufferAttribute) {
    for (let i = 0; i < n; i++) for (let k = 0; k < size; k++) out[i * size + k] = attr.getComponent(order[i], k);
  } else {
    const src = attr.array;
    for (let i = 0; i < n; i++) {
      const s = order[i] * size;
      for (let k = 0; k < size; k++) out[i * size + k] = src[s + k];
    }
  }
  const a = new THREE.BufferAttribute(out, size, attr.normalized);
  a.name = attr.name;
  return a;
}

/** 带变形目标的几何体只留自己索引用到的顶点; dropNormals 时去掉变形法线. 返回省下的变形纹理字节数 (估算). */
function leanMorphs(root, { compact, dropNormals }) {
  let saved = 0;
  root.traverse((o) => {
    const g = o.isMesh ? o.geometry : null;
    if (!g?.morphAttributes?.position?.length) return;
    const targets = g.morphAttributes.position.length;
    const perVertexBefore = targets * 16 * (g.morphAttributes.normal ? 2 : 1);
    const verticesBefore = g.attributes.position.count;
    if (dropNormals) delete g.morphAttributes.normal;
    if (compact && g.index) {
      const idx = g.index.array, n = g.attributes.position.count;
      const remap = new Int32Array(n).fill(-1);
      let used = 0;
      for (let i = 0; i < idx.length; i++) if (remap[idx[i]] < 0) remap[idx[i]] = used++;
      if (used < n) {
        const order = new Uint32Array(used);
        for (let v = 0; v < n; v++) if (remap[v] >= 0) order[remap[v]] = v;
        const ng = new THREE.BufferGeometry();
        for (const [name, attr] of Object.entries(g.attributes)) ng.setAttribute(name, pick(attr, order));
        for (const [name, list] of Object.entries(g.morphAttributes)) ng.morphAttributes[name] = list.map((a) => pick(a, order));
        ng.morphTargetsRelative = g.morphTargetsRelative;
        const ni = new (used > 65535 ? Uint32Array : Uint16Array)(idx.length);
        for (let i = 0; i < idx.length; i++) ni[i] = remap[idx[i]];
        ng.setIndex(new THREE.BufferAttribute(ni, 1));
        for (const gr of g.groups) ng.addGroup(gr.start, gr.count, gr.materialIndex);
        ng.name = g.name;
        ng.userData = g.userData;
        ng.computeBoundingBox();
        ng.computeBoundingSphere();
        o.geometry = ng;
        g.dispose();
      }
    }
    const geo = o.geometry;
    saved += verticesBefore * perVertexBefore - geo.attributes.position.count * targets * 16 * (geo.morphAttributes.normal ? 2 : 1);
  });
  return saved;
}

function swapRenderer(v, antialias) {
  const old = v.renderer;
  const r = new THREE.WebGLRenderer({ antialias, alpha: true, powerPreference: 'high-performance' });
  r.setPixelRatio(old.getPixelRatio());
  r.outputColorSpace = old.outputColorSpace;
  r.setClearColor(0x000000, 0);
  r.domElement.className = old.domElement.className;
  old.domElement.replaceWith(r.domElement);
  old.dispose();
  old.forceContextLoss();
  v.renderer = r;
}

/**
 * 加载角色并按 opts 省内存. 返回 { avatar, report }. report 记下每项做了什么, 写进自检结果.
 * opts: { tex: 贴图边长上限 (0 不限), morph: 'keep'|'compact'|'nonormal'|'compact-nonormal', aa: 1/0, pr: 像素比上限 }
 */
export async function loadLeanAvatar(container, url, opts) {
  const v = new VRMAvatar(container);
  if (!opts.aa) swapRenderer(v, false);
  try {
    await v.load(url);
  } catch (err) {
    v.dispose();
    throw err;
  }
  const report = { tex: opts.tex, morph: opts.morph, aa: opts.aa, pr: opts.pr };
  report.textures_capped = capTextures(v.vrm.scene, opts.tex);
  report.morph_saved_mb = +(leanMorphs(v.vrm.scene, {
    compact: opts.morph.startsWith('compact'), dropNormals: opts.morph.endsWith('nonormal') }) / 1048576).toFixed(1);
  v.renderer.setPixelRatio(Math.min(devicePixelRatio || 1, opts.pr));
  v._resize();
  // 上下文丢了 (显卡重置、驱动更新) 时整页重载; 释放了贴图原图的话没法原地恢复
  v.renderer.domElement.addEventListener('webglcontextlost', () => setTimeout(() => location.reload(), 500));
  return { avatar: v, report };
}

/** 贴图都上传显卡之后释放原图 (同一个原图的所有贴图都上传了才放). 返回释放了几张. */
export function freeTextureSources(v) {
  const props = v.renderer.properties;
  const bySource = new Map();
  for (const tex of textures(v.vrm.scene)) {
    const list = bySource.get(tex.source) || [];
    list.push(tex);
    bySource.set(tex.source, list);
  }
  let n = 0;
  for (const [source, list] of bySource) {
    const img = source.data;
    if (!img || img.isPlaceholder || !list.every((t) => props.get(t).__webglTexture)) continue;
    img.close?.();
    source.data = { width: img.width, height: img.height, isPlaceholder: true };
    n++;
  }
  return n;
}
