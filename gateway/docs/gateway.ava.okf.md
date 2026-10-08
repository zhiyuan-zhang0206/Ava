---
type: doc
title: Gateway
description: Ava cluster HTTP API, request admission and orchestration shared by frontend, CLI and SDK.
tags: []
---

# Gateway

Ava cluster HTTP API gateway—FastAPI service running on **port 8000** (loopback-only when no cluster secret is set; reachable addresses require both a secret and declared transport encryption — `gateway/cluster/server.py:main`). Pure JSON API, no HTML rendering — with a small enumerated set of exceptions: `GET /api/okf/graph` (D3 knowledge-graph page), uploads `FileResponse`, the agents' page-server reverse proxy, and the grafana reverse proxy. Frontend, CLI, agent SDK (`ava.agents.*`) and bootstrap scripts use the same `/api/*` endpoints.

## Terminology (domain ubiquitous language)

> This is the authoritative terminology for the gateway domain.

- **Gateway** — one FastAPI app, one port; after cutover more than an adapter, also the cluster orchestrator (`gateway/cluster/router.py`; `ops/cluster/rpc.py` for cross-machine RPC to agent-ops `/ops`). Keeps the old name — `gateway` is the identifier for 170+ files, renaming churn outweighs benefit. Three "not's": **not the lifecycle owner** (`agents_meta` is the truth; agents spawn/terminate/heartbeat via DB + the native supervisor; release decisions run outside the gateway too, a retained native operation), **does not route inter-agent messages** (`inbound_messages` is the bus; gateway writes to it via `gateway/agents/delivery.py:deliver_chat_inbound`, never routes between agents), **stateless** (restart loses no state). Chat writes may carry a `client_message_id` (unique constraint with the inbound); `POST .../messages/reconcile` returns the stable `inbound_id` after an ambiguous response and replays the pending wake/resurrection tail — a gateway death cannot duplicate chat.
- **Client** — HTTP consumers of the gateway (more than one): Next.js browser frontend + agent SDK on agent-runner, both directly connect to `/api/*` over private network. Client **does not** talk to agents directly — everything goes through gateway HTTP.

## Core Responsibilities

`http/` groups authentication and middleware. Agent history lives in
`agents/history/`; the OKF graph viewer lives in `inspect/` and upload intake in
`routers/upload/`. Wire endpoints and lifecycle ownership are unchanged.

HTTP admission reads the home's durable maintenance journal on each business
request. `stopping`, `stopped`, `starting` and `ready` block it; drain phases
keep SDK dependencies available to the remaining fleet. Atomic resume releases
this same authority immediately, without a cached database posture delaying it.
Unreadable or incomplete paused records block business requests. Control-plane
routes bypass the admission read so health and repair remain reachable. The
database posture is a status projection, not this gate's authority.

- **Agent lifecycle management**: unified handling of spawn, send_message, terminate, resurrect, restart via `/api/agents/*`
- **Eval result boundary**: artifact-read endpoints reject eval-isolated callers from their stored per-agent configuration, so bypassing the SDK cannot expose another run's transcript, activity, events, memory search, or task results
- **SSE event push**: Redis pub/sub → SSE bridge, pushing agent events to the browser in real time
- **Runtime observability**: process CPU/RSS/file descriptors, event-loop lag/slow ticks, and SSE connection depth/open/close rates flow through the unified OTLP emitter
- **Alert truth reconciliation**: the events-maintenance service's startup + five-minute reads of Grafana's active Alertmanager instances repair stored alert resolutions whose one-shot webhook was lost
- **Failure feedback delivery**: authenticated CI, QA, and merge failure events are deduplicated durably, then delivered to the author through chat auto-resurrection, the nearest live birth ancestor, or a task-registry alert
- **Schedule keep-alive**: built-in ScheduleManager, keeping schedule resident processes alive in their own sessions (not a timer trigger — timing logic is inside the script, the manager only ensures stay-up)
- **Cluster ops API**: cluster, config, inventory, metrics, system and other management endpoints
- **MCP control plane**: revocable scoped tokens guard default-off `/mcp`; human-credential-only `/api/mcp/clients` manages them
- **Per-agent command views**: `GET /api/commands?agent_id=` resolves the agent's runner then asks its `agent_skill_view` op for the command catalog that runner discovers from its own converged load dir plus the agent's persisted cwd; an unavailable, unknown, or version-skewed runner falls back to the gateway-local catalog
- **Authentication and browser-origin policy**: server-side `web_sessions` + bearer-secret auth, exact-origin CORS checks, and the Secure cookie policy — [[gateway/http/auth/docs/web-sessions.ava.okf.md]].
- **Inbound provenance**: gateway-created inbounds persist the server-verified credential kind, ingress transport, exact-content SHA-256, and a nullable agent source/token comparison. These are audit facts only and never reject delivery — [[inbound-provenance.ava.okf.md]].

## Architecture

```
Browser (frontend:3000) ──HTTP──▶ Gateway (:8000) ──▶ Postgres / Redis
                                      │
       agents ─────────────────────┐ │ ┌───────────────── schedules
   Gateway ──POST /ops──▶ agent-ops  │   Gateway ──▶ session backend (direct)
   daemon ──▶ detached process (runner) │   ScheduleManager._launch
```
- **agents**: spawn / lifecycle uniformly goes through `forward_to_home_machine` → `cluster_rpc` POST `/ops` to the agent-ops daemon, the runner commits durable work and publishes a wake to its agent host — **even if the target is the local machine, there is no in-process shortcut** (`gateway/agents/forward.py:forward_to_home_machine()`)
- **schedules**: the gateway only queues sync requests and reads log captures (`gateway/schedules/session_control.py`); the `schedule-manager` service launches schedule sessions

- Gateway connects to Postgres via one `base.db.pool()` per process, borrowing one connection per request. Going through the factory rather than constructing a `ConnectionPool` is what gives the borrows `prepare_threshold=None` (never prepare; transaction-pooling-safe under PgBouncer) and `PG_KEEPALIVE_KWARGS` (a request-serving pool outlives host sleeps; without keepalives a borrow on a half-dead socket stalls on the OS TCP-retransmit timeout). Rule 5 (`postgres-dial`) enforces it
- Event publishing uses a process-level shared `aredis.Redis` instance
- SSE subscribers open a separate Redis connection per request

## Key Dependencies

Feature packages (routes + helpers + wire models):
[[agents-router.ava.okf.md|agents]],
[[ops-surfaces.ava.okf.md|cluster]],
[[gateway/events/docs/sse.ava.okf.md|events]],
[[gateway/alerts/docs/alerts.ava.okf.md|alerts]],
[[services/derived/insights/run_timeline/docs/run_timeline.ava.okf.md|run_timeline]] (proxied),
[[gateway/inspect/docs/inspect.ava.okf.md|inspect]],
[[gateway/schedules/docs/schedules.ava.okf.md|schedules]],
[[gateway/mcp_server/docs/mcp-endpoint.ava.okf.md|mcp_server]], `auth`, `lgtm`,
`middleware`, `extensions`. [[routers.ava.okf.md]]:
single-module routers. [[agent/db/docs/db.ava.okf.md]]: Postgres pool.

## Entry Points

- Inventories: [[entry-points.ava.okf.md]], [[idempotency/idempotency.ava.okf.md]].

## Notes

- **Stateless design**: Gateway does not hold agent process state. Restarting Gateway does not affect running agents (they are detached processes, double fork reparented to init, not hanging off the gateway/ops process tree)
- **Endpoint contract**: `/docs` / `/redoc` / `/openapi.json` all `None` (`app.py:257-259`, to prevent route schema leakage) — no OpenAPI pages; the code is the sole source of truth for the contract, codegen generates frontend / SDK types from the code
- **Concurrency model**: async/await full chain (FastAPI + psycopg_pool + aredis)
