---
type: doc
title: Schedule Runner — the in-session entrypoint
description: The process a schedule session runs — loads the schedule, executes its script under a stall guard, and records terminal state the schedule manager reads.
tags: []
---

# Schedule Runner — the in-session entrypoint

Launched by the `schedule-manager` service ([[schedules.ava.okf.md]]) inside the session `ava-schedule-<id>`.

- **In-session entrypoint**: `.venv/bin/python -m services.wake.schedule_manager.runner <id>`
- Loads the schedule's script + command from the DB
- Materializes the script to `$AVA_HOME/schedules/<id>/`
- Binds the `schedule:<id>` actor identity (so `ava.agents.*` invocations are attributed to the schedule)
- `.py` scripts are executed in-process via `runpy`; other commands are run as subprocesses
- **Stall guard**: a deepest non-park frame stable beyond `schedule_stall_timeout_seconds` triggers cleanup. Wrapped sleep/sleep-family waits, subprocess `_wait` and selectors' `select` directly inside `_communicate` park; caller `timeout=` bounds child waits when supplied. Spawn, conversion, stdin flush and other selectors stay guarded. Leaving a park resets the budget.
- **Hard-exit cleanup**: before failure writes, capture descendants and verify birth identity + current ancestry. TERM, 3s grace, KILL survivors (recheck identity), reap up to 5s. Runner/shared PTY group excluded; `setsid()` covered. Already-reparented daemons, pre-signal ancestry/identity mismatches and later births are exempt. The runner owns the guard, which owns the admitted hard-exit action. That action owns a recorder handle and a single shared deadline, `schedule_stall_exit_record_deadline_seconds` (default 10s); always hard-exit 1. An abandoned NULL run row is closed as `interrupted` by manager reconcile.
- **Exit means terminal**: script exits cleanly with rc=0 → runner writes `status='completed'` before exiting (the resident process finished, manager will not restart); non-zero rc / uncaught exception → traceback written to `schedules.last_error` (latest crash) and, tail-truncated to 3000 chars after a `crashed: <ExceptionName>` first line, to that run's `schedule_runs.note` (one per crash; the session log is gone with the session), handed to manager to restart; SIGTERM/SIGHUP active kill → nothing written, not counted as a crash

The process entrypoint statically imports the SDK and supplies its database factory,
actor binding and plugin loading to `gateway/schedules/runner.py`. It captures the
SDK's loaded image and gives execution and lazy telemetry handles one code-version
gate. `runpy.init_globals` supplies immutable `ScheduleInputs` to the templates;
their execution bodies borrow the same builders without constructing or closing
another gate or writer. Direct Python template execution owns its separate entry.
Configuration reads the launcher's already-delivered environment without
redelivering it. The process closes its actual clients with a two-second pipeline
drain budget, preserving an execution error when cleanup also fails. The engine
materializes the script, binds its actor, then opens the run-history row. SDK
policy readers retain their normal live read timing; the gateway HTTP process
does not construct this schedule SDK owner. The manager launches the new entrypoint
for every new session. The retired `gateway.schedules.runner` CLI exits 1 with
the replacement command; existing schedule processes keep their original code.
The manager still recognizes their recorded revision and adopts matching live
sessions without reaping or replaying them. An old manager must be replaced before
launching new sessions from a checkout that has retired its old runner command.

The guard starts before plugin loading. Script completion closes stall admission
under the same lock used to claim a real stall, wakes the guard, and joins its
actual thread before restoring sleep or writing terminal outcomes. A sample
already in progress cannot admit a new stall after close. An action admitted
before close still reaps descendants and hard-exits; normal completion never
overtakes it. Ordinary close has a 1s join budget. After stall admission, the
finite guard join budget covers the existing 3s TERM grace, 5s reap and record
deadline with 1s scheduling margin. If that join expires
after a real stall was admitted, the runner still exits 1 without terminal
bookkeeping.

The recorder retains its thread and original unexpected error. Its finite join
uses the remaining shared deadline for both writes. At expiry, it reports
unfinished recording, signals the recorder to stop before another write, and
hard-exits even when the current database call cannot cooperate. Unexpected
worker errors are immediately visible and retained for the original owner's
close to raise; a failed script remains primary when guard close also fails.
A successful `SystemExit` (`0`, `None` or `False`) is completion and cannot mask
a guard close failure or produce a completed marker.
Recoverable frame-read failures and severable database-write failures retain
their existing warning/recovery behavior.
