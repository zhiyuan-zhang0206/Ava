---
type: doc
title: Ops Monitor Router
description: "GET /api/ops/monitor — the Insights Ops panel's single-round-trip time-bucketed series over Loki + Prometheus (the LGTM stack, task #1197): SSE/event-log backlog, LLM latency + TPS, process restart counts."
tags:
- gateway
- ops
- observability
---

# Ops Monitor Router

`GET /api/ops/monitor?window=1h|6h|24h|7d` — one round trip backs the whole
Insights Ops section (task #672, user-chartered 2026-08-03). Query core is
`gateway/ops_series_lgtm.py` (range/instant queries over Loki + Prometheus,
parallel fan-out; the pre-aggregated `ops_metrics` PG table was retired with
the ops-monitor migration, task #1197); schemas are `gateway/schemas/ops.py`.

## Contract

- **Response**: `{meta, sse, llm, restarts}` — meta carries `bucket_starts`
  (ISO UTC, oldest first, origin-anchored to the fixed 2000-01-01 grid); every
  series array is zero-filled across the whole window and positionally aligned
  to it via its `bucket` index. No caching — each call fans out a handful of
  small range/instant queries, so a refresh reflects events up to the query's
  `now()` anchor (the in-progress bucket is partial).
- **Buckets**: window → bucket seconds: 1h→60, 6h→300, 24h→1800, 7d→3600
  (fixed point counts 60/72/48/168). `window` is capped at 7d to bound the
  query volume.

## Metric groups (MVP)

| Group | Reads | Answers |
|---|---|---|
| `sse` | `sse_drop` (kind=queue_full/publish_error) + `event_log_drop` events (Loki counts) | how much did the live-view pipes back up per bucket |
| `llm` | `ava_llm_usage_*` Prometheus instruments (calls/tokens via `increase`, p50/p95 via `histogram_quantile`) + `llm_usage.latency_ms` Loki unwrap max + LLM error-family events | latency + throughput + error rate per bucket |
| `restarts` | `agent_restarted` + `service_started` (Loki counts + grouped breakdowns; agent labels from the `agents` registry) | which processes restarted, when, how often |

Fidelity vs the retired PG reader: counts are Prometheus `increase`
extrapolations rounded to int (a counter that started mid-bucket is spread
across the bucket); p50/p95 are histogram approximations (as before); max
latency and the service/agent breakdowns stay exact.

Instrumentation points (collection layer, all on the existing loguru →
unified emitter (`shared/telemetry.py`) → OTLP export, zero schema change):

- `shared/event_publisher.py` — `AgentEventPublisher` sheds → `sse_drop`
- `shared/telemetry.py` — emitter queue-full shedding (`event_log_drop`) →
  `event_log_drop`; `init_gateway_process` boot → `service_started`
- `agent/graph/_llm.py` + `agent/observe.py` — whole-call wall-clock →
  `llm_usage.latency_ms` → Prometheus histogram + counters (OTLP)

## Extensibility

A new panel metric = new event emissions + one query function in
`gateway/ops_series_lgtm` + one schema + one frontend panel.
