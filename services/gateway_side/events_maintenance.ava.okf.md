---
type: doc
title: Events Maintenance — unified event-stream maintenance (partitions / rollup / retention / ops pre-agg)
description: Gateway-owned maintenance daemon over the unified `events` stream — monthly partition rolling (events + legacy mirrors), since-birth day-grain rollup, category retention (audit 365d / telemetry 90d / log 30d). The ops_metrics pre-aggregation was retired with the ops-monitor LGTM migration (task #1197).
tags: []
---

# Events Maintenance — unified event-stream maintenance

## What it is
Gateway-owned background daemon (`services/events_maintenance/daemon.py`), the structural maintenance piece over the **unified `events` stream** (the single event river; the legacy `agent_events` / `event_log` tables are write mirrors that stay out of scope until the migration completes). The stream is append-only and unbounded (~650MB/month), **declaratively partitioned by month** (`PARTITION BY RANGE (ts)`, PK `(id, ts)`); since-birth aggregates like fleet_graph's total_tokens, agent_inspect's cumulative cost/tokens/turn/exec statistics that read the whole stream must survive before old partitions are dropped. This daemon runs every `AVA_EVENTS_MAINTENANCE_INTERVAL_SECONDS` (default 1h): first ensures current/next month partitions exist for `events` + `agent_events` (and carves any months stranded in DEFAULT), then applies the retention policy (`services/events_maintenance/retention.py`), then upserts fully-capped whole days into two day-grain tables. The retention slice **does delete data — that is its job**: it drops `events` month partitions whose categories have all outlived their retention and prunes the expired categories from still-live ones (audit 365d / telemetry 90d / log 30d by default, tunable via `AVA_EVENTS_RETENTION_{AUDIT,TELEMETRY,LOG}_DAYS`).

**Role home**: gateway side (pure agent-runner does not run) — `ops/spec.py`'s `ServiceSpec.capabilities=_GATEWAY`. The daemon ALWAYS runs on the gateway: its checkpoint reaper (Rule A fast loop + Rule B hourly, Task #1057/#1125) and blob vacuum are unconditional — checkpoint_blobs grows ~150MB/h without them (2026-08-12 design regression, fixed 2026-08-13). The events-archive slices (partition rolling / retention / rollup / index governance / resolved markers) are gated INSIDE the daemon by `AVA_EVENTS_MAINTENANCE_ENABLED` (`settings.daemon.events_maintenance_enabled`), off by default since the LGTM cutover (#1197): the PG `events` copy is a read-only archive and nothing rolls it up.

## Core responsibilities
- **Monthly partition roll** (`services/events_maintenance/partitions.py:ensure_month_partitions`): each round ensures current+next month partitions exist, and drains all stranded months in DEFAULT (catching up after a cross-month interruption), so writes never land in the DEFAULT catch-all partition and each month is a droppable whole chunk. Uses an **independent pool connection and commits before rollup** — sharing a non-autocommit connection would hold CREATE PARTITION locks until the (potentially slow) rollup ends, blocking writes, and a rollup failure would roll back the newly created partitions. Idempotent: already covered (including being covered by a broad legacy partition, overlap treated as covered without error) → no-op; DEFAULT already has rows for that month → detach/create/move/reattach carve.
- **Two rollup tables**: `agent_metrics_daily` (per agent × UTC-day turn/exec counts + mergeable turn-duration sum/min/max), `agent_model_tokens_daily` (per agent × UTC-day × model token counts; cost re-derived from tokens, not stored). Both are mergeable day-grain aggregates; p50/p90 are deliberately excluded (not mergeable across days). Both have corresponding `.down.sql` in `db/schema.sql`.
- **Single aggregation source**: `services/events_maintenance/rollup.py:compute_rollup` is the sole aggregation implementation shared by both backfill and steady-state scenarios — field extraction logic mirrors the live readers (fleet_graph / agent_inspect) to ensure rollup and live read paths are consistent and diff-verifiable.
- **Whole-day covering upsert**: only aggregates up to yesterday (UTC) full days; today is still read directly from the raw `events` stream by readers. Each upsert recomputes the full day by PK, thus idempotent — restarts or too-frequent intervals won't double-count.
- **Lookback window**: each round additionally recomputes the last `_LOOKBACK_DAYS` (3) already-capped days, absorbing late-arriving writes that land in closed days.
- **Liveness heartbeat**: rollup runs in a thread; main loop beats a liveness every 30s to prevent watchdog from falsely declaring a hang during the first full backfill that may take a long time.
- **Category-aware retention** (`services/events_maintenance/retention.py:apply_retention`): drops an `events` month partition once every category in it has outlived its retention; prunes only the expired categories (DELETE) from partitions still holding live audit data. Exact because retention is computed from `ts` and a partition is a contiguous `ts` range — all rows of one category in one partition expire at the same moment. Unknown categories block the whole-partition drop but not the pruning of known ones; DEFAULT and non-month partitions are never touched. Idempotent; reports every drop/prune with row counts (daemon logs them).

## Key dependencies
- [[db.ava.okf.md]] — reads `events`, writes the two rollup tables
- [[log.ava.okf.md]] — `events` write source (this daemon only reads the stream, never writes)

## Entry points
- `services/events_maintenance/daemon.py` — `.venv/bin/python -m services.events_maintenance.daemon`
- `services/events_maintenance/partitions.py:ensure_month_partitions` — monthly partition roll
- `services/events_maintenance/rollup.py:compute_rollup` — aggregation core (backfill + steady-state shared)
- Watchdog keeps alive via `services/healthchecks/events_maintenance.py`

## Notes
- This slice does not modify any reader (cutover of since-birth readers to rollup is a later slice); the unified `events` retention (drop/prune by category, Wave 2 W5) is live on the daemon loop, while the legacy `agent_events` / `event_log` mirrors stay out of scope until the migration completes
- Sidebar `total_events` changed to `SUM` of each leaf partition’s `pg_class.reltuples` because the partitioned parent table’s own reltuples is not maintained by autovacuum
