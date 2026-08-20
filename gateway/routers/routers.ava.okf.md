---
type: doc
title: Gateway Routers
description: Gateway's route modules, one FastAPI APIRouter per business domain under gateway/routers/<domain>.py, uniformly mounted to /api/* by app.py (grafana mounts outside /api).
tags: []
---

# Gateway Routers

Gateway's 37 route modules, split by business domain under `gateway/routers/<domain>.py`, each a FastAPI `APIRouter` `include_router`-mounted to `/api/*` at the bottom of `gateway/app.py` (grafana mounts outside `/api`). `_delivery.py` / `agents_forward.py` are not routers but internal helpers (chat-inbound delivery / cross-machine forward).

## Router categories

### Agent core (lifecycle + observability)
- **agents** (`/api/agents/*`) — spawn, terminate, resurrect, restart, compact, send_message, list, patch; plus `/api/cancel`, `/api/models`, `/api/agents/{id}/token-usage` (per-model soft/hard compact thresholds for the ContextMeter gauge), `/api/agents/{id}/context-breakdown` (#619: checkpoint messages bucketed by kind + system prompt by `#` sections; chars/4 normalized to the real input_tokens ratio; pure view in `gateway/context_breakdown.py`, one DB read), `/api/agents/{id}/exited|hibernating` (exit finalize / hibernate park) — split by surface: CRUD + spawn in `agents.py`, lifecycle in `agents_lifecycle.py`, message/state reads in `agents_state.py`, cross-machine forward helpers in `agents_forward.py`
- **agent_events** (`/api/agents/{id}/events` + `/events/stream`) — historical REST query over the unified `events` table + real-time SSE tail (filtered by agent_id)
- **events** (`/api/events`) — unified event stream query (Wave 2): every category (audit / telemetry / log) through one surface over the `events` table, filters category/event_name (legacy kind alias)/agent_id/trace_id/machine/level + time window (`from`/`to` or `hours`) + offset paging, `meta` (total/window/has_more) envelope
- **agent_inspect** (`/api/agents/{id}/inspect` + `/neighbors` + `/inspect/metrics`) — per-agent LLM cost/token/TPS + neighbor graph + the plugin-metric inspector surface (`/inspect/metrics`, W13b: reads the generator's `$AVA_HOME/state/plugin_metrics.json` registry, renders `output`-inspector templates per agent, re-validates the SQL, executes read-only over `events` — see `shared/plugin_metrics.py` + the `deploy/lgtm` dashboards README)
- **system** (`/api/system`, `/api/agents/{id}/system`, `/api/system/all`) — SSE broadcasting (see [[sse.ava.okf.md]])
- **_delivery** — chat inbound delivery internal helper (not a router)
- **ops_monitor** (`/api/ops/monitor`) — time-bucketed ops panel series (SSE backlog / LLM latency+TPS / restart counts), see [[gateway/routers/ops-monitor.ava.okf.md]]
- **alerts** (`/api/alerts` + `/stream` + `/read`) — the system→human alert store (Alertmanager shape, `alerts` table), unresolved-first list + counts, SSE tail, mark-as-read, IM fan-out via im_bridge [[gateway/routers/alerts.ava.okf.md]]

### Cluster & configuration
- **cluster** (`/api/cluster/*`) — cluster status, multi-machine roster, admin events, rollout/update/stop control (admin contracts: [[gateway/routers/ops-surfaces.ava.okf.md]])
- **bootstrap** (`/api/bootstrap`) — agent-runner registration handshake (returns cluster config)
- **config** (`/api/config`) — runtime configuration read/write (PUT is merge-patch reducer, not full-replace)
- **settings** (`/api/settings`) — frontend user preference KV store (`user_settings` table)
- **frontend_telemetry** (`POST /api/frontend-telemetry`) — user-modeling telemetry ingest: validates a batch of tracked frontend interactions (page/element/session_id/key/value, no free text) and emits one `frontend_interaction` event per accepted interaction into the unified stream (per-session rate-limit backstop)
- **inventory** (`/api/inventory`) — cross-machine plugin + MCP enable/disable panel
- **skills** (`/api/skills`) — read-only: this machine's `$AVA_HOME/skills/` load dir × install registry view (layer=core/plugin/machine/untracked, `modified_locally` drift flag). Unlike inventory, skills are **per-machine** (no cluster-shared rows)—no `?machine=` matrix, reports only this gateway's own
- **presets** (`/api/presets`) — agent configuration preset templates CRUD
- **packages** (`POST /api/packages/draft`) — **install entry** for skill/plugin/MCP: `{kind, nl}` → fixed prompt to `ava.skills.ava_package_installer` → spawn an installer agent, return `agent_id`. **Deliberately no URL/spec fields**—users can't judge candidate quality; candidate-finding, confirmation, install, test-agent verification, evaluation all happen in that agent's conversation. Same shape as guide/schedules draft (no DB row, no new state)
- **guide** (`POST /api/guide/draft`) — same-shaped ops entry: spawn an `ava-guide` agent to handle natural-language ops requests

### Frontend UI data
- **fleet_graph** (`/api/fleet/graph`) — weighted agent relationship graph; nodes from `agents_meta` + Prometheus counters, edges stitch frozen PG `events` + Loki
- **timeline** (`/api/agents/{id}/timeline`) — agent timeline messages
- **notices** (`/api/notices/*`) — `ava.ui.notify()` user notifications; rows carry `task_id` (`null` when unattached), driving notice→task linkage in the inspector
- **pages** (`/api/pages`, `/api/agents/{id}/pages`) — `ava.ui.show/close` registered UI pages + the streaming reverse proxy for them (`/api/agents/{id}/pages/{name}/...` → the page server; browser never dials it directly)
- **grafana** (`/grafana/*`, outside `/api`) — optional streaming reverse proxy to a co-located Grafana instance (`AVA_GRAFANA_PROXY_ENABLED`, `AVA_GRAFANA_HOST`/`AVA_GRAFANA_PORT`, default off → 404), auth-gated by the same cluster middleware, for dashboard iframes
- **ui_contributions** (`/api/ui/contributions`) — the merged, plugin-attributed `contributions.ui` declaration set of the cluster's ENABLED plugins (theme token packs today; nav + agent-inspect sections as those slices land). Read straight from each plugin's `ava-plugin.json` — no plugin code is imported to answer it [[okf/plugins/package-manifest.ava.okf.md]]
- **plugin_ui** (`/api/plugin-ui/<plugin>/…`) — the sibling mount of `pages`: static files from an ENABLED plugin's own `ui/` directory, for the sandboxed iframe the console embeds. `pages`' segment validation plus a resolved-path containment check

### Ops & system
- **status** (`/api/health`, `/api/status`, `/api/stats/dashboard`) — liveness + status panel + dashboard ([[gateway/routers/ops-surfaces.ava.okf.md|dashboard contract]])
- **metrics** (`/api/metrics`, `/api/metrics/agents`) — aggregated metrics over the unified `events` stream
- **schedules** (`/api/schedules/*`) — scheduled task CRUD + start/stop/restart
- **shell** (`/api/agents/{id}/shell/{sid}`) — terminal session monitor (session backend proxy)
- **tasks** (`/api/tasks` GET + `/api/tasks/{id}` PATCH) — task registry read + partial update (no create; PATCH does not notify owner); rows carry `priority` (`P0`..`P3`, validated, illegal 422)
- **memory** (`/api/memory/search`, `/refresh`, `/graph`) — Memory pool search/refresh/graph
- **commands** (`/api/commands`) — slash command list acceptable by composer
- **auth** (`/api/auth/login|logout|check`) — session authentication endpoints
- **uploads** (`/api/agents/{id}/uploads`) — file upload

## Design principles

Dependency direction, the no-turn-loop rule and its enumerated exception, the
handler/mounting split, and boundary typing:
[[gateway/routers/design-principles.ava.okf.md]].

## Entry points

- `gateway/routers/__init__.py` — empty file, router modules are independent
- `gateway/app.py` — mounting point for all routers (`app.include_router(x.router)`)

## Notes

New endpoint → create `gateway/routers/<domain>.py`, then `include_router` in `app.py`. Frontend/CLI/SDK share the same endpoints.
