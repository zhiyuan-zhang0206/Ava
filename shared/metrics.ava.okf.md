---
type: doc
title: Metrics
description: '`shared/metrics.py` is the core of system-level metric calculation over the unified `events` stream (categories telemetry + log): a single windowed query fetches N days of events, then runs a set of pluggable metric units (pure function `list[EventRow] -> MetricSection` registered with `@metric_unit`). CLI and gateway share this core.'
tags:
- shared
- library
- observability
---

# Metrics

## What is it

`shared/metrics.py` is the core of metric calculation over the unified `events` stream (`category IN ('telemetry','log')`). A single windowed query fetches N days of events, then runs a set of pluggable metric units (pure function `list[EventRow] -> MetricSection`, registered with `@metric_unit`). Adding a metric = one decorated function; no SQL is written beyond that single windowed fetch.

## Core Responsibilities

### Projected read
- `query_events(cur, days, agent_id)` — **within SQL** extracts each field of the payload jsonb into typed scalar columns (`EventRow` NamedTuple). For the large `body` field of **exec output**, only the length is taken (`body_len`); the projection's one full-text pull is `halt_body` (halt rows, for compact/idle detection). `sdk_usage` is a runtime event-count metric, not a scan of code text (the old `code_body` full-text pull was removed with the sdk_usage rewrite). Raw jsonb is never pulled (one week ~128MB), also saving psycopg's per-row json parse — the main cost on the read path. Units read `e.in_total` rather than `e.payload[...]`.
- `fetch_events(days, agent_id)` / `build_report(...)` — assembly entry points.

### Metric units (`@metric_unit`)
Currently 5: `syntax_fix`, `exec_stats`, `llm_turns`, `agent_activity`, `sdk_usage`. Each returns a `MetricSection` — containing both a text block (human-/agent-readable ASCII digest) and a `data` fragment (machine-readable shape).

- `sdk_usage` counts by **occurrences**: regex scans each `code` event's source code, +1 for every match of `ava.X.Y(` (no dedup) — writing the same function ten times in a code block counts ten; conversely, a call site inside a loop executes many times at runtime, but source scanning counts only once (this is source scanning, not runtime instrumentation — it measures how often the model "writes" a call, not the actual execution frequency). `data["functions"]` is the full list of functions sorted by count (for a sortable frontend table), while the text digest still renders only the top 20 to stay readable.
- `pctiles()` returns a typed `Pctiles` (`TypedDict`: `n`/`p50`/`p90`/`max`/`mean`), consumed by `render_pctiles` with the same shape.

### Two consumers share the same core
- `scripts/metrics.py` CLI — renders text + dumps JSON.
- gateway `/api/metrics` (`gateway/routers/metrics.py`) — returns `data` to the frontend Metrics page; the `agent_inspect` route also reuses the same `filter_since_compact` / `exec_stats` lens.

## Notes

- Unlike the reverted `shared/agent_perf` (agent-level profiling, introduced in #50, reverted in #76) — this is a system-level, event-driven metric, the only existing metrics module.
- Helper pure functions: `group_by_agent` / `filter_since_compact` (only after the most recent compact) / `pctiles` / `agent_rollup` / `render_bar` / `render_pctiles` etc.

## Key Dependencies

- [[db.ava.okf.md]] — `events` table (the unified event stream)
- [[log.ava.okf.md]] — `events` is written by the unified emitter (`shared/telemetry.py`), fed by `shared/log.py`
