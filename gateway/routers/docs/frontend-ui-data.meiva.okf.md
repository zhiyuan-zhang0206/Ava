---
type: doc
title: Frontend UI data routers
description: The route modules serving the frontend's read surfaces — fleet graph, timeline, notices, pages (+ streaming proxy), the Grafana reverse proxy, and plugin UI contributions/mounts.
tags: []
---

# Frontend UI data routers

- **fleet_graph** (`/api/fleet/graph`) — weighted agent relationship graph; nodes carry canonical lifecycle `status`, immutable birth parent (`born_spawner`), independent `liveness_state` from `agents_meta`, and token sums from `telemetry_events`; edges are aggregated in Postgres from `audit_events`
- **timeline** (`gateway/agents/history/timeline.py`, `/api/agents/{id}/timeline`) — current checkpoint tail plus optional scroll-up pages from retained `compact_boundary` checkpoints. Current cursors are `msg.block`; history cursors are `s<rank>.<checkpoint_id>.<msg>.<block>`, resolved by checkpoint id and bounded by `MEIVA_TIMELINE_COMPACT_HISTORY` (`0` off / positive newest-N / `-1` all). A segment-head response repeats its re-attached prompt/summary before the next older tail so a page boundary cannot hide the summary; a historical summary can continue across a re-attached-only boundary one rank per request. Crossovers may read source and target sequentially but keep at most one full segment resident. Bad, missing, non-boundary, or depth-ineligible historical ids terminate rather than selecting another segment ([decision](../../../docs/decisions/agents/context/2026-08-25-timeline-compact-history-pagination.md)).
- **notices** (`gateway/agents/notices.py`, `/api/notices/*`) — `meiva.ui.notify()` user notifications; rows carry `task_id` (`null` when unattached), driving notice→task linkage in the inspector
- **pages** (`/api/pages`, `/api/agents/{id}/pages`, keyed register/close under `/api/keyed/v1/agents/{id}/pages`) — `meiva.ui.show/close` registered UI pages + the streaming reverse proxy for them (`/pages/{id}-{name}/...` → the page server; browser never dials it directly)
- **grafana** (`/grafana/*`, outside `/api`) — optional streaming reverse proxy to a co-located Grafana instance (`MEIVA_GRAFANA_PROXY_ENABLED`, `MEIVA_GRAFANA_HOST`/`MEIVA_GRAFANA_PORT`, default off → 404), auth-gated by the same cluster middleware, for dashboard iframes
- **ui_contributions** (`gateway/extensions/ui_contributions.py`, `/api/ui/contributions`) — the merged, plugin-attributed `contributions.ui` declaration set of the cluster's ENABLED plugins (theme token packs, nav entries, and statistics-panel cards today; agent-inspect sections as that slice lands). Read straight from each plugin's `meiva-plugin.json` — no plugin code is imported to answer it [[docs/plugins/package-manifest.meiva.okf.md]]
- **plugin_ui** (`gateway/extensions/plugin_ui.py`, `/api/plugin-ui/<plugin>/…`) — the sibling mount of `pages`: static files from an ENABLED plugin's own `ui/` directory, for the sandboxed iframe the console embeds. `pages`' segment validation plus a resolved-path containment check

Reverse-proxy contracts live in `gateway/tests/routers/proxies/`: Grafana and
agent page servers preserve authenticated forwarding, streaming, error responses
and path-traversal rejection.

Traversal tests encode the path so the HTTP client cannot normalize literal
`..` before it reaches the server. Plugin UI requires 400 for encoded dot
segments; upload tests use `..%2f..%2fsecret` and accept 400 or 404. That upload
assertion alone does not establish that a particular resolver ran.
