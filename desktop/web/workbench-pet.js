// 工作台里的她 (2026-10-26 用户: "打开工作台能不能把桌宠放进来呢? 就是不显示小窝或者全身", 参考 dsh-pet-live2d 的桌前小人):
// 侧栏底下的名片上面一张"书桌": 和桌宠同一个 VRM 角色 (服务端的默认角色), 只露上半身, 坐在品牌色的键盘后面.
//   - 跟着工作台的进度: 等你 (安静, 眼睛跟着鼠标) / 你在打字 (看输入框, 你敲一下她也敲一下) / 想 (抬眼) /
//     写回复、跑工具 (低头敲键盘, 气泡说在做什么) / 要你点头 (看着你) / 做完 (笑、点头) / 没成 (难过);
//   - 摸头: 鼠标在她头上来回蹭几下会害羞; 点她一下有小反应;
//   - 省: 窗口看不见时不画, 收起来就整个卸掉 (显存紧, TTS 占着); 贴图、表情变形按桌宠的做法压 (avatar-lean.js); 平时 24 帧, 动起来 30 帧.
// 身体不挂 HAST-SEC 和行动层: 躯干、脖子用 avatar-vrm.js 的程序动作通道 (ch); 桌子和键盘是 3D 的 (挡住桌子下面的身体),
// 两只手用两段骨骼 IK 搭在键盘上 (肘按铰链方向弯, 和 locomotion.js 的 _armHinged 一个做法), 敲哪个键手就落到哪个键上.
import * as THREE from 'three';
import { freeTextureSources, loadLeanAvatar } from '/pet/avatar-lean.js';
import { bodyColliders, realtimeSprings } from '/pet/springs.js';

const OPTS = { tex: 512, morph: 'compact-nonormal', aa: 1, pr: 1.5 };
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const ease = (cur, goal, dt, tau) => cur + (goal - cur) * (1 - Math.exp(-dt / tau));
const deg = (r) => (r * 180) / Math.PI;
// 坐姿 (程序动作通道, 角度): 前倾 lean, 低头 hp, 转头 hy, 歪头 hr. 手臂通道 (ls/le…) 被下面的 IK 盖掉, 随便给
const SIT = { ls: 20, rs: 20, le: 90, re: 90, lean: 3, hp: 5, hy: 0, hr: 0 };
// 每个阶段: 表情, 气泡, 姿态的改动, 眼睛 (mouse = 跟鼠标, keys = 看键盘, up = 抬眼想, input = 看输入框, you = 看着你)
const PHASE = {
  idle: { emo: ['calm', 0.5], say: '', pose: {}, eyes: 'mouse' },
  user: { emo: ['neutral', 0.6], say: '', pose: { lean: 4, hp: 3 }, eyes: 'input' },
  think: { emo: ['calm', 0.7], say: '想一想…', pose: { lean: 1, hp: -3, hr: 5 }, eyes: 'up' },
  write: { emo: ['happy', 0.45], say: '在写…', pose: { lean: 6, hp: 11 }, eyes: 'keys', type: 8 },
  tool: { emo: ['neutral', 0.6], say: '', pose: { lean: 6, hp: 12 }, eyes: 'keys', type: 6 },
  ask: { emo: ['surprised', 0.55], say: '等你点头 ✋', pose: { lean: 2, hp: 0, hr: -4 }, eyes: 'you' },
  done: { emo: ['happy', 0.9], say: '好了!', pose: { lean: 2, hp: 1 }, eyes: 'mouse', nod: true },
  error: { emo: ['sad', 0.7], say: '没成…', pose: { lean: 1, hp: 8, hr: 3 }, eyes: 'mouse' },
};
const POKES = [['happy', '在呢~'], ['shy', '干嘛呀'], ['surprised', '嗯?']];
// 键盘: 四排键 + 空格 (按她看过去从左到右; 她的左边是 +X). 尺寸是米, 和角色一个比例
const ROWS = ['1234567890', 'QWERTYUIOP', 'ASDFGHJKL', 'ZXCVBNM'];
const KB = { w: 0.34, d: 0.125, h: 0.014, pitch: 0.03, key: 0.025, keyH: 0.008 };
const ARM = { left: ['leftUpperArm', 'leftLowerArm', 'leftHand', 'leftMiddleProximal'],
  right: ['rightUpperArm', 'rightLowerArm', 'rightHand', 'rightMiddleProximal'] };
const css = (el, name, fallback) => getComputedStyle(el).getPropertyValue(name).trim() || fallback;

/** 桌子 + 键盘 (底座天蓝 -> 淡紫的渐变, 白色键帽). 返回 { group, keys: [{ k, x, z }], press(i), update(dt) }. */
function buildDesk(el, at) {
  const g = new THREE.Group();
  const panel = new THREE.Color(css(el, '--panel', '#ffffff'));
  const dark = panel.getHSL({}).l < 0.4;
  const b1 = new THREE.Color(css(el, '--b1', '#8fb4f2')), b2 = new THREE.Color(css(el, '--b2', '#d9b2f0'));
  // 桌面: 从她身前一直铺到镜头外面
  const desk = new THREE.Mesh(new THREE.BoxGeometry(1.6, 0.04, 0.9),
    new THREE.MeshStandardMaterial({ color: dark ? new THREE.Color('#2b2a38') : new THREE.Color('#f4f1fb'), roughness: 0.85 }));
  desk.position.set(0, at.top - KB.h - 0.02, at.deskNear + 0.45);
  g.add(desk);
  // 键盘底座: 按 x 渐变
  const base = new THREE.BoxGeometry(KB.w, KB.h, KB.d, 8, 1, 1);
  const pos = base.attributes.position, col = [];
  for (let i = 0; i < pos.count; i++) {
    const c = b2.clone().lerp(b1, (pos.getX(i) + KB.w / 2) / KB.w);  // 看过去左边 (她的右边) 淡紫, 右边天蓝
    col.push(c.r, c.g, c.b);
  }
  base.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
  const kb = new THREE.Mesh(base, new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.45, metalness: 0.05 }));
  kb.position.set(0, at.top - KB.h / 2, at.near + KB.d / 2);
  g.add(kb);
  // 键帽
  const keys = [];
  ROWS.forEach((row, r) => {
    const n = row.length, x0 = ((n - 1) / 2) * KB.pitch + r * 0.004;
    for (let c = 0; c < n; c++) keys.push({ k: row[c], x: x0 - c * KB.pitch, z: at.near + 0.016 + r * 0.025 });
  });
  keys.push({ k: ' ', x: 0, z: at.near + 0.016 + 4 * 0.025, space: true });
  const mat = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 0.35 });
  const caps = new THREE.InstancedMesh(new THREE.BoxGeometry(KB.key, KB.keyH, 0.021), mat, keys.length);
  const m = new THREE.Matrix4(), q = new THREE.Quaternion(), white = new THREE.Color(dark ? '#d9d6ea' : '#ffffff');
  const glow = b2.clone().lerp(new THREE.Color('#ffffff'), 0.25), tmp = new THREE.Color();
  const down = new Float32Array(keys.length);
  const place = (i) => {
    const k = keys[i];
    m.compose(new THREE.Vector3(k.x, at.top + KB.keyH / 2 - down[i] * 0.004, k.z), q, new THREE.Vector3(k.space ? 4.4 : 1, 1, 1));
    caps.setMatrixAt(i, m);
    caps.setColorAt(i, tmp.copy(white).lerp(glow, down[i]));
  };
  keys.forEach((_, i) => place(i));
  g.add(caps);
  return {
    group: g, keys,
    press(i) { down[i] = 1; },
    update(dt) {
      let any = false;
      for (let i = 0; i < keys.length; i++) {
        if (!down[i]) continue;
        down[i] = down[i] < 0.02 ? 0 : down[i] * Math.exp(-dt / 0.09);
        place(i);
        any = true;
      }
      if (any) { caps.instanceMatrix.needsUpdate = true; caps.instanceColor.needsUpdate = true; }
    },
  };
}

const world = (n) => n.getWorldPosition(new THREE.Vector3());
const worldQ = (n) => n.getWorldQuaternion(new THREE.Quaternion());
/** 让 node 绕 pivot 转, 使 cur 那一点指向 goal (世界坐标). */
function aim(node, pivot, cur, goal) {
  const q = new THREE.Quaternion().setFromUnitVectors(cur.clone().sub(pivot).normalize(), goal.clone().sub(pivot).normalize());
  node.quaternion.copy(worldQ(node.parent).invert().multiply(q.multiply(worldQ(node))));
  node.updateWorldMatrix(false, true);
}
/** 两段骨骼: 手腕放到 target, 肘朝 hint 那边; 上臂绕自身拧到铰链轴 = 弯曲平面的法线 (右上臂局部 +Y, 左 -Y); 手掌朝下、手指朝 fingers. */
function armIK(nodes, side, target, hint, fingers) {
  const [up, lo, hand, mid] = nodes;
  const S = world(up), E0 = world(lo), H0 = world(hand);
  const a = S.distanceTo(E0), b = E0.distanceTo(H0);
  const d = target.clone().sub(S), L = clamp(d.length(), 0.01, a + b - 0.002);
  d.normalize();
  const cosA = clamp((a * a + L * L - b * b) / (2 * a * L), -1, 1);
  const p = hint.clone().addScaledVector(d, -hint.dot(d)).normalize();
  const E = S.clone().addScaledVector(d, a * cosA).addScaledVector(p, a * Math.sqrt(1 - cosA * cosA));
  const T = S.clone().addScaledVector(d, L);
  aim(up, S, E0, E);
  const u = E.clone().sub(S).normalize(), n = u.clone().cross(T.clone().sub(E)).normalize();
  const ax = new THREE.Vector3(0, side === 'left' ? -1 : 1, 0).applyQuaternion(worldQ(up));
  ax.addScaledVector(u, -ax.dot(u));
  if (ax.lengthSq() > 1e-8 && n.lengthSq() > 0.5) {
    const tw = new THREE.Quaternion().setFromUnitVectors(ax.normalize(), n);
    up.quaternion.copy(worldQ(up.parent).invert().multiply(tw.multiply(worldQ(up))));
    up.updateWorldMatrix(false, true);
  }
  aim(lo, world(lo), world(hand), T);
  if (mid) {
    // 手: 先让手指朝前下方, 再绕手指方向拧到手掌朝下 (手背的法线朝上)
    const W = world(hand);
    aim(hand, W, world(mid), W.clone().add(fingers));
    const f = fingers.clone().normalize();
    const back = new THREE.Vector3(0, 1, 0).applyQuaternion(worldQ(hand));
    const up2 = new THREE.Vector3(0, 1, 0).addScaledVector(f, -f.y).normalize();
    back.addScaledVector(f, -back.dot(f)).normalize();
    if (back.lengthSq() > 0.5) {
      const tw = new THREE.Quaternion().setFromUnitVectors(back, up2);
      hand.quaternion.copy(worldQ(hand.parent).invert().multiply(tw.multiply(worldQ(hand))));
      hand.updateWorldMatrix(false, true);
    }
  }
}

// 2D 门面 (Live2D, 和桌宠 face=live2d 时同一个模型): 2026-10-27 用户: "工作台左侧的桌宠还不能切换成2d的".
// 只露上半身 (live2d-avatar.js 的 frame=bust), 没有键盘和手: 各阶段换表情、低头抬眼、写回复和跑工具时头随着"敲字"轻轻点
const LIVE2D_MODEL = '/pet/live2d-models/Eustia/';
const EYES_2D = { up: [0, 16], keys: [0, -16], input: [22, -14] };  // 看哪儿 (度: 往右、往上); mouse = 跟鼠标, you = 看着你

async function mountDesk2D(el, { name }) {
  el.innerHTML = '<div class="stage"></div><div class="bubble" aria-live="polite"></div>';
  const stage = el.querySelector('.stage'), bubble = el.querySelector('.bubble');
  let v;
  try {
    const { loadLive2DAvatar } = await import('/pet/live2d-avatar.js');
    v = await loadLive2DAvatar(stage, LIVE2D_MODEL, { pixelRatio: 1.5, bubbleRoom: 0.02, frame: 'bust' });
  } catch (err) {
    console.warn('工作台的她 (2D) 没加载成', err);
    el.innerHTML = '';
    return null;
  }
  el.setAttribute('aria-label', `${name} (2D)`);
  const S = { phase: 'idle', t: 0, last: 0, drawn: 0, raf: 0, paused: false, back: 0, pokes: 0, mouse: null, busyUntil: 0 };
  const ch = { hp: 0, hy: 0, hr: 0, lean: 0 };
  const face = { viseme: { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0, level: 0 }, arkit: null };
  const say = (text, ms = 0) => {
    bubble.textContent = text || '';
    bubble.classList.toggle('on', !!text);
    clearTimeout(say.t);
    if (ms) say.t = setTimeout(() => say(PHASE[S.phase].say), ms);
  };
  const frame = (now) => {
    S.raf = requestAnimationFrame(frame);
    if (now - S.drawn < 1000 / 30) return;  // 最多 30 帧
    const dt = Math.min(0.1, (now - (S.last || now)) / 1000);
    S.last = S.drawn = now;
    S.t += dt;
    const p = PHASE[S.phase];
    const tap = p.type ? Math.sin(S.t * 9) * 1.4 : 0;  // 写、跑工具: 头随着"敲字"轻轻点
    ch.hp = ease(ch.hp, (p.pose.hp || 0) * 0.6 + tap, dt, 0.25);
    ch.hr = ease(ch.hr, p.pose.hr || 0, dt, 0.4);
    ch.lean = ease(ch.lean, p.pose.lean || 0, dt, 0.4);
    if (p.eyes === 'mouse' && S.mouse) v.follow(...S.mouse);
    else if (EYES_2D[p.eyes]) v.follow(...EYES_2D[p.eyes]);
    else v.follow(null);
    v.apply(ch, face, dt);
  };
  const run = () => { if (!S.raf && !S.paused && !document.hidden) { S.last = 0; S.raf = requestAnimationFrame(frame); } };
  const halt = () => { cancelAnimationFrame(S.raf); S.raf = 0; };
  const onVis = () => (document.hidden ? halt() : run());
  const onMove = (e) => {  // 鼠标在哪 (相对她的脸): 往右为正、往上为正, 度
    const r = stage.getBoundingClientRect(), [hx, hy] = v.headScreen();
    S.mouse = [clamp((e.clientX - r.left - hx) / 9, -30, 30), clamp(-(e.clientY - r.top - hy) / 9, -22, 22)];
  };
  const onLeave = () => { S.mouse = null; };
  const onClick = () => {
    const [emo, text] = POKES[S.pokes++ % POKES.length];
    v.setEmotion(emo);
    v.headAction('nod', { amplitude: emo === 'happy' ? 0.4 : 0.2, repetitions: 1 });
    say(text, 1800);
    setTimeout(() => v.setEmotion(PHASE[S.phase].emo[0]), 2200);
  };
  document.addEventListener('visibilitychange', onVis);
  addEventListener('mousemove', onMove, { passive: true });
  document.addEventListener('mouseleave', onLeave);
  stage.addEventListener('click', onClick);
  const api = {
    face: 'live2d',
    phase(name, text) {
      if (!PHASE[name]) return;
      clearTimeout(S.back);
      const changed = name !== S.phase;
      S.phase = name;
      const p = PHASE[name];
      if (changed) { v.setEmotion(p.emo[0]); if (p.nod) v.headAction('nod', { amplitude: 0.45, repetitions: 2 }); }
      if (text !== undefined || changed) say(text ?? p.say);
      if (name === 'done' || name === 'error') S.back = setTimeout(() => api.phase('idle'), name === 'done' ? 3500 : 6000);
    },
    typed() {  // 你在输入框里打字: 她看过去
      if (S.phase === 'idle' || S.phase === 'user') {
        if (S.phase === 'idle') api.phase('user');
        clearTimeout(S.back);
        S.back = setTimeout(() => S.phase === 'user' && api.phase('idle'), 2500);
      }
    },
    get phaseName() { return S.phase; },
    pause(on) { S.paused = on; on ? halt() : run(); },
    dispose() {
      halt();
      clearTimeout(S.back); clearTimeout(say.t);
      document.removeEventListener('visibilitychange', onVis);
      removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseleave', onLeave);
      stage.querySelector('canvas')?.getContext('webgl2')?.getExtension('WEBGL_lose_context')?.loseContext();
      el.innerHTML = '';
    },
  };
  v.setEmotion(PHASE.idle.emo[0]);
  run();
  return api;
}

/**
 * 在 el 里放她. 返回 { phase(name, text), typed(key), dispose() }; 角色没加载成 (没有 VRM、WebGL 不行) 时返回 null.
 * 页面看不见 (窗口最小化、被别的窗口挡住、切走) 时自动停画. face: 'vrm' (3D, 默认) 或 'live2d' (2D).
 */
export async function mountDesk(el, { name = '她', face: look = 'vrm' } = {}) {
  if (look === 'live2d') return mountDesk2D(el, { name });
  el.innerHTML = '<div class="stage"></div><div class="bubble" aria-live="polite"></div>';
  const stage = el.querySelector('.stage'), bubble = el.querySelector('.bubble');
  let list;
  try { list = await (await fetch('/api/avatars')).json(); } catch { return null; }
  if (!list?.default) return null;
  let v;
  try {
    ({ avatar: v } = await loadLeanAvatar(stage, `/avatars/${list.default}`, OPTS));
  } catch (err) {
    console.warn('工作台的她没加载成', err);
    return null;
  }
  realtimeSprings(v);
  bodyColliders(v);
  el.setAttribute('aria-label', `${name}坐在键盘后面`);

  // 量一下她 (静止姿势): 肩膀、上臂和前臂多长; 键盘放在前臂自然向前伸到的地方 (肘在身体两侧往下一点)
  const h = v.vrm.humanoid;
  const nodes = Object.fromEntries(Object.entries(ARM).map(([side, names]) => [side, names.map((n) => h.getNormalizedBoneNode(n))]));
  const rig = h.normalizedHumanBonesRoot || v.vrm.scene;
  v.vrm.scene.updateMatrixWorld(true);
  const sL = world(nodes.left[0]), sR = world(nodes.right[0]);
  const ua = sL.distanceTo(world(nodes.left[1])), fa = world(nodes.left[1]).distanceTo(world(nodes.left[2]));
  const sy = (sL.y + sR.y) / 2, sz = (sL.z + sR.z) / 2;
  const at = { top: sy - ua * 0.82, near: sz + ua * 0.45 + fa * 0.62, deskNear: sz + 0.08 };
  const desk = buildDesk(el, at);
  v.scene.add(desk.group);
  // 手腕平时的位置 (手指搭在中间那排键上); 肘往外、往下、往后
  const home = { left: new THREE.Vector3(0.075, at.top + 0.03, at.near - 0.004), right: new THREE.Vector3(-0.075, at.top + 0.03, at.near - 0.004) };
  const HINT = { left: new THREE.Vector3(0.7, -1, -0.5), right: new THREE.Vector3(-0.7, -1, -0.5) };
  const FING = { left: new THREE.Vector3(-0.12, -0.38, 1), right: new THREE.Vector3(0.12, -0.38, 1) };
  const wrist = { left: home.left.clone(), right: home.right.clone() };
  const press = { left: null, right: null };
  const vrmUpdate = v.vrm.update.bind(v.vrm);
  v.vrm.update = (dt) => {  // 程序动作把躯干、脖子写完之后、骨骼传给模型之前: 手放到键盘上
    rig.updateMatrixWorld(true);
    for (const side of ['left', 'right']) if (nodes[side][2]) armIK(nodes[side], side, wrist[side], HINT[side], FING[side]);
    vrmUpdate(dt);
  };

  // 取景: 头顶 (头发、耳朵) 到键盘前沿都在画面里, 镜头在桌子对面稍高的地方往下看
  const head = v.headPos.clone();
  const box = new THREE.Box3().setFromObject(v.vrm.scene);
  const topY = Math.min(box.max.y, head.y + 0.34) + 0.01;
  const pts = [new THREE.Vector3(0, topY, head.z), new THREE.Vector3(KB.w / 2 + 0.03, at.top, at.near + KB.d + 0.01),
    new THREE.Vector3(-KB.w / 2 - 0.03, at.top, at.near + KB.d + 0.01), new THREE.Vector3(sL.x + 0.08, sy, sz), new THREE.Vector3(sR.x - 0.08, sy, sz)];
  const base = v._resize.bind(v);
  v._resize = () => {
    base();
    const cam = v.camera, look = new THREE.Vector3(0, (topY + at.top) / 2 - 0.03, (head.z + at.near) / 2);
    const dir = new THREE.Vector3(0, 0.42, 1).normalize(), q = new THREE.Vector3();
    for (let d = 0.6; d < 5; d += 0.02) {
      cam.position.copy(look).addScaledVector(dir, d);
      cam.lookAt(look);
      cam.updateMatrixWorld();
      if (pts.every((p) => { q.copy(p).project(cam); return Math.abs(q.x) < 0.95 && q.y < 0.95 && q.y > -0.88; })) break;
    }
  };
  v._resize();

  const S = { phase: 'idle', say: '', t: 0, last: 0, raf: 0, frames: 0, freed: false, hidden: false, pose: { ...SIT },
    nextTap: 0, hand: 'left', mouse: null, mouseAt: 0, glanceAt: 0, userAt: 0, back: 0, pat: { d: 0, at: 0, last: null, cool: 0 }, pokes: 0, busyUntil: 0 };
  const ch = { ...SIT };
  const face = { viseme: { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0, level: 0 }, arkit: null };
  const neck = { roll: 0, pitch: 0 };
  const eye = new THREE.Vector3(), goal3 = new THREE.Vector3();

  const say = (text, ms = 0) => {
    S.say = text || '';
    bubble.textContent = S.say;
    bubble.classList.toggle('on', !!S.say);
    clearTimeout(say.t);
    if (ms) say.t = setTimeout(() => say(PHASE[S.phase].say), ms);
  };
  // 敲一下: 那个键 (认得出来的字母就是那个字母, 不然随便一个), 她的左边 (+X) 归左手; 空格两只手轮流
  const tap = (k) => {
    let i = k ? desk.keys.findIndex((x) => x.k === k.toUpperCase()) : -1;
    if (i < 0) i = Math.floor(Math.random() * desk.keys.length);
    const key = desk.keys[i];
    const side = key.space ? S.hand : key.x > 0 ? 'left' : 'right';
    S.hand = side === 'left' ? 'right' : 'left';
    press[side] = { key, at: S.t };
    desk.press(i);
    S.busyUntil = S.t + 1.5;
  };
  // 手腕: 敲的时候挪到那个键上方 (手指落在键上), 往下一点再抬起来; 停下 0.6 秒以后慢慢回到原位
  function hands(dt) {
    let dip = 0;
    for (const side of ['left', 'right']) {
      const pr = press[side], w = wrist[side];
      goal3.copy(home[side]);
      let down = 0;
      if (pr && S.t - pr.at < 0.6) {
        goal3.x += (pr.key.x - goal3.x) * 0.7;
        goal3.z += (pr.key.z - 0.062 - goal3.z) * 0.7;
        down = Math.max(0, 1 - Math.abs(S.t - pr.at - 0.045) / 0.075);
      }
      w.x = ease(w.x, goal3.x, dt, 0.05);
      w.z = ease(w.z, goal3.z, dt, 0.05);
      w.y = goal3.y - 0.013 * down + Math.sin(S.t * 1.5) * 0.002;
      dip += down;
    }
    return dip;
  }

  // 眼睛和头看向屏幕上的某一点 (页面坐标): 算出相对她眼睛的角度, glance 一小会儿; 头跟着转一点
  const gazeAt = (x, y, seconds = 0.9) => {
    const r = stage.getBoundingClientRect();
    eye.copy(head).setY(head.y + 0.07).project(v.camera);
    const ex = r.left + ((eye.x + 1) / 2) * r.width, ey = r.top + ((1 - eye.y) / 2) * r.height;
    const D = 520;  // 屏幕离她的"距离" (像素): 越大转得越少
    const yaw = clamp(deg(Math.atan2(x - ex, D)), -30, 30), pitch = clamp(-deg(Math.atan2(y - ey, D)), -20, 16);
    v.glance(yaw, pitch, seconds);
    return { yaw, pitch };
  };
  const centerOf = (sel) => { const r = document.querySelector(sel)?.getBoundingClientRect(); return r ? [r.left + r.width / 2, r.top + r.height / 2] : null; };
  let gaze = { yaw: 0, pitch: 0 };

  function eyes() {
    const p = PHASE[S.phase];
    if (S.t < S.glanceAt) return;
    const recent = S.mouse && S.t - S.mouseAt < 1.6;
    if (p.eyes === 'keys') {
      // 低头看键盘, 偶尔抬眼看一下对话区
      if (Math.random() < 0.18) { const c = centerOf('#chat'); if (c) gaze = gazeAt(...c, 0.8); S.glanceAt = S.t + 1.2; }
      else { v.glance(-4 + Math.random() * 8, -16, 1.4); gaze = { yaw: 0, pitch: -16 }; S.glanceAt = S.t + 1.1 + Math.random() * 0.8; }
    } else if (p.eyes === 'up') {
      const side = Math.random() < 0.5 ? -1 : 1;
      v.glance(side * (8 + Math.random() * 8), 12 + Math.random() * 5, 2);
      gaze = { yaw: side * 6, pitch: 8 };
      S.glanceAt = S.t + 2 + Math.random();
    } else if (p.eyes === 'input' || (S.t - S.userAt < 2 && !recent)) {
      const c = centerOf('#input');
      if (c) gaze = gazeAt(...c, 0.7);
      S.glanceAt = S.t + 0.5;
    } else if (recent) {
      gaze = gazeAt(...S.mouse, 0.7);
      S.glanceAt = S.t + 0.12;
    } else {
      v.lookAtUser();
      gaze = { yaw: 0, pitch: 0 };
      S.glanceAt = S.t + 0.6;
    }
  }

  function step(dt) {
    S.t += dt;
    const p = PHASE[S.phase];
    // 自己在敲 (写回复、跑工具): 一秒 p.type 下左右, 带点快慢
    if (p.type && S.t >= S.nextTap) { tap(); S.nextTap = S.t + (0.5 + Math.random()) / p.type; }
    eyes();
    const goal = { ...SIT, ...p.pose };
    goal.lean += Math.sin(S.t * 1.5) * 0.5;  // 呼吸
    goal.hy = gaze.yaw * 0.35;               // 看哪里头也跟着转一点
    goal.hp += -gaze.pitch * 0.25;
    for (const k of Object.keys(S.pose)) S.pose[k] = ease(S.pose[k], goal[k], dt, 0.35);
    Object.assign(ch, S.pose);
    ch.hp += 1.5 * hands(dt);  // 敲的时候头微微一点
    desk.update(dt);
    v.apply(ch, face, dt, neck);
    if (!S.freed && ++S.frames >= 6) { S.freed = true; freeTextureSources(v); }
  }

  function loop(now) {
    S.raf = requestAnimationFrame(loop);
    const fps = S.t < S.busyUntil || PHASE[S.phase].type ? 30 : 24;
    if (S.last && now - S.last < 1000 / fps - 3) return;
    const dt = S.last ? Math.min(0.1, (now - S.last) / 1000) : 1 / fps;
    S.last = now;
    step(dt);
  }
  const run = () => { if (!S.raf && !S.hidden && !S.paused) { S.last = 0; S.raf = requestAnimationFrame(loop); } };
  const halt = () => { cancelAnimationFrame(S.raf); S.raf = 0; };
  const onVis = () => { S.hidden = document.hidden; S.hidden ? halt() : run(); };
  document.addEventListener('visibilitychange', onVis);

  // 鼠标: 眼睛跟着; 在她头上来回蹭是摸头; 点一下有小反应
  const onMove = (e) => { S.mouse = [e.clientX, e.clientY]; S.mouseAt = S.t; if (S.t > S.glanceAt + 0.05) S.glanceAt = 0; };
  const onLeave = () => { S.mouse = null; };
  const headZone = (e) => {
    const r = stage.getBoundingClientRect();
    eye.copy(head).setY(head.y + 0.1).project(v.camera);
    const hx = r.left + ((eye.x + 1) / 2) * r.width, hy = r.top + ((1 - eye.y) / 2) * r.height;
    return Math.abs(e.clientX - hx) < r.width * 0.2 && e.clientY < hy + r.height * 0.06 && e.clientY > r.top;
  };
  const onStageMove = (e) => {
    const on = headZone(e);
    stage.classList.toggle('pat', on);
    const pt = S.pat;
    if (!on) { pt.last = null; return; }
    if (S.t - pt.at > 1.2) pt.d = 0;
    if (pt.last) pt.d += Math.hypot(e.clientX - pt.last[0], e.clientY - pt.last[1]);
    pt.last = [e.clientX, e.clientY];
    pt.at = S.t;
    if (pt.d > 260 && S.t > pt.cool) {
      pt.d = 0; pt.cool = S.t + 4;
      v.setEmotion('shy', 0.9);
      v.headAction('nod', { amplitude: 0.25, repetitions: 2 });
      say('嘿嘿…', 2400);
      S.busyUntil = S.t + 3;
      setTimeout(() => v.setEmotion(...PHASE[S.phase].emo), 2600);
    }
  };
  const onClick = () => {
    const [emo, text] = POKES[S.pokes++ % POKES.length];
    v.setEmotion(emo, 0.85);
    if (emo === 'happy') v.headAction('nod', { amplitude: 0.4, repetitions: 1 });
    if (emo === 'shy') { v.glance(16, -6, 1.2); S.glanceAt = S.t + 1.2; }
    say(text, 1800);
    S.busyUntil = S.t + 2.5;
    setTimeout(() => v.setEmotion(...PHASE[S.phase].emo), 2200);
  };
  addEventListener('mousemove', onMove, { passive: true });
  document.addEventListener('mouseleave', onLeave);
  stage.addEventListener('pointermove', onStageMove);
  stage.addEventListener('pointerleave', () => stage.classList.remove('pat'));
  stage.addEventListener('click', onClick);

  const api = {
    face: 'vrm',
    phase(name, text) {
      if (!PHASE[name]) return;
      clearTimeout(S.back);
      const changed = name !== S.phase;
      S.phase = name;
      const p = PHASE[name];
      if (changed) { v.setEmotion(...p.emo); S.glanceAt = 0; if (p.nod) v.headAction('nod', { amplitude: 0.45, repetitions: 2 }); }
      if (text !== undefined || changed) say(text ?? p.say);
      if (name === 'done' || name === 'error') S.back = setTimeout(() => api.phase('idle'), name === 'done' ? 3500 : 6000);
      S.busyUntil = S.t + 2;
    },
    /** 你在输入框里敲了一个键: 她看过去、跟着敲一下 */
    typed(key) {
      S.userAt = S.t;
      if (S.phase === 'idle' || S.phase === 'user') {
        if (S.phase === 'idle') api.phase('user');
        clearTimeout(S.back);
        S.back = setTimeout(() => S.phase === 'user' && api.phase('idle'), 2500);
      }
      tap(key && key.length === 1 ? key : null);
    },
    get phaseName() { return S.phase; },
    /** 看不见她的时候 (工作台打开了设置, 侧栏收起来) 停画 */
    pause(on) { S.paused = on; on ? halt() : run(); },
    dispose() {
      halt();
      clearTimeout(S.back); clearTimeout(say.t);
      document.removeEventListener('visibilitychange', onVis);
      removeEventListener('mousemove', onMove);
      document.removeEventListener('mouseleave', onLeave);
      v.dispose();
      el.innerHTML = '';
    },
  };
  v.setEmotion(...PHASE.idle.emo);
  onVis();
  run();
  return api;
}
