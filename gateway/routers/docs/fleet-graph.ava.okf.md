---
type: doc
title: Fleet graph
description: Fleet graph reads, fallback caches, and stale-event ownership.
---

# Fleet graph

`GET /api/fleet/graph` reads agent nodes, token totals, and lifecycle edges
from Postgres. The response can select a time window and include terminated
agents. A telemetry heartbeat guard independently marks telemetry degradation.

Successful responses use a 60-second Redis poll cache and a seven-day
last-good cache. Redis failures fall back to direct reads. When a graph read
fails, the route serves a complete last-good response when available, or known
nodes with empty edges. Degraded responses bypass the poll cache so the next
request retries the upstream read.

The gateway lifespan owns one `FleetGraphStaleEmitter` in
`app.state.fleet_graph_stale_emitter`. It limits stale events independently by
reason for the configured warning interval. Requests in the same application
share that budget; a separate application lifespan starts with its own budget.
The emitter retains a lock because route workers can report concurrently.
