// 说话时的头部与视线表演. 事件都按音频时钟 (AudioContext 时间) 排程, 渲染循环里用"扬声器正在播放的时刻"触发,
// 所以和声音、口型对齐. 打断时整体清空.
//
// 规则 (启发式, 参考对话中注视与韵律手势的常见现象, 不是心理学定论):
//   - 一句话开头视线短暂移开 (组织语言), 说着说着看回对方
//   - 重音处轻轻点头 (幅度随重音强度), 间隔不小于 0.6 s; 音高明显偏高的强重音配一下扬眉.
//     重音来自服务端的韵律分析 (基频 + 能量, prosody 消息); 没有时退回前端的响度 (安静后突然变响)
//   - 语调上扬的句末 (问句或没说完) 微微歪头、扬眉; 语调下降的句末轻轻低一下头收尾
//   - 文字是问句但语调没有上扬时, 仍按问号歪头
//   - 整轮说完视线回到对方, 轻点一下头
//   - 听对方说话时注视对方, 每 2~4 s 轻点一次头表示在听
import { HOP } from './lipsync.js';

const SPEAKABLE = /[㐀-鿿A-Za-z0-9]/g;
const QUESTION = /[?？]\s*$/;

export class SpeechPerformer {
  constructor() {
    this.avatar = null;
    this.events = [];
    this.neck = { roll: 0, pitch: 0 };        // 表演层给颈部的临时偏移 (度)
    this.neckTarget = { roll: 0, pitch: 0 };
    this.lastBeat = -Infinity;
    this.recent = [];
    this.gazeSide = 1;
    this.listening = false;
    this.nextListenNod = 0;
    this.arousal = 0;
    this.prosody = false;                       // 收到过服务端的韵律分析后, 不再用响度猜重音
    this.log = [];                              // 调试: 最近触发的表演事件
  }

  attach(avatar) { this.avatar = avatar; this.reset(); }

  reset() {
    this.events = [];
    this.neckTarget = { roll: 0, pitch: 0 };
    this.lastBeat = -Infinity;
  }

  setArousal(a) { this.arousal = a; }

  _at(t, name, fn) {
    this.events.push({ t, name, fn });
    this.events.sort((a, b) => a.t - b.t);
  }

  /** 一句话的第一段音频排进时间线时调用. start: 上下文时间 (秒). */
  beginClause(start, text) {
    this.closeClause();
    this.recent = [];
    if (!this.avatar) return;
    const n = (text.match(SPEAKABLE) || []).length;
    if (n >= 5) {
      this.gazeSide = -this.gazeSide;
      const yaw = this.gazeSide * (8 + Math.random() * 6), pitch = 4 + Math.random() * 5;
      const hold = Math.min(0.9, 0.35 + n * 0.03);
      this._at(start - 0.08, 'gaze_away', () => this.avatar.glance(yaw, pitch, hold));
    }
    if (QUESTION.test(text)) this.pendingQuestion = true;
  }

  /** 每段音频排进时间线时调用: 在能量重音处排点头. frames 来自 lipsync.analyzeVisemes. */
  addAudio(start, frames) {
    this.clauseEnd = start + frames.length * HOP;
    if (!this.avatar || this.prosody) return;
    frames.forEach((f, i) => {
      const t = start + i * HOP;
      const level = f.level || 0;
      // 重音: 最近 100 ms 里有过安静 (<= 0.3), 这一帧突然变响 (>= 0.55).
      const recentMin = this.recent.length ? Math.min(...this.recent) : 1;
      if (level >= 0.55 && recentMin <= 0.3 && t - this.lastBeat >= 0.9) {
        this.lastBeat = t;
        const amplitude = Math.min(0.32, 0.14 + 0.12 * Math.max(0, this.arousal));
        // 点头的最低点略早于重读音节, 所以提前一点开始.
        this._at(t - 0.12, 'beat_nod', () => this.avatar.headAction('nod', { amplitude, duration: 0.42 }));
      }
      this.recent.push(level);
      if (this.recent.length > 5) this.recent.shift();
    });
  }

  /** 服务端韵律事件 (已换算成上下文时间): {t, k: accent|rise|fall, s, hi}. */
  addProsody(events) {
    this.prosody = true;
    if (!this.avatar) return;
    for (const e of events) {
      if (e.k === 'accent') {
        if (e.t - this.lastBeat < 0.6 || e.s < 0.15) continue;
        this.lastBeat = e.t;
        const amplitude = Math.min(0.34, 0.1 + 0.18 * e.s + 0.08 * Math.max(0, this.arousal));
        this._at(e.t - 0.12, 'accent_nod', () => this.avatar.headAction('nod', { amplitude, duration: 0.42 }));
        if (e.hi && e.s > 0.55) {
          this._at(e.t - 0.06, 'accent_brow', () => {
            this.avatar.pulseAU('au01InnerBrowRaiser', 0.3, 0.35);
            this.avatar.pulseAU('au02OuterBrowRaiser', 0.28, 0.35);
          });
        }
      } else if (e.k === 'rise') {
        this.pendingQuestion = false;  // 语调已经说明了, 句末不再按问号重复歪头
        this._at(e.t - 0.35, 'rise_tilt', () => {
          this.neckTarget.roll = 6;
          this.avatar.pulseAU('au01InnerBrowRaiser', 0.4, 0.8);
          this.avatar.pulseAU('au02OuterBrowRaiser', 0.35, 0.8);
        });
        this._at(e.t + 0.8, 'rise_tilt_end', () => { this.neckTarget.roll = 0; });
      } else if (e.k === 'fall') {
        this._at(e.t - 0.25, 'fall_settle', () => { this.neckTarget.pitch = 3; });
        this._at(e.t + 0.35, 'fall_settle_end', () => { this.neckTarget.pitch = 0; });
      }
    }
  }

  /** 一句话的音频收齐了 (下一句开始或整轮结束): 问句在句末歪头扬眉. */
  closeClause() {
    if (!this.pendingQuestion || !this.avatar) { this.pendingQuestion = false; return; }
    this.pendingQuestion = false;
    const end = this.clauseEnd;
    this._at(end - 0.45, 'question_tilt', () => {
      this.neckTarget.roll = 7;
      this.avatar.pulseAU('au01InnerBrowRaiser', 0.45, 0.9);
      this.avatar.pulseAU('au02OuterBrowRaiser', 0.4, 0.9);
    });
    this._at(end + 0.9, 'question_tilt_end', () => { this.neckTarget.roll = 0; });
  }

  /** 整轮音频播完的上下文时间. */
  endTurn(end) {
    this.closeClause();
    if (!this.avatar) return;
    this._at(end + 0.05, 'end_nod', () => {
      this.avatar.lookAtUser();
      this.avatar.headAction('nod', { amplitude: 0.16, duration: 0.5 });
    });
  }

  /** 临时歪头 (度, 正值歪向角色自己的左边), seconds 秒后回正. */
  tilt(roll, seconds) {
    this.neckTarget.roll = roll;
    clearTimeout(this._tiltTimer);
    this._tiltTimer = setTimeout(() => { this.neckTarget.roll = 0; }, seconds * 1000);
  }

  thinking() {
    if (this.avatar) this.avatar.glance(10, 12, 1.4);  // 思考时抬眼看向一侧
  }

  setListening(on, now) {
    this.listening = on;
    if (on && this.avatar) { this.avatar.lookAtUser(); this.nextListenNod = now + 1.2; }
  }

  /** heard: 扬声器正在播放的上下文时间; now: performance 秒; dt: 帧间隔. */
  update(heard, now, dt) {
    while (this.events.length && this.events[0].t <= heard) {
      const e = this.events.shift();
      e.fn();
      this.log.push(e.name);
      if (this.log.length > 50) this.log.shift();
    }
    if (this.listening && this.avatar && now >= this.nextListenNod) {
      this.avatar.headAction('nod', { amplitude: 0.12, duration: 0.55 });
      this.log.push('listen_nod');
      this.nextListenNod = now + 2 + Math.random() * 2;
    }
    const k = 1 - Math.exp(-dt / 0.18);
    this.neck.roll += (this.neckTarget.roll - this.neck.roll) * k;
    this.neck.pitch += (this.neckTarget.pitch - this.neck.pitch) * k;
  }
}
