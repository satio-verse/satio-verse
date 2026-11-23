// 刚体 (小窝的物理与后果 P1, 见 docs/roadmap/room-physics.md): Rapier (web/vendor/rapier, @dimforge/rapier3d-compat 0.21.0,
// Apache-2.0) 的薄封装. 不依赖 three.js (Node 里直接单测); 位置 [x, y, z]、朝向四元数 [x, y, z, w], y 朝上.
//
// 物件分三种: 固定的 (墙、地、台面、固定器材)、动态的 (杯子、球、小哑铃, 能挪的家具有真实的质量、摩擦、重心)、
// 运动学的 (她的身体胶囊、手: 跟着动画走, 推得动轻的东西; 重的东西推不动, 由宿主看 blocked 让她停下).
// 交给物理的东西, 动画不能每帧再改写它的位置: 宿主只读 pose(), 拿在手里的由抓持 (grip.js) 用有上限的力拉着.
//
// 固定步长 dt (1/60 秒), 一帧最多补 maxSubsteps 步 (卡顿时丢掉多的, 不追). before(h) 每一小步之前调 (抓持在这里加力).
// 事件: 碰撞开始 / 结束 (两边的 id)、接触力超过门槛 (哪两件、多大的力, 牛顿), 给宿主转成她感知得到的证据.

import RAPIER from '../vendor/rapier/rapier.mjs';

export const RIGID = {
  dt: 1 / 60,
  maxSubsteps: 4,
  gravity: -9.81,
  forceEvent: 2,     // 接触力超过这么多 (牛顿) 才报 (放着不动时的压力不报)
};

// 量耗时用真的时钟 (执行评估会把 performance.now 换成模拟时钟)
const realNow = performance.now.bind(performance);
let ready = null;
/** 载入 Rapier (WASM 内嵌在 rapier.mjs 里, 第一次几百毫秒). */
export function loadRapier() {
  ready ??= RAPIER.init().then(() => RAPIER);
  return ready;
}

const v3 = (a) => ({ x: a[0], y: a[1], z: a[2] });
const q4 = (a) => ({ x: a[0], y: a[1], z: a[2], w: a[3] });

export class RigidWorld {
  /** 先 await loadRapier(), 再 new; 或者直接 await RigidWorld.create(). */
  constructor(R) {
    this.R = R;
    this.world = new R.World({ x: 0, y: RIGID.gravity, z: 0 });
    this.world.timestep = RIGID.dt;
    this.queue = new R.EventQueue(true);
    this.items = new Map();     // id -> {body, collider, kind}
    this.byCollider = new Map();  // collider handle -> id
    this.acc = 0;
    this.stats = { steps: 0, ms: 0, frames: 0, maxMs: 0, dropped: 0 };
  }

  static async create() {
    return new RigidWorld(await loadRapier());
  }

  _add(id, bodyDesc, colliderDesc, kind, data = {}) {
    if (this.items.has(id)) this.remove(id);
    const body = this.world.createRigidBody(bodyDesc);
    colliderDesc.setActiveEvents(this.R.ActiveEvents.COLLISION_EVENTS | this.R.ActiveEvents.CONTACT_FORCE_EVENTS);
    colliderDesc.setContactForceEventThreshold(RIGID.forceEvent);
    const collider = this.world.createCollider(colliderDesc, body);
    this.items.set(id, { id, body, collider, kind, ...data });
    this.byCollider.set(collider.handle, id);
    return this.items.get(id);
  }

  /** 固定的盒子 (按包围盒 min / max; 台面、家具部件、地面、墙). */
  addFixedBox(id, min, max, { friction = 0.7 } = {}) {
    const c = [(min[0] + max[0]) / 2, (min[1] + max[1]) / 2, (min[2] + max[2]) / 2];
    const h = [Math.max(1e-3, (max[0] - min[0]) / 2), Math.max(1e-3, (max[1] - min[1]) / 2), Math.max(1e-3, (max[2] - min[2]) / 2)];
    return this._add(id, this.R.RigidBodyDesc.fixed().setTranslation(...c),
      this.R.ColliderDesc.cuboid(...h).setFriction(friction), 'fixed');
  }

  /** 固定的三角网格 (房间模型: vertices 世界坐标 xyz 连着排, indices 三个一组). */
  addFixedMesh(id, vertices, indices, { friction = 0.7 } = {}) {
    return this._add(id, this.R.RigidBodyDesc.fixed(), this.R.ColliderDesc.trimesh(vertices, indices).setFriction(friction), 'fixed');
  }

  /**
   * 动态物件. shape: {box: [hx, hy, hz]} | {cylinder: [halfHeight, radius]} | {ball: r}; mass 千克 (按真实的);
   * com: 重心相对几何中心的偏移 (可挪的家具重心偏下); ccd: 小而快的 (杯子、球) 开连续碰撞.
   */
  addDynamic(id, { shape, mass = 0.3, friction = 0.6, restitution = 0.05, p = [0, 0, 0], q = [0, 0, 0, 1], ccd = true,
    linDamp = 0.05, angDamp = 0.3, data = {} }) {
    const R = this.R;
    const cd = shape.box ? R.ColliderDesc.cuboid(...shape.box) : shape.cylinder ? R.ColliderDesc.cylinder(...shape.cylinder)
      : R.ColliderDesc.ball(shape.ball);
    cd.setMass(mass).setFriction(friction).setRestitution(restitution);
    const bd = R.RigidBodyDesc.dynamic().setTranslation(...p).setRotation(q4(q)).setCcdEnabled(ccd)
      .setLinearDamping(linDamp).setAngularDamping(angDamp);
    return this._add(id, bd, cd, 'dynamic', { mass, ...data });
  }

  /** 运动学物件 (她的身体胶囊、手): 每帧 setKinematic 给下一步的位置, 推得动动态物件. shape 同上, 外加 {capsule: [halfHeight, r]}. */
  addKinematic(id, { shape, p = [0, 0, 0], q = [0, 0, 0, 1], data = {} }) {
    const R = this.R;
    const cd = shape.capsule ? R.ColliderDesc.capsule(...shape.capsule) : shape.box ? R.ColliderDesc.cuboid(...shape.box)
      : R.ColliderDesc.ball(shape.ball);
    return this._add(id, R.RigidBodyDesc.kinematicPositionBased().setTranslation(...p).setRotation(q4(q)), cd, 'kinematic', data);
  }

  setKinematic(id, p, q = null) {
    const it = this.items.get(id);
    if (!it) return;
    it.body.setNextKinematicTranslation(v3(p));
    if (q) it.body.setNextKinematicRotation(q4(q));
  }

  /** 直接放到某处 (存档读回来、她放下时手里的东西): 带速度或者静止. */
  setPose(id, p, q = null, v = [0, 0, 0], w = [0, 0, 0]) {
    const it = this.items.get(id);
    if (!it) return;
    it.body.setTranslation(v3(p), true);
    if (q) it.body.setRotation(q4(q), true);
    it.body.setLinvel(v3(v), true);
    it.body.setAngvel(v3(w), true);
  }

  pose(id) {
    const it = this.items.get(id);
    if (!it) return null;
    const t = it.body.translation(), r = it.body.rotation(), v = it.body.linvel(), w = it.body.angvel();
    return { p: [t.x, t.y, t.z], q: [r.x, r.y, r.z, r.w], v: [v.x, v.y, v.z], w: [w.x, w.y, w.z], sleeping: it.body.isSleeping() };
  }

  /** 碰撞分组 (16 位的"我是哪几组"、"跟哪几组碰"): 她的身体胶囊不碰她手里拿着的东西. */
  setGroups(id, member, filter = 0xffff) {
    this.items.get(id)?.collider.setCollisionGroups(((member & 0xffff) << 16) | (filter & 0xffff));
  }

  /** 给这一件加一个冲量 (牛顿·秒), 作用在重心. */
  impulse(id, J, wake = true) { this.items.get(id)?.body.applyImpulse(v3(J), wake); }
  torqueImpulse(id, J, wake = true) { this.items.get(id)?.body.applyTorqueImpulse(v3(J), wake); }
  mass(id) { return this.items.get(id)?.body.mass() ?? 0; }

  remove(id) {
    const it = this.items.get(id);
    if (!it) return;
    this.byCollider.delete(it.collider.handle);
    this.world.removeRigidBody(it.body);
    this.items.delete(id);
  }

  /** 推进 dt 秒 (按固定步长补步). before(h): 每一小步之前. 返回这一帧的事件 [{type: 'touch' | 'part' | 'force', a, b, force}]. */
  step(dt, before = null) {
    const t0 = realNow(), ev = [];
    this.acc = Math.min(this.acc + dt, RIGID.dt * RIGID.maxSubsteps);
    let n = 0;
    while (this.acc >= RIGID.dt - 1e-9) {
      before?.(RIGID.dt);
      this.world.step(this.queue);
      this.queue.drainCollisionEvents((h1, h2, started) => {
        ev.push({ type: started ? 'touch' : 'part', a: this.byCollider.get(h1), b: this.byCollider.get(h2) });
      });
      this.queue.drainContactForceEvents((e) => {
        ev.push({ type: 'force', a: this.byCollider.get(e.collider1()), b: this.byCollider.get(e.collider2()), force: e.totalForceMagnitude() });
      });
      this.acc -= RIGID.dt;
      n++;
    }
    const ms = realNow() - t0;
    Object.assign(this.stats, { steps: this.stats.steps + n, ms: this.stats.ms + ms, frames: this.stats.frames + 1, maxMs: Math.max(this.stats.maxMs, ms) });
    return ev;
  }
}
