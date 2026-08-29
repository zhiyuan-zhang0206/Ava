# Observability

Every agent turn is a trace, every event is one stream, and one backend
stack (LGTM) serves traces, logs, and metrics in a single UI — and backs the
gateway's /ops + inspect endpoints. Why Tempo (over Jaeger / SigNoz / Zipkin)
became the trace backend, and the 2026-08-11 rulings behind it, are recorded
in [decisions/2026-08-11-otel-viewer-selection.md](../../decisions/2026-08-11-otel-viewer-selection.md).

## Why it matters

- A fleet of long-lived agents is only operable if *what the model saw* and
  *what the system did* are inspectable after the fact.
- Recording must never depend on the network: an observability outage must not
  become an agent outage (and vice versa).

## How it works

```
agent process ── OTLP/HTTP ──▶ local collector ──▶ local JSONL trace mirror
                                      │
pure runner collector ── Bearer ──────┤ gateway AVA_MACHINE_HOST:<AVA_TELEMETRY_OTLP_PORT>
                                      ▼
gateway collector ──▶ loopback Tempo + Loki + Prometheus ──▶ Grafana/read paths
```

- **Traces** — OpenLLMetry auto-instruments the LLM SDKs; every span is
  sent to the machine-local collector. Its file exporter writes one standard
  OTLP `ExportTraceServiceRequest` per JSONL line while the live exporter sends
  to Tempo (directly from a gateway; through the authenticated gateway
  collector from a pure runner). LLM content is stripped at the source.
- **Logs + metrics** — the unified event stream exports live over OTLP/HTTP:
  each event becomes a LogRecord (Loki), telemetry events become metrics
  (Prometheus), with `trace_id`/`span_id` riding along so logs correlate with
  traces.
- **Recovery replay** — `ava trace ship` bypasses the local collector so a
  replay cannot write itself back into the mirror. Gateway units dial loopback
  Tempo; pure runners dial the gateway collector with the cluster bearer.
- **Ingress port single source** — the OTLP/HTTP ingress port
  (`AVA_TELEMETRY_OTLP_PORT`, default the standard 4318) is one settings
  field: the sidecar receiver, the gateway's authenticated remote receiver,
  the pure-runner relay endpoint, and the roster/healthcheck port probes all
  derive from it (WP3, task #1945); the agent-side export URL
  `AVA_TELEMETRY_OTLP_ENDPOINT` keeps its own full-URL override.
- **Backend** — `../../deploy/lgtm/` runs the Tempo + Loki + Prometheus +
  Grafana stack (lifecycle-owned on the marked host — see its README). It
  writes nothing to the mirror or the main flow, but the gateway's /ops +
  inspect endpoints, ops alerting, and the events-maintenance rollup read
  from it, so it is required serving infrastructure, not a stop-anytime
  viewer.

## Trace format — why JSONL, and is it required?

No — JSONL is a recovery/inspection mirror, and standard OTLP/HTTP is the live
network form.

- The mirror is **standard OTLP/JSON**: each line is an OTLP
  `ExportTraceServiceRequest`, the same wire shape any OTLP backend ingests —
  not a custom format.
- The **standard network form (OTLP over HTTP)** is used from every producer to
  its local collector and from collectors onward. The local receiver is the
  only endpoint an agent process knows.
- The collector's file exporter writes JSONL in parallel with live delivery.
  Trace/log exporters use persistent queues; metrics use bounded in-memory
  retry and may shed stale points. The memory limiter can also return
  backpressure before the mirror, so the system makes no absolute no-loss
  promise.
- Distinct from the pre-compact conversation dump (`../../agent/history_dump.py`):
  that JSONL holds full conversation content for audit/replay (opt-in), while
  the trace mirror is metadata-only OTel.

### Who consumes the mirror

1. `ava trace ship` — recovery replay to Tempo (watermark-resumable, windowed
   backfill, idempotent); live collector export is the normal backend path.
2. `fetch_trace.py --source mirror` (trace toolchain) — complete offline span
   retrieval with no size cap and no network, for trace reports and analysis
   (Tempo's full-trace API fails above its cap).
3. Nothing else reads it in-repo; retention defaults to 3 days and is
   configurable with `AVA_TRACE_RETENTION_DAYS`, pruned on agent start.

### Why local collectors plus recovery replay

The collector keeps network retries, backpressure and backend credentials out
of agent processes. A pure runner relays to one authenticated gateway ingress;
the unauthenticated Tempo/Loki/Prometheus ports remain loopback-only. The JSONL
mirror remains useful for offline inspection and gap replay, but scheduled
shipping is not the live delivery mechanism.

## Design decisions

- Unified event model: `../../decisions/2026-08-04-event-system-design.md`
- Tempo-only viewer (Jaeger dropped): `../../deploy/lgtm/README.md`
- Trace v2 content stripping: `../../shared/trace.py` module docstring
