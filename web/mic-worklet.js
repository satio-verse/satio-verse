// 麦克风采集: 把 AudioContext 采样率 (通常 48 kHz) 降到 16 kHz int16, 每 40 ms 发一帧给主线程.
// 降采样用盒式平均, 对 ASR 足够; 浏览器本身的回声消除/降噪在 getUserMedia 那一侧打开.
class MicProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const target = (options.processorOptions && options.processorOptions.targetRate) || 16000;
    this.ratio = sampleRate / target;
    this.frame = Math.round(target * 0.04);
    this.buf = new Int16Array(this.frame);
    this.n = 0;
    this.rest = new Float32Array(0);
    this.t = 0;
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input) return true;
    const data = new Float32Array(this.rest.length + input.length);
    data.set(this.rest);
    data.set(input, this.rest.length);
    let t = this.t;
    while (t + this.ratio <= data.length) {
      const a = Math.floor(t), b = Math.max(a + 1, Math.floor(t + this.ratio));
      let s = 0;
      for (let i = a; i < b; i++) s += data[i];
      const v = s / (b - a);
      this.buf[this.n++] = Math.max(-32768, Math.min(32767, Math.round(v * 32767)));
      if (this.n === this.frame) {
        this.port.postMessage(this.buf.buffer, [this.buf.buffer]);
        this.buf = new Int16Array(this.frame);
        this.n = 0;
      }
      t += this.ratio;
    }
    const used = Math.floor(t);
    this.rest = data.slice(used);
    this.t = t - used;
    return true;
  }
}

registerProcessor('mic-processor', MicProcessor);
