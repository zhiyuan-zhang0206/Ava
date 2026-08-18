# Observability

Every agent turn is a trace, every event is one stream, and one backend
stack (LGTM) serves traces, logs, and metrics in a single UI — and backs the
gateway's /ops + inspect endpoints.

## Why it matters

- A fleet of long-lived agents is only operable if *what the model saw* and
  *what the system did* are inspectable after the fact.
- Recording must never depend on the network: an observability outage must not
  become an agent outage (and vice versa).

## How it works

```
agent process ── OTel spans ──▶ local OTLP/JSON mirror ($AVA_HOME/traces/*.jsonl)
agent process ── events ──────▶ OTLP/HTTP (protobuf) ──▶ Loki (logs) + Prometheus (metrics)
ava trace ship ── replay mirror ──▶ OTLP/HTTP ──▶ Tempo (traces)
Grafana ◀── Tempo + Loki + Prometheus ── one UI + gateway read paths (deploy/lgtm/)
```

- **Traces** — OpenLLMetry auto-instruments the LLM SDKs; every span is
  recorded to a local JSONL mirror, one standard OTLP `ExportTraceServiceRequest`
  per line. LLM content is stripped at the source (metadata only by default).
  A scheduled `ava trace ship` replays the mirror to Tempo over OTLP/HTTP,
  resumable via watermark.
- **Logs + metrics** — the unified event stream exports live over OTLP/HTTP:
  each event becomes a LogRecord (Loki), telemetry events become metrics
  (Prometheus), with `trace_id`/`span_id` riding along so logs correlate with
  traces.
- **Backend** — `../../deploy/lgtm/` runs the Tempo + Loki + Prometheus +
  Grafana stack (lifecycle-owned on the marked host — see its README). It
  writes nothing to the mirror or the main flow, but the gateway's /ops +
  inspect endpoints, ops alerting, and the events-maintenance rollup read
  from it, so it is required serving infrastructure, not a stop-anytime
  viewer.

## Trace format — why JSONL, and is it required?

No — JSONL is not required, and the standard network form is in use today.

- The mirror is **standard OTLP/JSON**: each line is an OTLP
  `ExportTraceServiceRequest`, the same wire shape any OTLP backend ingests —
  not a custom format.
- The **standard network form (OTLP protobuf over HTTP)** is used by the live
  log/metric exporters (OTLP/HTTP to Loki/Prometheus) and by `ava trace ship`
  (OTLP/HTTP to Tempo's `/v1/traces`).
- Recording to a local file first is a **durability choice** (record/ship
  split, 2026-06-16): writing a file cannot fail the way a network POST can,
  so recording never drops spans to a backend outage — and shipping can be
  turned off without stopping recording.
- Distinct from the pre-compact conversation dump (`../../agent/history_dump.py`):
  that JSONL holds full conversation content for audit/replay (opt-in), while
  the trace mirror is metadata-only OTel.

### Who consumes the mirror

1. `ava trace ship` — the only viewer backend path; scheduled incremental
   replay to Tempo (watermark-resumable, windowed backfill, idempotent).
2. `fetch_trace.py --source mirror` (trace toolchain) — complete offline span
   retrieval with no size cap and no network, for trace reports and analysis
   (Tempo's full-trace API fails above its cap).
3. Nothing else reads it in-repo; retention is 14 days
   (`AVA_TRACE_RETENTION_DAYS`), pruned on agent start.

### Why record+ship instead of live export (documented)

`../../conventions/runbook.md` / `../../shared/trace.py`: "a tailnet blip can no longer
error mid-run or drop spans — the previous design pushed every span inline
over OTLP and raised `Exception while exporting Span.` whenever the POST
failed." Motivations, all documented: backend-outage degradation, no agent
hot-path blocking, resumable/backfillable shipping.

### Industry comparison and migration options

Standard OTel SDK buffers in memory only (BatchSpanProcessor, retry-5-then-
drop); durable buffering lives in the Collector layer (persistent queue /
file storage), with agents exporting OTLP protobuf over the network. Ava
moves durability into the agent process as a JSONL file — standard OTLP/JSON
format, nonstandard plumbing. To move closer to the standard: run a local
otel-collector per host (the LGTM stack already ships one) with a persistent
queue and switch the span exporter to OTLP/HTTP localhost (the live
log/metric exporters already POST to the same collector). To keep the status
quo: the mirror stays, with the answer "durability choice, not a format
limitation." — and the full three-question analysis of the mirror, its
   consumers, and the migration options lives in the observability design
   thread.

## Design decisions

- Unified event model: `../../decisions/2026-08-04-event-system-design.md`
- Tempo-only viewer (Jaeger dropped): `../../deploy/lgtm/README.md`
- Trace v2 content stripping: `../../shared/trace.py` module docstring
