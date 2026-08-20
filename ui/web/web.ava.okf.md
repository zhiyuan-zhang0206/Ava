---
type: doc
title: Frontend
description: Overview index of the Ava frontend subsystem—Next.js 16 Web UI (fleet supervision, agent conversation management, task tracking, cluster config).
---

# Frontend

## What it is

Ava frontend subsystem—Next.js 16 web interface for fleet supervision, agent conversation management, task tracking, cluster configuration. All source code is in `ui/web/src/`.

**Role assignment**: gateway side (pure agent-runner does not run it)—Next.js server is a `frontend` session from `build_services` (not in `_AGENT_RUNNER_ONLY_SESSIONS`), roster derived by `ops/spec.py:services_for_capabilities` by capability (re-exported by `cli/commands/_repo.py`).

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
its test. Design: [[future/frontend-plugin-contributions.md]].

## Sub-concepts

- [[ui/web/src/src.ava.okf.md|Src]]
