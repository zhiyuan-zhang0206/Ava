---
type: doc
title: Metrics
description: '`base/telemetry/metrics/report.py` is the core of system-level metric calculation over the unified `events` stream (categories telemetry + log): a single windowed query fetches N days of events, then runs a set of pluggable metric units (pure function `list[EventRow] -> MetricSection` registered with `@metric_unit`). CLI and gateway share this core.'
tags:
- base
- library
- observability
---

# Metrics

## What is it

`base/telemetry/metrics/report.py` is the core of metric calculation over the unified `events` stream (`category IN ('telemetry','log')`). A single windowed query fetches N days of events, then runs a set of pluggable metric units (pure function `list[EventRow] -> MetricSection`, registered with `@metric_unit`). Adding a metric = one decorated function; no SQL is written beyond that single windowed fetch.

## Core Responsibilities

### Projected read
- `query_events(cur, days, agent_id)` — **within SQL** extracts each field of the payload jsonb into typed scalar columns (`EventRow` NamedTuple). For the large `body` field of **exec output**, only the length is taken (`body_len`); the projection's one full-text pull is `halt_body` (halt rows, for compact/idle detection). `sdk_usage` is a runtime event-count metric, not a scan of code text (the old `code_body` full-text pull was removed with the sdk_usage rewrite). Raw jsonb is never pulled (one week ~128MB), also saving psycopg's per-row json parse — the main cost on the read path. Units read `e.in_total` rather than `e.payload[...]`.
- `fetch_events(days, agent_id)` / `build_report(...)` — assembly entry points.

### Metric units
Currently 5: `syntax_fix`, `exec`, `llm_turns`, `agent_activity`, `plugin_activation` — the list `_sections_from_aggregate` returns in `base/telemetry/metrics/aggregate.py`. Each is a `MetricSection` containing both a text block (human-/agent-readable ASCII digest) and a `data` fragment (machine-readable shape).

- `plugin_activation` counts **plugin injection surfaces that fired**: one `plugin_activation` event per firing (`base/packages/plugins/activation.py`), keyed by the same `<plugin>/<surface>/<identifier>` triple `meiva plugins inspect` lists as a registered contribution, plus the model in force. A contribution registered but never counted here is philosophy §6's removal evidence.
- `pctiles()` returns a typed `Pctiles` (`TypedDict`: `n`/`p50`/`p90`/`max`/`mean`), consumed by `render_pctiles` with the same shape.

### Two consumers share the same core
- `scripts/metrics.py` CLI — renders text + dumps JSON.
- gateway `/api/metrics` (`gateway/events/metrics.py`) — returns `data` to the frontend Metrics page. Both consumers fetch the window with `aggregate.fetch_aggregate` (`aggregate_sql`: a few statements over `telemetry_events` in one connection, nothing materialized per row); `/api/metrics/agents` runs only the per-agent counters and `llm_usage` sums (`fetch_agent_rollups`). Inspector statistics read persisted observations with cumulative or time-based windows.

## Notes

- Cache-hit rates use the same calls in numerator and denominator: only calls with numeric `cache_read` metering. A reported zero remains a measured miss; absent metering contributes no cache-rate input. Nonzero usage without metering yields null/N/A while calls, tokens and cost stay complete within the observed window. The early/mid/late cache buckets likewise consider only metered calls.

- `/api/metrics` and `/api/metrics/agents` sum usage-time `cost_usd` snapshots, including cache-write costs and the served Standard/Fast rates. Recorded zero is priced; `unpriced=1` is counted without fabricating a cost. Only older rows lacking both fields retain the legacy token-based price calculation. Token totals and monetary totals are reduced independently within the same agent/window/since-compact scope.
- Unlike the reverted `base/agent_perf` (agent-level profiling, introduced in #50, reverted in #76) — this is a system-level, event-driven metric, the only existing metrics module.
- Helper pure functions: `group_by_agent` / `filter_since_compact` (only after the most recent compact) / `pctiles` / `agent_rollup` / `render_bar` / `render_pctiles` etc.

## Key Dependencies

- [[agent/db/docs/db.meiva.okf.md]] — Postgres pool (`telemetry_events` holds the telemetry and log stream; the aggregate reads it)
- [[log.meiva.okf.md]] — the event stream is written by the unified emitter (`base/telemetry/emitter.py`), fed by `base/log/process.py`

## SDK event collection

The SDK wraps its public static functions at import and after plugin loading; dynamic
MCP tools share a wrapped call funnel. Each public entry records independently,
including nested fan-out and recursion. Plugin layers retain one installed recorder
for their public function. Async wrappers measure the awaited call, including errors
and cancellation. The execution owner supplies its complete unsampled tally through
`MeivaContext.sdk_calls`; calls and ordinary threads share that owner explicitly.

`MEIVA_SDK_CALL_SAMPLING_ENABLED=false` is the default (every call). When enabled,
`MEIVA_SDK_CALL_SAMPLE_EVERY=N` sets inclusion probability 1/N, with N >= 1. These
cluster fields use the existing config API/CLI. Processes start with their boot
policy and refresh active-call policy snapshots in the background every five
seconds from local `.env` on gateways and unenrolled tools, or the existing
authenticated bootstrap endpoint on an enrolled runner. Fetch failures warn and retain the last valid policy. SDK
calls never wait for a remote configuration fetch.

Events go directly to the unified emitter, so external callers need no logger
initialization or an execution tally. Call-time agent/source attribution snapshots
explicit SDK provenance, including borrowed identities, without validating a lease
or doing database I/O. Sampling is an explicit
loss of detail: sampled events cannot reconstruct a complete call history.

## Metrics test windows

`tests/telemetry_stream.py` writes append-only telemetry and passes a chosen
`now` into the read under test. Random slots are 40 days apart, wider than the
30-day read window, so different slots separate their fixture windows. The
sampling is with replacement; two streams can choose the same slot. This setup
reduces interference but does not guarantee unique windows or absolute isolation.

## Plugin query validation tests

`base/packages/tests/test_plugin_metrics.py` checks registry admission and the
SQL validator in `plugin_metrics.py` and `plugin_metrics_sql.py`. Table checks
recurse into scalar subqueries, including function arguments: nesting a
`pg_class` read inside a permitted function must not bypass validation. Legal
subqueries over `agents_meta` remain accepted. SQL templates are static, without
Grafana macros or template placeholders; event names are separate data and may
contain hyphens. Literal rendering escapes embedded quotes.

These tests validate query structure and registry rules. They do not execute
the sample SQL or establish that its referenced columns exist in the database.
