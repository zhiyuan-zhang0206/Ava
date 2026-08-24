---
type: doc
title: Events Maintenance — cost-ledger rollup + class resolution
description: Gateway-owned maintenance daemon — hourly Loki→PG day-grain rollup (the durable token+cost ledger), fixed-window immutable-Loki warning/error class resolution gauges, unconditional checkpoint reaper / blob vacuum, and the flag-gated PG events-archive slices (partition rolling, retention, index governance). The ops_metrics pre-aggregation was retired with the ops-monitor LGTM migration (task #1197).
tags: []
---

# Events Maintenance — cost-ledger rollup + class resolution

## What it is
Gateway-owned background daemon (`services/events_maintenance/daemon.py`). Since the LGTM cutover (#1197) the live event stream lives in **Loki** (168h retention) and the PG `events` table is a frozen read-only archive; this daemon's steady-state job is to make the short-lived Loki stream durable: every `AVA_EVENTS_MAINTENANCE_INTERVAL_SECONDS` (default 1h) it count-probes retained whole UTC days and aggregates only dirty days into `agent_metrics_daily` and `agent_model_tokens_daily`, the cluster's **durable token + cost ledger**. It also hosts the checkpoint reaper (Rule A fast loop + Rule B hourly) and the blob vacuum.

**Role home**: gateway side (pure agent-runner does not run) — `ops/spec.py`'s `ServiceSpec.capabilities=_GATEWAY`. The daemon ALWAYS runs on the gateway, and three responsibilities are unconditional: the **cost-ledger rollup** (Loki only retains 168h — a pass that does not run permanently loses days), the **checkpoint reaper** and the **blob vacuum** (checkpoint_blobs grows ~150MB/h without them; 2026-08-12 design regression, fixed 2026-08-13). The events-**archive** slices (partition rolling / retention / table retention / index governance) are gated INSIDE the daemon by `AVA_EVENTS_MAINTENANCE_ENABLED` (`settings.daemon.events_maintenance_enabled`), off by default since the cutover: the PG `events` copy is a read-only archive.

## Core responsibilities
- **Loki→PG day-grain rollup** (`services/events_maintenance/rollup.py:compute_rollup`, unconditional): `_day_source_count` uses one union-family LogQL count per era slice to compare each candidate with `rollup_day_state.source_count`; missing state, failed state, count changes, and the latest `AVA_EVENTS_ROLLUP_LATE_WRITE_LOOKBACK_DAYS` (default 1) are dirty and reach `_day_aggregates`. The first pass after migration catches up the retained window once; clean steady-state days avoid the fourteen full aggregate queries. Dirty days full-overwrite `agent_metrics_daily` (per agent × UTC-day turn/exec counts + mergeable duration sum/min/max) and `agent_model_tokens_daily` (per agent × UTC-day × model token/cost snapshot ledger) by PK, so reruns never double-count. A zero-row indexed slice preserves existing ledger rows and records failed state for retry; probe-zero plus empty aggregates records a legitimate rolled-empty day. `AVA_EVENTS_ROLLUP_PASS_DEADLINE_S` (default 1200) stops between day operations and leaves the remainder untouched for the next pass.
- **Low-priority Loki admission**: rollup and class-resolution queries share the events-maintenance process's own `FairQueryBudget(capacity=1, max_waiters=4, wait_timeout_s=30)` from `shared/loki_query_budget.py`. It is deliberately independent from the gateway process's four-slot user-read singleton, so the daemon can never fan out more than one Loki query at a time.
- **Retention-floor clamp**: a day is recomputable only while its 00:00 start is inside Loki's 168h retention; older days are never overwritten (this protects the one-time pre-LGTM archive backfill written by the `llm-cost-rollup-columns` migration — the frozen archive's last-ever code reader). A progress gap exceeding retention is logged loudly: those days are not aggregatable and stay missing (the JSONL mirror is the manual recovery source).
- **Readers**: window requests (≤ `StatsWindowHours` = 168h) are pure Loki; whole-life = ledger + Loki tail from the watermark (midnight after the newest rolled day) — `gateway/routers/_agent_cost.py`.
- **Archive slices (flag-gated)** — for clusters that still maintain the PG archive: monthly partition roll (`services/events_maintenance/partitions.py:ensure_month_partitions` — current+next month, drains months stranded in DEFAULT; idempotent carve), category-aware retention (`services/events_maintenance/retention.py:apply_retention` — drops a month partition once every category outlived its retention, prunes expired categories from live ones; audit 365d / telemetry 90d / log 30d via `AVA_EVENTS_RETENTION_{AUDIT,TELEMETRY,LOG}_DAYS`), table retention, index-bloat governance (REINDEX CONCURRENTLY on its own direct autocommit connection). Each step runs on a separate pool connection so partition DDL commits before the potentially long rollup.
- **Class resolution** (every `AVA_EVENTS_RESOLUTION_INTERVAL_SECONDS`, default 5m) — queries one grouped fixed-six-hour Loki window, subtracts active rows in `event_dismissals`, and emits the absolute `ava_resolution_status_unresolved_warnings` / `ava_resolution_status_unresolved_errors` Prometheus gauges. Loki lines remain immutable: resolution never mutates a historical event. A second ten-minute grouped read reopens an active class when its count exceeds `AVA_EVENTS_RESOLUTION_BURST_THRESHOLD` (default 5), recording the burst and emitting the reopen marker. Manual authenticated gateway API marking is primary; `AVA_EVENTS_AUTO_DISMISS_ENABLED` is an off-by-default daily stable-class scan.
- **Per-loop progress health**: dispatch, checkpoint trim, and class resolution each own a progress tracker with a hard deadline (`AVA_EVENTS_MAINTENANCE_PASS_DEADLINE_S`, `AVA_EVENTS_MAINTENANCE_TRIM_DEADLINE_S`, `AVA_EVENTS_MAINTENANCE_RESOLUTION_DEADLINE_S`; defaults 1500s / 300s / 600s). Only completed bounded work units and bounded inter-run sleeps beat; a timed-out worker permanently wedges its tracker, parks without retrying, and makes the aggregate `/healthz` return 503 even while sibling loops remain healthy. The payload exposes each loop's progress age, last success, last error, and wedge state so the watchdog restart is attributable.

## Key dependencies
- [[db.ava.okf.md]] — writes the two rollup tables; archive slices maintain the frozen `events` partitions
- Loki HTTP API (`AVA_TELEMETRY_LOKI_URL`) — the rollup's aggregation source (LogQL day aggregates, queried directly from `rollup.py`)

## Entry points
- `services/events_maintenance/daemon.py` — `.venv/bin/python -m services.events_maintenance.daemon`
- `services/events_maintenance/rollup.py:compute_rollup` — aggregation core (steady-state; the migration backfill was one-shot SQL)
- `services/events_maintenance/resolution.py:run_resolution_slice` — immutable Loki class resolution, marker transitions, and unresolved gauges
- `services/events_maintenance/partitions.py:ensure_month_partitions` — monthly partition roll (gated)
- Watchdog keeps alive via `services/healthchecks/events_maintenance.py`

## Notes
- Sidebar `total_events` changed to `SUM` of each leaf partition's `pg_class.reltuples` because the partitioned parent table's own reltuples is not maintained by autovacuum
