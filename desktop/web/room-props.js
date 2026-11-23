// 小屋里能动的东西: 房间 GLB 里的冰箱门、咖啡机出水、杯子饮料书本这些小物件 (IR_Item_*)、能挪的家具 (MV_*),
// 以及投喂的东西和被子 (程序做的简单模型).
// 房间平时画一次存起来 (room-life.js 的 RoomCache), 所以要动的东西先挪进"活动层" (每帧照常画, 存房间时不画), 动完再放回房间、
// 房间重画一次. 小物件拿在手里时借行动层的拿放 (web/locomotion.js 的 pick/put: 走到够得着的地方、蹲下或伸手、手握住、拿着走):
// 给它一个看不见的方盒代理, 行动层摆代理, 这里每帧让房间里的那件东西跟着代理.
import * as THREE from 'three';
import { perfOn } from './perf-flags.js';
import { roomGrasp } from './room-grasps.js';
import { composePose, normalizePose } from '/static/grasp-transform.js';

const worldPose = (node) => ({ position: node.getWorldPosition(new THREE.Vector3()).toArray(),
  quaternion: node.getWorldQuaternion(new THREE.Quaternion()).toArray() });

// Pose metadata is world-space; the scene/live/proxy parents may have a rigid transform.
function setWorldPose(node, pose) {
  const p = normalizePose(pose), position = new THREE.Vector3(...p.position), q = new THREE.Quaternion(...p.quaternion);
  if (node.parent) {
    node.parent.updateWorldMatrix(true, false);
    node.parent.worldToLocal(position);
    q.premultiply(node.parent.getWorldQuaternion(new THREE.Quaternion()).invert());
  }
  node.position.copy(position); node.quaternion.copy(q); node.updateWorldMatrix(false, true);
}

// Static mesh bounds in the node's own frame, including child transforms, not a rotated world AABB.
function localBounds(node) {
  node.updateWorldMatrix(true, true);
  const inverse = node.matrixWorld.clone().invert(), box = new THREE.Box3();
  node.traverse((child) => {
    if (!child.isMesh || !child.geometry || child.isSkinnedMesh) return;
    child.geometry.computeBoundingBox();
    const b = child.geometry.boundingBox, m = inverse.clone().multiply(child.matrixWorld);
    if (!b) return;
    for (const x of [b.min.x, b.max.x]) for (const y of [b.min.y, b.max.y]) for (const z of [b.min.z, b.max.z]) {
      box.expandByPoint(new THREE.Vector3(x, y, z).applyMatrix4(m));
    }
  });
  return box;
}

const KEY = 'pet.room.layout';
const NODE_MEMO = perfOn('node');
const BASE = 0.065;
const FOLD = 0.85;   // 被子叠在床尾时盖住多长 (米): 她躺下以后伸手能够到边 (大腿那里)  // 被子比床面高多少: 床尾那条针织毯比被子面高 6 cm, 低了会从被子里透出来
const smooth = (t) => { const x = Math.max(0, Math.min(1, t)); return x * x * (3 - 2 * x); };
const UP = new THREE.Vector3(0, 1, 0);

// 手里拿着的东西按种类握 (用户说手和五指一点用都没有, 书拿不住、咖啡要洒):
//   cup  杯子、瓶子、罐子、水壶: 竖着, 杯身贴着手心, 手指绕着杯身 (走路时姿势层把它端在腰前, 见 room-poses.js 的 carry);
//   book 书、本子、小册子、零食袋: 书面贴着手心, 手握在书的一头 (夹在身侧时书垂在手下面); 掌心朝下时 (摊在腿上) 本子压在手下面,
//        往另一只手那边挪, 两只手之间;
//   soft 靠垫、小兔子、瑜伽砖: 照行动层的 (软的, 抱着就行).
// 以前东西只跟着手转水平朝向: 书平着悬在竖着的手掌旁边, 离手心按书的半高 (10 cm) 算, 看起来是飘着的.
export const gripOf = (id, fed) => (/Book|^doc_/i.test(id) || fed === 'snack' ? 'book' : /Bunny|Cushion|Block/i.test(id) ? 'soft' : 'cup');
// 每种握法的手形, 交给手部层 (web/hand.js 的 hold, 格式见 web/hand-shapes.js): 四指 (食指到小指) 各 [掌指, 近节, 远节, 张开],
// 拇指 [往掌心收, 往下弯, 指间] (度). 手部层按接触层的握紧程度混过去, 盖过它按"球 / 书"的通用握法.
//   cup 圈住杯身; book 手指顺着书面伸着、拇指压在另一面 (书面贴手心, 手指弯了会插进书里); flat 手掌按在摊开的本子上; soft 抱着软的东西
const HOLD = {
  cup: { f: [[55, 62, 40, 2], [58, 66, 42, 0], [60, 68, 44, -2], [62, 70, 45, -4]], t: [34, 26, 24] },
  book: { f: [[16, 20, 10, 2], [16, 20, 10, 0], [17, 21, 11, -2], [18, 22, 11, -4]], t: [40, 18, 14] },
  flat: { f: [[5, 8, 4, 3], [5, 8, 4, 0], [6, 9, 4, -3], [6, 9, 5, -6]], t: [8, 4, 4] },
  soft: { f: [[30, 36, 24, 5], [32, 38, 26, 0], [34, 40, 26, -4], [36, 42, 28, -8]], t: [18, 12, 12] },
};
const FINGERS = ['Index', 'Middle', 'Ring', 'Little'];

function readLayout(storage) {
  try { return JSON.parse(storage?.getItem(KEY) || 'null') || {}; } catch { return {}; }
}

/** 投喂的东西: 罐子、纸盒、零食袋、咖啡豆袋 (行动层按方盒/圆柱当"书本"拿, 握在身侧). */
function fedMesh(kind) {
  const std = (color, o = {}) => new THREE.MeshStandardMaterial({ color, roughness: 0.55, ...o });
  let mesh;
  if (kind === 'drink') {
    const side = std('#e8577a', { metalness: 0.4, roughness: 0.35 }), top = std('#d9d9de', { metalness: 0.8, roughness: 0.3 });
    mesh = new THREE.Mesh(new THREE.CylinderGeometry(0.033, 0.033, 0.122, 20), [side, top, top]);
  } else if (kind === 'milk') {
    const white = std('#f5f3ee'), blue = std('#7fb2e5');
    mesh = new THREE.Mesh(new THREE.BoxGeometry(0.072, 0.17, 0.072), [white, white, blue, white, blue, white]);
  } else if (kind === 'snack') {
    const bag = std('#f2a93b', { roughness: 0.4 }), band = std('#e2574c');
    mesh = new THREE.Mesh(new THREE.BoxGeometry(0.15, 0.19, 0.05), [band, band, band, band, bag, bag]);
  } else {
    const kraft = std('#9b6b43', { roughness: 0.8 }), label = std('#3d2a1e');
    mesh = new THREE.Mesh(new THREE.BoxGeometry(0.12, 0.17, 0.07), [kraft, kraft, label, kraft, kraft, kraft]);
  }
  mesh.castShadow = true;
  return mesh;
}

export class Props {
  /** room: 房间 GLB 的根; scene: 整个三维场景; cache: 房间缓存 (要重画时设 dirty); layer: 行动层. */
  constructor({ room, scene, cache, layer, storage }) {
    // Experimental until the full reach/read/return body constraints pass, not a saved user setting.
    this.surfaceGrasps = new URLSearchParams(location.search).get('surfacegrasp') === '1';
    Object.assign(this, { room, scene, cache, layer });
    try { this.storage = storage === undefined ? globalThis.localStorage : storage; } catch { this.storage = null; }
    this.live = new THREE.Group();
    this.live.name = 'live-props';
    scene.add(this.live);
    cache.hidden.push(this.live);  // 存房间时不画, 平时每帧画
    this.glb = {};      // 代理着的房间物件: id -> { node, proxy, offset, yaw0, settled }
    this.tweens = [];
    this.fed = [];      // 地上还没捡的投喂: { id, kind }
    this.stash = [];    // 收起来留着的投喂 (零食放在电脑桌上): { id, kind }
    this.tilts = {};
    this.fridgeOpen = false;
    this.n = 0;
    this.layout = readLayout(this.storage);
    this.mats = {};
    room.traverse((o) => { if (o.isMesh && o.material?.name) this.mats[o.material.name] ??= o.material; });
  }

  /** 按名字找房间里 (再找活动层里) 的节点. 记住找到的那个: 每帧液体、感知要找好几次, 每次都把房间 600 个节点挨个比一遍
   *  (2026-10-27 量的, 每帧 8 次); 记住的还叫这个名字、还在房间或活动层里才用, 不然重新找. */
  node(name) {
    if (!NODE_MEMO) return this.room.getObjectByName(name) || this.live.getObjectByName(name);
    const hit = (this._nodes ??= new Map()).get(name);
    if (hit && hit.name === name && this._inside(hit)) return hit;
    const found = this.room.getObjectByName(name) || this.live.getObjectByName(name);
    if (found) this._nodes.set(name, found);
    else this._nodes.delete(name);
    return found;
  }

  _inside(o) {
    for (let x = o.parent; x; x = x.parent) if (x === this.room || x === this.live) return true;
    return false;
  }

  /** 挪进活动层 (保持世界位置), 房间重画一次 (画面里不再有它). */
  toLive(node) {
    if (!node || node.parent === this.live) return node;
    this.live.attach(node);
    this.cache.dirty = true;
    return node;
  }

  /** 放回房间, 房间重画一次. */
  toRoom(node) {
    if (!node || node.parent === this.room) return node;
    this.room.attach(node);
    node.updateMatrixWorld(true);
    this.cache.dirty = true;
    return node;
  }

  /** 补间: dur 秒内每帧调 fn(0..1 平滑), 完了调 done. 返回 Promise. */
  tween(dur, fn) {
    return new Promise((resolve) => this.tweens.push({ t: 0, dur: Math.max(0.01, dur), fn, resolve }));
  }

  get moving() { return this.tweens.length > 0 || Object.values(this.glb).some((g) => !g.settled); }

  /** 每帧调 fn(dt) (被子跟着身体动这种); 返回取消的函数. */
  every(fn) {
    (this.hooks ??= new Set()).add(fn);
    return () => this.hooks.delete(fn);
  }

  /** 每帧 (画之前). */
  tick(dt) {
    for (const fn of this.hooks || []) fn(dt);
    for (const tw of this.tweens.slice()) {
      tw.t += dt;
      tw.fn(smooth(tw.t / tw.dur));
      if (tw.t >= tw.dur) {
        this.tweens.splice(this.tweens.indexOf(tw), 1);
        tw.resolve();
      }
    }
    // 放下以后 0.6 秒没再动: 放回房间 (平时不用每帧画它)
    for (const g of Object.values(this.glb)) {
      if (g.settled || g.pending || this.layer.items[g.id]?.held) continue;
      if ((g.still = (g.still || 0) + dt) > 0.6) {
        g.settled = true;
        this.toRoom(g.node);
        this._remember(g);
      }
    }
  }

  /** 行动层每次摆完手里的东西之后: 房间物件跟着各自的代理. */
  follow() {
    for (const g of Object.values(this.glb)) {
      const item = this.layer.items[g.id];
      if (!item || g.settled) continue;
      // 交给刚体的 (room-rigid.js, 小窝物理 P1/P2): 位置、朝向由刚体摆, 这里不摆, 只记拿没拿着
      if (item.physical) { if (item.held) { g.pending = false; g.still = 0; } g.wasHeld = !!item.held; continue; }
      if (item.grasp && g.graspNodePose) {
        // The action layer owns the complete rigid object pose. Never reposition its visible mesh independently.
        setWorldPose(g.node, composePose(worldPose(item.mesh), g.graspNodePose));
        g.wasHeld = !!item.held;
        if (item.held) { g.pending = false; g.still = 0; }
        continue;
      }
      if (item.held && !g.wasHeld) g.proxyYaw0 = item.mesh.rotation.y - g.yawNow;  // 握住的那一刻: 东西的朝向不跳
      g.wasHeld = !!item.held;
      if (item.held) g.pending = false;
      const p = item.mesh.position;
      const yaw = (g.yawNow = item.mesh.rotation.y - g.proxyYaw0);
      const off = g.offset.clone().applyAxisAngle(new THREE.Vector3(0, 1, 0), yaw);
      g.node.position.set(p.x + off.x, p.y + off.y, p.z + off.z);
      g.node.quaternion.copy(new THREE.Quaternion().setFromAxisAngle(new THREE.Vector3(0, 1, 0), yaw).multiply(g.q0));
      if (g.tilt) g.node.quaternion.premultiply(g.tilt);
      if (item.held) g.still = 0;
    }
    // 投喂的东西喝的时候倾过来 (行动层每帧只摆朝向 yaw, 这里在那之上再倾)
    for (const [id, q] of Object.entries(this.tilts)) {
      const it = this.layer.items[id];
      if (!it?.held) { delete this.tilts[id]; continue; }
      it.mesh.quaternion.setFromAxisAngle(new THREE.Vector3(0, 1, 0), this.layer.char.yaw).premultiply(q);
    }
    // 握着的东西放进手心 (行动层和接触层摆完以后), 手指按种类弯
    const hold = {};
    for (const item of Object.values(this.layer.items)) {
      if (!item.held || !item.grip) continue;
      hold[item.held] = item.physical || item.grasp ? item.grip : this._inHand(item, this.glb[item.id]);  // 交给刚体的不摆进手心, 只弯手指
    }
    this._fingers(hold);
  }

  /** 这只手的手心: 中心 C (手腕到中指根 0.55 处)、掌心朝向 n (标准化骨骼零位掌心朝 -Y)、手指方向 d, 另一只手在哪. */
  _palm(side) {
    const h = this.layer.vrm.humanoid, hand = h.getNormalizedBoneNode(`${side}Hand`), mid = h.getNormalizedBoneNode(`${side}MiddleProximal`);
    if (!hand || !mid) return null;
    const W = hand.getWorldPosition(new THREE.Vector3()), M = mid.getWorldPosition(new THREE.Vector3());
    const n = new THREE.Vector3(0, -1, 0).applyQuaternion(hand.getWorldQuaternion(new THREE.Quaternion())).normalize();
    const other = h.getNormalizedBoneNode(`${side === 'left' ? 'right' : 'left'}Hand`)?.getWorldPosition(new THREE.Vector3());
    return { C: W.clone().lerp(M, 0.55), n, d: M.sub(W).normalize(), other };
  }

  /**
   * 把握着的东西摆进手心 (g: 房间物件; 没有就是投喂的东西、小册子, 直接摆它自己). 返回这只手的握法 (cup / book / flat / soft).
   * 大小按静止时的包围盒 (item.size): 最薄的那边贴手心, 最长的那边顺着手指.
   */
  _inHand(item, g) {
    const f = this._palm(item.held), size = item.size;
    if (!f || !size || item.grip === 'soft') return item.grip;
    if (item.grip === 'cup') {
      // 竖着: 杯身 (半径按水平两边短的那边, 把手不算) 贴着手心, 手握在杯身中间偏上
      const r = Math.min(size.x, size.z) / 2, nh = f.n.clone().setY(0);
      if (nh.lengthSq() < 0.04) nh.copy(f.d).setY(0);
      if (nh.lengthSq() < 1e-6) return 'cup';
      nh.normalize();
      const c = f.C.clone().addScaledVector(nh, r + 0.006);
      c.y -= size.y * 0.08;
      if (g) g.node.position.copy(c).add(g.offset.clone().applyAxisAngle(UP, g.yawNow));
      else item.mesh.position.copy(c);
      return 'cup';
    }
    // 书: 书的"上面" (平放时朝上那面) 贴着手心; 长边顺着手指 (夹在身侧时书垂在手下面), 摊开的本子放在腿上时长边横过来
    const flat = f.n.y < -0.6;
    const d = f.d.clone().addScaledVector(f.n, -f.d.dot(f.n));
    if (d.lengthSq() < 1e-6) return 'book';
    d.normalize();
    const across = new THREE.Vector3().crossVectors(f.n, d).normalize();
    const longDir = flat && /Open/i.test(item.id) ? across : d;
    // 静止时最薄、最长的两边 (书平放: 薄的是 y; 零食袋立着: 薄的是 z)
    const axes = [new THREE.Vector3(1, 0, 0), UP.clone(), new THREE.Vector3(0, 0, 1)], dims = [size.x, size.y, size.z];
    const order = [0, 1, 2].sort((a, b) => dims[a] - dims[b]), restThin = axes[order[0]], restLong = axes[order[2]];
    const rest = new THREE.Matrix4().makeBasis(restLong, restThin, new THREE.Vector3().crossVectors(restLong, restThin));
    const faceUp = f.n.clone().negate();
    const want = new THREE.Matrix4().makeBasis(longDir, faceUp, new THREE.Vector3().crossVectors(longDir, faceUp));
    const q = new THREE.Quaternion().setFromRotationMatrix(want.multiply(rest.transpose()));
    const long = dims[order[2]], c = f.C.clone().addScaledVector(f.n, dims[order[0]] / 2 + 0.004);
    if (!flat) c.addScaledVector(longDir, Math.max(0, long / 2 - 0.07));
    else if (f.other) {  // 摊在腿上: 往另一只手那边挪, 两只手之间
      const v = f.other.clone().sub(f.C).addScaledVector(f.n, -f.other.clone().sub(f.C).dot(f.n)), len = v.length();
      if (len > 1e-3 && len < 0.45) c.addScaledVector(v.normalize(), Math.min(0.09, len / 2));
    }
    if (g) {
      g.node.quaternion.copy(q).multiply(g.q0);
      g.node.position.copy(c).add(g.offset.clone().applyQuaternion(q));
    } else {
      item.mesh.quaternion.copy(q);
      item.mesh.position.copy(c);
    }
    return flat ? 'flat' : 'book';
  }

  /**
   * 握着东西的手按种类弯手指. 有手部层 (web/hand.js, 每帧在接触层之后把手指整个重写一遍) 时把手形交给它 (hold[side], 不拿了设回 null);
   * 没有的老版本照旧在接触层的结果上按它的握紧程度改 (轴和接触层一样: 左手绕 Z 负转).
   */
  _fingers(hold) {
    const hands = this.avatar?.hands;
    if (hands?.hold) {
      for (const side of ['left', 'right']) hands.hold[side] = HOLD[hold[side]] || null;
      return;
    }
    const h = this.layer.vrm.humanoid, contact = this.avatar?.contact;
    const z = new THREE.Vector3(0, 0, 1), y = new THREE.Vector3(0, 1, 0), rad = Math.PI / 180, q = new THREE.Quaternion();
    for (const side of ['left', 'right']) {
      const shape = HOLD[hold[side]];
      if (!shape) continue;
      const w = contact?.grip?.[side] ?? 1, sgn = side === 'left' ? -1 : 1, [p, m, d] = shape.f[1], th = shape.t[0];
      FINGERS.forEach((f, i) => {
        const spread = 1 + i * 0.08;
        [['Proximal', p], ['Intermediate', m], ['Distal', d]].forEach(([seg, a]) => {
          h.getNormalizedBoneNode(`${side}${f}${seg}`)?.quaternion.slerp(q.setFromAxisAngle(z, sgn * a * spread * rad), w);
        });
      });
      for (const [seg, a] of [['Metacarpal', th], ['Proximal', th], ['Distal', th * 0.8]]) {
        const n = h.getNormalizedBoneNode(`${side}Thumb${seg}`);
        if (n) n.quaternion.slerp(new THREE.Quaternion().setFromAxisAngle(y, -sgn * a * 0.8 * rad).multiply(new THREE.Quaternion().setFromAxisAngle(z, sgn * a * 0.4 * rad)), w);
      }
    }
  }

  /** 翻开日记本时书页上方飘起的微光 (查知识库). at(): 本子在哪. */
  sparkles(at) {
    const N = 40, geo = new THREE.BufferGeometry(), pos = new Float32Array(N * 3), life = new Float32Array(N);
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    const mat = new THREE.PointsMaterial({ color: 0xfff1b8, size: 0.018, transparent: true, opacity: 0.85, depthWrite: false, blending: THREE.AdditiveBlending });
    const pts = new THREE.Points(geo, mat);
    pts.frustumCulled = false;
    this.live.add(pts);
    for (let i = 0; i < N; i++) life[i] = Math.random();
    const off = this.every((dt) => {
      const c = at();
      for (let i = 0; i < N; i++) {
        life[i] += dt * 0.6;
        if (life[i] >= 1) life[i] -= 1;
        const a = i * 2.399, rr = 0.03 + 0.05 * ((i * 7) % 10) / 10;
        pos[i * 3] = c.x + Math.cos(a + life[i]) * rr;
        pos[i * 3 + 1] = c.y + 0.02 + life[i] * 0.22;
        pos[i * 3 + 2] = c.z + Math.sin(a + life[i]) * rr;
      }
      geo.attributes.position.needsUpdate = true;
    });
    let done = false;
    return {
      dispose: () => { if (done) return; done = true; off(); this.live.remove(pts); geo.dispose(); mat.dispose(); },
    };
  }

  /** 拖进来的文件: 一本小册子从镜头那边掉到台面上 (at: [x, y, z]). 返回物件 id. */
  async booklet(at, color = '#b9d7f0') {
    const paper = new THREE.MeshStandardMaterial({ color: '#f6f1e4', roughness: 0.9 });
    const cover = new THREE.MeshStandardMaterial({ color, roughness: 0.7 });
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(0.15, 0.018, 0.21), [paper, paper, cover, paper, paper, paper]);
    const id = `doc_${++this.n}`;
    mesh.name = id;
    mesh.castShadow = true;
    this.layer.itemGroup.add(mesh);
    this.layer.items[id] = { id, mesh, held: null, half: 0.009, on: 'surf_desk', origin: null, grip: 'book', size: new THREE.Vector3(0.15, 0.018, 0.21) };
    mesh.rotation.y = (Math.random() - 0.5) * 0.6;
    const [x, y, z] = at;
    await this.tween(0.6, (t) => {
      const fall = Math.min(1, t * 1.3), bounce = t > 0.75 ? Math.sin(((t - 0.75) / 0.25) * Math.PI) * 0.02 : 0;
      mesh.position.set(x, y + 0.009 + (1 - fall * fall) * 0.8 + bounce, z);
    });
    return id;
  }

  /** 健身区脚下的网格 (校准重心的意思): 淡淡浮出来, dispose 时淡掉. */
  gridAt(char) {
    const grid = new THREE.GridHelper(1.6, 16, 0x9fd3ff, 0x9fd3ff);
    grid.material.transparent = true;
    grid.material.opacity = 0;
    grid.material.depthWrite = false;
    grid.position.set(char.x, 0.012, char.z);
    this.live.add(grid);
    this.tween(0.8, (t) => { grid.material.opacity = 0.45 * t; });
    return {
      dispose: () => this.tween(0.8, (t) => { grid.material.opacity = 0.45 * (1 - t); }).then(() => { this.live.remove(grid); grid.geometry.dispose(); grid.material.dispose(); }),
    };
  }

  /** 喝的时候把手里的东西倾向嘴边 (angle 弧度, 0 摆正). */
  tilt(id, angle) {
    const q = angle ? new THREE.Quaternion().setFromAxisAngle(
      new THREE.Vector3(Math.cos(this.layer.char.yaw), 0, -Math.sin(this.layer.char.yaw)), -angle) : null;
    if (this.glb[id]) this.glb[id].tilt = q;
    else if (q) this.tilts[id] = q;
    else delete this.tilts[id];
  }

  // -- 房间物件当成能拿的东西 ------------------------------------------------------------------
  /** 让房间里的物件 (节点名) 能被行动层拿放. 返回物件 id (就是节点名). */
  asItem(name) {
    if (this.glb[name]) return name;
    const node = this.node(name);
    if (!node) return null;
    node.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(node);
    const size = box.getSize(new THREE.Vector3()), center = box.getCenter(new THREE.Vector3());
    const proxy = new THREE.Mesh(new THREE.BoxGeometry(size.x, size.y, size.z), new THREE.MeshBasicMaterial());
    proxy.visible = false;
    proxy.name = name;
    proxy.position.copy(center);
    this.layer.itemGroup.add(proxy);
    this.layer.items[name] = { id: name, mesh: proxy, held: null, half: size.y / 2, on: box.min.y < 0.04 ? 'floor' : 'room', origin: null,
      grip: gripOf(name), size: size.clone() };
    const pos = node.getWorldPosition(new THREE.Vector3());
    const reviewed = this.surfaceGrasps && this.layer.scene?.glb === '/pet/rooms/petal-oak-life-v1/room.glb' && name === 'IR_Item_Library_01';
    const bounds = reviewed ? localBounds(node) : null, localSize = bounds?.getSize(new THREE.Vector3());
    const scale = reviewed ? node.getWorldScale(new THREE.Vector3()) : null;
    const grasp = scale?.toArray().every((s) => Math.abs(s - 1) < 1e-6)
      ? roomGrasp(this.layer.scene.glb, name, localSize.toArray()) : null;
    let graspNodePose = null;
    if (grasp) {
      proxy.geometry.dispose();
      proxy.geometry = new THREE.BoxGeometry(localSize.x, localSize.y, localSize.z);
      const localCenter = bounds.getCenter(new THREE.Vector3());
      setWorldPose(proxy, { position: node.localToWorld(localCenter.clone()).toArray(),
        quaternion: node.getWorldQuaternion(new THREE.Quaternion()).toArray() });
      Object.assign(this.layer.items[name], { grasp, graspHalfExtents: localSize.toArray().map((s) => s / 2),
        grip: 'book', size: localSize.clone() });
      graspNodePose = { position: localCenter.clone().negate().toArray(), quaternion: [0, 0, 0, 1] };
    }
    this.glb[name] = { id: name, node, proxy, offset: pos.sub(center), q0: node.getWorldQuaternion(new THREE.Quaternion()),
      proxyYaw0: 0, yawNow: 0, settled: true, graspNodePose, home: { pos: node.position.clone(), q: node.quaternion.clone(), parent: node.parent } };
    return name;
  }

  /** 开始拿之前: 物件挪进活动层 (她走过去、伸手的这段时间留在那, 握住以后跟着手). */
  wake(id) {
    const g = this.glb[id];
    if (!g) return;
    this.toLive(g.node);
    g.settled = false;
    g.pending = true;
    g.still = 0;
  }

  /** 没拿成: 放回房间. */
  settle(id) {
    const g = this.glb[id];
    if (!g || this.layer.items[id]?.held) return;
    g.pending = false;
    g.settled = true;
    this.toRoom(g.node);
  }

  /** 物件放回原来的地方 (冰箱补货、收拾掉的空瓶回到架子上), 显示或隐藏. */
  reset(id, visible = true) {
    const g = this.glb[id];
    if (!g) return;
    const item = this.layer.items[id];
    if (item.held) return;
    g.home.parent.add(g.node);
    g.node.position.copy(g.home.pos);
    g.node.quaternion.copy(g.home.q);
    g.node.visible = visible;
    g.node.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(g.node);
    box.getCenter(item.mesh.position);
    item.mesh.rotation.set(0, 0, 0);
    if (item.grasp && g.graspNodePose) {
      // Reset the original authored object pose, including its non-central mesh pivot.
      const q = g.node.getWorldQuaternion(new THREE.Quaternion());
      const c = g.node.getWorldPosition(new THREE.Vector3()).sub(new THREE.Vector3(...g.graspNodePose.position).applyQuaternion(q));
      setWorldPose(item.mesh, { position: c.toArray(), quaternion: q.toArray() });
      delete item.graspBinding; delete item.graspOrigin;
    }
    item.on = 'room';
    g.tilt = null;
    g.yawNow = 0;
    g.pending = false;
    g.settled = true;
    delete this.layout.items?.[id];
    delete this.layout.itemPoses?.[id];
    this.cache.dirty = true;
    this.save();
  }

  hide(id) {
    const g = this.glb[id];
    if (!g) return;
    g.node.visible = false;
    this.cache.dirty = true;
  }

  /** 记下小物件被她挪到了哪 (下次打开还在那). */
  _remember(g) {
    const item = this.layer.items[g.id];
    if (!item || !g.node.visible || g.noSave) return;
    (this.layout.items ??= {})[g.id] = [...item.mesh.position.toArray().map((x) => +x.toFixed(3)), +g.yawNow.toFixed(3)];
    if (item.grasp) (this.layout.itemPoses ??= {})[g.id] = { version: 1, ...worldPose(item.mesh) };
    this.save();
  }

  /** 打开时把上次挪过的小物件放回去. */
  restoreItems() {
    for (const [id, [x, y, z, yaw]] of Object.entries(this.layout.items || {})) {
      if (!this.asItem(id) || this.glb[id].noSave) continue;
      const item = this.layer.items[id];
      const authored = item.grasp ? worldPose(item.mesh) : null;
      item.mesh.position.set(x, y, z);
      item.mesh.rotation.set(0, yaw, 0);
      if (item.grasp) {
        const saved = this.layout.itemPoses?.[id];
        try {
          if (saved !== undefined) {
            if (saved?.version !== 1) throw new Error('unknown saved pose version');
            setWorldPose(item.mesh, saved);
          } else {
            // Legacy positions were proxy-parent-local; yaw was a world-Y delta over the authored orientation.
            const q = new THREE.Quaternion().setFromAxisAngle(UP, yaw).multiply(new THREE.Quaternion(...authored.quaternion));
            setWorldPose(item.mesh, { position: item.mesh.getWorldPosition(new THREE.Vector3()).toArray(), quaternion: q.toArray() });
          }
        } catch { setWorldPose(item.mesh, authored); }  // Do not apply a malformed pose partially.
        delete item.graspBinding; delete item.graspOrigin;
      }
      item.on = 'moved';
      const g = this.glb[id];
      g.proxyYaw0 = 0;
      this.toLive(g.node);
      g.settled = false;
      g.still = 0.5;
      this.follow();
    }
  }

  save() {
    try { this.storage?.setItem(KEY, JSON.stringify(this.layout)); } catch { /* 存不了就算了 */ }
  }

  // -- 冰箱、咖啡机 ------------------------------------------------------------------------------
  async fridge(open) {
    const hinge = this.node('Fridge_Door_Hinge');
    if (!hinge) return;
    this.fridgeOpen = open;
    this.toLive(hinge);
    const from = hinge.rotation.y, to = open ? -1.5 : 0;
    await this.tween(open ? 0.7 : 0.55, (t) => { hinge.rotation.y = from + (to - from) * t; });
    if (!open) this.toRoom(hinge);
  }

  /** 咖啡机出水: 离杯子最近的那一股 (on=null 关掉全部). */
  stream(cupPos) {
    const streams = ['GK_Coffee_Stream_00', 'GK_Coffee_Stream_01'].map((n) => this.node(n)).filter(Boolean);
    let best = null, bestD = Infinity;
    for (const s of streams) {
      const c = new THREE.Box3().setFromObject(s).getCenter(new THREE.Vector3());
      const d = cupPos ? Math.hypot(c.x - cupPos.x, c.z - cupPos.z) : Infinity;
      if (d < bestD) { bestD = d; best = s; }
    }
    for (const s of streams) {
      const on = s === best;
      this.toLive(s);
      s.visible = on;
      if (!on) this.toRoom(s);
    }
    this.flow = best;
  }

  /** 加载后: 出水平时看不见. */
  quietMachine() {
    for (const n of ['GK_Coffee_Stream_00', 'GK_Coffee_Stream_01']) { const s = this.node(n); if (s) s.visible = false; }
    this.cache.dirty = true;
  }

  // -- 投喂的东西 --------------------------------------------------------------------------------
  /** 放一件投喂的东西 (底面在 at: [x, y, z]); on: 'floor' 或台面的名字. 返回物件 id. */
  spawn(kind, [x, y, z], on = 'floor') {
    const mesh = fedMesh(kind);
    const id = `fed_${kind}_${++this.n}`;
    mesh.name = id;
    const size = new THREE.Box3().setFromObject(mesh).getSize(new THREE.Vector3()), half = size.y / 2;
    mesh.position.set(x, y + half, z);
    this.layer.itemGroup.add(mesh);
    this.layer.items[id] = { id, mesh, held: null, half, on, origin: null, fed: kind, grip: gripOf(id, kind), size };
    return id;
  }

  /** 投喂: 东西从镜头这边掉到房间前面的地上 (at: [x, z]), 弹一下. 返回物件 id. */
  async drop(kind, [x, z]) {
    const id = this.spawn(kind, [x, 0, z]);
    const item = this.layer.items[id], mesh = item.mesh, half = item.half;
    this.fed.push({ id, kind });
    mesh.rotation.y = Math.random() * Math.PI * 2;
    await this.tween(0.55, (t) => {
      const fall = Math.min(1, t * 1.4);
      const bounce = t > 0.7 ? Math.sin(((t - 0.7) / 0.3) * Math.PI) * 0.05 : 0;
      mesh.position.set(x, half + (1 - fall * fall) * 1.1 + bounce, z);
    });
    return id;
  }

  /** 吃完、喝完、收好了: 投喂的东西消失. */
  remove(id) {
    const item = this.layer.items[id];
    if (!item) return;
    item.mesh.parent?.remove(item.mesh);
    delete this.layer.items[id];
    this.fed = this.fed.filter((f) => f.id !== id);
  }

  // -- 被子 ------------------------------------------------------------------------------------
  /**
   * 床上的被子 (bed: { top 被子面高度, x: [左, 右], zFoot 床尾那头, H 髋高 }): 平时叠在床尾 (cover = 0, 盖住床尾 FOLD 那么长),
   * 她躺进去以后用手把边拉上来 (cover 到 1, 上边到胸口下面). drape(骨骼, cover) 按她此刻的身体 (骨骼连成的胶囊) 重算一张高度图:
   * 盖住的地方比身体高一点, 再软化; 叠着的那段厚一些. 床面以上 0.3 m 以外的骨骼不算 (坐在床边、站着时被子不会跟着鼓起来).
   * 软化只往上抹平, 不会把被子压进身体里: 每一点都不低于那里的身体表面 + 1.8 cm (以前小腿、胳膊、脚尖这种细的地方会从被子里戳出来).
   * 返回 { mesh, drape(bones, cover), edgeZ(cover, bones) 上边在哪, surface(x, z) 那一点被子面多高, dispose() }.
   */
  blanket(bed) {
    const soft = new URLSearchParams(location.search).get('quilt') === 'soft';  // 对比用 (quilt=soft): 以前的做法, 软化会把被子压进身体
    const s = bed.H / 0.78, NX = soft ? 30 : 40, NZ = soft ? 40 : 56;
    const [x0, x1] = bed.x, zFoot = bed.zFoot;
    const geo = new THREE.BufferGeometry();
    const pos = new Float32Array((NX + 1) * (NZ + 1) * 3), uv = [], idx = [];
    for (let j = 0; j <= NZ; j++) for (let i = 0; i <= NX; i++) uv.push(i / NX, j / NZ);
    for (let j = 0; j < NZ; j++) for (let i = 0; i < NX; i++) {
      const a = j * (NX + 1) + i, b = a + 1, c = a + NX + 1, d = c + 1;
      idx.push(a, c, b, b, c, d);
    }
    geo.setAttribute('position', new THREE.BufferAttribute(pos, 3));
    geo.setAttribute('uv', new THREE.Float32BufferAttribute(uv, 2));
    geo.setIndex(idx);
    const base = this.mats['Game | Bed | pale blush cotton duvet'];
    const mat = base ? base.clone() : new THREE.MeshStandardMaterial({ color: '#d79a92', roughness: 0.9 });
    mat.side = THREE.DoubleSide;
    const mesh = new THREE.Mesh(geo, mat);
    mesh.name = 'pet-blanket';
    mesh.frustumCulled = false;
    this.live.add(mesh);
    const hgt = new Float32Array((NX + 1) * (NZ + 1)), skin = new Float32Array((NX + 1) * (NZ + 1));
    const tmp = new THREE.Vector3(), seg = new THREE.Line3();
    let zTop = zFoot - FOLD;
    // 身体的胶囊 (中心线两端 + 半径), 只要贴着床面的那些
    const capsules = (bones) => {
      const out = [];
      if (!bones) return out;
      const add = (a, b, r) => { if (a && b && Math.min(a.y, b.y) < bed.top + 0.3) out.push([a, b, r * s]); };
      add(bones.hips, bones.chest || bones.spine, 0.14);
      add(bones.chest || bones.spine, bones.neck, 0.12);
      for (const side of ['left', 'right']) {
        add(bones.hips, bones[`${side}UpperLeg`], 0.13);  // 胯、大腿比腿本身粗一点: 裙子
        add(bones[`${side}UpperLeg`], bones[`${side}LowerLeg`], 0.095);
        add(bones[`${side}LowerLeg`], bones[`${side}Foot`], 0.062);
        add(bones[`${side}Foot`], bones[`${side}Toes`] || bones[`${side}Foot`], 0.07);
        add(bones[`${side}UpperArm`], bones[`${side}LowerArm`], 0.05);
        add(bones[`${side}LowerArm`], bones[`${side}Hand`], 0.045);
        const hand = bones[`${side}Hand`], fore = bones[`${side}LowerArm`];
        if (hand && fore) add(hand, hand.clone().add(hand.clone().sub(fore).setLength(0.09 * s)), 0.04);  // 手掌
        // 手指到指尖 (指尖骨头再往外 2 cm): 只按手掌的话指尖露在被子外面 (自检量出来戳出被子的几乎全是手指)
        for (const [f, r] of [['Middle', 0.028], ['Index', 0.022], ['Little', 0.022], ['Thumb', 0.024]]) {
          const tip = bones[`${side}${f}Distal`];
          if (hand && tip) add(hand, tip.clone().add(tip.clone().sub(hand).setLength(0.02 * s)), r);
        }
      }
      return out;
    };
    const api = {
      mesh,
      /** 盖到哪 (0 叠在床尾, 1 拉到胸口下面) 时上边的 z. 胸口跟着她 (躺着时), 没躺下时按 bed.zChest. */
      edgeZ(cover, bones) {
        const chest = bones?.chest || bones?.spine;
        const zChest = chest && chest.y < bed.top + 0.35 ? chest.z + 0.08 : bed.zChest;
        return zFoot - FOLD + (zChest - (zFoot - FOLD)) * Math.max(0, Math.min(1, cover));
      },
      drape(bones, cover) {
        api.cover = cover;
        zTop = api.edgeZ(cover, bones);
        const caps = capsules(bones), fold = (1 - Math.max(0, Math.min(1, cover))) * 0.05;  // 叠着的那段厚一些
        for (let j = 0; j <= NZ; j++) {
          const z = zTop + ((zFoot - zTop) * j) / NZ;
          for (let i = 0; i <= NX; i++) {
            const x = x0 + ((x1 - x0) * i) / NX;
            let h = bed.top + BASE + fold, sk = 0;
            for (const [a, b, r] of caps) {
              seg.set(tmp.set(a.x, 0, a.z).clone(), new THREE.Vector3(b.x, 0, b.z));
              const t = seg.closestPointToPointParameter(tmp.set(x, 0, z), true);
              const cx = a.x + (b.x - a.x) * t, cz = a.z + (b.z - a.z) * t, cy = a.y + (b.y - a.y) * t;
              const d = Math.hypot(x - cx, z - cz), R = r + 0.05;
              if (d < R) h = Math.max(h, cy + Math.sqrt(R * R - d * d) * (r / R) + 0.012);
              if (d < r + 0.01) sk = Math.max(sk, cy + Math.sqrt(Math.max(0, (r + 0.01) ** 2 - d * d)) + 0.018);  // 身体表面 (放宽 1 cm) 再高 1.8 cm
            }
            hgt[j * (NX + 1) + i] = h;
            skin[j * (NX + 1) + i] = sk;
          }
        }
        for (let pass = 0; pass < 2; pass++) {  // 软化: 被子不贴着身体的每个起伏
          const src = hgt.slice();
          for (let j = 1; j < NZ; j++) for (let i = 1; i < NX; i++) {
            let sum = 0;
            for (let dj = -1; dj <= 1; dj++) for (let di = -1; di <= 1; di++) sum += src[(j + dj) * (NX + 1) + i + di];
            hgt[j * (NX + 1) + i] = Math.max(src[j * (NX + 1) + i] * 0.4 + (sum / 9) * 0.6, bed.top + BASE, soft ? 0 : skin[j * (NX + 1) + i]);
          }
        }
        for (let j = 0, k = 0; j <= NZ; j++) {
          const z = zTop + ((zFoot - zTop) * j) / NZ;
          for (let i = 0; i <= NX; i++, k++) {
            const edge = i === 0 || i === NX ? 0.06 : j === NZ ? 0.05 : 0;  // 两边和床尾垂下去一点
            pos[k * 3] = x0 + ((x1 - x0) * i) / NX;
            pos[k * 3 + 1] = hgt[k] - edge;
            pos[k * 3 + 2] = z;
          }
        }
        geo.attributes.position.needsUpdate = true;
        geo.computeVertexNormals();
        geo.computeBoundingSphere();
      },
      /** 被子面在 (x, z) 多高 (最近一次 drape 的结果; 上边以外返回床面). */
      surface(x, z) {
        if (z < zTop || z > zFoot) return bed.top + BASE;
        const i = Math.round(((x - x0) / (x1 - x0)) * NX), j = Math.round(((z - zTop) / (zFoot - zTop)) * NZ);
        return hgt[Math.max(0, Math.min(NZ, j)) * (NX + 1) + Math.max(0, Math.min(NX, i))];
      },
      /**
       * 自检用: 角色网格 (蒙皮以后的顶点, 每 3 个取 1 个) 有多少戳出被子 —— 在被子盖着的范围里、比被子面高、又没高出 25 cm (举起来的手不算).
       * 按从上边到床尾分 4 段数 (哪一段戳出来: 胸口 / 肚子大腿 / 膝盖小腿 / 脚).
       */
      poke(root, humanoid) {
        const v = new THREE.Vector3(), bins = [0, 0, 0, 0], near = {}, mats = {};
        const BONES = ['chest', 'spine', 'hips', 'leftUpperArm', 'leftLowerArm', 'leftHand', 'rightUpperArm', 'rightLowerArm', 'rightHand',
          'leftUpperLeg', 'rightUpperLeg', 'leftLowerLeg', 'rightLowerLeg', 'leftFoot', 'rightFoot', 'neck'];
        const at = BONES.map((b) => [b, humanoid?.getNormalizedBoneNode(b)?.getWorldPosition(new THREE.Vector3())]).filter(([, p]) => p);
        let n = 0, seen = 0;
        root.traverse((o) => {
          if (!o.isSkinnedMesh || !o.visible) return;
          const count = o.geometry.attributes.position.count;
          for (let i = 0; i < count; i += 3) {
            o.getVertexPosition(i, v).applyMatrix4(o.matrixWorld);
            if (v.x < x0 + 0.04 || v.x > x1 - 0.04 || v.z < zTop + 0.04 || v.z > zFoot - 0.02 || v.y < bed.top - 0.05) continue;
            seen++;
            const top = api.surface(v.x, v.z);
            if (v.y > top + 0.002 && v.y < top + 0.25) {
              n++;
              bins[Math.min(3, Math.floor(((v.z - zTop) / (zFoot - zTop)) * 4))]++;
              let best = null, bd = Infinity;
              for (const [b, p] of at) { const d = p.distanceToSquared(v); if (d < bd) { bd = d; best = b; } }
              if (best) near[best] = (near[best] || 0) + 1;
              const m = Array.isArray(o.material) ? o.material[0] : o.material;
              mats[m?.name || o.name] = (mats[m?.name || o.name] || 0) + 1;
            }
          }
        });
        const top3 = (x) => Object.entries(x).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([k, c]) => `${k.replace(/\s+/g, '_')}:${c}`).join(';');
        return { n, seen, bins, near: top3(near), mats: top3(mats) };
      },
      /** 自检用: 角色网格有多少顶点陷进床垫 3 cm 以上 (床的左右之间、床尾往里 2.1 m 以内), 按离哪根骨头最近分. 上床、躺下时看. */
      sink(root, humanoid) {
        const v = new THREE.Vector3(), near = {};
        const at = ['hips', 'leftUpperLeg', 'rightUpperLeg', 'leftLowerLeg', 'rightLowerLeg', 'leftFoot', 'rightFoot', 'leftHand', 'rightHand', 'chest', 'head']
          .map((b) => [b, humanoid?.getNormalizedBoneNode(b)?.getWorldPosition(new THREE.Vector3())]).filter(([, p]) => p);
        let n = 0, deep = 0;
        root.traverse((o) => {
          if (!o.isSkinnedMesh || !o.visible) return;
          for (let i = 0, count = o.geometry.attributes.position.count; i < count; i += 3) {
            o.getVertexPosition(i, v).applyMatrix4(o.matrixWorld);
            if (v.x < x0 || v.x > x1 || v.z > zFoot || v.z < zFoot - 2.1 || v.y > bed.top - 0.03 || v.y < bed.top - 0.4) continue;
            n++;
            deep = Math.max(deep, bed.top - v.y);
            let best = null, bd = Infinity;
            for (const [b, p] of at) { const d = p.distanceToSquared(v); if (d < bd) { bd = d; best = b; } }
            near[best] = (near[best] || 0) + 1;
          }
        });
        return { n, cm: +(deep * 100).toFixed(1), near: Object.entries(near).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([k, c]) => `${k}:${c}`).join(';') };
      },
      dispose: () => { this.live.remove(mesh); geo.dispose(); mat.dispose(); },
    };
    api.drape(null, 0);
    return api;
  }
}
