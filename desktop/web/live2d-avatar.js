// 桌宠的 2D 门面: Live2D Cubism 5 (SDK 和模型在不进版本库的 desktop/third_party/, 由 desktop/scripts/prepare_live2d.py 取出来).
// 接口和 VRM 那边对齐, pet.js、talk.js、performance.js 用到的那几样都有:
//   renderer.domElement (画布, 算命中掩码)、info、skills、apply(ch, face, dt, neck) (每帧: 驱动参数并画出来)、
//   setEmotion、headAction、lookAtUser、glance、pulseAU; 另有 follow (眼睛跟着鼠标) 和 headScreen (头在窗口里的位置).
// 驱动:
//   - 动作系统 (web/motion.js) 给的头部俯仰/转向/歪头和躯干前倾 -> ParamAngleY/X/Z、ParamBodyAngleY (点头、摇头、思考、鞠躬都走这里);
//   - 口型: A2F 的 jawOpen, 或者音频算出的口形权重 -> ParamMouthOpenY / ParamMouthForm;
//   - 情绪 -> 模型的表情 (pet-model.json 里的对应), 中性时收掉;
//   - 眨眼、呼吸、头发和衣服的物理用 Framework 自带的; 空闲时偶尔放一段模型自带的动作.
// 模型按"实际画出来的范围"放进窗口: 脚底贴着画布底边, 头顶留出对话栏的位置, 横向居中.
import { ExpressionLayer, createUpdater } from './expression-layer.js';
import { emotionTable } from './expression-caps.js';

const LIB = '/pet/live2d/';
const SHADERS = `${LIB}shaders/`;
const IDLE_MOTION_S = [25, 45];  // 空闲时隔这么久 (秒, 随机) 放一段模型自带的动作
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));

let fw = null;  // Framework 的各个模块 (第一次用时才加载)

async function loadFramework() {
  if (fw) return fw;
  if (!window.Live2DCubismCore) {
    await new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = `${LIB}core/live2dcubismcore.min.js`;
      s.onload = resolve;
      s.onerror = () => reject(new Error('Live2D Core 加载不了: 先跑 desktop/scripts/prepare_live2d.py'));
      document.head.append(s);
    });
  }
  const imp = (p) => import(`${LIB}framework/${p}`);
  const [core, user, setting, matrix, blink, breath, sched, expU, physU, blinkU, breathU, updater] = await Promise.all([
    imp('live2dcubismframework.js'), imp('model/cubismusermodel.js'), imp('cubismmodelsettingjson.js'), imp('math/cubismmatrix44.js'),
    imp('effect/cubismeyeblink.js'), imp('effect/cubismbreath.js'), imp('motion/cubismupdatescheduler.js'),
    imp('motion/cubismexpressionupdater.js'), imp('motion/cubismphysicsupdater.js'), imp('motion/cubismeyeblinkupdater.js'),
    imp('motion/cubismbreathupdater.js'), imp('motion/icubismupdater.js'),
  ]);
  const option = new core.Option();
  option.logFunction = (m) => console.log(`[Live2D] ${m}`);
  option.loggingLevel = core.LogLevel.LogLevel_Warning;
  core.CubismFramework.startUp(option);
  core.CubismFramework.initialize();
  fw = { core, user, setting, matrix, blink, breath, sched, expU, physU, blinkU, breathU, updater };
  return fw;
}

// CubismId.getString() 在这版 Framework 里直接是字符串 (老版本是带 .s 的 csmString)
const idName = (hd) => { const s = hd.getString(); return typeof s === 'string' ? s : s.s; };

const fetchBuf = async (url) => {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`取不到 ${url} (${r.status})`);
  return r.arrayBuffer();
};

/**
 * 模型放在画布里的位置 (裁剪坐标, 纯算式, 页面和自检共用): bounds 是默认姿势下所有图元顶点的范围 (模型单位), aspect 是画布宽/高.
 * 脚底贴底边 (-0.995)、横向居中、顶部留 bubbleRoom 给对话栏; r (<= 1) 是"占满窗口的比例", 绕脚底中点缩, r = 1 时和以前逐位相同.
 * 返回 { s (含 r), fit (r = 1 的适配值), low, tx, ty, railY (趴着的栏杆线), faceY (脸的高度) }.
 */
export function placeModel({ minX, maxX, minY, maxY }, aspect, { bubbleRoom = 0.16, frame = 'full', r = 1 } = {}) {
  // frame=bust: 工作台书桌上只露上半身 (头顶到腰, 模型高度的上 42%), 底边以下裁掉; 两边可以出画 (头发、袖子)
  const low = frame === 'bust' ? maxY - (maxY - minY) * 0.42 : minY;
  const fitH = (2 * (1 - bubbleRoom) * 0.97) / (maxY - low), fitW = (1.92 * aspect) / (maxX - minX);
  const fit = frame === 'bust' ? Math.min(fitH, fitW * 1.8) : Math.min(fitH, fitW);
  const s = fit * r;
  return {
    s, fit, low,
    tx: -((minX + maxX) / 2) * s,
    ty: -0.995 - low * s,
    // 趴着 (宿主让她把地面当栏杆): 栏杆线在胸口 (模型高度的 27% 处); 脸在模型高度的 11% 处
    railY: -0.995 + (maxY - (maxY - minY) * 0.27 - low) * s,
    faceY: -0.995 + (maxY - (maxY - minY) * 0.11 - low) * s,
  };
}

/**
 * 加载一个 Live2D 模型 (目录下有 pet-model.json, 见 prepare_live2d.py). opts.pixelRatio: 画布像素比上限.
 * opts.scale: 一开始就占满窗口的比例 (<= 1, 默认 1 = 撑满); 之后可用 avatar.setScale(r) 改 (桌宠的"大小", 见 pet.js).
 * 返回的对象接口见文件开头.
 */
export async function loadLive2DAvatar(stage, dir, { pixelRatio = 1.5, bubbleRoom = 0.16, frame = 'full', scale = 1 } = {}) {
  const t0 = performance.now();
  const F = await loadFramework();
  const pet = await (await fetch(`${dir}pet-model.json`)).json();
  const canvas = document.createElement('canvas');
  canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%';
  stage.append(canvas);
  const dpr = Math.min(window.devicePixelRatio || 1, pixelRatio);
  canvas.width = Math.round(stage.clientWidth * dpr);
  canvas.height = Math.round(stage.clientHeight * dpr);
  const gl = canvas.getContext('webgl2', { alpha: true, premultipliedAlpha: true, antialias: true });
  if (!gl) throw new Error('这台机器的 WebView2 拿不到 WebGL2');

  // 模型: CubismUserModel 自带运动、表情的管理器; 眨眼、呼吸、表情、物理挂在更新调度器上 (和 SDK 示例同样的顺序)
  const model = new F.user.CubismUserModel();
  const settingBuf = await fetchBuf(`${dir}${pet.model3}`);
  const ms = new F.setting.CubismModelSettingJson(settingBuf, settingBuf.byteLength);
  const mocBuf = await fetchBuf(`${dir}${ms.getModelFileName()}`);
  model.loadModel(mocBuf, false);
  const scheduler = new F.sched.CubismUpdateScheduler();
  // 表情: 默认走自己的表情层 (expression-layer.js: 情绪之间连续过渡、会衰减); 页面地址带 expr=cubism 走原来 Framework 的表情管理器 (A/B 用)
  const useLayer = new URLSearchParams(location.search).get('expr') !== 'cubism';
  const expressions = new Map();  // expr=cubism: 名 -> Framework 的表情
  const poseData = new Map();     // 默认: 名 -> exp3 解出来的对象
  for (const e of pet.expressions) {
    const buf = await fetchBuf(`${dir}${e.file}`);
    if (useLayer) poseData.set(e.name, JSON.parse(new TextDecoder().decode(buf)));
    else expressions.set(e.name, model.loadExpression(buf, buf.byteLength, e.name));
  }
  if (!useLayer) scheduler.addUpdatableList(new F.expU.CubismExpressionUpdater(model._expressionManager));
  if (ms.getPhysicsFileName()) {
    const buf = await fetchBuf(`${dir}${ms.getPhysicsFileName()}`);
    model.loadPhysics(buf, buf.byteLength);
    if (model._physics) scheduler.addUpdatableList(new F.physU.CubismPhysicsUpdater(model._physics));
  }
  let motionUpdated = false;
  if (ms.getEyeBlinkParameterCount() > 0) {
    scheduler.addUpdatableList(new F.blinkU.CubismEyeBlinkUpdater(() => motionUpdated, F.blink.CubismEyeBlink.create(ms)));
  }
  const id = (name) => F.core.CubismFramework.getIdManager().getId(name);
  // 表情层: 模型有哪些参数 -> 缺的参数去掉并记录 (report.expression); 在调度器里与原表情更新器同一个执行序, 加在眨眼之后
  let layer = null;
  const layerOpt = { speaking: false, halfLifeS: 0 };  // 每帧复用, 不建对象
  if (useLayer) {
    const cm0 = model.getModel();
    const have = new Set();
    for (let i = 0; i < cm0.getParameterCount(); i++) have.add(idName(cm0.getParameterId(i)));
    const pidx = new Map();  // 参数名 -> 模型里的序号 (按序号读写, 不用每次在参数表里线性找 id)
    const ix = (n) => pidx.get(n) ?? (pidx.set(n, cm0.getParameterIndex(id(n))), pidx.get(n));
    const io = { get: (n) => cm0.getParameterValueByIndex(ix(n)), set: (n, v) => cm0.setParameterValueByIndex(ix(n), v), add: (n, v) => cm0.addParameterValueByIndex(ix(n), v) };
    const range = (n) => { const i = ix(n); return cm0.getParameterMaximumValue(i) - cm0.getParameterMinimumValue(i); };
    layer = new ExpressionLayer({ poses: poseData, emotions: emotionTable(pet), hasParam: (n) => have.has(n), range });
    scheduler.addUpdatableList(createUpdater(F.updater.ICubismUpdater, F.updater.CubismUpdateOrder.CubismUpdateOrder_Expression, layer, io));
    if (layer.report.degraded.length || layer.report.unknown_blend.length) console.warn('[表情层] 退化:', JSON.stringify(layer.report));
  }
  const breath = F.breath.CubismBreath.create();
  breath.setParameters([new F.breath.BreathParameterData(id('ParamBreath'), 0.5, 0.5, 3.2345, 1)]);  // 头和身体的摆动由动作系统给
  scheduler.addUpdatableList(new F.breathU.CubismBreathUpdater(breath));
  // 动作要知道眨眼、口型是哪几个参数 (不设的话播放时会出错; 示例里也是加载完就设)
  const blinkIds = Array.from({ length: ms.getEyeBlinkParameterCount() }, (_, i) => ms.getEyeBlinkParameterId(i));
  const lipIds = Array.from({ length: ms.getLipSyncParameterCount() }, (_, i) => ms.getLipSyncParameterId(i));
  const motions = [];
  for (const file of pet.motions) {
    const buf = await fetchBuf(`${dir}${file}`);
    const m = model.loadMotion(buf, buf.byteLength, file);
    if (m) {
      m.setEffectIds(blinkIds, lipIds);
      m.setFadeInTime(0.6);
      m.setFadeOutTime(0.8);
      motions.push(m);
    }
  }

  // 渲染: 贴图按预乘 alpha 上传 (和示例一样), 着色器由 Framework 自己按路径取
  model.createRenderer(canvas.width, canvas.height);
  const renderer = model.getRenderer();
  renderer.startUp(gl);
  renderer.loadShaders(SHADERS);
  const textures = [];
  for (let i = 0; i < ms.getTextureCount(); i++) {
    const blob = await (await fetch(`${dir}${ms.getTextureFileName(i)}`)).blob();
    const bmp = await createImageBitmap(blob, { premultiplyAlpha: 'premultiply', colorSpaceConversion: 'none' });
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    // 不用缩小版贴图 (mipmap): 所有部件挤在一张 2048 的图集上, 桌宠的脸只有原图的七分之一左右, 用缩小版时嘴线那一小块
    // 会混进图集里相邻部件的像素, 闭着的嘴成了一团乱线, 整体也发糊. 不用缩小版和 Live2D 自家的查看器、VTube Studio 一样, 嘴是干净的短线.
    // l2dmip=full|1|2 比较用 (最多用到第几级)
    const mip = new URLSearchParams(location.search).get('l2dmip') ?? '0';
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, mip === '0' ? gl.LINEAR : gl.LINEAR_MIPMAP_LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, bmp);
    gl.generateMipmap(gl.TEXTURE_2D);
    if (mip !== 'full' && mip !== '0') gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAX_LEVEL, Number(mip));
    gl.bindTexture(gl.TEXTURE_2D, null);
    bmp.close();
    textures.push(tex);
    renderer.bindTexture(i, tex);
  }
  // 缩小后的纹理采样: 上面说的"不用缩小版"是按撑满窗口 (r = 1) 定的; 模型缩小以后一个屏幕像素盖住好几个贴图点,
  // 不用缩小版时发丝和衣服边缘会闪成锯齿 (r = 0.35 最明显). 按 r 选最多用到第几级缩小版: r >= 0.9 不用 (和以前完全一样),
  // 0.45 <= r < 0.9 用到第 1 级, 更小用到第 2 级 (A/B: 缩小后和"撑满画面再缩小"的参考图比, 平均绝对差 r=0.5 从 8.9 降到 5.5, r=0.35 从 12.6 降到 5.7).
  // 页面地址带 l2dmip (自检) 时不自动.
  const lodFor = (r) => (r >= 0.9 ? 0 : r >= 0.45 ? 1 : 2);
  let autoLod = (new URLSearchParams(location.search).get('l2dmip') ?? '0') === '0';
  let lod = autoLod ? 0 : -1;
  const applyLod = (level) => {
    if (level === lod) return;
    lod = level;
    for (const tex of textures) {
      gl.bindTexture(gl.TEXTURE_2D, tex);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, level > 0 ? gl.LINEAR_MIPMAP_LINEAR : gl.LINEAR);
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAX_LEVEL, level > 0 ? level : 1000);
    }
    gl.bindTexture(gl.TEXTURE_2D, null);
  };
  if (autoLod) applyLod(lodFor(scale));
  renderer.setIsPremultipliedAlpha(true);

  // 摆放: 先更新一次拿到默认姿势, 量所有图元顶点的范围 (模型画布常带空白), 脚底贴底边、头顶留出对话栏, 横向居中
  const cm = model.getModel();
  cm.update();
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (let i = 0; i < cm.getDrawableCount(); i++) {
    const v = cm.getDrawableVertices(i);
    for (let k = 0; k < v.length; k += 2) {
      minX = Math.min(minX, v[k]); maxX = Math.max(maxX, v[k]);
      minY = Math.min(minY, v[k + 1]); maxY = Math.max(maxY, v[k + 1]);
    }
  }
  const aspect = canvas.width / canvas.height;
  const bounds = { minX, maxX, minY, maxY };
  const mm = model.getModelMatrix();
  const zoom = Number(new URLSearchParams(location.search).get('l2dzoom') || 0);  // 自检: 把脸放大来看细节 (l2dzoom=5)
  let place = placeModel(bounds, aspect, { bubbleRoom, frame, r: scale });
  const setMatrix = () => {
    const s = place.s;
    mm.loadIdentity();
    mm.scale(s, s);
    mm.translate(place.tx, place.ty);
    if (zoom > 1) {
      const faceY = maxY - (maxY - minY) * 0.11;
      mm.scale(s * zoom, s * zoom);
      mm.translate(-((minX + maxX) / 2) * s * zoom, -faceY * s * zoom);
    }
  };
  setMatrix();
  const P = new F.matrix.CubismMatrix44();

  // 每帧的状态
  const P_ = {
    angleX: id('ParamAngleX'), angleY: id('ParamAngleY'), angleZ: id('ParamAngleZ'), bodyX: id('ParamBodyAngleX'), bodyY: id('ParamBodyAngleY'),
    bodyZ: id('ParamBodyAngleZ'), mouthOpen: id('ParamMouthOpenY'), mouthForm: id('ParamMouthForm'), eyeX: id('ParamEyeBallX'),
    eyeY: id('ParamEyeBallY'), browL: id('ParamBrowLY'), browR: id('ParamBrowRY'),
  };
  const st = { t: 0, emotion: 'neutral', actions: [], glance: null, brows: [], nextIdle: 8 + Math.random() * 10, peaks: {}, drawn: 0,
    follow: null, eye: { x: 0, y: 0 }, head: { x: 0, y: 0 }, lean: { on: false, w: 0, tilt: null },
    presence: null };  // 闲时状态机和对话反应的输出 (idle-director.js), setPresence 给
  // 趴着 (宿主让她把地面当栏杆): Eustia 的手是一张交握在身前的图, 叠不了胳膊; 改成从栏杆后面探出上半身:
  // 栏杆线在胸口 (模型高度的 27% 处), 身体往前倾一点、歪着头. 栏杆线以下由页面裁掉
  // railY / faceY: 栏杆线和脸在画布里的高度 (裁剪坐标), 跟着 place 走 (setScale 会换)
  const track = (k, v) => { st.peaks[k] = Math.max(st.peaks[k] || 0, Math.abs(v)); };

  const avatar = {
    kind: 'live2d',
    info: { name: pet.name, display: pet.display },
    skills: pet.skills,
    renderer: { domElement: canvas },
    expressionNames: [...(useLayer ? poseData : expressions).keys()],
    report: { live2d: true, model: pet.name, expressions: useLayer ? poseData.size : expressions.size, motions: motions.length, canvas: [canvas.width, canvas.height],
      fit: +place.fit.toFixed(4), scale: scale, expression_mode: useLayer ? 'layer' : 'cubism', expression: layer?.report || null },

    /** 每帧: 动作系统的通道 ch (度), face {viseme, arkit, ...}, 表演层的颈部偏移 neck. 算好参数后画出来. */
    apply(ch, face, dt, neck = { roll: 0, pitch: 0 }) {
      st.t += dt;
      cm.loadParameters();
      motionUpdated = false;
      if (!model._motionManager.isFinished()) motionUpdated = model._motionManager.updateMotion(cm, dt);
      else if (st.t > st.nextIdle && !(face?.viseme?.level > 0.02)) {  // 空闲 (不在说话): 偶尔放一段模型自带的动作
        // 犯困、睡着、醒来的时候不放 (闲时状态机说了算, 见 setPresence); 过了点就等下一轮
        if (motions.length && st.presence?.idleMotions !== false) model._motionManager.startMotionPriority(motions[Math.floor(Math.random() * motions.length)], false, 1);
        st.nextIdle = st.t + IDLE_MOTION_S[0] + Math.random() * (IDLE_MOTION_S[1] - IDLE_MOTION_S[0]);
      }
      cm.saveParameters();
      // 跟着鼠标: 眼睛快、头慢 (先转眼再转头); 不跟时都回到正前方 (看着用户)
      const f = st.follow || { yaw: 0, pitch: 0 };
      const ke = 1 - Math.exp(-dt / 0.08), kh = 1 - Math.exp(-dt / 0.35);
      st.eye.x += (clamp(f.yaw / 25, -1, 1) - st.eye.x) * ke;
      st.eye.y += (clamp(f.pitch / 25, -1, 1) - st.eye.y) * ke;
      st.head.x += (clamp(f.yaw * 0.45, -14, 14) - st.head.x) * kh;
      st.head.y += (clamp(f.pitch * 0.4, -10, 10) - st.head.y) * kh;
      cm.addParameterValueById(P_.eyeX, st.eye.x);
      cm.addParameterValueById(P_.eyeY, st.eye.y);
      track('eyeX', st.eye.x); track('eyeY', st.eye.y);
      // 头和身体: 动作系统 (含点头、摇头、思考、鞠躬) + 自己的头部动作 + 表演层 + 跟着鼠标
      let ay = -(ch.hp || 0) * 1.3 - (neck.pitch || 0) + st.head.y, ax = (ch.hy || 0) * 1.3 + st.head.x;
      let az = -(ch.hr || 0) * 1.5 - (neck.roll || 0);
      // 趴着: 往前探、歪头 (被点一下时再往另一边歪一下)
      const L = st.lean;
      L.w += ((L.on ? 1 : 0) - L.w) * (1 - Math.exp(-dt / (L.on ? 0.45 : 0.3)));
      const lk = L.w * L.w * (3 - 2 * L.w);
      const tilt = L.tilt ? clamp(Math.min((st.t - L.tilt.start) / 0.25, (L.tilt.start + 1.8 - st.t) / 0.35), 0, 1) * L.tilt.dir : 0;
      ay -= 3 * lk;
      az += 7 * lk + 9 * tilt;
      const lean = ch.lean || 0;
      for (const a of st.actions) {
        const k = (st.t - a.start) / a.dur;
        if (k < 0 || k > 1) continue;
        const env = Math.sin(Math.PI * k);
        if (a.kind === 'nod') ay -= a.amp * 14 * Math.abs(Math.sin(Math.PI * a.reps * k)) * env;
        else ax += a.amp * 18 * Math.sin(2 * Math.PI * a.reps * k) * env;
      }
      st.actions = st.actions.filter((a) => st.t - a.start < a.dur);
      ay -= Math.max(0, lean) * 0.5;
      // 闲时状态机和对话反应 (走神、犯困、睡、醒来、打字时看输入框…): 头的偏移叠加在上面 (趴着时也叠, 上限不变)
      const pr = st.presence;
      if (pr) { ay -= pr.head.pitch; ax += pr.head.yaw; az += pr.head.roll; }
      cm.addParameterValueById(P_.angleY, clamp(ay, -30, 30));
      cm.addParameterValueById(P_.angleX, clamp(ax, -30, 30));
      cm.addParameterValueById(P_.angleZ, clamp(az, -30, 30));
      cm.addParameterValueById(P_.bodyY, clamp(-lean * 0.35 - 6 * lk, -10, 10));
      cm.addParameterValueById(P_.bodyX, clamp(ax * 0.25, -10, 10));
      cm.addParameterValueById(P_.bodyZ, clamp(az * 0.3, -10, 10));
      track('angleY', ay); track('angleX', ax); track('angleZ', az); track('bodyY', lean * 0.35);
      // 视线: 瞟一眼 (表演层) 之后回来看着用户
      if (st.glance) {
        const g = st.glance, k = clamp((st.t - g.start) / 0.25, 0, 1) * clamp((g.end - st.t) / 0.3, 0, 1);
        cm.addParameterValueById(P_.eyeX, clamp(g.x, -1, 1) * k);
        cm.addParameterValueById(P_.eyeY, clamp(g.y, -1, 1) * k);
        if (st.t > g.end) st.glance = null;
      }
      if (pr) {
        cm.addParameterValueById(P_.eyeX, clamp(pr.gaze.x, -1, 1));
        cm.addParameterValueById(P_.eyeY, clamp(pr.gaze.y, -1, 1));
      }
      // 眉毛: 说话时的强调 (表演层的 pulseAU)
      let brow = 0;
      for (const b of st.brows) brow += b.amount * Math.max(0, 1 - (st.t - b.start) / b.dur);
      st.brows = st.brows.filter((b) => st.t - b.start < b.dur);
      if (brow) { cm.addParameterValueById(P_.browL, clamp(brow, 0, 1)); cm.addParameterValueById(P_.browR, clamp(brow, 0, 1)); }
      // 口型: 有 A2F 的面部数据就用下巴开合, 否则按音频的口形权重
      const vis = face?.viseme || {}, ark = face?.arkit;
      const open = clamp(Math.max((ark?.jawOpen || 0) * 1.6, (vis.level || 0) * 1.4, (vis.aa || 0) * 1.1, (vis.oh || 0) * 0.9), 0, 1);
      const form = clamp(((vis.ee || 0) + (vis.ih || 0)) * 0.9 - ((vis.ou || 0) + (vis.oh || 0)) * 0.9, -1, 1);
      cm.setParameterValueById(P_.mouthOpen, open);
      if (open > 0.02) cm.setParameterValueById(P_.mouthForm, form);
      track('mouth', open);
      if (layer) {  // 表情层往前走一帧 (她在出声时情绪保持); 写回在调度器里的 LayerUpdater
        layer.setAU(pr?.au);
        layerOpt.speaking = (vis.level || 0) > 0.02;
        layerOpt.halfLifeS = pr ? pr.halfLifeS : 0;
        layer.update(dt, layerOpt);
      }
      scheduler.onLateUpdate(cm, dt);
      cm.update();
      if (st.probe) st.probe.cb(st.probe.handles.map((hd) => cm.getParameterValueById(hd)), dt);  // dt = 这一帧层实际用的帧间隔
      // 画
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
      P.loadIdentity();
      P.scale(1 / aspect, 1);
      P.multiplyByMatrix(mm);
      renderer.setMvpMatrix(P);
      renderer.setRenderState(null, [0, 0, canvas.width, canvas.height]);
      renderer.drawModel(SHADERS);
      st.drawn++;
    },

    /** 情绪 -> 表情 (pet-model.json 的对应); 中性、平静时收掉表情. */
    setEmotion(label, strength) {
      if (layer) {  // 强度这一步忽略 (一律 1, 见 expression-layer.js); 同标签再设一次会重新计时 (衰减完又来一个 happy 要回到满)
        st.emotion = label;
        layer.setEmotion(label, strength);
        return;
      }
      if (label === st.emotion) return;
      st.emotion = label;
      const name = pet.emotions?.[label];
      const exp = name && expressions.get(name);
      if (exp) model._expressionManager.startMotion(exp, false);
      else model._expressionManager.stopAllMotions();
    },

    /** 直接用表情名 (自检和面板预览用). */
    setExpression(name) {
      if (layer) { layer.setExpression(name); return; }
      const exp = expressions.get(name);
      if (exp) model._expressionManager.startMotion(exp, false);
      else model._expressionManager.stopAllMotions();
    },

    /** 点头 / 摇头 (talk.js 和表演层用): 叠在动作系统的角度上. */
    headAction(kind, { amplitude = 0.6, speed = 1, repetitions = 2, duration = 0.7 } = {}) {
      if (kind !== 'nod' && kind !== 'shake') return;
      const dur = Math.max(0.3, (duration * repetitions) / Math.max(0.3, speed));
      st.actions.push({ kind, amp: clamp(amplitude, 0, 1.5), reps: repetitions, start: st.t, dur });
    },

    lookAtUser() { st.glance = null; },

    /** 闲时状态机和对话反应的输出 (idle-director.js 的 update 返回值, 每帧同一个对象); null 收掉. 头、视线、AU 每帧叠上去. */
    setPresence(out) { st.presence = out; },

    /** 被打断时: 头上正在做的点头 / 摇头清掉 (头回到原位). */
    cancelHeadActions() { st.actions.length = 0; },

    /** 表情层的输出相邻帧最大变化 (占参数全幅的比例), 读完清零; expr=cubism 时没有. 自检用. */
    exprStats() { return layer ? layer.takeStats() : null; },

    /** 自检: 每帧画完把这些参数的最终值 (数组) 和这一帧用的 dt (秒) 交给 cb, 返回各参数全幅; names 给 null 停止. */
    watchParams(names, cb) {
      if (!names) { st.probe = null; return []; }
      const handles = names.map((n) => id(n));
      st.probe = { handles, cb };
      return handles.map((hd) => { const i = cm.getParameterIndex(hd); return cm.getParameterMaximumValue(i) - cm.getParameterMinimumValue(i); });
    },


    /** 跟着鼠标看: yaw 正值看屏幕右边, pitch 正值向上 (度); null 回来看着用户. 瞟一眼 (glance) 叠在上面. */
    follow(yaw, pitch) { st.follow = yaw === null ? null : { yaw, pitch }; },

    /**
     * 桌宠的"大小": r (<= 1) 是占满窗口的比例, 绕脚底中点缩 (脚底、横向居中不变); 画布不变. 马上生效 (下一帧画出来).
     * 摆放同时影响头的位置 (headScreen) 和趴着时的栏杆线 (lean.railFrac), 它们都读最新的 place.
     */
    setScale(r) {
      const v = clamp(Number(r) || 1, 0.05, 1);
      if (v === scale) return;
      scale = v;
      place = placeModel(bounds, aspect, { bubbleRoom, frame, r: v });
      setMatrix();
      if (autoLod) applyLod(lodFor(v));
      avatar.report.scale = v;
    },

    /** 自检 (缩小后的纹理采样 A/B): 数字 = 固定用到第几级缩小版 (0 = 不用); null = 回到按大小自动选. */
    setTextureLod(level) {
      if (level === null || level === undefined) { autoLod = true; applyLod(lodFor(scale)); } else { autoLod = false; applyLod(level); }
    },

    /** 现在的摆放 (自检用): 占满窗口的比例和模型在画布里的位置. */
    placement() { return { r: scale, s: place.s, fit: place.fit, railY: place.railY, faceY: place.faceY, low: place.low }; },

    /** 头 (脸) 在窗口里的位置, CSS 像素. */
    headScreen() { return [stage.clientWidth / 2, ((1 - place.faceY) / 2) * stage.clientHeight]; },

    /** 趴着 (接口和 desk-body.js 的 leaner 对齐): set(on); w (0..1); poke(kind) 只会歪头; railFrac() 趴好时栏杆线在窗口里的位置. */
    lean: {
      get on() { return st.lean.on; },
      get w() { return st.lean.w; },
      set(on) { st.lean.on = !!on; },
      poke() {
        st.lean.tilt = { start: st.t, dir: st.lean.tilt?.dir === 1 ? -1 : 1 };
        return 1.8;
      },
      railFrac: () => (1 - place.railY) / 2,
      footFrac: () => (1 + 0.995) / 2,
    },

    /** 自检: 现在眼睛和头转到哪了. */
    gazeNow() { return { eye_x: +st.eye.x.toFixed(3), eye_y: +st.eye.y.toFixed(3), head_x: +st.head.x.toFixed(2), head_y: +st.head.y.toFixed(2) }; },

    /** 瞟一眼: yaw / pitch 是度, hold 秒. */
    glance(yaw, pitch, hold = 1) { st.glance = { x: yaw / 25, y: pitch / 25, start: st.t, end: st.t + hold }; },

    /** 表演层的面部动作单元: 这里只认挑眉 (au01 / au02). */
    pulseAU(name, amount = 0.3, dur = 0.5) {
      if (/au0[12]/.test(name)) st.brows.push({ amount, start: st.t, dur });
    },

    /** 自检: 各参数在这段时间里的最大幅度, 读完清零. */
    takePeaks() {
      const p = st.peaks;
      st.peaks = {};
      return Object.fromEntries(Object.entries(p).map(([k, v]) => [k, +v.toFixed(3)]));
    },
  };
  avatar.report.load_ms = Math.round(performance.now() - t0);
  return avatar;
}
