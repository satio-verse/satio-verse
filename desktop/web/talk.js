// 桌宠的对话通道: 连服务的 /ws, 协议和网页完全一样 (README "WebSocket 协议"), 服务端不用改.
// 下行的意图、情绪、回复、音频、面部 (A2F)、韵律都在这里处理; 上行发文字、能力声明、打点 (服务端据此算延迟).
// 逻辑照 web/app.js 搬, 去掉仪表盘、房间、行动层和 HAST-SEC (桌宠不用非商业的运动层).
import { AudioOut, MicIn } from '/static/audio.js';
import { ZERO } from '/static/lipsync.js';
import { SpeechPerformer } from '/static/performance.js';

const HEADER_BYTES = 20;  // <4sIIII: magic, turn, seg, sample_rate, seq
// 桌面上没有房间: 走动、看某样东西、转身、坐下站起、拿放都做不了, 不声明, 服务端会把这类要求改判成"做不到"
// 抬腿 (leg) 只有 HAST-SEC 做得出来 (程序动作没有这个技能), 桌宠不声明
export const DESKTOP_SKILLS = ['wave', 'raise', 'point', 'nod', 'shake', 'shrug', 'think', 'bow'];
const SPATIAL = new Set(['go', 'look', 'turn', 'sit', 'stand', 'pick', 'put']);
const nowS = () => performance.now() / 1000;
// 说完话、等回复时的"思考": 只抬眼歪头, 不抬手托腮 (每轮都抬手看着像乱动, 托腮的手也容易穿过头发)
const THINK_HEAD = { groups: { head: (t) => ({ hp: -7, hy: 9, hr: -7 + 1.5 * Math.sin(2 * Math.PI * 0.5 * t) }) }, dur: Infinity, tag: 'backchannel' };
// 无界面的对话 (本机接口的文字模式) 没有身体: 动作系统的调用一律什么也不做
const NO_MOTION = new Proxy({}, { get: () => () => {} });
// 默认人设预设: 服务端自己的人设、本地模型、没有 skill 和 MCP. 这种不发 profile, 服务端照旧
const isDefaultProfile = (p) => !p.persona?.prompt && (p.llm?.provider ?? 'local') === 'local' && !p.skills?.length && !p.mcp?.length;

export class Talk {
  /**
   * motion: MotionSystem; hooks: { onTurn(turn, text, source), onReply(turn, text), onServerDone(turn), onDone(turn),
   *   onInterrupted(turn, reason), onError(turn, message), onStatus(status), log(...parts) }
   * headless: 不出声、不动 (本机接口的文字模式另开的一条对话): 不建音频, 能力声明为空 (服务端据此不安排动作)
   */
  constructor(motion, hooks = {}, { headless = false } = {}) {
    this.headless = headless;
    this.motion = motion || NO_MOTION;
    this.hooks = hooks;
    this.performer = new SpeechPerformer();
    this.avatar = null;
    this.ws = null;
    this.ctx = null;
    this.out = null;
    this.offset = 0;
    this.pings = [];
    this.pingId = 0;
    this.dropUpTo = 0;
    this.emo = 'neutral';
    this.status = null;
    this.segText = new Map();
    this.segSeen = new Set();
    this.turn = null;      // 正在进行的一轮: { id, text, reply, sentAt, intentAt, audioAt, ... } (自检用)
    this.busyUntil = 0;    // 这之前算"在说话/在动", 页面据此升帧
    this.mic = null;
    // 服务端要的麦克风处理 (hello 的 mic: 它能去掉外放回声时, 让浏览器别降噪、别自动调音量; 见 web/audio.js 的 micConstraints).
    // undefined: 还没连上过; null: 照浏览器默认
    this.micWant = undefined;
    // 半双工: 角色说话时上行静音帧 (端点检测照常推进), 不把自己的声音当成用户插话. 桌宠多半是外放, 默认开.
    this.halfDuplex = true;
    this.asrRate = 16000;
    // 这个身体能做的技能 (能力声明), 和其中程序动作做不了、要身体自己做的 (pet.js 挂上 StayStill 之后加 squat)
    this.skills = [...DESKTOP_SKILLS];
    this.bodyActs = {};
    this.roomWait = new Map();  // 小屋问服务端"接下来做什么"还没回的: id -> resolve
    this.roomSeq = 0;
    this.retries = 0;  // 断开以后连了几次没连上 (先快后慢)
  }

  /** 身体多了一个技能 (fn 收服务端的意图项 {do, amp, spd, at}, 返回要做多少秒): 加进能力声明, 意图里的这个动作交给 fn. */
  addBodySkill(name, fn) {
    this.bodyActs[name] = fn;
    if (!this.skills.includes(name)) this.skills.push(name);
    this.reportCapabilities();
  }

  attach(avatar) {
    this.avatar = avatar;
    this.performer.attach(avatar);
    this.reportCapabilities();
  }

  connect() {
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
    ws.binaryType = 'arraybuffer';
    this.ws = ws;
    ws.onopen = () => {
      this.retries = 0;
      this.pings = [];
      for (let i = 0; i < 8; i++) setTimeout(() => this.ping(), i * 60);
      this.reportCapabilities();
      // 她在用户的电脑桌面上, 不在房间里: 服务端换桌宠的提示词 (没有家具, 不会说"我去坐沙发"). 文字模式也是同一个她
      this.reportState();
      // 无界面的对话不放声音: 让服务端这条连接不合成语音 (也不算面部和韵律), 只下发文字和意图
      if (this.headless) this.send({ type: 'output', audio: false });
      this.profileSent = false;  // 新连接: 服务端那边是默认设置, 也没有工具
      this.toolsSent = false;
      this.reportProfile();
      this.reportTools();
      this.hooks.onOpen?.();  // 新连接: QQ 设置也要重发 (qq.js)
    };
    ws.onclose = () => {
      this.status = null;
      this.hooks.onStatus?.(null);
      // 先快后慢: 头 10 次隔 0.3 秒 (服务端重启好了马上连上), 之后隔 1.5 秒
      setTimeout(() => this.connect(), ++this.retries <= 10 ? 300 : 1500);
    };
    ws.onmessage = (e) => {
      if (e.data instanceof ArrayBuffer) this.onAudio(e.data);
      else this.handle(JSON.parse(e.data));
    };
    if (!this.pingTimer) this.pingTimer = setInterval(() => this.ping(), 10000);
  }

  get connected() { return this.ws?.readyState === WebSocket.OPEN && !!this.status; }

  send(obj) {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(obj));
  }

  mark(turn, name, perfMs = performance.now()) {
    this.send({ type: 'mark', turn, name, t: perfMs + this.offset });
  }

  ping() { this.send({ type: 'ping', id: ++this.pingId, t_client: performance.now() }); }

  /** 这个身体此刻能做哪些技能 (服务端据此去掉做不到的动作, 全做不到时让角色坦白). */
  reportCapabilities() {
    this.send({ type: 'capabilities', skills: this.headless ? [] : this.skills });
  }

  /**
   * 当前人设预设 (宿主从操作面板的设置里给): 人设文字、模型 (云端的带 key)、skill、MCP 服务器. 连上时和改了时发给服务端 (本机的 /ws).
   * 一直是默认预设时不发; 从别的预设换回默认时发一次默认的, 让服务端换回来.
   */
  setProfile(profile) {
    this.profile = profile;
    this.reportProfile();
    if (this.activity) this.reportState();  // 换了本地 / 云端模型: 带不带窗口标题跟着变
  }

  /** 你在做什么 (宿主"看你在做什么"开着时, 见 activity.js 的 summary; 关了是 null): 放进 state 告诉服务端. */
  setActivity(activity) {
    this.activity = activity;
    this.reportState();
  }

  /** 小屋里她的心情 (room-needs.js 的 summary): state 里带 mood (固定值)、needs (缺的几项)、mood_score, 都是本机算的. */
  setMood(m) {
    this.mood = m ? { mood: m.mood, needs: m.needs, mood_score: m.score, fatigue: m.fatigue ?? 0, asleep: m.mood === 'asleep' } : null;
    this.reportState();
  }

  /**
   * state 是完整快照, 每次都带上 place. activity: 你在做什么 (类别、程序、标题、做了几分钟).
   * 当前预设用云端模型时不带窗口标题: 服务端会把 state 写进提示词发给云端. 标题是外面来的文字, 服务端只能当数据看 (不当指令).
   */
  reportState() {
    const state = { place: 'desktop' };
    if (this.activity) {
      const provider = this.profile?.llm?.provider;
      state.activity = { ...this.activity };
      if (provider && provider !== 'local') delete state.activity.title;
    }
    if (this.mood) Object.assign(state, this.mood);
    this.send({ type: 'state', state });
  }

  /** 服务端的格式 (根目录 README 的 profile): persona 是人设文字, model 是云端模型 (本地模型不写), skills 是名字和描述. */
  reportProfile() {
    const p = this.profile;
    if (!p) return;
    const plain = isDefaultProfile(p);
    if (plain && !this.profileSent) return;
    this.profileSent = !plain;
    if (plain) {  // 换回默认: 空的 profile 就是默认
      this.send({ type: 'profile' });
      return;
    }
    const llm = p.llm || {};
    const model = llm.provider && llm.provider !== 'local'
      ? { provider: llm.provider, base_url: llm.base_url || undefined, api_key: llm.api_key || undefined, model: llm.model } : undefined;
    this.send({ type: 'profile', persona: p.persona?.prompt || '', model, skills: p.skills || [] });
  }

  /** 交给云端模型的工具 (宿主按当前预设列的 MCP 工具和 load_skill): 连上时和变了时发. 空表只在发过工具之后才发 (清掉). */
  setTools(tools) {
    this.tools = tools || [];
    this.reportTools();
  }

  reportTools() {
    if (!this.tools || (!this.tools.length && !this.toolsSent)) return;
    this.toolsSent = this.tools.length > 0;
    this.send({ type: 'tools', tools: this.tools });
  }

  /**
   * 小屋里问服务端她接下来做什么 (上行 room_think, 字段见 room-acts.js 的 Life._sense; 都是编号、固定取值和数字).
   * 返回 Promise: 下行 room_plan (ok: false 时带 reason), ms 毫秒内没回是 null. 没连上时不发, 直接返回 null (不是 Promise, 不算问过).
   * 页面拿不到可用的就按本机规则挑.
   */
  roomThink(payload, ms = 4000) {
    if (!this.connected) return null;
    const id = ++this.roomSeq;
    return new Promise((resolve) => {
      const timer = setTimeout(() => { this.roomWait.delete(id); resolve(null); }, ms);
      this.roomWait.set(id, (msg) => { clearTimeout(timer); resolve(msg); });
      this.send({ type: 'room_think', id, ...payload });
    });
  }

  /**
   * 小屋要服务端做的几件事 (上行 sleep / diary / doc_drop, 协议见 docs/roadmap/desktop-pet.md 的"睡觉整理记忆、日记本"):
   * 发上行 (带 id), 等同一个 id、类型在 replies 里的下行; ms 毫秒没回 (服务端还不支持、断开了) 给 null, 页面按兜底做.
   */
  roomAsk(type, payload, replies, ms) {
    if (!this.connected) return Promise.resolve(null);
    const id = `room-${++this.roomSeq}`;
    this.askWait ??= new Map();
    return new Promise((resolve) => {
      const timer = setTimeout(() => { this.askWait.delete(id); resolve(null); }, ms);
      this.askWait.set(id, { replies, done: (m) => { clearTimeout(timer); resolve(m); } });
      this.send({ type, id, ...payload });
    });
  }

  /** 宿主或页面要求的音频输出; 宿主已经允许不经用户手势自动播放. */
  async ensureAudio() {
    if (!this.ctx) {
      this.ctx = new AudioContext({ latencyHint: 'interactive' });
      this.out = new AudioOut(this.ctx);
      if (this.muted) this.out.gain.gain.value = 0;
    }
    if (this.ctx.state !== 'running') await this.ctx.resume();
  }

  /** extra 挂在这一轮上 (本机接口用它认出自己发的那一轮). extra.task: 渠道任务单的号, 服务端不会把这一轮当成主人的指挥重新分类. */
  async say(text, extra = {}) {
    if (!this.headless) await this.ensureAudio();
    this.turn = { text, sentAt: performance.now(), ...extra };
    this.send({ type: 'text', text, ...(typeof extra.task === 'string' ? { task: extra.task } : {}) });
  }

  interrupt() { this.send({ type: 'interrupt' }); }

  /** 开麦克风: 16 kHz int16 帧直接上行 (协议的二进制上行). 服务端没开 ASR 时不开. 失败时抛错 (没权限、没设备). */
  async startMic() {
    if (this.mic?.active) return;
    if (this.status && !this.status.asr?.ok) throw new Error('服务端没开语音识别 (asr.engine=none)');
    await this.ensureAudio();
    this.mic = new MicIn(this.ctx, (pcm) => {
      if (this.ws?.readyState !== WebSocket.OPEN) return;
      const muted = this.halfDuplex && this.out?.playing;
      this.ws.send(muted ? new Int16Array(pcm.length).buffer : pcm.buffer);
    });
    try {
      await this.mic.start(this.asrRate, this.micWant || null);
    } catch (err) {
      this.mic = null;
      throw err;
    }
    this.reportMic();
  }

  /** 麦克风实际用上的处理报给服务端 (浏览器没照要的给时, 服务端照实知道; 自动增益关了它才自己补音量). */
  reportMic() {
    if (this.mic?.active) this.send({ type: 'mic_settings', settings: this.mic.settings, wanted: this.mic.processing });
  }

  /** 新连接 (hello) 要的麦克风处理和开着的这个不一样 (服务端换了配置、刚能去掉外放回声): 重开一次麦克风; 一样就只重报一遍. */
  async _syncMic() {
    if (!this.mic?.active) return;
    if (JSON.stringify(this.mic.processing || null) === JSON.stringify(this.micWant || null)) {
      this.reportMic();
      return;
    }
    this.stopMic();
    try {
      await this.startMic();
    } catch (err) {
      console.warn('按服务端的要求重开麦克风没成:', err);
    }
  }

  stopMic() {
    this.mic?.stop();
    this.mic = null;
  }

  get micOn() { return !!this.mic?.active; }

  /** 说话、做动作期间 (以及之后 1.5 秒) 算忙, 页面升到 30 帧. */
  get busy() { return nowS() < this.busyUntil || !!this.out?.playing; }
  _busyFor(seconds) { this.busyUntil = Math.max(this.busyUntil, nowS() + seconds); }

  /** 每帧: 口型、A2F 面部、说话表演的颈部偏移. */
  frame(t, dt) {
    const viseme = this.out ? this.out.viseme() : ZERO;
    const arkit = this.out ? this.out.faceAt() : null;
    this.performer.update(this.out ? this.out.heardTime() : 0, t, dt);
    if (this.turn && this.out?.playing) this.turn.maxLevel = Math.max(this.turn.maxLevel || 0, viseme.level || 0);
    return { viseme, arkit, neck: this.performer.neck };
  }

  onPong(msg) {
    const now = performance.now();
    const rtt = now - msg.t_client;
    this.pings.push({ rtt, offset: msg.t_server - (msg.t_client + rtt / 2) });
    if (this.pings.length > 12) this.pings.shift();
    this.offset = this.pings.reduce((a, b) => (b.rtt < a.rtt ? b : a)).offset;
  }

  /**
   * 连上了一条新连接 (收到 hello): 服务端每条连接是新开的会话, 轮号从 1 重新数, 上一条连接按轮号记的都清掉
   * (2026-11-08 陪看第四次真机试: 重启前第 9–11 轮被打断过, dropUpTo 停在 11, 重启后她的第 1–3 轮全被当成旧轮扔了 ——
   * 没显示、没出声, audio_end 照样报了). 上一条连接那一轮的声音还在放就让它放完, 但"放完了"现在就交代掉 (新会话马上会再用
   * 同一个轮号); 放声音也从头排 (新的第 1 轮可能正好和旧的最后一轮同号, 不能接着旧的排). this.turn 不用动: 下一轮 turn_start 换掉.
   */
  _newConnection() {
    this.dropUpTo = 0;
    this.segText.clear();
    this.segSeen.clear();
    this.out?.forgetTurn();  // 见 web/audio.js
  }

  handle(msg) {
    const vrm = this.avatar;
    const turn = this.turn;
    switch (msg.type) {
      case 'hello':
        this._newConnection();
        this.status = msg.status;
        this.asrRate = msg.asr_sample_rate || 16000;
        this.micWant = msg.mic || null;
        this._syncMic();
        this.hooks.onStatus?.(msg.status);
        this.hooks.onHello?.(msg);  // boot: 这次启动的情况 (重启过、退回过、学会了什么), 打盹的她据此醒来
        break;
      // 服务端要重启 / 停了 (看守进程先告诉它的): 小屋里她去睡一会儿, 这期间你的话变便利贴, 连上 (hello) 再醒
      case 'server_restart': this.hooks.onServerRestart?.(msg); break;
      case 'room_action': this.hooks.onRoomAction?.(msg); break;
      case 'room_action_cancel': this.hooks.onRoomActionCancel?.(msg); break;
      case 'room_gen': this.hooks.onRoomGen?.(msg); break;  // 你在设置里删了她的经历、重置了小窝学习 (删除代次, 见 room-gen.js)
      case 'pong': this.onPong(msg); break;
      case 'asr_partial': this.hooks.onPartial?.(msg.text); break;
      case 'listening':
        this.motion.backchannel('listen', nowS());
        this.performer.setListening(true, nowS());
        this.listeningAt = performance.now();
        this.hooks.onListening?.();
        this._busyFor(2);
        break;
      case 'listening_end':  // 那一段声音没认出话、或是外放的回声 (服务端 wm/echo.py): 不算你在说话了
        this.performer.setListening(false, nowS());
        this.hooks.onListeningEnd?.();
        break;
      case 'turn_start':
        this.performer.setListening(false, nowS());
        this.segText.clear();
        this.segSeen.clear();
        if (turn && turn.id === undefined && msg.text === turn.text) turn.id = msg.turn;
        else this.turn = { id: msg.turn, text: msg.text, sentAt: performance.now() };
        Object.assign(this.turn, { reply: '', source: msg.source, startedAt: performance.now() });
        this.hooks.onTurn?.(msg.turn, msg.text, msg.source);
        this._busyFor(3);
        break;
      case 'backchannel':
        if (msg.motion === 'think') {
          this.motion.play(THINK_HEAD, nowS());
          this.performer.thinking();
          this.hooks.onThinking?.();  // 闲时状态机 / 对话反应 (web/idle-director.js): 视线移开、歪头
        } else {
          this.motion.backchannel(msg.motion, nowS());
        }
        this.mark(msg.turn, 'client_backchannel');
        break;
      case 'intent':
        if (msg.turn <= this.dropUpTo) break;
        if (msg.uncertain) { this.performer.thinking(); this.hooks.onThinking?.(); }
        this.runActs(msg.acts, msg.op);
        this.mark(msg.turn, 'client_intent');
        if (turn?.id === msg.turn) Object.assign(turn, { op: msg.op, acts: msg.acts.map((a) => a.do), body: msg.body, intentAt: performance.now() });
        this._busyFor(3);
        break;
      case 'emotion':
        if (msg.turn <= this.dropUpTo) break;
        this.motion.setEmotion(msg.vad, nowS());
        vrm?.setEmotion(msg.emo);
        this.hooks.onEmotion?.(msg);  // 早于第一声音频到: 开口前的吸气 (idle-director.js)
        vrm?.loco?.setMood?.(msg.emo);  // 待机体态跟着情绪变 (害羞交叠双手、难过含胸、开心歪头、生气叉腰; web/locomotion.js 的 MOOD 表, 90 秒回平静)
        this.performer.setArousal(msg.vad[1]);
        this.emo = msg.emo;
        if (turn?.id === msg.turn) turn.emo = msg.emo;
        break;
      case 'reply_delta':
        if (msg.turn <= this.dropUpTo) break;
        if (turn?.id === msg.turn) turn.reply += msg.text;
        this.hooks.onReply?.(msg.turn, msg.text);
        break;
      case 'amend':
        if (turn?.id === msg.turn) turn.amend = msg.op;
        break;
      case 'audio_seg': this.segText.set(`${msg.turn}:${msg.seg}`, msg.text); break;
      case 'face':
        if (msg.turn > this.dropUpTo && this.out?.addFace(msg.turn, msg) && turn?.id === msg.turn) turn.faceFrames = (turn.faceFrames || 0) + msg.n;
        break;
      case 'prosody': {
        if (msg.turn <= this.dropUpTo || !this.out || msg.turn !== this.out.turn) break;
        const events = msg.events.map((e) => ({ ...e, t: this.out.segTime(msg.seg, e.t) })).filter((e) => e.t !== null);
        this.performer.addProsody(events);
        if (turn?.id === msg.turn) turn.prosody = (turn.prosody || 0) + events.length;
        break;
      }
      case 'audio_end': {
        const ws = this.ws;  // 放完时要是已经换了一条连接: 那边是新会话, 同一个轮号是另一轮, 这个打点不报
        const done = (endPerf) => {
          if (this.ws === ws) this.mark(msg.turn, 'client_audio_end', endPerf);
          if (this.turn?.id === msg.turn) this.turn.doneAt = performance.now();
          this._busyFor(1.5);
          this.hooks.onDone?.(msg.turn);
        };
        if (this.out) {
          if (this.out.turn === msg.turn && this.out.started) this.performer.endTurn(this.out.nextTime);
          this.out.endTurn(msg.turn, done);
        } else done(performance.now());
        break;
      }
      case 'turn_server_done': this.hooks.onServerDone?.(msg.turn); break;  // 回复的文字都发完了 (声音可能还在放)
      // 云端模型要调工具 / 服务端不等这个调用了 (超时、这一轮被打断): 交给页面 (再交给宿主, 见 desktop/host/src/tools.h)
      case 'tool_call': this.hooks.onToolCall?.(msg); this._busyFor(3); break;
      case 'tool_cancel': this.hooks.onToolCancel?.(msg); break;
      case 'profile_done':  // 人设预设生没生效 (key 已经打码)
        this.profileStatus = msg;
        this.hooks.onProfileDone?.(msg);
        break;
      case 'tools_done': this.toolsStatus = msg; break;
      case 'room_plan': {  // 小屋"接下来做什么"的回答 (超时以后才到的不要)
        const done = this.roomWait.get(msg.id);
        this.roomWait.delete(msg.id);
        done?.(msg);
        break;
      }
      case 'interrupted':
        this.hooks.onInterrupted?.(msg.turn, msg.reason);
        this.dropUpTo = Math.max(this.dropUpTo, msg.turn);
        if (this.out && this.out.turn === msg.turn) this.out.stop();
        this.motion.stopAll(nowS());
        this.performer.reset();
        vrm?.lookAtUser();
        if (msg.reason === 'barge_in') this.motion.backchannel('listen', nowS());
        break;
      case 'error':
        this.hooks.log?.('服务端错误', msg.message);
        this.hooks.onError?.(msg.turn, msg.message);
        break;
      default: {  // 小屋要的几件事的回答 (roomAsk); QQ 渠道的状态、草稿、提示 (只出不进, 见 qq.js)
        const wait = this.askWait?.get(msg.id);
        if (wait && wait.replies.includes(msg.type)) { this.askWait.delete(msg.id); wait.done(msg); break; }
        if (typeof msg.type === 'string' && msg.type.startsWith('channel_')) this.hooks.onChannel?.(msg);
        if (msg.type === 'watch_state' || msg.type === 'watch_ctl_done') this.hooks.onWatch?.(msg);  // 陪看 (wm/watch_along.py): 开始 / 结束、"不看了"的回话
        break;
      }
    }
  }

  onAudio(buf) {
    const dv = new DataView(buf);
    const magic = String.fromCharCode(dv.getUint8(0), dv.getUint8(1), dv.getUint8(2), dv.getUint8(3));
    if (magic !== 'WMA1') return;
    const turnId = dv.getUint32(4, true), seg = dv.getUint32(8, true), sr = dv.getUint32(12, true);
    if (turnId <= this.dropUpTo || !this.out) return;
    const pcm = new Int16Array(buf, HEADER_BYTES, (buf.byteLength - HEADER_BYTES) >> 1);
    const r = this.out.play(turnId, sr, pcm, seg);
    if (r.first) {
      this.mark(turnId, 'client_audio_start', r.startPerf);
      this.hooks.onAudioStart?.(turnId);
      if (this.turn?.id === turnId) this.turn.audioAt = r.startPerf;
    }
    if (this.turn?.id === turnId) {
      this.turn.audioSeconds = (this.turn.audioSeconds || 0) + r.duration;
      this.turn.segments = Math.max(this.turn.segments || 0, seg + 1);
    }
    const key = `${turnId}:${seg}`;
    if (!this.segSeen.has(key)) {  // 一句话的第一段音频: 句首的视线表演从这里排
      this.segSeen.add(key);
      this.performer.beginClause(r.start, this.segText.get(key) || '');
    }
    this.performer.addAudio(r.start, r.frames);
  }

  /**
   * 头部动作交给 Soullink (缓动更自然); 身体自己做的 (蹲下) 交给 bodyActs; 其余手势用程序动作.
   * 空间动作桌面上做不了 (能力声明里也没有).
   */
  runActs(acts, op) {
    const vrm = this.avatar;
    const head = vrm ? acts.filter((a) => a.do === 'nod' || a.do === 'shake') : [];
    for (const a of head) {
      setTimeout(() => this.avatar === vrm && vrm.headAction(a.do, { amplitude: 0.6 * a.amp, speed: a.spd, repetitions: 2, duration: 0.7 }),
        (a.at || 0) * 1000);
    }
    const body = acts.filter((a) => this.bodyActs[a.do]);
    for (const a of body) this._busyFor(this.bodyActs[a.do](a) + 1);
    const rest = acts.filter((a) => !head.includes(a) && !body.includes(a) && !SPATIAL.has(a.do));
    this.motion.applyIntent(op, rest, nowS());
  }
}
