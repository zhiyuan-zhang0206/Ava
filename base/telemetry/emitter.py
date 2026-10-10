"""Unified event emitter — the single entry point for every event in every process.

One event stream serves audit, telemetry, and log records under one schema and
one correlation key (`trace_id`). This module is its only writer.

Pipeline (Layer 1): a bounded queue and drain thread batch every event into
local JSONL mirrors, the `telemetry_events` table (telemetry and log events), best-effort
compact metrics, and OTLP logs/metrics. The record of an event is a Postgres table: audit
events in `audit_events` (written before the event is emitted), telemetry and log events in
`telemetry_events` (written here, from the drain thread, with the mirror as the fallback).
Loki and Prometheus hold the observation copy for Grafana and short windows.

Backpressure sheds records under overload, audit events included: their record
is `audit_events`, written before the event is emitted, so shedding the
projection loses nothing. Trace ids are captured at enqueue, and machine and
cluster dimensions are always populated.

Emit is best-effort and never raises: a broken sink must not crash the caller
(JSONL mirror + loguru file sinks are the durable backfill for everything
that reaches the drain thread; the mirror is replayed into `telemetry_events`).
Startup init (`init_telemetry`) is the one place that fails loud — a process
whose event pipeline cannot come up should not start silently blind.

Import discipline: this module imports `base.log` and `base.db` lazily
(inside functions) — `base/db/__init__.py` imports `base/log/__init__.py` at module scope
for `logger`, so a top-level import of either from here is a circular-import
failure for any process that reaches `base.db` first.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import math
import queue
import socket
import sys
import threading
import time
import traceback
from collections.abc import Callable, Generator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from hashlib import blake2b
from typing import Any, Literal

from base.events.contract import EVENTS
from base.events.contract import category_for_kind as registry_category
from base.paths import logs_dir
from base.telemetry.emitter_sync import DrainPhase, DrainResult, DrainStatus, SyncReceipt
from base.telemetry.observability import cluster_label
from base.telemetry.serialization import event_line, event_line_digest, event_payload

__all__ = [
    "Category",
    "DrainPhase",
    "DrainResult",
    "DrainStatus",
    "Event",
    "category_for_kind",
    "emit",
    "emit_prepared",
    "event_id",
    "event_line",
    "event_line_digest",
    "event_payload",
    "flush",
    "init_telemetry",
    "prepare_event",
    "stop",
]

Category = Literal["audit", "telemetry", "log"]
Level = Literal["debug", "info", "warning", "error", "critical"]

# Batch-write shape — within the design's 50-500/batch window, and the same
# cadence the former loguru Postgres sink used (50 / 0.5 s): a burst costs one
# round-trip per batch while a lone record still lands within half a second.
_BATCH_SIZE = 100
_FLUSH_INTERVAL_S = 0.5
# Queue bound: what stops a producer that outruns the drain thread from growing
# process memory without limit. Past this point records are shed (see
# `_EventPipeline.enqueue`) and `dropped` says how much. A shed record is gone
# from EVERY sink — the JSONL mirror only ever holds what reached the drain
# thread.
_QUEUE_MAXSIZE = 10_000

# JSONL mirror retention (day-stamped files, like the trace mirror).
_JSONL_RETENTION_DAYS = 7


def event_id(line: str, ts_ns: int) -> int:
    """Return the stable surrogate id shared by mirror and Loki event rows."""
    return int.from_bytes(blake2b(f"{ts_ns}:{line}".encode(), digest_size=8).digest(), "big")


@dataclass(frozen=True)
class Event:
    """One event in the unified stream — OTel LogRecord semantics (events = logs
    with names), the shape the event stream carries."""

    ts: datetime
    trace_id: str | None
    span_id: str | None
    agent_id: int | None
    machine: str
    cluster: str
    process: str
    category: Category
    event_name: str
    level: Level
    source: str
    target_agent_id: int | None
    attributes: dict[str, Any] = field(default_factory=dict[str, Any])


def event_row(event: Event) -> dict[str, Any]:
    """Canonical mirror row and stable identity shared by metric projection."""
    body = event_payload(event)
    body_str = event_line(event)
    ts_ns = int(event.ts.timestamp() * 1_000_000_000)
    return {**body, "id": event_id(body_str, ts_ns)}


# Telemetry event names — derived from the event contract registry
# (base/events/contract.py EVENTS, R2-C): the registry is the single source
# of truth; this set is its category projection (kept as a module-level name
# because tests/lint import it). Adding an event = one registry entry; this
# view updates automatically.
_TELEMETRY_KINDS = frozenset(name for name, spec in EVENTS.items() if spec.category == "telemetry")


def category_for_kind(event_name: str) -> Category:
    """Map an event name to its declared category (registry EVENTS, R2-C);
    a name with no declaration falls back to the bare-log category."""
    return registry_category(event_name)


def capture_trace_ids() -> tuple[str | None, str | None]:
    """Read the current OTel span's trace_id/span_id, if any.

    Called at enqueue time — the drain thread runs outside the span context, so
    capturing there would lose every trace id. Imported lazily: processes
    without OTel tracing should not pay the import for an always-None path.
    """
    try:
        from opentelemetry import trace as otel_trace

        span = otel_trace.get_current_span()
        ctx = span.get_span_context()
        if ctx.is_valid:
            return format(ctx.trace_id, "032x"), format(ctx.span_id, "016x")
    except ImportError:
        return None, None  # OTel is not installed in this process: no trace to capture
    except Exception as exc:  # trace capture must never break an emit — but is never silent
        report_sink_failure("trace-id capture", exc)
    return None, None


def _resolve_machine() -> str:
    """The machine dimension. `machine_name()` is the real identity (set on
    multi-machine units); anything without one (tests, ad-hoc scripts) falls
    back to the hostname so a row always carries the dimension.

    Only the documented absence (`MachineNameMissing`) falls back silently.
    Any OTHER failure is a programming error and must not be folded into
    hostname — it would pin the process's whole telemetry lifetime to a
    wrong dimension with zero trace (audit 2026-08-08 P2: a transient early
    failure split one machine's metric series into two)."""
    from base.cluster.machine import MachineNameMissing, machine_name
    from base.log import logger

    try:
        return machine_name()
    except MachineNameMissing:
        return socket.gethostname()
    except Exception:
        logger.exception("machine_name() failed unexpectedly — falling back to hostname")
        return socket.gethostname()


# ── pipeline state (per-process singleton) ────────────────────────────────────

# The emitter pipeline; None until first init/emit. Dict mutation avoids ruff
# PLW0603 the same way base/telemetry/tracing.py does.
_state: dict[str, Any] = {
    "pipeline": None,
    "process": "unknown",
    "agent_id": None,
    "machine": None,
    "cluster": None,
    "jsonl_day": None,
}


def _prune_jsonl_mirror() -> None:
    """Delete day-stamped mirror files older than their retention tier.

    Runs once per day (guarded by the `jsonl_day` stamp). The mirror is the
    durable fallback for audit events; log-stream lines are also held by the
    loguru file sinks, so the mirror's own retention is what bounds its disk
    footprint."""
    cutoff = (datetime.now(UTC) - timedelta(days=_JSONL_RETENTION_DAYS)).strftime("%Y%m%d")
    # `.rollup.jsonl` is the retired filtered tier; leftovers age out with the full mirror.
    for path in (
        *logs_dir().glob("events-????????.jsonl"),
        *logs_dir().glob("events-????????.rollup.jsonl"),
    ):
        day = path.name.removeprefix("events-").split(".", 1)[0]
        if day.isdigit() and day < cutoff:
            with contextlib.suppress(OSError):
                path.unlink()


def _append_jsonl(events: list[Event]) -> None:
    """Append the batch to today's mirror file, one JSON line per event.

    Each row carries the stable surrogate ``id`` derived from its id-free body
    and timestamp, matching the id Loki's read path returns for the same event.

    Best-effort — the mirror is a fallback, not a critical path; a write
    failure must never break the batch. But it must not be SILENT either:
    the mirror is the durable local copy, so a sustained mirror failure is
    reported (first + every 50th) — a disk-full / permission error would
    otherwise degrade it without a trace. Single write() per line with
    O_APPEND semantics (opened in append mode) keeps concurrent processes
    from interleaving."""
    day = datetime.now(UTC).strftime("%Y%m%d")
    try:
        lines = [
            json.dumps(event_row(e), default=str, separators=(",", ":"), ensure_ascii=False) + "\n"
            for e in events
        ]
        path = logs_dir() / f"events-{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write("".join(lines))
    except Exception as exc:  # report, never raise
        global _jsonl_failures  # noqa: PLW0603 — module-level counter
        _jsonl_failures += 1
        if _jsonl_failures == 1 or _jsonl_failures % 50 == 0:
            report_no_pipeline(
                "[event-emitter] JSONL mirror write failed ({n} consecutive) — "
                "the mirror is the durable local copy; a sustained failure "
                "means batches are not landing in it: {err}",
                n=_jsonl_failures,
                err=repr(exc),
            )
    else:
        # Once per day, after a write that landed: a prune failure is its own report and
        # never a second one for the same disk fault.
        if _state["jsonl_day"] != day:
            _state["jsonl_day"] = day
            with failure_isolated("jsonl mirror prune"):
                _prune_jsonl_mirror()


def _export_otlp(events: list[Event]) -> None:
    """Best-effort dual-write of a batch to the OTLP backend (logs + metrics).

    Runs on the drain thread right after the JSONL mirror write. The OTLP
    side is fully failure-isolated (bounded queue, drop semantics, SDK-owned
    export threads — see `base.telemetry.otlp.telemetry_otlp`), and this call is
    isolated end to end, so even a programming error there must not cost
    the batch its mirror copy or raise into the drain thread — it is reported."""
    with failure_isolated("otlp export"):
        from base.telemetry.otlp import telemetry_otlp  # deferred — heavy OTel imports

        telemetry_otlp.export_batch(events)


# ── drain-thread failure visibility ─────────────────────────────────────────
#
# The drain thread's mirror write failures used to be invisible (whole batch
# swallowed by suppress(), zero logs). This counter + the marker below keep a
# sustained failure loud without flooding: the first failure and every 50th
# after it are reported. (The PG write — the other former failure source — was
# retired with the LGTM cutover, task #1197.)
_jsonl_failures = 0

# Loguru extra key marking the emitter's own diagnostics. Records carrying it
# are filtered OUT of the emitter adapter's sink (see
# `base.log.add_postgres_sink`) so they reach stderr / JSONL file sinks only
# and never re-enter this pipeline — a mirror-down process would otherwise loop
# failure → warning → emit → failure forever.
_NO_EMITTER = "_no_emitter"


def report_no_pipeline(
    message: str, *, level: str = "warning", exc: BaseException | None = None, **extra: Any
) -> None:
    """Log a drain-thread diagnostic through loguru, marked `_NO_EMITTER` so
    the emitter adapter skips it; `exc` attaches that exception's traceback.
    Never raises, never blocks — and never silent: when loguru itself fails the
    diagnostic goes to stderr."""
    try:
        from base.log import logger

        # **{_NO_EMITTER: True} — bind() takes literal kwargs, so the marker
        # key must be the constant's VALUE, not its name.
        bound = logger.bind(**{_NO_EMITTER: True})
        if exc is not None:
            bound = bound.opt(exception=exc)
        bound.log(level.upper(), message, **extra)
    except Exception as log_exc:
        try:
            sys.stderr.write(
                f"{level.upper()}: {message} {extra!r} "
                f"(diagnostic logging failed: {log_exc!r}; cause: {exc!r})\n"
            )
        except (OSError, ValueError):
            return  # stderr is closed or detached: no reporting channel is left


# Failures per emitter-plumbing seam, for `report_sink_failure`.
_sink_failures: dict[str, int] = {}


def report_sink_failure(sink: str, exc: BaseException) -> None:
    """Report a failure of a best-effort side channel (an emitter sink export,
    the lazy pipeline init, an exit-time close, SDK-call or billing telemetry)
    with its traceback — the first and every 50th per `sink`, like the JSONL
    mirror: a seam that fails on every call must be loud without flooding the
    log. Goes through the `_no_emitter` path, so a failing telemetry pipeline
    cannot loop its own report back into itself."""
    n = _sink_failures[sink] = _sink_failures.get(sink, 0) + 1
    if n == 1 or n % 50 == 0:
        report_no_pipeline(
            "[best-effort] {sink} failed ({n} time(s) in this process; the first and every "
            "50th are reported); carrying on without it: {err}",
            sink=sink,
            n=n,
            err=repr(exc),
            exc=exc,
        )


@contextlib.contextmanager
def failure_isolated(sink: str) -> Generator[None]:
    """Isolate one best-effort emitter seam: an `Exception` in the body must not
    reach the producer or the drain thread, and is reported, not swallowed."""
    try:
        yield
    except Exception as exc:
        report_sink_failure(sink, exc)


def _write_batch(events: list[Event]) -> None:
    """Mirror first, then the telemetry_events record, the compact metric projection
    and OTLP export.

    Every sink is failure-isolated. The projection cannot turn an observation into a
    billing proof; the telemetry_events sink reports its own failures.
    """
    if not events:
        return
    _append_jsonl(events)
    with failure_isolated("telemetry_events store"):
        from base.db import Database
        from base.telemetry.event_store import store_events

        store_events(Database.from_settings(), events)
    try:
        from base.db import Database
        from base.telemetry.metrics.observed_metrics import project_events

        project_events(Database.from_settings(), events)
    except Exception as exc:
        report_no_pipeline("[observed-metrics] sink unavailable: {err}", err=repr(exc))
    _export_otlp(events)


class _EventPipeline:
    """Bounded queue + drain thread owning all event persistence for the process.

    Same shape as the former loguru Postgres sink (which this replaces): the
    queue bound is the backpressure, the drain thread batches, and shed records
    are counted and reported as one `event_log_drop` event per flush so the ops
    monitor panel keeps its backlog metric."""

    def __init__(
        self,
        *,
        writer: Callable[[list[Event]], None] | None = None,
        batch_size: int = _BATCH_SIZE,
        flush_interval_s: float = _FLUSH_INTERVAL_S,
        queue_maxsize: int = _QUEUE_MAXSIZE,
    ) -> None:
        if writer is None:
            writer = _write_batch
        self._writer = writer
        self._batch_size = batch_size
        self._flush_interval_s = flush_interval_s
        self._queue: queue.Queue[Event | SyncReceipt] = queue.Queue(maxsize=queue_maxsize)
        self._drop_reported_at = 0.0
        self._drop_example: Event | None = None
        self.dropped = 0  # records shed because the queue was full since the last flush
        # enqueue() runs on producer threads while _flush() (drain thread)
        # reads and zeroes the counter — `+=` is not atomic under the GIL, so
        # the read-modify-write pair is serialized.
        self._dropped_lock = threading.Lock()
        self._admission_lock = threading.Lock()
        self._stop_requested = threading.Event()
        self._finished = threading.Event()
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._drain, daemon=True, name="event-emitter")
        self._thread.start()

    def enqueue(self, event: Event) -> None:
        """Producer path: shed full, closed or failed admission without waiting on writes."""
        if not self._admit(event):
            self._record_drop(event)

    def _admit(self, event: Event | SyncReceipt) -> bool:
        with self._admission_lock:
            if self._stop_requested.is_set() or self._finished.is_set():
                return False
            try:
                self._queue.put_nowait(event)
                return True
            except queue.Full:
                return False

    def _record_drop(self, event: Event) -> None:
        with self._dropped_lock:
            self.dropped += 1
            self._drop_example = replace(event, ts=datetime.now(UTC))
            now = time.monotonic()
            due = self.dropped == 1 or now - getattr(self, "_drop_reported_at", 0.0) >= 5
            if due:
                self._drop_reported_at = now
        if due:
            from base.telemetry.loss import report_loss

            report_loss(event, 1, "emitter")

    def flush(self) -> DrainResult:
        """Acknowledge queued and held records through the sole drain writer."""
        return self.sync()

    def _check_error(self) -> None:
        if self._error is not None:
            raise self._error

    def _result(self, *, completed: bool, phase: DrainPhase) -> DrainResult:
        self._check_error()
        result = DrainResult(DrainStatus.COMPLETED if completed else DrainStatus.UNFINISHED, phase)
        if not completed:
            report_no_pipeline(
                "[event-emitter] {operation} timed out; telemetry shutdown degraded: "
                "unfinished ordinary records may be lost or land later",
                operation="stop()" if phase is DrainPhase.STOP else "sync()",
            )
        return result

    def sync(self, timeout: float = 5.0, *, bounded: bool = False) -> DrainResult:
        """Wait on a distinct FIFO receipt within one end-to-end deadline.

        All ordinary telemetry barriers are finite. ``bounded`` remains accepted
        for existing close callers. A stuck writer is never rescued by another
        writer; unfinished delivery is reported and returned to the caller.
        A terminal worker failure is raised with its original exception.
        """
        del bounded  # retained call compatibility; every ordinary barrier is now finite
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("event drain timeout must be finite and non-negative")
        if threading.current_thread() is self._thread:
            raise RuntimeError("event drain cannot wait on its own barrier")
        deadline = time.monotonic() + timeout
        receipt = SyncReceipt()
        admitted = False
        while not admitted:
            self._check_error()
            if self._finished.is_set():
                return self._result(completed=True, phase=DrainPhase.DRAIN)
            admitted = self._admit(receipt)
            remaining = deadline - time.monotonic()
            if not admitted and remaining <= 0:
                return self._result(completed=False, phase=DrainPhase.MARKER)
            if not admitted:
                self._finished.wait(min(0.01, remaining))
        while not receipt.done.is_set():
            self._check_error()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._result(completed=False, phase=DrainPhase.DRAIN)
            receipt.done.wait(min(0.01, remaining))
        return self._result(completed=True, phase=DrainPhase.DRAIN)

    def stop(self, timeout: float = 5.0) -> DrainResult:
        """Close admission, request the owned worker's exit, and join finitely.

        The stop request never enters the bounded event queue. Even a full queue
        and blocked writer leave the caller with an explicit unfinished result.
        Repeated stop calls observe late completion or the original failure.
        """
        if not math.isfinite(timeout) or timeout < 0:
            raise ValueError("event drain timeout must be finite and non-negative")
        deadline = time.monotonic() + timeout
        with self._admission_lock:
            self._stop_requested.set()
        if threading.current_thread() is self._thread:
            raise RuntimeError("event drain cannot join itself")
        self._thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return self._result(completed=not self._thread.is_alive(), phase=DrainPhase.STOP)

    def _flush(self, batch: list[Event]) -> None:
        """Write loss summaries directly; a saturated queue cannot shed its own alarm."""
        with self._dropped_lock:
            n = self.dropped
            self.dropped = 0
            example = self._drop_example
            self._drop_example = None
        if n and example is not None:
            from base.telemetry.loss import loss_event

            batch = [*batch, loss_event(example, n, "emitter", dropped_at=example.ts)]
        if not batch:
            return
        self._writer(batch)

    def _drain(self) -> None:
        """Own all writes and retain terminal failures for barrier/stop callers."""
        try:
            self._run_drain()
        except BaseException as exc:
            self._error = exc
            report_no_pipeline("[event-emitter] drain failed: {err}", err=repr(exc), exc=exc)
            # Early emit-before-init callers may have no logging sink yet.
            with contextlib.suppress(OSError, ValueError):
                traceback.print_exception(exc, file=sys.stderr)
        finally:
            self._finished.set()

    def _run_drain(self) -> None:
        """Flush batches and receipts in FIFO order; stop drains closed admission."""
        batch: list[Event] = []
        deadline = time.monotonic() + self._flush_interval_s
        while True:
            stopping = self._stop_requested.is_set()
            timeout = 0.0 if stopping else min(0.05, max(0.0, deadline - time.monotonic()))
            try:
                event = self._queue.get(timeout=timeout)
            except queue.Empty:
                if stopping or time.monotonic() >= deadline:
                    self._flush(batch)
                    batch = []
                    deadline = time.monotonic() + self._flush_interval_s
                if stopping:
                    return
                continue
            if isinstance(event, SyncReceipt):
                self._flush(batch)
                batch = []
                event.done.set()
                deadline = time.monotonic() + self._flush_interval_s
                continue
            batch.append(event)
            if len(batch) >= self._batch_size:
                self._flush(batch)
                batch = []
                deadline = time.monotonic() + self._flush_interval_s


def _open_pipeline() -> _EventPipeline:
    """Build the process pipeline: queue + drain thread. Startup does not depend on the
    DB: the `telemetry_events` sink connects lazily on the drain thread and backs off
    when the database does not answer, and the JSONL mirror is written first."""
    return _EventPipeline()


def process_name() -> str:
    """This process's bound identity (`init_telemetry(process=...)`), the
    bounded dimension every telemetry record carries as `process`. The OTLP
    backend stamps it into the metrics Resource (`service.name=ava-<process>`)
    so same-named counters from different process kinds cannot collide into
    one Prometheus series."""
    return str(_state["process"])


def init_telemetry(*, process: str = "unknown", agent_id: int | None = None) -> None:
    """Bind process identity and bring up the event pipeline. Idempotent.

    Called from the loguru `init_*` entry points (the single boot seam every
    process shares): `init_gateway_process(name)` → process=name; the exec
    child's `add_postgres_sink` → process="agent-exec" plus its agent id. The
    first call opens the drain thread; later calls only refresh the identity
    binding. Startup never depends on the DB (the `telemetry_events` sink connects
    lazily on the drain thread)."""
    _state["process"] = process
    _state["agent_id"] = agent_id
    if _state["machine"] is None:
        _state["machine"] = _resolve_machine()
    if _state["cluster"] is None:
        _state["cluster"] = cluster_label()
    if _state["pipeline"] is None:
        _state["pipeline"] = _open_pipeline()


def _ambient_agent_id() -> int | None:
    """Default identity established at process startup, if this process owns an agent."""
    return _state["agent_id"]


def _ensure_pipeline() -> _EventPipeline | None:
    """Lazy-init fallback for emit-before-init callers. Best-effort: a process
    whose pipeline init fails degrades to dropping (never raises, never
    blocks)."""
    if _state["pipeline"] is None:
        with failure_isolated("pipeline init"):
            init_telemetry()
    return _state["pipeline"]


def _as_utc(ts: datetime | None) -> datetime:
    """Normalize one event timestamp onto the stream's single clock: UTC.

    `emit()`'s default is `datetime.now(UTC)`; an explicit `ts` — the loguru
    adapter passes loguru's local-zone record time, replay/migration passes
    stored rows — is converted to UTC here so the Event and every
    serialization of it (the JSONL mirror `ts` field, the OTLP body) carries
    one offset. A naive `ts` is UTC by contract (the one-time-source rule);
    treating it as local would mix clocks. The 2026-08-25 mirror audit: loguru
    rows wrote +08:00 into the mirror while direct emits wrote +00:00, which
    made any local-wall-clock filter of the mirror misread gateway telemetry
    as missing since the UTC-day rollover (task #1638)."""
    if ts is None:
        return datetime.now(UTC)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def emit(
    category: Category,
    event_name: str,
    *,
    level: Level = "info",
    agent_id: int | None = None,
    source: str = "system",
    target_agent_id: int | None = None,
    attributes: dict[str, Any] | None = None,
    ts: datetime | None = None,
    capture: Callable[[Event], Event] | None = None,
) -> None:
    """Enqueue one event into the unified stream. Never raises — except for a
    contract violation (R2-C): an `event_name` with no `EventSpec` in
    `base/events/declarations`, or a category that contradicts the
    declaration, raises `ValueError` (AGENTS.md "explode on unknown enums").
    An explicitly supplied producer capture callback also propagates its errors
    before observation delivery; no current participant is inferred.
    The loguru adapter wraps its call with `catch=True`, so a logging line
    that drifts off-contract stays visible (JSONL mirror) without crashing
    the producer.

    An event is a named log record (OTel LogRecord semantics: events = logs
    with names); `event_name` is that name — the `event.name` field, and the
    registry key (`EVENTS`).

    `trace_id`/`span_id` are captured from the active OTel span at this point
    (enqueue time — the drain thread runs outside the span context). `agent_id`
    falls back to the process-bound value (init_telemetry); explicit wins.
    `ts` defaults to the PROCESS clock at enqueue time (`datetime.now(UTC)`) —
    the one time source for the entire stream, and an explicit `ts` is
    normalized to UTC before enqueue (`_as_utc`), so every serialization of
    the Event (the JSONL mirror `ts` field, the OTLP body) carries one offset.
    Callers pass an explicit `ts` only for loguru-adapter records (loguru
    stamps local zone — normalized here) and replayed/migrated rows; a
    DB-derived timestamp would silently mix two clocks (W7 rewired the last
    DB-clock writers, heartbeat + delivery watchdog, onto this path)."""
    event = prepare_event(
        category,
        event_name,
        level=level,
        agent_id=agent_id,
        source=source,
        target_agent_id=target_agent_id,
        attributes=attributes,
        ts=ts,
    )
    emit_prepared(event, **({"capture": capture} if capture is not None else {}))


def prepare_event(
    category: Category,
    event_name: str,
    *,
    level: Level = "info",
    agent_id: int | None = None,
    source: str = "system",
    target_agent_id: int | None = None,
    attributes: dict[str, Any] | None = None,
    ts: datetime | None = None,
) -> Event:
    """Construct one Event without enqueueing it.

    Transactional producers stage this exact immutable object before their
    database commit, then call :func:`emit_prepared` only after commit.  That
    preserves one canonical byte representation across their manifest receipt
    and the eventually exported telemetry line.
    """
    spec = EVENTS.get(event_name)
    if spec is None:
        raise ValueError(
            f"emit() got unregistered event_name={event_name!r} — declare an "
            "EventSpec in a base/events/declarations module first (base/events/"
            "registry.md is generated from it)"
        )
    if category != spec.category and category not in spec.extra_categories:
        raise ValueError(
            f"emit() category={category!r} contradicts the registry for "
            f"event_name={event_name!r} (declared {spec.category!r})"
        )
    trace_id, span_id = capture_trace_ids()
    return Event(
        ts=_as_utc(ts),
        trace_id=trace_id,
        span_id=span_id,
        agent_id=agent_id if agent_id is not None else _ambient_agent_id(),
        machine=_state["machine"] or _resolve_machine(),
        cluster=_state["cluster"] or cluster_label(),
        process=_state["process"],
        category=category,
        event_name=event_name,
        level=level,
        source=source,
        target_agent_id=target_agent_id,
        attributes=dict(attributes or {}),
    )


def emit_prepared(event: Event, *, capture: Callable[[Event], Event] | None = None) -> None:
    """Capture through an explicit producer dependency, then enqueue that exact event."""
    if capture is not None:
        event = capture(event)
    with failure_isolated("emit"):
        pipeline = _ensure_pipeline()
        if pipeline is not None:
            pipeline.enqueue(event)


def flush() -> DrainResult:
    """Acknowledge queued and held ordinary records within the default deadline."""
    pipeline = _state["pipeline"]
    if pipeline is not None:
        return pipeline.flush()
    return DrainResult(DrainStatus.COMPLETED, DrainPhase.DRAIN)


def sync(timeout: float = 5.0, *, bounded: bool = False) -> DrainResult:
    """Drain and acknowledge the event pipeline; close uses the bounded form."""
    pipeline = _state["pipeline"]
    if pipeline is not None:
        return pipeline.sync(timeout, bounded=bounded)
    return DrainResult(DrainStatus.COMPLETED, DrainPhase.DRAIN)


def stop(timeout: float = 5.0) -> DrainResult:
    """Stop the drain thread (process teardown / tests)."""
    pipeline = _state["pipeline"]
    if pipeline is not None:
        return pipeline.stop(timeout)
    return DrainResult(DrainStatus.COMPLETED, DrainPhase.STOP)


def _drain_on_exit() -> None:
    """Close ordinary admission and finitely join its single writer at normal exit.

    The ordered exit seam closes downstream sinks only after the writer finishes.
    Providers leave shutdown to their worker; an unfinished writer is reported;
    process exit may shed its ordinary tail. ``hard_exit`` bypasses this hook.
    """
    pipeline = _state["pipeline"]
    if pipeline is None:
        return
    result = pipeline.stop()
    if result.status is DrainStatus.UNFINISHED:
        return  # the owned writer still uses these sinks; process exit sheds its tail
    with failure_isolated("close observed-metrics projection"):
        from base.telemetry.metrics.observed_metrics import close_projection

        close_projection()
    with failure_isolated("close telemetry_events store"):
        from base.telemetry.event_store import close_store

        close_store()
    from base.telemetry.otlp import telemetry_otlp

    telemetry_otlp.shutdown()


atexit.register(_drain_on_exit)
