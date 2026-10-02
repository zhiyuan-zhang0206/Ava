---
type: doc
title: Ops Monitor Router
description: "GET /api/ops/monitor — the Insights Ops panel's single-round-trip time-bucketed series over `telemetry_events` (Postgres): SSE/event-log backlog, LLM latency + TPS, process restart counts."
tags:
- gateway
- ops
- observability
---

# Ops Monitor Router

`GET /api/ops/monitor?window=1h|6h|24h|7d` — one round trip backs the whole
Insights Ops section (task #672, user-chartered 2026-08-03). Query core is
`gateway/cluster/ops_series.py` (one grouped statement per metric group over the window's
`telemetry_events` rows, in one pooled connection with an 8-second statement timeout);
schemas are `gateway/cluster/schemas.py`.

## Contract

- **Response**: `{meta, sse, llm, restarts}` — meta carries `bucket_starts`
  (ISO UTC, oldest first, origin-anchored to the fixed 2000-01-01 grid); every
  series array is zero-filled across the whole window and positionally aligned
  to it via its `bucket` index. No caching — each call runs three grouped statements, so a refresh reflects events up to the
  query's `now()` anchor (the in-progress bucket is partial). A bucket is half-open,
  `[start, start + bucket)`.
- **Buckets**: window → bucket seconds: 1h→60, 6h→300, 24h→1800, 7d→3600
  (fixed point counts 60/72/48/168). `window` is capped at 7d to bound the
  query volume.

## Metric groups (MVP)

| Group | Reads | Answers |
|---|---|---|
| `sse` | `sse_drop` (kind=queue_full/publish_error) + `event_log_drop` events (row counts) | how much did the live-view pipes back up per bucket |
| `llm` | `llm_usage` rows (calls, tokens, latency sum, exact p50/p95/max of `latency_ms`) + LLM error-family events | latency + throughput + error rate per bucket |
| `restarts` | `agent_restarted` + `service_started` (row counts + grouped breakdowns; agent labels from the `agents` registry) | which processes restarted, when, how often |

Counts and sums are exact; p50/p95 are exact percentiles over the rows of the bucket (the
retired Prometheus reader approximated them from a latency histogram).

Instrumentation points (collection layer, all on the existing loguru →
unified emitter (`base/telemetry/emitter.py`) → OTLP export, zero schema change):

- `base/events/live/publisher.py` — `AgentEventPublisher` sheds → `sse_drop`
- `base/telemetry/emitter.py` — emitter queue-full shedding (`event_log_drop`) →
  `event_log_drop`; `init_gateway_process` boot → `service_started`
- `agent/graph/llm/node.py` + `agent/llm/usage.py` — whole-call wall-clock →
  `llm_usage.latency_ms`

## Extensibility

A new panel metric = new event emissions + one query function in
`gateway/cluster/ops_series` + one schema + one frontend panel.
