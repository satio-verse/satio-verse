// 工作台逐页截图 (2026-11-01 第六轮): 对着 memory-ui 测试服务端 (假模型、临时工作台目录), 假装桌宠宿主 v3, 把左栏每个入口、
// 右侧面板每个工具页、输入框的菜单、来源标签、标签栏溢出、新聊天、深色都截一张; 改版前后各跑一次, 同名图并排比.
// 设置页的 /pet/panel.html 测试服务端不给 (正式环境是宿主从磁盘给), 这里照宿主那样从 desktop/web/ 喂.
// 用法: node tools/workbench_page_shots.cjs <输出目录> [http://127.0.0.1:8798]   (playwright-core / chromium 同 test_workbench_ui.cjs)
const path = require('node:path'), fs = require('node:fs'), { execFileSync } = require('node:child_process');
const { chromium } = require(process.env.PLAYWRIGHT_CORE || 'G:/RunProgram/yourParalegal/apps/web/node_modules/playwright-core');
const EXE = process.env.CHROMIUM_EXE || 'C:/Users/ASUS/AppData/Local/ms-playwright/chromium-1228/chrome-win64/chrome.exe';
const out = path.resolve(process.argv[2]), origin = process.argv[3] || 'http://127.0.0.1:8798';
const port = new URL(origin).port;
const REPO = process.env.WM_REPO || path.resolve(__dirname, '..'), WEB = path.join(REPO, 'desktop', 'web');  // 草稿目录里的脚本用 WM_REPO 指回项目根
const TYPES = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.json': 'application/json' };
fs.mkdirSync(out, { recursive: true });
const log = [];
const note = (name, ok, detail = '') => { log.push({ name, ok, detail }); console.log(ok ? 'SHOT' : 'MISS', name, detail); };

async function newPage(browser, scheme, hostVersion) {
  const context = await browser.newContext({ viewport: { width: 1600, height: 960 }, colorScheme: scheme });
  // 服务端不给的 /pet/ 文件 (设置页 panel.html 和它带的脚本) 照宿主那样从磁盘读
  await context.route('**/pet/**', async route => {
    const res = await route.fetch().catch(() => null);
    if (res && res.status() !== 404) return route.fulfill({ response: res });
    const file = path.join(WEB, decodeURIComponent(new URL(route.request().url()).pathname.replace(/^\/pet\//, '')));
    if (!file.startsWith(WEB) || !fs.existsSync(file)) return route.fulfill({ status: 404, body: 'missing' });
    route.fulfill({ status: 200, contentType: TYPES[path.extname(file)] || 'application/octet-stream', body: fs.readFileSync(file) });
  });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/api/webview?**', route => route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: '<pre>测试</pre>' }));
  if (hostVersion) {
    await page.addInitScript(version => {
      const listeners = [];
      window.__sent = [];
      window.__host = { reply: data => listeners.forEach(fn => fn({ data })) };
      window.chrome = { webview: {
        postMessage: text => {
          window.__sent.push(text);
          // 设置页 (panel.html?embed=1) 一开就向宿主要设置: 回一份最小的, "常规 / 外观 / 通知 / 快捷键"这几页只用本地偏好, 画得出来
          if (text.startsWith('panel ')) { const m = JSON.parse(text.slice(6)); if (m.op === 'get') setTimeout(() => window.__host.reply({ t: 'panel', id: m.id, settings: { active: 'default', personas: { default: { name: '默认' } } }, skills: [], faces: [] }), 30); }
          if (text.startsWith('workbench ') && JSON.parse(text.slice(10)).op === 'ready') {
            setTimeout(() => window.__host.reply({ t: 'workbench-capabilities', version, native_browser: true, browser_tabs: true, browser_zoom: true, browser_selection: true, folder_picker: true }), 30);
          }
        },
        addEventListener: (type, fn) => { if (type === 'message') listeners.push(fn); },
      } };
    }, hostVersion);
  }
  return { context, page, errors };
}

async function shot(page, name, prep) {
  try {
    if (prep) await prep();
    await page.waitForTimeout(700);
    await page.screenshot({ path: path.join(out, name + '.png') });
    note(name, true);
  } catch (e) { note(name, false, String(e.message).split('\n')[0]); }
  try { await page.keyboard.press('Escape'); await page.waitForTimeout(150); } catch { /* 下一张 */ }
}

(async () => {
  const seed = JSON.parse(execFileSync('python', [path.join(__dirname, 'workbench_ui_seed.py'), port, path.join(out, 'seed')], { encoding: 'utf8', env: { ...process.env, PYTHONIOENCODING: 'utf-8' } }).trim().split('\n').pop());
  const browser = await chromium.launch({ executablePath: EXE, headless: true });
  try {
    for (const scheme of ['light', 'dark']) {
      const { context, page, errors } = await newPage(browser, scheme, 3);
      await page.goto(`${origin}/agent?desk=0&s=${seed.sid}`);
      await page.locator('.wb-edits').waitFor();
      await page.waitForTimeout(800);
      const s = scheme === 'light' ? '' : '-dark';
      const panelOpen = async () => { if (!await page.locator('#panel').isVisible()) await page.locator('#panelToggle').click(); };
      const home = async () => { await panelOpen(); await page.keyboard.press('Control+t'); await page.locator('.wb-home').waitFor(); };
      const tool = (name) => async () => {
        await home();
        if (!['agents', 'automation', 'mail', 'terminal', 'files'].includes(name)) { await page.locator('.wb-home-more summary').click(); }
        await page.locator(`.wb-home [data-home="${name}"]`).click();
        await page.waitForTimeout(1200);
      };
      // 关掉面板看对话本身
      if (await page.locator('#panel').isVisible()) await page.locator('#panelToggle').click();
      await shot(page, `01-chat${s}`);
      await page.locator('#chat').evaluate(el => { el.scrollTop = 0; });
      await shot(page, `01b-chat-top${s}`);
      await page.locator('#chat').evaluate(el => { el.scrollTop = el.scrollHeight; });
      await shot(page, `02-her-card${s}`, async () => { await page.locator('#herRow').click(); });
      if (await page.locator('#her').isVisible().catch(() => false)) await page.locator('#herRow').click().catch(() => {});
      await shot(page, `03-side-search${s}`, async () => { await page.locator('#sideSearchBtn').click(); await page.keyboard.type('app'); });
      await page.locator('#sideSearchBtn').click().catch(() => {});
      await shot(page, `04-newtab${s}`, home);
      if (scheme === 'light') {
        await shot(page, '05-capabilities', async () => { await page.locator('#pluginsOpen').click(); await page.waitForTimeout(1500); });
        await shot(page, '06-automation', async () => { await page.locator('#automationOpen').click(); await page.waitForTimeout(1500); });
        await shot(page, '07-agents', tool('agents'));
        await shot(page, '08-presets', tool('presets'));
        await shot(page, '09-life', tool('life'));
        await shot(page, '10-context', tool('context'));
        await shot(page, '11-extensions', tool('extensions'));
        await shot(page, '12-community', tool('community'));
        await shot(page, '13-data', tool('data'));
        await shot(page, '14-updates', tool('updates'));
        await shot(page, '16-mail', async () => { await page.locator('#mailOpen').click(); await page.waitForTimeout(1500); });
        await shot(page, '17-terminal', async () => { await panelOpen(); await page.keyboard.press('Control+Backquote'); await page.waitForTimeout(1500); });
        await shot(page, '18-palette', async () => { await page.keyboard.press('Control+p'); await page.waitForTimeout(400); });
        await shot(page, '19-file', async () => { await page.keyboard.press('Control+p'); await page.keyboard.type('app.py'); await page.locator('.wb-palette [role="option"][data-kind="file"]', { hasText: 'app.py' }).first().waitFor(); await page.keyboard.press('Enter'); await page.waitForTimeout(800); });
        await page.keyboard.press('Escape').catch(() => {});
        await shot(page, '20-diff', async () => { await page.locator('.wb-edits').last().locator('[data-view-changes]').click(); await page.waitForTimeout(800); });
        await shot(page, '21-plus-menu', async () => { await page.locator('#contextAdd').click(); });
        await shot(page, '22-mode-menu', async () => { await page.locator('#modeBtn').click(); });
        await shot(page, '23-model-menu', async () => { await page.locator('#modelBtn').click(); });
        await shot(page, '24-ws-menu', async () => { await page.locator('#tree .ws-row').first().click({ button: 'right' }); });
        await shot(page, '25-side-view', async () => { await page.locator('#sideViewBtn').click(); });
        await shot(page, '26-archived', async () => { await page.locator('#archivedSessions').click(); await page.waitForTimeout(800); });
        await page.keyboard.press('Escape').catch(() => {});
        await shot(page, '27-studio', async () => { await page.locator('#studioOpen').click(); await page.waitForTimeout(1500); });
        await page.keyboard.press('Escape').catch(() => {});

        // ---- 第六轮新加的几样 ----
        await page.goto(`${origin}/agent?desk=0&s=${seed.sid}`);
        await page.locator('.wb-edits').waitFor();
        await shot(page, '30-sources', async () => { await page.locator('.wb-src-btn').first().click(); await page.locator('.wb-sources-view').waitFor(); });
        await shot(page, '31-tooltip', async () => { await page.locator('#panelToggle').hover(); await page.waitForTimeout(700); });
        await shot(page, '32-mode-menu', async () => { await page.locator('#modeBtn').click(); await page.locator('#menu [data-val="read"]').hover(); await page.waitForTimeout(200); });
        await shot(page, '33-chip', async () => {
          await page.keyboard.press('Control+p'); await page.keyboard.type('app.py'); await page.locator('.wb-palette [role="option"][data-kind="file"]', { hasText: 'app.py' }).first().waitFor(); await page.keyboard.press('Enter');
          await page.locator('.wb-src .ln').first().waitFor();
          await page.locator('.wb-file-content').evaluate(el => { const lines = el.querySelectorAll('.ln .lc'), range = document.createRange(); range.setStart(lines[0], 0); range.setEnd(lines[1], lines[1].childNodes.length); getSelection().removeAllRanges(); getSelection().addRange(range); document.dispatchEvent(new Event('selectionchange')); });
          await page.locator('[data-f="quote"]').click();
          await page.waitForFunction(() => document.querySelector('#contextChips')?.textContent.includes('第 1–2 行'));
        });
        await shot(page, '36-file-md', async () => { await page.keyboard.press('Control+p'); await page.keyboard.type('README'); await page.locator('.wb-palette [role="option"][data-kind="file"]', { hasText: 'README.md' }).first().waitFor(); await page.keyboard.press('Enter'); await page.locator('.wb-md').waitFor(); });
        await shot(page, '34-tabs-overflow', async () => { for (const name of ['agents', 'automation', 'capabilities', 'context', 'presets', 'life', 'extensions', 'data', 'updates', 'community']) { await page.evaluate(n => dispatchEvent(new CustomEvent('wm-panel-tool', { detail: { name: n } })), name); await page.waitForTimeout(150); } });
        await shot(page, '35-tab-menu', async () => { await page.locator('.wb-ptab-more').click(); });
        await page.goto(`${origin}/agent?desk=0&s=${seed.sid}`);
        await page.locator('.wb-edits').waitFor();
        await shot(page, '28-settings', async () => { await page.locator('#dockKeys [data-act="settings"]').click(); await page.waitForTimeout(2500); });
        await page.goto(`${origin}/agent?desk=0&s=${seed.sid}`);
        await page.locator('.wb-edits').waitFor();
      }
      // 第七轮: 分工那个会话 (一个子任务等你点头、一个做完) 的协作任务页、工作经验页
      await page.goto(`${origin}/agent?desk=0&s=${seed.teamsid}`);
      await page.locator('.message-text').first().waitFor();
      await shot(page, `37-team${s}`, async () => { await page.evaluate(() => dispatchEvent(new CustomEvent('wm-panel-tool', { detail: { name: 'agents' } }))); await page.locator('.cv-team .cv-member').first().waitFor(); });
      await shot(page, `38-experiences${s}`, async () => { await page.evaluate(() => dispatchEvent(new CustomEvent('wm-panel-tool', { detail: { name: 'experiences' } }))); await page.locator('.wb-pview[data-view="tool"] .cv-head + .cv-stats').waitFor(); });
      // 资料本: 详情页、出处卡片 (两边都有; 阅读面板那张只有新版有, 旧版会记一条 MISS)
      await page.goto(`${origin}/agent?desk=0&s=${seed.nbsid}`);
      await page.locator('.message-text .wb-nb-cite').first().waitFor();
      await shot(page, `40-notebook-list${s}`, async () => { await page.evaluate(() => dispatchEvent(new CustomEvent('wm-panel-tool', { detail: { name: 'notebooks' } }))); await page.locator('.nb-books, .nb-sources').first().waitFor(); });
      await shot(page, `40-notebook${s}`, async () => {
        if (await page.locator('.nb-books').count()) await page.locator('.nb-books [data-nb-open]').first().click();
        await page.locator('.nb-sources').waitFor();
        await page.locator('.nb-sources > .nb-source').first().locator('summary').click();
      });
      if (await page.locator('#panel').isVisible()) await page.locator('#panelToggle').click();
      await shot(page, `41-notebook-cite${s}`, async () => { await page.locator('.message-text .wb-nb-cite').first().click(); await page.locator('.nb-pop .nb-block.this').waitFor(); });
      await page.goto(`${origin}/agent?desk=0&s=${seed.readsid || seed.nbsid}`);
      await page.waitForTimeout(600);
      await page.route('**/api/agent/notebooks?*file=1*', route => route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: '<p>PDF 原件</p>' }));
      await shot(page, `42-reading${s}`, async () => {
        await page.evaluate(() => dispatchEvent(new CustomEvent('wm-panel-tool', { detail: { name: 'notebooks' } })));
        await page.locator(`[data-nb-open="${seed.readnb}"]`).click({ timeout: 4000 });
        await page.locator('.nb-sources > .nb-source').first().locator('summary').click();
        await page.locator('[data-nb-read]').first().click({ timeout: 4000 });
        await page.locator('.wb-reading .rd-block').first().waitFor({ timeout: 4000 });
      });
      if (await page.locator('#panel').isVisible()) await page.locator('#panelToggle').click();
      await shot(page, `29-empty${s}`, async () => { await page.locator('#newSession').click(); await page.waitForTimeout(800); });
      note(`page errors ${scheme}`, errors.length === 0, errors.slice(0, 3).join(' | '));
      await context.close();
    }
    // 停掉那一轮分工 (主会话一直在等那个子任务点头), 不占着并行名额; 下次的测试数据开头也会清一遍
    const page = await (await browser.newContext()).newPage();
    await page.goto(`${origin}/agent?desk=0`);
    const waiting = (seed.children || []).find(c => c.status === 'waiting');
    await page.evaluate(async ({ sid, child }) => {
      const ws = new WebSocket(`${location.origin.replace(/^http/, 'ws')}/agent/ws?id=${sid}`);
      await new Promise(done => { ws.onopen = done; ws.onerror = done; });
      ws.send(JSON.stringify({ type: 'stop' }));
      await new Promise(done => setTimeout(done, 300));
      ws.close();
      if (child) await fetch('/api/agent/runtime', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action: 'cancel', id: sid, child_id: child }) });
    }, { sid: seed.teamsid, child: waiting?.id || '' });
  } finally { await browser.close(); }
  fs.writeFileSync(path.join(out, 'shots.json'), JSON.stringify({ origin, seed: seed.sid, log }, null, 1));
})().catch(e => { console.error(e); process.exit(1); });
