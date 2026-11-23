// 桌宠的小屋模式: 她在「抹茶樱花」小筑居 (用户在 Blender 里做的房间, desktop/scripts/prepare_room.py 导出) 里生活.
// 小屋是桌宠窗口里的一小块 3D 空间 (pet.js 带 room=1 时), 不是单独的程序窗口. 这个模块管房间、镜头、点击, 把各部分接起来:
//   - 房间描述 room-scenes/petal-oak.json (格式同网页版 scenes/room.json): 家具、她能去哪、坐哪; life: 她会做的事; pick: 点哪里算哪件事.
//   - 走路、坐下、拿放东西、待机用网页版的行动层 (web/locomotion.js); 躺下、喝东西、推家具、瑜伽是叠在上面的程序姿势 (room-poses.js).
//   - 她做什么 (按心情和需求挑、一件事怎么一步步做) 在 room-acts.js; 心情、需求、库存在 room-needs.js; 能动的东西在 room-props.js.
//   - 镜头: 斜上方看下去 (看得到椅背后面), 跟着她: 她走出画面中间那一块才慢慢平移 (平时不动, 房间不用重画);
//     拖动平移、滚轮缩放 (光标下那一点不动), 拖过或缩放过之后 10 秒内不跟; 双击回到跟着她.
//   - 点她: 她看向你、挥手 (睡着时把她叫醒); 点家具: 她走过去做那件事 (坐到床边、去冲咖啡...).
//   - 房间画一次存起来 (颜色和深度), 平时只画她和正在动的东西, 被家具挡住的地方照样挡住; 镜头动了、东西挪了才重画.
import * as THREE from 'three';
import { prepareDeviceRoom } from './room-device-actions.js';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { createLocomotion } from '/static/locomotion.js';
import { contactShadow } from '/static/scene.js';
import { buildGrid } from '/static/nav.js';
import { Fridge, Life, Mover } from '/pet/room-acts.js';
import { Afford } from '/pet/room-afford.js';
import { herDate } from '/pet/tz.js';
import { Body } from '/pet/room-body.js';
import { RoomLiquid } from '/pet/room-liquid.js';
import { RoomRigid, rigidOn } from '/pet/room-rigid.js';
import { Practice } from '/pet/room-practice.js';
import { Growth } from '/pet/room-growth.js';
import { Needs } from '/pet/room-needs.js';
import { PoseLayer } from '/pet/room-poses.js';
import { Props } from '/pet/room-props.js';
import { loadRoomScene } from '/pet/room-scene-config.js';
import { createRoomStorage, withRoomSceneServer, withRoomSceneThink } from '/pet/room-scene-state.js';
import { surfaceCollider } from '/pet/springs.js';
import { perfOn } from '/pet/perf-flags.js';

// 默认使用 Casa Luce V8 少女生活小屋；旧屋仍可通过显式 roomscene 选择。
// 页面参数 roomscene=petal-oak 换回原来那个. 显式指定的房间读不到会报错; 只有未指定时的旧默认可兼容回退.
const HOLD_S = 10;  // 用户拖过、缩放过之后多久不跟着她

/** 镜头: 看向 target, 沿固定方向 (斜上方) 退 dist 米. 只平移、缩放, 不转. */
class View {
  constructor(camera, el, cfg, onClick) {
    Object.assign(this, { cam: camera, el, cfg, onClick });
    const p = THREE.MathUtils.degToRad(cfg.pitch);
    this.dir = new THREE.Vector3(0, Math.sin(p), Math.cos(p));
    this.lo = new THREE.Vector3().fromArray(cfg.bounds[0]);
    this.hi = new THREE.Vector3().fromArray(cfg.bounds[1]);
    this.target = new THREE.Vector3(0, cfg.lookY, 0);
    this.dist = cfg.dist;
    this.held = -1e9;      // 用户最后一次拖、缩放的时刻
    this.recenter = false; // 正在跟过去
    this.changed = true;
    this.right = new THREE.Vector3();
    this.up = new THREE.Vector3();
    let drag = null;
    el.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      drag = { x: e.clientX, y: e.clientY, x0: e.clientX, y0: e.clientY, t0: performance.now(), moved: false };
      el.setPointerCapture(e.pointerId);
    });
    el.addEventListener('pointermove', (e) => {
      if (!drag) return;
      if (!drag.moved && Math.hypot(e.clientX - drag.x0, e.clientY - drag.y0) > 5) {
        drag.moved = true;
        el.classList.add('dragging');
      }
      if (drag.moved) this.pan(e.clientX - drag.x, e.clientY - drag.y);
      drag.x = e.clientX;
      drag.y = e.clientY;
    });
    const end = (e) => {
      if (drag && !drag.moved && performance.now() - drag.t0 < 500 && e.type === 'pointerup') {
        const r = el.getBoundingClientRect();
        this.onClick?.(e.clientX - r.left, e.clientY - r.top);
      }
      drag = null;
      el.classList.remove('dragging');
    };
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
    el.addEventListener('wheel', (e) => {
      e.preventDefault();
      const r = el.getBoundingClientRect();
      this.zoom(Math.exp(e.deltaY * 0.0012), e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
    el.addEventListener('dblclick', () => this.home());
  }

  get following() { return performance.now() / 1000 - this.held > HOLD_S; }

  /** 回到默认远近, 接着跟她. */
  home() {
    this.dist = this.cfg.dist;
    this.held = -1e9;
    this.recenter = true;
    this.changed = true;
  }

  _axes() {
    this.apply();
    this.cam.matrixWorld.extractBasis(this.right, this.up, new THREE.Vector3());
  }

  pan(dx, dy) {
    this._axes();
    const perPx = (2 * this.dist * Math.tan(THREE.MathUtils.degToRad(this.cfg.fov) / 2)) / this.el.clientHeight;
    this.target.addScaledVector(this.right, -dx * perPx).addScaledVector(this.up, dy * perPx).clamp(this.lo, this.hi);
    this.held = performance.now() / 1000;
    this.changed = true;
  }

  zoom(factor, px, py) {
    this._axes();
    const next = THREE.MathUtils.clamp(this.dist * factor, this.cfg.minDist, this.cfg.maxDist);
    const halfH = this.dist * Math.tan(THREE.MathUtils.degToRad(this.cfg.fov) / 2);
    const nx = (px / this.el.clientWidth) * 2 - 1, ny = 1 - (py / this.el.clientHeight) * 2, k = 1 - next / this.dist;
    this.target.addScaledVector(this.right, nx * halfH * this.cam.aspect * k).addScaledVector(this.up, ny * halfH * k).clamp(this.lo, this.hi);
    this.dist = next;
    this.held = performance.now() / 1000;
    this.changed = true;
  }

  /**
   * 跟着她 (地面上的 x, z): 镜头一动房间就要重画 (一次 100 多万个三角形), 所以少动 (一直追着走的话几乎每帧都要重画) ——
   *   - 开始走一段路 (path 换了): 平移一次, 对准她和目的地的中间 (两者离得远、一个画面放不下时对准目的地), 走的路上不再动;
   *   - 走着走着跑出画面很远 (超过 edge 的 1.5 倍): 补一次;
   *   - 停下来 (站着、坐着) 以后离中心超过 deadzone: 慢慢移到她身上.
   */
  follow(x, z, dt, walking, path) {
    if (!this.following) return;
    const off = Math.hypot(x - this.target.x, z - this.target.z);
    if (walking && path?.length && path !== this.pathSeen) {
      this.pathSeen = path;
      const [gx, gz] = path[path.length - 1];
      this.aim = Math.hypot(gx - x, gz - z) < this.cfg.span ? [(x + gx) / 2, (z + gz) / 2] : [gx, gz];
      this.recenter = true;
    } else if (walking && off > this.cfg.edge * 1.5) {
      this.aim = [x, z];
      this.recenter = true;
    } else if (!walking && off > this.cfg.deadzone && !this.recenter) {
      this.aim = [x, z];
      this.recenter = true;
    }
    if (!walking) this.pathSeen = null;
    if (!this.recenter) return;
    if (!walking) this.aim = [x, z];  // 停下来以后对准她现在的位置 (坐下时会挪一点)
    const dx = this.aim[0] - this.target.x, dz = this.aim[1] - this.target.z;
    if (Math.hypot(dx, dz) < 0.05) {
      this.recenter = false;
      return;
    }
    const k = 1 - Math.exp(-dt / 0.2);
    this.target.x += dx * k;
    this.target.z += dz * k;
    this.target.y += (this.cfg.lookY - this.target.y) * k;
    this.target.clamp(this.lo, this.hi);
    this.changed = true;
  }

  apply() {
    this.cam.fov = this.cfg.fov;
    this.cam.near = 0.05;
    this.cam.far = 40;
    this.cam.position.copy(this.target).addScaledVector(this.dir, this.dist);
    this.cam.lookAt(this.target);
    this.cam.updateProjectionMatrix();
    this.cam.updateMatrixWorld();
  }
}

/**
 * 房间画一次存起来 (107 万个三角形每帧重画的话核显要一半左右): 镜头动了、画布大小变了时画进带深度的离屏图 (4 倍多重采样),
 * 平时每帧用一个铺满画布的方片把颜色和深度写回去, 再只画她. 她投在房间上的实时影子没有了, 用脚下的软影子代替.
 */
class RoomCache {
  constructor(renderer, scene, camera, room, hidden) {
    Object.assign(this, { renderer, scene, camera, room, hidden });
    this.size = new THREE.Vector2();
    this.rt = new THREE.WebGLRenderTarget(1, 1, { samples: 4, depthTexture: new THREE.DepthTexture(1, 1, THREE.UnsignedIntType) });
    this.quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), new THREE.ShaderMaterial({
      uniforms: { tColor: { value: this.rt.texture }, tDepth: { value: this.rt.depthTexture } },
      vertexShader: 'varying vec2 vUv; void main() { vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }',
      // 存下的是线性颜色 (画进离屏图时不做色调映射和 sRGB 转换): 写回屏幕时补上
      fragmentShader: `uniform sampler2D tColor; uniform sampler2D tDepth; varying vec2 vUv;
        void main() {
          gl_FragColor = texture2D(tColor, vUv);
          gl_FragDepth = texture2D(tDepth, vUv).r;
          #include <tonemapping_fragment>
          #include <colorspace_fragment>
        }`,
      // 深度测试要开着 (关掉时 WebGL 连深度也不写, 她会画在家具前面), 比较方式设成"总是通过"
      depthTest: true,
      depthFunc: THREE.AlwaysDepth,
      depthWrite: true,
    }));
    this.quad.frustumCulled = false;
    this.quad.renderOrder = -1000;
    scene.add(this.quad);
    this.dirty = true;
    this.draws = 0;
  }

  update() {
    const size = this.renderer.getDrawingBufferSize(new THREE.Vector2());
    if (!this.dirty && size.equals(this.size)) return false;
    this.why = this.why || { moved: 0, size: 0 };
    this.why[this.dirty ? 'moved' : 'size']++;
    this.size.copy(size);
    this.rt.setSize(size.x, size.y);
    const shown = this.hidden.map((o) => o.visible);
    this.hidden.forEach((o) => { o.visible = false; });
    this.quad.visible = false;
    this.room.visible = true;
    this.renderer.setRenderTarget(this.rt);
    this.renderer.render(this.scene, this.camera);
    this.renderer.setRenderTarget(null);
    this.room.visible = false;
    this.quad.visible = true;
    this.hidden.forEach((o, i) => { o.visible = shown[i]; });
    this.dirty = false;
    this.draws++;
    return true;
  }
}

/** 房间 GLB (矩阵算一次) 和几盏暖色的灯 (灯不放在房间里: 房间存起来以后会藏起来, 灯还要照着她). */
async function loadRoom(url) {
  const gltf = await new GLTFLoader().loadAsync(url);
  const room = gltf.scene;
  room.name = 'room';
  room.traverse((o) => {
    if (!o.isMesh) return;
    o.matrixAutoUpdate = false;
    o.updateMatrix();
  });
  room.updateMatrixWorld(true);
  const lamps = [
    { position: [0.1, 2.6, 0.1], color: [1, 0.82, 0.62], intensity: 6, range: 9 },
    { position: [-0.88, 0.9, -1.97], color: [1, 0.76, 0.52], intensity: 0.8, range: 2.4 },
    { position: [-0.86, 1.55, 0.59], color: [1, 0.8, 0.6], intensity: 1.2, range: 3 },
  ].map((l) => {
    const p = new THREE.PointLight(new THREE.Color().fromArray(l.color), l.intensity, l.range, 2);
    p.position.fromArray(l.position);
    return p;
  });
  return { room, lamps };
}

const WARM = new THREE.Color(1, 0.62, 0.36);

/**
 * 灯: 睡着时主灯关掉、床头灯留一点, 墙上一盏月亮夜灯一呼一吸 (用户要的); 越困灯越暖, 像傍晚 (困倦 0.4 起, 分 5 档).
 * 房间是存起来画的, 灯一变就重画一次. 月亮旁边那盏冷色的小灯一开始就在 (亮度 0), 开关时不用重新编译着色器.
 */
function roomLights(v, lamps, props, cache) {
  const moonLight = new THREE.PointLight(0x9db4ff, 0, 3.5, 2);
  moonLight.position.set(-0.9, 1.55, -2.3);
  v.scene.add(moonLight);
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 128;
  const g = canvas.getContext('2d');
  const glow = g.createRadialGradient(64, 64, 8, 64, 64, 62);
  glow.addColorStop(0, 'rgba(230,236,255,0.95)');
  glow.addColorStop(0.35, 'rgba(170,190,255,0.45)');
  glow.addColorStop(1, 'rgba(120,140,255,0)');
  g.fillStyle = glow;
  g.fillRect(0, 0, 128, 128);
  g.globalCompositeOperation = 'destination-out';  // 弯月: 挖掉一个偏心的圆
  g.beginPath();
  g.arc(80, 52, 30, 0, Math.PI * 2);
  g.fill();
  const moon = new THREE.Sprite(new THREE.SpriteMaterial({ map: new THREE.CanvasTexture(canvas), transparent: true, depthWrite: false }));
  moon.position.set(-0.9, 1.62, -2.42);
  moon.scale.set(0.34, 0.34, 1);
  moon.visible = false;
  props.live.add(moon);
  const lights = [];
  v.scene.traverse((o) => { if (o.isLight && o !== moonLight) lights.push({ o, i: o.intensity, c: o.color.clone() }); });
  let night = false, warm = 0, t = 0;
  const off = new Set();  // 她关掉的灯 (lamps 的下标: 2 是落地灯; 可供性 room-afford.js 开关)
  const apply = () => {
    for (const l of lights) {
      const k = night ? (l.o === lamps[1] ? 0.35 : 0.12) : 1 - 0.2 * warm;  // lamps[1] 是床头灯
      l.o.intensity = l.i * k * (off.has(lamps.indexOf(l.o)) ? 0.04 : 1);
      l.o.color.copy(l.c).lerp(WARM, warm * 0.55);
    }
    moon.visible = night;
    moonLight.intensity = night ? 0.7 : 0;
    cache.dirty = true;
  };
  props.every((dt) => {  // 夜灯一呼一吸
    if (!night) return;
    t += dt;
    const b = 0.5 + 0.5 * Math.sin(t * 1.3);
    moon.material.opacity = 0.55 + 0.4 * b;
    moonLight.intensity = 0.45 + 0.4 * b;
  });
  return {
    night(on) { if (on !== night) { night = on; apply(); } },
    warmth(f) {
      const q = Math.round(Math.max(0, Math.min(1, (f - 0.4) / 0.5)) * 5) / 5;
      if (q !== warm) { warm = q; apply(); }
    },
    /** 单独开关一盏灯 (i: lamps 的下标), 和睡着、越困越暖叠在一起. */
    lamp(i, on) { if (lamps[i] && on === off.has(i)) { if (on) off.delete(i); else off.add(i); apply(); } },
  };
}

/** 拖进来的文件, 按顺序: 小册子 (item) 还在桌上的先去看; 看完的等下次翻日记本时记进知识库 (take). */
function docQueue() {
  const list = [];
  return {
    add(d) { list.push(d); },
    peek() { return list.find((d) => d.item) || null; },
    take() { const out = list.filter((d) => !d.item); for (const d of out) list.splice(list.indexOf(d), 1);
      return out.map(({ name, kind, text }) => ({ name, kind, text })); },
  };
}

/** 床头柜上的便利贴 (她睡着时你发的消息): 一张一张叠起来, 醒来以后撕掉. */
function stickyNotes(scene, props) {
  const stand = (scene.decor || []).find((d) => d.id === 'nightstand')?.box;
  const at = stand ? new THREE.Vector3(stand.center[0], (stand.height ?? 0.5) + 0.012, stand.center[1]) : new THREE.Vector3(-0.89, 0.52, -1.95);
  const mat = new THREE.MeshStandardMaterial({ color: '#ffe066', roughness: 0.8, side: THREE.DoubleSide });
  const list = [], meshes = [];
  return {
    at,
    get count() { return list.length; },
    add(text) {
      list.push(text);
      const m = new THREE.Mesh(new THREE.PlaneGeometry(0.075, 0.075), mat);
      m.rotation.set(-Math.PI / 2, 0, (Math.random() - 0.5) * 0.8);
      m.position.set(at.x + (Math.random() - 0.5) * 0.08, at.y + meshes.length * 0.002, at.z + (Math.random() - 0.5) * 0.06);
      props.live.add(m);
      meshes.push(m);
    },
    take() { return list.splice(0); },
    clear() { for (const m of meshes.splice(0)) { props.live.remove(m); m.geometry.dispose(); } },
  };
}

/**
 * 进小屋: v 是已经加载好的角色 (avatar-lean.js), motion 是动作系统 (手势叠到行动层上).
 * opts: { status(text) 她在做什么, busy() 正在对话, log, onPoke(kind) 点了她,
 *   think(上行, 毫秒) 问服务端接下来做什么 (talk.roomThink: 返回 room_plan 的 Promise, 没连上时是 null) }.
 * 返回每帧要调的 before(dt) / after(), 以及 needs (心情和需求)、feed(种类) 投喂、working(开/关) 工具调用.
 */
export async function enterRoom(v, motion, { status, busy, log, onPoke, think, server, say, chip, watching, memory }) {
  const t0 = performance.now();
  const { scene, sceneId, sightMap } = await loadRoomScene(location.search, { log });
  const storage = createRoomStorage(sceneId);
  const { ITEMS, NAMES, PET_REGIONS, REGIONS, observe: sightObserve, regionAt, setPlace, witnessed, zoneAt } = sightMap;
  server = withRoomSceneServer(sceneId, server);
  think = withRoomSceneThink(sceneId, think);
  const { room, lamps } = await loadRoom(scene.glb);
  const layer = await createLocomotion(v, scene);
  const shadow = contactShadow();
  shadow.name = 'shadow';
  v.attachLocomotion(layer, room, [shadow, layer.itemGroup, ...lamps]);
  v.cameraRig = null;  // 镜头自己管
  v.gestures = motion;
  v.renderer.shadowMap.enabled = false;  // 房间存起来画以后实时阴影看不见, 省一遍阴影
  v.renderer.setClearColor(0x2e2826, 1);  // 小屋卡片的底色 (剖开的房间外面)
  layer.onNotice = (text) => log(`小屋: ${text}`);
  const cache = new RoomCache(v.renderer, v.scene, v.camera, room, [v.vrm.scene, shadow, layer.itemGroup]);
  room.visible = false;
  const el = v.renderer.domElement;
  el.style.touchAction = 'none';

  const props = new Props({ room, scene: v.scene, cache, layer, storage });
  props.avatar = v;  // 握东西时按接触层的握紧程度弯手指
  props.quietMachine();
  const poses = new PoseLayer(layer);
  const needs = new Needs({ nutritionEnabled: scene.nutrition?.enabled !== false, nutritionCapabilities: scene.nutrition?.capabilities, actualConsumption: scene.nutrition?.actualConsumption === true, feedKinds: scene.nutrition?.feedKinds });
  const r = { layer, v, motion, poses, props, needs, scene, sceneId, sightMap, storage, status, busy, log, think, surface: surfaceCollider(v), server, say, busyChip: chip, watching };
  r.memoryCard = memory;
  r.lights = roomLights(v, lamps, props, cache);
  needs.onChange(() => r.lights.warmth(needs.fatigue));
  r.notes = stickyNotes(scene, props);
  r.docs = docQueue();
  // 座位的真实高度 (练习坐下时会临时试别的高度; 体感按真实的量屁股陷没陷进去)
  const seatBase = scene.objects.filter((o) => o.seat).map((o) => ({ pos: o.seat.pos.slice(), height: o.seat.height, o }));
  r.seatTop = (seat) => seatBase.find((b) => Math.hypot(b.o.seat.pos[0] - seat.pos[0], b.o.seat.pos[1] - seat.pos[1]) < 0.05)?.height ?? null;
  r.seatBase = (id) => seatBase.find((b) => b.o.id === id)?.height ?? null;
  r.body = new Body(r);
  layer.graspHeadBoxes = () => r.body._boxes(); // Same final-pose geometry as the body observations.
  r.practice = new Practice();
  r.fridge = new Fridge(r);
  r.mover = new Mover(r);
  r.mover.restore();  // 上次挪过的家具、小东西放回去
  // 杯子原来摆在咖啡机上的位置 (上次挪过的放回去之前量): "咖啡机上"那一处在哪, 她找到杯子以后也放回这里
  r.cupHome = {};  // 杯子 -> 原来在咖啡机上时的中心
  const bottoms = [];
  for (const id of scene.coffee?.cups || []) {
    const node = props.node(id);
    if (!node) continue;
    const b = new THREE.Box3().setFromObject(node);
    r.cupHome[id] = b.getCenter(new THREE.Vector3()).toArray();
    bottoms.push([r.cupHome[id][0], b.min.y, r.cupHome[id][2]]);
  }
  if (bottoms.length) setPlace('coffee_machine', [0, 1, 2].map((k) => bottoms.reduce((s, p) => s + p[k], 0) / bottoms.length));
  props.restoreItems();
  // 杯里的液体、洒出来的水渍 (小窝的物理与后果 P0, room-liquid.js): 咖啡机出水接进杯子、喝一口少一口、端着走会洒、烫到她
  r.liquid = new RoomLiquid(r);
  // 刚体 (小窝物理 P1/P2, room-rigid.js): 开关打开 (页面 rigid=1 或 pet.room.rigid) 才载入 Rapier; 默认关, 和原来一样
  if (rigidOn()) r.rigid = new RoomRigid(r);
  // 动作成长 (room-growth.js, docs/roadmap/motion-growth.md): 每帧看动作出没出毛病, 记失败片段发给服务端; 空闲时练、验证过才换上
  r.growth = new Growth(r);
  layer.grid = buildGrid(scene);
  // 姿势层叠在行动层之后 (手送到嘴边、躺下...), 再让手里的东西贴回手上; 房间物件跟着行动层摆的代理
  const update = layer.update.bind(layer), items = layer._items.bind(layer);
  layer._items = () => { items(); props.follow(); };
  layer.update = (dt) => { update(dt); poses.update(dt); layer._items(); };
  // 可供性 (room-afford.js, docs/roadmap/room-afford.md): 房间里的东西能拿来干什么, 她看见了才知道、试过才知道好不好;
  // Life 的候选里加上她认识的那些. 每秒看一眼她这一刻看见了什么. 旧表缺失可继续, 新房间缺失则明确报错.
  r.afford = await Afford.load(r, sceneId);
  prepareDeviceRoom(r);
  const life = new Life(r);
  r.life = life;
  setInterval(() => r.afford?.tick(), 1000);
  let errors = 0;
  v.onLocoError = (err) => {  // 行动层每帧更新里抛异常时 avatar-vrm.js 会停用它 (她就定住了): 记下来, 重新挂上
    log(`小屋: 行动层出错 ${err.stack || err.message}`);
    if (++errors > 3) return;
    v.loco = layer;
  };
  setInterval(() => needs.tick(), 1000);

  // 她看得到什么 (room-sight.js; 认知阶段 2): 页面是环境, 只报这一刻看得到的 (站在哪一处、那里的台面上有哪几个杯子、
  // 开着的冰箱里、手里), 她以为杯子在哪由服务端算 (上行 room_observe). 每秒看一眼, 和上次报的不一样才报; 走在路上不报;
  // 找东西时每一步做完马上报一份 (带 act, 等服务端回认知的变化). 断开了下次连上重报.
  // 感知门 (2026-10-27): 不读液体、体感的真值 (杯里多少毫升、多烫、一摊摊在哪), 只收它们过了感知门的固定词表:
  // 液面粗档 = 液体的 perceive() (看得到杯口的才有); 洒了 / 喝不到 = 液体的 onEvent (她碰到了或者看得见落点才有);
  // 身上的感觉 = 体感的 onFeel (哪里、湿 / 烫一下 / 烫伤 / 摔疼了). 这里只补上"当时在哪一段"(她自己在做什么) 和落在哪一处,
  // 走在路上也马上报 (events). 用钩子不用 feelings() / perceive() 的取走: 自检也在取, 取走了这边就少了.
  const box = new THREE.Box3(), mid = new THREE.Vector3();
  const sight = {
    map: sightMap,
    standFor: sightMap.standFor,
    favorite: sightMap.FAVORITE,
    names: sightMap.NAMES,
    announced: false,
    last: '',
    pending: [],         // 感觉到、看到的事, 下一秒报
    gust: null,          // 这一阵洒 (液体世界的原始事件只用来记: 从哪一段开始洒的、落在哪几点; 量和看没看见按液体那边的)
    lastStage: 'other',
    act: null,           // 活动脚本说的"正在做哪一段" (端东西时: pick / lift / put); 没说就按行动层的模式猜
    /** 这一刻在哪一段: 喝 / 走 (端着走) / 拿起 (伸手拿) / 挪到身前 (刚拿住) / 放下 (端着走过以后的动作). */
    stage() {
      const held = Object.values(layer.items).some((i) => i.held), mode = layer.mode, last = sight.lastStage;
      const s = poses.name === 'sip' ? 'drink' : sight.act ? sight.act : mode === 'go' ? (held ? 'walk' : 'other')
        : mode === 'act' ? (held ? (last === 'walk' || last === 'put' ? 'put' : 'lift') : (last === 'walk' || last === 'put' ? 'put' : 'pick')) : 'other';
      sight.lastStage = s;
      return s;
    },
    /** 液体世界的原始事件: 一阵洒 (隔 1.5 秒以上算新的一阵, 和液体那边一样) 从哪一段开始、落在哪几点. 不进她的认知. */
    world(e) {
      const now = performance.now();
      if (e.type === 'spill') {
        if (!sight.gust || now - sight.gust.last > 1500) sight.gust = { stage: sight.stage(), at: [], last: now };
        sight.gust.last = now;
      } else if (e.type === 'land' && sight.gust && sight.gust.at.length < 8) sight.gust.at.push(e.at);
    },
    /** 液体那边过了感知门的事 -> 上报的事件 (补上哪一段、落在哪一处). */
    liquid(e) {
      if (e.kind === 'empty_sip' && ITEMS.includes(e.cup)) sight.pending.push({ kind: 'empty_sip', item: e.cup });
      if (e.kind !== 'spill') return;
      const g = sight.gust;
      sight.regions();
      const where = ['floor', 'machine'].includes(e.where) || REGIONS[e.where] ? e.where : (g?.at.map(regionAt).find(Boolean) || 'surface');
      sight.pending.push({ kind: 'spill', item: e.from, amount: e.amount, where, touched: !!e.touched, seen: !!e.seen,
        stage: e.from === 'machine' ? 'other' : g?.stage || sight.stage() });
    },
    /** 体感那边的感觉 -> 上报的事件. */
    feel(f) { sight.pending.push({ kind: 'felt', part: f.part, level: f.feel, stage: sight.stage() }); },
    /** 刚体那边过了感知门的事 (room-rigid.js: 手里的东西滑了 / 脱手了 / 看见掉了、倒了 / 只听见响) -> 上报的事件, 只抄固定的几项. */
    rigid(e) {
      const keep = { slip: ['item', 'hand'], drop: ['item', 'hand'], fall: ['item', 'where', 'toppled', 'loud'], noise: ['loud'] }[e.kind];
      if (keep) sight.pending.push({ kind: e.kind, ...Object.fromEntries(keep.map((k) => [k, e[k]])), stage: sight.stage() });
    },
    items() {
      const out = {};
      for (const id of ITEMS) {
        if (layer.items[id]?.held) { out[id] = 'hand'; continue; }
        const node = props.node(id);
        out[id] = node?.visible ? box.setFromObject(node).getCenter(mid).toArray() : null;
      }
      return out;
    },
    regions() { for (const id of Object.keys(REGIONS)) if (layer.targets[id]?.place) setPlace(id, layer.targets[id].place); },  // 家具挪过: 台面跟着
    /** 这一刻在哪一处、看得到什么 (液体的 perceive() 也调它, 这里不能反过来调 perceive). */
    now() {
      sight.regions();
      return sightObserve({ x: layer.char.x, z: layer.char.z, items: sight.items(), open: props.fridgeOpen ? ['fridge'] : [] });
    },
    /** now() 加上看得到杯口的那几只杯子的液面粗档 (液体那边给的). */
    look() {
      const o = sight.now(), cups = o && r.liquid?.perceive({ take: false }).cups;
      return cups && Object.keys(cups).length ? { ...o, fill: cups } : o;
    },
    zone: () => zoneAt(layer.char.x, layer.char.z),
    /** 她这一刻看得到 id 放在哪一处; 看不到 (不在她这一处、在关着的冰箱里) 返回 null. */
    sees(id) {
      const o = sight.now();
      return o ? Object.keys(o.seen).find((k) => o.seen[k].includes(id)) || null : null;
    },
    /** 报一份 (extra: act / result / events). wait: 等服务端回 room_observe_done ({changes}), 没回是 null; 不然只管发. */
    async report(extra = {}, wait = false) {
      let o = sight.look();
      if (!o && extra.events?.length) {  // 走在路上感觉到、看到的: 不在任何一处, 只报手里拿着什么
        o = { at: null, seen: {}, hand: Object.entries(sight.items()).filter(([, p]) => p === 'hand').map(([id]) => id).sort(), open: [] };
      }
      if (!o || !server?.connected?.()) return null;
      if (o.at) sight.last = JSON.stringify(o);
      const msg = { ...o, ...extra, regions: PET_REGIONS };
      if (!wait) { server.tell('room_observe', msg); return null; }
      return server.ask('room_observe', msg, ['room_observe_done'], 2000);
    },
    /**
     * 端起杯子之前问服务端这一趟怎么端 (wm/cognition/carry.py, 按她端东西的经验): what {cup, hot, route, wait_s}, 液面按她现在
     * 看到的补上. 回 {choice: {hand, care, settle, wait}, why, say}; 没连上、关了、没回是 null (照原来端).
     */
    async carry(what) {
      if (!server?.connected?.()) return null;
      const fill = sight.look()?.fill?.[what.cup];
      const res = await server.ask('room_carry', { ...what, ...(fill ? { fill } : {}) }, ['room_carry_plan'], 2000);
      return res?.ok && !res.look ? res : null;
    },
    /** 端完了: 先把还没报的 (放下那一下洒的) 报了, 再说一声 (服务端把这一趟记成经验). waited: 等凉等了几秒;
     *  delivered: false 是中途被打断了. */
    async carryDone({ waited = 0, delivered = true } = {}) {
      sight.act = null;
      if (!server?.connected?.()) return;
      const events = sight.pending.splice(0);
      if (events.length) await sight.report({ events }, true);
      server.tell('room_carry_done', { delivered, waited });
    },
    tick() {
      sight.stage();
      if (!server?.connected?.()) { sight.last = ''; sight.pending.length = 0; sight.announced = false; return; }  // 没连上: 这期间的事不补报
      if (!sight.announced) {
        server.tell('room_observe', { at: null, seen: {}, hand: [], open: [], regions: PET_REGIONS });
        sight.announced = true;
      }
      const events = sight.pending.splice(0);
      if (events.length) { sight.report({ events }); return; }
      if (layer.mode === 'go') return;
      const o = sight.look();
      if (o && JSON.stringify(o) !== sight.last) sight.report();
    },
  };
  r.sight = sight;
  if (r.liquid) { r.liquid.onWorld = sight.world; r.liquid.onEvent = sight.liquid; }
  if (r.rigid) r.rigid.onEvent = sight.rigid;
  if (r.body) r.body.onFeel = sight.feel;
  setInterval(() => sight.tick(), 1000);

  /** 台面上找个空位 (别和放着的东西叠在一起). 返回底面那一点 [x, y, z]. */
  const freeSpot = (to, id) => {
    const p = layer.targets[to]?.place || REGIONS[to]?.at;
    if (!p) return null;
    const taken = Object.values(layer.items).filter((i) => !i.held && i.id !== id).map((i) => i.mesh.position);
    for (const [ox, oz] of [[0, 0], [0.12, 0], [-0.12, 0], [0, 0.1], [0.12, 0.1], [-0.12, 0.1]]) {
      const x = p[0] + ox, z = p[2] + oz;
      if (!taken.some((q) => Math.hypot(q.x - x, q.z - z) < 0.09 && Math.abs(q.y - p[1]) < 0.3)) return [x, p[1], z];
    }
    return p.slice();
  };
  /**
   * 你 (或自检) 把杯子 id 挪到 to 那一处 (台面; 冰箱不行): 她在场 (起点或终点在她这一处) 看得到, 马上报; 不在场她不知道,
   * 下次走过去才发现. 挪过的位置照样记住 (下次打开还在那). 返回 { from, to, witnessed }, 挪不了返回 null.
   */
  const moveItem = (id, to, who = '你') => {
    if (!ITEMS.includes(id) || !REGIONS[to] || REGIONS[to].box || layer.items[id]?.held || !props.asItem(id)) return null;
    const it = layer.items[id], g = props.glb[id];
    sight.regions();
    const from = regionAt(it.mesh.position.toArray());
    const home = r.cupHome[id];
    const spot = to === 'coffee_machine' && home ? [home[0], home[1] - it.half, home[2]] : freeSpot(to, id);
    if (!spot || from === to) return null;
    it.mesh.position.set(spot[0], spot[1] + it.half, spot[2]);
    it.on = 'moved';
    g.proxyYaw0 = 0;
    props.toLive(g.node);
    g.settled = false;
    g.pending = false;
    g.still = 0;
    props.follow();
    const ev = witnessed(sight.zone(), id, from, to);
    log(`小屋: ${who}把${NAMES[id]}从${NAMES[from] || '别处'}挪到${NAMES[to]}${ev ? ' (她看见了)' : ' (她没看见)'}`);
    if (ev) sight.report({ events: [ev] });
    return { from, to, witnessed: !!ev };
  };
  r.moveItem = moveItem;

  // 点击: 先看点没点到她 (骨骼投到屏幕上的范围, 放宽 20 像素), 再看点到哪件家具 (life 的 pick 盒子, 取最近的; 家具挪了盒子跟着挪)
  const ray = new THREE.Raycaster(), ndc = new THREE.Vector2(), tmp = new THREE.Vector3();
  const picks = () => scene.life.filter((a) => a.pick).map((a) => {
    const [cx, cz] = a.pick.center, [sx, sz] = a.pick.size, base = a.pick.base || 0;
    return { id: a.id, box: new THREE.Box3(new THREE.Vector3(cx - sx / 2, base, cz - sz / 2), new THREE.Vector3(cx + sx / 2, base + a.pick.height, cz + sz / 2)) };
  });
  const bones = ['head', 'hips', 'leftFoot', 'rightFoot', 'leftHand', 'rightHand', 'chest'].map((n) => v.vrm.humanoid.getRawBoneNode(n)).filter(Boolean);
  const screen = (node, w, h) => { node.getWorldPosition(tmp).project(v.camera); return [((tmp.x + 1) / 2) * w, ((1 - tmp.y) / 2) * h]; };
  // 小屋里发生的事告诉服务端 (上行 room_event {kind, part | item, count}: 记进当天日志和日记、按次数加亲近, 协议见根目录 README):
  // 只带固定字段, 同一种事 30 秒内合成一条 (count 是几次). 只管发不等回: 服务端不认识这个类型时不回, 断开时不发
  const MERGE_S = new URLSearchParams(location.search).get('fast') === '1' ? 1.5 : 30;  // 自检: 结果报回去以前就要发出去
  const told = new Map();
  const tell = (kind, key, value) => {
    const id = `${kind}:${value}`;
    if (told.has(id)) { told.get(id).count++; return; }
    const ev = { kind, [key]: value, count: 1 };
    told.set(id, ev);
    setTimeout(() => { told.delete(id); server?.tell?.('room_event', ev); }, MERGE_S * 1000);
  };
  const pokes = [];  // 最近点她的时刻 (秒)
  /**
   * 点了她 (part: head 头 / hand 手 / body 身上). 以前不管点哪、点几次都是看向你、挥手、"她看向你～" (用户说太机械).
   * 现在: 摸头眯起眼睛、有点害羞; 点手朝你挥挥手; 身上点一两下看向你, 一阵子里连着戳 3 次说痒, 5 次以上鼓起脸不太高兴.
   * 坐着、在忙 (做瑜伽、喝东西、写日记) 时只抬头看你一眼, 不打断. 返回状态栏那句话.
   */
  const poked = (part) => {
    const now = performance.now() / 1000;
    while (pokes.length && now - pokes[0] > 10) pokes.shift();
    pokes.push(now);
    const n = pokes.length, pick = (a) => a[Math.floor(Math.random() * a.length)];
    onPoke?.(part === 'body' ? 'her' : `her:${part}`);
    tell('poke', 'part', part === 'head' || part === 'hand' ? part : 'body');
    if (life.sleepLock || life.dozing) return status(life.restarting ? '服务端在重启, 她先睡一会儿, 醒了再回你…' : '她在整理今天的对话, 先别吵她…') ?? '在整理';
    if (life.sleeping) { life.hurry = true; return '叫醒'; }  // 睡着了: 叫醒
    let say, mood = 'happy', fx = () => {};
    if (n >= 5) {
      say = pick(['"再戳我要生气了！"', '她鼓起脸, 扭过头去不看你', '"哼, 不理你了"']);
      mood = 'angry';
      needs.add({ fun: -1 });
      fx = () => v.headAction?.('shake', { amplitude: 0.35, repetitions: 2 });
    } else if (n >= 3) {
      say = pick(['"别戳啦, 好痒～"', '她缩了一下, 笑着躲开', '"干嘛呀～"']);
      mood = 'shy';
      needs.add({ social: 2, fun: 1 });
      fx = () => v.headAction?.('shake', { amplitude: 0.2, repetitions: 1 });
    } else if (part === 'head') {
      say = pick(['她眯起眼睛, 往你手心里蹭了蹭～', '被摸头了, 她有点害羞地笑了', '"嘿嘿……"']);
      mood = 'shy';
      needs.add({ social: 6, fun: 2 });
      fx = () => { v.pulseAU?.('au45Blink', 1, 1.2); v.headAction?.('nod', { amplitude: 0.25, repetitions: 1 }); };
    } else if (part === 'hand') {
      say = pick(['她朝你挥挥手～', '她伸出手和你击了个掌', '"嗯？要拉我去哪？"']);
      needs.add({ social: 4 });
      fx = () => motion.applyIntent('act', [{ do: 'wave', side: 'R', amp: 0.8, spd: 1.1, at: 0.05 }], now);
    } else {
      say = pick(['她看向你～', '"嗯？怎么啦？"', '她冲你笑了笑']);
      needs.add({ social: 4 });
      fx = () => motion.applyIntent('act', [{ do: 'wave', side: 'R', amp: 0.6, spd: 1, at: 0.1 }], now);
    }
    v.setEmotion(mood, mood === 'angry' ? 0.6 : 0.8);
    setTimeout(() => v.setEmotion('neutral'), 3000);
    if (poses.pose || layer.seated) {  // 在忙、坐着: 抬头看你一眼, 不起来、不挥手
      if (!poses.pose || poses.name === 'chinRest') layer.lookAt('user', 4);
      if (mood !== 'happy') fx();
      else v.headAction?.('nod', { amplitude: 0.2, repetitions: 1 });
    } else {
      layer.lookAt('user', 6);
      fx();
    }
    status(say);
    return say;
  };
  let holding = null;  // 你点着的杯子 (再点一处台面放过去)
  /** 光标下的杯子 (看得见、她没拿着的, 盒子放宽 3 cm); 没有返回 null. */
  const cupUnder = () => {
    let best = null, bestD = Infinity;
    for (const id of ITEMS) {
      const node = props.node(id);
      if (!node?.visible || layer.items[id]?.held) continue;
      const hit = ray.ray.intersectBox(box.setFromObject(node).expandByScalar(0.03), tmp);
      const d = hit ? hit.distanceTo(ray.ray.origin) : Infinity;
      if (d < bestD) { bestD = d; best = id; }
    }
    return best;
  };
  /** 光标指着的那一处台面 (离光标那条线最近的放置点, 冰箱不算); 没有返回 null. */
  const regionUnder = () => {
    sight.regions();
    let best = null, bestD = Infinity;
    for (const [id, g] of Object.entries(REGIONS)) {
      if (g.box) continue;
      const d = ray.ray.distanceToPoint(tmp.set(...g.at));
      if (d < Math.max(0.3, g.r) && d < bestD) { bestD = d; best = id; }
    }
    return best;
  };
  const onClick = (x, y) => {
    const w = el.clientWidth, h = el.clientHeight;
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (const b of bones) {
      const [sx, sy] = screen(b, w, h);
      x0 = Math.min(x0, sx); x1 = Math.max(x1, sx); y0 = Math.min(y0, sy); y1 = Math.max(y1, sy);
    }
    const pad = 20;
    if (x >= x0 - pad && x <= x1 + pad && y >= y0 - pad * 2 && y <= y1 + pad) {
      // 点到哪里: 离头 (头骨往上 9 cm) 近算摸头, 离哪只手近算点手, 其余算身上. 头的范围按屏幕上头有多大
      const head = v.vrm.humanoid.getRawBoneNode('head'), [hx, hy] = head ? screen(head, w, h) : [Infinity, Infinity];
      const top = head ? (() => { head.getWorldPosition(tmp).y += 0.09; tmp.project(v.camera); return ((1 - tmp.y) / 2) * h; })() : hy;
      const headR = Math.max(14, Math.abs(hy - top) * 1.6);
      const hands = ['leftHand', 'rightHand'].map((n) => v.vrm.humanoid.getRawBoneNode(n)).filter(Boolean).map((b) => screen(b, w, h));
      const part = Math.hypot(x - hx, y - (hy + top) / 2) < headR ? 'head'
        : hands.some(([px, py]) => Math.hypot(x - px, y - py) < 16) ? 'hand' : 'body';
      poked(part);
      return;
    }
    ndc.set((x / w) * 2 - 1, 1 - (y / h) * 2);
    ray.setFromCamera(ndc, v.camera);
    // 点杯子: 你拿起来 (再点一下要放的台面, 杯子就挪过去); 拿着的时候点台面: 放到那里. 她不在场就不知道杯子去哪了
    const cup = cupUnder();
    if (cup) {
      holding = holding === cup ? null : cup;
      status(holding ? `你拿起了${NAMES[cup]} · 再点一下要放到哪 (咖啡机上、桌上、床上、小边桌上…)` : `你把${NAMES[cup]}放回去了`);
      return;
    }
    if (holding) {
      const id = holding, to = regionUnder();
      holding = null;
      if (to && moveItem(id, to)) { status(`你把${NAMES[id]}放到了${NAMES[to]}`); return; }
      status(`那里放不了, ${NAMES[id]}留在原处`);
      return;
    }
    let best = null, bestD = Infinity;
    for (const p of picks()) {
      const hit = ray.ray.intersectBox(p.box, tmp);
      const d = hit ? hit.distanceTo(ray.ray.origin) : Infinity;
      if (d < bestD) { bestD = d; best = p; }
    }
    if (!best) return;
    // 点床: 困了 (或者夜里) 就睡觉, 不然坐在床边发呆
    const hour = herDate().getHours();  // 按她的时区 (tz.js)
    const id = best.id === 'rest' && (needs.v.energy < 45 || hour >= 23 || hour < 7) ? 'sleep' : best.id;
    if (life.goDo(id)) onPoke?.(id);
  };
  const view = new View(v.camera, el, scene.view, onClick);
  view.target.set(layer.char.x, scene.view.lookY, layer.char.z);
  log(`小屋就绪: ${v.info?.name}, ${Math.round(performance.now() - t0)} ms`);
  // 预编译 (2026-10-27, 页面参数 perfoff=warm 关): 房间存起来画时画进离屏图 (线性颜色、不做色调映射), 东西挪进活动层以后第一次画到屏幕上
  // 要另编一套着色器, 页面卡 100-140 ms (咖啡机出水、拿起杯子那一下). 载入后把房间和活动层的材质按屏幕的画法先编好 (compileAsync 不等编完)
  if (perfOn('warm')) v.renderer.compileAsync(room, v.camera, v.scene).then(() => v.renderer.compileAsync(props.live, v.camera, v.scene)).catch(() => {});
  life.start(1.5);
  let lastSocial = 0, lastDt = 1 / 30;

  return {
    layer, life, view, cache, scene, sceneId, sightMap, storage, needs, props, poses, mover: r.mover, body: r.body, practice: r.practice, liquid: r.liquid, rigid: r.rigid,
    growth: r.growth, afford: r.afford,
    /** 每帧画之前: 镜头 (跟着她)、在动的东西、房间要不要重画. 返回这一帧镜头动没动. */
    before(dt) {
      lastDt = dt;
      props.tick(dt);
      life.tick(dt);
      // 坐下、站起这类动作会把她一点点挪到座位上: 动作当中也不追, 坐稳 / 站稳了再挪
      view.follow(layer.char.x, layer.char.z, dt, layer.mode === 'go' || layer.mode === 'act', layer.path);
      const moved = view.changed;
      view.apply();
      if (moved) cache.dirty = true;
      cache.update();
      view.changed = false;
      return moved;
    },
    /** 每帧画之后: 脚下的软影子跟着她. */
    after() {
      shadow.position.set(layer.char.x, shadow.position.y, layer.char.z);
      r.rigid?.tick(lastDt);  // 刚体: 行动层、姿势层摆完以后推进, 交给它的东西摆到刚体的位置 (液体在它之后)
      r.liquid?.tick(lastDt);  // 液体: 手里的杯子是这一帧最终的位置; 烫到的话交给体感
      r.body.tick(lastDt);  // 体感: 骨骼是这一帧最终的样子
      r.growth?.tick(lastDt);  // 动作成长: 体感之后量这一帧 (最终的骨骼、手里的东西、洒了多少)
    },
    /** 走路、做动作、东西在动、镜头在动: 页面升到 30 帧. */
    get active() {
      return layer.mode === 'go' || layer.mode === 'act' || view.recenter || !!layer.reaching || poses.moving || props.moving || !!r.mover.moving;
    },
    /** 你在和她说话: 她看向你 (睡着的话醒过来). */
    attend() {
      if (!life.sleeping && !poses.pose) layer.lookAt('user', 10);  // 睡着时不叫醒 (你的话变成便利贴)
      const now = performance.now();
      if (now - lastSocial > 20000) { lastSocial = now; needs.add({ social: 3 }); }
    },
    /** 投喂 (FEED 的种类): 东西掉在房间前面的地上, 她过来捡. 返回她说的话. */
    feed(kind) {
      if (!(needs.canFeed?.(kind) ?? needs.feedAllowed)) { status('这种补给的消费动作尚未开放，库存保留'); return null; }
      const text = needs.feed(kind);
      if (!text) return null;
      tell('feed', 'item', kind);
      const d = scene.drop, x = d.center[0] + (Math.random() * 2 - 1) * d.spread[0], z = d.center[1] + (Math.random() * 2 - 1) * d.spread[1];
      props.drop(kind, [x, z]);
      status(text);
      if (!life.sleeping) {
        layer.lookAt('user', 3);
        if (life.current?.id !== 'fetch') life.goDo('fetch');
      }
      return text;
    },
    /** 你们说了几个字 (困倦按这个涨). */
    heard(chars) { needs.heard(chars); },
    /**
     * 她睡着时 (或者服务端在重启、断开了: offline) 你发的消息: 变成床头柜上的便利贴, 不发出去 (醒来、连回来以后她一条条回).
     * 返回 true 表示收下了.
     */
    note(text, offline = false) {
      if (!life.sleeping && !life.restarting && !life.down && !offline) return false;
      r.notes.add(text);
      status(life.sleeping || life.restarting ? `便利贴贴在床头柜上了 (${r.notes.count} 张)`
        : `和服务端断开了, 先贴在床头柜上, 连上了再回你 (${r.notes.count} 张)`);
      return true;
    },
    /**
     * 服务端要重启了 (看守进程先发的 server_restart; self-evolution.md 4.3): 坐着 (看书、发呆、在电脑前) 就地靠着椅背打个盹,
     * 站着就去床上睡一会儿 (不整理记忆); 这期间你的话变便利贴, 服务端回来 (serverBack) 再醒. 已经睡着了就接着睡. 返回她怎么了.
     * (2026-10-26 真机重启: 她从阅读椅走去床上躺下要半分钟, 服务端 18 秒就回来了, 所以坐着时不挪地方)
     */
    nap() {
      life.restarting = true;
      life.serverBack = false;
      if (life.sleeping) return '接着睡';
      if (layer.seated && life.current && !life.current.script) {
        life.doze(true);
        return '就地打盹';
      }
      life.sleepReason = 'restart';
      status('服务端要重启了, 她去睡一会儿');
      return life.goDo('sleep') ? '去睡一会儿' : null;
    },
    /** 没预告就和服务端断开了 (崩了、网断了): 你的话先贴在床头柜上. */
    serverDown() {
      life.down = true;
      if (!life.sleeping && !life.restarting) status('和服务端断开了, 她等它回来…');
    },
    /**
     * 服务端回来了 (连上后的 hello). news: 醒来要说的 (学会了什么、刚才那版退回去了), 没有给 ''.
     * 打盹的醒过来、看便利贴; 醒着的 (断线的那阵子) 下一件就去看便利贴.
     */
    serverBack(news = '') {
      life.serverBack = true;
      life.down = false;
      if (news) life.bootNews = news;
      if (life.dozing) life.doze(false);  // 就地打盹的: 睁眼, 放下手上的事去看便利贴
      if (life.restarting && !life.sleepLock) life.restarting = false;  // 已经睡着在等的, 由睡觉那边醒来
      if (!life.current && !life.sleeping && !life.asking) life.start(0.5);
    },
    /** QQ 那边她在做什么 (服务端的 channel_doing {what, text, secs}): 回 QQ、看 B 站坐到电脑前, 研究本事坐在阅读椅上. */
    doing({ what, text, secs } = {}) { life.doing(what, text, secs); },
    /** 你让她去睡 ("去睡吧""晚安"…): 上床, 睡着后整理今天的对话, 整理完起床. 返回她是不是去了. */
    sleepNow() {
      if (life.sleeping) return false;
      life.sleepReason = 'user';
      return life.goDo('sleep');
    },
    get asleep() { return life.sleeping; },
    /** 点她 (head / hand / body), 和用鼠标点一样; 返回她的反应. 自检用. */
    poke: (part = 'body') => poked(part),
    get notesLeft() { return r.notes.count; },
    /**
     * 光标在小屋卡片上 (x, y: 卡片里的 CSS 像素; null 是不在): 她看向光标指着的那一处 (镜头穿过光标的射线, 落在她头那么高的平面上);
     * 头、眼一起转, 不转身. 不在卡片上时看你 (镜头). 睡着、在做要低头的事 (喝东西、写字) 时不看.
     */
    lookAtScreen(x, y) {
      if (life.sleeping || poses.pose) return;
      if (x === null) { layer.lookAt('user', 1.2); return; }
      ndc.set((x / el.clientWidth) * 2 - 1, 1 - (y / el.clientHeight) * 2);
      ray.setFromCamera(ndc, v.camera);
      const headY = v.vrm.humanoid.getNormalizedBoneNode('head')?.getWorldPosition(tmp).y ?? 1.3;
      const k = (headY - ray.ray.origin.y) / (ray.ray.direction.y || -1e-6);
      const p = ray.ray.origin.clone().addScaledVector(ray.ray.direction, Math.max(0.5, Math.min(12, k)));
      (layer.targets.__cursor ??= { id: '__cursor', name: '光标' }).look = [p.x, Math.min(1.7, p.y), p.z];
      layer.look = { ...layer.look, target: '__cursor', until: performance.now() / 1000 + 1.2 };
    },
    /**
     * 你把文件拖进了小屋 (pet.js 读好的: { name, kind, text }): 一本小册子掉在电脑桌上, 她走过去翻; 内容在她下次翻开日记本时记进知识库.
     * 返回她说的话.
     */
    async dropDoc(doc) {
      const place = layer.targets.surf_desk?.place || [0.95, 0.77, -1.78];
      const color = { md: '#c8e6c9', txt: '#f0e6c8', image: '#f6c9d3', pdf: '#f3b9a5' }[doc.kind] || '#b9d7f0';
      const item = await props.booklet([place[0] - 0.25 + Math.random() * 0.1, place[1], place[2] + 0.05], color);
      r.docs.add({ ...doc, item });
      if (!life.sleeping && life.current?.id !== 'readDoc') life.hurry = !!life.current;
      if (!life.current && !life.sleeping) life.start(0.5);
      return `桌上多了一本小册子: ${doc.name}`;
    },
    /** 想查日记 (服务端以后可以用): 下一次去翻日记本时带着这个意图. */
    recall(intent) { life.diaryIntent = intent; return life.goDo('diary'); },
    /** 正在调几个工具 (操控电脑、手机...): 有就坐到电脑前. */
    working(count, label) { life.working(count, label); },
    /** 她看得到什么 (room-sight.js) 和你挪杯子 (自检用; 鼠标是点杯子、再点台面). */
    sight,
    moveItem,
    get holding() { return holding; },
    /** 让她去做某件事 (桌宠自带的工具 room_do, 也用在提醒到点时"过来看你"): 返回那件事的名字; 不认识、做不了 (东西用完了) 返回 null. */
    requestAct(id, options) { return life.recoveryError ? Promise.resolve({ act: id, ok: false, started: false, completed: false, reason: `${life.recoveryError}；请重新进入小屋后重试` }) : life.requests.request(id, options); },
    cancelAct(reason) { life.requests.stop(reason); },
    capabilities() { return life.acts.map(a => ({ id: a.id, name: a.name, enabled: !!life.canRequest(a) })); },
    doAct(id) {
      const a = life.acts.find((x) => x.id === id);
      return a && life.goDo(id) ? a.name : null;
    },
  };
}
