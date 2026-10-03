---
type: doc
title: "Trace mirror and delivery topology"
description: "Why every producer talks to a machine-local collector, why the local trace mirror is JSONL, and who consumes the mirror."
tags:
- base
- telemetry
- otlp
- observability
---

# Trace mirror and delivery topology

## Delivery topology

```
agent process ── OTLP/HTTP ──▶ local collector ──▶ local JSONL trace mirror
                                      │
pure runner collector ── Bearer ──────┤ gateway collector ingress
                                      ▼
gateway collector ──▶ loopback Tempo + Loki + Prometheus ──▶ Grafana / read paths
```

The collector keeps network retries, backpressure and backend credentials out
of agent processes: the local receiver is the only endpoint an agent process
knows. A pure runner relays to one authenticated gateway ingress; the
unauthenticated Tempo / Loki / Prometheus ports stay loopback-only. The backend
stack under `deploy/lgtm/` is required serving infrastructure (the gateway's
ops and inspect endpoints, ops alerting and the events-maintenance rollup read
from it), not a stop-anytime viewer. Tempo is the only trace viewer
([why](../../../../../decisions/2026-08-11-otel-viewer-selection.md)).

## Why the mirror is JSONL

The mirror is a recovery and inspection copy, not the live path; standard
OTLP/HTTP is the live network form. Each line is one standard OTLP/JSON
`ExportTraceServiceRequest`, the shape any OTLP backend ingests, so no custom
format exists to maintain. The collector's file exporter writes it in parallel
with live delivery. Trace and log exporters use persistent queues; metrics use
bounded in-memory retry and may shed stale points; the memory limiter can
return backpressure before the mirror. The system makes no absolute no-loss
promise.

It is distinct from the pre-compact conversation dump
(`agent/hooks/history_dump.py`): that file holds full conversation content for
audit and replay (`AVA_COMPACT_HISTORY_DUMP`), while the trace mirror is
metadata-only OTel with LLM content stripped at the source.

## Who consumes the mirror

1. `ava trace ship` — recovery replay to Tempo (watermark-resumable, windowed
   backfill, idempotent). It bypasses the local collector so a replay cannot
   write itself back into the mirror. Scheduled shipping is not the live
   delivery mechanism.
2. `fetch_trace.py --source mirror` in the `inspect-a-trace` skill — complete
   offline span retrieval with no size cap and no network (Tempo's full-trace
   API fails above its cap).
3. Nothing else reads it in-repo. Retention defaults to three days
   (`AVA_TRACE_RETENTION_DAYS`) and is pruned on agent start.

The producer-side contract is in [[telemetry-otlp.ava.okf.md|OTLP export backend]].
