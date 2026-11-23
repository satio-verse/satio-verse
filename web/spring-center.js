// 弹簧骨"中心节点"的逆矩阵缓存, 不用 Proxy (2026-10-27, 先在桌宠里做的, 见知识库「小屋性能实验记录」; 网页版、桌宠共用这一份).
// three-vrm 3.x 设弹簧骨的 center 时, 给中心节点 (silvercat 是 Hips) 的世界矩阵装一个 Proxy: 把 matrixWorld.elements 换成 Proxy,
// 有人写就标记"逆矩阵要重算". three.js 的矩阵函数 (multiplyMatrices、updateMatrixWorld…) 碰到这个 Proxy 以后类型反馈变成多态,
// 之后对所有矩阵写小数都要先装箱: 每次矩阵乘法分配约 300 字节, 频繁小回收.
// 这里在载入模型之前先放一个不用 Proxy 的缓存 (读的时候比 16 个元素, 变了才重算; 和原来一样是这个矩阵此刻的逆, 结果逐位相同),
// three-vrm 的 center setter 看到已经有了就不再装 Proxy. avatar-vrm.js 一导入就装上 (桌宠经 /static/avatar-vrm.js 也是这一份).
// 页面参数 perfoff=proxy (或 perfoff=all, 和桌宠的 perf-flags.js 一样) 回到原来的, A/B 用.
import * as THREE from 'three';
import { VRMSpringBoneJoint } from '@pixiv/three-vrm';

export class InverseCache {
  constructor(matrix) {
    this.matrix = matrix;
    this._inverseCache = new THREE.Matrix4();
    this._seen = new Float64Array(16);
    this._stale = true;
  }

  get inverse() {
    const e = this.matrix.elements, s = this._seen;
    let changed = this._stale;
    // Object.is: 0 换成 -0 也重算 (原来是写了就重算; 这样不用去论证零的正负影响不到逆矩阵)
    for (let i = 0; i < 16; i++) if (!Object.is(s[i], e[i])) { s[i] = e[i]; changed = true; }
    if (changed) { this._inverseCache.copy(this.matrix).invert(); this._stale = false; }
    return this._inverseCache;
  }

  revert() {}
}

const off = String(new URLSearchParams(globalThis.location?.search || '').get('perfoff') || '').split(',');
/** 装上没有 (perfoff=proxy 关掉、或者 three-vrm 改了 center 的写法时没有). */
export const CENTER_CACHE = !off.includes('proxy') && !off.includes('all') && install();

function install() {
  const center = Object.getOwnPropertyDescriptor(VRMSpringBoneJoint.prototype, 'center');
  if (!center?.set || !center.get) return false;
  Object.defineProperty(VRMSpringBoneJoint.prototype, 'center', {
    configurable: true,
    get: center.get,
    set(t) {
      if (t?.userData && !t.userData.inverseCacheProxy) t.userData.inverseCacheProxy = new InverseCache(t.matrixWorld);
      center.set.call(this, t);
    },
  });
  return true;
}
