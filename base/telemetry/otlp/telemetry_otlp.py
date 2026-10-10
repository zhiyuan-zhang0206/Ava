"""OTLP export backend — the write side of the OTel + Tempo/Loki/Prometheus stack.

Exports unified emitter batches when the startup OTLP flag is enabled. The
JSONL mirror precedes this ordinary observation sink; OTLP does not replace it.

The endpoint (``AVA_TELEMETRY_OTLP_ENDPOINT``, default 127.0.0.1:4318) is the LOCAL OTel
Collector sidecar on every machine (task #1266, 2026-08-14): agents never dial a backend
directly. A gateway collector fans out logs -> loopback Loki and metrics -> loopback
Prometheus; a pure runner collector relays them to the gateway collector's authenticated
private-address OTLP receiver. A remote agent keeps the localhost producer endpoint — its
first hop is still its own sidecar. Three signals:

- **logs** — every ``Event`` becomes one OTLP LogRecord (Loki). The body is the
  full event as JSON (the same shape the JSONL mirror stores, so the mirror and
  Loki hold the same content class); the indexed dimensions
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
- **traces** — NOT exported here. ``base/telemetry/tracing.py`` exports them to the same
  local collector, whose file exporter writes the standard OTLP/JSON mirror.
  ``ava trace ship`` is the separate recovery replay: gateway units dial Tempo
  directly; pure runners use the authenticated gateway collector ingress.

Exporter log mapping and SDK failures retain their named isolation scopes.
The owned writer keeps provider creation, flush and teardown off exit callers;
finite observations report unfinished work without claiming SDK termination.

Export defaults require a registered production machine; explicit endpoints
allow other homes. Startup-frozen settings control enablement. First construction
is refused during interpreter finalization; disabled export keeps the JSONL mirror.

Child deferral (task #3816 M4b): an exec child arms `defer_until_exit()` before
its first record; batches are held in the bounded queue — OTel stack, settings
chain, and metric plumbing unimported — and complete at `finalize()` (clean
exit), on hold saturation, or at the max-age bound. The policy and its
semantics live in `base.telemetry.otlp.telemetry_otlp_defer`.

Backend initialization is retried every five minutes after a failed collector
probe or SDK setup. Each disabled/recovered attempt is emitted as a real event,
not only through the ``_NO_EMITTER`` diagnostic path, so the surviving JSONL
mirror records the outage even while OTLP itself cannot carry the event.
"""

from __future__ import annotations

import math
import queue
import sys
import threading
import time
from functools import cache
from typing import Any

from base.telemetry import (
    DrainPhase,
    DrainResult,
    DrainStatus,
    Event,
    failure_isolated,
    report_no_pipeline,
    report_sink_failure,
)
from base.telemetry.metrics import ci_runs_metrics
from base.telemetry.observability import (
    cluster_label,
    endpoint_override_is_explicit,
    gateway_observability_home,
    production_identity,
)
from base.telemetry.otlp import telemetry_otlp_metrics
from base.telemetry.otlp.telemetry_otlp_defer import ChildDeferral
from base.telemetry.otlp.telemetry_otlp_gauges import (
    GaugeValues,
    observable_gauge_callback,
    record_gauge,
)
from base.telemetry.otlp.telemetry_otlp_logs import _emit_log_record
from base.telemetry.otlp.telemetry_otlp_metrics import (
    _EVENT_LOOP_LAG_BUCKETS_MS as _EVENT_LOOP_LAG_BUCKETS_MS,
)
from base.telemetry.otlp.telemetry_otlp_metrics import (
    _LLM_LATENCY_BUCKETS_MS as _LLM_LATENCY_BUCKETS_MS,
)
from base.telemetry.otlp.telemetry_otlp_metrics import (
    _build_providers,
    _strip_unit_suffix,
    _unit_for,
)
from base.telemetry.otlp.telemetry_otlp_metrics import (
    _EventDimensionResourceExporter as _EventDimensionResourceExporter,
)
from base.telemetry.otlp.telemetry_otlp_metrics import (
    _metric_views as _metric_views,
)
from base.telemetry.otlp.telemetry_otlp_worker import OtlpWorker

__all__ = [
    "COLLECTOR_RETRY_INTERVAL_S",
    "defer_until_exit",
    "deferred_state",
    "endpoint_reachable",
    "export_batch",
    "finalize",
    "flush",
    "shutdown",
    "warmup",
]

# ── knobs ─────────────────────────────────────────────────────────────────────

# Bounded queue between the emitter drain thread and the OTLP worker. The SDK's
# own batch processors ALSO have a bounded queue (2048) and their emit() blocks
# when full — so this barrier must be no larger than the SDK's, and it is what
# turns "OTLP exporter thread stuck on a hung endpoint" into counted shedding
# instead of a stalled drain thread.
_QUEUE_MAXSIZE = 2048

# A failed collector probe costs up to 1.5 seconds. Retry on the drain thread
# every five minutes, not on every batch, so a missing sidecar cannot turn event
# volume into connection-probe volume.
COLLECTOR_RETRY_INTERVAL_S = 300
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
    ("event_log_drop", "last_dropped_at"): "gauge",
    ("llm_usage", "price_miss"): None,
    ("llm_usage", "price_hit"): None,
    ("llm_usage", "price_out"): None,
    ("llm_usage", "cost_usd"): "counter",
    # A compaction's source and replacement sizes are independent samples,
    # not cumulative byte counts. Its explicit `compactions=1` field remains
    # the counter used for frequency.
    ("compaction_completed", "history_chars"): "histogram",
    ("compaction_completed", "summary_chars"): "histogram",
    # Absolute unresolved/dismissed counts are non-monotonic. An
    # ObservableGauge holds the last value rather than adding each
    # five-minute sample forever.
    ("resolution_status", "unresolved_warnings"): "gauge",
    ("resolution_status", "unresolved_errors"): "gauge",
    ("resolution_status", "dismissed_warnings"): "gauge",
    ("resolution_status", "dismissed_errors"): "gauge",
    # PR-flow daily aggregates (task #2139): the macmini sampler re-emits its
    # whole trailing window on every run, so every numeric field is per-day
    # absolute state — a gauge replaces the value; a counter/histogram default
    # would accrue across re-emissions. queue_depth is a point-in-time
    # reading of the Trunk queue, same class.
    ("pr_flow_daily", "merged_count"): "gauge",
    ("pr_flow_daily", "ready_to_merge_median_seconds"): "gauge",
    ("pr_flow_daily", "ready_to_merge_p90_seconds"): "gauge",
    ("pr_flow_daily", "flake_new_quarantines"): "gauge",
    ("pr_flow_run", "queue_depth"): "gauge",
    # CI-run state is re-emitted absolute state, so every numeric field is a gauge (task #4014).
    **ci_runs_metrics.CI_RUN_GAUGE_DISPOSITIONS,
    # The agent registry max id is an absolute high-water mark, not a sum —
    # as an int it would default to a Counter and accrue value on every
    # sample. A gauge holds the latest sample (task #2010).
    ("agent_registry", "max_id"): "gauge",
    # The memory-search store's absolute state (task #2088): rows is an int
    # that would otherwise default to a Counter and accrue on every 60s
    # sample; last_save_seconds is a float duration that would default to a
    # Histogram — both are latest-value gauges.
    ("memory_search_stats", "rows"): "gauge",
    ("memory_search_stats", "last_save_seconds"): "gauge",
    # A root health timestamp is absolute freshness state. Summing it or
    # histogramming it would hide the age alert's only input.
    ("root_health_tick", "last_tick_timestamp_seconds"): "gauge",
    ("root_health_expected", "expected_since_timestamp_seconds"): "gauge",
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
    # Gateway process resources and SSE connection depth are absolute state.
    # Repeated samples replace the old value rather than accumulating it.
    ("sse", "active_connections"): "gauge",
    ("gateway_process", "cpu_percent"): "gauge",
    ("gateway_process", "rss_bytes"): "gauge",
    ("gateway_process", "fd_count"): "gauge",
}

# Loguru extra key marking the OTLP side's own diagnostics, so they reach the
# stderr/file sinks and never re-enter the event pipeline (same contract as
# base.telemetry._NO_EMITTER).
_NO_EMITTER = "_no_emitter"


@cache
def observability_export_allowed() -> bool:
    """Whether this process may arm OTLP export, frozen once per process."""
    if endpoint_override_is_explicit("AVA_TELEMETRY_OTLP_ENDPOINT"):
        return True
    if not production_identity():
        return False
    home = gateway_observability_home()
    if home is None:
        return True
    from base.telemetry.observability import home_is_observability_station

    allowed = home_is_observability_station(home)
    if not allowed:
        from base.log import logger

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
    (``base.telemetry.tracing.initialize_tracing``) — both arm their SDK exporters
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
    except OSError:  # URLError, refused / reset / timed-out connections: nothing answered
        return False


def _emit_backend_event(event_name: str, **attributes: Any) -> None:
    """Emit init status into the unified stream; never affect backend setup.

    When the collector is unavailable, the event reaches the JSONL mirror even
    though its OTLP copy cannot leave the process.
    """
    with failure_isolated("otlp init-status event"):
        from base import telemetry

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
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=queue_maxsize)
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
        self._worker: OtlpWorker | None = None
        self._closed = threading.Event()
        self._admission_lock = threading.Lock()
        self._producers_idle = threading.Event()
        self._producers_idle.set()
        self._active_producers = 0
        self._start_error: BaseException | None = None
        # Child deferral (task #3816 M4b) — policy in base.telemetry.otlp.telemetry_otlp_defer;
        # the lambdas look the backend methods up per call so test seams stay live.
        self._deferral = ChildDeferral(
            queue=self._queue,
            bring_up=lambda: self._enabled() and self._ensure(),
            record_metrics=lambda event: self._record_metrics(event),  # noqa: PLW0108 — per-call lookup keeps owner seams live
            export_live=lambda events: self._export_live(events),  # noqa: PLW0108 — per-call lookup keeps owner seams live
        )

    # ── public surface (called by base.telemetry) ─────────────────────────

    def export_batch(self, events: list[Event]) -> None:
        """Export one emitter batch to the OTLP backend. Never raises, never
        blocks the caller: logs enqueue to the bounded queue (shed when full),
        metrics record in memory (lock-free atomics).

        While deferred (exec-child arm, task #3816 M4b) the batch is held in the
        same bounded queue with no worker — no settings read, no OTel import —
        until saturation, the max-age timer, or finalize()/shutdown()."""
        if not events or self._closed.is_set():
            return
        if self._deferral.is_active() and self._logs is None:
            with self._admission_lock:
                if self._closed.is_set():
                    return
                self._active_producers += 1
                self._producers_idle.clear()
            try:
                self._deferral.hold_batch(events)
            finally:
                self._producer_finished()
            return
        if not self._enabled() or not self._ensure():
            return
        self._export_live(events)

    def _export_live(self, events: list[Event]) -> None:
        """Enqueue one batch on the live path + record its metrics.

        Split out of export_batch so the deferred hand-off (saturation,
        finalize) reuses the exact live semantics — including the counted shed
        when the queue stays full."""
        with self._admission_lock:
            if self._closed.is_set():
                return
            self._active_producers += 1
            self._producers_idle.clear()
        try:
            self._publish_live(events)
        finally:
            self._producer_finished()

    def _producer_finished(self) -> None:
        with self._admission_lock:
            self._active_producers -= 1
            if not self._active_producers:
                self._producers_idle.set()

    def _publish_live(self, events: list[Event]) -> None:
        """Record admitted events; teardown retains providers until this SDK use ends."""
        lost = 0
        example = None
        for event in events:
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                lost += 1
                example = event
        if lost and example is not None:
            from base.telemetry import _append_jsonl
            from base.telemetry.loss import report_loss

            with self._dropped_lock:
                self._dropped += lost
            report = report_loss(example, lost, "otlp")
            # The alarm itself bypasses both queues. Metrics can still export
            # while the log lane is full; the local mirror retains the evidence.
            _append_jsonl([report])
        for event in events:
            with failure_isolated("otlp metric mapping"):
                self._record_metrics(event)

    def flush(self, timeout: float = 2.0) -> DrainResult:
        """A finite receipt from the sole SDK writer, including provider flush."""
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("OTLP timeout must be finite and non-negative")
        worker = self._worker
        if worker is None:
            if self._start_error is not None:
                raise self._start_error
            completed = self._queue.empty()
            return DrainResult(
                DrainStatus.COMPLETED if completed else DrainStatus.UNFINISHED, DrainPhase.DRAIN
            )
        return worker.sync(timeout)

    def shutdown(self, timeout: float = 2.0) -> DrainResult:
        """Close admission and observe this exporter within one local deadline."""
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("OTLP timeout must be finite and non-negative")
        deadline = time.monotonic() + timeout
        worker, preparation_error = self._close_admission()
        deferred_finished = self._deferral.stop(timeout=max(0.0, deadline - time.monotonic()))
        if self._start_error is not None:
            raise self._start_error
        if worker is None:
            if preparation_error is not None:
                raise preparation_error
            completed = self._queue.empty() and deferred_finished and self._producers_idle.is_set()
            if not completed:
                self._report("OTLP shutdown degraded; deferred records remain unfinished")
            return DrainResult(
                DrainStatus.COMPLETED if completed else DrainStatus.UNFINISHED, DrainPhase.STOP
            )
        result = worker.stop(timeout=max(0.0, deadline - time.monotonic()))
        if preparation_error is not None:
            raise preparation_error
        if not deferred_finished:
            self._report("OTLP shutdown degraded; child deferral remains unfinished")
            return DrainResult(DrainStatus.UNFINISHED, DrainPhase.STOP)
        return result

    def _close_admission(self) -> tuple[OtlpWorker | None, BaseException | None]:
        """Keep a deferred attempt asynchronous, then stop even if preparation failed."""
        # The deferred hold can begin its existing exporter attempt without
        # waiting on SDK construction. It remains the same owned work after a
        # finite return; an empty hold still constructs nothing.
        preparation_error: BaseException | None = None
        try:
            if self._deferral.is_active() and not self._queue.empty() and not self._closed.is_set():
                self._get_worker()
        except BaseException as exc:
            preparation_error = exc
        with self._admission_lock:
            self._closed.set()
            worker = self._worker
            if worker is not None:
                worker.request_stop()
        return worker, preparation_error

    # ── child deferral (task #3816 M4b) ─────────────────────────────────────

    def defer_until_exit(self) -> None:
        """Arm a cold child hold; flag-off and already-live backends are no-ops."""
        if self._logs is not None:
            return
        self._deferral.arm()

    def finalize(self) -> None:
        """Complete an ordinary deferred hold, then observe its FIFO flush.

        Failed bring-up preserves the hold under the existing retry gate; an
        empty hold constructs no SDK resources. Shutdown has its finite seam.
        """
        if self._deferral.is_active():
            self._deferral.complete("finalize")
        self.flush()

    # ── flag + backend bring-up ──────────────────────────────────────────────

    @staticmethod
    def _enabled() -> bool:
        """Read the startup-frozen OTLP flag. Any read failure degrades to
        off — the OTLP side must never be the reason an emit path breaks."""
        try:
            from base.config import settings

            return (
                bool(settings.observability.telemetry_otlp_enabled)
                and observability_export_allowed()
            )
        except Exception as exc:
            report_sink_failure("otlp enabled-flag read", exc)
            return False

    @staticmethod
    def _endpoint_reachable(endpoint: str) -> bool:
        """Module-level ``endpoint_reachable`` — see its docstring."""
        return endpoint_reachable(endpoint)

    def _ensure(self) -> bool:
        """Resolve one lazy exporter attempt; a real stop wakes a stuck init waiter."""
        worker = self._get_worker()
        return worker is not None and worker.wait_ready()

    def _get_worker(self) -> OtlpWorker | None:
        if self._closed.is_set() or sys.is_finalizing() or self._start_error is not None:
            return None
        with self._init_lock, self._admission_lock:
            if self._closed.is_set():
                return None
            if self._worker is not None:
                if self._worker._thread.is_alive():
                    return self._worker
                self._worker._check_error()
                if not self._worker._cleanup_complete:
                    return self._worker  # retain uncertain SDK resources across retry opportunities
            if (
                self._init_failed_at is not None
                and time.monotonic() - self._init_failed_at < COLLECTOR_RETRY_INTERVAL_S
            ):
                return None
            try:
                worker = OtlpWorker(
                    event_queue=self._queue,
                    initialize=self._initialize_worker,
                    emit_log=self._emit_log,
                    record_metrics=self._record_metrics,
                    producers_idle=self._producers_idle,
                    deferred_idle=self._deferral.completion_idle,
                    deferred_active=self._deferral.is_active,
                )
                self._worker = worker
                self._thread = worker._thread
            except BaseException as exc:
                self._start_error = exc
                report_sink_failure("otlp worker start", exc)
                return None
        return worker

    def _initialize_worker(self, worker: OtlpWorker) -> bool:
        """Keep the existing named SDK init isolation and every partial resource."""
        endpoint: str | None = None
        disabled_at = self._init_failed_at
        try:
            if self._providers is not None:
                worker.logs, worker.metrics = self._providers
            else:
                from base.config import settings

                endpoint = settings.observability.telemetry_otlp_endpoint
                if not self._endpoint_reachable(endpoint):
                    self._init_failed_at = time.monotonic()
                    self._report(
                        f"OTLP endpoint {endpoint} not answering — OTLP export disabled; retrying in {COLLECTOR_RETRY_INTERVAL_S}s"
                    )
                    _emit_backend_event(
                        "otlp_backend_disabled", reason="endpoint not answering", endpoint=endpoint
                    )
                    return False
                worker.logs, worker.metrics = _build_providers(
                    endpoint,
                    keep_logs=lambda provider: setattr(worker, "logs", provider),
                    keep_reader=lambda reader: setattr(worker, "metric_reader", reader),
                    keep_metrics=lambda provider: setattr(worker, "metrics", provider),
                )
            meter = worker.metrics.get_meter("ava.telemetry")
            self._logs, self._metric_provider, self._meter = worker.logs, worker.metrics, meter
        except Exception as exc:
            self._init_failed_at = time.monotonic()
            self._report(
                f"OTLP backend init failed — OTLP side disabled; retrying in {COLLECTOR_RETRY_INTERVAL_S}s: {exc!r}"
            )
            _emit_backend_event(
                "otlp_backend_disabled", reason=f"init failed: {exc!r}", endpoint=endpoint
            )
            return False
        self._init_failed_at = None
        if disabled_at is not None:
            _emit_backend_event(
                "otlp_backend_recovered",
                endpoint=endpoint,
                disabled_s=max(0.0, time.monotonic() - disabled_at),
            )
        return True

    # ── signal mapping ───────────────────────────────────────────────────────

    def _emit_log(self, event: Event) -> None:
        """Map one Event to an OTLP LogRecord and emit it.

        The mapping body lives in `base.telemetry.otlp.telemetry_otlp_logs` (split for the
        800-line ceiling); runs on the worker thread (or flush()).
        """
        _emit_log_record(self._logs, event)

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
                    max_only=(event.event_name, key) == ("event_log_drop", "last_dropped_at"),
                )

    def _metric_attributes(self, event: Event) -> dict[str, Any]:
        """The datapoint attribute set: process dimensions + declared payload
        scalars that pass the content/cardinality guard.

        Only payload-declared keys (`base.events.contract.payload_keys`)
        become attributes — loguru decoration extras (msg, cache_pct,
        reason_pct, ...) are content, not dimensions: a per-event unique
        string (msg) would split every event into its own series, and a
        counter split into single-sample series reads as zero increments
        (increase() cannot see them). An event with no declared payload
        contributes no extra attributes."""
        from base.events.contract import payload_keys

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
        Best-effort: never raises, falls back to stderr."""
        report_no_pipeline(f"[otlp-exporter] {message}")


def _metrics_resource() -> Any:
    """Build the metric resource through the legacy monkeypatch seam."""
    original_cluster_label = telemetry_otlp_metrics.cluster_label
    telemetry_otlp_metrics.cluster_label = cluster_label
    try:
        return telemetry_otlp_metrics._metrics_resource()
    finally:
        telemetry_otlp_metrics.cluster_label = original_cluster_label


# Module singleton — base.telemetry calls the module functions below; tests
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
    with failure_isolated("otlp warmup"):
        if backend._enabled():
            backend._ensure()


def defer_until_exit() -> None:
    """Arm deferred export on the process backend (task #3816 M4b).

    The exec-child arm path; see `_OtlpBackend.defer_until_exit`.
    """
    backend.defer_until_exit()


def finalize() -> None:
    """Complete a deferred hold and flush (clean exit / shutdown, task #3816)."""
    backend.finalize()


def deferred_state() -> bool:
    """Whether the process backend currently holds a deferred backlog."""
    return backend._deferral.is_active()


def flush() -> None:
    """Synchronously process queued OTLP events (test seam / exit)."""
    backend.flush()


def shutdown(timeout: float = 2.0) -> DrainResult:
    """Finitely stop the OTLP backend and report any residual owned exporter work."""
    return backend.shutdown(timeout)
