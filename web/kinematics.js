// 运动学约束的监测和可视化: 每帧 (所有运动层和姿态平滑之后) 算
//   支撑稳定性: 质心在地面的投影到支撑多边形 (着地脚的凸包) 边界的距离 (稳定裕度, 里面为正);
//   关节可行域: 膝、肘的屈曲 (含反向弯曲)、髋的屈伸/外展、颈部偏转, 离极限的余量;
//   姿态一致性: 脚穿进地面、锁定的支撑脚打滑、防穿模层把手臂推出身体的次数、姿态平滑层抑制的跳变;
//   痛觉 (见 pain.js): 上面这些再加上穿进家具/墙、物品穿模、拿放时隔空取物, 按物理单位逐帧累计.
// 打开"约束"时在场景里画出支撑多边形、质心投影和按余量着色的关节标记.
// 每个动作 (行动层 run 一次) 从开始到结束汇总一份执行评估, 发给服务端写进 logs/motion-eval-日期.jsonl.
import * as THREE from 'three';
import { restorePlain, snapshotPlain } from './plain-state.js';
import { centerOfMass, convexHull, footPoints, HINGE, jointChecks, legPenetration, signedMargin, soleContact } from './kinematics-core.js';
import { accExcess, boxGroups, floorRect, furnitureBoxes, JERK_LIMIT, jointExcess, PAIN_TERMS, painScore, partsDepth, quatAngle, roomDepth } from './pain.js';

const BONES = ['hips', 'spine', 'chest', 'neck', 'head', 'leftUpperArm', 'leftLowerArm', 'leftHand', 'rightUpperArm',
  'rightLowerArm', 'rightHand', 'leftUpperLeg', 'leftLowerLeg', 'leftFoot', 'leftToes', 'rightUpperLeg', 'rightLowerLeg',
  'rightFoot', 'rightToes'];
const GREEN = new THREE.Color(0x3fb37f), AMBER = new THREE.Color(0xe0a84a), RED = new THREE.Color(0xe0524a);
const colorOf = (m) => (m < 0 ? RED : m < 0.25 ? AMBER : GREEN);
// 量会不会插进家具、墙的身体点和半径 (米). 骨盆不算: 坐下时本来就在沙发的包围盒里
const BODY_PROBES = [['leftHand', 0.03], ['rightHand', 0.03], ['leftLowerArm', 0.03], ['rightLowerArm', 0.03],
  ['leftLowerLeg', 0.045], ['rightLowerLeg', 0.045], ['leftFoot', 0.04], ['rightFoot', 0.04], ['leftToes', 0.025], ['rightToes', 0.025]];
const zeroPain = () => Object.fromEntries(Object.keys(PAIN_TERMS).map((k) => [k, 0]));
// 量冲击 (jerk) 的骨骼
const JERK_BONES = ['hips', 'chest', 'head', 'leftUpperArm', 'leftLowerArm', 'leftHand', 'rightUpperArm', 'rightLowerArm', 'rightHand',
  'leftUpperLeg', 'leftLowerLeg', 'leftFoot', 'rightUpperLeg', 'rightLowerLeg', 'rightFoot'];

export class KinematicsMonitor {
  constructor(avatar) {
    this.avatar = avatar;
    this.group = new THREE.Group();
    this.group.visible = false;
    this.group.renderOrder = 999;
    avatar.scene.add(this.group);
    const lineMat = new THREE.LineBasicMaterial({ color: GREEN, depthTest: false, transparent: true, opacity: 0.9 });
    this.poly = new THREE.LineLoop(new THREE.BufferGeometry(), lineMat);
    this.com = new THREE.Mesh(new THREE.RingGeometry(0.018, 0.03, 24).rotateX(-Math.PI / 2),
      new THREE.MeshBasicMaterial({ color: GREEN, depthTest: false, side: THREE.DoubleSide }));
    this.comLine = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: 0x8a92a6, depthTest: false }));
    this.marks = {};
    this.group.add(this.poly, this.com, this.comLine);
    this.now = {};
    this.prevAnkle = {};
    this.counters = { frames: 0, slideMm: 0, floorPen: 0, floating: 0, violations: 0, unstable: 0 };
    this.pain = zeroPain();     // 各项痛觉的累计 (单位见 pain.js), 和 counters 一样只增不减, 执行评估按起止相减
    this.painNow = { score: 0 };
    this.painPeak = 0;          // 痛觉值的峰值 (按 1 秒半衰期回落), 面板显示
    this.balanceBy = {};        // 重心出支撑面的累计 (米), 按"在做什么|几只脚着地"分
    this.episode = null;
    this._factory = snapshotPlain(this, ['_factory']);  // 出厂状态 (执行评估复位时放回, 见 plain-state.js)
  }

  /** 放回出厂状态 (执行评估复位用): 计数、累计痛觉、上一帧的骨骼朝向 / 脚踝 / 掌心、懒算的家具碰撞体都清掉. */
  restoreFactory() {
    if (this._factory) restorePlain(this, this._factory, ['_factory']);
  }

  setVisible(v) { this.group.visible = v; }

  _mark(joint) {
    if (!this.marks[joint]) {
      const m = new THREE.Mesh(new THREE.SphereGeometry(0.02, 12, 8), new THREE.MeshBasicMaterial({ color: GREEN, depthTest: false }));
      m.renderOrder = 1000;
      this.marks[joint] = m;
      this.group.add(m);
    }
    return this.marks[joint];
  }

  /** 每帧调用 (平滑层之后、渲染之前). */
  update(dt) {
    const a = this.avatar, vrm = a.vrm, loco = a.loco;
    if (!vrm || !(dt > 0)) return;
    vrm.scene.updateMatrixWorld(true);
    const h = vrm.humanoid, pos = {}, v = new THREE.Vector3(), q = new THREE.Quaternion();
    for (const n of BONES) {
      const node = h.getNormalizedBoneNode(n);
      if (node) pos[n] = node.getWorldPosition(v).toArray();
    }
    if (!pos.hips) return;
    // 骨盆坐标 (由左右髋关节和胸部定, 和 posture.js 一致) 和铰链轴 (上一节骨骼的局部轴, 见 HINGE)
    const L = new THREE.Vector3(...pos.leftUpperLeg), R = new THREE.Vector3(...pos.rightUpperLeg);
    const lAxis = L.clone().sub(R).normalize();
    const upV = new THREE.Vector3(...(pos.chest || pos.spine || pos.head)).sub(L.clone().add(R).multiplyScalar(0.5));
    upV.addScaledVector(lAxis, -upV.dot(lAxis)).normalize();
    const body = { fwd: lAxis.clone().cross(upV).normalize().toArray(), up: upV.toArray(), right: lAxis.clone().negate().toArray() };
    const axes = {};
    for (const [joint, [parent, local]] of Object.entries(HINGE)) {
      const p = h.getNormalizedBoneNode(parent);
      if (p) axes[joint] = new THREE.Vector3(...local).applyQuaternion(p.getWorldQuaternion(q)).toArray();
    }
    const joints = jointChecks(pos, axes, body);
    // 支撑: 行动层锁定的脚 (锁定权重 > 0.5), 没有行动层时按脚踝离地高度判断
    const seated = !!loco?.seated || (loco?.seatAmount ?? 0) > 0.05;  // 坐下/站起的过程中臀部已经落在座面上
    const pts = [], c = this.counters, pain = zeroPain();
    let feetDown = 0;
    // 鞋底: 行动层取样的鞋底顶点按蒙皮算出的最低点 (没有行动层时用脚踝和脚尖骨骼). 只看骨骼的话脚尖下压穿地、双脚悬空都量不到
    const soles = loco?.sole ? loco._solePoints() : null;
    const sole = soles?.map((P) => Math.min(...P.map((p) => p[1])));
    if (sole) {
      if (Math.min(...sole) < -0.015) c.floorPen++;                // 鞋底穿进地面 1.5 cm 以上 (地面 y = 0)
      if (!seated && Math.min(...sole) > 0.01) c.floating++;       // 两只脚都离地 1 cm 以上: 悬空 (没有跳跃动作)
      this.soleNow = sole;
      pain.floorPen = Math.max(0, -Math.min(...sole) - 0.005);
      pain.floating = seated ? 0 : Math.max(0, Math.min(...sole) - 0.01);
    }
    for (const [i, side] of ['left', 'right'].entries()) {
      const ankle = pos[`${side}Foot`], toe = pos[`${side}Toes`] || ankle;
      if (!ankle) continue;
      const st = loco?.feet?.[side];
      // 着地: 行动层锁定的脚, 或者鞋底离地不到 2 cm (待机库没有着地标记). 支撑面用鞋底的着地部分 (含脚跟、脚尖).
      // 以前按脚踝离地高度判断、用脚踝到脚尖的长方形: 各模型站着时脚踝高度的基准不同 (miomio 比静止低 4 cm), 长方形又没有脚跟
      const planted = (!!st?.lock && st.w > 0.5) || (sole ? sole[i] < 0.02 : ankle[1] < (loco?.ankle?.[side] ?? 0.1) + 0.025);
      if (planted) { pts.push(...(soles ? soleContact(soles[i]) : footPoints(ankle, toe))); feetDown++; }
      if (!sole && Math.min(ankle[1], toe[1]) < -0.015) c.floorPen++;  // 脚穿进地面 (地面 y = 0)
      const prev = this.prevAnkle[side];
      const onSeat = !!loco?.seated || (loco?.seatAmount ?? 0) > 0.05;  // 坐着时脚跟着座位落点走, 不锁在地上, 不算打滑
      // 支撑脚 (动作数据标着着地、脚踝也在地面附近) 每帧水平移动超过 5 mm 算打滑. 以前只看行动层锁定的脚 (锁定权重 > 0.9),
      // 自动调优把锁定调慢后打滑就"量不到"了; 按动作数据的着地标记量, 和可调参数无关
      const down = loco?.lastContacts?.[side === 'left' ? 0 : 1] && ankle[1] < (loco?.ankle?.[side] ?? 0.1) + 0.025;
      if (down && prev && !onSeat) {
        const d = Math.hypot(ankle[0] - prev[0], ankle[2] - prev[2]);
        if (d > 0.005) { c.slideMm += d * 1000; pain.slide += d - 0.005; }
      }
      this.prevAnkle[side] = ankle;
    }
    const hull = convexHull(pts);
    const com = centerOfMass(pos);
    const moving = loco && (loco.mode === 'go' || Math.hypot(loco.sim?.vx || 0, loco.sim?.vz || 0) > 0.1);
    const stability = com && hull.length >= 3 ? signedMargin(hull, [com[0], com[2]]) : null;
    // 走路时质心本来就会离开支撑面 (动态平衡), 只在站定和做动作时统计静态稳定性; 坐着时座面也是支撑, 不算
    const staticCheck = !moving && !seated && stability !== null;
    if (staticCheck && stability < 0) c.unstable++;
    const bad = joints.filter((j) => j.violated);
    c.violations += bad.length;
    c.frames++;
    pain.joint = jointExcess(joints);
    pain.jerk = this._jerk(h, dt, q);
    pain.balance = staticCheck && stability < 0 ? -stability : 0;
    if (pain.balance > 0) {  // 诊断: 重心出支撑面时在做什么、几只脚着地 (执行评估里按这个拆)
      const what = loco?.action ? `act:${loco.action.c.kind}` : loco?.idle ? `idle:${loco.idle.kind}` : `mode:${loco?.mode}`;
      const k = `${what}|${feetDown}脚`;
      this.balanceBy[k] = (this.balanceBy[k] || 0) + pain.balance;
    }
    pain.heldAcc = this._heldAcc(loco, dt);
    this._painOf(pain, pos, seated);
    this.now = { stability, staticCheck, seated, moving: !!moving, joints, bad: bad.map((j) => `${j.joint} ${j.value}°`),
      pushes: a.contact?.stats?.pushes ?? 0, suppressed: a.filter?.stats?.suppressed ?? 0 };
    if (this.episode) {
      const e = this.episode;
      if (staticCheck) e.minMargin = Math.min(e.minMargin, stability);
      e.maxViolations = Math.max(e.maxViolations, bad.length);
      e.painMax = Math.max(e.painMax, this.painNow.score || 0);
    }
    if (this.group.visible) this._draw(hull, com, joints, pos, stability, staticCheck);
  }

  /** 各骨骼世界朝向的角加速度超过 JERK_LIMIT 的部分 (乘 dt, 单位 rad/s), 加起来. */
  _jerk(h, dt, q) {
    this._ang ??= {};
    let s = 0;
    for (const n of JERK_BONES) {
      const node = h.getNormalizedBoneNode(n);
      if (!node) continue;
      node.getWorldQuaternion(q);
      const cur = [q.x, q.y, q.z, q.w], st = this._ang[n];
      if (st) {
        const w = quatAngle(st.q, cur) / dt;
        if (st.w !== null) s += Math.max(0, Math.abs(w - st.w) / dt - JERK_LIMIT) * dt;
        st.w = w;
        st.q = cur;
      } else this._ang[n] = { q: cur, w: null };
    }
    return s;
  }

  /** 拿着东西时掌心持物点 (行动层的 _palmHold, 按此刻 (平滑层之后) 手的位置和朝向) 的线加速度超过 HELD_ACC_LIMIT 的部分乘 dt.
   *  每只手记前两帧, 换了东西、帧长变了从头记; 拿起那一刻 (还没有前两帧) 不算, 那一下的隔空由 grab 量. */
  _heldAcc(loco, dt) {
    this._held ??= {};
    let s = 0;
    for (const side of ['left', 'right']) {
      const held = loco?._palmHold ? Object.entries(loco.items || {}).find(([, i]) => i.held === side) : null;
      if (!held) { this._held[side] = null; continue; }
      const [id, item] = held, p = loco._palmHold(item, side).toArray(), st = this._held[side];
      const same = st && st.id === id && Math.abs(st.dt - dt) < 1e-6;
      if (same && st.p2) s += accExcess(st.p2, st.p1, p, dt);
      this._held[side] = { id, dt, p1: p, p2: same ? st.p1 : null };
    }
    return s;
  }

  /** 外面量到的痛觉 (评估场景里液体洒出来的量 spill 等) 加进累计和这一帧. */
  addPain(term, v) {
    if (!PAIN_TERMS[term] || !(v > 0)) return;
    this.pain[term] += v;
    this.pain.score = (this.pain.score || 0) + v * PAIN_TERMS[term].w;
    if (this.painNow) { this.painNow[term] = (this.painNow[term] || 0) + v; this.painNow.score = (this.painNow.score || 0) + v * PAIN_TERMS[term].w; }
  }

  /** 痛觉里和接触层、平滑层、家具、物品有关的几项, 加进累计. */
  _painOf(pain, pos, seated) {
    const a = this.avatar, loco = a.loco;
    pain.selfPen = a.contact ? a.contact.penetration(loco?.heldItems() || []) : 0;
    pain.legPen = loco?.legShape && !seated ? legPenetration(loco.legCapsules()) : 0;  // 坐着翘腿、盘腿是动作本身, 不算
    pain.pushed = a.contact?.stats?.lastPush || 0;
    const sup = a.filter?.stats?.suppressed ?? 0;
    pain.jump = this._sup === undefined ? 0 : sup - this._sup;
    this._sup = sup;
    const gap = loco?.stats?.grabGap ?? 0;
    pain.grab = this._gap === undefined ? 0 : gap - this._gap;
    this._gap = gap;
    if (loco?.scene) {
      // 碰撞体: 房间里实际的家具部件 (见 scene.js 的 collisionParts); 没有房间时用场景 JSON 的包围盒
      this.parts ??= a.collision?.() || boxGroups(furnitureBoxes(loco.scene.objects));
      this.room ??= loco.scene.floor ? floorRect(loco.scene.floor) : null;
      // 坐着时所坐那件家具的软垫不算, 座面只放过搭在面上、陷进去不到 8 cm 的点 (膝盖压在座垫前沿、手撑在旁边);
      // 脚塞进沙发底座、靠背和扶手照算
      const seatId = seated ? (loco.seated?.seat?.id || loco.action?.seat?.info?.id) : null;
      const skip = seatId ? (g, part, p) => g.id === seatId && (part.part === 'soft' || (part.part === 'seat' && p[1] > part.max[1] - 0.08)) : null;
      for (const [n, r] of BODY_PROBES) {
        const p = pos[n];
        if (!p) continue;
        pain.furniture += Math.max(this.room ? roomDepth(p, r, this.room) : 0, partsDepth(this.parts, p, r, skip));
      }
      // 坐稳时臀部陷进座面: 髋关节比坐下时对准的高度 (座面 + 0.12 髋高, 见 locomotion._seatLift) 低 5 cm 以上的部分.
      // 髋不在上面的探针里, 以前骨盆陷进座位 15-20 cm 也量不到
      const sat = loco.mode === 'seated' ? loco.seated?.seat : null;
      if (sat) pain.furniture += Math.max(0, sat.height + 0.12 * loco.hipHeight - 0.05 - pos.hips[1]);
      for (const item of Object.values(loco.items || {})) {
        const p = item.mesh.position.toArray();
        pain.itemPen += Math.max(Math.max(0, item.half - p[1] - 0.005), partsDepth(this.parts, p, item.half - 0.005));  // 地面、家具
      }
    }
    pain.score = painScore(pain);
    this.painNow = pain;
    this.painPeak = Math.max(pain.score, this.painPeak * 0.989);  // 60 帧约 1 秒半衰
    for (const k of Object.keys(PAIN_TERMS)) this.pain[k] += pain[k];
    this.pain.score = (this.pain.score || 0) + pain.score;
  }

  _draw(hull, com, joints, pos, stability, staticCheck) {
    const y = 0.006;
    this.poly.geometry.setFromPoints(hull.map(([x, z]) => new THREE.Vector3(x, y, z)));
    this.poly.visible = hull.length >= 3;
    if (com) {
      const col = stability === null ? AMBER : colorOf(staticCheck ? stability / 0.03 : 1);
      this.com.position.set(com[0], y, com[2]);
      this.com.material.color.copy(col);
      this.poly.material.color.copy(col);
      this.comLine.geometry.setFromPoints([new THREE.Vector3(com[0], com[1], com[2]), new THREE.Vector3(com[0], y, com[2])]);
    }
    for (const j of joints) {
      const m = this._mark(j.joint), p = pos[j.joint];
      if (!p) continue;
      m.position.set(...p);
      m.material.color.copy(j.violated ? RED : colorOf(j.margin));
    }
  }

  /** 一个动作开始: 记下计数器起点. 跨帧的取样 (骨骼朝向 _ang、掌心 _held、脚踝 prevAnkle) 从这一段重新记,
   *  这一段的角加速度、持物加速度、打滑不拿上一段最后一帧比 (头两帧不算). */
  begin(label) {
    this._ang = {};
    this._held = {};
    this.prevAnkle = {};
    const c = this.counters, a = this.avatar;
    this.episode = { label, t0: performance.now(), c0: { ...c }, push0: a.contact?.stats?.pushes ?? 0,
      sup0: a.filter?.stats?.suppressed ?? 0, minMargin: Infinity, maxViolations: 0, pain0: { ...this.pain }, painMax: 0 };
  }

  /** 动作结束: 汇总执行评估. result 是行动层给的失败原因 (成功为 null). */
  end(result = null) {
    const e = this.episode;
    if (!e) return null;
    this.episode = null;
    const c = this.counters, a = this.avatar, frames = Math.max(1, c.frames - e.c0.frames);
    const secs = (performance.now() - e.t0) / 1000;
    return {
      label: e.label, ok: !result, reason: result || null, seconds: +secs.toFixed(2), frames,
      model: a.vrm?.meta?.name || a.vrm?.meta?.title || null,
      min_static_margin_cm: Number.isFinite(e.minMargin) ? +(e.minMargin * 100).toFixed(1) : null,
      unstable_frames: c.unstable - e.c0.unstable,
      joint_violation_frames: c.violations - e.c0.violations, max_violations: e.maxViolations,
      foot_slide_mm: +(c.slideMm - e.c0.slideMm).toFixed(1), floor_penetration_frames: c.floorPen - e.c0.floorPen,
      floating_frames: c.floating - e.c0.floating,
      arm_pushes: (a.contact?.stats?.pushes ?? 0) - e.push0, suppressed_jumps: (a.filter?.stats?.suppressed ?? 0) - e.sup0,
      // 痛觉: 各项在这段动作里的累计 (单位见 pain.js), 痛觉值的每帧平均和峰值
      pain: Object.fromEntries(Object.keys(PAIN_TERMS).map((k) => [k, +(this.pain[k] - (e.pain0[k] || 0)).toFixed(4)])),
      // 运动仲裁表: 这段动作里哪一层驱动哪个部位 (各部位各层的平均权重 + 每 0.2 秒一行的时间线, 见 arbiter.js)
      arbiter: a.arbiter?.report(e.t0 / 1000, performance.now() / 1000) ?? null,
      pain_mean: +((this.pain.score - (e.pain0.score || 0)) / frames).toFixed(3), pain_max: +e.painMax.toFixed(2),
    };
  }
}
