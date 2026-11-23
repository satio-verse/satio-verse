// 不依赖任何模型文件的 2D 兜底形象: 用来直观检查动作衔接、口型和情绪, 不追求美观.
const rad = (d) => (d * Math.PI) / 180;
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));

export class Avatar2D {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.mouth = { aa: 0, ih: 0, ou: 0, ee: 0, oh: 0 };
    this.blink = 0;
    this.nextBlink = 1.5;
    this.blinkT = -1;
    this.time = 0;
    this._colors();
    this._ro = new ResizeObserver(() => this._resize());
    this._ro.observe(canvas);
    this._resize();
    matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => this._colors());
  }

  _colors() {
    const css = getComputedStyle(document.documentElement);
    const v = (name, fallback) => (css.getPropertyValue(name) || fallback).trim();
    this.c = {
      skin: v('--avatar-skin', '#f3d6c4'), hair: v('--avatar-hair', '#4a3a5c'), body: v('--avatar-body', '#5b7bd5'),
      line: v('--avatar-line', '#2a2230'), mouth: v('--avatar-mouth', '#9c3b4a'), blush: v('--avatar-blush', '#f08a9a'),
      bubble: v('--surface-2', '#fff'), text: v('--text-2', '#555'),
    };
  }

  _resize() {
    const dpr = window.devicePixelRatio || 1;
    const { width, height } = this.canvas.getBoundingClientRect();
    this.canvas.width = Math.max(1, Math.round(width * dpr));
    this.canvas.height = Math.max(1, Math.round(height * dpr));
    this.w = width; this.h = height; this.dpr = dpr;
  }

  _smoothMouth(target, dt) {
    for (const k of Object.keys(this.mouth)) {
      const tau = target[k] > this.mouth[k] ? 0.03 : 0.07;
      this.mouth[k] += (target[k] - this.mouth[k]) * (1 - Math.exp(-dt / tau));
    }
  }

  _updateBlink(dt) {
    this.time += dt;
    if (this.blinkT < 0 && this.time > this.nextBlink) { this.blinkT = 0; }
    if (this.blinkT >= 0) {
      this.blinkT += dt;
      this.blink = Math.sin(clamp(this.blinkT / 0.14, 0, 1) * Math.PI);
      if (this.blinkT > 0.14) { this.blinkT = -1; this.blink = 0; this.nextBlink = this.time + 2 + Math.random() * 3; }
    }
  }

  render(ch, face, dt) {
    this._smoothMouth(face.viseme, dt);
    this._updateBlink(dt);
    const g = this.ctx, c = this.c, [v, a] = face.vad;
    g.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    g.clearRect(0, 0, this.w, this.h);
    // 手臂水平伸出时手在中心外 4.2u, 宽度按 8.6u 留边; 高度按 8.5u 放下整个上半身.
    const u = Math.min(this.w / 8.6, this.h / 8.5);
    const x0 = this.w / 2, hipY = this.h * 0.98;
    const lean = clamp(ch.lean, -15, 40);
    const torsoH = 3.4 * u * (1 - Math.max(0, lean) / 110);
    const shY = hipY - torsoH;

    // 躯干
    g.fillStyle = c.body;
    g.beginPath();
    g.roundRect(x0 - 1.35 * u, shY, 2.7 * u, torsoH + u, [0.9 * u, 0.9 * u, 0, 0]);
    g.fill();

    // 手臂: L 是角色自己的左手, 画在观众的右边.
    const arm = (sign, s, e) => {
      const sx = x0 + sign * 1.25 * u, sy = shY + 0.35 * u;
      const l1 = 1.55 * u, l2 = 1.4 * u, f = s + e;
      const ex = sx + sign * Math.sin(rad(s)) * l1, ey = sy + Math.cos(rad(s)) * l1;
      const hx = ex + sign * Math.sin(rad(f)) * l2, hy = ey + Math.cos(rad(f)) * l2;
      g.strokeStyle = c.body; g.lineWidth = 0.55 * u; g.lineCap = 'round'; g.lineJoin = 'round';
      g.beginPath(); g.moveTo(sx, sy); g.lineTo(ex, ey); g.lineTo(hx, hy); g.stroke();
      g.fillStyle = c.skin; g.beginPath(); g.arc(hx, hy, 0.3 * u, 0, Math.PI * 2); g.fill();
    };
    arm(-1, ch.rs, ch.re);
    arm(1, ch.ls, ch.le);

    // 头
    const r = 1.2 * u;
    const hx = x0, hy = shY - 0.25 * u - r + lean * 0.012 * u;
    g.fillStyle = c.skin;
    g.fillRect(x0 - 0.3 * u, shY - 0.5 * u, 0.6 * u, 0.6 * u);
    g.save();
    g.translate(hx, hy);
    g.rotate(rad(ch.hr));
    g.fillStyle = c.skin; g.beginPath(); g.arc(0, 0, r, 0, Math.PI * 2); g.fill();
    const fx = Math.sin(rad(ch.hy)) * r * 0.55, fy = Math.sin(rad(ch.hp + lean * 0.3)) * r * 0.45;
    g.fillStyle = c.hair;
    g.beginPath(); g.arc(0, 0, r * 1.04, Math.PI * 1.02, Math.PI * 1.98); g.quadraticCurveTo(fx, -r * 0.45 + fy * 0.4, -r * 1.02, -r * 0.1); g.fill();

    // 眼睛
    const eyeY = fy - 0.02 * r, eyeDX = 0.38 * r;
    const happyEyes = v > 0.45 && this.blink < 0.5;
    g.strokeStyle = c.line; g.fillStyle = c.line; g.lineWidth = 0.06 * r;
    for (const sx of [-1, 1]) {
      const ex = fx + sx * eyeDX;
      if (happyEyes) {
        g.beginPath(); g.arc(ex, eyeY + 0.05 * r, 0.12 * r, Math.PI * 1.1, Math.PI * 1.9); g.stroke();
      } else {
        const open = Math.max(0.012 * r, 0.13 * r * (1 - this.blink) * (1 + 0.25 * Math.max(0, a - 0.5)));
        g.beginPath(); g.ellipse(ex + fx * 0.08, eyeY, 0.085 * r, open, 0, 0, Math.PI * 2); g.fill();
      }
    }
    // 眉毛: 难过时内侧上扬, 生气时内侧下压, 惊讶时整体上抬.
    const inner = 0.1 * r * Math.max(0, -v) * (1 - Math.max(0, a)) - 0.09 * r * Math.max(0, -v) * Math.max(0, a) + 0.1 * r * Math.max(0, a - 0.55);
    const outer = 0.07 * r * Math.max(0, a - 0.45);
    g.lineWidth = 0.055 * r;
    for (const sx of [-1, 1]) {
      const bx = fx + sx * eyeDX, by = eyeY - 0.27 * r;
      g.beginPath(); g.moveTo(bx - sx * 0.15 * r, by - inner); g.lineTo(bx + sx * 0.15 * r, by - outer); g.stroke();
    }
    // 腮红
    const blush = clamp(Math.max(0, v) * 0.35 + (face.emo === 'shy' ? 0.35 : 0), 0, 0.6);
    if (blush > 0.02) {
      g.globalAlpha = blush; g.fillStyle = c.blush;
      for (const sx of [-1, 1]) { g.beginPath(); g.ellipse(fx + sx * 0.55 * r, eyeY + 0.25 * r, 0.14 * r, 0.07 * r, 0, 0, Math.PI * 2); g.fill(); }
      g.globalAlpha = 1;
    }
    // 嘴: 张合来自 aa/oh/ou, 宽窄来自 ee/ih 和 ou.
    const m = this.mouth;
    const open = clamp(m.aa * 1.0 + m.oh * 0.8 + m.ou * 0.55 + m.ee * 0.35 + m.ih * 0.25, 0, 1);
    const width = 0.42 * r * (1 + 0.45 * m.ee + 0.35 * m.ih - 0.45 * m.ou - 0.2 * m.oh) * (1 + 0.15 * Math.max(0, v));
    const height = 0.36 * r * open;
    const smile = 0.09 * r * v;
    const mx = fx, my = fy + 0.48 * r;
    g.strokeStyle = c.mouth; g.fillStyle = c.mouth; g.lineWidth = 0.05 * r;
    g.beginPath();
    g.moveTo(mx - width / 2, my - smile);
    g.quadraticCurveTo(mx, my - height * 0.35 + smile * 0.4, mx + width / 2, my - smile);
    g.quadraticCurveTo(mx, my + height + smile * 0.6, mx - width / 2, my - smile);
    if (height > 0.02 * r) g.fill(); else g.stroke();
    g.restore();

    // 思考气泡
    if (face.thinking) {
      g.fillStyle = c.bubble; g.strokeStyle = c.text; g.lineWidth = 1;
      const bx = hx + 1.45 * r, by = hy - 1.05 * r;
      for (const [dx, dy, rr] of [[-0.55, 0.75, 0.08], [-0.3, 0.45, 0.13]]) {
        g.beginPath(); g.arc(bx + dx * r, by + dy * r, rr * r, 0, Math.PI * 2); g.fill(); g.stroke();
      }
      g.beginPath(); g.ellipse(bx, by, 0.48 * r, 0.3 * r, 0, 0, Math.PI * 2); g.fill(); g.stroke();
      g.fillStyle = c.text;
      const n = 1 + (Math.floor(this.time * 3) % 3);
      for (let i = 0; i < n; i++) { g.beginPath(); g.arc(bx + (i - 1) * 0.16 * r, by, 0.045 * r, 0, Math.PI * 2); g.fill(); }
    }
  }

  dispose() { this._ro.disconnect(); }
}
