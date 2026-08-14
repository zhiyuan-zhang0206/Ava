'use strict';

const fs = require('fs');
const path = require('path');

/** Defaults for the shell. Overridable via settings.json in the userData dir. */
const DEFAULTS = Object.freeze({
  // Gate entry — login page when unauthenticated, proxied app when authenticated.
  entryUrl: 'http://localhost:3000',
  // Gateway API + SSE base (cookie is host-only, shared across ports).
  gatewayUrl: 'http://localhost:8000',
  // Auto-login with the local cluster secret (~/.ava/.env) when present.
  autoLogin: true,
});

/** Load settings.json (userData dir) over DEFAULTS. Call after app ready. */
function loadSettings() {
  const { app } = require('electron');
  let overrides = {};
  const file = path.join(app.getPath('userData'), 'settings.json');
  try {
    overrides = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch {
    overrides = {};
  }
  return { ...DEFAULTS, ...overrides };
}

module.exports = { DEFAULTS, loadSettings };
