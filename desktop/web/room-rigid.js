// 小窝的刚体 (物理与后果 P1、P2, 见 docs/roadmap/room-physics.md): 房间里的小物件交给 Rapier (web/physics/rigid.js),
// 拿在手里的用有上限的力抓着 (web/physics/grip.js), 会滑、会脱手; 松手的、被她碰到的自己落、自己停.
// 先挂开关: 页面加 rigid=1, 或者 localStorage pet.room.rigid = '1' (Rapier 4 MB, 开了才载入). 默认关, 和原来一样.
//
// 醒着的在活动层: 她伸手去拿 (props.wake)、身体碰到的时候交给物理; 停下来 (刚体睡着) 就还给房间缓存, 记下停在哪
// (歪倒的连朝向一起, 存在 pet.room.rigid.poses; 竖着的照旧在 pet.room.layout).
// 交给物理的东西动画不再每帧摆: 行动层只给掌心目标 (item.target), 房间物件跟着刚体 (room-props.js 的 follow 跳过它).
//
// 她知道的只是感知得到的 (perceive / 体感的 feelings), 不是真值: 手里的东西在滑 (slip)、脱手了 (drop)、身上被碰了一下 (bump)、
// 看见东西倒了掉了 (fall, 看得见才有, 带是哪件)、听见响 (noise, 看不见只知道响了, 不知道是什么). 力多大、停在哪的坐标不给.
// 钩子 onEvent(e) 同 room-liquid.js: 认知那边的感知门设, 这里不设.
import * as THREE from 'three';
import { boxGroups, furnitureBoxes } from '/static/pain.js';

const KEY = 'pet.room.rigid';
const POSES = 'pet.room.rigid.poses';
const LOUD = [[10, 'soft'], [40, 'clack'], [Infinity, 'bang']];  // 落下、撞上的响 (牛顿): 轻轻一声 / 咔哒 / 砰
const MASS = { cup: 0.3, book: 0.5, soft: 0.2 };                   // 千克 (杯子另加里面的液体)
const SEE = { dist: 3, half: 1.2 };  // 看得见: 离头 3 m 内、脸朝的方向左右 1.2 弧度内 (和 room-liquid.js 一样, 不算遮挡)
const COOL = 3;                       // 身体碰醒的东西停下以后这么多秒内不再碰醒 (站在旁边时不反复交出去、还回来)
const bin = (table, x) => table.find(([lim]) => x < lim)[1];

export const rigidOn = () => {
  try { return new URLSearchParams(location.search).get('rigid') === '1' || localStorage.getItem(KEY) === '1'; } catch { return false; }
};

export class RoomRigid {
  constructor(r) {
    this.r = r;
    try { this.storage = r.storage === undefined ? globalThis.localStorage : r.storage; } catch { this.storage = null; }
    this.felt = [];
    this.hits = {};      // id -> 这一回撞上最大的力 (落地那一下报一次)
    this.stats = { frames: 0, ms: 0, maxMs: 0, adopted: 0, slept: 0 };
    this.ready = this._load().catch((err) => { console.warn('刚体载入失败', err); this.failed = String(err); });
  }

  async _load() {
    const [{ RigidWorld }, { PhysicsLayer }] = await Promise.all([import('/static/physics/rigid.js'), import('/static/physics/rigid-scene.js')]);
    const t0 = performance.now();
    const { layer, props, body } = this.r;
    this.world = await RigidWorld.create();
    // 房间的家具部件 (行动层的碰撞体; 没有的话按场景 JSON 的家具盒子, 和 room-liquid.js 的落点一样)
    const parts = layer.avatar?.collision?.();
    const groups = parts?.length ? parts : boxGroups(furnitureBoxes(this.r.scene.objects));
    this.layer = new PhysicsLayer(this.world, layer, { groups, strength: (side) => this._strength(side) });
    this.loadMs = +(performance.now() - t0).toFixed(1);
    // 伸手去拿之前 (房间物件挪进活动层): 交给物理
    const wake = props.wake.bind(props);
    props.wake = (id) => { wake(id); this.adopt(id); };
    this._restore();
  }

  /** 交给物理 (房间物件或投喂的东西; 已经交了就算了). */
  adopt(id) {
    const L = this.layer, item = this.r.layer.items[id];
    if (!L || !item || item.physical || item.held || !this._visible(item)) return;
    const size = item.size || new THREE.Vector3(0.08, 0.1, 0.08);
    const shape = item.grip === 'cup'
      ? { cylinder: [size.y / 2, Math.max(0.02, Math.min(size.x, size.z) / 2)] } : { box: [size.x / 2, size.y / 2, size.z / 2] };
    L.adopt(item, { shape, mass: this._mass(id) });
    const g = this.r.props.glb[id];
    if (g) { this.r.props.toLive(g.node); g.settled = false; g.still = 0; }
    this.stats.adopted++;
  }

  /** 这只手握得多紧 (0-1): 烫伤的手握不紧 (room-body.js 的 grip); 睡着了手松开 (睡着时手里还拿着交给物理的东西, 它会从手里滑下去,
   *  落到被子上、床边, 后果照算; 她睡着, 不知道). */
  _strength(side) {
    return (this.r.life?.sleeping ? 0.05 : 1) * (this.r.body?.grip?.(side) ?? 1);
  }

  /** 房间物件看房间里的那个节点 (行动层摆的是看不见的方盒代理), 投喂的东西看它自己. */
  _visible(item) { const g = this.r.props.glb[item.id]; return g ? g.node.visible : item.mesh.visible; }

  _mass(id) {
    const item = this.r.layer.items[id], ml = this.r.liquid?.world?.cups[id]?.ml || 0;
    return (MASS[item?.grip] ?? 0.3) + ml / 1000;
  }

  /** 这件此刻的刚体位姿 ({p, q, v, w, sleeping}; 没交给物理就是 null). 给液体用 (掉下来、被碰倒的杯子也照样晃、洒). */
  pose(id) { return this.layer?.items[id] ? this.world.pose(id) : null; }

  /** 每帧 (行动层、姿势层摆完以后, 液体之前). */
  tick(dt) {
    const L = this.layer;
    if (!L) return;
    const t0 = performance.now(), { layer, props } = this.r;
    this._touch();
    for (const [id, o] of Object.entries(L.items)) {
      L.setMass(id, this._mass(id));
      if (L.grips[id]) L.grips[id].strength = this._strength(L.grips[id].side);  // 握力每帧跟着她的状态 (烫到、睡着)
      // 喝的时候倾过来: 抓持的目标朝向 = 她面朝的方向 x 姿势层要的倾斜 (不交给物理时 room-props.js 在摆好以后再倾)
      const g = props.glb[id], it = o.item;
      if (g?.tilt && it.held && it.target?.q) it.target.q = g.tilt.clone().multiply(new THREE.Quaternion(...it.target.q)).toArray();
    }
    for (const e of L.tick(dt)) this._event(e);
    for (const [id, o] of Object.entries(L.items)) {
      const g = props.glb[id], P = o.pose;
      if (!P) continue;
      if (g) {  // 房间物件跟着刚体 (代理就是刚体的位姿); 还在动的留在活动层 (活动脚本收尾时 settle 过的也拿回来)
        if (g.settled && !P.sleeping) { props.toLive(g.node); g.settled = false; }
        const q = new THREE.Quaternion(...P.q);
        g.node.position.set(...P.p).add(g.offset.clone().applyQuaternion(q));
        g.node.quaternion.copy(q).multiply(g.q0);
        g.yawNow = new THREE.Euler().setFromQuaternion(q, 'YXZ').y;
        if (!P.sleeping || o.item.held) g.still = 0;
      }
      // 睡着了、没拿着: 还给房间缓存 (props.tick 0.6 秒后放回去、记下停在哪), 歪着的连朝向一起记
      if (P.sleeping && !o.item.held && !L.grips[id]) this._sleep(id, P);
    }
    for (const [id, o] of Object.entries(L.items)) {
      const side = o.item.held;
      if (side) o.hold = L.holdState(id, this.r.v?.contact?.grip?.[side] ?? null);  // 三件事分开: 请求握住、手指看起来握住、实际稳住
    }
    const ms = performance.now() - t0;
    Object.assign(this.stats, { frames: this.stats.frames + 1, ms: this.stats.ms + ms, maxMs: Math.max(this.stats.maxMs, ms) });
  }

  /** 她身体碰到的 (没交给物理的小物件): 交给物理, 让身体胶囊推得动它. */
  _touch() {
    const { layer } = this.r, c = layer.char, now = performance.now() / 1000;
    for (const item of Object.values(layer.items)) {
      if (item.physical || item.held || !item.size || !this._visible(item) || (this.cool?.[item.id] ?? 0) > now) continue;
      const p = item.mesh.position, rad = Math.max(item.size.x, item.size.z) / 2;
      if (p.y < 1.1 && Math.hypot(p.x - c.x, p.z - c.z) < 0.16 + rad) this.adopt(item.id);  // 胶囊半径 0.16 m, 插进去了才算碰到
    }
  }

  _sleep(id, P) {
    const item = this.layer.items[id].item, g = this.r.props.glb[id];
    this.layer.release(item);
    this.stats.slept++;
    if (g?.settled) { this.r.props.cache.dirty = true; this.r.props._remember(g); }  // 已经放回房间了 (活动收尾的 settle): 这里记下停在哪
    (this.cool ??= {})[id] = performance.now() / 1000 + COOL;
    const up = new THREE.Vector3(0, 1, 0).applyQuaternion(new THREE.Quaternion(...P.q)).y;
    const saved = this._poses();
    if (g && up < 0.95) saved[id] = { p: g.node.getWorldPosition(new THREE.Vector3()).toArray().map((x) => +x.toFixed(4)),
      q: g.node.getWorldQuaternion(new THREE.Quaternion()).toArray().map((x) => +x.toFixed(4)), c: P.p.map((x) => +x.toFixed(4)), r: P.q.map((x) => +x.toFixed(4)) };
    else delete saved[id];
    try { this.storage?.setItem(POSES, JSON.stringify(saved)); } catch { /* 存不了就算了 */ }
  }

  _poses() { try { return JSON.parse(this.storage?.getItem(POSES) || '{}') || {}; } catch { return {}; } }

  /** 打开时: 上次歪倒着停下的东西照原样放回去 (竖着的 room-props.js 的 restoreItems 已经放好了). */
  _restore() {
    const { props, layer } = this.r;
    for (const [id, s] of Object.entries(this._poses())) {
      if (!props.asItem(id)) continue;
      const g = props.glb[id], item = layer.items[id];
      g.node.parent?.remove(g.node);
      props.room.add(g.node);
      g.node.position.copy(props.room.worldToLocal(new THREE.Vector3(...s.p)));
      g.node.quaternion.copy(props.room.getWorldQuaternion(new THREE.Quaternion()).invert().multiply(new THREE.Quaternion(...s.q)));
      g.node.updateMatrixWorld(true);
      item.mesh.position.set(...s.c);
      item.mesh.quaternion.set(...s.r);
      item.on = 'moved';
      g.settled = true;
      g.pending = false;
      props.cache.dirty = true;
    }
  }

  // -- 感知 ---------------------------------------------------------------------------------------
  _event(e) {
    const { layer, body } = this.r, hand = (side) => (side === 'left' ? '左手' : '右手');
    if (this.r.life?.sleeping && e.type !== 'release' && e.type !== 'bump') { if (e.type === 'drop') this.hits[e.id] = { force: 0, dropped: true, asleep: true }; return; }
    if (e.type === 'slip') {
      body?.sense?.(hand(e.side), 'slip');
      this._felt({ kind: 'slip', item: e.id, hand: e.side, touched: true });
    } else if (e.type === 'drop') {
      body?.sense?.(hand(e.side), 'slip');
      this._felt({ kind: 'drop', item: e.id, hand: e.side, touched: true });
      this.hits[e.id] = { force: 0, dropped: true };
    } else if (e.type === 'release') {
      this.hits[e.id] = { force: 0 };
    } else if (e.type === 'bump') {
      const other = e.a === 'body' ? e.b : e.b === 'body' ? e.a : null;
      if (other && layer.items[other]) {  // 身上被碰了一下 (按东西的高度估哪里)
        const y = layer.items[other].mesh.position.y;
        if (e.force > 3) body?.sense?.(y < 0.35 ? '脚' : y < 0.8 ? '腿' : '身上', 'bump');
        return;
      }
      for (const id of [e.a, e.b]) {
        const h = this.hits[id];
        if (!h || !layer.items[id] || layer.items[id].held) continue;
        h.force = Math.max(h.force, e.force);
        if (h.reported) continue;
        h.reported = true;  // 落地 / 撞上的第一下: 看得见就知道是哪件、落在哪 (倒没倒), 看不见只听见响
        setTimeout(() => this._landed(id), 120);
      }
    }
  }

  _landed(id) {
    const h = this.hits[id], item = this.r.layer.items[id];
    if (!h || !item) return;
    delete this.hits[id];
    if (h.asleep || this.r.life?.sleeping) return;  // 睡着了: 掉下来她不知道 (醒来看见了才知道)
    const p = item.mesh.position, loud = bin(LOUD, h.force);
    if (loud === 'soft' && !h.dropped) return;  // 放下时轻轻碰到台面: 不算事
    const up = new THREE.Vector3(0, 1, 0).applyQuaternion(item.mesh.quaternion).y;
    if (this._sees(p)) this._felt({ kind: 'fall', item: id, where: p.y < 0.1 ? 'floor' : 'surface', toppled: up < 0.5, loud, seen: true });
    else this._felt({ kind: 'noise', loud, seen: false });
  }

  _sees(p) {
    const head = this.r.layer.nodes.head;
    if (!head) return false;
    const h = head.getWorldPosition(new THREE.Vector3()), q = head.getWorldQuaternion(new THREE.Quaternion());
    const fwd = new THREE.Vector3(0, 0, this.r.poses?.v0 ? -1 : 1).applyQuaternion(q).setY(0).normalize();
    const d = new THREE.Vector3(p.x - h.x, 0, p.z - h.z);
    return d.length() < SEE.dist && (d.length() < 0.4 || fwd.angleTo(d.normalize()) < SEE.half);
  }

  _felt(ev) {
    const e = { ...ev, t: +(performance.now() / 1000).toFixed(1) };
    this.felt.push(e);
    if (this.felt.length > 30) this.felt.shift();
    try { this.onEvent?.(e); } catch (err) { console.warn(err); }
  }

  /**
   * 她感知到的东西的事 (给认知; 不是真值), 自上次取走以后: 固定词表
   *   {kind: 'slip', item, hand, touched}  手里的东西在滑;  {kind: 'drop', item, hand, touched}  脱手了;
   *   {kind: 'fall', item, where: 'floor' | 'surface', toppled, loud: soft | clack | bang, seen: true}  看见东西掉下来 / 倒了;
   *   {kind: 'noise', loud, seen: false}  没看见, 只听见响.
   * 身上的 (哪只手觉得在滑、哪里被碰了一下) 在体感那边 (room-body.js 的 feelings(): slip / bump).
   */
  perceive({ take = true } = {}) {
    return { events: take ? this.felt.splice(0) : this.felt.slice() };
  }

  /** 自检看 (真值, 不给认知). */
  snapshot() {
    const L = this.layer, st = this.stats, ws = this.world?.stats;
    if (!L) return { on: true, ready: false, failed: this.failed || null };
    const items = Object.fromEntries(Object.entries(L.items).map(([id, o]) => [id, { p: o.pose?.p.map((x) => +x.toFixed(3)),
      sleeping: o.pose?.sleeping, held: o.item.held || null, hold: o.hold || null }]));
    return { on: true, ready: true, loadMs: this.loadMs, bodies: this.world.items.size, items, adopted: st.adopted, slept: st.slept,
      msPerFrame: st.frames ? +(st.ms / st.frames).toFixed(3) : 0, maxMs: +st.maxMs.toFixed(2), steps: ws?.steps ?? 0,
      stepMs: ws?.frames ? +(ws.ms / ws.frames).toFixed(3) : 0, felt: this.felt.slice(-8) };
  }
}
