# Every reader of telemetry and log events computes on `telemetry_events`; Loki is a droppable projection

## Context

[`2026-10-02-telemetry-events-in-postgres.md`](2026-10-02-telemetry-events-in-postgres.md) made
`telemetry_events` the record and moved the run timeline, the inspector and `/api/events` onto it. The
stats dashboard's token and cost tails, the `/api/metrics` aggregates, the ops monitor series and the
events-maintenance rollup and resolution passes still aggregated in Loki. Each carried machinery that
existed only because Loki is a poor aggregation engine: a result cache with in-flight sharing, a
process query budget, pagination of per-day windows under a 90-day query ceiling, and an 84-hour
retention clamp. The user asked for Loki to become a projection that can be dropped, with Grafana
untouched.

## Decision

- Every such reader is a SQL aggregate over `telemetry_events` (or `audit_events` for lifecycle
  rows), in one connection under a statement timeout (a timeout is a retriable 503). Percentiles and
  distributions are exact over the rows of a bucket or window, not histogram approximations.
- The caches, the budget slots and the fan-out that fed them are deleted from the gateway readers.
  The only Loki read left is `query_events`, the live source of the backfill scripts; its transport
  keeps the process budget.
- A covering index `(ts) INCLUDE (agent_id, event_name)` answers the per-agent and per-name counts
  from the index; a partial index on `level IN ('warning', 'error', 'critical')` answers the anomaly
  counts without reading the info rows that make up most of the table.
- The rollup recomputes closed UTC days from `telemetry_events` into the existing ledger tables
  under a monotone guard (a recompute never lowers a ledger row), so a gap in the table cannot erase
  a day that was already summed.

## Alternatives rejected

- **Keep a cache in front of the SQL.** The windows are selected and refreshed by hand or polled at
  tens of seconds; a covering index plus a statement timeout bounds the cost, and a cache would
  bring back the staleness and the single-flight code that Loki needed.
- **Materialized views or continuous aggregates for the dashboards.** They need a refresh policy
  per window and diverge from the registry-driven reads; add one when a measured query needs it.
- **Read Loki and the table side by side and compare.** Loki's 84-hour window and the table's full
  history differ by design, so a comparison would only measure the retention gap.

## Consequences

- The stats dashboard's 168-hour window is no longer clamped to Loki's retention.
- `/api/metrics` windows beyond three and a half days report real data.
- The exec failure outcomes are the names the registry lists (`exec_failed`, `exec_timeout`,
  `exec_cancelled`, `exec_node_timeout` and their `exec(...)` spellings).
- The rollup and the resolution pass depend on the backfill being complete for the days they read;
  the monotone guard keeps an incomplete day from lowering a ledger row.
- `rollup_day_state` and `agent_metric_scans` hold nothing the readers use; dropping them follows in
  its own expand-contract step (migration `20261003T043232_drop-dead-tables`, with `agent_archive_stats` and
  `llm_usage_hourly`, whose last readers and writers went in the same sweep).
- The fleet graph still reads Prometheus.
