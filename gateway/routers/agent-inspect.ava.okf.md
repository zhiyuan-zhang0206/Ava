---
type: doc
title: Agent Inspect Router
description: "GET /api/agents/{id}/inspect/statistics + /inspect/live + /neighbors + /inspect/metrics + /inspect/widgets — per-agent LLM cost/token/TPS and neighbor graph, plus the plugin metric and inspector-widget surfaces."
tags:
- gateway
- observability
---

# Agent Inspect Router

`/api/agents/{id}/inspect/statistics` + `/inspect/live` + `/neighbors` + `/inspect/metrics`
+ `/inspect/widgets` — per-agent LLM cost/token/TPS + neighbor graph.

Statistics read persisted compact metric observations and day summaries from
Postgres. Pre-cutover full days preserve the existing cost/turn ledgers without
adding the same observed day twice. Exact duration observations feed quantiles;
older ledger-only histograms retain their declared integer-second precision.
The panel names exact/bucketed/mixed precision and explains missing duration
denominators or retained historical durations that cannot fit the selected window.
Missing evidence is null/partial with window and observation timestamps, not a
zero or a claim of complete collection. No synchronous log scan or completed
result TTL is part of the statistics path. Since-compact is unavailable without
an authoritative completed compact boundary. The `/inspect/live` half remains
window-independent — see [[gateway/routers/ops-surfaces.ava.okf.md]].

## Plugin metric surface (`/inspect/metrics`, W13b)

Builds the metric registry in process — shipped plugin `metrics.py` modules +
core definitions — renders `output`-inspector templates per agent, re-validates
the rendered query, executes LogQL over Loki / SQL read-only over Postgres —
see `shared/plugin_metrics.py` + the `deploy/lgtm` dashboards README.

## Plugin inspector-widget surface (`/inspect/widgets`, task #2909)

Builds the widget registry in process — ENABLED builtin plugins' `inspector.py`
modules under their `PluginContext` — resolves the closed target set
server-side (the agent's open notice; the queue's task-ownership rule) and
drops any button whose target did not resolve, so a widget with nothing to show
leaves the payload — see `shared/plugin_inspector.py`. An `inspector.py` that
fails to import is skipped with a loud report (loguru ERROR + the
`plugin_load_failed` event) and the remaining widgets still serve — fail-soft
per the plugin-load contract (user ruling 2026-09-11); registrations from the
failed import are dropped so the next request retries clean.
