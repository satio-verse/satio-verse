// 自动调整姿态: 按关节活动范围 (ROM, 见 rom.js) 把越界的关节转回范围内, 用最小的转动, 尽量保留原动作.
//   四肢 (运动来源和 IK 之后、防穿模层之前): 肩的后伸/内收, 肘和膝的屈曲/过伸, 前臂旋前/旋后, 腕的屈伸/尺桡偏,
//     髋的屈伸/外展/内收, 踝的背屈/跖屈. 腿多留 5 度余量: 为一点点越界去改腿, 会把锁定的支撑脚拉离地面.
//   颈 (头部动作叠完之后、姿态平滑之前): 前屈/后伸/转头/侧屈超出范围时, 按比例把转动收回.
// 肩和髋的内外旋暂不限制 (扭转的零位要另外定义); 腰椎只在约束面板里统计, 不修正 (见 axial).
// 肩和髋用解剖学坐标系 (由左右关节和躯干标志点定), 不用骨骼节点自己的朝向.
import * as THREE from 'three';
import { ROM, eulerYXZ, hingeCorrection, keepRatio, limitDirection, twistAngle } from './rom.js';

const LEG_SLACK = 5;  // 度
const X = new THREE.Vector3(1, 0, 0), Y = new THREE.Vector3(0, 1, 0), Z = new THREE.Vector3(0, 0, 1);

export class PostureRegulator {
  constructor(vrm) {
    this.vrm = vrm;
    this.h = vrm.humanoid;
    this.enabled = true;
    this.stats = { frames: 0, fixes: 0, byJoint: {} };
    // 脚尖方向 (静止姿态下, 小腿局部坐标里从脚踝指向脚尖): 踝的背屈/跖屈相对它来算
    const foot = (side) => {
      const f = this.raw(`${side}Foot`), t = this.raw(`${side}Toes`);
      if (!f || !t) return new THREE.Vector3(0, -0.3, 1).normalize();
      vrm.scene.updateMatrixWorld(true);
      const d = t.getWorldPosition(new THREE.Vector3()).sub(f.getWorldPosition(new THREE.Vector3()));
      return d.applyQuaternion(vrm.scene.getWorldQuaternion(new THREE.Quaternion()).invert()).normalize();
    };
    this.toeRest = { left: foot('left'), right: foot('right') };
  }

  node(n) { return this.h.getNormalizedBoneNode(n); }
  raw(n) { return this.h.getRawBoneNode(n); }
  _w(n) { return this.node(n)?.getWorldPosition(new THREE.Vector3()); }
  _hit(joint) { this.stats.fixes++; this.stats.byJoint[joint] = (this.stats.byJoint[joint] || 0) + 1; }

  /** 世界坐标里绕 axis 转动 node (deg 度). */
  _turn(node, axis, deg) {
    const q = new THREE.Quaternion().setFromAxisAngle(axis.clone().normalize(), THREE.MathUtils.degToRad(deg));
    const parent = node.parent.getWorldQuaternion(new THREE.Quaternion());
    const world = node.getWorldQuaternion(new THREE.Quaternion());
    node.quaternion.copy(parent.invert().multiply(q.multiply(world)));
    node.updateWorldMatrix(false, true);
  }

  /** 让 node 的 cur 方向转到 want 方向 (都是世界坐标). */
  _aim(node, cur, want) {
    const q = new THREE.Quaternion().setFromUnitVectors(cur.clone().normalize(), want.clone().normalize());
    const parent = node.parent.getWorldQuaternion(new THREE.Quaternion());
    const world = node.getWorldQuaternion(new THREE.Quaternion());
    node.quaternion.copy(parent.invert().multiply(q.multiply(world)));
    node.updateWorldMatrix(false, true);
  }

  /** 解剖学坐标系 (像临床测量一样由标志点定, 不用骨骼节点自己的朝向): 骨盆 = 左右髋关节连线 + 髋中点指向胸部;
   *  胸廓 = 左右肩连线 + 胸部指向颈部. MMD 模型的胯骨映射到"腰"骨, 动画重定向后它本身是前倾的 (上半身由脊柱反向补回),
   *  用它的朝向量髋角度会把正常站姿量成后伸 30 度以上. 返回前 f、上 u、外侧 s (左边朝左, 右边朝右). */
  _frame(kind, side) {
    const [l1, l2, b1, b2] = kind === 'pelvis' ? ['leftUpperLeg', 'rightUpperLeg', null, 'chest'] : ['leftUpperArm', 'rightUpperArm', 'chest', 'neck'];
    const L = this._w(l1), R = this._w(l2);
    const base = b1 ? this._w(b1) : L.clone().add(R).multiplyScalar(0.5);
    const top = this._w(b2) || this._w('spine');
    const l = L.clone().sub(R).normalize();
    const u = top.clone().sub(base);
    u.addScaledVector(l, -u.dot(l)).normalize();
    const f = l.clone().cross(u).normalize();
    return { f: f.toArray(), u: u.toArray(), s: (side === 'left' ? l : l.negate()).toArray() };
  }

  /** 四肢那一遍. */
  limbs() {
    if (!this.enabled) return;
    this.vrm.scene.updateMatrixWorld(true);
    this.stats.frames++;
    for (const side of ['left', 'right']) {
      const out = side === 'left' ? X : X.clone().negate();
      // 肩: 上臂方向在胸廓坐标里, 后伸不超过 60 度, 越过中线不超过 45 度
      const sh = this.node(`${side}UpperArm`), el = this.node(`${side}LowerArm`), wr = this.node(`${side}Hand`);
      if (sh && el && wr) {
        const d = this._w(`${side}LowerArm`).sub(this._w(`${side}UpperArm`));
        const r = limitDirection(d.toArray(), this._frame('thorax', side), { back: ROM.shoulder.ext, cross: ROM.shoulder.add });
        if (r.hit.length) { this._aim(sh, d, new THREE.Vector3(...r.d)); this._hit(`${side}Shoulder`); }
        this._hinge(sh, el, `${side}UpperArm`, `${side}LowerArm`, `${side}Hand`, new THREE.Vector3(0, side === 'left' ? -1 : 1, 0), ROM.elbow, `${side}Elbow`, 0);
        // 前臂: 绕前臂轴的扭转, 朝外的轴正转是旋前
        const tw = twistAngle(el.quaternion.toArray(), out.toArray());
        const lim = Math.max(-ROM.forearm.sup, Math.min(ROM.forearm.pron, tw));
        if (lim !== tw) { el.quaternion.multiply(new THREE.Quaternion().setFromAxisAngle(out, THREE.MathUtils.degToRad(lim - tw))); el.updateWorldMatrix(false, true); this._hit(`${side}Forearm`); }
        this._wrist(wr, out, side);
      }
      // 髋: 大腿方向在骨盆坐标里; 膝; 踝
      const hip = this.node(`${side}UpperLeg`), knee = this.node(`${side}LowerLeg`), ankle = this.node(`${side}Foot`);
      if (hip && knee && ankle) {
        const d = this._w(`${side}LowerLeg`).sub(this._w(`${side}UpperLeg`));
        const s = LEG_SLACK;
        const r = limitDirection(d.toArray(), this._frame('pelvis', side),
          { back: ROM.hip.ext + s, cross: ROM.hip.add + s, fwdMax: ROM.hip.flex + s, sideMax: ROM.hip.abd + s });
        if (r.hit.length) { this._aim(hip, d, new THREE.Vector3(...r.d)); this._hit(`${side}Hip`); }
        this._hinge(hip, knee, `${side}UpperLeg`, `${side}LowerLeg`, `${side}Foot`, X, ROM.knee, `${side}Knee`, s);
        this._ankle(knee, ankle, side, s);
      }
    }
  }

  /** 铰链关节 (肘、膝): 屈曲超过上限或过伸超过允许值时, 绕铰链轴把下一段转回来. */
  _hinge(upper, lower, a, b, c, localAxis, rom, joint, slack) {
    const A = this._w(a), B = this._w(b), C = this._w(c);
    const axis = localAxis.clone().applyQuaternion(upper.getWorldQuaternion(new THREE.Quaternion()));
    const u = B.clone().sub(A), l = C.clone().sub(B);
    const { fix } = hingeCorrection(u.toArray(), l.toArray(), axis.toArray(), { flex: rom.flex + slack, hyper: rom.hyper + slack });
    if (Math.abs(fix) < 0.05) return;
    // 屈曲过头: 绕实际的弯曲平面法线转回来 (上一段扭转时铰链轴不准, 见 rom.js 的 TWISTED); 过伸: 绕铰链轴
    const n = u.clone().cross(l);
    this._turn(lower, fix < 0 && n.lengthSq() > 1e-10 ? n.normalize() : axis, fix);
    this._hit(joint);
  }

  /** 腕: 手的方向 (前臂局部坐标) 朝掌心是掌屈、朝手背是背伸, 朝拇指 (+Z) 是桡偏、反向是尺偏. */
  _wrist(hand, out, side) {
    const d = out.clone().applyQuaternion(hand.quaternion);  // 手相对前臂的方向
    const ax = d.dot(out), flex = THREE.MathUtils.radToDeg(Math.atan2(-d.y, ax)), dev = THREE.MathUtils.radToDeg(Math.atan2(d.z, ax));
    const f2 = Math.max(-ROM.wrist.ext, Math.min(ROM.wrist.flex, flex)), d2 = Math.max(-ROM.wrist.ulnar, Math.min(ROM.wrist.radial, dev));
    if (f2 === flex && d2 === dev) return;
    const want = out.clone().add(new THREE.Vector3(0, -Math.tan(THREE.MathUtils.degToRad(f2)), Math.tan(THREE.MathUtils.degToRad(d2)))).normalize();
    hand.quaternion.premultiply(new THREE.Quaternion().setFromUnitVectors(d.normalize(), want));
    hand.updateWorldMatrix(false, true);
    this._hit(`${side}Wrist`);
  }

  /** 踝: 脚尖方向 (小腿局部坐标) 相对静止时绕横轴转了多少: 脚尖往上是背屈, 往下是跖屈. */
  _ankle(knee, foot, side, slack) {
    const rest = this.toeRest[side];
    const d = rest.clone().applyQuaternion(foot.quaternion);
    // 绕 +X 转动: 正 = 脚尖往下 (跖屈)
    const pitch = THREE.MathUtils.radToDeg(Math.atan2(rest.clone().cross(d).dot(X), rest.dot(d)));
    const lim = Math.max(-(ROM.ankle.dorsi + slack), Math.min(ROM.ankle.plantar + slack, pitch));
    if (lim === pitch) return;
    foot.quaternion.premultiply(new THREE.Quaternion().setFromAxisAngle(X, THREE.MathUtils.degToRad(lim - pitch)));
    foot.updateWorldMatrix(false, true);
    this._hit(`${side}Ankle`);
  }

  /** 颈和腰那一遍: 颈 (neck x head) 和腰 (spine x chest x upperChest) 的合成转动, 超出范围时按比例收回. */
  axial() {
    if (!this.enabled) return;
    const group = (names, limits, joint) => {
      const nodes = names.map((n) => this.node(n)).filter(Boolean);
      if (!nodes.length) return;
      const q = nodes.reduce((acc, n) => acc.multiply(n.quaternion), new THREE.Quaternion());
      const e = eulerYXZ(q.toArray());
      const k = keepRatio({ pitch: e.pitch, yaw: e.yaw, roll: e.roll }, limits);
      if (k >= 0.999) return;
      for (const n of nodes) n.quaternion.slerp(new THREE.Quaternion(), 1 - k);  // 标准化骨骼的零位是单位旋转
      nodes[0].updateWorldMatrix(false, true);
      this._hit(joint);
    };
    // 点头往下 (绕 +X 正转) 是前屈. 腰椎不修正: 这些模型看不到骨盆真实的倾斜 (MMD 的"腰"骨重定向后本身前倾,
    // 脊柱反向补回), 相对胯骨量出来的"后伸"其实是补偿, 按 ROM 收回反而会让角色弯腰
    group(['neck', 'head'], { pitch: [-ROM.neck.ext, ROM.neck.flex], yaw: ROM.neck.rot, roll: ROM.neck.lat }, 'neck');
  }
}
