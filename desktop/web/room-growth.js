// 动作成长 (docs/roadmap/motion-growth.md) 的日常那一半: 她在小屋里做事时, 每帧用和评估同一把尺子 (web/motion-faults.js)
// 看动作出没出毛病; 出了就记一条"失败片段": 哪种毛病、算动作缺陷还是环境约束、在做什么 (活动、片段名和第几帧、拿着什么、
// 往多高的台面放)、场景、形象、用的是哪一版动作参数, 加上前 1.6 秒的逐帧摘要. 攒着每 20 秒发给服务端 (POST /api/motion/faults,
// 存在用户数据区, 见 wm/motion_growth.py), 每类本事做了几次也一起发 (算失败率; 换了一版参数以后失败率变高就自动退回).
//
// 不设液体、刚体、体感的钩子 (onEvent / onWorld / onFeel 是认知那边的单槽位): 脱手看行动层的 item.dropped, 洒了多少读液体账本的增量.
// 这些是真值 (像痛觉一样的身体自检), 只进动作成长的记录, 不进她的认知 (r.sight) 和提示词.
//
// 每 5 分钟问服务端一次 (POST /api/motion/growth {tick, busy}): 用户在设置里打开了"空闲时自己练"、她不忙、电脑空闲, 服务端才开始调参
// (默认关). 拿着东西时判出动作缺陷: r.body.sense('右手', 'jerk') —— 她感觉到手抖了一下, 认知那边那一段的后果不拿去学端法.
// 换上新一版动作参数 / 退回时, 等她闲下来在状态栏说一句 ("我最近把拿杯子练稳了"), 告诉服务端说过了.
import { CONTACT } from '/static/contact.js';
import { loadTunedParams, TUNE } from '/static/locomotion.js';
import { FaultDetector, Sensor } from '/static/motion-faults.js';
import { applyParams } from '/static/motion-params.js';
import { LIMITS } from '/static/pose-filter.js';

// 这个页面上动作参数实际是哪一版 (模块级: 换形象重新进小屋时还在). 换形象时按它去问服务端, 服务端发来新形象该用的完整值,
// 上一个形象那一槽的值不会串过来 (TUNE 这些是模块级的, 不重新载入页面就一直在)
let applied = null;

const SEND_S = 20;       // 攒多久发一次
const TICK_S = 300;      // 多久问一次服务端 (要不要开始练、有没有要说的)
const MAX_QUEUE = 40;    // 发不出去时最多攒这么多条
const MAX_PER_HOUR = 60; // 一小时最多记这么多条 (同一个毛病反复出时不刷屏)
// 手上的毛病 (拿着东西时报"手抖了一下"): 隔空取物、掌心跳、手砸下去、一顿一甩、掌心离东西远、手臂被推开 / 插进身体
const HAND = new Set(['grab.gap', 'hand.jump', 'put.slam', 'held.acc', 'hold.gap', 'arm.push', 'self.pen']);

export class Growth {
  constructor(r) {
    this.r = r;
    this.det = new FaultDetector();
    this.sensor = new Sensor(r.layer);
    this.t = 0;
    this.queue = [];
    this.sent = 0;
    this.hour = { start: Date.now(), n: 0 };
    this.exposure0 = {};
    this.lastSend = 0;
    this.lastTick = -TICK_S + 3;   // 进小屋 3 秒后问第一次 (这个形象有自己那一版参数的话尽快换上; 闲着时才换)
    this.gen = 0;                  // 服务端的删除代次 (重置一次加一); 旧代次攒着的不发
    this.errors = 0;
    this.spilled = r.liquid?.world?.ledger?.spilled ?? 0;
    this.version = 'factory';
    this.session = `${Date.now().toString(36)}-${Math.floor(Math.random() * 1e6).toString(36)}`;
    this.avatar = { name: r.v.info?.name || null, file: new URLSearchParams(location.search).get('avatar') || null };
    // 没指定形象 (用服务端的默认形象): 问一下是哪个文件, 参数按形象各存一版要靠它
    if (!this.avatar.file) {
      fetch('/api/avatars').then((res) => (res.ok ? res.json() : null)).then((d) => { if (d?.default && !this.avatar.file) this.avatar.file = d.default; })
        .catch(() => {});
    }
    this.scene = r.scene?.name || null;
    this.announce = null;
    this.recent = [];  // 最近记下的 (自检看)
    this.marks = [];   // 最近的动作缺陷 {t, kind} (glitches 用)
    if (applied) this.version = applied;
    else loadTunedParams().then((p) => { if (!applied) this.version = p?.growth?.version || 'factory'; }).catch(() => {});
    addEventListener('pagehide', () => this.flush(true));
  }

  /** 每帧 (体感之后, 骨骼是这一帧最终的样子). */
  tick(dt) {
    if (this.errors > 5 || !(dt > 0)) return;
    try {
      this._tick(dt);
    } catch (err) {
      if (++this.errors <= 3) console.warn('动作成长的检测出错', err);
    }
  }

  _tick(dt) {
    const r = this.r, L = r.layer;
    this.t += dt;
    const ledger = r.liquid?.world?.ledger;
    const spillMl = ledger ? Math.max(0, ledger.spilled - this.spilled) : 0;
    if (ledger) this.spilled = ledger.spilled;
    const pain = r.v.kin?.painNow || {};
    // 刚体的抓持在滑: 这一帧的事件 (开了刚体才有, Rapier 载入完之前 layer 是 null; 事件格式见 web/physics/rigid-scene.js 的 tick)
    const slipped = (r.rigid?.layer?.events || []).filter((e) => e.type === 'slip').length;
    const sig = this.sensor.read(this.t, dt, pain, { spillMl, slipped, activity: r.life?.current?.id ?? null, pose: r.poses?.name ?? null });
    for (const f of this.det.push(sig)) this._record(f, L);
    if (this.t - this.lastSend >= SEND_S) this.flush();
    if (this.t - this.lastTick >= TICK_S) { this.lastTick = this.t; this._ask(); }
    if ((this.announce || this.pendingParams) && this._idle()) this._say();
  }

  _record(f, L) {
    if (Date.now() - this.hour.start > 3600e3) this.hour = { start: Date.now(), n: 0 };
    if (++this.hour.n > MAX_PER_HOUR) return;
    // 拿放的是哪一处 (离要拿的东西 / 落点最近的目标), 片段帧、台面高度在信号的 ctx.action 里
    const at = f.ctx.action?.at;
    let target = null, bd = 0.6;
    for (const [id, g] of Object.entries(at ? L.targets || {} : {})) {
      const p = g.place;
      if (!p) continue;
      const d = Math.hypot(p[0] - at[0], p[2] - at[1]);
      if (d < bd) { bd = d; target = id; }
    }
    const rec = {
      v: 1, at: new Date().toISOString(), session: this.session, kind: f.kind, cls: f.cls, cause: f.cause, family: f.family,
      value: f.value, peak: f.peak, thr: f.thr, unit: f.unit, sev: f.sev,
      ctx: { ...f.ctx, goT0: undefined, target, care: L.carryCare ?? TUNE.carryCare ?? null, hand: L.handSpeed ?? TUNE.handSpeed ?? null,
        fps: Math.round(1 / Math.max(1e-3, f.ring.length > 1 ? (f.ring.at(-1).t - f.ring[0].t) / (f.ring.length - 1) : 1 / 30)) },
      scene: this.scene, avatar: this.avatar, params: this.version, ring: f.ring,
    };
    this.recent.push(rec);
    if (this.recent.length > 20) this.recent.shift();
    if (f.cls === 'motion') {
      this.marks.push({ t: this.t, kind: f.cause || f.kind });
      // 拿着东西时手上的动作缺陷 (或者动作缺陷弄洒了、脱手了): 她自己感觉得到手抖了一下 (手没按想的动). 走体感的公开接口,
      // 认知那边收成 felt {part, level: jerk}, 那一段的洒、烫不拿去学端法 (不然会学成"满杯不能端"). 只给部位和这一个词,
      // 不带杯子、液体的真值. 脚打滑、重心这些不是手上的, 不报 (真弄洒了按后果那一条报)
      const side = f.ctx.held?.[0]?.side;
      if (side && (HAND.has(f.kind) || ['spill', 'drop', 'slip'].includes(f.kind))) this.r.body?.sense?.(side === 'left' ? '左手' : '右手', 'jerk');
    }
    if (this.queue.length < MAX_QUEUE) this.queue.push(rec);
    this.r.log?.(`小屋: 动作失败片段 ${f.kind} (${f.cls}${f.cause ? ` <- ${f.cause}` : ''}, ${f.sev} 倍阈值, ${f.ctx.action?.clip || f.ctx.mode})`);
  }

  /** 发给服务端: 失败片段和这段时间每类本事做了几次. beacon: 页面要关了 (用 sendBeacon). */
  flush(beacon = false) {
    this.lastSend = this.t;
    const exp = {};
    for (const [k, n] of Object.entries(this.det.exposure)) if (n - (this.exposure0[k] || 0) > 0) exp[k] = n - (this.exposure0[k] || 0);
    if (!this.queue.length && !Object.keys(exp).length) return;
    const body = JSON.stringify({ session: this.session, params: this.version, avatar: this.avatar, scene: this.scene, gen: this.gen,
      exposure: exp, records: this.queue.splice(0) });
    const sentExp = { ...this.det.exposure };
    if (beacon && navigator.sendBeacon) {
      navigator.sendBeacon('/api/motion/faults', new Blob([body], { type: 'application/json' }));
      this.exposure0 = sentExp;
      return;
    }
    fetch('/api/motion/faults', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body })
      .then((res) => (res.ok ? res.json() : null)).then((d) => {
        if (!d) return;
        this.exposure0 = sentExp;
        this.sent++;
        this._gen(d.gen);
      }).catch(() => {});
  }

  /** 服务端的代次变了 (设置里重置过): 之前攒的都不要了 (服务端也不收). */
  _gen(gen) {
    if (!Number.isFinite(gen) || gen <= this.gen) return;
    this.gen = gen;
    this.queue.length = 0;
    this.exposure0 = { ...this.det.exposure };
    this.recent.length = 0;
    this.marks.length = 0;
  }

  /** 问服务端: 要不要开始练 (它自己看开关和电脑空不空闲)、有没有要说的、参数换了没有. */
  _ask() {
    const body = { tick: true, params: this.version, busy: !!this.r.busy?.(), avatar: this.avatar.file, gen: this.gen };
    fetch('/api/motion/growth', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then((res) => (res.ok ? res.json() : null)).then((s) => {
        if (!s) return;
        this._gen(s.gen);
        if (s.announce?.line) this.announce = s.announce;
        if (s.params?.version && s.params.version !== this.version && s.params.values) this.pendingParams = s.params;
      }).catch(() => {});
  }

  _idle() {
    const r = this.r, L = r.layer;
    return L.mode === 'idle' && !L.action && !L.reaching && !r.poses?.pose && !r.life?.sleeping && !Object.values(L.items || {}).some((i) => i.held);
  }

  /** 闲下来的时候: 换上服务端那边新的一版参数 (不在动作当中换, 免得一下子跳), 说一句. */
  _say() {
    const r = this.r;
    if (this.pendingParams) {
      this.flush();  // 攒着的失败片段和曝光记在旧版本名下 (发的时候按 this.version 记)
      applyParams({ tune: TUNE, filter: LIMITS, contact: CONTACT }, this.pendingParams.values);
      this.version = applied = this.pendingParams.version;
      this.pendingParams = null;
    }
    const a = this.announce;
    this.announce = null;
    if (!a) return;
    r.status?.(a.line);
    r.log?.(`小屋: 动作成长 ${a.id}: ${a.line}`);
    fetch('/api/motion/growth', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ ack: a.id }) }).catch(() => {});
  }

  /**
   * 最近 secs 秒里有没有动作缺陷 (身体出了毛病, 不是她的做法不对): 给认知那边, 这一下的后果别当成经验学
   * (比如放下时手砸下去洒了, 不该学成"满杯不能端"). 只给种类 (['put.slam']), 不给数值和真值; 没有返回 [].
   */
  glitches(secs = 3) {
    while (this.marks.length && this.t - this.marks[0].t > 60) this.marks.shift();
    return [...new Set(this.marks.filter((m) => this.t - m.t <= secs).map((m) => m.kind))];
  }

  /** 自检看. */
  snapshot() {
    return { t: +this.t.toFixed(1), version: this.version, exposure: { ...this.det.exposure }, queued: this.queue.length, sent: this.sent,
      seen: this.det.faultsSeen, recent: this.recent.slice(-8).map((x) => ({ kind: x.kind, cls: x.cls, cause: x.cause, sev: x.sev,
        act: x.ctx.action?.clip || x.ctx.mode, activity: x.ctx.activity })) };
  }
}
