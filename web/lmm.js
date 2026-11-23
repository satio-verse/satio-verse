// R1a: Learned Motion Matching 式的动作来源 (docs/roadmap/motion-model.md; 训练见 tools/train_lmm.py). 思路参考 Holden 等人的
// Learned Motion Matching (2020) 与 orangeduck/Motion-Matching (MIT) 的公开实现, 代码是重新写的.
//
// 两个和动作库同接口的对象, 换进行动层 (loco.dbs.neutral / loco.db / loco.idleDB) 以后行动层照常用: mm.js 的 samplePose、
// search 看到库对象自带 sample / search 就交给它, 其余 (片段表、匹配特征 feats、触地 contacts、meta) 照原样读数组.
//   LMMNeutral: 运动匹配. 搜索 = 投影器 (查询 -> 匹配特征 x 和潜变量 z); 播放 = 步进器一帧一帧往前推, 解压器出姿态.
//     帧号是一个小窗口里的虚拟帧号: 几个槽轮流用, 每个槽是一段"片段", 每次搜索占一个新槽 (正在播的那个不动);
//     播到槽尾时行动层自己会强制再搜一次. 用到哪一帧才推到哪一帧、解压哪一帧.
//   LMMIdle: 待机库. 帧号、片段和原来的库一样; 每 K 帧存一个潜变量关键帧, 取哪一帧就插值一次、解压一次.
//     触地标记照抄原库 (很小, 行动层的 _liftSoon 直接往后读).
// 权重是 16 位整数加每行一个比例, 运行时不展开成 Float32. 不依赖 three.js, 可以在 Node 里跑 (tools/lmm_check.mjs).
import { qnlerp } from './mm.js';

const HEAD_FORMAT = 'wm-lmm';

/** 解析 tools/train_lmm.py 导出的文件: [头长 u32][JSON 头][补齐到 4 字节][数据]. 返回 {head, view(ref), bytes}. */
export function parseLMM(buf) {
  const hl = new DataView(buf).getUint32(0, true);
  const head = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, hl)));
  if (head.format !== HEAD_FORMAT) throw new Error(`不是动作网络文件 (${head.format})`);
  const base = 4 + hl + ((4 - ((4 + hl) % 4)) % 4);
  const view = (r) => (r.type === 'i16' ? new Int16Array(buf, base + r.off, r.n)
    : r.type === 'u8' ? new Uint8Array(buf, base + r.off, r.n) : r.type === 'i8' ? new Int8Array(buf, base + r.off, r.n)
      : new Float32Array(buf, base + r.off, r.n));
  return { head, view, bytes: buf.byteLength };
}

/** 多层感知机: 每层 out = b + s * (W_int16 · x), 隐藏层后接 ELU 或 ReLU. */
export class Net {
  constructor(spec, view) {
    this.act = spec.act;
    this.layers = spec.layers.map((l) => ({ nin: l.nin, nout: l.nout, w: view(l.w), s: view(l.s), b: view(l.b) }));
    const wide = Math.max(...this.layers.map((l) => l.nout));
    this.a = new Float32Array(wide);
    this.b = new Float32Array(wide);
    this.nin = this.layers[0].nin;
    this.nout = this.layers[this.layers.length - 1].nout;
  }

  run(x, out = new Float32Array(this.nout)) {
    const last = this.layers.length - 1, elu = this.act === 'elu';
    let inp = x;
    for (let li = 0; li <= last; li++) {
      const L = this.layers[li], o = li === last ? out : li % 2 ? this.b : this.a, { w, s, b, nin, nout } = L;
      for (let r = 0; r < nout; r++) {
        let acc = 0;
        const row = r * nin;
        for (let i = 0; i < nin; i++) acc += w[row + i] * inp[i];
        let v = b[r] + acc * s[r];
        if (li < last) v = v > 0 ? v : elu ? Math.exp(v) - 1 : 0;
        o[r] = v;
      }
      inp = o;
    }
    return out;
  }
}

/** 解压器的输出 (标准化的) -> 骨骼四元数 [x,y,z,w] (写进 quats 的 o 处起)、髋、根. y: 输出, mean/std: 标准化参数. */
function decodePose(y, mean, std, B, quats, o, hips, root, ho) {
  for (let b = 0; b < B; b++) {
    const k = b * 6;
    const ax = y[k] * std[k] + mean[k], ay = y[k + 1] * std[k + 1] + mean[k + 1], az = y[k + 2] * std[k + 2] + mean[k + 2];
    let cx = y[k + 3] * std[k + 3] + mean[k + 3], cy = y[k + 4] * std[k + 4] + mean[k + 4], cz = y[k + 5] * std[k + 5] + mean[k + 5];
    // Gram-Schmidt: 第 0 列 b1, 第 1 列 b2, 第 2 列 b3 = b1 x b2
    const n1 = Math.hypot(ax, ay, az) || 1, x1 = ax / n1, y1 = ay / n1, z1 = az / n1;
    const d = x1 * cx + y1 * cy + z1 * cz;
    cx -= d * x1; cy -= d * y1; cz -= d * z1;
    const n2 = Math.hypot(cx, cy, cz) || 1, x2 = cx / n2, y2 = cy / n2, z2 = cz / n2;
    const x3 = y1 * z2 - z1 * y2, y3 = z1 * x2 - x1 * z2, z3 = x1 * y2 - y1 * x2;
    // 矩阵 m[行][列]: 列是 b1, b2, b3. 和 build_motion_db.py 的 mat_to_quat 同一种算法 (w >= 0)
    const m00 = x1, m10 = y1, m20 = z1, m01 = x2, m11 = y2, m21 = z2, m02 = x3, m12 = y3, m22 = z3;
    let qw = Math.sqrt(Math.max(0, 1 + m00 + m11 + m22)) / 2;
    let qx = Math.sqrt(Math.max(0, 1 + m00 - m11 - m22)) / 2, qy = Math.sqrt(Math.max(0, 1 - m00 + m11 - m22)) / 2;
    let qz = Math.sqrt(Math.max(0, 1 - m00 - m11 + m22)) / 2;
    qx = m21 - m12 < 0 ? -qx : qx; qy = m02 - m20 < 0 ? -qy : qy; qz = m10 - m01 < 0 ? -qz : qz;
    const n = Math.hypot(qx, qy, qz, qw) || 1;
    const q = o + b * 4;
    quats[q] = qx / n; quats[q + 1] = qy / n; quats[q + 2] = qz / n; quats[q + 3] = qw / n;
  }
  const k = B * 6;
  for (let i = 0; i < 3; i++) {
    hips[ho + i] = y[k + i] * std[k + i] + mean[k + i];
    root[ho + i] = y[k + 3 + i] * std[k + 3 + i] + mean[k + 3 + i];
  }
}

/** 中性风格的运动匹配库. slots 个槽, 每槽 slotLen 帧. */
export class LMMNeutral {
  constructor({ head, view, bytes }, { slots = 4, slotLen = 256, costBias = 0, contactGate = false, idleSlot = true } = {}) {
    const B = head.bones.length, NZ = head.nz;
    this.head = head;
    // 投影结果的代价加多少再报给行动层: 投影器的近似让"换段"比原库勤 (每次搜索 67% vs 41%), 加一点让它少换
    this.costBias = costBias;
    // 换段要求触地一致 (R1a 定位轮): 候选状态解出来的两只脚着地和当前播的帧不一样就报无穷大的代价, 行动层不换
    // (到段尾、换库时行动层不看代价, 照样换). 离线: 碎成短于 4 帧的触地段 6.8% -> 2.2%, 换段次数只少 5%
    this.contactGate = contactGate;
    this._lastC = null;
    // 第 0 个槽固定放"待机帧" (2026-10-28): 行动层离开待机、动作做完交回走路时从库的待机帧 (_idleFrame) 起播, 网络版没有待机段,
    // 原来落到第 0 个槽的第一帧, 那是上一次搜索留下的状态 (可能是半步走路), 跟前面跑过什么有关. 现在装网络时把原库的待机帧
    // 投影过来放进第 0 个槽 (seedIdle), 搜索只轮换其余的槽. idleSlot: false 是原来的做法 (复现旧结果用)
    this.idleSlot = idleSlot;
    this.fileBytes = bytes;
    this.B = B; this.NZ = NZ; this.fps = head.fps; this.bones = head.bones;
    this.D = new Net(head.nets.decompressor, view);
    this.S = new Net(head.nets.stepper, view);
    this.P = new Net(head.nets.projector, view);
    // 触地分类器 (R1a 第四版起): 输入就是步进器的状态, 在投影、步进出来的状态上训的; 没有就用解压器自带的触地
    this.K = head.nets.contact ? new Net(head.nets.contact, view) : null;
    this._kout = new Float32Array(2);
    const n = head.norms;
    this.yMean = view(n.y_mean); this.yStd = view(n.y_std); this.zMean = view(n.z_mean); this.zStd = view(n.z_std);
    this.dMean = view(n.d_mean); this.dStd = view(n.d_std);
    this.mean = Float32Array.from(head.meta.featureMean);
    this.scale = Float32Array.from(head.meta.featureScale);
    this.L = slotLen; this.nslots = slots;
    const Fw = slots * slotLen;
    this.F = Fw;
    this.clips = Array.from({ length: slots }, (_, i) => ({ name: `lmm${i}`, kind: 'LMM', start: i * slotLen, end: (i + 1) * slotLen }));
    this.clipOf = new Uint8Array(Fw);
    for (let i = 0; i < slots; i++) this.clipOf.fill(i, i * slotLen, (i + 1) * slotLen);
    this.meta = { ...head.meta, bones: head.bones, fps: head.fps, frames: Fw, clips: this.clips, lmm: true };
    this.feats = new Float32Array(Fw * 27);       // 匹配特征 (行动层的 buildQuery、frameCost 直接读)
    this.state = new Float32Array(Fw * (27 + NZ)); // 步进器的状态 [x, 标准化的 z]
    this.quats = new Float32Array(Fw * B * 4);
    this.hips = new Float32Array(Fw * 3);
    this.root = new Float32Array(Fw * 3);
    this.contacts = new Uint8Array(Fw * 2);
    this.ready = new Int32Array(slots);            // 每个槽从开头起推到了第几帧 (不含)
    this.decoded = new Uint8Array(Fw);
    this.blocks = [];
    this.next = idleSlot ? 1 : 0;
    this.playing = -1;
    this._in = new Float32Array(27 + NZ);
    this._out = new Float32Array(Math.max(this.D.nout, this.S.nout, this.P.nout));
    this.stats = { steps: 0, decodes: 0, projections: 0, gated: 0 };
  }

  /** 运行时占用: 文件 (权重就是对它的视图) + 窗口里的数组. */
  bytes() {
    const win = [this.feats, this.state, this.quats, this.hips, this.root, this.contacts, this.decoded, this.clipOf]
      .reduce((s, a) => s + a.byteLength, 0);
    return { file: this.fileBytes, window: win, total: this.fileBytes + win };
  }

  _seed(slot, x, zn) {
    const s0 = slot * this.L, W = 27 + this.NZ;
    this.state.set(x, s0 * W);
    this.state.set(zn, s0 * W + 27);
    this.feats.set(x, s0 * 27);
    this.ready[slot] = 1;
    this.decoded.fill(0, s0, s0 + this.L);
  }

  /** 槽里推到第 f 帧 (含). */
  _ensure(f) {
    const slot = this.clipOf[f], s0 = slot * this.L, W = 27 + this.NZ, end = s0 + this.L;
    const want = Math.min(f, end - 1);
    while (s0 + this.ready[slot] <= want) {
      const i = s0 + this.ready[slot] - 1;
      const cur = this.state.subarray(i * W, i * W + W), d = this.S.run(cur, this._out);
      const o = (i + 1) * W;
      for (let k = 0; k < W; k++) this.state[o + k] = cur[k] + d[k] * this.dStd[k] + this.dMean[k];
      this.feats.set(this.state.subarray(o, o + 27), (i + 1) * 27);
      this.ready[slot]++;
      this.stats.steps++;
    }
  }

  _decode(i) {
    if (this.decoded[i]) return;
    const W = 27 + this.NZ, inp = this._in, st = this.state;
    for (let k = 0; k < 27; k++) inp[k] = st[i * W + k];
    for (let k = 0; k < this.NZ; k++) inp[27 + k] = st[i * W + 27 + k] * this.zStd[k] + this.zMean[k];
    const y = this.D.run(inp, this._out), B = this.B;
    decodePose(y, this.yMean, this.yStd, B, this.quats, i * B * 4, this.hips, this.root, i * 3);
    const c = this.K ? this.K.run(st.subarray(i * W, i * W + W), this._kout) : y.subarray(B * 6 + 6, B * 6 + 8);
    this.contacts[i * 2] = c[0] > 0 ? 1 : 0;
    this.contacts[i * 2 + 1] = c[1] > 0 ? 1 : 0;
    this.decoded[i] = 1;
    this.stats.decodes++;
  }

  /** 和 mm.js 的 samplePose 一样: 第 f 帧 (可带小数) 的姿态写进 out, 返回髋部、根部运动、触地. */
  sample(f, out) {
    const i0f = Math.floor(f), slot = this.clipOf[i0f], end = (slot + 1) * this.L;
    this.playing = slot;
    const i0 = Math.min(i0f, end - 1), i1 = Math.min(i0 + 1, end - 1), t = Math.min(1, Math.max(0, f - i0));
    // 多推一帧: 行动层下一次搜索时读当前帧 (四舍五入) 的匹配特征
    this._ensure(Math.min(i1 + 1, end - 1));
    this._decode(i0);
    this._decode(i1);
    const B = this.B, q0 = [0, 0, 0, 1], q1 = [0, 0, 0, 1];
    for (let b = 0; b < B; b++) {
      const a = (i0 * B + b) * 4, c = (i1 * B + b) * 4;
      for (let k = 0; k < 4; k++) { q0[k] = this.quats[a + k]; q1[k] = this.quats[c + k]; }
      out[b] = qnlerp(q0, q1, t, out[b] || [0, 0, 0, 1]);
    }
    const lerp3 = (arr) => [0, 1, 2].map((k) => arr[i0 * 3 + k] * (1 - t) + arr[i1 * 3 + k] * t);
    const n = Math.round(f) <= end - 1 ? Math.round(f) : end - 1;
    if (n !== i0 && n !== i1) this._decode(n);
    this._lastC = [this.contacts[n * 2], this.contacts[n * 2 + 1]];
    return { hips: lerp3(this.hips), root: lerp3(this.root), contacts: this._lastC };
  }

  /** 和 mm.js 的 search 一样返回 {frame, cost}: 投影器给出最近的 [x, z], 放进一个新槽, 返回槽的第一帧. */
  search(query) {
    const y = this.P.run(query, this._out);
    let cost = 0;
    for (let k = 0; k < 27; k++) { const d = query[k] - y[k]; cost += d * d; }
    const lo = this.idleSlot ? 1 : 0, wrap = (s) => (s >= this.nslots ? lo + (s - lo) % (this.nslots - lo) : s);
    let slot = wrap(this.next);
    if (slot === this.playing) slot = wrap(slot + 1);
    this.next = slot + 1;
    this._seed(slot, y.subarray(0, 27), y.subarray(27, 27 + this.NZ));
    this.stats.projections++;
    if (this.contactGate && this._lastC) {
      const i = slot * this.L;
      this._decode(i);
      if (this.contacts[i * 2] !== this._lastC[0] || this.contacts[i * 2 + 1] !== this._lastC[1]) {
        this.stats.gated++;
        return { frame: i, cost: Infinity };
      }
    }
    return { frame: slot * this.L, cost: cost + this.costBias };
  }

  /** 第 0 个槽放待机帧: query 是原库待机帧的匹配特征. 返回 {frame: 0}. */
  seedIdle(query) {
    const y = this.P.run(query, this._out);
    this._seed(0, y.subarray(0, 27), y.subarray(27, 27 + this.NZ));
    this.stats.projections++;
    return { frame: 0 };
  }

  /** 执行评估复位用: 轮换、触地闸门的记忆放回刚装好的样子 (槽里的内容是缓存, 第 0 个槽从同一个状态推, 不用清). 返回待机帧. */
  reset() {
    this.next = this.idleSlot ? 1 : 0;
    this.playing = -1;
    this._lastC = null;
    return 0;
  }
}

/**
 * 第三版 (压缩的匹配库): 搜索、换段和原来的运动匹配一样 (匹配特征量化成 8 位, 精确搜索, 按 16 帧分块剪枝), 姿态由解压器从
 * 潜变量解出来 (每 key 帧存一个 8 位的, 中间插值). 没有步进器、投影器. 帧号用和 LMMNeutral 一样的槽窗口: 每次换段占一个新槽,
 * 槽的第一帧对应原库的某一帧, 往后按原库的帧顺序取 (到原片段尾就是槽尾).
 */
export class LMMMatch {
  constructor({ head, view, bytes }, { slots = 4, slotLen = 64 } = {}) {
    const B = head.bones.length, NZ = head.nz, m = head.meta;
    this.head = head;
    this.fileBytes = bytes;
    this.B = B; this.NZ = NZ; this.fps = head.fps; this.bones = head.bones;
    this.D = new Net(head.nets.decompressor, view);
    this.yMean = view(head.norms.y_mean); this.yStd = view(head.norms.y_std);
    this.mean = Float32Array.from(m.featureMean);
    this.scale = Float32Array.from(m.featureScale);
    this.src = m.clips;
    this.K = m.key;
    this.fq = view(head.featq.q); this.fs = view(head.featq.s);
    this.kq = view(head.keyz8.q); this.ks = view(head.keyz8.s);
    this.srcContacts = view(head.contacts);
    // 根部运动照原库存的 (meta.storedRoot): 解压器解出来的急转会被压平, 转向变钝
    this.rq = head.rootq ? view(head.rootq.q) : null;
    this.rs = head.rootq ? view(head.rootq.s) : null;
    // 可搜索的帧 (未来 1 秒还在同一片段里, 同 mm.js) 按 16 帧分块, 块的上下界 (和特征同样的位数)
    const horizon = Math.max(...m.horizon), blocks = [], Q = this.fq.constructor, big = Q === Int16Array ? 32767 : 127;
    for (const c of this.src) for (let s = c.start; s < c.end - horizon - 1; s += 16) blocks.push([s, Math.min(s + 16, c.end - horizon - 1)]);
    this.blk = new Int32Array(blocks.flat());
    this.lo = new Q(blocks.length * 27).fill(big);
    this.hi = new Q(blocks.length * 27).fill(-big - 1);
    blocks.forEach(([s, e], b) => {
      for (let f = s; f < e; f++) for (let k = 0; k < 27; k++) {
        const v = this.fq[f * 27 + k];
        if (v < this.lo[b * 27 + k]) this.lo[b * 27 + k] = v;
        if (v > this.hi[b * 27 + k]) this.hi[b * 27 + k] = v;
      }
    });
    this.L = slotLen; this.nslots = slots;
    const Fw = slots * slotLen;
    this.F = Fw;
    this.clips = Array.from({ length: slots }, (_, i) => ({ name: `match${i}`, kind: 'LMM', start: i * slotLen, end: (i + 1) * slotLen }));
    this.clipOf = new Uint8Array(Fw);
    for (let i = 0; i < slots; i++) this.clipOf.fill(i, i * slotLen, (i + 1) * slotLen);
    this.meta = { ...m, bones: head.bones, fps: head.fps, frames: Fw, clips: this.clips, lmm: true };
    this.feats = new Float32Array(Fw * 27);
    this.quats = new Float32Array(Fw * B * 4);
    this.hips = new Float32Array(Fw * 3);
    this.root = new Float32Array(Fw * 3);
    this.contacts = new Uint8Array(Fw * 2);
    this.decoded = new Uint8Array(Fw);
    this.origin = new Int32Array(slots);   // 槽的第一帧是原库的哪一帧
    this.ready = new Int32Array(slots);
    this.blocks = [];
    this.next = 0;
    this.playing = -1;
    this.lastF = 0;
    this._in = new Float32Array(27 + NZ);
    this._out = new Float32Array(this.D.nout);
    this.stats = { decodes: 0, searches: 0 };
  }

  bytes() {
    const win = [this.feats, this.quats, this.hips, this.root, this.contacts, this.decoded, this.clipOf, this.lo, this.hi, this.blk]
      .reduce((s, a) => s + a.byteLength, 0);
    return { file: this.fileBytes, window: win, total: this.fileBytes + win };
  }

  _srcClip(f) {
    let lo = 0, hi = this.src.length - 1;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (this.src[m].start <= f) lo = m; else hi = m - 1; }
    return this.src[lo];
  }

  _seed(slot, f) {
    const c = this._srcClip(f), s0 = slot * this.L;
    this.origin[slot] = f;
    this.clips[slot].end = s0 + Math.min(this.L, c.end - f);
    this.ready[slot] = 0;
    this.decoded.fill(0, s0, s0 + this.L);
  }

  /** 槽里第 i 帧 (含) 以前的匹配特征从原库 (8 位) 取过来. */
  _ensure(i) {
    const slot = this.clipOf[i], s0 = slot * this.L, end = this.clips[slot].end;
    const want = Math.min(i, end - 1);
    while (s0 + this.ready[slot] <= want) {
      const w = s0 + this.ready[slot], f = this.origin[slot] + this.ready[slot];
      for (let k = 0; k < 27; k++) this.feats[w * 27 + k] = this.fq[f * 27 + k] * this.fs[k];
      this.contacts[w * 2] = this.srcContacts[f * 2];
      this.contacts[w * 2 + 1] = this.srcContacts[f * 2 + 1];
      this.ready[slot]++;
    }
  }

  _decode(i) {
    if (this.decoded[i]) return;
    const slot = this.clipOf[i], f = this.origin[slot] + (i - slot * this.L), c = this._srcClip(f);
    const j = Math.min(Math.floor((f - c.start) / this.K), Math.max(0, c.nk - 2));
    const k0 = c.start + j * this.K, k1 = j + 1 >= c.nk - 1 ? c.end - 1 : c.start + (j + 1) * this.K;
    const t = c.nk > 1 && k1 > k0 ? (f - k0) / (k1 - k0) : 0;
    const NZ = this.NZ, a = (c.kf + j) * NZ, b = (c.kf + Math.min(j + 1, c.nk - 1)) * NZ, inp = this._in;
    for (let k = 0; k < 27; k++) inp[k] = this.feats[i * 27 + k];
    for (let d = 0; d < NZ; d++) inp[27 + d] = (this.kq[a + d] * (1 - t) + this.kq[b + d] * t) * this.ks[d];
    const y = this.D.run(inp, this._out);
    decodePose(y, this.yMean, this.yStd, this.B, this.quats, i * this.B * 4, this.hips, this.root, i * 3);
    if (this.rq) for (let k = 0; k < 3; k++) this.root[i * 3 + k] = this.rq[f * 3 + k] * this.rs[k];
    this.decoded[i] = 1;
    this.stats.decodes++;
  }

  sample(f, out) {
    const i0f = Math.floor(f), slot = this.clipOf[i0f], end = this.clips[slot].end;
    this.playing = slot;
    this.lastF = f;
    const i0 = Math.min(i0f, end - 1), i1 = Math.min(i0 + 1, end - 1), t = Math.min(1, Math.max(0, f - i0));
    this._ensure(Math.min(i1 + 1, end - 1));
    this._decode(i0);
    this._decode(i1);
    const B = this.B, q0 = [0, 0, 0, 1], q1 = [0, 0, 0, 1];
    for (let b = 0; b < B; b++) {
      const a = (i0 * B + b) * 4, c = (i1 * B + b) * 4;
      for (let k = 0; k < 4; k++) { q0[k] = this.quats[a + k]; q1[k] = this.quats[c + k]; }
      out[b] = qnlerp(q0, q1, t, out[b] || [0, 0, 0, 1]);
    }
    const lerp3 = (arr) => [0, 1, 2].map((k) => arr[i0 * 3 + k] * (1 - t) + arr[i1 * 3 + k] * t);
    const n = Math.round(f) <= end - 1 ? Math.round(f) : end - 1;
    return { hips: lerp3(this.hips), root: lerp3(this.root), contacts: [this.contacts[n * 2], this.contacts[n * 2 + 1]] };
  }

  /** 精确搜索 (8 位特征, 分块剪枝). 找到的是正在播的那段的下几帧时返回同一个槽里的帧号 (行动层当作"差不多就是当前帧", 不换). */
  search(query) {
    const fq = this.fq, fs = this.fs, blk = this.blk, nb = blk.length / 2;
    let best = -1, bestCost = Infinity;
    for (let b = 0; b < nb; b++) {
      let bound = 0;
      for (let k = 0; k < 27 && bound < bestCost; k++) {
        const q = query[k], lo = this.lo[b * 27 + k] * fs[k], hi = this.hi[b * 27 + k] * fs[k];
        const d = q < lo ? lo - q : q > hi ? q - hi : 0;
        bound += d * d;
      }
      if (bound >= bestCost) continue;
      for (let f = blk[b * 2]; f < blk[b * 2 + 1]; f++) {
        let cost = 0;
        const o = f * 27;
        for (let k = 0; k < 27 && cost < bestCost; k++) { const d = query[k] - fq[o + k] * fs[k]; cost += d * d; }
        if (cost < bestCost) { bestCost = cost; best = f; }
      }
    }
    this.stats.searches++;
    if (best < 0) return { frame: -1, cost: Infinity };
    const ps = this.playing;
    if (ps >= 0) {
      const cur = this.origin[ps] + (Math.floor(this.lastF) - ps * this.L), off = best - this.origin[ps];
      if (this._srcClip(best) === this._srcClip(cur) && Math.abs(best - cur) < 4 && off >= 0 && ps * this.L + off < this.clips[ps].end) {
        return { frame: ps * this.L + off, cost: bestCost };
      }
    }
    let slot = this.next % this.nslots;
    if (slot === ps) slot = (slot + 1) % this.nslots;
    this.next = slot + 1;
    this._seed(slot, best);
    return { frame: slot * this.L, cost: bestCost };
  }
}

/** 待机库: 片段、帧号和原库一样, 关键帧潜变量插值后解压. */
export class LMMIdle {
  constructor({ head, view, bytes }) {
    const B = head.bones.length;
    this.head = head;
    this.fileBytes = bytes;
    this.B = B; this.NZ = head.nz; this.fps = head.fps; this.bones = head.bones;
    this.D = new Net(head.nets.decompressor, view);
    this.yMean = view(head.norms.y_mean); this.yStd = view(head.norms.y_std);
    this.clips = head.meta.clips;
    this.groups = head.meta.groups;
    this.F = head.meta.sourceFrames;
    this.meta = { ...head.meta, bones: head.bones, fps: head.fps, frames: this.F, lmm: true };
    this.K = head.meta.key;
    this.kq = view(head.keyz.q);
    this.ks = view(head.keyz.s);
    this.contacts = view(head.contacts);
    this._z = new Float32Array(this.NZ);
    this._y = new Float32Array(this.D.nout);
    this._q = new Float32Array(B * 4);
    this._h = new Float32Array(3);
    this._r = new Float32Array(3);
    this.stats = { decodes: 0 };
  }

  bytes() { return { file: this.fileBytes, window: 0, total: this.fileBytes }; }

  _clip(f) {
    let lo = 0, hi = this.clips.length - 1;
    while (lo < hi) { const m = (lo + hi + 1) >> 1; if (this.clips[m].start <= f) lo = m; else hi = m - 1; }
    return this.clips[lo];
  }

  sample(f, out) {
    const c = this._clip(Math.floor(f)), fc = Math.min(c.end - 1, Math.max(c.start, f));
    const j = Math.min(Math.floor((fc - c.start) / this.K), Math.max(0, c.nk - 2));
    const k0 = c.start + j * this.K, k1 = j + 1 >= c.nk - 1 ? c.end - 1 : c.start + (j + 1) * this.K;
    const t = c.nk > 1 && k1 > k0 ? (fc - k0) / (k1 - k0) : 0;
    const NZ = this.NZ, a = (c.kf + j) * NZ, b = (c.kf + Math.min(j + 1, c.nk - 1)) * NZ;
    for (let d = 0; d < NZ; d++) this._z[d] = (this.kq[a + d] * (1 - t) + this.kq[b + d] * t) * this.ks[d];
    const y = this.D.run(this._z, this._y);
    decodePose(y, this.yMean, this.yStd, this.B, this._q, 0, this._h, this._r, 0);
    for (let bn = 0; bn < this.B; bn++) {
      const q = out[bn] || (out[bn] = [0, 0, 0, 1]);
      for (let k = 0; k < 4; k++) q[k] = this._q[bn * 4 + k];
    }
    this.stats.decodes++;
    const n = Math.min(c.end - 1, Math.round(f));
    return { hips: [this._h[0], this._h[1], this._h[2]], root: [this._r[0], this._r[1], this._r[2]],
      contacts: [this.contacts[n * 2], this.contacts[n * 2 + 1]] };
  }
}

/** 从服务端取 (/motiondb/<名字>.motion 是 <名字>.bin 的别名). */
export async function fetchLMM(name) {
  const r = await fetch(`/motiondb/${name}.motion`);
  if (!r.ok) throw new Error(`没有动作网络 ${name}`);
  return parseLMM(await r.arrayBuffer());
}

/**
 * 把行动层的中性风格库和 (或) 待机库换成网络. 换之前播放到哪一帧, 中性库从那一帧的匹配特征投影过来接着播;
 * 待机正在播的段帧号不变, 直接换库. 返回占用和用时.
 */
export async function installLMM(loco, { neutral = true, idle = true, slots, slotLen, costBias, contactGate, idleSlot } = {}) {
  const t0 = performance.now(), res = {};
  if (neutral) {
    // neutral: true = 第一、二版 (投影器 + 步进器), 'match' = 第三版 (压缩的匹配库)
    const src = loco.dbs.neutral, lmm = neutral === 'match' ? new LMMMatch(await fetchLMM('lmmmatch'), { slots, slotLen })
      : new LMMNeutral(await fetchLMM('lmmneutral'), { slots, slotLen, costBias, contactGate, idleSlot });
    const q = new Float32Array(27);
    if (loco.db === src && src.feats) q.set(src.feats.subarray(Math.round(loco.frame) * 27, Math.round(loco.frame) * 27 + 27));
    let r;
    if (lmm.idleSlot) {
      // 第 0 个槽: 原库的待机帧 (和行动层 _idleFrame 同一帧) 投影过来; 换之前不在待机帧上就再从当前帧搜一次
      const id = src.clips.find((c) => c.kind === 'ID'), fi = id ? id.start + 30 : src.clips[0].start, qi = new Float32Array(27);
      if (src.feats) qi.set(src.feats.subarray(fi * 27, fi * 27 + 27));
      r = lmm.seedIdle(qi);
      if (loco.db === src && Math.round(loco.frame) !== fi) r = lmm.search(q);
    } else r = lmm.search(q);
    if (loco.db === src) { loco.db = lmm; loco.frame = r.frame; }
    if (loco._pendingDB === src) loco._pendingDB = lmm;
    loco.dbs.neutral = lmm;
    res.neutral = lmm.bytes();
  }
  if (idle && loco.idleDB) {
    const old = loco.idleDB, lmm = new LMMIdle(await fetchLMM('lmmidle'));
    loco.idleDB = lmm;
    if (loco.idle?.db === old) loco.idle.db = lmm;
    res.idle = lmm.bytes();
  }
  res.ms = +(performance.now() - t0).toFixed(1);
  return res;
}
