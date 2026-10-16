from __future__ import annotations

import sys
import threading
import time
from datetime import UTC, datetime

from base import telemetry
from base.log import logger
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import Event


def _event() -> Event:
    return Event(
        datetime.now(UTC),
        None,
        None,
        None,
        "test",
        "test",
        "test",
        "log",
        "log",
        "info",
        "test",
        None,
    )


def owned_writer() -> None:
    written: list[Event] = []
    prior = set(threading.enumerate())
    pipeline = telemetry.EventPipeline(writer=written.extend)
    try:
        telemetry.init_telemetry(
            process="owned-process", pipeline=pipeline, machine_reader=lambda: "owned-machine"
        )
        assert len(set(threading.enumerate()) - prior) == 1
        telemetry.emit("log", "log", attributes={"owned": True})
        assert pipeline.sync(timeout=1).status is telemetry.DrainStatus.COMPLETED
        assert len(written) == 1
        assert written[0].machine == "owned-machine"
        assert written[0].process == "owned-process"
    finally:
        pipeline.stop(timeout=1)


def cold_emit() -> None:
    logger.add(sys.stderr, enqueue=False)

    def forbidden_thread(*args: object, **kwargs: object) -> None:
        raise AssertionError("emit must not construct a writer")

    threading.Thread = forbidden_thread
    telemetry.emit("log", "log", attributes={"cold": True})
    assert telemetry.flush().status is telemetry.DrainStatus.COMPLETED


def replace_live_owner() -> None:
    first = telemetry.EventPipeline(writer=lambda _batch: None)
    second = telemetry.EventPipeline(writer=lambda _batch: None)
    try:
        telemetry.init_telemetry(process="original-owner", pipeline=first)
        try:
            telemetry.init_telemetry(process="replacement", pipeline=second)
        except RuntimeError as exc:
            assert "another event pipeline" in str(exc)
        else:
            raise AssertionError("a live process writer was replaced")
        assert telemetry.process_name() == "original-owner"
    finally:
        first.stop(timeout=1)
        second.stop(timeout=1)


def unreleasable_writer() -> None:
    logger.add(sys.stderr)
    entered = threading.Event()

    def blocked(batch: list[Event]) -> None:
        entered.set()
        threading.Event().wait()

    pipe = EventPipeline(writer=blocked, batch_size=1, queue_maxsize=1)
    event = _event()
    pipe.enqueue(event)
    assert entered.wait(1)
    pipe.enqueue(event)
    started = time.monotonic()
    assert pipe.stop(timeout=0.03).status is telemetry.DrainStatus.UNFINISHED
    assert time.monotonic() - started < 0.5


def zero_sink_failure() -> None:
    logger.remove()

    def broken(batch: list[Event]) -> None:
        raise RuntimeError("zero-sink worker defect")

    pipe = EventPipeline(writer=broken, batch_size=1)
    pipe.enqueue(_event())
    assert pipe._finished.wait(1)
    try:
        pipe.stop(timeout=1)
    except RuntimeError as exc:
        assert str(exc) == "zero-sink worker defect"
    else:
        raise AssertionError("worker failure was falsely acknowledged")


def reuse_joined_owner() -> None:
    first = telemetry.EventPipeline(writer=lambda _batch: None)
    second = telemetry.EventPipeline(writer=lambda _batch: None)
    try:
        telemetry.init_telemetry(process="first", pipeline=first)
        assert not first.stopped
        assert first.stop(timeout=1).status is telemetry.DrainStatus.COMPLETED
        assert first.stopped
        telemetry.init_telemetry(process="second", pipeline=second)
        assert telemetry.process_name() == "second"
        assert telemetry.sync(timeout=1).status is telemetry.DrainStatus.COMPLETED
    finally:
        first.stop(timeout=1)
        second.stop(timeout=1)


def collect_failed_owner() -> None:
    error = ValueError("original writer failure")

    def fail(batch: list[Event]) -> None:
        raise error

    first = telemetry.EventPipeline(writer=fail, batch_size=1)
    second = telemetry.EventPipeline(writer=lambda _batch: None)
    telemetry.init_telemetry(process="first", pipeline=first)
    telemetry.emit("log", "log")
    try:
        first.stop(timeout=1)
    except ValueError as caught:
        assert caught is error
    assert first.stopped
    try:
        telemetry.init_telemetry(process="second", pipeline=second)
    except ValueError as caught:
        assert caught is error
    else:
        raise AssertionError("a failed writer was silently replaced")
    assert telemetry.process_name() == "first"
    second.stop(timeout=1)


SCENARIOS = {
    "owned_writer": owned_writer,
    "cold_emit": cold_emit,
    "replace_live_owner": replace_live_owner,
    "unreleasable_writer": unreleasable_writer,
    "zero_sink_failure": zero_sink_failure,
    "reuse_joined_owner": reuse_joined_owner,
    "collect_failed_owner": collect_failed_owner,
}

if __name__ == "__main__":
    SCENARIOS[sys.argv[1]]()
