---
type: doc
title: Schedule Manager — resident schedule sessions
description: Gateway-side service that keeps one resident PTY session alive per enabled schedule, under a crash backoff and breaker whose state lives on the schedule row, and consumes the API's sync requests.
tags: []
---

# Schedule Manager — resident schedule sessions

## What is it
The service that supervises the `schedules` table: one session `ava-schedule-<id>` per enabled row, running `gateway.schedule_runner`. It runs once per cluster on the gateway side (`ServiceSpec.capabilities=_GATEWAY`, health port slot `schedule_manager` = 8122) as its own root unit. Stopping or restarting it never touches the HTTP gateway, and the schedule sessions survive it: the next start re-adopts the live ones. The full behavior (state machine, breaker, runner) is in [[gateway/schedules/docs/schedules.ava.okf.md]].

## Loops
Two resident sequential loops under one `TaskGroup` (`services/wake/schedule_manager/daemon.py`, on `base/daemon/round_loop.py`). A loop that raises cancels its sibling and ends the process; the supervisor restarts it. Each loop reports its own progress to `/healthz`.

- **`reconcile`** (`manager.py`) — every 5 s.
- **`requests`** (`requests.py`) — every second, runs the sync of each queued `schedule_sync_requests` row (kill, relaunch if enabled, clear the backoff), then deletes it only if unchanged. A maintenance hold leaves the rows queued.

## State that moved from memory to the row
| was (gateway, in memory) | now |
|---|---|
| `_backoff[id] = (count, deadline)` | `schedules.launch_count`, `schedules.next_launch_at`; a launch is claimed with one conditional UPDATE |
| `_not_live_since`, `_stall_alerted` | `schedules.not_live_since`, `schedules.stall_alerted_at` |
| the call `ScheduleManager.sync(id)` from the router | a row in `schedule_sync_requests`, consumed by the `requests` loop |

Still in memory, deliberately: the set of schedules whose stale same-name survivor resisted a reap, and the reap-failure log throttle. A restarted service meets the same survivor again.

## Key Dependencies
- `base.sessions.backend` — the host's PTY session backend; the service must run on the gateway host.
- `base.deploy.lifecycle.start_serving`, `base.deploy.maintenance.admission` — launches proceed only while serving and not under a maintenance hold.
- `gateway/schedules/session_control.py` — the API side of the request queue and the log capture.

## Entry Points
- `services/wake/schedule_manager/daemon.py` — `.venv/bin/python -m services.wake.schedule_manager.daemon`
- The application root supervises it through the roster's `/healthz` identity probe (`ops/roster/healthz.py`).
