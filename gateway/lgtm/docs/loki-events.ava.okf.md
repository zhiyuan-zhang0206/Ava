---
type: doc
title: "Loki live event read"
description: "`gateway/lgtm/loki_events.py` — the one live read over the Loki observation copy of the event stream: `query_events`, used by the backfill scripts; every gateway reader uses `telemetry_events` and `audit_events`."
tags:
- gateway
- loki
- observability
---

# Loki live event read

## What it is

Loki holds an observation copy of the telemetry and log stream (the write side,
`base/telemetry/otlp/telemetry_otlp.py`, [[base/telemetry/otlp/docs/telemetry-otlp/telemetry-otlp.ava.okf.md|OTLP exporter]],
ships every event as an OTLP log whose line body is the full event JSON). The
record of events is Postgres (`telemetry_events`, `audit_events`); the gateway
reads no Loki aggregate. The one remaining reader is `query_events()`, the
live-Loki source of `scripts/data_repair/backfill_telemetry_events.py` and
`backfill_audit_events.py`. Loki is a droppable projection for Grafana.

## Core responsibilities

- **`query_events()`** — the row list: LogQL `{service_name="unknown_service"}`
  selector → line filters → `| json` → `cluster=<this home> or cluster=""`
  (the empty branch retains this single-cluster Loki's pre-labeling history)
  plus other structured-metadata filters → `query_range` (backward,
  newest-first). Every matching line parses back to the `EventRow` shape; row
  `id` is a stable blake2b surrogate over (ts, line) — Loki has no numeric id.
  Offset pages in memory (`limit + offset + 1` fetched, `has_more` from the +1
  lookahead); the default window is the last 24h.
- **Read gate** — a gateway home without `lgtm-host` refuses the implicit
  loopback Loki URL before any HTTP call, as a typed refusal the gateway maps to
  HTTP 503. An explicit `AVA_TELEMETRY_LOKI_URL` is the operator escape hatch;
  pure runners and role-less maintenance processes retain their existing
  behavior.

## Loki quirks (verified 2026-08-12; label exceptions 2026-08-23)

- Structured metadata is NOT index-label matched by `{...}` selectors, but
  **pipeline filters match it directly** — no `| json` stage needed for
  level / category / machine / trace_id filters. The collector promotes
  `agent_id` and `event_name` to stream labels, so live reads narrow those
  fields inside `{...}` and verify the body values in the pipeline (see
  `base/telemetry/loki_index_labels.py`). The separate archive stream has no
  promoted labels; archive reads extract those fields from JSON.
- `| agent_id=""` matches a JSON null (service-only rows).
- Loki has no offset, hence the in-memory paging.

## Notes

- `AVA_TELEMETRY_LOKI_URL` (default `http://127.0.0.1:3100`,
  restart_required gateway) points at the single-binary Loki HTTP port. The
  default is valid only for the marked LGTM gateway home.
- Every query runs through one long-lived module-level `httpx.Client` (the lazy
  `_client()` accessor — the seam tests swap).
- Every HTTP query also crosses the process's FIFO singleton in
  `gateway/lgtm/loki_query_budget.py`: its reusable state machine lives in
  `base/telemetry/loki_query_budget.py`; the gateway adapter supplies six active
  slots, matching Loki's deployed `querier.max_concurrent`, plus a bounded
  waiter queue and 10s acquisition deadline. `queue_full` and `acquire_timeout`
  are typed local refusals, mapped to retriable HTTP 503 without emitting the
  transport-only `loki_query_failed` event. Every queue/acquire/release/reject
  transition emits the registered `loki_query_budget` telemetry/metric shape;
  the observer only enqueues into telemetry and never calls DB/Loki or runs
  while the budget lock is held.
- Tests: `gateway/lgtm/tests/test_loki_events.py` (httpx-faked unit tests).
- Parent node: [[gateway.ava.okf.md|Gateway]]; write side:
  [[base/telemetry/otlp/docs/telemetry-otlp/telemetry-otlp.ava.okf.md|OTLP exporter]].
