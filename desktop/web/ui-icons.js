// Small, local line icons. Names and paths are application constants, never model output.
const paths = {
  sliders: '<path d="M4 7h16M4 17h16"/><circle cx="9" cy="7" r="2.5"/><circle cx="15" cy="17" r="2.5"/>',
  palette: '<path d="M12 3a9 9 0 1 0 0 18h1a2 2 0 0 0 0-4h-1a2 2 0 0 1 0-4h5a4 4 0 0 0 4-4c0-3.5-4-6-9-6Z"/><path d="M7 9h.01M10 6h.01M15 7h.01M6 14h.01"/>',
  bell: '<path d="M5 17h14l-2-3V9a5 5 0 0 0-10 0v5ZM10 21h4"/>',
  keyboard: '<rect x="3" y="6" width="18" height="12" rx="3"/><path d="M7 10h.01M12 10h.01M17 10h.01M7 14h10"/>',
  heart: '<path d="M12 20S3 14.7 3 8.8A4.8 4.8 0 0 1 12 6a4.8 4.8 0 0 1 9 2.8C21 14.7 12 20 12 20Z"/>',
  person: '<circle cx="12" cy="8" r="3.5"/><path d="M4.5 21v-1a7.5 7.5 0 0 1 15 0v1"/>',
  chip: '<rect x="6" y="6" width="12" height="12" rx="3"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/><path d="M10 10h4v4h-4z"/>',
  cloud: '<path d="M7 18a5 5 0 0 1-.7-10A6 6 0 0 1 18 9a4.5 4.5 0 0 1 0 9Z"/>',
  store: '<path d="m4 9 2-5h12l2 5M5 12v8h14v-8M9 20v-6h6v6M4 9a2 2 0 0 0 4 0 2 2 0 0 0 4 0 2 2 0 0 0 4 0 2 2 0 0 0 4 0"/>',
  blocks: '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><path d="M17.5 14v7M14 17.5h7"/>',
  box: '<path d="m12 3 9 5-9 5-9-5ZM3 8v9l9 5 9-5V8M12 13v9M7.5 5.5l9 5"/>',
  plug: '<path d="M8 3v5M16 3v5M6 8h12v3a6 6 0 0 1-12 0ZM12 17v5"/>',
  star: '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2-5.6-3-5.6 3 1.1-6.2L3 9.6l6.2-.9Z"/>',
  chat: '<path d="M5 4h14a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H9l-6 4V6a2 2 0 0 1 2-2Z"/><path d="M7 9h10M7 13h6"/>',
  wallet: '<rect x="3" y="5" width="18" height="15" rx="3"/><path d="M3 9h18M16 14h2"/>',
  server: '<rect x="4" y="3" width="16" height="7" rx="2"/><rect x="4" y="14" width="16" height="7" rx="2"/><path d="M8 6.5h.01M8 17.5h.01M12 6.5h4M12 17.5h4"/>',
  download: '<path d="M12 3v12m-5-5 5 5 5-5M4 16v4h16v-4"/>',
  folder: '<path d="M3 7V5h7l2 2h9v13H3ZM3 7h8l2 3h8"/>',
  pulse: '<path d="M2 12h5l3-8 4 16 3-8h5"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  mic: '<rect x="9" y="2" width="6" height="13" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"/>',
  eye: '<path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>',
};
export function uiIcon(name, className = '') {
  return `<svg class="ui-icon ${className}" viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${paths[name] || paths.blocks}</svg>`;
}
