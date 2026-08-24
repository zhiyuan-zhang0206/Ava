---
type: doc
title: Frontend UI data routers
description: The route modules serving the frontend's read surfaces — fleet graph, timeline, notices, pages (+ streaming proxy), the Grafana reverse proxy, and plugin UI contributions/mounts.
tags: []
---

# Frontend UI data routers

- **fleet_graph** (`/api/fleet/graph`) — weighted agent relationship graph; nodes carry canonical lifecycle `status` plus independent `liveness_state` from `agents_meta` and Prometheus counters, edges stitch frozen PG `events` + Loki
- **timeline** (`/api/agents/{id}/timeline`) — agent timeline messages
- **notices** (`/api/notices/*`) — `ava.ui.notify()` user notifications; rows carry `task_id` (`null` when unattached), driving notice→task linkage in the inspector
- **pages** (`/api/pages`, `/api/agents/{id}/pages`) — `ava.ui.show/close` registered UI pages + the streaming reverse proxy for them (`/api/agents/{id}/pages/{name}/...` → the page server; browser never dials it directly)
- **grafana** (`/grafana/*`, outside `/api`) — optional streaming reverse proxy to a co-located Grafana instance (`AVA_GRAFANA_PROXY_ENABLED`, `AVA_GRAFANA_HOST`/`AVA_GRAFANA_PORT`, default off → 404), auth-gated by the same cluster middleware, for dashboard iframes
- **ui_contributions** (`/api/ui/contributions`) — the merged, plugin-attributed `contributions.ui` declaration set of the cluster's ENABLED plugins (theme token packs today; nav + agent-inspect sections as those slices land). Read straight from each plugin's `ava-plugin.json` — no plugin code is imported to answer it [[okf/plugins/package-manifest.ava.okf.md]]
- **plugin_ui** (`/api/plugin-ui/<plugin>/…`) — the sibling mount of `pages`: static files from an ENABLED plugin's own `ui/` directory, for the sandboxed iframe the console embeds. `pages`' segment validation plus a resolved-path containment check
