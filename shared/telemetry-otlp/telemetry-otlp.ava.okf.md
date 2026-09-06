---
type: doc
title: "OTLP export backend & trace ship to Tempo"
description: "The OTLP logs, metrics, and traces backend: producer mapping, collector delivery, local trace mirror, and recovery shipping."
tags:
- shared
- telemetry
- otlp
- observability
---

# OTLP export backend & trace ship to Tempo

## What it is

`shared/telemetry_otlp.py` is the agent-side write half of the OTel + Tempo +
Loki + Prometheus + Grafana stack (2026-08-11 decision). The local OTel
Collector sidecar (`ava-otel-collector`, task #1266) delivers the three signals
and mirrors traces to local JSONL for recovery shipping. Which homes may export
or collect, and the `cluster` Resource boundary that isolates co-located homes,
are specified in [[cluster-isolation.ava.okf.md|Telemetry cluster isolation]].

- `shared/telemetry_otlp.py` — events → OTLP logs + metrics when enabled
  (default **on** for the registered `.ava` production identity). Production
  exec children warm and flush the same backend; test/ad-hoc exec children stay
  off unless an operator supplies an explicit endpoint. Their request-file
  handshake is not an export authority.
- `shared/trace.py` — spans → OTLP/HTTP (protobuf wire, content-stripped) to
  the sidecar's `/v1/traces`; the sidecar's file exporter writes the OTLP/JSON
  mirror. Producer timeout, circuit-breaker, shedding, and synchronous-flush
  bounds live in [[export-backpressure.ava.okf.md|OTLP export backpressure]].
- `ava trace ship` (`cli/commands/trace.py`) — recovery replay that bypasses
  the local sidecar (replaying through it would loop the mirror watermark).
  Gateway/single-box units dial loopback Tempo; pure runners dial the gateway
  collector's authenticated receiver with the cluster bearer.
- The sidecar also **scrapes** the traditional SRE layer — host, Postgres,
  Redis — into the same metrics fan-out, with no producer code involved:
  [[infra-metrics.ava.okf.md|Infrastructure metrics]].

## Core responsibilities

- **Logs** (`_emit_log`) — every `Event` becomes one OTLP LogRecord (Loki).
  Body = the full event as JSON in the mirror shape; event_name / category /
  level / machine / cluster / process / source (+ agent_id / target_agent_id
  when set) as attributes; trace_id / span_id ride the LogRecord fields, so Loki
  rows correlate with Tempo spans.
  Severity mapping: debug 5 / info 9 / warning 13 / error 17 / critical 21.
- **Metrics** (`_record_metrics`) — telemetry-category events only: numeric
  payload fields become Prometheus series via a per-field disposition, a
  per-process Resource, and latency-shaping Views — the full mapping contract
  is its own node: [[shared/telemetry-otlp/metrics-mapping.ava.okf.md]].
- **Traces** — exported with the cluster Resource by `shared/trace.py` to the sidecar's `/v1/traces`
  (OTLP/HTTP protobuf, content-stripped before leaving the process); the sidecar's
  file exporter mirrors each batch to `$AVA_HOME/traces/spans.jsonl`
  (rotated `spans-<ISO>.jsonl`). Recovery replay is `ava trace ship`: it
  POSTs each mirror line as OTLP/HTTP protobuf to the role-correct recovery
  target: gateway/single-box → `{AVA_TELEMETRY_TEMPO_ENDPOINT}/v1/traces`
  without auth; pure runner → gateway private address on the OTLP ingress
  port (`AVA_TELEMETRY_OTLP_PORT`, default 4318) with
  `Authorization: Bearer $AVA_CLUSTER_SECRET`. It refuses while the OTLP flag
  is off (one kill switch for the
  whole OTLP surface — with the sidecar architecture that also stops
  recording). Incremental (per-file byte-offset watermark
  `traces/.ship-watermark.json`) or windowed (`--since` / `--until`);
  ingestion is idempotent by span id. Tempo is the only target.
- **Failure isolation** — the producer queue, network deadlines, trace circuit,
  drop accounting, initialization retries, and exec-child flush ceilings are
  specified in [[export-backpressure.ava.okf.md|OTLP export backpressure]].

## Read side

The LGTM consumers are documented separately:
[[gateway/loki-events.ava.okf.md|Loki event history]] and
[[gateway/prom-metrics.ava.okf.md|Prometheus telemetry aggregates]].

**Flag semantics** — `AVA_TELEMETRY_OTLP_ENABLED` /
`AVA_TELEMETRY_OTLP_ENDPOINT` (default `http://127.0.0.1:4318` — the standard
local OTLP HTTP port, derived from `AVA_TELEMETRY_OTLP_PORT`'s default, the
single ingress-port source) are startup-applied
(`restart_required=all`); `shared/config` has no live-reload — flip +
restart applies changes. Off leaves only the JSONL event sink: Loki/Prometheus
and their read surfaces stop advancing, with no Postgres fallback. Converge
renders a role-specific collector config only for the marked LGTM gateway or a
pure runner. An explicit `AVA_TELEMETRY_OTLP_ENDPOINT` bypasses the producer
gate but does not install a collector on an unmarked gateway.
The LGTM gateway uses `AVA_TELEMETRY_TEMPO_ENDPOINT` (default loopback host
port 14318), `AVA_TELEMETRY_LOKI_URL` and
`AVA_TELEMETRY_PROMETHEUS_URL` as gateway-local backend URLs. Pure runners do
not consume those loopback backend URLs: they derive the gateway collector
ingress host from `AVA_GATEWAY_URL` and authenticate with
`AVA_CLUSTER_SECRET`. The remote receiver exists only on a gateway with a
non-empty secret and an exact non-loopback `AVA_MACHINE_HOST`; combined
single-box hosts collapse to the local receiver even when their secret is set.

## Key dependencies

- `shared/telemetry.py` — caller: `_write_batch` runs the OTLP export after
  the JSONL mirror append; `_drain_on_exit` calls
  `telemetry_otlp.shutdown()` at exit.
- OTel SDK (`opentelemetry-*`) — imported lazily inside `_build_providers` /
  `_emit_log`, so flag-off processes never pay for it.
- `shared/config/observability.py` — the producer-local `telemetry_otlp_*`
  settings plus gateway-local backend read/write URLs.
- `cli/commands/_otel_collector.py` + `deploy/otel-collector/otel-collector.yaml`
  — the pinned otelcol-contrib install + generated sidecar config (converge
  step), including cluster filtering and empty-password Postgres receiver
  omission; `ops/spec.py` (`ava-otel-collector` service) +
  `services/healthchecks/otel_collector.py` (watchdog supervision).
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

- Routine OTLP diagnostics use loguru with `_no_emitter`, reaching stderr/file
  without re-entering the pipeline. Registered disabled/recovered events also
  record the otherwise invisible episode in the JSONL mirror.
- Runner exporter component IDs stay `otlphttp/tempo`, `otlphttp/loki` and
  `otlphttp/prometheus` across the direct-backend → relay cutover. The
  file_storage extension keys the Tempo/Loki persistent queues by component
  ID; renaming either would orphan the upgrade backlog. Metrics keep the ID but
  retain bounded in-memory retry. Queue/drop/silence observability lives in
  [[infra-metrics.ava.okf.md|Infrastructure metrics]].
- The stack's operational story (collector, Grafana) is in
  `conventions/runbook.md` (Observability / Tracing).
- Parent node: [[shared.ava.okf.md|Shared Libraries]].
