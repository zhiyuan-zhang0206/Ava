---
type: doc
title: Process Supervision
description: Native sessions, agent shells held by the pty-sessions service, start-serving readiness gating, daemon health/liveness, and OS-scheduler-owned watchdog/log maintenance.
tags:
- base
- library
- process-supervision
---

# Process Supervision

`base/sessions/posixproc.py`, `base/sessions/backend.py`,
`base/daemon/shutdown.py`, `base/daemon/health.py`,
`base/deploy/lifecycle/start_serving.py`: services, orchestration sessions and agent
processes are **native** sessions; agent shells are held by the machine's **pty-sessions
service** ([[base/sessions/pty/docs/pty_sessions/pty_sessions.ava.okf.md]]). Start-serving
gates recovery until readiness passes. Daemon health
accepts either one `Liveness` heartbeat or a worst-case `LivenessGroup` whose
per-loop progress snapshots make concurrent-loop failures attributable. OS
schedulers own health/watchdog probes, boot autostart, and daily
rotate-then-retain log maintenance through `base/os_*.py`. Launch shape, the
kill contract and the SIGTERM unwind:
[[base/sessions/docs/session-backend.ava.okf.md|session backend]].
External coding tools add
[[coding-session-owner.ava.okf.md|canonical generation ownership]].

`posixproc.process_group_has_live_members(pgid)` exposes the session supervisor's
momentary numeric-group liveness observation. Zombie-only groups have no live
members. It does not establish birth identity, signal authority or closure:
group IDs can be reused, members can change during the scan, and inaccessible
members can prevent a complete observation. Native execution domains keep their
stronger retained-leader closure contract.
