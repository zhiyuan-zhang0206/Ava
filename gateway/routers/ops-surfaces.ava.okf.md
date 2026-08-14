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
polled every 5s. `hours` is the aggregation window, whitelisted to
1 / 6 / 24 / 72 / 168 (default 24).

| Card data | Source |
|---|---|
| `live_count` | `agents_meta` — running + idling + starting |
| windowed tokens, average turn duration, warning/error counts | `events` |

No standalone daemon and no cache: the gateway aggregates off the DB on demand.

## Key Dependencies

- [[routers.ava.okf.md]] — the router index these two belong to
- [[shared/log.ava.okf.md]] — the emitter that fills the unified `events` stream, and its partitioning
