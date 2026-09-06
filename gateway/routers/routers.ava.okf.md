---
type: doc
title: Gateway Routers
description: Gateway's route modules, one FastAPI APIRouter per business domain under gateway/routers/<domain>.py, uniformly mounted to /api/* by app.py (grafana mounts outside /api).
tags: []
---

# Gateway Routers

Gateway's 43 route modules, split by business domain under `gateway/routers/<domain>.py`, each a FastAPI `APIRouter` `include_router`-mounted to `/api/*` at the bottom of `gateway/app.py` (grafana mounts outside `/api`). `_delivery.py` / `agents_forward.py` are not routers but internal helpers (chat-inbound delivery / cross-machine forward).

## Router categories

### Agent core (lifecycle + observability)
- **agents** (`/api/agents/*`) — [[gateway/routers/agents-router.ava.okf.md|lifecycle and projection contract]].
- **agent_events** (`/api/agents/{id}/events` + `/events/stream`) — historical REST query over the unified `events` table + real-time SSE tail (filtered by agent_id)
- **events** (`/api/events`) — unified event stream query (Wave 2): every category (audit / telemetry / log) through one surface over the `events` table, filters category/event_name (legacy kind alias)/agent_id/trace_id/machine/level + time window (`from`/`to` or `hours`) + offset paging, `meta` (total/window/has_more) envelope
- **run_timeline** (`/api/agents/{id}/run-timeline`) — event-driven run/turn view: reads bounded Loki history for one agent, joins `llm_usage` to `turn_end` by span ID when available and otherwise once by completed-turn time window; execution and anomaly events use the same time-window association. It returns turn rows or caller-selected time buckets with lifecycle, compact, idle, and failure markers, reports fallback and unmatched usage counts, and supports the default compact-ended or `session=current` lifecycle window. Missing lifecycle history falls back to the last 24 hours.
- **agent_inspect** (`/api/agents/{id}/inspect` + `/inspect/live` + `/neighbors` + `/inspect/metrics`) — per-agent LLM cost/token/TPS + neighbor graph; inspect p50/p90 combine complete daily integer-second duration histograms (backfilled archive-era days bucketed) with an exact live tail; frozen-archive exact values only as a raw full-window fallback while daily histogram coverage is incomplete; min/max always exact; plus the plugin-metric inspector surface (`/inspect/metrics`, W13b: builds the metric registry in process — shipped plugin `metrics.py` modules + core definitions — renders `output`-inspector templates per agent, re-validates the rendered query, executes LogQL over Loki / SQL read-only over Postgres — see `shared/plugin_metrics.py` + the `deploy/lgtm` dashboards README)
- **system** (`/api/system`, `/api/agents/{id}/system`, `/api/system/all`) — SSE broadcasting (see [[sse.ava.okf.md]])
- **_delivery** — chat inbound delivery internal helper (not a router); gateway callers attach server-owned credential, transport, content-hash, and source-assertion facts at the durable insert
- **ops_monitor** (`/api/ops/monitor`) — time-bucketed ops panel series (SSE backlog / LLM latency+TPS / restart counts), see [[gateway/routers/ops-monitor.ava.okf.md]]
- **alerts** (`/api/alerts` + `/stream` + `/read`) — the system→human alert store (Alertmanager shape, `alerts` table), unresolved-first list + counts, SSE tail, mark-as-read, IM fan-out via im_bridge [[gateway/routers/alerts.ava.okf.md]]
- **work_failed** (`POST /api/work-failed`) — durable, deduplicated CI/QA/merge failure feedback routed to the author, nearest live birth-lineage delegator, or a P1 task alert [[gateway/routers/work-failed.ava.okf.md]]
- **event_resolutions** (`/api/event-resolutions`) — authenticated immutable-Loki warning/error class dismissal history: create, status-filtered review list, and manual reopen; writes `event_dismissals` and emits transition markers, while the events-maintenance daemon publishes the resulting gauges

### Cluster & configuration
- **cluster** (`/api/cluster/*`) — cluster status, multi-machine roster, admin events, rollout/update/stop control (admin contracts: [[gateway/routers/ops-surfaces.ava.okf.md]])
- **bootstrap** (`/api/bootstrap`) — agent-runner registration handshake (returns cluster config)
- **config** (`/api/config`) — runtime configuration read/write (PUT is merge-patch reducer, not full-replace); validates the full affected Settings candidate before persisting (400 invalid / 409 concurrent-write retry)
- **settings** (`/api/settings`) — frontend user preference KV store (`user_settings` table)
- **frontend_telemetry** (`POST /api/frontend-telemetry`) — user-modeling telemetry ingest: validates a batch of tracked frontend interactions (page/element/session_id/key/value, no free text) and emits one `frontend_interaction` event per accepted interaction into the unified stream (per-session rate-limit backstop)
- **inventory** (`/api/inventory`) — cross-machine plugin + MCP enable/disable panel
- **skills** (`/api/skills`) — read-only: this machine's `$AVA_HOME/skills/` load dir × install registry view (layer=core/plugin/machine/untracked, `modified_locally` drift flag). Unlike inventory, skills are **per-machine** (no cluster-shared rows)—no `?machine=` matrix, reports only this gateway's own
- **presets** (`/api/presets`) — agent configuration preset templates CRUD
- **packages** (`POST /api/packages/draft`) — **install entry** for skill/plugin/MCP: `{kind, nl}` → fixed prompt to `ava.skills.ava_package_installer` → spawn an installer agent, return `agent_id`. **Deliberately no URL/spec fields**—users can't judge candidate quality; candidate-finding, confirmation, install, test-agent verification, evaluation all happen in that agent's conversation. Same shape as guide/schedules draft (no DB row, no new state)
- **guide** (`POST /api/guide/draft`) — same-shaped ops entry: spawn an `ava-guide` agent to handle natural-language ops requests

### Frontend UI data

[[gateway/routers/frontend-ui-data.ava.okf.md]]

### Ops & system
- **status** (`/api/health`, `/api/status`, `/api/stats/dashboard`) — liveness + status panel + dashboard; public health exposes process `started_at` and boot-frozen `sha` for rollout observers ([[gateway/routers/ops-surfaces.ava.okf.md|dashboard contract]])
- **metrics** (`/api/metrics`, `/api/metrics/agents`) — aggregated metrics over the unified `events` stream
- **schedules** (`/api/schedules/*`) — scheduled task CRUD + start/stop/restart
- **shell** (`/api/agents/{id}/shell/{sid}`) — terminal session monitor (session backend proxy)
- **tasks** (`/api/tasks` GET + `/api/tasks/{id}` PATCH) — task registry read + partial update (no create; an owner reassignment notifies the new and, when live, previous owner); GET defaults to a full compatibility row or serves a metadata-only SQL projection with `fields=summary`; rows carry `priority` (`P0`..`P3`, validated, illegal 422)
- **memory** (`/api/memory/search`, `/refresh`, `/graph`) — Memory pool search/refresh/graph
- **commands** (`/api/commands`) — slash command list acceptable by composer
- **auth** (`/api/auth/login|logout|check|sessions`) — opaque server-side session login, validation, listing, and per-session revocation
- **uploads** (`/api/agents/{id}/uploads`) — file upload; notifications name the machine owning each path. Remote runner pulls are tracked per filename; a failed pull retains the gateway location and authenticated download route without shifting another file's path.

## Design principles

Dependency direction, the no-turn-loop rule and its enumerated exception, the
handler/mounting split, and boundary typing:
[[gateway/routers/design-principles.ava.okf.md]].

## Entry points

- `gateway/routers/__init__.py` — empty file, router modules are independent
- `gateway/app.py` — mounting point for all routers (`app.include_router(x.router)`)

## Notes

New endpoint → create `gateway/routers/<domain>.py`, then `include_router` in `app.py`. Frontend/CLI/SDK share the same endpoints.
