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
import queue
import socket
import sys
import threading
import time
from collections.abc import Callable, Generator
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from hashlib import blake2b
from typing import Any, Literal

from base.events.contract import EVENTS
from base.events.contract import category_for_kind as registry_category
from base.paths import logs_dir
from base.telemetry.emitter_sync import synchronize
from base.telemetry.observability import cluster_label
from base.telemetry.serialization import event_line, event_line_digest, event_payload

__all__ = [
    "Category",
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


class _SyncMarker:
    """Sentinel for _EventPipeline.sync(): the drain thread flushes its
    held batch and signals completion when it dequeues one."""


_SYNC = _SyncMarker()


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
        self._queue: queue.Queue[Event | _SyncMarker | None] = queue.Queue(maxsize=queue_maxsize)
        self._drop_reported_at = 0.0
        self._drop_example: Event | None = None
        self.dropped = 0  # records shed because the queue was full since the last flush
        # enqueue() runs on producer threads while _flush() (drain thread)
        # reads and zeroes the counter — `+=` is not atomic under the GIL, so
        # the read-modify-write pair is serialized.
        self._dropped_lock = threading.Lock()
        self._sync_done = threading.Event()  # set by the drain thread after a sync() flush
        self._thread = threading.Thread(target=self._drain, daemon=True, name="event-emitter")
        self._thread.start()

    def enqueue(self, event: Event) -> None:
        """Producer path — never blocks: the event is shed (counted) when the queue is full."""
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            self._record_drop(event)

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

    def flush(self) -> None:
        """Synchronously drain queued records for tests and shutdown seams."""
        events: list[Event] = []
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(event, _SyncMarker):
                continue  # sync() barrier — the drain thread consumes it
            if event is None:
                break
            events.append(event)
        self._flush(events)

    def sync(self, timeout: float = 5.0, *, bounded: bool = False) -> None:
        """Drain the queue AND wait for the drain thread's held batch to land.

        flush() drains the queue on the calling thread, but a batch the
        drain thread already dequeued can still be written up to one
        flush_interval later — a TRUNCATE or a mirror read in between
        loses that race (the test_events_api straggler flake class,
        testing/ci-flakes-pr1686-20260807.md). sync() closes the window:
        it flushes the queue, pokes the drain thread to write its held
        batch immediately, and blocks until that write completed.
        """
        outcome = synchronize(
            flush=self.flush,
            event_queue=self._queue,
            marker=_SYNC,
            drain_thread=self._thread,
            marker_done=self._sync_done,
            timeout=timeout,
            bounded=bounded,
        )
        if outcome is not None:
            report_no_pipeline(
                f"[event-emitter] sync() timed out after {{t}}s during {outcome} — the mirror may land later",
                t=timeout,
            )

    def stop(self) -> None:
        """Signal the drain thread to exit and join. Daemon thread won't block
        process exit even if join times out."""
        self._queue.put(None)  # sentinel
        self._thread.join(timeout=5.0)

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
        """Accumulate up to `_batch_size` events, or whatever arrived within
        `_flush_interval_s`, and write them in one round-trip.

        The deadline is what keeps a quiet process from holding a partial batch
        indefinitely — a single record still lands within the interval."""
        batch: list[Event] = []
        deadline = time.monotonic() + self._flush_interval_s
        while True:
            timeout = max(0.0, deadline - time.monotonic())
            try:
                event = self._queue.get(timeout=timeout)
            except queue.Empty:
                self._flush(batch)
                batch = []
                deadline = time.monotonic() + self._flush_interval_s
                continue
            if isinstance(event, _SyncMarker):  # sync() barrier: write held batch, signal
                self._flush(batch)
                batch = []
                self._sync_done.set()
                deadline = time.monotonic() + self._flush_interval_s
                continue
            if event is None:  # sentinel from stop()
                self._flush(batch)
                return
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
    """The agent an event belongs to when the caller named none.

    Turn first, then the process binding: a hosted runner emits on behalf of
    every local agent, so its process binding is None and the turn contextvar
    (`base/native_process/turn_identity.py`) is the only truthful answer. An exec child or
    standalone script may instead carry the process-level `init_telemetry` value."""
    from base.native_process.turn_identity import current_turn_agent_id

    bound = current_turn_agent_id()
    if bound is not None:
        return bound
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
) -> None:
    """Enqueue one event into the unified stream. Never raises — except for a
    contract violation (R2-C): an `event_name` with no `EventSpec` in
    `base/events/declarations`, or a category that contradicts the
    declaration, raises `ValueError` (AGENTS.md "explode on unknown enums").
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
    emit_prepared(event)


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


def emit_prepared(event: Event) -> None:
    """Enqueue an already constructed event without changing its identity."""
    with failure_isolated("emit"):
        # The external-controller recorder is deliberately at the producer
        # seam, before this bounded queue can shed the event.  Its import stays
        # lazy to preserve telemetry's standalone startup path.
        from base.agents.impersonation_manifest import capture_local_event

        event = capture_local_event(event)
        pipeline = _ensure_pipeline()
        if pipeline is not None:
            pipeline.enqueue(event)


def flush() -> None:
    """Drain the queue synchronously — tests assert rows right after emit, and
    shutdown seams want the last records landed before exit."""
    pipeline = _state["pipeline"]
    if pipeline is not None:
        pipeline.flush()


def sync(timeout: float = 5.0, *, bounded: bool = False) -> None:
    """Drain and acknowledge the event pipeline; close uses the bounded form."""
    pipeline = _state["pipeline"]
    if pipeline is not None:
        pipeline.sync(timeout, bounded=bounded)


def stop() -> None:
    """Stop the drain thread (process teardown / tests)."""
    pipeline = _state["pipeline"]
    if pipeline is not None:
        pipeline.stop()


def _drain_on_exit() -> None:
    """Flush queued events + stop the drain thread at process exit.

    The drain thread is a daemon — without an exit hook, a process that exits
    with events still queued (or mid-batch) silently loses them, which is
    exactly the failure mode the `process_exit` event exists to report. Runs
    via atexit on normal exits (main returns, SystemExit — including the agent
    kernel's signal→SystemExit conversion in `agent/lifecycle.py` and the
    exec-subprocess path, where `init_subprocess_logger` never opened the
    pipeline and `_ensure_pipeline` built it lazily on first emit). SIGKILL /
    SIGSTOP cannot be intercepted; there the JSONL mirror remains the recovery
    source.
    The ONE ordered exit seam for OTLP: providers are built with `shutdown_on_exit=False`;
    no provider atexit shutdown can strand a tail record (task #4320)."""
    pipeline = _state["pipeline"]
    if pipeline is None:
        return
    pipeline.flush()
    pipeline.stop()
    with failure_isolated("close observed-metrics projection"):
        from base.telemetry.metrics.observed_metrics import close_projection

        close_projection()
    with failure_isolated("close telemetry_events store"):
        from base.telemetry.event_store import close_store

        close_store()
    with failure_isolated("otlp shutdown"):
        from base.telemetry.otlp import telemetry_otlp  # deferred — heavy OTel imports

        telemetry_otlp.shutdown()


atexit.register(_drain_on_exit)
