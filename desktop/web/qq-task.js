// 渠道任务单在桌宠页面这边的部分 (服务端见 wm/channels/manager.py 的 computer_task, 宿主见 desktop/host/src/tools.h 的 task_begin):
//   - 主人在 QQ 上说"帮我看一下屏幕""打开记事本", 或者她自己想去电脑上看点什么, 服务端发 channel_task {id, by, text, prompt};
//   - 页面先对宿主说 task-begin (by=owner: 主人亲口说了就算同意, 这一轮里的读屏、动手不再逐个问, 守门照旧; by=self: 只放行读屏),
//     再把 prompt 当一轮对话交给带工具的那条会话 (say(..., {task}): 服务端不会把它再当成"主人的指挥"重新分类);
//   - 做的过程中每个工具调用报一次进度 (channel_task_progress), 这一轮说完或被打断了, 对宿主说 task-end,
//     宿主把最后一张截图 (屏幕上没有聊天、密码这类窗口时才留) 经 task-shot 交回来, 页面压成小图, 连同她最后说的话一起 channel_task_report.
// 同时只做一件; 页面正在跟主人说话时不接 (回 ack ok:false, 服务端告诉 QQ 上的主人).
const SHOT_W = 1000;      // 发回 QQ 的截图最宽多少像素
const SHOT_Q = 0.72;      // JPEG 质量
const SHOT_WAIT_MS = 4000;  // 等宿主交回截图最多多久
const BUSY_MS = 120000;   // 对话里有一轮"进行中"的标记最多信多久 (万一没收到结束)
// 要主人点头的卡片同时在 QQ 上问他时, 卡片等多久 (默认 30 秒, 主人不在电脑前来不及). 要比服务端的 llm.cloud.tool_timeout_s
// (本机 config/local.json 是 900 秒; 默认 90 秒) 略短, 这样是卡片自己按"超时"收起, 不是被服务端收掉; manager.py 的 TOOL_ASK_S 同值
export const ASK_QQ_S = 890;

/** 宿主给的截图 (base64) 压成小 JPEG 的 data: URL; 失败给空串 (只发文字). */
async function shrink(mime, data) {
  try {
    const img = new Image();
    img.src = `data:${mime || 'image/png'};base64,${data}`;
    await img.decode();
    const k = Math.min(1, SHOT_W / img.naturalWidth);
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(img.naturalWidth * k));
    canvas.height = Math.max(1, Math.round(img.naturalHeight * k));
    canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
    return canvas.toDataURL('image/jpeg', SHOT_Q);
  } catch {
    return '';
  }
}

export class QQTasks {
  /** talk: 桌面上这条对话 (/ws); post: 发给宿主; shell: 界面壳 (对话栏里提示一句); log */
  constructor({ talk, post, shell, log }) {
    this.talk = talk;
    this.post = post;
    this.shell = shell;
    this.log = log;
    this.cur = null;      // 正在做的单子: { id, by, text, prompt, turn, reply, steps }
    this.busyAt = 0;      // 对话里有一轮 (不是任务单的) 在进行
    this.shotWait = null; // 等宿主交回截图: { id, done }
    this.asked = new Set(); // 同时在 QQ 上问主人的卡片 (call_id)
  }

  get busy() { return this.busyAt && performance.now() - this.busyAt < BUSY_MS; }

  /** 当前预设的工具里有没有能看屏幕、能点的 (Windows-MCP 的 Snapshot / Screenshot / Click). */
  hasControl() {
    return (this.talk.tools || []).some((t) => /^mcp__.+__(Snapshot|Screenshot|Click)$/.test(t?.name || ''));
  }

  /**
   * 工具确认卡片弹出来了 (m: 宿主的 tool-ask). 高风险的工具、她自己想动手 (self 任务里的点击输入) 的, 同时告诉服务端:
   * 它会在 QQ 上问主人 (主人在 QQ 上让她做的事马上问, 别的过一会儿再问). 返回要不要把卡片的等待时间拉长.
   */
  askOpen(m, online) {
    if (!online || !(m.tier === 'power' || (m.tier === 'act' && this.cur?.by === 'self'))) return false;
    this.asked.add(m.call_id);
    this.talk.send({ type: 'channel_ask_open', call_id: m.call_id, tier: m.tier, tool: m.tool, title: m.title,
      args: String(m.args || '').slice(0, 700), by: this.cur?.by || '' });
    return true;
  }

  /** 这张卡片有结果了: QQ 上等答复的那一项作废. reason: answered (电脑上点了) / cancelled (服务端不等了、被停了, 没有执行). */
  askClosed(callId, reason = 'answered') {
    if (this.asked.delete(callId)) this.talk.send({ type: 'channel_ask_closed', call_id: callId, reason });
  }

  /** 服务端下发的 channel_task / channel_task_cancel / channel_ask_answer. */
  handle(msg) {
    if (msg.type === 'channel_ask_answer') this.shell.answerAsk(String(msg.call_id || ''), msg.how);  // 主人在 QQ 上答了: 当成点了按钮
    else if (msg.type === 'channel_task') this.begin(msg);
    else if (msg.type === 'channel_task_cancel' && this.cur?.id === msg.id) {
      this.shell.notice('这件事做太久了, 停下');
      this.talk.interrupt();
    }
  }

  ack(id, ok, reason = '') {
    this.talk.send({ type: 'channel_task_ack', id, ok, ...(ok ? {} : { reason }) });
  }

  begin(msg) {
    const id = String(msg.id || '').slice(0, 40);
    if (!id || !/^[\w-]+$/.test(id)) return;
    if (this.cur) return this.ack(id, false, '上一件事还没做完');
    if (this.busy) return this.ack(id, false, '她正在跟你说话');
    if (!this.hasControl()) return this.ack(id, false, '桌宠这边没接电脑操控 (设置里没开 Windows-MCP, 或者用的是本地模型)');
    const by = msg.by === 'owner' ? 'owner' : 'self';
    const text = String(msg.text || '').slice(0, 300);
    this.cur = { id, by, text, prompt: String(msg.prompt || ''), turn: null, reply: '', steps: 0 };
    this.post(`task-begin ${id} ${by}`);
    this.ack(id, true);
    this.shell.notice(by === 'owner' ? `主人在 QQ 上让她: ${text}` : `她想用电脑: ${text}`);
    this.log('任务单', id, by, text);
    this.talk.say(this.cur.prompt, { task: id });
  }

  /** 这一轮是不是任务单的 (对话栏里显示任务本身, 不显示长长的提示). 不是的话标记"对话里有一轮在进行". */
  turnStart(turn, text) {
    if (this.cur && this.cur.turn === null && text === this.cur.prompt) {
      this.cur.turn = turn;
      return true;
    }
    this.busyAt = performance.now();
    return false;
  }

  /** 对话栏里显示的字. */
  shown() { return `（QQ 任务）${this.cur?.text || ''}`; }

  reply(turn, delta) {
    if (this.cur?.turn === turn) this.cur.reply += delta;
  }

  /** 她调了个工具 (msg: 下行 tool_call): 给服务端报一次进度 (服务端限频后才发到 QQ). */
  tool(msg) {
    if (!this.cur || this.cur.turn !== msg.turn) return;
    this.cur.steps += 1;
    const m = /^mcp__.+?__(.+)$/.exec(msg.name || '');
    this.talk.send({ type: 'channel_task_progress', id: this.cur.id, tool: m ? m[1] : '' });
  }

  /** 一轮说完了 / 被打断了. 是任务单的那一轮就结束这件事. */
  turnEnd(turn, interrupted) {
    if (this.cur?.turn === turn) {
      this.finish(interrupted);
      return;
    }
    this.busyAt = 0;
  }

  async finish(interrupted) {
    const t = this.cur;
    this.cur = null;
    this.post(`task-end ${t.id}`);
    const shot = await new Promise((resolve) => {
      const timer = setTimeout(() => { this.shotWait = null; resolve(null); }, SHOT_WAIT_MS);
      this.shotWait = { id: t.id, done: (m) => { clearTimeout(timer); this.shotWait = null; resolve(m); } };
    });
    const said = t.reply.trim();
    const ok = !interrupted && said.length > 0;
    const jpeg = shot?.data ? await shrink(shot.mime, shot.data) : '';
    this.log('任务单结束', t.id, ok ? '做完了' : interrupted ? '被打断了' : '没话说', `${t.steps} 步`, jpeg ? '带截图' : shot?.skipped ? '截图没留' : '');
    this.talk.send({
      type: 'channel_task_report', id: t.id, ok,
      text: said.slice(0, 1200) || (interrupted ? '被停下了 (你或者我点了停, 或者你动了鼠标键盘)' : ''),
      shot: jpeg, skipped: !jpeg && !!shot?.skipped,
    });
  }

  /** 宿主交回的截图 ({t: 'task-shot', id, mime, data, skipped}). */
  onShot(m) {
    if (this.shotWait?.id === m.id) this.shotWait.done(m);
  }
}
