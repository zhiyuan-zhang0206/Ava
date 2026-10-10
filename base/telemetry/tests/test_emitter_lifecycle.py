"""Finite close, admission and per-operation receipts of the ordinary event writer."""

from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import pytest

from base import telemetry
from base.host import proc
from base.telemetry import emitter


def event(i: int) -> telemetry.Event:
    return telemetry.Event(
        ts=datetime.now(UTC),
        trace_id=None,
        span_id=None,
        agent_id=None,
        machine="test",
        cluster="test",
        process="test",
        category="log",
        event_name="log",
        level="info",
        source="test",
        target_agent_id=None,
        attributes={"i": i},
    )


def test_full_queue_stop_is_finite_and_closes_admission() -> None:
    entered, release = threading.Event(), threading.Event()
    written: list[telemetry.Event] = []

    def writer(batch: list[telemetry.Event]) -> None:
        entered.set()
        assert release.wait(3)
        written.extend(batch)

    pipe = telemetry._EventPipeline(writer=writer, batch_size=1, queue_maxsize=1)
    try:
        pipe.enqueue(event(1))
        assert entered.wait(1)
        pipe.enqueue(event(2))
        assert pipe._queue.full()
        started = time.monotonic()
        assert pipe.stop(timeout=0.03).status is telemetry.DrainStatus.UNFINISHED
        assert time.monotonic() - started < 0.5
        pipe.enqueue(event(3))
        assert pipe._queue.qsize() == 1
        assert pipe.dropped == 1
    finally:
        release.set()
        assert pipe.stop(timeout=1).status is telemetry.DrainStatus.COMPLETED
    assert [e.attributes.get("i") for e in written if e.event_name == "log"] == [1, 2]
    assert not pipe._thread.is_alive()
    assert pipe.stop(timeout=0).status is telemetry.DrainStatus.COMPLETED


def test_sync_deadline_covers_marker_admission_and_write(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release = threading.Event(), threading.Event()

    def writer(batch: list[telemetry.Event]) -> None:
        entered.set()
        assert release.wait(3)

    pipe = telemetry._EventPipeline(writer=writer, batch_size=1, queue_maxsize=1)
    try:
        pipe.enqueue(event(1))
        assert entered.wait(1)
        pipe.enqueue(event(2))
        started = time.monotonic()
        assert pipe.sync(timeout=0.03, bounded=True) == telemetry.DrainResult(
            telemetry.DrainStatus.UNFINISHED, telemetry.DrainPhase.MARKER
        )
        assert time.monotonic() - started < 0.5

        # No caller-side writer or rescue worker may be started by sync.
        def forbid_thread(*args: Any, **kwargs: Any) -> None:
            raise AssertionError("sync started another writer")

        monkeypatch.setattr(threading, "Thread", forbid_thread)
        pipe._queue.get_nowait()  # give the barrier a slot while the sole writer stays blocked
        assert pipe.sync(timeout=0.03, bounded=True) == telemetry.DrainResult(
            telemetry.DrainStatus.UNFINISHED, telemetry.DrainPhase.DRAIN
        )
    finally:
        release.set()
        pipe.stop(timeout=1)


def test_concurrent_sync_receipts_wait_for_their_own_fifo_batch() -> None:
    entered, release = threading.Event(), threading.Event()
    written: list[int] = []

    def writer(batch: list[telemetry.Event]) -> None:
        written.extend(e.attributes["i"] for e in batch)
        if 2 in written:
            entered.set()
            assert release.wait(3)

    pipe = telemetry._EventPipeline(writer=writer, batch_size=100, flush_interval_s=60)
    try:
        pipe.enqueue(event(1))
        with ThreadPoolExecutor(max_workers=2) as callers:
            first = callers.submit(pipe.sync, 1)
            assert first.result(timeout=2).status is telemetry.DrainStatus.COMPLETED
            pipe.enqueue(event(2))
            second = callers.submit(pipe.sync, 1)
            assert entered.wait(1)
            third = callers.submit(pipe.sync, 1)
            assert not second.done()
            assert not third.done()
            release.set()
            assert second.result(timeout=2).status is telemetry.DrainStatus.COMPLETED
            assert third.result(timeout=2).status is telemetry.DrainStatus.COMPLETED
    finally:
        release.set()
        pipe.stop(timeout=1)
    assert written == [1, 2]


@pytest.mark.parametrize("failure", [RuntimeError("writer defect"), SystemExit("writer exit")])
def test_worker_failure_reports_immediately_and_never_acknowledges(
    failure: BaseException,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports: list[BaseException] = []

    def report(message: str, **extra: Any) -> None:
        if "exc" in extra:
            reports.append(extra["exc"])

    def writer(batch: list[telemetry.Event]) -> None:
        raise failure

    monkeypatch.setattr(emitter, "report_no_pipeline", report)
    pipe = telemetry._EventPipeline(writer=writer, batch_size=100, flush_interval_s=60)
    pipe.enqueue(event(1))
    with pytest.raises(type(failure)) as observed:
        pipe.sync(timeout=1)
    assert observed.value is failure
    assert pipe._finished.wait(1)
    assert reports == [failure]
    # Late producer observations still shed; stop owns the retained failure.
    pipe.enqueue(event(2))
    assert pipe.dropped == 1
    with pytest.raises(type(failure)) as stopped:
        pipe.stop(timeout=1)
    assert stopped.value is failure
    assert not pipe._thread.is_alive()


def test_explicit_sink_isolation_preserves_successful_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports: list[str] = []

    def report(sink: str, _exc: BaseException) -> None:
        reports.append(sink)

    monkeypatch.setattr(emitter, "report_sink_failure", report)

    def writer(batch: list[telemetry.Event]) -> None:
        with emitter.failure_isolated("documented exporter"):
            raise RuntimeError("optional sink failure")

    pipe = telemetry._EventPipeline(writer=writer, flush_interval_s=60)
    try:
        pipe.enqueue(event(1))
        assert pipe.sync(timeout=1).status is telemetry.DrainStatus.COMPLETED
        assert reports == ["documented exporter"]
    finally:
        assert pipe.stop(timeout=1).status is telemetry.DrainStatus.COMPLETED


def test_unreleasable_writer_cannot_make_stop_unbounded() -> None:
    code = """
import threading, time
from base.telemetry import _EventPipeline, Event
from base import telemetry
from base.log import logger
import sys
logger.add(sys.stderr)
from datetime import datetime, UTC
entered = threading.Event()
def blocked(batch):
    entered.set()
    threading.Event().wait()
p = _EventPipeline(writer=blocked, batch_size=1, queue_maxsize=1)
e = Event(datetime.now(UTC), None, None, None, "test", "test", "test", "log", "log", "info", "test", None)
p.enqueue(e)
assert entered.wait(1)
p.enqueue(e)
t = time.monotonic()
assert p.stop(timeout=0.03).status is telemetry.DrainStatus.UNFINISHED
assert time.monotonic() - t < 0.5
"""
    child = proc.run_bounded(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert child.returncode == 0, child.stderr
    assert "unfinished" in child.stderr


def test_one_deadline_spans_queue_admission_and_blocked_writer() -> None:
    first_entered, first_release = threading.Event(), threading.Event()
    second_release = threading.Event()

    def writer(batch: list[telemetry.Event]) -> None:
        if batch[0].attributes["i"] == 1:
            first_entered.set()
            assert first_release.wait(3)
        else:
            assert second_release.wait(3)

    pipe = telemetry._EventPipeline(writer=writer, batch_size=1, queue_maxsize=1)
    timer = threading.Timer(0.15, first_release.set)
    try:
        pipe.enqueue(event(1))
        assert first_entered.wait(1)
        pipe.enqueue(event(2))
        timer.start()
        started = time.monotonic()
        assert pipe.sync(timeout=0.2, bounded=True) == telemetry.DrainResult(
            telemetry.DrainStatus.UNFINISHED, telemetry.DrainPhase.DRAIN
        )
        assert time.monotonic() - started < 0.28
    finally:
        timer.cancel()
        first_release.set()
        second_release.set()
        pipe.stop(timeout=1)


@pytest.mark.parametrize("timeout", [-1.0, float("inf"), float("nan")])
def test_invalid_timeout_cannot_create_an_unbounded_close(timeout: float) -> None:
    pipe = telemetry._EventPipeline(writer=lambda _batch: None)
    try:
        with pytest.raises(ValueError, match="finite and non-negative"):
            pipe.sync(timeout=timeout)
        with pytest.raises(ValueError, match="finite and non-negative"):
            pipe.stop(timeout=timeout)
    finally:
        pipe.stop(timeout=1)


def test_unknown_worker_failure_is_visible_without_a_logging_sink() -> None:
    code = """
from base.telemetry import _EventPipeline, Event
from base import telemetry
from base.log import logger
from datetime import datetime, UTC
logger.remove()
def broken(batch):
    raise RuntimeError("zero-sink worker defect")
p = _EventPipeline(writer=broken, batch_size=1)
e = Event(datetime.now(UTC), None, None, None, "test", "test", "test", "log", "log", "info", "test", None)
p.enqueue(e)
assert p._finished.wait(1)
try:
    p.stop(timeout=1)
except RuntimeError as exc:
    assert str(exc) == "zero-sink worker defect"
else:
    raise AssertionError("worker failure was falsely acknowledged")
"""
    child = proc.run_bounded(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert child.returncode == 0, child.stderr
    assert "Traceback (most recent call last)" in child.stderr
    assert "RuntimeError: zero-sink worker defect" in child.stderr
