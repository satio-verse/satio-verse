// VRM 角色: 面部交给 SoullinkEmotion3D 的 FaceEngine (情绪 -> FACS 动作单元 -> 模型表情形变, 眨眼, 视线, 头部微动),
// 身体沿用 motion.js 的程序动作, 嘴部把我们按音频时钟算出的口型合进去.
// 口型来源: 服务端 A2F (按语音算的 ARKit 52 通道) 优先; 它没到或没开时用前端按频谱估的五个口型.
//
// 分工: 颈部 = motion.js 的头部姿态 (思考/倾听/鞠躬) + 表演层的临时偏移; 头部骨骼 = Soullink (微动/点头/摇头/情绪姿态);
// 眼睛 = Soullink 视线. 两层叠加, 互不覆盖.
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { VRMLoaderPlugin, VRMUtils } from '@pixiv/three-vrm';
import './spring-center.js';  // 弹簧骨中心节点的逆矩阵缓存不用 Proxy (要在载入模型之前装上; 省掉矩阵运算装箱, 结果逐位相同)
import { FaceEngine } from './vendor/soullink3d/dist/src/engine.js';
import { emotionByName } from './vendor/soullink3d/dist/src/emotions.js';
import { computeMorphWeights, resolveProfile, vroidProfile } from './vendor/soullink3d/dist/src/profile.js';
import { applyModelCorrectives } from './model-constraints.js';
import { FaceRig } from './vendor/soullink3d/dist/src/face-rig.js';
import { canonicalMorphName, normalizeLegacyMorphNames } from './vendor/soullink3d/dist/src/model-utils.js';
import { arkitKeys, arkitToAUs, directShapeChannel } from './vendor/soullink3d/dist/src/arkit.js';
import { CameraRig, collisionParts } from './scene.js';
import { arkitToVisemes } from './lipsync.js';
import { ContactLayer } from './contact.js';
import { HandLayer } from './hand.js';
import { Arbiter } from './arbiter.js';
import { PoseFilter } from './pose-filter.js';
import { KinematicsMonitor } from './kinematics.js';
import { PostureRegulator } from './posture.js';

const rad = (d) => (d * Math.PI) / 180;
const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
// 我们的 8 个情绪标签 -> Soullink 的 24 个情绪锚点
const EMOTION_NAME = { neutral: 'neutral', happy: 'happy', excited: 'excited', calm: 'calm', sad: 'sad',
  shy: 'shy', angry: 'anger', surprised: 'surprised' };
const VISEMES = { aa: 'A', ih: 'I', ou: 'U', ee: 'E', oh: 'O' };
const VISEME_GAIN = { aa: 1, ih: 0.8, ou: 0.9, ee: 0.8, oh: 0.9 };
// A2F 的上半脸 (眉、眯眼、颧) 换成这些 AU, 和情绪引擎的取较大值. 眨眼和视线仍归 Soullink.
const A2F_UPPER_AUS = ['au01InnerBrowRaiser', 'au02OuterBrowRaiser', 'au04BrowLowerer', 'au05UpperLidRaiser',
  'au06CheekRaiser', 'au07LidTightener', 'au09NoseWrinkler'];
const A2F_UPPER = new Set(['browInnerUp', 'browDownLeft', 'browDownRight', 'browOuterUpLeft', 'browOuterUpRight',
  'eyeWideLeft', 'eyeWideRight', 'cheekSquintLeft', 'cheekSquintRight', 'eyeSquintLeft', 'eyeSquintRight',
  'noseSneerLeft', 'noseSneerRight']);
// 模型自带 ARKit 通道时由 A2F 直接驱动的嘴部通道. 笑/撇嘴/酒窝归情绪引擎, 不在这里.
const A2F_MOUTH = ['jawOpen', 'jawForward', 'jawLeft', 'jawRight', 'mouthClose', 'mouthFunnel', 'mouthPucker',
  'mouthLeft', 'mouthRight', 'mouthStretchLeft', 'mouthStretchRight', 'mouthRollLower', 'mouthRollUpper',
  'mouthShrugLower', 'mouthShrugUpper', 'mouthPressLeft', 'mouthPressRight', 'mouthLowerDownLeft',
  'mouthLowerDownRight', 'mouthUpperUpLeft', 'mouthUpperUpRight'];
// 眼神层用的形态键 (高光往四个方向挪、高光放大、瞳孔放大). 左右是角色自己的.
const EYE_LIFE = ['eyeHighlightLeft', 'eyeHighlightRight', 'eyeHighlightUp', 'eyeHighlightDown', 'eyeSparkle', 'pupilDilate'];

/**
 * 骨骼索引改成 16 位. glTF 允许每个图元用 8 位存骨骼索引 (只用到前 256 根时); combineSkeletons 合并骨架后
 * 重新编号, 新编号超过 255 时写回 8 位数组会溢出绕回, 顶点绑到别的骨骼上 (MMD 转来的初音有 600 多根骨骼,
 * 虹膜从眼骨 10/11 号被绕到了裙子骨骼, 眼睛只剩眼白).
 */
export function widenSkinIndices(root) {
  root.traverse((o) => {
    const attr = o.geometry?.attributes?.skinIndex;
    if (!attr || attr.array instanceof Uint16Array || attr.array instanceof Uint32Array) return;
    o.geometry.setAttribute('skinIndex', new THREE.Uint16BufferAttribute(Uint16Array.from(attr.array), attr.itemSize));
  });
}

/**
 * 程序动作的手臂通道 -> 上臂和前臂的局部欧拉角. 归一化骨骼以 T-pose 为零位, 左臂沿 +X, 绕 Z 轴负转放下.
 * 手臂最低不贴到身体上. 肘部: 手臂举起时 (挥手) 在身体正面的平面里弯; 手臂下垂时改成向前屈 (托腮/摊手/自然垂手),
 * 否则 3D 里前臂会折到身后. 两者按上臂抬起角度平滑过渡.
 */
export function armPose(ch) {
  const elbow = (s, e) => {
    const f = clamp((s - 50) / 40); const frontal = f * f * (3 - 2 * f);
    return { z: rad(e * frontal), y: rad(Math.min(140, e * (1 - frontal))) };
  };
  const ls = Math.max(ch.ls, 16), rs = Math.max(ch.rs, 16);
  const le = elbow(ls, ch.le), re = elbow(rs, ch.re);
  return {
    left: { UpperArm: [0, 0, rad(ls - 90)], LowerArm: [0, -le.y, le.z] },
    right: { UpperArm: [0, 0, rad(90 - rs)], LowerArm: [0, re.y, -re.z] },
  };
}

/** 程序动作的躯干和头部通道 -> 局部欧拉角: 前倾分给腰 (一半) 和胸, 头部姿态的 70% 给颈部 (头部骨骼归 Soullink). */
export function axialPose(ch) {
  return {
    spine: [rad(ch.lean) * 0.5, 0, 0],
    chest: [rad(ch.lean) * 0.35, 0, 0],
    neck: [rad(ch.hp) * 0.7, rad(ch.hy) * 0.7, -rad(ch.hr) * 0.7],
  };
}

export class VRMAvatar {
  constructor(container) {
    this.container = container;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 1.75));
    this.renderer.outputColorSpace = THREE.SRGBColorSpace;
    this.renderer.setClearColor(0x000000, 0);
    this.renderer.domElement.className = 'vrm-canvas';
    container.appendChild(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(24, 1, 0.05, 20);
    // 灯光沿用 SoullinkEmotion3D 的三点布光, MToon 材质在这个设置下观感最好.
    this.scene.add(new THREE.HemisphereLight(0xfffcf6, 0xc0b8d2, 2));
    const key = new THREE.DirectionalLight(0xfffcf4, 2.2); key.position.set(-1, 2, 3); this.scene.add(key);
    this.key = key;
    const fill = new THREE.DirectionalLight(0xc9c3ff, 0.65); fill.position.set(2, 1.6, -1); this.scene.add(fill);
    this.loader = new GLTFLoader();
    this.loader.register((parser) => ({ name: 'SOULLINK_legacy_morph_names', beforeRoot: async () => normalizeLegacyMorphNames(parser.json) }));
    this.loader.register((parser) => new VRMLoaderPlugin(parser));
    this.engine = new FaceEngine({ idle: true, autoBlink: true });
    this.mouth = { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0 };
    this.a2f = Object.fromEntries(arkitKeys.map((k) => [k, 0]));  // 此刻的 A2F 权重 (没有时全 0)
    this.a2fMix = 0;  // 0 = 频谱口型, 1 = A2F; 数据到了在 80 ms 内交接
    this.vrm = null;
    this.body = null;
    this.gestures = null;  // 手势来源 (motion.js 的 MotionSystem): 设了以后, 没有 HAST-SEC 时把它的手势叠到行动层上
    this.gestureW = { left: 0, right: 0, torso: 0, head: 0 };
    this.gesturing = false;  // 这一帧有没有手势叠在行动层上 (行动层据此暂不插待机小动作)
    this._q = new THREE.Quaternion();
    this._e = new THREE.Euler();
    this.timers = new Set();
    this.kin = new KinematicsMonitor(this);  // 运动学约束的监测和可视化 (见 kinematics.js)
    this._ro = new ResizeObserver(() => this._resize());
    this._ro.observe(container);
  }

  async load(source, onProgress = () => {}) {
    const url = typeof source === 'string' ? source : URL.createObjectURL(source);
    let gltf;
    try {
      gltf = await this.loader.loadAsync(url, (p) => onProgress(p.total ? p.loaded / p.total : 0));
    } finally {
      if (typeof source !== 'string') URL.revokeObjectURL(url);
    }
    const vrm = gltf.userData.vrm;
    if (!vrm || !vrm.humanoid) { VRMUtils.deepDispose(gltf.scene); throw new Error('不是有效的 VRM 0.x / 1.0 模型'); }
    VRMUtils.rotateVRM0(vrm);
    widenSkinIndices(vrm.scene);
    VRMUtils.combineSkeletons(vrm.scene);
    // Authored collider group assignments are authoritative, including arm colliders.
    if (this.vrm) { this.scene.remove(this.vrm.scene); VRMUtils.deepDispose(this.vrm.scene); }
    this.body = null;
    this.vrm = vrm;
    this.filter = new PoseFilter(Object.fromEntries(Object.keys(vrm.humanoid.humanBones)
      .map((n) => [n, vrm.humanoid.getNormalizedBoneNode(n)])), { enabled: this.filter?.enabled ?? true });
    this.scene.add(vrm.scene);
    this.morphs = [];
    this.baseline = new Map();
    this.lastWeights = {};
    const names = new Set();
    vrm.scene.traverse((o) => {
      if (!o.isMesh) return;
      o.frustumCulled = false;
      if (o.morphTargetDictionary && o.morphTargetInfluences) {
        this.morphs.push(o);
        this.baseline.set(o, [...o.morphTargetInfluences]);
        Object.keys(o.morphTargetDictionary).forEach((n) => names.add(n));
      }
    });
    this.morphNames = names;
    this.bindings = resolveProfile(vroidProfile, names);
    this.fallback = this.bindings.filter((b) => b.supported).length < 4;
    const find = (canonical) => [...names].find((n) => canonicalMorphName(n) === canonical);
    // 口型落点: VRoid 的 Fcl_MTH_A/I/U/E/O 优先, 其次 ARKit 通道, 都没有就用 VRM 预设表情.
    this.visemeMorphs = Object.fromEntries(Object.entries(VISEMES).map(([v, s]) => [v, find(`Fcl_MTH_${s}`)]));
    this.mouthKeys = [...names].filter((n) => /Fcl_MTH_/.test(canonicalMorphName(n)));
    this.arkit = Object.fromEntries(arkitKeys.map((c) => [c, [...names].find((n) => directShapeChannel(n) === c)]));
    this.relaxedLid = find('Fcl_EYE_Sorrow');
    // 眼神层: 模型带单独的高光网格和这几个形态键时 (silvercat), 由 _eyeLife 每帧驱动
    this.eyeLife = EYE_LIFE.every((n) => names.has(n)) ? { x: 0, y: 0, vx: 0, vy: 0, pupil: 0.3, t: 0, q: null } : null;
    if (vrm.lookAt) vrm.lookAt.autoUpdate = false;
    this.rig = new FaceRig(vrm);
    this.contact = new ContactLayer(vrm);  // 手臂和身体、手里东西的避让, 握紧程度
    this.hands = new HandLayer(vrm);       // 手指: 放松、张开、指向、搭在腿上、握持 (见 hand.js)
    this.posture = new PostureRegulator(vrm);  // 按关节活动范围 (ROM) 自动调整姿态
    this.arbiter = new Arbiter(vrm.humanoid, vrm.scene);  // 运动仲裁表: 每帧哪一层驱动哪个部位 (见 arbiter.js)
    this.replayAt = null;                  // 回放: 仲裁表缓存里的第几帧 (null = 实时)
    this.engine.reset();
    vrm.update(0);
    vrm.scene.updateMatrixWorld(true);
    this._frame();
    const meta = vrm.meta || {};
    this.info = { name: meta.name || meta.title || '未命名 VRM', morphs: names.size,
      bindings: this.bindings.filter((b) => b.supported).length, fallback: this.fallback,
      visemes: Object.values(this.visemeMorphs).filter(Boolean).length };
    return this.info;
  }

  _frame() {
    const head = new THREE.Vector3();
    const node = this.vrm.humanoid.getNormalizedBoneNode('head');
    if (node) node.getWorldPosition(head);
    else head.set(0, 1.4, 0);
    this.headPos = head;
    this._resize();
  }

  _resize() {
    const { width, height } = this.container.getBoundingClientRect();
    if (!width || !height) return;
    this.renderer.setSize(width, height, false);
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
    if (!this.headPos) return;
    // 取景: 程序动作时腰部以上 (竖向约 1.1 m); HAST-SEC 驱动身体时拉到小腿 (约 1.55 m), 重心转移和抬腿才看得见.
    // 头顶上方都留出举手的空间. 横向至少放下肩宽和下垂的手臂 (0.62 m), 窄屏时水平伸直的手臂允许被裁掉一点.
    const fov = 24;
    const tan = Math.tan(rad(fov) / 2);
    const span = this.body ? 1.55 : 1.1;
    const dist = Math.min(4.5, Math.max(span / (2 * tan), 0.62 / (2 * tan * this.camera.aspect)));
    const cy = this.headPos.y - (this.body ? 0.42 : 0.2);
    this.portrait = { dist, cy, fov };
    if (this.cameraRig) return;  // 有行动层时由跟拍镜头每帧取景
    this.camera.position.set(0, cy + 0.03, dist);
    this.camera.lookAt(0, cy, 0);
  }

  /** 交给外部运动层 (HAST-SEC) 驱动身体. 它会把脚放到地面, 所以重新取景. */
  attachBody(body) {
    this.body = body;
    this.engine.configure({ idle: !body });  // 动捕的头部本身就有微动, 再叠 Soullink 的待机摆动会显得晃
    if (this.loco) this.loco.body = body;
    this.vrm.scene.updateMatrixWorld(true);
    this._frame();
  }

  /** 接上开放空间行动层: 显示房间, 镜头改为跟拍. */
  attachLocomotion(loco, room, extras = []) {
    this.loco = loco;
    loco.arbiter = this.arbiter;
    this._collision = null;
    loco.thighRadius = (side) => this.contact?.radius?.[`${side}Thigh`];  // 坐着时手放在腿上要知道大腿多粗、手多厚
    loco.handRadius = (side) => this.contact?.radius?.[`${side}:hand`];
    loco.foreArmRadius = (side) => this.contact?.radius?.[`${side}:foreArm`];
    this.room = room;
    this.scene.add(room, ...extras);
    // 房间里打开阴影: 主光从窗户一侧斜上方照进来, 投影范围覆盖整个房间; 角色和家具都投下阴影
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    const k = this.key;
    k.position.set(1.8, 4.2, 2.6);
    k.target.position.set(-0.4, 0, -0.6);
    this.scene.add(k.target);
    k.castShadow = true;
    k.shadow.mapSize.set(2048, 2048);
    Object.assign(k.shadow.camera, { left: -4.5, right: 4.5, top: 4.5, bottom: -4.5, near: 0.5, far: 12 });
    k.shadow.camera.updateProjectionMatrix();
    k.shadow.bias = -0.0004;
    k.shadow.normalBias = 0.005;  // 0.02 时鞋底最下面约 2 cm 投不出影子, 鞋和影子之间有一道缝, 看起来像悬空
    this.vrm.scene.traverse((o) => { if (o.isMesh) o.castShadow = true; });
    this.shadow = extras.find((o) => o.name === 'shadow') || null;
    this.camera.far = 30;
    this.cameraRig = new CameraRig(this.camera, loco.scene);
    this.cameraRig.t = 0;
    this._resize();
  }

  /** 回放仲裁表缓存里的第 replayAt 帧: 骨骼和根节点放回那一帧, 只更新 VRM、镜头跟着那一帧的位置、渲染; 各层都不跑. */
  _replay() {
    this.arbiter.restore(Math.max(0, Math.min(this.arbiter.count - 1, this.replayAt)));
    this.vrm.update(0);
    if (this.cameraRig && this.loco) {
      const p = this.vrm.scene.position;
      this.cameraRig.update(0.016, { char: { ...this.loco.char, x: p.x, z: p.z }, portrait: this.portrait, active: true,
        userSpot: this.loco.targets.user.approach });
    }
    this.renderer.render(this.scene, this.camera);
  }

  /** 房间家具的碰撞体 (scene.js 的 collisionParts, 第一次用时生成); 没有房间时为 null. */
  collision() {
    if (!this.room || !this.loco?.scene) return null;
    this._collision ??= collisionParts(this.room, this.loco.scene);
    return this._collision;
  }

  // -- 表演接口 (app.js / performance.js 调用) --------------------------------------------
  setEmotion(label, intensity = 0.8) {
    const name = EMOTION_NAME[label] || 'neutral';
    if (emotionByName.has(name)) this.engine.triggerEmotion(name, intensity);
  }

  headAction(name, { amplitude = 0.5, speed = 1, repetitions = 1, duration } = {}) {
    this.engine.playAction(name, { amplitude, speed, repetitions, duration, emotion: false });
  }

  /** 视线短暂移开再自动回来. yaw: 正值看角色自己的左边, pitch: 正值向上 (度). */
  glance(yaw, pitch, seconds) {
    this.engine.glanceDegrees({ yaw, pitch }, seconds);
  }

  lookAtUser() {
    this.engine.releaseGazeControl();
  }

  /** 临时把某个面部动作单元推到 value, seconds 后交还给情绪引擎 (仍经过它的平滑). */
  pulseAU(key, value, seconds) {
    this.engine.setAU(key, value);
    const id = setTimeout(() => { this.engine.setAU(key, null); this.timers.delete(id); }, seconds * 1000);
    this.timers.add(id);
  }

  /**
   * 眼神: 高光是贴在虹膜前面的独立小网格, 形态键把它在虹膜上挪动. 头一转, 高光像挂在弹簧上一样往反方向滞后再回弹
   * (反光点相对眼球是落后的, 看着像眼神在流转); 平时有很慢的轻微流动和闪动, 笑的时候更亮.
   * 瞳孔: 惊讶 (上眼睑抬起) 放大, 皱眉收小, 再加一点很慢的起伏.
   */
  _eyeLife(dt, weights, aus) {
    const e = this.eyeLife, head = this.vrm.humanoid.getNormalizedBoneNode('head');
    if (!head || !(dt > 0)) return;
    const q = head.getWorldQuaternion(new THREE.Quaternion());
    let yaw = 0, pitch = 0;
    if (e.q) {  // 这一帧头转了多少 (头自己的坐标里, 小角度近似): 绕竖轴 = 左右, 绕横轴 = 上下; 换成角速度
      const d = e.q.invert().multiply(q), s = d.w < 0 ? -2 : 2;
      yaw = (d.y * s) / dt; pitch = (d.x * s) / dt;
    }
    e.q = q;
    e.t += dt;
    // 角色朝 +z, 左是 +x: 头往左转 (绕 +y 正转) 时反光往右落后; 低头 (绕 +x 正转) 时往上落后
    const tx = clamp(-yaw * 0.35, -1, 1) + 0.18 * Math.sin(e.t * 0.7) + 0.08 * Math.sin(e.t * 1.9 + 1);
    const ty = clamp(pitch * 0.35, -1, 1) + 0.12 * Math.sin(e.t * 0.5 + 2);
    const k = 60, c = 2 * 0.45 * Math.sqrt(k), h = Math.min(dt, 1 / 30);  // 欠阻尼 (约 1.2 Hz), 会轻微回弹
    e.vx += (k * (tx - e.x) - c * e.vx) * h; e.x += e.vx * h;
    e.vy += (k * (ty - e.y) - c * e.vy) * h; e.y += e.vy * h;
    weights.eyeHighlightLeft = clamp(e.x); weights.eyeHighlightRight = clamp(-e.x);
    weights.eyeHighlightUp = clamp(e.y); weights.eyeHighlightDown = clamp(-e.y);
    weights.eyeSparkle = clamp(0.15 + 0.15 * Math.sin(e.t * 1.3) + 0.4 * (aus.au12LipCornerPuller || 0));
    const pupil = clamp(0.3 + 0.7 * (aus.au05UpperLidRaiser || 0) - 0.4 * (aus.au04BrowLowerer || 0) + 0.06 * Math.sin(e.t * 0.37));
    e.pupil += (pupil - e.pupil) * (1 - Math.exp(-dt / 0.35));
    weights.pupilDilate = e.pupil;
  }

  // -- 每帧 -----------------------------------------------------------------------------
  /** neck: 表演层给颈部的临时偏移 (度), 如问句歪头. */
  apply(ch, face, dt, neck = { roll: 0, pitch: 0 }) {
    if (!this.vrm) return;
    const vrm = this.vrm;
    if (this.replayAt !== null && this.arbiter?.count) { this._replay(); return; }
    const frame = this.engine.update(dt);
    const bone = (name) => vrm.humanoid.getNormalizedBoneNode(name);
    const set = (name, x, y, z) => { const b = bone(name); if (b) b.rotation.set(x, y, z); };
    // 运动仲裁表: 每层写完记一笔这一层让各部位转了多少 (见 arbiter.js)
    const arb = this.arbiter;
    arb?.begin();

    if (this.body) {
      // 身体由 HAST-SEC 求解并写进骨骼; 这里只在颈部叠加表演层的偏移.
      try {
        this.body.update(dt);
      } catch (err) {
        console.error(err);
        this.attachBody(null);
        this.onBodyError?.(err);
      }
      if (!this.loco) arb?.mark('hast', { absolute: true, all: true });
    }
    if (this.loco) {
      // 行动层: 走动/转身时接管根位移和全身 (按权重叠在 HAST-SEC 之上), 脚部/手部 IK, 视线.
      try {
        this.loco.update(dt);
      } catch (err) {
        console.error(err);
        this.loco = null;
        this.onLocoError?.(err);
      }
    }
    this.gesturing = this.loco && this.gestures && !this.body ? this._gestures(ch, dt) : false;
    arb?.mark('gesture');
    if (this.posture && (this.body || this.loco)) this.posture.limbs();  // 越出关节活动范围的四肢转回来
    arb?.mark('rom');
    if (this.contact) {
      // 所有运动来源写完之后: 手臂不插进身体, 拿着东西的手握起来; 然后让东西重新贴到手上
      try {
        this.contact.update(dt, this.loco?.heldItems() || [], this.loco?.armsBusy() || {});
        this.loco?._items();
      } catch (err) {
        console.error(err);
        this.contact = null;
      }
    }
    arb?.mark('contact');
    // 手指 (动捕和 HAST-SEC 都没有手指): 按行动层的情境和接触层的握紧程度摆手形
    const handCtx = this.loco?.handContext() || {};
    for (const side of ['left', 'right']) {  // 手搭在裙子外面时手指放平、顺着裙子 (接触层的 _skirt)
      const r = this.contact?.skirtRest?.(side) || 0;
      if (r > 0) handCtx[side] = { ...handCtx[side], lap: Math.max(handCtx[side]?.lap || 0, 0.8 * r) };
    }
    this.hands?.update(dt, handCtx, this.contact?.grip, this.contact?.gripKind);
    arb?.mark('hands', { absolute: true, fingers: !!this.hands?.has });
    if (this.body || this.loco) {
      const n = bone('neck');
      if (n && (neck.roll || neck.pitch)) n.quaternion.multiply(this._q.setFromEuler(this._e.set(rad(neck.pitch), 0, -rad(neck.roll))));
      arb?.mark('neck');
    } else {
      // 程序动作
      const arms = armPose(ch);
      for (const side of ['left', 'right']) for (const part of ['UpperArm', 'LowerArm']) set(side + part, ...arms[side][part]);
      // 颈部: 姿态层 (思考/倾听/鞠躬) + 表演层偏移. 头部骨骼由下面的 FaceRig 驱动.
      const axial = axialPose({ ...ch, hp: ch.hp + neck.pitch, hr: ch.hr + neck.roll });
      set('spine', ...axial.spine);
      set('chest', axial.chest[0] + Math.sin(frame.time * 1.4) * 0.007, 0, 0);
      set('neck', ...axial.neck);
      arb?.mark('proc', { absolute: true, all: true });
    }
    // 头部: Soullink 的转动叠在身体层的头部姿态之上 (HAST-SEC 时是动捕的头部, 程序动作时是静止位).
    const head = bone('head');
    const bodyHead = (this.body || this.loco) && head ? head.quaternion.clone() : null;
    this.rig.apply(frame.angles.head, frame.angles.eyes);
    if (bodyHead) head.quaternion.premultiply(bodyHead);
    arb?.mark('head');
    if (this.posture && (this.body || this.loco)) this.posture.axial();  // 颈和腰 (含头部动作) 不超出活动范围
    arb?.mark('rom');
    // 各层写完之后: 时序平滑和异常抑制 (见 pose-filter.js); 手里的东西按平滑后的手重新贴上
    if (this.filter && (this.body || this.loco) && this.filter.apply(dt)) this.loco?._items();
    arb?.mark('filter');
    // 落地放在平滑层之后 (最后一道): 平滑层限制骨盆每帧的移动, 放在前面的话落地当帧抬起的量会被削掉一部分, 鞋底又回到地面以下;
    // 平滑层也看不到落地的偏移, 两边不会互相拉扯
    if (this.loco && this.loco.weight > 0.002 && this.loco._ground(dt)) this.loco._items();
    // 端稳 (行动层的 carryCare): 骨盆最后一次挪过以后, 端着东西的手再按目标放一次, 东西不跟着落地修正颠
    if (this.loco?._carrySteady?.()) this.loco._items();
    if (this.loco?._cupPickFinalize?.(dt)) this.loco._items();
    if (this.loco?._graspFinalize?.(dt)) this.loco._items(); // opt-in final pose and visible props sync in the same frame
    arb?.mark('ground');
    arb?.end(performance.now() / 1000, this.loco ? this.loco.motionSource() : this.body ? 'hast' : 'proc',
      this.loco ? this.loco.weight : 0);
    if (this.body || this.loco) this.kin.update(dt);

    // A2F 数据: 首句开头还没算完 (几十到一两百毫秒) 或没开 A2F 时为空, 这时用频谱口型.
    const a2f = face.arkit;
    if (a2f) for (let i = 0; i < a2f.names.length; i++) this.a2f[a2f.names[i]] = a2f.values[i];
    else for (const k in this.a2f) this.a2f[k] = 0;
    this.a2fMix += ((a2f ? 1 : 0) - this.a2fMix) * (1 - Math.exp(-dt / 0.08));
    const mix = this.a2fMix < 0.002 ? 0 : this.a2fMix;
    // 口型平滑: 张嘴快、合嘴慢; 稍微提前取样已在 AudioOut 里做了. A2F 本身是平滑的 30 fps 曲线, 只轻微平滑.
    const fv = face.viseme;
    const av = mix ? arkitToVisemes(this.a2f) : null;
    for (const k of Object.keys(this.mouth)) {
      const target = av ? fv[k] * (1 - mix) + av[k] * mix : fv[k];
      const tau = target > this.mouth[k] ? 0.03 - 0.018 * mix : 0.07 - 0.045 * mix;
      this.mouth[k] += (target - this.mouth[k]) * (1 - Math.exp(-dt / tau));
    }
    const speaking = clamp((fv.level || 0) * 1.5);
    // 上半脸: A2F 随语音的挑眉、眯眼并进情绪引擎的动作单元, 取较大值.
    let aus = frame.aus;
    if (mix) {
      const upper = Object.fromEntries(arkitKeys.map((k) => [k, A2F_UPPER.has(k) ? this.a2f[k] : 0]));
      const up = arkitToAUs(upper);
      aus = { ...frame.aus };
      for (const k of A2F_UPPER_AUS) aus[k] = Math.max(aus[k], up[k] * mix);
    }

    const manager = vrm.expressionManager;
    if (manager) {
      for (const key of Object.keys(manager.expressionMap)) manager.setValue(key, 0);
      if (this.fallback) {  // 模型没有可用的 VRoid/ARKit 形变时, 用 VRM 预设表情近似
        manager.setValue('happy', clamp(aus.au12LipCornerPuller * 1.2));
        manager.setValue('angry', clamp(aus.au04BrowLowerer));
        manager.setValue('sad', clamp(Math.max(aus.au01InnerBrowRaiser, aus.au15LipCornerDepressor)));
        manager.setValue('surprised', clamp(aus.au05UpperLidRaiser));
        manager.setValue('blink', clamp(aus.au45Blink));
      }
      const hasMorphVisemes = Object.values(this.visemeMorphs).some(Boolean) || this.arkit.jawOpen;
      if (!hasMorphVisemes) for (const k of Object.keys(VISEMES)) manager.setValue(k, clamp(this.mouth[k] * VISEME_GAIN[k]));
    }
    vrm.update(Math.min(Math.max(dt, 0), 0.05));

    const weights = computeMorphWeights(aus, this.bindings, {}, {
      contrast: Math.max(0, (frame.settings.gain - 1) / 3) * 1.5, pinned: new Set(frame.pinnedAUs) });
    if (this.relaxedLid) weights[this.relaxedLid] = clamp(frame.details.eyelidRelaxation * (1 - frame.aus.au45Blink));
    for (const name of this.morphNames) {
      const channel = directShapeChannel(name);
      if (channel && !(name in weights)) weights[name] = frame.blendshapes[channel];
    }
    // 眨眼叠在笑眼/眯眼上: 都是把眼睑往中间合, 直接相加会合过头 (上下眼睑交叉、虹膜露到眼睑外). 眨得越深, 笑眼、眯眼让出越多
    // (乘 1 - 眨眼), 眨到底时只剩眨眼本身 = 完全闭眼. 以前反过来给眨眼打折: 眯眼上下眼睑合得不一样 (22% / 42%),
    // 睡着时 (au45 = 1、情绪放松带眯眼) 眨眼只到 0.86–0.89, 眼睛留一条缝
    for (const side of ['Left', 'Right']) {
      const blink = this.arkit[`eyeBlink${side}`];
      if (!blink || !weights[blink]) continue;
      const keep = 1 - clamp(weights[blink]);
      for (const k of [`cheekSquint${side}`, `eyeSquint${side}`]) {
        const name = this.arkit[k];
        if (name && weights[name]) weights[name] *= keep;
      }
    }
    if (this.eyeLife) this._eyeLife(dt, weights, aus);
    // 嘴部所有权: 说话时口型优先, 情绪带来的嘴型 (笑/撇嘴) 按响度让位但不清零, 边笑边说才自然.
    if (Object.values(this.visemeMorphs).some(Boolean)) {
      for (const key of this.mouthKeys) if (key in weights) weights[key] *= 1 - 0.6 * speaking;
      for (const [vis, name] of Object.entries(this.visemeMorphs)) {
        if (name) weights[name] = clamp(Math.max(weights[name] || 0, this.mouth[vis] * VISEME_GAIN[vis]));
      }
    } else if (this.arkit.jawOpen) {
      const m = this.mouth, a = this.arkit;
      const put = (name, value) => { if (name) weights[name] = clamp(Math.max((weights[name] || 0) * (1 - 0.6 * speaking), value)); };
      put(a.jawOpen, m.aa * 0.7 + m.oh * 0.5 + m.ou * 0.3 + m.ee * 0.3 + m.ih * 0.2);
      put(a.mouthFunnel, m.oh * 0.6 + m.ou * 0.4);
      put(a.mouthPucker, m.ou * 0.7);
      put(a.mouthStretchLeft, m.ee * 0.4 + m.ih * 0.3);
      put(a.mouthStretchRight, m.ee * 0.4 + m.ih * 0.3);
      if (mix) {  // 模型自带 ARKit 通道: A2F 的嘴部通道直接用, 和上面按五个口型的近似按交接比例混合
        for (const c of A2F_MOUTH) {
          const name = a[c];
          if (name) weights[name] = clamp((weights[name] || 0) * (1 - mix) + Math.max(this.a2f[c], (weights[name] || 0) * (1 - 0.6 * speaking)) * mix);
        }
      }
    }
    for (const m of this.morphs) {
      for (const name of Object.keys(this.lastWeights)) {
        const i = m.morphTargetDictionary[name];
        if (i !== undefined && !(name in weights)) m.morphTargetInfluences[i] = this.baseline.get(m)?.[i] ?? 0;
      }
      for (const [name, value] of Object.entries(weights)) {
        const i = m.morphTargetDictionary[name];
        if (i !== undefined) m.morphTargetInfluences[i] = value;
      }
    }
    // Read the final per-mesh influences, after procedural blink and A2F/viseme mixing.
    // Models without these explicitly authored correction keys are unchanged.
    this.correctiveWeights = applyModelCorrectives(this.morphs);
    this.lastWeights = weights;
    this.lastFrame = frame;
    if (this.cameraRig && this.loco) {
      const c = this.loco.char;
      this.cameraRig.update(dt, { char: c, portrait: this.portrait, active: this.loco.active, userSpot: this.loco.targets.user.approach });
      if (this.shadow) this.shadow.position.set(c.x, this.shadow.position.y, c.z);
    }
    this.renderer.render(this.scene, this.camera);
  }

  /**
   * 没有 HAST-SEC 时把程序动作的手势叠到行动层上: 行动层驱动全身之后不再用程序动作的通道, 挥手、举手、思考、鞠躬会看不见.
   * 按 motion.js 的组 (左臂/右臂/躯干/头部) 分别淡入 (0.12 s), 这一组回到待机片段后淡回行动层的姿态 (0.3 s).
   * 手臂: 肩膀混回标准位置, 上臂和前臂混合到程序动作的角度. 躯干和头部: 程序动作的角度叠加在行动层的姿态上, 待机的体态保留 (鞠躬 = 待机姿态再前倾).
   * 行动层自己要用的部位让给它: 拿放、伸手、指向用的那只手; 坐下/站起/拿放片段 (全身动作) 时的躯干和头;
   * 站着伸手 (自己会前倾) 时的躯干; 看向某样东西时的头 (走路时看前方路线只是轻轻转头, 不让, 边走边鞠躬时头也低下去).
   * 放在行动层之后、关节范围和防穿模之前. 返回这一帧是否有手势在叠加 (行动层据此暂不插待机小动作).
   */
  _gestures(ch, dt) {
    const loco = this.loco, motion = this.gestures, h = this.vrm.humanoid;
    const hand = (side) => loco.action?.grab?.side === side || loco.reaching?.side === side
      || (loco.pointing?.side === side && loco.pointing.w > 0.05) || !!loco.emoteUses?.(side);
    const clip = !!loco.action && !loco.action.loop;  // 坐着时的待机是循环片段, 不算
    const looking = !!loco.look?.target && performance.now() / 1000 < loco.look.until;
    const busy = { left: hand('left'), right: hand('right'), torso: clip || !!loco.reaching, head: clip || looking };
    const GROUP = { left: 'larm', right: 'rarm', torso: 'torso', head: 'head' };
    const weight = (part) => {
      const goal = !busy[part] && motion.groups[GROUP[part]]?.clip !== motion.idle ? 1 : 0;
      return this.gestureW[part] += (goal - this.gestureW[part]) * (1 - Math.exp(-dt / (goal > this.gestureW[part] ? 0.12 : 0.3)));
    };
    const arms = armPose(ch), axial = axialPose(ch);
    for (const side of ['left', 'right']) {
      const w = weight(side);
      if (w < 1e-3) continue;
      // 程序动作的手臂角度以肩膀在标准位置为前提; StayStill 待机把肩膀转了十几度, 照搬会让举手越过头顶、挥手横过脸.
      // 所以肩膀按同样的权重混回标准位置 (和桌宠 desk-body.js 同一做法)
      h.getNormalizedBoneNode(side + 'Shoulder')?.quaternion.slerp(this._q.identity(), w);
      for (const part of ['UpperArm', 'LowerArm']) {
        h.getNormalizedBoneNode(side + part)?.quaternion.slerp(this._q.setFromEuler(this._e.set(...arms[side][part])), w);
      }
    }
    const add = (name, w) => h.getNormalizedBoneNode(name)?.quaternion.multiply(this._q.setFromEuler(this._e.set(...axial[name].map((a) => a * w))));
    const wt = weight('torso'), wh = weight('head');
    if (wt >= 1e-3) { add('spine', wt); add('chest', wt); }
    if (wh >= 1e-3) add('neck', wh);
    return Object.values(this.gestureW).some((w) => w >= 1e-3);
  }

  dispose() {
    for (const id of this.timers) clearTimeout(id);
    this._ro.disconnect();
    if (this.vrm) VRMUtils.deepDispose(this.vrm.scene);
    this.renderer.dispose();
    this.renderer.domElement.remove();
  }
}

export async function createVRMAvatar(container, source, onProgress) {
  const avatar = new VRMAvatar(container);
  try {
    await avatar.load(source, onProgress);
  } catch (err) {
    avatar.dispose();
    throw err;
  }
  return avatar;
}
