---
type: doc
title: Schedule Manager & Runner
description: Gateway's built-in schedule supervisor—a schedule is a supervised resident process (not a cron trigger). The ScheduleManager background coroutine guarantees a live session for every enabled schedule.
tags: []
---

# Schedule Manager & Runner

A **schedule** is a **resident process** supervised by the gateway (supervised resident process), **not** a cron trigger. The `schedules` table has **no time fields at all**—when to do things and how often is entirely determined internally by the script (typically a `while True: … sleep()` resumable loop). The ScheduleManager ensures every enabled schedule has a **live** session (stay-up); if it dies, it is brought back up according to crash semantics. It also alerts when an enabled, non-completed schedule stays sessionless for more than two hours, without interpreting cron expressions.

## Two Components

### ScheduleManager (`gateway/schedule_manager.py`)
- **Background reconcile loop**: every 5 seconds, compares the database `schedules` table (desired: enabled rows) against actual sessions (actual: live sessions). Each missing enabled session launch holds the local `shared.start_serving` generation lock and proceeds only while serving; cleanup (disabled/deleted session reaping and orphan run closure) remains active before that boundary.
- **Starts missing** sessions, **kills excess** sessions (when a schedule is disabled/deleted). Every explicit stop and enabled-state value change synchronously invokes the identity-checked PTY backend; `stopped` is written only after that backend confirms the session is gone, while a failed reap remains queued for an identity-checked retry.
- **Generation boundary**: exact old-session records support cleanup only; they never decide desired state. A missing PTY is rebuilt after gateway restart when, and only when, the current schedule row remains enabled.
- **Liveness identity**: the session name `ava-schedule-<id>` is the schedule's stable identity—it encodes no cluster/machine name (path-only cluster identity, #629/#633); the PTY session namespace itself is host-local + per-home (`$AVA_HOME/run/pty/`), and home already isolates sessions adequately
- **Gateway restart safe**: after a restart, it re-identifies existing sessions (no need to recreate)
- **Distinguishes clean exit from crash**: each tick reads liveness before status. When a session disappears, it reads its terminal state—`status='completed'` (written by the runner before exiting with rc=0) = the resident process finished on its own, **terminal state, no restart, not counted toward circuit breaker**; no completed marker = crash (non-zero rc / signal / hard kill), brought up according to crash handling
- **Alerts on prolonged silence**: an enabled schedule with no live session for more than two hours emits one WARNING plus one `schedule_stalled` telemetry event. `status='error'` remains eligible even though the breaker will not relaunch it. Seeing the session live again rearms a later outage; completed and disabled schedules are excluded.

### ScheduleRunner (`gateway/schedule_runner.py`)
- **In-session entrypoint**: `.venv/bin/python -m gateway.schedule_runner <id>`
- Loads the schedule's script + command from the DB
- Materializes the script to `$AVA_HOME/schedules/<id>/`
- Binds the `schedule:<id>` actor identity (so `ava.agents.*` invocations are attributed to the schedule)
- `.py` scripts are executed in-process via `runpy`; other commands are run as subprocesses
- **Exit means terminal**: script exits cleanly with rc=0 → runner writes `status='completed'` before exiting (the resident process finished, manager will not restart); non-zero rc / uncaught exception → traceback written to `schedules.last_error` (crash, handed to manager to restart); SIGTERM/SIGHUP active kill → nothing written, not counted as a crash

### Built-in Cron Slot Claims (`schedules/catchup.py`)

- Cron knowledge remains in each script template; neither `ScheduleManager` nor the `schedules` data model learns cron expressions.
- Startup enumerates boundaries after the latest `schedule_fire_log` claim, using the schedule's `created_at` when no claim exists. It runs at most the two most recent missed slots and logs a WARNING when older candidates remain.
- Startup catch-up and normal online fires both call `fire_slot_once()`. Its `INSERT ... ON CONFLICT DO NOTHING` commits before the callback, making `(schedule_id, slot_fire_at)` at-most-once across concurrent processes and restarts.
- A crash after the claim but before callback completion leaves the slot claimed. This accepted loss window is the explicit cost of at-most-once behavior; there is no at-least-once retry path.

## State Machine (`schedules.status`)

| status | meaning | reconcile behavior |
|---|---|---|
| `running` | manager has launched, session alive | maintain |
| `completed` | script exited cleanly with rc=0 (**terminal**) | **no restart, no breaker**; `start`/`restart` can rerun |
| `error` | crash loop breaker tripped (see below, **relaunch-terminal**) | **no auto restart** (same skip as `completed`)—breaker state lives in DB, not in-memory `_backoff`, so **gateway restart won't restart it**; the two-hour silence alert still applies; recover via `start`/`restart` manually (the existing API resets status + clears backoff) |
| `stopped` | disabled / killed by manager | don't restart |

## Circuit Breaker

Only applies to **crashes** (not clean exits) looping—clean exits go to `completed` terminal state and never reach the breaker:
- **Max retries**: 5 (`_BREAKER_MAX`)
- **Exponential backoff**: 2s → 4s → 8s → ... cap 60s (`_BACKOFF_CAP_S`)
- **Recovery condition**: schedule runs continuously for more than 60s (`_STABLE_S`) → counter reset
- **After breaker trips**: `status='error'`, no more auto restart, waiting for manual re-enable. This `error` is **persisted**—reconcile treats it as a terminal state and skips it (same as `completed`), so a gateway restart that clears in-memory counters will **not** re-launch an already-tripped crash loop with a fresh counter.

## Key Dependencies

- [[db.ava.okf.md]] — reads/writes the `schedules` and `schedule_fire_log` tables
- `shared.cluster.session_name()` — generates the session name `ava-schedule-<id>`
- [[agent/lifecycle.ava.okf.md]] — agent lifecycle is independent of persistent schedule sessions

## Entry Points

- `gateway/schedule_manager.py:ScheduleManager` — reconcile loop
- `gateway/schedule_runner.py:run()` — loads and runs a single schedule (the in-session entrypoint of `main()` → `.venv/bin/python -m gateway.schedule_runner <id>`)

## Notes

Schedules are **supervised resident processes**, not cron triggers—the `schedules` table has no time fields; "when to run" is entirely inside the script. A typical script is a **resumable loop** (`while True: … sleep()`) whose built-in cron slots are reconstructed from `schedule_fire_log` on restart. One-shot scripts are also valid—exiting with rc=0 yields `completed` and is not mistaken for "disappeared" and endlessly restarted.
