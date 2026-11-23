// 程序化动作 + 惯性化 (inertialization) 衔接.
//
// 每个动作片段只描述"目标姿态随时间怎么变", 不关心从哪里过渡过来.
// 切换片段时记下 旧输出 - 新片段 的位置差和速度差, 让这个差值按临界阻尼衰减到 0:
//   offset(τ) = (x0 + (v0 + ω·x0)·τ)·e^(−ω·τ)
// 新片段从第一帧起就原样播放 (没有低通滤波的相位滞后), 旧姿态也不会跳变.
//
// 通道 (单位: 度): hp 低头为正, hy 转向画面右侧为正, hr 歪头, lean 前倾,
// ls/rs 肩外展 (0 下垂, 90 水平, 180 举过头), le/re 肘部在同一平面继续上折.

import { restorePlain, snapshotPlain } from './plain-state.js';

export const GROUPS = { head: ['hp', 'hy', 'hr'], torso: ['lean'], larm: ['ls', 'le'], rarm: ['rs', 're'] };
const OMEGA = { head: 12, torso: 7, larm: 9, rarm: 9 };
const CHANNELS = Object.values(GROUPS).flat();
const TAU = Math.PI * 2;

const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const smooth01 = (x) => { const t = clamp(x, 0, 1); return t * t * (3 - 2 * t); };
const envelope = (t, dur, attack, release) => smooth01(t / attack) * (1 - smooth01((t - (dur - release)) / release));

// 片段: { groups: {组名: (t, now) => {通道: 值}}, dur, tag }
function clip(groups, dur = Infinity, tag = 'act') { return { groups, dur, tag }; }

function armGroups(side, fn) {
  const out = {};
  if (side === 'L' || side === 'B') out.larm = (t, now) => { const p = fn(t, now); return { ls: p.s, le: p.e }; };
  if (side === 'R' || side === 'B' || !side) out.rarm = (t, now) => { const p = fn(t, now); return { rs: p.s, re: p.e }; };
  return out;
}

function idleClip(state) {
  // 待机随情绪变化: 唤醒度决定动作能量, 支配度决定抬头/挺胸.
  const energy = () => 0.6 + 0.9 * Math.max(0, state.vad[1]);
  const dom = () => state.vad[2];
  return clip({
    head: (t, now) => ({
      hp: -5 * dom() + 1.6 * energy() * Math.sin(TAU * 0.23 * now),
      hy: 3 * energy() * Math.sin(TAU * 0.11 * now + 1),
      hr: 2.2 * energy() * Math.sin(TAU * 0.07 * now + 2),
    }),
    torso: (t, now) => ({ lean: -3 * dom() + 1.2 * Math.sin(TAU * 0.25 * now) }),
    larm: (t, now) => ({ ls: 9 + 2 * energy() * Math.sin(TAU * 0.25 * now + 0.5), le: 12 + 3 * Math.sin(TAU * 0.25 * now) }),
    rarm: (t, now) => ({ rs: 9 + 2 * energy() * Math.sin(TAU * 0.25 * now + 0.9), re: 12 + 3 * Math.sin(TAU * 0.25 * now + 0.4) }),
  }, Infinity, 'idle');
}

const BACKCHANNEL = {
  // 端点一到就做, 不等 LLM: 微微抬眼歪头, 右手托腮.
  think: () => clip({
    head: (t) => ({ hp: -7, hy: 9, hr: -7 + 1.5 * Math.sin(TAU * 0.5 * t) }),
    rarm: (t) => ({ rs: 28, re: 160 }),
  }, Infinity, 'backchannel'),
  listen: () => clip({
    head: (t) => ({ hp: 4, hy: -3, hr: 7 }),
    torso: () => ({ lean: 4 }),
  }, Infinity, 'backchannel'),
};

const SKILLS = {
  wave: (a) => clip(armGroups(a.side, (t) => ({ s: 105 + 15 * a.amp, e: 85 + 28 * Math.sin(TAU * 2.0 * a.spd * t) })), 2.8 / a.spd),
  raise: (a) => clip(armGroups(a.side, (t) => ({ s: 40 + 140 * a.amp, e: 8 + 3 * Math.sin(TAU * 0.7 * t) })), 2.6 / a.spd),
  point: (a) => clip(armGroups(a.side, () => ({ s: 86, e: 4 })), 2.0 / a.spd),
  nod: (a) => {
    const f = 1.6 * a.spd;
    return clip({ head: (t) => ({ hp: 14 * a.amp * Math.sin(Math.PI * f * t) ** 2, hy: 0, hr: 0 }) }, 2 / f);
  },
  shake: (a) => {
    const f = 1.8 * a.spd, dur = 2 / f;
    return clip({ head: (t) => ({ hp: 2, hy: 16 * a.amp * Math.sin(TAU * f * t) * envelope(t, dur, 0.1, 0.25), hr: 0 }) }, dur);
  },
  shrug: (a) => clip({
    larm: () => ({ ls: 26, le: 78 }),
    rarm: () => ({ rs: 26, re: 78 }),
    head: (t) => ({ hp: 3, hy: 0, hr: 9 * a.amp }),
  }, 1.8 / a.spd),
  think: () => clip({
    head: (t) => ({ hp: -7, hy: 9, hr: -7 }),
    rarm: () => ({ rs: 28, re: 160 }),
  }, 3.0),
  bow: (a) => {
    const dur = 2.2 / a.spd;
    return clip({
      torso: (t) => ({ lean: 30 * a.amp * envelope(t, dur, 0.5, 0.6) }),
      head: (t) => ({ hp: 18 * a.amp * envelope(t, dur, 0.5, 0.6), hy: 0, hr: 0 }),
    }, dur);
  },
};

export class MotionSystem {
  constructor() {
    this.state = { vad: [0, 0, 0] };
    this.emotion = { target: [0, 0, 0], setAt: 0 };
    this.idle = idleClip(this.state);
    this.out = Object.fromEntries(CHANNELS.map((c) => [c, 0]));
    this.vel = Object.fromEntries(CHANNELS.map((c) => [c, 0]));
    this.groups = {};
    this.pending = [];
    this.now = 0;
    for (const g of Object.keys(GROUPS)) this.groups[g] = { clip: this.idle, start: 0, offsets: {} };
    // 起始帧直接取待机姿态, 不做过渡.
    for (const [g, chans] of Object.entries(GROUPS)) Object.assign(this.out, this.idle.groups[g](0, 0));
    this._factory = snapshotPlain(this, ['_factory']);  // 出厂状态 (执行评估复位时放回, 见 plain-state.js)
  }

  /** 放回出厂状态 (执行评估复位用): stopAll 只换回待机, 上一段的输出和速度还会经过渡偏移带进来. */
  restoreFactory() {
    if (this._factory) restorePlain(this, this._factory, ['_factory']);
  }

  _sample(group, entry, now) {
    const fn = entry.clip.groups[group];
    return fn(now - entry.start, now);
  }

  _switch(group, clipObj, now) {
    const entry = { clip: clipObj, start: now, offsets: {} };
    const dt = 1 / 120;
    const a = clipObj.groups[group](0, now), b = clipObj.groups[group](dt, now + dt);
    for (const ch of GROUPS[group]) {
      entry.offsets[ch] = { x0: this.out[ch] - a[ch], v0: this.vel[ch] - (b[ch] - a[ch]) / dt };
    }
    this.groups[group] = entry;
  }

  play(clipObj, now) {
    for (const group of Object.keys(clipObj.groups)) this._switch(group, clipObj, now);
  }

  backchannel(kind, now) {
    if (BACKCHANNEL[kind]) this.play(BACKCHANNEL[kind](), now);
  }

  /** 收到意图: 结束起势动作, 按 at 排队执行各个动作. stop 回到待机. */
  applyIntent(op, acts, now) {
    this.pending = [];
    for (const [group, entry] of Object.entries(this.groups)) {
      if (entry.clip.tag === 'backchannel' || op === 'stop') this._switch(group, this.idle, now);
    }
    if (op === 'stop') return;
    for (const act of acts || []) {
      if (!SKILLS[act.do]) continue;
      this.pending.push({ time: now + (act.at || 0), act });
    }
    this.pending.sort((x, y) => x.time - y.time);
  }

  /** 立即做一个动作 (不清掉排队中的其他动作). */
  perform(act, now) {
    if (SKILLS[act.do]) this.play(SKILLS[act.do](act), now);
  }

  stopAll(now) {
    this.pending = [];
    for (const group of Object.keys(GROUPS)) this._switch(group, this.idle, now);
  }

  setEmotion(vad, now) {
    this.emotion = { target: vad.slice(), setAt: now };
  }

  /** speech: 当前说话响度 0..1, 给头部加一点随语音的小幅点动. */
  update(now, dt, speech = 0) {
    this.now = now;
    // 情绪: 目标值按 8 秒半衰期回落, 当前值以 3/s 追赶目标.
    const k = Math.exp(-Math.LN2 * (now - this.emotion.setAt) / 8);
    const follow = 1 - Math.exp(-3 * dt);
    for (let i = 0; i < 3; i++) this.state.vad[i] += (this.emotion.target[i] * k - this.state.vad[i]) * follow;

    while (this.pending.length && this.pending[0].time <= now) {
      const { act } = this.pending.shift();
      this.play(SKILLS[act.do](act), now);
    }
    for (const [group, entry] of Object.entries(this.groups)) {
      if (now - entry.start > entry.clip.dur) this._switch(group, this.idle, now);
    }

    const next = {};
    for (const [group, entry] of Object.entries(this.groups)) {
      const base = this._sample(group, entry, now);
      const tau = now - entry.start, w = OMEGA[group];
      for (const ch of GROUPS[group]) {
        const o = entry.offsets[ch];
        const off = o ? (o.x0 + (o.v0 + w * o.x0) * tau) * Math.exp(-w * tau) : 0;
        next[ch] = base[ch] + off;
      }
    }
    next.hp += 2.5 * speech;
    for (const ch of CHANNELS) {
      this.vel[ch] = dt > 0 ? (next[ch] - this.out[ch]) / dt : 0;
      this.out[ch] = next[ch];
    }
    return { ...this.out };
  }

  get vad() { return this.state.vad; }
  get thinking() { return Object.values(this.groups).some((g) => g.clip.tag === 'backchannel' && g.clip.groups.rarm); }
}
