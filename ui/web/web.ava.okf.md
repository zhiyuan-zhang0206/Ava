---
type: doc
title: Frontend
description: Overview index of the Ava frontend subsystem—Next.js 16 Web UI (fleet supervision, agent conversation management, task tracking, cluster config).
---

# Frontend

## What it is

Ava frontend subsystem—Next.js 16 web interface for fleet supervision, agent conversation management, task tracking, cluster configuration. All source code is in `ui/web/src/`.

The console exposes exactly three agent statuses: `running`, `idling`, and
`terminated`. The gateway wire deliberately retains its finer control-plane
states (`allocated`, `starting`, and `restarting`); every
frontend ingest path (`/api/agents`, lifecycle SSE snapshots, and the fleet
graph) exhaustively projects those non-executing transitions to `idling` before
they enter a cache. Machine/process reachability remains the separate
`liveness_state` axis, so this projection never hides an offline runner.

**Role assignment**: gateway side (pure agent-runner does not run it)—Next.js server is a `frontend` session from `build_services` (not in `_AGENT_RUNNER_ONLY_SESSIONS`), roster derived by `ops/spec.py:services_for_capabilities` by capability (re-exported by `cli/commands/_repo.py`).

## Browser security boundary

`src/proxy.ts` makes application rendering request-bound and emits one
nonce-based Content Security Policy on both the forwarded request and the
browser response. Next.js uses the forwarded policy to nonce framework scripts;
the browser receives that same per-request policy. `next-themes` receives the
same nonce for its theme bootstrap script. The policy builder at
`src/lib/content-security-policy.ts` permits the frontend origin, the configured
gateway HTTP and WebSocket origins, and no broad protocol sources. Grafana is
served through the configured gateway, and plugin iframe mounts use
`pluginPageSrc` → `${API_BASE}/api/plugin-ui/...`; `frame-src` therefore permits
the frontend and configured gateway origins only. Production permits
`'unsafe-inline'` only through CSP3's `style-src-attr`, because dynamic layout
attributes require it; `script-src` remains nonce-only with no unsafe directive.

`NEXT_PUBLIC_API_BASE` supplies an explicit gateway origin. Without it, converge
supplies `NEXT_PUBLIC_GATEWAY_PORT` and the proxy combines that port with the
browser-facing host. The gate preserves the browser's `Host` and any
`X-Forwarded-Host` / `X-Forwarded-Proto` while proxying to Next.js. A TLS
terminator must overwrite both forwarding headers with the public origin before
the gate; direct/private-network access instead uses the preserved `Host`.
Use lowercase `http` or `https` for `X-Forwarded-Proto`; the frontend normalizes
other casing before deriving the CSP origin.
The gate also relays the CSP and the static browser-security headers on the
response, which is the public browser boundary.

## Native shell (explicitly non-core)

`ui/app/` is a thin Tauri 2 wrapper for macOS, Windows, and Android. It loads
this frontend from the gate and adds only platform behavior (window/tray,
desktop auto-login/updater, Android onboarding/residency/notifications). The web
console remains the universal body and the shell remains non-core. Node:
[[../app/app.ava.okf.md]].

## Plugin contributions

The console renders what the cluster's enabled plugins declare under
`contributions.ui` (`GET /api/ui/contributions`) — never plugin JavaScript.
Today that is theme packs: a named set of values for the `globals.css` `:root`
color tokens, applied as inline custom properties on the root element by
`components/theme-pack-tokens.tsx` and chosen in Display settings
(`display.theme_pack`, stored `<plugin>/<theme>`). A pack has two halves —
`tokens` for light and optional `darkTokens` for dark, picked by the resolved
mode — because an inline custom property outranks both `:root` and `.dark`, so
one flat map would pin its colors across BOTH modes and silently disable the
light/dark toggle for them. Omitting `darkTokens` declares exactly that pinning
on purpose, and the picker marks the pack. Unset tokens keep following the
mode.
Nav entries (`contributions.ui.nav`) place a link on the sidebar, the Control
page's Plugins section, or the fleet toolbar; it opens
`/plugin/<plugin>/<path>`, which frames the plugin's own page from the
gateway's plugin mount in a **sandboxed iframe**
(`components/plugin-page-frame.tsx`). The frame is breakage containment, not a
security boundary — the trust decision is the install scan gate — so what it
buys is that a broken plugin page fails alone and no third-party code enters
this bundle. Icon names map to lucide components the console imports
(`components/plugin-nav-icon.ts`), locked against the validator's vocabulary by
its test. Design: [frontend-plugin-contributions.md](../../future/frontend-plugin-contributions.md).

## Sub-concepts

- [[ui/web/src/src.ava.okf.md|Src]]
