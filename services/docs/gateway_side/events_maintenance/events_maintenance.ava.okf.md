---
type: doc
title: Events Maintenance — cost-ledger rollup + class resolution
description: Gateway-owned maintenance daemon for the durable token and cost ledger, immutable-Loki resolution metrics, checkpoint size monitoring, and blob vacuum.
tags: []
---

# Events Maintenance — cost-ledger rollup + class resolution

## What it is
Gateway-owned background daemon (`services/events_maintenance/daemon.py`). The telemetry and log record lives in `telemetry_events` (Postgres, permanent); this daemon keeps the derived tables and the record itself in step with it: every `AVA_EVENTS_MAINTENANCE_INTERVAL_SECONDS` (default 1h) it recovers missed observations, replays this machine's JSONL mirror into `telemetry_events`, and recomputes the last eight closed UTC days into `agent_metrics_daily` and `agent_model_tokens_daily`, the cluster's **durable token + cost ledger**. Checkpoint trim opt-in retired 2026-09-30; its implementation remains unscheduled.

**Role home**: gateway side (pure agent-runner does not run) — `ops/spec.py`'s `ServiceSpec.capabilities=_GATEWAY`. The daemon ALWAYS runs on the gateway, and its responsibilities are unconditional: the **cost-ledger rollup**, the **observation and mirror recoveries**, the **checkpoint size sample**, and the **blob vacuum**. VACUUM reclaims physical space without deleting live checkpoint history.

## Core responsibilities
- **Day-grain rollup** (`services/events_maintenance/rollup.py:compute_rollup`, unconditional): each pass recomputes the last eight closed UTC days (`RECOMPUTE_DAYS`, covering the mirror replay's seven-day lateness) from `telemetry_events` with two idempotent full-day overwrite statements per day: `agent_model_tokens_daily` (per agent × day × model: calls, token sums, usage-time cost, costed/unpriced calls; `estimated_calls` is never written) and `agent_metrics_daily` (per agent × UTC-day turn/exec counts, exact turn-duration sum/min/max, and a mergeable `floor(duration_seconds)` histogram for p50/p90). A monotone guard (`ON CONFLICT … WHERE` the recompute has at least as many calls, turns and execs) means a day that `telemetry_events` holds only in part never lowers a ledger row. The operator CLI `python -m services.events_maintenance.rollup --from YYYYMMDD --to YYYYMMDD` recomputes a range after a backfill.
- **Observed Inspector metrics**: persisted typed projection and recoverable source replay — [[observed-metrics.ava.okf.md]].
- **Telemetry events replay**: the hourly mirror → `telemetry_events` pass — [[telemetry-replay.ava.okf.md]].
- **Class resolution** (5m cadence) — counts the warning, error and critical rows of one fixed six-hour window in `telemetry_events` (the partial index `telemetry_events_anomaly_ts`; it emits no gauge when the newest recorded event is over 15 minutes old) (grouped by `category, level, event_name, source, process`; pre-dimension rows read as the empty label), subtracts active rows in `event_dismissals` (exact process match, or a wildcard `process = ''` row cancelling every process of its class — task #4329 B5), and emits the absolute `ava_resolution_status_unresolved_warnings_ratio` / `ava_resolution_status_unresolved_errors_ratio` / `ava_resolution_status_dismissed_warnings_ratio` / `ava_resolution_status_dismissed_errors_ratio` Prometheus gauges (with the dashboard `Warning` / `Error` tiles they render the total / resolved / net trio). The same `resolution.level_splits` arithmetic backs the dashboard's selected-window split (task #1935). Recorded events remain immutable: resolution never mutates a historical event. A second ten-minute read reopens a dismissal whose count exceeds `AVA_EVENTS_RESOLUTION_BURST_THRESHOLD` (default 5; exact rows watch their process, wildcards the base-wide sum). Manual API marking is primary; `AVA_EVENTS_AUTO_DISMISS_ENABLED` (off by default) is the daily stable-class scan.
- **Retained checkpoint trim implementation** (unscheduled) — [[checkpoint-retention.ava.okf.md]].
- **Checkpoint physical-size monitoring** (`services/events_maintenance/blob_vacuum.py`) — each actual plain-vacuum pass measures `checkpoint_blobs`, `checkpoints`, and `checkpoint_writes` with `pg_total_relation_size` and emits the latest values through the existing OTLP metric path as absolute gauges. Grafana warns at 2.5 GiB and errors at 4 GiB for `checkpoint_blobs`; measurements refresh only in the 05:00-08:00 window or on a force run, so an alert lasts through the daily sampling gap. The alerts prompt an operator-led repack/capacity decision before the disk watermark; they do not change vacuum behavior.
- **Configuration**: `services/events_maintenance/daemon.py:events_maintenance_config` is the package's only `settings` read; it builds the frozen `EventsMaintenanceConfig` (`config.py`) and passes it to the loops and passes (`settings-read` gate).
- **Per-loop progress health**: dispatch and class resolution own separate progress trackers with hard deadlines; a timed-out worker wedges its tracker and makes the aggregate `/healthz` return 503 while the sibling loop stays healthy — [[progress-health.ava.okf.md]].

## Key dependencies
- [[db.ava.okf.md]] — writes the two rollup tables (the frozen `events` archive was dropped; see the task #1281/#1823 cleanup)
- `telemetry_events` — the source of the rollup, the class counts and the observation recovery

## Entry points
- `services/events_maintenance/daemon.py` — `.venv/bin/python -m services.events_maintenance.daemon`
- `services/events_maintenance/rollup.py` — also the operator CLI for a range of days
- `services/events_maintenance/resolution.py:run_resolution_slice` — immutable Loki class resolution, marker transitions, and the unresolved/dismissed gauges
- Root's health monitor keeps it alive via the roster's `/healthz` identity probe (`ops/roster/healthz.py`)

## Notes
- Sidebar `total_events` is a historical parity constant (see `gateway/cluster/status.py` `ARCHIVE_TOTAL_ROWS`) — the PG archive it once counted was dropped with the task #1281/#1823 cleanup
