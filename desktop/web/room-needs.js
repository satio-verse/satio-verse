// 小屋里她的心情和需求 (用户说"加入心情模式, 用户可以投喂饮料、咖啡豆、零食、牛奶"):
//   - 五项需求 0-100: 精力 (困了要睡)、饱腹 (饿了吃零食)、水分 (渴了喝饮料)、开心 (无聊了玩电脑、做瑜伽、看书)、陪伴 (想你了走过来).
//     随真实时间慢慢掉 (每小时几分), 做对应的事补上; 关掉桌宠再打开时按离开了多久补算 (最多算 12 小时).
//   - 心情由需求算出来 (加权平均, 最缺的那项决定说法和表情), 她挑下一件事时按缺多少加权 (见 room-life.js).
//   - 库存: 冰箱里的饮料、咖啡豆 (一包冲几杯)、零食、牛奶. 投喂的东西先落在房间前面的地上, 她走过去捡起来.
//   - 存在本机浏览器存储里 (只是这只桌宠自己的状态, 读写失败就当第一次打开).
import { herDate } from '/pet/tz.js';
import { freshLedger, coffeeAmount, settleLiquid, settlePreparation } from './room-consumption.js';

const KEY = 'pet.room.needs';
const HOUR = 3600;
export const NEEDS = { energy: '精力', food: '饱腹', water: '水分', fun: '开心', social: '陪伴' };
// 每小时掉多少 (醒着); 睡觉时精力每小时回 RESTORE
const DECAY = { energy: 7, food: 9, water: 12, fun: 9, social: 6 };
const RESTORE = 45;
const WEIGHT = { energy: 0.24, food: 0.2, water: 0.2, fun: 0.2, social: 0.16 };
// 投喂: 名字、加进哪样库存、加几份, 她收到时说的话
export const FEED = {
  drink: { name: '饮料', icon: '🥤', stock: 'drinks', add: 1, thanks: ['哇, 饮料! 谢谢～', '正好有点渴了～'] },
  beans: { name: '咖啡豆', icon: '☕', stock: 'beans', add: 4, thanks: ['咖啡豆! 可以冲好几杯了', '闻起来好香～谢谢'] },
  snack: { name: '零食', icon: '🍪', stock: 'snacks', add: 1, thanks: ['零食! 最喜欢你了～', '嘿嘿, 偷偷吃一点'] },
  milk: { name: '牛奶', icon: '🥛', stock: 'milk', add: 1, thanks: ['牛奶～谢谢你', '喝了长高高'] },
};
const STOCK0 = { drinks: 6, beans: 3, snacks: 0, milk: 0 };
// 困倦和说了多少话挂钩 (用户要的: 对话多了她会困, 睡一觉整理完才清醒): 这次醒着以后你们俩说的字数 / LOAD_MAX.
// 服务端对话只留最近几轮, 真正的"上下文"不会满; 这里量的是要整理进长期记忆的那些对话. 6000 字约是一晚上聊天
export const LOAD_MAX = 6000;
const clamp = (x) => Math.max(0, Math.min(100, x));
const pick = (list) => list[Math.floor(Math.random() * list.length)];

function readStore() {
  try { return JSON.parse(localStorage.getItem(KEY) || 'null'); } catch { return null; }
}

export class Needs {
  constructor({ now = () => Date.now() / 1000, nutritionEnabled = true, nutritionCapabilities = null, actualConsumption = false, feedKinds = null } = {}) {
    this.now = now;
    this.nutritionEnabled = nutritionEnabled !== false;
    this.nutritionCapabilities = { food: nutritionCapabilities?.food ?? this.nutritionEnabled, water: nutritionCapabilities?.water ?? this.nutritionEnabled };
    this.actualConsumption = actualConsumption === true;
    this.feedKinds = Array.isArray(feedKinds) ? [...feedKinds] : null;
    this.consumption = freshLedger(); this.consumptionRevision = 0;
    this.v = { energy: 80, food: 75, water: 70, fun: 65, social: 60 };
    this.stock = { ...STOCK0 };
    this.fed = 0;
    this.load = 0;       // 这次醒着以后说了多少字 (你和她)
    this.sleeping = false;
    this.listeners = new Set();
    const saved = readStore();
    if (saved?.consumption?.version === 1) this.consumption = saved.consumption;
    this.consumptionRevision = saved?.consumptionRevision || 0;
    if (saved?.v) {
      for (const k of Object.keys(NEEDS)) if (Number.isFinite(saved.v[k])) this.v[k] = clamp(saved.v[k]);
      for (const k of Object.keys(STOCK0)) if (Number.isFinite(saved.stock?.[k])) this.stock[k] = Math.max(0, Math.floor(saved.stock[k]));
      this.fed = saved.fed || 0;
      this.load = Math.max(0, saved.load || 0);
      // 离开期间: 按小时补算 (最多 12 小时); 跨过夜里就当睡过了
      const away = Math.min(12 * HOUR, Math.max(0, this.now() - (saved.at || this.now())));
      // 离线期间按上次房间的能力补算；换回已适配房间不能追扣暂停期。旧存档默认启用。
      if (away > 60) this._decay(away, { slept: this._crossedNight(saved.at, away), nutritionEnabled: saved.nutritionEnabled !== false, nutritionCapabilities: saved.nutritionCapabilities || { food: saved.nutritionEnabled !== false, water: saved.nutritionEnabled !== false } });
    }
    this.at = this.now();
    this._savedAt = 0;
    // 立即记录切换边界，避免进入房间后尚未 tick 就退出而丢掉暂停状态。
    if ((saved?.nutritionEnabled !== false) !== this.nutritionEnabled || ['food', 'water'].some((k) => (saved?.nutritionCapabilities?.[k] ?? (saved?.nutritionEnabled !== false)) !== this.nutritionCapabilities[k])) this.save();
  }

  get feedAllowed() { return Object.keys(FEED).some((k) => this.canFeed(k)); }
  canFeed(kind) { return !!FEED[kind] && (!this.feedKinds || this.feedKinds.includes(kind)) && this._active(kind === 'snack' ? 'food' : 'water'); }
  _active(k) { return k === 'food' || k === 'water' ? this.nutritionCapabilities[k] === true : true; }
  get coffeeBuff() { return coffeeAmount(this.consumption, this.now()); }
  get effectiveEnergy() { return clamp(this.v.energy + this.coffeeBuff); }

  // One atomic Needs record contains both the high-water mark and its effects.
  // World snapshots are persisted before this internal API is invoked.
  _refreshSettlement() {
    const saved = readStore();
    if ((saved?.consumptionRevision || 0) > this.consumptionRevision) {
      this.v = { ...saved.v }; this.stock = { ...saved.stock };
      this.consumption = saved.consumption; this.consumptionRevision = saved.consumptionRevision;
    }
  }
  _settle(e, preparation = false) {
    this._refreshSettlement();
    const state = { v: this.v, stock: this.stock, consumption: this.consumption };
    const result = preparation ? settlePreparation(state, e, this.actualConsumption && this._active('water'))
      : settleLiquid(state, e, { enabled: this.actualConsumption && this._active('water'), now: this.now() });
    if (!result.next) return result;
    const revision = this.consumptionRevision + 1;
    try { localStorage.setItem(KEY, JSON.stringify({ ...this._record(), ...result.next, consumptionRevision: revision })); }
    catch { return { ok: false, reason: 'storage-unavailable' }; }
    Object.assign(this, result.next); this.consumptionRevision = revision;
    this._emit();
    return { ok: true, id: result.id, appliedMl: result.appliedMl };
  }
  consumeLiquid(receipt) { return this._settle(receipt); }
  prepareStock(receipt) { return this._settle(receipt, true); }


  _crossedNight(from, secs) {
    if (!from || secs < 3 * HOUR) return false;
    for (let t = from; t < from + secs; t += 1800) {
      const h = herDate(t * 1000).getHours();  // 按她的时区 (tz.js)
      if (h >= 1 && h < 6) return true;
    }
    return false;
  }

  _decay(secs, { slept = false, nutritionEnabled = this.nutritionEnabled, nutritionCapabilities = this.nutritionCapabilities } = {}) {
    const h = secs / HOUR;
    for (const k of Object.keys(DECAY)) {
      if ((k === 'food' || k === 'water') && !(nutritionCapabilities?.[k] ?? nutritionEnabled)) continue;
      if (k === 'energy' && (slept || this.sleeping)) this.v.energy = clamp(this.v.energy + RESTORE * h);
      else this.v[k] = clamp(this.v[k] - DECAY[k] * h * (this.sleeping && k !== 'energy' ? 0.35 : 1));  // 睡着时饿得、渴得慢
    }
  }

  /** 每秒调: 按真实经过的时间掉 (或者睡觉时回精力). */
  tick() {
    const t = this.now(), dt = Math.min(60, Math.max(0, t - this.at));
    this.at = t;
    if (dt > 0) this._decay(dt);
    if (t - this._savedAt > 20) this.save();
    this._emit();
  }

  add(changes) {
    for (const [k, d] of Object.entries(changes || {})) if (k in this.v && Number.isFinite(d) && this._active(k) && !(this.actualConsumption && (k === 'food' || k === 'water'))) this.v[k] = clamp(this.v[k] + d);
    this._emit();
  }

  /** 困倦 0-1: 这次醒着以后说了多少话. */
  get fatigue() { return Math.min(1, this.load / LOAD_MAX); }

  /** 你们又说了 chars 个字. */
  heard(chars) {
    this.load += Math.max(0, chars);
    this._emit();
  }

  /** 睡醒了 (对话整理完): 困倦清零. */
  rested() {
    this.load = 0;
    this.save();
    this._emit();
  }

  /** 用掉一份库存; 没有了返回 false. */
  use(stock) {
    if (this.actualConsumption || !this.feedAllowed || !(this.stock[stock] > 0)) return false;
    this.stock[stock]--;
    this.save();
    this._emit();
    return true;
  }

  /** 用户投喂: 库存加上, 陪伴 +8. 返回她说的话. */
  feed(kind) {
    if (!this.canFeed(kind)) return null;
    const f = FEED[kind];
    if (!f) return null;
    this.stock[f.stock] += f.add;
    this.fed++;
    this.add({ social: 8, fun: 3 });
    this.save();
    return pick(f.thanks);
  }

  /** 最缺的那项和缺的程度 (0 不缺, 1 见底). */
  worst() {
    let key = null, gap = 0;
    for (const k of Object.keys(NEEDS)) {
      if (!this._active(k)) continue;
      const g = Math.max(0, (60 - this.v[k]) / 60);
      if (g > gap) { gap = g; key = k; }
    }
    return { key, gap };
  }

  /** 心情: 分数 (0-100)、说法、表情 (给 setEmotion). */
  get mood() {
    const weights = Object.entries(WEIGHT).filter(([k]) => this._active(k));
    // 暂停的两项不拉低心情；其余权重重新归一，仍是 0-100 分。
    const score = weights.reduce((s, [k, w]) => s + this.v[k] * w, 0) / weights.reduce((s, [, w]) => s + w, 0);
    const { key, gap } = this.worst();
    if (this.sleeping) return { score, label: '睡着了', emotion: 'calm', icon: '😴' };
    if (this.fatigue >= 0.7) return { score, label: this.fatigue >= 0.9 ? '困得不行' : '有点困', emotion: 'calm', icon: '🥱', need: 'energy' };
    if (gap > 0.55) {
      const low = { energy: ['困了', 'sad', '🥱'], food: ['饿了', 'sad', '😣'], water: ['渴了', 'sad', '🥵'],
        fun: ['好无聊', 'sad', '😑'], social: ['想你了', 'shy', '🥺'] }[key];
      return { score, label: low[0], emotion: low[1], icon: low[2], need: key };
    }
    if (score >= 75) return { score, label: '开心', emotion: 'happy', icon: '😊' };
    if (score >= 55) return { score, label: '平静', emotion: 'calm', icon: '🙂' };
    return { score, label: '有点低落', emotion: 'sad', icon: '😔', need: key };
  }

  /**
   * 告诉服务端的摘要 (按陪伴底层方案 docs/roadmap/companion-core.md 的约定): mood 取固定值, needs 是缺的那几项;
   * 面板、自检看的 label (中文说法)、各项数值、库存也在里面, 发给服务端时只取 mood / needs / score (talk.js).
   */
  summary() {
    const m = this.mood, low = { energy: 'sleepy', food: 'hungry', water: 'thirsty', fun: 'bored', social: 'lonely' };
    const mood = this.sleeping ? 'asleep' : m.need ? low[m.need] : m.score >= 75 ? 'happy' : m.score >= 55 ? 'calm' : 'low';
    return { mood, needs: Object.keys(low).filter((k) => this._active(k) && this.v[k] < 30).map((k) => low[k]), score: Math.round(m.score),
      label: m.label, values: Object.fromEntries(Object.entries(this.v).map(([k, x]) => [k, Math.round(x)])), stock: { ...this.stock },
      fatigue: +this.fatigue.toFixed(2), load: this.load, nutritionEnabled: this.nutritionEnabled, nutritionCapabilities: { ...this.nutritionCapabilities }, coffeeBuff: +this.coffeeBuff.toFixed(2), effectiveEnergy: +this.effectiveEnergy.toFixed(2) };
  }

  _record() {
    return { v: this.v, stock: this.stock, fed: this.fed, load: this.load, at: this.now(), nutritionEnabled: this.nutritionEnabled,
      nutritionCapabilities: this.nutritionCapabilities, consumption: this.consumption, consumptionRevision: this.consumptionRevision };
  }
  save() {
    this._refreshSettlement(); this._savedAt = this.now();
    try { localStorage.setItem(KEY, JSON.stringify(this._record())); return true; } catch { return false; }
  }

  onChange(fn) { this.listeners.add(fn); return () => this.listeners.delete(fn); }
  _emit() { for (const fn of this.listeners) fn(this); }
}

/**
 * 小屋卡片左上角的心情牌: 平时一小块 (表情 + 心情), 点开是五项需求的小条、库存和投喂的四个按钮.
 * feed(kind): 点了投喂; extra(): 额外几行 (练习、受伤). 返回 { el, close() }.
 */
export function needsPanel(needs, { feed, extra }) {
  const el = document.createElement('div');
  el.className = 'room-mood';
  el.innerHTML = `<button class="mood-chip" type="button" title="她的心情 (点开投喂)"><span class="mood-icon"></span><span class="mood-label"></span></button>
    <div class="mood-card" hidden>
      <div class="mood-bars"></div>
      <div class="mood-stock"></div>
      <div class="mood-extra"></div>
      <div class="mood-feed">${Object.entries(FEED).map(([k, f]) =>
        `<button type="button" data-feed="${k}" title="投喂${f.name}"><span>${f.icon}</span>${f.name}</button>`).join('')}</div>
    </div>`;
  const chip = el.querySelector('.mood-chip'), card = el.querySelector('.mood-card');
  const bars = el.querySelector('.mood-bars'), stock = el.querySelector('.mood-stock');
  bars.innerHTML = Object.entries(NEEDS).map(([k, n]) =>
    `<div class="mood-bar" data-need="${k}"><span>${n}</span><i><b></b></i>${k === 'food' || k === 'water' ?
      '<small data-nutrition-note hidden style="grid-column:1/-1">饮食适配中，暂停变化</small>' : ''}</div>`).join('');
  chip.addEventListener('click', () => { card.hidden = !card.hidden; el.classList.toggle('open', !card.hidden); });
  el.querySelectorAll('[data-feed]').forEach((b) => b.addEventListener('click', () => {
    if (!needs.canFeed(b.dataset.feed)) return;
    feed(b.dataset.feed);
    b.classList.add('fed');
    setTimeout(() => b.classList.remove('fed'), 600);
  }));
  const render = () => {
    const m = needs.mood;
    el.querySelector('.mood-icon').textContent = m.icon;
    el.querySelector('.mood-label').textContent = m.label;
    for (const [k, x] of Object.entries(needs.v)) {
      const b = bars.querySelector(`[data-need="${k}"] b`);
      b.style.width = `${Math.round(x)}%`;
      const row = b.parentElement.parentElement;
      const paused = !needs._active(k);
      row.classList.toggle('low', !paused && x < 30);
      const note = row.querySelector('[data-nutrition-note]');
      if (note) note.hidden = !paused;
    }
    const s = needs.stock;
    stock.textContent = `冰箱饮料 ${s.drinks} · 咖啡豆 ${s.beans} 杯 · 零食 ${s.snacks} · 牛奶 ${s.milk}`;
    chip.title = needs.feedAllowed ? '她的心情 (点开投喂)' : '她的心情 (饮食适配中，暂不可投喂)';
    el.querySelectorAll('[data-feed]').forEach((b) => {
      b.disabled = !needs.canFeed(b.dataset.feed);
      b.title = needs.canFeed(b.dataset.feed) ? `投喂${FEED[b.dataset.feed].name}` : '饮食适配中，暂停变化，暂不可投喂';
    });
    const lines = [...(extra?.() || [])];
    if (needs._active('water') && !needs._active('food')) lines.unshift('饮水按实际入口结算；食物适配中，饱腹暂停变化。');
    if (needs.coffeeBuff > 0.05) lines.unshift('喝过咖啡，暂时精神一点；仍然需要休息。');
    if (!needs.feedAllowed) lines.unshift('饮食适配中，暂不可投喂；原数值和库存保留。');
    el.querySelector('.mood-extra').innerHTML = lines.map((t) => `<div>${t}</div>`).join('');
  };
  needs.onChange(render);
  render();
  return { el, close() { card.hidden = true; el.classList.remove('open'); } };
}
