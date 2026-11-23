// 评估专用: 实际选中的片段 + 每帧执行轨迹摘要. 不读取 seed, 不改变选片/动作.
// 摘要按 1e-6 量化根部、最终标准化骨骼局部变换和物件变换; 用于同批去重, 不是统计独立性或密码学证明.
export const COVERAGE_VERSION = 1;
export const TRAJECTORY_SCHEME = 'pose-items-dual-fnv32-q1e6-v1';

export class MotionCoverage {
  constructor(mode = 'normal') {
    this.mode = mode;
    this.frames = 0;
    this.h1 = 0x811c9dc5;
    this.h2 = 0x9e3779b9;
    this.clips = new Map();
    this.sequence = [];
    this.last = null;
  }

  _hash(s) {
    for (let i = 0; i < s.length; i++) {
      const c = s.charCodeAt(i);
      this.h1 = Math.imul(this.h1 ^ c, 0x01000193);
      this.h2 = Math.imul(this.h2 ^ c, 0x5bd1e995);
    }
  }

  // 在 step 前后都看: 单帧结束的片段不能因 action 在 step 内清空而漏记.
  observe(L) {
    const a = L.action, idle = L.idle;
    let c, kind, library, frame;
    if (a) {
      c = a.c; kind = a.grab?.kind || (a.seat ? (a.rise ? 'stand' : 'sit') : 'act');
      library = 'actions'; frame = a.frame;
    } else if (L.reaching) {
      c = { name: 'reach', start: null, end: null }; kind = L.reaching.kind || 'reach';
      library = 'procedural'; frame = null;
    } else if (idle) {
      c = idle.db?.clips?.[idle.clip]; kind = idle.kind || 'idle';
      library = idle.db === L.gestDB ? 'gestures' : idle.db === L.acts ? 'actions' : 'idle'; frame = idle.frame;
    } else {
      c = L.db?.clips?.[L.db?.clipOf?.[Math.floor(L.frame)]];
      kind = L.mode || 'unknown'; library = `mm:${L.style || 'neutral'}`; frame = L.frame;
    }
    if (!c?.name) { this.last = null; return null; }
    const clip = { library, kind, name: c.name, start: c.start ?? null, end: c.end ?? null };
    const id = JSON.stringify([library, kind, clip.name, clip.start, clip.end]);
    if (!this.clips.has(id)) this.clips.set(id, { id, ...clip });
    if (this.last !== id) { this.sequence.push(id); this.last = id; }
    return [id, frame ?? null];
  }

  push(L, dt) {
    const source = this.observe(L);
    const num = (v) => Number.isFinite(v) ? Math.round(v * 1e6) : null;
    const xyz = (v) => v ? [num(v.x), num(v.y), num(v.z)] : null;
    const quat = (q) => q ? [num(q.x), num(q.y), num(q.z), num(q.w)] : null;
    const bones = Object.keys(L.nodes || {}).sort().map((n) => [n, xyz(L.nodes[n].position), quat(L.nodes[n].quaternion)]);
    const items = Object.keys(L.items || {}).sort().map((n) => {
      const i = L.items[n];
      return [n, i.held || null, xyz(i.mesh?.position), quat(i.mesh?.quaternion)];
    });
    this._hash(JSON.stringify([num(dt), source, [num(L.char?.x), num(L.char?.z), num(L.char?.yaw)], bones, items]) + '\n');
    this.frames++;
  }

  result() {
    const hex = (v) => (v >>> 0).toString(16).padStart(8, '0');
    return { version: COVERAGE_VERSION, mode: this.mode, frames: this.frames,
      trajectory: { scheme: TRAJECTORY_SCHEME, id: `${hex(this.h1)}${hex(this.h2)}` },
      clips: [...this.clips.values()], sequence: this.sequence.slice() };
  }
}
