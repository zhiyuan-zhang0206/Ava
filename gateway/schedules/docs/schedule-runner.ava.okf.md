---
type: doc
title: Schedule Runner — the in-session entrypoint
description: The process a schedule session runs — loads the schedule, executes its script under a stall guard, and records terminal state the schedule manager reads.
tags: []
---

# Schedule Runner — the in-session entrypoint

Launched by the `schedule-manager` service ([[schedules.ava.okf.md]]) inside the session `ava-schedule-<id>`.

- **In-session entrypoint**: `.venv/bin/python -m gateway.schedule_runner <id>`
- Loads the schedule's script + command from the DB
- Materializes the script to `$AVA_HOME/schedules/<id>/`
- Binds the `schedule:<id>` actor identity (so `ava.agents.*` invocations are attributed to the schedule)
- `.py` scripts are executed in-process via `runpy`; other commands are run as subprocesses
- **Stall guard**: a deepest non-park frame stable beyond `schedule_stall_timeout_seconds` triggers cleanup. Wrapped sleep/sleep-family waits, subprocess `_wait` and selectors' `select` directly inside `_communicate` park; caller `timeout=` bounds child waits when supplied. Spawn, conversion, stdin flush and other selectors stay guarded. Leaving a park resets the budget.
- **Hard-exit cleanup**: before failure writes, capture descendants and verify birth identity + current ancestry. TERM, 3s grace, KILL survivors (recheck identity), reap up to 5s. Runner/shared PTY group excluded; `setsid()` covered. Already-reparented daemons, pre-signal ancestry/identity mismatches and later births are exempt. Failure writes share a daemon-thread budget, `schedule_stall_exit_record_deadline_seconds` (default 10s); always hard-exit 1. An abandoned NULL run row is closed as `interrupted` by manager reconcile.
- **Exit means terminal**: script exits cleanly with rc=0 → runner writes `status='completed'` before exiting (the resident process finished, manager will not restart); non-zero rc / uncaught exception → traceback written to `schedules.last_error` (crash, handed to manager to restart); SIGTERM/SIGHUP active kill → nothing written, not counted as a crash
