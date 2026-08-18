---
type: doc
title: "OTLP export backend & trace ship to Tempo"
description: "The LGTM read+write side of the unified event stream — `shared/telemetry_otlp.py` (OTLP dual-write exporter: events → OTLP logs to Loki, numeric payloads → OTLP metrics to Prometheus, spans → local OTel Collector sidecar), the per-machine OTel Collector sidecar (task #1266: fan-out to Tempo/Loki/Prometheus + local JSONL trace mirror), `gateway/loki_events.py` + `gateway/prom_metrics.py` (the Loki/Prometheus read paths replacing PG `events` reads, task #1197), plus `ava trace ship` replaying the mirror straight to Tempo."
tags:
- shared
- telemetry
- otlp
- observability
---

# OTLP export backend & trace ship to Tempo

## What it is

`shared/telemetry_otlp.py` is the agent-side write half of the OTel + Tempo +
Loki + Prometheus + Grafana stack (2026-08-11 decision). The OTLP entry on
every machine is the **local OTel Collector sidecar** (`ava-otel-collector`
session, task #1266 — user ruling 2026-08-14): agents export to
127.0.0.1:4318 (OTLP/HTTP) and never dial a backend directly. The sidecar fans out
traces → Tempo (OTLP/HTTP), logs → Loki (`/otlp/v1/logs`), metrics →
Prometheus (OTLP receiver), and mirrors traces to local JSONL
(`$AVA_HOME/traces/spans.jsonl`, rotated) via its file exporter — the grep
surface, and the durable record `ava trace ship` replays when the live
fan-out missed data.

- `shared/telemetry_otlp.py` — events → OTLP logs (Loki) + telemetry payloads
  → OTLP metrics (Prometheus), POSTed to the sidecar when
  `AVA_TELEMETRY_OTLP_ENABLED=true` (default **on**).
- `shared/trace.py` — spans → OTLP/HTTP (protobuf wire, content-stripped) to
  the sidecar's `/v1/traces`; the sidecar's file exporter writes the mirror.
  A missing sidecar at agent init disables recording for that process
  (reported).
- `ava trace ship` (`cli/commands/trace.py`) — recovery replay of the mirror
  straight to Tempo (`AVA_TELEMETRY_TEMPO_ENDPOINT`), bypassing the sidecar
  (replaying through it would loop the watermark).

## Core responsibilities

- **Logs** (`_emit_log`) — every `Event` becomes one OTLP LogRecord (Loki).
  Body = the full event as JSON in the mirror shape; event_name / category /
  level / machine / process / source (+ agent_id / target_agent_id when set)
  as attributes; trace_id / span_id ride the LogRecord fields, so Loki
  rows correlate with Tempo spans.
  Severity mapping: debug 5 / info 9 / warning 13 / error 17 / critical 21.
- **Metrics** (`_record_metrics`) — telemetry-category events only (log/audit
  events are the event stream, not a measurement). Each numeric payload field
  maps to one instrument named `ava_<event_name>_<field>`: int -> Counter,
  float -> Histogram. bool / short-str (<= 64 chars) declared payload
  scalars become datapoint attributes; `body` and longer strings never do
  (content / cardinality guard); None values are skipped. Unit: `_ms` -> ms,
  `_seconds` / `duration` / `latency` -> s, else "1". Exported every 15 s
  (`PeriodicExportingMetricReader`).
- **Traces** — exported by `shared/trace.py` to the sidecar's `/v1/traces`
  (OTLP/JSON, content-stripped before leaving the process); the sidecar's
  file exporter mirrors each batch to `$AVA_HOME/traces/spans.jsonl`
  (rotated `spans-<ISO>.jsonl`). Recovery replay is `ava trace ship`: it
  POSTs each mirror line as OTLP/HTTP protobuf straight to
  `{AVA_TELEMETRY_TEMPO_ENDPOINT}/v1/traces` (no auth, bypassing the
  sidecar), refusing while the OTLP flag is off (one kill switch for the
  whole OTLP surface — with the sidecar architecture that also stops
  recording). Incremental (per-file byte-offset watermark
  `traces/.ship-watermark.json`) or windowed (`--since` / `--until`);
  ingestion is idempotent by span id. Tempo is the only target.
- **Failure isolation** — the contract this module exists to keep: the OTLP
  side never blocks, breaks, or slows the PG write. The drain thread only
  does bounded `put_nowait` into a 2048-entry queue (shed, counted) plus
  in-memory metric atomics; all network I/O runs on SDK-owned threads with
  their own retry/drop. Every entry point is suppress-guarded end to end; a backend
  init failure disables the OTLP side for the process lifetime (reported
  once). A dead collector cannot cost a batch its DB copy.

## Read side (task #1197 — the LGTM cutover)

The historical read path (`gateway/loki_events.py` — query_events /
count_events / attribute_aggregate over Loki, serving the events routes and
the per-agent inspector) is documented in its own node:
[[gateway/loki-events.ava.okf.md|Loki event-history read path]].

The telemetry-aggregate read path (`gateway/prom_metrics.py` — the llm_usage
counter queries behind the fleet graph and the dashboard) is documented in
its own node:
[[gateway/prom-metrics.ava.okf.md|Prometheus telemetry-aggregate read path]].

**Flag semantics** — `AVA_TELEMETRY_OTLP_ENABLED` /
`AVA_TELEMETRY_OTLP_ENDPOINT` (default `http://127.0.0.1:4318` — the LOCAL
sidecar on every machine, standard OTLP HTTP port) are startup-applied
(`restart_required=all`); `shared/config` has no live-reload — flip +
restart is the apply path. The sidecar's fan-out endpoints are baked into
its config at converge: `AVA_TELEMETRY_TEMPO_ENDPOINT` (default
`http://127.0.0.1:14318`, host OTLP port of the LGTM stack), plus
`AVA_TELEMETRY_LOKI_URL` / `AVA_TELEMETRY_PROMETHEUS_URL` (bases; `/otlp` and
`/api/v1/otlp` appended). A remote machine sets those three to the LGTM
host's reachable addresses — its agents still export to its own localhost
sidecar.

## Key dependencies

- `shared/telemetry.py` — caller: `_write_batch` runs the OTLP export after
  the JSONL mirror append and before the events INSERT; `_drain_on_exit`
  calls `telemetry_otlp.shutdown()` at exit.
- OTel SDK (`opentelemetry-*`) — imported lazily inside `_build_providers` /
  `_emit_log`, so flag-off processes never pay for it.
- `shared/config/observability.py` — the `telemetry_otlp_*` settings plus
  `telemetry_tempo_endpoint` / `telemetry_loki_url` / `telemetry_prometheus_url`
  (the sidecar fan-out bases + ship's Tempo target).
- `cli/commands/_otel_collector.py` + `deploy/otel-collector/otel-collector.yaml`
  — the pinned otelcol-contrib install + generated sidecar config (converge
  step); `ops/spec.py` (`ava-otel-collector` service) + `services/healthchecks/
  otel_collector.py` (watchdog supervision).
- `gateway/loki_events.py` — the Loki read path; `gateway/routers/agent_events.py`
  and `gateway/routers/cluster.py` call it.
- `gateway/prom_metrics.py` — the Prometheus aggregate read path;
  `gateway/routers/fleet_graph.py` and `gateway/routers/status.py` call it.
- Read-path tests: `test_loki_events.py` + `test_prom_metrics.py`
  (httpx-faked) and the route tests (`test_agent_events_query.py` /
  `test_cluster_endpoints.py` / `test_fleet_graph.py` /
  `test_stats_dashboard.py`, query mocked).
- `shared/trace.py` + `cli/commands/trace.py` + `cli/parsers/host.py` — the
  mirror `ava trace ship` replays, and the ship command.

## Entry points

- `shared/telemetry_otlp.py:export_batch(events)` — module singleton
  `backend`; no-op when the flag is off; `flush()` / `shutdown()` — test
  seam / process exit
- `shared/telemetry.py:_export_otlp` — the drain-thread call site
  (suppress-guarded, deferred import)
- `cli/commands/trace.py:cmd_trace_ship` — `ava trace ship
  [--since/--until] [--dry-run]`

## Notes

- OTLP-side diagnostics go through loguru bound with `_no_emitter`, so they
  reach the stderr/file sinks and never re-enter the event pipeline.
- Write-side tests: `tests/shared/test_telemetry_otlp.py` (mapping, flag,
  isolation, dual-write via in-memory OTel providers) +
  `tests/cli/test_trace_ship.py` (the tempo target); the conftest autouse
  fixture pins the flag off for the rest of the suite.
- The stack's operational story (collector, Grafana) is in
  `conventions/runbook.md` (Observability / Tracing).
- Parent node: [[shared.ava.okf.md|Shared Libraries]].
