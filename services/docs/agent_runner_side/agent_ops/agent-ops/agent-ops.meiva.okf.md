---
type: doc
title: Agent-Ops — Agent-Runner Inbound HTTP Ops Service
description: The sole resident Meiva HTTP process on agent-runner — binds 0.0.0.0, receives POST /ops ops requests from Gateway after authentication with a write-generation machine API token, calls the ops op clusters in-process, executes and returns synchronously. Compact request/response, non-streaming.
tags: []
---

# Agent-Ops — Agent-Runner Inbound HTTP Ops Service

## What is it
The sole resident Meiva HTTP process on agent-runner (session `ops`) — Gateway resolves the local address from the `machines` table and directly connects to `POST /ops`; the daemon calls the ops op clusters (`ops.cluster.operations` / `ops.host_config` / `ops.inventory` / `ops.lifecycle`) in-process to execute cluster ops operations, returning results synchronously in the HTTP response. Compact request/response, non-streaming, no queue / no SSE / no reconnection.

**Role affiliation**: agent-runner side (gateway does not run; instead it runs `gateway.ops_*` in-process) — `ServiceSpec.capabilities=AGENT_RUNNER` in `services/supervision/roster/spec.py`.

## Core Responsibilities
- **Inbound HTTP endpoint**: binds `0.0.0.0:<ops_port>` when the host declares a reachable address (`MEIVA_MACHINE_HOST`), else `127.0.0.1`; serves `POST /ops`; `GET /healthz` (watchdog health check) on the same port, via localhost without auth. The health envelope reports active-op age and informational worker saturation; an operation held longer than 11 minutes (the no-progress bound plus margin) returns 503 so the watchdog restarts the daemon.
- **Bearer-authenticated** (`services/agent_runner/agent_ops/boot.ops_acceptance`): every `/ops` carries a machine API token of this unit's write generation — the gateway's gateway-class token, or a runner-class token from the unit's own processes — verified against digests only (a remote unit's capability carries the gateway token's digest, never the token or the human secret); a single-machine gateway dials its own `/ops` the same way. The acceptance set is read once, at daemon boot: a revoked generation stays refused only because its one revocation path, the release fence, runs with root and the ops daemon already stopped ([[base/host/db_authority/docs/api-tokens.meiva.okf.md|API tokens]]).
- **Activity ownership**: each daemon invocation owns its active-op map, concurrency semaphore, request tokens and worker futures. Handlers and health share those values explicitly; a new invocation starts empty. Completion or failure removes active entries. Cancelling an awaiter keeps its worker retained and counted until completion.
- **In-process execution**: `ops_concurrency` bounds requests in the daemon; each invocation passes its DB pool and executor to request and worker arms.
- **Off the event loop**: agent launch and lifecycle operations run on the loop;
  synchronous arms run in the daemon's worker pool through
  `services/agent_runner/agent_ops/dispatch_sync.py:dispatch_sync`. Blocking filesystem work
  leaves health and the generation-checked hold release reachable.
  Configuration and inventory read-modify-write operations share a thread lock.
- **Admission**: only the current `OpKind` vocabulary reaches maintenance
  admission or idempotency storage. Retired updater RPCs fail without effects,
  even when a caller presents an old successful idempotency key. The daemon
  accepts no bootstrap mode; unknown argv refuses before ordinary imports.
- **work kinds**: `spawn-launch` / `lifecycle` / `status_probe` / `config_read|write` / `inventory_read|write` / `agent_skill_view`. The command-view read uses this runner's converged load dir plus the agent checkpoint's `meiva_code__cwd` project roots, and scopes skill-as-command entries through that agent's persisted `config_overlay > birth_config` narrowing; provider cleanup is unconditional so one request cannot leak project skills into the next. Its result also carries this runner's sorted enabled MCP server names as phase-2 groundwork, with no gateway or frontend consumer yet.
- **Singleton**: pidfile ensures only one instance per agent-runner; before start, `assert_schema_current` (refuses service if DB is ahead).
- **Boot self-registration**: the public `boot.register_boot` refreshes this unit's serving liveness after health starts. It retains its non-fatal registration behavior; see [[services/agent_runner/agent_ops/docs/process-composition.meiva.okf.md]].

`ops.cluster_status.pause` uses `ops.agent_pause` for the shared native drain;
`ops.agent_pause.probe` checks actual daemon identity and admitted work.
Dependency APIs remain available until existing native actions finish.
Local service teardown closes new API admission only after the drain; normal
start resumes the existing hold after readiness. The dispatch pool runs
`min_size=0`. Shell-closure notices are not an ops task: `meiva stop` writes
them itself (`ops/pty_close_notices.py`). A cluster update stops and starts each unit through
`cli.fleet_update`, not through ops kinds. See [[base/deploy/maintenance/docs/maintenance.meiva.okf.md|Native pause and maintenance]].

## Strongly-Typed Wire Layer (`ops/rpc_schemas/__init__.py`)

The wire contract — `OpEnvelope`/`OpResponse` envelopes, the `OpKind` literal,
and the per-kind payload/result models the daemon validates before dispatch —
is specified in [[services/docs/agent_runner_side/agent_ops/agent-ops/wire-layer.meiva.okf.md]].

## Message delivery

SDK and CLI message sends retry transient transport failures with the same
idempotency key for a finite number of attempts. The final failure is returned
to the caller. Agent-ops serves HTTP without a resident delivery-outbox flusher.
Existing `$MEIVA_HOME/state/delivery-outbox/` JSON records are retained on disk for
inspection; upgrading neither replays nor deletes them.

## Key Dependencies
- [[gateway-cli.meiva.okf.md]] — Gateway issues ops commands to agent-runner via this service
- [[services/supervision/meiva_root_glue/docs/meiva_root_glue.meiva.okf.md]] — keeps alive every 60s (HTTP `/healthz`)
- [[agent/db/docs/db.meiva.okf.md]] — ops directly reads/writes the cluster DB in-process

The executable's image, database admission and bounded event writer are described
in [[services/agent_runner/agent_ops/docs/process-composition.meiva.okf.md]].

## Entry Points
- `services/agent_runner/agent_ops/daemon.py` — `.venv/bin/python -m services.agent_runner.agent_ops.daemon`
- `ops/rpc_schemas/__init__.py` — `OpEnvelope`/`OpResponse`/`OpKind` + per-kind payload/result models

## Notes
- Unlike Gateway's `/api/*` endpoints — agent-ops is the inbound ops port on the agent-runner side.
- Binding and auth are consistent per host, no single-vs-multi-host branching; reach follows the declared reachable address; the bearer is always required.
