# Ava workspace (thin Electron wrapper)

Wraps the existing Ava web console (gate :3000 -> app :3001, gateway :8000)
as a local desktop app. **Thin wrapper**: the main window *is* the browser
console; every link click opens in the browser.

> v0.3 dropped v0.2's automatic Page split-view (WebContentsView right panel +
> SSE tracking): the split pane duplicated what a browser tab already does.
> The desktop build keeps fullscreen + Dock + tray residency.

## Layout

```
desktop/
  electron/
    main.js                # entry: main window / tray / lifecycle
    external-links.js      # external links: window.open always falls back to
                           # the browser; navigation is limited to gate/gateway
    config.js              # settings.json (userData) overrides defaults
  assets/                  # app icon + tray template icon
```

## Run

```bash
cd desktop
npm install        # first run: downloads the Electron binary (~100MB)
npm start          # start (loads the gate entry; shows the login page when signed out)
```

- First login: type the cluster password in the window (same as browser login;
  the session cookie persists for 7 days).
- Closing the main window minimizes to the tray; the tray menu can quit.

## Link behavior

- **window.open / target=_blank (including Inspector page links) always opens
  in the machine's browser** (three-level fallback: the local ava-browser new
  tab -> the system default browser).
- Same-window navigation is restricted to the local gate/gateway host
  (default localhost).
- No cross-machine forwarding; external links are handled locally.

## Auto-login

When a local `~/.ava/.env` exists (agent-runner machine, carries
`AVA_CLUSTER_SECRET`), startup auto-logs-in with the cluster secret so no
password is needed; a frontend-only machine (no `.env`) shows the login page.
`settings.json` `"autoLogin": false` or `--no-auto-login` disables it.

## Config (userData/settings.json)

```json
{
  "entryUrl": "http://localhost:3000",
  "gatewayUrl": "http://localhost:8000"
}
```

## Build a dmg

```bash
cd desktop
npm run dist       # artifact at desktop/dist/Ava-0.3.0.dmg (ad-hoc unsigned, local use)
```

## Known limitations

- Auto-update not implemented; resident memory ~100-300MB (Chromium).
- ad-hoc signing + macOS 15: session cookies do not persist across app
  restarts (issue #702 observed): restarting the app requires re-login; tray
  residency is unaffected.
