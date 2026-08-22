"""OTLP export backend — the write side of the OTel + Tempo/Loki/Prometheus stack.

Exports the unified event stream: every batch the emitter's drain thread
flushes (``shared.telemetry._write_batch``) is exported here, when
``AVA_TELEMETRY_OTLP_ENABLED=true`` (default since the 2026-08-11 stack
decision). The Postgres `events` copy was retired with the LGTM cutover
(task #1197) — OTLP is now the only live sink besides the JSONL mirror.

The endpoint (``AVA_TELEMETRY_OTLP_ENDPOINT``, default 127.0.0.1:4318) is the
LOCAL OTel Collector sidecar on every machine (task #1266, 2026-08-14):
agents never dial a backend directly. A gateway collector fans out logs ->
loopback Loki and metrics -> loopback Prometheus; a pure runner collector
relays them to the gateway collector's authenticated private-address OTLP
receiver. A remote agent keeps the localhost producer endpoint — its first hop
is still its own sidecar.
Three signals:

- **logs** — every ``Event`` becomes one OTLP LogRecord (Loki). The body is the
  full event as JSON (the same shape the JSONL mirror stores, so Loki holds the
  same content class as the ``events`` table); the indexed dimensions
  (event_name / category / level / machine / process / source / agent ids) ride
  as attributes; ``trace_id`` / ``span_id`` fill the LogRecord fields so logs
  correlate with Tempo spans.
- **metrics** — telemetry-category events become OTLP metrics (Prometheus).
  Each numeric payload field maps to one instrument named
  ``ava_<event_name>_<field>``: int -> Counter, float -> Histogram (see
  ``_record_metrics`` for the rules); datapoint attributes are the process
  dimensions + declared payload scalars only (never loguru decoration
  extras — a per-event msg string would split every counter into its own
  series). Log/audit events produce no metrics: they are the event stream,
  not a measurement.
- **traces** — NOT exported here. ``shared/trace.py`` exports them to the same
  local collector, whose file exporter writes the standard OTLP/JSON mirror.
  ``ava trace ship`` is the separate recovery replay: gateway units dial Tempo
  directly; pure runners use the authenticated gateway collector ingress.

Failure isolation — the contract this module exists to keep: **the OTLP side
must never block, break, or slow the PG write.** Three layers:

1. The emitter drain thread only does bounded ``put_nowait`` into this
   module's queue (shed, counted, reported) plus in-memory metric recordings —
   nothing here can block it.
2. All network I/O runs on SDK-owned threads (``BatchLogRecordProcessor`` /
   ``PeriodicExportingMetricReader``). Their exporters retry on their own
   clock and drop when their bounded queues fill; they cannot raise into any
   Ava thread.
3. Every entry point is suppress-guarded end to end, and the emitter wraps the
   call again (``_export_otlp``) — even a programming error here cannot cost a
   batch its PG copy.

Flag semantics: disposable exec children (identified by their
``AVA_EXEC_REQUEST_FILE`` handshake) are always off. Other processes read
``AVA_TELEMETRY_OTLP_ENABLED`` / ``AVA_TELEMETRY_OTLP_ENDPOINT`` from the
startup-frozen settings singleton (``restart_required`` on the config fields).
This is **startup-applied**, matching every other config field in the system —
there is no live-reload mechanism in ``shared/config``, and the isolation above
makes the flag a rare emergency kill switch, not the primary defense. Flipping
it + restarting is the documented apply path.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any, Literal

from shared.telemetry import Event

__all__ = [
    "endpoint_reachable",
    "export_batch",
    "flush",
    "register_observable_metric",
    "shutdown",
]

ObservableKind = Literal["counter", "gauge"]
ObservableCallback = Callable[[], Iterable[tuple[int | float, dict[str, str]]]]

# ── knobs ─────────────────────────────────────────────────────────────────────

# Bounded queue between the emitter drain thread and the OTLP worker. The SDK's
# own batch processors ALSO have a bounded queue (2048) and their emit() blocks
# when full — so this barrier must be no larger than the SDK's, and it is what
# turns "OTLP exporter thread stuck on a hung endpoint" into counted shedding
# instead of a stalled PG write.
_QUEUE_MAXSIZE = 2048

# Metrics export cadence (PeriodicExportingMetricReader). 15 s keeps Grafana
# near-live without a per-batch network round-trip; the reader thread owns the
# timing, the emitter never waits on it.
_METRICS_INTERVAL_S = 15

# Log-record severity mapping — OTel SeverityNumber values (plain ints; the
# enum is constructed at the record site, see _emit_log).
_SEVERITY_NUMBERS: dict[str, int] = {
    "debug": 5,
    "info": 9,
    "warning": 13,
    "error": 17,
    "critical": 21,
}

# Metric-attribute guard rails: payload keys that never become metric
# attributes, and the max length of a string attribute. The `body` key is
# exec/code payload content — as a Prometheus label it would leak code into
# series cardinality. Strings longer than the cap are dropped on the same
# reasoning as the trace content guard (metadata is small).
_NO_METRIC_ATTRS = frozenset({"body"})
_MAX_METRIC_ATTR_CHARS = 64

# Per-field metric disposition overrides. The default rule (int -> Counter,
# float -> Histogram) fits counts and durations; fields where the type is the
# wrong signal declare themselves here. None = no metric at all (the field
# stays in the Loki/JSONL event body — exclusion here never touches the
# event stream).
#   llm_usage.price_*  — the usage-time price snapshot: a RATE (USD per 1M
#     tokens), not a measurement. As default-bucket histograms they minted
#     ~50 series per (agent, model) and their distribution is meaningless.
#   llm_usage.cost_usd — money is summed, never percentiled: a float Counter
#     (OTel Counter.add takes floats; Prometheus counters are float64), so
#     `increase(ava_llm_usage_cost_usd_total[...])` is the exact windowed
#     spend at usage-time rates.
_METRIC_DISPOSITION: dict[tuple[str, str], str | None] = {
    ("llm_usage", "price_miss"): None,
    ("llm_usage", "price_hit"): None,
    ("llm_usage", "price_out"): None,
    ("llm_usage", "cost_usd"): "counter",
}

# Histogram bucket boundaries for LLM-scale latencies (ms). The OTel defaults
# top out at 10000 — every call slower than 10s fell into +Inf and clipped
# p95/p50 at exactly 10s on the ops panels.
_LLM_LATENCY_BUCKETS_MS = (250, 500, 1000, 2000, 4000, 8000, 15000, 30000, 60000, 120000, 300000)

# Loguru extra key marking the OTLP side's own diagnostics, so they reach the
# stderr/file sinks and never re-enter the event pipeline (same contract as
# shared.telemetry._NO_EMITTER).
_NO_EMITTER = "_no_emitter"


def endpoint_reachable(endpoint: str) -> bool:
    """One quick preflight against an OTLP collector. Any HTTP answer proves a
    listener is up — including 4xx/5xx, which ``urlopen`` raises as
    HTTPError (the collector answers /v1/logs with 415 unless the body
    carries an OTLP content type). Only connection-level failures mean no
    collector (a fresh install without the LGTM stack), where building the
    SDK exporters would make their own threads log 'Exception while
    exporting' every interval forever. The probe posts an OTLP/JSON body
    so a healthy collector answers 200. A transient blip disables OTLP for
    the process lifetime — the same tradeoff the init-failure path
    already makes.

    Shared by the events exporter (here) and the trace exporter
    (``shared.trace.initialize_tracing``) — both arm their SDK exporters
    against the same local sidecar.

    2026-08-12 prod incident: this probe sent no Content-Type, so urllib
    defaulted to application/x-www-form-urlencoded, the collector answered
    415, HTTPError was suppressed as "not answering", and every process
    that restarted after the #1214 rollout silently disabled its OTLP
    export — no events in Loki, no ava_* metrics in Prometheus.
    """
    import urllib.error
    import urllib.request
    from urllib.parse import urljoin, urlsplit

    # Only http(s) endpoints can carry an OTLP collector; anything else
    # (file:, a bare host) is not something this probe should open.
    if urlsplit(endpoint).scheme not in ("http", "https"):
        return True
    try:
        url = urljoin(endpoint.rstrip("/") + "/", "v1/logs")
        req = urllib.request.Request(  # noqa: S310 — scheme validated above; deliberate one-shot preflight
            url,
            method="POST",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=1.5):  # noqa: S310 — same validated probe
            return True
    except urllib.error.HTTPError:
        # Any HTTP status proves a listener answered the port — a 415
        # from a collector that rejects the probe's content type is still
        # the collector.
        return True
    except Exception:
        return False


class _OtlpBackend:
    """One OTLP export backend per process: bounded queue + worker thread that
    feeds the OTel SDK log processor, plus direct in-memory metric recording.

    ``providers`` is the test seam — an injected (LoggerProvider,
    MeterProvider) pair wired to in-memory exporters; production builds the
    real OTLP/HTTP pair from settings (``_build_providers``).
    """

    def __init__(
        self,
        *,
        providers: tuple[Any, Any] | None = None,
        queue_maxsize: int = _QUEUE_MAXSIZE,
    ) -> None:
        self._queue: queue.Queue[Event | None] = queue.Queue(maxsize=queue_maxsize)
        self._dropped = 0
        self._dropped_lock = threading.Lock()
        self._providers = providers
        self._logs: Any = None  # LoggerProvider (any-typed: OTel SDK imported lazily)
        self._metric_provider: Any = None
        self._meter: Any = None
        self._instruments: dict[tuple[str, str], Any] = {}
        self._observable_specs: dict[str, tuple[ObservableKind, ObservableCallback, str, str]] = {}
        self._observable_instruments: dict[str, Any] = {}
        self._observable_failures: set[str] = set()
        self._init_attempted = False
        self._thread: threading.Thread | None = None

    # ── public surface (called by shared.telemetry) ─────────────────────────

    def export_batch(self, events: list[Event]) -> None:
        """Export one emitter batch to the OTLP backend. Never raises, never
        blocks the caller: logs enqueue to the bounded queue (shed when full),
        metrics record in memory (lock-free atomics)."""
        if not events or not self._enabled() or not self._ensure():
            return
        for event in events:
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                with self._dropped_lock:
                    self._dropped += 1
                    n = self._dropped
                if n == 1 or n % 50 == 0:
                    self._report(
                        f"OTLP log queue full — shed {n} event(s) cumulative "
                        "(OTLP side only; the PG copy is unaffected)"
                    )
        for event in events:
            with contextlib.suppress(Exception):
                self._record_metrics(event)

    def flush(self, timeout: float = 2.0) -> None:
        """Synchronously process queued events on the calling thread.

        Test seam + shutdown helper. The worker thread may concurrently take
        events; each event is processed exactly once by whichever thread
        dequeues it, so a flush + worker race is harmless."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                event = self._queue.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return
            if event is None:
                return
            with contextlib.suppress(Exception):
                self._emit_log(event)

    def shutdown(self) -> None:
        """Stop the worker thread and flush the SDK providers (process exit)."""
        with contextlib.suppress(Exception):
            self._queue.put_nowait(None)
        with contextlib.suppress(Exception):
            if self._logs is not None:
                self._logs.force_flush(timeout_millis=2000)
        with contextlib.suppress(Exception):
            if self._metric_provider is not None:
                self._metric_provider.force_flush(timeout_millis=2000)

    # ── flag + backend bring-up ──────────────────────────────────────────────

    @staticmethod
    def _enabled() -> bool:
        """Disable OTLP in disposable exec children; otherwise read the flag
        from startup-frozen settings. Any read failure degrades to off — the
        OTLP side must never be the reason an emit path breaks."""
        if "AVA_EXEC_REQUEST_FILE" in os.environ:
            return False
        with contextlib.suppress(Exception):
            from shared.config import settings

            return bool(settings.observability.telemetry_otlp_enabled)
        return False

    @staticmethod
    def _endpoint_reachable(endpoint: str) -> bool:
        """Module-level ``endpoint_reachable`` — see its docstring."""
        return endpoint_reachable(endpoint)

    def _ensure(self) -> bool:
        """Bring up the backend once. Failure disables the OTLP side for the
        process lifetime (reported) — a misconfigured endpoint must not retry
        per batch."""
        if self._logs is not None:
            return True
        if self._init_attempted:
            return False
        self._init_attempted = True
        try:
            if self._providers is not None:
                self._logs, self._metric_provider = self._providers
            else:
                from shared.config import settings

                endpoint = settings.observability.telemetry_otlp_endpoint
                if not self._endpoint_reachable(endpoint):
                    self._report(
                        f"OTLP endpoint {endpoint} not answering — OTLP export "
                        "disabled for this process (no collector on this box?)"
                    )
                    return False
                self._logs, self._metric_provider = _build_providers(endpoint)
            self._meter = self._metric_provider.get_meter("ava.telemetry")
            self._ensure_observable_metrics()
            self._thread = threading.Thread(target=self._run, daemon=True, name="otlp-exporter")
            self._thread.start()
            return True
        except Exception as exc:  # report once, disable for the process lifetime
            self._report(f"OTLP backend init failed — OTLP side disabled for this process: {exc!r}")
            return False

    def register_observable_metric(
        self,
        name: str,
        *,
        kind: ObservableKind,
        callback: ObservableCallback,
        description: str,
        unit: str = "1",
    ) -> None:
        """Register one process-state observer without opening an exporter.

        Registration happens during service startup. Collection runs later on
        the SDK metrics thread; callback failures shed that observation and
        never reach the service thread.
        """
        spec = (kind, callback, description, unit)
        existing = self._observable_specs.get(name)
        if existing is not None and existing != spec:
            raise ValueError(f"observable metric {name!r} registered twice")
        self._observable_specs[name] = spec
        if self._meter is not None:
            self._ensure_observable_metrics()

    def _ensure_observable_metrics(self) -> None:
        from opentelemetry.metrics import Observation

        from shared.telemetry import metric_dimensions

        for name, (kind, callback, description, unit) in self._observable_specs.items():
            if name in self._observable_instruments:
                continue

            def observe(
                _options: object,
                callback: ObservableCallback = callback,
                name: str = name,
            ) -> Iterable[Any]:
                try:
                    base = metric_dimensions()
                    return [
                        Observation(value, {**base, **attributes})
                        for value, attributes in callback()
                    ]
                except Exception as exc:
                    if name not in self._observable_failures:
                        self._observable_failures.add(name)
                        self._report(
                            f"observable metric {name!r} callback failed; observation shed: {exc!r}"
                        )
                    return []

            constructor = (
                self._meter.create_observable_counter
                if kind == "counter"
                else self._meter.create_observable_gauge
            )
            try:
                self._observable_instruments[name] = constructor(
                    name,
                    callbacks=[observe],
                    unit=unit,
                    description=description,
                )
            except Exception as exc:
                if name not in self._observable_failures:
                    self._observable_failures.add(name)
                    self._report(f"observable metric {name!r} registration failed: {exc!r}")

    def _run(self) -> None:
        """Worker loop: map + emit queued events to the OTel log processor."""
        while True:
            event = self._queue.get()
            if event is None:
                return
            with contextlib.suppress(Exception):
                self._emit_log(event)

    # ── signal mapping ───────────────────────────────────────────────────────

    def _emit_log(self, event: Event) -> None:
        """Map one Event to an OTLP LogRecord and emit it.

        Body = the full event as JSON (mirror shape). Attributes = the indexed
        dimensions; trace_id/span_id ride the LogRecord fields so Loki rows
        correlate with Tempo spans. Runs on the worker thread (or flush()).
        """
        from opentelemetry._logs import LogRecord
        from opentelemetry._logs.severity import SeverityNumber
        from opentelemetry.trace import (
            NonRecordingSpan,
            SpanContext,
            TraceFlags,
            set_span_in_context,
        )

        attributes: dict[str, Any] = {
            "event_name": event.event_name,
            "category": event.category,
            "level": event.level,
            "machine": event.machine,
            "process": event.process,
            "source": event.source,
        }
        if event.agent_id is not None:
            attributes["agent_id"] = event.agent_id
        if event.target_agent_id is not None:
            attributes["target_agent_id"] = event.target_agent_id
        body = json.dumps(
            {
                "ts": event.ts.isoformat(),
                "trace_id": event.trace_id,
                "span_id": event.span_id,
                "agent_id": event.agent_id,
                "machine": event.machine,
                "process": event.process,
                "category": event.category,
                "event_name": event.event_name,
                "level": event.level,
                "source": event.source,
                "target_agent_id": event.target_agent_id,
                "attributes": event.attributes,
            },
            default=str,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        # Trace correlation via `context` (the non-deprecated LogRecord
        # constructor): a NonRecordingSpan carries the captured trace/span ids
        # into the OTLP LogRecord fields. No ids -> no context -> trace_id 0
        # (OTLP's "no trace").
        context: Any = None
        if event.trace_id and event.span_id:
            span_context = SpanContext(
                trace_id=int(event.trace_id, 16),
                span_id=int(event.span_id, 16),
                is_remote=False,
                trace_flags=TraceFlags(TraceFlags.SAMPLED),
            )
            context = set_span_in_context(NonRecordingSpan(span_context))
        record = LogRecord(
            timestamp=int(event.ts.timestamp() * 1_000_000_000),
            observed_timestamp=time.time_ns(),
            context=context,
            severity_text=event.level,
            # The event.name semantic field is not in the stub overloads yet;
            # event_name rides as an attribute (Loki label) either way.
            severity_number=SeverityNumber(_SEVERITY_NUMBERS[event.level]),
            body=body,
            attributes=attributes,
        )
        self._logs.get_logger("ava.telemetry").emit(record)

    def _record_metrics(self, event: Event) -> None:
        """Map a telemetry event's numeric payload fields to OTLP instruments.

        Rules (deliberately simple, documented):
        - telemetry category only — log/audit events are the event stream, not
          a measurement.
        - int payload field -> Counter (token counts, event counts: things you
          sum). float -> Histogram (latencies/durations: things you
          percentile). `_METRIC_DISPOSITION` overrides per field: a float that
          is really a sum (cost_usd) records as a Counter; a rate snapshot
          (price_*) records nothing.
        - bool / short-str payload fields become datapoint attributes (model,
          ok, fn); `body` and strings over the length cap never do (content /
          cardinality guard).
        - None values are skipped (an absent optional metric is not zero).
        """
        if event.category != "telemetry":
            return
        attrs = self._metric_attributes(event)
        for key, value in event.attributes.items():
            # bool must be checked before int — `isinstance(True, int)` is True.
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            default_kind = "counter" if isinstance(value, int) else "histogram"
            kind = _METRIC_DISPOSITION.get((event.event_name, key), default_kind)
            if kind is None:
                continue
            inst = self._instrument(event.event_name, key, kind)
            if inst is None:
                continue
            if kind == "counter":
                inst.add(value, attrs)
            else:
                inst.record(value, attrs)

    def _metric_attributes(self, event: Event) -> dict[str, Any]:
        """The datapoint attribute set: process dimensions + declared payload
        scalars that pass the content/cardinality guard.

        Only payload-declared keys (`shared.events.contract.payload_keys`)
        become attributes — loguru decoration extras (msg, cache_pct,
        reason_pct, ...) are content, not dimensions: a per-event unique
        string (msg) would split every event into its own series, and a
        counter split into single-sample series reads as zero increments
        (increase() cannot see them). An event with no declared payload
        contributes no extra attributes."""
        from shared.events.contract import payload_keys

        attrs: dict[str, Any] = {"machine": event.machine, "process": event.process}
        if event.agent_id is not None:
            attrs["agent_id"] = event.agent_id
        payload = payload_keys(event.event_name)
        for key, value in event.attributes.items():
            if key in _NO_METRIC_ATTRS or not payload or key not in payload:
                continue
            if (isinstance(value, bool)) or (
                isinstance(value, str) and len(value) <= _MAX_METRIC_ATTR_CHARS
            ):
                attrs[key] = value
        return attrs

    def _instrument(self, event_name: str, field: str, kind: str) -> Any:
        """Lazily create (and cache) the instrument for one (event, field).

        Returns None (and reports) on a creation conflict — e.g. two event/
        field pairs collapsing onto one metric name with different kinds —
        so one bad pair sheds only its own metrics, never the batch."""
        key = (event_name, field)
        inst = self._instruments.get(key)
        if inst is not None:
            return inst
        name = f"ava_{event_name}_{_strip_unit_suffix(field)}"
        try:
            if kind == "counter":
                inst = self._meter.create_counter(
                    name,
                    unit=_unit_for(field),
                    description=f"{event_name}.{field} — OTLP-mapped from the unified event stream",
                )
            else:
                inst = self._meter.create_histogram(
                    name,
                    unit=_unit_for(field),
                    description=f"{event_name}.{field} — OTLP-mapped from the unified event stream",
                )
        except Exception as exc:  # report once per pair, skip the pair
            self._report(f"OTLP metric instrument {name!r} creation failed: {exc!r}")
            return None
        self._instruments[key] = inst
        return inst

    # ── diagnostics ──────────────────────────────────────────────────────────

    def _report(self, message: str) -> None:
        """Log an OTLP-side diagnostic through loguru, marked `_NO_EMITTER` so
        it reaches stderr/file sinks and never re-enters the event pipeline.
        Best-effort: never raises."""
        with contextlib.suppress(Exception):
            from shared.log import logger

            logger.bind(**{_NO_EMITTER: True}).warning(f"[otlp-exporter] {message}")


def _unit_for(field: str) -> str:
    """OTel unit for a payload field, from its name. Durations say what they
    are; everything else is a dimensionless count ("1")."""
    if field.endswith("_ms"):
        return "ms"
    if field.endswith("_seconds") or field in ("duration", "latency"):
        return "s"
    return "1"


def _strip_unit_suffix(field: str) -> str:
    """Instrument name for a payload field: the unit suffix comes off, because
    the OTel unit supplies it on export — `latency_ms` + unit "ms" would
    otherwise render as `ava_..._latency_ms_milliseconds_*` in Prometheus
    (the unit stated twice)."""
    for suffix in ("_ms", "_seconds"):
        if field.endswith(suffix):
            return field[: -len(suffix)]
    return field


def _metric_views() -> list[Any]:
    """Views shaping the LLM latency histograms: explicit LLM-scale buckets
    (the OTel defaults clip at 10s) and no `agent_id` attribute — latency
    percentiles are read per model/fleet, never per agent, and dropping the
    key removes the per-(agent, model) histogram fan-out (17 series each).
    Views match the INSTRUMENT name (unit suffix already stripped)."""
    from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View

    aggregation = ExplicitBucketHistogramAggregation(list(_LLM_LATENCY_BUCKETS_MS))
    return [
        View(
            instrument_name=name,
            aggregation=aggregation,
            attribute_keys={"machine", "process", "model"},
        )
        for name in ("ava_llm_usage_latency", "ava_llm_usage_decode")
    ]


def _metrics_resource() -> Any:
    """The metrics-side OTel Resource: `service.name=ava-<process>` (the
    bounded process dimension) + a per-process-instance `service.instance.id`.
    Without it every series lands as job="unknown_service" and two same-named
    processes exporting the same counter collide into ONE series with
    interleaved cumulative values (increase() reads garbage). Metrics only:
    the logs Resource is deliberately untouched — Loki default-promotes
    resource attributes to index labels, so a per-process-unique instance id
    there would mint a new stream per process start, and the service_name
    selector rewrite has its own coordinated change."""
    import uuid
    from importlib.metadata import version

    from opentelemetry.sdk.resources import Resource

    from shared.telemetry import process_name

    return Resource.create(
        {
            "service.namespace": "ava",
            "service.name": f"ava-{process_name()}",
            "service.instance.id": str(uuid.uuid4()),
            "service.version": version("ava"),
        }
    )


def _build_providers(endpoint: str) -> tuple[Any, Any]:
    """Build the production (LoggerProvider, MeterProvider) pair exporting
    OTLP/HTTP to ``endpoint`` (signal paths /v1/logs, /v1/metrics are appended
    by the exporters). Imports the OTel SDK lazily — the no-tracing path and
    flag-off processes never pay for it."""
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

    logs = LoggerProvider()
    logs.add_log_record_processor(
        BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{endpoint}/v1/logs"))
    )
    metrics = MeterProvider(
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"),
                export_interval_millis=_METRICS_INTERVAL_S * 1000,
            )
        ],
        resource=_metrics_resource(),
        views=_metric_views(),
    )
    return logs, metrics


# Module singleton — shared.telemetry calls the module functions below; tests
# replace `backend` wholesale with a providers-injected instance.
backend = _OtlpBackend()


def export_batch(events: list[Event]) -> None:
    """Dual-write one emitter batch to the OTLP backend (logs + metrics).

    No-op when AVA_TELEMETRY_OTLP_ENABLED is off; otherwise best-effort and
    fully isolated — see the module docstring."""
    backend.export_batch(events)


def register_observable_metric(
    name: str,
    *,
    kind: ObservableKind,
    callback: ObservableCallback,
    description: str,
    unit: str = "1",
) -> None:
    """Register a process-state metric on the shared OTLP backend."""
    try:
        backend.register_observable_metric(
            name,
            kind=kind,
            callback=callback,
            description=description,
            unit=unit,
        )
    except Exception as exc:
        backend._report(f"observable metric {name!r} registration failed: {exc!r}")


def flush() -> None:
    """Synchronously process queued OTLP events (test seam / exit)."""
    backend.flush()


def shutdown() -> None:
    """Flush + stop the OTLP backend (process exit, called by telemetry)."""
    backend.shutdown()
