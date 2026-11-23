// 小窝里的液体 (小窝的物理与后果 P0, 见 docs/roadmap/room-physics.md): 杯里装着什么、多少、多烫, 端着走会晃、会洒,
// 洒出来落到她身上 (烫到、弄湿) 和台面、地上 (一摊摊, 会凉、会干, 咖啡干了留下印子). 计算在 web/physics/liquid.js.
//
// 接在哪 (不改小屋的活动脚本): 咖啡机出水 (props.stream) 时出水底下的杯子接住, 没杯子、接满了就洒在台面上;
// 喝 (props.tilt 倾向嘴边) 一口就是从杯里喝掉一口, 空杯喝不到, 太烫会烫嘴. 杯子在手里时按手的运动算晃动.
// 存在本机 (pet.room.liquid), 关了、最小化、切到工作台再回来接着算 (离线的冷却、蒸发按解析解补上).
//
// 这里的量是真值 (环境): 她只能通过看到、碰到、烫到知道. 给认知的只有 perceive() (她这一刻感知得到的证据, 固定词表);
// r.log 只进宿主的调试日志 (wmpet.log), 不进她的记忆 (记忆走 room_event / room_observe, 由认知那边决定收不收).
import * as THREE from 'three';
import { bodyCapsules, grounder, hitter, PART_NAMES } from '/static/physics/body-hit.js';
import { capacityMl, heatEffect, KINDS, LiquidWorld } from '/static/physics/liquid.js';
import { boxGroups, furnitureBoxes } from '/static/pain.js';
import { perfOn } from './perf-flags.js';
import { DownRay } from './room-ray.js';
import { PuddleSheets } from './room-puddles.js';
import { ITEMS, NAMES } from './room-sight.js';
import { ConsumptionOutbox, sipTarget } from './room-consumption.js';
import { COFFEE_EVIDENCE, restoreCoffeeEvidence, tastedCoffee, coffeeThermalView } from './room-coffee-policy.js';

const KEY = 'pet.room.liquid';
const SIP = 15;          // 一口 (ml)
const BREW = 10;         // 咖啡机出水 (ml/秒; 冲一杯 6 秒约 60 ml)
const BREW_TEMP = 70;    // 刚冲好的浓缩咖啡 (℃)
const UP = new THREE.Vector3(0, 1, 0);
// 给认知的固定词表: 看得到杯口时液面的粗档 (占容量的比例以下), 洒了多少的粗档 (毫升以下)
export const LEVELS = [[0.03, 'empty'], [0.3, 'low'], [0.75, 'half'], [Infinity, 'full']];  // 空 / 快见底 / 半杯 / 接近满杯

// Actual consumption must not interpret visible remainder as an empty cup.
// Below the rendered liquid-disc threshold the amount is unknown, not empty.
export function observedCupLevel(cup, actual = false) {
  if (!actual) return bin(LEVELS, cup.ml / cup.capacity);
  if (cup.ml <= 1e-6) return 'empty';
  if (cup.ml < 0.5) return 'unknown';
  const level = bin(LEVELS, cup.ml / cup.capacity);
  return level === 'empty' ? 'low' : level;
}
// 洒了多少的粗档 (和认知那边的感知门同一套分界): 几滴 / 一点 / 一些 / 一大股 / 泼出来一大片
export const AMOUNTS = [[3, 'drops'], [10, 'little'], [20, 'some'], [40, 'stream'], [Infinity, 'splash']];
const bin = (table, x) => table.find(([lim]) => x < lim)[1];
const SEE_DIST = 3;      // 落点离她的头这么远以内、
const SEE_HALF = 1.2;    // 在她脸朝的方向左右这么多弧度以内, 才算看见洒在那里

function read(storage) {
  try { return JSON.parse(storage?.getItem(KEY) || 'null'); } catch { return null; }
}

/** Visible cup geometry only. Quasi-static gravity predicts the low pouring rim;
 * near upright (no unique low edge), approach from the mouth's rim-plane bearing.
 * No liquid amount, temperature, slosh state or physical spring position is read.
 * The short angular blend makes the upright limit continuous; it is not a change
 * to physical intake distance or proof that dynamic overflow follows this edge. */
export function pourRimPoint(p, q, R, H, mouth) {
  const finite = (a, n) => Array.isArray(a) && a.length === n && Array.from(a).every(Number.isFinite);
  if (!finite(p, 3) || !finite(q, 4) || !finite(mouth, 3) || !(R > 0) || !(H > 0) || !Number.isFinite(R + H)) return null;
  const Q = Math.hypot(...q); if (Q < 1e-8) return null;
  const [x, y, z, w] = q.map(v => v / Q), n = [2 * (x*y-w*z), 1-2*(x*x+z*z), 2*(y*z+w*x)];
  const dot = (a, b) => a.reduce((s, v, i) => s + v*b[i], 0);
  const cross = (a, b) => [a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
  const C = p.map((v, i) => v + H*n[i]), toward = mouth.map((v, i) => v-C[i]);
  const along = dot(toward, n), projected = toward.map((v, i) => v-along*n[i]);
  const gravity = [n[0]*n[1], -1+n[1]*n[1], n[2]*n[1]], gn = Math.hypot(...gravity), mn = Math.hypot(...projected);
  if (gn < 1e-10 && mn < 1e-8) return null;
  let dir = mn >= 1e-8 ? projected.map(v => v/mn) : gravity.map(v => v/gn);
  if (gn >= 1e-10) {
    const down = gravity.map(v => v/gn), tilt = Math.acos(Math.max(-1, Math.min(1, n[1])));
    const u = Math.min(1, tilt/.08), blend = u*u*(3-2*u);
    const cosine = Math.max(-1, Math.min(1, dot(dir, down))), sine = dot(n, cross(dir, down));
    const angle = (cosine < -.999999 && Math.abs(sine) < 1e-6 ? Math.PI : Math.atan2(sine, cosine)) * blend;
    const tangent = cross(n, dir), ca = Math.cos(angle), sa = Math.sin(angle);
    dir = dir.map((v, i) => v*ca+tangent[i]*sa);
  }
  return C.map((v, i) => v+R*dir[i]);
}

export class RoomLiquid {
  /** r: 小屋的各个部件 (props、layer、v、body、scene、status、log). */
  constructor(r) {
    this.r = r;
    try { this.storage = r.storage === undefined ? globalThis.localStorage : r.storage; } catch { this.storage = null; }
    this.items = r.sightMap?.ITEMS ?? ITEMS;
    this.names = r.sightMap?.NAMES ?? NAMES;
    const saved = read(this.storage);
    this.world = LiquidWorld.from(saved?.world);
    // Old saves start a fresh stream. Historical ledger.drunk is never rewarded.
    this.consumption = new ConsumptionOutbox(saved?.consumption);
    this._restoreCoffeeEvidence(saved?.coffeeEvidence); this.preparation = null;
    this.ground = this._grounder();
    const away = saved?.at ? (Date.now() - saved.at) / 1000 : 0;
    if (away > 1) this.world.advance(Math.min(away, 30 * 86400), { ground: this.ground });
    this.cups = {};      // id -> { bottom (节点局部坐标的杯底中心), restInv (放着时的朝向的逆), disc }
    this.held = {};
    this.tilted = {};
    this.spill = null;   // 这一阵洒了多少 (一阵结束时记一笔)
    this.felt = [];      // 她感知到的液体事件 (给 perceive, 取走就清)
    this.group = new THREE.Group();
    this.group.name = 'liquid';
    r.props.live.add(this.group);  // 活动层: 每帧画, 存房间时不画
    for (const id of this.items) this._register(id);
    this.puddleMeshes = new Map();
    // 一摊摊水渍、印子用两张实例化网格画 (room-puddles.js; 页面参数 perfoff=puddles 换回每摊一个网格). 构造时就建好, 载入后的预编译能编到
    this.sheets = perfOn('puddles') ? new PuddleSheets(this.group, (kind) => KINDS[kind]?.color || '#888') : null;
    this._wrap();
    this.dirty = true;
    this.lastSave = 0;
    const save = () => this.save();
    addEventListener('pagehide', save);
    document.addEventListener('visibilitychange', () => { if (document.hidden) save(); });
    if (this.consumption.state.pending.length || this.consumption.state.preparations.length) this.save();
  }

  /**
   * 落到哪: 从落点往下照房间模型 (看得见的网格), 最高的那一面; 叫什么按场景的家具盒子 (顶面差 3 cm 以内、在盒子范围里), 不然算地上.
   * 只按场景盒子算时, 地毯比地面高 1-2 cm, 一摊画在地面高度上被地毯盖住看不见. 按 2 cm 一格缓存 (房间不动; 挪了家具清掉).
   */
  _grounder() {
    const named = grounder(boxGroups(furnitureBoxes(this.r.scene.objects)));
    const ray = new THREE.Raycaster(), down = new THREE.Vector3(0, -1, 0), from = new THREE.Vector3(), cache = new Map();
    const visible = (o) => { for (let x = o; x; x = x.parent) if (!x.visible && x !== this.r.props.room) return false; return true; };
    this.groundCache = cache;
    const fast = perfOn('ray') ? (this.downRay = new DownRay()) : null;
    return (x, z, y) => {
      const key = `${Math.round(x * 50)},${Math.round(z * 50)},${Math.round(y * 20)}`;
      if (cache.has(key)) return cache.get(key);
      from.set(x, y + 0.02, z);
      ray.set(from, down);
      ray.far = y + 0.5;
      // 往下照 (2026-10-27): 房间 116 万个三角形, three.js 的 intersectObject 一次 15 ms, 洒的时候一帧几十次; DownRay 按网格分格只试
      // 竖线经过的三角形, 结果和原来逐位相同 (room-ray.js; 页面参数 perfoff=ray 换回原来的)
      const accept = (o) => o.isMesh && visible(o) && !this.items.includes(o.name);
      const hit = fast ? fast.hit(this.r.props.room, from, ray.far, accept) : ray.intersectObject(this.r.props.room, true).find((h) => accept(h.object));
      const box = named(x, z, y);
      const out = hit ? { y: hit.point.y, surface: Math.abs(box.y - hit.point.y) < 0.03 ? box.surface : 'floor' } : box;
      cache.set(key, out);
      return out;
    };
  }

  /** 杯子的形状按房间模型量: 内半径约外半宽 (取窄的那边, 不算把手) 的 0.85, 内高扣掉 6 mm 杯底. */
  _register(id) {
    const node = this.r.props.node(id);
    if (!node) return;
    node.updateMatrixWorld(true);
    const box = new THREE.Box3().setFromObject(node), size = box.getSize(new THREE.Vector3());
    const R = 0.85 * Math.min(size.x, size.z) / 2, H = Math.max(0.02, size.y - 0.006);
    const bottom = node.worldToLocal(new THREE.Vector3((box.min.x + box.max.x) / 2, box.min.y + 0.006, (box.min.z + box.max.z) / 2));
    const restInv = node.getWorldQuaternion(new THREE.Quaternion()).invert();
    if (!this.world.cups[id]) this.world.cup(id, { R, H });
    const c = this.world.cups[id];
    const disc = new THREE.Mesh(new THREE.CircleGeometry(c.R * 0.97, 24), new THREE.MeshStandardMaterial({ roughness: 0.15, metalness: 0 }));
    disc.visible = false;
    disc.name = `liquid_${id}`;
    this.group.add(disc);
    this.cups[id] = { bottom, restInv, disc };
  }

  /** 咖啡机出水、喝: 包一层, 活动脚本不用改. */
  _wrap() {
    const props = this.r.props;
    const stream = props.stream.bind(props), tilt = props.tilt.bind(props);
    props.stream = (cupPos) => { stream(cupPos); this.brewing = !!cupPos; };
    this.tiltRaw = tilt;
    this.tiltGoal = {};  // 杯子 id -> 要倾到多少 (弧度); 每帧慢慢倾过去 (_tiltStep)
    this.sipping = {};   // 杯子 id -> 这一口 {got 喝到, spilled 洒了} (ml)
    this.tiltNow = {};
    // 端着有液体的杯子送到嘴边 (喝 sip): 姿势层的混合、手的目标平滑放慢 2.5 倍. 原来 0.3 秒左右从腰前抬到嘴边,
    // 半杯浓缩杯液面晃起 4-5 cm, 还没倾就溢出来洒到胸口 (真人端着热的、挺满的杯子不会这么猛地往嘴边送)
    const poses = this.r.poses;
    if (poses?.set) {
      const set = poses.set.bind(poses);
      poses.set = (name, opts = {}) => {
        const side = opts.side || 'right', id = Object.values(this.r.layer.items).find((i) => i.held === side)?.id;
        const exactSip = name === 'sip' && this.r.needs?.actualConsumption && this.r.scene?.sceneId === 'casa-luce-v8'
          && !!this.world.cups[id] && !this.r.layer.items[id]?.physical;
        set(name, exactSip ? { ...opts, sipExact: true, sipPalmIn: Math.PI / 6 } : opts);
        if (name === 'sip' && poses.pose && (this.world.cups[id]?.ml ?? 0) > 5) {
          poses.pose = { ...poses.pose, blend: (poses.pose.blend ?? 0.22) * 2.5, halflife: (poses.pose.halflife ?? 0.35) * 2.5 };
        }
      };
    }
    props.tilt = (id, angle) => {
      const c = this.world.cups[id];
      if (!c || !props.glb[id]) { tilt(id, angle); return; }  // 不是装液体的杯子 (投喂的罐子): 照原来
      // 喝: 倾到液面刚过杯沿 (按杯里剩的: 满的倾一点, 快见底的倾得多), 流出来的进不进嘴由液体模型按杯沿和嘴的几何定
      // (见 liquid.js 的 _intoMouth); 喝够一口 (SIP) 或者洒到身上了就倾回杯沿以下. 倾的时候慢慢倾 (0.2 秒的时间常数), 送到嘴边才倾.
      // (以前一叫倾就从杯里直接扣一口算喝掉, 不看杯子在不在嘴边; 活动脚本给的固定 0.85 弧度、一帧倾到会洒到胸口)
      if (angle && this.r.layer.items[id]?.held) {
        if (c.ml < 0.5) this._empty(id);
        else { this.sipping[id] = { got: 0, spilled: 0 }; angle = this._sipAngle(id); }
      } else delete this.sipping[id];
      this.tiltGoal[id] = angle || 0;
      this.tilted[id] = !!angle;
    };
  }

  /** 每帧: 杯子慢慢倾到 tiltGoal (放平也是慢慢的). 要倾的时候等杯子送到嘴边 (姿势层摆到九成) 再倾. */
  _tiltStep(dt) {
    const atMouth = (this.r.poses?.W ?? 1) > 0.9;
    for (const id of Object.keys(this.sipping)) if (this.tiltGoal[id]) this.tiltGoal[id] = this._sipAngle(id);  // 液面随喝下去变低
    for (const [id, goal] of Object.entries(this.tiltGoal)) {
      const now = this.tiltNow[id] ?? 0, s = this.sipping[id];
      if (s && this.r.needs?.actualConsumption) {
        // Real consumption never tips on a timeout. This is the visible geometric
        // pouring-rim contact gate, not proof that the liquid's actual outlet enters the
        // mouth: the physical flow/direction test remains authoritative.
        const gap = this.lipGap?.[id];
        if (!atMouth || this.r.poses?.name !== 'sip' || !Number.isFinite(gap) || gap > 0.045) s.contact = false;
        else if (gap < 0.035) s.contact = true;
        if (s.contact) s.reached = true;
        let g = s.contact ? goal : 0, next;
        const prealign = !s.reached && atMouth && this.r.poses?.name === 'sip' && Number.isFinite(gap) && goal > 0 && s.target?.reachable;
        if (prealign) {
          // Existing low-level capacity estimate only; this is not an observed
          // amount or a drinking receipt. Below-critical prealignment can make
          // the visible rim reachable without opening the physical intake gate.
          const limit = Math.min(0.35, s.target.critical * 0.4);
          s.armSteady = this._sipArmSteady(id, s, dt);
          g = s.armSteady ? limit : Math.min(now, limit);
          next = g < now ? now + (g-now)*(1-Math.exp(-dt/0.08)) : Math.min(g, now+0.25*dt);
          s.phase = s.armSteady ? 'prealign' : 'await-stable';
          s.prealignLimit = limit;
        } else {
          s.armStableFor = 0; s.armSample = null; s.armSteady = false;
          s.phase = s.contact ? 'drinking' : s.reached ? 'lost-contact' : 'waiting';
          next = now + (g - now) * (1 - Math.exp(-dt / (s.contact ? 0.2 : 0.08)));
        }
        this.tiltNow[id] = prealign && next > now ? next : Math.abs(next - g) < 1e-3 ? g : next;
        this.tiltRaw(id, this.tiltNow[id]);
        if (!goal && !this.tiltNow[id]) { delete this.tiltGoal[id]; delete this.tiltNow[id]; }
        continue;
      }
      // 喝的时候等杯沿贴到嘴上 (她感觉得到) 再倾; 2 秒还没贴上也倾 (洒了就是洒了)
      const ready = !s || (this.lipGap?.[id] ?? 1) < 0.035 || (atMouth && (s.wait = (s.wait || 0) + dt) > 2);
      // 杯子到过嘴边又离开 (姿势退了、换了) 时还倾着: 马上放平到杯沿以下 (以前倾着跟手臂一起往下甩, 手腕一转杯子过了 72 度, 一帧倒光)
      if (s && atMouth && ready && this.r.poses?.name === 'sip') s.reached = true;
      const leaving = s?.reached && goal > 0 && (!atMouth || this.r.poses?.name !== 'sip');
      if (!leaving && goal > now && (!atMouth || !ready)) continue;
      const g = leaving ? Math.min(goal, Math.max(0.2, this._sipAngle(id) - 0.3)) : goal;
      const next = now + (g - now) * (1 - Math.exp(-dt / (leaving ? 0.08 : 0.2)));
      this.tiltNow[id] = Math.abs(next - g) < 1e-3 ? g : next;
      this.tiltRaw(id, this.tiltNow[id]);
      if (!goal && !this.tiltNow[id]) { delete this.tiltGoal[id]; delete this.tiltNow[id]; }
    }
  }

  /** Final wrist relative to shoulder, and wrist world rotation, must settle
   * before increasing the prealignment angle. Missing geometry fails closed. */
  _sipArmSteady(id, sip, dt) {
    const L = this.r.layer, side = L.items[id]?.held, hand = L.nodes?.[`${side}Hand`], shoulder = L.nodes?.[`${side}UpperArm`];
    if (!(dt > 0) || !hand || !shoulder || !['idle', 'seated'].includes(L.mode)) { sip.armStableFor = 0; sip.armSample = null; return false; }
    const h = hand.getWorldPosition(new THREE.Vector3()), a = shoulder.getWorldPosition(new THREE.Vector3());
    const rot = hand.getWorldQuaternion(new THREE.Quaternion()), p = [h.x-a.x,h.y-a.y,h.z-a.z], q = [rot.x,rot.y,rot.z,rot.w];
    const previous = sip.armSample; sip.armSample = { p, q };
    if (!previous || ![...p,...q].every(Number.isFinite)) { sip.armStableFor = 0; return false; }
    const speed = Math.hypot(...p.map((v,i)=>v-previous.p[i]))/dt;
    const product = Math.hypot(...q)*Math.hypot(...previous.q);
    const cosine = product > 0 ? Math.min(1,Math.abs(q.reduce((sum,v,i)=>sum+v*previous.q[i],0))/product) : 0;
    const angularSpeed = 2*Math.acos(cosine)/dt;
    sip.armStableFor = speed <= 0.05 && angularSpeed <= 0.5 ? (sip.armStableFor || 0)+dt : 0;
    return sip.armStableFor >= 0.2;
  }

  /** 这一口倾到多少 (弧度): 还没喝够、也没洒到身上时倾到液面刚过杯沿 (流出来), 否则倾回杯沿以下 (不流). */
  _sipAngle(id) {
    const c = this.world.cups[id], s = this.sipping[id];
    const target = sipTarget(c, capacityMl);
    if (s) s.target = target;
    if (!target.reachable) return 0;  // Empty or beyond the existing wrist budget; no forced last sip.
    return s && s.got < SIP && s.spilled < 2 ? target.angle : Math.max(0, Math.min(1.0, target.critical - 0.15));
  }

  /** Actual mode follows the visible geometric pouring edge; legacy keeps nearest rim. */
  _lip(pose, c, mouth) {
    if (this.r.needs?.actualConsumption) {
      const point = pourRimPoint(pose.p, pose.q, c.R, c.H, mouth);
      return point ? new THREE.Vector3(...point) : null;
    }
    const q = new THREE.Quaternion(...pose.q), m = new THREE.Vector3(...mouth);
    const top = new THREE.Vector3(...pose.p).add(new THREE.Vector3(0, c.H, 0).applyQuaternion(q));
    const axis = UP.clone().applyQuaternion(q), d = m.sub(top);
    d.addScaledVector(axis, -d.dot(axis));
    return top.addScaledVector(d.lengthSq() > 1e-12 ? d.normalize() : d, c.R);
  }

  /**
   * 送到嘴边喝 (姿势层的 sip) 时: 手腕目标 = 此刻手腕 + "控制杯沿 -> 嘴"偏差 (实际消费用几何最低沿，legacy用最近沿；一次最多 5 cm,
   * 和上一帧的目标各取一半), 杯沿真的贴到嘴上. 姿势层原来按头骨骼估的嘴加固定偏移摆手腕, 杯沿离真的嘴 7-10 cm, 倾过来全洒在身上.
   * 从此刻的手腕算, 再补上上一帧目标手没够到的那段 (防穿模把手臂往外推、IK 差一点, 一直差 4 cm 左右), 这段最多补 4 cm:
   * 不设上限地在上一帧的目标上累加时, 手够不着就越加越远, 小臂插进头里 (试过).
   * 姿势还没送到九成时不修 (手还在路上, 偏差不说明问题).
   */
  _reach(id, pose, mouth) {
    const P = this.r.poses, side = pose.hold, hand = this.r.layer.nodes[`${side}Hand`];
    if (P?.name !== 'sip' || !P.opts || !hand || (P.opts.side || 'right') !== side || (P.W ?? 0) < 0.9) return;
    const lip = this._lip(pose, this.world.cups[id], mouth);
    if (!lip) { (this.lipGap ??= {})[id] = Infinity; return; }
    const err = new THREE.Vector3(...mouth).sub(lip);
    (this.lipGap ??= {})[id] = err.length();
    const wrist = hand.getWorldPosition(new THREE.Vector3());
    const short = P.opts.reach ? P.opts.reach.clone().sub(wrist).clampLength(0, 0.04) : new THREE.Vector3();
    const want = wrist.add(err.clampLength(0, 0.05)).add(short);
    P.opts.reach = P.opts.reach ? P.opts.reach.clone().lerp(want, 0.5) : want;
  }

  /** 拿起来喝, 是空的: 倾了也没东西流出来 (她知道是空的). */
  _empty(id) {
    this.r.status?.(`${this.names[id] || '杯子'}是空的`);
    this.r.log?.(`小屋: 拿起${this.names[id] || '杯子'}喝, 是空的`);
    this._felt({ kind: 'empty_sip', cup: id });
  }

  /** 喝进嘴里的烫不烫 (嘴的门槛比手高, 见 liquid.js 的 heatEffect). */
  _mouthHeat(ml, temp) {
    const effect = heatEffect(temp, ml, 'mouth');
    if (effect.level !== 'wet') this.r.body?.stimulus?.({ part: '嘴', id: 'mouth', kind: 'heat', ml, temp, effect });
  }

  /** 每帧 (画完以后, 骨骼和手里的东西是这一帧最终的样子). */
  tick(dt) {
    const { layer, v, props } = this.r, poses = {};
    this._tiltStep(dt);
    for (const [id, cup] of Object.entries(this.cups)) {
      const node = props.node(id), item = layer.items[id];
      const held = item?.held || null;
      if (held !== (this.held[id] || null)) { this.world.cups[id].teleported(); this.held[id] = held; }  // 拿起、放下: 跳到手里 / 台面上
      // 交给刚体的杯子 (room-rigid.js): 拿着、或者还在动 (掉下来、被碰倒) 都算, 速度用刚体的
      const P = this.r.rigid?.pose?.(id) || null;
      if (!node?.visible || !(held || (P && !P.sleeping))) continue;
      const q = node.getWorldQuaternion(new THREE.Quaternion()).multiply(cup.restInv);
      // 正在喝 (倾着或者正在放平、送到嘴边的姿势还在一半以上): 杯沿溢出来的往嘴里去 (按姿势的意图, 不按估的嘴离杯沿多远:
      // 姿势里杯口中心离嘴 8-19 cm 不等). 喝完先放平再放下: 放平的那一下杯子还在嘴边 (姿势已经清了, 权重还没退下去)
      const drinking = ((this.tiltNow[id] ?? 0) > 0.05 || !!this.tilted[id]) && (this.r.poses?.W ?? 0) > 0.5
        && (this.r.poses?.name === 'sip' || this.r.poses?._last?.ik?.hand);
      poses[id] = { p: node.localToWorld(cup.bottom.clone()).toArray(), q: q.toArray(), hold: held, drinking, ...(P ? { physical: true, v: P.v } : {}) };
    }
    if (this.brewing) this._brew(dt);
    if (this.r.mover?.moving) this.groundCache?.clear();  // 在挪家具: 台面变了
    let caps = null;
    const hit = hitter(() => (caps ??= bodyCapsules(v)));
    // 嘴的位置一直给 (拿着杯子时): 进不进嘴只看几何, 不看是不是在"喝"
    const mouth = Object.values(poses).some((p) => p.hold) ? this._mouth() : null;
    const tiltedId = Object.keys(poses).find((k) => this.tilted[k]);
    if (mouth && tiltedId) {
      const lip = this._lip(poses[tiltedId], this.world.cups[tiltedId], mouth);
      this.mouthGap = lip ? +lip.distanceTo(new THREE.Vector3(...mouth)).toFixed(3) : null;  // 调试: 与实际控制器同一条可见几何杯沿
    }
    if (mouth) for (const [id, p] of Object.entries(poses)) if (p.hold) this._reach(id, p, mouth);
    const ev = this.world.step(dt, { poses, hit, ground: this.ground, mouth });
    for (const e of ev) {
      this._event(e);
      // 原始事件 (真值: 毫升、落点、碰到哪): 只给认知那边的感知门 (room-sight.js) 自己分档、自己判断她看没看见, 不能原样进她的认知
      try { this.onWorld?.(e); } catch (err) { console.warn(err); }
    }
    if (this.spill && performance.now() - this.spill.last > 1500) this._spillDone();
    this._draw();
    if (ev.length) this.dirty = true;
    if (this.dirty && performance.now() - this.lastSave > (this.consumption.state.pending.length ? 1000 : 10000)) this.save();
  }

  /** 出水: 出水口正下方的杯子接住 (接满了多的洒出来), 没有杯子就落到下面的台面上. */
  _brew(dt) {
    const s = this.r.props.flow;
    if (!s?.visible) return;
    if (this.r.needs?.actualConsumption && (!this.preparation ||
        (this.consumption.state.preparations.length && !this.save()))) return;
    const preparation = this.r.needs?.actualConsumption ? this.preparation : null;
    if (preparation?.blocked) return;
    // 出水口 = 那一股水流模型的顶 (水流模型往下伸进杯子里, 底不能当出水口)
    const b = new THREE.Box3().setFromObject(s), at = [(b.min.x + b.max.x) / 2, b.max.y, (b.min.z + b.max.z) / 2];
    const want = BREW * dt;
    for (const [id, cup] of Object.entries(this.cups)) {
      if (preparation && preparation.cupId !== id) continue;
      const node = this.r.props.node(id), c = this.world.cups[id];
      if (!node?.visible || this.r.layer.items[id]?.held) continue;
      const p = node.localToWorld(cup.bottom.clone());
      if (Math.hypot(p.x - at[0], p.z - at[2]) > c.R + 0.015 || p.y + c.H > at[1] + 0.02) continue;
      // Valve-level execution guard, not a high-level observation: prevent a
      // stale empty-cup decision from adding to a changed cup before first fill.
      if (preparation && !preparation.preparedMl && c.ml > 1e-6) {
        preparation.blocked = '准备前杯中状态已变化，已停止出水；先重新观察';
        this.r.props.stream(null);
        return;
      }
      const put = this.world.fill(id, want, BREW_TEMP, 'coffee');
      if (put > 0 && this.preparation) {
        const prep = this.preparation;
        if (!prep.preparedMl) this.consumption.state.preparations.push({ id: prep.id, stock: prep.stock, preparedMl: put });
        prep.preparedMl += put;
        if (this.r.sight?.now?.()?.seen && this._visibleCup(id)) {
          const now = this._evidenceNow();
          this.thermalEvidence[id] = { at: now, temperature: 'hot', inferred: true };
        }
        // A failed durable save leaves the same preparation pending and blocks further brew.
        if (!prep.committed) prep.committed = this.save();
      }
      if (want - put > 1e-6) {
        this.world.pour([p.x + c.R, p.y + c.H, p.z], want - put, BREW_TEMP, 'coffee', [0, -0.1, 0]);
        this._spillNote(want - put, 'machine');  // 接满了还在出水
      }
      this.dirty = true;
      return;
    }
    if (preparation) {
      preparation.blocked = '空杯没有保持在出水口下方，已停止出水';
      this.r.props.stream(null);
      return;
    }
    this.world.pour(at, want, BREW_TEMP, 'coffee');
    this._spillNote(want, 'machine');  // 出水口下面没有杯子
    this.dirty = true;
  }

  /** Explicit preparation boundary; the stock receipt is emitted by actual fill, never this intent. */
  beginPreparation(stock = 'beans', cupId) {
    if (stock !== 'beans' || this.preparation || !this.r.needs?.actualConsumption ||
        !this.r.needs.canFeed('beans') || !(this.r.needs.stock.beans > 0) || this.consumption.state.preparations.length)
      throw new Error('咖啡准备不可用：请检查库存或未结算的准备记录');
    if (!this.world.cups[cupId] || this.inspect(cupId).level !== 'empty')
      throw new Error('没有观察到空杯，不开始加液；先确认杯中是否有剩余');
    const id = crypto.randomUUID();
    this.preparation = { id, stock, cupId, preparedMl: 0, committed: false }; return id;
  }
  endPreparation(id) {
    const prep = this.preparation;
    if (!prep || prep.id !== id) return { ok: false, id, preparedMl: 0, reason: '准备记录不匹配' };
    const saved = this.save(); this.preparation = null;
    return { ok: saved && prep.preparedMl > 0, id, preparedMl: prep.preparedMl,
      ...(!saved ? { reason: '准备结果尚未保存，暂不重复冲泡' } : !prep.preparedMl ? { reason: prep.blocked || '没有杯子实际接到咖啡' } : {}) };
  }
  _visibleCup(id) {
    const o = this.r.sight?.now?.();
    return !!o && [...(o.hand || []), ...Object.values(o.seen || {}).flat()].includes(id) && !!this.r.props.node(id)?.visible;
  }
  _restoreCoffeeEvidence(saved) {
    this.evidenceClock = Date.now();
    const restored = restoreCoffeeEvidence(saved, this.evidenceClock);
    this.thermalEvidence = restored.thermal; this.cupEvidence = restored.levels;
  }
  _evidenceNow() {
    const now = Date.now();
    if (Number.isFinite(this.evidenceClock) && now + 1000 < this.evidenceClock) {
      // Clock rollback cannot erase a hot warning or make a cooldown expire.
      this._restoreCoffeeEvidence({ version: 1, at: this.evidenceClock, thermal: this.thermalEvidence, levels: this.cupEvidence });
      this.dirty = true;
    }
    this.evidenceClock = now;
    return now;
  }
  _rememberLevel(id, level, now = this._evidenceNow()) {
    if (!['empty', 'low', 'half', 'full'].includes(level)) return;
    this.cupEvidence ??= {};
    const old = this.cupEvidence[id];
    if (!old || old.level !== level || now - old.at > 1000) {
      this.cupEvidence[id] = { at: now, level }; this.dirty = true;
    }
  }
  /** Only observed cup level and recent taste / observed brewing evidence. Never exact temperature. */
  inspect(id) {
    const now = this._evidenceNow(), visible = this._visibleCup(id) && !!this.world.cups[id];
    // Cup-mouth visibility is already gated by perceive(); no truth-based level memory.
    const level = visible ? this.perceive({ take: false }).cups[id] ?? 'unknown' : 'unknown';
    if (visible) this._rememberLevel(id, level, now);
    const old = this.cupEvidence?.[id];
    const remembered = old && now >= old.at && now - old.at <= COFFEE_EVIDENCE.levelMs ? { level: old.level } : null;
    const thermal = coffeeThermalView(this.thermalEvidence[id], now);
    if (!visible) return { visible: false, level, remembered, ...thermal, known: false, category: 'unknown', temperature: 'unknown', inferred: false,
      text: thermal.pastHot ? thermal.text : '现在看不见杯子，还不知道冷热', reason: '杯子不在当前观察中；只保留过去实际观察的记录' };
    return { visible: true, level, remembered, ...thermal };
  }

  /** 这一阵洒的记一笔 (从哪来: 杯子 id 或 'machine'). */
  _spillNote(ml, from) {
    this.spill ??= { ml: 0, cup: from, where: {}, at: [], touched: false, hot: false, last: 0 };
    this.spill.ml += ml;
    this.spill.last = performance.now();
  }

  /** 她感知到的一件液体的事 (固定词表), 给 perceive; 设了 onEvent (认知那边) 也马上告诉它. */
  _felt(ev) {
    const e = { ...ev, t: +(performance.now() / 1000).toFixed(1) };
    this.felt.push(e);
    if (this.felt.length > 30) this.felt.shift();
    try { this.onEvent?.(e); } catch (err) { console.warn(err); }
  }

  // -- 给别的模块 (可供性 room-afford.js 等) 的公开接口: 只给她感知得到的、只做物理上做得到的 ------------------------------
  /** 她看得见这一点吗 (规则同 _sees: 离头 3 m 内、脸朝的方向左右 1.2 弧度内, 不算遮挡). */
  sees(at) { return this._sees(at); }

  /**
   * 她此刻看得见的一摊摊 (不是真值, 只有粗的): [{id, at: 粗位置 (取到 0.1 m), surface: 'floor' | 台面 id, kind: 'coffee' | 'water',
   * stain: 干了留下的印子, size: 'small' | 'big'}]. id 只是擦的时候用的把手; 毫升、温度不给.
   */
  seenPuddles() {
    return this.world.puddles.filter((p) => this._sees(p.at)).map((p) => ({ id: p.id, at: p.at.map((x) => Math.round(x * 10) / 10),
      surface: p.surface, kind: p.kind === 'water' ? 'water' : 'coffee', stain: !!p.stain, size: p.r > 0.06 ? 'big' : 'small' }));
  }

  /** 擦掉一摊: 她得站在旁边 (根离这摊水平 0.9 m 以内) 才擦得到, 不看她想不想擦. 擦到了返回 true. */
  wipe(id) {
    const p = this.world.puddles.find((x) => x.id === id), c = this.r.layer.char;
    if (!p || Math.hypot(p.at[0] - c.x, p.at[2] - c.z) > 0.9) return false;
    this.world.wipe(id);
    this.dirty = true;
    return true;
  }

  /** 从 at ([x, y, z]) 漏出 ml 毫升 (浇花浇多了盆底渗水这种), 往下落到台面或地上成一摊; 记账照常 (算洒出来的). */
  leak(at, ml, { temp = this.world.env, kind = 'water' } = {}) {
    this.world.pour(at, ml, temp, kind, [0, -0.05, 0]);
    this.dirty = true;
  }

  /** 她看得见这一点吗: 离头 SEE_DIST 以内、在脸朝的方向左右 SEE_HALF 以内 (不算遮挡). */
  _sees(at) {
    const head = this.r.layer.nodes.head;
    if (!head) return false;
    const p = head.getWorldPosition(new THREE.Vector3()), q = head.getWorldQuaternion(new THREE.Quaternion());
    const fwd = new THREE.Vector3(0, 0, this.r.poses?.v0 ? -1 : 1).applyQuaternion(q).setY(0).normalize();
    const d = new THREE.Vector3(at[0] - p.x, 0, at[2] - p.z);
    return d.length() < SEE_DIST && (d.length() < 0.4 || fwd.angleTo(d.normalize()) < SEE_HALF);
  }

  /**
   * 她这一刻感知得到的 (给认知; 不是真值): 看得到杯口的杯子 (手里的, 和她站的那一处台面上的; 杯子歪过 60 度看不到杯口)
   * 液面的粗档 (LEVELS: empty / low / half / full); 自上次取走以后感知到的液体事件 (固定词表):
   *   {kind: 'spill', amount: drops | little | some | stream | splash (AMOUNTS), where: 'floor' | 台面 id | 'machine', from: 杯子 id | 'machine', touched, seen}
   *     洒了: 只在碰到她 (touched) 或者落点在她看得见的地方 (seen) 时才有; 都不是她就不知道.
   *   {kind: 'empty_sip', cup}  喝的时候喝不到 (杯子是空的).
   * 身上的感觉 (哪里烫、哪里湿) 在体感那边 (room-body.js 的 feelings()).
   * 钩子: onEvent(e) 上面这些事 (已经过了感知门) 一有就调; onWorld(e) 液体世界的原始事件 (真值, 见 liquid.js 的 step:
   * spill {cup, ml, at} / land {puddle, surface, ml, at} / contact {part, ml, temp, effect} / drink {cup, ml, temp}),
   * 只给认知那边的感知门自己分档用, 不能原样进她的认知.
   */
  perceive({ take = true } = {}) {
    const o = this.r.sight?.now?.();
    const cups = {};
    if (o) {
      for (const id of [...(o.hand || []), ...Object.values(o.seen || {}).flat()]) {
        const c = this.world.cups[id], node = this.r.props.node(id), cup = this.cups[id];
        if (!c || !node?.visible || !cup) continue;
        const axis = UP.clone().applyQuaternion(node.getWorldQuaternion(new THREE.Quaternion()).multiply(cup.restInv));
        if (axis.y < 0.5) continue;  // 歪得看不到杯口
        cups[id] = observedCupLevel(c, this.r.needs?.actualConsumption === true);
        if (this.r.needs?.actualConsumption) this._rememberLevel(id, cups[id]);
      }
    }
    const events = take ? this.felt.splice(0) : this.felt.slice();
    return { cups, events };
  }

  /**
   * 嘴在哪 (世界坐标): 头骨骼上的一个固定点, 第一次用时量好. 优先按张嘴表情 (aa) 动到的顶点的中心 (模型自己的嘴), 没有就按两眼往下、
   * 往前推, 再没有按头骨骼估. (以前按头骨骼往前 2 cm、往下 6 cm: silvercat 上比真的嘴低 5.7 cm、靠后 6.5 cm)
   */
  _mouth() {
    const head = this.r.layer.nodes.head;
    if (!head) return null;
    if (!this.mouthLocal) {
      const m = this._measureMouth(head);
      this.mouthFrom = m.from;
      this.mouthLocal = head.worldToLocal(m.at);
    }
    return head.localToWorld(this.mouthLocal.clone()).toArray();
  }

  _measureMouth(head) {
    const vrm = this.r.layer.vrm;
    vrm.scene.updateMatrixWorld(true);
    // 张嘴表情 (aa) 绑定的 morph: 动得最多的那些顶点 (每个网格里位移到最大值三成以上的) 的中心
    const sum = new THREE.Vector3(), p = new THREE.Vector3(), d = new THREE.Vector3();
    let n = 0;
    for (const bind of vrm.expressionManager?.getExpression('aa')?._binds || []) {
      for (const mesh of bind.primitives || []) {
        const pos = mesh.geometry?.attributes.position, mt = mesh.geometry?.morphAttributes.position?.[bind.index];
        if (!pos || !mt || mt.count !== pos.count) continue;
        let max = 0;
        for (let i = 0; i < mt.count; i++) max = Math.max(max, d.fromBufferAttribute(mt, i).length());
        if (max < 0.003) continue;
        for (let i = 0; i < mt.count; i++) {
          if (d.fromBufferAttribute(mt, i).length() < max * 0.3) continue;
          (mesh.getVertexPosition ? mesh.getVertexPosition(i, p) : p.fromBufferAttribute(pos, i));
          sum.add(mesh.localToWorld(p));
          n++;
        }
      }
    }
    if (n >= 10) return { at: sum.divideScalar(n), from: 'aa' };
    const hq = head.getWorldQuaternion(new THREE.Quaternion());
    const fwd = new THREE.Vector3(0, 0, this.r.poses?.v0 ? -1 : 1).applyQuaternion(hq);
    const eyes = ['leftEye', 'rightEye'].map((k) => vrm.humanoid?.getRawBoneNode(k)?.getWorldPosition(new THREE.Vector3())).filter(Boolean);
    if (eyes.length === 2) {  // 两眼中点往下 0.66 个眼距、往前 0.5 个眼距 (silvercat 量的)
      const s = eyes[0].distanceTo(eyes[1]);
      return { at: eyes[0].add(eyes[1]).multiplyScalar(0.5).addScaledVector(UP, -0.66 * s).addScaledVector(fwd, 0.5 * s), from: 'eyes' };
    }
    return { at: head.getWorldPosition(new THREE.Vector3()).addScaledVector(fwd, 0.085), from: 'head' };
  }

  _event(e) {
    if (e.type === 'contact') {
      const part = PART_NAMES[e.part] || e.part;
      this.r.body?.stimulus?.({ part, id: e.part, kind: 'heat', ml: e.ml, temp: e.temp, effect: e.effect });
      if (this.spill) this.spill.touched = true;
    } else if (e.type === 'drink') {
      const receipt = this.consumption.drink(e, this.world.cups[e.cup]?.kind, this.r.scene.sceneId || 'petal-oak-life-v1', Date.now() / 1000);
      if (!receipt) return;
      const now = this._evidenceNow();
      this.thermalEvidence[e.cup] = tastedCoffee(this.thermalEvidence[e.cup], e.temp < 10 ? 'cold' : e.temp < 30 ? 'cool' : e.temp < 62 ? 'warm' : 'hot', now);
      this._mouthHeat(e.ml, e.temp);  // 倾着时从杯沿流进嘴里的 (体感那边同一处 1.5 秒内只记一次)
      if (this.sipping[e.cup]) this.sipping[e.cup].got += e.ml;
      this.dirty = true;
    } else if (e.type === 'spill') {
      if (this.sipping[e.cup]) this.sipping[e.cup].spilled += e.ml;  // 倾着喝的时候洒到身上了: 她会停下 (倾回杯沿以下)
      this._spillNote(e.ml, e.cup);
      // 调试 (自检看): 每次洒的时候在干什么
      (this.spillDbg ??= []).push([+(performance.now() / 1000).toFixed(1), +e.ml.toFixed(2), this.r.poses?.name || this.r.layer.mode,
        +(this.r.poses?.W ?? 0).toFixed(2), +(this.tiltNow[e.cup] ?? 0).toFixed(2)]);
      if (this.spillDbg.length > 60) this.spillDbg.shift();
    } else if (e.type === 'land' && this.spill) {
      this.spill.where[e.surface] = (this.spill.where[e.surface] || 0) + e.ml;
      if (this.spill.at.length < 8) this.spill.at.push(e.at);
    }
  }

  /** 洒了一阵: 状态栏写一句 (给你看), 调试日志记一笔; 她碰到了或者看得见落点, 才算她知道 (给 perceive). 都不写毫升. */
  _spillDone() {
    const s = this.spill;
    this.spill = null;
    if (s.ml < 0.3) return;
    const how = { drops: '洒了几滴', little: '洒了一点', some: '洒了一些', stream: '洒了一大股', splash: '泼出来一大片' }[bin(AMOUNTS, s.ml)];
    const top = Object.entries(s.where).sort((a, b) => b[1] - a[1])[0]?.[0];
    const where = !top ? '' : top === 'floor' ? ', 洒在地上' : `, 洒在${this.r.layer.targets[top]?.name || '台面'}上`;
    const what = s.cup === 'machine' ? '咖啡' : KINDS[this.world.cups[s.cup]?.kind]?.name || '水';
    this.r.status?.(`${what}${how}${where}`);
    this.r.log?.(`小屋: ${s.cup === 'machine' ? '咖啡机出水' : `端着${this.names[s.cup] || '杯子'}`}, ${what}${how}${where}`);
    const seen = s.at.some((p) => this._sees(p));
    if (s.touched || seen) this._felt({ kind: 'spill', amount: bin(AMOUNTS, s.ml), where: top || 'floor', from: s.cup, touched: s.touched, seen });
  }

  /** 杯里的液面 (跟着晃动倾斜)、一摊摊的水渍和干了的印子. 只画状态里真有的. */
  _draw() {
    const props = this.r.props;
    for (const [id, cup] of Object.entries(this.cups)) {
      const c = this.world.cups[id], node = props.node(id), d = cup.disc;
      if (!node?.visible || c.ml < 0.5) { d.visible = false; continue; }
      const h = c.ml / 1e6 / (Math.PI * c.R * c.R);
      const q = node.getWorldQuaternion(new THREE.Quaternion()).multiply(cup.restInv);
      const center = node.localToWorld(cup.bottom.clone()).add(new THREE.Vector3(0, Math.min(h, c.H - 0.002), 0).applyQuaternion(q));
      const n = new THREE.Vector3(-c.A[0] / c.R, 1, -c.A[1] / c.R).normalize().applyQuaternion(q);
      d.position.copy(center);
      d.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), n);
      d.material.color.set(KINDS[c.kind]?.color || '#888');
      d.visible = true;
    }
    if (this.sheets) { this.sheets.update(this.world.puddles); return; }
    const seen = new Set();
    for (const p of this.world.puddles) {
      seen.add(p.id);
      let m = this.puddleMeshes.get(p.id);
      if (!m) {
        m = new THREE.Mesh(new THREE.CircleGeometry(1, 32), new THREE.MeshStandardMaterial({ transparent: true, roughness: 0.1, depthWrite: false }));
        m.rotation.x = -Math.PI / 2;
        m.renderOrder = 2;
        m.name = `puddle_${p.id}`;
        this.group.add(m);
        this.puddleMeshes.set(p.id, m);
      }
      const r = p.ml > 0 ? p.r : Math.max(p.r, 0.02);
      m.position.set(p.at[0], p.at[1] + 0.0015, p.at[2]);
      m.scale.setScalar(Math.max(0.01, r));
      m.material.color.set(KINDS[p.kind]?.color || '#888');
      m.material.opacity = p.ml > 0 ? 0.8 : 0.22;  // 干了的印子淡一些
    }
    for (const [id, m] of this.puddleMeshes) {
      if (seen.has(id)) continue;
      this.group.remove(m);
      m.geometry.dispose();
      m.material.dispose();
      this.puddleMeshes.delete(id);
    }
  }

  save() {
    this.lastSave = performance.now();
    const ok = this.consumption.flush((consumption) => {
      if (!this.storage?.setItem) return false;
      try { const now = this._evidenceNow(); this.storage.setItem(KEY, JSON.stringify({ at: now, world: this.world.toJSON(), consumption,
        coffeeEvidence: { version: COFFEE_EVIDENCE.version, at: now, thermal: this.thermalEvidence, levels: this.cupEvidence || {} } })); return true; }
      catch { return false; }
    }, this.r.needs);
    this.dirty = !ok; return ok;
  }

  /** 自检、调试看: 每只杯子装了多少多烫、每一摊 (真值, 不进她的提示词). */
  snapshot() {
    const w = this.world;
    return {
      cups: Object.fromEntries(Object.entries(w.cups).map(([id, c]) => [id, { ml: +c.ml.toFixed(1), temp: +c.temp.toFixed(1), kind: c.kind, R: +c.R.toFixed(3), H: +c.H.toFixed(3) }])),
      puddles: w.puddles.map((p) => ({ id: p.id, surface: p.surface, at: p.at.map((x) => +x.toFixed(3)), r: +p.r.toFixed(3), ml: +p.ml.toFixed(2),
        temp: +p.temp.toFixed(1), stain: p.stain })),
      body: JSON.parse(JSON.stringify(w.body)), ledger: { ...w.ledger }, balanceError: +w.balance().error.toExponential(1),
      spillDbg: (this.spillDbg || []).slice(-30),  // [秒, 毫升, 姿势或行动, 姿势权重, 倾了多少弧度]
      mouthFrom: this.mouthFrom || null,  // 嘴的位置按什么量的: aa 张嘴表情 / eyes 两眼 / head 头骨骼
    };
  }
}
