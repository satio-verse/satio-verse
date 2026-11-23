// 角色骨架的 updateMatrixWorld(true) 快速版: 结果和 three.js 的一样 (同样的算式、同样的输入, 逐位相同), 只是没变的节点不再重算.
// 2026-10-27 量的 (知识库「小屋性能实验记录」): 行动层、接触层、运动学监测、姿势层每帧把整个角色 (几百个节点) 强制重算 9-12 次,
// 每次 0.3 ms 左右, 一帧 3-4 ms; 其实两次之间只动了几根骨头.
//
// 每个节点记下上次算矩阵时用的位置、朝向、缩放 (还有局部矩阵、算出来的世界矩阵), 下次:
//   - 局部的输入没变、父节点这一遍没重算、世界矩阵也还是上次写的那个 (没被别处改过) -> 不算, 原样留着 (和重算的结果一样);
//   - 否则照 three.js 的 Object3D.updateMatrixWorld 算 (compose、乘父节点), 子节点跟着重算.
// 自己重写了 updateMatrixWorld 的节点 (蒙皮网格、相机、弹簧骨的碰撞体…) 变了就交给它自己 (force), 没变就跳过 (它算的东西只看世界矩阵).
// 只装在角色的根 (vrm.scene) 上: 别处直接调某个骨头的 updateMatrixWorld / updateWorldMatrix 照旧走 three.js, 算完的世界矩阵和这里记的
// 对不上, 下一遍这里会重算它 (所以不会漏).
import * as THREE from 'three';
import { perfOn } from './perf-flags.js';

const BASE = THREE.Object3D.prototype.updateMatrixWorld;

/** 装上 (root: vrm.scene). 返回统计 {calls, nodes, recomputed}; 页面参数 perfoff=matrix 不装 (A/B 用). */
export function fastMatrices(root) {
  if (!perfOn('matrix') || !root || root.__fastMatrix) return null;
  const stats = { calls: 0, nodes: 0, recomputed: 0 };
  const memo = new WeakMap();  // 节点 -> Float64Array(10 + 16 + 16): 位置、朝向、缩放, 局部矩阵, 上次写的世界矩阵
  const own = root.updateMatrixWorld;  // 一般就是 BASE

  const visit = (o, parentChanged) => {
    stats.nodes++;
    if (o !== root && o.updateMatrixWorld !== BASE) {  // 自己重写过的: 变了交给它 (连同它下面的), 没变就跳过它自己, 子节点照常看
      let m = memo.get(o);
      const changed = parentChanged || !m || inputsChanged(o, m) || worldChanged(o, m);
      if (changed) {
        o.updateMatrixWorld(true);
        stats.recomputed++;
        if (!m) { m = new Float64Array(42); memo.set(o, m); }
        save(o, m);
        return;  // 它已经强制算过整棵子树
      }
      o.matrixWorldNeedsUpdate = false;
      const ch = o.children;
      for (let i = 0, l = ch.length; i < l; i++) visit(ch[i], false);
      return;
    }
    // 不看 matrixWorldNeedsUpdate: getWorldPosition 之类走的 updateWorldMatrix 会调 updateMatrix 把它设成真却不清掉, 几乎总是真的
    // (数值变没变由下面逐项比; 直接改了局部矩阵、世界矩阵的也比得出来)
    let m = memo.get(o), changed = parentChanged || !m;
    if (!changed && inputsChanged(o, m)) changed = true;
    if (!changed && worldChanged(o, m)) changed = true;
    if (stats.why && changed && !parentChanged) stats.why[o.name || o.type] = (stats.why[o.name || o.type] || 0) + 1;  // 调试: 哪些节点自己变了
    if (changed) {
      if (o.matrixAutoUpdate) o.updateMatrix();
      if (o.matrixWorldAutoUpdate === true) {
        if (o.parent === null) o.matrixWorld.copy(o.matrix);
        else o.matrixWorld.multiplyMatrices(o.parent.matrixWorld, o.matrix);
      }
      o.matrixWorldNeedsUpdate = false;
      stats.recomputed++;
      if (!m) { m = new Float64Array(42); memo.set(o, m); }
      save(o, m);
    }
    o.matrixWorldNeedsUpdate = false;
    const ch = o.children;
    for (let i = 0, l = ch.length; i < l; i++) visit(ch[i], changed);
  };

  const parentSeen = new Float64Array(16).fill(NaN);  // 根的父节点 (场景) 上次的世界矩阵: 变了整棵重算
  root.updateMatrixWorld = function (force) {
    if (this !== root) return own.call(this, force);
    stats.calls++;
    // force 为假时 three.js 也会因为 matrixAutoUpdate 把整棵树重算 (updateMatrix 把 matrixWorldNeedsUpdate 设成真), 结果同 force
    let moved = false;
    const pe = root.parent?.matrixWorld.elements;
    if (pe) for (let i = 0; i < 16; i++) if (parentSeen[i] !== pe[i]) { moved = true; parentSeen[i] = pe[i]; }
    visit(root, moved);
  };
  root.__fastMatrix = stats;
  return stats;
}

/** 局部输入 (位置、朝向、缩放; 不自动更新的看局部矩阵) 和上次算的时候比变没变. */
function inputsChanged(o, m) {
  if (o.matrixAutoUpdate) {
    const p = o.position, q = o.quaternion, s = o.scale;
    if (m[0] !== p.x || m[1] !== p.y || m[2] !== p.z || m[3] !== q._x || m[4] !== q._y || m[5] !== q._z || m[6] !== q._w
      || m[7] !== s.x || m[8] !== s.y || m[9] !== s.z) return true;
  }
  const e = o.matrix.elements;  // 局部矩阵被直接改过 (自动更新的也比: 和 three.js 一样每次 compose 会盖掉它)
  for (let i = 0; i < 16; i++) if (m[10 + i] !== e[i]) return true;
  return false;
}

/** 世界矩阵是不是还是上次这里写的 (别处改过就重算). */
function worldChanged(o, m) {
  const e = o.matrixWorld.elements;
  for (let i = 0; i < 16; i++) if (m[26 + i] !== e[i]) return true;
  return false;
}

function save(o, m) {
  const p = o.position, q = o.quaternion, s = o.scale;
  m[0] = p.x; m[1] = p.y; m[2] = p.z; m[3] = q._x; m[4] = q._y; m[5] = q._z; m[6] = q._w; m[7] = s.x; m[8] = s.y; m[9] = s.z;
  const e = o.matrix.elements, w = o.matrixWorld.elements;
  for (let i = 0; i < 16; i++) { m[10 + i] = e[i]; m[26 + i] = w[i]; }
}
