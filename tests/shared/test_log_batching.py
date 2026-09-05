"""The event emitter batches, and sheds instead of growing without bound.

Hermetic — no Postgres. The point is the *shape* of the write path, which is
what went missing before: `314708af` added batching to the loguru Postgres
sink, `e92f6e82` removed it three hours later while replaying a stale branch,
and the whole suite stayed green for three weeks because
`tests/gateway/test_log_sink.py` registered `_postgres_sink` directly with
`enqueue=False` and never touched `_ThreadedPostgresSink` at all.

The batching now lives in the unified emitter (`shared.telemetry`), which the
loguru sink feeds; these tests assert the property the batching exists for —
how many writes N records cost — rather than the presence of a symbol, which a
rename would break and a deletion would not.
"""

from __future__ import annotations

import itertools
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from shared import log as slog
from shared import telemetry


def _event(i: int, category: str = "log") -> telemetry.Event:
    """A minimal valid event; only identity matters to the batching tests."""
    return telemetry.Event(
        ts=datetime(2026, 7, 22, 12, 0, tzinfo=UTC),
        trace_id=None,
        span_id=None,
        agent_id=None,
        machine="test",
        cluster=".ava-test",
        process="test",
        category=category,  # type: ignore[arg-type]
        event_name=f"kind-{i}",
        level="info",
        source="system",
        target_agent_id=None,
        attributes={},
    )


@pytest.fixture
def tuned(monkeypatch: pytest.MonkeyPatch):
    """Shrink the batching constants so tests stay fast."""

    def _apply(batch: int = 10, interval: float = 0.05, maxsize: int = 100) -> None:
        monkeypatch.setattr(telemetry, "_BATCH_SIZE", batch)
        monkeypatch.setattr(telemetry, "_FLUSH_INTERVAL_S", interval)
        monkeypatch.setattr(telemetry, "_QUEUE_MAXSIZE", maxsize)

    return _apply


def test_sync_flushes_batch_held_by_drain_thread() -> None:
    """sync() must land a batch the drain thread already dequeued.

    flush() drains the queue on the calling thread only — a batch the
    drain thread fetched earlier is written up to one flush_interval
    later, which is exactly the window that polluted exact-content
    events assertions after a TRUNCATE (straggler flake class). sync()
    closes it: the held batch must reach the writer before sync returns.
    """

    rec = _Recorder()
    sink = _make_sink(rec, batch=100, interval=60.0, maxsize=100)
    try:
        sink.enqueue(_event(0))
        # Wait until the drain thread has actually dequeued the record (it
        # holds the batch until its flush_interval — 60s here — so the held
        # batch is stable). A fixed 0.2s sleep could race the drain thread
        # under load and blow the assertion on the wrong side (audit round-2
        # cc-docs-tests P2 — the file's own _drain_sink deadline-polling
        # primitive is the house pattern).
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not sink._queue.empty():
            time.sleep(0.01)
        assert sink._queue.empty(), "drain thread never dequeued the record"
        sink.flush()
        assert rec.batches == [], "flush() must not write the held batch"
        sink.sync(timeout=2.0)
        assert [e.event_name for b in rec.batches for e in b] == ["kind-0"]
        sink.sync(timeout=2.0)  # idempotent: nothing new, no extra write
        assert [e.event_name for b in rec.batches for e in b] == ["kind-0"]
    finally:
        sink.stop()


def _drain_sink(sink: Any, expected: int, timeout: float = 5.0) -> None:
    """Wait until `expected` records have reached the writer."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sum(len(b) for b in sink._writer.batches) >= expected:
            return
        time.sleep(0.01)


class _Recorder:
    def __init__(self) -> None:
        self.batches: list[list[Any]] = []

    def __call__(self, batch: list[Any]) -> None:
        self.batches.append(list(batch))


def _make_sink(
    writer: Callable[[list[Any]], None], *, batch: int, interval: float, maxsize: int
) -> telemetry._EventPipeline:
    return telemetry._EventPipeline(
        writer=writer,
        batch_size=batch,
        flush_interval_s=interval,
        queue_maxsize=maxsize,
    )


# --- the regression: write amplification ---


def test_many_records_cost_few_writes(tuned: Any) -> None:
    """100 records must not become 100 round-trips.

    This is the assertion whose absence let the batching be deleted unnoticed.
    """
    tuned(batch=10, interval=0.05)
    rec = _Recorder()
    sink = _make_sink(rec, batch=10, interval=0.05, maxsize=100)
    try:
        for i in range(100):
            sink.enqueue(_event(i))
        _drain_sink(sink, 100)
    finally:
        sink.stop()

    delivered = [m for b in rec.batches for m in b]
    assert len(delivered) == 100, "records were lost"
    assert [m.event_name for m in delivered] == [f"kind-{i}" for i in range(100)], (
        "order was not preserved"
    )
    assert len(rec.batches) <= 20, f"expected batched writes, got {len(rec.batches)}"


def test_single_record_still_lands_within_the_interval(tuned: Any) -> None:
    """Batching must not hold a lone record hostage waiting for a full batch."""
    tuned(batch=10, interval=0.05)
    rec = _Recorder()
    sink = _make_sink(rec, batch=10, interval=0.05, maxsize=100)
    try:
        sink.enqueue(_event(1))
        _drain_sink(sink, 1, timeout=2.0)
    finally:
        sink.stop()
    assert [m.event_name for b in rec.batches for m in b] == ["kind-1"]


def test_stop_flushes_what_is_buffered(tuned: Any) -> None:
    """A partial batch pending at teardown must not be dropped."""
    tuned(batch=1000, interval=60.0)  # neither trigger can fire on its own
    rec = _Recorder()
    sink = _make_sink(rec, batch=1000, interval=60.0, maxsize=100)
    for i in range(5):
        sink.enqueue(_event(i))
    time.sleep(0.05)  # let the drain thread pick them up into its batch
    sink.stop()
    assert [m.event_name for b in rec.batches for m in b] == [f"kind-{i}" for i in range(5)]


# --- bounded queue ---


def test_queue_is_bounded_and_counts_what_it_sheds(
    tuned: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A producer outrunning the drain thread sheds records, not memory.

    The drain thread is stalled so the queue genuinely fills; before this bound
    existed the same burst grew the agent process's heap without limit.
    """
    tuned(batch=10, interval=0.05, maxsize=20)
    blocked = _Recorder()

    def _stalled(batch: list[Any]) -> None:
        time.sleep(5.0)
        blocked(batch)

    reports: list[dict] = []
    monkeypatch.setattr(
        slog.logger,
        "warning",
        lambda _msg, **kw: reports.append(kw) if kw.get("event") == "event_log_drop" else None,  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )
    # Not stopped in a finally: stop() joins, and this writer sleeps 5s by
    # design. The drain thread is already a daemon, so it cannot hold the
    # interpreter open.
    sink = _make_sink(_stalled, batch=10, interval=0.05, maxsize=20)
    for i in range(500):
        sink.enqueue(_event(i))
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and not reports:
        time.sleep(0.01)

    assert reports, "expected an event_log_drop report after shedding records"
    assert all(r["event"] == "event_log_drop" for r in reports)  # pyright: ignore[reportUnknownArgumentType]
    assert reports[0]["n"] > 0
    assert sink._queue.qsize() <= 20, "queue grew past its bound"


def test_nothing_is_dropped_when_the_drain_keeps_up(tuned: Any) -> None:
    tuned(batch=10, interval=0.05, maxsize=1000)
    rec = _Recorder()
    sink = _make_sink(rec, batch=10, interval=0.05, maxsize=1000)
    try:
        for i in range(50):
            sink.enqueue(_event(i))
        _drain_sink(sink, 50)
    finally:
        sink.stop()
    assert sink.dropped == 0


# --- the batch writer itself ---


# --- production wiring ---


def test_production_loguru_sink_routes_through_the_emitter() -> None:
    """`_postgres_sink` must enqueue into the emitter (which batches), not write
    straight to the DB per record — the regression that deleted batching before.
    """

    import inspect

    src = inspect.getsource(slog._postgres_sink)
    assert "telemetry.emit" in src
    wiring = inspect.getsource(slog._add_postgres_sink)
    assert "telemetry.init_telemetry" in wiring
    assert "_postgres_sink" in wiring  # the adapter is the loguru handler


def _rec(extra: dict[str, Any]) -> Any:
    """A minimal loguru-Record-shaped object — the filter reads only `extra`."""
    return {"extra": extra}


def test_add_postgres_sink_registers_at_most_once() -> None:
    """Two `_add_postgres_sink` calls (exec_child's env + request init paths)
    must not double-register the adapter: one loguru record lands exactly one
    mirror row. The 2026-08-24 double registration wrote every post-init
    record twice — byte-identical rows, same surrogate id — into the JSONL
    mirror (task #1638)."""
    from datetime import UTC as _UTC

    from shared.log import logger as _g

    first = slog._add_postgres_sink(process="test-dup-guard")
    second = slog._add_postgres_sink(process="test-dup-guard")
    assert second == first, "repeat registration must return the live sink id"

    marker = f"dup-guard-{time.time_ns()}"
    try:
        _g.configure(extra={"agent_id": "8901"})
        _g.info(f"one record {marker}", event="label_change")
        telemetry.flush()

        from shared.paths import logs_dir

        day = datetime.now(_UTC).strftime("%Y%m%d")
        path = logs_dir() / f"events-{day}.jsonl"
        rows: list[dict[str, Any]] = []
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                msg = obj.get("attributes", {}).get("msg", "")
                if obj.get("event_name") == "label_change" and marker in msg:
                    rows.append(obj)
        assert len(rows) == 1, f"expected 1 mirror row, got {len(rows)}"
    finally:
        _g.remove(first)


def test_event_pipeline_filter_drops_no_emitter_and_node_enter() -> None:
    """The pipeline filter admits ordinary records but drops two families:
    the emitter's own failure reports (`_no_emitter` marker — a DB-down
    process must not loop failure → warning → emit) and `node_enter` (zero
    events-table consumers; the row would be pure write amplification — the
    log-file line is the death-analysis source). Anything else — including
    `node_exit`, which agent_inspect reads — passes."""

    f = slog._event_pipeline_filter
    assert f(_rec({"event": "node_exit"}))
    assert f(_rec({"event": "llm_usage"}))
    assert f(_rec({}))
    assert not f(_rec({"event": "node_enter"}))
    assert not f(_rec({"_no_emitter": True}))
    # The node_enter drop is by event name only, not by message — a record
    # that merely *mentions* node_enter in its message still passes.
    assert f(_rec({"msg": "node_enter handling"}))
    # The OTel SDK's post-end warning (a deliberate consequence of ending the
    # claim node span early in claim_idle_wait_span) is dropped from the
    # table; a record merely mentioning the text still passes.
    assert not f(cast(Any, dict(_rec({}), message="Setting attribute on ended span.")))
    assert f(cast(Any, dict(_rec({}), message="Setting attribute on ended span. (context)")))


def _rec_level(extra: dict[str, Any], level: str = "INFO") -> Any:
    """A loguru-Record-shaped dict with a level — the sampling branch reads it."""
    return {"extra": extra, "level": SimpleNamespace(name=level)}


def test_event_pipeline_filter_samples_info_log_one_in_ten() -> None:
    """Bare INFO `log` records are level-graded-sampled: exactly one in ten
    passes; the counter is per-process and resettable for tests."""
    f = slog._event_pipeline_filter
    slog._log_info_sample_counter = itertools.count()
    kept = [f(_rec_level({"event": "log"})) for _ in range(10)]
    assert kept.count(True) == 1
    assert kept[0] is True  # the first record of each block is the survivor


def test_event_pipeline_filter_keeps_warning_and_named_events() -> None:
    """WARNING+ bare logs and every named event pass unchanged — the alerts /
    unresolved surfaces depend on them; only bare INFO logs are sampled."""
    f = slog._event_pipeline_filter
    slog._log_info_sample_counter = itertools.count()
    assert f(_rec_level({"event": "log"}, "WARNING"))
    assert f(_rec_level({"event": "log"}, "ERROR"))
    assert f(_rec_level({"event": "log"}, "CRITICAL"))
    # named events (node_exit, llm_usage) and label-alias events are not bare logs
    assert f(_rec_level({"event": "node_exit"}))
    assert f(_rec_level({"event": "llm_usage"}))
    assert f(_rec_level({"label": "exec"}))
    # a record without a level passes unsampled (test-records / tolerant path)
    assert f(_rec({}))


# --- the drop report (ops monitor collection point) ---


def test_shed_records_report_one_event_log_drop(
    tuned: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Queue-full shedding surfaces as a single structured `event_log_drop`
    report per flush — the ops monitor panel's event-log backlog metric.

    The report is emitted by the drain thread's `_flush` (at most once per
    flush interval, when drops occurred), not per dropped record; a burst of
    sheds collapses into one row with the cumulative n.
    """
    tuned(batch=10, interval=0.05, maxsize=100)
    recorder = _Recorder()
    sink = _make_sink(recorder, batch=10, interval=0.05, maxsize=100)
    reports: list[dict] = []
    monkeypatch.setattr(
        slog.logger,
        "warning",
        lambda _msg, **kw: reports.append(kw) if kw.get("event") == "event_log_drop" else None,  # pyright: ignore[reportUnknownArgumentType, reportUnknownMemberType]
    )
    # Overfill the queue: producer sheds, drain keeps flushing.
    for i in range(500):
        sink.enqueue(_event(i))
    # Wait for at least one drop report (flush cadence is 0.05s in `tuned`).
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not reports:
        time.sleep(0.01)
    sink.stop()

    assert reports, "expected at least one event_log_drop report"
    assert all(r["event"] == "event_log_drop" for r in reports)  # pyright: ignore[reportUnknownArgumentType]
    assert reports[0]["n"] > 0
    # Every shed record is accounted for across the reports (a later report
    # may carry sheds that happened after the first).
    assert sum(r["n"] for r in reports) <= 500  # pyright: ignore[reportUnknownArgumentType]


# ── emitter self-diagnostics (audit-round2 events-obs P2) ────────────────────


def test_jsonl_mirror_failure_is_reported_not_silent(monkeypatch: pytest.MonkeyPatch) -> None:
    """A JSONL mirror write failure must be REPORTED (first + every 50th), not
    swallowed: the mirror is the durable fallback for the DB copy, and a
    disk-full / permission error would otherwise degrade both copies without
    a trace. The report carries the `_no_emitter` marker so it never re-enters
    the pipeline (a mirror-down process would otherwise loop failure -> warn
    -> emit -> failure)."""
    from loguru import logger

    captured: list[str] = []

    def _boom() -> None:
        raise OSError("disk full")

    # The module-level counter may be non-zero from a prior test in the
    # same process (xdist workers share the module) — reset it so the
    # first-failure report cadence assertion is deterministic.
    monkeypatch.setattr(telemetry, "_jsonl_failures", 0)
    monkeypatch.setattr(telemetry, "logs_dir", _boom)
    sink_id = logger.add(
        lambda m: captured.append(m.record["message"]),
        level="WARNING",
        filter=lambda r: bool(r["extra"].get("_no_emitter")),
    )
    try:
        telemetry._append_jsonl([_event(0)])
        telemetry._append_jsonl([_event(1)])  # second consecutive failure — same report window
    finally:
        logger.remove(sink_id)

    assert len(captured) == 1, "first failure reported once, not per batch"
    assert "JSONL mirror write failed" in captured[0]


def test_sync_reports_timeout_when_drain_thread_wedged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """sync() must not fail silently when the drain thread cannot land its held
    batch in time — a caller about to TRUNCATE `events` needs to know the
    barrier did not complete (the straggler-flake class this closes)."""
    import threading

    from loguru import logger

    captured: list[str] = []
    gate = threading.Event()

    def _stuck(_batch: list[Any]) -> None:
        gate.wait(timeout=10.0)  # drain thread wedged until the test releases it

    sink_id = logger.add(
        lambda m: captured.append(m.record["message"]),
        level="WARNING",
        filter=lambda r: bool(r["extra"].get("_no_emitter")),
    )
    sink = _make_sink(_stuck, batch=100, interval=60.0, maxsize=100)
    try:
        sink.enqueue(_event(0))
        time.sleep(0.2)  # drain thread dequeues and holds the batch
        sink.sync(timeout=0.2)  # writer never returns -> barrier times out
        assert any("sync() timed out" in m for m in captured)
    finally:
        gate.set()  # release the drain thread so stop() can join
        sink.stop()
        logger.remove(sink_id)
