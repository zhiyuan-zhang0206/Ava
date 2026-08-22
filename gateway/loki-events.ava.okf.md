---
type: doc
title: "Loki event-history read path"
description: "`gateway/loki_events.py` — the LGTM read side of the unified event stream (task #1197): query_events / count_events / attribute_aggregate over Loki, serving the events routes and the per-agent inspector."
tags:
- gateway
- loki
- observability
---

# Loki event-history read path

## What it is

`gateway/loki_events.py` is the historical read path that replaced the PG
`events` table reads (task #1197 — the LGTM cutover, user decision 2026-08-12:
all event history reads serve from Loki). The write side
(`shared/telemetry_otlp.py`, [[shared/telemetry-otlp/telemetry-otlp.ava.okf.md|OTLP exporter]])
ships every event as an OTLP log whose line body is the full event JSON and
whose top-level fields ride as structured metadata.

## Core responsibilities

- **`query_events()`** — the row list: LogQL `{service_name="unknown_service"}`
  selector → line filters → `| json` → structured-metadata filters →
  `query_range` (backward, newest-first). Every matching line parses back to
  the `AgentEventRow` shape; row `id` is a stable blake2b surrogate over
  (ts, line) — Loki has no numeric id. Offset pages in memory (`limit + offset
  + 1` fetched, `has_more` from the +1 lookahead); the default window is the
  last 24h.
- **`count_events()`** — the exact filtered line count (the `/api/events`
  opt-in `meta.total`, `with_total=1`): `sum(count_over_time(...))` as an *instant* query at the
  window end (range vector `[to - from]`), with `| __error__=""` so count and
  row-parse semantics agree. The earlier "500-series cap" finding was a
  query-shape artifact (missing `sum()`); the sum-wrapped form counts any
  window exactly.
- **`attribute_aggregate()`** — numeric aggregates over one **payload
  attribute** (nested in the event JSON's `attributes` object): `sum`/`min`/
  `max` via `{sum,min,max}_over_time` with the same-named cross-series
  wrapper, `quantile` via a count-by-value distribution + client-side linear
  interpolation (`percentile_cont` semantics — the old SQL used
  `percentile_cont`), `count` for line counts; optional `group_by` on another
  payload attribute (per-model token sums).

## Loki quirks (verified 2026-08-12)

- Structured metadata is NOT index-label matched by `{...}` selectors, but
  **pipeline filters match it directly** — no `| json` stage needed for
  agent_id / event_name / level / category / machine / trace_id filters.
- A plain `| json` flattens the nested `attributes` object into per-line
  labels (`attributes_msg`, ...), and `trace_id`/`span_id` differ per line —
  together they make every event its own series, so per-series range
  aggregations (`quantile_over_time` etc.) are meaningless unless wrapped in
  the same-named cross-series aggregation (`sum(...)`, `min(...)`, `max(...)`).
  The attribute-aggregate pipeline therefore omits the plain `| json` stage.
- Nested extraction needs one `| json k="attributes.k"` stage per key —
  multiple extractions in one stage are a LogQL parse error. `| unwrap` only
  parses inside range aggregations.
- `| agent_id=""` matches a JSON null (service-only rows).
- Loki has no offset and no cheap exact count that honors json-parsed filters
  — hence the in-memory paging and the instant `count_over_time` shape.

## Entry points

- `query_events()` / `count_events()` / `attribute_aggregate()` — called by
  `gateway/routers/agent_events.py`, `gateway/routers/cluster.py`,
  `gateway/routers/events.py`, `gateway/routers/agent_inspect.py`.

## Notes

- `AVA_TELEMETRY_LOKI_URL` (default `http://127.0.0.1:3100`,
  restart_required gateway) points at the single-binary Loki HTTP port.
- Every query runs through one long-lived module-level `httpx.Client`
  (the lazy `_client()` accessor — the seam tests swap): connection reuse
  across the gateway's fan-out reads instead of a TCP connection per query.
- Every HTTP query also crosses the process-wide FIFO budget in
  `gateway/loki_query_budget.py`: four active slots, matching Loki's deployed
  `querier.max_concurrent`, plus a bounded waiter queue and 10s acquisition
  deadline. Inspect, stats, events, and ops therefore share one backpressure
  boundary; timeout/cancellation always releases the slot. `queue_full` and
  `acquire_timeout` are typed local refusals, mapped uniformly to retriable
  HTTP 503 without emitting the transport-only `loki_query_failed` event.
  Every queue/acquire/release/reject transition emits the registered
  `loki_query_budget` telemetry/metric shape (active, queued, high-water,
  wait-ms and outcome deltas); the observer only enqueues into telemetry and
  never calls DB/Loki or runs while the budget lock is held. Routes stage their
  Postgres reads outside this boundary, so queueing for Loki does not hold a
  pooled database connection.
- Tests: `tests/gateway/test_loki_events.py` (httpx-faked unit tests for all
  three functions), `tests/gateway/test_agent_inspect.py` (route tests with
  an in-memory `_FakeLoki`).
- Parent node: [[gateway.ava.okf.md|Gateway]]; write side:
  [[shared/telemetry-otlp/telemetry-otlp.ava.okf.md|OTLP exporter]].
