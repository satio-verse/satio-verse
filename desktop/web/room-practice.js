// 练习: 同一个动作做多了越做越好 (用户要的: 执行好了有奖励, 执行差了有负反馈, 让她想想怎么做得更好).
// 每个技能 (坐到某张椅子上、蹲下够矮处、树式、前屈、躺到床上…) 有几个能调的参数 (座面高低、蹲多深、重心往哪边挪、头躺在哪…).
// 每做一次: 在现在最好的参数附近随机试一组 (所以每次做法都有点不一样), 做完按体感 (room-body.js 的分数: 疼、摔、穿模、
// 关节、失衡、座面) 打分; 比平时好就把这一组当新的最好, 步子放大一点接着试, 不如平时就步子收小 —— (1+1) 进化策略, 1/5 成功率法则.
// 分数平滑着记 (一次运气好不算数), 按技能存在本机存储里, 下次打开接着练. 每次做完她说一句心得 ("这次稳多了"、"还是不太对").
const KEY = 'pet.room.practice';
const gauss = () => { let u = 0, v = 0; while (!u) u = Math.random(); while (!v) v = Math.random(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); };
const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
const pick = (list) => list[Math.floor(Math.random() * list.length)];

function readStore() {
  try { return JSON.parse(localStorage.getItem(KEY) || 'null') || {}; } catch { return {}; }
}

export class Practice {
  constructor() {
    this.skills = readStore();
    this.history = [];  // 这次打开以后的每一次 (自检看)
    // 试的步子: 平时做事只在学会的做法附近稍微变一变 (每次不完全一样, 但不冒险); 去健身区练的时候 (train) 放开了试
    this.scale = 0.35;
  }

  /**
   * 开始一次: spec = { 参数名: [最小, 最大, 起始] }. 返回 { params, finish(结果) }; finish 收体感的汇总 (分数 score), 返回心得.
   * explore: 这次要不要试新的 (false 就用现在最好的, 比如赶时间时).
   */
  attempt(name, spec, { explore = true } = {}) {
    const k = this.skills[name] ??= { mu: {}, sigma: 0.25, n: 0, avg: null, best: null, wins: 0 };
    for (const [p, [lo, hi, init]] of Object.entries(spec)) if (!Number.isFinite(k.mu[p])) k.mu[p] = clamp(init, lo, hi);
    const params = {};
    for (const [p, [lo, hi]] of Object.entries(spec)) {
      params[p] = explore && k.n > 0 ? clamp(k.mu[p] + gauss() * k.sigma * this.scale * (hi - lo), lo, hi) : k.mu[p];
    }
    return {
      params,
      finish: (result) => this._finish(name, k, params, result),
    };
  }

  _finish(name, k, params, result) {
    const score = result?.score ?? 0;
    const first = k.avg === null;
    const better = first || score > k.avg + 0.02;
    if (better) {
      Object.assign(k.mu, params);
      k.wins++;
      k.sigma = Math.min(0.45, k.sigma * 1.25);
    } else {
      k.sigma = Math.max(0.04, k.sigma * 0.9);
    }
    k.avg = first ? score : k.avg + (score - k.avg) * 0.35;  // 平时的水平 (平滑)
    k.best = k.best === null ? score : Math.max(k.best, score);
    k.n++;
    k.last = +score.toFixed(3);
    this.history.push({ skill: name, n: k.n, score: +score.toFixed(3), avg: +k.avg.toFixed(3), better, params: round(params),
      falls: result?.falls || 0, pain: result?.pain || 0 });
    if (this.history.length > 60) this.history.shift();
    this.save();
    return { better, first, score, avg: k.avg, n: k.n, line: this._line(name, k, result, better, first) };
  }

  /** 做完的心得: 按这次的体感和比平时好不好. */
  _line(name, k, res, better, first) {
    const what = LABEL[name.split('@')[0]] || '这个动作';
    if (res?.falls) return pick([`${what}没站稳摔了…下次重心放稳一点`, `摔得好疼, ${what}得再练练`]);
    if (res?.pain > 0.6) return pick([`${what}的时候撞到了, 下次换个位置`, `哎…${what}还是有点别扭`]);
    if (first) return null;
    if (better && k.n >= 3) return pick([`${what}越来越顺手了`, `这次${what}比上次稳`, `嗯, ${what}这样做舒服多了`]);
    if (!better && Math.random() < 0.3) return pick([`${what}还是不太对, 下次换个法子`, `${what}有点别扭, 再试试`]);
    return null;
  }

  /** 给面板、自检看: 每个技能练了几次、平时多少分. */
  summary() {
    return Object.fromEntries(Object.entries(this.skills).map(([n, k]) => [n, { n: k.n, avg: k.avg === null ? null : +k.avg.toFixed(2),
      best: k.best === null ? null : +k.best.toFixed(2), sigma: +k.sigma.toFixed(2), mu: round(k.mu) }]));
  }

  save() {
    try { localStorage.setItem(KEY, JSON.stringify(this.skills)); } catch { /* 存不了就算了 */ }
  }
}

const round = (o) => Object.fromEntries(Object.entries(o).map(([k, v]) => [k, +v.toFixed(3)]));
const LABEL = { sit: '坐下', crouch: '蹲下拿东西', crouchLow: '蹲下够地上的东西', treeR: '树式', treeL: '树式', fold: '前屈', bend: '侧弯', lie: '躺下', sip: '喝东西' };
