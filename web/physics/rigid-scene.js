// 把行动层 (locomotion.js) 的东西和房间交给刚体 (rigid.js; 小窝的物理与后果 P1、P2): 房间的家具部件、地面是固定的;
// 交给物理的东西 (adopt) 是动态的, 不拿着时自己落、自己停, 拿在手里时由抓持 (grip.js) 用有上限的力拉向掌心;
// 她的身体是跟着根走的运动学胶囊 (推得动地上的东西, 不碰她手里拿着的).
// 行动层那边: 交给物理的东西标 item.physical, _items 只算掌心目标 (item.target) 不摆, _release 只松手不瞬移
// (放到台面、地上时给一个落点 item.target = {p, q, place: true}: 手接着拿着往下放, 放稳了才松开, 见 GRIP.place).
// 握住的那一刻东西跳到掌心 (和不交给物理时一样: 拿起时手离东西差多远是动作那边的事, 行动层的 grabGap 记着; 这里事件里也带 gap).
// 网页版评测台 (motion-eval.js 的 carry 加 phys=1) 和小窝 (desktop/web/room-rigid.js) 共用.

import { GRIP, Grip } from './grip.js';

const G_WORLD = 0x0001, G_BODY = 0x0002, G_HELD = 0x0004;  // 碰撞分组: 一般的东西、她的身体、她手里拿着的

export class PhysicsLayer {
  /**
   * rigid: RigidWorld; loco: 行动层; groups: 房间的碰撞部件 ([{id, parts: [{min, max}]}], 见 scene.js 的 collisionParts);
   * floor: 地面高度; character: 要不要她的身体胶囊; strength(side): 这只手握得多紧 (0-1, 烫伤的手握不紧).
   */
  constructor(rigid, loco, { groups = [], floor = 0, character = true, strength = null } = {}) {
    Object.assign(this, { rigid, loco, strength });
    this.items = {};     // id -> {item, shape, mass, half}
    this.grips = {};     // id -> Grip
    this.last = {};      // id -> 上一帧的掌心目标 (算速度)
    this.events = [];
    for (const g of groups) g.parts.forEach((p, k) => rigid.addFixedBox(`${g.id}#${k}`, p.min, p.max));
    rigid.addFixedBox('floor', [-50, floor - 0.2, -50], [50, floor, 50]);
    if (character) {
      rigid.addKinematic('body', { shape: { capsule: [0.45, 0.16] }, p: [loco.char.x, 0.62, loco.char.z] });
      rigid.setGroups('body', G_BODY, 0xffff & ~G_HELD);
    }
    this.character = character;
  }

  /** 一件东西交给物理: shape ({cylinder: [半高, 半径]} / {box: [...]} / {ball: r}), mass 千克; 从 mesh 现在的位置开始. */
  adopt(item, { shape, mass, friction = 0.6 }) {
    const p = item.mesh.position.toArray(), q = item.mesh.quaternion.toArray();
    this.rigid.addDynamic(item.id, { shape, mass, friction, p, q });
    this.rigid.setGroups(item.id, G_WORLD);
    item.physical = true;
    const half = shape.cylinder ? shape.cylinder[0] : shape.box ? shape.box[1] : shape.ball;
    this.items[item.id] = { item, shape, mass, half };
  }

  /** 不再交给物理 (还给动画). */
  release(item) {
    this.rigid.remove(item.id);
    delete this.items[item.id];
    delete this.grips[item.id];
    item.physical = false;
  }

  /** 改一件的质量 (杯里的液体多了少了). */
  setMass(id, mass) {
    const it = this.rigid.items.get(id);
    if (it && Math.abs(it.collider.mass() - mass) > 0.005) it.collider.setMass(mass);
  }

  /**
   * 每帧 (行动层摆完这一帧以后): 身体胶囊跟着根; 拿着的拉向掌心 (这一帧的掌心目标在 item.target, 小步之间按直线插值);
   * 松了手的放开 (放到台面上的先放稳); 推进; 脱手的告诉行动层; 把位置写回 mesh. 返回这一帧的事件:
   * {type: 'grab', id, side, gap}、{type: 'drop' | 'slip' | 'release', id, side}、{type: 'bump', a, b, force} (碰撞、受阻;
   * 宿主转成她感知得到的). grab 时东西是瞬移到掌心的, 宿主别把这一帧的位移当成加速度 (液体: cup.teleported()).
   */
  tick(dt) {
    const L = this.loco, r = this.rigid, ev = [];
    if (this.character) r.setKinematic('body', [L.char.x, 0.62, L.char.z]);
    const plans = {};
    for (const [id, o] of Object.entries(this.items)) {
      const it = o.item, side = it.held;
      if (side && !this.grips[id] && it.target) {
        const P0 = r.pose(id), t = it.target.p;
        const gap = P0 ? Math.hypot(P0.p[0] - t[0], P0.p[1] - t[1], P0.p[2] - t[2]) : 0;
        r.setPose(id, t, it.target.q);
        this.grips[id] = new Grip(r, id, side, { strength: this.strength?.(side) ?? 1 });
        r.setGroups(id, G_HELD);
        it.dropped = false;  // 脱手过又拿起来了
        ev.push({ type: 'grab', id, side, gap: +gap.toFixed(3) });
        this.last[id] = null;
      } else if (!side && this.grips[id]) {
        // 松手: 有落点的 (放到台面上、地上) 接着拿着往落点放, 放稳了或者到了 GRIP.place 秒才放开; 没有落点的马上放开, 带着当时的速度
        const g = this.grips[id];
        if (it.target?.place) g.placing = (g.placing ?? -dt) + dt;
        else this._letGo(id, ev);
      }
      const g = this.grips[id];
      if (g && it.target) {
        const p1 = it.target.p, p0 = this.last[id] || p1;
        plans[id] = { g, p0, p1, q: it.target.q, v: [(p1[0] - p0[0]) / dt, (p1[1] - p0[1]) / dt, (p1[2] - p0[2]) / dt] };
        this.last[id] = [...p1];
      }
    }
    let t = 0;
    const raw = r.step(dt, (h) => {
      t += h;
      const k = Math.min(1, t / dt);
      for (const [id, P] of Object.entries(plans)) {
        if (P.g.state === 'drop') continue;
        const p = [P.p0[0] + (P.p1[0] - P.p0[0]) * k, P.p0[1] + (P.p1[1] - P.p0[1]) * k, P.p0[2] + (P.p1[2] - P.p0[2]) * k];
        const before = P.g.state;
        P.g.step(h, { p, q: P.q, v: P.v });
        if (P.g.state === 'slip' && before === 'hold') ev.push({ type: 'slip', id, side: P.g.side });
      }
    });
    // 放稳了就放开: 碰到台面、地 (固定的东西) 了, 或者手到了落点、东西不动了, 或者放了太久.
    // (手跟不上动画: 放下的片段手往下甩得快, 手落后掌心目标十几厘米, 东西先碰到台面; 手再往下按就滑脱了. 人是碰到台面就松手)
    const touched = new Set(raw.filter((e) => e.type === 'force' && (r.items.get(e.a)?.kind === 'fixed' || r.items.get(e.b)?.kind === 'fixed'))
      .flatMap((e) => [e.a, e.b]));
    for (const [id, P] of Object.entries(plans)) {
      if (P.g.placing === undefined || P.g.state === 'drop') continue;
      const v = r.pose(id)?.v || [0, 0, 0], h = P.g.hand.p;
      const there = Math.hypot(h[0] - P.p1[0], h[1] - P.p1[1], h[2] - P.p1[2]) < 0.01;
      if (touched.has(id) || (there && Math.hypot(...v) < 0.05) || P.g.placing >= GRIP.place) this._letGo(id, ev);
    }
    // 脱手: 行动层那边松手 (东西带着当时的速度继续飞)
    for (const [id, P] of Object.entries(plans)) {
      if (P.g.state !== 'drop') continue;
      const it = this.items[id].item;
      ev.push({ type: 'drop', id, side: P.g.side });
      delete this.grips[id];
      r.setGroups(id, G_WORLD);
      it.held = null;
      it.on = null;
      it.dropped = true;
    }
    for (const e of raw) {
      if (e.type !== 'force' || !e.a || !e.b) continue;
      ev.push({ type: 'bump', a: e.a, b: e.b, force: +e.force.toFixed(2) });
    }
    // 写回 mesh
    for (const [id, o] of Object.entries(this.items)) {
      const P = r.pose(id);
      if (!P) continue;
      o.item.mesh.position.fromArray(P.p);
      o.item.mesh.quaternion.fromArray(P.q);
      o.pose = P;
    }
    this.events = ev;
    return ev;
  }

  /** 放开 (还在动的带着当时的速度). */
  _letGo(id, ev) {
    const g = this.grips[id];
    delete this.grips[id];
    this.rigid.setGroups(id, G_WORLD);
    const it = this.items[id]?.item;
    if (it && !it.held) it.target = null;
    if (g && g.state !== 'drop') ev.push({ type: 'release', id, side: g.side });
  }

  /** 这一件此刻是不是稳在手里 (三件事分开: 请求握住、手指看起来握住了、实际稳住了). */
  holdState(id, fingers = null) {
    const g = this.grips[id];
    if (!g) return null;
    if (fingers !== null) g.fingers = fingers;
    return g.summary();
  }
}
