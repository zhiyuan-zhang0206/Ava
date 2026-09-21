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
| `plugin_stats` (plugin-declared cards) | `plugin_stats` rows (`shared/plugin_stats.py`), joined by the console against the `contributions.ui.stats` declarations; NOT windowed — a plugin value is a point in time |

No standalone daemon: the gateway aggregates on demand. Loki work runs before
the short Postgres metadata read, so waiting for the global Loki budget never
holds a pooled DB connection. The four telemetry `llm_usage` token/cost sums
are full-window instant aggregates and cache for 60s per requested window to
absorb every other sidebar poll. Turn/warning/error aggregates remain fresh
and merge the shared helper's contiguous, clock-aligned 12h shards for a
longer window. The warning/error section reads per-class counts with the
events-maintenance daemon's grouped query and applies its class arithmetic
(`resolution.level_splits`) over the SELECTED window (task #1935): events
whose class has an active dismissal in `event_dismissals` — an exact
`(category, level, event_name, source, process)` match, or a wildcard row
with an empty `process` (task #4329 B5) — land in `*_dismissed`, the rest in
`*_net`, and dismissed + net == the raw total —
the same cancellation the daemon's fixed-six-hour Grafana gauges apply. The `llm_usage.cost_usd` sum is
the usage-time quote snapshot, not historical tokens repriced against today's
registry. While a last-good response for the window is within
`display.stats_dashboard_stale_max_s`, a failed recompute — a local budget
refusal or a Loki transport/status failure — serves that last-good response
marked `stale` (its `as_of` keeps the original read time) and emits one
rate-capped `stats_dashboard_stale` event; with no last-good response, or
past the cap, both degrade to the pre-existing 503 paths (the global typed
budget envelope / a retriable `Retry-After: 1` after `loki_events` records
the failing query shape).

Every dashboard Loki read is explicitly scoped to the current home-derived
cluster label. The fleet graph's Loki event tail applies the same dimension,
and an unmarked gateway without an explicit Loki URL receives the shared clean
503 instead of reading another home's loopback stack.

`GET /api/agents/{id}/inspect/statistics` owns window-dependent cost, stats,
TPS and activity. It reads one repeatable-read Postgres snapshot over persisted
observations, day summaries and disjoint historical ledger days. Exact percentile
work scales with selected duration observations; additive work uses day summaries
and at most two raw boundary days. Up to four in-flight leaders are admitted and
identical callers share their read; completed results have no TTL. Each SQL query
has a two-second statement limit and the HTTP wait has a 15-second limit. Collection
is explicitly observed, never certified lossless. Missing historical sections and
unknown compact boundaries are unavailable, with declared coverage/precision and
observation timestamps. No runner probe, current-state projection, heartbeat query,
or synchronous Loki scan occurs here.

`GET /api/agents/{id}/inspect/live` exclusively owns the current projection,
notice, runner shell probe, and indexed recent-pause lookup from the durable
Postgres trail. It never queries Loki. An unavailable runner sets
`shells_available=false`. `/inspect/widgets` owns plugin extensions. The three reads load,
render, fail, and retry independently. The old mixed `/inspect` route is absent.

## Key Dependencies

- [[routers.ava.okf.md]] — the router index these two belong to
- [[shared/log.ava.okf.md]] — the emitter that fills the unified `events` stream, and its partitioning
