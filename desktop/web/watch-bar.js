// 桌宠对话栏上面的陪看提示条: 写什么、什么时候收 (pet.js 接 watch_state / watch_ctl_done; 界面是 ui-shell.js 的 watch / watchEnding).
// 2026-11-07 陪看 (wm/watch_along.py, 给了链接 / BV 号 / 本机视频): "她在陪你看 《标题》".
// 2026-11-08 屏幕陪看 (wm/screen_along.py, watch_state 带 mode: "screen"; 方案见知识库 09 问题与待办/桌宠陪看升级方案, 开发日志 221):
// "她在看你的屏幕 《标题》", 下面一行小字"程序 · 只看前台那个窗口" (程序名放第二行: 第一行 290px 放了程序名, 短标题也被截);
// 她自己决定不看了 (切走好一阵、看太久) 先写为什么, 停几秒再收.

export const WATCH_END_HOLD_MS = 6000;

/** watch_state (active) → 提示条上写的: { kind: 'screen' | 'video', lead, what, hint } */
export function watchView(m) {
  if (m.mode === 'screen') {
    // 标题是服务端去掉站名、浏览器名的; B 站客户端的标题一直是"哔哩哔哩", 到这里是空的: 第一行写程序
    const title = String(m.title || '').trim(), app = String(m.app || '').trim();
    if (!title) return { kind: 'screen', lead: '她在看你的屏幕', what: app, hint: '只看前台那个窗口' };
    return { kind: 'screen', lead: '她在看你的屏幕', what: `《${title}》`, hint: [app, '只看前台那个窗口'].filter(Boolean).join(' · ') };
  }
  return { kind: 'video', lead: '她在陪你看', what: videoLabel(m), hint: '' };
}

// 陪看的是什么: 有标题写《标题》; 不然 BV 号、短链去掉 https://、本机视频只写文件名
function videoLabel(m) {
  if (m.title) return `《${m.title}》`;
  const s = String(m.source || '').trim();
  if (/^https?:\/\//i.test(s)) return s.replace(/^https?:\/\//i, '');
  return s.split(/[\\/]/).pop() || s;
}

/** 陪看结束了 (watch_state active: false): 她自己决定不看的, 提示条先写为什么 (她也说了一句, 但不一定看得到);
 *  别的 (你说不看了、换了别的看、断线) 马上收, 返回空串 */
export function watchEndNote(m) {
  if (m.active || m.mode !== 'screen') return '';
  return { away: '你切走好一阵了，她先不看了', max_session: '看了很久了，她先歇会儿' }[m.reason] || '';
}

/** 提示条: 接服务端的消息. shell 是 ui-shell.js 的 Shell; refresh 是界面变了以后重算点穿掩码 (pet.js 的 refreshMask). */
export class WatchBar {
  // 计时器包一层: 直接把 setTimeout 存起来再 this.setTimer(...) 调, 浏览器里会报 Illegal invocation (Node 里不报, 单元测试抓不到)
  constructor(shell, { refresh = () => {}, holdMs = WATCH_END_HOLD_MS, setTimer = (fn, ms) => setTimeout(fn, ms), clearTimer = (id) => clearTimeout(id) } = {}) {
    Object.assign(this, { shell, refresh, holdMs, setTimer, clearTimer });
    this.timer = 0;
  }

  /** watch_state: 开始、换了片 / 换了窗口时更新; 结束时一般马上收, 她自己决定不看的先写为什么, 停 holdMs 再收 */
  update(m) {
    this._cancel();
    if (m.active) {
      const view = watchView(m);
      this.shell.watch(true, view.what, view);
    } else {
      const note = watchEndNote(m);
      if (note && this.shell.watchEnding(note)) {
        this.timer = this.setTimer(() => { this.timer = 0; this.shell.watch(false); this.refresh(); }, this.holdMs);
      } else this.shell.watch(false);
    }
    this.refresh();
  }

  /** 马上收: 你点了"不看了"、连接断了、新连接 */
  hide() {
    this._cancel();
    this.shell.watch(false);
    this.refresh();
  }

  _cancel() {
    if (this.timer) this.clearTimer(this.timer);
    this.timer = 0;
  }
}
