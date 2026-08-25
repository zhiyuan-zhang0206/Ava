---
type: doc
title: "Loki event-history read path"
description: "`gateway/loki_events.py` — the cluster-scoped LGTM read side of the unified event stream: query_events / count_events / attribute_aggregate over Loki, with unmarked gateways refusing the implicit loopback backend."
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
whose top-level fields, including the home-derived `cluster`, ride as
structured metadata.

## Core responsibilities

- **`query_events()`** — the row list: LogQL `{service_name="unknown_service"}`
  selector → line filters → `| json` → `cluster=<this home> or cluster=""`
  (the empty branch retains this single-cluster Loki's pre-labeling history)
  plus other
  structured-metadata filters →
  `query_range` (backward, newest-first). Every matching line parses back to
  the `EventRow` shape; row `id` is a stable blake2b surrogate over
  (ts, line) — Loki has no numeric id. Offset pages in memory (`limit + offset
  + 1` fetched, `has_more` from the +1 lookahead); the default window is the
  last 24h.
  `/api/events?tier=` adds one derived LogQL predicate that ORs requested
  tiers before paging (and drops JSON parse errors first), so its list and
  exact-count paths keep the same tier membership.
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
- **Aggregation result cache** — `count_events()` and
  `attribute_aggregate()` share successful results for 60 seconds. Window
  bounds are keyed on a minute grid so repeated 30-second dashboard polls and
  common clock-aligned shards reuse the same Loki result; concurrent misses for
  one key share the leader's result, while bounded waiters fall back to their
  own query if the leader never finishes. Failures are never cached, and
  grouped results are copied before returning to callers.
- **Read gate** — a gateway home without `lgtm-host` refuses the implicit
  loopback Loki URL before any HTTP call. The gateway maps the typed refusal to
  HTTP 503. An explicit `AVA_TELEMETRY_LOKI_URL` is the operator escape hatch;
  pure runners and role-less maintenance processes retain their existing
  behavior.

## Loki quirks (verified 2026-08-12; label exceptions 2026-08-23)

- Structured metadata is NOT index-label matched by `{...}` selectors, but
  **pipeline filters match it directly** — no `| json` stage needed for
  level / category / machine / trace_id filters. `agent_id` and `event_name`
  are the exceptions since the 2026-08-23 index-label cutover (Task #1407
  B2): the collector promotes them to stream labels, so indexed-era slices
  match them inside `{...}` (see `shared/loki_index_labels.py`); pre-cutover
  rows keep the pipeline-filter form until legacy retention expires
  2026-08-30.
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
  restart_required gateway) points at the single-binary Loki HTTP port. The
  default is valid only for the marked LGTM gateway home.
- Every query runs through one long-lived module-level `httpx.Client`
  (the lazy `_client()` accessor — the seam tests swap): connection reuse
  across the gateway's fan-out reads instead of a TCP connection per query.
- Every HTTP query also crosses the gateway process's FIFO singleton in
  `gateway/loki_query_budget.py`: its reusable state machine lives in
  `shared/loki_query_budget.py`; the gateway adapter supplies four active slots, matching Loki's deployed
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
