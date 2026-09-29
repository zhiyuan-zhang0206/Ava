---
type: doc
title: Process Supervision
description: Native sessions, PTY-hosted agent shells, start-serving readiness gating, daemon health/liveness, and OS-scheduler-owned watchdog/log maintenance.
tags:
- shared
- library
- process-supervision
---

# Process Supervision

`shared/sessions/posixproc.py`, `shared/sessions/windows/winproc.py`, `shared/sessions/backend.py`,
`shared/daemon/shutdown.py`, `shared/daemon/health.py`,
`shared/deploy/lifecycle/start_serving.py`: services, orchestration sessions and agent
processes are **native** sessions; agent shells run on per-session **PTY
hosts** ([[shared/sessions/pty/pty_sessions.ava.okf.md]]); Windows uses
winproc. Start-serving gates recovery until readiness passes. Daemon health
accepts either one `Liveness` heartbeat or a worst-case `LivenessGroup` whose
per-loop progress snapshots make concurrent-loop failures attributable. OS
schedulers own health/watchdog probes, boot autostart, and daily
rotate-then-retain log maintenance through `shared/os_*.py`. Launch shape, the
kill contract and the SIGTERM unwind:
[[shared/sessions/session-backend.ava.okf.md|session backend]].
External coding tools add
[[coding-session-owner.ava.okf.md|canonical generation ownership]].
