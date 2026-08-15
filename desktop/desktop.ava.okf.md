---
type: doc
title: Desktop Shell (Electron)
description: "Thin Electron wrapper around the Ava web console — tray/Dock-resident window loading the gate (:3000 → app :3001 → gateway :8000), every link opened in the machine's browser, auto-login with the local cluster secret. Explicitly non-core: web is the product body, the desktop keeps only the shell."
tags:
  - desktop
  - electron
---

# Desktop Shell (Electron)

## What it is

`desktop/` is a **thin Electron wrapper** around the existing Ava web console: the main window *is* the browser UI (gate `:3000` → app `:3001` → gateway `:8000`), with fullscreen / Dock / tray residency and nothing more (user ruling 2026-08-04: desktop = thin wrapper, features as few as possible; web is the universal body — v0.3 dropped v0.2's automatic Page split-view because the split pane duplicated what a browser tab already does). It carries no UI logic of its own and is **not a core subsystem** — CI treats `desktop/` as frontend (FRONTEND path filter). App identity: package `ava-desktop`, product name "Ava", `appId com.ava.desktop`, Electron ^43 + electron-builder; local builds are ad-hoc unsigned DMGs (`npm run dist` → `dist/Ava-0.3.0.dmg`) for self-use — distribution would need Developer ID signing + notarization.

## Core responsibilities

- **Console window**: `BrowserWindow` 1280×840 (min 960×640) loads `entryUrl` (default `http://localhost:3000` — the always-up gate, which shows the login page when unauthenticated and proxies the app on `:3001` when authenticated). Hardened webPreferences: `contextIsolation: true`, `nodeIntegration: false`, `sandbox: true`.
- **Tray/Dock residency**: closing the window hides to tray instead of quitting; the real exit is the tray menu "Quit" or Cmd+Q. The tray menu also carries "Launch at login" (macOS login item, `openAsHidden`). Single-instance lock: a second launch focuses the existing window; `window-all-closed` is intentionally ignored.
- **Every link opens in the browser**: `window.open` / `target=_blank` are always denied and routed through a three-level fallback — (1) new tab in the local ava-browser via the browser-mcp unix socket (`~/.ava/run/chrome-mcp.<cdp_port>.sock`, `call_tool new_page` line protocol, 4s timeout), (2) `shell.openExternal` (system default browser). In-page navigation is restricted to the local gate/gateway host (localhost/127.0.0.1 + the `entryUrl`/`gatewayUrl` hostnames); anything else is intercepted and opened externally. No cross-machine forwarding.
- **Auto-login**: at startup, when a local `~/.ava/.env` (or `$AVA_HOME`) carries `AVA_CLUSTER_SECRET`, check `GET {gateway}/api/auth/check`; if unauthenticated, `POST /api/auth/login` with the secret as password, extract the `ava_session` cookie (7-day TTL) and inject it into Electron's session with an explicit `expirationDate` so it persists to disk. Failures are always silent; a frontend-only machine (no `.env`) just shows the login page. Disabled by `settings.json` `"autoLogin": false` or `--no-auto-login`.
- **Rollout resilience**: `did-fail-load` retries the entry URL up to 10 times at 3s intervals (the gate is briefly unavailable during a cluster update), then logs loudly instead of leaving a dead window.
- **Config**: `settings.json` in the Electron userData dir (`~/Library/Application Support/ava-desktop`) overrides the defaults — `entryUrl`, `gatewayUrl`, `autoLogin`.

## Key dependencies

- [[../ui/web/web.ava.okf.md|Frontend]] — the web console the window renders (the product body; desktop carries no UI of its own)
- [[services/gateway_side/gateway_side.ava.okf.md|Gateway-side services]] — the gate (`services/gate/daemon.py`) owns `:3000` (the desktop's `entryUrl`) and serves the login page when unauthenticated
- [[../gateway/gateway.ava.okf.md|Gateway]] — auto-login talks to `/api/auth/login` + `/api/auth/check`; the `ava_session` cookie is host-only and shared across ports, which is why entry and gateway must share a hostname
- [[services/agent_runner_side/browser/browser.ava.okf.md|Browser]] — the browser-mcp unix socket the external-link fallback opens tabs through (present only on a machine with a local cluster)

## Entry points

- `desktop/electron/main.js` — app lifecycle: main window, tray, single-instance lock, load-failure retry
- `desktop/electron/config.js` — settings.json loading (userData overrides DEFAULTS)
- `desktop/electron/external-links.js` — external-link guard + ava-browser / system-browser fallback
- `desktop/electron/auto-login.js` — cluster-secret auto-login (secret read, auth check, cookie injection)
- `desktop/package.json` — `npm start` (run) / `npm run dist` (electron-builder DMG)

## Notes

- **Known limitations**: no auto-update; resident memory ~100–300MB (Chromium); with ad-hoc signing on macOS 15 session cookies do not persist across app restarts (issue #702) — restarting requires re-login, tray residency is unaffected.
- **Deployment pitfall**: on a machine whose cluster is not on localhost (e.g. reached over Tailscale), `entryUrl`/`gatewayUrl` must point at the cluster's reachable host — the session cookie is host-only, so an entry page and gateway on different hosts leave the cookie on the wrong host and the login loops (observed with a localhost entry against a Tailscale-IP gateway).
