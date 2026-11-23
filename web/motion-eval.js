// 执行评估的场景 (自动调优用): 在页面里跑, tools/motion_eval.mjs 用无界面浏览器调它, 也可以在控制台
//   (await import('/static/motion-eval.js')).runEval({ scenarios: ['walk'], seeds: [1] })
//
// 结果可复现: 评估期间 performance.now 换成模拟时钟 (每帧 1/60 秒), Math.random 换成按种子的随机数, 每个场景开始前
// 把角色、物品、脚的锁定、平滑层、落地偏移都复位, 从同一段待机的同一帧起步. 不渲染 (只算骨骼), HAST-SEC 先卸下
// (可商用的那条路; 它在不说话时本来也让给行动层). 每个场景给出: 完成没有、执行评估 (含痛觉各项, 见 pain.js)、
// 像不像真人 (见 motion-score.js).
//
// 走路、坐下、拿放用哪些目标由场景 JSON 推出来 (scene-tasks.js): 换场景不用改这里. 默认的一组 (调优用, defaultScenarios)
// 是每类一个; 体检 (tools/motion_checkup.py) 用全套 (sweepScenarios: 走到每个目标、坐每个座位、每件东西放到每个台面),
// 名字带目标, 比如 walk:window、sit:sofa、pickput:ball>coffee_table. checks: true 时另外给出场景和模型的静态检查.
import { CONTACT } from './contact.js';
import { TUNE } from './locomotion.js';
import { applyParams, readParams } from './motion-params.js';
import { DIM, concat, dbFeatures, mean, nnDistances, pairFeatures, poseDirs, FEATURE_BONES } from './motion-score.js';
import { findPath, pathLength } from './nav.js';
import { bodyCapsules, grounder, hitter } from './physics/body-hit.js';
import { LIQUID, LiquidWorld } from './physics/liquid.js';
import { LIMITS } from './pose-filter.js';
import { defaultScenarios, parseScenario, sceneChecks, scenePicks, sceneTasks, sweepScenarios } from './scene-tasks.js';
import { seededRandom } from './vendor/soullink3d/dist/vendor/soullink/utils/seededRandom.js';

const FPS = 60, DT = 1 / FPS;
const SAMPLE_EVERY = 6;  // 每 6 帧取一对姿态 (相隔 2 帧 = 1/30 秒) 算像不像真人
// 每个场景都从模拟时钟的这个时刻开始: 头部表演引擎等按绝对时间走的逻辑不受这个页面之前跑过什么影响
// (以前从真实时间接着走, 同一个页面连续跑几组时, 手势、说话场景的像真人分会差 0.1 左右; 调优器就是这样连续跑的)
const EPOCH = 1e8;
const TARGETS = { tune: TUNE, filter: LIMITS, contact: CONTACT };

function mulberry32(seed) {
  let a = seed | 0;
  return () => {
    a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// -- 参照集 (像不像真人) --------------------------------------------------------------------
let REF = null;
/** 各个动捕库按预算取样; 每 5 段留出 1 段不进参照, 留出段的平均最近邻距离当"真人水平". */
function reference(loco) {
  if (REF) return REF;
  const parts = [], held = [];
  for (const [db, budget] of [[loco.dbs.neutral, 3000], [loco.idleDB, 2500], [loco.acts, 1500], [loco.gestDB, 1000]]) {
    if (!db) continue;
    const stride = Math.max(1, Math.ceil(db.F / budget));
    parts.push(dbFeatures(db, { stride, keep: (ci) => ci % 5 !== 0 }));
    held.push(dbFeatures(db, { stride: stride * 4, keep: (ci) => ci % 5 === 0 }));
  }
  const ref = concat(parts), heldOut = concat(held);
  const base = mean(nnDistances(ref, heldOut));
  REF = { ref, base, frames: ref.length / DIM, held: heldOut.length / DIM };
  return REF;
}

// -- 场景 -------------------------------------------------------------------------------
function ctxFor(state) {
  const vrm = state.vrm, loco = vrm.loco, kin = vrm.kin, h = vrm.vrm.humanoid;
  const clock = { t: 0 };
  const ctx = { state, vrm, loco, kin, clock, frame: 0, pairs: [], prev: null, sample: true };
  const bones = [...new Set(FEATURE_BONES.flat())];
  const dirsNow = () => {
    const pos = {};
    for (const n of bones) { const b = h.getNormalizedBoneNode(n); if (b) pos[n] = b.getWorldPosition(b.position.clone()).toArray(); }
    const hips = h.getNormalizedBoneNode('hips');
    return { dirs: poseDirs(pos, loco.char.yaw), hipsY: hips.getWorldPosition(hips.position.clone()).y / loco.hipHeight };
  };
  // 风格 (2026-10-25 用户: "太多动了, 姿势也不够少女和优雅"): 每 3 帧量一次.
  //   act: 上半身 (脊柱、胸、颈、头、两臂、两手) 每秒转过的角度之和 (度/秒), 动得多不多;
  //   站着待机时: stance 两脚横向间距 / 两髋关节间距, elbowOut 手肘离胸口中线的横向距离 (厘米), head 头每秒转过的角度;
  //   走路时: armSwing 两上臂每秒转过的角度, stepWidth 两脚横向间距 (厘米).
  const STYLE_BONES = ['spine', 'chest', 'neck', 'head', 'leftUpperArm', 'rightUpperArm', 'leftLowerArm', 'rightLowerArm', 'leftHand', 'rightHand'];
  const styleSample = () => {
    const S = ctx.style, q = {}, wq = (n) => { const b = h.getNormalizedBoneNode(n); return b ? b.getWorldQuaternion(b.quaternion.clone()) : null; };
    for (const n of STYLE_BONES) q[n] = wq(n);
    const W = (n) => { const b = h.getNormalizedBoneNode(n); return b.getWorldPosition(b.position.clone()); };
    if (S.prevQ) {
      const ang = (n) => (q[n] && S.prevQ[n] ? 2 * Math.acos(Math.min(1, Math.abs(q[n].dot(S.prevQ[n])))) * 180 / Math.PI * (FPS / 3) : 0);
      const act = STYLE_BONES.reduce((s, n) => s + ang(n), 0);
      S.act.push(act);
      const fwd = [Math.sin(loco.char.yaw), Math.cos(loco.char.yaw)], left = [fwd[1], -fwd[0]];
      const lat = (a, b) => Math.abs((a.x - b.x) * left[0] + (a.z - b.z) * left[1]);
      if (loco.mode === 'idle' && !loco.action && !loco.seated && loco.idle) {
        S.stance.push(lat(W('leftFoot'), W('rightFoot')) / lat(W('leftUpperLeg'), W('rightUpperLeg')));
        const ch = W('chest');
        S.elbowOut.push((lat(W('leftLowerArm'), ch) + lat(W('rightLowerArm'), ch)) / 2 * 100);
        S.head.push(ang('head'));
      } else if (loco.mode === 'go' && !loco.action) {
        S.armSwing.push((ang('leftUpperArm') + ang('rightUpperArm')) / 2);
        S.stepWidth.push(lat(W('leftFoot'), W('rightFoot')) * 100);
      }
    }
    S.prevQ = q;
  };
  ctx.resetStyle = () => { ctx.style = { prevQ: null, act: [], stance: [], elbowOut: [], head: [], armSwing: [], stepWidth: [] }; };
  ctx.styleSummary = () => Object.fromEntries(Object.entries(ctx.style).filter(([k]) => k !== 'prevQ')
    .map(([k, v]) => [k, v.length ? +mean(v).toFixed(k === 'stance' ? 3 : 1) : null]));
  ctx.resetStyle();
  /** 推进 n 帧 (模拟时钟一起走), 顺带取样. */
  ctx.step = (n = 1) => {
    for (let i = 0; i < n; i++) {
      clock.t += 1000 * DT;
      state.step(DT, DT);
      ctx.onFrame?.();
      ctx.frame++;
      if (!ctx.sample) continue;
      if (ctx.frame % 3 === 0) styleSample();
      const k = ctx.frame % SAMPLE_EVERY;
      if (k === 0) ctx.prev = dirsNow();
      else if (k === 2 && ctx.prev) { const cur = dirsNow(); ctx.pairs.push({ ...cur, prev: ctx.prev.dirs, dt: 2 * DT }); ctx.prev = null; }
    }
  };
  /** 推进直到 cond() 为真 (最多 secs 秒), 返回是否等到了. */
  ctx.until = (cond, secs) => {
    for (let i = 0; i < secs * FPS; i++) { if (cond()) return true; ctx.step(1); }
    return cond();
  };
  /** 执行一个行动层动作 (走到/坐下/站起/拿/放), 等它做完, 返回失败原因 (成功为 null). */
  ctx.act = (a, secs = 20) => {
    let res;
    loco.run({ side: null, to: null, amp: 1, spd: 1, at: 0, ...a }, (r) => { res = r ?? null; });
    if (!ctx.until(() => res !== undefined, secs)) return `超时: ${a.do} ${a.to || ''}`;
    return res;
  };
  ctx.idleAgain = (secs = 6) => ctx.until(() => loco.mode === 'idle' && !loco.action && !!loco.idle, secs);
  return ctx;
}

/** 复位: 角色回出生点, 物品回原处, 各层状态清零, 从待机库同一段的同一帧开始站稳. */
async function reset(ctx) {
  const { loco, vrm, kin, state } = ctx;
  ctx.onFrame = null;
  await loco.setStyle('neutral');
  // 各层放回出厂状态 (还没跑过一帧的样子, 见 plain-state.js), 弹簧骨 (头发、裙子) 放回初始姿态: 每个场景从同一个状态开始,
  // 和这一页前面跑过什么无关. 以前只清下面这些字段, 同一个场景连跑两次结果就不一样 (begin 那一刻行动层有 80 多处不同),
  // 评测台开几个标签页、tools/motion_eval.mjs --serve 一个标签页连跑几个任务都不能逐位复现
  loco.restoreFactory?.();
  vrm.contact?.restoreFactory?.();
  vrm.filter?.restoreFactory?.();
  vrm.hands?.restoreFactory?.();
  kin.restoreFactory?.();
  state.motion?.restoreFactory?.();
  ctx.clock.t = EPOCH;
  // 面部引擎的情绪状态控制器自带随机数 (seededRandom(9137), 整页只建一次, reset 不重置): 头部的情绪漂移 (下次漂移的时刻、目标)
  // 跟这一页前面抽过几次有关. 换一个新的 (和刚载入时一样)
  if (vrm.engine?.controller) vrm.engine.controller.random = seededRandom(9137);
  vrm.engine?.reset();
  state.performer?.reset();
  state.motion?.stopAll(EPOCH / 1000);
  for (const k of ['rot', 'vel', 'pos', 'posVel']) loco.inert[k]?.fill(0);
  if (vrm.contact) Object.assign(vrm.contact, { spreadDeg: { left: 0, right: 0 }, swivel: { left: 0, right: 0 }, grip: { left: 0, right: 0 } });
  vrm.hands?.reset();
  vrm.arbiter?.clear();
  kin._ang = {};
  kin.prevAnkle = {};
  const sc = loco.scene, [sx, sz] = sc.spawn.pos, yaw = sc.spawn.yaw || 0;
  Object.assign(loco, { action: null, seated: null, reaching: null, queue: [], path: null, goal: null, mode: 'idle', gestureReq: null,
    pending: [], speaking: false, hipsExtraY: 0, seatAmount: 0, groundOff: 0, hipDrop: 0, nextTalk: 0, _lastAction: null,
    gaitW: 0, searchTimer: 0, carryW: { left: 0, right: 0 }, lapW: { left: 0, right: 0 }, _legSep: 0, carryCare: null, _carryOff: null, _carryDrift: null, _carryYaw: null, handSpeed: null });
  loco.char = { x: sx, z: sz, yaw };
  loco.sim = { x: sx, z: sz, vx: 0, vz: 0, ax: 0, az: 0 };
  loco.simRot = { yaw, w: 0 };
  loco.desired = { vx: 0, vz: 0, yaw };
  loco.feet = { left: { lock: null, w: 0, was: false }, right: { lock: null, w: 0, was: false } };
  loco.look = { target: null, until: 0, yaw: 0, pitch: 0, gaze: false };
  loco.pointing = { side: null, target: null, until: 0, w: 0 };
  loco.offer = { side: null, until: 0, w: 0 };
  loco.balanceOff?.set(0, 0, 0);
  for (const o of sc.objects) {
    const it = loco.items[o.id];
    if (!it || !o.item) continue;
    Object.assign(it, { held: null, on: o.item.on, origin: null, physical: false, target: null, dropped: false });
    it.mesh.position.fromArray(o.item.pos);
    it.mesh.rotation.set(0, 0, 0);
  }
  vrm.filter?.reset();
  const ci = loco.idleDB.clips.findIndex((c) => c.name === 'idle_02');
  const c = loco.idleDB.clips[ci >= 0 ? ci : 0];
  loco.idle = null;
  loco._idleSwitch(ci >= 0 ? ci : 0, c.start + 300, 'idle', ctx.clock.t / 1000, 0.01);
  loco.idle.nextAction = loco.idle.nextSwitch = ctx.clock.t / 1000 + 600;  // 场景自己决定什么时候插小动作
  // 弹簧骨 (头发、裙子) 按父骨骼此刻的世界位置重新起算: 先把根摆到出生点、标准化骨骼 (已经放回出厂姿态) 抄到原始骨骼
  // (头发、裙子挂在原始骨骼上, 要 humanoid.update 才跟上), 再复位. 以前在根还停在上一个场景结束的地方、原始骨骼还是上一个场景
  // 最后的姿势时复位, 头发、裙子的尾端从不同的地方起算, 站稳那 2 秒每次都不一样
  loco._applyRoot();
  vrm.vrm?.humanoid?.update();
  vrm.vrm?.scene.updateMatrixWorld(true);
  vrm.vrm?.springBoneManager?.reset();
  ctx.sample = false;
  ctx.step(FPS * 2);  // 站稳
  // 取样从头开始: 帧计数清零 (取样的相位不随前一个场景的长度变), 上一帧的姿态也清掉 (不然新场景第一对的速度是跨场景算的,
  // 一个离群点就让像真人分差 0.1 左右; 以前同一次评估里排在后面的场景都受影响)
  ctx.frame = 0;
  ctx.prev = null;
  ctx.sample = true;
  ctx.pairs = [];
  ctx.resetStyle();
}

// -- 各个场景 -----------------------------------------------------------------------------
// 每个场景 (ctx, 目标): 目标来自名字冒号后面 (walk:window), 没写就用 scenePicks 挑的
export const SCENARIOS = {
  /** 站着待机 14 秒: 3 秒后插一个小动作, 8 秒后换一段待机 (哪一个由种子决定). */
  idle(ctx) {
    const now = ctx.clock.t / 1000;
    ctx.loco.idle.nextAction = now + 3;
    ctx.loco.idle.nextSwitch = now + 8;
    ctx.step(FPS * 14);
    return null;
  },
  /** 站着待机 60 秒, 小动作和换段按平时的节奏 (量"动得多不多"用; 不在默认的一组里). */
  idle60(ctx) {
    const now = ctx.clock.t / 1000, { loco } = ctx;
    loco.idle.nextAction = now + TUNE.fidgetGap * (1 + Math.random());
    loco.idle.nextSwitch = now + 30 + Math.random() * 15;
    ctx.step(FPS * 60);
    return null;
  },
  /** 依次走到几个目标 (默认: room.json 里是窗边再回房间中间). */
  walk(ctx, ids = ctx.picks.walk) {
    for (const to of ids) { const r = ctx.act({ do: 'go', to }); if (r) return r; }
    ctx.step(FPS);
    return null;
  },
  /** 走到座位坐下, 坐 3 秒, 站起来. */
  sit(ctx, [id] = [ctx.picks.sit]) {
    const r = ctx.act({ do: 'sit', to: id }, 25);
    if (r) return r;
    ctx.step(FPS * 3);
    return ctx.act({ do: 'stand' }) || (ctx.idleAgain(), null);
  },
  /** 拿起一件东西, 放到一个台面上 (默认: 地上的球放到茶几上). */
  pickput(ctx, [item, to] = ctx.picks.pickput) {
    return ctx.act({ do: 'pick', to: item }, 25) || ctx.act({ do: 'put', to }, 25) || (ctx.idleAgain(), null);
  },
  /** 站着双手挥手, 再耸肩 (动捕手势). */
  gesture(ctx) {
    const { loco } = ctx;
    for (const a of [{ do: 'wave', side: 'B', spd: 1 }, { do: 'shrug', spd: 1 }]) {
      if (!loco.gesture(a)) return `做不了手势 ${a.do}`;
      ctx.step(2);
      if (!ctx.until(() => loco.idle?.kind !== 'gesture' && !loco.gestureReq, 10)) return `手势没做完 ${a.do}`;
      ctx.step(FPS / 2);
    }
    return null;
  },
  /** 伸手邀请牵手: 先转过去背对用户, 再伸右手 (要先转身), 伸 4 秒, 收回. 另外量伸出去那一段 (权重 > 0.9) 的手:
   *  掌心朝上多少 (palmUp, 1 = 正朝上)、手指朝用户多少 (fingerToUser)、手在胸前多远 (handAhead, 米)、手比肩低多少 (handBelow, 米)、
   *  身体朝向和"朝用户"差几度 (faceErr), 写进结果的 hand 段. */
  hand(ctx) {
    const { loco, vrm } = ctx, h = vrm.vrm.humanoid;
    const r = ctx.act({ do: 'turn', side: 'B' });
    if (r) return r;
    ctx.step(30);
    loco.offerHand('R', 4);
    const acc = { palmUp: [], fingerToUser: [], handAhead: [], handBelow: [], faceErr: [] };
    const W = (n) => h.getNormalizedBoneNode(n).getWorldPosition(h.getNormalizedBoneNode('hips').position.clone());
    for (let i = 0; i < FPS * 6; i++) {
      ctx.step(1);
      if (loco.offer.w < 0.9) continue;
      const hand = h.getNormalizedBoneNode('rightHand'), q = hand.getWorldQuaternion(hand.quaternion.clone());
      const palm = W('rightHand').set(0, -1, 0).applyQuaternion(q);
      const hw = W('rightHand'), fing = W('rightMiddleProximal').sub(hw).normalize();
      const user = loco._lookPoint(loco.targets.user), chest = W('chest');
      const toUser = user.clone().sub(chest).setY(0).normalize();
      const fwd = [Math.sin(loco.char.yaw), Math.cos(loco.char.yaw)];
      acc.palmUp.push(palm.y);  // 掌心法线 (零位朝 -Y) 朝上时 palm.y = 1
      acc.fingerToUser.push(fing.clone().setY(0).normalize().dot(toUser));
      acc.handAhead.push(hw.clone().sub(chest).setY(0).dot(toUser));
      acc.handBelow.push(W('rightUpperArm').y - hw.y);
      acc.faceErr.push(Math.acos(Math.max(-1, Math.min(1, fwd[0] * toUser.x + fwd[1] * toUser.z))) * 180 / Math.PI);
    }
    ctx.extra.hand = Object.fromEntries(Object.entries(acc).map(([k, v]) => [k, v.length ? +mean(v).toFixed(3) : null]));
    ctx.extra.hand.frames = acc.palmUp.length;
    if (!acc.palmUp.length) return '手没伸出去';
    return ctx.idleAgain(), null;
  },
  /** 表情动作 (比心、捂嘴笑、托腮、拍手、比耶、捧脸; 行动层的 _emote) 各做一次, 前一个完全收回再做下一个 (每个最多 6 秒).
   *  extra.emote: 每个动作做满时手腕离目标多远 (米; 没做出来是 null). */
  emote(ctx) {
    const err = {};
    for (const kind of ['heart', 'giggle', 'chin', 'clap', 'peace', 'cheeks']) {
      ctx.loco.emote(kind, { side: 'R' });
      err[kind] = null;
      for (let i = 0; i < FPS * 6 && ctx.loco.emoteSt; i++) {
        ctx.step(1);
        const st = ctx.loco.emoteSt;
        if (st?.kind === kind && st.w > 0.95 && st.err) err[kind] = +Math.max(...Object.values(st.err)).toFixed(3);
      }
      ctx.step(Math.round(FPS * 0.5));
    }
    ctx.extra.emote = err;
    return Object.values(err).some((e) => e === null) ? '有表情动作没做出来' : null;
  },
  /** 说话 8 秒 (没有 HAST-SEC 时说话间隙插动捕说话手势). */
  talk(ctx) {
    ctx.loco.speaking = true;
    ctx.step(FPS * 8);
    ctx.loco.speaking = false;
    return null;
  },
  /** 端一杯东西 (小窝的物理与后果 P0 的评测台, 见 docs/roadmap/room-physics.md): 场景里放在台面上的东西 (room.json 是桌上的书)
   *  当马克杯 (内半径 4 cm、内高 9 cm), 拿起来端到离它最远的另一个台面 (room.json 是茶几), 放下. 液体按手里杯子每帧的运动算
   *  (web/physics/liquid.js), 洒出来的落到身上、台面、地上. 名字里写条件 carry:装量>走法>温度:
   *    装量 empty / half / full (离杯沿 8 mm); 温度 hot (80 ℃ 咖啡) / cold (22 ℃ 水);
   *    走法 slow (0.6 倍速) / normal / turn (1.4 倍速, 中途经过房间中间不停, 拐个急弯) / stop (1.4 倍速, 走到一半急停 0.8 秒再走) / run;
   *    第四项 (可省) 是端得多稳 (行动层的 carryCare, 0-1, 默认 TUNE.carryCare).
   *  她能挑的几样也可以写成 键=值 (放在后面, 覆盖前面的): care=0-1 端得多稳; hand=0.4-1.5 拿放的快慢 (行动层的 handSpeed);
   *  fill=0-1 装多少 (杯子容量的比例, 也可以写 empty / half / full); target=毫升 要送到的量 (默认满杯 412 ml);
   *  phys=1 杯子是刚体 (Rapier, 小窝物理 P1/P2): 手用有上限的力抓着, 会滑、会脱手; 放下时手接着拿着往落点放, 碰到台面就松开;
   *  extra.carry.phys 记脱手、滑、碰撞、每帧耗时、握住时手离杯子多远 (grabGap, 杯子是跳到掌心的, 和不交给物理时一样)、
   *  杯子最后停在哪; 送到要杯子真的停在那个台面上 (没掉、没倒).
   *  settle=1 拿起来以后先把杯子端到身前再走; temp=摄氏度 咖啡多烫 (也可以写在第三项); wait=秒 倒好以后放着等多久再端
   *  (按液体模型冷却, 等的时间单独报 wait, 不进得分).
   *  extra.carry: 送到没有、剩多少、洒了多少、落在哪、碰到身体哪里、烫到没有、用了几秒、得分; 按阶段 (一段接一段, 不重叠:
   *  pick 从发拿起到拿住, lift 拿住以后到第一次开始走, walk 从开始走到发放下 (中间停下、站着都算), put 从发放下到结束):
   *  phases 每段洒了多少, phaseMl 每段开始、结束时杯里多少, contactPhases 每段碰到身上哪里 (每处取最重的那次).
   *  得分 = 送到才有分: min(1, 剩下的 / 要送的量) - 0.5 x 最重的烫伤 - 0.01 x 超过 20 秒的秒数. 不动 (没送到)、少装或先倒掉
   *  (送到的量不够) 都拿不到高分; 磨磨蹭蹭也扣. (2026-10-27 以前是"剩下的比例", 空杯送到算 1 分, 改了) */
  carry(ctx, args = []) {
    const { loco, vrm } = ctx;
    const pos = args.filter((a) => !a.includes('=')), opt = Object.fromEntries(args.filter((a) => a.includes('=')).map((a) => a.split('=')));
    let [fill = 'full', how = 'normal', temp = 'hot', care = null] = pos;
    if ('fill' in opt) fill = opt.fill;
    if ('care' in opt) care = opt.care;
    if ('temp' in opt) temp = opt.temp;
    const hand = 'hand' in opt ? +opt.hand : null;
    if (hand !== null && !Number.isFinite(hand)) return `不认识的拿放快慢 ${opt.hand}`;
    const t = sceneTasks(loco.scene);
    const it = t.items.find((i) => i.on && i.on !== 'floor') || t.items[0];
    if (!it) return '场景里没有能拿的东西';
    const from = loco.targets[it.on]?.place || loco.items[it.id].mesh.position.toArray();
    // 放到离它最远的桌子、茶几这类台面 (不放座位上, 不放比 0.9 m 高的架子上)
    const far = (p) => Math.hypot(p.place[0] - from[0], p.place[2] - from[2]);
    const to = t.places.filter((p) => p.id !== it.on && !t.seats.includes(p.id) && p.y <= 0.9)
      .map((p) => ({ ...p, place: loco.targets[p.id].place })).sort((a, b) => far(b) - far(a))[0];
    if (!to) return '没有别的台面可放';
    const R = 0.04, H = 0.09, cap = Math.PI * R * R * H * 1e6, full = cap - Math.PI * R * R * 0.008 * 1e6;
    const named = { empty: 0, half: cap / 2, full }[fill];
    const ml = named ?? (Number.isFinite(+fill) ? Math.max(0, Math.min(1, +fill)) * cap : undefined);
    if (ml === undefined) return `不认识的装量 ${fill}`;
    if (care !== null && !Number.isFinite(+care)) return `不认识的端法 ${care}`;
    const target = 'target' in opt ? +opt.target : full;
    // 同一个种子的各个条件用同一串随机数 (默认按场景名长短换种子, 冷热、端法比较时动作就不一样了)
    Math.random = mulberry32((ctx.seed ?? 1) * 7919 + 101);
    loco.carryCare = care === null ? null : +care;
    loco.handSpeed = hand;
    const world = new LiquidWorld();
    const cup = world.cup(it.id, { R, H });
    // 温度: hot 80 ℃ 咖啡、cold 22 ℃ 水, 或者直接写摄氏度 (咖啡; 比如 60: 等凉一点再端)
    const tempC = temp === 'cold' ? 22 : temp === 'hot' ? 80 : +temp;
    if (!Number.isFinite(tempC)) return `不认识的温度 ${temp}`;
    world.fill(it.id, ml, tempC, temp === 'cold' ? 'water' : 'coffee');
    // wait=秒: 倒好以后先放着等这么久再端 (按液体模型冷却; 412 ml 马克杯 80 -> 65 ℃ 约 12 分钟、-> 55 ℃ 约 22 分钟). 等的时间不进得分
    const wait = 'wait' in opt ? Math.max(0, +opt.wait || 0) : 0;
    if (wait) world.advance(wait);
    const startTemp = cup.temp;
    const item = loco.items[it.id];
    const grip0 = item.grip;
    item.grip = 'cup';  // 按杯子端 (腰前, 和小窝的姿势层一样), 场景结束还原
    let caps = [];
    const hit = hitter(() => caps), ground = grounder(vrm.collision?.());
    const log = { spills: [], contacts: {}, lands: {}, phases: {}, phaseMl: {}, contactPhases: {} };
    let held = item.held, maxA = 0, putting = false;
    // 阶段按事件一段接一段 (不重叠): pick 从发拿起到拿住; lift 拿住以后到第一次开始走; walk 从开始走到发放下 (中间停下、站着都算);
    // put 从发放下到结束 (落下的也等它落完). 每段记开始、结束时杯里多少 (phaseMl)
    let ph = 'pick';
    const enter = (next) => {
      if (next === ph) return;
      (log.phaseMl[ph] ??= [cup.ml, cup.ml])[1] = cup.ml;
      ph = next;
      log.phaseMl[ph] ??= [cup.ml, cup.ml];
    };
    log.phaseMl.pick = [ml, ml];
    const q = item.mesh.quaternion.clone(), pw = item.mesh.position.clone();
    // phys=1: 杯子是刚体 (圆柱: 半高 4.5 cm、半径 4 cm; 0.3 kg 加液体的重量), 房间的家具部件、地面是固定的; 拿在手里由抓持拉着,
    // 松手、脱手以后自己落. 液体按刚体的位置、速度算 (不过 P0 的跟手弹簧)
    let physics = null;
    const half0 = item.half, CUP_H = 0.045, phys = { events: [], drops: 0, slips: 0, bumps: 0, maxBump: 0, ms: 0 };
    if (opt.phys === '1') {
      if (!ctx.phys) return '刚体没载入';
      physics = new ctx.phys.PhysicsLayer(new ctx.phys.RigidWorld(ctx.phys.R), loco, { groups: vrm.collision?.() || [] });
      item.mesh.position.y += CUP_H - half0;  // 书的中心 -> 杯子的中心 (底面在同一个台面上)
      item.mesh.quaternion.identity();
      item.half = CUP_H;
      physics.adopt(item, { shape: { cylinder: [CUP_H, R] }, mass: 0.3 + ml / 1000 });
    }
    ctx.onFrame = () => {
      if (putting) enter('put');
      else if (ph === 'pick' && item.held) enter('lift');
      else if (ph === 'lift' && loco.mode === 'go') enter('walk');
      if (physics) {
        physics.setMass(it.id, 0.3 + cup.ml / 1000);
        for (const e of physics.tick(DT)) {
          if (e.type === 'grab') { cup.teleported(); phys.grabGap = e.gap; }  // 握住时东西跳到掌心 (和不交给物理时一样), 不算加速度
          if (e.type === 'drop') phys.drops++;
          else if (e.type === 'slip') phys.slips++;
          else if (e.type === 'bump' && (e.a === it.id || e.b === it.id)) { phys.bumps++; phys.maxBump = Math.max(phys.maxBump, e.force); }
          if (e.type !== 'bump' && phys.events.length < 20) phys.events.push([ph, e.type, +(ctx.frame / FPS).toFixed(2)]);
        }
      } else if (item.held !== held) cup.teleported();  // 拿起、放下时东西跳到手里 / 台面上
      held = item.held;
      caps = bodyCapsules(vrm);
      const poses = {};
      const P = physics?.items[it.id]?.pose;
      if (P && (item.held || !P.sleeping)) {  // 刚体: 杯底 = 中心往下半高; 拿着或者还在动 (掉下来、滑) 都算
        const V3 = item.mesh.position.constructor;
        const bottom = new V3().fromArray(P.p).sub(new V3(0, CUP_H, 0).applyQuaternion(item.mesh.quaternion));
        poses[it.id] = { p: bottom.toArray(), q: P.q, v: P.v, hold: item.held || null, physical: true };
      } else if (!physics && item.held) {
        item.mesh.getWorldPosition(pw);
        item.mesh.getWorldQuaternion(q);
        poses[it.id] = { p: pw.toArray(), q: q.toArray(), hold: item.held };
      }
      for (const e of world.step(DT, { poses, hit, ground })) {
        if (e.type === 'spill') {
          log.spills.push([+(ctx.frame / FPS).toFixed(2), +e.ml.toFixed(2)]);
          log.phases[ph] = (log.phases[ph] || 0) + e.ml;
          vrm.kin?.addPain?.('spill', e.ml);  // 痛觉的 spill 项: 调优、回归、动作成长都看得到
        } else if (e.type === 'contact') {
          // 全程按部位一份, 每一段按部位一份 (都取最重的那次); 碰到身上的那一刻在哪一段 (洒出来往下落要一会儿, 按碰到的时候算)
          for (const c of [(log.contacts[e.part] ??= { ml: 0, level: 'wet', severity: 0 }),
            ((log.contactPhases[ph] ??= {})[e.part] ??= { ml: 0, level: 'wet', severity: 0 })]) {
            c.ml += e.ml;
            if (e.effect.severity >= c.severity) Object.assign(c, { level: e.effect.level, severity: e.effect.severity });
          }
        } else if (e.type === 'land') log.lands[e.surface] = (log.lands[e.surface] || 0) + e.ml;
      }
      maxA = Math.max(maxA, Math.hypot(...cup.A));
    };
    const f0 = ctx.frame;
    let reason = ctx.act({ do: 'pick', to: it.id }, 25);
    // settle=1: 拿起来以后先把杯子端到身前 (端的权重到 0.95, 最多 3 秒) 再走 (不然端到身前和起步叠在一起)
    if (!reason && opt.settle === '1') ctx.until(() => (loco.carryW[item.held] ?? 0) >= 0.95, 3);
    if (!reason) {
      const speed = { slow: 0.6, normal: 1, turn: 1.4, stop: 1.4, run: 1.8 }[how];
      if (speed === undefined) reason = `不认识的走法 ${how}`;
      else if (how === 'turn' && loco.targets.center) {
        // 经过房间中间不停: 两段路接起来, 拐角处不减速到停
        const via = loco.targets.center.approach, goal = loco.targets[to.id].approach;
        let done = false;
        if (!loco.goTo(to.id, { speed, after: () => { done = true; } })) reason = `走不到${to.id}`;
        else {
          const a = findPath(loco.grid, [loco.char.x, loco.char.z], via), b = findPath(loco.grid, via, goal);
          if (a && b) { loco.path = [...a, ...b.slice(1)]; loco.pathLen = pathLength(loco.path); }
          if (!ctx.until(() => done, 25)) reason = '超时: 端过去';
        }
      } else if (how === 'stop') {
        let done = false;
        if (!loco.goTo(to.id, { speed, after: () => { done = true; } })) reason = `走不到${to.id}`;
        else {
          const s0 = [loco.char.x, loco.char.z], g = loco.targets[to.id].approach, half = Math.hypot(g[0] - s0[0], g[1] - s0[1]) / 2;
          ctx.until(() => done || Math.hypot(loco.char.x - s0[0], loco.char.z - s0[1]) >= half, 20);
          loco.stop();
          ctx.step(Math.round(FPS * 0.8));
          done = false;
          if (!loco.goTo(to.id, { speed, after: () => { done = true; } })) reason = `走不到${to.id}`;
          else if (!ctx.until(() => done, 25)) reason = '超时: 端过去';
        }
      } else reason = ctx.act({ do: 'go', to: to.id, spd: speed }, 25);
    }
    putting = true;
    if (!reason) reason = ctx.act({ do: 'put', to: to.id }, 25);
    const secs = (ctx.frame - f0) / FPS;
    ctx.step(FPS * 2);  // 落下的都落地
    ctx.onFrame = null;
    (log.phaseMl[ph] ??= [cup.ml, cup.ml])[1] = cup.ml;
    item.grip = grip0;
    // 送到: 放下了, 而且 (刚体时) 杯子真的停在那个台面上 (没掉到地上、没歪倒)
    let landed = true;
    if (physics) {
      const P = physics.rigid.pose(it.id), place = loco.targets[to.id].place;
      const upright = Math.abs(P.q[0]) < 0.2 && Math.abs(P.q[2]) < 0.2;
      landed = Math.hypot(P.p[0] - place[0], P.p[2] - place[2]) < 0.35 && Math.abs(P.p[1] - (place[1] + CUP_H)) < 0.03 && upright;
      Object.assign(phys, { landed, upright, steps: physics.rigid.stats.steps, msPerFrame: +(physics.rigid.stats.ms / Math.max(1, physics.rigid.stats.frames)).toFixed(3),
        maxMs: +physics.rigid.stats.maxMs.toFixed(2), final: P.p.map((x) => +x.toFixed(3)) });
      physics.release(item);
      item.half = half0;
    }
    // 刚体时按杯子最后停在哪算 (放下时滑脱、最后一下掉在台面上也算送到; 脱手几次在 phys.drops)
    const delivered = !reason && (physics ? landed : item.on === to.id);
    const r2 = (x) => +x.toFixed(2);
    const worst = Math.max(0, ...Object.values(log.contacts).map((c) => c.severity));
    const left = cup.ml, spilled = world.ledger.spilled;
    const stimuli = { wet: 0, hot: 0, burn: 0 };
    for (const c of Object.values(log.contacts)) stimuli[c.level] = (stimuli[c.level] || 0) + 1;
    ctx.extra.carry = {
      item: it.id, to: to.id, fill, how, temp, wait, startTemp: r2(startTemp), care: loco.carryCare ?? TUNE.carryCare, hand: loco.handSpeed ?? TUNE.handSpeed, settle: opt.settle === '1',
      ml: +ml.toFixed(1), target: +target.toFixed(1), delivered, left: +left.toFixed(1), spilled: +spilled.toFixed(2),
      spilledFrac: ml > 0 ? +(spilled / ml).toFixed(4) : 0, maxSloshMm: +(maxA * 1000).toFixed(1),
      phases: Object.fromEntries(Object.entries(log.phases).map(([k, v]) => [k, r2(v)])),
      phaseMl: Object.fromEntries(Object.entries(log.phaseMl).map(([k, [a, b]]) => [k, [r2(a), r2(b)]])),
      lands: Object.fromEntries(Object.entries(log.lands).map(([k, v]) => [k, r2(v)])),
      contacts: Object.fromEntries(Object.entries(log.contacts).map(([k, c]) => [k, { ...c, ml: r2(c.ml) }])),
      contactPhases: Object.fromEntries(Object.entries(log.contactPhases).map(([p, parts]) => [p,
        Object.fromEntries(Object.entries(parts).map(([k, c]) => [k, { ...c, ml: r2(c.ml) }]))])),
      stimuli, worstBurn: worst, secs: +secs.toFixed(1), spillTimes: log.spills.slice(0, 20),
      score: delivered ? +(Math.min(1, left / Math.max(1, target)) - 0.5 * worst - 0.01 * Math.max(0, secs - 20)).toFixed(3) : 0,
      balanceError: +world.balance().error.toExponential(1),
    };
    if (physics) ctx.extra.carry.phys = phys;
    ctx.extra.carry.env = carryEnv(ctx, !!physics);
    loco.handSpeed = null;
    return reason;
  },
};

// -- 模型检查 (体检用; 场景检查见 scene-tasks.js) -------------------------------------------------
/** 模型检查: 身体比例和骨骼 (长度都是髋高的倍数), 新模型和已经调好的模型比. */
export function modelChecks(vrm) {
  const loco = vrm.loco, h = vrm.vrm.humanoid, r = loco.rest, H = loco.hipHeight;
  const len = (a, b) => (r[a] && r[b] ? r[a].distanceTo(r[b]) : 0);
  const both = (f) => (f('left') + f('right')) / 2 / H;
  const used = [...new Set([...FEATURE_BONES.flat(), 'spine', 'leftShoulder', 'rightShoulder'])];
  return {
    name: vrm.vrm.meta?.name || vrm.vrm.meta?.title || null,
    hipHeight: +H.toFixed(3),
    armToHip: +both((s) => len(`${s}UpperArm`, `${s}LowerArm`) + len(`${s}LowerArm`, `${s}Hand`)).toFixed(3),
    legToHip: +both((s) => len(`${s}UpperLeg`, `${s}LowerLeg`) + len(`${s}LowerLeg`, `${s}Foot`)).toFixed(3),
    shoulderToHip: +(len('leftUpperArm', 'rightUpperArm') / H).toFixed(3),
    ankle: +((loco.ankle.left + loco.ankle.right) / 2).toFixed(3),
    hangDeg: vrm.contact ? { ...vrm.contact.hang } : null,
    missingBones: used.filter((n) => !h.getNormalizedBoneNode(n)),
  };
}

/**
 * 跑一组场景. params: 要试的参数 ({名字: 值}, 见 motion-params.js), 跑完恢复原值; seeds: 每个场景各用哪些种子.
 * 返回 {params, ref: {...}, results: [{scenario, seed, ok, reason, frames, natural, pain..., ...执行评估}]}.
 */
const fnv = (s) => {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193); }
  return (h >>> 0).toString(16).padStart(8, '0');
};

/**
 * 这一趟用的动作来源和物理环境的指纹 (端咖啡评测台的两条对照线): 比动作模型时物理要固定, 比物理时动作来源要固定, 两边都单独
 * 验证过再测组合. tools/bench_carry.py --compare 拿两份结果对: 两边都变了就不下结论.
 *   motion: 动作库是原来的运动匹配 (mm) 还是学出来的 (lmm)、库多大、待机库多大、可调参数 (motion-params.js) 的哈希、模型,
 *           code: 行动层源文件的哈希 (MOTION_SRC; 只改了代码、没改参数也分得出来);
 *   physics: 液体常数的哈希, 刚体 (phys=1) 时加刚体、抓持、手臂常数的哈希, 不是刚体时是 'stand-in' (P0 的跟手弹簧),
 *           code: 物理源文件的哈希 (PHYS_SRC).
 */
const MOTION_SRC = ['locomotion.js', 'contact.js', 'hand.js', 'mm.js', 'lmm.js', 'pose-filter.js', 'motion-params.js', 'kinematics.js'];
const PHYS_SRC = ['physics/liquid.js', 'physics/grip.js', 'physics/rigid.js', 'physics/rigid-scene.js', 'physics/body-hit.js'];
async function srcHash(files) {
  const t = await Promise.all(files.map((f) => fetch(new URL(f, import.meta.url)).then((r) => (r.ok ? r.text() : '')).catch(() => '')));
  return fnv(t.join('\n'));
}

function carryEnv(ctx, rigid) {
  const L = ctx.loco, m = L.db?.meta || {};
  const motion = { source: m.lmm ? 'lmm' : 'mm', frames: m.frames ?? L.db?.frames ?? null, clips: (m.clips || L.db?.clips)?.length ?? null,
    idle: L.idleDB?.meta?.frames ?? L.idleDB?.frames ?? null, params: fnv(JSON.stringify(readParams(TARGETS))), avatar: ctx.vrm?.info?.name ?? null,
    code: ctx.src?.motion ?? null };
  const physics = { liquid: fnv(JSON.stringify(LIQUID)), rigid: rigid ? fnv(JSON.stringify(ctx.phys.consts)) : 'stand-in', code: ctx.src?.physics ?? null };
  return { motion: { ...motion, id: fnv(JSON.stringify(motion)) }, physics: { ...physics, id: fnv(JSON.stringify(physics)) } };
}

export async function runEval({ scenarios = null, seeds = [1], params = {}, keepBody = false, checks = false } = {}) {
  const state = window.__wm;
  if (!state?.vrm?.loco?.idleDB) throw new Error('行动层或待机库没有载入');
  const ctx = ctxFor(state);
  const { vrm, loco, kin } = ctx;
  ctx.picks = scenePicks(loco.scene);
  if (scenarios === 'sweep') scenarios = sweepScenarios(loco.scene, loco);
  scenarios ??= defaultScenarios(loco.scene, loco);
  // 刚体 (小窝物理 P1、P2; Rapier 4 MB): 只有场景要 (phys=1) 时才载入, 平时的评估不受影响
  if (scenarios.some((s) => s.includes('phys=1')) && !ctx.phys) {
    const [{ loadRapier, RigidWorld, RIGID }, { PhysicsLayer }, { GRIP, ARM }] = await Promise.all([import('./physics/rigid.js'),
      import('./physics/rigid-scene.js'), import('./physics/grip.js')]);
    ctx.phys = { R: await loadRapier(), RigidWorld, PhysicsLayer, consts: { RIGID, GRIP, ARM } };
  }
  // 代码指纹 (端咖啡评测台的两条对照线, 见 carryEnv): 只有跑 carry 时才算
  if (scenarios.some((s) => s.startsWith('carry'))) ctx.src = { motion: await srcHash(MOTION_SRC), physics: await srcHash(PHYS_SRC) };
  const ref = reference(loco);
  const body = vrm.body, render = vrm.renderer.render, now0 = performance.now, rand0 = Math.random;
  const before = applyParams(TARGETS, params), used = readParams(TARGETS);
  const results = [];
  ctx.clock.t = now0.call(performance);
  performance.now = () => ctx.clock.t;
  vrm.renderer.render = () => {};
  if (body && !keepBody) vrm.attachBody(null);
  try {
    for (const name of scenarios) {
      for (const seed of seeds) {
        Math.random = mulberry32(seed * 7919 + name.length);
        ctx.seed = seed;
        await reset(ctx);
        kin.begin(`${name}#${seed}`);
        const f0 = ctx.frame, by0 = { ...(vrm.contact?.stats.by || {}) }, bal0 = { ...kin.balanceBy };
        ctx.extra = {};
        let reason;
        const [kind, ids] = parseScenario(name);
        try { reason = SCENARIOS[kind] ? SCENARIOS[kind](ctx, ids) : `没有这个场景 ${name}`; } catch (err) { reason = `出错: ${err.message}`; }
        const sum = kin.end(reason);
        const q = pairFeatures(ctx.pairs);
        const d = q.length ? nnDistances(ref.ref, q) : new Float32Array(0);
        const sorted = Float32Array.from(d).sort();
        // 防穿模层按"身体段:手臂取样点"累计的穿透深度 (米), 看手臂是在哪里穿模
        const contactBy = Object.fromEntries(Object.entries(vrm.contact?.stats.by || {})
          .map(([k, v]) => [k, +(v - (by0[k] || 0)).toFixed(3)]).filter(([, v]) => v > 0));
        results.push({ scenario: name, seed, ok: !reason, reason: reason || null, frames: ctx.frame - f0,
          natural: +(mean(d) / ref.base).toFixed(3), natural_p90: +((sorted[Math.floor(sorted.length * 0.9)] || 0) / ref.base).toFixed(3),
          ...sum, contactBy, balanceBy: Object.fromEntries(Object.entries(kin.balanceBy)
            .map(([k, v]) => [k, +(v - (bal0[k] || 0)).toFixed(3)]).filter(([, v]) => v > 0)), style: ctx.styleSummary(), ...ctx.extra });
      }
    }
  } finally {
    Math.random = rand0;
    performance.now = now0;
    vrm.renderer.render = render;
    if (body && !keepBody) vrm.attachBody(body);
    applyParams(TARGETS, before);
  }
  return { params: used, ref: { frames: ref.frames, held: ref.held, base: +ref.base.toFixed(4) }, scene: loco.scene.name || null,
    ...(checks ? { checks: { scene: sceneChecks(loco, vrm.collision?.()), model: modelChecks(vrm) } } : {}), results };
}
