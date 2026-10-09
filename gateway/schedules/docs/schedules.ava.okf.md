---
type: doc
title: Schedule Manager & Runner
description: The built-in supervisor keeps a live session for each enabled schedule—a resident process, not a cron trigger.
tags: []
---

# Schedule Manager & Runner

A **schedule** is a resident process supervised by the `schedule-manager` service (`services/wake/schedule_manager/daemon.py`, its own root unit; the gateway only serves the API). Its script controls timing, typically with a resumable loop; the `schedules` table has no time fields. ScheduleManager maintains enabled sessions, restarts crashes and alerts on prolonged absence. Clean completion is terminal.

## Two Components

### ScheduleManager (`services/wake/schedule_manager/manager.py`)
- **Built-in seeding**: the service's start calls `provision_builtin_schedules`, which creates missing manifest schedules when `AVA_PROVISION_BUILTIN_SCHEDULES=1` (default). Setting it to `0` keeps a fresh cluster unseeded; existing schedules still run and explicit `ava schedules provision` still works. It also resyncs an existing built-in row whose script or command differs from the template (new `schedule_versions` row, sync request queued; `enabled` and description untouched); agent-created schedules are never read.
- **Reconcile loop** (one of the service's two resident loops; a loop that raises ends the process and root restarts it): every 5 seconds, compares the database `schedules` table (desired: enabled rows) against actual sessions (actual: live sessions). Each missing enabled session launch holds the local `base.deploy.lifecycle.start_serving` generation lock and proceeds only while serving; cleanup (disabled/deleted session reaping and orphan run closure) remains active before that boundary.
- **Edit acceptance**: same-value edits add no versions or sync work. Real changes commit with their queue entry; keyed retries and revision recovery are documented in [[schedule-convergence.ava.okf.md]].
- **API requests**: start / stop / restart / script edit / delete do not call the service; the router upserts a row in `schedule_sync_requests` (`gateway/schedules/session_control.py`) and waits up to 8 s for it to be consumed. The service's `requests` loop (every second) converges the latest desired revision, adopting matching PTYs and deletes the row only if it is unchanged; while a maintenance hold is up the rows stay queued. Log capture reads the session straight from the shell backend in the gateway.
- **Starts missing** sessions, **kills excess** sessions (when a schedule is disabled/deleted). Every explicit stop and enabled-state value change synchronously invokes the identity-checked PTY backend; `stopped` is written only after that backend confirms the session is gone, while a failed reap remains queued for an identity-checked retry.
- **Generation boundary**: exact old-session records support cleanup only; they never decide desired state. A missing PTY is rebuilt after a restart when, and only when, the current schedule row remains enabled.
- **Liveness identity**: the session name `ava-schedule-<id>` is the schedule's stable identity—it encodes no cluster/machine name (path-only cluster identity, #629/#633); the PTY session namespace itself is host-local + per-home (the service socket `$AVA_HOME/run/pty-sessions.sock`), and home already isolates sessions adequately
- **Restart safe**: after a restart of the service (or the gateway), it re-identifies existing sessions (no need to recreate), and resumes the crash backoff and the stall clock from the row
- **Distinguishes clean exit from crash**: each tick reads liveness before status. When a session disappears, it reads its terminal state—`status='completed'` (written by the runner before exiting with rc=0) = the resident process finished on its own, **terminal state, no restart, not counted toward circuit breaker**; no completed marker = crash (non-zero rc / signal / hard kill), brought up according to crash handling
- **Alerts on prolonged silence**: an enabled schedule with no live session for more than two hours emits one WARNING plus one `schedule_stalled` telemetry event. `status='error'` remains eligible even though the breaker will not relaunch it. The first sessionless observation (`not_live_since`) and the alert (`stall_alerted_at`) are columns of the row, so one outage alerts once across restarts. Seeing the session live again clears both and rearms a later outage; completed and disabled schedules are excluded.

### ScheduleRunner (`gateway/schedules/runner.py`)
- The in-session entrypoint `.venv/bin/python -m gateway.schedules.runner <id>`; its script execution, stall guard, hard-exit cleanup and exit semantics are in [[schedule-runner.ava.okf.md]].

### Built-in Cron Slot Claims (`schedules/catchup.py`)

- Cron knowledge remains in each script template; neither `ScheduleManager` nor the `schedules` data model learns cron expressions.
- Startup enumerates boundaries after the latest `schedule_fire_log` claim, using the schedule's `created_at` when no claim exists. It runs at most the two most recent missed slots and logs a WARNING when older candidates remain.
- Startup catch-up and normal online fires both call `fire_slot_once()`. Its `INSERT ... ON CONFLICT DO NOTHING` commits before the callback, making `(schedule_id, slot_fire_at)` at-most-once across concurrent processes and restarts.
- The winning callback receives `(slot_fire_at, payload)` explicitly; the slot is normalized to UTC before claiming and dispatch. Window-based reports use that slot for both catch-up and online fires.
- A crash after the claim but before callback completion leaves the slot claimed. This accepted loss window is the explicit cost of at-most-once behavior; there is no at-least-once retry path.

## State Machine (`schedules.status`)

| status | meaning | reconcile behavior |
|---|---|---|
| `running` | manager has launched, session alive | maintain |
| `completed` | script exited cleanly with rc=0 (**terminal**) | **no restart, no breaker**; `start`/`restart` can rerun |
| `error` | crash loop breaker tripped (see below, **relaunch-terminal**) | **no auto restart** (same skip as `completed`)—breaker state lives in the DB (`status`, `launch_count`, `next_launch_at`), so **a restart won't relaunch it**; the two-hour silence alert still applies; recover via `start`/`restart` manually (the existing API resets status + clears backoff) |
| `stopped` | disabled / killed by manager | don't restart |

## Circuit Breaker

Only applies to **crashes** (not clean exits) looping—clean exits go to `completed` terminal state and never reach the breaker:
- **Max retries**: 5 (`_BREAKER_MAX`)
- **Exponential backoff**: 2s → 4s → 8s → ... cap 60s (`_BACKOFF_CAP_S`); the launch is claimed with one conditional UPDATE of `launch_count` / `next_launch_at`
- **Recovery condition**: schedule runs continuously for more than 60s (`_STABLE_S`) → counter reset
- **After breaker trips**: `status='error'`, no more auto restart, waiting for manual re-enable. This `error` is **persisted**—reconcile treats it as a terminal state and skips it (same as `completed`), and the counter itself is a column, so a restart will **not** re-launch an already-tripped crash loop with a fresh counter.

## Key Dependencies

- [[agent/db/docs/db.ava.okf.md]] — reads/writes the `schedules` and `schedule_fire_log` tables
- `base.cluster.session_name()` — generates the session name `ava-schedule-<id>`
- [[agent/docs/lifecycle.ava.okf.md]] — agent lifecycle is independent of persistent schedule sessions

## Entry Points

- `services/wake/schedule_manager/daemon.py` — `.venv/bin/python -m services.wake.schedule_manager.daemon`: the service, its two loops and the checkout guard
- `services/wake/schedule_manager/manager.py:ScheduleManager` — reconcile logic
- `gateway/schedules/runner.py:run()` — loads and runs a single schedule (the in-session entrypoint of `main()` → `.venv/bin/python -m gateway.schedules.runner <id>`)
