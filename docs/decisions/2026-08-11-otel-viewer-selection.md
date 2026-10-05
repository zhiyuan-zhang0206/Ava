---
type: decision
title: OTel viewer selection — self-hosted trace backend (Tempo over Jaeger/SigNoz/Zipkin)
description: User rulings 2026-08-11: no Langfuse machine; pick the most general, most popular OpenTelemetry frontend hosted locally; Tempo is the only trace backend, viewed through Grafana (Loki + Prometheus + Tempo in one UI); Jaeger dropped; write/read decoupled, viewer read-only and stop-anytime.
tags: [observability, otel, tracing, tempo, jaeger, signoz, zipkin, grafana, telemetry]
date: 2026-08-11
status: accepted
---

# OTel viewer selection — self-hosted local trace backend

> Recorded 2026-08-26 (task #1166) from the user's 2026-08-11 rulings; the
> decision was implemented 2026-08-11..14 (PRs #2363, #2382, #2395, #2404)
> and verified live 2026-08-26.

## Context

The self-built events/telemetry storage was being replaced by the standard
OpenTelemetry ecosystem. The trace viewer was the open question:

- 2026-08-11 13:38 — user ruling: **no Langfuse dedicated machine** (wrong
  direction). Pick the most general, most popular OpenTelemetry frontend and
  host it **locally** (candidates: Jaeger / Tempo / SigNoz / Zipkin). Show the
  user several real traces before release. Telemetry produce/consume are
  decoupled; the viewer is read-only and can be started/stopped anytime.
- 2026-08-11 14:05 — user ruling: switch to the OTel ecosystem wholesale —
  Tempo (trace) / Loki (event) / Prometheus (metric) with Grafana as the
  frontend.
- 2026-08-11 14:16 — user ruling: **drop Jaeger; Tempo is the only trace
  backend**. The local Jaeger 2.20.0 / traces-viewer (~1.5 GB) stays untouched
  until the user says otherwise.
- 2026-08-11 14:44 — after OTel is stable, remove the legacy self-built
  storage chain (tracked separately, #1173): no backward compatibility, no
  dead old code.

## Candidates (facts as of 2026-08-26)

| | Jaeger | Tempo | SigNoz | Zipkin |
|---|---|---|---|---|
| GitHub stars | 23,143 | 5,455 | 31,933 | 17,453 |
| License | Apache-2.0 | AGPL-3.0 | mixed — open-source core + enterprise edition | Apache-2.0 |
| Scope | traces only | traces (+ metrics-generator) | full APM: traces + logs + metrics + dashboards + alerts | traces only |
| Local deployment | single binary / brew / docker all-in-one; memory / Elasticsearch / Cassandra backends | **no macOS binary** (linux/windows only) — docker or a linux host; monolithic or microservices mode | docker compose: ClickHouse + query service + frontend + collector; ClickHouse is a real ops burden | single jar / docker; in-memory / Elasticsearch storage |
| Ingest | OTLP native | OTLP native | OTLP native | OTLP (collector) / Zipkin API |
| UI quality | Jaeger UI: waterfall, DAG; traces only | Grafana Tempo datasource: TraceQL queries, service graph, span metrics | own UI: flame graphs, query builder, correlated signals, dashboards | Zipkin UI: waterfall; dated |
| Query | tag-based search | TraceQL (structured, powerful) | query builder | tag-based search |
| Fit with our stack | second UI next to Grafana; heavier storage ops | **same Grafana UI that already shows Loki + Prometheus** — one read surface | replaces Grafana as the UI; a second full platform | second UI; minimal capabilities |

## Decision

**Tempo is the trace backend; Grafana is the one viewer for traces, logs, and
metrics.** Write side stays decoupled: every agent process exports OTLP/HTTP
to its machine-local collector sidecar (`127.0.0.1:4318`), which fans out to
Tempo (traces), Loki (logs), and Prometheus (metrics). The viewer (Grafana) is
read-only, anonymous, and can be stopped anytime without affecting recording.

Why, against the other candidates:

1. **One UI.** We already run Loki + Prometheus + Grafana for the event-stream
   logs and metrics; Tempo plugs into the same Grafana, so all three signals
   live behind one read surface with correlated `trace_id` — no second UI to
   operate or maintain.
2. **Tempo is the Grafana Labs trace backend** — production-grade, object
   storage backed, TraceQL gives structured trace queries, and it is the
   natural fit for a Grafana-centric stack.
3. **Jaeger was dropped by the user (14:16).** Traces-only UI separate from
   Grafana, and its storage options (Elasticsearch/Cassandra) are heavier to
   run than Tempo's block storage; keeping one ecosystem won.
4. **SigNoz** is the most starred and has the best all-in-one UI, but it *is*
   a full APM platform (ClickHouse underneath): it would replace Grafana
   rather than complement it, and duplicates what Grafana already does for
   logs and metrics locally.
5. **Zipkin** is the simplest but the oldest; its UI and query capabilities
   trail the field, and it is not where the OTel ecosystem is moving.

## Consequences (implemented)

- Deployment lives in `deploy/lgtm/` (README there is authoritative):
  Loki 3.7.6, Prometheus 3.13.2, Grafana 13.1.3 run as **native launchd
  services on the LGTM host** (macmini, pinned in
  `deploy/lgtm/native/versions.yml`), lifecycle-owned by `ava lgtm on/off`
  and `deploy/lgtm/start.sh` / `stop.sh`; backend listeners bind loopback
  only; Grafana :3003 is the anonymous read-only surface.
- **Tempo has no macOS binary**, so it runs on the linux host
  (zzy-lenovo-1, WSL, linux/amd64) selected by
  `AVA_TELEMETRY_TEMPO_ENDPOINT` (OTLP intake) and
  `AVA_TELEMETRY_TEMPO_QUERY_URL` (Grafana/Prometheus queries); Tempo 3.0.2
  verified live. The compose copy in `deploy/lgtm/` remains the rollback
  asset.
- Every machine runs a native `otelcol-contrib` sidecar (0.155.0) as the one
  local OTLP entry; the same config also mirrors traces to a grep-friendly
  JSONL file (`$AVA_HOME/traces/spans.jsonl`), and `ava trace ship` replays
  mirror gaps straight to Tempo, bypassing the collector.
- Viewer start/stop does not disturb recording: write side and read side are
  independent.
- Legacy self-built storage removal is tracked (#1173) and waits for a stable
  observation window; the leftover local Jaeger package is kept untouched per
  ruling until the user decides its fate.
