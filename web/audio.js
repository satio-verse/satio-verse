// 音频输出 = 主时钟. 每段 PCM 一到就排进 AudioContext 时间线, 同时算好这段的口型帧,
// 渲染时按"扬声器此刻正在播放的上下文时间"去查口型, 声音和嘴型天然对齐.
// 服务端的面部动画 (A2F, 30 fps 的 ARKit 权重, 每句分几段陆续到) 也按同一个时钟查: 先找此刻在播哪一句的第几秒.
import { analyzeVisemes, HOP, ZERO } from './lipsync.js';

const FIRST_LEAD = 0.03; // 每轮第一段留 30 ms 缓冲, 防止刚排上就欠载

export class AudioOut {
  constructor(ctx) {
    this.ctx = ctx;
    this.gain = ctx.createGain();
    this.gain.connect(ctx.destination);
    this.turn = null;
    this.reset();
  }

  reset() {
    for (const src of this.sources || []) { try { src.stop(); } catch { /* 已停止 */ } }
    this.sources = new Set();
    this.track = [];
    this.faces = new Map();   // seg -> { fps, n (已到的帧数), k, names, w: Uint8Array, final }
    this.segPos = new Map();  // seg -> 已排上的采样数, 算每段在句内的偏移
    this.nextTime = 0;
    this.started = false;
    this.ended = false;
    this.onDrained = null;
  }

  /** 上下文时间 t 被用户听到时对应的 performance.now() 毫秒值. */
  ctxToPerf(t) {
    const ts = this.ctx.getOutputTimestamp ? this.ctx.getOutputTimestamp() : null;
    if (ts && ts.performanceTime > 0) return ts.performanceTime + (t - ts.contextTime) * 1000;
    return performance.now() + (t - this.ctx.currentTime + (this.ctx.outputLatency || 0)) * 1000;
  }

  /** 扬声器此刻正在播放的上下文时间. */
  heardTime() {
    const ts = this.ctx.getOutputTimestamp ? this.ctx.getOutputTimestamp() : null;
    if (ts && ts.performanceTime > 0) return ts.contextTime + (performance.now() - ts.performanceTime) / 1000;
    return this.ctx.currentTime - (this.ctx.outputLatency || 0);
  }

  get playing() { return this.sources.size > 0; }

  /** 排一段 int16 PCM. 返回 { first, startPerf, start, frames, duration }: first 表示这一轮第一段声音,
   *  start 是这段在 AudioContext 时间线上的开始时刻, frames 是它的口型帧 (表演层用来找重音). */
  play(turn, sampleRate, pcm16, seg = 0) {
    if (turn !== this.turn) { this.reset(); this.turn = turn; }
    const f32 = new Float32Array(pcm16.length);
    for (let i = 0; i < pcm16.length; i++) f32[i] = pcm16[i] / 32768;
    const buffer = this.ctx.createBuffer(1, f32.length, sampleRate);
    buffer.copyToChannel(f32, 0);
    const src = this.ctx.createBufferSource();
    src.buffer = buffer;
    src.connect(this.gain);
    const first = !this.started;
    const start = Math.max(this.nextTime, this.ctx.currentTime + (first ? FIRST_LEAD : 0.005));
    src.start(start);
    this.nextTime = start + buffer.duration;
    this.started = true;
    this.sources.add(src);
    src.onended = () => { this.sources.delete(src); this._checkDrained(); };
    const frames = analyzeVisemes(f32, sampleRate);
    const pos = this.segPos.get(seg) || 0;
    this.segPos.set(seg, pos + pcm16.length);
    this.track.push({ t0: start, frames, seg, off: pos / sampleRate, dur: buffer.duration });
    return { first, startPerf: this.ctxToPerf(start), start, frames, duration: buffer.duration };
  }

  /** 服务端说这一轮音频发完了; 本地播放完时回调 (结束时刻, performance 毫秒). */
  endTurn(turn, onDrained) {
    if (turn !== this.turn) { onDrained(this.ctxToPerf(this.ctx.currentTime)); return; }
    this.ended = true;
    this.onDrained = onDrained;
    this._checkDrained();
  }

  _checkDrained() {
    if (this.ended && this.sources.size === 0 && this.onDrained) {
      const cb = this.onDrained;
      this.onDrained = null;
      cb(this.ctxToPerf(this.nextTime));
    }
  }

  stop() {
    const turn = this.turn;
    this.reset();
    this.turn = turn;
  }

  /** 换了一条连接 (服务端新开的会话, 轮号从 1 重新数): 不再认上一条连接的那一轮 —— 新的一轮从头排, 哪怕轮号正好一样;
   *  那一轮的声音还在放就放完, 但"放完了"现在就回调, 不拖到新会话再用这个轮号以后 (2026-11-08 重连吞头几轮那次加的). */
  forgetTurn() {
    const drained = this.onDrained;
    this.turn = null;
    this.ended = false;
    this.onDrained = null;
    drained?.(this.ctxToPerf(this.ctx.currentTime));
  }

  /** 服务端算好的一段面部动画 (这一句从第 start 帧起的 n 帧), 接到这一句已有的帧后面. 只收当前这一轮的. */
  addFace(turn, msg) {
    if (turn !== this.turn) return false;
    const start = msg.start || 0, k = msg.names.length;
    let f = this.faces.get(msg.seg);
    if (!f || start === 0) f = { fps: msg.fps, n: 0, k, names: msg.names, w: new Uint8Array(0), final: false };
    if (start !== f.n) return false;  // 服务端按顺序发, 对不上就是丢了一段, 不拼
    const raw = atob(msg.w);
    const w = new Uint8Array((f.n + msg.n) * k);
    w.set(f.w);
    for (let i = 0; i < raw.length; i++) w[f.n * k + i] = raw.charCodeAt(i);
    f.w = w;
    f.n += msg.n;
    f.final = msg.final !== false;
    this.faces.set(msg.seg, f);
    return true;
  }

  /** 此刻听到的那一帧面部动画: { names, values } (0..1, 帧间线性插值); 这一刻的数据还没到或没在说话时返回 null. */
  faceAt(lookahead = 0.015) {
    if (!this.faces.size) return null;
    const t = this.heardTime() + lookahead;
    for (const c of this.track) {
      if (t < c.t0 || t >= c.t0 + c.dur) continue;
      const f = this.faces.get(c.seg);
      if (!f || !f.n) return null;
      let x = (c.off + t - c.t0) * f.fps;
      if (x > f.n - 1) {
        if (!f.final) return null;  // 后面的帧还在算
        x = f.n - 1;
      }
      const i = Math.floor(x), a = x - i, j = Math.min(i + 1, f.n - 1);
      if (!this._face || this._face.length !== f.k) this._face = new Float32Array(f.k);
      for (let ch = 0; ch < f.k; ch++) this._face[ch] = ((1 - a) * f.w[i * f.k + ch] + a * f.w[j * f.k + ch]) / 255;
      return { names: f.names, values: this._face };
    }
    return null;
  }

  /** 句内第 t 秒在 AudioContext 时间线上的时刻; 那段音频还没排进来时返回 null. */
  segTime(seg, t) {
    for (const c of this.track) if (c.seg === seg && t >= c.off - 1e-6 && t < c.off + c.dur + 0.05) return c.t0 + (t - c.off);
    return null;
  }

  /** 取当前口型. lookahead 抵消渲染端平滑带来的滞后. */
  viseme(lookahead = 0.02) {
    const t = this.heardTime() + lookahead;
    while (this.track.length && t > this.track[0].t0 + this.track[0].frames.length * HOP + 1) this.track.shift();
    for (const seg of this.track) {
      const idx = Math.floor((t - seg.t0) / HOP);
      if (idx >= 0 && idx < seg.frames.length) return seg.frames[idx];
    }
    return ZERO;
  }
}

/** 浏览器那三样处理 (回声消除、降噪、自动增益) 默认都开; processing 里给了的照它 (服务端在 hello 的 mic 里给, 见 wm/echo.py).
 * 服务端能拿系统声音回环去掉外放回声时, 让浏览器别降噪、别自动调音量: 它们夹在"喇叭到麦克风"和服务端的回声消除之间,
 * 把回声变得不再是参考的线性函数, 消不干净 (2026-11-11 真机: 固定的线性滤波器在这样的信号上最多也只消得掉约 12 dB). */
export function micConstraints(processing = null) {
  const audio = { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  for (const key of ['echoCancellation', 'noiseSuppression', 'autoGainControl']) {
    const v = processing?.[key];
    if (typeof v === 'boolean' || (key === 'echoCancellation' && typeof v === 'string')) audio[key] = v;
  }
  return { audio };
}

/** 麦克风实际用上的处理 (浏览器不一定照要的给): 报给服务端, 它据此决定要不要自己补音量. */
export function micSettings(track) {
  const s = track?.getSettings?.() || {};
  const out = {};
  for (const key of ['echoCancellation', 'noiseSuppression', 'autoGainControl', 'sampleRate', 'channelCount']) {
    if (s[key] !== undefined) out[key] = s[key];
  }
  return out;
}

export class MicIn {
  constructor(ctx, onFrame) {
    this.ctx = ctx;
    this.onFrame = onFrame;
    this.node = null;
    this.stream = null;
    this.level = -100;
    this.processing = null;  // 这次开麦克风时要的处理 (micConstraints 的参数)
    this.settings = {};      // 浏览器实际给的
  }

  async start(targetRate = 16000, processing = null) {
    await this.ctx.audioWorklet.addModule('/static/mic-worklet.js');
    this.processing = processing;
    this.stream = await navigator.mediaDevices.getUserMedia(micConstraints(processing));
    this.settings = micSettings(this.stream.getAudioTracks()[0]);
    const source = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, 'mic-processor', { processorOptions: { targetRate } });
    this.node.port.onmessage = (e) => {
      const pcm = new Int16Array(e.data);
      let s = 0;
      for (let i = 0; i < pcm.length; i++) s += pcm[i] * pcm[i];
      this.level = 20 * Math.log10(Math.sqrt(s / pcm.length) / 32768 + 1e-9);
      this.onFrame(pcm);
    };
    source.connect(this.node);
    // worklet 需要接到图里才会被调度; 用静音增益接到输出, 不会听到自己.
    const mute = this.ctx.createGain();
    mute.gain.value = 0;
    this.node.connect(mute).connect(this.ctx.destination);
  }

  stop() {
    if (this.node) { this.node.port.onmessage = null; this.node.disconnect(); this.node = null; }
    if (this.stream) { this.stream.getTracks().forEach((t) => t.stop()); this.stream = null; }
    this.level = -100;
  }

  get active() { return !!this.node; }
}
