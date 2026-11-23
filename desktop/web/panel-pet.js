// 操作面板的"桌宠与工作台"页 (2026-10-26 用户: "设置也是, 会直接综合成一个工作台+桌宠的整体设置配置菜单"):
// 以前只在托盘里的开关 (置顶、眼睛跟着鼠标、趴下、落地、看你在做什么、小屋、帧率、本机接口) 和工作台的两样
// (打开工作台时收起桌宠、桌前的她) 放在一页. 桌宠的开关在宿主那边 (op pet-get / pet-set, 改了马上生效、记进设置);
// 桌前的她是工作台页面自己记的 (localStorage, 和工作台同源, 改了工作台马上跟着变).
import { growthForget, renderGrowth } from './panel-growth.js';
import { experienceForget, renderExperience } from './panel-experience.js';
import { clampPetScale, readPetScale, readTermSplash, writePetScale, writeTermSplash } from './ui-preferences.js';

// "时间"一栏 (2026-10-27 用户: "时间可以在设置里面预设，如果不预设，就根据设备时间或者地理位置时间决定"): 存在服务端
// (GET / POST /api/clock, wm/tz.py), 不在宿主的设置里: 服务端 (QQ 上的她、睡前回顾) 和桌宠页面 (小屋的夜晚) 都按它.
const pet = { data: null, error: '', section: 'companion' };
const tzs = { data: null, error: '', saving: false };
const DESK_KEY = 'wm.workbench.desk';

async function loadTz(ctx) {
  try {
    const r = await fetch('/api/clock', { cache: 'no-store' });
    if (r.status === 404) throw new Error('服务端是加时区设置之前起的: 重启服务端以后才能设');
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    tzs.data = await r.json();
    tzs.error = '';
  } catch (err) {
    tzs.error = err.message.includes('重启') ? err.message : `连不上服务端 (${err.message}): 服务端开着才能设`;
  }
  if (ctx.isTab()) ctx.render();
}

async function saveTz(ctx, tz) {
  tzs.saving = true;
  ctx.render();
  try {
    const r = await fetch('/api/clock', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ tz }) });
    if (!r.ok) throw new Error(await r.text() || `HTTP ${r.status}`);
    tzs.data = await r.json();
    tzs.error = '';
  } catch (err) {
    tzs.error = `没改成: ${err.message}`;
  }
  tzs.saving = false;
  if (ctx.isTab()) ctx.render();
}

function tzCard(esc) {
  const t = tzs.data;
  if (!t) return `<div class="card"><h2>时间</h2><p class="hint">${esc(tzs.error || '读取中…')}</p></div>`;
  const dev = `${t.device.name}${t.device.zone ? ` (${t.device.zone})` : ''}`;
  const zones = t.zones.some((z) => z.value === t.tz) ? t.zones : [...t.zones, { value: t.tz, label: '' }];
  return `
    <div class="card">
      <h2>时间</h2>
      <div class="row inline timezone-row"><span class="tr-text"><b>时区</b><span class="hint">用于消息时间、免打扰和小屋的昼夜。默认跟随电脑。</span></span>
        <select id="petTz" ${tzs.saving ? 'disabled' : ''}>${zones.map((z) => `<option value="${esc(z.value)}" ${z.value === t.tz ? 'selected' : ''}>${
          esc(z.value ? `${z.value}${z.label ? ` ${z.label}` : ''}` : `跟着电脑 (${dev})`)}</option>`).join('')}</select></div>
      <p class="hint">她那边现在是 ${esc(t.now)}${tzs.error ? ` · <span class="warn">${esc(tzs.error)}</span>` : ''}</p>
    </div>`;
}

// 陪伴模式 / 实验室模式 (2026-11-01 用户: "不能把桌宠完全用于搞动作仿真, 动作仿真是我们的商业竞争模型……而桌宠更多是社交、社区"):
// 实验室 = 原来的"小屋模式" (宿主的 room 开关: 3D 生活小屋、拿放和物理、动作成长都在这里), 是动作模型的研发场;
// 陪伴 = 小屋关着, 在桌面上陪你 (2D、3D 都行). 宿主那边还是同一个 room 开关, 这里只换说法、放到最上面
const MODES = [
  ['companion', '陪伴模式', '推荐', '在桌面上陪你聊天、看你在做什么，2D、3D 都行。不跑动作实验，更稳、更省显卡。'],
  ['lab', '实验室模式', '研发中', '3D 生活小屋：走动、拿放、喝咖啡，动作会自己练。这是动作模型的研发场，常有做不成、穿插，也更吃显卡。'],
];

function modeCard(p) {
  const cur = p.room && !p.live2d ? 'lab' : 'companion';  // 2D 门面时宿主不开小屋 (room 设置可能还留着 true)
  return `
    <div class="card mode-card">
      <h2>桌宠模式</h2>
      <div class="mode-choices" role="radiogroup" aria-label="桌宠模式">${MODES.map(([id, name, tag, desc]) => {
        const off = id === 'lab' && p.live2d;
        return `<button type="button" role="radio" class="mode-choice ${cur === id ? 'on' : ''}" data-mode="${id}"
          aria-checked="${cur === id}" ${off ? 'disabled' : ''}>
          <span class="mode-head"><b>${name}</b><span class="mode-tag ${id}">${tag}</span></span>
          <span class="hint">${desc}</span></button>`;
      }).join('')}</div>
      ${p.live2d ? '<p class="hint">2D 门面没有小屋，只能用陪伴模式；换成 3D 门面才能进实验室。</p>' : ''}
    </div>`;
}

// 大小 (2026-11-16 pet-scale): 存在 localStorage (ui-preferences.js), 桌宠页面收到 storage 事件马上跟着变; 只有 2D 门面能缩放.
// 现在的窗口是宿主定死的, 放不下比 100% 更大的 (要放大得换新版宿主), 所以滑条到 100% 为止.
const scaleRow = (p) => {
  const pct = Math.round(Math.min(1, readPetScale()) * 20) * 5;
  const off = !p.live2d;
  return `
      <div class="row inline"><span class="tr-text"><b>大小</b><span class="hint">${off ? '3D 门面这一版还不能缩放' : '鼠标放在她身上按住 Ctrl 滚滑轮 (触控板双指捏合) 也可以。最大是现在窗口放得下的 100%, 想更大需要新版桌宠程序'}</span></span>
        <span class="scale-ctl" style="display:inline-flex;align-items:center;gap:8px;white-space:nowrap"><input type="range" id="petScale" min="35" max="100" step="5" value="${Math.max(35, pct)}" aria-label="桌宠大小" ${off ? 'disabled' : ''}> <b id="petScaleVal">${Math.max(35, pct)}%</b> <button type="button" id="petScaleReset" ${off ? 'disabled' : ''}>还原</button></span></div>`;
};

const deskOn = () => { try { return localStorage.getItem(DESK_KEY) !== '0'; } catch { return true; } };
const row = (key, title, desc, on, disabled = false) => `
  <label class="toggle-row ${disabled ? 'off' : ''}">
    <span class="tr-text"><b>${title}</b><span class="hint">${desc}</span></span>
    <input type="checkbox" class="switch" data-key="${key}" ${on ? 'checked' : ''} ${disabled ? 'disabled' : ''}>
  </label>`;

async function load(ctx) {
  const r = await ctx.call('pet-get');
  if (r?.ok) { pet.data = r.pet; pet.error = ''; } else pet.error = '宿主没回 (旧版桌宠?)';
  if (ctx.isTab()) ctx.render();
}

/** ctx: { esc, $, $$, call, render, isTab() } */
export function renderPet(main, ctx) {
  const { esc, $$ } = ctx;
  if (!pet.data) {
    main.innerHTML = `<h1>桌宠与工作台</h1><p class="empty">${esc(pet.error || '读取中…')}</p>`;
    if (!pet.error) load(ctx);
    return;
  }
  const p = pet.data;
  if (pet.section === 'companion' && !tzs.data && !tzs.error) loadTz(ctx);
  main.innerHTML = `
    <h1>桌宠与工作台</h1>
    <p class="lead">调整她的陪伴方式。更改即时生效，与桌面托盘保持同步。</p>
    <div class="subtabs" role="tablist" aria-label="桌宠设置分类">${[['companion', '陪伴'], ['growth', '动作成长（实验室）'], ['experience', '经历与数据']].map(([id, label]) => `<button type="button" role="tab" aria-selected="${pet.section === id}" class="${pet.section === id ? 'on' : ''}" data-pet-section="${id}">${label}</button>`).join('')}</div>
    <section class="settings-section" data-section="companion" ${pet.section === 'companion' ? '' : 'hidden'}>
    ${modeCard(p)}
    <div class="card">
      <h2>工作台</h2>
      ${row('workbench_hides', '打开工作台时收起桌宠', '关闭或最小化工作台后，她会回到桌面。语音陪伴不受影响。', p.workbench_hides)}
      ${row('desk', '在侧栏显示她', '在工作台中陪你工作。关闭后释放角色占用的显存。', deskOn())}
      ${row('termsplash', '启动画面', '每次启动程序时先显示 S.A.T.I.O Verse 的开场 (点一下可跳过)。只在冷启动放；系统设了“减少动态效果”时只淡入淡出。', readTermSplash())}
    </div>
    <div class="card">
      <h2>桌面上的她</h2>
      ${row('topmost', '始终置顶', '让她显示在其他窗口上方。', p.topmost)}
      ${row('follow_mouse', '眼睛跟着鼠标', '', p.follow_mouse)}
      ${row('lean', '放着不动会趴下', p.lean_ok ? '在窗口顶边上放着不动一会儿就趴下' : '这个角色趴不了', p.lean, !p.lean_ok)}
      ${row('gravity', '松手落到地面', '关着时停在松手的地方', p.gravity)}
      ${row('activity', '感知当前活动', '只在本机读取前台程序名称，不截图或读取聊天内容。', p.activity)}
      <div class="row inline"><span class="tr-text"><b>空闲时的帧率</b><span class="hint">越低越省电; 说话、动起来时会自己升上去</span></span>
        <select id="petFps">${[10, 15, 30, 60].map((f) => `<option value="${f}" ${p.fps === f ? 'selected' : ''}>${f} 帧</option>`).join('')}</select></div>
      ${scaleRow(p)}
    </div>
    ${tzCard(esc)}
    <div class="card">
      <h2>本机接口</h2>
      ${row('api', `OpenAI 兼容 API 和 MCP (端口 ${p.api_port})`, '给别的程序 (比如 Claude Code、dsh) 连她用; 访客的 token 在"外部渠道"页', p.api)}
    </div>
    </section>
    <section class="settings-section" data-section="growth" ${pet.section === 'growth' ? '' : 'hidden'}><div class="card" id="growthCard"></div></section>
    <section class="settings-section" data-section="experience" ${pet.section === 'experience' ? '' : 'hidden'}><div class="card" id="experienceCard"></div></section>`;
  $$('[data-pet-section]', main).forEach((button) => button.addEventListener('click', () => {
    pet.section = button.dataset.petSection;
    ctx.render();
    main.scrollTop = 0;
    main.querySelector(`[data-pet-section="${pet.section}"]`)?.focus();
  }));
  $$('.switch', main).forEach((el) => el.addEventListener('change', async () => {
    const key = el.dataset.key;
    if (key === 'desk') {
      try { localStorage.setItem(DESK_KEY, el.checked ? '1' : '0'); } catch { /* 无所谓 */ }
      return;
    }
    if (key === 'termsplash') {  // 终端启动画面: 页面自己的存储, 下次启动时生效
      writeTermSplash(el.checked);
      return;
    }
    el.disabled = true;
    const r = await ctx.call('pet-set', { key, value: el.checked });
    if (r?.ok) pet.data = r.pet;
    if (ctx.isTab()) ctx.render();
  }));
  $$('.mode-choice', main).forEach((el) => el.addEventListener('click', async () => {
    const lab = el.dataset.mode === 'lab';
    if (lab === !!pet.data.room) return;
    $$('.mode-choice', main).forEach((b) => { b.disabled = true; });
    const r = await ctx.call('pet-set', { key: 'room', value: lab });
    if (r?.ok) pet.data = r.pet;
    if (ctx.isTab()) ctx.render();
  }));
  main.querySelector('#petFps').addEventListener('change', async (e) => {
    const r = await ctx.call('pet-set', { key: 'fps', value: Number(e.target.value) });
    if (r?.ok) pet.data = r.pet;
  });
  main.querySelector('#petTz')?.addEventListener('change', (e) => saveTz(ctx, e.target.value));
  const scaleInput = main.querySelector('#petScale'), scaleVal = main.querySelector('#petScaleVal');
  if (scaleInput) {  // 拖动时不重绘整页 (会打断拖动), 只改数字和存储; 桌宠那边靠 storage 事件马上变
    scaleInput.addEventListener('input', () => {
      const k = clampPetScale(Number(scaleInput.value) / 100, 1);
      scaleVal.textContent = `${Math.round(k * 100)}%`;
      writePetScale(k);
    });
    main.querySelector('#petScaleReset').addEventListener('click', () => {
      scaleInput.value = '100';
      scaleVal.textContent = '100%';
      writePetScale(1);
    });
  }
  // 她自己练动作 (panel-growth.js): 放在最后, 上面给 .switch 挂的是宿主的开关, 别挂到它的开关上
  if (pet.section === 'growth') renderGrowth(main.querySelector('#growthCard'), ctx);
  if (pet.section === 'experience') renderExperience(main.querySelector('#experienceCard'), ctx);
}

/** 换页回来时重新读 (托盘里可能改过) */
export function petForget() { pet.data = null; tzs.data = null; tzs.error = ''; growthForget(); experienceForget(); }
