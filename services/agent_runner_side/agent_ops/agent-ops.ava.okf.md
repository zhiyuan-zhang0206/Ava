---
type: doc
title: Agent-Ops — Agent-Runner Inbound HTTP Ops Service
description: The sole resident Ava HTTP process on agent-runner — binds 0.0.0.0, receives POST /ops ops requests from Gateway after authentication with cluster secret, calls ops/ops_*.py in-process, executes and returns synchronously. Compact request/response, non-streaming.
tags: []
---

# Agent-Ops — Agent-Runner Inbound HTTP Ops Service

## What is it
The sole resident Ava HTTP process on agent-runner (session `ops`) — Gateway resolves the local address from the `machines` table and directly connects to `POST /ops`; the daemon calls `ops/ops_*.py` (cluster/config/inventory/lifecycle) in-process to execute cluster ops operations, returning results synchronously in the HTTP response. Compact request/response, non-streaming, no queue / no SSE / no reconnection.

**Role affiliation**: agent-runner side (gateway does not run; instead it runs `gateway.ops_*` in-process) — `ServiceSpec.capabilities=_AGENT_RUNNER` in `ops/spec.py`.

## Core Responsibilities
- **Inbound HTTP endpoint**: binds `0.0.0.0:<ops_port>`, serves `POST /ops`; `GET /healthz` (watchdog health check) on the same port, via localhost without auth. The health envelope reports update-lock / active-op age and informational worker saturation; an operation held longer than 20 minutes (the no-progress bound plus margin) returns 503 so the watchdog restarts the daemon.
- **Bearer-authenticated when configured**: with `AVA_CLUSTER_SECRET`, every `/ops` carries it as a bearer — including a single-machine gateway dialing its own `/ops`. An empty secret is the deliberate no-auth, loopback-only single-box posture.
- **In-process execution**: each request calls ops functions inside the daemon, no extra spawn; bounded concurrency semaphore (`ops_concurrency`) + shared DB pool.
- **Off the event loop**: only `spawn` / `lifecycle` are awaited on the loop; every other arm is synchronous and runs in a worker thread (`_dispatch_sync`). A blocking op must never hold the loop — one that did cost a 2 h daemon-wide freeze on the Windows runner (2026-08-12), taking the controllers and the stranded-pause self-heal down with it. Threading costs the serialization the loop gave for free, so it is restated explicitly: a second concurrent `cluster_update` is **refused** with `ClusterUpdateInProgress` rather than queued (its caller learns immediately instead of waiting out a stuck one), while `config_write` / `inventory_write` — read-modify-write of `.env` and the plugin JSON — take a blocking `threading.Lock` so an interleave cannot land one writer's snapshot over another's fields. A stuck worker therefore leaves a host that serves everything normally and refuses only updates; the tell is a growing run of `refusing a concurrent cluster_update` in this daemon's log, and the fix is to bounce the daemon.
- **work kinds**: `spawn` / `lifecycle` / `cluster_stop` / `cluster_update` / `cluster_resume` / `status_probe` / `config_read|write` / `inventory_read|write` / `agent_skill_view`. The command-view read uses this runner's converged load dir plus the agent checkpoint's `ava_code__cwd` project roots, and scopes skill-as-command entries through that agent's persisted `config_overlay > birth_config` narrowing; provider cleanup is unconditional so one request cannot leak project skills into the next. Its result also carries this runner's sorted enabled MCP server names as phase-2 groundwork, with no gateway or frontend consumer yet.
- **Singleton**: pidfile ensures only one instance per agent-runner; before start, `assert_schema_current` (refuses service if DB is ahead).
- **Boot self-registration** (`_register_boot`): once the health server is up, the daemon calls `shared.machines.register_self(url=unit_dial_url(machine_role()))` for its own unit — clearing any `stopped_at` latch and restamping `up_since_at`. The `machine_units` row is a liveness record, so the process whose liveness it stands for is the one that writes it; `ava start` alone could not, because a host also comes back via an OS autostart, a watchdog respawn, or a rollout's restart leg. Deliberately **non-fatal** (unlike `assert_schema_current`): a stale row is not incorrect dispatch, and exiting would hand the watchdog a respawn loop that takes the host dark for the gateway. `unit_dial_url` is shared with `ava start`, so the two writers cannot advertise different addresses for one unit.

`ops.cluster_pause` uses `ops.agent_pause` for the shared native drain;
`ops.agent_pause_probe` checks actual daemon identity and admitted work.
Dependency APIs remain available until existing native actions finish.
Local service teardown closes new API admission only after the drain; normal
start resumes the existing hold after readiness. See
[[shared/maintenance.ava.okf.md|Native pause and maintenance]].

## Strongly-Typed Wire Layer (`ops/rpc_schemas.py`)
Request/response are no longer hand-assembled dicts — `OpEnvelope` (`{kind: str, payload: dict}`) in, `OpResponse` (`{status: "completed"|"failed", result: dict}`) out, with the legal set of `kind` being the `OpKind` Literal (re-exported from here by `ops/cluster_rpc.py` to keep old importers working). Each kind has dedicated pydantic payload/result models (e.g., `LifecyclePayload`, `ClusterUpdatePayload`, `ConfigWritePayload`, `InventoryWritePayload`, `AgentSkillViewPayload`; `ConfigReadResult`, `ConfigWriteOpResult`, `InventoryReadResult`, `InventoryWriteOpResult`, `ClusterSpawnSession`, `AgentSkillViewResult`). Pending-work resurrection carries `LifecyclePayload.trigger_inbound_id` plus `trigger_inbound_kind` (`chat` or `compact_request`) only on the distinct `resurrect-if-pending-work-v2` path; explicit manual resurrection uses `resurrect-explicit-v2`. A version-skewed old runner rejects both unknown v2 paths, while a new runner rejects legacy `/resurrect`, so neither rollout direction silently drops the guard and resurrects unconditionally. The daemon's `_dispatch` (`services/agent_ops/daemon.py:_dispatch`) uses `match kind:` to branch, `model_validate` the payload, call the corresponding `ops/ops_*.py` function, and `.model_dump(mode="json")` serialize the result — unknown kind falls to `case _` returning `failed`, not a crash. Failure state is unified as `OpFailure` (`error`/`detail`/`reason`), where `reason` is an enum value of `AvaAgentError`, allowing the gateway side to reconstruct the original exception. This batch of schemas is a bidirectional contract between gateway↔agent-runner, so placed in the `ops` layer following import layering (`shared < ops < gateway`) — gateway imports downward, `ops`/`services` never reach upward into gateway.

## Key Dependencies
- [[gateway-cli.ava.okf.md]] — Gateway issues ops commands to agent-runner via this service
- [[services/watchdog/watchdog.ava.okf.md]] — keeps alive every 60s (HTTP `/healthz`)
- [[db.ava.okf.md]] — ops directly reads/writes the cluster DB in-process

## Entry Points
- `services/agent_ops/daemon.py` — `.venv/bin/python -m services.agent_ops.daemon`
- `ops/rpc_schemas.py` — `OpEnvelope`/`OpResponse`/`OpKind` + per-kind payload/result models

## Notes
- Unlike Gateway's `/api/*` endpoints — agent-ops is the inbound ops port on the agent-runner side.
- Binding and auth are consistent per host, no single-vs-multi-host branching; LAN reachable but without secret it won't work.
