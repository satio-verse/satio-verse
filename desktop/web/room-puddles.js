// 水渍、印子 (液体那边的一摊摊: room-liquid.js 的 _draw) 用两张实例化网格画: 湿的一张 (不透明度 0.8)、干了的印子一张 (0.22).
// 2026-10-27 量的 (知识库「小屋性能实验记录」): 以前每一摊一个网格、一份几何、一份材质, 每帧画一次; 咖啡、牛奶干了的印子不会消失,
// 冲一次咖啡洒一路就留下 17-37 摊, 攒几十次就是几百上千次绘制. 这里每帧比一下每一摊的位置、大小、种类、干没干, 变了才改那一个实例.
// 画出来的样子同原来 (同样的圆、颜色、高度、不透明度; 只是同一张里的几摊之间不再按远近排序, 都是贴地的薄片, 看不出来).
// 量过 (2026-10-27): 合成 500 个印子时每帧多 47 次绘制 (其余在镜头外被剔掉)、渲染多 0.36 ms、_draw 多 0.05 ms, 随印子数线性涨.
// 物理那边 (LIQUID.version 5) 印子不按条数删, 只在同一处 5 cm 内并成一块, 块数随洒过的不同地方涨; 这里不管多少块都是 2 次绘制,
// update 每帧 500 块 0.034 ms、2000 块 0.14 ms (node 量的). room-liquid.js 构造时建、_draw 里调 update; perfoff=puddles 换回每摊一个网格.
import * as THREE from 'three';

const LIFT = 0.0015;  // 比落点高一点, 不和台面、地面抢深度 (同原来)

class Sheet {
  constructor(group, opacity, name) {
    this.group = group;
    this.opacity = opacity;
    this.name = name;
    this.cap = 0;
    this.mesh = null;
    this.ids = [];       // 第 i 个实例是哪一摊
    this.key = [];       // 第 i 个实例上次写的 (x, y, z, r, 颜色)
    this._m = new THREE.Matrix4();
    this._q = new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(1, 0, 0), -Math.PI / 2);
    this._p = new THREE.Vector3();
    this._s = new THREE.Vector3();
    this._c = new THREE.Color();
  }

  _grow(n) {
    let cap = Math.max(16, this.cap);
    while (cap < n) cap *= 2;
    if (cap === this.cap) return;
    const old = this.mesh;
    const mat = old?.material || new THREE.MeshStandardMaterial({ transparent: true, roughness: 0.1, depthWrite: false, opacity: this.opacity });
    const geo = old?.geometry || new THREE.CircleGeometry(1, 32);
    const mesh = new THREE.InstancedMesh(geo, mat, cap);
    mesh.instanceMatrix.setUsage(THREE.DynamicDrawUsage);
    mesh.setColorAt(0, this._c.set('#888'));
    mesh.renderOrder = 2;
    mesh.frustumCulled = false;  // 实例散在房间各处, 包围球按第一个算会错
    mesh.name = this.name;
    if (old) { this.group.remove(old); old.dispose(); }
    this.group.add(mesh);
    this.mesh = mesh;
    this.cap = cap;
    this.key = [];  // 新的一张: 全部重写
  }

  /** puddles: 液体那边的全部; wet: 这一张画湿的 (true) 还是干了的印子 (false). 每帧调, 只改变了的实例, 不分配新对象. */
  set(puddles, wet, colorOf) {
    let n = 0;
    for (const p of puddles) if ((p.ml > 0) === wet) n++;
    if (n > this.cap) this._grow(n);
    if (!this.mesh) return;
    const m = this.mesh;
    let i = 0, dirty = false;
    for (const p of puddles) {
      if ((p.ml > 0) !== wet) continue;
      const r = Math.max(0.01, wet ? p.r : Math.max(p.r, 0.02)), color = colorOf(p.kind), k = this.key[i];
      if (!(k && k[0] === p.at[0] && k[1] === p.at[1] && k[2] === p.at[2] && k[3] === r && k[4] === color)) {
        this._p.set(p.at[0], p.at[1] + LIFT, p.at[2]);
        this._s.set(r, r, r);
        m.setMatrixAt(i, this._m.compose(this._p, this._q, this._s));
        m.setColorAt(i, this._c.set(color));
        this.key[i] = [p.at[0], p.at[1], p.at[2], r, color];
        dirty = true;
      }
      i++;
    }
    if (m.count !== n) { m.count = n; this.key.length = n; }
    if (dirty) { m.instanceMatrix.needsUpdate = true; if (m.instanceColor) m.instanceColor.needsUpdate = true; }
    m.visible = n > 0;
  }
}

/** 液体那边的一摊摊 (world.puddles) 画到 group 里. colorOf(kind) 给颜色. */
export class PuddleSheets {
  constructor(group, colorOf) {
    this.colorOf = colorOf;
    this.wet = new Sheet(group, 0.8, 'puddles_wet');
    this.dry = new Sheet(group, 0.22, 'puddles_stain');  // 干了的印子淡一些
    // 一开始就建好 (空的、不显示): 载入后的预编译 (room-life.js) 能把它们的着色器先编好, 第一次洒的时候不卡
    for (const s of [this.wet, this.dry]) { s._grow(16); s.mesh.count = 0; s.mesh.visible = false; }
  }

  update(puddles) {
    this.wet.set(puddles, true, this.colorOf);
    this.dry.set(puddles, false, this.colorOf);
  }

  get draws() { return (this.wet.mesh?.visible ? 1 : 0) + (this.dry.mesh?.visible ? 1 : 0); }

  /** 现在画着几摊 (湿的加干的; 自检报告的 puddle_meshes 用). */
  get count() { return (this.wet.mesh?.count || 0) + (this.dry.mesh?.count || 0); }
}
