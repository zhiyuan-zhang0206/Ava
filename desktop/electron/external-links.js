'use strict';

const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');
const { shell } = require('electron');

/**
 * External-link handling — the main window's uniform three-level fallback:
 *
 * 1. Cluster Chrome (ava-browser) present locally (browser-mcp unix socket
 *    exists and is connectable) -> open a new tab in ava-browser (browser-mcp
 *    line protocol: {"method":"call_tool","tool":"new_page",
 *    "args":{"url":...}}, see services/browser/mcp_daemon.py)
 * 2. Otherwise -> shell.openExternal (system default browser)
 *
 * No cross-machine forwarding: external links are handled locally.
 */

/** Navigation is restricted to the local gate/gateway host (default localhost). */
function isAllowedNavUrl(url, settings) {
  try {
    const u = new URL(url);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return false;
    const allowed = new Set(['localhost', '127.0.0.1']);
    allowed.add(new URL(settings.entryUrl).hostname);
    allowed.add(new URL(settings.gatewayUrl).hostname);
    return allowed.has(u.hostname);
  } catch {
    return false;
  }
}

/**
 * Find the local browser-mcp unix socket (the shared.paths.chrome_mcp_socket
 * shape: $AVA_HOME/run/chrome-mcp.<cdp_port>.sock). The desktop app is a thin
 * wrapper over the local cluster, so it only looks under ~/.ava/run/; the port
 * varies with the cluster config, hence glob matching.
 */
function findBrowserMcpSocket() {
  const runDir = path.join(os.homedir(), '.ava', 'run');
  let names;
  try {
    names = fs.readdirSync(runDir);
  } catch {
    return null;
  }
  const sock = names.find((n) => /^chrome-mcp\.\d+\.sock$/.test(n));
  return sock ? path.join(runDir, sock) : null;
}

/** Open url in ava-browser; success (daemon replies ok) -> true, else false. */
function openInClusterChrome(url) {
  return new Promise((resolve) => {
    const sockPath = findBrowserMcpSocket();
    if (!sockPath) return resolve(false);
    const socket = net.createConnection(sockPath);
    let settled = false;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      socket.destroy();
      resolve(ok);
    };
    const timer = setTimeout(() => finish(false), 4000);
    socket.on('connect', () => {
      socket.write(
        `${JSON.stringify({ id: 1, method: 'call_tool', tool: 'new_page', args: { url } })}\n`,
      );
    });
    let buf = '';
    socket.on('data', (chunk) => {
      buf += chunk.toString('utf8');
      const nl = buf.indexOf('\n');
      if (nl < 0) return; // line incomplete, keep waiting
      let ok = false;
      try {
        ok = JSON.parse(buf.slice(0, nl)).ok === true;
      } catch {
        ok = false;
      }
      finish(ok);
    });
    socket.on('error', () => finish(false));
    socket.on('close', () => finish(false));
  });
}

/**
 * Three-level external-link fallback entry: new tab in the local ava-browser,
 * falling back to the system browser. Non-blocking (fire-and-forget).
 */
async function openExternalUrl(url, settings) {
  try {
    if (await openInClusterChrome(url)) {
      console.log(`[ava-desktop] external link -> ava-browser new tab: ${url}`);
      return;
    }
  } catch {
    // any exception falls back to the system browser
  }
  console.log(`[ava-desktop] external link -> system browser: ${url}`);
  shell.openExternal(url);
}

/**
 * Attach the external-link guard to a webContents:
 * - window.open / target=_blank (setWindowOpenHandler) -> always use the
 *   browser fallback (the desktop app opens every link in the browser —
 *   including Inspector page links, which are target=_blank new tabs in the
 *   web version; the desktop has no tabs, so the equivalent behavior is the
 *   browser)
 * - in-page navigation (will-navigate) -> only the local gate/gateway host is
 *   allowed; everything else is intercepted + falls back
 */
function guardWebContents(webContents, { settings }) {
  webContents.setWindowOpenHandler(({ url }) => {
    openExternalUrl(url, settings);
    return { action: 'deny' };
  });
  webContents.on('will-navigate', (e, url) => {
    if (!isAllowedNavUrl(url, settings)) {
      e.preventDefault();
      openExternalUrl(url, settings);
    }
  });
}

module.exports = { guardWebContents, isAllowedNavUrl, openExternalUrl };
