// 桌宠的身体: StayStill 待机 (MIT, 12 人的真实待机动捕) 驱动全身, 手势叠加在上面.
// 待机来自 web/locomotion.js 的行动层, 给它一个"桌面场景": 只有一块空地, 角色站在原点、朝向镜头, 没有家具和可拿的东西
// (房间、阴影、跟拍镜头都不挂). 行动层一挂上, avatar-vrm.js 就不再用程序动作的手臂通道, 挥手、举手、思考这类手势会看不见,
// 所以在行动层写完姿态之后、防穿模之前, 按程序动作的手臂角度对上臂和前臂做混合; 手势结束后淡回待机.
import * as THREE from 'three';
import { createLocomotion } from '/static/locomotion.js';
import { ROM } from '/static/rom.js';

export const DESK_SCENE = {
  name: 'desktop',
  note: '桌宠用的空场景: 只有一块空地, 没有家具',
  floor: [[-2, -2], [2, -2], [2, 2], [-2, 2]],
  wallHeight: 0,
  agentRadius: 0.3,
  spawn: { pos: [0, 0], yaw: 0 },
  camera: {},
  objects: [{ id: 'user', name: '用户', approach: [0, 1.5], look: 'camera' }],
  decor: [],
};

const rad = (d) => (d * Math.PI) / 180;
const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
const IN = 0.12, OUT = 0.3;  // 手势叠加的淡入/淡出时间常数 (秒)
// StayStill 的待机小动作只留左右换重心. 摸脸 (t_fac)、挠头 (sc_hea)、挠胳膊 (sc_arm)、看表 (l_wat)、伸懒腰 (st_arm)
// 这几类手会贴到脸、头发、袖子上, 宽袖长发的模型最容易穿模; 环顾 (l_aro) 头会转到近乎侧后方. 在桌面上看着都像"乱动".
export const CALM_IDLE_ACTIONS = ['wei_lr', 'wei_rl'];
// 一段待机里, 两只手同时在身体正前方 (双手交叠在身前、抱臂) 的时间超过这个比例, 就换掉这段: 手会压进裙子, 前臂会压进外套.
// (试过只用防穿模层的推开次数当判据: 手自然垂在大腿边、贴着衣服时也一直在推, 分不出来, 误伤了 9/12 段.
//  2026-10-27 起两个一起看: 位置在身前, 而且这一帧接触层真把手臂推出身体 PRESS_M 以上. 只看位置会把动捕自己两手合在腹前、没插进衣服的段也去掉;
//  裙子那一项 (stats.skirt) 不算: 手放在腹前本来就贴着裙子, 裙子修正几乎每帧都有, 加上它 idle_08 照样被去掉)
const FRONT_SHARE = 0.4;
const PRESS_M = 0.005;  // 这一帧接触层把手臂推出身体 (stats.lastPush) 超过这么多, 才算"压着"
const MIN_IDLE_CLIPS = 3;  // 至少留几段待机
const identity = new THREE.Quaternion();
const q = (x, y, z) => new THREE.Quaternion().setFromEuler(new THREE.Euler(x, y, z));

/** 程序动作的手臂角度 -> 标准化骨骼的局部旋转 (和 avatar-vrm.js 的程序动作分支同一套算法). */
function armTargets(ch) {
  const elbow = (s, e) => {
    const f = clamp((s - 50) / 40), frontal = f * f * (3 - 2 * f);
    return { z: rad(e * frontal), y: rad(Math.min(140, e * (1 - frontal))) };
  };
  const ls = Math.max(ch.ls, 16), rs = Math.max(ch.rs, 16);
  const le = elbow(ls, ch.le), re = elbow(rs, ch.re);
  return {
    L: { upper: q(0, 0, rad(ls - 90)), lower: q(0, -le.y, le.z) },
    R: { upper: q(0, 0, rad(90 - rs)), lower: q(0, re.y, -re.z) },
  };
}

/**
 * 挂上 StayStill 待机. 服务端没有运动匹配库时返回 null (角色照旧用程序待机).
 * motion: MotionSystem (手势的来源); channels(): 这一帧程序动作的通道值;
 * calm: 待机小动作只留换重心和环顾 (默认).
 * 叠加层: 手臂按程序动作的角度混合 (绝对值); 躯干前倾 (鞠躬) 和头部俯仰/转向/歪头 (思考、耸肩、鞠躬时的低头) 叠加在待机姿态上.
 * 点头、摇头不走这里, 交给 Soullink 的头部动作.
 */
export async function attachDeskBody(v, motion, channels, { calm = true } = {}) {
  let status;
  try {
    status = (await (await fetch('/api/status')).json()).motion || {};
  } catch {
    return null;
  }
  if (!status.locomotion?.includes('neutral')) return null;
  const t0 = performance.now();
  const layer = await createLocomotion(v, DESK_SCENE);
  layer.acts = null;  // 坐下、拿放的动作库桌面上用不到
  // avatar-vrm.js 的 attachLocomotion 会挂房间、阴影和跟拍镜头, 桌宠只要这三个回调
  layer.thighRadius = (side) => v.contact?.radius?.[`${side}Thigh`];
  layer.handRadius = (side) => v.contact?.radius?.[`${side}:hand`];
  layer.foreArmRadius = (side) => v.contact?.radius?.[`${side}:foreArm`];

  if (calm && layer.idleDB?.groups?.actions) {
    for (const kind of Object.keys(layer.idleDB.groups.actions)) {
      if (!CALM_IDLE_ACTIONS.includes(kind)) layer.idleDB.groups.actions[kind] = [];
    }
  }
  const avoid = idleAvoider(v, layer);

  const h = v.vrm.humanoid;
  const node = (n) => h.getNormalizedBoneNode(n);
  // 程序动作的手臂角度是在"肩膀处在标准位置"的前提下算的; StayStill 会转动肩膀, 照搬会让手臂横过脸、越过头顶.
  // 手势进行时肩膀按同样的权重混回标准位置.
  const bones = {
    L: { shoulder: node('leftShoulder'), upper: node('leftUpperArm'), lower: node('leftLowerArm') },
    R: { shoulder: node('rightShoulder'), upper: node('rightUpperArm'), lower: node('rightLowerArm') },
  };
  const spine = node('spine'), chest = node('chest') || node('upperChest'), neck = node('neck');
  const weight = { L: 0, R: 0, torso: 0, head: 0 };
  const ease = (key, on, dt) => {
    const goal = on ? 1 : 0;
    weight[key] += (goal - weight[key]) * (1 - Math.exp(-dt / (goal > weight[key] ? IN : OUT)));
    return weight[key] >= 1e-3;
  };
  const add = (bone, rot, w) => bone?.quaternion.multiply(identity.clone().slerp(rot, w));
  const squat = squatter(layer, node, weight, add);
  const lean = leaner(layer, node, add);
  const update = layer.update.bind(layer);
  layer.update = (dt) => {
    update(dt);
    // 蹲着、趴着、行动层让手在腰前交叠或做表情动作 (比心、拍手、托腮…) 时手在身前: 不算待机片段的毛病. 以前不排除交叠,
    // 交叠一停 3 秒那段待机就被当成'手压在身前'去掉, 而且记在本机, 几分钟就只剩 3 段
    avoid(weight.L > 1e-3 || weight.R > 1e-3 || squat.active || lean.w > 1e-3 || (layer.claspW || 0) > 0.05
      || !!layer.emoteUses?.('left') || !!layer.emoteUses?.('right'));
    if (!lean.on) squat.update(dt);  // 趴着时不蹲
    lean.update(dt);
    const g = motion.groups, idle = motion.idle;
    // 趴着时胳膊叠在栏杆上: 手势的手臂和鞠躬不叠上去 (头部动作照常)
    const free = lean.w < 1e-3;
    const active = { L: free && g.larm?.clip !== idle, R: free && g.rarm?.clip !== idle, torso: free && g.torso?.clip !== idle,
      head: g.head?.clip !== idle };
    if (!Object.values(active).some(Boolean) && Object.values(weight).every((w) => w < 1e-3)) return;
    const ch = channels();
    const arms = armTargets(ch);
    for (const side of ['L', 'R']) {
      if (!ease(side, active[side], dt)) continue;
      bones[side].shoulder?.quaternion.slerp(identity, weight[side]);
      for (const part of ['upper', 'lower']) bones[side][part]?.quaternion.slerp(arms[side][part], weight[side]);
    }
    if (ease('torso', active.torso, dt)) {  // 鞠躬: 和程序动作分支同样的分配 (腰 0.5、胸 0.35)
      add(spine, q(rad(ch.lean) * 0.5, 0, 0), weight.torso);
      add(chest, q(rad(ch.lean) * 0.35, 0, 0), weight.torso);
    }
    if (ease('head', active.head, dt)) add(neck, q(rad(ch.hp) * 0.7, rad(ch.hy) * 0.7, -rad(ch.hr) * 0.7), weight.head);
  };
  v.loco = layer;
  return { layer, weight, idle: !!layer.idleDB, calm, avoided: avoid.banned, load_ms: Math.round(performance.now() - t0),
    squat: (act) => squat.start(act), lean };
}

/**
 * 趴着 (宿主让页面把地面当栏杆趴上去): 上身前倾压在栏杆上, 两条前臂叠放在身前、横在栏杆上 (左手搭到右肘、右手搭到左肘),
 * 头抬回来看着用户. 腿不管 (藏在栏杆后面, 页面把栏杆线以下裁掉).
 * 和蹲下一样每帧从行动层刚写好的姿态重新算. 栏杆线 = 前臂底下 (rail, 世界坐标), 页面把它投到屏幕上报给宿主.
 * 趴着时的小动作 (poke): tilt 歪头, wave 抬起右手招一招.
 */
function leaner(layer, node, add) {
  const IN_S = 0.45, OUT_S = 0.3;  // 趴下、站起的时间常数 (秒)
  const spine = node('spine'), chest = node('chest') || node('upperChest'), neck = node('neck'), head = node('head');
  // base: 先摆一个大致的叠臂姿势 (上臂往前、往外下方, 前臂往里横过身前), 再用两骨骼 IK 把手对准.
  // IK 在手臂已经弯着时沿用现在的弯曲平面, 不先摆的话手肘会照待机的样子朝后下方, 前臂斜着戳上去
  const arms = Object.fromEntries([['L', 'left', 1], ['R', 'right', -1]].map(([key, s, out]) =>
    [key, { shoulder: node(`${s}Shoulder`), upper: node(`${s}UpperArm`), lower: node(`${s}LowerArm`), hand: node(`${s}Hand`), out,
      base: { upper: q(0, -out * rad(55), -out * rad(30)), lower: q(0, -out * rad(110), 0) } }]));
  const st = { on: false, w: 0, t: 0, tilt: null, wave: null, anchor: null, miss: {}, reach: {}, reach0: 0 };
  const rail = new THREE.Vector3(), tv = new THREE.Vector3();
  const up = new THREE.Vector3(0, 1, 0);
  const smooth = (x) => { const k = clamp(x); return k * k * (3 - 2 * k); };
  // 一段小动作的包络: 0 -> 1 -> 0
  const env = (a, dur) => (a ? clamp(Math.min((st.t - a.start) / 0.25, (a.start + dur - st.t) / 0.35)) : 0);

  function apply(w, dt) {
    const k = smooth(w);
    // 上身: 腰、胸往前压 (共约 45°), 头抬回来看着用户; 歪头是小动作
    add(spine, q(rad(24) * k, 0, 0), 1);
    add(chest, q(rad(20) * k, 0, 0), 1);
    const tilt = st.tilt ? env(st.tilt, st.tilt.dur) * st.tilt.dir : 0;
    add(neck, q(-rad(30) * k, 0, rad(14) * tilt), 1);
    add(head, q(-rad(8) * k, 0, rad(6) * tilt), 1);
    for (const a of Object.values(arms)) a.shoulder?.quaternion.slerp(identity, k);  // 肩膀回到标准位置 (待机会耸肩、转肩)
    chest.updateWorldMatrix(true, true);
    // 栏杆: 两肩中点往前 24 cm、往下 20 cm; 两只手各搭到另一边 (前臂横着叠在一起, 左臂在上)
    const fwd = layer._forward(), left = new THREE.Vector3(fwd.z, 0, -fwd.x);  // 角色的左边
    const shoulders = arms.L.upper.getWorldPosition(new THREE.Vector3()).add(arms.R.upper.getWorldPosition(tv)).multiplyScalar(0.5);
    // 各个尺寸按这个角色的手臂长度 (上臂 + 前臂) 算: 手臂短的角色 (wendy 0.33 m, miomio 0.42 m) 照同样的米数会够不着,
    // IK 就把胳膊伸直指向镜头
    if (!st.reach0) {
      const p = (b) => b.getWorldPosition(new THREE.Vector3());
      st.reach0 = p(arms.L.upper).distanceTo(p(arms.L.lower)) + p(arms.L.lower).distanceTo(p(arms.L.hand)) || 0.4;
    }
    const R = st.reach0;
    const center = shoulders.clone().addScaledVector(fwd, 0.52 * R).addScaledVector(up, -0.46 * R);
    // 趴好之后栏杆几乎不动: 待机换重心、歪头时肩膀会动, 跟着肩膀算的话栏杆线会上下晃 (宿主跟着挪窗口, 整个人一跳一跳的).
    // 只慢慢跟着肩膀走 (时间常数 4 秒): 小动作带不动它; 趴好那一刻碰巧姿势不对 (实测 wendy 有一次栏杆高了 9 cm) 也能自己回来.
    // 手用 IK 去够它, 身体在后面轻轻晃
    if (st.on && w > 0.99) {
      if (!st.anchor) st.anchor = center.clone();
      else st.anchor.lerp(center, 1 - Math.exp(-dt / 4));
      center.copy(st.anchor);
    } else if (!st.on) {
      st.anchor = null;
    }
    rail.copy(center).addScaledVector(up, -0.11 * R);  // 前臂底下
    const wave = env(st.wave, st.wave?.dur || 0);
    for (const [key, a] of Object.entries(arms)) {
      if (!a.upper || !a.hand) continue;
      a.upper.quaternion.slerp(a.base.upper, k);
      a.lower.quaternion.slerp(a.base.lower, k);
      a.upper.updateWorldMatrix(true, true);
      // 搭到另一边 (手落在对面的胳膊肘附近; 伸太远的话肩窄的角色手会从对面袖子外面戳出来)
      const target = center.clone().addScaledVector(left, -a.out * 0.18 * R)
        .addScaledVector(up, key === 'L' ? 0.085 * R : 0).addScaledVector(fwd, key === 'L' ? 0.05 * R : 0);
      if (key === 'R' && wave > 0) {  // 招手: 右手抬起来, 手肘还撑在胳膊上, 前臂竖着在脸的外侧前面左右摆
        const swing = Math.sin((st.t - st.wave.start) * 11) * 0.1 * R;
        target.lerp(center.clone().addScaledVector(left, -0.48 * R + swing).addScaledVector(up, 0.48 * R).addScaledVector(fwd, 0.2 * R), wave);
      }
      // 手肘往外、略往下 (前臂差不多横着)
      layer._twoBone(a.upper, a.lower, a.hand, hand.lerp(target, k), left.clone().multiplyScalar(a.out).addScaledVector(up, -0.35));
      if (k > 0.99) {  // 自检: 手离目标还差多少 (够不着时 IK 会把胳膊伸直指向目标)
        a.lower.updateWorldMatrix(true, true);
        st.miss[key] = +a.hand.getWorldPosition(tv).distanceTo(target).toFixed(3);
        st.reach[key] = +(a.upper.getWorldPosition(new THREE.Vector3()).distanceTo(a.lower.getWorldPosition(tv)) +
          a.lower.getWorldPosition(new THREE.Vector3()).distanceTo(a.hand.getWorldPosition(tv))).toFixed(3);
      }
      // 手掌顺着前臂平放
      a.lower.updateWorldMatrix(true, false);
      a.hand.quaternion.slerp(identity, k);
      a.hand.updateWorldMatrix(false, true);
    }
  }

  return {
    get on() { return st.on; },
    get w() { return st.w; },
    /** 自检: 两只手离目标多远、胳膊多长 (米), 肩宽. */
    get debug() {
      const sw = arms.L.upper.getWorldPosition(new THREE.Vector3()).distanceTo(arms.R.upper.getWorldPosition(tv));
      return { miss: st.miss, reach: st.reach, shoulder_width: +sw.toFixed(3) };
    },
    rail,
    /** 宿主说趴下 / 站起来. */
    set(on) { st.on = !!on; },
    /** 趴着时的小动作: tilt (歪头, 左右交替) 或 wave (抬手招一招). 返回要多久 (秒). */
    poke(kind) {
      if (kind === 'wave') { st.wave = { start: st.t, dur: 1.6 }; return 1.6; }
      st.tilt = { start: st.t, dur: 1.8, dir: st.tilt?.dir === 1 ? -1 : 1 };
      return 1.8;
    },
    update(dt) {
      st.t += dt;
      const goal = st.on ? 1 : 0;
      st.w += (goal - st.w) * (1 - Math.exp(-dt / (goal > st.w ? IN_S : OUT_S)));
      if (Math.abs(goal - st.w) < 1e-3) st.w = goal;
      if (st.w > 1e-3) apply(st.w, dt);
    },
  };
}

/**
 * 蹲下 (squat, 服务端的一次性动作: 蹲一会儿自己站起来; amp 0.5 是半蹲). 程序动作和 StayStill 都没有这个姿势,
 * 在行动层写完姿态之后叠上去: 髋部压低、略往后, 骨盆和上身前倾保持平衡, 头抬回来看着用户;
 * 两条腿用行动层的两骨骼 IK 把脚钉在原地 (膝盖朝前, 脚掌保持原来的朝向); 没在做手势的手搭到膝盖上方.
 * 每帧都从行动层刚写好的姿态重新算, 不会越叠越多. 返回 { start(act), update(dt), active }.
 */
function squatter(layer, node, weight, add) {
  const DOWN = 0.7, HOLD = 2.2, UP = 0.8;  // 秒 (spd 越大越快; 停留不变)
  const DEPTH = 0.42;                      // 全蹲时髋部降低的比例 (髋高的几成)
  const KNEE_IN = 0.65;                    // 膝盖往中线并拢的程度 (穿裙子的模型正对镜头蹲, 两膝分开会走光)
  const hips = node('hips'), spine = node('spine'), chest = node('chest') || node('upperChest'), neck = node('neck');
  const legs = ['left', 'right'].map((s) => ({ upper: node(`${s}UpperLeg`), lower: node(`${s}LowerLeg`), foot: node(`${s}Foot`) }));
  // out: 手肘往哪边撇 (角色朝 +z, 左手在 +x)
  const arms = Object.fromEntries([['L', 'left', 1], ['R', 'right', -1]].map(([key, s, out]) =>
    [key, { upper: node(`${s}UpperArm`), lower: node(`${s}LowerArm`), hand: node(`${s}Hand`), mid: node(`${s}MiddleProximal`), out }]));
  const st = { active: false, t: 0, amp: 1, spd: 1 };
  const smooth = (x) => { const k = clamp(x); return k * k * (3 - 2 * k); };
  const total = () => (DOWN + UP) / st.spd + HOLD;
  const curve = () => {
    const down = DOWN / st.spd, up = UP / st.spd;
    if (st.t < 0) return 0;
    if (st.t < down) return smooth(st.t / down);
    if (st.t < down + HOLD) return 1;
    return 1 - smooth((st.t - down - HOLD) / up);
  };
  const tq = new THREE.Quaternion(), tv = new THREE.Vector3();
  const saved = legs.map(() => ({ pos: new THREE.Vector3(), rot: new THREE.Quaternion() }));
  const local = (dir) => dir.applyQuaternion(hips.parent.getWorldQuaternion(tq).invert());

  // 每条腿髋关节到脚踝最近能多近: 膝盖弯到活动范围 (rom.js 的 ROM.knee.flex) 少 5 度时的距离. 只管这一头: 往下蹲、往后坐只会让距离变短;
  // 原来还管了'不超过腿长的 0.98', 站着时腿本来就接近伸直, 刚开始蹲的几帧被判成够不着, 加了帧间平滑以后脚反而被拉走 7 cm
  let reach = null;
  const legReach = () => legs.map((l) => {
    const A = l.upper.getWorldPosition(new THREE.Vector3()), K = l.lower.getWorldPosition(new THREE.Vector3()), F = l.foot.getWorldPosition(new THREE.Vector3());
    const a = A.distanceTo(K), b = K.distanceTo(F), flex = rad(ROM.knee.flex - 5);
    return { min: Math.sqrt(Math.max(0, a * a + b * b + 2 * a * b * Math.cos(flex))) };
  });

  function apply(w) {
    const H = layer.hipHeight || 0.8;
    reach ??= legReach();
    legs.forEach((l, i) => { l.foot.getWorldPosition(saved[i].pos); l.foot.getWorldQuaternion(saved[i].rot); });
    // 髋部: 往下, 并往后一点 (重心落在脚掌上方); 骨盆前倾. 方向换算到髋部父节点的坐标系
    const back = layer._forward().negate();
    const pose0 = [hips.position.clone(), hips.quaternion.clone(), spine?.quaternion.clone(), chest?.quaternion.clone(), neck?.quaternion.clone()];
    const lower = (k) => {
      hips.position.copy(pose0[0]); hips.quaternion.copy(pose0[1]);
      [spine, chest, neck].forEach((b, i) => b?.quaternion.copy(pose0[i + 2]));
      hips.position.add(local(new THREE.Vector3(0, -1, 0)).multiplyScalar(DEPTH * H * k));
      hips.position.add(local(back).multiplyScalar(0.16 * H * k));
      add(hips, q(rad(16) * k, 0, 0), 1);
      add(spine, q(rad(12) * k, 0, 0), 1);
      add(chest, q(rad(8) * k, 0, 0), 1);
      add(neck, q(-rad(26) * k, 0, 0), 1);  // 上身前倾了, 头抬回来, 还看着用户
      hips.updateWorldMatrix(false, true);
    };
    // 下蹲深度按腿夹住: 两条腿的髋关节到原来的落脚点都不能近于膝盖弯到头时的距离, 不然脚钉不住、被腿拉过去 (桌宠自检里蹲下脚挪 16-20 cm).
    // 够不着时二分找最深的那一档
    const fits = () => legs.every((l, i) => l.upper.getWorldPosition(tv).distanceTo(saved[i].pos) >= reach[i].min);
    const want = w * st.amp;
    let k = want;
    lower(k);
    if (!fits()) {
      let lo = 0, hi = k;
      for (let it = 0; it < 7; it++) { const m = (lo + hi) / 2; lower(m); if (fits()) lo = m; else hi = m; }
      k = lo;
      lower(k);
    }
    // 夹浅的量帧间平滑: 要多夹时马上跟上 (脚不被拉走), 能少夹时慢慢放 (半衰期 0.15 秒), 深度不一帧一跳.
    // 网页里量过: 一次蹲下只有 0-5 帧要夹, 最多夹 3%, 推开次数和不夹时一样 (0-38)
    st.cut = Math.max(want - k, (st.cut || 0) * Math.exp((-Math.LN2 * (st.dt || 1 / 60)) / 0.15));
    if (want - st.cut < k - 1e-4) { k = want - st.cut; lower(k); }
    st.depth = k;  // 这一帧实际蹲了几成 (调试用)
    // 腿: 脚钉在原地, 膝盖朝前并往中线并拢 (小腿外撇)
    const fwd = layer._forward(), left = new THREE.Vector3(fwd.z, 0, -fwd.x);  // 角色的左边
    legs.forEach((l, i) => {
      const inward = left.clone().multiplyScalar(i === 0 ? -KNEE_IN : KNEE_IN);  // legs[0] 是左腿
      layer._twoBone(l.upper, l.lower, l.foot, saved[i].pos, fwd.clone().add(inward).normalize());
      l.lower.updateWorldMatrix(true, false);
      l.foot.quaternion.copy(l.lower.getWorldQuaternion(tq).invert().multiply(saved[i].rot));
      l.foot.updateWorldMatrix(false, true);
    });
    // 手: 没在做手势的手搭到并拢的膝盖上 (膝盖 = 小腿的起点), 两只手一左一右挨着, 挡在前面; 手肘朝外后方
    const knees = legs[0].lower.getWorldPosition(new THREE.Vector3()).add(legs[1].lower.getWorldPosition(tv)).multiplyScalar(0.5);
    for (const [key, a] of Object.entries(arms)) {
      const free = 1 - clamp(weight[key] * 2);
      if (!a.upper || !a.hand || free <= 0) continue;
      // 手离膝盖多高: 大腿半径 + 手半径 + 1.5 cm (接触层量的). 以前固定 8 cm, 比大腿胶囊表面低 1 cm, 蹲着时每帧都被推开 (桌宠自检 164-177 次)
      const s = key === 'L' ? 'left' : 'right';
      const lift = (layer.thighRadius?.(s) ?? 0.065) + (layer.handRadius?.(s) ?? 0.02) + 0.015;
      const target = knees.clone().addScaledVector(left, a.out * 0.055).add(new THREE.Vector3(0, lift, 0)).addScaledVector(fwd, 0.04);
      // 手腕不进两条大腿: 蹲着时大腿是斜的, 膝盖上面固定的高度不够 (左手腕插进左大腿), 按大腿胶囊沿径向推到表面外面 2 cm
      legs.forEach((l, i) => {
        const A = l.upper.getWorldPosition(new THREE.Vector3()), B = l.lower.getWorldPosition(new THREE.Vector3());
        const ab = B.clone().sub(A), tt = clamp(target.clone().sub(A).dot(ab) / Math.max(1e-6, ab.lengthSq())), C = A.addScaledVector(ab, tt);
        const d = target.clone().sub(C), need = (layer.thighRadius?.(i === 0 ? 'left' : 'right') ?? 0.065) + (layer.handRadius?.(s) ?? 0.02) + 0.02;
        if (d.length() < need && d.lengthSq() > 1e-10) target.copy(C.addScaledVector(d.normalize(), need));
      });
      // 按关节角混 (手沿弧线放上去、拿下来): 以前混的是手腕的世界位置 (直线挪), 站起来那一段手从膝盖直线回到垂着的位置, 穿过骨盆
      // 手臂用行动层的 _armHinged (肘一定朝提示方向、按铰链弯, 自己按权重在关节角上混); 两骨 IK 会沿用待机片段里手臂的弯曲平面,
      // 片段不同肘的朝向就不同, 推开次数在 0-200 之间跳. 提示方向按角色朝向算 (以前用世界坐标, 默认角色朝 +z)
      const hint = left.clone().multiplyScalar(a.out * 0.6).add(new THREE.Vector3(0, -0.6, 0)).addScaledVector(fwd, -0.5);
      layer._armHinged(a.upper, a.lower, a.hand, target, hint, w * free, key === 'L' ? 'left' : 'right');
      // 掌心朝下、手指朝前略往下搭在膝盖上. 以前只摆了手腕的位置, 手的朝向还是待机片段里垂着的 (手指朝下), 指尖插进大腿,
      // 每帧被接触层推开 (桌宠自检 93-176 次, 网页无界面复现 366-370 次, 全是右大腿:指尖). 朝向由手指、掌心两个方向直接定
      if (a.mid) {
        a.lower.updateWorldMatrix(true, true);
        const hq = a.hand.getWorldQuaternion(new THREE.Quaternion()), hw = a.hand.getWorldPosition(new THREE.Vector3());
        const fl = a.mid.getWorldPosition(new THREE.Vector3()).sub(hw).applyQuaternion(hq.clone().invert()).normalize();
        const pl = new THREE.Vector3(0, -1, 0).addScaledVector(fl, fl.y).normalize();  // 手的局部坐标里掌心朝 -Y
        const D = fwd.clone().add(new THREE.Vector3(0, -0.15, 0)).addScaledVector(left, -a.out * 0.2).normalize();
        const N = new THREE.Vector3(0, -1, 0); N.addScaledVector(D, -N.dot(D)).normalize();
        const basis = (u, b) => new THREE.Matrix4().makeBasis(u, b, u.clone().cross(b));
        const rot = new THREE.Quaternion().setFromRotationMatrix(basis(D, N).multiply(basis(fl, pl).transpose()));
        a.hand.quaternion.slerp(a.hand.parent.getWorldQuaternion(new THREE.Quaternion()).invert().multiply(rot), w * free);
        a.hand.updateWorldMatrix(false, true);
      }
    }
  }

  return {
    get active() { return st.active; },
    /** act: 服务端的意图项 {do: 'squat', amp, spd, at}. 正蹲着时再来一次: 从现在的高度接着蹲, 停留重新计时 */
    start(act = {}) {
      const amp = clamp(Number(act.amp ?? 1) || 1, 0.3, 1), spd = clamp(Number(act.spd ?? 1) || 1, 0.5, 2);
      const was = st.active ? curve() : 0;
      Object.assign(st, { active: true, amp: Math.max(amp, was * st.amp), spd });
      st.t = was > 0 ? (DOWN / spd) * was : -(Number(act.at) || 0);
      return total() + Math.max(0, Number(act.at) || 0);  // 这个动作一共要多久 (秒), 页面据此保持高帧率
    },
    update(dt) {
      if (!st.active) { layer.squatting = 0; st.cut = 0; return; }
      st.t += dt;
      st.dt = dt;
      const w = curve();
      layer.squatting = w * st.amp;  // 告诉行动层蹲着 (armsBusy 的 seated: 接触层不按垂着的裙子推手)
      if (st.t >= total()) { st.active = false; layer.squatting = 0; return; }
      if (w > 1e-3) apply(w);
    },
  };
}

/**
 * 避开会压进衣服的待机片段: 一段待机播了 3 秒以上, 两只手同时在身体正前方 (比髋部靠前 5 cm 以上、离中线 16 cm 以内、
 * 髋下 30 cm 到髋上 45 cm, 也就是从大腿到胸口) 的帧数超过 FRONT_SHARE, 就把它从候选里去掉、马上换一段.
 * 去掉的片段按模型记在本机 (localStorage), 下次启动直接跳过. 返回每帧调用的函数 (gesturing: 正在做手势, 这时不算).
 */
function idleAvoider(v, layer) {
  const db = layer.idleDB, list = db?.groups?.idle;
  // v3 (2026-10-26): 交叠、表情动作不再算; 以前 v2 里因为交叠被误去掉的段不带过来, 真压在身前的段会重新认出来
  // v5 (2026-10-27): 两只手都在身前还不够, 这一帧接触层真的把手臂推出身体 (PRESS_M 以上) 才算"压着".
  //   v3 把动捕自己两手合在腹前的 idle_08 (最平静的一段, 手没插进衣服, 推开 0 次) 也去掉了; 按接触层判断, 换衣服厚的模型也对.
  //   (v4 还算了裙子那一项, 开发版里试的时候照样去掉了 idle_08, 只在开发版的数据里用过)
  const key = `pet-idle-avoid-v5:${v.info?.name || 'vrm'}`;
  const banned = new Set();
  const check = () => {};
  const h = v.vrm.humanoid;
  const hips = h.getRawBoneNode('hips'), hands = [h.getRawBoneNode('leftHand'), h.getRawBoneNode('rightHand')];
  if (!list || !hips || !hands[0] || !hands[1]) return Object.assign(check, { banned: [] });
  const hp = new THREE.Vector3(), hd = new THREE.Vector3();
  const inFront = (hand) => {  // 角色朝向镜头 (+z)
    hips.getWorldPosition(hp);
    hand.getWorldPosition(hd);
    return hd.z - hp.z > 0.05 && Math.abs(hd.x - hp.x) < 0.16 && hd.y - hp.y < 0.45 && hd.y - hp.y > -0.3;
  };
  try {
    for (const c of JSON.parse(localStorage.getItem(key) || '[]')) banned.add(c);
  } catch { /* 存储不可用: 只在这次运行里避开 */ }
  const keep = (c) => !banned.has(c);
  if (list.filter(keep).length >= MIN_IDLE_CLIPS) list.splice(0, list.length, ...list.filter(keep));
  let cur = null, t0 = 0, frames = 0, front = 0;
  const fn = (gesturing) => {
    const st = layer.idle, now = performance.now() / 1000;
    const stats = v.contact?.stats;
    const corrected = !stats || (stats.lastPush || 0) > PRESS_M;  // 没有接触层: 照旧只看位置
    if (!st || st.kind !== 'idle' || gesturing) { cur = null; return; }
    if (cur !== st.clip) { cur = st.clip; t0 = now; frames = front = 0; return; }
    frames++;
    if (inFront(hands[0]) && inFront(hands[1]) && corrected) front++;
    if (now - t0 < 3) return;
    if (front / frames > FRONT_SHARE && list.length > MIN_IDLE_CLIPS) {
      banned.add(cur);
      list.splice(list.indexOf(cur), 1);
      try { localStorage.setItem(key, JSON.stringify([...banned])); } catch { /* 同上 */ }
      layer._idleRandom(now, cur);
      cur = null;
    } else if (now - t0 > 8) {  // 滚动窗口: 一段很长的待机里只看最近 8 秒
      t0 = now;
      frames = front = 0;
    }
  };
  layer.avoided = banned;  // 自检读这个看去掉了哪些
  return Object.assign(fn, { banned: [...banned] });
}
