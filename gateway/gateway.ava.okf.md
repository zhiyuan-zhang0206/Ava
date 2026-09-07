---
type: doc
title: Gateway
description: 'Ava cluster HTTP API gateway—FastAPI service running on **port 8000**, bound to loopback without a cluster secret / to reachable addresses only with a secret and declared transport encryption. Pure JSON API, no HTML rendering — with one deliberate exception: GET /api/okf/graph serves a self-contained HTML page (D3 OKF knowledge-graph visualization).'
tags: []
---

# Gateway

Ava cluster HTTP API gateway—FastAPI service running on **port 8000** (loopback-only when no cluster secret is set; reachable addresses require both a secret and declared transport encryption — `gateway/_server.py:main`). Pure JSON API, no HTML rendering — with a small enumerated set of exceptions: `GET /api/okf/graph` (D3 knowledge-graph page), uploads `FileResponse`, the agents' page-server reverse proxy, and the grafana reverse proxy. Frontend, CLI, agent SDK (`ava.agents.*`), bootstrap scripts all access the cluster through the same set of `/api/*` endpoints.

## Terminology (domain ubiquitous language)

> This is the authoritative terminology for the gateway domain.

- **Gateway** — central gateway process: one FastAPI app, one port. Named gateway but after cutover it is **more than an adapter** — it is both the HTTP surface and the cluster orchestrator (`gateway/routers/cluster.py` — `/api/cluster/*` endpoints; `ops/cluster_rpc.py` — cross-machine RPC via POST to the target's agent-ops `/ops`). **Keeping the old name** because `gateway` is already the established identifier for 170+ files / service session names / Python packages, renaming churn far outweighs the benefit. Three key "not's": gateway **is not the lifecycle owner** (the `agents_meta` table is the truth, agents themselves spawn/terminate/heartbeat via DB + the native supervisor), **does not route inter-agent messages** (the `inbound_messages` table is the bus; gateway sits on the **write** path — `gateway/routers/_delivery.py:deliver_chat_inbound` inserts chat inbound for send_message / UI replies — but never routes between agents), **stateless** (restart loses no state). Browser chat writes optionally carry a `client_message_id` committed under a unique constraint with the inbound; `POST .../messages/reconcile` returns the stable `inbound_id` after an ambiguous response and replays the pending wake/exact-trigger resurrection tail, so a gateway death cannot turn an HTTP retry into a duplicate chat.
- **Client** — HTTP consumers of the gateway (more than one): Next.js browser frontend + agent SDK on agent-runner, both directly connect to `/api/*` over private network. Client **does not** talk to agents directly — everything goes through gateway HTTP.

## Core Responsibilities

- **Agent lifecycle management**: unified handling of spawn, send_message, terminate, resurrect, restart via `/api/agents/*`
- **Eval result boundary**: artifact-read endpoints reject eval-isolated callers from their stored per-agent configuration, so bypassing the SDK cannot expose another run's transcript, activity, events, memory search, or task results
- **SSE event push**: Redis pub/sub → SSE bridge, pushing agent events to the browser in real time
- **Runtime observability**: process CPU/RSS/file descriptors, event-loop lag/slow ticks, and SSE connection depth/open/close rates flow through the unified OTLP emitter
- **Alert truth reconciliation**: startup + five-minute reads of Grafana's active Alertmanager instances repair stored alert resolutions whose one-shot webhook was lost
- **Failure feedback delivery**: authenticated CI, QA, and merge failure events are deduplicated durably, then delivered to the author through chat auto-resurrection, the nearest live birth ancestor, or a task-registry alert
- **Schedule keep-alive**: built-in ScheduleManager, keeping schedule resident processes alive in their own sessions (not a timer trigger — timing logic is inside the script, the manager only ensures stay-up)
- **Cluster ops API**: cluster, config, inventory, metrics, system and other management endpoints
- **MCP control plane**: revocable scoped tokens guard default-off `/mcp`; cluster-authenticated `/api/mcp/clients` manages them
- **Per-agent command views**: `GET /api/commands?agent_id=` resolves the agent's runner then asks its `agent_skill_view` op for the command catalog that runner discovers from its own converged load dir plus the agent's persisted cwd; an unavailable, unknown, or version-skewed runner falls back to the gateway-local catalog
- **Authentication and browser-origin policy**: server-side `web_sessions` + bearer-secret auth, exact-origin CORS checks, and the Secure cookie policy — [[web-sessions.ava.okf.md]].
- **Inbound provenance**: gateway-created inbounds persist the server-verified credential kind, ingress transport, exact-content SHA-256, and a nullable agent source/token comparison. These are audit facts only and never reject delivery — [[shared/inbound-provenance.ava.okf.md]].

## Architecture

```
Browser (frontend:3000) ──HTTP──▶ Gateway (:8000) ──▶ Postgres / Redis
                                      │
       agents ─────────────────────┐ │ ┌───────────────── schedules
   Gateway ──POST /ops──▶ agent-ops  │   Gateway ──▶ session backend (direct)
   daemon ──▶ detached process (runner) │   schedule_manager._launch
```
- **agents**: spawn / lifecycle uniformly goes through `_forward_to_home_machine` → `cluster_rpc` POST `/ops` to the agent-ops daemon, the runner commits durable work and publishes a wake to its agent host — **even if the target is the local machine, there is no in-process shortcut** (`gateway/routers/agents_forward.py:_forward_to_home_machine()`)
- **schedules**: `schedule_manager._launch` is gateway's **only** path that directly manages sessions (agent processes are hosted by the native supervisor on the runner side, not by the gateway)

- Gateway connects to Postgres via one `shared.db.pool()` per process, borrowing one connection per request. Going through the factory rather than constructing a `ConnectionPool` is what gives the borrows `prepare_threshold=0` (transaction-pooling-safe under PgBouncer) and `PG_KEEPALIVE_KWARGS` (a request-serving pool outlives host sleeps; without keepalives a borrow on a half-dead socket stalls on the OS TCP-retransmit timeout). `scripts/lint_pool_keepalives.py` enforces it
- Event publishing uses a process-level shared `aredis.Redis` instance
- SSE subscribers open a separate Redis connection per request

## Key Dependencies

- [[routers.ava.okf.md]] — per-domain router modules in `gateway/routers/` (incl. `okf_graph.py` and `default_model.py`)
- [[sse.ava.okf.md]] — Redis → SSE event stream
- [[gateway/routers/ops-monitor.ava.okf.md]] — `GET /api/ops/monitor` ops panel series
- [[telemetry-staleness.ava.okf.md]] — Loki/Prometheus heartbeat guard for stale telemetry reads
- [[runtime-metrics.ava.okf.md]] — process resources, event-loop responsiveness, and SSE lifecycle metrics
- [[mcp_endpoint.ava.okf.md]] — MCP boundary details
- [[scheduler.ava.okf.md]] — ScheduleManager + ScheduleRunner
- [[db.ava.okf.md]] — Postgres connection pool

## Entry Points

- Entry point inventory: [[entry-points.ava.okf.md]].

## Notes

- **Stateless design**: Gateway does not hold agent process state. Restarting Gateway does not affect running agents (they are detached processes, double fork reparented to init, not hanging off the gateway/ops process tree)
- **Endpoint contract**: `/docs` / `/redoc` / `/openapi.json` all `None` (`app.py:257-259`, to prevent route schema leakage) — no OpenAPI pages; the code is the sole source of truth for the contract, codegen generates frontend / SDK types from the code
- **Concurrency model**: async/await full chain (FastAPI + psycopg_pool + aredis)
