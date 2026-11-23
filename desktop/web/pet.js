// 桌宠页面: 透明背景上的 VRM 角色, 连服务的 /ws 说话 (协议和网页一样, 见 talk.js); 头顶灵动对话栏、右侧侧菜单 (ui-shell.js).
// 给宿主发: 命中掩码 (宿主据此按像素点穿)、拖动请求、要键盘焦点、侧菜单的按钮 (麦克风/静音/隐藏).
// 宿主发来: 空闲帧率 (隐藏或暂停时 0)、光标在不在角色上、麦克风和静音的状态、打开输入框、本机接口的请求 (api-bridge.js).
// 说话和做动作时页面自己升到 30 帧, 空闲回到宿主给的帧率.
// 复用 web/ 的模块 (同源: 宿主只把 /pet/* 换成 desktop/web/ 的文件).
import * as THREE from 'three';
import { MotionSystem } from '/static/motion.js';
import { freeTextureSources, leanOptions, loadLeanAvatar } from '/pet/avatar-lean.js';
import { ApiBridge } from '/pet/api-bridge.js';
import { WatchBar } from '/pet/watch-bar.js';
import { QQChannel } from '/pet/qq.js';
import { QQTasks, ASK_QQ_S } from '/pet/qq-task.js';
import { GEN_KEY, roomGenPlan, roomGenKeys } from '/pet/room-gen.js';
import { DEFAULT_ROOM_SCENE, roomStorageKey } from '/pet/room-scene-state.js';
import { attachDeskBody } from '/pet/desk-body.js';
import { EMOTES } from '/static/locomotion.js';
import { Talk } from '/pet/talk.js';
import { Shell } from '/pet/ui-shell.js';
import { loadLive2DAvatar } from '/pet/live2d-avatar.js';
import { PET_SCALE_KEY, PET_SCALE_WHEEL, clampPetScale, petScaleFit, petScaleMax, readPetScale, wheelPetScale, writePetScale } from '/pet/ui-preferences.js';
import { ActivityReactor } from '/pet/activity.js';
import { bodyColliders, realtimeSprings } from '/pet/springs.js';
import { fastMatrices } from '/pet/fast-matrix.js';
import { enterRoom } from '/pet/room-life.js';
import { LOAD_MAX, needsPanel } from '/pet/room-needs.js';
import { herDate, startTz } from '/pet/tz.js';
import { IdleDirector } from '/pet/idle-director.js';

startTz();  // 她按哪个时区过日子 (面板里设的, 没设跟着电脑): 小屋的夜晚、半夜劝你睡觉都按它
const host = window.chrome?.webview;
let postTap = null;  // 自检 (scaletest): 设了就先让它看一眼发给宿主的消息, 返回真就不转发 (不能真的让宿主开始拖窗口)
const post = (msg) => {
  if (postTap?.(msg)) return;
  if (typeof msg === 'string' && msg.startsWith('ready ')) document.getElementById('splash')?.remove();  // 角色加载好了: 启动画面直接收掉
  host?.postMessage(msg);
};
setTimeout(() => document.getElementById('splash')?.remove(), 20000);  // 一直没就绪 (出错) 也别一直挂着
const log = (...parts) => { console.log(...parts); post(`log ${parts.join(' ')}`); };
window.addEventListener('error', (e) => post(`log 错误 ${e.message} @ ${e.filename}:${e.lineno}`));
window.addEventListener('unhandledrejection', (e) => post(`log 错误 ${e.reason?.stack || e.reason}`));

const CELL = 6;            // 掩码一格 = 6 CSS 像素
const ALPHA_MIN = 24;      // 透明度高于这个 (0..255) 算有角色
const MASK_EVERY = 0.25;   // 秒; 窗口跟着角色一起动, 拖动时掩码不变, 不用更勤
const MASK_RESEND = 1.0;   // 掩码没变时也隔这么久重发一次, 宿主据此判断掩码是否还新鲜
const TALK_FPS = 30;       // 说话、做动作时的帧率
const BUBBLE_ROOM = 0.16;  // 窗口顶部留给对话栏的高度比例
const PARAMS = new URLSearchParams(location.search);
// 省内存的做法 (见 avatar-lean.js 和 docs/roadmap/desktop-pet.md 的 M1 实测). 页面地址可以临时覆盖 (自检对比用):
//   tex 贴图边长上限 (0 不限), morph keep|compact|nonormal|compact-nonormal, free 上传后释放原图,
//   aa MSAA 开关, pr 像素比上限, render 0 = 只加载不渲染
// 默认值按 M1 实测定: 变形纹理压缩 (省约 515 MB) + 去变形法线 + 释放原图 + 贴图 512; 关 MSAA、降像素比画面明显变差, 不用.
const OPTS = leanOptions(PARAMS, { tex: 512, morph: 'compact-nonormal', free: 1, aa: 1, pr: 1.5, render: 1 });
// 待机: staystill (行动层 + StayStill 动捕, 见 desk-body.js) 或 procedural (程序待机, 对比用)
const IDLE = PARAMS.get('idle') || 'staystill';
// 门面: 3D (VRM, 默认) 或 2D (face=live2d: Live2D 模型, 见 live2d-avatar.js). 宿主按当前人设预设加这个参数, 换门面时重新打开页面
const LIVE2D = PARAMS.get('face') === 'live2d';
const LIVE2D_MODEL = '/pet/live2d-models/Eustia/';
// 闲时状态机和对话里的细微反应 (2D 才有, idle-director.js): idlefx=0 关掉; idlefx_scale=N 把时间轴压缩 N 倍 (自检和看效果用)
const IDLEFX = LIVE2D && PARAMS.get('idlefx') !== '0';
const dnow = () => performance.now() / 1000;
const director = IDLEFX
  ? new IdleDirector({ hour: () => { const d = herDate(); return d.getHours() + d.getMinutes() / 60; }, cfg: { scale: Number(PARAMS.get('idlefx_scale')) || 1 } })
  : null;
const liveTools = new Set();  // 正在调的工具 (所有, 不止 MCP): 她在做事, 闲时状态机不往下走
// 小屋模式 (room=1, 宿主按设置加): 桌宠窗口下半部分是一小块 3D 空间, 她在当前选定的小屋里生活 (见 room-life.js).
// 只有 3D 有; 2D 门面照常站在桌面上
const ROOM = PARAMS.get('room') === '1' && !LIVE2D;
// 小屋卡片在窗口里的位置 (CSS 像素, 高度是窗口的比例). 右边留一条放侧菜单 (在 3D 空间外面, 用户要的; 原来叠在卡片右边, 按钮多了下面的被切掉)
const ROOM_CARD = { left: 8, right: 52, bottom: 6, height: 0.5 };
// 工具确认框 (自检用): toolanswers=once,deny,timeout,always 依次自动回答; asktimeout 改等待秒数 (默认 30)
const TOOL_ANSWERS = (PARAMS.get('toolanswers') || '').split(',').filter(Boolean);
const ASK_S = Number(PARAMS.get('asktimeout')) || 30;
const TOOL_STOP_MS = Number(PARAMS.get('toolstop')) || 0;
// QQ 任务单 (自检用): qqtask=owner|电脑:Screenshot | Click {...} — 工具表到位后自己发一张任务单 (by|提示词), 不用真连 QQ
const SELF_TASK = (PARAMS.get('qqtask') || '').split('|');
// 眼睛跟着鼠标 (宿主发来光标位置, 托盘里可以关): 光标动过之后 FOLLOW_S 秒内看着它, 之后回来看着用户.
// 角度按光标离头多远算, 假定人离屏幕 GAZE_DIST 个 CSS 像素; 光标在头附近 (GAZE_NEAR 以内) 移动时升到 30 帧, 远处的照空闲帧率跟
const FOLLOW_S = 3;
const GAZE_DIST = 600;
const GAZE_NEAR = 500;
// 眼球范围保留导入 VRM 的设置；头部跟随仍使用下面的分摊参数。
const HEAD_SHARE = { from: 6, k: 0.5, yaw: 24, pitch: 12 };  // 视线超过 from 度的部分, 头转 k 倍, 最多 yaw / pitch 度

const stage = document.getElementById('stage');
const motion = new MotionSystem();
// 小屋模式: 角色画在下半部分的卡片里 (#stage 变成卡片), 卡片顶上一条把手 (按住拖动挪桌宠), 左下角写她在做什么
const roomStatus = document.createElement('div');
if (ROOM) {
  document.body.classList.add('room-mode');
  Object.assign(stage.style, { left: `${ROOM_CARD.left}px`, right: `${ROOM_CARD.right}px`, bottom: `${ROOM_CARD.bottom}px`,
    top: 'auto', height: `${ROOM_CARD.height * 100}%` });
  const bar = document.createElement('div');
  bar.className = 'room-bar';
  bar.textContent = '生活小屋';
  bar.title = '按住拖动, 把小屋挪到别处';
  bar.addEventListener('pointerdown', (e) => { if (e.button === 0) post('drag'); });
  roomStatus.className = 'room-status';
  roomStatus.textContent = '小屋收拾中…';
  document.body.append(bar, roomStatus);
}
const state = { avatar: null, report: null, hostFps: 10, fps: 10, timer: 0, raf: 0, due: 0, last: performance.now() / 1000,
  maskAt: -1, maskSentAt: -1, maskMsg: '', mask: null, ready: false, rendered: 0, freed: false,
  frames: 0, workMs: 0, statsAt: performance.now() / 1000, talkGo: null, ch: null, footSent: -1,
  body: null, rail: null, railSent: -1, pokes: 0, pokeTimer: 0,
  serverGone: false, outbox: [] };  // 服务端断过 (重启、崩了, 连回来算"醒来"); 桌面上断开时你说的话 (连上了再发)
const ground = new THREE.Vector3();
const handPos = new THREE.Vector3();
const headTmp = new THREE.Vector3();
const railTmp = new THREE.Vector3();
// 光标 (CSS 像素, 相对窗口)、最后一次动的时间、算出来的角度; test: 自检时不理宿主发来的真光标
const gaze = { x: 0, y: 0, at: -1e9, yaw: 0, pitch: 0, sentYaw: null, sentPitch: null, near: false, active: false, received: 0, test: false, head: false };
// 桌宠自带的工具 (宿主的 builtins.h) 在对话栏里怎么说
const BUILTIN_NOTES = { remind: '在记提醒', cancel_reminder: '在看提醒', room_do: '起身去做事', open_url: '想帮你打开网址' };
const roomTools = new Set();  // 正在调的工具 (小屋里她坐在电脑前)
// 自检时不出声: mock 的 TTS 只发元音占位音 ("啊哦噫诶"), 不能放到用户的扬声器上 (口型按音频数据算, 静音不影响检查).
// 所有自检都算: 小屋自检里她醒来会把便利贴当消息发出去, 原来漏了它, 用户听到的"机器杂音"就是测试桌宠放的占位音 (2026-10-25)
const SELFTEST_MUTE = ['talk', 'skills', 'roomtest', 'ui', 'gaze', 'lean', 'activity', 'toolanswers', 'exprtest', 'idletest', 'scaletest'].some((k) => PARAMS.has(k)) || PARAMS.get('mute') === '1';
// 你让她去睡: 说"去睡吧""睡觉吧""快睡""晚安"这类 (只看你说的, 不看她说的)
const SLEEP_WORDS = /去睡|睡觉吧|睡吧|快睡|早点睡|晚安/;
const WATCH = new Set(['code', 'office', 'browse', 'video', 'game']);  // 你在做这些时她会去电脑前看你
const maskCanvas = document.createElement('canvas');
const maskCtx = maskCanvas.getContext('2d');

// 界面壳: 按钮交给宿主 (和托盘是同一套开关), 打字直接走对话通道. 界面一变就马上重算掩码, 刚出来的按钮才点得到.
const shell = new Shell(document.getElementById('shell'), {
  mic: () => post('mic-toggle'),
  mute: () => post('mute-toggle'),
  pin: () => post('pin-toggle'),
  room: () => post('room'),
  drag: () => post('drag'),
  settings: () => post('panel'),
  workbench: () => post('workbench'),
  persona: (id) => post(`persona ${id}`),
  stopTool: (callId) => post(`tool-stop ${callId}`),
  controlStop: () => post('control-stop'),
  watchStop: () => talk.send({ type: 'watch_ctl', action: 'stop' }),  // 陪看提示条上的"不看了" (和说出来一样; 回话在 watch_ctl_done)
  hide: () => post('hide'),
  send: (text) => userSays(text),
  focus: () => { post('focus'); refreshMask(); },
});

// 打字: 视线落向输入框, 停顿时歪头 (idle-director.js)
shell.el.input.addEventListener('input', () => director?.typing(dnow(), shell.el.input.value.length));

// 陪看 / 屏幕陪看的提示条: 写什么、结束时停不停一下, 见 watch-bar.js
const watchBar = new WatchBar(shell, { refresh: refreshMask });

// ---------------------------------------------------------------------------
// 桌宠的大小 (2026-11-16 pet-scale, 用户: "我们的模型无法进行缩放, 调整大小").
// k 是相对默认窗口高 800 的比例 (1 = 以前的样子, 下限 35%), 存 localStorage (ui-preferences.js; 设置页的滑条和 Ctrl+滚轮都改它).
// 画给门面的是"占满窗口的比例" r = min(1, k * 800 / 窗口高): 现在的窗口 (宿主定死 420x800) 撑得下的最大就是 100%, 只能缩小;
// 宿主以后能放大窗口了, 同一个 k 自动可以大于 1. 只在 2D 桌面模式生效 (3D 门面、小屋不动).
// 页面地址 petscale=0.5 优先于存的值、不存 (自检用).
const SCALE_URL = PARAMS.has('petscale') ? clampPetScale(PARAMS.get('petscale'), 1.5) : null;
const petScale = { k: SCALE_URL ?? readPetScale(), saveTimer: 0, noticeAt: -1e9 };
const petScaleOn = () => LIVE2D && !ROOM;
const petFit = () => (petScaleOn() ? petScaleFit(petScale.k, innerHeight) : 1);

/** 改大小 (马上生效: 门面换摆放, 对话栏和侧菜单跟着挪, 掩码下一帧重算; save: 存起来, 连续滚动时合并成一次写). */
function applyPetScale(k, { save = false } = {}) {
  petScale.k = clampPetScale(k, 1.5);
  if (save && SCALE_URL === null) {
    clearTimeout(petScale.saveTimer);
    petScale.saveTimer = setTimeout(() => { petScale.saveTimer = 0; writePetScale(petScale.k); }, 200);
  }
  const r = petFit();
  shell.setScale(r);
  state.avatar?.setScale?.(r);
  refreshMask();
}

// 鼠标放在她身上按住 Ctrl 滚滑轮 (触控板双指捏合在 Chromium 里就是 Ctrl+滚轮). ui-preferences.js 在 window 上也吞 Ctrl+滚轮
// (防整页缩放), 两处都 preventDefault, 谁先收到不影响结果.
stage.addEventListener('wheel', (e) => {
  if (!e.ctrlKey || !petScaleOn() || !state.avatar?.setScale) return;
  e.preventDefault();
  const max = petScaleMax(innerHeight), next = wheelPetScale(petScale.k, e.deltaY, e.deltaMode, max);
  if (next !== petScale.k) applyPetScale(next, { save: true });
  else if (e.deltaY < 0 && petScale.k >= max - 1e-6 && performance.now() - petScale.noticeAt > 10000) {
    petScale.noticeAt = performance.now();
    shell.notice('已经是这个窗口能放下的最大了。要更大需要新版宿主, 或者启动时加 --size');
  }
}, { passive: false });

// 设置页 (另一个窗口) 改了: storage 事件马上来; 再在每 2 秒一次的统计里读一下兜底 (不新增定时器, 只读一个键、比一个数)
function syncPetScale() {
  if (SCALE_URL !== null || petScale.saveTimer || !petScaleOn()) return;
  const k = readPetScale();
  if (Math.abs(k - petScale.k) > 1e-4) applyPetScale(k);
}
window.addEventListener('storage', (e) => { if (e.key === PET_SCALE_KEY || e.key === null) syncPetScale(); });

// 桌面上这条对话的回调: 对话栏照常显示; 本机接口发的那几轮, 回复同时报给宿主
const talk = new Talk(motion, {
  onTurn: (turn, text, source) => {
    director?.turnStart(dnow());
    const task = tasks.turnStart(turn, text);  // QQ 任务单变成的这一轮: 对话栏里显示任务本身, 不算"你说的话"
    shell.turn(task ? tasks.shown() : text, source === 'watch' ? '陪看' : ''); refreshMask(); state.room?.attend(); api.onTurn('pet', turn, text, source);
    // 全屏时宿主临时露出对话栏 (见 peek). 2026-11-08 起单独一行: 11-07 加这句时插在上一行中间, 后半行 (attend、api.onTurn) 被挤进注释、一直没执行
    if (source === 'watch' && !state.peekWanted) { state.peekWanted = true; post('peek 1'); }
    if (task) return;
    state.room?.heard(text?.length || 0);  // 困倦按说了多少字涨
    if (state.room && SLEEP_WORDS.test(text || '')) state.room.sleepNow();  // "去睡吧""晚安": 上床, 睡着后整理今天的对话
  },
  onListening: () => { director?.interact(dnow(), 'listening'); shell.listening(); refreshMask(); },
  onThinking: () => director?.thinkingOn(dnow()),
  onEmotion: () => director?.emotionEvent(dnow()),
  onAudioStart: () => director?.audioStart(dnow()),
  onListeningEnd: () => { shell.listeningEnd(); refreshMask(); },
  onPartial: (text) => shell.partial(text),
  onReply: (turn, text) => { tasks.reply(turn, text); shell.reply(text); api.onReply('pet', turn, text); state.room?.heard(text?.length || 0); },
  onServerDone: (turn) => api.onServerDone('pet', turn),
  onDone: (turn) => { shell.done(); api.onDone('pet', turn); post('turn-done'); tasks.turnEnd(turn, false); },
  onInterrupted: (turn) => { director?.interrupted(dnow()); api.onInterrupted('pet', turn); post('turn-done'); tasks.turnEnd(turn, true); },
  onError: (turn, message) => api.onError('pet', turn, message),
  // 云端模型要调工具: 交给宿主 (按权限自动调、在对话栏里问、或者拒绝, 见 desktop/host/src/tools.h), 结果由宿主发回来
  onToolCall: (msg) => {
    post(`tool ${JSON.stringify({ call_id: msg.call_id, turn: msg.turn, name: msg.name, arguments: msg.arguments })}`);
    tasks.tool(msg);  // QQ 任务单里的工具调用: 给服务端报一次进度
    liveTools.add(msg.call_id);
    director?.setPresent('tool', true, dnow());
    const m = /^mcp__(.+?)__(.+)$/.exec(msg.name);
    if (m) {  // 小屋里: 她坐到电脑前帮你操作 (MCP 的工具才算; load_skill 和自带的提醒、小屋动作不用坐过去)
      roomTools.add(msg.call_id);
      state.room?.working(roomTools.size, m[2]);
    }
    if (msg.name === 'load_skill') shell.toolNote('在看 skill…');
    else shell.toolNote(`${m ? `在用工具 ${m[1]} · ${m[2]}` : BUILTIN_NOTES[msg.name] || `在用工具 ${msg.name}`}…`, msg.call_id);
    // 自检 (toolstop=毫秒): 参数里带 tag: "stop" 的调用, 隔这么久替用户点"停"
    if (TOOL_STOP_MS && msg.arguments?.tag === 'stop') setTimeout(() => shell.el.stop.click(), TOOL_STOP_MS);
    refreshMask();
  },
  onToolCancel: (msg) => {
    post(`tool-cancel ${msg.call_id}`); roomTools.delete(msg.call_id); state.room?.working(roomTools.size);
    liveTools.delete(msg.call_id); director?.setPresent('tool', liveTools.size > 0, dnow());
  },
  onProfileDone: (msg) => {
    const llm = msg.llm || {};  // 本机模型 (2026-11-02): llm.local 为真, 写"本机 · 模型名"
    if (msg.ok) log('人设预设生效', llm.local ? `本机 · ${llm.model || '服务加载的那个'}` : llm.engine === 'cloud' ? `${llm.provider}:${llm.model}` : '内置小模型', `工具 ${msg.tools ?? 0} 个`);
    else shell.notice(`人设预设没生效: ${msg.error}`);
    if (msg.ok && msg.warnings?.length) shell.notice(msg.warnings.join('；'));  // 服务端给的提醒: 本机服务连不上、上下文太短这类 (已经是人话)
  },
  onStatus: (status) => {
    log('服务', status ? `LLM ${status.llm.engine} / TTS ${status.tts.engine} / ASR ${status.asr.engine}` : '断开');
    if (!status) state.room?.cancelAct?.('连接断开，停止待执行请求');
    if (!status) { watchBar.hide(); director?.setPresent('watch', false, dnow()); }  // 陪看跟着这条连接: 断了服务端就不看了, 页面收不到结束消息
    if (!status && !state.serverGone) { state.serverGone = true; state.room?.serverDown(); }  // 没预告就断了 (崩了、网断了)
  },
  // 陪看 (2026-11-07, wm/watch_along.py) 和屏幕陪看 (2026-11-08, wm/screen_along.py): 开始 / 换了片、换了窗口 / 结束时 watch_state,
  // 对话栏上面一条提示 (写什么见 watch-bar.js); "不看了"按钮的回话 (只显示不念) 在 watch_ctl_done
  onWatch: (msg) => {
    if (msg.type === 'watch_state') { watchBar.update(msg); director?.setPresent('watch', !!msg.active, dnow()); }  // 陪看 / 屏幕陪看在进行: 她在场, 不走神不犯困
    else if (msg.type === 'watch_ctl_done') {
      if (msg.text) shell.notice(msg.text);
      if (!msg.active) { watchBar.hide(); director?.setPresent('watch', false, dnow()); }
    }
    refreshMask();
  },
  // 服务端要重启 (看守进程先告诉它的; self-evolution.md 4.3): 小屋里她去睡一会儿, 这期间你的话变便利贴
  onServerRestart: (msg) => {
    state.serverGone = true;
    log('服务端要重启', msg.reason, msg.eta_s ?? '');
    if (state.room) state.room.nap();
    else shell.notice(`她打了个盹 · 服务端在${msg.stop ? '停' : '重启'}${msg.eta_s ? `, 大概 ${msg.eta_s} 秒` : ''}, 你说的话等她醒了再回`);
  },
  // 连上了 (hello): 这个页面里断过的话算"醒来", 说说醒来知道的事 (学会了什么、退回去了), 看便利贴 / 发攒着的话
  onHello: (msg) => {
    syncRoomGen(msg.room_gen);  // 你在设置里删了她的经历、重置了小窝学习: 本机存的跟着清 (room-gen.js)
    if (!state.serverGone) return;
    state.serverGone = false;
    const news = bootNews(msg.boot);
    log('服务端回来了', msg.boot?.id || '', msg.boot?.mode || '', news);
    if (state.room) state.room.serverBack(news);
    else {
      if (news) shell.notice(news);
      (async () => { while (state.outbox.length && await sayInTurn(state.outbox[0])) state.outbox.shift(); })();
    }
  },
  onOpen: () => {
    qq.onOpen();
    watchBar.hide();  // 新连接: 服务端那边没在陪看
    director?.setPresent('watch', false, dnow());
    sendRoomCapabilities();
    // 小屋: 每条新连接先说一声"日记本合着", 服务端从此按日记本的开合门控这条连接 (合着时只取名字、喜好和关系概况,
    // 对话摘要、资料、笔记要翻开日记本才读得到; 不发的话不门控, 和网页一样). 翻开时日记脚本再发 open: true
    if (ROOM) talk.send({ type: 'diary', open: false });
  },
  // QQ 渠道的状态、草稿、提示 (qq.js) 和电脑任务单 (qq-task.js); 她在 QQ 那边做什么 (channel_doing): 小屋里坐到电脑前 / 阅读椅上
  onChannel: (msg) => (msg.type === 'channel_doing' ? state.room?.doing(msg)
    : /^channel_(task|ask)/.test(msg.type || '') ? tasks.handle(msg) : qq.handle(msg)),
  onRoomAction: async (msg) => {
    const room = state.room;
    const result = room && room.sceneId === msg.scene_id
      ? await room.requestAct(msg.act, { requestId: msg.request_id })
      : { ok: false, started: false, completed: false, reason: '当前小屋未加载或场景已切换' };
    talk.send({ type: 'room_action_result', ...result, request_id: msg.request_id, scene_id: msg.scene_id, act: msg.act });
    sendRoomCapabilities();
  },
  onRoomActionCancel: (msg) => {
    const room = state.room;
    if (room?.sceneId === msg.scene_id && room.life.requests.active?.id === msg.request_id) room.cancelAct(msg.reason);
  },
  onRoomGen: (msg) => syncRoomGen(msg.gen),  // 服务端刚删 / 重置完推过来的 (不用等下次连上)
  log,
});
function sendRoomCapabilities() {
  const room = state.room;
  if (room) talk.send({ type: 'room_capabilities', scene_id: room.sceneId, activities: room.capabilities() });
}
const api = new ApiBridge(talk, { post, wake, log });
const qq = new QQChannel({ talk, shell, post, refresh: () => refreshMask(), focus: () => post('focus'), log });
const tasks = new QQTasks({ talk, post, shell, log });

/**
 * 你在对话栏里打的字: 小屋里她睡着时 (或者服务端在重启、断开了) 变成床头柜上的便利贴, 醒来、连上再回;
 * 桌面上断开时先攒着, 连上了再发.
 */
function userSays(text) {
  if (state.room?.note(text, !talk.connected)) return;
  if (talk.connected) { sendRoomCapabilities(); talk.say(text); return; }
  state.outbox.push(text);
  shell.notice(`和服务端断开了, 这句先记下, 连上了再发 (${state.outbox.length} 句)`);
}

/** 服务端这次是怎么起的 (hello 里的 boot, 看守进程告诉它的): 醒来 / 连回来时她说的一句. 没什么要说的给 ''. */
function bootNews(boot) {
  if (boot?.mode === 'reverted') return boot.note || '刚才那版没跑起来, 退回去了';
  if (boot?.mode === 'crashed') return '刚才服务端出了点问题, 现在好了';
  if (boot?.learned?.length) return `睡了一觉, 学会了「${boot.learned.join('」「')}」`;
  return '';
}

/**
 * 删除代次 (room-gen.js): 服务端的比本机清到的大, 就按那几次的范围清掉本机存的 (发现簿、练习参数), 记下新代次.
 * 小屋开着时清完重新载入页面: 内存里已经读进来的旧数据不会再写回去. 回包没拿到就这次不动, 下次连上再看.
 */
// Capture before enterRoom loads Known/Practice. Another window's later GEN_KEY
// write proves shared disk was cleared, not that this window discarded its memory.
const roomGenNumber = (value) => { const n = Number(value); return Number.isSafeInteger(n) && n > 0 ? n : 0; };
let roomGenApplied = (() => { try { return roomGenNumber(localStorage.getItem(GEN_KEY)); } catch { return 0; } })();
let roomGenBusy = false, roomGenPending = 0;
async function syncRoomGen(gen) {
  if (!Number.isSafeInteger(gen) || gen <= roomGenApplied) return;
  roomGenPending = Math.max(roomGenPending, gen);
  if (roomGenBusy) return;  // Keep the newer request; the active loop must not lose it.
  roomGenBusy = true;
  try {
    while (roomGenPending > roomGenApplied) {
      const mine = roomGenApplied, target = roomGenPending;
      try { localStorage.getItem(GEN_KEY); } catch { return; }  // No successful application without working storage.
      let info = null;
      try { const r = await fetch('/api/experience', { cache: 'no-store' }); if (r.ok) info = await r.json(); } catch { /* 下次再看 */ }
      if (roomGenPending > target) continue;  // Fetch the latest reset scope, even if the earlier request failed.
      const plan = roomGenPlan(target, mine, info);
      if (!plan || plan.retry) { if (plan) log('删除代次', target, '范围没拿到, 下次连上再清'); return; }
      let clearKeys;
      try {
        clearKeys = roomGenKeys(plan.keys, Array.from({ length: localStorage.length }, (_, i) => localStorage.key(i)));
        for (const key of clearKeys) localStorage.removeItem(key);
        // Never lower another window's newer disk marker. It still does not
        // advance this window's applied generation beyond the scope just cleared.
        const shared = roomGenNumber(localStorage.getItem(GEN_KEY));
        localStorage.setItem(GEN_KEY, String(Math.max(shared, plan.gen)));
      } catch { return; }  // Partial deletion is retryable; don't acknowledge it.
      if (ROOM && plan.keys.length) {
        try { location.reload(); } catch { log('删除代次', target, '存储已清，页面未能重载，保留重试'); return; }
      }
      roomGenApplied = plan.gen;
      log('删除代次', `${mine} -> ${plan.gen}`, `清了 ${clearKeys.join(', ') || '(本机没有要清的)'}`, plan.guessed ? '(中间几次的范围不知道, 按全部清)' : '');
    }
  } finally {
    roomGenBusy = false;
  }
}
window.addEventListener('storage', (event) => {
  if (event.key !== GEN_KEY) return;
  try { if (event.storageArea && event.storageArea !== localStorage) return; } catch { return; }
  void syncRoomGen(roomGenNumber(event.newValue));
});

/**
 * 表情动作 (比心、捂嘴笑、托腮、拍手、比耶、捧脸; web/locomotion.js 的 EMOTES, 服务端 wm/intent.py 里是 OPT_IN):
 * 挂了行动层 (桌面的待机身体或小屋) 才声明, 服务端才会选. 收到就按 at 延迟交给 loco.emote (走动、坐下站起、拿放时它自己先等着).
 * 没接之前说"比个心"会照实答做不到.
 */
function addEmotes(v) {
  if (!v.loco?.emote) return;
  for (const [kind, def] of Object.entries(EMOTES)) {
    talk.addBodySkill(kind, (act) => {
      setTimeout(() => state.avatar === v && v.loco?.emote(kind, act), (act.at || 0) * 1000);
      return (act.at || 0) + def.dur / Math.min(2, Math.max(0.5, act.spd || 1));
    });
  }
}

/** 一条一条地说 (便利贴、断开时攒下的): 等上一条说完再说下一条. 没连上返回 false (留着, 连上了再说). */
async function sayInTurn(text) {
  const wait = (ms) => new Promise((ok) => setTimeout(ok, ms));
  if (!talk.connected) return false;
  while (talk.busy || shell.active) await wait(500);
  if (!talk.connected) return false;
  await talk.say(text);
  await wait(1500);
  while (talk.busy) await wait(500);
  return true;
}

// 看你在做什么 (宿主托盘里打开才有): 她在对话栏里说一句、换个表情、点点头; 摘要放进 state 告诉服务端
const activity = new ActivityReactor({
  say: (text, emotion) => {
    shell.notice(text);
    const v = state.avatar;
    if (v && emotion) {
      v.setEmotion(emotion);
      clearTimeout(state.activityEmo);
      state.activityEmo = setTimeout(() => v.setEmotion('neutral'), 4000);
    }
    v?.headAction?.('nod', { amplitude: 0.3, repetitions: 1 });
    director?.interact(dnow(), 'activity');
    talk._busyFor?.(2);
    wake();
    refreshMask();
  },
  busy: () => talk.busy || shell.active,
  onChange: (summary) => talk.setActivity(summary),
});
setInterval(() => activity.tick(), 1000);

/** 界面变了 (对话栏出现/展开、侧菜单滑出): 下一帧重算掩码, 并马上排一帧. */
function refreshMask() {
  state.maskAt = -1;
  wake();
}

// ---------------------------------------------------------------------------
/** 全身取景: 脚底到头顶都在画面里, 顶部留出气泡的位置, 横向放得下下垂的手臂和待机摆动. */
function frameCamera(v) {
  if (!v.headPos) return;
  const tan = Math.tan((v.camera.fov * Math.PI) / 360);
  const bottom = -0.03, head = v.headPos.y + 0.18;
  const top = bottom + (head - bottom) / (1 - BUBBLE_ROOM);
  const span = top - bottom;
  const dist = Math.max(span / (2 * tan), 0.75 / (2 * tan * v.camera.aspect));
  const cy = (top + bottom) / 2;
  v.camera.position.set(0, cy, dist);
  v.camera.lookAt(0, cy, 0);
}

/**
 * 命中掩码: 把刚画完的 WebGL 画布缩到 cols x rows 读透明度 (同一个任务里读, 画布内容还在),
 * 有角色的格外扩一格, 对话栏和滑出的侧菜单也算进去 (否则点不到); 按行优先每字节 8 格 (低位在前) 打包成 base64.
 */
function computeMask(v, now) {
  if (state.peek) return peekMask(now);
  if (ROOM) return roomMask(now);
  const w = stage.clientWidth, h = stage.clientHeight;
  const cols = Math.max(1, Math.ceil(w / CELL)), rows = Math.max(1, Math.ceil(h / CELL));
  if (maskCanvas.width !== cols || maskCanvas.height !== rows) { maskCanvas.width = cols; maskCanvas.height = rows; }
  maskCtx.clearRect(0, 0, cols, rows);
  maskCtx.drawImage(v.renderer.domElement, 0, 0, cols, rows);
  const px = maskCtx.getImageData(0, 0, cols, rows).data;
  const raw = new Uint8Array(cols * rows);
  let foot = -1;
  // 趴着时栏杆线以下是裁掉的 (画面里看不见, 下面的任务栏要点得到): 只算栏杆线以上
  const lastRow = state.rail ? Math.min(rows, Math.floor((state.rail * h) / CELL)) : rows;
  for (let r = 0; r < lastRow; r++) {
    for (let c = 0; c < cols; c++) {
      if (px[(r * cols + c) * 4 + 3] > ALPHA_MIN) { raw[r * cols + c] = 1; foot = r; }
    }
  }
  const bits = new Uint8Array(cols * rows);
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      if (!raw[r * cols + c]) continue;
      for (let dr = -1; dr <= 1; dr++) {
        for (let dc = -1; dc <= 1; dc++) {
          const rr = r + dr, cc = c + dc;
          if (rr >= 0 && cc >= 0 && rr < rows && cc < cols) bits[rr * cols + cc] = 1;
        }
      }
    }
  }
  for (const b of shell.hitRects()) {
    for (let r = Math.max(0, Math.floor(b.top / CELL)); r <= Math.min(rows - 1, Math.floor(b.bottom / CELL)); r++) {
      for (let c = Math.max(0, Math.floor(b.left / CELL)); c <= Math.min(cols - 1, Math.floor(b.right / CELL)); c++) bits[r * cols + c] = 1;
    }
  }
  state.lastFoot = foot;
  sendMask(cols, rows, bits, foot, now);
}

/** 全屏时露出对话栏 (宿主 peek): 角色不显示、不算, 只有对话栏和陪看提示条能点 (不挡全屏视频); 脚底照上一次报 */
function peekMask(now) {
  const w = innerWidth, h = innerHeight;
  const cols = Math.max(1, Math.ceil(w / CELL)), rows = Math.max(1, Math.ceil(h / CELL));
  const bits = new Uint8Array(cols * rows);
  for (const b of shell.hitRects()) {
    for (let r = Math.max(0, Math.floor(b.top / CELL)); r <= Math.min(rows - 1, Math.floor(b.bottom / CELL)); r++) {
      for (let c = Math.max(0, Math.floor(b.left / CELL)); c <= Math.min(cols - 1, Math.floor(b.right / CELL)); c++) bits[r * cols + c] = 1;
    }
  }
  sendMask(cols, rows, bits, state.lastFoot ?? -1, now);
}

function sendMask(cols, rows, bits, foot, now) {
  state.mask = { cols, rows, bits };
  const packed = new Uint8Array(Math.ceil((cols * rows) / 8));
  for (let i = 0; i < bits.length; i++) if (bits[i]) packed[i >> 3] |= 1 << (i & 7);
  let bin = '';
  for (const b of packed) bin += String.fromCharCode(b);
  const msg = `mask ${cols} ${rows} ${foot} ${btoa(bin)}`;
  if (msg !== state.maskMsg || now - state.maskSentAt >= MASK_RESEND) {
    post(msg);
    state.maskMsg = msg;
    state.maskSentAt = now;
  }
}

/** 小屋模式的掩码: 整张小屋卡片 (里面拖动、点击都归页面) 加上对话栏、侧菜单. 脚底 = 卡片底边. */
function roomMask(now) {
  const w = innerWidth, h = innerHeight;
  const cols = Math.max(1, Math.ceil(w / CELL)), rows = Math.max(1, Math.ceil(h / CELL));
  const bits = new Uint8Array(cols * rows);
  const card = stage.getBoundingClientRect();
  const rects = [card, ...shell.hitRects()];
  for (const b of rects) {
    for (let r = Math.max(0, Math.floor(b.top / CELL)); r <= Math.min(rows - 1, Math.floor((b.bottom - 1) / CELL)); r++) {
      for (let c = Math.max(0, Math.floor(b.left / CELL)); c <= Math.min(cols - 1, Math.floor((b.right - 1) / CELL)); c++) bits[r * cols + c] = 1;
    }
  }
  sendMask(cols, rows, bits, Math.min(rows - 1, Math.floor((card.bottom - 1) / CELL)), now);
  const foot = card.bottom / h;  // 宿主按这个把卡片底边放在任务栏上
  if (Math.abs(foot - state.footSent) > 1e-4) {
    state.footSent = foot;
    post(`foot ${foot.toFixed(5)}`);
  }
}

/**
 * 脚底在窗口里的位置 (0 顶 .. 1 底): 地面原点投影到屏幕. 行动层把鞋底落在地面 (y=0) 上, 镜头又是固定的, 所以这个值不随姿势抖动,
 * 宿主拿它把角色放到任务栏或窗口顶边上 (比掩码最下面一行准: 掩码一格就有 7 个物理像素).
 */
function reportFoot(v) {
  ground.set(v.loco?.char?.x || 0, 0, v.loco?.char?.z || 0).project(v.camera);
  const frac = (1 - ground.y) / 2;
  if (Math.abs(frac - state.footSent) < 1e-4) return;
  state.footSent = frac;
  post(`foot ${frac.toFixed(5)}`);
}

/** 两只手里较高的那只的高度 (米, 世界坐标). */
function handHeight(v) {
  const h = v.vrm.humanoid;
  let y = -Infinity;
  for (const name of ['leftHand', 'rightHand']) {
    const node = h.getRawBoneNode(name);
    if (node) y = Math.max(y, node.getWorldPosition(handPos).y);
  }
  return y;
}

/**
 * 对话自检: 这一轮里手抬起来最高多少 (米, 相对对话开始前的静止姿态), 用来确认手势叠加在 StayStill 上真的做出来了.
 * 基准不取每一轮开始时: 上一轮的手势可能还没放下.
 */
function trackHands(v) {
  const turn = talk.turn;
  if (!turn || turn.doneAt || state.handRest === undefined) return;
  const y = handHeight(v);
  if (Number.isFinite(y)) turn.handLift = Math.max(turn.handLift || 0, y - state.handRest);
}

// ---------------------------------------------------------------------------
function frame() {
  state.raf = 0;
  state.timer = 0;
  const t = performance.now() / 1000;
  const dt = Math.min(0.1, t - state.last);
  state.last = t;
  const v = state.avatar;
  if (v && OPTS.render) {
    const t0 = performance.now();
    const f = talk.frame(t, dt);
    const ch = motion.update(t, dt, f.viseme.level || 0);
    state.ch = ch;  // 挂了 StayStill 时, 手势叠加层从这里取手臂角度 (见 desk-body.js)
    if (state.room) state.room.before(dt);  // 小屋: 镜头跟着她, 房间要不要重画
    else if (!LIVE2D) frameCamera(v);
    updateGaze(v, t);
    if (director) updatePresence(v, t);
    v.apply(ch, { viseme: f.viseme, arkit: f.arkit, vad: motion.vad, thinking: motion.thinking, emo: talk.emo }, dt, f.neck);
    state.room?.after();
    updateLean(v);
    state.rendered++;
    if (!LIVE2D) trackHands(v);
    if (!LIVE2D && OPTS.free && !state.freed && state.rendered >= 5) {  // 几帧之后所有材质都画过, 贴图都上传了
      state.freed = true;
      state.report.textures_freed = freeTextureSources(v);
      log('贴图已上传, 释放原图', state.report.textures_freed, '张');
    }
    if (t - state.maskAt >= MASK_EVERY) {
      state.maskAt = t;
      computeMask(v, t);
      if (!LIVE2D && !ROOM) reportFoot(v);  // 2D 没有地面投影: 宿主用掩码最下面一行当脚底; 小屋: 卡片底边 (roomMask 里报)
      const head = headScreen(v);  // 思考气泡贴在头的左边
      if (head) shell.setHead(head[0], head[1]);
      if (!state.ready) {
        state.ready = true;
        post(`ready ${JSON.stringify({ avatar: v.info?.name, dpr: devicePixelRatio, lean: state.report,
          css: [stage.clientWidth, stage.clientHeight], canvas: [v.renderer.domElement.width, v.renderer.domElement.height], scale: petScale.k })}`);
      }
    }
    state.workMs += performance.now() - t0;
    state.frames++;
  }
  const wasActive = shell.active;
  shell.tick(t, talk.mic?.level ?? -100);
  if (wasActive !== shell.active) refreshMask();  // 对话栏收起了: 那块马上变回点穿
  if (state.peekWanted && shell.state === 'idle') { state.peekWanted = false; post('peek 0'); }  // 陪看那句说完、停留够了: 宿主收回
  if (t - state.statsAt >= 2) {  // 实际帧率和每帧 JS 耗时 (动作 + 渲染提交 + 掩码)
    syncPetScale();
    post(`stats fps=${(state.frames / (t - state.statsAt)).toFixed(1)} ms=${(state.workMs / Math.max(1, state.frames)).toFixed(2)}`);
    state.frames = 0;
    state.workMs = 0;
    state.statsAt = t;
  }
  setFps(wantedFps(), false);
  schedule();
}

/** 宿主给的是空闲帧率 (隐藏或暂停时 0); 说话、做动作、对话栏在动 (在听、在想、展开)、光标在头附近移动时升到 30. */
function wantedFps() {
  if (state.hostFps <= 0) return 0;
  const watching = gaze.near && gaze.active && performance.now() / 1000 - gaze.at < 0.8;
  const ctl = leanCtl(), leaning = ctl && Math.abs((ctl.on ? 1 : 0) - ctl.w) > 0.002;  // 正在趴下 / 站起来
  const roomBusy = state.room?.active;  // 小屋里走路、做动作、镜头在跟
  return talk.busy || shell.active || watching || leaning || roomBusy ? Math.max(state.hostFps, TALK_FPS) : state.hostFps;
}

/**
 * 闲时状态机 / 对话反应 (idle-director.js) 每帧一次: 她在说话、对话栏在动、陪看、工具在跑都算"在场"; 光标在头附近动算互动;
 * 输出交给门面叠到参数上 (setPresence), 一次性的事件 (换表情、清掉点头) 在这里办.
 */
function updatePresence(v, t) {
  director.setPresent('talk', talk.busy || shell.active, t);
  if (gaze.near && gaze.active && t - gaze.at < 0.8) director.interact(t, 'cursor');
  v.setPresence(director.update(t));
  const events = director.takeEvents();
  for (let i = 0; i < events.length; i++) {
    if (events[i].type === 'emotion') v.setEmotion(events[i].label);
    else if (events[i].type === 'cancelHead') v.cancelHeadActions?.();
  }
}

/** 头 (脸) 在窗口里的位置 (CSS 像素). 3D 用头骨骼现在的位置 (趴着、鞠躬时头会低下去), 往上 7 cm 是脸. */
function headScreen(v) {
  if (LIVE2D) return v.headScreen();
  const bone = v.vrm?.humanoid?.getRawBoneNode('head');
  if (bone) bone.getWorldPosition(headTmp).y += 0.07;
  else if (v.headPos) headTmp.copy(v.headPos);
  else return null;
  headTmp.project(v.camera);
  const r = ROOM ? stage.getBoundingClientRect() : { left: 0, top: 0, width: stage.clientWidth, height: stage.clientHeight };  // 小屋卡片不在窗口原点
  return [r.left + ((headTmp.x + 1) / 2) * r.width, r.top + ((1 - headTmp.y) / 2) * r.height];
}

/** 趴着的控制: 3D 在行动层的身体上 (desk-body.js 的 leaner), 2D 在 Live2D 角色上. 没有时趴不了. */
const leanCtl = () => (LIVE2D ? state.avatar?.lean : state.body?.lean) || null;

/**
 * 趴着: 栏杆线在窗口里的位置 (比例), 从脚底线随姿势过渡到前臂底下 (2D: 胸口). 过渡时每帧报给宿主, 宿主跟着把窗口往下挪,
 * 看起来是她一边往前趴、一边沉到栏杆 (任务栏) 后面. 栏杆线以下裁掉 (画布的 clip-path, 掩码也不算). 完全站起来时不裁.
 */
function updateLean(v) {
  const ctl = leanCtl();
  if (!ctl || (!ctl.on && ctl.w < 1e-3)) {
    if (state.rail !== null) setRail(null);
    return;
  }
  const k = ctl.w * ctl.w * (3 - 2 * ctl.w);
  let foot, full;
  if (LIVE2D) {
    foot = ctl.footFrac();
    full = ctl.railFrac();
  } else {
    ground.set(v.loco?.char?.x || 0, 0, v.loco?.char?.z || 0).project(v.camera);
    foot = (1 - ground.y) / 2;
    railTmp.copy(ctl.rail).project(v.camera);
    full = (1 - railTmp.y) / 2;
  }
  setRail(foot + (full - foot) * k);
}

function setRail(rail) {
  state.rail = rail;
  const canvas = state.avatar?.renderer?.domElement;
  if (canvas) canvas.style.clipPath = rail === null ? '' : `inset(0 0 ${((1 - rail) * 100).toFixed(2)}% 0)`;
  const head = rail === null ? null : headScreen(state.avatar);
  shell.setRail(rail, head ? head[1] - 70 : null);
  if (rail === null) {
    state.railSent = -1;
  } else if (leanCtl()?.on && Math.abs(rail - state.railSent) > 0.0005) {  // 站起来的过程不报 (宿主那边已经在拖了)
    state.railSent = rail;
    post(`lean ${rail.toFixed(5)}`);
  }
}

/** 趴着时被点了一下 (没拖动): 依次歪头笑、点头、抬手招一招 (2D 歪头)、害羞地瞟开. */
const POKES = ['tilt', 'nod', 'wave', 'shy'];
function poke() {
  const v = state.avatar, ctl = leanCtl();
  if (!v || !ctl) return;
  // 戳的次数记一个会衰减的数 (idle-director.js): 戳多了她会不好意思、再多就嫌你 (头偏开、皱眉); 前两下照旧轮着做四种反应
  const level = director ? director.poke(dnow()) : 0;
  if (level > 0) {
    talk._busyFor?.(2.5);
    wake();
    clearTimeout(state.pokeTimer);  // 表情由状态机的事件换、自己衰减回去
    return level === 1 ? 'shy-away' : 'annoyed';
  }
  const kind = POKES[state.pokes++ % POKES.length];
  if (kind === 'tilt') { ctl.poke('tilt'); v.setEmotion('happy'); }
  else if (kind === 'nod') v.headAction('nod', { amplitude: 0.5, repetitions: 2 });
  else if (kind === 'wave') { ctl.poke(LIVE2D ? 'tilt' : 'wave'); v.setEmotion('happy'); }
  else { v.setEmotion('shy'); v.glance(18, -8, 1.2); }
  talk._busyFor?.(2.5);  // 做小动作时升到 30 帧
  wake();
  clearTimeout(state.pokeTimer);
  state.pokeTimer = setTimeout(() => v.setEmotion('neutral'), 2600);
  return kind;
}

/**
 * 看向 (yaw 正值看屏幕右边, pitch 正值向上, 度), null 回来看着用户.
 * 3D 用表情引擎的视线 (它自己会平滑, 眼睛有上限); 它每设一次都会打断表演层的"瞟一眼", 所以角度变了半度以上才设.
 */
function follow(v, yaw, pitch) {
  if (LIVE2D) return v.follow(yaw, pitch);
  if (yaw === null) {
    gaze.sentYaw = gaze.sentPitch = null;
    v.engine?.releaseGazeControl();
    if (gaze.head) { gaze.head = false; v.engine?.releaseHeadControl(); }
  } else if (gaze.sentYaw === null || !v.engine?.gazePrecision ||  // 表演层 lookAtUser 放开过视线: 重新设
             Math.abs(yaw - gaze.sentYaw) > 0.5 || Math.abs(pitch - gaze.sentPitch) > 0.5) {
    gaze.sentYaw = yaw;
    gaze.sentPitch = pitch;
    v.engine?.setGazeDegrees({ yaw, pitch });
    // 头分担大的角度 (小屋里头归行动层的"看向"管, 不抢)
    const share = (a, max) => Math.sign(a) * Math.min(max, Math.max(0, Math.abs(a) - HEAD_SHARE.from) * HEAD_SHARE.k);
    if (!ROOM && v.engine) {
      v.engine.setHeadAngles({ yaw: share(yaw, HEAD_SHARE.yaw), pitch: share(pitch, HEAD_SHARE.pitch), roll: 0 });
      gaze.head = true;
    }
  }
}



/** 每帧: 光标最近动过就看着它, 否则回来看着用户. */
function updateGaze(v, t) {
  if (!(state.follow || gaze.test) || t - gaze.at >= FOLLOW_S) {
    if (gaze.active) { gaze.active = false; follow(v, null); }
    return;
  }
  if (ROOM && state.room) {  // 小屋: 光标在卡片上 (或附近) 时, 她抬头看光标指着的那一处 (头、眼一起, 不用转身); 远了看你
    const card = stage.getBoundingClientRect(), m = 60;
    const inside = gaze.x > card.left - m && gaze.x < card.right + m && gaze.y > card.top - m && gaze.y < card.bottom + m;
    if (t - (gaze.roomAt || 0) > 0.1) {
      gaze.roomAt = t;
      state.room.lookAtScreen(inside ? gaze.x - card.left : null, inside ? gaze.y - card.top : null);
    }
    gaze.active = true;
    return;
  }
  const head = headScreen(v);
  if (!head) return;
  const dx = gaze.x - head[0], dy = head[1] - gaze.y, deg = 180 / Math.PI;
  gaze.yaw = Math.atan2(dx, GAZE_DIST) * deg;
  gaze.pitch = Math.atan2(dy, GAZE_DIST) * deg;
  gaze.near = Math.hypot(dx, dy) < GAZE_NEAR;
  gaze.active = true;
  follow(v, gaze.yaw, gaze.pitch);
}

/** 光标位置 (窗口的比例, 宿主发来的; 自检直接调). */
function onCursor(fx, fy) {
  gaze.x = fx * stage.clientWidth;
  gaze.y = fy * stage.clientHeight;
  gaze.at = performance.now() / 1000;
  if (gaze.near) wake();
}

// 低于 60 帧用定时器. 按固定节拍排下一帧 (扣掉这一帧自己的耗时和定时器的拖延), 否则 15 帧实际只有 13 帧左右.
function schedule() {
  if (state.raf || state.timer || state.fps <= 0) return;
  if (state.fps >= 60) {
    state.raf = requestAnimationFrame(frame);
    return;
  }
  const now = performance.now(), interval = 1000 / state.fps;
  state.due = state.due && now - state.due < interval ? state.due + interval : now + interval;
  state.timer = setTimeout(frame, Math.max(0, state.due - now));
}

function setFps(fps, reschedule = true) {
  if (fps === state.fps && !reschedule) return;
  if (state.raf) cancelAnimationFrame(state.raf);
  if (state.timer) clearTimeout(state.timer);
  state.raf = state.timer = state.due = 0;
  state.fps = fps;
  if (reschedule) {
    state.last = performance.now() / 1000;
    schedule();
  }
}

/** 宿主说开/关麦克风 (托盘开关; 隐藏时宿主会先关掉). 结果报回宿主, 托盘据此显示. */
async function setMic(on) {
  if (!on) {
    talk.stopMic();
    post('mic off');
    return;
  }
  try {
    await talk.startMic();
    post('mic on');
  } catch (err) {
    post(`mic error ${err.message}`);
    shell.notice(`麦克风打不开: ${err.message}`);
  }
}

/** 有新消息 (对话开始、输入框打开) 时马上排一帧, 不等空闲帧率的下一拍. */
function wake() {
  const fps = wantedFps();
  if (fps !== state.fps) setFps(fps);
}

host?.addEventListener('message', (e) => {
  const m = e.data;
  if (m?.t === 'fps') {
    state.hostFps = Number(m.v) || 0;
    setFps(wantedFps());
  } else if (m?.t === 'input') {
    director?.interact(dnow(), 'input');
    shell.openInput();
  } else if (m?.t === 'hover') {
    if (m.v) director?.interact(dnow(), 'hover');
    shell.hover(!!m.v);
    refreshMask();
  } else if (m?.t === 'peek') {  // 全屏时露出对话栏 (2026-11-07): 角色、侧菜单不显示, 对话栏挪到右下角; 掩码只算对话栏
    state.peek = !!m.on;
    document.body.classList.toggle('peek', state.peek);
    refreshMask();
  } else if (m?.t === 'ui') {
    shell.setMic(!!m.mic);
    shell.setMuted(!!m.muted);
    shell.setPinned(m.topmost !== false);
    state.follow = !!m.follow;
  } else if (m?.t === 'lean') {  // 宿主: 放着不动够久了趴下, 或者被拖起来了站起来
    const ctl = leanCtl();
    if (!ctl) {
      if (m.v) post('lean none');  // 3D 没挂上行动层: 做不了这个姿势
    } else {
      ctl.set(!!m.v);
      wake();
    }
  } else if (m?.t === 'activity' || m?.t === 'activity-mode') {  // 看你在做什么 (宿主看前台窗口)
    activity.handle(m);
  } else if (m?.t === 'cursor') {
    gaze.received++;
    if (!gaze.test && !gaze.freeze) onCursor(Number(m.x), Number(m.y));  // freeze: 动作自检时不跟鼠标
  } else if (m?.t === 'mute') {
    if (talk.out) talk.out.gain.gain.value = m.v ? 0 : 1;
    talk.muted = !!m.v;
  } else if (m?.t === 'mic') {
    setMic(!!m.v);
  } else if (m?.t === 'talk-go') {
    state.talkGo?.();
  } else if (m?.t === 'api') {
    api.handle(m);
  } else if (m?.t === 'profile') {  // 操作面板里的当前人设预设 (含云端模型的 key, 不记日志)
    talk.setProfile(m.profile);
    api.setProfile(m.profile);
    shell.setPersonas(m.personas, m.active);
  } else if (m?.t === 'qq') {  // QQ 渠道的设置 (含 token 和 key, 不记日志): 连上时和改了时发给服务端 (qq.js)
    qq.setConfig(m.config, m.paused);
  } else if (m?.t === 'qq-pause') {
    qq.pause(!!m.v);
  } else if (m?.t === 'tools') {  // 宿主按当前预设列好的工具表 (文字模式那条连接不接工具: 没有地方问用户)
    talk.setTools(m.tools);
    state.tools = { count: m.tools.length, cloud: m.cloud, servers: m.servers };
    if (SELF_TASK.length > 1 && m.tools.length && !state.selfTask) {  // 自检: 发一张假的 QQ 任务单
      state.selfTask = true;
      setTimeout(() => tasks.handle({ type: 'channel_task', id: 'selftest', by: SELF_TASK[0], text: '自检任务', prompt: SELF_TASK.slice(1).join('|') }), 1500);
    }
  } else if (m?.t === 'tool-ask') {  // "每次确认"的工具: 在对话栏下面问用户
    const auto = TOOL_ANSWERS.shift() || null;
    shell.toolNote('等你确认…');
    // 高风险的工具、她自己想动手: 同时在 QQ 上问主人 (他不在电脑前时也答得了), 卡片多等一会儿; 自检指定了 asktimeout 就照它
    const relay = tasks.askOpen(m, qq.status?.state === 'online');
    shell.askTool(m, relay && !PARAMS.has('asktimeout') ? ASK_QQ_S : ASK_S, auto).then((how) => {
      tasks.askClosed(m.call_id, how === 'once' || how === 'always' || how === 'turn' || how === 'deny' ? 'answered' : 'cancelled');
      post(`tool-answer ${m.call_id} ${how}`);
      if (how === 'once' || how === 'always' || how === 'turn') shell.toolNote(`在用工具 ${m.server} · ${m.tool}…`, m.call_id);
      else shell.toolNote('想一想…');
      refreshMask();
    });
    refreshMask();
  } else if (m?.t === 'room-capabilities') {
    post(`builtin-done ${m.call_id} 1 ${JSON.stringify({ scene_id: state.room?.sceneId || null, activities: state.room?.capabilities?.() || [] })}`);
  } else if (m?.t === 'room-action-cancel') {
    if (state.room?.life.requests.active?.id === `tool:${m.call_id}`) state.room.cancelAct(m.reason || '工具请求已停止');
  } else if (m?.t === 'room-do') {  // 自带工具 room_do: 让她在小屋里做某件事, 做没做成回给宿主 (再交给模型)
    const room = state.room;
    if (!room) post(`builtin-done ${m.call_id} 0 现在不在小屋模式`);
    else room.requestAct(m.act, { requestId: `tool:${m.call_id}` }).then(result => {
      post(`builtin-done ${m.call_id} ${result.ok ? 1 : 0} ${JSON.stringify({ ok: result.ok, act: result.act, started: result.started, completed: result.completed, outcome: result.outcome, cleanupConfirmed: result.cleanupConfirmed, cancelled: result.cancelled, reason: result.reason || '', detail: result.detail || null })}`);
    }).catch(error => post(`builtin-done ${m.call_id} 0 ${error.message}`));
  } else if (m?.t === 'remind') {  // 提醒到点了 (宿主定时): 对话栏里说 (停 30 秒), 挥挥手; 小屋里走过来看你
    const late = m.late >= 1 ? ` (晚了 ${Math.round(m.late)} 分钟)` : '';
    shell.notice(`⏰ 提醒你: ${m.text}${late}`, 30);
    if (state.room) state.room.doAct?.('visit');
    else talk.runActs([{ do: 'wave', side: 'R', to: null, amp: 1, spd: 1, at: 0 }], 'act');
    wake();
    refreshMask();
    log('提醒到了', m.id);
  } else if (m?.t === 'task-shot') {  // QQ 任务单做完了: 宿主交回暂存的最后一张截图 (屏幕上有聊天、密码这类窗口时没留), 压小以后发到 QQ
    tasks.onShot(m);
  } else if (m?.t === 'tool-blocked') {  // 守门拦下了 (付款、发消息、删文件这类地方要用户自己来): 对话栏里提示一句
    shell.toolNote(m.text);
    refreshMask();
  } else if (m?.t === 'control') {  // 操控电脑开始 / 结束: 对话栏上面的提示条
    director?.setPresent('control', !!m.on, dnow());
    shell.control(!!m.on, m.hotkey);
    refreshMask();
  } else if (m?.t === 'control-stop') {  // 一键停 (停、快捷键、真人动了鼠标键盘、断开): 打断这一轮
    talk.interrupt();
    shell.control(false);
    shell.notice(`停下了 (${m.why})`);
    roomTools.clear();
    liveTools.clear();
    director?.setPresent('tool', false, dnow());
    director?.setPresent('control', false, dnow());
    state.room?.cancelAct?.('用户停止了执行');
    state.room?.working(0);
    refreshMask();
  } else if (m?.t === 'tool-ask-cancel') {  // 服务端不等了 (这一轮超过 tool_timeout_s 被收掉) 或被停了: 卡片收起, QQ 上等答复的那项也作废并告诉主人
    shell.cancelAsk(m.call_id);
    tasks.askClosed(m.call_id, 'cancelled');
    refreshMask();
  } else if (m?.t === 'tool-result') {
    shell.toolDone(m.call_id);
    roomTools.delete(m.call_id);
    liveTools.delete(m.call_id);
    director?.setPresent('tool', liveTools.size > 0, dnow());
    state.room?.working(roomTools.size);
    refreshMask();
    talk.send({ type: 'tool_result', call_id: m.call_id, content: m.content, is_error: !!m.is_error });
  }
});

// 小屋: 你把文件 (Markdown、文本、图片、PDF) 从桌面拖进来, 一本小册子掉在她的电脑桌上, 她过去翻, 下次翻开日记本时记进知识库.
// 文本类读出来最多 20000 字 (内容是外面来的, 服务端只当资料存, 不当指令); 图片、PDF 只带名字
const DOC_KIND = (name) => (/\.(md|markdown)$/i.test(name) ? 'md' : /\.txt$/i.test(name) ? 'txt' : /\.(png|jpe?g|gif|webp|bmp)$/i.test(name) ? 'image'
  : /\.pdf$/i.test(name) ? 'pdf' : null);
addEventListener('dragover', (e) => { if (ROOM && e.dataTransfer?.types?.includes('Files')) { e.preventDefault(); e.dataTransfer.dropEffect = 'copy'; } });
addEventListener('drop', async (e) => {
  if (!ROOM) return;
  e.preventDefault();
  for (const f of [...(e.dataTransfer?.files || [])].slice(0, 3)) {
    const kind = DOC_KIND(f.name);
    if (!kind) { shell.notice(`这个她看不懂: ${f.name}`); continue; }
    const text = kind === 'md' || kind === 'txt' ? (await f.text()).slice(0, 20000) : '';
    const said = await state.room?.dropDoc({ name: f.name.replace(/\.[^.]+$/, '').slice(0, 40), kind, text });
    if (said) { roomStatus.textContent = said; log(`小屋: 拖进来一个文件 (${kind}, ${text.length} 字)`); }
  }
  wake();
});

/** 她想起来的事 / 今天的日记: 房间上面那块空处滑出一张小卡片, 十几秒后收起. */
function showMemoryCard({ title, text, day }) {
  let el = document.querySelector('.room-memory');
  if (!el) { el = document.createElement('div'); el.className = 'room-memory'; document.body.append(el); }
  el.innerHTML = '';
  const h = document.createElement('div');
  h.className = 'room-memory-title';
  h.textContent = day ? `${title} · ${day}` : title;
  const p = document.createElement('div');
  p.className = 'room-memory-text';
  p.textContent = text.length > 160 ? `${text.slice(0, 160)}…` : text;
  el.append(h, p);
  const card = stage.getBoundingClientRect();
  el.style.bottom = `${innerHeight - card.top + 12}px`;
  requestAnimationFrame(() => el.classList.add('show'));
  clearTimeout(state.memoryTimer);
  state.memoryTimer = setTimeout(() => el.classList.remove('show'), 14000);
}

// 按在角色身上才拖; 宿主收到后自己跟着光标移动窗口, 松手落到地面. 双击角色打开输入框.
// 趴着时先不拖: 按住移动超过 5 像素才拖 (拖起来就站起来了), 没动就松手算点了她一下 (poke).
let pressed = null;
stage.addEventListener('pointerdown', (e) => {
  const m = state.mask;
  if (ROOM) return;  // 小屋卡片里拖动是看房间 (room-life.js 的镜头); 挪桌宠用卡片顶上的把手
  if (e.button !== 0 || !m || e.target !== stage && e.target.tagName !== 'CANVAS') return;
  const c = Math.floor(e.clientX / CELL), r = Math.floor(e.clientY / CELL);
  if (!m.bits[r * m.cols + c]) return;
  if (leanCtl()?.on) {
    pressed = { x: e.clientX, y: e.clientY };
    stage.setPointerCapture(e.pointerId);  // 光标滑出剪影也继续收到移动
  } else {
    post('drag');
  }
});
stage.addEventListener('pointermove', (e) => {
  if (pressed && Math.hypot(e.clientX - pressed.x, e.clientY - pressed.y) > 5) {
    pressed = null;
    post('drag');
  }
});
stage.addEventListener('pointerup', () => {
  if (!pressed) return;
  pressed = null;
  const kind = poke();
  if (kind) post(`log 趴着时被点了一下: ${kind}`);
});
stage.addEventListener('dblclick', () => { if (!ROOM) shell.openInput(); });  // 小屋里双击是镜头回到跟着她

// ---------------------------------------------------------------------------
/**
 * 表情还能不能动 (压缩变形目标之后的检查, 页面地址带 ?check=morph 时做): 把"张嘴 A"的权重分别设成 0 和 1 各渲染一次,
 * 比较画面中上部 (脸) 有多少像素变了. 返回变了的像素数; 找不到口型形变时返回 -1.
 */
function morphCheck(v) {
  const name = v.visemeMorphs?.aa || v.arkit?.jawOpen;
  if (!name) return -1;
  frameCamera(v);
  const gl = v.renderer.getContext();
  const w = gl.drawingBufferWidth, h = gl.drawingBufferHeight;
  const grab = (value) => {
    for (const m of v.morphs) {
      const i = m.morphTargetDictionary[name];
      if (i !== undefined) m.morphTargetInfluences[i] = value;
    }
    v.renderer.render(v.scene, v.camera);
    const px = new Uint8Array(w * Math.floor(h / 3) * 4);
    gl.readPixels(0, h - Math.floor(h / 3), w, Math.floor(h / 3), gl.RGBA, gl.UNSIGNED_BYTE, px);
    return px;
  };
  const closed = grab(0), open = grab(1);
  grab(0);
  let changed = 0;
  for (let i = 0; i < closed.length; i += 4) {
    if (Math.abs(closed[i] - open[i]) + Math.abs(closed[i + 1] - open[i + 1]) + Math.abs(closed[i + 2] - open[i + 2]) > 30) changed++;
  }
  return changed;
}

/**
 * 对话自检 (页面地址带 ?talk=句子1|句子2 时): 等宿主说开始, 依次发出每一句, 等这一轮的声音放完再发下一句,
 * 把每一轮的意图、回复、音频、口型汇总发给宿主. 只在 8792 的 mock 服务上跑, 不对真实服务发测试对话.
 */
async function talkSelftest(lines) {
  await new Promise((resolve) => { state.talkGo = resolve; });
  const until = async (pred, ms) => {
    for (let t = 0; t < ms; t += 100) {
      if (pred()) return true;
      await new Promise((r) => setTimeout(r, 100));
    }
    return pred();
  };
  const connected = await until(() => talk.connected, 10000);
  if (!LIVE2D) state.handRest = handHeight(state.avatar);  // 静止姿态下手的高度, 各轮手抬高多少都相对它 (2D 没有手的骨骼)
  const turns = [];
  for (const text of connected ? lines : []) {
    await talk.say(text);
    const turn = talk.turn;
    const done = await until(() => turn.doneAt, 30000);
    const fpsDuring = state.fps;
    turns.push({ text, done: !!done, turn: turn.id, op: turn.op, acts: turn.acts, body: turn.body, emo: turn.emo,
      reply: (turn.reply || '').slice(0, 60), amend: turn.amend,
      intent_ms: turn.intentAt ? Math.round(turn.intentAt - turn.sentAt) : null,
      audio_ms: turn.audioAt ? Math.round(turn.audioAt - turn.sentAt) : null,
      audio_s: +(turn.audioSeconds || 0).toFixed(2), segments: turn.segments || 0, face_frames: turn.faceFrames || 0,
      prosody: turn.prosody || 0, max_mouth: +(turn.maxLevel || 0).toFixed(2), fps_during: fpsDuring,
      hand_lift_m: +(turn.handLift || 0).toFixed(3) });
    await new Promise((r) => setTimeout(r, 800));
  }
  await until(() => !talk.busy, 5000);
  const voice = PARAMS.has('voice') && connected ? await voiceSelftest(PARAMS.get('voice'), until) : null;
  post(`talk ${JSON.stringify({ connected, status: talk.status && { llm: talk.status.llm.engine, tts: talk.status.tts.engine,
    asr: talk.status.asr.engine }, turns, voice, fps_after: state.fps, audio_state: talk.ctx?.state })}`);
}

/**
 * 界面演示 (页面地址带 ?ui=demo 时, 自检连拍用): 不经过服务端, 依次演一遍侧菜单滑出、在听、识别中、在想、逐字讲一段长故事、
 * 说完、展开对话记录. 麦克风只是界面上显示开着, 不真的开.
 */
async function uiDemo() {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  shell.hover(true);
  shell.setMic(true);
  refreshMask();
  await sleep(1500);
  // 侧菜单的人设列表: 打开, 停一会儿 (连拍看得到), 换成另一个预设 (有的话), 宿主存下来再发回新的列表
  shell.showPersonas(true);
  refreshMask();
  await sleep(1600);
  const other = shell.el.personaList.querySelector('li:not(.cur)');
  const switchedTo = other?.dataset.id ?? null;
  if (other) other.click();
  else shell.showPersonas(false);
  refreshMask();
  await sleep(2000);
  // 置顶键: 点一下取消置顶 (宿主改窗口, 再把窗口真实的样子报回来), 再点一下恢复. 中间那一会儿连拍里桌宠会被棋盘格盖住
  const pins = [shell.pinned];
  shell.el.pin.click();
  await sleep(800);
  pins.push(shell.pinned);
  shell.el.pin.click();
  await sleep(800);
  pins.push(shell.pinned);
  shell.listening();
  await sleep(1200);
  shell.partial('给我讲个');
  await sleep(700);
  shell.partial('给我讲个故事吧');
  await sleep(700);
  shell.turn('给我讲个故事吧');
  await sleep(1500);
  const story = '从前有一座山，山里住着一只小狐狸。它每天傍晚都跑到山顶看星星，数到第一百颗就回家睡觉。'
    + '有一天，它发现天上多了一颗会眨眼的星星，那颗星星一直跟着它走，一直跟到了它的家门口。'
    + '小狐狸鼓起勇气问：你是谁呀？星星说：我是从天上掉下来的，想找个朋友。';
  for (const piece of story.match(/[^，。？！：]+[，。？！：]?/g)) {
    shell.reply(piece);
    await sleep(380);
  }
  shell.done();
  await sleep(2000);
  shell.setExpanded(true);
  refreshMask();
  await sleep(2500);
  const rects = shell.hitRects().length;
  shell.setExpanded(false);
  shell.setMic(false);
  shell.hover(false);
  post(`talk ${JSON.stringify({ ui: 'demo', hit_rects_expanded: rects, history: shell.history.length,
    personas: shell.personas.length, switched_to: switchedTo, active_after: shell.activePersona, pinned: pins })}`);
}

/**
 * 小屋的自检 (room=1&roomtest=秒, 常带 life=顺序&fast=1): 等这么久, 中途替你点她一下、点一下床 (直接调点击, 不经过鼠标),
 * 再拖一下镜头, 报她做过的事、房间重画了几次、头发物理每帧拆成几步. 连拍 (--film) 看样子.
 * 带 life=顺序 时只按顺序做那几件事 (不拖镜头、不点床), 做完或到时间就报; feed=drink,snack 开始时依次投喂 (每样隔 1 秒).
 * thinktest=1 (常带 think=0 每件都问服务端): 不点不拖, 记每件事开始前状态栏那句, 报每件是服务端想的 (src: plan) 还是本机规则 (rule).
 * 心情和摆法存在页面的本机存储里: 自检跑的是开发版 (build/dev, 浏览器数据在 build/dev/webview2-data), 不碰用户那份;
 * roomreset=1 开始前清掉, 每次从头来.
 */
async function roomSelftest() {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const room = state.room, secs = Number(PARAMS.get('roomtest')) || 30;
  if (!room) {
    post(`talk ${JSON.stringify({ room: false, error: '小屋没加载' })}`);
    return;
  }
  if (PARAMS.has('pokes')) {  // pokes=head,body,body,...: 隔 1.2 秒点她一下, 记她每次的反应 (反应按点到哪、这一阵点了几次)
    await sleep(3000);
    const said = [];
    for (const part of PARAMS.get('pokes').split(',').filter(Boolean)) {
      said.push(`${part}: ${room.poke(part)}`);
      await sleep(1200);
    }
    post(`talk ${JSON.stringify({ room: true, pokes: said, needs: room.needs.summary() })}`);
    return;
  }
  if (PARAMS.has('liquidtest')) {
    // 小窝的液体 (room-liquid.js): liquidtest=pour (常带 roomreset=1): 白色浓缩杯 (咖啡机上) 倒满热咖啡, 她脚前的地上、
    // 电脑桌上各倒一摊, 等落下来、连拍看液面和水渍; liquidtest=check (不带 roomreset): 重新打开以后报存下来的 (该还在、凉了一些)
    await sleep(3000);
    const L = room.liquid, w = L?.world;
    if (!w) { post(`talk ${JSON.stringify({ room: true, liquidtest: { error: '没有液体模块' } })}`); return; }
    const before = L.snapshot();
    if (PARAMS.get('liquidtest') === 'pour') {
      w.fill('IR_Item_Espresso_White', 60, 70, 'coffee');
      const c = room.layer.char, f = [Math.sin(c.yaw), Math.cos(c.yaw)];
      w.pour([c.x + 0.45 * f[0], 0.6, c.z + 0.45 * f[1]], 25, 70, 'coffee');
      L._spillNote(25, 'test');  // 当成洒了一阵: 落在她脚前 (看得见), 她该知道
      const desk = room.layer.targets.surf_desk?.place;
      if (desk) w.pour([desk[0], desk[1] + 0.3, desk[2]], 12, 22, 'water');
      L.dirty = true;
      await sleep(6000);
      L.save();
    }
    post(`talk ${JSON.stringify({ room: true, liquidtest: { mode: PARAMS.get('liquidtest'), before, after: L.snapshot(),
      perceive: L.perceive(), feelings: room.body.feelings(),
      puddle_meshes: L.sheets ? L.sheets.count : L.puddleMeshes.size, discs: Object.values(L.cups).filter((c) => c.disc.visible).length } })}`);
    return;
  }
  if (PARAMS.has('cuptest')) {
    // 被移动的杯子 (认知阶段 2, wm/cognition/): 第一天先冲一次咖啡 (看到杯子在咖啡机上); 每一天她去阅读角看书, 坐下以后替你把粉色杯子
    // 挪到 cuptest=那一处 (逗号隔开是每天一处, 默认电脑桌上: 她看不到; 小边桌上就在她旁边, 看得到), 问她"你的杯子在哪呀",
    // 再让她去冲咖啡: 走过去、发现不在、按服务端给的步子找、找到放回咖啡机上再冲. cupdays=几天 (默认和 cuptest 给的处数一样, 至少 2:
    // 第二天应该直接去上次找到的地方). 报每天找的每一步、她的回答、状态栏.
    await sleep(3000);
    const targets = (PARAMS.get('cuptest') || 'surf_desk').split(',').filter(Boolean);
    const days = Number(PARAMS.get('cupdays') || Math.max(2, targets.length)), out = { targets, days: [] };
    const replies = [], onReply = talk.hooks.onReply, status = [];
    talk.hooks.onReply = (turn, text) => { replies.push(text); onReply(turn, text); };
    let last = '';
    const watch = setInterval(() => { const t = roomStatus.textContent; if (t !== last) { status.push(t); last = t; } }, 200);
    const until = async (ok, ms) => { for (const t = performance.now(); !ok() && performance.now() - t < ms;) await sleep(250); return ok(); };
    const doing = async (id, ms) => {  // 让她去做 id (正在做的先收尾), 等做完; 冲咖啡前精神压到 50 (85 以上她不想喝)、咖啡豆补到 2 份
      if (id === 'coffee') {
        room.needs.v.energy = Math.min(room.needs.v.energy, 50);
        room.needs.stock.beans = Math.max(room.needs.stock.beans, 2);
      }
      const went = await until(() => room.life.current?.id === id || room.life.goDo(id) && room.life.current?.id === id, 60000);
      await until(() => room.life.current?.id !== id, ms);
      return went;
    };
    for (let d = 0; d < days; d++) {
      const day = {}, to = targets[d % targets.length];
      if (d === 0) day.first_coffee = await doing('coffee', 120000);
      room.life.goDo('read');
      day.seated = await until(() => room.life.current?.id === 'read' && room.sight.zone() === 'reading' && !!room.layer.seated, 60000);
      day.moved = room.moveItem('IR_Item_Espresso_Pink', to, '自检');
      await sleep(1500);
      const n = replies.length;
      userSays('你的杯子在哪呀');
      await until(() => replies.length > n, 20000);
      day.answer = replies.slice(n).join('');
      status.length = 0;
      const searches = room.life.searchLog.length;
      day.coffee = await doing('coffee', 240000);  // 别看了, 去冲咖啡
      day.search = room.life.searchLog.slice(searches);
      day.status = status.slice(-16);
      day.done = room.life.done.at(-1);
      out.days.push(day);
    }
    clearInterval(watch);
    talk.hooks.onReply = onReply;
    out.sees = room.sight.now();
    post(`talk ${JSON.stringify({ room: true, cuptest: out })}`);
    return;
  }
  if (PARAMS.has('restarttest')) {
    // 服务端重启 = 她睡一觉 (self-evolution.md 4.3): 假装看守进程发来 server_restart, 她去床上睡; 睡着后断开连接 (像服务端停了),
    // 替你说两句 (变便利贴); 马上重连上 (hello) 她醒来, 撕下便利贴一条条回. 再假装 QQ 那边在看 B 站: 她坐到电脑前, 状态栏写着在看什么
    await sleep(3000);
    const heard = [], onTurn = talk.hooks.onTurn, t0 = performance.now(), secs = () => +((performance.now() - t0) / 1000).toFixed(1);
    talk.hooks.onTurn = (turn, text, source) => { heard.push(text); onTurn(turn, text, source); };
    // 等她坐下 (看书、发呆) 再发: 坐着的就地打盹; 站着的去床上 (那条路 restarttest=bed 测: 不等坐下)
    if (PARAMS.get('restarttest') !== 'bed') for (const t = performance.now(); !room.layer.seated && performance.now() - t < 60000;) await sleep(300);
    const out = { started: room.life.current?.id || null, seated: !!room.layer.seated };
    talk.hooks.onServerRestart({ type: 'server_restart', reason: 'restart', eta_s: 5, stop: false });
    out.nap = room.life.restarting;
    while (!room.asleep && !room.life.dozing && performance.now() - t0 < 60000) await sleep(300);
    out.asleep_s = secs();
    out.how = room.life.dozing ? 'doze' : room.asleep ? 'bed' : null;  // 坐着: 就地打盹; 站着: 去床上
    out.status_asleep = roomStatus.textContent;
    talk.ws.close();  // 像服务端停了 (马上会重连, 连上时的 hello 叫醒她)
    userSays('重启自检 1');
    userSays('重启自检 2');
    out.notes_while_down = room.notesLeft;
    out.status_down = roomStatus.textContent;
    while ((heard.filter((t) => /^重启自检/.test(t)).length < 2) && performance.now() - t0 < 150000) await sleep(500);
    out.replied = heard.filter((t) => /^重启自检/.test(t));
    out.replied_s = secs();
    out.awake = !room.asleep && !room.life.dozing;
    out.last_sleep = room.life.lastSleep;
    talk.hooks.onTurn = onTurn;
    await sleep(2000);
    talk.hooks.onChannel({ type: 'channel_doing', channel: 'qq', what: 'video', text: '在 B 站看《自检视频》', secs: 120 });
    const t1 = performance.now();
    while (room.life.current?.id !== 'computer' && performance.now() - t1 < 60000) await sleep(500);
    const went = +((performance.now() - t1) / 1000).toFixed(1);
    while (!roomStatus.textContent.includes('自检视频') && performance.now() - t1 < 60000) await sleep(500);  // 坐下以后状态栏写着在看什么
    out.doing = { act: room.life.current?.id || null, went_s: went, status: roomStatus.textContent, s: +((performance.now() - t1) / 1000).toFixed(1) };
    talk.hooks.onChannel({ type: 'channel_doing', channel: 'qq', what: 'video', text: '', secs: 0 });
    post(`talk ${JSON.stringify({ room: true, restarttest: out })}`);
    return;
  }
  if (PARAMS.has('life')) {
    for (const kind of (PARAMS.get('feed') || '').split(',').filter(Boolean)) { room.feed(kind); await sleep(1000); }
    if (PARAMS.has('fatigue')) room.needs.load = Number(PARAMS.get('fatigue')) * LOAD_MAX;  // fatigue=0.95: 一开始就困得不行
    if (PARAMS.has('doc')) room.dropDoc({ name: '自检资料', kind: 'md', text: '# 自检资料\n这是自检拖进来的一段文字。' });
    const notes = Number(PARAMS.get('notes') || 0);
    if (notes) (async () => {  // 睡着以后贴几张便利贴 (就像睡着时有人发了消息)
      while (!room.asleep) await sleep(500);
      for (let i = 1; i <= notes; i++) { room.note(`自检便利贴 ${i}`); await sleep(800); }
    })();
    const want = PARAMS.get('life').split(',').length + (PARAMS.get('feed') || '').split(',').filter(Boolean).length;
    const samples = [];  // 每 2 秒: 在做什么、姿势、状态栏
    const end = performance.now() + secs * 1000;
    while (performance.now() < end && (room.life.done.length < want || room.life.current)) {
      await sleep(1000);
      let extra = '';
      if (/^tree|^bend|^fold/.test(room.poses.name || '')) {  // 平衡: 体感算的余量 (负的是质心出了支撑面), 站定了多久
        const h = state.avatar.vrm.humanoid, hips = h.getNormalizedBoneNode('hips').getWorldPosition(new THREE.Vector3());
        const foot = (f) => h.getNormalizedBoneNode(`${f}Foot`).getWorldPosition(new THREE.Vector3());
        const lf = foot('left'), rf = foot('right');
        extra = `|m=${room.body.margin?.toFixed(3)},still=${room.body.still.toFixed(1)},hipX=${hips.x.toFixed(3)},lf=${lf.x.toFixed(3)}/${lf.y.toFixed(2)},rf=${rf.x.toFixed(3)}/${rf.y.toFixed(2)}`;
      }
      if (room.poses.name === 'sip' || room.poses.name === 'bite') {  // 手离嘴多远 (喝东西的手有没有送到嘴边)
        const side = room.poses.opts.side || 'right', h = state.avatar.vrm.humanoid;
        const hand = h.getNormalizedBoneNode(`${side}Hand`).getWorldPosition(new THREE.Vector3());
        const head = h.getNormalizedBoneNode('head').getWorldPosition(new THREE.Vector3());
        extra = `|hand-head=${hand.distanceTo(head).toFixed(2)},W=${room.poses.W.toFixed(2)},ik=${room.poses.err}`;
      }
      for (const it of Object.values(room.layer.items)) {  // 手里的东西: 离手心多远 (cm, 0 是贴着/握在里面)、竖着没有 (杯子)
        if (!it.held) continue;
        const f = room.props._palm(it.held), node = room.props.glb[it.id]?.node || it.mesh;
        if (!f) continue;
        const gap = new THREE.Box3().setFromObject(node).distanceToPoint(f.C) * 100;
        const up = new THREE.Vector3(0, 1, 0).applyQuaternion(node.getWorldQuaternion(new THREE.Quaternion())).y;
        const mid = state.avatar.vrm.humanoid.getNormalizedBoneNode(`${it.held}MiddleProximal`);  // 中指根节弯了多少度 (手形用上了没有)
        const curl = mid ? (2 * Math.acos(Math.min(1, Math.abs(mid.quaternion.w))) * 180) / Math.PI : NaN;
        extra += `|hold=${it.grip}:${gap.toFixed(1)}cm,up=${up.toFixed(2)},curl=${curl.toFixed(0)}`;
        const liq = room.liquid?.world.cups[it.id];  // 杯里装了多少 (ml)、多烫、液面贴壁升高 (mm, 晃得多厉害); 她看到的粗档
        if (liq) extra += `,liq=${liq.ml.toFixed(0)}ml/${liq.temp.toFixed(0)}C/${(Math.hypot(...liq.A) * 1000).toFixed(0)}mm,saw=${room.liquid.perceive({ take: false }).cups[it.id] ?? '-'}${room.liquid.tilted[it.id] ? `,mouthGap=${room.liquid.mouthGap}` : ''}`;
      }
      if (room.body.self) extra += `|self=${room.body.self.pair}:${room.body.self.cm}cm`;
      if (room.layer.mode === 'idle' && !room.layer.seated && !room.poses.pose) {
        // 站着: 鞋底平不平 —— 脚踝比脚尖高多少, 和模型静止姿势 (标准化骨骼的 T 姿势, 脚是平的) 时差几 cm, 0 是平贴地面
        const h = state.avatar.vrm.humanoid, rest = h.normalizedRestPose || {};
        const flat = ['left', 'right'].map((f) => {
          const drop0 = -(rest[`${f}Toes`]?.position?.[1] ?? NaN);  // 静止时脚尖比脚踝低多少 (标准化骨骼零位: 局部偏移就是世界偏移)
          const a = h.getNormalizedBoneNode(`${f}Foot`)?.getWorldPosition(new THREE.Vector3()), t = h.getNormalizedBoneNode(`${f}Toes`)?.getWorldPosition(new THREE.Vector3());
          return a && t && Number.isFinite(drop0) ? ((a.y - t.y - drop0) * 100).toFixed(1) : '-';
        });
        extra += `|flat=${flat.join('/')}cm`;
      }
      if (room.layer.seated) {  // 坐着: 骨盆比该在的高度 (座面 + 0.09 m 按身高换算) 高多少 cm (负的是陷进去), 行动层的"坐下程度"
        const L = room.layer, hipsY = state.avatar.vrm.humanoid.getNormalizedBoneNode('hips').getWorldPosition(new THREE.Vector3()).y;
        const top = room.body.r.seatTop?.(L.seated.seat) ?? L.seated.seat.height;
        extra += `|seat=${((hipsY - top - 0.09 * L.hipHeight / 0.78) * 100).toFixed(1)}cm,amt=${L.seatAmount.toFixed(2)},h=${L.seated.seat.height.toFixed(2)},lift=${(L.hipsExtraY ?? 0).toFixed(2)}`;
        extra += `,drop=${(L.hipDrop ?? 0).toFixed(3)}`;  // 腿够不着地时行动层把骨盆降了多少 (坐着时应该是 0)
      }
      if (/^(lie|lotus)/.test(room.poses.name || '')) {  // 睡着、冥想: 眼睛闭上没有 (脸上 eyeBlink / blink 形态键此刻的权重, 1 是闭上)
        let eyes = 0;
        state.avatar.vrm.scene.traverse((o) => {
          const dict = o.morphTargetDictionary, inf = o.morphTargetInfluences;
          if (!dict || !inf) return;
          for (const [name, i] of Object.entries(dict)) if (/eyeBlink|^blink$|Blink_(L|R)$/i.test(name)) eyes = Math.max(eyes, inf[i] || 0);
        });
        extra += `|eyes=${eyes.toFixed(2)}`;
      }
      if (/^(tuck|longSit|lie)/.test(room.poses.name || '') && room.props.bedBlanket) {  // 上床、躺下: 有多少顶点陷进床垫
        const k = room.props.bedBlanket.sink(state.avatar.vrm.scene, state.avatar.vrm.humanoid);
        extra += `|sink=${k.n},${k.cm}cm,${k.near}`;
      }
      const quilt = room.props.bedBlanket;
      if (quilt?.cover > 0.3) {  // 盖着被子: 角色网格有多少顶点戳出被子 (按上边到床尾 4 段)
        const p = quilt.poke(state.avatar.vrm.scene, state.avatar.vrm.humanoid);
        extra += `|poke=${p.n}/${p.seen}[${p.bins.join(',')}],cover=${quilt.cover.toFixed(2)},near=${p.near},mat=${p.mats}`;
      }
      samples.push(`${room.life.current?.id || '-'}:${room.poses.name || room.layer.mode}@${room.layer.char.x.toFixed(1)},${room.layer.char.z.toFixed(1)}/${state.fps}${extra}`);
    }
    const springs = state.avatar.vrm.springBoneManager.__realtime;
    post(`talk ${JSON.stringify({ room: true, avatar: state.avatar.info?.name, done: room.life.done, samples: samples.join(' '),
      needs: room.needs.summary(), fed_left: room.props.fed.length, stash: room.props.stash.length,
      furniture: room.mover.pose, candidates: room.mover.candidates().length, draws: room.cache.draws, why: room.cache.why,
      body: room.body.totals, body_log: room.body.log.slice(-12), body_parts: room.body.parts, liquid: room.liquid?.snapshot?.() ?? null, rigid: room.rigid?.snapshot?.() ?? null,
      rigid_perceive: room.rigid?.perceive?.({ take: false }) ?? null,
      perceive: room.liquid?.perceive?.({ take: false }) ?? null, feelings: room.body.feelings?.({ take: false }) ?? null,
      self_clip: room.body.selfStats, practice: room.practice.summary(), practice_history: room.practice.history.slice(-24),
      afford: room.afford?.summary?.() ?? null,  // 可供性: 她认识了哪些东西、每件事在什么情境下试过几次、觉得怎么样
      last_sleep: room.life.lastSleep || null, fatigue: +room.needs.fatigue.toFixed(2), notes_left: room.notesLeft,
      body_colliders: state.avatar.vrm.springBoneManager.__body, char: [+room.layer.char.x.toFixed(2), +room.layer.char.z.toFixed(2)],
      spring_steps_per_frame: springs ? +(springs.steps / Math.max(1, springs.frames)).toFixed(2) : null, fps: state.fps })}`);
    return;
  }
  if (PARAMS.has('thinktest')) {  // 她按服务端想的做 (think=秒 隔多久问一次): 每件事做之前的那句、怎么挑的, 到时间报
    const said = [], end = performance.now() + secs * 1000;
    let last = '';
    while (performance.now() < end) {
      await sleep(250);
      const text = roomStatus.textContent;
      if (text !== last && (text.startsWith('她想') || /^她(去|朝|走|坐|想)/.test(text))) said.push(text);
      last = text;
    }
    post(`talk ${JSON.stringify({ room: true, think: true, done: room.life.done, plans: room.life.plans, said: said.slice(-16),
      current: room.life.current?.id || null, source: room.life.source, connected: talk.connected })}`);
    return;
  }
  const timeline = [];  // 每秒: 房间重画了几次、她在干什么 (调镜头跟随用)
  for (let i = 0, last = room.cache.draws; i < Math.round(secs * 0.4); i++) {
    await sleep(1000);
    timeline.push(`${room.cache.draws - last}${room.layer.mode[0]}`);
    last = room.cache.draws;
  }
  const drawsBefore = room.cache.draws;
  room.view.pan(40, 0);  // 拖一下: 之后 10 秒不跟她
  const held = !room.view.following;
  const went = room.life.goDo('rest');  // 和点了床一样: 马上去床边坐 (坐着的话先站起来)
  await sleep(secs * 600);
  const springs = state.avatar.vrm.springBoneManager.__realtime;
  post(`talk ${JSON.stringify({ room: true, avatar: state.avatar.info?.name, done: room.life.done, draws: room.cache.draws,
    draws_before_pan: drawsBefore, timeline: timeline.join(' '), held_after_pan: held, go_do_rest: went, mode: room.layer.mode, why: room.cache.why,
    view: { target: room.view.target.toArray().map((x) => +x.toFixed(2)), recenter: room.view.recenter, following: room.view.following },
    life_current: room.life.current?.id, life_order: room.life.order, char: [+room.layer.char.x.toFixed(2), +room.layer.char.z.toFixed(2)],
    spring_steps_per_frame: springs ? +(springs.steps / Math.max(1, springs.frames)).toFixed(2) : null, fps: state.fps })}`);
}

/**
 * 看你在做什么的自检 (activity=demo): 不看真实的窗口, 用假的钟按宿主消息的格式演一遍, 记下她每一句说了什么、什么时候说的:
 * 打开 -> 玩 ELDEN RING (15 秒后说一句) -> 玩到 61 分钟 (提醒歇歇) -> 换到 QQ (离上一句不到 6 分钟, 不说) -> 走开 6 分钟又回来
 * ("回来啦") -> 看 B 站视频 -> 7 分钟后到了半夜 1 点 (劝早点睡). 也核对告诉服务端的摘要: 云端模型时不带标题.
 */
async function activityDemo() {
  let t = 0, hour = 15;
  const said = [];
  const sent = [];
  const r = new ActivityReactor({ say: (text, emotion) => said.push({ t, text, emotion }), busy: () => false,
    onChange: (s) => sent.push(s), now: () => t, clock: () => new Date(2026, 9, 24, hour, 5) });
  const run = (s) => { for (let i = 0; i < s; i++) { t += 1; r.tick(); } };
  const to = (category, app, title = '') => r.handle({ t: 'activity', on: true, category, app, title });
  r.handle({ t: 'activity-mode', on: true });
  to('game', 'ELDEN RING');
  run(20);
  run(61 * 60);
  to('chat', 'QQ');
  run(20);
  to('away', '');
  run(6 * 60);
  to('video', '哔哩哔哩', '【4K】猫咪合集');
  run(7 * 60);
  hour = 1;
  run(5);
  // 告诉服务端的: 本地模型带标题, 云端模型不带
  const states = [];
  const fake = { send: (m) => states.push(m), profile: { llm: { provider: 'local' } } };
  talk.reportState.call(Object.assign(fake, { activity: r.summary() }));
  fake.profile = { llm: { provider: 'deepseek' } };
  talk.reportState.call(fake);
  r.handle({ t: 'activity', on: false });
  post(`talk ${JSON.stringify({ activity: true, said, last_summary: sent.at(-1), states: states.map((m) => m.state) })}`);
}

/**
 * 趴着的自检 (lean=demo, 宿主带 --lean-after 2): 等宿主说趴下、趴好, 记下栏杆线、头和两只手在窗口里的高度;
 * 再依次"点"她四下 (直接调 poke, 不经过鼠标), 每下之间停 2.2 秒. 连拍 (--film) 用来看样子. 自检时宿主不挪窗口.
 */
async function leanSelftest() {
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const until = async (pred, ms) => {
    for (let t = 0; t < ms; t += 100) {
      if (pred()) return true;
      await sleep(100);
    }
    return pred();
  };
  const v = state.avatar, ctl = leanCtl();
  if (!ctl) {
    post(`talk ${JSON.stringify({ lean: false, error: '趴不了 (3D 没挂上行动层)' })}`);
    return;
  }
  const on = await until(() => ctl.on, 20000);
  const settled = await until(() => ctl.w > 0.995, 8000);
  await sleep(600);
  const frac = (p) => +((1 - p.project(v.camera).y) / 2).toFixed(4);
  const look = () => {
    const head = headScreen(v);
    const out = { rail: +(state.rail ?? -1).toFixed(4), head_y: head ? +(head[1] / stage.clientHeight).toFixed(4) : null, fps: state.fps };
    if (!LIVE2D) {  // 两只手应该在栏杆线上方一点 (叠在前臂上)
      const h = v.vrm.humanoid;
      out.hands_y = ['leftHand', 'rightHand'].map((n) => frac(h.getRawBoneNode(n).getWorldPosition(new THREE.Vector3())));
    }
    // 掩码里栏杆线以下还有几格是"有角色" (应该是 0: 下面的任务栏要点得到)
    const m = state.mask;
    if (m && state.rail) {
      let below = 0;
      for (let r = Math.ceil((state.rail * stage.clientHeight) / CELL) + 1; r < m.rows; r++) for (let c = 0; c < m.cols; c++) below += m.bits[r * m.cols + c];
      out.mask_below_rail = below;
    }
    return out;
  };
  const results = [{ what: '趴好', ...look(), rail_sent: +state.railSent.toFixed(4) }];
  for (let i = 0; i < 4; i++) {
    const kind = poke();
    await sleep(2200);
    results.push({ what: kind, ...look() });
  }
  post(`talk ${JSON.stringify({ lean: true, face: LIVE2D ? 'live2d' : 'vrm', avatar: v.info?.name || v.info?.display, on, settled, results,
    arms: ctl.debug })}`);
}

/**
 * 眼睛跟着鼠标的自检 (gaze=1): 不动真的光标 (宿主发来的光标位置这段时间不理), 按宿主消息的格式依次给头的右边、左边、上面、下面,
 * 每处 1.2 秒, 量眼睛和头转到哪了; 最后停下不动, 过了 FOLLOW_S 秒应该回到正前方.
 */
async function gazeSelftest() {
  const v = state.avatar;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const received = gaze.received;  // 宿主真的在发光标位置 (启动时至少发一次)
  gaze.test = true;
  const head = headScreen(v), W = stage.clientWidth, H = stage.clientHeight;
  const bone = (n) => { const b = v.rig?.readBone?.(n); return b ? [+b.yaw.toFixed(1), +b.pitch.toFixed(1)] : null; };
  const now = () => (LIVE2D ? v.gazeNow() : { eye_x: +v.engine.gazeCurrent.x.toFixed(3), eye_y: +v.engine.gazeCurrent.y.toFixed(3),
    eye_deg: bone('leftEye'), head_deg: bone('head') });
  const results = [];
  for (const [what, dx, dy] of [['右', 300, 0], ['左', -300, 0], ['上', 0, -300], ['下', 0, 250], ['右边很远', 1600, 0]]) {
    for (const end = performance.now() + 1200; performance.now() < end;) {
      onCursor((head[0] + dx) / W, (head[1] + dy) / H);
      await sleep(100);
    }
    results.push({ what, yaw: +gaze.yaw.toFixed(1), pitch: +gaze.pitch.toFixed(1), ...now(), fps: state.fps });
  }
  await sleep((FOLLOW_S + 1.5) * 1000);
  results.push({ what: '不动了', ...now(), fps: state.fps });
  gaze.test = false;
  post(`talk ${JSON.stringify({ gaze: true, face: LIVE2D ? 'live2d' : 'vrm', head: head.map(Math.round), host_cursor_msgs: received, results })}`);
}

/**
 * 动作自检 (页面地址带 ?skills=1 时, 在对话自检的位置做): 不经过服务端, 逐个在本地做桌宠声明的每个技能,
 * 量这段时间里手抬高多少、胸口和头转了多少度、防穿模层推开手臂几次; 最后静静待机一段作对照 (待机本身的晃动和推开次数).
 */
async function skillsSelftest() {
  const v = state.avatar, h = v.vrm.humanoid;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const chest = h.getRawBoneNode('chest') || h.getRawBoneNode('upperChest') || h.getRawBoneNode('spine');
  const head = h.getRawBoneNode('head');
  const hips = h.getRawBoneNode('hips'), feet = [h.getRawBoneNode('leftFoot'), h.getRawBoneNode('rightFoot')];
  const qa = new THREE.Quaternion(), qb = new THREE.Quaternion(), pa = new THREE.Vector3();
  const deg = (a, b) => (2 * Math.acos(Math.min(1, Math.abs(a.dot(b)))) * 180) / Math.PI;
  const snap = () => ({ hand: handHeight(v), chest: chest.getWorldQuaternion(new THREE.Quaternion()), head: head.getWorldQuaternion(new THREE.Quaternion()),
    hipsY: hips.getWorldPosition(new THREE.Vector3()).y });
  // 每个技能量: 手抬高、胸口和头转了多少度、髋部降了多少 (蹲下)、脚在地上滑了多远 (蹲下时脚要钉住)、防穿模层推了几次
  const watch = async (ms, rest) => {
    const pushes0 = v.contact?.stats?.pushes || 0;
    const by0 = { ...(v.contact?.stats?.by || {}) }, clip = v.loco?.idle?.name || null;  // 推在哪 ("胶囊:部位" -> 米) 和当时是哪段待机
    const feet0 = feet.map((f) => f.getWorldPosition(new THREE.Vector3()));
    let hand = 0, torso = 0, headDeg = 0, drop = 0, slide = 0;
    for (const t0 = performance.now(); performance.now() - t0 < ms;) {
      await sleep(40);
      hand = Math.max(hand, handHeight(v) - rest.hand);
      torso = Math.max(torso, deg(chest.getWorldQuaternion(qa), rest.chest));
      headDeg = Math.max(headDeg, deg(head.getWorldQuaternion(qb), rest.head));
      drop = Math.max(drop, rest.hipsY - hips.getWorldPosition(pa).y);
      feet.forEach((f, i) => { f.getWorldPosition(pa); slide = Math.max(slide, Math.hypot(pa.x - feet0[i].x, pa.z - feet0[i].z)); });
    }
    // push_mm: 每个"胶囊:部位"这一项里推出去的距离之和 (毫米); 除以 pushes 是平均每次推多深 (1-2 mm 以内是擦, 3 mm 以上要查)
    const push_mm = {};
    for (const [k, m] of Object.entries(v.contact?.stats?.by || {})) {
      const d = (m - (by0[k] || 0)) * 1000;
      if (d > 0.05) push_mm[k] = +d.toFixed(1);
    }
    return { hand_lift_m: +hand.toFixed(3), torso_deg: +torso.toFixed(1), head_deg: +headDeg.toFixed(1), hips_drop_m: +drop.toFixed(3),
      foot_slide_m: +slide.toFixed(3), pushes: (v.contact?.stats?.pushes || 0) - pushes0, push_mm, clip };
  };
  // 量之前先不跟鼠标: 头跟着光标转、转过头身体跟着转身, 各项的胸口、头、脚滑就成了"那会儿鼠标在哪" (用户正在用电脑)
  gaze.freeze = true;
  gaze.at = 0;
  // 也不转身、不换待机段 (web/locomotion.js 的开关): 换待机段、换重心时脚按新片段重新落脚, 斜着站的片段胸口转 10° 左右, 都会算进各项里
  if (v.loco) v.loco.noTurn = v.loco.holdIdle = true;
  talk._busyFor(1);
  await sleep(1500 + FOLLOW_S * 1000);
  const rest = snap();
  const results = [{ skill: '(待机对照)', ...(await watch(3200, rest)) }];
  const only = PARAMS.get('skills') === '1' ? null : PARAMS.get('skills').split(',');
  for (const skill of ['wave', 'raise', 'point', 'shrug', 'think', 'bow', 'nod', 'shake', 'squat', ...Object.keys(EMOTES)]
    .filter((s) => talk.skills.includes(s) && (!only || only.includes(s)))) {
    talk._busyFor(5);
    talk.runActs([{ do: skill, side: EMOTES[skill] && EMOTES[skill].hands === 'both' ? 'B' : 'R', to: null, amp: 1, spd: 1, at: 0 }], 'act');
    results.push({ skill, ...(await watch(skill === 'squat' ? 4200 : EMOTES[skill] ? (EMOTES[skill].dur + 0.6) * 1000 : 3200, rest)) });  // 蹲下一共约 3.7 秒
    await sleep(1300);
  }
  if (v.loco) v.loco.noTurn = v.loco.holdIdle = false;  // 最后这段要量的就是换待机段、换重心, 开关关回去
  gaze.freeze = false;
  const idle = await watch(20000, rest);  // 静静待机 20 秒: 换重心、环顾时的推开次数
  post(`talk ${JSON.stringify({ connected: talk.connected, skills: results, idle_20s: idle,
    idle_actions: Object.entries(v.loco?.idleDB?.groups?.actions || {}).filter(([, c]) => c.length).map(([k]) => k),
    idle_clips: v.loco?.idleDB?.groups?.idle?.length, avoided: [...(v.loco?.avoided || [])] })}`);
}

/**
 * 语音自检: 开麦克风, 等出现一轮来源是语音的对话并说完, 关麦克风. 宿主用假麦克风 (--fake-mic) 放一段合成的测试语音,
 * 不碰真实麦克风. expected 是那段语音的原文, 用来对照识别结果.
 */
async function voiceSelftest(expected, until) {
  const before = talk.turn;
  const t0 = performance.now();
  talk.listeningAt = 0;
  let error = null;
  try {
    await talk.startMic();
  } catch (err) {
    error = err.message;
  }
  const isVoice = () => talk.turn && talk.turn !== before && talk.turn.source === 'voice';
  const done = !error && await until(() => isVoice() && talk.turn.doneAt, 25000);
  const t = isVoice() ? talk.turn : null;
  talk.stopMic();
  const plain = (s) => (s || '').replace(/[\s，。！？、,.!?]/g, '');
  return { expected, error, done: !!done, recognized: t?.text ?? null, exact: !!t && plain(t.text) === plain(expected),
    op: t?.op, acts: t?.acts, reply: (t?.reply || '').slice(0, 60),
    listen_ms: talk.listeningAt ? Math.round(talk.listeningAt - t0) : null,
    turn_ms: t?.startedAt ? Math.round(t.startedAt - t0) : null, hand_lift_m: +(t?.handLift || 0).toFixed(3) };
}

async function main() {
  if (LIVE2D) {
    await main2d();
    return;
  }
  if (PARAMS.get('avatar') === 'none') {
    // 空页面基线 (自检对比用): 不加载角色, 只量 WebView2 自己占多少
    post(`ready ${JSON.stringify({ avatar: null, dpr: devicePixelRatio, css: [stage.clientWidth, stage.clientHeight] })}`);
    post(`mask 1 1 -1 ${btoa('\0')}`);
    return;
  }
  let list;
  try {
    list = await (await fetch('/api/avatars')).json();
  } catch (err) {
    log('取不到角色列表', err.message);
    return;
  }
  if (!list.default) { log('服务端没有 VRM 模型'); return; }
  const t0 = performance.now();
  // avatar=wendy.vrm: 自检时换一个角色 (默认用服务端说的默认角色)
  const pick = list.items?.some((i) => i.name === PARAMS.get('avatar')) ? PARAMS.get('avatar') : list.default;
  const { avatar: v, report } = await loadLeanAvatar(stage, `/avatars/${pick}`, OPTS);
  report.load_ms = Math.round(performance.now() - t0);
  if (PARAMS.get('check') === 'morph') report.morph_check_px = morphCheck(v);
  report.springs = !!realtimeSprings(v);  // 头发、裙子的物理按真实时间、拆成小步算 (见 springs.js)
  report.fast_matrix = !!fastMatrices(v.vrm?.scene);  // 角色骨架每帧强制重算 9-12 次: 没变的节点不重算, 结果相同 (见 fast-matrix.js)
  report.body_colliders = bodyColliders(v);  // 仅无有效导入分组的头发/裙链使用后备碰撞体
  report.eye_limits = v.rig?.state().capabilities.eyeOutputLimits ?? null;  // 只读，保留模型范围
  report.idle = 'procedural';
  if (ROOM) {  // 小屋: 行动层接房间 (不挂桌面的待机身体; 走路、坐下、待机都由它管)
    try {
      if (PARAMS.get('roomreset') === '1') {
        const sceneId = PARAMS.get('roomscene') || DEFAULT_ROOM_SCENE;
        const personal = ['pet.room.needs', 'pet.room.practice', 'pet.room.body'];
        const spatial = ['pet.room.layout', 'pet.room.liquid', 'pet.room.rigid.poses', 'pet.room.known', 'pet.room.world'];
        for (const k of [...personal, ...spatial.map((key) => roomStorageKey(sceneId, key))]) try { localStorage.removeItem(k); } catch { /* 无所谓 */ }
      }
      state.room = await enterRoom(v, motion, {
        status: (text) => { roomStatus.textContent = text; },
        busy: () => talk.busy || shell.active,
        log: (text) => log(text),
        onPoke: (what) => log(`小屋: 点了 ${what}`),
        think: (payload, ms) => talk.roomThink(payload, ms),
        // 睡觉整理记忆、日记本、拖进来的文件 (服务端没回时 roomAsk 给 null, 小屋按兜底做)
        server: {
          ask: (type, payload, replies, ms) => talk.roomAsk(type, payload, replies, ms),
          reset: () => talk.send({ type: 'reset' }),
          tell: (type, payload) => talk.send({ type, ...payload }),
          say: sayInTurn,  // 便利贴: 一条一条发, 等上一条说完; 没连上返回 false (贴回去)
          connected: () => talk.connected,
          mic: (on) => {  // 睡着时先关掉麦克风 (说的话不叫醒她), 醒了再打开
            if (!on && talk.mic?.active) { state.micResume = true; setMic(false); }
            else if (on && state.micResume) { state.micResume = false; setMic(true); }
          },
        },
        say: (text) => { shell.notice(text); refreshMask(); },
        memory: (card) => showMemoryCard(card),
        chip: (text) => { roomStatus.classList.toggle('working', !!text); roomStatus.dataset.chip = text || ''; },
        watching: () => activity.on && WATCH.has(activity.cur?.category),
      });
      const roomBar = document.querySelector('.room-bar');
      if (roomBar) roomBar.textContent = `${state.room.scene.name || '生活小屋'} · 实验室`;  // 小屋是动作模型的研发场 (2026-11-01: 日常陪伴用陪伴模式)
      sendRoomCapabilities();
      report.idle = 'room';
      // 卡片左上角的心情牌: 点开看需求、投喂; 心情摘要放进 state 告诉服务端 (只有心情和数字, 没有外面来的文字)
      const NAMES = { treeR: '树式 (右)', treeL: '树式 (左)', fold: '前屈', bend: '侧弯', crouch: '蹲下拿东西', crouchLow: '蹲下够地上', lie: '躺下', 'sit@reading_chair': '坐阅读椅',
        'sit@desk_chair': '坐电竞椅', 'sit@bed': '坐床边', 'sit@bed_side': '坐床沿' };
      // 钱包 (服务端的账本 wm/ledger.py): 她想事情、说话花的钱, 和冰箱里的饮料零食放在一起看. 每分钟取一次; 服务端旧了 (404) 就不显示
      let wallet = '';
      const loadWallet = async () => {
        try {
          const r = await fetch('/api/billing');
          if (!r.ok) return;
          const d = await r.json(), b = d.balance || {}, t = d.today || {};
          wallet = [b.total != null ? `钱包 ¥${b.total.toFixed(2)}${b.low ? ' (不多了)' : ''}` : '', `今天花了 ¥${(t.cost || 0).toFixed(2)}`,
            t.hit_rate != null ? `缓存命中 ${Math.round(t.hit_rate * 100)}%` : '', d.now ? `${d.now.tier === 'peak' ? '高峰' : '空闲'}时段` : '']
            .filter(Boolean).join(' · ');
        } catch { /* 服务端没开 */ }
      };
      loadWallet();
      setInterval(loadWallet, 60000);
      const extra = () => {  // 心情牌里: 钱包、受伤了没有、练得最多的三样 (次数和平时的分数, 1 是满分)
        const b = state.room.body, out = wallet ? [wallet] : [];
        if (b.injured) out.push(`受伤: ${b.injury.part}, 还有 ${Math.ceil((b.injury.until - performance.now()) / 60000)} 分钟`);
        const n = state.room.needs;  // 困倦: 这次醒着以后聊了多少 (睡一觉整理完清零)
        out.push(`这次醒着聊了 ${(n.load / 1000).toFixed(1)}k / ${LOAD_MAX / 1000}k 字${n.fatigue >= 0.7 ? ' · 困了' : ''}`);
        const top = Object.entries(state.room.practice.summary()).filter(([, k]) => k.n).sort((x, y) => y[1].n - x[1].n).slice(0, 3);
        if (top.length) out.push(`练习: ${top.map(([n, k]) => `${NAMES[n] || n} ${k.n} 次 ${k.avg >= 0.8 ? '很稳' : k.avg >= 0.3 ? '还行' : '还不熟'}`).join(' · ')}`);
        const life = state.room.life, src = life.current && { plan: '她自己想的', rule: '按习惯挑的' }[life.source];
        if (src) out.push(`在做的这件: ${src}`);  // 服务端想的 (room_plan) 还是本机规则; 点了、投喂的不写
        return out;
      };
      addEmotes(v);  // 小屋的行动层 (enterRoom 里挂到 v.loco) 也做得出表情动作
      const panel = needsPanel(state.room.needs, { feed: (kind) => { state.room.feed(kind); wake(); }, extra });
      document.body.append(panel.el);
      let moodSent = '';
      const sendMood = () => {
        const m = state.room.needs.summary(), key = `${m.mood}|${m.needs.join()}|${Math.round(m.score / 10)}|${Math.round(m.fatigue * 10)}`;
        if (key !== moodSent) { moodSent = key; talk.setMood(m); }
      };
      state.room.needs.onChange(sendMood);
      sendMood();
    } catch (err) {
      log('小屋加载失败', err.stack || err.message);
      shell.notice(`小屋打不开: ${err.message}`);
    }
  } else if (IDLE === 'staystill') {
    try {
      const body = await attachDeskBody(v, motion, () => state.ch || motion.out, { calm: PARAMS.get('calm') !== '0' });
      if (body) {
        state.body = body;  // 趴着的姿势在它的 lean 里 (见 desk-body.js 的 leaner)
        report.idle = body.idle ? 'staystill' : 'matching';
        report.body_ms = body.load_ms;
        talk.addBodySkill('squat', (act) => body.squat(act));  // 蹲下只有挂了行动层的身体做得出 (见 desk-body.js)
        addEmotes(v);
      }
    } catch (err) {
      log('行动层加载失败, 用程序待机', err.message);
    }
  }
  state.avatar = v;
  state.report = report;
  if (state.room) {  // 对话栏挪到卡片上方的空处 (和趴着时同一套排法); 侧菜单放在卡片右边那条空处, 按钮太多放不下时缩小
    const layout = () => {
      const card = stage.getBoundingClientRect();
      shell.setRail(card.top / innerHeight, card.top - 10);
      const n = shell.el.dock.querySelectorAll('button').length, gap = 6;
      const size = Math.max(24, Math.min(36, Math.floor((card.height - 8 - gap * (n - 1)) / n)));
      shell.placeDock(card.top + Math.max(4, (card.height - (n * size + (n - 1) * gap)) / 2), size, gap);
    };
    layout();
    addEventListener('resize', layout);
  }
  log('角色已加载', v.info?.name, `像素比 ${devicePixelRatio}`, JSON.stringify(report));
  if (!OPTS.render) {  // 只加载不渲染 (自检对比用): 掩码为空
    post(`ready ${JSON.stringify({ avatar: v.info?.name, dpr: devicePixelRatio, lean: report })}`);
    post(`mask 1 1 -1 ${btoa('\0')}`);
    return;
  }
  talk.attach(v);
  // 自检时不出声: mock 的 TTS 只发元音占位音 ("啊哦噫诶"), 不能放到用户的扬声器上. 口型按音频数据算, 静音不影响检查
  // mute=1: 本机接口的自检也一样
  if (SELFTEST_MUTE) talk.muted = true;
  if (PARAMS.get('ws') !== '0') talk.connect();
  if (PARAMS.has('skills')) {  // skills=1 全部技能, skills=squat,bow 只做这几个
    new Promise((resolve) => { state.talkGo = resolve; }).then(skillsSelftest);
  } else if (PARAMS.has('gaze')) {
    new Promise((resolve) => { state.talkGo = resolve; }).then(gazeSelftest);
  } else if (PARAMS.has('lean')) {
    new Promise((resolve) => { state.talkGo = resolve; }).then(leanSelftest);
  } else if (PARAMS.has('activity')) {
    new Promise((resolve) => { state.talkGo = resolve; }).then(activityDemo);
  } else if (PARAMS.has('roomtest')) {
    new Promise((resolve) => { state.talkGo = resolve; }).then(roomSelftest);
  } else if (PARAMS.get('ui') === 'demo') {
    new Promise((resolve) => { state.talkGo = resolve; }).then(uiDemo);
  } else if (PARAMS.has('talk')) {
    talkSelftest(PARAMS.get('talk').split('|').filter(Boolean));
  }
}

/**
 * 2D 门面: 加载 Live2D 模型, 能力声明只报这个模型做得到的 (Eustia 的手是一张交握的图: 点头、摇头、思考、鞠躬),
 * 其余 (对话、对话栏、命中掩码、本机接口、工具调用) 和 3D 一样.
 */
async function main2d() {
  let v;
  try {
    v = await loadLive2DAvatar(stage, LIVE2D_MODEL, { pixelRatio: OPTS.pr || 1.5, bubbleRoom: BUBBLE_ROOM, scale: petFit() });
  } catch (err) {
    log('Live2D 模型加载失败', err.message);
    shell.notice(`2D 模型加载不了: ${err.message}`);
    return;
  }
  talk.skills = [...v.skills];
  state.avatar = v;
  state.report = v.report;
  shell.setScale(petFit());
  log('Live2D 模型已加载', v.info.display, JSON.stringify(v.report));
  talk.attach(v);
  if (SELFTEST_MUTE) talk.muted = true;
  if (PARAMS.get('ws') !== '0') talk.connect();
  if (PARAMS.has('skills')) new Promise((resolve) => { state.talkGo = resolve; }).then(live2dSelftest);
  else if (PARAMS.has('gaze')) new Promise((resolve) => { state.talkGo = resolve; }).then(gazeSelftest);
  else if (PARAMS.has('lean')) new Promise((resolve) => { state.talkGo = resolve; }).then(leanSelftest);
  else if (PARAMS.has('exprtest')) new Promise((resolve) => { state.talkGo = resolve; }).then(exprSelftest);
  else if (PARAMS.has('idletest')) new Promise((resolve) => { state.talkGo = resolve; }).then(idleSelftest);
  else if (PARAMS.has('scaletest')) new Promise((resolve) => { state.talkGo = resolve; }).then(scaleSelftest);
  else if (PARAMS.get('ui') === 'demo') new Promise((resolve) => { state.talkGo = resolve; }).then(uiDemo);
  else if (PARAMS.has('talk')) talkSelftest(PARAMS.get('talk').split('|').filter(Boolean));
}

/**
 * 闲时状态机的页面自检 (face=live2d&idletest=1&idlefx_scale=60, 时间轴压缩 60 倍: 走神 2 秒、犯困 8 秒、睡 30 秒): 什么都不做 38 秒,
 * 每秒记一次状态和这一秒里头、视线、嘴的最大幅度; 然后"碰她一下" (director.interact), 再记 4 秒醒来的过程. 不经过服务端.
 */
async function idleSelftest() {
  const v = state.avatar;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  if (!director) { post(`talk ${JSON.stringify({ idletest: true, error: 'idlefx 关着' })}`); return; }
  await sleep(500);
  v.takePeaks();
  const rows = [];
  const sample = (what) => rows.push({ what, state: director.out.state, au: { eyeClose: +director.out.au.eyeClose.toFixed(2) }, head: { pitch: +director.out.head.pitch.toFixed(1), roll: +director.out.head.roll.toFixed(1) }, ...v.takePeaks() });
  for (let i = 0; i < 38; i++) { await sleep(1000); sample(`闲 ${i + 1}s`); }
  director.interact(dnow(), 'selftest');
  for (let i = 0; i < 4; i++) { await sleep(1000); sample(`碰了之后 ${i + 1}s`); }
  const order = rows.map((r) => r.state).filter((x, i, a) => x !== a[i - 1]);
  post(`talk ${JSON.stringify({ idletest: true, face: 'live2d', scale: Number(PARAMS.get('idlefx_scale')) || 1, order, rows })}`);
}

/**
 * 表情连续化自检 (face=live2d&exprtest=1&idlefx=0, 加 expr=cubism 是改前的路径): 不经过服务端, 8 种情绪两两切换 (56 对),
 * 每对先停稳再切, 逐帧读表情参数画完以后的最终值, 量相邻帧最大变化占该参数全幅的比例 (改前最坏 1.0: 切到平静是硬切).
 * 不含眼睛开合 (眨眼会乱入); 帧率是页面真实跑出来的 (talk._busyFor 保持 30 帧). 结果: talk {exprtest, mode, worst, worst_pair, fps_seen, pairs}.
 */
async function exprSelftest() {
  const v = state.avatar;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const labels = ['neutral', 'happy', 'excited', 'calm', 'sad', 'shy', 'angry', 'surprised'];
  // 不含头 (AngleY/Z) 和眼球 (EyeBall) 和眼睛开合: 它们本来就在动 (头的摆动、跟着光标、眨眼), 会混进"表情的跳变"里
  const names = ['ParamBrowLY', 'ParamBrowRY', 'ParamBrowLX', 'ParamBrowRX',
    'ParamBrowLAngle', 'ParamBrowRAngle', 'ParamBrowLForm', 'ParamBrowRForm', 'ParamMouthForm', 'ParamMouthOpenY', 'ParamCheek'];
  let frames = [];
  const ranges = v.watchParams(names, (vals, dt) => { vals.dt = dt; frames.push(vals); });  // dt 是门面这一帧实际用的 (模拟时间), 不是回调的时钟
  const pairs = [];
  let worst = 0, worstPair = '', total = 0, spent = 0, worstSpeed = 0, worstSpeedPair = '', maxDt = 0;
  for (const a of labels) for (const b of labels) {
    if (a === b) continue;
    talk._busyFor(6);
    v.setEmotion(a);
    await sleep(1500);
    frames = [];
    await sleep(100);
    const t0 = performance.now();
    v.setEmotion(b);
    await sleep(1300);
    spent += performance.now() - t0;
    total += frames.length;
    let mx = 0, speed = 0;
    for (let i = 1; i < frames.length; i++) {
      const dt = frames[i].dt;
      maxDt = Math.max(maxDt, dt);
      for (let k = 0; k < names.length; k++) {
        const d = Math.abs(frames[i][k] - frames[i - 1][k]) / ranges[k];
        mx = Math.max(mx, d);
        if (dt > 0.005) speed = Math.max(speed, d / dt);  // 全幅比例每秒: 页面偶尔掉帧 (dt 变大) 时相邻帧变化会跟着大, 速度不会
      }
    }
    pairs.push([`${a}>${b}`, +mx.toFixed(3), +speed.toFixed(2)]);
    if (mx > worst) { worst = mx; worstPair = `${a}>${b}`; }
    if (speed > worstSpeed) { worstSpeed = speed; worstSpeedPair = `${a}>${b}`; }
  }
  v.watchParams(null);
  post(`talk ${JSON.stringify({ exprtest: true, face: 'live2d', mode: v.report.expression_mode, worst: +worst.toFixed(3), worst_pair: worstPair,
    worst_speed: +worstSpeed.toFixed(2), worst_speed_pair: worstSpeedPair, over_speed_6: pairs.filter((x) => x[2] > 6).length, max_dt_ms: Math.round(maxDt * 1000),
    over_0_2: pairs.filter((x) => x[1] > 0.2).length, fps_seen: +(total / (spent / 1000)).toFixed(1), pairs, expr_stats: v.exprStats(), expression: v.report.expression })}`);
}

/**
 * 2D 的动作自检 (face=live2d&skills=1): 不经过服务端, 依次做每个表情、每个动作、一段假的口型,
 * 量这段时间里各参数的最大幅度 (头的角度、身体前倾、嘴的开合). 连拍 (--film) 时用来看样子.
 */
async function live2dSelftest() {
  const v = state.avatar;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  talk._busyFor(2);
  await sleep(1500);
  v.takePeaks();
  await sleep(2500);
  const results = [{ what: '(待机对照)', ...v.takePeaks() }];
  for (const name of v.expressionNames) {
    talk._busyFor(3);
    v.setExpression(name);
    await sleep(1800);
    results.push({ what: `表情 ${name}`, ...v.takePeaks() });
  }
  v.setExpression(null);
  for (const skill of talk.skills) {
    talk._busyFor(4);
    talk.runActs([{ do: skill, side: 'R', to: null, amp: 1, spd: 1, at: 0 }], 'act');
    await sleep(2600);
    results.push({ what: skill, ...v.takePeaks() });
    await sleep(800);
  }
  // 口型: 不经过服务端, 直接给几帧假的口形权重
  talk._busyFor(3);
  const realFrame = talk.frame.bind(talk);
  const start = performance.now();
  talk.frame = (t, dt) => {
    const f = realFrame(t, dt), k = (performance.now() - start) / 1000;
    return { ...f, viseme: { aa: 0.8 * Math.abs(Math.sin(k * 7)), ih: 0, ou: 0, ee: 0.3, oh: 0, level: 0.7 * Math.abs(Math.sin(k * 7)) } };
  };
  await sleep(2000);
  talk.frame = realFrame;
  results.push({ what: '口型 (假的音频)', ...v.takePeaks() });
  await sleep(500);
  post(`talk ${JSON.stringify({ connected: talk.connected, face: 'live2d', skills: talk.skills, results, drawn: v.report })}`);
}

/**
 * 桌宠大小的页面自检 (face=live2d&scaletest=1, 建议带 idlefx=0; 不经过服务端; 开发版宿主, 见 backups/pet-scale/run_page_scale.ps1).
 * scaletest=1 整套 (结束时清掉存的大小); scaletest=keep 同 1 但把大小 (0.6) 留在存储里, 给 scaletest=2 (重开后记得住) 用, 2 读完清掉.
 * 全程用真的 Ctrl+滚轮事件 (派发到画布) 改大小, 往宿主发的消息截下来 (postTap) 不转发.
 * 结果: talk {scaletest, fails: [], rows: [{k, r, 模型包围框, 命中, 对话栏、侧菜单、思考气泡位置...}], wheel: {...}, perf: {...}}.
 */
async function scaleSelftest() {
  const v = state.avatar;
  const mode = PARAMS.get('scaletest');
  if (mode === 'mip') { await mipSelftest(); return; }
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const W = stage.clientWidth, H = stage.clientHeight;
  const canvas = v.renderer.domElement;
  const out = { scaletest: mode, face: 'live2d', win: [W, H], dpr: devicePixelRatio, max: petScaleMax(innerHeight), canvas0: [canvas.width, canvas.height], fails: [], rows: [] };
  const fail = (why) => out.fails.push(why);
  const med = (a) => { const b = [...a].sort((x, y) => x - y); return b.length ? b[Math.floor(b.length / 2)] : null; };
  const untilFrames = async (n) => {
    const from = state.rendered, m0 = state.mask;
    for (let i = 0; i < 400 && !(state.rendered - from >= n && state.mask !== m0); i++) await sleep(10);
  };
  const box = () => {  // 掩码里有角色的格子的包围框 (CSS 像素)
    const m = state.mask;
    let c0 = 1e9, c1 = -1, r0 = 1e9, r1 = -1;
    for (let r = 0; r < m.rows; r++) for (let c = 0; c < m.cols; c++) if (m.bits[r * m.cols + c]) {
      if (c < c0) c0 = c; if (c > c1) c1 = c; if (r < r0) r0 = r; if (r > r1) r1 = r;
    }
    return { left: c0 * CELL, right: (c1 + 1) * CELL, top: r0 * CELL, bottom: (r1 + 1) * CELL };
  };
  const setK = async (k) => { applyPetScale(k, { save: true }); await sleep(260); await untilFrames(2); };  // 要存: 统计那一拍的兜底读会按存的值走, 不存的话 2 秒后会被改回去
  const bit = (x, y) => { const m = state.mask; const c = Math.floor(x / CELL), r = Math.floor(y / CELL); return c >= 0 && r >= 0 && c < m.cols && r < m.rows && m.bits[r * m.cols + c] === 1; };
  const sent = [];
  const statsLog = [];
  const leanPosts = [];
  postTap = (m) => { if (m === 'drag') { sent.push(m); return true; } if (typeof m === 'string') { if (m.startsWith('stats ')) statsLog.push(m); else if (m.startsWith('lean ') && m !== 'lean none') leanPosts.push(m); } return false; };  // 只截 drag (其余照常发给宿主, 掩码才不会过期)
  const wheel = (target, over = canvas, ctrl = true) => {  // 真的 WheelEvent; 返回 defaultPrevented
    const ev = new WheelEvent('wheel', { deltaY: target, ctrlKey: ctrl, bubbles: true, cancelable: true });
    over.dispatchEvent(ev);
    return ev.defaultPrevented;
  };
  const dyFor = (from, to) => Math.max(-300, Math.min(300, -Math.log((to >= 1 ? 1.05 : to) / from) / PET_SCALE_WHEEL));  // 目标 1 (最大) 往上多滚一点, 靠夹范围落在整 1

  // ---- 第二遍: 重开后记得住 ----
  if (mode === '2') {
    await untilFrames(2);
    await sleep(500);
    const b = box(), p = v.placement();
    out.stored = (() => { try { return localStorage.getItem(PET_SCALE_KEY); } catch { return null; } })();
    out.k = petScale.k; out.r = p.r; out.box = b; out.box_h = b.bottom - b.top; out.report_scale = v.report.scale;
    out.ready_scale = state.report?.scale;
    if (Math.abs(petScale.k - 0.6) > 0.002) fail(`重开后 k=${petScale.k}, 应该是 0.6`);
    if (Math.abs(p.r - 0.6) > 0.002) fail(`重开后 r=${p.r}`);
    try { localStorage.removeItem(PET_SCALE_KEY); } catch { /* 无所谓 */ }
    postTap = null;
    post(`talk ${JSON.stringify(out)}`);
    return;
  }

  try { localStorage.removeItem(PET_SCALE_KEY); } catch { /* 无所谓 */ }
  if (SCALE_URL !== null) fail('scaletest 不能带 petscale 参数');
  await setK(1);
  await sleep(300);
  await untilFrames(2);

  // ---- 1. 逐档: 滚轮 -> 位置 / 命中 / 对话栏 / 侧菜单 / 气泡 ----
  const stack = shell.el.island.parentElement, think = shell.el.think, dock = shell.el.dock;
  let base = null;
  for (const target of [1, 0.75, 0.5, 0.35, 0.75, 1]) {
    // 滚到目标 (每次事件最多 300 像素, 可能要几下); 第一下计时 = 从派发到画面和掩码都更新
    const t0 = performance.now();
    const from = state.rendered, m0 = state.mask;
    let prevented = true;
    for (let i = 0; i < 8 && Math.abs(petScale.k - target) > 0.004; i++) prevented = wheel(dyFor(petScale.k, target)) && prevented;
    await untilFrames(2);
    const latency = Math.round(performance.now() - t0);
    await sleep(350);
    await untilFrames(2);
    const row = { target, k: petScale.k, r: v.placement().r, latency_ms: latency, frames: state.rendered - from, mask_refreshed: state.mask !== m0, prevented };
    if (Math.abs(petScale.k - target) > 0.004) fail(`滚轮到不了 ${target}: k=${petScale.k}`);
    // 画布不变
    row.canvas = [canvas.width, canvas.height];
    if (canvas.width !== out.canvas0[0] || canvas.height !== out.canvas0[1]) fail(`画布尺寸变了 ${row.canvas}`);
    // 模型包围框 (三次取中位数, 她在待机晃动)
    const bs = [];
    for (let i = 0; i < 3; i++) { bs.push(box()); await sleep(300); }
    const b = { left: med(bs.map((x) => x.left)), right: med(bs.map((x) => x.right)), top: med(bs.map((x) => x.top)), bottom: med(bs.map((x) => x.bottom)) };
    row.box = b; row.box_h = b.bottom - b.top; row.box_w = b.right - b.left;
    const p = v.placement();
    row.place = { s: +p.s.toFixed(5), railY: +p.railY.toFixed(5), faceY: +p.faceY.toFixed(5) };
    if (target === 1 && base === null) base = { b, railFrac: v.lean.railFrac(), dockTop: null };
    const kk = row.r;
    // 缩放是绕"脚底中点"缩: 底边不动、中线不动, 高度宽度按 r 缩 (相对 r=1 那一遍量的; 容差 2 格)
    const tol = 2 * CELL;
    if (base) {
      const cx1 = (base.b.left + base.b.right) / 2, cx = (b.left + b.right) / 2;
      const expTop = H - (H - base.b.top) * kk, expBottom = H - (H - base.b.bottom) * kk;
      row.expect = { top: +expTop.toFixed(1), bottom: +expBottom.toFixed(1), cx: +cx1.toFixed(1) };
      if (Math.abs(b.top - expTop) > tol) fail(`r=${kk} 顶边 ${b.top} 应该是 ${expTop.toFixed(1)}±${tol}`);
      if (Math.abs(b.bottom - expBottom) > tol) fail(`r=${kk} 底边(脚底) ${b.bottom} 应该是 ${expBottom.toFixed(1)}±${tol}`);
      if (Math.abs(cx - cx1) > tol) fail(`r=${kk} 中线 ${cx} 应该是 ${cx1.toFixed(1)}±${tol}`);
    }
    // 脚底：掩码最下面一行 (宿主据此站任务栏) 在所有档位都贴着窗口底边
    row.foot_row = state.lastFoot; row.rows = state.mask.rows;
    if (state.lastFoot < state.mask.rows - 3) fail(`r=${kk} 脚底行 ${state.lastFoot}/${state.mask.rows}: 没贴底`);
    // 命中: 头中心、身体中心、脚底中心都在掩码里; 包围框外 40 像素不在
    const head = headScreen(v);
    row.head = head.map((x) => +x.toFixed(1));
    const footRow = state.lastFoot, mk = state.mask;  // 脚点: 掩码最下面一行里最左边的有角色格子 (正中间可能是两脚之间的空)
    let footCol = -1;
    for (let c = 0; c < mk.cols && footCol < 0; c++) if (mk.bits[footRow * mk.cols + c]) footCol = c;
    const body = [(b.left + b.right) / 2, (b.top + b.bottom) / 2], feet = [footCol * CELL + CELL / 2, footRow * CELL + CELL / 2];
    row.hit = { head: bit(head[0], head[1]), body: bit(body[0], body[1]), feet: bit(feet[0], feet[1]),
      outside_left: bit(Math.max(0, b.left - 40), body[1]), outside_above: bit(body[0], Math.max(0, b.top - 40)) };
    if (!row.hit.head || !row.hit.body || !row.hit.feet) fail(`r=${kk} 头/身/脚没命中 ${JSON.stringify(row.hit)}`);
    if (row.hit.outside_left || (b.top > 60 && row.hit.outside_above)) fail(`r=${kk} 包围框外也命中了`);
    if (!(head[1] >= b.top - 6 && head[1] <= b.top + 0.3 * (b.bottom - b.top))) fail(`r=${kk} 头的位置 ${head[1]} 不在模型头部 (${b.top}..${b.top + 0.3 * (b.bottom - b.top)})`);
    // 点她: 往头中心派真的 pointerdown, 应截到恰好一条 drag; 往框外派, 0 条
    const down = (x, y) => { sent.length = 0; canvas.dispatchEvent(new PointerEvent('pointerdown', { button: 0, clientX: x, clientY: y, bubbles: true, pointerId: 7 })); return sent.filter((m) => m === 'drag').length; };
    row.drag = { head: down(head[0], head[1]), feet: down(feet[0], feet[1]), outside: down(Math.max(1, b.left - 40), body[1]) };
    if (row.drag.head !== 1 || row.drag.feet !== 1 || row.drag.outside !== 0) fail(`r=${kk} 点击拖动 ${JSON.stringify(row.drag)}`);
    // 趴着时的栏杆线 (窗口比例): 应该和 r=1 时的相比按 r 压向脚底
    row.rail_frac = +v.lean.railFrac().toFixed(5);
    if (base) {
      const expRail = 1 - (1 - base.railFrac) * kk;
      if (Math.abs(row.rail_frac - expRail) > 0.002) fail(`r=${kk} 栏杆线 ${row.rail_frac} 应该是 ${expRail.toFixed(4)}`);
    }
    // 对话栏: 底边在窗口高 - 0.11 * r * 窗口高; 侧菜单整条在窗口里 (r=1 时顶边 = 36%); 思考气泡贴头
    shell.notice('自检: 对话栏位置');
    dock.classList.add('open');
    shell._set('thinking', '自检: 想');
    shell.setHead(head[0], head[1]);
    await sleep(350);
    const sr = stack.getBoundingClientRect(), dr = dock.getBoundingClientRect(), ir = shell.el.island.getBoundingClientRect(), tr = think.getBoundingClientRect();
    row.shell = { stack_bottom: +sr.bottom.toFixed(1), island: [+ir.top.toFixed(1), +ir.bottom.toFixed(1)], dock: [+dr.top.toFixed(1), +dr.bottom.toFixed(1)],
      think: [+tr.left.toFixed(1), +tr.top.toFixed(1), +tr.width.toFixed(1), +tr.height.toFixed(1)] };
    const expStackBottom = H - 0.11 * kk * H;
    if (Math.abs(sr.bottom - expStackBottom) > 2) fail(`r=${kk} 对话栏底边 ${sr.bottom.toFixed(1)} 应该是 ${expStackBottom.toFixed(1)}`);
    if (ir.top < 0 || ir.bottom > H) fail(`r=${kk} 对话栏出窗口 ${ir.top}..${ir.bottom}`);
    if (dr.top < -0.5 || dr.bottom > H + 0.5) fail(`r=${kk} 侧菜单出窗口 ${dr.top}..${dr.bottom}`);
    if (kk === 1) {
      row.dock_default = Math.abs(dr.top - 0.36 * H) < 1;
      if (!row.dock_default) fail(`r=1 侧菜单顶边 ${dr.top} 应该是 36% = ${0.36 * H}`);
    }
    if (!(Math.abs(tr.right - (head[0] - 44)) < 3 && Math.abs(tr.top - (head[1] - 30)) < 4 + 40)) fail(`r=${kk} 思考气泡离头太远 ${JSON.stringify(row.shell.think)}`);
    shell._set('idle', '');
    dock.classList.remove('open');
    stack.style.removeProperty('transition');
    out.rows.push(row);
  }

  // ---- 1b. 趴着 (自检里宿主不会让她趴, 这里直接让门面趴): 栏杆线、画布裁剪、掩码、对话栏顶边、侧菜单 ----
  const ctl = leanCtl();
  out.lean = [];
  if (!ctl) fail('没有趴着的控制');
  else for (const r of [1, 0.5]) {
    await setK(r);
    leanPosts.length = 0;
    ctl.set(true);
    for (let i = 0; i < 400 && ctl.w < 0.995; i++) await sleep(20);
    await sleep(600); await untilFrames(2);
    const rail = state.rail, expRail = v.lean.railFrac();
    const m = state.mask;  // 先量掩码 (只有她自己: 对话栏、侧菜单本来就算进掩码); 栏杆线以下应该一格都没有 (下面的任务栏要点得到)
    let below = 0;
    for (let rr = Math.ceil((rail * H) / CELL) + 1; rr < m.rows; rr++) for (let c = 0; c < m.cols; c++) below += m.bits[rr * m.cols + c];
    shell.notice('自检: 趴着');
    dock.classList.add('open');
    await sleep(400);
    const sr = stack.getBoundingClientRect(), dr = dock.getBoundingClientRect(), ir = shell.el.island.getBoundingClientRect();
    const rootStyle = shell.el.dock.parentElement.style;
    const row = { r, w: +ctl.w.toFixed(3), rail: rail === null ? null : +rail.toFixed(4), railFrac: +expRail.toFixed(4), clip: canvas.style.clipPath, stack_class: stack.className,
      stack_top: +sr.top.toFixed(1), island: [+ir.top.toFixed(1), +ir.bottom.toFixed(1)], dock: [+dr.top.toFixed(1), +dr.bottom.toFixed(1)], history: rootStyle.getPropertyValue('--lean-history'),
      head_y: +headScreen(v)[1].toFixed(1), mask_below_rail: below, posted: leanPosts[leanPosts.length - 1] || null };
    out.lean.push(row);
    if (rail === null || Math.abs(rail - expRail) > 0.002) fail(`趴着 r=${r} 栏杆线 ${rail} 应该是 ${expRail.toFixed(4)}`);
    if (!stack.classList.contains('lean')) fail(`趴着 r=${r} 对话栏没挪到头顶`);
    const expTop = H - (H - 8) * r;
    if (Math.abs(sr.top - expTop) > 1.5) fail(`趴着 r=${r} 对话栏顶边 ${sr.top.toFixed(1)} 应该是 ${expTop.toFixed(1)}`);
    if (ir.top < 0 || ir.bottom > rail * H) fail(`趴着 r=${r} 对话栏 ${ir.top.toFixed(1)}..${ir.bottom.toFixed(1)} 越出栏杆线 ${(rail * H).toFixed(1)}`);
    // 侧菜单整条 (约 444 像素) 放在栏杆线以上; 栏杆线太高放不下时 (r=1: 栏杆线在 40%) 贴着窗口顶边, 和改前一样
    if (dr.top < -0.5 || (dr.bottom > rail * H + 0.5 && dr.top > 0.02 * H + 0.5)) fail(`趴着 r=${r} 侧菜单 ${dr.top.toFixed(1)}..${dr.bottom.toFixed(1)} 栏杆线 ${(rail * H).toFixed(1)}`);
    if (below !== 0) fail(`趴着 r=${r} 栏杆线以下掩码还有 ${below} 格`);
    const clipPct = Number(/([0-9.]+)%/.exec(canvas.style.clipPath || '')?.[1]);
    if (!(Math.abs(clipPct - (1 - rail) * 100) < 0.1)) fail(`趴着 r=${r} 画布裁剪 ${canvas.style.clipPath} 应该是 ${((1 - rail) * 100).toFixed(2)}%`);
    if (!row.posted || Math.abs(Number(row.posted.slice(5)) - rail) > 0.002) fail(`趴着 r=${r} 发给宿主的栏杆线 ${row.posted} 和页面的 ${rail} 对不上`);
    // 站起来: 一切回到站着的样子
    ctl.set(false);
    for (let i = 0; i < 400 && (ctl.w > 0.001 || state.rail !== null); i++) await sleep(20);
    await sleep(300);
    const after = { rail: state.rail, lean_class: stack.classList.contains('lean'), clip: canvas.style.clipPath, dock_top: rootStyle.getPropertyValue('--dock-top'), lean_history: rootStyle.getPropertyValue('--lean-history') };
    row.after = after;
    if (after.rail !== null || after.lean_class || after.clip || after.lean_history) fail(`站起来后没收干净 r=${r} ${JSON.stringify(after)}`);
    if (r === 1 && after.dock_top) fail(`站起来后 r=1 不该留 --dock-top ${after.dock_top}`);
    if (r < 1 && !after.dock_top) fail(`站起来后 r=${r} 侧菜单位置没恢复`);
    shell._set('idle', '');
    dock.classList.remove('open');
  }

  // ---- 2. 滚轮的边界和整页缩放并存 ----
  const w = {};
  await setK(1);
  w.at_max_prevented = wheel(-100);           // 已经最大了再往大滚: 不变, 也要挡掉整页缩放
  w.at_max_k = petScale.k;
  await setK(0.35);
  w.at_min_prevented = wheel(100);
  w.at_min_k = petScale.k;
  await setK(0.8);
  w.outside_prevented = wheel(-100, document.body);  // 不在她身上: ui-preferences.js 的 window 监听挡整页缩放, 她的大小不变
  w.outside_k = petScale.k;
  w.plain_prevented = wheel(-100, canvas, false);    // 没按 Ctrl: 谁也不挡 (后面的窗口照常能滚)
  w.plain_k = petScale.k;
  w.on_her_prevented = wheel(-100);                  // 在她身上按 Ctrl: 挡掉整页缩放, 她放大
  w.on_her_k = petScale.k;
  if (!w.at_max_prevented || w.at_max_k !== 1) fail(`上限处 ${JSON.stringify(w)}`);
  if (!w.at_min_prevented || w.at_min_k !== 0.35) fail(`下限处 ${JSON.stringify(w)}`);
  if (!w.outside_prevented || w.outside_k !== 0.8) fail(`身外 Ctrl+滚轮: 整页缩放没挡住或她动了 ${JSON.stringify(w)}`);
  if (w.plain_prevented || w.plain_k !== 0.8) fail(`没按 Ctrl 的滚轮被拦了 ${JSON.stringify(w)}`);
  if (!w.on_her_prevented || !(w.on_her_k > 0.8)) fail(`她身上的 Ctrl+滚轮没生效 ${JSON.stringify(w)}`);
  out.wheel = w;

  // ---- 3. 记得住: 滚到 0.6, 等存盘 ----
  await setK(0.8);
  for (let i = 0; i < 8 && Math.abs(petScale.k - 0.6) > 0.004; i++) wheel(dyFor(petScale.k, 0.6));
  await sleep(500);
  out.stored = (() => { try { return localStorage.getItem(PET_SCALE_KEY); } catch { return null; } })();
  if (!(Math.abs(Number(out.stored) - petScale.k) < 0.0006) || Math.abs(petScale.k - 0.6) > 0.004) fail(`存的是 ${out.stored}, 当前 ${petScale.k}`);
  // 设置页那条路: 直接写存储 + storage 事件 (同一个文档里 storage 事件不会自己来, 手动派发); 桌宠应该跟上
  writePetScale(0.5);
  window.dispatchEvent(new StorageEvent('storage', { key: PET_SCALE_KEY, newValue: '0.5' }));
  await untilFrames(2);
  out.after_storage = { k: petScale.k, r: v.placement().r };
  if (Math.abs(petScale.k - 0.5) > 0.002) fail(`storage 事件后 k=${petScale.k}, 应该是 0.5`);
  writePetScale(0.7);  // 没有 storage 事件时: 统计那一拍的兜底读 (这里直接调, 免得等 2 秒)
  syncPetScale();
  out.after_poll = { k: petScale.k };
  if (Math.abs(petScale.k - 0.7) > 0.002) fail(`兜底读之后 k=${petScale.k}, 应该是 0.7`);
  if (mode === 'keep') { applyPetScale(0.6); writePetScale(0.6); } else { try { localStorage.removeItem(PET_SCALE_KEY); } catch { /* 无所谓 */ } }

  // ---- 4. 每帧耗时: r=1 和 r=0.5 交替 (每轮换先后), 说话状态 (保持 30 帧) 下取页面自己每 2 秒报的 stats (每帧 JS 耗时: 动作 + 渲染提交 + 掩码) ----
  const perf = { r1: [], r05: [] }, fpsSeen = { r1: [], r05: [] };
  const nextStats = async () => { const n = statsLog.length; for (let i = 0; i < 500 && statsLog.length === n; i++) await sleep(10); return statsLog[statsLog.length - 1] || ''; };
  for (let round = 0; round < 6; round++) {
    for (const r of (round % 2 ? [0.5, 1] : [1, 0.5])) {
      await setK(r);
      talk._busyFor(8);
      await nextStats();                       // 切换时跨着的那一窗不要
      const m = /fps=([0-9.]+) ms=([0-9.]+)/.exec(await nextStats());
      if (m) { perf[r === 1 ? 'r1' : 'r05'].push(+m[2]); fpsSeen[r === 1 ? 'r1' : 'r05'].push(+m[1]); }
    }
  }
  out.perf = { r1: perf.r1, r05: perf.r05, fps_r1: fpsSeen.r1, fps_r05: fpsSeen.r05, median_r1: med(perf.r1), median_r05: med(perf.r05), min_r1: Math.min(...perf.r1), min_r05: Math.min(...perf.r05) };
  if (mode === 'keep') { await setK(0.6); writePetScale(0.6); } else { await setK(1); try { localStorage.removeItem(PET_SCALE_KEY); } catch { /* 无所谓 */ } }
  postTap = null;
  out.ok = out.fails.length === 0;
  post(`talk ${JSON.stringify(out)}`);
}

/**
 * 缩小后纹理采样的 A/B (face=live2d&scaletest=mip&idlefx=0): 停掉页面自己的帧循环, 手动画 (dt 极小, 姿势几乎冻住), 把画布拷到 2D 画布里比:
 * 参考 = r=1、不用缩小版贴图 (现状) 的那一帧, 按 r 绕脚底中点缩小 (浏览器高质量缩小); 候选 = 在 r 下直接画, 贴图用第 0..3 级缩小版.
 * 误差 = 区域内 (模型包围框 / 脸 = 包围框最上 22%) RGBA 平均绝对差 (0..255). 结果: talk {mipcompare, rows: [{r, lod, mad_all, mad_face}]}.
 */
async function mipSelftest() {
  const v = state.avatar, canvas = v.renderer.domElement;
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const cw = canvas.width, ch = canvas.height;
  const mk = () => { const c = document.createElement('canvas'); c.width = cw; c.height = ch; return c; };
  setFps(0);
  await sleep(400);
  const snap = (r, lod) => {
    v.setScale(r); v.setTextureLod(lod === 'auto' ? null : lod);
    v.apply({}, {}, 0.0001, { roll: 0, pitch: 0 });
    const c = mk();
    c.getContext('2d').drawImage(canvas, 0, 0);  // 同一个任务里读, 画布内容还在
    return c;
  };
  const pix = (c) => c.getContext('2d').getImageData(0, 0, cw, ch).data;
  const bboxOf = (d) => {
    let x0 = cw, x1 = -1, y0 = ch, y1 = -1;
    for (let y = 0; y < ch; y++) for (let x = 0; x < cw; x++) if (d[(y * cw + x) * 4 + 3] > 24) { if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
    return { x0, x1, y0, y1 };
  };
  const mad = (a, b, box, y0, y1) => {
    let sum = 0, n = 0;
    for (let y = Math.max(0, y0); y <= Math.min(ch - 1, y1); y++) for (let x = Math.max(0, box.x0 - 8); x <= Math.min(cw - 1, box.x1 + 8); x++) {
      const i = (y * cw + x) * 4;
      if (a[i + 3] < 9 && b[i + 3] < 9) continue;
      sum += Math.abs(a[i] - b[i]) + Math.abs(a[i + 1] - b[i + 1]) + Math.abs(a[i + 2] - b[i + 2]) + Math.abs(a[i + 3] - b[i + 3]);
      n++;
    }
    return n ? +(sum / n / 4).toFixed(3) : null;
  };
  const refCanvas = snap(1, 0), refPix = pix(refCanvas), refBox = bboxOf(refPix);
  const fx = cw / 2, fy = ch * 0.9975;
  const rows = [], lods = [0, 1, 2, 3, 'auto'], montages = {};
  for (const r of [1, 0.75, 0.5, 0.35]) {
    let ref = refCanvas, rp = refPix;
    if (r < 1) {  // 参考图缩到 r
      ref = mk();
      const g = ref.getContext('2d');
      g.imageSmoothingEnabled = true; g.imageSmoothingQuality = 'high';
      g.setTransform(r, 0, 0, r, fx * (1 - r), fy * (1 - r));
      g.drawImage(refCanvas, 0, 0);
      rp = pix(ref);
    }
    const box = { x0: Math.round(fx - (fx - refBox.x0) * r), x1: Math.round(fx + (refBox.x1 - fx) * r), y0: Math.round(fy - (fy - refBox.y0) * r), y1: Math.round(fy - (fy - refBox.y1) * r) };
    const faceY1 = box.y0 + Math.round((box.y1 - box.y0) * 0.22);
    const crops = [ref];
    for (const lod of lods) {
      const cv = snap(r, lod);
      if (lod === 0 || lod === 1 || lod === 2 || lod === 'auto') crops.push(cv);
      const cp = pix(cv);
      rows.push({ r, lod, mad_all: mad(cp, rp, box, box.y0, box.y1), mad_face: mad(cp, rp, box, box.y0, faceY1) });
    }
    if (r < 1) {  // 脸部放大 3 倍 (最近邻) 并排: 参考 | 不用缩小版 (现状) | 第 1 级 | 第 2 级 | 自动
      const w = Math.max(40, box.x1 - box.x0), h = Math.max(30, faceY1 - box.y0), k = 3;
      const m = document.createElement('canvas');
      m.width = w * k * crops.length; m.height = h * k;
      const g = m.getContext('2d');
      g.imageSmoothingEnabled = false;
      crops.forEach((cv, i) => g.drawImage(cv, box.x0, box.y0, w, h, i * w * k, 0, w * k, h * k));
      montages[r] = m.toDataURL('image/png');
    }
  }
  v.setScale(1); v.setTextureLod(null);
  setFps(wantedFps());
  post(`talk ${JSON.stringify({ mipcompare: true, face: 'live2d', canvas: [cw, ch], ref_box: refBox, rows, montages })}`);
}

schedule();
main().catch((err) => log('错误', err.stack || err.message));
