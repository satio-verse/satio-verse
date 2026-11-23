// QQ 渠道在桌宠页面这边的部分 (服务端见 wm/channels, 协议见根目录 README "外部渠道：QQ"):
//   - 宿主给的 QQ 设置 ({t: 'qq', config, paused}) 连上时和改了时经 /ws 发给服务端 (channel_config, 整份快照; token 和 key 不记日志);
//   - 服务端的草稿 (channel_draft) 在对话栏上面弹一张卡片: 发 / 改了再发 / 不发, 主人点了才发出去;
//   - 提示 (channel_notice: 拦下的话、好友申请、被禁言、额度用完…) 在对话栏里说一句; 状态 (channel_status) 报给宿主, 面板的 QQ 页显示;
//   - 暂停 (托盘或面板): channel_pause.
// QQ 上的消息本身不经过这里: 服务端的访客会话自己收发, 这条 /ws 只有状态、草稿和提示 (只出不进).
const CSS = `
.qq-draft .ask-title b { font-weight: 600; }
.qq-draft .qq-quote { font-size: 12px; opacity: .7; margin: 2px 0 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.qq-draft .qq-text { white-space: pre-wrap; word-break: break-word; margin: 4px 0; }
.qq-draft textarea { width: 100%; box-sizing: border-box; min-height: 52px; resize: vertical; font: inherit; margin: 4px 0;
  border-radius: 8px; border: 1px solid rgba(127,127,127,.4); padding: 6px; background: rgba(255,255,255,.9); color: #222; }
.qq-draft .qq-more { font-size: 12px; opacity: .7; }`;

export class QQChannel {
  /** talk: 桌面上这条对话 (/ws); shell: 界面壳 (卡片放进它的 .talk-stack, 带 .hit 才点得到); post: 发给宿主; refresh: 重算命中掩码 */
  constructor({ talk, shell, post, refresh, focus, log }) {
    this.talk = talk;
    this.shell = shell;
    this.post = post;
    this.refresh = refresh;
    this.focus = focus;
    this.log = log;
    this.config = null;
    this.paused = false;
    this.status = null;
    this.drafts = [];  // 还没处理的草稿 (一次显示第一个)
    this.editing = false;
    // 自检 (qqanswers=send,edit:改过的话,drop): 不等人, 每张草稿隔一会儿按顺序自动回答
    this.auto = (new URLSearchParams(location.search).get('qqanswers') || '').split(',').filter(Boolean);
    const style = document.createElement('style');
    style.textContent = CSS;
    document.head.appendChild(style);
    this.card = document.createElement('div');
    this.card.className = 'ask qq-draft hit';
    this.card.hidden = true;
    this.card.setAttribute('role', 'alertdialog');
    shell.root.querySelector('.talk-stack')?.prepend(this.card);
    this.card.addEventListener('click', (e) => {
      const act = e.target.closest('button')?.dataset.qq;
      if (act) this.answer(act);
    });
  }

  /** 宿主给的设置 (含明文 token 和 key, 不记日志). config 是 null: 没配过, 不发. */
  setConfig(config, paused) {
    this.config = config || null;
    this.paused = !!paused;
    this.report();
  }

  /** /ws 连上了 (新连接上服务端是 config 里的默认): 再发一遍. */
  onOpen() { this.report(); }

  report() {
    if (!this.config) return;
    this.talk.send(this.config);
    this.talk.send({ type: 'channel_pause', paused: this.paused });
  }

  pause(v) {
    this.paused = !!v;
    this.talk.send({ type: 'channel_pause', paused: this.paused });
  }

  /** 服务端下发的 channel_* 消息. */
  handle(msg) {
    switch (msg.type) {
      case 'channel_status':
        this.status = msg;
        this.post(`qq-status ${JSON.stringify(msg)}`);
        break;
      case 'channel_config_done':
        if (!msg.ok) this.shell.notice(`QQ 设置没生效: ${msg.error}`);
        else if (msg.status) this.post(`qq-status ${JSON.stringify(msg.status)}`);
        this.log('QQ 设置', msg.ok ? '已生效' : `没生效: ${msg.error}`, ...(msg.warnings || []));
        break;
      case 'channel_draft':
        this.drafts = this.drafts.filter((d) => d.id !== msg.id && !(d.conv.type === msg.conv.type && d.conv.id === msg.conv.id));
        this.drafts.push(msg);
        if (this.drafts.length === 1 || !this.editing) this.show();
        break;
      case 'channel_draft_done':
        this.drop(msg.id);
        if (!msg.ok && msg.error && !/新的草稿/.test(msg.error)) this.shell.notice(`QQ 草稿: ${msg.error}`);
        break;
      case 'channel_notice':  // 账本的额度提醒 (channel: billing) 不是 QQ 的事, 前缀换成"额度"
        this.shell.notice(msg.channel === 'billing' ? `额度: ${msg.text}` : `QQ: ${msg.text}`);
        if (msg.refresh) this.post('tools-refresh');  // 她给自己装了 MCP: 让宿主重新连工具
        this.refresh();
        break;
      default:
        break;
    }
  }

  drop(id) {
    const first = this.drafts[0]?.id === id;
    this.drafts = this.drafts.filter((d) => d.id !== id);
    if (first) {
      this.editing = false;
      this.show();
    }
  }

  show() {
    const d = this.drafts[0];
    if (!d) {
      this.card.hidden = true;
      this.refresh();
      return;
    }
    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
    const where = d.conv.type === 'group' ? `群「${d.conv.name}」` : d.conv.name === '主人' ? '你的私聊' : `「${d.conv.name}」的私聊`;
    // 服务端问你一件事 (她想学新本事、她写的工具要不要装、超预算要不要接着写): 标题和两个按钮的字都是服务端给的
    const ask = d.ask;
    const title = ask ? `<b>${esc(ask.title)}</b>`
      : d.conv.type === 'qzone' ? '她想发一条 <b>QQ 空间说说</b>' : `她想在 QQ 上回 <b>${esc(where)}</b>`;
    this.card.innerHTML = `
      <div class="ask-title">${title}</div>
      ${d.reply_to?.text ? `<div class="qq-quote">回复: ${esc(d.reply_to.text)}</div>` : ''}
      ${this.editing ? `<textarea class="qq-edit">${esc(d.text)}</textarea>` : `<div class="qq-text">${esc(d.text)}</div>`}
      ${this.drafts.length > 1 ? `<div class="qq-more">后面还有 ${this.drafts.length - 1} 条</div>` : ''}
      <div class="ask-buttons">
        ${ask ? `<button type="button" data-qq="send">${esc(ask.ok)}</button><button type="button" data-qq="drop">${esc(ask.no)}</button>`
          : this.editing ? '<button type="button" data-qq="edit">发改好的</button><button type="button" data-qq="back">算了</button>'
          : '<button type="button" data-qq="send">发</button><button type="button" data-qq="start-edit">改一下</button><button type="button" data-qq="drop">不发</button>'}
      </div>`;
    this.card.hidden = false;
    if (this.auto.length && !this.shot) {  // 自检: 第一张卡片截一张图 (宿主带了 --ask-shot 才截)
      this.shot = true;
      setTimeout(() => this.post('qq-shot'), 300);
    }
    if (this.auto.length && !this.editing && !d.autoAt) {
      d.autoAt = setTimeout(() => {
        if (this.drafts[0] !== d) return;
        const a = this.auto.shift();
        if (a.startsWith('edit:')) {
          this.talk.send({ type: 'channel_draft_review', id: d.id, action: 'edit', text: a.slice(5) });
          this.drafts.shift();
          this.show();
        } else this.answer(a);
      }, 1200);
    }
    if (this.editing) {
      this.focus?.();
      const ta = this.card.querySelector('textarea');
      ta.focus();
      ta.setSelectionRange(ta.value.length, ta.value.length);
      ta.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !e.isComposing) this.answer('edit');
        if (e.key === 'Escape') this.answer('back');
      });
    }
    this.refresh();
  }

  answer(act) {
    const d = this.drafts[0];
    if (!d) return;
    if (act === 'start-edit' || act === 'back') {
      this.editing = act === 'start-edit';
      this.show();
      return;
    }
    const msg = { type: 'channel_draft_review', id: d.id, action: act };
    if (act === 'edit') {
      msg.text = this.card.querySelector('textarea')?.value.trim() || '';
      if (!msg.text) return;
    }
    this.talk.send(msg);
    // 结果 (channel_draft_done) 到了再收起; 先收起这一张, 免得连点两次
    this.drafts.shift();
    this.editing = false;
    this.show();
  }

  /** 自检和 pet_state 用: 不带 token 和 key. */
  state() {
    return { configured: !!this.config, enabled: !!this.config?.enabled, paused: this.paused, state: this.status?.state ?? null,
      error: this.status?.error ?? null, drafts: this.drafts.length, showing: !this.card.hidden };
  }
}
