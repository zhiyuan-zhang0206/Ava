// Auto-login: when AVA_CLUSTER_SECRET exists locally (~/.ava/.env, an
// agent-runner machine), log in with the cluster secret so no password is
// needed. A frontend-only machine (no .env) silently falls back to the login
// page.
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

const COOKIE_NAME = 'ava_session';
const SESSION_TTL_S = 7 * 24 * 3600; // matches the server-side token TTL

function envPath() {
  return path.join(process.env.AVA_HOME || path.join(os.homedir(), '.ava'), '.env');
}

/** Read AVA_CLUSTER_SECRET from the local .env; missing file/key -> null
 * (frontend-only machine). */
function loadClusterSecret() {
  try {
    const text = fs.readFileSync(envPath(), 'utf8');
    for (const line of text.split('\n')) {
      const m = line.match(/^\s*AVA_CLUSTER_SECRET=(.*?)\s*$/);
      if (m && m[1]) return m[1];
    }
  } catch {
    // .env missing or unreadable -> manual login
  }
  return null;
}

/** Whether the current session is logged in (GET /api/auth/check). */
async function isAuthenticated(gatewayUrl) {
  try {
    const resp = await fetch(`${gatewayUrl}/api/auth/check`, { redirect: 'manual' });
    if (!resp.ok) return false;
    const data = await resp.json();
    return data && data.authenticated === true;
  } catch {
    return false;
  }
}

/** Extract the ava_session token from Set-Cookie (handles both
 * getSetCookie and get implementations). */
function extractSessionToken(resp) {
  const raw =
    typeof resp.headers.getSetCookie === 'function'
      ? resp.headers.getSetCookie().join(';')
      : resp.headers.get('set-cookie') || '';
  const m = raw.match(new RegExp(`${COOKIE_NAME}=([^;]+)`));
  return m ? m[1] : null;
}

/**
 * Attempt auto-login: read the local secret -> POST /api/auth/login -> inject
 * the session cookie. Returns true = logged in (including already logged in);
 * false = cannot auto-login, show the login page. Failures are always silent
 * (never print the secret, never throw).
 */
async function autoLogin(gatewayUrl) {
  const secret = loadClusterSecret();
  if (!secret) return false;
  const base = String(gatewayUrl).replace(/\/+$/, '');
  if (await isAuthenticated(base)) return true;
  try {
    const resp = await fetch(`${base}/api/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: secret }),
    });
    if (!resp.ok) return false;
    const token = extractSessionToken(resp);
    if (!token) return false;
    const { session } = require('electron');
    // fetch keeps no cookie jar, so inject manually; an explicit
// expirationDate ensures it persists to disk (#706)
    await session.defaultSession.cookies.set({
      url: base,
      name: COOKIE_NAME,
      value: token,
      httpOnly: true,
      sameSite: 'lax',
      path: '/',
      expirationDate: Math.floor(Date.now() / 1000) + SESSION_TTL_S,
    });
    return true;
  } catch {
    return false;
  }
}

module.exports = { autoLogin, loadClusterSecret };
