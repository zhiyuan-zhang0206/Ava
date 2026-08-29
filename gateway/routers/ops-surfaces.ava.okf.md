---
type: doc
title: Ops Surfaces (cluster admin + stats dashboard)
description: The two read/act-on-a-live-cluster contracts worth stating in full — the ssh-free cluster admin endpoints (which deliberately bypass the paused-cluster guard) and the single-round-trip stats dashboard aggregate.
tags:
- gateway
- ops
---

# Ops Surfaces (cluster admin + stats dashboard)

## Cluster admin endpoints

Gateway-only, for ssh-free ops on a deployed cluster. Both **bypass the
paused-posture 503 guard** — deliberately: a stuck `ava update` is exactly when
that guard is on and exactly when you need to look.

- **`GET /api/cluster/admin/events`** — query the unified `events` stream with `agent_id` /
  `service_only` (skips agent-process rows) / `level` / `since` (ISO timestamp) /
  `event` (loguru event-name match) / `grep` (message substring) / `limit`.
  Answers "what did the labeler say in the last 10 minutes" without ssh.
- **`DELETE /api/cluster/machines/{name}`** — drop a stale `machines` row (a
  one-off bench host that is never coming back). **Refuses to delete the
  caller's own row** (`machine_name()` on the gateway process), so a cluster
  cannot delete itself out of its own roster. Idempotent: a name that does not
  exist returns 204.

Neither has an SDK wrapper. They are HTTP-only, called directly over the private
network, because they are operator tools and the SDK surface is for agents.

## `GET /api/stats/dashboard?hours=`

Backs the stat cards at the top of the frontend sidebar in **one** round trip,
polled on one page-wide 30s cadence regardless of how many sidebar consumers
are mounted. `hours` is the aggregation window, whitelisted to
1 / 6 / 24 / 72 / 168 (default 24).

| Card data | Source |
|---|---|
| `live_count`, lifetime event estimate | Postgres metadata |
| windowed tokens, cost, turn duration, warning/error counts | Loki event history |
| warning/error `*_dismissed` / `*_net` split | active `event_dismissals` rows (Postgres) applied to the same window's Loki class counts |

No standalone daemon: the gateway aggregates on demand. Loki work runs before
the short Postgres metadata read, so waiting for the global Loki budget never
holds a pooled DB connection. The four telemetry `llm_usage` token/cost sums
are full-window instant aggregates and cache for 60s per requested window to
absorb every other sidebar poll. Turn/warning/error aggregates remain fresh
and merge the shared helper's contiguous, clock-aligned 12h shards for a
longer window. The warning/error section reads per-class counts with the
events-maintenance daemon's grouped query and applies its class arithmetic
(`resolution.level_splits`) over the SELECTED window (task #1935): events
whose class has an active dismissal in `event_dismissals` land in
`*_dismissed`, the rest in `*_net`, and dismissed + net == the raw total —
the same cancellation the daemon's fixed-six-hour Grafana gauges apply. The `llm_usage.cost_usd` sum is
the usage-time quote snapshot, not historical tokens repriced against today's
registry. A local budget refusal retains the global typed 503 response; a Loki
transport/status failure is a retriable 503 with `Retry-After: 1` after
`loki_events` records the failing query shape.

Every dashboard Loki read is explicitly scoped to the current home-derived
cluster label. The fleet graph's Loki event tail applies the same dimension,
and an unmarked gateway without an explicit Loki URL receives the shared clean
503 instead of reading another home's loopback stack.

`GET /api/agents/{id}/inspect` draws the same boundary per agent: its 75s
single-flight TTL retains only history aggregates. Completed UTC days read the
durable Postgres ledger; duration percentiles and lifecycle/node details stitch
the frozen archive to the retained Loki tail. Live spans are split into <=3h
queries under the shared Loki budget, rather than rescanning one long window.
Every request freshly reads machine, config overlay, status/heartbeat inputs,
liveness and probe timestamp, releases that DB borrow, then joins/loads the
aggregate. Thus both manual refresh and the 60s panel poll see control-plane
changes immediately without re-running the historical fan-out. The interactive
response has a 15s aggregate deadline: budget refusal, deadline expiry, and
Loki transport/status failure are retriable HTTP 503 responses with
`Retry-After`; a synchronous leader already in progress remains the sole
single-flight load and may populate the TTL after its original caller has
received the bounded failure. Async followers await that shared claim without
occupying the gateway's worker pool.

`GET /api/agents/{id}/inspect/live` is the uncached, window-independent half:
fresh DB projection + notice, runner shell probe, and one bounded recent-pause
lookup. Shell failure becomes `[]`; Loki failure drops only the optional pause
hint. Cost, stats, TPS, and activity remain exclusive to `/inspect`.

## Key Dependencies

- [[routers.ava.okf.md]] — the router index these two belong to
- [[shared/log.ava.okf.md]] — the emitter that fills the unified `events` stream, and its partitioning
