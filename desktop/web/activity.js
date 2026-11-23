// 看你在做什么之后她的反应 (宿主托盘里"看你在做什么"打开时才有, 见 desktop/host/src/activity.h):
//   - 换了一件事、稳定 15 秒后说一句 (开始玩游戏、看视频、写代码、听歌、聊天...), 同一件事有冷却 (同一个游戏 40 分钟内不再说);
//   - 一件事做久了关心一下 (游戏 1 小时、写代码 / 办公 1.5 小时、视频 1.5 小时, 之后隔一阵再提醒);
//   - 走开 5 分钟以上又回来: "回来啦"; 半夜 0:30 到 5 点还在用电脑: 劝早点休息.
//   - 两句之间至少隔 6 分钟 ("回来啦"除外); 她在说话、你在打字或说话时先不说, 等一会儿.
// 说的话只显示在对话栏里 (不出声, 不经过服务端), 配一个表情和点头. 告诉服务端的见 summary() (talk.js 放进 state.activity).
// "半夜"按她的时区算 (tz.js: 面板里设的, 没设跟着电脑).
import { herDate } from '/pet/tz.js';

const MIN = 60;
const LINES = {
  game: [['要玩{app}了吗？好好玩～', '{app}！我在旁边看你玩～'], 'excited', 40 * MIN],
  video: [['在看{app}呀，在看什么好看的？', '看视频呢，我也想一起看～'], 'happy', 30 * MIN],
  music: [['在听「{title}」吗？', '在听歌呀，好听吗？'], 'happy', 30 * MIN],
  code: [['又在写代码啦，加油～', '写代码的样子好认真呀。'], 'happy', 120 * MIN],
  office: [['在忙呀，我安静陪着你。', '工作辛苦啦，我在这儿陪你。'], 'neutral', 120 * MIN],
  chat: [['在和朋友聊天吗？'], 'happy', 60 * MIN],
  browser: [['在看「{title}」吗？'], 'neutral', 45 * MIN],
};
// 做久了: 多久开始提醒、之后隔多久再提醒
const LONG = {
  game: [60 * MIN, 45 * MIN, '已经玩了{dur}啦，喝口水、歇歇眼睛吧～'],
  code: [90 * MIN, 60 * MIN, '坐了{dur}了，起来活动活动吧～'],
  office: [90 * MIN, 60 * MIN, '忙了{dur}了，休息一下好不好？'],
  video: [90 * MIN, 60 * MIN, '看了{dur}了，眼睛累不累呀？'],
};
const STABLE_S = 15;    // 一件事持续这么久才算"在做"
const GAP_S = 6 * MIN;  // 两句之间至少隔这么久

const fmtDur = (s) => {
  const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
  return h ? `${h} 小时${m ? `${m} 分钟` : ''}` : `${m} 分钟`;
};
const pickOne = (list) => list[Math.floor(Math.random() * list.length)];

export class ActivityReactor {
  /**
   * say(text, emotion): 她说一句 (对话栏 + 表情); busy(): 她在说话、你在打字或说话; onChange(summary): 告诉服务端的摘要变了.
   * now(): 秒 (自检时用假的钟).
   */
  constructor({ say, busy, onChange, now = () => performance.now() / 1000, clock = () => herDate() }) {
    Object.assign(this, { say, busy, onChange, now, clock });
    this.on = false;
    this.cur = null;     // {category, app, title, since}
    this.reacted = null; // 这件事已经说过了
    this.lastSaid = -1e9;
    this.cool = new Map();  // 同一件事上次说的时间 (键: 类别 + app)
    this.nagged = new Map();
    this.awaySince = null;
    this.nightSaid = -1e9;
    this.queue = null;   // 想说但还没轮到的
  }

  /** 宿主发来的消息: {t:'activity-mode', on} 刚打开; {t:'activity', on:false} 关了; {t:'activity', on:true, category, app, title} 变了. */
  handle(m) {
    const t = this.now();
    if (m.t === 'activity-mode') {  // 刚打开: 说明一下她看什么 (不算进"两句之间的间隔", 接下来的第一件事照常说)
      this.on = true;
      this.say('好呀，我会看着你在用什么程序陪你～ 不截图，聊天软件只看是哪个。', 'happy');
      return;
    }
    if (!m.on) {
      this.on = false;
      this.cur = null;
      this.onChange?.(null);
      return;
    }
    this.on = true;
    const next = { category: m.category, app: m.app || '', title: m.title || '', since: t };
    const prev = this.cur;
    if (prev && prev.category === next.category && prev.app === next.app) {  // 同一件事只是换了标题 (换了网页、换了歌)
      prev.title = next.title;
      this.onChange?.(this.summary());
      return;
    }
    if (next.category === 'away') this.awaySince = t;
    else if (this.awaySince !== null) {
      if (t - this.awaySince >= 5 * MIN) this._speak('回来啦！刚刚去哪了呀？', 'happy', true);
      this.awaySince = null;
    }
    this.cur = next;
    this.reacted = null;
    this.onChange?.(this.summary());
  }

  /** 给服务端的摘要 (title 由调用方决定带不带). */
  summary() {
    const c = this.cur;
    if (!this.on || !c) return null;
    return { category: c.category, app: c.app, title: c.title, minutes: Math.floor((this.now() - c.since) / 60) };
  }

  /** 每秒左右调一次. */
  tick() {
    if (!this.on) return;
    const t = this.now(), c = this.cur;
    if (this.queue && this._speak(this.queue.text, this.queue.emotion, this.queue.force)) this.queue = null;
    if (!c || c.category === 'away') return;
    const held = t - c.since;
    // 换了一件事: 稳定一会儿再说 (来回切窗口时不说)
    const line = LINES[c.category];
    const key = `${c.category}:${c.app}`;
    if (line && this.reacted !== c && held >= STABLE_S && !(line[0][0].includes('{title}') && !c.title)) {
      this.reacted = c;
      if (t - (this.cool.get(key) ?? -1e9) >= line[2]) {
        const lines = line[0].filter((x) => !x.includes('{title}') || c.title);
        const text = pickOne(lines).replace('{app}', c.app).replace('{title}', c.title);
        if (this._speak(text, line[1])) this.cool.set(key, t);
      }
    }
    // 做久了
    const long = LONG[c.category];
    if (long && held >= long[0] && t - (this.nagged.get(key) ?? -1e9) >= long[1]) {
      if (this._speak(long[2].replace('{dur}', fmtDur(held)), 'sad')) this.nagged.set(key, t);
    }
    // 半夜还在用电脑
    const d = this.clock(), hm = d.getHours() * 60 + d.getMinutes();
    if (hm >= 30 && hm < 5 * 60 && t - this.nightSaid >= 60 * MIN) {
      const time = `${d.getHours()}:${String(d.getMinutes()).padStart(2, '0')}`;
      if (this._speak(`已经 ${time} 了，早点休息好不好？`, 'sad')) this.nightSaid = t;
    }
  }

  /** 说一句: 隔得够久、她和你都没在说话时才说, 否则排队 (只留最新的一句). force: 不管间隔 ("回来啦"、刚打开时). */
  _speak(text, emotion, force = false) {
    const t = this.now();
    if (!force && t - this.lastSaid < GAP_S) return false;
    if (this.busy()) {
      this.queue = { text, emotion, force };
      return false;
    }
    this.lastSaid = t;
    this.say(text, emotion);
    return true;
  }
}
