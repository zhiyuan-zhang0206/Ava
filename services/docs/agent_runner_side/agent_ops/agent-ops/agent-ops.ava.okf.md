---
type: doc
title: Agent-Ops — Agent-Runner Inbound HTTP Ops Service
description: The sole resident Ava HTTP process on agent-runner — binds 0.0.0.0, receives POST /ops ops requests from Gateway after authentication with a write-generation machine API token, calls the ops op clusters in-process, executes and returns synchronously. Compact request/response, non-streaming.
tags: []
---

# Agent-Ops — Agent-Runner Inbound HTTP Ops Service

## What is it
The sole resident Ava HTTP process on agent-runner (session `ops`) — Gateway resolves the local address from the `machines` table and directly connects to `POST /ops`; the daemon calls the ops op clusters (`ops.cluster` / `ops.host_config` / `ops.inventory` / `ops.lifecycle` / `ops.uploads`) in-process to execute cluster ops operations, returning results synchronously in the HTTP response. Compact request/response, non-streaming, no queue / no SSE / no reconnection.

**Role affiliation**: agent-runner side (gateway does not run; instead it runs `gateway.ops_*` in-process) — `ServiceSpec.capabilities=_AGENT_RUNNER` in `ops/spec.py`.

## Core Responsibilities
- **Inbound HTTP endpoint**: binds `0.0.0.0:<ops_port>`, serves `POST /ops`; `GET /healthz` (watchdog health check) on the same port, via localhost without auth. The health envelope reports active-op age and informational worker saturation; an operation held longer than 20 minutes (the no-progress bound plus margin) returns 503 so the watchdog restarts the daemon.
- **Bearer-authenticated when configured** (`services/agent_runner/agent_ops/_boot._ops_acceptance`): every `/ops` carries a machine API token of this unit's write generation — the gateway's gateway-class token, or a runner-class token from the unit's own processes — verified against digests only (a remote unit's capability carries the gateway token's digest, never the token or the human secret); a single-machine gateway dials its own `/ops` the same way. An open cluster (empty secret; a capability without API admission) is the deliberate no-auth, loopback-only posture. The acceptance set is read once, at daemon boot: a revoked generation stays refused only because its one revocation path, the release fence, runs with root and the ops daemon already stopped ([[base/cluster/authority/docs/api-tokens.ava.okf.md|API tokens]]).
- **In-process execution**: each request calls ops functions inside the daemon, no extra spawn; bounded concurrency semaphore (`ops_concurrency`) + shared DB pool.
- **Off the event loop**: agent launch and lifecycle operations run on the loop;
  synchronous arms run in the daemon's worker pool through
  `services/agent_runner/agent_ops/dispatch_sync.py:dispatch_sync`. Blocking filesystem work
  leaves health and the generation-checked hold release reachable.
  Configuration and inventory read-modify-write operations share a thread lock.
- **Admission**: only the current `OpKind` vocabulary reaches maintenance
  admission or idempotency storage. Retired updater RPCs fail without effects,
  even when a caller presents an old successful idempotency key. The daemon
  accepts no bootstrap mode; unknown argv refuses before ordinary imports.
- **work kinds**: `spawn-launch|spawn-launch-v2` / `lifecycle` / `status_probe` / `config_read|write` / `inventory_read|write` / `agent_skill_view`. The command-view read uses this runner's converged load dir plus the agent checkpoint's `ava_code__cwd` project roots, and scopes skill-as-command entries through that agent's persisted `config_overlay > birth_config` narrowing; provider cleanup is unconditional so one request cannot leak project skills into the next. Its result also carries this runner's sorted enabled MCP server names as phase-2 groundwork, with no gateway or frontend consumer yet.
- **Singleton**: pidfile ensures only one instance per agent-runner; before start, `assert_schema_current` (refuses service if DB is ahead).
- **Boot self-registration** (`_register_boot`): once the health server is up, the daemon calls `base.cluster.machines.register_self(url=unit_dial_url(machine_role()))` for its own unit — clearing any `stopped_at` latch and restamping `up_since_at`. The `machine_units` row is a liveness record, so the process whose liveness it stands for is the one that writes it; `ava start` alone could not, because a host also comes back via an OS autostart, a watchdog respawn, or a rollout's restart leg. Deliberately **non-fatal** (unlike `assert_schema_current`): a stale row is not incorrect dispatch, and exiting would hand the watchdog a respawn loop that takes the host dark for the gateway. `unit_dial_url` is shared with `ava start`, so the two writers cannot advertise different addresses for one unit.

`ops.cluster_pause` uses `ops.agent_pause` for the shared native drain;
`ops.agent_pause.probe` checks actual daemon identity and admitted work.
Dependency APIs remain available until existing native actions finish.
Local service teardown closes new API admission only after the drain; normal
start resumes the existing hold after readiness. The dispatch pool runs
`min_size=0`. Shell-closure notices are not an ops task: `ava stop` writes
them itself (`ops/pty_close_notices.py`). A cluster update stops and starts each unit through
`cli.fleet_update`, not through ops kinds. See [[base/deploy/maintenance/docs/maintenance.ava.okf.md|Native pause and maintenance]].

## Strongly-Typed Wire Layer (`ops/rpc_schemas/__init__.py`)

The wire contract — `OpEnvelope`/`OpResponse` envelopes, the `OpKind` literal,
and the per-kind payload/result models the daemon validates before dispatch —
is specified in [[services/docs/agent_runner_side/agent_ops/agent-ops/wire-layer.ava.okf.md]].

## Resident Loops

The ops server and the **delivery-outbox redelivery loop** (`services/agent_runner/agent_ops/outbox_flusher.py`) run under one `TaskGroup` in `_main`: a loop that raises cancels the server and ends the process, and the supervisor restarts the unit. The loop re-commits this machine's `$AVA_HOME/state/delivery-outbox/` records (chat sends that exhausted their retries) through the canonical chat-inbound path, one round per `AVA_DELIVERY_OUTBOX_FLUSH_INTERVAL_SECONDS`, skipping rounds while the unit is quiesced. Record state (attempts, backoff position, abandonment) lives in the record file, so a restart resumes every cooldown. The loop reports progress per record to `/healthz` (`loops.delivery-outbox`); a pass that handles no record for four flush intervals plus a minute reads as wedged.

## Key Dependencies
- [[gateway-cli.ava.okf.md]] — Gateway issues ops commands to agent-runner via this service
- [[services/supervision/ava_root_glue/docs/ava_root_glue.ava.okf.md]] — keeps alive every 60s (HTTP `/healthz`)
- [[db.ava.okf.md]] — ops directly reads/writes the cluster DB in-process

## Entry Points
- `services/agent_runner/agent_ops/daemon.py` — `.venv/bin/python -m services.agent_runner.agent_ops.daemon`
- `ops/rpc_schemas/__init__.py` — `OpEnvelope`/`OpResponse`/`OpKind` + per-kind payload/result models

## Notes
- Unlike Gateway's `/api/*` endpoints — agent-ops is the inbound ops port on the agent-runner side.
- Binding and auth are consistent per host, no single-vs-multi-host branching; LAN reachable but without secret it won't work.

### Deferred delivery journal state

`base/agents/messages/delivery_outbox.py:DeliveryOutboxState` owns the durable
`pending` / `abandoned` disposition. Reads restore enum members; writes retain
plain JSON strings. A corrupt or unknown state remains unreadable and retained
for inspection without preventing healthy entries from being flushed.
