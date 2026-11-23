// 人体姿态估计 (看用户): MediaPipe Pose Landmarker (full, Apache-2.0) 在浏览器里本地跑 (显卡 WebGL, 不行退回 CPU),
// 摄像头画面不离开这台电脑; 只把归纳出的几个固定字段 (在不在、站/坐、举哪只手、挥手、是否面对镜头) 并进角色状态,
// 发给本机服务端, 进 LLM 的 [当前] 状态行. 预览画面镜像显示并画出骨架.
import { FilesetResolver, PoseLandmarker } from './vendor/mediapipe/vision_bundle.mjs';
import { describe, summarize } from './hpe-core.js';

const EDGES = [[11, 12], [11, 13], [13, 15], [12, 14], [14, 16], [11, 23], [12, 24], [23, 24], [23, 25], [24, 26], [25, 27], [26, 28]];

/** 载入姿态模型 (VIDEO: 摄像头逐帧; IMAGE: 单张图, 自检用). */
export async function createLandmarker(mode = 'VIDEO') {
  const files = await FilesetResolver.forVisionTasks('/static/vendor/mediapipe/wasm');
  const opts = (delegate) => ({ baseOptions: { modelAssetPath: '/hpe/pose_landmarker_full.task', delegate }, runningMode: mode, numPoses: 1 });
  try {
    return await PoseLandmarker.createFromOptions(files, opts('GPU'));
  } catch (err) {
    console.warn('姿态模型: 显卡不可用, 改用 CPU', err);
    return PoseLandmarker.createFromOptions(files, opts('CPU'));
  }
}

export class UserPose {
  constructor({ preview, label }) {
    this.preview = preview;  // <canvas>
    this.label = label;
    this.video = document.createElement('video');
    this.video.playsInline = true;
    this.video.muted = true;
    this.track = { left: [], right: [] };
    this.summary = null;     // 最近一次归纳结果 (发给服务端)
    this.stats = { frames: 0, ms: 0 };
    this.running = false;
  }

  async start() {
    this.landmarker ||= await createLandmarker('VIDEO');
    this.stream = await navigator.mediaDevices.getUserMedia({ video: { width: 640, height: 480 }, audio: false });
    this.video.srcObject = this.stream;
    await this.video.play();
    this.running = true;
    this.preview.hidden = false;
    const loop = () => { if (this.running) { this._frame(); this.raf = requestAnimationFrame(loop); } };
    loop();
  }

  stop() {
    this.running = false;
    cancelAnimationFrame(this.raf);
    this.stream?.getTracks().forEach((t) => t.stop());
    this.preview.hidden = true;
    this.summary = null;
    if (this.label) this.label.textContent = '';
  }

  _frame() {
    if (this.video.readyState < 2) return;
    const t0 = performance.now();
    const res = this.landmarker.detectForVideo(this.video, t0);
    this.stats.frames++;
    this.stats.ms += ((performance.now() - t0) - this.stats.ms) * 0.1;
    const lm = res.landmarks?.[0];
    this.summary = summarize(lm, t0 / 1000, this.track);
    if (this.label) this.label.textContent = `你: ${describe(this.summary)} · ${this.stats.ms.toFixed(0)} ms`;
    this._draw(lm);
  }

  _draw(lm) {
    const c = this.preview, ctx = c.getContext('2d'), W = c.width, H = c.height;
    ctx.save();
    ctx.translate(W, 0);
    ctx.scale(-1, 1);  // 镜像, 像照镜子
    ctx.drawImage(this.video, 0, 0, W, H);
    if (lm) {
      ctx.strokeStyle = '#4fd1a5'; ctx.lineWidth = 3;
      for (const [a, b] of EDGES) {
        if ((lm[a].visibility ?? 1) < 0.5 || (lm[b].visibility ?? 1) < 0.5) continue;
        ctx.beginPath(); ctx.moveTo(lm[a].x * W, lm[a].y * H); ctx.lineTo(lm[b].x * W, lm[b].y * H); ctx.stroke();
      }
    }
    ctx.restore();
  }
}
