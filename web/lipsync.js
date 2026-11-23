// 音频驱动口型: 从最终输出的 PCM 直接算出 VRM 的 aa/ih/ou/ee/oh 权重.
// 每 20 ms 一帧: 响度决定张嘴程度, 5 个频带的能量分布和元音模板比对决定口型.
// 不依赖 TTS 的音素时间戳, 所以换 TTS、接不接 RVC 都能对齐.

export const HOP = 0.02;
const EDGES = [250, 600, 1000, 1500, 2600, 4000];
// 各元音在 5 个频带上的典型能量占比 (粗略, 按成年女声共振峰估计).
const TEMPLATES = {
  aa: [0.10, 0.40, 0.35, 0.10, 0.05],
  ih: [0.45, 0.05, 0.05, 0.30, 0.15],
  ou: [0.55, 0.35, 0.05, 0.03, 0.02],
  ee: [0.25, 0.25, 0.10, 0.30, 0.10],
  oh: [0.25, 0.50, 0.15, 0.05, 0.05],
};
const NAMES = Object.keys(TEMPLATES);
export const ZERO = Object.freeze({ aa: 0, ih: 0, ou: 0, ee: 0, oh: 0, level: 0 });

const windows = new Map();
function hann(n) {
  if (!windows.has(n)) {
    const w = new Float32Array(n);
    for (let i = 0; i < n; i++) w[i] = 0.5 - 0.5 * Math.cos((2 * Math.PI * i) / (n - 1));
    windows.set(n, w);
  }
  return windows.get(n);
}

function fft(re, im) {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      [re[i], re[j]] = [re[j], re[i]];
      [im[i], im[j]] = [im[j], im[i]];
    }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const ang = (-2 * Math.PI) / len, wr = Math.cos(ang), wi = Math.sin(ang), half = len >> 1;
    for (let i = 0; i < n; i += len) {
      let cr = 1, ci = 0;
      for (let k = 0; k < half; k++) {
        const a = i + k, b = a + half;
        const tr = re[b] * cr - im[b] * ci, ti = re[b] * ci + im[b] * cr;
        re[b] = re[a] - tr; im[b] = im[a] - ti; re[a] += tr; im[a] += ti;
        const ncr = cr * wr - ci * wi;
        ci = cr * wi + ci * wr;
        cr = ncr;
      }
    }
  }
}

/** samples: Float32Array (-1..1). 返回每 HOP 秒一帧的口型权重. */
export function analyzeVisemes(samples, sampleRate) {
  const hop = Math.max(1, Math.round(sampleRate * HOP));
  let n = 256;
  while (n < sampleRate * 0.03) n <<= 1;
  const win = hann(n), re = new Float32Array(n), im = new Float32Array(n);
  const bins = EDGES.map((hz) => Math.min(n / 2, Math.round((hz * n) / sampleRate)));
  const frames = [];
  for (let start = 0; start < samples.length; start += hop) {
    const c = start + (hop >> 1) - (n >> 1);
    let energy = 0;
    for (let i = 0; i < n; i++) {
      const idx = c + i;
      const v = idx >= 0 && idx < samples.length ? samples[idx] : 0;
      energy += v * v;
      re[i] = v * win[i];
      im[i] = 0;
    }
    const db = 20 * Math.log10(Math.sqrt(energy / n) + 1e-9);
    const level = Math.max(0, Math.min(1, (db + 48) / 30));
    if (level <= 0) { frames.push(ZERO); continue; }
    fft(re, im);
    const bands = new Array(5).fill(0);
    let total = 0;
    for (let b = 0; b < 5; b++) {
      for (let k = bins[b]; k < bins[b + 1]; k++) bands[b] += re[k] * re[k] + im[k] * im[k];
      total += bands[b];
    }
    if (total <= 0) { frames.push(ZERO); continue; }
    const scores = NAMES.map((name) => {
      const t = TEMPLATES[name];
      let d = 0;
      for (let b = 0; b < 5; b++) d += (bands[b] / total - t[b]) ** 2;
      return -d / 0.03;
    });
    const max = Math.max(...scores);
    const exps = scores.map((s) => Math.exp(s - max));
    const sum = exps.reduce((s, v) => s + v, 0);
    const frame = { level };
    NAMES.forEach((name, i) => { frame[name] = (exps[i] / sum) * level; });
    frames.push(frame);
  }
  return frames;
}

const clamp01 = (x) => Math.max(0, Math.min(1, x));

/** ARKit 嘴部通道 -> VRoid 的 A/I/U/E/O 五个口型. 归一化参数按 A2F (Mark) 说中文时的分布定:
 *  jawOpen 中位 0.1、九成 0.3; 圆唇 (funnel/pucker) 九成 0.4; 嘴角横拉 (stretch) 静止时就有 0.15 左右. */
export function arkitToVisemes(s) {
  const open = clamp01((s.jawOpen - 0.6 * s.mouthClose - 0.03) / 0.35);
  const round = clamp01((Math.max(s.mouthFunnel, s.mouthPucker) - 0.05) / 0.4);
  // 嘴角上扬 (smile) 不算横拉: 开心时 A2F 的 smile 一直在 0.4 左右, 笑容已由情绪引擎画, 算进来会满嘴 E/I.
  const wide = clamp01(((s.mouthStretchLeft + s.mouthStretchRight) / 2 - 0.15) / 0.3);
  const part = clamp01((s.jawOpen - 0.02) / 0.06);  // 双唇分开了才有 I 口型
  return {
    aa: open * (1 - round) * (1 - 0.5 * wide),
    oh: open * round,
    ou: round * (1 - open) * 0.9,
    ee: open * wide * (1 - round),
    ih: wide * (1 - round) * (1 - open) * part,
  };
}
