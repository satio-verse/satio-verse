// 闲时状态机和对话里的细微反应 (2D 门面). 纯逻辑: 不碰 DOM、不碰 Cubism、没有计时器, 时间全由调用方传进来 (update(now)),
// 随机数和钟点可以注入, 所以 20 分钟的剧本几毫秒就能跑完 (tests/idle_director.test.mjs).
//
// 只管身体: 输出"表现状态"给 live2d-avatar.js 的 setPresence(): 头 (度)、视线 (-1..1)、AU 强度 (expression-caps.js 的名字)、
// 要不要放模型自带的动作、表情衰减的半衰期. 没有任何说话的出口; 做什么事、要不要搭话由她定. 也不演"在做某件事"(看书、打字…)——
// 她真在做的事 (对话忙、陪看、屏幕陪看、工具在跑、在操作电脑) 只会让她"在场", 不会往下走.
//
// 约定: head.pitch 正值 = 低头 (点头方向); head.yaw 正值 = 朝屏幕右边; head.roll 正值 = 歪头 (live2d-avatar 里加到 ParamAngleZ);
//       gaze.x 正值 = 看右边, gaze.y 正值 = 看上面.
//
// 状态 (距上次互动 idle 秒; 夜里 22:30–06:00 走神和犯困提前):
//   present 在场 -> wander 走神 (120 / 夜 90) -> drowsy 犯困 (480 / 夜 300) -> asleep 睡 (1800)
//   任何互动 (含她在场的信号) 回 present; 在 wander / drowsy / asleep 时先走 waking 醒来过渡 (0.5 / 1.0 / 1.6 秒): 抬头、睁眼、认出、点头.
//   页面被藏起来期间 (全屏自动隐藏、托盘隐藏) 帧循环停着, 重新显示时 now 一下跳很多: 藏着的那段算"没人理", 该睡就直接是睡着,
//   不补放中间的过程; 重新显示本身不算互动 (没有人碰她).

/** 阈值和常量集中在这里. 页面参数 idlefx_scale=N 把时间轴压缩 N 倍 (测试和看效果用). */
export const CFG = {
  wanderS: 120, drowsyS: 480, asleepS: 1800,
  nightWanderS: 90, nightDrowsyS: 300, nightFrom: 22.5, nightTo: 6,
  wakeS: { wander: 0.5, drowsy: 1.0, asleep: 1.6 },
  halfLifeS: { present: 20, other: 8 },
  scale: 1,
  pokeHalfLifeS: 15, pokeAnnoyed: 2.5, pokeAngry: 4.5, pokeCalm: 1.5,
  typingStaleS: 8, typingPauseS: 1.2, thinkingMaxS: 6, inhaleMaxS: 1.2,
};

const smooth = (x) => (x <= 0 ? 0 : x >= 1 ? 1 : x * x * (3 - 2 * x));
const bump = (x) => (x <= 0 || x >= 1 ? 0 : Math.sin(Math.PI * x) ** 2);  // 0 -> 1 -> 0
const clamp01 = (x) => Math.max(0, Math.min(1, x));
export const AU_NAMES = ['eyeClose', 'eyeWide', 'browSoft', 'browUp', 'mouthOpen', 'mouthYawn', 'breathDeep'];
export const OUT_KEYS = ['state', 'head', 'gaze', 'au', 'idleMotions', 'halfLifeS', 'wake'];

class Chan {  // 一阶平滑的通道
  constructor() { this.v = 0; this.t = 0; }
  step(k) { this.v += (this.t - this.v) * k; }
}

export class IdleDirector {
  /** rand(): 0..1; hour(): 她那边现在是几点 (小数, 22.5 = 22:30); cfg: 覆盖 CFG 里的项. */
  constructor({ rand = Math.random, hour = () => 12, cfg = {} } = {}) {
    this.cfg = { ...CFG, ...cfg, wakeS: { ...CFG.wakeS, ...cfg.wakeS }, halfLifeS: { ...CFG.halfLifeS, ...cfg.halfLifeS } };
    this.rand = rand;
    this.hour = hour;
    this.state = 'present';
    this.last = null;           // 上一次 update 的时间
    this.lastInteract = null;
    this.flags = new Set();     // 她在场的信号: talk、watch、tool、control…
    this.ch = { pitch: new Chan(), yaw: new Chan(), roll: new Chan(), gx: new Chan(), gy: new Chan(),
      lid: new Chan(), brow: new Chan(), softbrow: new Chan(), breath: new Chan() };
    this.cues = [];             // {kind, start, dur, a}: 一次性的小动作, 直接叠在平滑通道上
    this.events = [];           // 给页面的一次性事件: {type:'emotion', label} / {type:'cancelHead'} (takeEvents 取走)
    this.pending = [];          // {at, ev}: 还没到时间的事件
    this.next = {};             // 各种随机小动作的下一次时间
    this.look = null;
    this.tiltTo = 0; this.tiltUntil = 0;
    this.drift = 0;
    this.wake = null;
    this.typ = null;            // {last, len}
    this.typingPausedAt = null; this.typingDir = 1;
    this.thinking = null;       // {since}
    this.inhale = null;
    this.audioStarted = false;
    this.pokeScore = 0; this.pokeAt = 0; this.annoyed = false; this.away = 1;
    this.out = { state: 'present', head: { pitch: 0, yaw: 0, roll: 0 }, gaze: { x: 0, y: 0 },
      au: Object.fromEntries(AU_NAMES.map((n) => [n, 0])), idleMotions: true, halfLifeS: this.cfg.halfLifeS.present, wake: null };
    this.stats = { updates: 0, changes: 0 };
  }

  // ---- 输入 ------------------------------------------------------------------------------------------------------------

  /** 互动: 打字、开口、点她、光标在头附近动、拖她、打开输入框…. 重置计时; 在睡意里就开始醒来. */
  interact(now, _kind = '') {
    this._init(now);
    if (this.state === 'wander' || this.state === 'drowsy' || this.state === 'asleep') this._beginWake(now);
    this.lastInteract = now;
  }

  /** 她在场的信号 (talk 忙、陪看、屏幕陪看、工具在跑、在操作电脑…): 有一个为真就不往下走; 信号刚出现时算一次互动. */
  setPresent(key, on, now) {
    this._init(now);
    if (on) {
      if (!this.flags.has(key)) { this.flags.add(key); this.interact(now, key); }
    } else this.flags.delete(key);
  }

  /** 输入框里的字变了 (len = 现在的字数); 0 = 清空或发出去了. */
  typing(now, len) {
    this.interact(now, 'typing');
    if (len > 0) {
      if (!this.typ) this.typingDir = -this.typingDir;
      this.typ = { last: now, len };
    } else this.typ = null;
    this.typingPausedAt = null;
  }

  turnStart(now) { this.audioStarted = false; this.typ = null; this.interact(now, 'turn'); }
  thinkingOn(now) { this._init(now); this.thinking = { since: now }; }
  /** emotion 消息到了 (早于第一声): 开口前的吸气. */
  emotionEvent(now) {
    this._init(now);
    if (!this.audioStarted) this.inhale = { since: now };
  }
  audioStart(now) { this._init(now); this.audioStarted = true; this.inhale = null; this.thinking = null; }
  /** 被打断: 眨一下眼、头上的点头摇头清掉、微微扬眉. */
  interrupted(now) {
    this.interact(now, 'interrupted');
    this.thinking = null; this.inhale = null;
    this.events.push({ type: 'cancelHead' });
    this._cue('blink', now, 0.15);
    this._cue('startle', now, 0.8, 0.2);
  }
  /** 被戳: 返回 0 (还不烦: 页面照旧轮着做四种反应) / 1 (有点不好意思) / 2 (嫌你); 1 和 2 的表情由 takeEvents 给. */
  poke(now) {
    this.interact(now, 'poke');
    this.pokeScore = this._pokeNow(now) + 1;
    this.pokeAt = now;
    const level = this.pokeScore < this.cfg.pokeAnnoyed ? 0 : this.pokeScore < this.cfg.pokeAngry ? 1 : 2;
    if (level >= 1) { this.away = this.rand() < 0.5 ? -1 : 1; }
    if (level === 1) { this.events.push({ type: 'emotion', label: 'shy' }); this._cue('turnaway', now, 2, 8 * this.away); }
    if (level === 2) { this.events.push({ type: 'emotion', label: 'angry' }); this.annoyed = true; }
    return level;
  }

  takeEvents() {
    if (!this.events.length) return this.events;
    const e = this.events;
    this.events = [];
    return e;
  }

  // ---- 每帧 ------------------------------------------------------------------------------------------------------------

  /** now: 秒 (单调钟; 页面藏起来再显示时会一下跳很多). 返回同一个 out 对象 (每帧复用, 别留着引用做对比). */
  update(now) {
    this._init(now);
    const dt = Math.max(0, now - this.last);
    this.last = now;
    const dtF = Math.min(dt, 0.25);  // 平滑只往前走一小步, 藏很久以后回来是缓缓过去而不是跳
    this.stats.updates++;
    if (this.flags.size) this.lastInteract = now;
    // 状态
    if (this.state === 'waking') {
      if (now >= this.wake.start + this.wake.dur) { this.state = 'present'; this.wake = null; this.lastInteract = Math.max(this.lastInteract, now); this._enter('present', now); }
    } else {
      const s = this._depth((now - this.lastInteract) * this.cfg.scale, now);
      if (s !== this.state) { this.state = s; this._enter(s, now); }
    }
    for (let i = this.pending.length - 1; i >= 0; i--) if (this.pending[i].at <= now) { this.events.push(this.pending[i].ev); this.pending.splice(i, 1); }
    const c = this.ch;
    // 平滑通道的目标 + 时间常数
    let tauHead = 0.8, tauEye = 0.12, tauLid = 1.2, tauBrow = 0.25;
    c.pitch.t = c.yaw.t = c.roll.t = c.gx.t = c.gy.t = c.lid.t = c.brow.t = c.softbrow.t = c.breath.t = 0;
    switch (this.state) {
      case 'present': this._present(now); break;
      case 'wander': this._wander(now); break;
      case 'drowsy': this._drowsy(now); break;
      case 'asleep': this._asleep(now); break;
      case 'waking': {
        const d = this.wake.dur;
        tauHead = 0.1 + 0.25 * d; tauLid = 0.08 + 0.2 * d; tauEye = 0.1;
        break;
      }
    }
    if (this.state === 'present' || this.state === 'waking') this._conversation(now, c);
    if (this.annoyed) {  // 被戳多了: 计数降到 pokeCalm 以下才回来
      if (this._pokeNow(now) < this.cfg.pokeCalm) { this.annoyed = false; this.events.push({ type: 'emotion', label: 'neutral' }); }
      else { c.gx.t = 0.5 * this.away; c.yaw.t = 12 * this.away; }  // 头偏开、眼睛看向别处
    }
    const kh = 1 - Math.exp(-dtF / tauHead), ke = 1 - Math.exp(-dtF / tauEye), kl = 1 - Math.exp(-dtF / tauLid), kb = 1 - Math.exp(-dtF / tauBrow);
    c.pitch.step(kh); c.yaw.step(kh); c.roll.step(kh);
    c.gx.step(ke); c.gy.step(ke);
    c.lid.step(kl); c.breath.step(kl);
    c.brow.step(kb); c.softbrow.step(kb);
    // 一次性的小动作 (已经是连续的包络, 直接叠加)
    let dp = 0, dy = 0, dr = 0, lid = 0, wide = 0, browUp = 0, mouth = 0, yawn = 0, breath = 0;
    for (let i = this.cues.length - 1; i >= 0; i--) {
      const q = this.cues[i], u = (now - q.start) / q.dur;
      if (u >= 1) { this.cues.splice(i, 1); continue; }
      if (u < 0) continue;
      switch (q.kind) {
        case 'nodoff': {  // 慢慢低头合眼, 然后猛一抬头睁大
          const t = u * q.dur;
          if (t < 2.5) { const p = smooth(t / 2.5); dp += 8 * p; lid += 0.35 * p; }
          else if (t < 2.75) { const p = (t - 2.5) / 0.25; dp += 8 * (1 - smooth(p)); lid += 0.35 * (1 - p); wide += 0.3 * smooth(p); }
          else wide += 0.3 * (1 - (t - 2.75) / (q.dur - 2.75));
          break;
        }
        case 'yawn': { const e = bump(u); yawn += 0.7 * e; lid += 0.4 * e; dp -= 2 * e; break; }
        case 'sigh': breath += 0.5 * bump(u); break;
        case 'shift': dr += q.a * bump(u); break;
        case 'blink': lid += 0.9 * bump(u); break;
        case 'startle': browUp += q.a * (1 - u); break;
        case 'turnaway': dy += q.a * bump(u); break;
        case 'nod': dp += q.a * bump(u); break;
        case 'recog': browUp += q.a * bump(u); break;
        default: break;
      }
    }
    if (this.inhale) {
      const t = now - this.inhale.since;
      if (t > this.cfg.inhaleMaxS + 0.2) this.inhale = null;
      else { const e = smooth(t / 0.25) * (1 - smooth((t - this.cfg.inhaleMaxS) / 0.2)); dp -= 1.5 * e; browUp += 0.12 * e; mouth += 0.15 * e; }
    }
    // 出
    const o = this.out;
    o.state = this.state;
    o.head.pitch = c.pitch.v + dp;
    o.head.yaw = c.yaw.v + dy;
    o.head.roll = c.roll.v + dr;
    o.gaze.x = c.gx.v;
    o.gaze.y = c.gy.v;
    const au = o.au;
    au.eyeClose = clamp01(c.lid.v + lid);
    au.eyeWide = clamp01(wide);
    au.browSoft = clamp01(c.softbrow.v);
    au.browUp = clamp01(c.brow.v + browUp);
    au.mouthOpen = clamp01(mouth);
    au.mouthYawn = clamp01(yawn);
    au.breathDeep = clamp01(c.breath.v + breath);
    o.idleMotions = this.state === 'present' || this.state === 'wander';
    o.halfLifeS = this.state === 'present' ? this.cfg.halfLifeS.present : this.cfg.halfLifeS.other;
    o.wake = this.state === 'waking' ? { from: this.wake.from, u: clamp01((now - this.wake.start) / this.wake.dur) } : null;
    return o;
  }

  // ---- 内部 ------------------------------------------------------------------------------------------------------------

  _init(now) {
    if (this.last === null) { this.last = now; this.lastInteract = now; this._enter('present', now); }
  }

  /** 夜里吗: 钟点最多每 5 秒问一次 (hour() 里要建 Date 对象, 不在每帧里做). */
  _isNight(now) {
    if (this._nightAt === undefined || now - this._nightAt >= 5 || now < this._nightAt) {
      const h = this.hour();
      this._night = h >= this.cfg.nightFrom || h < this.cfg.nightTo;
      this._nightAt = now;
    }
    return this._night;
  }

  _depth(idle, now) {
    const night = this._isNight(now);
    const w = night ? this.cfg.nightWanderS : this.cfg.wanderS, d = night ? this.cfg.nightDrowsyS : this.cfg.drowsyS;
    return idle >= this.cfg.asleepS ? 'asleep' : idle >= d ? 'drowsy' : idle >= w ? 'wander' : 'present';
  }

  _pokeNow(now) { return this.pokeScore * Math.pow(0.5, Math.max(0, now - this.pokeAt) / this.cfg.pokeHalfLifeS); }

  _cue(kind, start, dur, a = 0) { this.cues.push({ kind, start, dur, a }); }
  _range(a, b) { return a + this.rand() * (b - a); }

  _enter(state, now) {
    const n = this.next;
    this.look = null; this.tiltUntil = 0;
    n.look = now + this._range(1, 4); n.tilt = now + this._range(20, 40);
    n.sigh = now + this._range(40, 90); n.nodoff = now + this._range(25, 50); n.yawn = now + this._range(180, 300);
    n.shift = now + this._range(30, 50); n.drift = now + this._range(10, 20);
    this.drift = 0;
  }

  _beginWake(now) {
    const from = this.state;
    const dur = this.cfg.wakeS[from];
    this.state = 'waking';
    this.wake = { start: now, dur, from };
    this._enter('waking', now);
    this.cues = this.cues.filter((q) => q.kind === 'blink' || q.kind === 'startle');
    this._cue('recog', now + 0.35 * dur, 0.5, 0.3);
    this._cue('nod', now + 0.7 * dur, 0.45, 5);
    this.pending.push({ at: now + 0.7 * dur, ev: { type: 'emotion', label: 'happy' } });
  }

  _present(now) {
    const c = this.ch, n = this.next;
    if (now >= n.look) {  // 眼神小幅飘一下再回来
      this.look = { x: this._range(-0.24, 0.24), y: this._range(-0.15, 0.15), until: now + this._range(0.6, 1.2) };
      n.look = now + this._range(5, 12);
    }
    if (this.look && now < this.look.until) { c.gx.t = this.look.x; c.gy.t = this.look.y; }
    if (now >= n.tilt) { this.tiltTo = this._range(-3, 3); this.tiltUntil = now + 3; n.tilt = now + this._range(20, 40); }
    if (now < this.tiltUntil) c.roll.t = this.tiltTo;
  }

  _wander(now) {
    const c = this.ch, n = this.next;
    if (now >= n.look) {  // 视线飘向一侧或上方, 过几秒慢慢回来
      const side = this.rand() < 0.5 ? -1 : 1;
      this.look = { x: side * this._range(0.35, 0.6), y: this._range(0.15, 0.35), until: now + this._range(3, 6) };
      n.look = now + this._range(8, 20);
    }
    if (this.look && now < this.look.until) { c.gx.t = this.look.x; c.gy.t = this.look.y; }
    if (now >= n.drift) { this.drift = this._range(-4, 4); n.drift = now + this._range(10, 20); }
    c.roll.t = this.drift;
    c.softbrow.t = 0.2;
    if (now >= n.sigh) { this._cue('sigh', now, 2.5); n.sigh = now + this._range(40, 90); }
  }

  _drowsy(now) {
    const c = this.ch, n = this.next;
    c.lid.t = 0.25 + 0.15 * (1 + Math.sin((2 * Math.PI * now) / 40));
    c.softbrow.t = 0.3;
    c.pitch.t = 1;
    if (now >= n.nodoff) { this._cue('nodoff', now, 3.4); n.nodoff = now + 3.4 + this._range(25, 50); }
    if (now >= n.yawn) { this._cue('yawn', now, 2.2); n.yawn = now + 2.2 + this._range(180, 300); }
  }

  _asleep(now) {
    const c = this.ch, n = this.next;
    c.lid.t = 0.95;
    c.pitch.t = 10;
    c.softbrow.t = 0.3;
    c.breath.t = 0.25 + 0.15 * Math.sin((2 * Math.PI * now) / 4);
    if (now >= n.shift) { this._cue('shift', now, 3, this._range(-2, 2)); n.shift = now + 3 + this._range(30, 50); }
  }

  // 对话里的持续反应 (在场时): 打字、想
  _conversation(now, c) {
    const t = this.typ;
    if (t && now - t.last < this.cfg.typingStaleS) {
      c.gy.t = -0.32;           // 视线落向输入框
      c.pitch.t = 2;
      if (now - t.last >= this.cfg.typingPauseS) { c.roll.t = 5 * this.typingDir; c.brow.t = 0.12; }  // 停顿: 歪头、微抬眉
    }
    const k = this.thinking;
    if (k) {
      if (now - k.since > this.cfg.thinkingMaxS) this.thinking = null;
      else { c.gx.t = -0.4; c.gy.t = 0.4; c.roll.t = 5; c.brow.t = 0.15; }  // 视线移向左上、歪头
    }
  }
}
