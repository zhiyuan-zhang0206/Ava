---
type: doc
title: Frontend
description: Overview index of the Ava frontend subsystem—Next.js 16 Web UI (fleet supervision, agent conversation management, task tracking, cluster config).
---

# Frontend

## What it is

Ava frontend subsystem—Next.js 16 web interface for fleet supervision, agent conversation management, task tracking, cluster configuration. All source code is in `frontend/src/`.

**Role assignment**: gateway side (pure agent-runner does not run it)—Next.js server is a `frontend` session from `build_services` (not in `_AGENT_RUNNER_ONLY_SESSIONS`), roster derived by `ops/spec.py:services_for_capabilities` by capability (re-exported by `cli/commands/_repo.py`).

## Desktop shell (explicitly non-core)

`desktop/` is a **thin Electron wrapper** — fullscreen / Dock / tray persistence plus a web-console window, nothing more (user ruling 2026-08-04: desktop = thin wrapper, features as few as possible; web is the universal body). It renders this same frontend, carries no UI logic of its own, and is therefore **not a core subsystem** — CI treats `desktop/` as frontend (FRONTEND path filter). Node: [[../desktop/desktop.ava.okf.md]].

## Sub-concepts

- [[frontend/src/src.ava.okf.md|Src]]
