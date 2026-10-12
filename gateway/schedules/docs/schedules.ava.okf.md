---
type: doc
title: Schedule Manager & Runner
description: The built-in supervisor keeps a live session for each enabled schedule—a resident process, not a cron trigger.
tags: []
---

# Schedule Manager & Runner

A **schedule** is a resident process supervised by the `schedule-manager` service (`services/wake/schedule_manager/daemon.py`, its own root unit; the gateway only serves the API). Its script controls timing, typically with a resumable loop; the `schedules` table has no time fields. ScheduleManager admits one launch per desired revision, adopts live sessions and alerts on prolonged absence. Clean completion is terminal.

## Two Components

### ScheduleManager (`services/wake/schedule_manager/manager.py`)
- **Built-in seeding**: the service's start calls `provision_builtin_schedules`, which creates missing manifest schedules when `AVA_PROVISION_BUILTIN_SCHEDULES=1` (default). Setting it to `0` keeps a fresh cluster unseeded; existing schedules still run and explicit `ava schedules provision` still works. It also resyncs an existing built-in row whose script or command differs from the template (new `schedule_versions` row, sync request queued; `enabled` and description untouched); agent-created schedules are never read.
- **Reconcile loop** (one of the service's two resident loops; a loop that raises ends the process and root restarts it): every 5 seconds, compares the database `schedules` table (desired: enabled rows) against actual sessions (actual: live sessions). Each missing enabled session launch holds the local `base.deploy.lifecycle.start_serving` generation lock and proceeds only while serving; cleanup (disabled/deleted session reaping and orphan run closure) remains active before that boundary.
- **Edit acceptance**: same-value edits add no versions or sync work. Real changes commit with their queue entry; keyed retries and revision recovery are documented in [[schedule-convergence.ava.okf.md]].
- **API requests**: start / stop / restart / script edit / delete do not call the service; the router upserts a row in `schedule_sync_requests` (`gateway/schedules/session_control.py`) and waits up to 8 s for it to be consumed. The service's `requests` loop (every second) converges the latest desired revision, adopting matching PTYs and deletes the row only if it is unchanged; while a maintenance hold is up the rows stay queued. Log capture reads the session straight from the shell backend in the gateway.
- **Starts missing** sessions, **kills excess** sessions (when a schedule is disabled/deleted). Every explicit stop and enabled-state value change synchronously invokes the identity-checked PTY backend; `stopped` is written only after that backend confirms the session is gone, while a failed reap remains queued for an identity-checked retry.
- **Generation boundary**: exact old-session records support cleanup only. Loss of a started PTY does not authorize another launch for the same revision.
- **Liveness identity**: the session name `ava-schedule-<id>` is the schedule's stable identity—it encodes no cluster/machine name (path-only cluster identity, #629/#633); the PTY session namespace itself is host-local + per-home (the service socket `$AVA_HOME/run/pty-sessions.sock`), and home already isolates sessions adequately
- **Service restart**: re-identifies live sessions and retains the persisted one-attempt latch and stall clock. Missing started sessions become errors, without script replay.
- **Terminal execution**: a disappeared session with `status=completed` remains completed. Otherwise the manager records an error requiring explicit start/restart. Neither case relaunches automatically.
- **Alerts on prolonged silence**: an enabled schedule with no live session for more than two hours emits one WARNING plus one `schedule_stalled` telemetry event. `status='error'` remains eligible without being relaunched. The first sessionless observation (`not_live_since`) and the alert (`stall_alerted_at`) are columns of the row, so one outage alerts once across restarts. Seeing the session live again clears both and rearms a later outage; completed and disabled schedules are excluded.

### ScheduleRunner (`gateway/schedules/runner.py`)
- The in-session entrypoint `.venv/bin/python -m services.wake.schedule_manager.runner <id>`; its script execution, stall guard, hard-exit cleanup and exit semantics are in [[schedule-runner.ava.okf.md]].

### Built-in Cron Slot Claims (`schedules/catchup.py`)

- Cron knowledge remains in each script template; neither `ScheduleManager` nor the `schedules` data model learns cron expressions.
- Online fires call `fire_slot_once()`; a slot missed while the schedule was not running is not replayed. The TTL reaper deletes every claim past the retention window.
- `fire_slot_once()` claims with `INSERT ... ON CONFLICT DO NOTHING`, which commits before the callback, making `(schedule_id, slot_fire_at)` at-most-once across concurrent processes and restarts.
- The winning callback receives `(slot_fire_at, payload)` explicitly; the slot is normalized to UTC before claiming and dispatch. Window-based reports use that slot.
- A crash after the claim but before callback completion leaves the slot claimed. This accepted loss window is the explicit cost of at-most-once behavior; there is no at-least-once retry path.

## State Machine (`schedules.status`)

| status | meaning | reconcile behavior |
|---|---|---|
| `running` | manager has launched, session alive | maintain |
| `completed` | script exited cleanly with rc=0 (**terminal**) | **no restart**; `start`/`restart` can rerun |
| `error` | failed, lost or unknown launch/execution outcome | no auto restart; explicit start/restart creates a new revision; the silence alert still applies |
| `stopped` | disabled / killed by manager | don't restart |

## One launch attempt per revision

The manager conditionally commits `launch_count=1` and an unknown-outcome error
before asking the PTY backend to create the session. It marks a confirmed live
session running. A failed response or process disappearance is not replayed.
The latch survives manager restart; only an explicit new revision resets it.
The legacy `next_launch_at` column is ignored. A crash between admission and
actual process birth can lose work; this is accepted to prevent duplicate starts.

## Key Dependencies

- [[agent/db/docs/db.ava.okf.md]] — reads/writes the `schedules` and `schedule_fire_log` tables
- `base.cluster.session_name()` — generates the session name `ava-schedule-<id>`
- [[agent/docs/lifecycle.ava.okf.md]] — agent lifecycle is independent of persistent schedule sessions

## Entry Points

- `services/wake/schedule_manager/daemon.py` — `.venv/bin/python -m services.wake.schedule_manager.daemon`: the service, its two loops and the checkout guard
- `services/wake/schedule_manager/manager.py:ScheduleManager` — reconcile logic
- `services/wake/schedule_manager/runner.py:main()` — the process entrypoint; calls `gateway/schedules/runner.py:run()` with explicit SDK inputs
