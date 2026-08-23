---
type: doc
title: "Prometheus telemetry-aggregate read path"
description: "`gateway/prom_metrics.py` — the LGTM telemetry-aggregate read side (task #1197): instant queries over the OTLP-mapped llm_usage counters, serving the fleet graph node totals/scores and the dashboard token + cost block."
tags:
- gateway
- prometheus
- observability
---

# Prometheus telemetry-aggregate read path

## What it is

`gateway/prom_metrics.py` is the telemetry-aggregate read path that replaced
the PG `events` llm_usage reads (task #1197 — the LGTM cutover, user decision
2026-08-12: all telemetry aggregates serve from Prometheus). The write side
(`shared/telemetry_otlp.py`,
[[shared/telemetry-otlp/telemetry-otlp.ava.okf.md|OTLP exporter]]) maps each
telemetry event's numeric payload fields to OTLP instruments — int -> Counter
named `ava_<event_name>_<field>` (Prometheus appends `_total`), float ->
Histogram — with machine / process / agent_id + declared payload scalars as
datapoint attributes.

## Core responsibilities

- **`query(expr, timeout_s=None)`** — one instant query against Prometheus's
  HTTP API (`/api/v1/query`), returning the `data.result` as (labels, value)
  pairs. Omitting the optional per-call timeout uses the shared client default.
- **`sum_by(metric, by, window_hours=None, timeout_s=None)`** —
  `sum by (<by>) (<metric>)`:
  raw cumulative counters for the all-time view, wrapped in
  `increase(<metric>[<N>h])` for the windowed view (the PromQL equivalent of
  the old SQL `ts > now() - N hours`). Series missing the grouping label
  aggregate under `""` (the old SQL NULL group).
- Counters are cumulative since the exporting process started, so "all-time"
  means "since exporter start / retention" after the cutover.

## Consumers

- `GET /api/fleet/graph` — node `total_tokens` (all-time in+out) + windowed
  `node_score` (in*0.1 + out*1.0); a Prometheus outage degrades the graph to
  a `stale` last-good response when available, otherwise to the PG node set
  with empty edges and zeroed metric fields.
- `GET /api/stats/dashboard` — windowed per-model in/out/cache_read sums,
  priced per model via `shared.lm.pricing.cost_usd`.
- `AVA_TELEMETRY_PROMETHEUS_URL` (default `http://127.0.0.1:9090`,
  restart_required gateway) points at the Prometheus HTTP API port.

## Notes

- Series-explosion guard (write side): metric attributes are
  payload-whitelisted — loguru decoration extras (msg / cache_pct /
  reason_pct) never become labels, so counters stay one series per
  (machine, process, agent_id, model) instead of one per event (a per-event
  unique msg split every counter into single-sample series, making
  `increase()` aggregations undercount ~45% on live data — fixed with the
  cutover, old series age out with Prometheus retention).
- Parent node: [[gateway.ava.okf.md|Gateway]].
- The fleet graph passes an eight-second per-call timeout for its Prometheus
  aggregates; other Prometheus consumers retain the shared client default.
