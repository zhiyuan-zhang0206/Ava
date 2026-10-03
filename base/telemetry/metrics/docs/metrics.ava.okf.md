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

- `plugin_activation` counts **plugin injection surfaces that fired**: one `plugin_activation` event per firing (`base/packages/plugins/activation.py`), keyed by the same `<plugin>/<surface>/<identifier>` triple `ava plugins inspect` lists as a registered contribution, plus the model in force. A contribution registered but never counted here is philosophy §6's removal evidence.
- `pctiles()` returns a typed `Pctiles` (`TypedDict`: `n`/`p50`/`p90`/`max`/`mean`), consumed by `render_pctiles` with the same shape.

### Two consumers share the same core
- `scripts/metrics.py` CLI — renders text + dumps JSON.
- gateway `/api/metrics` (`gateway/events/metrics.py`) — returns `data` to the frontend Metrics page. Both consumers fetch the window with `aggregate.fetch_aggregate` (`aggregate_sql`: a few statements over `telemetry_events` in one connection, nothing materialized per row); `/api/metrics/agents` runs only the per-agent counters and `llm_usage` sums (`fetch_agent_rollups`). Inspector statistics read persisted observations with cumulative or time-based windows.

## Notes

- Unlike the reverted `base/agent_perf` (agent-level profiling, introduced in #50, reverted in #76) — this is a system-level, event-driven metric, the only existing metrics module.
- Helper pure functions: `group_by_agent` / `filter_since_compact` (only after the most recent compact) / `pctiles` / `agent_rollup` / `render_bar` / `render_pctiles` etc.

## Key Dependencies

- [[db.ava.okf.md]] — Postgres pool (`telemetry_events` holds the telemetry and log stream; the aggregate reads it)
- [[log.ava.okf.md]] — the event stream is written by the unified emitter (`base/telemetry/emitter.py`), fed by `base/log/__init__.py`

## SDK event collection

The SDK wraps its public static functions at import and after plugin loading; dynamic
MCP tools share a wrapped call funnel. `recording()` only collects the complete
per-execution tally. Async wrappers measure the awaited call, including errors and
cancellation; context-local frames prevent concurrent tasks from suppressing one
another. Nested SDK implementation calls count once at the outer public boundary.

`AVA_SDK_CALL_SAMPLING_ENABLED=false` is the default (every call). When enabled,
`AVA_SDK_CALL_SAMPLE_EVERY=N` sets inclusion probability 1/N, with N >= 1. These
cluster fields use the existing config API/CLI. Processes start with their boot
policy and refresh active-call policy snapshots in the background every five
seconds from local `.env` on gateways and unenrolled tools, or the existing
authenticated bootstrap endpoint on an enrolled runner. Fetch failures warn and retain the last valid policy. SDK
calls never wait for a remote configuration fetch.

Events go directly to the unified emitter, so external callers need no logger
initialization or `recording()` context. Call-time agent/source attribution uses
cached SDK provenance, including borrowed identities, without validating a lease
or doing database I/O. Sampling is an explicit
loss of detail: sampled events cannot reconstruct a complete call history.
