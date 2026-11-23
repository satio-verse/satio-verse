// Shared select-only combobox. The hidden native select retains its value and events.
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
const CHEV = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>';
const CHECK = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>';
let openMenu = null, sequence = 0;
const records = new Set();
const disabled = (option) => option.disabled || option.parentElement?.disabled === true;
const visible = (option) => !option.hidden && !option.parentElement?.hidden;

function close() {
  if (!openMenu) return;
  const { menu, btn } = openMenu;
  openMenu = null;
  menu.remove();
  btn.setAttribute('aria-expanded', 'false');
  btn.removeAttribute('aria-controls');
  btn.removeAttribute('aria-activedescendant');
  btn.classList.remove('open');
}

function accessibleName(sel) {
  const explicit = sel.getAttribute('aria-label')?.trim();
  if (explicit) return explicit;
  const references = (sel.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean)
    .map((id) => document.getElementById(id)?.textContent || '').join(' ').trim();
  if (references) return references;
  const labels = [...(sel.labels || [])].map((label) => {
    const copy = label.cloneNode(true);
    // Read label text only, never values or text from nested form controls/options.
    copy.querySelectorAll('select, input, textarea, button, script, style, [aria-hidden="true"]').forEach((node) => node.remove());
    return copy.textContent.trim();
  }).filter(Boolean).join(' ');
  return (labels || sel.title || '选择选项').replace(/\s+/g, ' ').trim().slice(0, 180);
}

function sync(sel) {
  const btn = sel._dd;
  if (!btn) return;
  const label = sel.options[sel.selectedIndex]?.textContent || '—';
  // Avoid a childList mutation on every enhancement pass (which would queue another pass).
  if (btn.querySelector('.dd-label').textContent !== label) btn.querySelector('.dd-label').textContent = label;
  btn.disabled = sel.matches(':disabled');
  btn.hidden = sel.hidden;
  btn.title = sel.title || '';
  btn.setAttribute('aria-label', accessibleName(sel));
  btn.setAttribute('aria-disabled', String(btn.disabled));
  const description = sel.getAttribute('aria-describedby');
  if (description) btn.setAttribute('aria-describedby', description); else btn.removeAttribute('aria-describedby');
  if ((btn.disabled || btn.hidden) && openMenu?.btn === btn) close();
}

function position(menu, btn) {
  const viewport = window.visualViewport;
  const width = viewport?.width || document.documentElement.clientWidth || innerWidth;
  const height = viewport?.height || document.documentElement.clientHeight || innerHeight;
  const x = viewport?.offsetLeft || 0, y = viewport?.offsetTop || 0;
  const margin = Math.min(8, width / 2, height / 2), gap = 6;
  const r = btn.getBoundingClientRect();
  const maxWidth = Math.max(0, width - margin * 2);
  menu.style.minWidth = `${Math.min(Math.max(r.width, 180), maxWidth)}px`;
  menu.style.maxWidth = `${Math.min(Math.max(420, r.width), maxWidth)}px`;
  menu.style.left = `${x + margin}px`;
  menu.style.top = `${y + margin}px`;
  const below = Math.max(0, y + height - margin - r.bottom - gap);
  const above = Math.max(0, r.top - y - margin - gap);
  const useBelow = below >= menu.offsetHeight || below >= above;
  menu.style.maxHeight = `${Math.min(360, useBelow ? below : above)}px`;
  const clamp = (value, min, max) => Math.min(Math.max(value, min), Math.max(min, max));
  menu.style.left = `${clamp(r.left, x + margin, x + width - menu.offsetWidth - margin)}px`;
  menu.style.top = `${clamp(useBelow ? r.bottom + gap : r.top - menu.offsetHeight - gap, y + margin, y + height - menu.offsetHeight - margin)}px`;
}

function open(sel, edge) {
  close();
  const btn = sel._dd;
  if (!sel.isConnected || !btn?.isConnected || btn.disabled || btn.hidden) return;
  const items = [...sel.options].filter(visible);
  const enabled = items.map((option, index) => disabled(option) ? -1 : index).filter((index) => index >= 0);
  const menu = document.createElement('div');
  menu.id = `${btn.id}-listbox`;
  menu.className = 'dd-menu glass';
  menu.setAttribute('role', 'listbox');
  menu.setAttribute('aria-label', accessibleName(sel));
  menu.innerHTML = items.map((option, index) => `<div id="${menu.id}-option-${index}" class="dd-item ${option.selected ? 'sel' : ''} ${disabled(option) ? 'off' : ''}" role="option" data-i="${index}" aria-selected="${option.selected}" aria-disabled="${disabled(option)}"><div class="dd-text"><div class="dd-name">${esc(option.textContent)}</div>${option.dataset.desc ? `<div class="dd-desc">${esc(option.dataset.desc)}</div>` : ''}</div><span class="dd-check">${option.selected ? CHECK : ''}</span></div>`).join('');
  // A popup inside a modal must remain in its top layer, outside the scrollable form.
  (btn.closest('dialog[open]') || document.body).append(menu);
  position(menu, btn);
  let cur = items.findIndex((option) => option.selected && !disabled(option));
  if (cur < 0) cur = enabled[0] ?? -1;
  if (edge === 'Home') cur = enabled[0] ?? -1;
  if (edge === 'End') cur = enabled.at(-1) ?? -1;
  const mark = (scroll = false) => {
    const nodes = [...menu.querySelectorAll('.dd-item')];
    nodes.forEach((node, index) => node.classList.toggle('hot', index === cur));
    const active = nodes[cur];
    if (active) {
      btn.setAttribute('aria-activedescendant', active.id);
      if (scroll) {
        const top = active.offsetTop, bottom = top + active.offsetHeight;
        if (top < menu.scrollTop) menu.scrollTop = top;
        else if (bottom > menu.scrollTop + menu.clientHeight) menu.scrollTop = bottom - menu.clientHeight;
      }
    } else btn.removeAttribute('aria-activedescendant');
  };
  const pick = (index) => {
    const option = items[index];
    if (!option || disabled(option) || !sel.isConnected || !btn.isConnected) return;
    const changed = sel.selectedIndex !== option.index;
    sel.selectedIndex = option.index;
    close();
    sync(sel);
    if (changed) {
      sel.dispatchEvent(new Event('change', { bubbles: true }));
      sel.dispatchEvent(new Event('input', { bubbles: true }));
    }
    // The change handler may have replaced the whole settings page.
    if (btn.isConnected && !btn.disabled) btn.focus({ preventScroll: true });
  };
  menu.addEventListener('pointerdown', (event) => event.preventDefault());
  menu.addEventListener('mousemove', (event) => {
    const node = event.target.closest('.dd-item');
    if (node && !disabled(items[Number(node.dataset.i)])) { cur = Number(node.dataset.i); mark(); }
  });
  menu.addEventListener('click', (event) => { const node = event.target.closest('.dd-item'); if (node) pick(Number(node.dataset.i)); });
  const key = (event) => {
    if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
      event.preventDefault(); event.stopPropagation();
      if (enabled.length) {
        const at = enabled.indexOf(cur);
        cur = event.key === 'Home' ? enabled[0] : event.key === 'End' ? enabled.at(-1)
          : enabled[(at + (event.key === 'ArrowDown' ? 1 : -1) + enabled.length) % enabled.length];
        mark(true);
      }
    } else if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); event.stopPropagation(); pick(cur); }
    else if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close(); }
    else if (event.key === 'Tab') close(); // Keep the browser's normal forward/backward focus order.
  };
  openMenu = { menu, btn, sel, key };
  btn.classList.add('open');
  btn.setAttribute('aria-expanded', 'true');
  btn.setAttribute('aria-controls', menu.id);
  mark(true);
}

function removeDetached() {
  for (const record of records) {
    if (record.sel.isConnected && record.btn.isConnected) continue;
    if (openMenu?.btn === record.btn) close();
    record.observer.disconnect();
    record.sel.removeEventListener('change', record.onChange);
    record.btn.remove();
    if (record.sel._dd === record.btn) delete record.sel._dd;
    records.delete(record);
  }
  if (openMenu && !openMenu.menu.isConnected) close();
}

export function enhanceSelects(root = document) {
  for (const sel of root.querySelectorAll('select:not([data-native])')) {
    if (sel._dd?.isConnected) { sync(sel); continue; }
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.id = `wm-dd-${++sequence}`;
    btn.className = ['dd', ...sel.classList].filter((name) => name !== 'dd-native').join(' ');
    btn.setAttribute('role', 'combobox');
    btn.setAttribute('aria-haspopup', 'listbox');
    btn.setAttribute('aria-expanded', 'false');
    btn.setAttribute('aria-readonly', 'true');
    btn.innerHTML = `<span class="dd-label"></span><span class="dd-chev">${CHEV}</span>`;
    sel._dd = btn;
    sel.classList.add('dd-native');
    sel.after(btn);
    sync(sel);
    btn.addEventListener('click', (event) => { event.stopPropagation(); openMenu?.btn === btn ? close() : open(sel); });
    btn.addEventListener('keydown', (event) => {
      if (openMenu?.btn === btn) { openMenu.key(event); return; }
      if (['ArrowDown', 'ArrowUp', 'Home', 'End', 'Enter', ' '].includes(event.key)) { event.preventDefault(); event.stopPropagation(); open(sel, event.key); }
    });
    const onChange = () => sync(sel);
    sel.addEventListener('change', onChange);
    const observer = new MutationObserver(() => { sync(sel); if (openMenu?.sel === sel) close(); });
    observer.observe(sel, { childList: true, subtree: true, attributes: true, characterData: true });
    records.add({ sel, btn, observer, onChange });
  }
}

export function autoEnhance(root) {
  enhanceSelects(root);
  let pending = false;
  const observer = new MutationObserver(() => {
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => { pending = false; if (root.isConnected) enhanceSelects(root); });
  });
  observer.observe(root, { childList: true, subtree: true });
  return () => observer.disconnect();
}

new MutationObserver((changes) => {
  if (changes.some((change) => change.removedNodes.length)) removeDetached();
}).observe(document.documentElement, { childList: true, subtree: true });
document.addEventListener('pointerdown', (event) => {
  if (openMenu && !openMenu.menu.contains(event.target) && !openMenu.btn.contains(event.target)) close();
}, true);
addEventListener('resize', close);
addEventListener('blur', close);
document.addEventListener('scroll', (event) => {
  if (openMenu && !openMenu.menu.contains(event.target) && (event.target === document || event.target.contains?.(openMenu.btn))) close();
}, true);
