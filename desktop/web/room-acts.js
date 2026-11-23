// 小屋里她做的事 (room-life.js 加载房间以后交给这里):
//   - Life: 挑下一件事 —— 地上有投喂的东西先去捡; 某项需求快见底了先去补; 其余按权重随机 (缺哪项就更想做补哪项的事,
//     晚上 23-7 点 night 加权、白天才做的 day 不做; 没有库存的不做). 点了家具 = 马上去做那件事 (正在做的先收尾).
//     候选除了场景表 life 里的, 还有她自己发现的可供性 (room-afford.js: 看见过东西才想得到, 好不好按她试过的经验).
//   - 隔几分钟 (THINK_S) 那次"其余"不按权重, 问服务端 (上行 room_think: 心情、需求、候选、最近做的、疼和摔、练习心得;
//     下行 room_plan, 服务端用这条连接的模型按人设想): 挑哪件、开始前想的那句、做法偏好 (slow 慢一点、careful 稳一点、
//     look 先看看你、wave 先冲你挥挥手). 你在说话、没连上、THINK_WAIT_MS 内没回、回了做不了的, 都按本机规则.
//   - 每件事是一段脚本 (SCRIPTS): 走过去、坐下、拿起来、用、放回去, 一步一步等着做完. 用户点了别的、和她说话时 hurry:
//     等待马上结束, 手里的东西照样放好再走.
//   - Fridge: 冰箱里的饮料 (库存几瓶就摆几瓶), Mover: 挪家具 (推着走, 碰撞盒、站位、座位跟着变, 记住摆法).
import * as THREE from 'three';
import { blockers, buildGrid, nearestWalkable, toCell, toWorld, walkable } from '/static/nav.js';
import { forPlan } from '/pet/room-known.js';
import { FEED } from '/pet/room-needs.js';
import { RoomActionRequests } from '/pet/room-action-requests.js';
import { createDeviceScripts } from '/pet/room-device-actions.js';
import { coffeeDecision } from '/pet/room-coffee-policy.js';
import * as legacySight from '/pet/room-sight.js';
import { herDate } from '/pet/tz.js';
import { putItem } from '/static/grasp-controller.js';
import { cupPickItem } from '/static/cup-pick.js';

const PARAMS = new URLSearchParams(location.search);
const rand = (a, b) => a + Math.random() * (b - a);
const pickOne = (list) => list[Math.floor(Math.random() * list.length)];
const isNight = () => { const h = herDate().getHours(); return h >= 23 || h < 7; };  // 按她的时区 (tz.js)
const FETCH = { id: 'fetch', name: '捡投喂的东西', script: 'fetch' };
const READ_DOC = { id: 'readDoc', name: '看拖进来的文件', script: 'readDoc' };
// 床头柜上有便利贴、醒着、连着服务端 (断线那阵子你发的): 去撕下来一条条回
const NOTES = { id: 'notes', name: '看便利贴', script: 'notes' };
// QQ 那边她在做的事 (服务端的 channel_doing) 在小屋里坐到哪: 回 QQ、看 B 站 -> 电脑前; 研究本事、给自己写工具 -> 阅读椅上翻资料
const DOING_AT = { qq: 'computer', video: 'computer', study: 'read', tool: 'read' };
const RESTART_NAP_MAX_S = 600;  // 服务端重启时最多睡多久 (等不到它回来也起床, 便利贴留着等连上)
const LABEL_ZH = { treeR: '树式 (右腿抬)', treeL: '树式 (左腿抬)', fold: '前屈', bend: '侧弯' };
const STEP_S = 45;
// 隔多久问一次服务端 (秒; 页面参数 think=秒 改, think=off 不问), 最多等多久 (服务端本地模型一般 0.2-0.6 秒)
const THINK_S = PARAMS.get('think') === 'off' ? null : Number(PARAMS.get('think') ?? 180);
const THINK_WAIT_MS = 4000;
// 端咖啡时她挑的做法 (wm/cognition/carry.py): 拿放放慢 = 行动层的 handSpeed (和物理评测台一样 0.6); 等它凉一点 = 放着等这么久再端
// (液体模型: 浓缩杯里 60 ml 70 ℃ 的咖啡约 8 分钟凉到 52 ℃, 溅上了只是烫一下)
const HAND_SLOW = 0.6;
const COOL_S = 480;

/**
 * 座位前面站不站得下 (坐下要先背对着座位站到它前面): 座位前沿外 10 cm 那一点最近的能站的格子还在座位前面、离得不远.
 * 站不下 (挨着墙、桌子) 时行动层会找最近能站的地方当起点, 常常在椅背后面, 坐下的那一下整个人就穿过椅背 (用户发现的).
 */
// 座位前面窄 (阅读椅: 左边书架、右边小边桌、前面是房间边上, 只有 0.34-0.42 m 的缝) 时, 寻路按这么胖算 (侧着身子挤进去)
const SQUEEZE = 0.15;

function seatFrontOk(grid, o) {
  const seat = o?.seat;
  if (!seat || !o.box) return true;
  const f = [Math.sin(seat.yaw), Math.cos(seat.yaw)], [cx, cz] = o.box.center, [sx, sz] = o.box.size;
  const front = Math.max(...[[-1, -1], [1, -1], [-1, 1], [1, 1]].map(([a, b]) =>
    (cx + (a * sx) / 2 - seat.pos[0]) * f[0] + (cz + (b * sz) / 2 - seat.pos[1]) * f[1]));
  const p = [seat.pos[0] + f[0] * (front + 0.1), seat.pos[1] + f[1] * (front + 0.1)];
  const g = nearestWalkable(grid, ...p);
  return (g[0] - seat.pos[0]) * f[0] + (g[1] - seat.pos[1]) * f[1] >= front + 0.05 && Math.hypot(g[0] - p[0], g[1] - p[1]) < 0.45;
}

/**
 * 从窄地方挤出来往哪走: 在瘦的网格 (tight) 里从她脚下往外泛洪, 按路程最先碰到的正常能站的格子 (normal).
 * 不按直线最近: 阅读椅前面直线最近的是书架和椅子之间那条更窄的缝, 走那边头、胳膊肘会蹭到书架 (换了个子高的模型时撞了头).
 */
function squeezeExit(tight, normal, x, z) {
  const [i0, j0] = toCell(tight, x, z), seen = new Uint8Array(tight.w * tight.h), q = [i0, j0];
  if (!walkable(tight, i0, j0)) return nearestWalkable(normal, x, z);
  seen[j0 * tight.w + i0] = 1;
  for (let k = 0; k < q.length; k += 2) {
    const i = q[k], j = q[k + 1];
    if (walkable(normal, i, j)) return toWorld(normal, i, j);
    for (const [di, dj] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const a = i + di, b = j + dj;
      if (walkable(tight, a, b) && !seen[b * tight.w + a]) { seen[b * tight.w + a] = 1; q.push(a, b); }
    }
  }
  return nearestWalkable(normal, x, z);
}

/** 一段脚本的执行环境: 等动作做完、等几秒 (hurry 时提前结束)、收尾 (出错、被打断也会做). */
class Ctx {
  constructor(life, request = null) {
    this.life = life;
    this.r = life.r;
    this.undo = [];
    this.request = request;
    this.cleaning = false;
  }

  get fast() { return this.life.fast; }
  get hurry() { return this.life.hurry; }
  _checkCancelled(cleaning = this.cleaning) {
    if (!cleaning && this.request && (this.request.cancelled || this.life.requests.active !== this.request))
      throw new Error('动作请求已停止或已过期');
  }
  /** 服务端想的做法: 慢一点 (走路 0.75 倍)、稳一点 (练习不试新参数, 用现在最好的). */
  get slow() { return this.life.how.has('slow'); }
  get careful() { return this.life.how.has('careful'); }
  get explore() { return !this.hurry && !this.careful; }
  say(text) { this.r.status(text); }

  /** 一步最多等 STEP_S 秒: 行动层偶尔会绕着到不了的起点一直走, 超时就停下, 这件事算没成. */
  _operation() {
    const cleaning = this.cleaning;
    let active = true;
    return { get active() { return active; }, close: () => { active = false; },
      check: () => { if (!active) throw new Error('动作步骤已结束'); this._checkCancelled(cleaning); } };
  }

  _limit(promise, what, operation = null) {
    let timer, cancelTimer;
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => {
        operation?.close();
        const L = this.r.layer;
        try {
          operation?.cancelPick?.('取杯步骤超时');
          operation?.cancelPut?.('放杯步骤超时');
          L.stop();
          if ((L.action && !L.action.loop) || L.reaching || L.path) this.stopFailure = `${what}超时后动作仍未确认结束`;
        } catch (error) { this.stopFailure = `停止行走未确认: ${error.message}`; }
        reject(new Error(`${what}太久没做完`));
      }, STEP_S * 1000);
    });
    const waits = [promise, timeout];
    if (this.request && !this.cleaning && (what === 'walk' || what === 'go' || (what === 'pick' && operation?.cancelPick))) waits.push(new Promise((_, reject) => {
      const check = () => {
        if (this.cleaning || (operation && !operation.active)) return;
        if (what === 'pick') {
          if (this.request.cancelled || this.life.requests.active !== this.request) {
            try { operation.cancelPick(this.request.cancelReason || '取杯请求已停止或过期'); }
            catch (error) { this.stopFailure = `停止取杯未确认: ${error.message}`; reject(error); }
          }
          return;  // Revoke only this unbound cup task; wait for the real clip/approach callback.
        }
        if (this.life.requests.active !== this.request) { reject(new Error('动作请求已过期')); return; }
        if (!this.request.cancelled) return;
        const L = this.r.layer;
        // Finish an in-flight stand/reach safely. Its guarded continuation must not start the old goal.
        if (L.action || L.reaching || L.seated) return;
        try { L.stop(); }
        catch (error) { this.stopFailure = `停止行走未确认: ${error.message}`; reject(error); return; }
        // stop clears the arrival callback: settle our wait explicitly, then run ordinary cleanup.
        reject(new Error(this.request.cancelReason || '动作请求已停止'));
      };
      cancelTimer = setInterval(check, 40);
      check();
    }));
    return Promise.race(waits).finally(() => {
      operation?.close(); clearTimeout(timer); clearInterval(cancelTimer);
      try {
        if (operation?.cancelPick?.('取杯步骤已结束')) {
          const L = this.r.layer;
          if ((L.action && !L.action.loop) || L.reaching || L.path) this.stopFailure = '取杯结束后动作仍未确认结束';
        }
        if (operation?.cancelPut?.('放杯步骤已结束')) {
          const L = this.r.layer;
          if ((L.action && !L.action.loop) || L.reaching || L.path) this.stopFailure = '放杯结束后动作仍未确认结束';
        }
      } catch (error) { this.stopFailure = `${what === 'put' ? '停止放杯' : '停止取杯'}未确认: ${error.message}`; }
    });
  }

  /** 行动层的一步 (go / sit / stand / pick / put / turn). 慢一点时走过去 (go) 用 0.75 倍速. */
  async run(step) {
    this._checkCancelled();
    await this._leaveChair(step);
    return this._run(step);
  }

  _run(step) {
    this._checkCancelled();
    if (this.slow && step.do === 'go' && !step.spd) step = { ...step, spd: 0.75 };
    const operation = this._operation();
    if (step.do === 'pick') {
      const cleaning = this.cleaning;
      let cupTask = null, revoked = false;
      operation.capturePick = previous => {
        const task = this.r.layer._cupPickTask;
        if (task && task !== previous && task.item?.id === step.to) {
          cupTask = task;
          task.isOperationActive = () => operation.active && (cleaning || !this.request ||
            (!this.request.cancelled && this.life.requests.active === this.request));
        }
      };
      operation.cancelPick = reason => {
        if (!cupTask || revoked) return false;
        const cancelled = this.r.layer._cancelCupPick?.(reason, cupTask) === true;
        if (cancelled) revoked = true;
        return cancelled;
      };
    }
    if (step.do === 'put') {
      const L = this.r.layer, item = putItem(L.items || {}, step.to && L.targets?.[step.to]);
      if (cupPickItem(item)) (this.returningCups ??= new Set()).add(item);
      let cupTask = null, revoked = false;
      operation.putActive = () => operation.active && (!this.request || this.life.requests.active === this.request);
      operation.capturePut = previous => {
        const task = L._cupPutTask;
        if (task && task !== previous && task.kind === 'put' && task.requestedTo === step.to && (!item || task.item === item)) {
          cupTask = task;
          (this.returningCups ??= new Set()).add(task.item);
          task.isOperationActive = operation.putActive;  // Normal cancellation permits this safe return.
        }
      };
      operation.cancelPut = reason => {
        if (!cupTask || revoked) return false;
        const cancelled = L._cancelCupPut?.(reason, cupTask) === true;
        if (cancelled) revoked = true;
        return cancelled;
      };
    }
    return this._limit(new Promise((resolve, reject) => {
      const fail = error => { if (operation.active) { operation.close(); reject(error); } };
      const done = err => {
        if (!operation.active) return;
        try { if (err) throw err instanceof Error ? err : new Error(err); operation.check(); operation.close(); resolve(); } catch (error) { fail(error); }
      };
      const run = () => {
        if (!operation.active) return;
        try {
          if (step.do === 'put') {
            if (!operation.putActive()) throw new Error('放杯操作已结束或请求已过期');
          } else operation.check();
          const previous = this.r.layer._cupPickTask, previousPut = this.r.layer._cupPutTask;
          try { this.r.layer.run(step, done); }
          finally { operation.capturePick?.(previous); operation.capturePut?.(previousPut); }
        } catch (error) { fail(error); }
      };
      if ((step.do === 'go' || step.do === 'pick' || step.do === 'put') && this.r.layer.seated) this.r.layer.standUp(run);
      else run();
    }), step.do, operation);
  }

  /** 走到任意一点 (到了面向 yaw), speed 1 正常. */
  async walk(xz, yaw = null, speed = 1) {
    this._checkCancelled();
    await this._leaveChair(null);
    return this._walk(xz, yaw, speed);
  }

  /**
   * 带滑轮的椅子 (电竞椅) 拉出来了的话: 坐着就先连人带椅子挪出来; 要离开 (走开、拿放东西、坐别处) 就站起来, 把椅子推回桌子底下.
   * 只是站起来 (stand)、再坐回这张椅子时不推回去.
   */
  async _leaveChair(step) {
    const r = this.r, id = this.life.chair, sq = this.life.squeezed;
    if (sq && !(step?.do === 'turn' || step?.do === 'stand' || (step?.do === 'sit' && step.to === sq))) {
      // 挤进窄地方坐的: 站起来, 按瘦的网格原路挤到外面最近一处正常能站的地方, 再换回正常的寻路网格
      const L = r.layer, normal = buildGrid(r.scene);
      this.life.squeezed = null;
      this.life.squeezing = true;
      try {
        if (L.seated) await this._run({ do: 'stand' });
        this.say('她侧着身子从缝里挤出来');
        await this._walk(squeezeExit(L.grid, normal, L.char.x, L.char.z));
      } finally {
        L.grid = normal;
        this.life.squeezing = false;
      }
    }
    if (!id || step?.do === 'turn') return;
    if (r.layer.seated && (r.mover.slid[id] || 0) < 1) await r.mover.slide(this, id, 1);
    if (step?.do === 'stand' || (step?.do === 'sit' && step.to === id)) return;
    if (r.layer.seated) await this._run({ do: 'stand' });
    this.life.chair = null;
    try {
      await this._chairPull(id, 0);
    } catch (err) {  // 推不回去 (旁边被挡住) 也别卡住后面的事: 椅子自己滑回去
      r.log(`小屋: 椅子没推回去 (${err.message})`);
      await r.mover.slide(this, id, 0);
    }
  }

  /** 站到带滑轮椅子的侧面, 一只手扶着椅背, 把它拉出来 (k = 1) / 推回桌子底下 (k = 0). */
  async _chairPull(id, k) {
    const r = this.r, m = r.mover, o = m._entry(id), seat = o?.seat;
    if (!seat?.scoot || (m.slid[id] || 0) === k) return;
    const f = [Math.sin(seat.yaw), Math.cos(seat.yaw)], l = [Math.cos(seat.yaw), -Math.sin(seat.yaw)];
    const half = Math.abs(l[0]) * o.box.size[0] / 2 + Math.abs(l[1]) * o.box.size[1] / 2;
    const back = [seat.pos[0] - f[0] * 0.22, seat.pos[1] - f[1] * 0.22];  // 椅背在座位后面
    let best = null;
    for (const s of [1, -1]) {  // 两边挑一边站得下的
      const p = [back[0] + l[0] * s * (half + 0.28) - f[0] * 0.25, back[1] + l[1] * s * (half + 0.28) - f[1] * 0.25];
      const g = nearestWalkable(r.layer.grid, ...p), off = Math.hypot(g[0] - p[0], g[1] - p[1]);
      if (!best || off < best.off) best = { s, g, off };
    }
    if (best.off > 0.35) throw new Error(`${o.name}旁边站不下, 拉不出来`);
    const grip = new THREE.Vector3(back[0] + l[0] * best.s * (half - 0.06), 0.86, back[1] + l[1] * best.s * (half - 0.06));
    const mid = [grip.x - f[0] * seat.scoot * (k ? 0.5 : -0.5), grip.z - f[1] * seat.scoot * (k ? 0.5 : -0.5)];
    const yaw = Math.atan2(mid[0] - best.g[0], mid[1] - best.g[1]);
    await this._walk(best.g, yaw);
    const left = [Math.cos(yaw), -Math.sin(yaw)];
    const side = (grip.x - r.layer.char.x) * left[0] + (grip.z - r.layer.char.z) * left[1] > 0 ? 'left' : 'right';
    this.say(k ? `她扶着椅背, 把${o.name}拉出来` : `她把${o.name}推回桌子底下`);
    r.poses.set('hold', { target: grip, side });
    try {
      await this.wait(0.6, { hurry: false });
      await m.slide(this, id, k, grip);
      await this.wait(0.3, { hurry: false });
    } finally {
      r.poses.set(null);
    }
  }

  _walk([x, z], yaw = null, speed = 1) {
    this._checkCancelled();
    const layer = this.r.layer, goal = nearestWalkable(layer.grid, x, z);
    const k = (this.slow ? 0.75 : 1) * (0.82 + 0.18 * Math.min(1, (this.r.needs.effectiveEnergy ?? this.r.needs.v.energy ?? 80) / 80));  // 没精神时走得慢
    const operation = this._operation();
    return this._limit(new Promise((resolve, reject) => {
      const fail = error => { if (operation.active) { operation.close(); reject(error); } };
      const arrived = () => {
        if (!operation.active) return;
        try { operation.check(); operation.close(); resolve(); } catch (error) { fail(error); }
      };
      const go = () => {
        if (!operation.active) return;
        try {
          operation.check();  // A timed-out stand continuation cannot become a cleanup operation.
          if (!layer._walkTo(goal, { yaw, speed: speed * k * this.r.body.speed, after: arrived })) fail(new Error('走不到那里'));
        } catch (error) { fail(error); }
      };
      if (layer.seated) layer.standUp(go);
      else go();
    }), 'walk', operation);
  }

  /** 人一样停一下 (看一眼要拿的东西、坐稳了缓一缓): a-b 秒, 自检 (fast) 时一闪而过. 不被 hurry 打断. */
  beat(a, b) {
    return this.wait(this.fast ? 0.15 : rand(a, b), { hurry: false });
  }

  /**
   * 坐到 id 上: 座面高度按练习的 (每次在现在最好的附近试一点, 屁股陷进去或悬空体感会扣分), 坐稳以后量 1.6 秒打分.
   */
  async sit(id) {
    const r = this.r, t = r.layer.targets[id], base = r.seatBase?.(id);
    if (!t?.seat || base === null || base === undefined) return this.run({ do: 'sit', to: id });
    if (r.layer.seated) await this.run({ do: 'stand' });
    const rolling = !!t.seat.scoot && !!r.mover.base[t.seat.mover];
    if (rolling) {  // 电竞椅: 先把椅子拉出来, 从前面坐
      if (this.life.chair && this.life.chair !== id) await this._leaveChair({ do: 'sit', to: id });
      await this._chairPull(id, 1);
      this.life.chair = id;
    }
    if (!seatFrontOk(r.layer.grid, t)) {  // 前面窄: 寻路按瘦一点算, 侧着身子从缝里挤到座位前面; 走的时候原路挤出来 (_leaveChair)
      const tight = buildGrid({ ...r.scene, agentRadius: SQUEEZE });
      if (!seatFrontOk(tight, t)) throw new Error(`${t.name || id}前面没地方站, 坐不了`);
      r.layer.grid = tight;
      this.life.squeezed = id;
      this.say(`她侧着身子挤到${t.name || '座位'}前面`);
    }
    const tr = r.practice.attempt(`sit@${id}`, { dh: [-0.06, 0.06, 0] }, { explore: this.explore });
    t.seat.height = base + tr.params.dh;
    this.life.squeezing = this.life.squeezed === id;  // 挤进去的这一段: 体感里擦着家具不算撞
    try {
      await this.run({ do: 'sit', to: id });
    } catch (err) {
      if (this.life.squeezed === id) { this.life.squeezed = null; r.layer.grid = buildGrid(r.scene); }
      throw err;
    } finally {
      this.life.squeezing = false;
    }
    await this.beat(0.6, 1.4);  // 坐下来先缓一下
    r.body.begin(`sit@${id}`);
    await this.wait(1.6, { hurry: false });
    this.learn(tr, r.body.end(true));
    if (rolling) {  // 坐稳了, 脚蹬一下, 连人带椅子挪到桌子跟前
      this.say(`她连人带${t.name || '椅子'}往桌子那边挪了挪`);
      await r.mover.slide(this, id, 0);
    }
  }

  /** 练习的一次做完了: 记分, 有心得就说一句. */
  learn(tr, res, extra = 0) {
    if (res) res.score = +(res.score - extra).toFixed(3);
    const out = tr.finish(res);
    this.r.log(`小屋: 练习 ${res?.label} 第 ${out.n} 次 ${out.score.toFixed(2)} (平时 ${out.avg.toFixed(2)})${out.better ? ' 更好' : ''}`);
    if (out.line) this.say(out.line);
    return out;
  }

  /** 站着伸手够一点 (1.6 秒, 0.7 秒时手到位调 onEvent). */
  reach(side, point, onEvent) {
    this._checkCancelled();
    return new Promise((resolve, reject) => this.r.layer._reachStart(side, point.clone(), () => {
      if (this.cleaning || !this.request || (!this.request.cancelled && this.life.requests.active === this.request)) onEvent?.();
    }, () => { try { this._checkCancelled(); resolve(); } catch (error) { reject(error); } }));
  }

  wait(s, { hurry = true } = {}) {
    this._checkCancelled();
    const end = performance.now() + (this.fast ? Math.min(s, 2) : s) * 1000;
    return new Promise((resolve, reject) => {
      const tick = () => {
        try { this._checkCancelled(); } catch (e) { reject(e); return; }
        if (performance.now() >= end || (hurry && this.life.hurry && !this.cleaning)) resolve();
        else setTimeout(tick, 100);
      };
      tick();
    });
  }

  later(fn) { this.undo.push(fn); }

  async cleanup() {
    this.cleaning = true;
    const errors = this.stopFailure ? [this.stopFailure] : [];
    while (this.undo.length) {
      try { await this.undo.pop()(); } catch (err) { errors.push(err.message); this.r.log(`小屋: 收尾出错 ${err.message}`); }
    }
    for (const item of this.returningCups || []) {
      if (item.held) errors.push(`${item.id || '杯子'}仍在手中，归还未确认`);
    }
    this.cleaning = false;
    return errors;
  }
}

// -- 小工具 ---------------------------------------------------------------------------------------
/** 台面上找一个空位 (别和已经放着的东西叠在一起), 返回一个临时的放置目标 id. */
function freePlace(r, to) {
  const t = r.layer.targets[to];
  if (!t?.place) return to;
  const [px, py, pz] = t.place;
  const taken = Object.values(r.layer.items).filter((i) => !i.held).map((i) => i.mesh.position);
  for (const [ox, oz] of [[0, 0], [0.14, 0], [-0.14, 0], [0, 0.12], [0.14, 0.12], [-0.14, 0.12], [0.26, 0], [-0.26, 0]]) {
    const x = px + ox, z = pz + oz;
    if (taken.some((p) => Math.hypot(p.x - x, p.z - z) < 0.1 && Math.abs(p.y - py) < 0.3)) continue;
    r.layer.targets.__place = { id: '__place', name: t.name, place: [x, py, z], look: [x, py + 0.1, z], approach: t.approach };
    return '__place';
  }
  return to;
}

/** 放回原处用的目标: 记下东西现在的底面位置. */
function homeOf(r, id, name, approach) {
  const it = r.layer.items[id], p = it.mesh.position;
  const key = `home_${id}`;
  const target = { id: key, name, place: [p.x, p.y - it.half, p.z], look: [p.x, p.y, p.z], approach };
  if (it.grasp && !it.physical) {
    // Resuming a cancelled read must keep the shelf origin, not make the held pose a new home.
    if (it.held && !it.graspOrigin?.pose) throw new Error('缺少抓握原位置，不能把手中姿态当成归还点');
    const origin = it.held ? it.graspOrigin.pose : null;
    const center = origin ? new THREE.Vector3(...origin.position) : it.mesh.getWorldPosition(new THREE.Vector3());
    const q = origin ? new THREE.Quaternion(...origin.quaternion) : it.mesh.getWorldQuaternion(new THREE.Quaternion());
    const half = it.graspHalfExtents;
    const below = half ? [new THREE.Vector3(1, 0, 0), new THREE.Vector3(0, 1, 0), new THREE.Vector3(0, 0, 1)]
      .reduce((sum, axis, i) => sum + Math.abs(axis.applyQuaternion(q).y) * half[i], 0) : it.half;
    Object.assign(target, { place: [center.x, center.y - below, center.z], look: center.toArray(),
      objectPosition: center.toArray(), objectQuaternion: q.toArray(), itemId: id });
  }
  r.layer.targets[key] = target;
  return key;
}

/** 站到离 point 约 dist 米、够得着的地方 (走得到、离她最近), 面向它; 那个距离走不到就往外让. */
function standNear(r, point, dist = 0.42) {
  const L = r.layer;
  for (const rr of [dist, dist + 0.1, dist + 0.2, dist + 0.3]) {
    let best = null;
    for (let k = 0; k < 16; k++) {
      const yaw = (k / 16) * Math.PI * 2, pos = [point.x - Math.sin(yaw) * rr, point.z - Math.cos(yaw) * rr];
      if (!walkable(L.grid, ...toCell(L.grid, ...pos))) continue;
      const d = Math.hypot(pos[0] - L.char.x, pos[1] - L.char.z);
      if (!best || d < best.d) best = { pos, yaw, d };
    }
    if (best) return best;
  }
  return null;
}

/**
 * 蹲下, 一只手伸到 point (手到的时候调 onEvent), 再站起来. 站多远、蹲多深、骨盆往前往后是练出来的:
 * 太远够不着 (手离目标远扣分)、太往前会失衡甚至摔倒、蹲太深膝盖超出范围会疼.
 */
async function crouchReach(ctx, point, side, onEvent) {
  const r = ctx.r;
  // 地上的东西 (健身区的瑜伽砖): 平时那套 (骨盆最低到 0.5 倍髋高、上身前倾 16 度) 手够不着, 自检里每次差 25 cm、-2 分.
  // 单独一套: 蹲得更深, 上身往前探多少 (lean, 度) 也是练出来的
  const low = point.y < 0.3;
  const tr = low
    ? r.practice.attempt('crouchLow', { dist: [0.28, 0.6, 0.4], drop: [0.3, 0.62, 0.44], back: [-0.2, 0.04, -0.08], lean: [4, 40, 22] }, { explore: ctx.explore })
    : r.practice.attempt('crouch', { dist: [0.34, 0.75, 0.5], drop: [0.5, 0.8, 0.64], back: [-0.18, 0.04, -0.05] }, { explore: ctx.explore });
  const at = standNear(r, point, tr.params.dist);
  if (!at) throw new Error('够不着');
  await ctx.walk(at.pos, at.yaw);
  r.body.begin(low ? 'crouchLow' : 'crouch');
  r.poses.set('crouch', { side, target: point.clone(), drop: tr.params.drop, back: tr.params.back, lean: tr.params.lean ?? 0 });
  await ctx.wait(1.1, { hurry: false });
  const miss = r.poses.err ?? 0;  // 手离目标多远
  onEvent();
  await ctx.wait(0.35, { hurry: false });
  r.poses.set(null);
  await ctx.wait(0.6, { hurry: false });
  ctx.learn(tr, r.body.end(true), Math.max(0, miss - 0.04) * 8);
}

/** 拿起来: 先用行动层的 (走过去、蹲下或伸手的动捕); 矮台面挨着家具、它走不过去时, 蹲下伸手拿. */
async function grab(ctx, id) {
  const r = ctx.r, L = r.layer;
  try {
    await ctx.run({ do: 'pick', to: id });
    return;
  } catch (err) {
    // Preserve navigation failure for take() to clear movable obstacles and retry.
    if (L.items[id]?.grasp && !L.items[id].physical) throw err;
    // A cup's final binding gate must never fall through to the old unverified crouch attachment.
    const cup = L.items[id];
    if (String(err.code || '').startsWith('CUP_PICK_') || (cup?.grip === 'cup' && !cup.physical && cup.size)) throw err;
    if (!/走不到|没有合适/.test(err.message)) throw err;
  }
  const it = L.items[id];
  if (it.grasp && !it.physical) throw new Error('抓取接触未完成，不能用旧拿法绑定');
  const side = L._freeHand();
  if (!side) throw new Error('两只手都拿着东西');
  const p = it.mesh.position.clone();
  await crouchReach(ctx, p.clone().setY(p.y + it.half * 0.4), side, () => {
    it.origin = { on: it.on, pos: it.mesh.position.clone() };
    it.on = null;
    it.held = side;
  });
}

/** 放下: 同上, 行动层放不了时蹲下放. to: 放置目标 (有 place 的). */
async function place(ctx, to) {
  const r = ctx.r, L = r.layer;
  try {
    await ctx.run({ do: 'put', to });
    return;
  } catch (err) {
    const target = L.targets[to];
    const item = target && Object.hasOwn(target, 'itemId') ? L.items[target.itemId] : Object.values(L.items).find((i) => i.held);
    // A surface grasp must retain its identity and failure, never release another item as fallback.
    if ((target && Object.hasOwn(target, 'itemId')) || (item?.grasp && !item.physical)) throw err;
    if (String(err.code || '').startsWith('CUP_PUT_') || (item?.cupBinding && !item.physical)) throw err;
    if (!/走不到|没有合适/.test(err.message)) throw err;
  }
  const it = Object.values(L.items).find((i) => i.held), pl = L.targets[to]?.place;
  if (!it || !pl) throw new Error('放不了');
  if (it.grasp && !it.physical) throw new Error('归还接触未完成，不能吸附到落点');
  const p = new THREE.Vector3(...pl);
  await crouchReach(ctx, p.clone().setY(p.y + it.half * 2 + 0.04), it.held, () => L._release(it, p, to));
}

/**
 * 一次平衡动作 (树式、侧弯、前屈): 一直找平衡 (质心对着支撑脚, 骨盆跟着挪), 纠正得多快 (gain) 是练出来的; 单腿站要快, 慢了会倒.
 * 按体感打分 (摔、疼、失衡). 平时做 (瑜伽) 只在学会的附近稍微变; 健身区 (train) 放开了试.
 */
async function balanceTrial(ctx, name, seconds) {
  const r = ctx.r, P = r.poses;
  const skill = name.startsWith('tree') ? name : name.startsWith('bend') ? 'bend' : name === 'fold' ? 'fold' : null;
  const bal = skill ? r.practice.attempt(skill, { gain: [0.4, 9, skill.startsWith('tree') ? 1.5 : 2] }, { explore: !ctx.careful }) : null;
  const feet = name === 'treeR' ? ['left'] : name === 'treeL' ? ['right'] : ['left', 'right'];
  if (bal) r.body.begin(name);
  P.set(name, bal ? { balance: { feet, gain: bal.params.gain } } : {});
  await ctx.wait(seconds);
  return bal ? ctx.learn(bal, r.body.end(!r.body.fallen && !ctx.hurry)) : null;
}

/** 坐着双手捧着看 (小册子). */
function P_hold(r) { r.poses.set('diaryHold', { side: 'right' }); }

/** Re-level a real cup before withdrawing the arm, also during cancellation cleanup. */
async function settleSip(ctx, id) {
  const r = ctx.r;
  r.props.tilt(id, 0);
  const deadline = performance.now() + 3500;
  let stableSince = null;
  await new Promise((resolve, reject) => {
    const check = () => {
      const node = r.props.node(id), q = node?.getWorldQuaternion(new THREE.Quaternion());
      const up = q ? new THREE.Vector3(0, 1, 0).applyQuaternion(q).y : -1;
      const upright = Math.abs(r.liquid?.tiltNow?.[id] || 0) < .02 && up > Math.cos(.08);
      stableSince = upright ? (stableSince ?? performance.now()) : null;
      if (stableSince !== null && performance.now() - stableSince >= 200) { resolve(); return; }
      if (performance.now() >= deadline) { reject(new Error('杯子还没放平，停止后续移动')); return; }
      setTimeout(check, 40);
    };
    check();
  });
  r.poses.set(null);
  if (!r.poses.exitingSip) { await new Promise(resolve => setTimeout(resolve, 1000)); return; }
  // Observe the real final carry pose as well: W=0 can itself expose a second owner.
  const withdrawDeadline = performance.now() + 7500;
  let last = null, settledSince = null;
  await new Promise((resolve, reject) => {
    const check = () => {
      const now = performance.now(), node = r.props.node(id);
      const pos = node?.getWorldPosition(new THREE.Vector3()), q = node?.getWorldQuaternion(new THREE.Quaternion());
      const dt = last ? (now - last.at) / 1000 : 0;
      const stable = pos && q && dt > 0 && !r.poses.exitingSip && !r.poses.name && r.poses.W === 0
        && Math.abs(r.liquid?.tiltNow?.[id] || 0) < .02 && new THREE.Vector3(0, 1, 0).applyQuaternion(q).y > Math.cos(.08)
        && pos.distanceTo(last.pos) / dt < .06 && q.angleTo(last.q) / dt < .25;
      settledSince = stable ? (settledSince ?? now) : null;
      if (settledSince !== null && now - settledSince >= 400) { resolve(); return; }
      if (now >= withdrawDeadline) { reject(new Error('退杯交接还没稳定，停止后续移动')); return; }
      last = pos && q ? { at: now, pos, q } : null;
      setTimeout(check, 40);
    };
    check();
  });
}

/** 喝 (sip) / 吃 (bite) 手里的东西 times 口. */
async function consume(ctx, id, { kind = 'sip', times = 3, gap = [2.5, 5], lines = [] }) {
  const r = ctx.r;
  for (let i = 0; i < times; i++) {
    const side = r.layer.items[id]?.held;
    if (!side) return;
    if (lines[i]) ctx.say(lines[i]);
    const stable = kind === 'sip' && r.needs.actualConsumption;
    try {
      r.poses.set(kind, { side });
      await ctx.wait((kind === 'sip' ? 0.75 : 0.6) * r.poses.tempo, { hurry: false });
      if (kind === 'sip') r.props.tilt(id, 0.85);
      else { const m = r.layer.items[id].mesh; m.scale.multiplyScalar(0.8); }
      if (stable) {
        const deadline = performance.now() + 10000;
        while (performance.now() < deadline) {
          await ctx.wait(.1, { hurry: false });
          const sip = r.liquid.sipping[id];
          const felt = r.liquid.inspect(id);
          if ((sip?.got || 0) >= 3 || (sip?.got > 0 && felt.known && !felt.inferred && felt.category === 'hot')) break;
        }
      } else await ctx.wait((kind === 'sip' ? 1.8 : 0.9) * r.poses.tempo, { hurry: false });
    } finally {
      if (stable) await settleSip(ctx, id);
      else { r.props.tilt(id, 0); r.poses.set(null); }
    }
    const thermal = stable && r.liquid.inspect(id);
    if (thermal?.known && !thermal.inferred && thermal.category === 'hot') { ctx.say('刚才入口还烫，先放回去，晚一点再喝'); break; }
    if (ctx.hurry) break;
    if (i < times - 1) await ctx.wait(rand(...gap));
  }
}

/** 拿着喝的: 一半坐到阅读椅上喝, 一半站到窗边喝. */
async function drinkHeld(ctx, id, gives, lines) {
  const r = ctx.r;
  // 在哪喝: 阅读椅上、窗边、床边, 或者就站着 (每次不一样)
  const where = pickOne(['chair', 'chair', 'window', 'bed', 'stand']);
  if (where === 'chair') await ctx.sit('reading_chair');
  else if (where === 'bed') await ctx.sit('bed_side');
  else if (where === 'window') await ctx.run({ do: 'go', to: 'window' });
  else r.layer.lookAt('user', 3);
  r.v.setEmotion('happy');
  await consume(ctx, id, { kind: 'sip', times: 2 + Math.floor(Math.random() * 3), lines });
  r.needs.add(gives);
  r.v.setEmotion('neutral');
}

async function eatHeld(ctx, id, gives) {
  const r = ctx.r;
  await ctx.sit(Math.random() < 0.7 ? 'reading_chair' : 'bed_side');
  r.v.setEmotion('happy');
  await consume(ctx, id, { kind: 'bite', times: 4 + Math.floor(Math.random() * 3), gap: [1.2, 2.2], lines: ['她拆开零食', '咔嚓咔嚓…', '好吃!', '再来一口', '吃完啦'] });
  r.needs.add(gives);
  r.props.remove(id);
  r.v.setEmotion('neutral');
}

/** 醒来以后: 床头柜上有便利贴 (睡着时你发的消息) 就走过去撕下来, 一条一条回你. */
async function readNotes(ctx) {
  const r = ctx.r;
  if (!r.notes?.count || r.server?.connected?.() === false) return;  // 没连上服务端 (还在重启): 便利贴先留着
  const notes = r.notes.take();
  ctx.say(`床头柜上有 ${notes.length} 张便利贴…`);
  await ctx.beat(0.8, 1.6);
  try {
    await crouchReach(ctx, r.notes.at.clone(), 'right', () => r.notes.clear());
  } catch {
    r.notes.clear();  // 走不过去也照样看 (便利贴消失)
  }
  r.layer.lookAt('user', 4);
  ctx.say('她撕下便利贴, 一条条回你');
  for (let i = 0; i < notes.length; i++) {
    if (await r.server?.say(notes[i]) === false) {  // 回到一半又断了: 没回的贴回去, 连上了再回
      for (const text of notes.slice(i)) r.notes.add(text);
      ctx.say(`和服务端断开了, 还有 ${notes.length - i} 张没回, 先贴回床头柜`);
      return;
    }
  }
}

/** 醒来 (或者连回服务端) 知道的事: 学会了什么、刚才那版没跑起来退回去了. 说一次. */
function tellBootNews(ctx) {
  const news = ctx.life.bootNews;
  ctx.life.bootNews = '';
  if (!news) return;
  ctx.say(news);
  ctx.r.say?.(news);
}

/** 杯子在咖啡机上原来那个位置 (载入时量的, room-life.js 的 cupHome): 找回来的杯子放回这里. 返回放置目标 id. */
function cupSlot(r, id) {
  const p = r.cupHome?.[id], it = r.layer.items[id];
  if (!p || !it) return null;
  const key = `slot_${id}`;
  r.layer.targets[key] = { id: key, name: '咖啡机', place: [p[0], p[1] - it.half, p[2]], look: p, approach: r.scene.coffee.stand };
  return key;
}

/**
 * 找东西 (认知阶段 2, wm/cognition/): 页面不知道东西在哪, 走过去看了才知道. 每一步问服务端 (上行 room_search: 按她以为东西在哪、
 * 这次找过哪、经验排好的), 做完马上报这一刻看到的 (room_observe, 带这一步), 再问下一步; 拿到手、放弃、问了你就停.
 * 服务端没连上、没回、被叫走了 (hurry): 不找了. 每次找的经过记在 life.searchLog (自检看). 返回拿没拿到.
 */
async function findItem(ctx, id) {
  const r = ctx.r, rec = { item: id, steps: [], found: false };
  const { NAMES, standFor } = r.sightMap || legacySight;
  const t0 = performance.now();
  ctx.life.searchLog.push(rec);
  if (ctx.life.searchLog.length > 20) ctx.life.searchLog.shift();
  for (let k = 0; k < 24 && !ctx.hurry; k++) {
    const step = await r.server?.ask?.('room_search', { item: id, new: k === 0 }, ['room_step'], 3000);
    if (!step?.ok) { rec.stop = step ? step.reason || 'bad' : '没回'; break; }
    const [act, arg] = step.action;
    if (k === 0) {
      rec.knows = step.knows;
      r.log(`小屋: 找${NAMES[id]} · 她记得: ${step.knows}`);
    }
    if (act === 'give_up') { rec.steps.push({ action: step.action }); break; }
    ctx.say(step.say);
    let ok = true, why = '';
    try {
      if (act === 'go') { const s = standFor(arg); await ctx.walk(s.xz, s.yaw); }
      else if (act === 'open' && arg === 'fridge') { await r.props.fridge(true); ctx.later(() => r.props.fridgeOpen && r.props.fridge(false)); }
      else if (act === 'close' && arg === 'fridge') await r.props.fridge(false);
      else if (act === 'look') await ctx.beat(0.8, 1.6);
      else if (act === 'pick') {
        if (!r.props.asItem(arg)) throw new Error('拿不到');
        r.props.wake(arg);
        ctx.later(() => r.props.settle(arg));
        await grab(ctx, arg);
      } else if (act === 'ask') {
        r.layer.lookAt('user', 3);
        r.say?.(step.say);
        await ctx.wait(2.5);
      } else ok = false;  // 这个小屋做不了的一步 (比如开抽屉): 照样报没做成, 服务端会换
    } catch (err) {
      ok = false;
      why = err.message;
    }
    if (act === 'go' && r.sight.zone() !== arg) {  // 没走到那一处 (堵住了、停在半路): 她报不了那里看到什么, 再问也是同一步, 不找了
      rec.steps.push({ action: step.action, say: step.say, ok: false, why: why || '没走到那里' });
      rec.stop = '走不到';
      break;
    }
    const seen = await r.sight.report({ act: step.action, result: { ok } }, true);
    rec.steps.push({ action: step.action, say: step.say, ok, ...(why && { why }), changes: seen?.changes || null });
    if (act === 'ask') break;  // 问了你: 先不找了 (你回答的话以后接进来)
    if (act === 'pick' && ok && r.layer.items[id]?.held) { rec.found = true; break; }
  }
  rec.s = +((performance.now() - t0) / 1000).toFixed(1);
  r.log(`小屋: 找${NAMES[id]}${rec.found ? '找到了' : '没找到'} · ${rec.steps.length} 步 · ${rec.s} 秒`);
  return rec.found;
}

/** 打开冰箱, 伸手往里放 (onIn: 手到了的时候), 关上. */
async function intoFridge(ctx, onIn) {
  const r = ctx.r;
  const point = r.scene.fridge?.reach;
  let inside;
  if (point !== undefined) {
    if (!Array.isArray(point) || point.length !== 3 || !Array.from(point).every(Number.isFinite)) throw new Error('冰箱伸手位置配置无效');
    if (!r.props.node('Fridge_Door_Hinge')) throw new Error('找不到这间房的冰箱门，不能存放东西');
    inside = new THREE.Vector3(...point);
  } else inside = new THREE.Vector3(1.92, 0.72, -1.78);
  await ctx.run({ do: 'go', to: 'fridge' });
  await r.props.fridge(true);
  ctx.later(() => r.props.fridgeOpen && r.props.fridge(false));
  await ctx.reach('right', inside, onIn);
  await r.props.fridge(false);
}

// 可供性 (room-afford.js) 拿这几块积木组合新的事
export { crouchReach, freePlace, grab, homeOf, place, standNear };

// -- 每件事 -------------------------------------------------------------------------------------
const SCRIPTS = {
  ...createDeviceScripts({ grab, place, homeOf }),
  async inspect_temperature(ctx) {
    const r = ctx.r, cfg = r.scene.coffee;
    await ctx.walk(cfg.stand, Math.PI);
    const cup = cfg.cups.find(id => r.layer.items[id]?.held) || cfg.cups.find(id => r.sight?.sees(id));
    if (!cup) throw new Error('还没有看到可查看的杯子');
    const sensed = r.liquid?.inspect?.(cup);
    ctx.say(sensed?.text || '只能看到杯子，还没有冷热的感受证据');
    ctx.life.actionDetail = sensed || { known: false, category: 'unknown', text: '温度未知' };
    await ctx.wait(1.2);
  },
  /** 走过去或坐下, 待一阵 (电脑: 有工具在用时一直坐着). */
  async stay(ctx, a) {
    const r = ctx.r;
    if (a.do === 'sit') await ctx.sit(a.target);
    else await ctx.run({ do: 'go', to: a.target });
    ctx.say(`她${ctx.life.doingFor(a.id) || a.doing || a.name}`);
    const mood = r.needs.mood;
    r.v.setEmotion(a.emotion || (mood.need ? mood.emotion : 'neutral'));
    if (a.look) r.layer.lookAt(a.look, 8);
    if (a.wave) r.motion.applyIntent('act', [{ do: 'wave', side: 'R', amp: 1, spd: 1, at: 0.4 }], performance.now() / 1000);
    if (a.id === 'computer' && !ctx.life.tools && r.watching?.()) {  // 看你在电脑上做什么: 托着腮盯着屏幕想
      ctx.say(pickOne(['她坐在电脑前, 看你在忙什么', '她托着腮盯着屏幕, 好像在想什么']));
      r.poses.set('chinRest', { side: 'right' });
      ctx.later(() => r.poses.name === 'chinRest' && r.poses.set(null));
    }
    await ctx.wait(ctx.fast ? 6 : rand(a.stay[0], a.stay[1]));
    if (r.poses.name === 'chinRest') r.poses.set(null);
    while (a.id === 'computer' && ctx.life.tools > 0) {  // 你让她用工具 (操控电脑或手机) 时一直坐在电脑前
      ctx.say(`她在电脑前帮你操作${ctx.life.toolLabel ? ` · ${ctx.life.toolLabel}` : ''}…`);
      await ctx.wait(1, { hurry: false });
    }
    // QQ 那边在忙 (回消息、看 B 站、研究本事): 一直坐着, 状态栏写她在做什么; 点了别的家具、叫她就起来
    for (let doing; (doing = ctx.life.doingFor(a.id)) && !ctx.hurry;) {
      ctx.say(`她${doing}…`);
      await ctx.wait(2);
    }
    // 服务端在重启, 她就地打盹: 接着坐着, 等它回来 (Life.doze)
    for (let zz = 0; ctx.life.dozing;) {
      ctx.say(`她靠着椅背打盹 · 服务端在重启 ${'Zz'.repeat(1 + (zz++ % 3))}`);
      await ctx.wait(1, { hurry: false });
    }
    r.needs.add(a.gives);
    r.v.setEmotion('neutral');
    if (r.layer.seated) await ctx.run({ do: 'stand' });
  },

  /** 去冰箱拿一瓶饮料, 坐着或站在窗边慢慢喝, 空瓶放到小边桌上 (过一会儿收掉). */
  async drink(ctx, a) {
    const r = ctx.r, id = r.fridge.next();
    if (!id) throw new Error('冰箱里没有饮料了');
    ctx.say(`她${r.needs.v.water < 35 ? '渴了, ' : ''}去冰箱拿饮料…`);
    await ctx.run({ do: 'go', to: 'fridge' });
    await r.props.fridge(true);
    ctx.later(() => r.props.fridgeOpen && r.props.fridge(false));
    r.props.wake(id);
    ctx.later(() => r.props.settle(id));
    await ctx.run({ do: 'pick', to: id });
    r.needs.use('drinks');
    r.fridge.taken(id);
    await r.props.fridge(false);
    await drinkHeld(ctx, id, a.gives, ['她拧开瓶盖喝了一口', '咕嘟咕嘟…', '好喝～']);
    ctx.say('她把空瓶放到小边桌上');
    await place(ctx, freePlace(r, 'surf_table'));
    r.fridge.empty(id);
  },

  /**
   * 冲咖啡: 先走到咖啡角看一眼 —— 她最喜欢的粉色杯子在咖啡机上就用它; 不在 (被你收走了) 就按她的认知去找 (findItem),
   * 找到拿回来放在咖啡机上; 找不到就用咖啡机上别的杯子, 都没有就冲不了. 然后按开关、按出水, 等咖啡流完, 端起来喝, 杯子放回咖啡机上.
   * (以前开头直接查杯子在不在咖啡机上: 人还没走过去就知道了, 不在就不冲, 也不找.)
   */
  async coffee(ctx, a) {
    const r = ctx.r, cfg = r.scene.coffee;
    const { FAVORITE, NAMES } = r.sightMap || legacySight;
    const actual = r.needs.actualConsumption;
    ctx.say(actual ? '她去看看咖啡角的饮品…' : '她去冲咖啡…');
    await ctx.walk(cfg.stand, Math.PI);
    // 看得到的、在咖啡机上的杯子 (没有 sight 的老页面: 照原来按东西在不在算)
    const here = () => cfg.cups.filter((c) => r.props.node(c)?.visible && !r.layer.items[c]?.held
      && (r.sight ? r.sight.sees(c) === 'coffee_machine' : true));
    r.sight?.report({ act: ['go', 'coffee'], result: { ok: true } });
    const existing = () => actual ? here().filter(id => ['low', 'half', 'full'].includes(r.liquid.inspect(id).level)) : [];
    const chooseExisting = () => {
      const present = existing(), ready = present.filter(id => coffeeDecision(r.liquid.inspect(id), { beans: r.needs.stock.beans }).action === 'reuse');
      const candidates = ready.length ? ready : present;
      return candidates.includes(FAVORITE) ? FAVORITE : candidates[0];
    };
    let cup = chooseExisting() || (here().includes(FAVORITE) ? FAVORITE : null);
    if (!cup && r.sight && cfg.cups.includes(FAVORITE)) {
      ctx.say(`咖啡机上没有她的${NAMES[FAVORITE]}… 她四处找找`);
      r.v.setEmotion('surprised');
      if (await findItem(ctx, FAVORITE)) {
        r.v.setEmotion('happy');
        if (r.sight.zone() !== 'coffee') await ctx.walk(cfg.stand, Math.PI);
        const slot = cupSlot(r, FAVORITE);
        ctx.say(`找到啦, 她把${NAMES[FAVORITE]}拿回咖啡机上`);
        await (slot ? ctx.run({ do: 'put', to: slot }) : place(ctx, freePlace(r, 'surf_desk')));
        // 放杯子时她会挪几步, 可能挪到冰箱前 (离咖啡角站位只有 0.4 m, 那里看不到咖啡机上): 回到咖啡角站好再看
        if (r.sight.zone() !== 'coffee') await ctx.walk(cfg.stand, Math.PI);
        r.sight.report({ act: ['put', FAVORITE, 'coffee_machine'], result: { ok: true } });
        cup = here().includes(FAVORITE) ? FAVORITE : null;
      } else {
        if (ctx.hurry) throw new Error('找杯子的时候被叫走了');
        if (r.sight.zone() !== 'coffee') await ctx.walk(cfg.stand, Math.PI);
        if (here().length) ctx.say(`${NAMES[FAVORITE]}没找着… 先用别的杯子吧`);
      }
      r.v.setEmotion('neutral');
    }
    cup ??= here()[0];
    if (!cup) throw new Error('咖啡机上没有杯子');  // 走过来看过了 (不是人没过来就知道)
    r.props.asItem(cup);
    let observed = actual ? r.liquid.inspect(cup) : null;
    let decision = actual ? coffeeDecision(observed, { beans: r.needs.stock.beans }) : { action: 'brew' };
    if (actual && decision.action === 'observe') {
      r.layer.lookAt('coffee', 2);
      await ctx.wait(0.6, { hurry: false });
      observed = r.liquid.inspect(cup);
      decision = coffeeDecision(observed, { beans: r.needs.stock.beans });
    }
    if (actual && ['observe', 'wait', 'refuse'].includes(decision.action)) {
      ctx.life.actionDetail = { action: decision.action, consumed: false, observation: observed, text: decision.reason };
      throw new Error(decision.reason);
    }
    const brewed = decision.action === 'brew';
    const at = (n) => r.props.node(n)?.getWorldPosition(new THREE.Vector3());
    if (brewed) {
      const preparation = actual ? r.liquid.beginPreparation('beans', cup) : null;
      let prepared;
      try {
        ctx.say('她按下咖啡机的开关');
        await ctx.reach('right', at(cfg.power));
        await ctx.wait(0.8, { hurry: false });
        const cupPos = r.layer.items[cup].mesh.position.clone();
        await ctx.reach('right', at(cfg.brew), () => r.props.stream(cupPos));
        ctx.say('咖啡机开始出水，她看着杯子接咖啡');
        r.layer.lookAt('coffee', 7);
        await ctx.wait(6, { hurry: false });
      } finally {
        r.props.stream(null);
        if (preparation) prepared = r.liquid.endPreparation(preparation);
      }
      if (preparation && !prepared?.ok) throw new Error(prepared?.reason || '杯子没有接到咖啡');
      if (!actual) r.needs.use('beans');
    }
    else ctx.say(decision.reason);
    observed = actual ? r.liquid.inspect(cup) : null;
    const home = homeOf(r, cup, '咖啡机', cfg.stand);
    // 怎么端, 按她端东西的经验 (服务端 room_carry; 她站在咖啡机前喝, 不走): 拿放放慢、端稳、等它凉一点再端. 拿起 / 送到嘴边 /
    // 放下每一段感知到的 (洒了、烫了) 由 sight 标上是哪一段, 端完 (carryDone) 服务端记成经验. 没连上就照原来端
    const L = r.layer, plan = await r.sight?.carry({ cup, ...(!actual ? { hot: true } : observed?.known ? { hot: observed.category === 'hot' } : {}), route: 'none', wait_s: COOL_S });
    let waited = 0;
    if (plan?.choice?.wait) {
      ctx.say('她等咖啡凉一点再喝');
      const t0 = performance.now();
      await ctx.wait(COOL_S);
      waited = (performance.now() - t0) / 1000;
    }
    const handBefore = L.handSpeed, careBefore = L.carryCare;
    const steady = r.needs.actualConsumption;
    let reported = !plan;
    const restore = () => {
      if (r.sight) r.sight.act = null;
      L.handSpeed = handBefore;
      L.carryCare = careBefore;
      if (!reported) { reported = true; r.sight.carryDone({ waited, delivered: false }); }  // 被打断了: 感知到的照样记下
    };
    if (steady || plan?.choice?.hand === 'slow') L.handSpeed = HAND_SLOW;
    if (steady || plan?.choice?.care) L.carryCare = 1;
    ctx.later(restore);
    if (plan?.say) ctx.say(plan.say);
    r.props.wake(cup);
    ctx.later(() => r.props.settle(cup));
    if (steady) ctx.later(async () => {
      if (!L.items[cup]?.held) return;
      await settleSip(ctx, cup);
      if (r.sight) r.sight.act = 'put';
      await ctx.run({ do: 'put', to: home });
      r.props.settle(cup);
    });
    if (r.sight) r.sight.act = 'pick';
    await ctx.run({ do: 'pick', to: cup });
    if (r.sight) r.sight.act = 'lift';
    ctx.say('她端着咖啡吹了吹');
    if (steady) await ctx.wait(1.2, { hurry: false });
    else r.layer.lookAt('user', 3);
    const drunkBefore = r.liquid?.world.ledger.drunk || 0;
    await consume(ctx, cup, { kind: 'sip', times: 3, lines: ['她把杯子送到嘴边，试着小口喝', '她调整杯子的倾斜', '她慢慢放平杯子'] });
    const consumed = (r.liquid?.world.ledger.drunk || 0) - drunkBefore;
    if (!r.needs.actualConsumption) r.needs.add(a.gives);
    const sensed = r.liquid?.inspect?.(cup);
    if (sensed?.text) ctx.say(sensed.text);
    ctx.say('她把杯子放回咖啡机上');
    if (r.sight) r.sight.act = 'put';
    await ctx.run({ do: 'put', to: home });
    // 自己放回咖啡机上的: 报一声 (喝的时候常挪到冰箱前那一处, 那里看不到咖啡机上; 不报的话她以为杯子还在手里)
    if (r.layer.items[cup]?.on && r.sight?.sees(cup) !== 'coffee_machine') r.sight?.report({ act: ['put', cup, 'coffee_machine'], result: { ok: true } });
    if (plan) {
      await ctx.wait(1.6, { hurry: false });  // 放下那一下洒的, 液体那边停了 1.5 秒才报
      reported = true;
      await r.sight.carryDone({ waited });
    }
    restore();
    if (r.needs.actualConsumption) {
      if (consumed <= 0) {
        ctx.life.actionDetail = { action: brewed ? 'brew' : 'reuse', consumed: false, observation: sensed,
          text: '这次没有实际喝到；保留杯中剩余，没有结算饮用效果' };
        throw new Error(brewed ? '冲泡完成，但这次没有实际喝到；没有结算饮用效果' : '杯里还有剩余，但这次没能实际喝到；不强行倾杯，也没有结算饮用效果');
      }
      const saved = r.liquid.save(), queue = r.liquid.consumption.state;
      if (!saved || queue.pending.length || queue.preparations.length) throw new Error('已经实际喝到，但饮用效果尚未保存，等待重试结算');
      ctx.life.actionDetail = { ...sensed, action: brewed ? 'brew' : 'reuse', consumed: true, text: `已实际喝到并结算。${sensed?.text || ''}` };
    }
  },

  /** 吃零食 (投喂了收在电脑桌上的; 重新打开以后桌上的不在了, 就在原处重新摆一包). */
  async snack(ctx, a) {
    const r = ctx.r;
    let id = r.props.stash.find((s) => s.kind === 'snack' && r.layer.items[s.id])?.id;
    if (!id) id = r.props.spawn('snack', r.layer.targets[freePlace(r, 'surf_desk')].place, 'surf_desk');
    r.props.stash = r.props.stash.filter((s) => s.id !== id);
    ctx.say('她去拿零食');
    ctx.later(() => { if (r.layer.items[id] && !r.layer.items[id].held && r.layer.items[id].on !== 'surf_desk') r.props.remove(id); });
    await grab(ctx, id);
    r.needs.use('snacks');
    await eatHeld(ctx, id, a.gives);
  },

  /** 喝牛奶: 从冰箱里拿出来 (投喂过收进冰箱的), 喝完纸盒扔掉. */
  async milk(ctx, a) {
    const r = ctx.r;
    let id = null;
    await intoFridge(ctx, () => {
      id = r.props.spawn('milk', [0, 0, 0]);
      Object.assign(r.layer.items[id], { held: 'right', on: null });
    });
    ctx.later(() => id && r.props.remove(id));
    r.needs.use('milk');
    await drinkHeld(ctx, id, a.gives, ['她打开牛奶', '咕嘟咕嘟…', '喝了长高高']);
    r.props.remove(id);
    id = null;
  },

  /** 捡投喂的东西: 渴了饿了马上吃喝, 不然收起来 (饮料、牛奶放冰箱, 零食放电脑桌上, 咖啡豆倒进咖啡角的罐子). */
  async fetch(ctx) {
    const r = ctx.r, f = r.props.fed[0];
    if (!f) return;
    const feed = FEED[f.kind], acts = Object.fromEntries(ctx.life.acts.map((x) => [x.id, x]));
    ctx.say(`她看到地上的${feed.name}了!`);
    r.v.setEmotion('surprised');
    await grab(ctx, f.id);
    r.props.fed = r.props.fed.filter((x) => x.id !== f.id);
    r.v.setEmotion('happy');
    r.layer.lookAt('user', 3);
    ctx.say(`她捡起${feed.name}, 冲你笑了笑`);
    await ctx.wait(1.5, { hurry: false });
    const v = r.needs.v;
    if (f.kind === 'drink' && v.water < 75) {
      r.needs.use('drinks');
      await drinkHeld(ctx, f.id, acts.drink?.gives, ['她打开罐子喝了一口', '咕嘟咕嘟…', '谢谢你的饮料～']);
      r.props.remove(f.id);
    } else if (f.kind === 'milk' && (v.water < 70 || v.food < 60)) {
      r.needs.use('milk');
      await drinkHeld(ctx, f.id, acts.milk?.gives, ['她打开牛奶', '咕嘟咕嘟…', '谢谢你的牛奶～']);
      r.props.remove(f.id);
    } else if (f.kind === 'snack' && v.food < 80) {
      r.needs.use('snacks');
      await eatHeld(ctx, f.id, acts.snack?.gives);
    } else if (f.kind === 'snack') {
      ctx.say('她把零食收到电脑桌上, 留着慢慢吃');
      await place(ctx, freePlace(r, 'surf_desk'));
      r.props.stash.push({ id: f.id, kind: 'snack' });
    } else if (f.kind === 'beans') {
      ctx.say('她把咖啡豆拿去咖啡角');
      const cfg = r.scene.coffee, configured = cfg.beansNode !== undefined || cfg.beansReach !== undefined || cfg.beansStand !== undefined;
      let jar, stand = cfg.stand;
      if (configured) {
        if (cfg.beansNode !== undefined && (typeof cfg.beansNode !== 'string' || !cfg.beansNode)) throw new Error('咖啡豆罐节点配置无效');
        const node = cfg.beansNode !== undefined ? r.props.node(cfg.beansNode) : null;
        if (cfg.beansNode !== undefined && !node) throw new Error('找不到这间房的咖啡豆罐');
        if (cfg.beansReach !== undefined) {
          if (!Array.isArray(cfg.beansReach) || cfg.beansReach.length !== 3 || !Array.from(cfg.beansReach).every(Number.isFinite)) throw new Error('咖啡豆罐伸手位置配置无效');
          jar = new THREE.Vector3(...cfg.beansReach);
        } else if (node) jar = node.getWorldPosition(new THREE.Vector3());
        else throw new Error('这间房还没有咖啡豆存放目标');
        if (cfg.beansStand !== undefined) {
          if (!Array.isArray(cfg.beansStand) || cfg.beansStand.length !== 2 || !Array.from(cfg.beansStand).every(Number.isFinite)) throw new Error('咖啡豆罐站位配置无效');
          stand = cfg.beansStand;
        }
      }
      await ctx.walk(stand, Math.PI);
      if (!configured) {
        jar = r.props.node('IR_Item_Canister_0')?.getWorldPosition(new THREE.Vector3()) || new THREE.Vector3(2.3, 1.5, -1.9);
        jar.setY(Math.min(jar.y, 1.45));
      }
      await ctx.reach('right', jar, () => r.props.remove(f.id));
      ctx.say('咖啡豆收好啦, 可以冲好几杯');
    } else {
      ctx.say(`她把${feed.name}放进冰箱`);
      await intoFridge(ctx, () => {
        r.props.remove(f.id);
        if (f.kind === 'drink') r.fridge.restock();
      });
    }
  },

  /** 做瑜伽: 站到铺着的瑜伽垫上 (房间里本来就铺着一张, 卷着的那张是备用的), 一套动作: 举手伸展、侧弯、前屈、树式、盘腿冥想. */
  async yoga(ctx, a) {
    const r = ctx.r, cfg = r.scene.yoga, P = r.poses, falls0 = r.body.totals.falls;
    ctx.say('她走到瑜伽垫上');
    await ctx.walk(cfg.stand, cfg.yaw);
    r.v.setEmotion('calm');
    ctx.later(() => { P.set(null); r.layer.groundOn = true; r.v.engine.setAU('au45Blink', null); });
    // 每次的做法不一样: 举手伸展开头, 侧弯、前屈、树式挑几式、顺序打乱, 每式停的时间也不一样
    const more = [['bend', '侧弯'], ['fold', '前屈'], ['tree', '树式']].sort(() => Math.random() - 0.5).slice(0, 2 + Math.floor(Math.random() * 2));
    const seq = [['reach', '举手伸展']];
    for (const [k, label] of more) {
      if (k === 'bend') seq.push(['bendL', `向左${label}`], ['bendR', `向右${label}`]);
      else if (k === 'tree') seq.push(...(Math.random() < 0.5 ? [['treeR', label], ['treeL', `换一条腿, ${label}`]] : [['treeL', label], ['treeR', `换一条腿, ${label}`]]));
      else seq.push(['fold', label]);
    }
    for (const [name, label] of seq) {
      if (ctx.hurry) break;
      ctx.say(`她在做瑜伽 · ${label}`);
      await balanceTrial(ctx, name, rand(8, 14));
    }
    if (!ctx.hurry) {
      ctx.say('她盘腿坐下, 闭上眼睛冥想');
      r.layer.groundOn = false;  // 坐在地上: 行动层的"落地"会把骨盆往下拉到鞋底着地
      P.set('lotus');
      await ctx.wait(2, { hurry: false });
      r.v.engine.setAU('au45Blink', 1);
      await ctx.wait(15);
      r.v.engine.setAU('au45Blink', null);
      // 起身前先把腿松开: 直接淡回站姿的话两条腿从交叉直接插到站着, 脚从另一条小腿里扫过去 (自检量出 6.8 cm)
      P.set('uncross');
      await ctx.wait(0.9, { hurry: false });
    }
    P.set(null);
    await ctx.wait(1.2, { hurry: false });
    if (!r.body.fallen) r.layer.groundOn = true;
    if (r.body.totals.falls > falls0) {  // 摔了: 不练了, 开心只补一点
      while (r.body.fallen) await ctx.wait(0.5, { hurry: false });
      r.needs.add({ fun: 3 });
      ctx.say(pickOne(['今天先不练了, 有点疼…', '摔了一跤, 歇会儿再练']));
      return;
    }
    r.needs.add(a.gives);
    r.v.setEmotion('happy');
    ctx.say(pickOne(['做完瑜伽, 浑身舒服～', '伸展完, 身体轻松多了', '呼——心情好多了']));
  },

  /**
   * 睡觉, 一步一步来: 走到床边坐下 -> 两条腿收上床沿 -> 转过来坐在床上 -> 往后躺下 -> 伸手抓住叠在床尾的被子边 ->
   * 两只手把被子拉到胸口 (被子边跟着手走, 被子每帧按身体重新铺) -> 手放到被子上 -> 闭眼. 晚上睡到早上 7 点, 白天睡几分钟;
   * 点她、和她说话会醒: 睁眼, 躺着伸个懒腰, 把被子推到腿上, 坐起来, 腿放下床, 站起来再伸个懒腰.
   */
  async sleep(ctx) {
    const r = ctx.r, cfg = r.scene.sleep, L = r.layer, P = r.poses, V = THREE.Vector3;
    const s = L.hipHeight / 0.78, span = (L.rest.head?.y ?? L.rest.hips.y + 0.6) - L.rest.hips.y;
    const lie = r.practice.attempt('lie', { head: [-0.15, 0.15, 0], up: [0.05, 0.13, 0.085] }, { explore: !ctx.careful });  // 头往枕头里还是往外 (往里插进枕头会疼)、骨盆多高
    const hipsZ = cfg.headZ + lie.params.head + span;
    const bed = { top: cfg.top, yaw: cfg.yaw, x: cfg.x, zFoot: cfg.zFoot ?? 0.05, zChest: hipsZ - span * 0.5, H: L.hipHeight,
      hips: new V(cfg.hipsX, cfg.top + lie.params.up * s, hipsZ) };
    const blanket = r.props.bedBlanket ??= r.props.blanket(bed);  // 叠在床尾, 一直留着
    const bones = {};
    const read = () => {
      for (const n of ['hips', 'spine', 'chest', 'neck', 'leftUpperLeg', 'leftLowerLeg', 'leftFoot', 'leftToes', 'rightUpperLeg', 'rightLowerLeg',
        'rightFoot', 'rightToes', 'leftUpperArm', 'leftLowerArm', 'leftHand', 'rightUpperArm', 'rightLowerArm', 'rightHand',
        ...['Middle', 'Index', 'Little', 'Thumb'].flatMap((f) => [`left${f}Distal`, `right${f}Distal`])]) {
        const b = r.v.vrm.humanoid.getNormalizedBoneNode(n);
        if (b) (bones[n] ??= new V()).copy(b.getWorldPosition(new V()));
      }
      return bones;
    };
    let cover = 0, draping = false;
    const off = r.props.every(() => { if (draping) blanket.drape(read(), cover); });
    ctx.later(() => {
      off();
      draping = false;
      cover = 0;
      blanket.drape(null, 0);  // 出了意外 (被打断、出错) 也把被子叠回床尾
      P.set(null);
      r.surface?.set(null);
      r.v.engine.setAU('au45Blink', null);
      r.needs.sleeping = false;
      ctx.life.sleeping = false;
    });
    ctx.say(pickOne(['她打了个哈欠, 走到床边…', '她揉揉眼睛, 去床边…', '她困了, 慢慢走到床边…']));
    await ctx.sit(cfg.seat);
    L.look.until = 0;
    bed.edge = r.v.vrm.humanoid.getNormalizedBoneNode('hips').getWorldPosition(new V());
    bed.edgeYaw = L.seated?.seat?.yaw ?? Math.PI / 2;
    bed.sit = new V(bed.edge.x + (cfg.hipsX - bed.edge.x) * 0.5, cfg.top + 0.1 * s, hipsZ);
    draping = true;
    ctx.say('她把腿收上床沿…');
    P.set('tuck', { bed });
    await ctx.wait(1.1, { hurry: false });
    ctx.say('她转过身坐到床上…');
    P.set('longSit', { bed });
    await ctx.wait(1.5, { hurry: false });
    ctx.say('她往后躺下…');
    P.set('lie', { bed });
    // 头发落在床上: 球顶在头那里 (以前顶在胸口, 头发那里的球面低 6-14 cm, 头发落进床垫); 往腰那边低下去, 裙子落在被子下面
    // (试过球顶在腰、半径 6 m: 裙子被托高, 从被子里戳出来, 自检量出 600 个顶点)
    r.surface?.set(new V(cfg.hipsX, cfg.top - 0.01, hipsZ - span), 1.6);
    await ctx.wait(1.2, { hurry: false });
    r.body.begin('lie');
    await ctx.wait(1.4, { hurry: false });
    ctx.learn(lie, r.body.end(true));
    // 两只手抓住叠在腿上的被子边, 一起往上拉; 被子边跟着手走
    const hands = { left: new V(), right: new V() };
    const lx = Math.cos(bed.yaw), lz = -Math.sin(bed.yaw);
    const atEdge = (c) => {
      const z = blanket.edgeZ(c, bones);
      for (const [k, sgn] of [['left', 1], ['right', -1]]) {
        const x = cfg.hipsX + sgn * 0.17 * s * lx, zz = z + sgn * 0.17 * s * lz;
        hands[k].set(x, blanket.surface(x, zz) + 0.035, zz - 0.02);
      }
    };
    atEdge(0);
    ctx.say('她伸手去拉被子');
    P.set('lieHands', { bed, hands });
    await ctx.wait(1, { hurry: false });
    await r.props.tween(rand(1.4, 2), (t) => { cover = t; atEdge(cover); });
    // 手放到肚子上, 被子盖着手 (被子一直跟着身体铺, 手放下去时从上面盖过来; 以前手放稳之前被子就不动了,
    // 手一半露在被子上一半陷在被子里, 自检量出来 4000 个顶点戳出被子).
    // 位置按此刻的骨盆、胸口定, 两只手并排: 以前按骨盆往头那边 0.1 m、高度取那一点的被子面, 躯干短的模型那一点在被子上边以外,
    // 量出来是床面, 手的目标在身体里 3-9 cm, 防穿模层每帧往外推, 手一直在抖. 高度从身体轴线往上一点开始, 姿势层把它挪到
    // 身体碰撞体外面 (room-poses.js 的 clear), 正好落在肚子上
    read();
    const chest = bones.chest || bones.spine, belly = Math.max(blanket.edgeZ(1, bones) + 0.03, bones.hips.z + (chest.z - bones.hips.z) * 0.3);
    hands.left.set(cfg.hipsX + 0.06 * s, bones.hips.y + 0.02, belly);
    hands.right.set(cfg.hipsX - 0.06 * s, bones.hips.y + 0.02, belly);
    P.set('lieRest', { bed, hands });
    await ctx.wait(1.6, { hurry: false });
    r.v.engine.setAU('au45Blink', 1);
    r.needs.sleeping = true;
    ctx.life.sleeping = true;
    // 睡着以后才整理 (用户要的: 睡觉 = 把今天的对话整理进长期记忆、清掉短期上下文; 整理完以前一动不动, 叫不醒)
    ctx.life.sleepLock = true;
    r.lights?.night(true);
    r.server?.mic?.(false);  // 睡着时你说话不叫醒她 (麦克风先关, 醒了再开)
    ctx.later(() => { r.lights?.night(false); ctx.life.sleepLock = false; r.server?.mic?.(true); });
    const reason = ctx.life.sleepReason || (r.needs.fatigue >= 0.7 ? 'tired' : isNight() ? 'night' : 'energy');
    ctx.life.sleepReason = null;
    const t0 = performance.now();
    if (reason === 'restart') {
      // 服务端在重启 (看守进程先发了 server_restart; self-evolution.md 4.3): 不整理记忆 (服务端不在), 一直睡到它回来 (hello) 再醒,
      // 这期间你的话都变便利贴, 点她也不醒 (醒了也回不了你)
      let zz = 0;
      r.busyChip?.('服务端在重启…');
      while (!ctx.life.serverBack && performance.now() - t0 < RESTART_NAP_MAX_S * 1000) {
        ctx.say(`她睡着了 · 服务端在重启, 醒来再回你 ${'Zz'.repeat(1 + (zz++ % 3))}`);
        await ctx.wait(1, { hurry: false });
      }
      r.busyChip?.(null);
      ctx.life.sleepLock = false;
      ctx.life.restarting = false;
      ctx.life.lastSleep = { reason, ok: !!ctx.life.serverBack, facts: null, ms: Math.round(performance.now() - t0), reply: null };
      r.log(`小屋: 服务端重启时睡了一觉, ${ctx.life.lastSleep.ms} ms${ctx.life.serverBack ? '' : ', 没等到服务端回来'}`);
    } else {
      ctx.say('她睡着了 · 正在整理今天的对话…');
      r.busyChip?.('整理今天的对话…');
      const res = r.server ? await r.server.ask('sleep', { reason, chars: r.needs.load }, ['sleep_done'], ctx.fast ? 15000 : 90000) : null;
      if (!res?.ok) r.server?.reset();  // 服务端还不会整理: 至少把短期上下文清掉
      r.busyChip?.(null);
      r.needs.rested();
      ctx.life.sleepLock = false;
      ctx.life.lastSleep = { reason, ok: !!res?.ok, facts: res?.facts ?? null, ms: Math.round(performance.now() - t0), reply: res ? res.type : null };
      r.log(`小屋: 睡觉整理 ${res ? (res.ok ? `完成, 记下 ${res.facts ?? 0} 件` : `没整理 (${res.reason || '不支持'}), 只清了短期上下文`) : '服务端没回, 只清了短期上下文'}, ${ctx.life.lastSleep.ms} ms`);
      ctx.say(res?.ok ? `整理好了${res.facts ? `, 记下了 ${res.facts} 件事` : ''}…` : '她睡得很沉…');
      // 困了、你让她去睡的: 整理完歇一小会儿就起床; 夜里、没精神了睡的: 接着睡到早上 (或者精神满了)
      const wakeSoon = reason === 'tired' || reason === 'user';
      const night = isNight();
      const until = performance.now() + (ctx.fast ? 4 : wakeSoon ? rand(8, 15) : night ? 9 * 3600 : rand(180, 420)) * 1000;
      let zz = 0;
      while (performance.now() < until && !ctx.hurry) {
        if (night && herDate().getHours() === 7) break;
        if (!night && r.needs.v.energy >= 98) break;
        ctx.say(`她盖着被子睡着了 ${'Zz'.repeat(1 + (zz++ % 3))}`);
        await ctx.wait(4);
      }
    }
    // 醒来
    ctx.life.sleeping = false;
    r.needs.sleeping = false;
    r.lights?.night(false);
    r.server?.mic?.(true);
    ctx.say(ctx.hurry ? '她被你叫醒了, 揉揉眼睛' : '她醒了, 揉揉眼睛');
    r.v.engine.setAU('au45Blink', null);
    draping = true;
    P.set('lieStretch', { bed });
    await ctx.wait(1.6, { hurry: false });
    atEdge(1);
    P.set('lieHands', { bed, hands });
    await ctx.wait(0.8, { hurry: false });
    await r.props.tween(1.3, (t) => { cover = 1 - t; atEdge(cover); });
    ctx.say('她坐起来…');
    P.set('longSit', { bed });
    r.surface?.set(null);
    await ctx.wait(1.5, { hurry: false });
    P.set('tuck', { bed });
    await ctx.wait(1, { hurry: false });
    P.set(null);  // 回到坐在床边 (腿放下床)
    await ctx.wait(1.2, { hurry: false });
    draping = false;
    blanket.drape(null, 0);
    await ctx.run({ do: 'stand' });
    P.set('reach');  // 伸个懒腰
    await ctx.wait(2.5, { hurry: false });
    P.set(null);
    r.v.setEmotion('happy');
    ctx.say(pickOne(['睡醒啦～', '睡得好香～', '精神满满!']));
    await readNotes(ctx);
    tellBootNews(ctx);
  },

  /** 断线那阵子你发的话贴在床头柜上: 连回服务端以后去撕下来一条条回, 再说说连回来时知道的事. */
  async notes(ctx) {
    await readNotes(ctx);
    tellBootNews(ctx);
  },

  /**
   * 健身区 (用户要的: 健身区是她优化自己动作、自我训练、调整姿态的地方): 走到瑜伽垫上, 脚下浮出一层淡淡的网格 (校准重心),
   * 挑练得最差的两样平衡动作各试两次 (放开了试), 再蹲下摸一下地上的瑜伽砖 (练站多远、蹲多深). 练完说一句心得.
   */
  async train(ctx, a) {
    const r = ctx.r, cfg = r.scene.yoga;
    ctx.say('她去健身区练练动作');
    await ctx.walk(cfg.stand, cfg.yaw);
    const grid = r.props.gridAt?.(r.layer.char);
    ctx.later(() => grid?.dispose());
    r.practice.scale = 1.3;
    ctx.later(() => { r.practice.scale = 0.35; r.poses.set(null); });
    const sum = r.practice.summary(), n0 = r.practice.history.length;
    const weak = ['treeR', 'treeL', 'fold', 'bend'].map((k) => ({ k, avg: sum[k]?.avg ?? 0, n: sum[k]?.n ?? 0 }))
      .sort((x, y) => x.avg - y.avg || x.n - y.n).slice(0, 2);
    for (const w of weak) {
      for (let i = 0; i < 2 && !ctx.hurry; i++) {
        const name = w.k === 'bend' ? pickOne(['bendL', 'bendR']) : w.k;
        ctx.say(`她在健身区校准动作 · ${LABEL_ZH[w.k]} (第 ${(sum[w.k]?.n ?? 0) + i + 1} 次)`);
        await balanceTrial(ctx, name, rand(6, 10));
        r.poses.set(null);
        await ctx.beat(1.5, 3);
      }
    }
    const block = r.props.node('IR_Item_Yoga_Block_0');
    if (block && !ctx.hurry) {
      ctx.say('她在健身区校准动作 · 蹲下摸瑜伽砖');
      const p = new THREE.Box3().setFromObject(block).getCenter(new THREE.Vector3());
      await crouchReach(ctx, p.setY(p.y + 0.06), 'right', () => {}).catch(() => {});
    }
    const tried = r.practice.history.slice(n0), better = tried.filter((h) => h.better).length;
    r.needs.add(a.gives);
    ctx.say(tried.some((h) => h.falls) ? '练的时候摔了一下, 不过找到点感觉了' : better ? `练完了, 有 ${better} 样比原来稳` : '练完了, 动作还在找感觉');
  },

  /**
   * 写日记、翻日记 (用户要的: 打开日记本 AI 才能读到知识库, 写日记要真的用这个本子):
   * 拿起日记本 (小边桌上那本摊开的书), 坐下, 左手把本子按在腿上, 翻开的那一下才告诉服务端"日记本开了" (上行 diary open),
   * 服务端那时才放行知识库, 回她想起来的几条 (做成房间外面的一张小卡片); 右手拿着笔在本子上写写画画, 服务端同时写一段今天的日记;
   * 合上本子 (diary close) 断开, 放回小边桌. 拖进来的文件 (r.docs) 也在这时一起记进去. 服务端还不支持时照样做动作, 只是没有内容.
   */
  async diary(ctx, a) {
    const r = ctx.r, P = r.poses, id = r.props.asItem('IR_Item_Open_Book');
    if (!id) throw new Error('日记本不见了');
    const it = r.layer.items[id];
    ctx.say(a.going ? `她${a.going}…` : '她去拿日记本…');
    r.props.wake(id);
    ctx.later(() => r.props.settle(id));
    await grab(ctx, id);
    if (it.held === 'right') it.held = 'left';  // 左手拿本子, 右手写
    await ctx.sit(pickOne(['reading_chair', 'reading_chair', 'bed_side']));
    const book = () => r.layer.items[id].mesh.position;
    P.set('diaryHold', { side: 'right' });
    await ctx.beat(1, 2);
    // 翻开: 这一下才放行知识库
    ctx.say('她翻开日记本…');
    r.busyChip?.('翻日记…');
    const sparkle = r.props.sparkles(book);
    ctx.later(() => { sparkle.dispose(); r.busyChip?.(null); r.server?.tell?.('diary', { open: false }); });
    const docs = r.docs?.take() || [];
    for (const d of docs) r.server?.tell?.('doc_drop', d);  // 拖进来的文件: 翻开日记本的时候一起记进去
    const recall = await r.server?.ask('diary', { open: true, intent: ctx.life.diaryIntent || '' }, ['diary_recall'], 8000);
    ctx.life.diaryIntent = '';
    r.busyChip?.(null);
    const items = recall?.items || [];
    if (items.length) r.memoryCard?.({ title: '她想起来…', text: items[0].text, day: items[0].day });
    ctx.say(items.length ? '她看着日记, 想起了一些事' : docs.length ? `她把${docs[0].name}的内容记进日记里` : '她翻了翻日记本');
    await ctx.beat(2, 4);
    // 写: 右手拿笔在本子上写写画画, 服务端写今天的日记
    ctx.say('她在日记本上写写画画…');
    P.set('diaryWrite', { side: 'right', book });
    const written = r.server?.ask('diary', { open: true, write: true }, ['diary_written'], 20000);
    await ctx.wait(ctx.fast ? 3 : rand(8, 16));
    const w = await written;
    if (w?.text) r.memoryCard?.({ title: '今天的日记', text: w.text });
    P.set('diaryHold', { side: 'right' });
    await ctx.beat(1, 2);
    ctx.say('她合上日记本');
    sparkle.dispose();
    r.server?.tell?.('diary', { open: false });
    P.set(null);
    r.needs.add(a.gives);
    await place(ctx, freePlace(r, 'surf_table'));
  },

  /** 把一件小东西 (小兔子、书、水壶、靠垫) 拿到别的台面上. */
  async tidy(ctx) {
    const r = ctx.r, carry = r.scene.carry || {}, options = [];
    for (const [id, c] of Object.entries(carry)) {
      if (!r.props.asItem(id)) continue;
      const it = r.layer.items[id];
      if (it.held || !r.props.node(id).visible) continue;
      const p = it.mesh.position;
      for (const to of c.to) {
        const pl = r.layer.targets[to]?.place;
        if (pl && Math.hypot(pl[0] - p.x, pl[2] - p.z) > 0.5) options.push({ id, to, name: c.name });
      }
    }
    if (!options.length) throw new Error('没有要收拾的');
    const o = pickOne(options), where = r.layer.targets[o.to].name;
    ctx.say(`她去拿${o.name}`);
    r.props.wake(o.id);
    ctx.later(() => r.props.settle(o.id));
    await grab(ctx, o.id);
    ctx.say(`她把${o.name}拿到${where}`);
    await place(ctx, freePlace(r, o.to));
    ctx.say(`她把${o.name}放到了${where}`);
  },

  /** 你拖进来的文件: 小册子掉在电脑桌上, 她走过去拿起来翻一翻, 再去写日记 (翻开日记本时把内容记进知识库). */
  async readDoc(ctx) {
    const r = ctx.r, d = r.docs?.peek();
    if (!d?.item) return;
    ctx.say(`桌上多了一本小册子: ${d.name}`);
    r.v.setEmotion('surprised');
    await grab(ctx, d.item);
    r.v.setEmotion('calm');
    await ctx.sit('desk_chair');
    P_hold(r);
    ctx.say(`她翻着《${d.name}》…`);
    await ctx.wait(ctx.fast ? 3 : rand(10, 18));
    r.poses.set(null);
    r.props.remove(d.item);  // 看完收起来 (内容等写日记时记进去)
    d.item = null;
    ctx.life.diaryIntent = d.name;
    ctx.life.order.unshift('diary');
  },

  /** 换个摆法: 挑一件能挪的家具和一个合适的新位置, 推过去. */
  async rearrange(ctx) {
    const r = ctx.r, list = r.mover.candidates();
    if (!list.length) throw new Error('没有能换的摆法');
    const { key, to } = pickOne(list);
    await r.mover.push(ctx, key, to);
  },

  /** 她自己发现的事 (可供性: 看书、浇花、开关灯、擦洒出来的、抱小兔子、四处看看; room-afford.js 按动词用积木做, 记下她的感受). */
  async afford(ctx, a) {
    if (!ctx.r.afford) throw new Error('没有可供性表');
    await ctx.r.afford.run(ctx, a);
  },
};

/** 冰箱里的饮料: 按配置的顺序摆, 库存几瓶摆几瓶. 拿走的空瓶放在外面, 过一会儿收掉 (藏起来, 投喂了再补回冰箱). */
export class Fridge {
  constructor(r) {
    this.r = r;
    this.slots = (r.scene.fridge?.items || []).filter((id) => r.props.asItem(id)).map((id) => ({ id, state: 'in' }));
    for (const s of this.slots) r.props.glb[s.id].noSave = true;  // 冰箱里的不记位置: 每次打开按库存重新摆
    const n = Math.min(this.slots.length, r.needs.stock.drinks);
    this.slots.forEach((s, i) => { if (i >= n) { s.state = 'gone'; r.props.hide(s.id); } });
  }

  next() { return this.slots.find((s) => s.state === 'in')?.id || null; }
  taken(id) { const s = this.slots.find((x) => x.id === id); if (s) s.state = 'out'; }

  /** 空瓶放下了: 90 秒后收掉. */
  empty(id) {
    const s = this.slots.find((x) => x.id === id);
    if (!s) return;
    s.state = 'empty';
    setTimeout(() => {
      if (s.state !== 'empty' || this.r.layer.items[id]?.held) return;
      this.r.props.reset(id, false);
      s.state = 'gone';
    }, 90000);
  }

  /** 补一瓶 (投喂的饮料放进冰箱了). */
  restock() {
    const s = this.slots.find((x) => x.state === 'gone');
    if (!s) return;
    this.r.props.reset(s.id, true);
    s.state = 'in';
  }
}

/**
 * 挪家具: movable 里每件家具的节点、牵连的场景条目 (碰撞盒、站位、座位、放东西的点、点击盒子) 和几个候选位置.
 * 换位置前先验: 在地板里、不压着别的家具、推过去的一路不撞东西、挪完以后哪都还走得到 (重新建寻路网格试一遍).
 */
export class Mover {
  constructor(r) {
    this.r = r;
    this.cfg = r.scene.movable || {};
    this.base = {};
    this.pose = {};
    this.moving = null;
    for (const [key, m] of Object.entries(this.cfg)) {
      const node = r.props.node(m.node);
      if (!node) continue;
      const defs = {};
      for (const id of [...(m.objects || []), ...(m.decor || [])]) { const e = this._entry(id); if (e) defs[id] = structuredClone(e); }
      const picks = {};
      for (const id of m.picks || []) { const a = r.scene.life.find((x) => x.id === id); if (a?.pick) picks[id] = structuredClone(a.pick); }
      this.base[key] = { node, x: node.position.x, z: node.position.z, yaw: node.rotation.y, defs, picks };
      this.pose[key] = { x: node.position.x, z: node.position.z, yaw: node.rotation.y };
    }
    this.slid = {};    // 带滑轮的椅子拉出来多少 (座位 id -> 0 塞在桌下 ... 1 拉出来 scoot 米)
    this.tucked = {};  // 拉出来之前 (塞在桌下) 的位置
    // 原摆法下就走不到跟前的站位 (阅读椅的站位在椅子和前墙之间的死角, 坐下时行动层另找起点) 验的时候不算
    const grid = buildGrid(r.scene);
    this.checked = new Set(r.scene.objects.filter((o) => {
      if (!o.approach || o.id === 'user') return false;
      const g = nearestWalkable(grid, o.approach[0], o.approach[1]);
      return Math.hypot(g[0] - o.approach[0], g[1] - o.approach[1]) < 0.45;
    }).map((o) => o.id));
  }

  _entry(id) { return this.r.scene.objects.find((o) => o.id === id) || (this.r.scene.decor || []).find((o) => o.id === id); }

  /** 摆到 p ({x, z, yaw}): 节点和牵连的场景条目跟着平移、转动. */
  apply(key, p) {
    const b = this.base[key], d = p.yaw - b.yaw, c = Math.cos(d), s = Math.sin(d);
    const T = ([x, z]) => { const dx = x - b.x, dz = z - b.z; return [p.x + c * dx + s * dz, p.z - s * dx + c * dz]; };
    b.node.position.set(p.x, b.node.position.y, p.z);
    b.node.rotation.y = p.yaw;
    b.node.updateMatrixWorld(true);
    for (const [id, def] of Object.entries(b.defs)) {
      const o = this._entry(id);
      if (!o) continue;
      if (def.box) {
        const [sx, sz] = def.box.size, ac = Math.abs(c), as = Math.abs(s);
        o.box = { ...def.box, center: T(def.box.center), size: [sx * ac + sz * as, sx * as + sz * ac] };
      }
      if (def.approach) o.approach = T(def.approach);
      if (Array.isArray(def.look)) { const [lx, lz] = T([def.look[0], def.look[2]]); o.look = [lx, def.look[1], lz]; }
      if (def.seat) o.seat = { ...def.seat, pos: T(def.seat.pos), yaw: def.seat.yaw + d };
      if (def.place) { const [px, pz] = T([def.place[0], def.place[2]]); o.place = [px, def.place[1], pz]; }
    }
    for (const [id, pk] of Object.entries(b.picks)) {
      const a = this.r.scene.life.find((x) => x.id === id);
      if (a) a.pick = { ...pk, center: T(pk.center) };
    }
    this.pose[key] = { ...p };
  }

  _boxes(key) {
    const m = this.cfg[key];
    return [...(m.objects || []), ...(m.decor || [])].map((id) => this._entry(id)?.box).filter(Boolean);
  }

  _overlaps(key) {
    const m = this.cfg[key], mine = new Set([...(m.objects || []), ...(m.decor || [])]);
    const others = blockers(this.r.scene).filter((o) => !mine.has(o.id)).map((o) => o.box);
    const xs = this.r.scene.floor.map((q) => q[0]), zs = this.r.scene.floor.map((q) => q[1]);
    const [fx0, fx1, fz0, fz1] = [Math.min(...xs) + 0.03, Math.max(...xs) - 0.03, Math.min(...zs) + 0.03, Math.max(...zs) - 0.03];
    for (const a of this._boxes(key)) {
      const [ax, az] = a.center, [aw, ad] = a.size;
      if (ax - aw / 2 < fx0 || ax + aw / 2 > fx1 || az - ad / 2 < fz0 || az + ad / 2 > fz1) return true;
      for (const o of others) {
        const [bx, bz] = o.center, [bw, bd] = o.size;
        if (Math.abs(ax - bx) < (aw + bw) / 2 + 0.05 && Math.abs(az - bz) < (ad + bd) / 2 + 0.05) return true;
      }
    }
    return false;
  }

  /** 从她脚下泛洪: 走得到的格子. */
  _reach(grid, from) {
    const seen = new Uint8Array(grid.w * grid.h), q = [];
    const [i0, j0] = toCell(grid, ...from);
    if (!walkable(grid, i0, j0)) return seen;
    seen[j0 * grid.w + i0] = 1;
    q.push(i0, j0);
    for (let k = 0; k < q.length; k += 2) {
      const i = q[k], j = q[k + 1];
      for (const [di, dj] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
        const a = i + di, b = j + dj;
        if (walkable(grid, a, b) && !seen[b * grid.w + a]) { seen[b * grid.w + a] = 1; q.push(a, b); }
      }
    }
    return seen;
  }

  /** 这个位置行不行 (一路推过去不撞东西; 挪完以后每个站位附近 0.45 m 内都有走得到的格子; 她不在新位置里). */
  valid(key, to) {
    const r = this.r, from = { ...this.pose[key] };
    let ok = true;
    for (let i = 1; i <= 6 && ok; i++) {
      const t = i / 6;
      this.apply(key, { x: from.x + (to.x - from.x) * t, z: from.z + (to.z - from.z) * t, yaw: from.yaw + (to.yaw - from.yaw) * t });
      ok = !this._overlaps(key);
    }
    if (ok) {
      const grid = buildGrid(r.scene), layer = r.layer;
      const start = nearestWalkable(grid, layer.char.x, layer.char.z);
      ok = Math.hypot(start[0] - layer.char.x, start[1] - layer.char.z) < 0.4;  // 不然她会被压在下面
      const seen = ok ? this._reach(grid, start) : null;
      for (const o of r.scene.objects) {
        if (!ok || !this.checked.has(o.id)) continue;
        const g = nearestWalkable(grid, o.approach[0], o.approach[1]), [i, j] = toCell(grid, ...g);
        ok = Math.hypot(g[0] - o.approach[0], g[1] - o.approach[1]) < 0.45 && !!seen[j * grid.w + i];
      }
      // 挪的是椅子、沙发这种: 座位前面要站得下 (不然她只能从椅背后面上去)
      for (const id of this.cfg[key].objects || []) {
        const o = this._entry(id);
        if (ok && o?.seat && !o.seat.scoot) ok = seatFrontOk(grid, o) || seatFrontOk(buildGrid({ ...r.scene, agentRadius: SQUEEZE }), o);
      }
    }
    this.apply(key, from);
    return ok;
  }

  /** 现在能换去的位置: 配置里的几个, 加上原来位置周围取样 (0.35 / 0.6 m 八个方向, 转 0 / ±20°); 离现在够远、验得过的, 最多 limit 个. */
  candidates(limit = 6) {
    const out = [];
    const keys = Object.keys(this.cfg).filter((k) => this.base[k] && this.cfg[k].rearrange !== false).sort(() => Math.random() - 0.5);
    for (const key of keys) {
      const b = this.base[key], cur = this.pose[key];
      const spots = (this.cfg[key].spots || []).map(([x, z, yaw]) => ({ x, z, yaw: b.yaw + (yaw || 0) }));
      for (const rr of [0.35, 0.6]) for (let k = 0; k < 8; k++) for (const dy of [0, 0.35, -0.35]) {
        spots.push({ x: b.x + Math.sin((k / 8) * Math.PI * 2) * rr, z: b.z + Math.cos((k / 8) * Math.PI * 2) * rr, yaw: b.yaw + dy });
      }
      let n = 0;
      for (const to of spots.sort(() => Math.random() - 0.5)) {
        if (Math.hypot(to.x - cur.x, to.z - cur.z) < 0.2 && Math.abs(to.yaw - cur.yaw) < 0.15) continue;
        if (this.valid(key, to)) { out.push({ key, to }); if (++n >= 2) break; }
      }
      if (out.length >= limit) break;
    }
    return out;
  }

  /**
   * 打开时按上次的摆法放; 没挪过 (或上次的验不过) 的用配置里的默认位置 start (阅读椅: 离墙远一点, 前面站得下).
   * start 的朝向和 spots 一样是相对原来的. 放在上面的靠垫、小兔子跟着挪.
   */
  restore() {
    const saved = this.r.props.layout.furniture || {};
    for (const [key, m] of Object.entries(this.cfg)) {
      const b = this.base[key];
      if (!b) continue;
      const tries = [];
      if (Array.isArray(saved[key])) tries.push({ x: saved[key][0], z: saved[key][1], yaw: saved[key][2] });
      if (Array.isArray(m.start)) tries.push({ x: m.start[0], z: m.start[1], yaw: b.yaw + (m.start[2] || 0) });
      const to = tries.find((p) => this.valid(key, p));
      if (!to) continue;
      const mv = { key, riders: this._riders(key), setGrip: () => {} };
      this._place(mv, to);
      this.apply(key, to);
      for (const rd of mv.riders) { if (this.r.props.glb[rd.id]) this.r.props.glb[rd.id].pending = false; }
    }
    this.r.props.cache.dirty = true;
  }

  /**
   * 带滑轮的椅子 (座位 id, 座位上有 scoot 和 mover) 滑到 k (0 塞在桌下, 1 拉出来 scoot 米), 用时按滑的距离.
   * 她坐在上面就连人一起挪 (座位跟着走, 脚跟着座位放); grip 是扶着椅背的那只手的点, 跟着椅子走. 挪完重建寻路网格.
   */
  async slide(ctx, id, k, grip = null) {
    const r = this.r, L = r.layer, o = this._entry(id), key = o?.seat?.mover, b = this.base[key];
    const k0 = this.slid[id] || 0;
    if (!b || !o.seat.scoot || Math.abs(k - k0) < 1e-3) return;
    if (!k0) this.tucked[id] = { ...this.pose[key] };
    const home = this.tucked[id], d = o.seat.scoot, f = [Math.sin(o.seat.yaw), Math.cos(o.seat.yaw)];
    const at = (kk) => ({ x: home.x - f[0] * d * kk, z: home.z - f[1] * d * kk, yaw: home.yaw });
    const withHer = !!L.seated;
    let last = k0;
    r.props.toLive(b.node);
    try {
      await r.props.tween((ctx.fast ? 0.4 : 1.3) * Math.abs(k - k0), (u) => {
        const kk = k0 + (k - k0) * u, step = (kk - last) * d;
        last = kk;
        this.apply(key, at(kk));
        if (withHer && L.seated) {
          L.char.x -= f[0] * step;
          L.char.z -= f[1] * step;
          L.seated.seat.pos = this._entry(id).seat.pos.slice();
        }
        if (grip) { grip.x -= f[0] * step; grip.z -= f[1] * step; }
      });
    } finally {
      r.props.toRoom(b.node);
      this.slid[id] = k;
      L.grid = buildGrid(r.scene);
    }
  }

  _save() {
    const L = this.r.props.layout;
    L.furniture = Object.fromEntries(Object.entries(this.pose).map(([k, p]) => [k, [+p.x.toFixed(3), +p.z.toFixed(3), +p.yaw.toFixed(3)]]));
    this.r.props.save();
  }

  /** 放在这件家具上面的小东西 (挪的时候一起走): 物件 id 和它在家具坐标里的位置. */
  _riders(key) {
    const m = this.cfg[key], p = this.pose[key], box = this._boxes(key)[0], out = [];
    if (!box) return out;
    for (const id of m.carries || []) {
      if (!this.r.props.asItem(id)) continue;
      const it = this.r.layer.items[id];
      if (it.held) continue;
      const q = it.mesh.position;
      if (Math.abs(q.x - box.center[0]) > box.size[0] / 2 || Math.abs(q.z - box.center[1]) > box.size[1] / 2 || q.y < 0.15) continue;
      const dx = q.x - p.x, dz = q.z - p.z, c = Math.cos(-p.yaw), s = Math.sin(-p.yaw);
      this.r.props.wake(id);
      out.push({ id, local: [c * dx + s * dz, -s * dx + c * dz], y: q.y, yaw: it.mesh.rotation.y - p.yaw });
    }
    return out;
  }

  /** 每帧: 推着的家具跟着她往前走 (按她沿推的方向走了多远). */
  tick() {
    const mv = this.moving;
    if (!mv) return;
    const L = this.r.layer, { from, to, dir, dist } = mv;
    const prog = Math.max(0, Math.min(dist, (L.char.x - mv.start[0]) * dir[0] + (L.char.z - mv.start[1]) * dir[1]));
    const t = dist > 1e-3 ? prog / dist : 0;
    this._place(mv, { x: from.x + dir[0] * prog, z: from.z + dir[1] * prog, yaw: from.yaw + (to.yaw - from.yaw) * t });
  }

  _place(mv, p) {
    const b = this.base[mv.key];
    b.node.position.set(p.x, b.node.position.y, p.z);
    b.node.rotation.y = p.yaw;
    for (const rd of mv.riders) {
      const it = this.r.layer.items[rd.id];
      if (!it || it.held) continue;
      const c = Math.cos(p.yaw), s = Math.sin(p.yaw), [lx, lz] = rd.local;
      it.mesh.position.set(p.x + c * lx + s * lz, rd.y, p.z - s * lx + c * lz);
      it.mesh.rotation.y = rd.yaw + p.yaw;
    }
    mv.setGrip(p);
  }

  /** 推过去: 站到家具后面, 两只手按着, 慢慢往前走, 家具跟着走; 到了以后场景描述跟着改, 记住摆法. why: 状态栏那句 (为什么推, 不给是换个摆法). */
  async push(ctx, key, to, why = null) {
    const r = this.r, m = this.cfg[key], b = this.base[key], from = { ...this.pose[key] };
    const dx = to.x - from.x, dz = to.z - from.z, dist = Math.hypot(dx, dz);
    const L = r.layer;
    const dir = dist > 0.05 ? [dx / dist, dz / dist]
      : (() => { const ex = from.x - L.char.x, ez = from.z - L.char.z, n = Math.hypot(ex, ez) || 1; return [ex / n, ez / n]; })();
    const yawDir = Math.atan2(dir[0], dir[1]);
    const box = this._boxes(key)[0];
    const ext = box ? Math.abs(dir[0]) * box.size[0] / 2 + Math.abs(dir[1]) * box.size[1] / 2 : 0.3;
    const back = ext + 0.3, top = Math.min(0.82, Math.max(0.45, (box?.height ?? 0.6) * 0.9));
    ctx.say(why || `她想换个摆法, 去推${m.name}`);
    await ctx.walk([from.x - dir[0] * back, from.z - dir[1] * back], yawDir);
    // 开推: 家具挪进活动层, 寻路不再把它当障碍 (她要走进它原来的地方)
    const ids = [...(m.objects || []), ...(m.decor || [])];
    for (const id of ids) this._entry(id).walkable = true;
    L.grid = buildGrid(r.scene);
    ctx.later(() => { for (const id of ids) delete this._entry(id).walkable; L.grid = buildGrid(r.scene); });
    r.props.toLive(b.node);
    ctx.later(() => r.props.toRoom(b.node));
    const left = new THREE.Vector3(dir[1], 0, -dir[0]);
    const grip = { left: new THREE.Vector3(), right: new THREE.Vector3() };
    const setGrip = (p) => {
      const nx = p.x - dir[0] * (ext - 0.02), nz = p.z - dir[1] * (ext - 0.02);
      grip.left.set(nx, top, nz).addScaledVector(left, 0.17);
      grip.right.set(nx, top, nz).addScaledVector(left, -0.17);
    };
    const mv = { key, from, to, dist, dir, start: null, setGrip, riders: this._riders(key) };
    ctx.later(() => { for (const rd of mv.riders) { if (r.props.glb[rd.id]) r.props.glb[rd.id].pending = false; } });
    setGrip(from);
    r.poses.set('push', { grip });
    ctx.later(() => r.poses.set(null));
    await ctx.wait(0.7, { hurry: false });
    ctx.say(`她在推${m.name}…`);
    if (dist > 0.05) {
      mv.start = [L.char.x, L.char.z];
      this.moving = mv;
      try {
        await ctx.walk([to.x - dir[0] * back, to.z - dir[1] * back], yawDir, 0.6);
      } finally {
        this.moving = null;
      }
    } else {
      await r.props.tween(1.6, (t) => this._place(mv, { x: from.x, z: from.z, yaw: from.yaw + (to.yaw - from.yaw) * t }));
    }
    this._place(mv, to);
    this.apply(key, to);
    this._save();
    r.poses.set(null);
    await ctx.wait(0.5, { hurry: false });
    r.v.setEmotion('happy');
    ctx.say(`${m.name}挪好啦`);
  }
}

// 她开始做一件事之前想什么: 为什么 (缺哪项)、现在几点、上次练这个怎么样. 没有固定的一句, 每次拼一句.
const WHY = { water: ['有点渴了', '想喝点东西'], food: ['肚子有点饿', '嘴巴有点馋'], energy: ['有点困', '没什么精神'],
  fun: ['有点无聊', '想找点事做'], social: ['有点想你', '想看看你在干嘛'] };
const SKILLS = { yoga: ['treeR', 'treeL', 'fold', 'bend'], sleep: ['lie'], tidy: ['crouch'], drink: ['crouch'], read: ['sit@reading_chair'],
  computer: ['sit@desk_chair'], rest: ['sit@bed'] };
function thought(r, a) {
  if (a.script === 'sleep' && r.life?.sleepReason === 'restart') return '服务端要重启了, 她去床上睡一会儿…';
  const n = r.needs, parts = [];
  const need = Object.keys(a.want || {}).filter((k) => n.v[k] < 50).sort((x, y) => n.v[x] - n.v[y])[0];
  if (need) parts.push(pickOne(WHY[need]));
  else if (Math.random() < 0.3) {
    const h = herDate().getHours();
    parts.push(h < 5 ? '夜深了' : h < 9 ? '早上了' : h < 12 ? '上午天气不错' : h < 14 ? '中午了' : h < 18 ? '下午有点懒洋洋的' : h < 22 ? '晚上了' : '夜深了');
  }
  const past = r.practice.history.filter((x) => (SKILLS[a.id] || []).includes(x.skill)).at(-1);
  if (past?.falls) parts.push(pickOne(['上次摔了一跤, 这次小心点', '上回没站稳, 这次慢慢来']));
  else if (past?.pain > 0.6) parts.push('上次撞到了, 这次注意点');
  else if (past && !past.better && Math.random() < 0.4) parts.push('上次做得不太好, 再试试');
  if (r.body.injured) parts.push(`${r.body.injury.part}还有点疼`);
  const going = a.going ? `${a.going}…` : `去${a.name}…`;
  return parts.length ? `她想: ${parts.join(', ')}. ${going}` : `她${going}`;
}

/** 她自己生活 (见文件开头). r: 小屋的各个部件 (行动层、姿势层、道具、需求、挪家具...). */
export class Life {
  constructor(r) {
    this.r = r;
    this.base = r.scene.life;
    this.requests = new RoomActionRequests({ enqueue: id => this.goDo(id), cancel: id => {
      this.order = this.order.filter(x => x !== id);
      if (this.current?.id === id) this.hurry = true;
    } });
    this.last = null;
    this.timer = 0;
    this.order = (PARAMS.get('life') || '').split(',').filter((id) => this.acts.some((a) => a.id === id));
    this.fast = PARAMS.get('fast') === '1';
    this.done = [];
    this.hurry = false;
    this.current = null;
    this.sleeping = false;
    this.tools = 0;
    this.toolLabel = '';
    this.how = new Set();     // 这件事的做法 (服务端想的, 见 Ctx 的 slow / careful)
    this.source = null;       // 这件事怎么挑的: plan 服务端想的 / rule 本机规则 / urgent 马上要做的 (点了、投喂、快见底)
    this.thinkAt = -Infinity; // 上次问服务端的时刻
    this.asking = false;      // 正在等服务端的回答
    this.plans = 0;           // 按服务端想的做了几件 (自检看)
    this.searchLog = [];      // 找东西的经过 (findItem; 自检看): { item, knows, steps: [{action, say, ok, changes}], found, s }
    // 服务端重启 = 她睡一觉 (self-evolution.md 4.3): restarting 收到了 server_restart、还没连回来; serverBack 连回来了 (打盹的醒);
    // down 没预告就断了 (崩了、网断了); bootNews 醒来要说的 (学会了什么、退回去了)
    this.restarting = false;
    this.serverBack = true;
    this.dozing = false;  // 坐着就地打盹 (服务端在重启)
    this.down = false;
    this.bootNews = '';
    this.busyWith = new Map();  // QQ 那边在做的事 (channel_doing): what -> { text, until }
  }

  /**
   * 能做的事: 场景表 life 里原有的, 加上可供性 (room-afford.js: 她看见过东西才想得到, 期望和权重按她试过的经验). 她还不认识的也在表里
   * (known: false, 自检排的顺序、room_do 让她去做时用), 自己挑的时候 pool 去掉.
   */
  get acts() { return [...this.base, ...(this.r.afford?.acts({ all: true }) || [])]; }

  /** QQ 那边她在做什么 (what: qq / video / study / tool; text 空 = 做完了; secs 最多算多久). 小屋里坐到对应的地方去. */
  doing(what, text, secs) {
    if (!text || !secs) { this.busyWith.delete(what); return; }
    const at = DOING_AT[what] || 'computer';
    this.busyWith.set(what, { text, until: performance.now() + secs * 1000, at });
    if (this.sleeping || this.dozing || this.current?.id === at) return;
    if (!this.current) { if (!this.asking) { clearTimeout(this.timer); this.next(); } }  // 闲着: 马上过去
    else if (!this.current.script) this.hurry = true;  // 在发呆、看书这类随时能放下的: 起来过去
  }

  /**
   * 服务端在重启, 她坐着 (看书、发呆、在电脑前): 就地靠着椅背打个盹 (闭眼, 不走去床上: 走过去躺下要半分钟, 服务端早回来了).
   * on=false: 醒了 (服务端回来了), 把手上的事放下, 去看便利贴.
   */
  doze(on) {
    const r = this.r;
    this.dozing = on;
    r.log(on ? '小屋: 服务端在重启, 她就地打盹' : '小屋: 打盹醒了');  // 日志里看得出走的是哪条路 (2026-10-26 真机重启时只能靠没有 sleep 这一行推断)
    r.v.engine.setAU('au45Blink', on ? 1 : null);
    r.server?.mic?.(!on);  // 打盹时你说话不叫醒她 (麦克风先关, 醒了再开)
    if (on) r.status('她靠着椅背打了个盹 · 服务端在重启, 醒来再回你');
    else {
      r.status('她醒了, 揉揉眼睛');
      if (this.current) this.hurry = true;
    }
  }

  /** 这件事 (act id) 对应的那边还在忙的话, 返回在忙什么 (状态栏用); 不忙返回 ''. */
  doingFor(id) {
    const now = performance.now();
    let text = '';
    for (const [what, d] of this.busyWith) {
      if (d.until < now) this.busyWith.delete(what);
      else if (d.at === id) text = d.text;
    }
    return text;
  }

  start(delay = 2) {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.next(), delay * 1000);
  }

  /** Explicit requests may approach and observe coffee without already knowing its contents. */
  canRequest(a) {
    if (!a || this.recoveryError || this.requests.blocked) return false;
    if (a.script === 'afford' && a.ctx === null) return false;
    if (a.script === 'coffee' && this.r.needs.actualConsumption)
      return !!this.r.liquid?.inspect && !!this.r.scene.coffee?.cups?.length && Array.isArray(this.r.scene.coffee.stand);
    return !a.need || this.r.needs.stock[a.need] > 0;
  }

  can(a, night = isNight()) {
    const r = this.r;
    if (this.recoveryError || this.requests.blocked) return false;
    if (night && a.day) return false;
    if (a.script === 'afford') return a.ctx !== null;  // 她以为现在做得了 (有记得的一摊才擦、有书才看)
    if (a.script === 'coffee' && r.needs.actualConsumption) {
      const options = (r.scene.coffee?.cups || []).map(id => coffeeDecision(r.liquid?.inspect?.(id), { beans: r.needs.stock.beans }));
      const reusable = options.some(o => o.action === 'reuse' || o.autoApproach);
      if (!(r.needs.stock.beans > 0) && !reusable) return false;
      if (options.some(o => o.action === 'wait') && !reusable) return false;
      if (options.length && options.every(o => o.action === 'wait' || o.action === 'refuse')) return false;
    } else if (a.need && !(r.needs.stock[a.need] > 0)) return false;
    if (a.script === 'sleep') return night || r.needs.v.energy < 40 || r.needs.fatigue >= 0.7;
    if (a.script === 'coffee' && (r.needs.effectiveEnergy ?? r.needs.v.energy) > 85 &&
        (!r.needs.actualConsumption || r.needs.v.water >= 50)) return false;
    // 电脑只在要操作设备 (工具调用) 或者看你在电脑上做什么 (托盘里开了"看你在做什么") 的时候用
    // (QQ 那边在回消息、看 B 站时也去: 坐在电脑前)
    if (a.id === 'computer') return this.tools > 0 || !!r.watching?.() || !!this.doingFor('computer');
    return true;
  }

  weight(a, night) {
    let w = (a.weight || 1) * (night && a.night ? a.night : 1);
    if (a.script !== 'afford') w *= this.r.afford?.quiet(a.id) ?? 1;  // 她有了真的做法以后, 占位的那件淡一点 (room-afford.js)
    if (a.script === 'sleep') w *= 1 + 8 * Math.max(0, this.r.needs.fatigue - 0.6);  // 说了一晚上话, 越来越想睡
    if (a.script === 'train') {  // 平衡动作练得越差越想去健身区 (没练过的按 0.5 算)
      const sum = this.r.practice.summary();
      const weak = ['treeR', 'treeL', 'fold', 'bend'].reduce((x, k) => x + Math.max(0, 1 - (sum[k]?.avg ?? 0.5)), 0) / 4;
      w *= 1 + 3 * weak;
    }
    for (const [k, f] of Object.entries(a.want || {})) {
      const value = k === 'energy' && a.script !== 'sleep' ? (this.r.needs.effectiveEnergy ?? this.r.needs.v[k]) : this.r.needs.v[k];
      const gap = Math.max(0, (70 - (value ?? 70)) / 70);
      w *= 1 + f * gap * gap * 4;
    }
    return w;
  }

  /** 马上要做的, 不用想: 点了家具 / 自检排好的顺序、地上有投喂的、某项快见底了 (补得最多的那件). 没有返回 null. */
  urgent(pool) {
    if (this.order.length) {
      const id = this.order.shift();
      return id === 'fetch' ? FETCH : this.acts.find((a) => a.id === id);
    }
    if (this.r.props.fed.length) return FETCH;
    if (this.r.docs?.peek()?.item) return READ_DOC;
    // 断线那阵子你发的便利贴 (或者连回来时有话要说): 连上了就去看
    if ((this.r.notes?.count || this.bootNews) && !this.restarting && this.r.server?.connected?.() !== false) return NOTES;
    // QQ 那边在忙: 坐到对应的地方 (电脑前 / 阅读椅)
    const busy = pool.find((a) => this.doingFor(a.id));
    if (busy) return busy;
    if (this.r.needs.fatigue >= 0.9) {  // 困得不行: 去睡觉, 顺便整理今天聊的
      const sleep = this.acts.find((a) => a.script === 'sleep');
      if (sleep && !this.sleeping) {
        this.sleepReason = 'tired';
        this.r.say?.(pickOne(['好困啊…我去睡一会儿, 顺便把今天聊的整理一下', '眼皮好重…先去睡一下, 醒了再陪你']));
        return sleep;
      }
    }
    const { key, gap } = this.r.needs.worst();
    if (gap > 0.75 && key === 'water' && this.r.needs.actualConsumption) {
      const drink = pool.find(a => a.script === 'coffee');
      if (drink) return drink;
    }
    if (gap > 0.75) return pool.filter((a) => (a.gives?.[key] || 0) > 10).sort((x, y) => y.gives[key] - x.gives[key])[0] || null;
    return null;
  }

  /** 现在能做的 (刚做过的不算; 可供性只算她认识的). */
  pool(night = isNight()) { return this.acts.filter((a) => a.id !== this.last && a.known !== false && this.can(a, night)); }

  /** 按权重随机挑一件 (本机规则). */
  weighted(pool, night = isNight()) {
    let left = Math.random() * pool.reduce((s, a) => s + this.weight(a, night), 0);
    for (const a of pool) if ((left -= this.weight(a, night)) <= 0) return a;
    return pool[0];
  }

  pick() {
    const night = isNight(), pool = this.pool(night);
    return this.urgent(pool) || this.weighted(pool, night);
  }

  /** 告诉服务端的 (上行 room_think): 只有编号、固定取值和数字. 最近做的从近到远; 疼、摔只带 10 分钟以内的; 练习每件候选带最近一次. */
  _sense(pool) {
    const r = this.r, s = r.needs.summary(), now = performance.now() / 1000;
    const fresh = r.body.log.filter((e) => now - e.t < 600);
    const practice = [];
    for (const a of pool) {
      const skills = SKILLS[a.id] || [];
      const h = r.practice.history.filter((x) => skills.includes(x.skill)).at(-1);
      if (h) { practice.push({ act: a.id, skill: h.skill, n: h.n, avg: h.avg, better: h.better, falls: h.falls, pain: h.pain }); continue; }
      const [name, k] = skills.map((n) => [n, r.practice.skills[n]]).filter(([, x]) => x?.n).sort((x, y) => y[1].n - x[1].n)[0] || [];
      if (k) practice.push({ act: a.id, skill: name, n: k.n, avg: k.avg === null ? null : +k.avg.toFixed(3), better: false, falls: 0, pain: 0 });
    }
    return {
      mood: s.mood, needs: s.values, options: pool.map((a) => a.id),
      known: forPlan(pool),  // 可供性: 这个情境试过几次、她期望补哪项多少 (她的经验, 不是真值)
      recent: this.done.slice(-5).reverse().map((d) => ({ id: d.id, ok: d.ok })),
      body: { injured: r.body.injured ? r.body.injury.part : null,
        pains: fresh.filter((e) => e.kind === 'pain').slice(-3).map((e) => ({ part: e.part.split(/撞到|陷进/)[0], act: e.act })),
        falls: fresh.filter((e) => e.kind === 'fall').slice(-3).map((e) => ({ act: e.act })) },
      practice,
    };
  }

  /** 问服务端接下来做什么: 隔 THINK_S 秒一次, 你在说话时不问. 返回能做的那份 room_plan (pick 在候选里), 不然 null. */
  async _ask(pool) {
    const r = this.r, now = performance.now();
    if (!r.think || THINK_S === null || !pool.length || now - this.thinkAt < THINK_S * 1000 || r.busy()) return null;
    const ask = r.think(this._sense(pool), THINK_WAIT_MS);  // 没连上服务端是 null: 不算问过, 下一件接着试
    if (!ask) return null;
    this.thinkAt = now;
    this.asking = true;
    let plan = null;
    try {
      plan = await ask;
    } catch { /* 按本机规则 */ } finally {
      this.asking = false;
    }
    if (!plan?.ok) r.log(`小屋: 想下一件事没用上 (${plan ? plan.reason : '没回'})`);
    else if (!pool.some((a) => a.id === plan.pick)) r.log(`小屋: 服务端挑的 ${plan.pick} 现在做不了`);
    return plan?.ok && pool.some((a) => a.id === plan.pick) ? plan : null;
  }

  async next() {
    if (this.current || this.recoveryError || this.requests.blocked) return;
    if (this.r.busy() && !this.order.length) {  // 在和你说话: 等说完再换
      this.start(3);
      return;
    }
    const night = isNight();
    let pool = this.pool(night), a = this.urgent(pool), plan = null, source = 'urgent';
    if (!a) {
      plan = await this._ask(pool);
      // 等回答的时候点了家具、投喂了、开始说话了: 先顾那边, 这份不用
      if (plan && (this.order.length || this.r.props.fed.length || this.r.busy())) plan = null;
      if (this.r.busy() && !this.order.length) { this.start(3); return; }
      pool = this.pool(isNight());
      a = plan ? pool.find((x) => x.id === plan.pick) : null;
      if (a) source = 'plan';
      else {
        plan = null;
        a = this.urgent(pool);
        if (!a) { a = this.weighted(pool, night); source = 'rule'; }
      }
    }
    // A pending plan must not revive work after cancellation or another execution start.
    if (this.current || this.recoveryError || this.requests.blocked) return;
    if (!a) { this.start(5); return; }
    this.last = a.id;
    this.actionDetail = null;
    this.current = a;
    const request = this.requests.begin(a.id);
    this.hurry = false;
    this.how = new Set(plan?.how || []);
    this.source = source;
    if (plan) this.plans++;
    const ctx = new Ctx(this, request), t0 = performance.now();
    const think = plan?.thought ? `她想: ${plan.thought}` : thought(this.r, a);
    this.r.status(think);
    this.r.log(`小屋: ${a.id} · ${think}${plan ? ` (服务端想的, ${plan.ms} ms${plan.how.length ? `, ${plan.how.join('/')}` : ''})` : ''}`);
    const outcome = { id: a.id, ok: false, src: source, ...(plan && { how: plan.how }) };
    try {
      if (plan) await this._prelude(ctx);
      await (SCRIPTS[a.script] || SCRIPTS.stay)(ctx, a);
      if (request && (this.hurry || request.cancelled)) throw new Error('动作被中断，已完成的消费保留');
      outcome.ok = true;
    } catch (err) {
      outcome.error = err.message;
      this.r.log(`小屋: ${a.name}没成: ${err.message}`);
    }
    const cleanupErrors = await ctx.cleanup();
    // Cancellation during cleanup must also supersede a successful script return.
    if (request?.cancelled) { outcome.ok = false; outcome.error = request.cancelReason || '动作请求已停止'; }
    if (cleanupErrors.length) { outcome.ok = false; outcome.error = `动作收尾失败: ${cleanupErrors.join("; ")}`; }
    Object.assign(outcome, { s: +((performance.now() - t0) / 1000).toFixed(1), ...this._felt(a) });
    this.done.push(outcome);
    this.requests.finish(request, { ok: outcome.ok, reason: outcome.error || '', detail: this.actionDetail, name: a.name, cleanupConfirmed: cleanupErrors.length === 0 });
    this.current = null;
    this.hurry = false;
    this.how = new Set();
    this.r.needs.save();
    if (cleanupErrors.length) {
      this.recoveryError = outcome.error;
      this.requests.stop(outcome.error);
      this.order = [];
      clearTimeout(this.timer);
      this.r.status(`${outcome.error}；已暂停自动活动，请重新进入小屋后重试`);
      return;
    }
    // 做完一件事不马上接下一件: 歇一会儿 (15-40 秒), 中间看看你、看看窗外、伸个懒腰
    // 插队的 (点了家具、服务端要重启让她去睡)、床头柜上有便利贴、醒来有话要说、QQ 那边在忙: 不歇, 马上去做
    // (2026-10-26 真机重启: 她看完书歇了十几秒才去睡, 服务端都回来了)
    const now = performance.now();
    const soon = this.order.length || this.r.notes?.count || this.bootNews || [...this.busyWith.values()].some((d) => d.until > now);
    const gap = this.fast || soon ? 0.5 : rand(15, 40);
    this.start(gap);
    if (!this.fast && !soon) setTimeout(() => this._idleMoment(), rand(4, gap * 0.7) * 1000);
  }

  /** 可供性做完记下的 (自检、快进看): 情境、她感到的、用的哪件东西、是她自己挑的还是认识之前被叫去做的. */
  _felt(a) {
    const o = a.script === 'afford' && this.r.afford?.last;
    return o?.id === a.id ? { ctx: o.ctx, felt: o.felt, ...(o.thing && { thing: o.thing }), ...(o.skip && { skip: true }), known: a.known } : {};
  }

  /** 两件事之间的小动作 (不在做事、没在说话、没睡着时). */
  _idleMoment() {
    const r = this.r, L = r.layer;
    if (this.recoveryError || this.requests.blocked || this.current || this.sleeping || r.busy() || r.poses.pose || L.mode !== 'idle' && L.mode !== 'seated') return;
    const roll = Math.random();
    if (r.needs.fatigue >= 0.7 && roll < 0.6) {  // 困了: 打哈欠、揉眼睛
      if (roll < 0.3) {
        r.v.pulseAU?.('au26JawDrop', 0.75, 2.2);
        r.v.pulseAU?.('au27MouthStretch', 0.35, 2.2);
        r.v.pulseAU?.('au45Blink', 0.7, 2.4);
        r.status('她打了个哈欠…');
      } else {
        r.poses.set('rubEyes');
        r.status('她揉了揉眼睛');
        setTimeout(() => { if (r.poses.name === 'rubEyes' && !this.current) r.poses.set(null); }, rand(1.6, 2.6) * 1000);
      }
      if (Math.random() < 0.3) r.say?.(pickOne(['好困啊…', '眼睛快睁不开了…', '想睡觉了…']));
    } else if (roll < 0.35) L.lookAt('user', rand(2.5, 5));
    else if (roll < 0.6) L.lookAt('window', rand(3, 6));
    else if (roll < 0.75 && !L.seated) {
      r.poses.set('reach');
      setTimeout(() => { if (r.poses.name === 'reach' && !this.current) r.poses.set(null); }, rand(2.2, 3.5) * 1000);
    }
  }

  /** 服务端想的做法里开始前的那一下: 先看看你 / 先冲你挥挥手. */
  async _prelude(ctx) {
    const r = this.r;
    if (!this.how.has('look') && !this.how.has('wave')) return;
    r.layer.lookAt('user', 3);
    if (this.how.has('wave')) {
      r.v.setEmotion('happy');
      r.motion.applyIntent('act', [{ do: 'wave', side: 'R', amp: 0.8, spd: 1, at: 0.1 }], performance.now() / 1000);
    }
    await ctx.wait(this.how.has('wave') ? 1.8 : 1.2);
    r.v.setEmotion('neutral');
  }

  /** 点了家具 / 投喂了 / 叫她: 马上去做那件事 (正在做的先收尾). */
  goDo(id) {
    if (this.recoveryError || this.requests.blocked) { this.r.status('收尾尚未确认完成，暂时不能开始新活动'); return false; }
    const a = id === 'fetch' ? FETCH : this.acts.find((x) => x.id === id);
    if (!a) return false;
    if (a !== FETCH && !this.canRequest(a)) {
      const stock = { drinks: '冰箱里没有饮料了', beans: '咖啡豆用完了', snacks: '没有零食了', milk: '没有牛奶了' }[a.need];
      this.r.status(stock ? `${stock}, 投喂一点吧` : `她现在不想${a.name}`);
      return false;
    }
    this.order = [id, ...this.order.filter((x) => x !== id)];  // 插到最前面 (自检排好的顺序留着)
    if (this.current) this.hurry = true;
    else if (!this.asking) { clearTimeout(this.timer); this.next(); }  // 正在等服务端: 那边回来就先做这件
    return true;
  }

  /** 每帧: 推着的家具跟着走; 坐着时头发、裙子落在座面上. */
  tick() {
    const r = this.r, L = r.layer;
    r.mover.tick();
    const seat = !this.sleeping && L.seated && L.mode === 'seated' ? L.seated.seat : null;
    if (seat !== this._seat && !r.poses.pose) {
      this._seat = seat;
      r.surface?.set(seat ? new THREE.Vector3(seat.pos[0], seat.height, seat.pos[1]) : null, 0.55);
    }
  }

  /** 正在调的工具有几个 (count) 和最近那个的名字: 有了就坐到电脑前帮你操作 (操控电脑、手机的工具就是"玩电脑"). */
  working(count, label = '') {
    const was = this.tools;
    this.tools = Math.max(0, count);
    if (label) this.toolLabel = label;
    if (!was && this.tools && this.current?.id !== 'computer' && !this.sleeping) this.goDo('computer');
  }
}
