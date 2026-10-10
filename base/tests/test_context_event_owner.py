"""Context-owned ordinary writers stay cold and retain finite-stop evidence."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

import pytest

from base.agents.context.clients import ClientSet
from base.telemetry.delivery.pipeline import EventPipeline
from base.telemetry.delivery.receipts import DrainStatus, Event


def event() -> Event:
    return Event(
        ts=datetime.now(UTC),
        trace_id=None,
        span_id=None,
        agent_id=None,
        machine="test",
        cluster="test",
        process="test",
        category="telemetry",
        event_name="sdk_call",
        level="info",
        source="system",
        target_agent_id=None,
    )


def test_unused_owner_sync_and_close_never_start_a_writer() -> None:
    def forbidden() -> EventPipeline:
        pytest.fail("quiet owner constructed an event writer")

    before = set(threading.enumerate())
    clients = ClientSet(pipeline_factory=forbidden)
    assert clients.sync_events().status is DrainStatus.COMPLETED
    clients.close()
    assert set(threading.enumerate()) == before


def test_unfinished_close_retains_the_actual_writer_until_late_completion() -> None:
    entered, release = threading.Event(), threading.Event()
    built: list[EventPipeline] = []
    written: list[Event] = []

    def writer(batch: list[Event]) -> None:
        entered.set()
        assert release.wait(3)
        written.extend(batch)

    def factory() -> EventPipeline:
        pipe = EventPipeline(writer=writer, batch_size=1)
        built.append(pipe)
        return pipe

    clients = ClientSet(pipeline_factory=factory)
    pipe = clients.event_pipeline()
    item = event()
    try:
        pipe.enqueue(item)
        assert entered.wait(1)
        started = time.monotonic()
        clients.close(pipeline_timeout=0.01)
        assert time.monotonic() - started < 0.5
        assert clients.event_pipeline() is pipe
        assert len(built) == 1
        release.set()
        clients.close(pipeline_timeout=1)
        assert written == [item]
        assert clients.event_pipeline() is not pipe
        assert len(built) == 2
    finally:
        release.set()
        clients.close(pipeline_timeout=1)
        for owned in built:
            assert owned.stop(timeout=1).status is DrainStatus.COMPLETED


def test_late_writer_error_retains_original_error_after_other_client_cleanup() -> None:
    entered, release = threading.Event(), threading.Event()
    original = RuntimeError("original event writer failure")

    def writer(batch: list[Event]) -> None:
        entered.set()
        assert release.wait(3)
        raise original

    pipe = EventPipeline(writer=writer, batch_size=1)
    clients = ClientSet(pipeline_factory=lambda: pipe)

    class OtherClient:
        closed = False

        def close(self) -> None:
            self.closed = True

    other = clients.get(OtherClient)
    try:
        assert clients.event_pipeline() is pipe
        pipe.enqueue(event())
        assert entered.wait(1)
        clients.close(pipeline_timeout=0.01)
        assert other.closed
        assert clients.event_pipeline() is pipe
        release.set()
        for _ in range(2):
            with pytest.raises(RuntimeError) as observed:
                clients.close(pipeline_timeout=1)
            assert observed.value is original
            assert clients.event_pipeline() is pipe
    finally:
        release.set()
        with pytest.raises(RuntimeError) as observed:
            pipe.stop(timeout=1)
        assert observed.value is original


def test_sync_observes_the_owned_writer_failure_without_reconstructing_it() -> None:
    original = ValueError("writer rejected the real event")

    def writer(batch: list[Event]) -> None:
        raise original

    built: list[EventPipeline] = []

    def factory() -> EventPipeline:
        pipe = EventPipeline(writer=writer, batch_size=1)
        built.append(pipe)
        return pipe

    clients = ClientSet(pipeline_factory=factory)
    pipe = clients.event_pipeline()
    try:
        pipe.enqueue(event())
        with pytest.raises(ValueError) as observed:
            clients.sync_events(timeout=1)
        assert observed.value is original
        assert clients.event_pipeline() is pipe
        assert built == [pipe]
    finally:
        with pytest.raises(ValueError):
            clients.close(pipeline_timeout=1)
