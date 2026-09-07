"""Unified event emitter — the single entry point for every event in every process.

Mental model (event-system design doc §1/§2): Ava has one event stream. An
event is a named log record (OTel LogRecord semantics: events = logs with
names, `event.name`); audit, telemetry and log are all events in that stream,
sharing one schema (`events` table) and one correlation key (`trace_id`). This
module is the only writer of the stream.

Pipeline (Layer 1): a bounded queue + drain thread per process. Every `emit()`
enqueues one event; the drain thread batch-writes (default 100/batch, 0.5 s
interval) in one transaction:

1. append the batch to the local JSONL mirrors under `logs_dir()` — the full
   event stream for 7 days, a filtered ledger-rollup source for 90 days by
   default, and a filtered lineage copy for 365 days (the permanently retained
   class's second, independent failure domain) — then
2. export the batch to the OTLP backend (`shared.telemetry_otlp`) — events ->
   OTLP logs (Loki), telemetry numeric payloads -> OTLP metrics (Prometheus) —
   when `AVA_TELEMETRY_OTLP_ENABLED` is on (default). Fully failure-isolated:
   the OTLP side sheds instead of blocking; see telemetry_otlp's docstring
   for the contract.

The Postgres `events` copy was retired with the LGTM cutover (task #1197,
user ruling 2026-08-12): the PG table is now a read-only archive — nothing
writes it, the read side is Loki/Prometheus, and `events_maintenance`'s
events-archive slices are disabled (the daemon still always runs its checkpoint
reaper + blob vacuum, which are independent of the events pipeline). The full
JSONL mirror remains the local debugging backfill; its filtered rollup-source
mirror is the automated ledger-gap recovery source, and its filtered lineage
mirror is the local copy of the permanently retained lineage class.

Backpressure: the queue is bounded (10 000); a producer that outruns the drain
thread sheds records instead of growing memory, and the shed count is reported
as one structured `event_log_drop` event per flush — the same semantics the
former loguru Postgres sink had. Audit-category events get a durable lane:
they block briefly for a slot (bounded backpressure, `_AUDIT_BLOCK_S`) so an
overloaded queue sheds telemetry/log before audit evidence — a shed record is
lost from every sink, JSONL mirror included, so the audit lane is what keeps
the compliance stream intact under load.

`trace_id` / `span_id` are captured from the active OTel span at *enqueue* time
(the drain thread runs outside the span context), so every event emitted inside
`turn_span()` — llm_usage, turn_end, exec, sdk_call, business events — auto-
carries its turn's trace id with no per-callsite plumbing. Events emitted
outside any span (gateway/daemon paths) get NULL.

`machine` and `cluster` are required dimensions, bound at process start (see
`init_telemetry`); processes that never init fall back to the hostname and
home-derived cluster label so a row is never written without either.

Emit is best-effort and never raises: a broken sink must not crash the caller
(JSONL mirror + loguru file sinks are the durable backfill for everything
that reaches the drain thread; audit events additionally block briefly at
enqueue so they are not shed while the queue is overloaded).
Startup init (`init_telemetry`) is the one place that fails loud — a process
whose event pipeline cannot come up should not start silently blind.

Import discipline: this module imports `shared.log` and `shared.db` lazily
(inside functions) — `shared/db.py` imports `shared/log.py` at module scope
for `logger`, so a top-level import of either from here is a circular-import
failure for any process that reaches `shared.db` first.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import queue
import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import blake2b
from typing import Any, Literal

from shared.events.contract import EVENTS, lineage_event_names
from shared.events.contract import category_for_kind as registry_category
from shared.observability import cluster_label
from shared.paths import logs_dir

__all__ = [
    "Category",
    "Event",
    "category_for_kind",
    "emit",
    "event_id",
    "flush",
    "init_telemetry",
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
# process memory without limit. Past this point non-audit records are shed (see
# `_EventPipeline.enqueue`) and `dropped` says how much. A shed record is gone
# from EVERY sink — the JSONL mirror only ever holds what reached the drain
# thread — so audit-category events get a durable lane instead (below).
_QUEUE_MAXSIZE = 10_000

# How long an audit event's producer blocks on a full queue before the event is
# shed (bounded backpressure). Audit events are the compliance evidence — the
# one class that must not vanish under load, which is exactly when the queue
# fills — so they wait for the drain thread to free a slot instead of dropping
# immediately. 5s is far above the drain thread's flush cadence (100/batch,
# 0.5s interval), so a healthy pipeline frees the slot in well under a second
# and the cap only binds when the drain thread itself is gone.
_AUDIT_BLOCK_S = 5.0

# JSONL mirror retention (day-stamped files, like the trace mirror).
_JSONL_RETENTION_DAYS = 7
_JSONL_ROLLUP_RETENTION_DAYS = 90
# Lineage mirror retention (design 2026-09-02 §3C). The lineage class is
# permanent in Loki (a 100-year per-stream override, see
# `shared/loki_index_labels.LINEAGE_RETENTION_PERIOD`); this mirror exists
# because that is ONE copy in ONE failure domain, and 2026-08-20 is what a
# single copy is worth — a global-retention bucket deleted the pre-cutover
# archive and nothing else held those rows. Its failure domain is this box's
# disk, independent of Loki's config and data volume. At ~412 rows/day
# cluster-wide (<1MB/day) a year of it costs ~100MB, so the retention is long
# rather than tuned; unlike the rollup tier nothing replays it on a schedule,
# so it stays a constant instead of a settings knob until an operator needs it.
_JSONL_LINEAGE_RETENTION_DAYS = 365

# MUST match the event selectors aggregated by
# services/events_maintenance/rollup.py:_tokens_queries/_metrics_queries
# (shared cannot import services without reversing the layer boundary).
# Loki additionally restricts llm_usage/turn_end to telemetry|log; those
# families are emitted only in those categories today, so this name-only filter
# is equivalent. A category change must update both selectors together.
_JSONL_ROLLUP_SOURCE_EVENTS = frozenset({"llm_usage", "turn_end"})


def _is_rollup_source(event_name: str) -> bool:
    """Whether an event feeds the durable token/metrics ledger rollup."""
    return (
        event_name in _JSONL_ROLLUP_SOURCE_EVENTS
        or event_name == "exec"
        or event_name.startswith(("exec_", "exec("))
    )


# Derived from the registry's `retention_class="lineage"` declarations — the
# same source the deployed Loki per-stream selector is validated against
# (`shared/loki_index_labels.validate_loki_deploy_config`), so the two
# permanent copies cannot come to disagree about what lineage is. Snapshotted
# at import: the drain thread tests it once per event.
_JSONL_LINEAGE_SOURCE_EVENTS = lineage_event_names()


def _is_lineage_source(event_name: str) -> bool:
    """Whether an event belongs to the permanently retained lineage class."""
    return event_name in _JSONL_LINEAGE_SOURCE_EVENTS


def event_id(line: str, ts_ns: int) -> int:
    """Return the stable surrogate id shared by mirror and Loki event rows."""
    return int.from_bytes(blake2b(f"{ts_ns}:{line}".encode(), digest_size=8).digest(), "big")


@dataclass(frozen=True)
class Event:
    """One event in the unified stream — OTel LogRecord semantics (events = logs
    with names), the shape the `events` table stores."""

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


class _SyncMarker:
    """Sentinel for _EventPipeline.sync(): the drain thread flushes its
    held batch and signals completion when it dequeues one."""


_SYNC = _SyncMarker()


# Telemetry event names — derived from the event contract registry
# (shared/events/contract.py EVENTS, R2-C): the registry is the single source
# of truth; this set is its category projection (kept as a module-level name
# because tests/lint import it). Adding an event = one registry entry; this
# view updates automatically.
_TELEMETRY_KINDS = frozenset(name for name, spec in EVENTS.items() if spec.category == "telemetry")


def category_for_kind(event_name: str) -> Category:
    """Map an event name to its declared category (registry EVENTS, R2-C);
    a name with no declaration falls back to the bare-log category."""
    return registry_category(event_name)


def _capture_trace_ids() -> tuple[str | None, str | None]:
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
    except Exception:  # noqa: S110
        pass  # fail-fast-ok: trace capture must never break an emit
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
    from shared.log import logger
    from shared.machine import MachineNameMissing, machine_name

    try:
        return machine_name()
    except MachineNameMissing:
        return socket.gethostname()
    except Exception:
        logger.exception("machine_name() failed unexpectedly — falling back to hostname")
        return socket.gethostname()


# ── pipeline state (per-process singleton) ────────────────────────────────────

# The emitter pipeline; None until first init/emit. Dict mutation avoids ruff
# PLW0603 the same way shared/trace.py does.
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
    from shared.config import settings

    now = datetime.now(UTC)
    full_cutoff = (now - timedelta(days=_JSONL_RETENTION_DAYS)).strftime("%Y%m%d")
    for path in logs_dir().glob("events-????????.jsonl"):
        day = path.name.removeprefix("events-").removesuffix(".jsonl")
        if day.isdigit() and day < full_cutoff:
            with contextlib.suppress(OSError):
                path.unlink()
    rollup_retention_days = settings.daemon.events_jsonl_rollup_retention_days
    rollup_cutoff = (now - timedelta(days=rollup_retention_days)).strftime("%Y%m%d")
    for path in logs_dir().glob("events-????????.rollup.jsonl"):
        day = path.name.removeprefix("events-").removesuffix(".rollup.jsonl")
        if day.isdigit() and day < rollup_cutoff:
            with contextlib.suppress(OSError):
                path.unlink()
    lineage_cutoff = (now - timedelta(days=_JSONL_LINEAGE_RETENTION_DAYS)).strftime("%Y%m%d")
    for path in logs_dir().glob("events-????????.lineage.jsonl"):
        day = path.name.removeprefix("events-").removesuffix(".lineage.jsonl")
        if day.isdigit() and day < lineage_cutoff:
            with contextlib.suppress(OSError):
                path.unlink()


def _append_jsonl(events: list[Event]) -> None:
    """Append the batch to today's mirror file, one JSON line per event.

    Each row carries the stable surrogate ``id`` derived from its id-free body
    and timestamp, matching the id Loki's read path returns for the same event.

    Three tiers, one pass over the batch: the full mirror, the filtered
    rollup source, and the filtered lineage copy — all written under the same
    try, so one failure reports once for the batch rather than three times.

    Best-effort — the mirror is a fallback, not a critical path; a write
    failure must never break the batch. But it must not be SILENT either:
    the mirror is the durable fallback for the DB copy, so a sustained
    mirror failure is reported (first + every 50th, same cadence as DB write
    failures) — a disk-full / permission error would otherwise degrade both
    copies without a trace. Single write() per line with O_APPEND semantics
    (opened in append mode) keeps concurrent processes from interleaving."""
    day = datetime.now(UTC).strftime("%Y%m%d")
    if _state["jsonl_day"] != day:
        _state["jsonl_day"] = day
        with contextlib.suppress(Exception):
            _prune_jsonl_mirror()
    try:
        lines: list[str] = []
        rollup_lines: list[str] = []
        lineage_lines: list[str] = []
        for e in events:
            body = {
                "ts": e.ts.isoformat(),
                "trace_id": e.trace_id,
                "span_id": e.span_id,
                "agent_id": e.agent_id,
                "machine": e.machine,
                "cluster": e.cluster,
                "process": e.process,
                "category": e.category,
                "event_name": e.event_name,
                "level": e.level,
                "source": e.source,
                "target_agent_id": e.target_agent_id,
                "attributes": e.attributes,
            }
            body_str = json.dumps(body, default=str, separators=(",", ":"), ensure_ascii=False)
            ts_ns = int(e.ts.timestamp() * 1_000_000_000)
            eid = event_id(body_str, ts_ns)
            line = (
                json.dumps(
                    {**body, "id": eid}, default=str, separators=(",", ":"), ensure_ascii=False
                )
                + "\n"
            )
            lines.append(line)
            if _is_rollup_source(e.event_name):
                rollup_lines.append(line)
            if _is_lineage_source(e.event_name):
                lineage_lines.append(line)
        path = logs_dir() / f"events-{day}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write("".join(lines))
        if rollup_lines:
            rollup_path = logs_dir() / f"events-{day}.rollup.jsonl"
            with rollup_path.open("a", encoding="utf-8") as f:
                f.write("".join(rollup_lines))
        if lineage_lines:
            lineage_path = logs_dir() / f"events-{day}.lineage.jsonl"
            with lineage_path.open("a", encoding="utf-8") as f:
                f.write("".join(lineage_lines))
    except Exception as exc:  # report, never raise
        global _jsonl_failures  # noqa: PLW0603 — module-level counter
        _jsonl_failures += 1
        if _jsonl_failures == 1 or _jsonl_failures % 50 == 0:
            _report_no_pipeline(
                "[event-emitter] JSONL mirror write failed ({n} consecutive) — "
                "the mirror is the durable fallback; with the DB copy also "
                "down the batch is lost entirely: {err}",
                n=_jsonl_failures,
                err=repr(exc),
            )


def _export_otlp(events: list[Event]) -> None:
    """Best-effort dual-write of a batch to the OTLP backend (logs + metrics).

    Runs on the drain thread right after the JSONL mirror write, before the DB
    transaction. The OTLP side is fully failure-isolated (bounded queue, drop
    semantics, SDK-owned export threads — see `shared.telemetry_otlp`), and
    this call is suppressed end to end, so even a programming error there must
    not cost the batch its PG copy or raise into the drain thread."""
    with contextlib.suppress(Exception):
        from shared import telemetry_otlp  # deferred — heavy OTel imports

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
# `shared.log._add_postgres_sink`) so they reach stderr / JSONL file sinks only
# and never re-enter this pipeline — a DB-down process would otherwise loop
# failure → warning → emit → failure forever.
_NO_EMITTER = "_no_emitter"


def _report_no_pipeline(message: str, **extra: Any) -> None:
    """Log a drain-thread diagnostic through loguru, marked `_NO_EMITTER` so
    the emitter adapter skips it. Best-effort: never raises, never blocks."""
    with contextlib.suppress(Exception):
        from shared.log import logger

        # **{_NO_EMITTER: True} — bind() takes literal kwargs, so the marker
        # key must be the constant's VALUE, not its name.
        logger.bind(**{_NO_EMITTER: True}).warning(message, **extra)


def _write_batch(events: list[Event]) -> None:
    """Write one batch: JSONL mirror first (durable), then the OTLP export
    (Loki logs + Prometheus metrics). The Postgres `events` copy was retired
    with the LGTM cutover (task #1197) — nothing writes it anymore.

    Both sinks are best-effort and failure-isolated: the mirror is a local
    append (never raises into the drain thread), the OTLP side sheds instead
    of blocking (see telemetry_otlp's docstring)."""
    if not events:
        return
    _append_jsonl(events)
    _export_otlp(events)


class _EventPipeline:
    """Bounded queue + drain thread owning all event DB writes for the process.

    Same shape as the former loguru Postgres sink (which this replaces): the
    queue bound is the backpressure, the drain thread batches, and shed records
    are counted and reported as one `event_log_drop` event per flush so the ops
    monitor panel keeps its backlog metric. Audit events enqueue through a
    bounded-blocking lane (see `enqueue`) so overload sheds telemetry/log first."""

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
        self._queue: queue.Queue[Event | None | _SyncMarker] = queue.Queue(maxsize=queue_maxsize)
        self.dropped = 0  # records shed because the queue was full since the last flush
        # enqueue() runs on producer threads while _flush() (drain thread)
        # reads and zeroes the counter — `+=` is not atomic under the GIL, so
        # the read-modify-write pair is serialized.
        self._dropped_lock = threading.Lock()
        self._sync_done = threading.Event()  # set by the drain thread after a sync() flush
        self._thread = threading.Thread(target=self._drain, daemon=True, name="event-emitter")
        self._thread.start()

    def enqueue(self, event: Event) -> None:
        """Producer path — non-blocking for regular events, bounded-blocking
        for audit events.

        Regular events shed (counted) when the queue is full. Audit events
        instead block up to `_AUDIT_BLOCK_S` for a slot — bounded backpressure
        — so an overloaded queue sheds telemetry/log before it ever sheds audit
        evidence; only a sustained overflow past the cap drops an audit event
        (counted, and reported by the next flush). The drain thread frees slots
        on its 0.5s cadence, so a healthy pipeline never actually spends the
        cap."""
        if event.category == "audit":
            try:
                self._queue.put(event, timeout=_AUDIT_BLOCK_S)
            except queue.Full:
                with self._dropped_lock:
                    self.dropped += 1
            return
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            with self._dropped_lock:
                self.dropped += 1

    def flush(self) -> None:
        """Synchronously drain whatever is queued (tests, shutdown seams).

        Runs on the calling thread; a concurrent drain-thread flush can race,
        which is harmless (both write whole batches in their own transaction).
        """
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

    def sync(self, timeout: float = 5.0) -> None:
        """Drain the queue AND wait for the drain thread's held batch to land.

        flush() drains the queue on the calling thread, but a batch the
        drain thread already dequeued can still be written up to one
        flush_interval later — a TRUNCATE of the events table in between
        loses that race (the test_events_api straggler flake class,
        testing/ci-flakes-pr1686-20260807.md). sync() closes the window:
        it flushes the queue, pokes the drain thread to write its held
        batch immediately, and blocks until that write completed.
        """
        self.flush()
        if threading.current_thread() is self._thread or not self._thread.is_alive():
            return  # nothing to wait for (stop() already ran)
        self._sync_done.clear()
        self._queue.put(_SYNC)
        if not self._sync_done.wait(timeout):
            _report_no_pipeline(
                "[event-emitter] sync() timed out after {t}s — the drain "
                "thread's held batch may not have landed; a subsequent "
                "TRUNCATE of `events` could lose it",
                t=timeout,
            )

    def stop(self) -> None:
        """Signal the drain thread to exit and join. Daemon thread won't block
        process exit even if join times out."""
        self._queue.put(None)  # sentinel
        self._thread.join(timeout=5.0)

    def _flush(self, batch: list[Event]) -> None:
        """Best-effort write of one batch, plus the shed-record report.

        Runs on the drain thread at least every `_flush_interval_s`. The drop
        report goes through loguru like any other line — if the queue is STILL
        full it is shed too and `dropped` bumps again, bounding the report to
        one per interval with no recursion (enqueue() never logs)."""
        with self._dropped_lock:
            n = self.dropped
            self.dropped = 0
        if n:
            with contextlib.suppress(Exception):
                from shared.log import logger

                logger.warning(
                    "[event-emitter] dropped {n} event(s) (queue full) — shed "
                    "before any sink; audit events block first and are only "
                    "dropped past their own cap",
                    event="event_log_drop",
                    n=n,
                )
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
    """Build the process pipeline: queue + drain thread. The Postgres events
    copy is retired (task #1197), so startup no longer depends on the DB —
    the pipeline writes the JSONL mirror and the OTLP backend only."""
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
    process shares): `init_agent_process` → process="agent-kernel",
    `init_gateway_process(name)` → process=name, etc. The first call opens the
    drain thread; later calls only refresh the identity binding. The DB is no
    longer part of the pipeline (task #1197), so startup never depends on it."""
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
    (`shared/turn_identity.py`) is the only truthful answer. An exec child or
    standalone script may instead carry the process-level `init_telemetry` value."""
    from shared.turn_identity import current_turn_agent_id

    bound = current_turn_agent_id()
    if bound is not None:
        return bound
    return _state["agent_id"]


def _ensure_pipeline() -> _EventPipeline | None:
    """Lazy-init fallback for emit-before-init callers. Best-effort: a process
    with no DB available degrades to dropping (never raises, never blocks)."""
    if _state["pipeline"] is None:
        with contextlib.suppress(Exception):
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
    `shared/events/contract.py`, or a category that contradicts the
    declaration, raises `ValueError` (AGENTS.md "explode on unknown enums").
    The loguru adapter wraps its call with `catch=True`, so a logging line
    that drifts off-contract stays visible (JSONL mirror) without crashing
    the producer.

    An event is a named log record (OTel LogRecord semantics: events = logs
    with names); `event_name` is that name — the `event.name` column of the
    `events` table, and the registry key (`EVENTS`).

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
    spec = EVENTS.get(event_name)
    if spec is None:
        raise ValueError(
            f"emit() got unregistered event_name={event_name!r} — declare an "
            "EventSpec in shared/events/contract.py EVENTS first (shared/events/"
            "registry.md is generated from it)"
        )
    if category != spec.category and category not in spec.extra_categories:
        raise ValueError(
            f"emit() category={category!r} contradicts the registry for "
            f"event_name={event_name!r} (declared {spec.category!r})"
        )
    with contextlib.suppress(Exception):
        pipeline = _ensure_pipeline()
        if pipeline is None:
            return
        trace_id, span_id = _capture_trace_ids()
        pipeline.enqueue(
            Event(
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
        )


def flush() -> None:
    """Drain the queue synchronously — tests assert rows right after emit, and
    shutdown seams want the last records landed before exit."""
    pipeline = _state["pipeline"]
    if pipeline is not None:
        pipeline.flush()


def sync() -> None:
    """Drain the queue AND wait for the drain thread's held batch to land —
    the barrier to call before TRUNCATE-ing `events` when the test asserts
    its exact contents afterwards (see _EventPipeline.sync)."""
    pipeline = _state["pipeline"]
    if pipeline is not None:
        pipeline.sync()


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
    source."""
    pipeline = _state["pipeline"]
    if pipeline is None:
        return
    pipeline.flush()
    pipeline.stop()
    with contextlib.suppress(Exception):
        from shared import telemetry_otlp  # deferred — heavy OTel imports

        telemetry_otlp.shutdown()


atexit.register(_drain_on_exit)
