# Metrics read path — extract payload fields in SQL, not a new index

## Context

`GET /api/metrics` (the settings Metrics tab, compute in `shared/metrics.py`)
took 30s+ for the 3/7-day windows. The first read blamed a missing `ts` index —
"every query full-table scans." Measurement against the ~3.35M-row prod
`agent_events` said the scan was never the problem.

## Decision

Push payload-field extraction into SQL. `query_events` now SELECTs each payload
field a metric actually reads as a **typed scalar column** — the large `body` is
reduced to `length(body)` server-side (units only ever measure it), except on
`halt` rows where the short body text is kept for compact/idle detection — rather
than SELECTing the raw JSONB `payload`. No new index, no migration, no
materialized view. The pure-function metric-unit architecture and every output
number are unchanged (`EventRow` is now the typed result of that projection;
units read `e.in_total` instead of `e.payload["in_total"]`).

## Alternatives rejected

- **Add a `ts` index.** EXPLAIN put PG-side execution at ~310ms; the 7-day
  window is ~17% of the table, so the planner correctly picks a parallel seq
  scan and would ignore a plain `ts` index. The real cost is client-side:
  transferring ~128 MB of JSONB per week and psycopg parsing ~580K rows into
  dicts (~5s locally, amplified to 30s in prod by network + GIL under load). An
  index treats a cost that isn't there.
- **Materialized view / rollup table.** `agent_events` is append-only and the
  windows are arbitrary (1/3/7d) plus a `since_compact` filter; a rollup would
  need continuous maintenance and still not cheaply serve the position-bucketed
  (early/mid/late thirds) and kind-parsed metrics. Once the rows are narrow the
  per-request Python aggregation is <0.5s — not worth the operational surface.
- **Narrow the fetch to only the ~9 consumed event types** (node_enter/exit/log
  are ~78% of rows and no metric reads them). Another ~2x, but `total_events` /
  `distinct_agents` / `agent_lifetime` count *all* events, and `since_compact`
  would force the compact-cutoff to be duplicated in SQL. Deferred as a
  follow-up; the chosen change keeps every number provably identical.

## Consequences

- PG does more per row (13 field extractions + casts): 7d execution rose ~310ms
  → ~755ms, but the sort spill halved (no bodies in the sorted rows) and the
  client fetch dropped ~4.7s → ~2.0s; net read path ~5.2s → ~2.3s locally, and
  the wire shrank ~128 MB → ~20 MB — the factor that amplifies badly under prod
  network/load.
- The SQL projection and the test `ev()` helper are the two places that must
  agree on extraction semantics (defaults, `length(body)`, halt-only body). The
  endpoint integration tests (`tests/gateway/test_metrics_router.py`) exercise
  the real SQL path, so a divergence reds.
- A future metric needing a new payload field adds one line to the projection
  and one to `EventRow` — same place, still "add a metric = one function."
