---
type: doc
title: Schedule Manager — resident schedule sessions
description: Gateway-side service that admits one PTY launch per desired schedule revision and consumes explicit lifecycle requests.
tags: []
---

# Schedule Manager — resident schedule sessions

## What is it
The service that supervises the `schedules` table: one session `meiva-schedule-<id>` per enabled row, running `services.wake.schedule_manager.runner`. It runs once per cluster on the gateway side (`ServiceSpec.capabilities=GATEWAY`, health port slot `schedule_manager` = 8122) as its own root unit. Stopping or restarting it never touches the HTTP gateway, and the schedule sessions survive it: the next start re-adopts the live ones. The full behavior (state machine, launch admission, runner) is in [[gateway/schedules/docs/schedules.meiva.okf.md]].

## Loops
Two resident sequential loops under one `TaskGroup` (`services/wake/schedule_manager/daemon.py`, on `base/daemon/round_loop.py`). A loop that raises cancels its sibling and ends the process; the supervisor restarts it. Each loop reports its own progress to `/healthz`.

- **`reconcile`** (`manager.py`) — every 5 s.
- **`requests`** (`requests.py`) — every second, converges each queued `schedule_sync_requests` row to its latest desired revision (adopt a matching live execution; officially reap a predecessor before replacement), then deletes it only if unchanged. A maintenance hold leaves work queued; a failed reap or uncertain live provenance does too, for a bounded window (ten minutes of one failure streak), then the manager records the schedule as an error, emits `schedule_convergence_abandoned` and consumes the request. A consumed launch attempt is terminal for that revision, even if its outcome is unknown. See [[gateway/schedules/docs/schedule-convergence.meiva.okf.md]] for transactional receipts and advisory ownership.

## State that moved from memory to the row
| was (gateway, in memory) | now |
|---|---|
| launch admission | `schedules.launch_count` is a one-attempt latch reset by an explicit new revision; the retired `next_launch_at` column is ignored |
| `_not_live_since`, `_stall_alerted` | `schedules.not_live_since`, `schedules.stall_alerted_at` |
| the call `ScheduleManager.sync(id)` from the router | a row in `schedule_sync_requests`, consumed by the `requests` loop |

Still in memory, deliberately: the set of schedules whose stale same-name survivor resisted a reap, and the reap-failure log throttle. A restarted service meets the same survivor again.

## Key Dependencies
- `base.sessions.backend` — the host's PTY session backend; the service must run on the gateway host.
- `base.deploy.lifecycle.start_serving`, `base.deploy.maintenance.admission` — launches proceed only while serving and not under a maintenance hold.
- `gateway/schedules/session_control.py` — the API side of the request queue and the log capture.

## Entry Points
- `services/wake/schedule_manager/runner.py` — `.venv/bin/python -m services.wake.schedule_manager.runner <id> [revision]`, the independent process that composes the SDK before calling the gateway schedule execution engine
- `services/wake/schedule_manager/daemon.py` — `.venv/bin/python -m services.wake.schedule_manager.daemon`
- The application root supervises it through the roster's `/healthz` identity probe (`services/supervision/roster/healthz.py`).


## Process composition

The executable entry captures one `LoadedCommit` and retains its `CodeVersion`
and `ProcessDbGate`. The work database factory and the lazy telemetry producer
share that gate, so new handles preserve the process admission read budget.
Logging and the existing health endpoint receive the same captured image; a
checkout move cannot relabel the running daemon. The daemon's existing startup,
pidfile, work and bounded-exit order remains unchanged.

The service entry retains its prepared `ConfigBoot` and resolved unit runtime
path. Endpoint and pidfile operations borrow those inputs through
`ServiceEndpoints.from_boot`; each original lookup reads the same live view.
The work and logging database factory calls `db_config_from_boot` at each
handle creation and retains the entry's gate and lazy machine host reader.
Schema checks, health-before-pool ordering and shutdown stay at their original
boundaries. Business-specific configuration slices keep their existing readers.
