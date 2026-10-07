---
type: doc
title: Gateway Routers
description: The gateway's route catalog — feature packages (gateway/<feature>/) plus single-module routers (gateway/routers/<domain>.py), all mounted to /api/* by app.py (grafana outside /api).
tags: []
---

# Gateway Routers

Every route module is a FastAPI `APIRouter` `include_router`-mounted to `/api/*` at the bottom of `gateway/app.py` (grafana mounts outside `/api`). A surface whose routes, helpers and wire models change together is a feature package; a single-module surface stays in `gateway/routers/<domain>.py` with its models in `gateway/schemas/<domain>.py`:

| package | routers |
|---|---|
| `gateway/agents/` | `router` (agents), `lifecycle`, `state`, `conversation`, `timeline`, `notices`; helpers `forward`, `delivery` |
| `gateway/events/` | `router` (events), `agent_events`, `computer_traces`, `resolutions`, `metrics`, `system` |
| `gateway/cluster/` | `router` (cluster), `machine_pause`, `bootstrap`, `status`, `ops_monitor` |
| `gateway/extensions/` | `inventory`, `skills`, `plugin_ui`, `ui_contributions`, `packages` |
| `gateway/alerts/`, `gateway/auth/`, `gateway/mcp_server/`, `gateway/schedules/`, `gateway/run_timeline/`, `gateway/inspect/` | `router` |

## Router categories

### Agent core (lifecycle + observability)
- **agents** (`/api/agents/*`) — [[gateway/agents/docs/agents-router.ava.okf.md|lifecycle and projection contract]].
- **agent_events** (`/api/agents/{id}/events` + `/events/stream`) — historical REST query over the unified event stream (Loki) + real-time SSE tail (filtered by agent_id)
- **events** (`/api/events`) — unified event stream query (Wave 2): every category (audit / telemetry / log) through one surface: audit rows from Postgres `audit_events`, telemetry/log from Postgres `telemetry_events`, merged newest-first; filters category/event_name/agent_id/trace_id/machine/level + time window (`from`/`to` or `hours`) + offset paging, `meta` (total/window/has_more) envelope
- **run_timeline** (`/api/agents/{id}/run-timeline`) — see [[run_timeline.ava.okf.md|the understanding tree and message units over a window]] (its `/messages` range read included).
- **inspect** (`/api/agents/{id}/inspect/*` + `/neighbors`) — per-agent LLM cost/token/TPS + neighbor graph; plugin metric and inspector-widget surfaces — [[gateway/inspect/docs/inspect.ava.okf.md]]
- **system** (`/api/system`, `/api/agents/{id}/system`, `/api/system/all`) — SSE broadcasting (see [[gateway/events/docs/sse.ava.okf.md]])
- **delivery** — chat inbound delivery helper (not a router); gateway callers attach server-owned credential, transport, content-hash, and source-assertion facts at the durable insert
- **ops_monitor** (`/api/ops/monitor`) — time-bucketed ops panel series (SSE backlog / LLM latency+TPS / restart counts), see [[gateway/cluster/docs/ops-monitor.ava.okf.md]]
- **alerts** (`/api/alerts` + `/stream` + `/read`) — the system→human alert store (Alertmanager shape, `alerts` table), unresolved-first list + counts, SSE tail, mark-as-read, IM fan-out via im_bridge [[gateway/alerts/docs/alerts.ava.okf.md]]
- **event_resolutions** (`/api/event-resolutions`) — authenticated immutable-Loki warning/error class dismissal history: create, status-filtered review list, and manual reopen; writes `event_dismissals` and emits transition markers, while the events-maintenance daemon publishes the resulting gauges

### Cluster & configuration
- **cluster** (`/api/cluster/*`) — cluster status, multi-machine roster, admin events, and maintenance control (admin contracts: [[gateway/cluster/docs/ops-surfaces.ava.okf.md]])
- **bootstrap** (`/api/bootstrap`) — agent-runner registration handshake (returns cluster config; `AVA_DB_URL` is the credential-free endpoint, never a login, and the human secret is never served; admits a unit's machine API token)
- **config** (`/api/config`) — runtime configuration read/write (PUT is merge-patch reducer, not full-replace); validates the full affected Settings candidate before persisting (400 invalid / 409 concurrent-write retry)
- **settings** (`/api/settings`) — frontend user preference KV store (`user_settings` table)
- **frontend_telemetry** (`POST /api/frontend-telemetry`) — user-modeling telemetry ingest: validates a batch of tracked frontend interactions (page/element/session_id/key/value, no free text) and emits one `frontend_interaction` event per accepted interaction into the unified stream (per-session rate-limit backstop)
- **inventory** (`/api/inventory`) — cross-machine plugin + MCP enable/disable panel
- **skills** (`/api/skills`) — read-only: this machine's `$AVA_HOME/skills/` load dir × install registry view (layer=core/plugin/machine/untracked, `modified_locally` drift flag). Unlike inventory, skills are **per-machine** (no cluster-shared rows)—no `?machine=` matrix, reports only this gateway's own
- **presets** (`/api/presets`) — agent configuration preset templates CRUD; [[resource-creation.ava.okf.md|creation receipts]] preserve accepted identity across retries.
- **packages** (`POST /api/packages/draft`) — **install entry** for skill/plugin/MCP: `{kind, nl}` → fixed prompt to `ava.skills.ava_guide.packages.install` → spawn an installer agent, return `agent_id`. **Deliberately no URL/spec fields**—users can't judge candidate quality; candidate-finding, confirmation, install, test-agent verification, evaluation all happen in that agent's conversation. Same shape as guide/schedules draft (no DB row, no new state)
- **guide** (`POST /api/guide/draft`) — same-shaped ops entry: spawn an `ava-guide` agent to handle natural-language ops requests

### Frontend UI data

[[gateway/routers/docs/frontend-ui-data.ava.okf.md]]

### Ops & system
- **status** + **alert_classes** (`/api/health`, `/api/status`, `/api/stats/dashboard`, `/api/stats/alert-classes[/samples]`) — liveness + status panel + dashboard + the card's warning/error classes; public health exposes process `started_at` and boot-frozen `sha` for rollout observers ([[gateway/cluster/docs/ops-surfaces.ava.okf.md|dashboard contract]])
- **metrics** (`/api/metrics`, `/api/metrics/agents`) — aggregated metrics over `telemetry_events`
- **schedules** (`/api/schedules/*`) — scheduled task CRUD + start/stop/restart
- **shell** (`/api/agents/{id}/shell/{sid}`) — terminal session monitor (session backend proxy)
- **tasks** (`/api/tasks` GET + `/api/tasks/{id}` PATCH) — [[task-patch-receipts.ava.okf.md|task registry read + keyed partial update]] (no create; an owner reassignment notifies the new and, when live, previous owner); GET defaults to a full compatibility row or serves a metadata-only SQL projection with `fields=summary`; rows carry `priority` (`P0`..`P3`, validated, illegal 422)
- **memory** (`/api/memory/search`, `/refresh`, `/graph`) — Memory pool search/refresh/graph
- **commands** (`/api/commands`) — slash command list acceptable by composer
- **auth** (`/api/auth/login|logout|check|sessions`) — opaque server-side session login, validation, listing, and per-session revocation
- **uploads** (`/api/agents/{id}/uploads`) — file upload; notifications name the machine owning each path. Remote runner pulls are tracked per filename; a failed pull retains the gateway location and authenticated download route without shifting another file's path.

## Design principles

Dependency direction, the no-turn-loop rule and its enumerated exception, the
handler/mounting split, and boundary typing:
[[gateway/routers/docs/design-principles.ava.okf.md]].

## Entry points

- `gateway/routers/__init__.py` — empty file, router modules are independent
- `gateway/app.py` — mounting point for all routers (`app.include_router(x.router)`), in a fixed order

## Notes

New endpoint → add it to its feature package, or create `gateway/routers/<domain>.py` for a new single-module surface; then `include_router` in `app.py`. Frontend/CLI/SDK share the same endpoints.
