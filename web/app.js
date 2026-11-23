import { LAYER_INDEX, LAYERS, PARTS, summarize, topLayers } from './arbiter.js';
import { AudioOut, MicIn } from './audio.js';
import { Avatar2D } from './avatar2d.js';
import { Dashboard } from './dashboard.js';
import { ZERO } from './lipsync.js';
import { FrameMeter, ResourcePanel } from './resources.js';
import { MotionSystem } from './motion.js';
import { PAIN_TERMS } from './pain.js';
import { SpeechPerformer } from './performance.js';

const $ = (sel) => document.querySelector(sel);
const HEADER_BYTES = 20; // <4sIIII: magic, turn, seg, sample_rate, seq
const SKILL_NAMES = { wave: '挥手', raise: '举手', point: '指向', nod: '点头', shake: '摇头', shrug: '耸肩', think: '思考', bow: '鞠躬', leg: '抬腿',
  go: '走到', look: '看向', turn: '转身', sit: '坐下', stand: '站起来', pick: '拿起', put: '放下', hand: '伸手牵手',
  heart: '比心', giggle: '捂嘴笑', chin: '托腮', clap: '拍手', peace: '比耶', cheeks: '捧脸' };
const SIDE_NAMES = { L: '左', R: '右', B: '双' };
const TURN_NAMES = { L: '向左', R: '向右', B: '向后' };
const SPATIAL = new Set(['go', 'look', 'turn', 'sit', 'stand', 'pick', 'put']);
// 要花时间的动作按顺序执行: 前一个做完 (走到了/坐下了/拿到了) 才开始下一个, 后面的 at 从那一刻算起
const LONG = new Set(['go', 'turn', 'sit', 'stand', 'pick', 'put']);
const OP_NAMES = { act: '动作', chat: '聊天', stop: '停止', na: '做不到' };
const EXAMPLES = ['你好呀', '举起右手', '先挥挥左手，再点点头', '两只手一起举过头顶', '我今天考试没考好', '给我表演个后空翻',
  '走到窗户那边看看', '去沙发那里然后挥挥手', '指一下台灯', '过来一下', '去沙发上坐一会儿', '把球捡起来', '把书放到书架上', '站起来吧',
  '你手里拿的是什么', '放回去吧'];

const ui = {
  chat: $('#chat'), partial: $('#partial'), form: $('#composer'), input: $('#text'),
  mic: $('#mic'), interrupt: $('#interrupt'), half: $('#half-duplex'), vrm: $('#vrm-file'), reset: $('#reset'),
  status: $('#status'), stage: $('#stage'), canvas: $('#avatar'), toast: $('#toast'), level: $('#mic-level'),
  examples: $('#examples'), clock: $('#clock'), avatarSelect: $('#avatar-select'), avatarStatus: $('#avatar-status'),
  camera: $('#camera-mode'),
};

const state = {
  ws: null, offset: 0, pings: [], pingId: 0,
  ctx: null, out: null, mic: null,
  motion: new MotionSystem(), avatar: new Avatar2D(ui.canvas), vrm: null,
  dropUpTo: 0, emo: 'neutral', asrRate: 16000, status: null,
  bubbles: new Map(),
  performer: new SpeechPerformer(), segText: new Map(), segSeen: new Set(),
  scene: null, targetNames: {},
};
const dashboard = new Dashboard($('#latency'));

// ---------------------------------------------------------------------------
// 连接与对时: 浏览器 performance.now() + offset = 服务端时钟, 用 RTT 最小的样本估计 offset.
function send(obj) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify(obj));
}

function mark(turn, name, perfMs = performance.now()) {
  send({ type: 'mark', turn, name, t: perfMs + state.offset });
}

function ping() {
  send({ type: 'ping', id: ++state.pingId, t_client: performance.now() });
}

function onPong(msg) {
  const now = performance.now();
  const rtt = now - msg.t_client;
  state.pings.push({ rtt, offset: msg.t_server - (msg.t_client + rtt / 2) });
  if (state.pings.length > 12) state.pings.shift();
  const best = state.pings.reduce((a, b) => (b.rtt < a.rtt ? b : a));
  state.offset = best.offset;
  ui.clock.textContent = `对时 RTT ${best.rtt.toFixed(1)} ms`;
}

function connect() {
  const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';
  state.ws = ws;
  ws.onopen = () => {
    state.pings = [];
    for (let i = 0; i < 8; i++) setTimeout(ping, i * 60);
    reportState(true);
    reportCapabilities();
  };
  ws.onclose = () => {
    renderStatus(null);
    setTimeout(connect, 1500);
  };
  ws.onmessage = (e) => {
    if (e.data instanceof ArrayBuffer) onAudio(e.data);
    else handle(JSON.parse(e.data));
  };
}
setInterval(ping, 10000);

// 可行性声明: 这个身体此刻能做哪些技能. 服务端据此去掉做不到的动作 (全都做不到时改判成"做不到", 让角色坦白),
// 而不是在前端悄悄跳过. 换模型、行动层就绪时重新声明.
const GESTURES = ['wave', 'raise', 'point', 'nod', 'shake', 'shrug', 'think', 'bow'];
const EMOTE_ACTS = ['heart', 'giggle', 'chin', 'clap', 'peace', 'cheeks'];  // 服务端 wm/intent.py 的 EMOTES
function reportCapabilities() {
  const v = state.vrm;
  // hand (伸手邀请牵手) 是行动层的手臂 IK 加手部动作层的手形, 只有挂了行动层才做得出
  // 表情动作 (比心、捂嘴笑、托腮、拍手、比耶、捧脸) 是行动层的 _emote (手臂 IK 到脸旁、胸前加手形), 同样要挂了行动层
  const skills = [...GESTURES, ...(v ? ['leg'] : []),
    ...(v?.loco ? ['go', 'look', 'turn', 'sit', 'stand', 'pick', 'put', 'hand', ...EMOTE_ACTS] : [])];
  send({ type: 'capabilities', skills });
}

// 角色状态 (在哪、坐着还是站着、手里拿着什么) 变了就告诉服务端, LLM 回答"你手里拿的是什么""放回去"要用.
let lastState = '';
function reportState(force = false) {
  const loco = state.vrm?.loco;
  if (!loco) return;
  // 开着摄像头时, 带上看到的用户姿态 (固定取值的几个字段, 服务端按白名单写进状态行)
  const snap = JSON.stringify({ ...loco.snapshot(), ...(state.userPose?.running ? { user: state.userPose.summary || { present: false } } : {}) });
  if (force || snap !== lastState) {
    lastState = snap;
    send({ type: 'state', state: JSON.parse(snap) });
  }
}
setInterval(reportState, 400);

async function ensureAudio() {
  if (!state.ctx) {
    state.ctx = new AudioContext({ latencyHint: 'interactive' });
    state.out = new AudioOut(state.ctx);
  }
  if (state.ctx.state !== 'running') await state.ctx.resume();
}

// ---------------------------------------------------------------------------
// 消息处理
const nowS = () => performance.now() / 1000;

function handle(msg) {
  switch (msg.type) {
    case 'hello':
      // 一条新连接就是服务端新开的会话, 轮号从 1 重新数: 上一条连接按轮号记的都清掉 (2026-11-08: 被打断过以后断线重连,
      // 新连接头几轮的意图、情绪、声音都被当成旧轮扔了, 和桌宠 desktop/web/talk.js 同一个毛病); 气泡表也清, 旧气泡留在对话里
      state.dropUpTo = 0;
      state.segText.clear();
      state.segSeen.clear();
      state.bubbles.clear();
      state.out?.forgetTurn();
      state.asrRate = msg.asr_sample_rate || 16000;
      renderStatus(msg.status);
      dashboard.init(msg.marks, msg.metrics, msg.stats);
      break;
    case 'pong': onPong(msg); break;
    case 'asr_partial': ui.partial.textContent = msg.text; break;
    case 'listening':
      state.motion.backchannel('listen', nowS());
      state.performer.setListening(true, nowS());
      state.vrm?.body?.setListening(true);
      break;
    case 'listening_end':  // 那一段声音没认出话、或是外放的回声: 不算在说话了
      ui.partial.textContent = '';
      state.performer.setListening(false, nowS());
      state.vrm?.body?.setListening(false);
      break;
    case 'turn_start':
      ui.partial.textContent = '';
      state.performer.setListening(false, nowS());
      state.vrm?.body?.setListening(false);
      state.segText.clear();
      state.segSeen.clear();
      addTurn(msg.turn, msg.text, msg.source);
      break;
    case 'backchannel':
      state.motion.backchannel(msg.motion, nowS());
      if (msg.motion === 'think') state.performer.thinking();
      mark(msg.turn, 'client_backchannel');
      break;
    case 'intent': {
      if (msg.turn <= state.dropUpTo) break;
      if (msg.op === 'stop') state.vrm?.loco?.stop();
      if (msg.uncertain) state.performer.thinking();  // 模型自己不太确定 (没能复核): 动作前先抬眼想一下
      runSequence(msg.acts, msg.op, msg.turn);
      mark(msg.turn, 'client_intent');
      const tags = [msg.escalated ? '已复核' : msg.uncertain ? '不确定' : null].filter(Boolean);
      addChips(msg.turn, [OP_NAMES[msg.op] || msg.op, ...msg.acts.map(describeAct), ...tags], msg.fallback || msg.uncertain ? 'warn' : '');
      break;
    }
    case 'emotion':
      if (msg.turn <= state.dropUpTo) break;
      state.motion.setEmotion(msg.vad, nowS());
      if (state.vrm) state.vrm.setEmotion(msg.emo);
      state.vrm?.loco?.setStyle(msg.emo);
      state.performer.setArousal(msg.vad[1]);
      state.emo = msg.emo;
      addChips(msg.turn, [`情绪 ${msg.emo}`], 'emo');
      break;
    case 'reply_delta': appendReply(msg.turn, msg.text); break;
    case 'amend':  // 回复侧检查: 模型嘴上答应了做不到的事, 服务端改判成"做不到"
      if (msg.turn > state.dropUpTo) addChips(msg.turn, [OP_NAMES[msg.op] || msg.op, '回复检查'], 'warn');
      break;
    case 'audio_seg': state.segText.set(`${msg.turn}:${msg.seg}`, msg.text); break;
    case 'face': if (msg.turn > state.dropUpTo) state.out?.addFace(msg.turn, msg); break;
    case 'prosody': {  // 服务端韵律事件: 句内秒数换算成音频时钟, 交给说话表演
      if (msg.turn <= state.dropUpTo || !state.out || msg.turn !== state.out.turn) break;
      const events = msg.events.map((e) => ({ ...e, t: state.out.segTime(msg.seg, e.t) })).filter((e) => e.t !== null);
      state.performer.addProsody(events);
      break;
    }
    case 'audio_end':
      if (state.out) {
        if (state.out.turn === msg.turn && state.out.started) state.performer.endTurn(state.out.nextTime);
        const ws = state.ws;  // 放完时要是已经换了一条连接: 那边是新会话, 同一个轮号是另一轮, 这个打点不报
        state.out.endTurn(msg.turn, (endPerf) => {
          if (state.ws === ws) mark(msg.turn, 'client_audio_end', endPerf);
          setSpeaking(false);
        });
      } else mark(msg.turn, 'client_audio_end');
      break;
    case 'interrupted':
      state.dropUpTo = Math.max(state.dropUpTo, msg.turn);
      if (state.out && state.out.turn === msg.turn) state.out.stop();
      state.motion.stopAll(nowS());
      state.performer.reset();
      setSpeaking(false);
      if (state.vrm) state.vrm.lookAtUser();
      if (msg.reason === 'barge_in') state.motion.backchannel('listen', nowS());
      markInterrupted(msg.turn);
      break;
    case 'trace':
      dashboard.renderTrace(msg.trace);
      dashboard.renderStats(msg.stats);
      break;
    case 'error': toast(msg.message); break;
    case 'reset_done': ui.chat.replaceChildren(); state.bubbles.clear(); break;
    default: break;
  }
}

function onAudio(buf) {
  const dv = new DataView(buf);
  const magic = String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3));
  if (magic !== 'WMA1') return;
  const turn = dv.getUint32(4, true), seg = dv.getUint32(8, true), sr = dv.getUint32(12, true);
  if (turn <= state.dropUpTo || !state.out) return;
  const pcm = new Int16Array(buf, HEADER_BYTES, (buf.byteLength - HEADER_BYTES) >> 1);
  const r = state.out.play(turn, sr, pcm, seg);
  if (r.first) {
    mark(turn, 'client_audio_start', r.startPerf);
    // 底座行为切到"聊天": 说话期间的手势来自 BEAT2 等动捕训练的模型. 声音开始前一点点切, 手势略早于语音.
    setTimeout(() => setSpeaking(true), Math.max(0, r.startPerf - performance.now() - 150));
  }
  const key = `${turn}:${seg}`;
  if (!state.segSeen.has(key)) {  // 一句话的第一段音频: 句首的视线表演从这里排
    state.segSeen.add(key);
    state.performer.beginClause(r.start, state.segText.get(key) || '');
  }
  state.performer.addAudio(r.start, r.frames);
}

/** 按顺序执行: 第一个要花时间的动作之前的都马上做, 它做完再接着处理后面的. 没有行动层时跳过这类动作. */
function runSequence(acts, op, turn = null) {
  const loco = state.vrm?.loco;
  const i = loco ? acts.findIndex((a) => LONG.has(a.do)) : -1;
  if (i < 0) { runActs(acts, op); return; }
  runActs(acts.slice(0, i), op);
  const vrm = state.vrm;
  setTimeout(() => {
    if (state.vrm !== vrm) return;
    vrm.kin?.begin(acts[i].do + (acts[i].to ? `:${acts[i].to}` : ''));
    loco.run(acts[i], (result) => {
      // 这个动作的执行评估 (成败、稳定裕度、关节越界、打滑、跳变抑制): 写进服务端日志, 没做成时下一轮告诉模型
      const ev = vrm.kin?.end(result);
      if (ev) { send({ type: 'motion_eval', turn, do: acts[i].do, to: acts[i].to, ...ev }); state.lastEval = ev; }
      runSequence(acts.slice(i + 1), 'act', turn);
    });
  }, (acts[i].at || 0) * 1000);
}

/** 执行一组动作: 头部动作给 Soullink, 看向/指向给行动层, 手臂/腿给 HAST-SEC (没有就用程序动作). */
function runActs(acts, op) {
  const vrm = state.vrm, loco = vrm?.loco;
  const later = (a, fn) => setTimeout(() => state.vrm === vrm && fn(), (a.at || 0) * 1000);
  // VRM 时点头/摇头交给 Soullink 的头部动作 (缓动更自然、受情绪影响).
  const head = vrm ? acts.filter((a) => a.do === 'nod' || a.do === 'shake') : [];
  for (const a of head) later(a, () => vrm.headAction(a.do, { amplitude: 0.6 * a.amp, speed: a.spd, repetitions: 2, duration: 0.7 }));
  const spatial = loco ? acts.filter((a) => a.do === 'look' || a.do === 'hand' || (a.do === 'point' && a.to) || EMOTE_ACTS.includes(a.do)) : [];
  for (const a of spatial) {
    if (a.do === 'look') later(a, () => loco.lookAt(a.to));
    else if (a.do === 'hand') later(a, () => loco.offerHand(a.side));
    else if (EMOTE_ACTS.includes(a.do)) later(a, () => loco.emote(a.do, a));
    else later(a, () => loco.pointAt(a.side, a.to));
  }
  let rest = acts.filter((a) => !head.includes(a) && !spatial.includes(a) && !SPATIAL.has(a.do));
  const body = vrm?.body;
  // 挥手/耸肩: 站着时用行动层手势库里的 CMU 动捕整段播放; 到时候正在走动、坐着或在做别的动作, 就按原来的方式做这一个
  const mocap = op !== 'stop' && loco?.gestDB ? rest.filter((a) => MOCAP_GESTURES.has(a.do) && loco.gestDB.groups[a.do]?.length) : [];
  for (const a of mocap) {
    later(a, () => {
      if (loco.gesture(a)) return;
      const now = { ...a, at: 0 };
      if (vrm.body) {
        vrm.body.perform(state.hast.toHastIntents([now]), op).catch((err) => toast(`身体动作执行失败: ${err.message}`));
        expressWithoutBody(now);
      } else state.motion.perform(now, nowS());
    });
  }
  rest = rest.filter((a) => !mocap.includes(a));
  if (body) {
    // HAST-SEC: 手臂/腿的动作由动捕技能执行; 它没有的耸肩/思考/鞠躬改用头部和表情表达.
    body.perform(state.hast.toHastIntents(rest), op).catch((err) => toast(`身体动作执行失败: ${err.message}`));
    for (const a of rest) expressWithoutBody(a);
    state.motion.applyIntent(op, [], nowS());
  } else {
    state.motion.applyIntent(op, rest, nowS());
  }
}

// 鞠躬不在里面: 下载的动捕只有芭蕾式屈膝礼 (后撤一大步), 原地播放时比原来的做法差, 见 tools/build_motion_db.py
const MOCAP_GESTURES = new Set(['wave', 'shrug']);

/** 说话状态告诉身体层: HAST-SEC 说话时接管全身做聊天手势; 没有 HAST-SEC 时行动层在说话间隙插动捕说话手势. */
function setSpeaking(on) {
  state.vrm?.body?.setSpeaking(on);
  if (state.vrm?.loco) state.vrm.loco.speaking = on;
}

/** HAST-SEC 没有对应动捕技能的动作, 用头部、视线和表情表达, 不让身体硬凑. */
function expressWithoutBody(a) {
  const vrm = state.vrm;
  const later = (fn) => setTimeout(() => state.vrm === vrm && fn(), (a.at || 0) * 1000);
  if (a.do === 'shrug') later(() => {
    vrm.pulseAU('au01InnerBrowRaiser', 0.5, 1.2);
    vrm.pulseAU('au02OuterBrowRaiser', 0.45, 1.2);
    state.performer.tilt(8, 1.2);
  });
  else if (a.do === 'think') later(() => { state.performer.thinking(); state.performer.tilt(-6, 1.6); });
  else if (a.do === 'bow') later(() => vrm.headAction('nod', { amplitude: 0.9, duration: 1.6 }));
}

// ---------------------------------------------------------------------------
// 对话气泡
function describeAct(a) {
  const parts = [SKILL_NAMES[a.do] || a.do];
  if (a.do === 'turn' && !a.to) parts.unshift(TURN_NAMES[a.side] || '');
  else if (a.side) parts.unshift(SIDE_NAMES[a.side]);
  if (a.to) parts.push(state.targetNames[a.to] || (a.to === 'home' ? '原处' : a.to));
  if (a.amp !== 1) parts.push(`幅度${a.amp}`);
  if (a.spd !== 1) parts.push(`速度${a.spd}`);
  if (a.at) parts.push(`+${a.at}s`);
  return parts.join(' ');
}

function addTurn(turn, text, source) {
  const user = document.createElement('div');
  user.className = 'bubble user';
  user.textContent = text;
  if (source === 'voice') user.dataset.source = '语音';
  const bot = document.createElement('div');
  bot.className = 'bubble bot';
  const chips = document.createElement('div');
  chips.className = 'chips';
  const reply = document.createElement('div');
  reply.className = 'reply';
  bot.append(chips, reply);
  ui.chat.append(user, bot);
  state.bubbles.set(turn, { bot, chips, reply });
  ui.chat.scrollTop = ui.chat.scrollHeight;
}

function addChips(turn, labels, kind) {
  const b = state.bubbles.get(turn);
  if (!b) return;
  for (const label of labels) {
    const chip = document.createElement('span');
    chip.className = `chip ${kind}`;
    chip.textContent = label;
    b.chips.append(chip);
  }
}

function appendReply(turn, text) {
  const b = state.bubbles.get(turn);
  if (!b) return;
  b.reply.textContent += text;
  ui.chat.scrollTop = ui.chat.scrollHeight;
}

function markInterrupted(turn) {
  const b = state.bubbles.get(turn);
  if (b) b.bot.classList.add('interrupted');
}

function renderStatus(status) {
  state.status = status;
  if (!status) {
    ui.status.innerHTML = '<span class="pill bad">未连接</span>';
    return;
  }
  const pill = (label, value, ok) => `<span class="pill ${ok ? 'ok' : 'bad'}" title="${value}">${label} ${value}</span>`;
  const llm = status.llm;
  ui.status.innerHTML = [
    pill('LLM', llm.engine === 'mock' ? 'mock' : `${llm.label}${llm.ok ? '' : ' 离线'}`, llm.ok),
    pill('ASR', status.asr.engine, status.asr.ok),
    pill('TTS', status.tts.engine, true),
    pill('RVC', status.rvc.engine, true),
  ].join('');
  const errors = Object.entries(status.errors || {});
  if (errors.length) toast(errors.map(([k, v]) => `${k.toUpperCase()} 加载失败: ${v}`).join('\n'), 8000);
  ui.mic.disabled = !status.asr.ok;
  ui.mic.title = status.asr.ok ? '' : 'ASR 未启用 (asr.engine=none), 只能文字输入';
}

let toastTimer = 0;
/** 动作库对比用的固定路线 (地址后加 #walkdemo, 或在控制台调 __wm.walkDemo()): 各种情绪的步态各走一个来回, 最后坐下再站起.
 *  两个窗口分别开新旧动作库的服务 (见 .claude/launch.json 的 motion-before / motion-after), 能并排对照. */
const DEMO_LEGS = [['neutral', '中性'], ['shy', '害羞'], ['calm', '平静'], ['happy', '开心'], ['sad', '难过'], ['angry', '生气']];
async function walkDemo(loco) {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const run = (act) => new Promise((r) => loco.run({ side: null, to: null, amp: 1, spd: 1, at: 0, ...act }, r));
  for (const [emo, name] of DEMO_LEGS) {
    await loco.setStyle(emo);
    toast(`步态: ${name} (${loco.style})`, 4000);
    for (const to of ['window', 'center']) {
      await new Promise((r) => loco.goTo(to, { after: r }));
      await wait(700);
    }
  }
  await loco.setStyle('neutral');
  toast('坐下, 再站起', 4000);
  await run({ do: 'sit', to: 'sofa' });
  await wait(2500);
  await run({ do: 'stand' });
  toast('演示结束', 3000);
}
state.walkDemo = () => state.vrm?.loco && walkDemo(state.vrm.loco);

/** 手势前后对比 (地址后加 #gesturedemo, 或在控制台调 __wm.gestureDemo()): 每个手势先用原来的做法、再用动捕各做一遍. */
const DEMO_GESTURES = [[{ do: 'wave', side: 'R' }, '右手挥手', 4500], [{ do: 'wave', side: 'B' }, '双手挥手', 7000], [{ do: 'shrug' }, '耸肩', 4000]];
async function gestureDemo(loco) {
  const wait = (ms) => new Promise((r) => setTimeout(r, ms));
  const db = loco.gestDB;
  for (const [act, name, ms] of DEMO_GESTURES) {
    for (const mocap of [false, true]) {
      loco.gestDB = mocap ? db : null;
      toast(`${name}: ${mocap ? '动捕' : '原来的做法'}`, ms - 500);
      runActs([{ side: null, to: null, amp: 1, spd: 1, at: 0, ...act }], 'act');
      await wait(ms);
    }
  }
  loco.gestDB = db;
  toast('演示结束', 3000);
}
state.gestureDemo = () => state.vrm?.loco && gestureDemo(state.vrm.loco);

function toast(text, ms = 5000) {
  ui.toast.textContent = text;
  ui.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { ui.toast.hidden = true; }, ms);
}

// ---------------------------------------------------------------------------
// 输入
ui.form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = ui.input.value.trim();
  if (!text) return;
  await ensureAudio();
  reportState();
  send({ type: 'text', text });
  ui.input.value = '';
});

for (const text of EXAMPLES) {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = 'example';
  b.textContent = text;
  b.addEventListener('click', async () => { await ensureAudio(); reportState(); send({ type: 'text', text }); });
  ui.examples.append(b);
}

ui.mic.addEventListener('click', async () => {
  await ensureAudio();
  if (state.mic && state.mic.active) {
    state.mic.stop();
    ui.mic.classList.remove('on');
    ui.mic.textContent = '打开麦克风';
    return;
  }
  state.mic = new MicIn(state.ctx, (pcm) => {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    // 半双工: 角色说话时上行静音帧, 端点检测照常推进, 也不会把自己的声音当成插话.
    const muted = ui.half.checked && state.out && state.out.playing;
    state.ws.send(muted ? new Int16Array(pcm.length).buffer : pcm.buffer);
  });
  try {
    await state.mic.start(state.asrRate);
    ui.mic.classList.add('on');
    ui.mic.textContent = '关闭麦克风';
  } catch (err) {
    toast(`麦克风不可用: ${err.message}`);
  }
});

ui.interrupt.addEventListener('click', () => send({ type: 'interrupt' }));
ui.reset.addEventListener('click', () => send({ type: 'reset' }));

// 角色: 启动时加载服务端的默认 VRM; 下拉框可切换 models/vrm 里的其他模型或本地文件. 失败就保留 2D 形象.
async function loadAvatar(source, label) {
  ui.avatarStatus.textContent = `正在加载 ${label}…`;
  try {
    const { createVRMAvatar } = await import('./avatar-vrm.js');
    const vrm = await createVRMAvatar(ui.stage, source, (p) => {
      ui.avatarStatus.textContent = `正在加载 ${label} ${Math.round(p * 100)}%`;
    });
    if (state.vrm) state.vrm.dispose();
    state.vrm = vrm;
    state.performer.attach(vrm);
    ui.canvas.hidden = true;
    const info = vrm.info;
    const face = info.fallback ? '表情形变不足, 用预设近似' : `${info.bindings}/20 个面部动作已映射`;
    ui.avatarStatus.textContent = `${info.name} · ${face}`;
    await attachBody(vrm, `${info.name} · ${face}`);
    await attachLocomotion(vrm);
    reportCapabilities();
  } catch (err) {
    console.error(err);
    ui.avatarStatus.textContent = '';
    toast(`VRM 加载失败, 继续用 2D 形象: ${err.message}`, 8000);
  }
}

/** 服务端装了 HAST-SEC 就让它驱动身体; 失败时保留程序动作. */
async function attachBody(vrm, label) {
  let motion = 'procedural';
  try { motion = (await (await fetch('/api/status')).json()).motion?.engine || motion; } catch { /* 用程序动作 */ }
  // 地址里 ?body=none 不载 HAST-SEC (无界面执行评估用: 评估走可商用的那条路, 无界面浏览器里它也载不完)
  if (new URLSearchParams(location.search).get('body') === 'none') motion = 'procedural';
  if (motion !== 'hast') { ui.avatarStatus.textContent = `${label} · 身体: 程序动作`; return; }
  ui.avatarStatus.textContent = `${label} · 正在加载身体运动层…`;
  try {
    state.hast ??= await import('./hast-body.js');
    const body = await state.hast.createHastBody(vrm.vrm);
    if (state.vrm !== vrm) return;  // 加载期间换了角色
    vrm.onBodyError = (err) => {
      ui.avatarStatus.textContent = `${label} · 身体: 程序动作 (HAST-SEC 出错已退回)`;
      toast(`HAST-SEC 运动层出错, 已退回程序动作: ${err.message}`, 8000);
    };
    vrm.attachBody(body);
    ui.avatarStatus.textContent = `${label} · 身体: HAST-SEC (非商业研究)`;
  } catch (err) {
    console.error(err);
    ui.avatarStatus.textContent = `${label} · 身体: 程序动作`;
    toast(`HAST-SEC 运动层加载失败, 用程序动作: ${err.message}`, 8000);
  }
}

/** 生成过运动匹配库就接上开放空间行动层 (房间、寻路、走动、视线); 失败时角色照常原地表演. */
async function attachLocomotion(vrm) {
  let styles = [];
  try { styles = (await (await fetch('/api/status')).json()).motion?.locomotion || []; } catch { /* 没有行动层 */ }
  if (!styles.includes('neutral')) return;
  const base = ui.avatarStatus.textContent;
  ui.avatarStatus.textContent = `${base} · 正在加载行动层…`;
  try {
    const [loco, room] = await Promise.all([import('./locomotion.js'), import('./scene.js')]);
    state.scene ??= await loco.fetchScene();
    const layer = await loco.createLocomotion(vrm, state.scene, { body: vrm.body });
    if (state.vrm !== vrm) return;
    const shadow = room.contactShadow();
    shadow.name = 'shadow';
    vrm.attachLocomotion(layer, room.buildRoom(state.scene), [shadow, layer.itemGroup]);
    vrm.gestures = state.motion;  // 没有 HAST-SEC 时手势来自程序动作, 叠到行动层的姿态上
    vrm.cameraRig.mode = ui.camera.value;
    vrm.onLocoError = (err) => toast(`行动层出错, 已停用: ${err.message}`, 8000);
    layer.onNotice = (text) => toast(text);
    if (location.hash === '#walkdemo') setTimeout(() => state.vrm === vrm && walkDemo(layer), 1500);
    else if (location.hash === '#gesturedemo') setTimeout(() => state.vrm === vrm && gestureDemo(layer), 1500);
    ui.avatarStatus.textContent = `${base} · 行动层: 运动匹配${layer.acts ? ' + 物体交互' : ''}`;
  } catch (err) {
    console.error(err);
    ui.avatarStatus.textContent = base;
    toast(`行动层加载失败, 角色原地表演: ${err.message}`, 8000);
  }
}

ui.camera.addEventListener('change', () => { if (state.vrm?.cameraRig) state.vrm.cameraRig.mode = ui.camera.value; });

async function initAvatars() {
  let list = { default: null, items: [] };
  try { list = await (await fetch('/api/avatars')).json(); } catch { /* 没有模型就用 2D */ }
  const options = list.items.map((i) => new Option(i.name.replace(/\.vrm$/, ''), `/avatars/${i.name}`));
  options.push(new Option('2D 简笔画', ''), new Option('本地 VRM 文件…', 'file'));
  ui.avatarSelect.replaceChildren(...options);
  // 地址里 ?avatar=xxx.vrm 指定形象 (执行评估、自动调优换模型用), 否则用默认的
  const want = new URLSearchParams(location.search).get('avatar');
  const name = want && list.items.some((i) => i.name === want) ? want : list.default;
  if (name) {
    ui.avatarSelect.value = `/avatars/${name}`;
    await loadAvatar(`/avatars/${name}`, name.replace(/\.vrm$/, ''));
  } else {
    ui.avatarSelect.value = '';
  }
}

ui.avatarSelect.addEventListener('change', async () => {
  const value = ui.avatarSelect.value;
  if (value === 'file') { ui.vrm.click(); return; }
  if (!value) {
    if (state.vrm) state.vrm.dispose();
    state.vrm = null;
    state.performer.attach(null);
    ui.canvas.hidden = false;
    ui.avatarStatus.textContent = '';
    reportCapabilities();  // 2D 形象: 只能做手势, 不能走路坐下
    return;
  }
  await loadAvatar(value, ui.avatarSelect.selectedOptions[0].textContent);
});

ui.vrm.addEventListener('change', async () => {
  const file = ui.vrm.files[0];
  if (file) await loadAvatar(file, file.name);
});

// ---------------------------------------------------------------------------
// 渲染循环: 动作和口型都在这里按同一帧时间更新.
let last = performance.now() / 1000;
const meter = new FrameMeter();
function frame() {
  const t = performance.now() / 1000;
  const dt = Math.min(0.05, t - last);
  last = t;
  const t0 = performance.now();
  tick(t, dt);
  meter.tick(performance.now() - t0);
  requestAnimationFrame(frame);
}
// 资源面板的浏览器端数据: 帧率、单帧耗时、绘制批次、弹簧关节、姿态平滑层每秒抑制的帧数
new ResourcePanel(document.getElementById('resources'), () => {
  const v = state.vrm, info = v?.renderer?.info?.render;
  return { fps: meter.fps, ms: meter.ms, calls: info?.calls, tris: info?.triangles,
    springs: v?.vrm?.springBoneManager?.joints?.size, filterRate: v?.filter?.stats?.rate };
});

function tick(t, dt) {
  const viseme = state.out ? state.out.viseme() : ZERO;
  const ch = state.motion.update(t, dt, viseme.level || 0);
  const arkit = state.out ? state.out.faceAt() : null;  // 服务端 A2F 的 ARKit 权重, 没有时为 null
  const face = { viseme, arkit, vad: state.motion.vad, thinking: state.motion.thinking, emo: state.emo };
  if (state.vrm) {
    state.performer.update(state.out ? state.out.heardTime() : 0, t, dt);
    state.vrm.apply(ch, face, dt, state.performer.neck);
  } else {
    state.avatar.render(ch, face, dt);
  }
  if (state.mic && state.mic.active) {
    ui.level.style.setProperty('--level', `${Math.max(0, Math.min(1, (state.mic.level + 60) / 50))}`);
  }
}

const PAIN_NAMES = { selfPen: '手臂插进身体', pushed: '手臂被推开', furniture: '碰进家具/墙', itemPen: '物品穿模', floorPen: '踩进地面',
  floating: '脚离地', slide: '脚打滑', joint: '关节越界', balance: '重心出支撑面', jump: '动作跳变', jerk: '动作冲击', grab: '隔空拿放' };
/** 这一帧痛觉最大的一项 (见 pain.js). */
function painWhy(now) {
  let best = null, v = 0;
  for (const [k, def] of Object.entries(PAIN_TERMS)) { const x = (now[k] || 0) * def.w; if (x > v) { v = x; best = k; } }
  return best && v > 0.2 ? ` (${PAIN_NAMES[best] || best})` : '';
}

/** 调试: 页面在后台 (requestAnimationFrame 不触发) 时手动推进 seconds 秒. */
state.step = (seconds, dt = 1 / 60) => {
  for (let k = 0; k < Math.round(seconds / dt); k++) tick(performance.now() / 1000, dt);
};

connect();
requestAnimationFrame(frame);
// ?scene=名字 换场景 (web/scenes/名字.json; 执行评估和体检 tools/motion_checkup.py 用), 默认客厅
const sceneParam = new URLSearchParams(location.search).get('scene') || '';
const sceneName = /^[\w-]+$/.test(sceneParam) ? sceneParam : 'room';
fetch(`/static/scenes/${sceneName}.json`).then((r) => r.json()).then((sc) => {
  state.scene = sc;
  state.targetNames = Object.fromEntries(sc.objects.map((o) => [o.id, o.name]));
}).catch(() => {}).finally(initAvatars);
window.__wm = state; // 方便在控制台调试
state.runActs = runActs;  // 调试和执行评估用: 不经过对话直接执行一组动作

// 运动学约束: 开关画出支撑多边形/质心/关节可行域, 读数每 0.2 秒刷新
const kinToggle = document.getElementById('kin-toggle'), kinPanel = document.getElementById('kin-panel');
// 面板分三块: 约束读数、运动仲裁表 (每 0.2 秒重画), 回放控制 (只建一次, 不跟着重画)
kinPanel.innerHTML = `<div class="kin-read"></div><div class="arb"></div>
  <div class="arb-ctl"><button type="button" data-a="pause" title="停住画面, 拖动看最近 20 秒每一帧是哪一层在驱动">回放</button>
  <button type="button" data-a="play" title="从这一帧往后按原速播放" hidden>▶</button>
  <input type="range" min="0" max="0" value="0" hidden aria-label="回放到第几帧">
  <button type="button" data-a="export" title="把最近 20 秒的记录 (每帧各层权重和姿态) 存成 JSON">导出</button>
  <button type="button" data-a="import" title="载入导出的记录回放 (同一个模型)">载入</button>
  <input type="file" accept="application/json,.json" hidden><span class="arb-at"></span></div>`;
const kinRead = kinPanel.querySelector('.kin-read'), arbBox = kinPanel.querySelector('.arb'), arbCtl = kinPanel.querySelector('.arb-ctl');
const arbSlider = arbCtl.querySelector('input[type=range]'), arbFile = arbCtl.querySelector('input[type=file]');
const arbPlayBtn = arbCtl.querySelector('[data-a=play]'), arbPauseBtn = arbCtl.querySelector('[data-a=pause]'), arbAt = arbCtl.querySelector('.arb-at');
let arbPlaying = null;  // 回放播放中: {from: 帧, t0: 开始时刻, ft0: 那一帧的时间}

/** 运动仲裁表: 每个部位一行, 一条按各层权重分段着色的横条, 后面写权重最大的那一层. */
function drawArbiter(w, src, mix, body) {
  const rows = PARTS.map((part, p) => {
    const top = topLayers(w, p, 6, 0.01);
    if (!top.length) return `<div class="arb-row"><span class="arb-part">${part.name}</span><span class="arb-bar"></span><span class="arb-top">-</span></div>`;
    const segs = top.map(([id, v]) => {
      const l = LAYERS[LAYER_INDEX[id]];
      return `<i style="width:${(v * 100).toFixed(1)}%;background:${l.color}" title="${l.name} ${(v * 100).toFixed(0)}%"></i>`;
    }).join('');
    const [id, v] = top[0];
    return `<div class="arb-row"><span class="arb-part">${part.name}</span><span class="arb-bar">${segs}</span>`
      + `<span class="arb-top">${LAYERS[LAYER_INDEX[id]].name} ${(v * 100).toFixed(0)}%</span></div>`;
  }).join('');
  const share = body ? ` · 行动层 ${(mix * 100).toFixed(0)}% / HAST-SEC ${((1 - mix) * 100).toFixed(0)}%` : '';
  arbBox.innerHTML = `<div class="arb-head">谁在驱动哪里 <small>动作来源 ${src || '-'}${share}</small></div>${rows}`;
}

function setReplay(i) {
  const v = state.vrm, a = v?.arbiter;
  if (!a?.count) return;
  v.replayAt = Math.max(0, Math.min(a.count - 1, i));
  arbSlider.max = String(a.count - 1);
  arbSlider.value = String(v.replayAt);
  const f = a.frame(v.replayAt), lastT = a.frame(a.count - 1).t;
  arbAt.textContent = ` ${(f.t - lastT).toFixed(2)} s`;
  drawArbiter(f.w, f.src, f.mix, !!v.body);
}

function leaveReplay() {
  if (state.vrm) state.vrm.replayAt = null;
  arbPlaying = null;
  arbPauseBtn.textContent = '回放';
  arbSlider.hidden = arbPlayBtn.hidden = true;
  arbAt.textContent = '';
}

arbCtl.addEventListener('click', async (ev) => {
  const act = ev.target.closest('button')?.dataset.a, v = state.vrm, a = v?.arbiter;
  if (!act || !a) return;
  if (act === 'pause') {
    if (v.replayAt !== null) { leaveReplay(); return; }
    if (!a.count) { toast('还没有记录'); return; }
    arbPauseBtn.textContent = '回到实时';
    arbSlider.hidden = arbPlayBtn.hidden = false;
    setReplay(a.count - 1);
  } else if (act === 'play' && v.replayAt !== null) {
    arbPlaying = arbPlaying ? null : { from: v.replayAt, t0: performance.now() / 1000, ft0: a.frame(v.replayAt).t };
  } else if (act === 'export') {
    const url = URL.createObjectURL(new Blob([JSON.stringify(a.dump())], { type: 'application/json' }));
    const link = Object.assign(document.createElement('a'), { href: url, download: `arbiter-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')}.json` });
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } else if (act === 'import') {
    arbFile.click();
  }
});
arbSlider.addEventListener('input', () => { arbPlaying = null; setReplay(Number(arbSlider.value)); });
arbFile.addEventListener('change', async () => {
  const file = arbFile.files?.[0], v = state.vrm;
  arbFile.value = '';
  if (!file || !v?.arbiter) return;
  try {
    v.arbiter.load(JSON.parse(await file.text()));
    arbPauseBtn.textContent = '回到实时';
    arbSlider.hidden = arbPlayBtn.hidden = false;
    setReplay(0);
  } catch (err) {
    toast(`载入失败: ${err.message}`);
  }
});
// 回放播放: 按记录里的时间原速往后走, 到最后一帧停住
setInterval(() => {
  const v = state.vrm, a = v?.arbiter;
  if (!arbPlaying || !a || v.replayAt === null) return;
  const want = arbPlaying.ft0 + (performance.now() / 1000 - arbPlaying.t0);
  let i = v.replayAt;
  while (i < a.count - 1 && a.frame(i + 1).t <= want) i++;
  if (i >= a.count - 1) arbPlaying = null;
  setReplay(i);
}, 50);

kinToggle.addEventListener('change', () => {
  state.vrm?.kin?.setVisible(kinToggle.checked);
  kinPanel.hidden = !kinToggle.checked;
  if (!kinToggle.checked) leaveReplay();
});
setInterval(() => {
  const k = state.vrm?.kin;
  if (!kinToggle.checked || !k) return;
  k.setVisible(true);
  // 仲裁表: 实时时按最近 0.5 秒平均 (逐帧跳得厉害); 回放时由 setReplay 画那一帧
  const a = state.vrm.arbiter;
  if (a && state.vrm.replayAt === null && a.last) {
    const now = a.last.t, sum = summarize(a.framesBetween(now - 0.5, now));
    const w = new Float32Array(LAYERS.length * PARTS.length);
    PARTS.forEach((part, p) => { for (const [id, v] of Object.entries(sum[part.id] || {})) w[LAYER_INDEX[id] * PARTS.length + p] = v; });
    drawArbiter(w, a.last.src, a.last.mix, !!state.vrm.body);
  }
  const n = k.now, c = k.counters, e = state.lastEval;
  const st = n.seated ? '坐着 (座面支撑)' : n.moving ? '行走中 (动态平衡, 不计)' : n.stability === null ? '-' : `${(n.stability * 100).toFixed(1)} cm`;
  const worst = (n.joints || []).reduce((m, j) => (j.margin < m.margin ? j : m), { margin: Infinity });
  kinRead.innerHTML = `稳定裕度 <b class="${n.staticCheck && n.stability < 0 ? 'bad' : ''}">${st}</b><br>`
    + `关节余量最小 <b class="${worst.margin < 0.25 ? 'bad' : ''}">${worst.joint ? `${worst.joint} ${worst.value}°` : '-'}</b>`
    + (n.bad?.length ? `<br><span class="bad">越界: ${n.bad.join(', ')}</span>` : '')
    + `<br>支撑脚打滑 <b>${c.slideMm.toFixed(0)} mm</b> · 穿地 <b>${c.floorPen}</b> 帧 · 悬空 <b>${c.floating}</b> 帧`
    + `<br>手臂推出身体 <b>${n.pushes}</b> 次 · 跳变抑制 <b>${n.suppressed}</b> 次`
    + `<br>按 ROM 修正 <b>${state.vrm?.posture?.stats.fixes ?? 0}</b> 次 · 配平偏移 <b>${((state.vrm?.loco?.balanceOff?.length() ?? 0) * 100).toFixed(1)} cm</b>`
    + `<br>痛觉 <b class="${k.painPeak > 3 ? 'bad' : ''}">${k.painPeak.toFixed(1)}</b>${painWhy(k.painNow)}`
    + (e ? `<br>上个动作 ${e.label}: ${e.ok ? '完成' : '失败'} ${e.seconds}s, 最小裕度 ${e.min_static_margin_cm ?? '-'} cm, 越界 ${e.joint_violation_frames} 帧, 打滑 ${e.foot_slide_mm} mm` : '');
}, 200);

// 摄像头: 人体姿态估计 (MediaPipe, 浏览器里本地跑). 第一次打开时才载入模型和运行库.
const camBtn = document.getElementById('camera');
camBtn.addEventListener('click', async () => {
  if (state.userPose?.running) { state.userPose.stop(); camBtn.textContent = '摄像头'; return; }
  camBtn.disabled = true;
  try {
    const { UserPose } = await import('./hpe.js');
    state.userPose ||= new UserPose({ preview: document.getElementById('hpe-preview'), label: document.getElementById('hpe-label') });
    await state.userPose.start();
    camBtn.textContent = '关闭摄像头';
  } catch (err) {
    console.error(err);
    toast(`摄像头打不开: ${err.message || err}`);
  } finally {
    camBtn.disabled = false;
  }
});
