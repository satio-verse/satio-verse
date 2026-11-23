// Preferences are loaded independently of the 3D runtime: a temporary model
// service failure must not prevent the conversation surface receiving its theme.
import { initPreferences } from '/pet/ui-preferences.js';
document.body.classList.add('wm-pet-surface');
initPreferences();
