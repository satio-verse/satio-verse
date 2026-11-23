// HAST-SEC 运动层适配 (非商业研究许可, 见 third_party/hast-sec/LICENSE 与 MODIFICATIONS.md).
//
// 只调用上游原始模块, 不改其源码. 初始化顺序照搬上游 web/v13_lab.mjs 与 feasible_view.mjs, 区别是:
//   - 不创建上游的渲染器和页面, 直接在 WorldModel 已加载的 VRM 上建姿态控制器和第九版可行性求解器;
//   - 关闭上游面部驱动 (面部交给 SoullinkEmotion3D);
//   - 指令来自我们的单次流式 LLM, 而不是上游的千问两次调用.
// 底座行为 (待机/聊天/倾听) 来自用 CMU 与 BEAT2 动捕训练的小模型, 技能 (挥手/抬手/抬腿等) 是动捕片段.
import { Box3 } from 'three';

const H = '/hast';
const clamp = (x, a, b) => Math.max(a, Math.min(b, Number.isFinite(x) ? x : a));
const SIDES = { L: ['left'], R: ['right'], B: ['left', 'right'] };

/** LLM 的动作 -> 上游技能键. 上游没有的动作 (点头/摇头/耸肩/思考/鞠躬) 交给头部和表情层表达. */
export function toHastIntents(acts) {
  const out = [];
  for (const a of acts || []) {
    let motion = null, strength = 1;
    if (a.do === 'wave') motion = '挥手打招呼';
    else if (a.do === 'raise') motion = a.amp >= 0.9 ? '向上举过头部' : a.amp >= 0.65 ? '向上抬到头部高度' : '向上抬高';
    else if (a.do === 'point') { motion = '向前移动'; strength = clamp(a.amp, 0.3, 1); }
    else if (a.do === 'leg') motion = '抬腿';
    if (!motion) continue;
    const sides = a.do === 'leg' && a.side === 'B' ? ['right'] : SIDES[a.side] || ['right'];  // 双腿同时抬站不住
    for (const side of sides) {
      out.push({ key: `${side}-${motion}`, strength, speed: clamp(a.spd, 0.4, 2), hold: 2, delay: clamp(a.at || 0, 0, 30) });
    }
  }
  return out;
}

export async function createHastBody(vrm) {
  const load = (p) => import(`${H}/${p}`);
  const [{ generation }, { skillAssets }, { feasibleData }, { loadDirectionModel }, { beatRuntimeV9 }, { beatFrame },
    { controllerV9 }, { fixedClock }, { beatLayers }, { PoseController }, { createClearance }, { morphMetric },
    { poseState }, { FACE_ORDER }] = await Promise.all([
    'web/v14_generation.mjs', 'web/v13_assets.mjs', 'web/feasible_data.mjs', 'web/beat_direction_model.mjs',
    'training/beat_runtime_v9.mjs', 'web/beat_model.mjs', 'motion/v9/controller.mjs', 'motion/v9/clock.mjs',
    'motion/beat_layers.mjs', 'motion/pose_controller.mjs', 'hands/clearance.mjs', 'face/morph_metric.mjs',
    'motion/feasible_state.mjs', 'face/feasible_face.mjs'].map(load));

  const selected = await generation();
  const behavior = selected.behavior;
  const assets = await skillAssets(selected.bootstrap);
  const data = await feasibleData({
    motionFactory: () => loadDirectionModel({
      bodyFactory: (m, w, c) => beatRuntimeV9(m, w, c, { policy: behavior.policy }), prefix: '/baseline-v9-filtered-data/' }),
    configUrl: '/configs/v9_control.json' });
  const meta = data.motion.meta;
  if (JSON.stringify(assets.library.bones) !== JSON.stringify(meta.bones)) throw new Error('HAST-SEC 技能库与身体模型骨架不一致');
  data.config.arm_collision_enabled = true;
  data.config.cached_derivative = true;

  // 上游服务按当前角色文件改写 vrm_version; 我们的角色可以在页面上切换, 所以在这里按已加载的模型填.
  const profile = await (await fetch('/configs/skeleton_profile.json')).json();
  profile.vrm_version = vrm.meta?.metaVersion === '0' ? '0' : '1';
  const controller = new PoseController(vrm, profile);
  controller.reset();
  vrm.scene.position.y -= new Box3().setFromObject(vrm.scene).min.y;  // 脚落地, 接触约束以 y=0 为地面
  controller.sync();
  createClearance(controller).shoulders(data.config.shoulder_offset_m ?? 0.02);
  // 求解器的面部投影要求通道表非空, 所以照常给它模型自带的标准表情; 它写进表情管理器的值
  // 会在 VRMAvatar.apply 里被清零, 面部最终以 Soullink 为准.
  const faceNames = FACE_ORDER.filter((n) => vrm.expressionManager?.getExpression(n));
  const solver = controllerV9(controller, { model: data.history, config: data.config, faceNames,
    faceMetric: morphMetric(vrm, FACE_ORDER), faceOrder: FACE_ORDER, faceGroups: [], regions: data.regions });
  const policy = await selected.policy(assets.library);
  const layers = beatLayers(meta.bones, assets.sources);
  const stepOptions = { lockedFeet: false, temporal: true, regionsEnabled: true, manualFace: {},
    forwardLeanDegrees: data.config.playback_torso_forward_degrees ?? 0, faceBehavior: null };
  let history = [], basePose = null, raw = null;

  // 20 Hz 固定步长, 与上游 v13_lab 的 step 相同.
  function step(dt) {
    behavior.lock(layers.active());
    const row = behavior.sample(dt), frame = beatFrame(row.values, meta);
    basePose = frame.rotations;
    raw = layers.sample(basePose, dt);
    frame.rotations = raw;
    frame.rootObserved = false;
    const leg = layers.state().tracks.find((t) => t.part === 'leg' && t.weight > 0.1);
    frame.swingFoot = leg ? `${leg.side}Foot` : null;
    const input = poseState({ ...frame, expressions: {} }, controller, solver.rig.offsets[0], meta.face);
    const result = solver.step(input, dt, stepOptions);
    solver.write(result);
    history.push(result.rotations.map((q) => q.map((v, i) => (i === 0 || i === 2 ? v * controller.direction : v))));
    if (history.length > 20) history.shift();
    if (result.last.requires_pause) throw new Error('HAST-SEC 姿态求解连续失败');
    return result;
  }
  const clock = fixedClock(step, (r) => solver.validate?.(r) ?? true, 20, meta.bones);
  behavior.reset(data.motion);
  layers.reset();
  solver.reset();
  clock.reset();
  clock.advance(0);

  const counts = { performed: 0, skipped: [] };
  return {
    name: 'HAST-SEC',
    /** 每帧调用: 推进 20 Hz 求解时钟, 把插值后的姿态写进骨骼. */
    update(dt) {
      clock.advance(Math.min(dt, 0.2));
      const shown = clock.display();
      if (shown) solver.write(shown);
    },
    /** 执行意图. op 为 stop 时先让所有轨道淡出. */
    async perform(intents, op) {
      if (op === 'stop') layers.clear();
      for (const intent of intents) {
        const entry = assets.entries.get(intent.key);
        if (!entry) { counts.skipped.push(intent.key); continue; }
        await assets.load(entry);
        if (entry.side === 'body') { layers.clear(); continue; }
        const choice = policy.select(entry, history);
        layers.add(choice.entry, { ...intent, ...choice.options }, raw ?? basePose);
        counts.performed++;
      }
      behavior.lock(layers.active());
    },
    speaking: false,  // 行动层用它决定: 说话时把身体让给 HAST-SEC 的聊天手势, 不说话时播 StayStill 待机
    setSpeaking(on) { this.speaking = !!on; behavior.setSpeaking(!!on); },
    setListening(on) { behavior.setListening(!!on); },
    state: () => ({ behavior: behavior.state(), tracks: layers.state().tracks, solver: solver.report().last, counts }),
  };
}
