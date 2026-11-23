import { getPreferences, updatePreferences, initPreferences, getWallpaper, setWallpaperFile, clearWallpaper } from '/pet/ui-preferences.js';

export const PREFERENCE_SEARCH = [
  { tab: 'general', label: '发送消息', description: 'Enter Ctrl Enter 换行 输入框', selector: '#pref-sendKey' },
  { tab: 'general', label: '显示会话时间', description: '侧栏 时间 最近会话', selector: '#pref-showSessionTimes' },
  { tab: 'general', label: '展开工具调用', description: '工具 详情 命令 执行过程', selector: '#pref-expandToolDetails' },
  { tab: 'appearance', label: '外观', description: '主题 系统 浅色 深色', selector: '#pref-theme' },
  { tab: 'appearance', label: '主题配色', description: '强调色 紫色 蓝色 绿色 中性', selector: '#pref-themePreset' },
  { tab: 'appearance', label: '界面字号', description: '文字 字体 大小 导航', selector: '#pref-uiFontSize' },
  { tab: 'appearance', label: '对话字号', description: '正文 阅读 字体 大小', selector: '#pref-contentFontSize' },
  { tab: 'appearance', label: '代码字体', description: '等宽 Consolas 系统', selector: '#pref-codeFont' },
  { tab: 'appearance', label: '内容宽度', description: '紧凑 标准 宽阔 对话', selector: '#pref-contentWidth' },
  { tab: 'appearance', label: '减少动态效果', description: '动画 动效 系统', selector: '#pref-reducedMotion' },
  { tab: 'appearance', label: '玻璃材质', description: '琉璃 磨砂 模糊 不透明度 壁纸', selector: '#pref-material' },
  { tab: 'appearance', label: '壁纸与取色', description: '背景 图片 自动取色 本地', selector: '#pref-wallpaper' },
  { tab: 'appearance', label: '界面字体', description: '字体 自定义 微软雅黑 Segoe UI', selector: '#pref-uiFont' },
  { tab: 'notifications', label: '任务完成提醒', description: '通知 后台 提示 完成', selector: '#pref-notifyDone' },
  { tab: 'notifications', label: '待确认提醒', description: '批准 权限 等待 确认', selector: '#pref-notifyApproval' },
  { tab: 'notifications', label: '提示音', description: '声音 通知 静音', selector: '#pref-notificationSound' },
  { tab: 'shortcuts', label: '键盘快捷键', description: '键盘 Ctrl 搜索 新会话 设置 发送', selector: '#pref-shortcutSearch' },
];

const PRESETS = [
  ['neutral', '中性', '#696970'], ['lilac', '淡紫', '#8171c8'],
  ['blue', '海蓝', '#477bbd'], ['forest', '松绿', '#40836c'],
];
const TITLES = {
  general: ['常规', '让工作台更符合你的使用习惯。'],
  appearance: ['外观', '选择让你专注、舒适的界面。更改即时预览。'],
  notifications: ['通知', '选择何时在工作台内提醒你，安心处理手头的事。'],
  shortcuts: ['快捷键', '用键盘更快地切换、查找和继续工作。'],
};
const SELECTS = {
  sendKey: [['enter', 'Enter 发送'], ['ctrlEnter', 'Ctrl + Enter 发送']],
  uiFontSize: [13, 14, 15, 16].map((v) => [v, `${v} px`]),
  contentFontSize: [14, 15, 16, 18].map((v) => [v, `${v} px`]),
  codeFont: [['system', '系统等宽字体'], ['consolas', 'Consolas']],
  contentWidth: [['compact', '紧凑'], ['standard', '标准'], ['wide', '宽阔']],
  reducedMotion: [['system', '跟随系统'], ['on', '开启'], ['off', '关闭']],
  notifyDone: [['all', '始终提醒'], ['background', '仅在后台时'], ['off', '关闭']],
  material: [['glass', '柔雾玻璃'], ['solid', '简洁纯色']],
  uiFont: [['system','系统推荐'],['yahei','微软雅黑'],['segoe','Segoe UI'],['custom','自定义字体']],
};

let unbind = null;

export function renderPreferences(main, ctx) {
  unbind?.();
  initPreferences();
  const tab = TITLES[ctx.tab] ? ctx.tab : 'general';
  const esc = ctx.esc;
  const p = getPreferences();
  const heading = `<h1>${TITLES[tab][0]}</h1><p class="lead">${TITLES[tab][1]}</p>`;
  const select = (key) => `<select id="pref-control-${key}" data-pref="${key}">${SELECTS[key].map(([v, text]) => `<option value="${esc(v)}" ${p[key] === v ? 'selected' : ''}>${esc(text)}</option>`).join('')}</select>`;
  const toggle = (key) => `<input id="pref-control-${key}" type="checkbox" class="switch" data-pref="${key}" ${p[key] ? 'checked' : ''}>`;
  const row = (key, title, description, control = select(key)) => `<div class="pref-row" id="pref-${key}"><label for="pref-control-${key}"><span class="pref-label">${title}</span><span class="pref-description">${description}</span></label><div class="pref-control">${control}</div></div>`;
  const group = (title, content) => `<section class="pref-section"><h2>${title}</h2>${content}</section>`;
  const range = (key,min,max,unit) => `<div class="pref-range"><input id="pref-control-${key}" type="range" data-pref="${key}" min="${min}" max="${max}" value="${p[key]}" aria-valuetext="${p[key]}${unit}"><output data-pref-output="${key}" data-unit="${unit}" for="pref-control-${key}">${p[key]}${unit}</output></div>`;

  let body = '';
  if (tab === 'general') {
    body = group('对话', row('sendKey', '发送消息', '选择发送方式。Shift + Enter 始终用于换行。')
      + row('expandToolDetails', '默认展开工具调用', '打开会话时，直接查看命令、读取结果与文件改动。', toggle('expandToolDetails')))
      + group('会话栏', row('showSessionTimes', '显示会话时间', '在会话标题旁显示最近更新的时间。', toggle('showSessionTimes')));
  } else if (tab === 'appearance') {
    body = `<section class="pref-section pref-choice-section" id="pref-theme"><h2>视觉风格</h2><div class="pref-choice-label">显示模式</div><div class="pref-theme-options" role="radiogroup" aria-label="外观">
      ${[['system', '跟随系统'], ['light', '浅色'], ['dark', '深色']].map(([v, label]) => `<label class="pref-theme-option"><input type="radio" name="theme" value="${v}" data-pref="theme" ${p.theme === v ? 'checked' : ''}><span class="pref-theme-preview pref-preview-${v}" aria-hidden="true"><i class="preview-side"><b></b><b></b><b></b></i><i class="preview-page"><b></b><b></b><em></em></i><span class="pref-selected">✓</span></span><span class="pref-theme-name">${label}</span></label>`).join('')}</div></section>`
      + group('材质与背景', row('material','界面材质','导航与工具面板分层呈现，正文保持清晰。')
        + `<div class="pref-wallpaper" id="pref-wallpaper"><div class="pref-wallpaper-thumbnail" aria-hidden="true"></div><div><strong>让工作台有你的颜色</strong><p>选一张本地图片，自动提取主题色。图片仅保存在这台电脑。</p><div class="pref-wallpaper-actions"><button type="button" class="btn" data-wallpaper-choose>选择壁纸</button><button type="button" class="btn" data-wallpaper-clear ${getWallpaper()?'':'disabled'}>移除</button><span data-wallpaper-state role="status">${getWallpaper()?'已使用本地壁纸':'未设置壁纸'}</span></div></div><input type="file" accept="image/jpeg,image/png,image/webp" data-wallpaper-file hidden></div>`
        + `<details class="pref-material-details"><summary>调整玻璃质感</summary>${row('glassBlur','模糊程度','只影响面板后的背景。',range('glassBlur',0,48,' px'))}${row('glassOpacity','面板不透明度','提高数值可获得更稳定的阅读对比度。',range('glassOpacity',62,100,'%'))}${row('wallpaperDim','壁纸柔化','降低背景存在感，让内容更突出。',range('wallpaperDim',0,70,'%'))}</details>`)
      + `<section class="pref-section pref-choice-section" id="pref-themePreset"><h2>主题配色</h2><div class="pref-palette" role="radiogroup" aria-label="主题配色">${PRESETS.map(([v, label, color]) => `<label class="pref-swatch"><input type="radio" name="themePreset" data-pref="themePreset" value="${v}" ${p.colorSource==='preset'&&p.themePreset === v ? 'checked' : ''}><span style="--swatch:${color}"><i aria-hidden="true"></i>${label}</span></label>`).join('')}<label class="pref-custom-color"><input type="color" data-pref="seedColor" value="${p.seedColor}" aria-label="自定义强调色"><span>自选颜色</span></label></div><p class="hint" data-color-source>${p.colorSource==='wallpaper'?'当前配色来自壁纸':p.colorSource==='custom'?'当前使用自选颜色':'选择一种强调色'}</p></section>`
      + group('文字与阅读', row('uiFont','界面字体','设置、工作台和桌宠对话使用相同字体。')
        + `<div class="pref-row" id="pref-customFont" ${p.uiFont==='custom'?'':'hidden'}><label for="pref-control-customFont"><span class="pref-label">本机字体名称</span><span class="pref-description">未安装的字体会自动使用系统字体。</span></label><div class="pref-control"><input id="pref-control-customFont" data-pref="customFont" maxlength="80" value="${esc(p.customFont)}" placeholder="例如 MiSans"></div></div>`
        + row('uiFontSize', '界面字号', '调整导航、菜单和设置控件的文字大小。')
        + row('contentFontSize', '对话字号', '调整消息正文与输入框的文字大小。')
        + row('codeFont', '代码字体', '用于代码块、命令和文件路径。')
        + row('contentWidth', '内容宽度', '调整工作台中对话和输入框的最大宽度。'))
      + `<section class="pref-sample" aria-label="文字预览"><span>阅读预览</span><p>从一个想法开始，一起把事情做好。</p><code>const idea = "Hello, WorldModel";</code></section>`
      + group('界面效果', row('reducedMotion', '减少动态效果', '减少界面动画与过渡，不影响角色动作。'));
  } else if (tab === 'notifications') {
    body = group('工作台内提醒', row('notifyDone', '任务完成', '选择“仅在后台时”，会在返回工作台时显示待查看的提醒。')
      + row('notifyApproval', '等待你的确认', '需要批准操作时，提示你返回会话处理。', toggle('notifyApproval'))
      + row('notificationSound', '提示音', '提醒出现时播放轻提示音。首次打开后，需先与工作台交互一次。', toggle('notificationSound')));
  } else {
    const send = p.sendKey === 'ctrlEnter' ? ['Ctrl', 'Enter'] : ['Enter'];
    const shortcuts = [
      ['新建会话', '工作台', ['Ctrl', 'N']], ['搜索会话', '工作台', ['Ctrl', 'K']],
      ['展开或收起会话栏', '工作台', ['Ctrl', 'B']], ['打开或关闭设置', '工作台与设置', ['Ctrl', ',']],
      ['发送消息', '输入框', send], ['换行', '输入框', p.sendKey === 'ctrlEnter' ? ['Enter'] : ['Shift', 'Enter']],
      ['查看指令', '输入框开头', ['/']], ['关闭菜单或返回对话', '菜单与设置', ['Esc']],
      ['切换菜单选项', '菜单', ['↑', '↓']], ['选择选项', '菜单', ['Enter']],
    ];
    body = `<div class="pref-shortcut-search" id="pref-shortcutSearch"><input type="search" id="pref-shortcut-query" aria-label="搜索快捷键" placeholder="搜索操作或快捷键"></div><section class="pref-shortcut-list" aria-label="快捷键列表">${shortcuts.map(([name, where, keys]) => `<div class="pref-shortcut-row" data-shortcut="${esc(`${name} ${where} ${keys.join(' ')}`.toLowerCase())}"><span>${name}<small>${where}</small></span><span class="pref-keys">${keys.map((key) => `<kbd>${esc(key)}</kbd>`).join('')}</span></div>`).join('')}<p class="pref-empty" hidden>没有匹配的快捷键。</p></section><p class="hint">在技能编辑器或弹窗中，Esc 会优先保留当前编辑。</p>`;
  }
  main.innerHTML = heading + `<div class="preferences-page" data-preferences-page="${tab}">${body}</div>`;

  const onChange = (event) => {
    const input = event.target.closest('[data-pref]');
    if (!input || !main.contains(input)) return;
    const key = input.dataset.pref;
    const value = input.type === 'checkbox' ? input.checked
      : (input.type==='range'||key === 'uiFontSize' || key === 'contentFontSize') ? Number(input.value) : input.value;
    try { updatePreferences({ [key]: value, ...(key==='themePreset'?{colorSource:'preset'}:key==='seedColor'?{colorSource:'custom'}:{}) }); ctx.setStatus?.('偏好已保存'); }
    catch (error) { ctx.setStatus?.(error.message, true); sync(); }
  };
  const sync = () => {
    if (!main.querySelector(`[data-preferences-page="${tab}"]`)) return;
    const next = getPreferences();
    main.querySelectorAll('[data-pref]').forEach((input) => {
      const value = next[input.dataset.pref];
      if (input.type === 'checkbox') input.checked = value;
      else if (input.type === 'radio') input.checked = input.value === value && (input.dataset.pref!=='themePreset'||next.colorSource==='preset');
      else {
        input.value = String(value);
        // ui-kit wraps select elements; keep its visible label in sync without a change loop.
        const wrapper = input.nextElementSibling;
        const label = wrapper?.classList.contains('dd') ? wrapper.querySelector('.dd-label') : null;
        if (label) label.textContent = input.selectedOptions[0]?.textContent || '';
      }
    });
    main.querySelectorAll('[data-pref-output]').forEach(output=>{output.value=next[output.dataset.prefOutput]+output.dataset.unit;const input=main.querySelector(`[data-pref="${output.dataset.prefOutput}"]`);input?.setAttribute('aria-valuetext',output.value);});
    const custom=main.querySelector('#pref-customFont');if(custom)custom.hidden=next.uiFont!=='custom';
    const materialDetails=main.querySelector('.pref-material-details');if(materialDetails)materialDetails.hidden=next.material!=='glass';
    const colorSource=main.querySelector('[data-color-source]');if(colorSource)colorSource.textContent=next.colorSource==='wallpaper'?'当前配色来自壁纸':next.colorSource==='custom'?'当前使用自选颜色':'选择一种强调色';
    const clear=main.querySelector('[data-wallpaper-clear]');if(clear)clear.disabled=!getWallpaper();
  };
  const onRange = event => {if(event.target.matches('input[type="range"][data-pref], input[type="color"][data-pref]'))onChange(event);};
  main.addEventListener('change', onChange);
  main.addEventListener('input', onRange);
  window.addEventListener('wm-preferences-changed', sync);
  if(tab==='appearance') {
    const input=main.querySelector('[data-wallpaper-file]'),status=main.querySelector('[data-wallpaper-state]');
    main.querySelector('[data-wallpaper-choose]').addEventListener('click',()=>input.click());
    main.querySelector('[data-wallpaper-clear]').addEventListener('click',()=>{try{clearWallpaper();status.textContent='壁纸已移除';ctx.setStatus?.('偏好已保存');}catch(error){status.textContent=error.message;}});
    input.addEventListener('change',async()=>{const file=input.files?.[0];if(!file)return;input.value='';const choose=main.querySelector('[data-wallpaper-choose]');choose.disabled=true;status.textContent='正在处理图片…';try{await setWallpaperFile(file);status.textContent='壁纸与主题色已更新';ctx.setStatus?.('偏好已保存');}catch(error){status.textContent=error.message;}finally{choose.disabled=false;}});
    sync();
  }
  if (tab === 'shortcuts') main.querySelector('#pref-shortcut-query').addEventListener('input', (event) => {
    const query = event.target.value.trim().toLowerCase();
    const rows = [...main.querySelectorAll('[data-shortcut]')];
    rows.forEach((item) => { item.hidden = !item.dataset.shortcut.includes(query); });
    main.querySelector('.pref-empty').hidden = rows.some((item) => !item.hidden);
  });
  unbind = () => { main.removeEventListener('change', onChange); main.removeEventListener('input', onRange); window.removeEventListener('wm-preferences-changed', sync); };
}
