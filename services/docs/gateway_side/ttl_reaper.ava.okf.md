---
type: doc
title: TTL Reaper — wall-clock deadline enforcement
description: Gateway-side service that reclaims every row past its wall-clock deadline (pages, persistent shell sessions, browser sessions, notices, impersonation leases) and runs the slow lifecycle-pointer and fire-log maintenance on durable cadences.
tags: []
---

# TTL Reaper — wall-clock deadline enforcement

## What is it
The service that enforces every deadline the cluster hands out. It runs once per cluster on the gateway side (`ServiceSpec.capabilities=_GATEWAY`, health port slot `ttl_reaper` = 8121), as its own root unit: stopping or restarting it never touches the HTTP gateway, and the gateway holds no loop that can kill a shell.

## Loops
Two resident sequential loops under one `TaskGroup` (`services/upkeep/ttl_reaper/daemon.py`), each running a round every `AVA_TTL_REAPER_POLL_INTERVAL_SECONDS` (default 60s). A loop that raises cancels its sibling and ends the process; the supervisor restarts it. An unreachable database skips a round. Each loop reports its own progress to `/healthz`.

- **`sweep`** (`sweep.py`) — database only: expired `agent_pages` (terminalized with `expired_at`, `PageClosed` published), `web_sessions`, `agent_notices`, impersonation leases (reminded, then reaped), plus three slow phases claimed in `maintenance_state` (`cadence.py`): the `schedule_fire_log` retention prune (daily), the torn lifecycle-pointer scan and the absent-machine fence settle (hourly, `lifecycle_fences.py`).
- **`remote`** (`remote.py`) — calls out: TTL-expired persistent shell sessions are killed on their home machines (`shells.py`; machines concurrently, one machine's rows in order, each dispatch under a deadline sized from the RPC client's budget).

## Semantics that matter
- A shell row is deleted only on a definitive verdict (`killed` / `absent` / `machine_absent`); an unreachable machine, a failed op or a dispatch past its deadline leaves it for the next round. Each kill re-checks the row is still expired against `clock_timestamp()`, which pairs with the renewal guard so a renewed session is never killed.
- Owners are notified (system inbound) only while `running` / `idling`, never resurrected; a shell reclaim notifies only when it interrupted a running job.
- The cadence stamp is the claim, not the outcome: a slow phase that fails waits out its interval, and a restart resumes the clocks instead of running every slow phase at once.
- Known gap, unchanged: a kill dispatched but cut off before its row is deleted loses the "interrupted" notice (the next round's kill answers `absent`, silent).

## Key Dependencies
- `agent_shell_ttls`, `agent_pages`, `web_sessions`, `agent_notices`, `maintenance_state` — the tables it reads and settles.
- `ops.cluster_rpc` — the `shell_kill` op to runners' ops servers.
- `base/daemon/round_loop.py` — the round runner and bounded fan-out the resident service loops share (also the delivery watchdog).

## Entry Points
- `services/upkeep/ttl_reaper/daemon.py` — `.venv/bin/python -m services.upkeep.ttl_reaper.daemon`
- The application root supervises it through the roster's `/healthz` identity probe (`ops/roster/healthz.py`).
