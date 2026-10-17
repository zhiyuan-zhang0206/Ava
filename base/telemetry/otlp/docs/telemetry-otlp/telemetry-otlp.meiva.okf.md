---
type: doc
title: "OTLP export backend & trace ship to Tempo"
description: "The OTLP logs, metrics, and traces backend: producer mapping, collector delivery, local trace mirror, and recovery shipping."
tags:
- base
- telemetry
- otlp
- observability
---

# OTLP export backend & trace ship to Tempo

## What it is

`base/telemetry/otlp/telemetry_otlp.py` is the agent-side write half of the OTel + Tempo +
Loki + Prometheus + Grafana stack (2026-08-11 decision). The local OTel
Collector sidecar (`meiva-otel-collector`, task #1266) delivers the three signals
and mirrors traces to local JSONL for recovery shipping. Which homes may export
or collect, and the `cluster` Resource boundary that isolates co-located homes,
are specified in [[cluster-isolation.meiva.okf.md|Telemetry cluster isolation]].

- `base/telemetry/otlp/telemetry_otlp.py` — events → OTLP logs + metrics when enabled
  (default **on** for the registered `.meiva` production identity). Production
  exec children hold their records until exit, hold saturation, or the max-age
  bound (`base.telemetry.otlp.telemetry_otlp_defer`, task #3816 M4b) instead of warming the
  exporter stack at boot; test/ad-hoc exec children stay off unless an operator
  supplies an explicit endpoint. Their request-file handshake is not an export
  authority.
- `base/telemetry/tracing.py` — spans → OTLP/HTTP (protobuf, content-stripped) to
  the sidecar's `/v1/traces`; the sidecar's file exporter writes the OTLP/JSON
  mirror. Producer timeout, circuit-breaker, shedding, and synchronous-flush
  bounds live in [[export-backpressure.meiva.okf.md|OTLP export backpressure]].
- `meiva trace ship` (`cli/commands/observability/trace.py`) — recovery replay that bypasses
  the local sidecar (replaying through it would loop the mirror watermark).
  Gateway/single-box units dial loopback Tempo; pure runners dial the gateway
  collector's authenticated receiver with the telemetry token.
- The sidecar also **scrapes** the traditional SRE layer — host, Postgres,
  Redis — into the same metrics fan-out:
  [[infra-metrics.meiva.okf.md|Infrastructure metrics]].

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
  is its own node: [[base/telemetry/otlp/docs/telemetry-otlp/metrics-mapping.meiva.okf.md]].
- **Traces** — exported with the cluster Resource to the sidecar's `/v1/traces`
  (OTLP/HTTP protobuf, content-stripped before leaving the process); the sidecar's
  file exporter mirrors each batch to `$MEIVA_HOME/traces/spans.jsonl`
  (rotated `spans-<ISO>.jsonl`). Recovery replay is `meiva trace ship`: it
  POSTs each mirror line as OTLP/HTTP protobuf to the role-correct recovery
  target: gateway/single-box → `{MEIVA_TELEMETRY_TEMPO_ENDPOINT}/v1/traces`
  without auth; pure runner → gateway private address on the OTLP ingress
  port (`MEIVA_TELEMETRY_OTLP_PORT`, default 4318) with
  `Authorization: Bearer <telemetry token>`. It refuses while the OTLP flag
  is off (one kill switch for the
  whole OTLP surface — with the sidecar architecture that also stops
  recording). Incremental (per-file byte-offset watermark
  `traces/.ship-watermark.json`) or windowed (`--since` / `--until`);
  ingestion is idempotent by span id. Tempo is the only target.
- **Failure isolation** — the producer queue, network deadlines, trace circuit,
  drop accounting, initialization retries, and exec-child flush ceilings are
  specified in [[export-backpressure.meiva.okf.md|OTLP export backpressure]].

## Read side

Enablement flags, endpoint defaults and the read topology have their own node:
[[base/telemetry/otlp/docs/telemetry-otlp/flag-semantics.meiva.okf.md|OTLP flag semantics & endpoints]].

## Key dependencies

- `base/telemetry/pipeline_build.py` — caller: `_write_batch` runs the OTLP export after
  the JSONL mirror append; the concrete pipeline closes its own exporter after
  joining the ordinary writer. `_drain_on_exit` observes that same pipeline.
- OTel SDK (`opentelemetry-*`) — imported lazily inside `_build_providers` /
  `_emit_log`, so flag-off processes never pay for it.
- `base/config/domains/observability/settings.py` — the producer-local `telemetry_otlp_*`
  settings plus gateway-local backend URLs.
- `base/deploy/release/collector_artifact.py` — pinned downloader for release prep and
  `cli/commands/observability/otel_collector.py` converge. Converge and
  `deploy/otel-collector/otel-collector.yaml` implement cluster filtering and
  the credential-free (peer, monitoring role) Postgres receiver. Root health: `services/supervision/roster/spec.py`
  (`meiva-otel-collector`) + `services/supervision/healthchecks/otel_collector.py`.
- `base/telemetry/tracing.py` + `cli/commands/observability/trace.py` + `cli/parsers/host.py` — the
  mirror `meiva trace ship` replays, and the ship command.

## Entry points

- `base/telemetry/otlp/telemetry_otlp.py:OtlpBackend` — built by the root
  with its explicit eligibility callback and owned by `ExportingEventPipeline`;
  no-op when the flag is off; `flush()` / `shutdown()` observe that owner. `telemetry_otlp_worker.OtlpWorker`
  owns the one writer and each partial SDK resource through teardown.
- `base/telemetry/otlp/telemetry_otlp_exporting.py:ExportingEventPipeline` — one ordinary
  writer beside the exporter it owns; every stop stage receives only the deadline's
  real remaining time.
- `base/telemetry/pipeline_build.py:build_pipeline` — constructs the concrete writer
  and passes its backend export callback to `_write_batch`.
- `cli/commands/observability/trace.py:cmd_trace_ship` — `meiva trace ship
  [--since/--until] [--dry-run]`

## Notes

- Routine OTLP diagnostics use loguru with `_no_emitter`, reaching stderr/file
  without re-entering the pipeline. Registered disabled/recovered events also
  record the otherwise invisible episode in the JSONL mirror.
- Runner exporter component IDs stay `otlphttp/tempo`, `otlphttp/loki` and
  `otlphttp/prometheus` across the direct-backend → relay cutover so their
  collector self-metrics keep their series. Every exporter keeps a bounded
  in-memory sending queue; a collector restart drops what was queued.
  Queue/drop/silence observability lives in
  [[infra-metrics.meiva.okf.md|Infrastructure metrics]].
- Exit flush (task #4320) — both providers build with
  `shutdown_on_exit=False`; the single ordered exit seam and its accepted
  semantics: [[exit-flush.meiva.okf.md|OTLP exit flush]].
- Collector/Grafana operations:
  `docs/conventions/runbook.md` (Observability / Tracing).
- Parent node: [[base.meiva.okf.md|Base Library]].
