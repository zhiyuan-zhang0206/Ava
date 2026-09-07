---
type: doc
title: Watcher Registry
description: The `agent_watchers` table — the "should it exist?" half of the R1 watcher frame. Every ava.watcher session registers a row; the agent boot reconcile rebuilds cron watchers a stop/crash/reboot reaped and marks missed one-shots.
tags:
- watcher
- r1
---

# Watcher Registry

## What it is

`shared/watcher_registry.py` is the pure-DB API for the **`agent_watchers` table** — the registry half of the registry × lease frame ([[okf/design/r1-state-liveness/r1-state-liveness.ava.okf.md|R1 design]]) applied to watchers. `ava.watcher.at/cron/launch` writes one row per spawned watcher session; the row outlives the session exactly when the session was **killed** rather than ended, so a surviving row with a missing session means "this watcher should exist and does not". The agent's boot reconcile (`ava.watcher.reconcile`) reads that and rebuilds cron watchers / marks missed one-shots — the fix for issue #1014 (4th recurrence: rollouts of that era and `ava stop` reaped every watcher session, and nothing knew a recurring watcher should exist, so cron schedules silently died; sessions now survive rollouts, and the registry remains the net for `ava stop`, host crashes, and reboots).

**Liveness is the session itself** — a watcher process IS its session — so the registry deliberately holds no lease column; a session gone means the watcher should be gone. One caveat the registry cannot see: a killed pty host leaves the watcher child alive as a ppid=1 orphan that keeps firing — the generated bootstrap's orphan guard (template v4) makes the child exit itself, and the reconcile SIGKILLs any process still running a missing watcher's script before rebuilding (task #1726; see [[ava/watcher.ava.okf.md|ava.watcher SDK]]).

## Core Responsibilities

### The table (`agent_watchers`)

Keyed by `(agent_id, session_id)` — shell-session ids are per-agent counters, not cluster-global. `kind` ∈ {`at`, `cron`, `launch`}; the row carries that kind's **rebuild payload** (the exact arguments the SDK verb needs to re-spawn): `message`, `fires_at` (at), `cron_expr` / `cron_timezone` / `cron_end_at` (cron), `timeout_secs` (launch), plus the PTY allocation `generation` that admitted the exact session. `status` ∈ {`running` (spawn state), `rebuilt` (died and re-spawned — history kept; the rebuild's NEW session has its own `running` row), `missed` (one-shot whose moment passed while its session was gone), `reaped` (a superseded generation retained as terminal history)}.

### Writers

- `register_watcher` — called by `ava/watcher.py:_spawn` at spawn, **before the child can exit** (the child deletes its row on clean exit). Fail-soft: a registry write must never break the watcher it only observes.
- `delete_watcher` — the watcher child's clean-exit finally (a fired one-shot, an ended schedule, or a crashed script that still ran its finally) and `ava.shell.sessions.kill` (a deliberately killed watcher must NOT be rebuilt at the next boot). A missing row is a no-op.
- `mark_status` — `running` → `rebuilt` / `missed` / `reaped` during reconcile.

### The boot reconcile (`ava.watcher.reconcile`, called from `agent/_process_boot.py`)

For every row whose session is **gone**, reconcile first compares the row's
generation with the host's current allocation generation. A superseded row is
marked `reaped` and never creates another session; a superseded `at` or
`launch` row also alerts its owner that the one-shot was missed. A matching
current row continues through ordinary recovery:

- **cron** — re-spawned from the stored expression (a standing schedule is the whole point of the registry); the old row is marked `rebuilt`. A schedule whose `end_time` has passed is deleted instead — it ended, just not cleanly.
- **at** — re-spawned while its moment is still in the future; once the moment has passed the wake is lost, so the row is marked `missed` and the agent is told (it created the one-shot; it should know it never fired).
- **launch** — one-shot scripts are not re-run at boot (their work is time-bound and probably stale): marked `missed` + alerted.

Fail-soft throughout: a registry read / session list / spawn failure is logged and skipped, never allowed to block the boot it runs in.

## Key Dependencies

- [[ava/watcher.ava.okf.md|ava.watcher SDK]] — the spawn / reconcile surface
- [[okf/design/r1-state-liveness/r1-state-liveness.ava.okf.md|R1 state & liveness design]] — the registry × lease frame
- [[loop.ava.okf.md]] — the boot reconcile call site
- `ava/shell/sessions.py` — `kill` deletes the row; the watcher child IS a shell session

## Entry Points

- `shared/watcher_registry.py:register_watcher` / `delete_watcher` / `mark_status` / `watcher_rows` / `watcher_session_ids`
- `ava/watcher.py:reconcile` — the rebuild / mark-missed pass
- `agent/_process_boot.py` — boot-time invocation

## Notes

- Pure DB, no SDK imports — the watcher child's bootstrap finally calls `delete_watcher` without pulling the SDK into a short-lived child.
- The generated script files under `$AVA_HOME/watchers/` remain ephemeral; the registry is the persistence, the session is the liveness.
