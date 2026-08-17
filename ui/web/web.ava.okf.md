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

## Sub-concepts

- [[ui/web/src/src.ava.okf.md|Src]]
