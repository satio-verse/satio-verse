// 实时资源面板: 服务端 (CPU/内存/显卡/本项目进程/外部大户, 来自 /api/resources) + 浏览器端 (帧率、单帧耗时、
// 绘制批次、弹簧关节、姿态平滑层的抑制次数). 每 2 秒刷新一次, 页面在后台时不拉.

const fmt = (x, d = 0) => (x === null || x === undefined || Number.isNaN(x) ? '-' : Number(x).toFixed(d));

export class FrameMeter {
  constructor() { this.frames = 0; this.busy = 0; this.t0 = performance.now(); this.fps = 0; this.ms = 0; }
  /** 每帧调用: busyMs 是这一帧更新 + 渲染花的时间. */
  tick(busyMs) {
    this.frames++;
    this.busy += busyMs;
    const now = performance.now();
    if (now - this.t0 >= 1000) {
      this.fps = (this.frames * 1000) / (now - this.t0);
      this.ms = this.busy / this.frames;
      this.frames = 0; this.busy = 0; this.t0 = now;
    }
  }
}

export class ResourcePanel {
  constructor(root, getClient) {
    this.root = root;
    this.getClient = getClient;
    this.hist = [];
    root.innerHTML = `
      <h3>资源</h3>
      <div class="res-grid" data-res-grid></div>
      <canvas class="res-spark" width="300" height="46" data-res-spark aria-label="最近两分钟的 CPU 和显卡占用"></canvas>
      <div class="muted small" data-res-procs></div>`;
    this.grid = root.querySelector('[data-res-grid]');
    this.spark = root.querySelector('[data-res-spark]');
    this.procs = root.querySelector('[data-res-procs]');
    this.timer = setInterval(() => { if (!document.hidden) this.refresh(); }, 2000);
    this.refresh();
  }

  async refresh() {
    let server = null;
    try { server = await (await fetch('/api/resources')).json(); } catch { /* 服务端不可用时只显示浏览器端 */ }
    const s = server?.latest || {}, g = s.gpu || {}, c = this.getClient() || {};
    const cell = (k, v, warn = false) => `<div class="res-cell${warn ? ' warn' : ''}"><span>${k}</span><b>${v}</b></div>`;
    this.grid.innerHTML = [
      cell('CPU', `${fmt(s.cpu)}%`, s.cpu > 80),
      cell('内存', s.ram_mb ? `${fmt(s.ram_mb / 1024, 1)} / ${fmt(s.ram_total_mb / 1024, 0)} GB` : '-', s.ram_mb / s.ram_total_mb > 0.9),
      cell('显卡', `${fmt(g.util)}%`, g.util > 90),
      cell('显存', g.vram_mb ? `${fmt(g.vram_mb / 1024, 1)} / ${fmt(g.vram_total_mb / 1024, 0)} GB` : '-', g.vram_mb / g.vram_total_mb > 0.9),
      cell('显卡温度', g.temp_c ? `${fmt(g.temp_c)}°C` : '-', g.temp_c > 85),
      cell('帧率', `${fmt(c.fps)} fps`, c.fps && c.fps < 40),
      cell('单帧', `${fmt(c.ms, 1)} ms`, c.ms > 20),
      cell('绘制批次', c.calls ? `${c.calls} / ${fmt(c.tris / 1000)}k 面` : '-'),
      cell('弹簧关节', fmt(c.springs)),
      cell('姿态抑制', c.filterRate !== undefined ? `${fmt(c.filterRate, 1)} 帧/秒` : '-', c.filterRate > 5),
    ].join('');
    const ours = (s.ours || []).map((p) => `${p.self ? '服务' : p.name.replace('.exe', '')} ${fmt(p.cpu)}% ${fmt(p.rss_mb)}MB`).join(' · ');
    const others = (s.others || []).map((p) => `${p.name.replace('.exe', '')} ${fmt(p.cpu)}%`).join(' · ');
    this.procs.textContent = `本项目: ${ours || '-'}` + (others ? ` | 外部大户 (近 ${fmt((s.others_span_s || 0) / 60)} 分钟): ${others}` : '');
    this.drawSpark(server?.history || []);
  }

  drawSpark(hist) {
    const cv = this.spark, ctx = cv.getContext('2d'), W = cv.width, H = cv.height;
    ctx.clearRect(0, 0, W, H);
    if (hist.length < 2) return;
    const css = getComputedStyle(this.root);
    const line = (key, color) => {
      ctx.strokeStyle = color; ctx.lineWidth = 1.5; ctx.beginPath();
      hist.forEach((h, i) => {
        const x = (i / (hist.length - 1)) * (W - 2) + 1, y = H - 2 - ((h[key] ?? 0) / 100) * (H - 4);
        if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y);
      });
      ctx.stroke();
    };
    line('cpu', css.getPropertyValue('--res-cpu').trim() || '#5b8def');
    line('gpu', css.getPropertyValue('--res-gpu').trim() || '#e0784c');
  }
}
