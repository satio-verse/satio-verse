// 桌宠的界面壳: 左下方的灵动对话栏, 头左边的思考气泡, 右侧的侧菜单. 都是 DOM (不在 WebGL 画布里), 可以点的部分 (.hit) 由 pet.js 算进命中掩码.
//   对话栏 (左下方, 盖在腿上, 不挡脸): 平时收起; 在听 (随音量跳的音柱 + 识别中的文字) -> 在想 (原话; 跳动的点在头左边的思考气泡里)
//          -> 在说 (逐字出现, 长回复自动滚到最新) -> 说完停几秒收起. 点它展开最近几轮对话和输入框. 工具确认卡片叠在它上面.
//   操控电脑时 (tools.h): 卡片换成"允许一次 / 这一轮都允许 / 拒绝"并带一行提醒; 对话栏上面一条"她在操作你的电脑", 带"停".
//   侧菜单: 光标在角色身上时滑出 (宿主告诉页面), 移开 1.5 秒后收起: 麦克风开关、打字、人设预设 (点开是列表)、回小屋、静音、置顶、
//          设置 (操作面板)、隐藏.
const ICONS = {
  mic: '<path d="M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3z"/><path d="M19 11a7 7 0 0 1-14 0M12 18v3"/>',
  micOff: '<path d="M15 9.3V6a3 3 0 0 0-5.7-1.3M9 9v3a3 3 0 0 0 5.1 2.1M19 11a7 7 0 0 1-1.2 3.9M5 11a7 7 0 0 0 10.6 6M12 18v3M3 3l18 18"/>',
  keyboard: '<rect x="2.5" y="6" width="19" height="12" rx="2"/><path d="M6.5 10h.01M10 10h.01M14 10h.01M17.5 10h.01M7 14h10"/>',
  volume: '<path d="M4 9.5v5h3.5L12 18V6L7.5 9.5H4z"/><path d="M15.5 9a4 4 0 0 1 0 6M18 6.5a7.5 7.5 0 0 1 0 11"/>',
  volumeOff: '<path d="M4 9.5v5h3.5L12 18V6L7.5 9.5H4z"/><path d="M16 9.5l5 5M21 9.5l-5 5"/>',
  persona: '<circle cx="12" cy="8" r="3.6"/><path d="M4.5 20.5a7.5 7.5 0 0 1 15 0"/>',
  settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  hide: '<path d="M3 3l18 18M10.6 6.1A9.8 9.8 0 0 1 12 6c5 0 8.5 4.6 9.5 6a17.6 17.6 0 0 1-3 3.3M6.6 6.6A17.3 17.3 0 0 0 2.5 12c1 1.4 4.5 6 9.5 6 1.6 0 3-.4 4.3-1.1M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
  workbench: '<rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="M7 10l3 2.5L7 15M12.5 15h4"/>',
  room: '<path d="M3.5 11 12 4l8.5 7"/><path d="M5.5 9.5V20h13V9.5"/><path d="M10 20v-5.5h4V20"/>',
  move: '<path d="M12 3v18M3 12h18"/><path d="M9 6l3-3 3 3M9 18l3 3 3-3M6 9l-3 3 3 3M18 9l3 3-3 3"/>',
  pin: '<path d="M8.5 3.5h7M10 3.5l-.6 6L6.5 13v1.5h11V13l-2.9-3.5-.6-6M12 14.5V21"/>',
  pinOff: '<path d="M8.5 3.5h7M10 3.5l-.6 6L6.5 13v1.5h11V13l-2.9-3.5-.6-6M12 14.5V21M3 3l18 18"/>',
};
const svg = (name) => `<svg viewBox="0 0 24 24" aria-hidden="true">${ICONS[name]}</svg>`;
const HOLD_S = 6;          // 说完后对话栏停留几秒
const DOCK_HIDE_MS = 1500; // 光标离开后侧菜单几毫秒收起
const HISTORY = 8;         // 展开时显示最近几轮
const ASK_S = 30;          // 工具确认框等几秒, 不回答算拒绝 (和服务端的 90 秒超时配合)

export class Shell {
  /** actions: { mic(), type(), mute(), pin(), room(), settings(), workbench(), hide(), persona(id), stopTool(callId), controlStop(), watchStop(), send(text), focus() } 由 pet.js 接到宿主和对话通道 */
  constructor(root, actions) {
    this.actions = actions;
    this.state = 'idle';
    this.until = 0;
    this.history = [];
    this.current = null;
    this.expanded = false;
    this.hovered = false;
    this.root = root;
    root.innerHTML = `
      <div id="think" class="think" aria-hidden="true" hidden><i></i><i></i><i></i></div>
      <div class="talk-stack">
      <div id="control" class="control hit" role="status" hidden>
        <span class="control-dot"></span><span class="control-text">她在操作你的电脑</span>
        <button type="button" class="control-stop" title="停下来 (她会知道你停了)">停</button>
        <span class="control-key"></span>
      </div>
      <div id="watch" class="watch hit" role="status" hidden>
        <span class="watch-icon" aria-hidden="true"></span><span class="watch-text">她在陪你看</span><span class="watch-what"></span>
        <button type="button" class="watch-stop" title="不看了 (和说「不看了」一样)">不看了</button>
        <span class="watch-hint" hidden></span>
      </div>
      <div id="ask" class="ask hit" role="alertdialog" aria-live="assertive" hidden>
        <div class="ask-title">她想用一个工具</div>
        <div class="ask-what"></div>
        <div class="ask-args"></div>
        <div class="ask-note"></div>
        <div class="ask-buttons">
          <button type="button" data-how="once">允许一次</button>
          <button type="button" data-how="always">总是允许</button>
          <button type="button" data-how="turn" hidden>这一轮都允许</button>
          <button type="button" data-how="deny">拒绝</button>
        </div>
        <div class="ask-bar"><i></i></div>
      </div>
      <div id="island" class="island hit" data-state="idle" hidden>
        <div class="island-row">
          <span class="island-icon"><i></i><i></i><i></i><i></i><i></i></span>
          <span class="island-label"></span>
          <button type="button" class="island-stop" title="停掉这次工具调用 (她会知道你停了)" hidden>停</button>
        </div>
        <div class="island-body">
          <div class="island-heard"></div>
          <div class="island-reply"></div>
        </div>
        <div class="island-panel">
          <ol class="island-history"></ol>
          <input class="island-input" type="text" autocomplete="off" placeholder="说点什么, 回车发送, Esc 收起">
        </div>
      </div>
      </div>
      <div id="personas" class="persona-menu hit" role="menu" hidden>
        <ul></ul>
        <button type="button" class="manage">管理预设…</button>
      </div>
      <nav id="dock" class="dock" aria-label="桌宠菜单">
        <button type="button" class="hit move" data-act="move" title="按住拖动, 把桌宠挪到想放的地方">${svg('move')}</button>
        <button type="button" class="hit" data-act="mic" title="麦克风: 开着时说话就能对话">${svg('micOff')}</button>
        <button type="button" class="hit" data-act="type" title="打字">${svg('keyboard')}</button>
        <button type="button" class="hit" data-act="persona" title="人设预设">${svg('persona')}</button>
        <button type="button" class="hit" data-act="room" title="小屋模式「抹茶樱花」小筑居 (再点一下回到桌面)">${svg('room')}</button>
        <button type="button" class="hit" data-act="mute" title="静音">${svg('volume')}</button>
        <button type="button" class="hit on" data-act="pin" title="置顶中: 点一下取消">${svg('pin')}</button>
        <button type="button" class="hit" data-act="workbench" title="工作台: 她替你干活 (读写文件、搜、跑命令)">${svg('workbench')}</button>
        <button type="button" class="hit" data-act="settings" title="设置: 人设预设、模型、MCP 服务器、skill">${svg('settings')}</button>
        <button type="button" class="hit" data-act="hide" title="隐藏 (托盘里可以再显示)">${svg('hide')}</button>
      </nav>`;
    const $ = (s) => root.querySelector(s);
    this.el = { island: $('#island'), label: $('.island-label'), icon: $('.island-icon'), heard: $('.island-heard'),
      reply: $('.island-reply'), body: $('.island-body'), history: $('.island-history'), input: $('.island-input'),
      dock: $('#dock'), mic: $('[data-act="mic"]'), mute: $('[data-act="mute"]'), pin: $('[data-act="pin"]'),
      ask: $('#ask'), askTitle: $('.ask-title'), askWhat: $('.ask-what'), askArgs: $('.ask-args'), askNote: $('.ask-note'),
      askAlways: $('[data-how="always"]'), askTurn: $('[data-how="turn"]'), askBar: $('.ask-bar i'),
      control: $('#control'), controlKey: $('.control-key'), watch: $('#watch'), watchWhat: $('.watch-what'),
      watchText: $('.watch-text'), watchHint: $('.watch-hint'),
      persona: $('[data-act="persona"]'), personas: $('#personas'), personaList: $('#personas ul'), stop: $('.island-stop'),
      think: $('#think') };
    this.stopId = null;  // 在调的工具 (对话栏上带"停"按钮)
    this.personas = [];
    this.activePersona = null;
    this.el.personas.addEventListener('click', (e) => {
      const item = e.target.closest('[data-id]');
      if (item) {
        this.showPersonas(false);
        if (item.dataset.id !== this.activePersona) this.actions.persona?.(item.dataset.id);
      } else if (e.target.closest('.manage')) {
        this.showPersonas(false);
        this.actions.settings?.();
      }
    });
    this.asks = [];  // 等用户回答的工具调用 (一次只显示第一个)
    this.el.ask.addEventListener('click', (e) => {
      const how = e.target.closest('button')?.dataset.how;
      if (how && this.asks[0]) this._answer(this.asks[0], how);
    });
    this.el.control.addEventListener('click', (e) => {
      if (e.target.closest('.control-stop')) this.actions.controlStop?.();
    });
    this.el.watch.addEventListener('click', (e) => {
      if (e.target.closest('.watch-stop')) this.actions.watchStop?.();
    });
    this.bars = [...this.el.icon.querySelectorAll('i')];
    // 拖动把手: 按下就交给宿主跟着光标挪窗口 (松手停在那里)
    this.el.dock.addEventListener('pointerdown', (e) => {
      if (e.button === 0 && e.target.closest('button')?.dataset.act === 'move') this.actions.drag?.();
    });
    this.el.dock.addEventListener('click', (e) => {
      const act = e.target.closest('button')?.dataset.act;
      if (act === 'type') this.openInput();
      else if (act === 'persona') this.showPersonas(this.el.personas.hidden);
      else if (act) this.actions[act]?.();
    });
    this.el.island.addEventListener('click', (e) => {
      if (e.target.closest('.island-stop')) {
        if (this.stopId) this.actions.stopTool?.(this.stopId);
        this.toolNote('停掉了, 想一想…');
        return;
      }
      if (!e.target.closest('.island-panel')) this.setExpanded(!this.expanded);
    });
    this.el.input.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') this.setExpanded(false);
      if (e.key !== 'Enter' || e.isComposing) return;
      const text = this.el.input.value.trim();
      this.el.input.value = '';
      if (text) {
        this.setExpanded(false);
        this.actions.send(text);
      }
    });
  }

  // -- 对话的各个阶段 (talk.js 的钩子调这些) ---------------------------------------------
  listening() {
    if (this.state === 'speaking') return;
    this._set('listening', '在听…');
    this.el.heard.textContent = '';
    this.el.reply.textContent = '';
  }

  /** 那一段声音没认出话 (或是外放的回声): "在听…" 收起, 回到闲着. 已经在想 / 在说了就不动 */
  listeningEnd() {
    if (this.state === 'listening') this._set('idle', '');
  }

  partial(text) {
    if (!text) return;
    this._set('listening', '在听…');
    this.el.heard.textContent = text;
  }

  /** tag: 这一轮不是你说的话时的小标签 (陪看那一轮 text 是空的, 写"陪看"; 以前显示一条空的"你说的") */
  turn(text, tag = '') {
    this.current = { user: text, pet: '', tag };
    this.history.push(this.current);
    if (this.history.length > HISTORY) this.history.shift();
    this.el.heard.textContent = text;
    if (tag) this.el.heard.prepend(this._tag(tag));
    this.el.reply.textContent = '';
    this._set('thinking', '');  // "在想"由头左边的思考气泡表示, 对话栏里只留原话
  }

  reply(text) {
    if (this.current) this.current.pet += text;
    this._set('speaking', '');
    this.el.reply.textContent += text;
    this.el.body.scrollTop = this.el.body.scrollHeight;  // 长回复 (讲故事) 停在最新一行
  }

  done() {
    if (this.state === 'speaking' || this.state === 'thinking') this._set('done', '');
    this.until = performance.now() / 1000 + HOLD_S;
  }

  /** 临时提示 (比如麦克风打不开), 停几秒收起. */
  notice(text, seconds = HOLD_S) {
    this.el.heard.textContent = '';
    this.el.reply.textContent = text;
    this._set('done', '');
    this.until = performance.now() / 1000 + seconds;
  }

  /** 工具正在调用: 对话栏上写一句 (还在"想"的阶段, 回复还没开始). stopId: 在调的那次的 call_id, 带一个"停"按钮. */
  toolNote(text, stopId = null) {
    if (this.state === 'thinking' || this.state === 'done' || this.state === 'idle') this._set('thinking', text);
    this.stopId = this.state === 'thinking' ? stopId : null;
    this.el.stop.hidden = !this.stopId;
  }

  /** 操控电脑的提示条: on 时对话栏上面一条"她在操作你的电脑", 带"停"和快捷键. */
  control(on, hotkey = '') {
    this.el.control.hidden = !on;
    this.el.controlKey.textContent = hotkey;
    this._render();
  }

  /** 陪看的提示条 (2026-11-07): on 时对话栏上面一条"她在陪你看 · 什么", 带"不看了". 一看就是很久, 不算 active (不把帧率一直拉到 30).
   *  屏幕陪看 (2026-11-08): kind 'screen', 开头换成"她在看你的屏幕"、图标换成一块小屏幕, hint 是下面一行小字 (写什么见 watch-bar.js). */
  watch(on, what = '', { lead = '她在陪你看', hint = '', kind = 'video' } = {}) {
    const w = this.el.watch;
    w.hidden = !on;
    w.classList.toggle('screen', kind === 'screen');
    w.classList.remove('ending');
    this.el.watchText.textContent = lead;
    this.el.watchWhat.textContent = what;
    this.el.watchHint.textContent = hint;
    this.el.watchHint.hidden = !hint;
    w.title = what ? `${lead} ${what}${hint ? `（${hint}）` : ''}` : hint;
  }

  /** 陪看结束了、提示条先别收: 换成一句为什么 (没有"不看了"按钮), 由调用的人过一会儿 watch(false). 本来就没显示的不弹出来, 返回 false. */
  watchEnding(note) {
    const w = this.el.watch;
    if (w.hidden) return false;
    w.classList.add('ending');
    this.el.watchText.textContent = note;
    this.el.watchWhat.textContent = '';
    this.el.watchHint.hidden = true;
    w.title = note;
    return true;
  }

  _tag(text) {
    const t = document.createElement('span');
    t.className = 'heard-tag';
    t.textContent = text;
    return t;
  }

  /** 这次工具调用有结果了 (成了、没成、停掉了): 收起"停". */
  toolDone(callId) {
    if (this.stopId !== callId) return;
    this.stopId = null;
    this.el.stop.hidden = true;
  }

  // -- 工具确认 ---------------------------------------------------------------------------
  /**
   * 云端模型要调一个"每次确认"的工具: 在对话栏下面问用户. info: {call_id, server, tool, title, args, tier?, note?, turn_ok?}.
   * 操控电脑的 (turn_ok) 没有"总是允许", 换成"这一轮都允许"; note 是给用户的提醒 (会发给谁、会动鼠标键盘).
   * 返回 Promise: 'once' | 'always' | 'turn' | 'deny' | 'timeout' (timeoutS 秒没回答). 同时来好几个时排队, 一个一个问.
   * auto: 自检用, 不等用户, 隔一会儿自动给这个回答.
   */
  askTool(info, timeoutS = ASK_S, auto = null) {
    return new Promise((resolve) => {
      this.asks.push({ info, resolve, timeoutS, auto });
      if (this.asks.length === 1) this._showAsk();
    });
  }

  /** 服务端不等了 (超时、这一轮被打断): 收起这一个. */
  cancelAsk(callId) {
    const a = this.asks.find((x) => x.info.call_id === callId);
    if (a) this._answer(a, null);
  }

  /** 主人在别处 (QQ 上) 答复了这张卡片: 当成点了这个按钮 (once | deny). 卡片早收起来了就什么都不做. */
  answerAsk(callId, how) {
    const a = this.asks.find((x) => x.info.call_id === callId);
    if (a && (how === 'once' || how === 'deny')) this._answer(a, how);
    return !!a;
  }

  _showAsk() {
    clearTimeout(this.askTimer);
    const a = this.asks[0];
    if (!a) {
      this.el.ask.hidden = true;
      this._render();
      return;
    }
    const name = a.info.title && a.info.title !== a.info.tool ? `${a.info.title} (${a.info.tool})` : a.info.tool;
    this.el.askTitle.textContent = { screen: '她想看一眼你的屏幕', act: '她想操作你的电脑', power: '她想用高风险的工具 (命令、文件、系统)' }[a.info.tier] || '她想用一个工具';
    this.el.ask.classList.toggle('ask-power', a.info.tier === 'power');
    this.el.askWhat.textContent = `${a.info.server} · ${name}`;
    this.el.askArgs.textContent = a.info.args && a.info.args !== '{}' ? a.info.args : '';
    this.el.askNote.textContent = a.info.note || '';
    // 按钮: 宿主给了 options 就按它的 (网址只有"允许一次 / 拒绝"); 操控电脑的是"允许一次 / 这一轮都允许 / 拒绝"
    const opts = a.info.options || (a.info.turn_ok ? ['once', 'turn', 'deny'] : ['once', 'always', 'deny']);
    for (const b of this.el.ask.querySelectorAll('.ask-buttons button')) b.hidden = !opts.includes(b.dataset.how);
    this.el.ask.hidden = false;
    // 倒计时条: 从满到空
    const bar = this.el.askBar;
    bar.style.transition = 'none';
    bar.style.transform = 'scaleX(1)';
    void bar.offsetWidth;
    bar.style.transition = `transform ${a.timeoutS}s linear`;
    bar.style.transform = 'scaleX(0)';
    this.askTimer = setTimeout(() => this._answer(a, 'timeout'), a.timeoutS * 1000);
    if (a.auto && a.auto !== 'timeout') this.askAuto = setTimeout(() => this._answer(a, a.auto), 1500);
    this._render();
  }

  _answer(a, how) {
    const i = this.asks.indexOf(a);
    if (i < 0) return;
    this.asks.splice(i, 1);
    clearTimeout(this.askAuto);
    if (how) a.resolve(how);
    if (i === 0) this._showAsk();
  }

  // -- 侧菜单和输入 ---------------------------------------------------------------------
  hover(on) {
    this.hovered = on;
    clearTimeout(this.dockTimer);
    if (on) this.el.dock.classList.add('open');
    else this.dockTimer = setTimeout(() => {
      if (this.expanded) return;
      this.el.dock.classList.remove('open');
      this.showPersonas(false);
    }, DOCK_HIDE_MS);
    this._render();
  }

  setMic(on) {
    this.micOn = on;
    this.el.mic.classList.toggle('on', on);
    this.el.mic.innerHTML = svg(on ? 'mic' : 'micOff');
    this.el.mic.title = on ? '麦克风开着: 点一下关掉' : '麦克风关着: 点一下打开, 说话就能对话';
    this._render();
  }

  /** 人设预设列表 (宿主给的: [{id, name, provider}]) 和当前是哪个. */
  setPersonas(list, active) {
    const changed = this.activePersona !== null && active !== this.activePersona;
    this.personas = list || [];
    this.activePersona = active;
    const cur = this.personas.find((p) => p.id === active);
    this.el.persona.title = `人设预设: ${cur?.name || '默认'} (点一下换)`;
    if (!this.el.personas.hidden) this._renderPersonas();
    if (changed && cur) this.notice(`换成「${cur.name}」了`);
  }

  /** 侧菜单里的人设列表: 贴在人设按钮左边. */
  showPersonas(on) {
    if (on) {
      this._renderPersonas();
      const b = this.el.persona.getBoundingClientRect();
      this.el.personas.style.top = `${Math.max(8, b.top - 6)}px`;
    }
    this.el.personas.hidden = !on;
    this.el.persona.classList.toggle('on', on);
    this._render();
  }

  _renderPersonas() {
    const ul = this.el.personaList;
    ul.innerHTML = '';
    for (const p of this.personas) {
      const li = document.createElement('li');
      li.dataset.id = p.id;
      li.className = p.id === this.activePersona ? 'cur' : '';
      li.setAttribute('role', 'menuitemradio');
      const name = document.createElement('span');
      name.textContent = p.name || '(没有名字)';
      const kind = document.createElement('small');
      kind.textContent = `${p.face === 'live2d' ? '2D · ' : ''}${p.provider === 'local' ? '本地' : '云端'}`;
      li.append(name, kind);
      ul.append(li);
    }
  }

  setMuted(on) {
    this.el.mute.classList.toggle('on', on);
    this.el.mute.innerHTML = svg(on ? 'volumeOff' : 'volume');
    this.el.mute.title = on ? '已静音: 点一下恢复' : '静音';
  }

  /** 置顶 (宿主报的窗口真实状态). 用户切换时在对话栏提示一下. */
  setPinned(on) {
    const changed = this.pinned !== undefined && on !== this.pinned;
    this.pinned = on;
    this.el.pin.classList.toggle('on', on);
    this.el.pin.innerHTML = svg(on ? 'pin' : 'pinOff');
    this.el.pin.title = on ? '置顶中: 点一下取消' : '没有置顶 (会被别的窗口盖住): 点一下置顶';
    if (changed) this.notice(on ? '置顶了' : '不置顶了, 别的窗口可以盖住我');
  }

  openInput() {
    this.setExpanded(true);
    this.el.input.focus();
    this.actions.focus();
  }

  setExpanded(on) {
    this.expanded = on;
    if (on) {
      this.el.history.innerHTML = '';
      for (const h of this.history) {
        const li = document.createElement('li');
        const u = document.createElement('p');
        u.className = 'h-user';
        u.textContent = h.user;
        if (h.tag) u.prepend(this._tag(h.tag));
        const p = document.createElement('p');
        p.className = 'h-pet';
        p.textContent = h.pet || '…';
        li.append(u, p);
        this.el.history.append(li);
      }
      this.el.history.scrollTop = this.el.history.scrollHeight;
    } else {
      this.el.input.blur();
      if (!this.hovered) this.hover(false);
    }
    this._render();
  }

  /** 每帧: 音柱跟着麦克风音量跳 (dB), 说完的停留到时就收起. */
  tick(now, micLevelDb) {
    if (this.state === 'listening') {
      const v = Math.max(0, Math.min(1, (micLevelDb + 60) / 45));
      this.bars.forEach((b, i) => { b.style.transform = `scaleY(${0.25 + v * (0.5 + 0.5 * Math.sin(now * 9 + i * 1.3) ** 2)})`; });
    }
    if (this.state === 'done' && now > this.until && !this.expanded) this._set('idle', '');
  }

  /** 可以点的部分 (算进命中掩码). */
  hitRects() {
    const dockOpen = this.el.dock.classList.contains('open');
    // 对话栏和菜单都是 position: fixed, offsetParent 恒为 null; 用有没有布局框判断是否显示
    return [...this.root.querySelectorAll('.hit')].filter((e) => e.getClientRects().length > 0 && (dockOpen || !e.closest('.dock')))
      .map((e) => e.getBoundingClientRect());
  }

  get active() { return this.expanded || this.asks.length > 0 || !this.el.control.hidden || (this.state !== 'idle' && this.state !== 'done'); }

  _set(state, label) {
    this.state = state;
    this.el.label.textContent = label;
    if (state !== 'thinking') {  // 开始说了 (或收起了): 工具那一步过去了
      this.stopId = null;
      this.el.stop.hidden = true;
    }
    this._render();
  }

  /**
   * 趴着时的栏杆线 (窗口高度的比例, null: 站着) 和头顶的位置 (CSS 像素): 栏杆线以下是裁掉的,
   * 对话栏挪到头顶上方的空处 (从窗口顶往下长, 展开的记录按头顶以上的空间收矮), 侧菜单挪到栏杆线以上.
   */
  setRail(rail, headTop) {
    const s = this.el.dock.parentElement.style;  // 根元素 (#shell) 上的 CSS 变量
    const stack = this.el.island.parentElement;
    if (rail === null) {
      this.leaning = false;
      stack.classList.remove('lean');
      for (const k of ['--dock-top', '--lean-history']) s.removeProperty(k);
      this._placeDock();  // 站起来了: 侧菜单回到缩放后的位置 (没缩放时 CSS 默认)
      return;
    }
    this.leaning = true;
    stack.classList.add('lean');
    const dockPct = (this.el.dock.offsetHeight / innerHeight) * 100;
    s.setProperty('--dock-top', `${Math.max(2, rail * 100 - dockPct - 2).toFixed(2)}%`);
    // 对话栏在栈顶往下长, 栈顶 = 窗口高 - (窗口高 - 8) * r (没缩放时就是顶边下 8 像素); 记录最多长到头顶以上
    const stackTop = (this.scale ?? 1) < 1 ? innerHeight - (innerHeight - 8) * this.scale : 8;
    if (headTop !== null) s.setProperty('--lean-history', `${Math.max(48, Math.round(headTop - 112 - stackTop))}px`);
  }

  /**
   * 桌宠的大小 (pet.js 的 applyPetScale 给): r (<= 1) 是模型占满窗口的比例. 对话栏、侧菜单是按窗口百分比定位的,
   * 模型缩小后它们要跟着往下靠 (--pet-k 让 CSS 里的 bottom 11% / 趴着时的顶边按 r 压缩); 侧菜单算成像素并夹在窗口里.
   * r = 1 时什么变量都不设, 和没有这个功能时完全一样.
   */
  setScale(r) {
    this.scale = r < 1 ? r : 1;
    const s = this.el.dock.parentElement.style;
    if (this.scale < 1) s.setProperty('--pet-k', this.scale.toFixed(4));
    else s.removeProperty('--pet-k');
    if (this.leaning) {  // 趴着时侧菜单和记录高度由 setRail 管, pet.js 下一帧会再调 setRail; 这里只让变量先到位
      return;
    }
    this._placeDock();
  }

  _placeDock() {
    const s = this.el.dock.parentElement.style;
    if (!((this.scale ?? 1) < 1)) { s.removeProperty('--dock-top'); return; }
    const H = innerHeight, h = this.el.dock.offsetHeight || 440;
    const top = Math.max(8, Math.min(H - (H - 0.36 * H) * this.scale, H - h - 8));  // 原来在 36% 处; 压向脚底, 但整条要在窗口里
    s.setProperty('--dock-top', `${Math.round(top)}px`);
  }

  /** 侧菜单的顶边放在这里 (CSS 像素; 小屋模式: 卡片右边的那条空处). size / gap: 按钮多大、隔多远 (放不下时缩小). */
  placeDock(topPx, size = null, gap = null) {
    const st = this.el.dock.parentElement.style;
    st.setProperty('--dock-top', `${Math.round(topPx)}px`);
    if (size) st.setProperty('--dock-btn', `${size}px`);
    if (gap) st.setProperty('--dock-gap', `${gap}px`);
  }

  /** 头 (脸) 在窗口里的位置 (CSS 像素, pet.js 每次算掩码时给): 思考气泡贴在头的左边. */
  setHead(x, y) {
    if (this.head && Math.abs(this.head[0] - x) < 1 && Math.abs(this.head[1] - y) < 1) return;
    const first = !this.head;
    this.head = [x, y];
    this.el.think.style.left = `${Math.round(x - 44)}px`;  // 气泡的右边缘在脸的左边 (CSS 里往左平移自身宽度)
    this.el.think.style.top = `${Math.round(y - 30)}px`;
    if (first) this._render();
  }

  // 麦克风开着时对话栏的位置一直留一个小红点 (开着麦就该让人看得见); 光标在角色上时再带一句提示
  _render() {
    const i = this.el.island;
    const thinking = this.state === 'thinking' && !this.expanded;
    // 在想的时候没有原话也没有提示 (比如本机接口发来的、还没显示的一轮): 只有思考气泡
    const blank = thinking && !this.el.heard.textContent && !this.el.label.textContent;
    const show = this.expanded || (this.state !== 'idle' && !blank) || this.micOn;
    i.hidden = !show;
    i.dataset.state = this.expanded ? 'expanded' : this.state === 'idle' ? 'peek' : this.state;
    if (this.state === 'idle' && !this.expanded) this.el.label.textContent = this.micOn && this.hovered ? '麦克风开着, 直接说话' : '';
    this.el.think.hidden = !thinking || !this.head;
  }
}
