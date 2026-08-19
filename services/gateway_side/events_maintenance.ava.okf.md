---
type: doc
title: Events Maintenance — cost-ledger rollup + archive maintenance
description: Gateway-owned maintenance daemon — hourly Loki→PG day-grain rollup (the durable token+cost ledger), unconditional checkpoint reaper / blob vacuum, and the flag-gated PG events-archive slices (partition rolling, retention, index governance). The ops_metrics pre-aggregation was retired with the ops-monitor LGTM migration (task #1197).
tags: []
---

# Events Maintenance — cost-ledger rollup + archive maintenance

## What it is
Gateway-owned background daemon (`services/events_maintenance/daemon.py`). Since the LGTM cutover (#1197) the live event stream lives in **Loki** (168h retention) and the PG `events` table is a frozen read-only archive; this daemon's steady-state job is to make the short-lived Loki stream durable: every `AVA_EVENTS_MAINTENANCE_INTERVAL_SECONDS` (default 1h) it aggregates whole UTC days from Loki into the two day-grain rollup tables — `agent_metrics_daily` and `agent_model_tokens_daily`, the cluster's **durable token + cost ledger** — so whole-life statistics survive Loki's retention horizon. It also hosts the checkpoint reaper (Rule A fast loop + Rule B hourly) and the blob vacuum.

**Role home**: gateway side (pure agent-runner does not run) — `ops/spec.py`'s `ServiceSpec.capabilities=_GATEWAY`. The daemon ALWAYS runs on the gateway, and three responsibilities are unconditional: the **cost-ledger rollup** (Loki only retains 168h — a pass that does not run permanently loses days), the **checkpoint reaper** and the **blob vacuum** (checkpoint_blobs grows ~150MB/h without them; 2026-08-12 design regression, fixed 2026-08-13). The events-**archive** slices (partition rolling / retention / table retention / index governance / resolved markers) are gated INSIDE the daemon by `AVA_EVENTS_MAINTENANCE_ENABLED` (`settings.daemon.events_maintenance_enabled`), off by default since the cutover: the PG `events` copy is a read-only archive.

## Core responsibilities
- **Loki→PG day-grain rollup** (`services/events_maintenance/rollup.py:compute_rollup`, unconditional): aggregates whole retained days up to yesterday (UTC) from Loki (`_day_aggregates` is the single Loki-facing seam) into `agent_metrics_daily` (per agent × UTC-day turn/exec counts + mergeable turn-duration sum/min/max; p50/p90 deliberately excluded — not mergeable across days) and `agent_model_tokens_daily` (per agent × UTC-day × model token counts + `cost_usd` = sum of usage-time price snapshots, `costed_calls` / `unpriced_calls` — **no read-time pricing**; a snapshot-less call is counted unpriced at zero cost). Full-day overwrite upsert keyed on the PK ⇒ idempotent; restarts or fast intervals never double-count.
- **Retention-floor clamp**: a day is recomputable only while its 00:00 start is inside Loki's 168h retention; older days are never overwritten (this protects the one-time pre-LGTM archive backfill written by the `llm-cost-rollup-columns` migration — the frozen archive's last-ever code reader). A progress gap exceeding retention is logged loudly: those days are not aggregatable and stay missing (the JSONL mirror is the manual recovery source).
- **Readers**: window requests (≤ `StatsWindowHours` = 168h) are pure Loki; whole-life = ledger + Loki tail from the watermark (midnight after the newest rolled day) — `gateway/routers/_agent_cost.py`.
- **Archive slices (flag-gated)** — for clusters that still maintain the PG archive: monthly partition roll (`partitions.py:ensure_month_partitions` — current+next month, drains months stranded in DEFAULT; idempotent carve), category-aware retention (`retention.py:apply_retention` — drops a month partition once every category outlived its retention, prunes expired categories from live ones; audit 365d / telemetry 90d / log 30d via `AVA_EVENTS_RETENTION_{AUDIT,TELEMETRY,LOG}_DAYS`), table retention, index-bloat governance (REINDEX CONCURRENTLY on its own direct autocommit connection), resolved-marker consumption. Each step runs on a separate pool connection so partition DDL commits before the potentially long rollup.
- **Liveness heartbeat**: the maintenance pass runs in a thread; the main loop beats liveness every 30s so the watchdog never falsely declares a hang during a long pass.

## Key dependencies
- [[db.ava.okf.md]] — writes the two rollup tables; archive slices maintain the frozen `events` partitions
- Loki HTTP API (`AVA_TELEMETRY_LOKI_URL`) — the rollup's aggregation source (LogQL day aggregates, queried directly from `rollup.py`)

## Entry points
- `services/events_maintenance/daemon.py` — `.venv/bin/python -m services.events_maintenance.daemon`
- `services/events_maintenance/rollup.py:compute_rollup` — aggregation core (steady-state; the migration backfill was one-shot SQL)
- `services/events_maintenance/partitions.py:ensure_month_partitions` — monthly partition roll (gated)
- Watchdog keeps alive via `services/healthchecks/events_maintenance.py`

## Notes
- Sidebar `total_events` changed to `SUM` of each leaf partition's `pg_class.reltuples` because the partitioned parent table's own reltuples is not maintained by autovacuum
