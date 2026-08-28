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
  as attributes; event_name and agent_id also select each record's resource so
  Loki can index them without mixing event types in one resource batch;
  ``trace_id`` / ``span_id`` fill the LogRecord fields so logs correlate with
  Tempo spans.
- **metrics** — telemetry-category events become OTLP metrics (Prometheus).
  Each numeric payload field maps to one instrument named
  ``ava_<event_name>_<field>``: int -> Counter, float -> Histogram, with
  explicitly declared absolute-state fields exported as Observable Gauges
  (see ``_record_metrics`` for the rules); datapoint attributes are the
  process dimensions + declared payload scalars only (never loguru decoration
  extras — a per-event msg string would split every counter into its own
  series). Log/audit events produce no metrics: they are the event stream,
  not a measurement.
- **traces** — NOT exported here. ``shared/trace.py`` exports them to the same
  local collector, whose file exporter writes the standard OTLP/JSON mirror.
  ``ava trace ship`` is the separate recovery replay: gateway units dial Tempo
  directly; pure runners use the authenticated gateway collector ingress.

Failure isolation — the contract this module exists to keep: **the OTLP side
must never block, break, or slow the event drain after its JSONL mirror write.**
Three layers:

1. The emitter drain thread only does bounded ``put_nowait`` into this
   module's queue (shed, counted, reported) plus in-memory metric recordings —
   nothing here can block it.
2. All network I/O runs on SDK-owned threads (``BatchLogRecordProcessor`` /
   ``PeriodicExportingMetricReader``). Their exporters retry on their own
   clock and drop when their bounded queues fill; they cannot raise into any
   Ava thread.
3. Every entry point is suppress-guarded end to end, and the emitter wraps the
   call again (``_export_otlp``) — even a programming error here cannot cost a
   batch its JSONL copy.

Flag semantics: the implicit collector is available only to a registered
machine running against the production ``~/.ava`` cluster. Other processes,
including disposable exec children in test or ad-hoc homes, stay off unless an
operator explicitly sets ``AVA_TELEMETRY_OTLP_ENDPOINT``. Every allowed process
reads ``AVA_TELEMETRY_OTLP_ENABLED`` from the startup-frozen settings singleton
(``restart_required`` on the config fields). Exec children call ``warmup()``
before agent code runs so constructing the OTel SDK cannot first happen during
interpreter shutdown. This is **startup-applied**, matching every other config
field in the system — there is no live-reload mechanism in ``shared/config``,
and the isolation above makes the flag a rare emergency kill switch, not the
primary defense. Off means JSONL mirror only: Loki and Prometheus stop advancing.
Flipping it + restarting is the documented apply path.

Backend initialization is retried every five minutes after a failed collector
probe or SDK setup. Each disabled/recovered attempt is emitted as a real event,
not only through the ``_NO_EMITTER`` diagnostic path, so the surviving JSONL
mirror records the outage even while OTLP itself cannot carry the event.
"""

from __future__ import annotations

import contextlib
import json
import queue
import threading
import time
from collections.abc import Sequence
from functools import cache
from typing import Any

from shared.observability import (
    cluster_label,
    endpoint_override_is_explicit,
    gateway_observability_home,
    production_identity,
)
from shared.telemetry import Event
from shared.telemetry_otlp_gauges import GaugeValues, observable_gauge_callback, record_gauge

__all__ = [
    "COLLECTOR_RETRY_INTERVAL_S",
    "endpoint_reachable",
    "export_batch",
    "flush",
    "shutdown",
    "warmup",
]

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

# A failed collector probe costs up to 1.5 seconds. Retry on the drain thread
# every five minutes, not on every batch, so a missing sidecar cannot turn event
# volume into connection-probe volume.
COLLECTOR_RETRY_INTERVAL_S = 300

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
    # Absolute unresolved counts are non-monotonic. An ObservableGauge holds
    # the last value rather than adding each five-minute sample forever.
    ("resolution_status", "unresolved_warnings"): "gauge",
    ("resolution_status", "unresolved_errors"): "gauge",
    # The hourly maintenance pass refreshes these table high-water marks; a
    # gauge preserves the latest measurement between samples. The *_live
    # fields are the live tuple counts, emitted alongside so physical size can
    # be decomposed into live growth vs dead-tuple bloat.
    ("checkpoint_table_sizes", "blobs_bytes"): "gauge",
    ("checkpoint_table_sizes", "checkpoints_bytes"): "gauge",
    ("checkpoint_table_sizes", "writes_bytes"): "gauge",
    ("checkpoint_table_sizes", "blobs_live"): "gauge",
    ("checkpoint_table_sizes", "checkpoints_live"): "gauge",
    ("checkpoint_table_sizes", "writes_live"): "gauge",
}

# Histogram bucket boundaries for LLM-scale latencies (ms). The OTel defaults
# top out at 10000 — every call slower than 10s fell into +Inf and clipped
# p95/p50 at exactly 10s on the ops panels.
_LLM_LATENCY_BUCKETS_MS = (250, 500, 1000, 2000, 4000, 8000, 15000, 30000, 60000, 120000, 300000)

# Loguru extra key marking the OTLP side's own diagnostics, so they reach the
# stderr/file sinks and never re-enter the event pipeline (same contract as
# shared.telemetry._NO_EMITTER).
_NO_EMITTER = "_no_emitter"


@cache
def _observability_export_allowed() -> bool:
    """Whether this process may arm OTLP export, frozen once per process."""
    if endpoint_override_is_explicit("AVA_TELEMETRY_OTLP_ENDPOINT"):
        return True
    if not production_identity():
        return False
    home = gateway_observability_home()
    if home is None:
        return True
    from shared.observability import home_is_observability_station

    allowed = home_is_observability_station(home)
    if not allowed:
        from shared.log import logger

        logger.bind(**{_NO_EMITTER: True}).warning(
            "[otlp-exporter] OTLP export disabled: gateway home is not the "
            "observability station (no {} marker and no observability-station "
            "capability); set AVA_TELEMETRY_OTLP_ENDPOINT to use an explicit collector",
            home / "lgtm-host",
        )
    return allowed


def endpoint_reachable(endpoint: str) -> bool:
    """One quick preflight against an OTLP collector. Any HTTP answer proves a
    listener is up — including 4xx/5xx, which ``urlopen`` raises as
    HTTPError (the collector answers /v1/logs with 415 unless the body
    carries an OTLP content type). Only connection-level failures mean no
    collector (a fresh install without the LGTM stack), where building the
    SDK exporters would make their own threads log 'Exception while
    exporting' every interval forever. The probe posts an OTLP/JSON body
    so a healthy collector answers 200. A failed probe is retried every five
    minutes; disabled and recovered episodes are also reported as real events.

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


class _EventDimensionResourceExporter:
    """Give each event its own OTLP resource dimensions before serialization.

    A ``LoggerProvider`` has one static Resource, but Loki indexes resource
    attributes and the OTLP encoder groups a batch by that Resource. Rewriting
    a shared resource downstream therefore makes the last record's dimensions
    describe every record in the batch. This wrapper keeps one bounded SDK
    exporter worker while handing the encoder a resource that matches each
    individual record; the encoder then emits resource-homogeneous groups.
    """

    def __init__(self, exporter: Any) -> None:
        self._exporter = exporter

    def export(self, batch: Sequence[Any]) -> Any:
        from opentelemetry.sdk._logs import ReadableLogRecord
        from opentelemetry.sdk.resources import Resource

        resource_tagged: list[Any] = []
        for record in batch:
            dimensions = dict(record.resource.attributes)
            attributes = record.log_record.attributes
            dimensions["event_name"] = attributes["event_name"]
            if "agent_id" in attributes:
                dimensions["agent_id"] = attributes["agent_id"]
            if "cluster" in attributes:
                dimensions["cluster"] = attributes["cluster"]
            resource_tagged.append(
                ReadableLogRecord(
                    log_record=record.log_record,
                    resource=Resource(dimensions, schema_url=record.resource.schema_url),
                    instrumentation_scope=record.instrumentation_scope,
                    limits=record.limits,
                )
            )
        return self._exporter.export(resource_tagged)

    def shutdown(self) -> None:
        self._exporter.shutdown()


def _emit_backend_event(event_name: str, **attributes: Any) -> None:
    """Emit init status into the unified stream; never affect backend setup.

    When the collector is unavailable, the event reaches the JSONL mirror even
    though its OTLP copy cannot leave the process.
    """
    with contextlib.suppress(Exception):
        from shared import telemetry

        telemetry.emit("telemetry", event_name, attributes=attributes)


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
        self._gauge_values: GaugeValues = {}
        self._gauge_lock = threading.Lock()
        self._init_failed_at: float | None = None
        self._init_lock = threading.Lock()
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
                        "(OTLP side only; the JSONL mirror is unaffected)"
                    )
        for event in events:
            with contextlib.suppress(Exception):
                self._record_metrics(event)

    def flush(self, timeout: float = 2.0) -> None:
        """Synchronously process queued events on the calling thread.

        Test seam + shutdown helper. The worker thread may concurrently take
        events; each event is processed exactly once by whichever thread
        dequeues it, so a flush + worker race is harmless. Draining is
        non-blocking (``get_nowait``) — a flush must never add a multi-second
        wait to a short-lived process.

        After draining, the SDK batch processors are force-flushed (bounded
        by their own timeout): a short-lived process (the exec child) exits
        before the 5s batch window would fire on its own, so without this the
        queued OTLP records — SDK calls — never reach the collector."""
        del timeout  # signature kept for callers; the drain is best-effort
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            if event is None:
                break
            with contextlib.suppress(Exception):
                self._emit_log(event)
        with contextlib.suppress(Exception):
            if self._logs is not None:
                self._logs.force_flush(timeout_millis=500)
        with contextlib.suppress(Exception):
            if self._metric_provider is not None:
                self._metric_provider.force_flush(timeout_millis=500)

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
        """Read the startup-frozen OTLP flag. Any read failure degrades to
        off — the OTLP side must never be the reason an emit path breaks."""
        with contextlib.suppress(Exception):
            from shared.config import settings

            return (
                bool(settings.observability.telemetry_otlp_enabled)
                and _observability_export_allowed()
            )
        return False

    @staticmethod
    def _endpoint_reachable(endpoint: str) -> bool:
        """Module-level ``endpoint_reachable`` — see its docstring."""
        return endpoint_reachable(endpoint)

    def _ensure(self) -> bool:
        """Bring up the backend, retrying a failed init at five-minute cadence.

        The interval gate prevents per-batch probes. Initialization remains
        best-effort and never raises into the event drain.
        """
        if self._logs is not None:
            return True
        now = time.monotonic()
        if (
            self._init_failed_at is not None
            and now - self._init_failed_at < COLLECTOR_RETRY_INTERVAL_S
        ):
            return False
        with self._init_lock:
            if self._logs is not None:
                return True
            now = time.monotonic()
            if (
                self._init_failed_at is not None
                and now - self._init_failed_at < COLLECTOR_RETRY_INTERVAL_S
            ):
                return False

            endpoint: str | None = None
            try:
                if self._providers is not None:
                    logs, metric_provider = self._providers
                else:
                    from shared.config import settings

                    endpoint = settings.observability.telemetry_otlp_endpoint
                    if not self._endpoint_reachable(endpoint):
                        self._init_failed_at = time.monotonic()
                        self._report(
                            f"OTLP endpoint {endpoint} not answering — OTLP export disabled; "
                            f"retrying in {COLLECTOR_RETRY_INTERVAL_S}s"
                        )
                        _emit_backend_event(
                            "otlp_backend_disabled",
                            reason="endpoint not answering",
                            endpoint=endpoint,
                        )
                        return False
                    logs, metric_provider = _build_providers(endpoint)
                meter = metric_provider.get_meter("ava.telemetry")
                worker = threading.Thread(target=self._run, daemon=True, name="otlp-exporter")
                disabled_at = self._init_failed_at
                self._logs = logs
                self._metric_provider = metric_provider
                self._meter = meter
                self._thread = worker
                worker.start()
            except Exception as exc:
                self._logs = None
                self._metric_provider = None
                self._meter = None
                self._thread = None
                self._init_failed_at = time.monotonic()
                reason = f"init failed: {exc!r}"
                self._report(
                    "OTLP backend init failed — OTLP side disabled; "
                    f"retrying in {COLLECTOR_RETRY_INTERVAL_S}s: {exc!r}"
                )
                _emit_backend_event(
                    "otlp_backend_disabled",
                    reason=reason,
                    endpoint=endpoint,
                )
                return False

            self._init_failed_at = None
            if disabled_at is not None:
                _emit_backend_event(
                    "otlp_backend_recovered",
                    endpoint=endpoint,
                    disabled_s=max(0.0, now - disabled_at),
                )
            return True

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

        Body = the full event as JSON (the id-free mirror shape; the mirror row
        itself also carries the surrogate `id`, which the body deliberately does
        not). Attributes = the indexed
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
            "cluster": event.cluster,
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
                "cluster": event.cluster,
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
          is really a sum (cost_usd) records as a Counter; an absolute state
          (resolution_status) records an ObservableGauge; a rate snapshot
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
            elif kind == "histogram":
                inst.record(value, attrs)
            else:
                record_gauge(
                    self._gauge_values,
                    self._gauge_lock,
                    (event.event_name, key),
                    value,
                    attrs,
                )

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
            elif kind == "histogram":
                inst = self._meter.create_histogram(
                    name,
                    unit=_unit_for(field),
                    description=f"{event_name}.{field} — OTLP-mapped from the unified event stream",
                )
            else:
                inst = self._meter.create_observable_gauge(
                    name,
                    callbacks=[
                        observable_gauge_callback(
                            self._gauge_values, self._gauge_lock, (event_name, field)
                        )
                    ],
                    unit=_unit_for(field),
                    description=f"{event_name}.{field} — OTLP-mapped absolute state from the unified event stream",
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
    there would mint a new stream per process start. Per-agent event indexing
    (Task #1327) keeps this resource as-is: the collector promotes the
    per-record agent_id/event_name resource attributes (set in
    _EventDimensionResourceExporter) to Loki index labels, and the read side
    era-slices its selector around the cutover (shared/loki_index_labels.py)
    — no service_name rewrite needed."""
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
            "cluster": cluster_label(),
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
    log_exporter: Any = _EventDimensionResourceExporter(
        OTLPLogExporter(endpoint=f"{endpoint}/v1/logs")
    )
    logs.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
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


def warmup() -> None:
    """Bring up the enabled backend before short-lived code can emit events.

    The bounded endpoint preflight and SDK setup happen before agent code runs;
    a failed warmup is reported and retried after five minutes without ever
    raising into the caller.
    """
    with contextlib.suppress(Exception):
        if backend._enabled():
            backend._ensure()


def flush() -> None:
    """Synchronously process queued OTLP events (test seam / exit)."""
    backend.flush()


def shutdown() -> None:
    """Flush + stop the OTLP backend (process exit, called by telemetry)."""
    backend.shutdown()
