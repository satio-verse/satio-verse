// 本机接口 (OpenAI 兼容 /v1 和 MCP /mcp, 宿主那边见 desktop/host/src/api.h) 转来的请求: 变成 /ws 上的一轮对话或本地的动作,
// 做到哪了用 "apiev <id> <json>" 报回宿主 (delta 一段回复 / done 结束).
//   chat, mode=pet: 桌面上这条对话 (开口说、表情、手势, 对话栏照常显示, 上下文和用户跟她的对话共用);
//   chat, mode=text: 另开一条不出声、不动的对话 (上下文分开), 第一次用时才连;
//   chat, mode=guest: wm-guest (第三方机器人框架转来的别人的话): 走服务端的访客连接 /guest, 不是 /ws. 没有工具、读不到主人的记忆、
//     不写记忆, 按 user 分会话; 第一次用时才连;
//   act: 本地做一个技能; state: 页面这边的状态; cancel: 宿主不等了 (超时或调用方断开), 正在说的这一轮打断.
// 同一种模式的请求排队一个一个来: 新的一句会打断她正在说的话, 所以桌面模式要等上一句说完 (声音放完) 再发下一句.
import { DESKTOP_SKILLS, Talk } from '/pet/talk.js';

const TURN_TIMEOUT_MS = 170000;  // 宿主等 180 秒, 页面先放弃, 报回的错误更清楚
const SPEAK_TIMEOUT_MS = 90000;  // 文字发完之后最多再等这么久声音放完, 才放下一句

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
async function until(pred, ms) {
  for (let t = 0; t < ms; t += 100) {
    if (pred()) return true;
    await sleep(100);
  }
  return pred();
}

/** 服务端的访客连接 (/guest): 一问一答 ({type: chat, id, user, text} -> {type: reply, id, ok, reply, emo | status, error}). */
class GuestLink {
  constructor(log) {
    this.log = log;
    this.ws = null;
    this.seq = 0;
    this.waiting = new Map();  // id -> resolve
  }

  get connected() { return this.ws?.readyState === WebSocket.OPEN; }

  // 等 open / close 事件, 不轮询: 桌宠隐藏时 (用户藏起来、全屏游戏) WebView2 会压页面的计时器, 轮询一次请求能拖一分多钟
  open(ms) {
    if (this.connected) return Promise.resolve(true);
    if (!this.ws || this.ws.readyState > WebSocket.OPEN) {
      const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/guest`);
      this.ws = ws;
      ws.onmessage = (e) => {
        const m = JSON.parse(e.data);
        const done = this.waiting.get(m.id);
        if (done) { this.waiting.delete(m.id); done(m); }
      };
      ws.onclose = () => {
        if (this.ws === ws) this.ws = null;
        for (const done of this.waiting.values()) done({ ok: false, status: 503, error: '访客连接断了' });
        this.waiting.clear();
      };
    }
    const ws = this.ws;
    return new Promise((resolve) => {
      const done = () => { clearTimeout(timer); resolve(this.connected); };
      const timer = setTimeout(done, ms);
      ws.addEventListener('open', done, { once: true });
      ws.addEventListener('close', done, { once: true });
    });
  }

  async chat(user, text, ms) {
    if (!(await this.open(8000))) return { ok: false, status: 503, error: '连不上 WorldModel 服务的访客连接 (/guest)' };
    const id = ++this.seq;
    return new Promise((resolve) => {
      const timer = setTimeout(() => { this.waiting.delete(id); resolve({ ok: false, status: 504, error: `等了 ${ms / 1000} 秒没有回复` }); }, ms);
      this.waiting.set(id, (m) => { clearTimeout(timer); resolve(m); });
      this.ws.send(JSON.stringify({ type: 'chat', id, user, text }));
    });
  }
}

export class ApiBridge {
  /** talk: 桌面上的对话; post: 发给宿主; wake: 马上排一帧 (要说话、要动了) */
  constructor(talk, { post, wake, log }) {
    this.talks = { pet: talk, text: null };
    this.guestLink = new GuestLink(log);
    this.post = post;
    this.wake = wake;
    this.log = log;
    this.queue = { pet: Promise.resolve(), text: Promise.resolve(), guest: Promise.resolve() };
    this.jobs = new Map();  // 请求 id → { id, mode, text, turn, reply, sent, finished, release }
  }

  /** 文字模式的对话, 第一次用时才连; 和桌面上的对话用同一个人设预设. */
  textTalk() {
    if (!this.talks.text) {
      this.talks.text = new Talk(null, this.hooks('text'), { headless: true });
      if (this.profile) this.talks.text.profile = this.profile;
      this.talks.text.connect();
    }
    return this.talks.text;
  }

  setProfile(profile) {
    this.profile = profile;
    this.talks.text?.setProfile(profile);
  }

  /** 给某条对话的回调: 桌面那条由 pet.js 在自己的回调里转过来, 文字那条直接用这一份. */
  hooks(mode) {
    return {
      onTurn: (turn, text, source) => this.onTurn(mode, turn, text, source),
      onReply: (turn, text) => this.onReply(mode, turn, text),
      onServerDone: (turn) => this.onServerDone(mode, turn),
      onDone: (turn) => this.onDone(mode, turn),
      onInterrupted: (turn) => this.onInterrupted(mode, turn),
      onError: (turn, message) => this.onError(mode, turn, message),
      log: this.log,
    };
  }

  handle(m) {
    if (m.kind === 'chat') this.chat(m);
    else if (m.kind === 'act') this.act(m);
    else if (m.kind === 'state') this.send(m.id, { ev: 'done', ok: true, state: this.state() });
    else if (m.kind === 'cancel') this.cancel(m.id);
    else this.send(m.id, { ev: 'done', ok: false, status: 400, error: `不认识的请求 ${m.kind}` });
  }

  send(id, obj) { this.post(`apiev ${id} ${JSON.stringify(obj)}`); }

  chat(m) {
    const mode = m.mode === 'text' ? 'text' : m.mode === 'guest' ? 'guest' : 'pet';
    const text = String(m.text || '').trim();
    if (!text) {
      this.send(m.id, { ev: 'done', ok: false, status: 400, error: '没有要说的话' });
      return;
    }
    const job = { id: m.id, mode, text, user: String(m.user || ''), turn: undefined, reply: '', sent: false, finished: false, release: null };
    this.jobs.set(m.id, job);
    this.queue[mode] = this.queue[mode].then(() => this.run(job)).catch((err) => this.log('本机接口出错', err.message));
  }

  /** wm-guest: 交给访客连接, 不碰桌面上的对话 (不显示、不出声). */
  async runGuest(job) {
    job.sent = true;
    const r = await this.guestLink.chat(job.user, job.text, TURN_TIMEOUT_MS);
    if (job.finished) return;  // 宿主不等了
    if (r.ok && r.reply) this.send(job.id, { ev: 'delta', text: r.reply });
    this.finish(job, r.ok ? { ok: true, reply: r.reply || '', emo: r.emo ?? null, blocked: r.blocked ?? null }
      : { ok: false, status: r.status || 502, error: r.error || '出错了' });
  }

  async run(job) {
    if (job.finished) return;  // 排队时就取消了
    if (job.mode === 'guest') return this.runGuest(job);
    const talk = job.mode === 'pet' ? this.talks.pet : this.textTalk();
    if (!(await until(() => talk.connected, 8000))) {
      this.finish(job, { ok: false, status: 503, error: '连不上 WorldModel 服务 (/ws)' });
      return;
    }
    await new Promise((resolve) => {
      job.release = resolve;
      job.timer = setTimeout(() => this.finish(job, { ok: false, status: 504, error: `等了 ${TURN_TIMEOUT_MS / 1000} 秒没有回复完` }), TURN_TIMEOUT_MS);
      job.sent = true;
      talk.say(job.text, { api: job.id });
      this.wake();
    });
  }

  /** 这条对话里正在等某一轮的请求. */
  jobFor(mode, turn) {
    for (const job of this.jobs.values()) if (job.mode === mode && job.turn === turn && turn !== undefined) return job;
    return null;
  }

  onTurn(mode, turn, text, source) {
    // 认自己发的那一轮: 同一条对话、来源是文字、内容一样 (服务端只去掉首尾空白). 同一模式一次只有一个请求在等.
    for (const job of this.jobs.values()) {
      if (job.mode === mode && job.sent && job.turn === undefined && source === 'text' && text === job.text) {
        job.turn = turn;
        return;
      }
    }
  }

  onReply(mode, turn, text) {
    const job = this.jobFor(mode, turn);
    if (!job || job.finished) return;
    job.reply += text;
    this.send(job.id, { ev: 'delta', text });
  }

  onServerDone(mode, turn) {
    const job = this.jobFor(mode, turn);
    if (!job) return;
    const t = this.talks[mode].turn;
    const extra = t?.id === turn ? { emo: t.emo ?? null, acts: t.acts ?? [], op: t.op ?? null } : {};
    // 文字模式到这里就结束; 桌面模式的回复文字也发完了, 但要等声音放完 (onDone) 才放下一句
    this.finish(job, { ok: true, reply: job.reply, ...extra }, mode === 'text' || job.spoken);
    if (mode === 'pet' && !job.spoken) job.speakTimer = setTimeout(() => this.release(job), SPEAK_TIMEOUT_MS);
  }

  /** 声音放完了 (很短的回复可能比服务端的"做完了"还早到). */
  onDone(mode, turn) {
    const job = this.jobFor(mode, turn);
    if (!job) return;
    job.spoken = true;
    if (job.finished) this.release(job);
  }

  onInterrupted(mode, turn) {
    const job = this.jobFor(mode, turn);
    if (job) this.finish(job, { ok: true, reply: job.reply, interrupted: true });
  }

  onError(mode, turn, message) {
    const job = this.jobFor(mode, turn);
    if (job) this.finish(job, { ok: false, status: 502, error: `服务端出错: ${message}` });
  }

  /** 报回结果; release 时同时放下一个请求. */
  finish(job, result, release = true) {
    if (!job.finished) {
      job.finished = true;
      clearTimeout(job.timer);
      this.send(job.id, { ev: 'done', ...result });
    }
    if (release) this.release(job);
  }

  release(job) {
    clearTimeout(job.speakTimer);
    this.jobs.delete(job.id);
    job.release?.();
    job.release = null;
  }

  /** 宿主不等了: 还在排队的不发; 已经在说的打断 (打断的回应到了会走 onInterrupted, 那时宿主已经不收了). */
  cancel(id) {
    const job = this.jobs.get(id);
    if (!job) return;
    const talk = this.talks[job.mode];
    if (job.sent && !job.finished && talk?.turn?.api === id) talk.interrupt();
    job.finished = true;
    clearTimeout(job.timer);
    this.release(job);
  }

  act(m) {
    const talk = this.talks.pet;
    if (!talk.skills.includes(m.skill)) {
      const why = DESKTOP_SKILLS.includes(m.skill) || m.skill === 'squat' ? '这个身体现在做不了' : '没有这个手势';
      this.send(m.id, { ev: 'done', ok: false, status: 400, error: `${why}: ${m.skill}` });
      return;
    }
    if (!talk.avatar) {
      this.send(m.id, { ev: 'done', ok: false, status: 503, error: '角色还没加载好' });
      return;
    }
    talk._busyFor(4);
    talk.runActs([{ do: m.skill, side: m.side === 'L' ? 'L' : 'R', to: null, amp: 1, spd: 1, at: 0 }], 'act');
    this.wake();
    this.send(m.id, { ev: 'done', ok: true });
  }

  state() {
    const talk = this.talks.pet, s = talk.status;
    return {
      connected: talk.connected, speaking: !!talk.out?.playing, busy: talk.busy, listening: talk.micOn, emotion: talk.emo,
      avatar: talk.avatar?.info?.name ?? null,
      engines: s ? { llm: s.llm?.engine, tts: s.tts?.engine, asr: s.asr?.engine } : null,
      text_mode: this.talks.text ? { connected: this.talks.text.connected } : null,
      guest_mode: { connected: this.guestLink.connected },
      // 服务端对人设预设和工具表的回应 (不带 key)
      profile_status: talk.profileStatus ? { ok: talk.profileStatus.ok, error: talk.profileStatus.error ?? null,
        engine: talk.profileStatus.llm?.engine ?? null, model: talk.profileStatus.llm?.model ?? null,
        local: talk.profileStatus.llm?.local === true, warnings: talk.profileStatus.warnings || [] } : null,
      tools: { declared: talk.tools?.length || 0, accepted: talk.toolsStatus?.count ?? null, active: talk.toolsStatus?.active ?? null },
      // 当前人设预设 (不带 key)
      persona: this.profile ? { name: this.profile.persona?.name ?? null, provider: this.profile.llm?.provider ?? 'local',
        model: this.profile.llm?.model || null, skills: (this.profile.skills || []).map((s) => s.name), mcp: this.profile.mcp || [] } : null,
      queued: Object.fromEntries(['pet', 'text', 'guest'].map((k) => [k, [...this.jobs.values()].filter((j) => j.mode === k).length])),
    };
  }
}
