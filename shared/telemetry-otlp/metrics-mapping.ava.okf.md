---
type: doc
title: "OTLP metrics mapping"
description: "How telemetry-category events become Prometheus series — the per-field disposition (`_METRIC_DISPOSITION`), instrument naming (unit suffix stripped), the cluster-aware metrics Resource, and the Views shaping the LLM latency histograms."
tags:
- shared
- telemetry
- otlp
- observability
- metrics
---

# OTLP metrics mapping

`_record_metrics` (in `shared/telemetry_otlp.py`) maps telemetry-category
events to OTLP instruments; log/audit events are the event stream, not a
measurement, and produce no metrics.

## Instrument naming

Each numeric payload field maps to one instrument named
`ava_<event_name>_<field-minus-unit-suffix>` (`_strip_unit_suffix`): the OTel
unit supplies the suffix on export, so `latency_ms` + unit `ms` renders as
`ava_llm_usage_latency_milliseconds_*` in Prometheus — the unit stated once.
Unit comes from the field name (`_unit_for`): `_ms` -> ms, `_seconds` /
`duration` / `latency` -> s, else the dimensionless "1".

## Disposition

Default rule: int -> Counter (things you sum), float -> Histogram (things
you percentile). `_METRIC_DISPOSITION` overrides per (event, field):

- `llm_usage.price_miss/price_hit/price_out` -> **no metric**. The
  usage-time price snapshot is a RATE (USD per 1M tokens), not a
  measurement; as default-bucket histograms they minted ~50 series per
  (agent, model) with a meaningless distribution. The fields stay in the
  Loki/JSONL event body — exclusion here never touches the event stream.
- `llm_usage.cost_usd` -> **float Counter** (`ava_llm_usage_cost_usd_total`).
  Money is summed, never percentiled; `increase(...)` over any window is the
  exact spend at usage-time rates.
- `watchdog_tick.last_tick_timestamp_seconds` -> **ObservableGauge**
  (`ava_watchdog_tick_last_tick_timestamp_seconds`). It holds the wall-clock
  time of the most recently completed watchdog round, rather than accumulating
  every round; the stale-tick alert retains its `machine` and `process`
  attributes to distinguish the two capability watchdogs on one host.
- `compaction_completed.history_chars` and `.summary_chars` -> **Histogram**.
  They are independent source/replacement size samples; the explicit
  `.compactions=1` field remains the Counter for completion frequency, and
  the float `.summary_history_ratio` uses the default Histogram disposition.
- `sse.active_connections` -> **ObservableGauge** by stream mode; opened and
  closed remain Counters.
- `gateway_process.cpu_percent/rss_bytes/fd_count` -> **ObservableGauge**;
  each 60-second process sample replaces the previous absolute values.

`llm_usage.calls` (constant 1) exists so the int rule mints
`ava_llm_usage_calls_total` — the per-agent/per-model call counter (histogram
counts cannot serve this: the Views below drop `agent_id`).
`llm_usage.unpriced` (1 exactly when the price snapshot is absent) mints
`ava_llm_usage_unpriced_total` so unpriced call volume is countable.

## Attributes

Declared payload scalars (bool / str <= 64 chars) become datapoint
attributes, plus the process dimensions (machine / process / agent_id);
`body` and longer strings never do (content / cardinality guard); None
values are skipped (an absent optional metric is not zero).

## Resource

The MeterProvider's Resource carries `service.name=ava-<process>` (the
bounded process dimension, `shared/telemetry.py:process_name`), a
per-instance `service.instance.id` (uuid4) and
`service.version`, plus the home-derived `cluster` label. Without the service
identity every series landed as
job="unknown_service" and two same-named processes exporting the same
counter collided into ONE series with interleaved cumulative values.
Metrics only: the logs Resource is deliberately default — Loki
index-label-promotes resource attributes, so a per-process-unique id there
would mint a new stream per process start.

## Views

`_metric_views()` shapes `ava_llm_usage_latency` / `ava_llm_usage_decode` with
explicit LLM-scale buckets (250 ms .. 300 s — the OTel defaults top out at
10 s, clipping every slower call into +Inf and flatlining ops p95 at exactly
10 s) and an attribute keep-list of {machine, process, model}. `agent_id`
comes off because latency percentiles are read per model/fleet, and the key
multiplied every (agent, model) into its own 17-series histogram. It also shapes
`ava_gateway_event_loop_lag` with explicit 1 ms .. 300 s buckets, retaining a
recovered minute-scale gateway freeze instead of folding it into +Inf.

## Export

`PeriodicExportingMetricReader`, 15 s interval (`_METRICS_INTERVAL_S`), to
the sidecar's `/v1/metrics`.
